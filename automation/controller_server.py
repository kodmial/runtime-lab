"""Persistent Render controller HTTP service (issue #10).

Lightweight persistent service that directly receives GitHub webhooks and
owns dispatch of ephemeral Render workers. Intended to run on Render
independently of GitHub Actions; GitHub Actions is never used as a relay
in this path and OpenCode never executes inside this process (all
OpenCode work happens inside the ephemeral worker via its runner API).

Ingress contract:

- ``GET /health`` -- controller health/readiness for Render and operators.
- ``POST /webhooks/github`` (aliases ``/github/webhook``, ``/webhook``)
  -- GitHub webhook endpoint. Verifies ``X-Hub-Signature-256``, uses
  ``X-GitHub-Delivery`` as the idempotency key, durably accepts the
  delivery and acknowledges with a 2xx within 10 seconds even after a
  long idle period. Dispatch happens on background threads afterwards.
- ``GET /v1/deliveries`` / ``GET /v1/deliveries/{id}`` -- delivery
  inspection for recovery.
- ``POST /v1/deliveries/{id}/redeliver`` -- idempotent recovery for
  deliveries that failed before durable acceptance or before dispatch.

Availability invariant: a sleeping Render Free web service must not be
the sole webhook receiver. Deploy this controller with
``CONTROLLER_ALWAYS_ON=true`` (always-on-controller) or front it with an
independent always-on ingress/queue (``CONTROLLER_QUEUE_URL=...``,
queued-ingress). ``GET /health`` reports the deployment posture.

Stdlib only. Entrypoint: ``python -m automation.controller_server`` (the
Render start command for the persistent controller service).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_controller import (
        CONTROLLER_HEALTH_PATH,
        CONTROLLER_SERVICE_NAME,
        CONTROLLER_VERSION,
        DELIVERIES_PATH,
        WEBHOOK_ACK_BUDGET_SECONDS,
        WEBHOOK_PATH_ALIASES,
        Controller,
        DeliveryStore,
        StaticSnapshotProvider,
        format_correlation,
        validate_deployment,
    )
    from automation.render_lifecycle import MAX_CONCURRENT_AUTOMATION_JOBS
except ImportError:  # pytest inserts automation/ on sys.path
    from render_controller import (  # type: ignore[no-redef]
        CONTROLLER_HEALTH_PATH,
        CONTROLLER_SERVICE_NAME,
        CONTROLLER_VERSION,
        DELIVERIES_PATH,
        WEBHOOK_ACK_BUDGET_SECONDS,
        WEBHOOK_PATH_ALIASES,
        Controller,
        DeliveryStore,
        StaticSnapshotProvider,
        format_correlation,
        validate_deployment,
    )
    from render_lifecycle import (  # type: ignore[no-redef]
        MAX_CONCURRENT_AUTOMATION_JOBS,
    )

SERVICE_NAME = CONTROLLER_SERVICE_NAME
SERVICE_VERSION = CONTROLLER_VERSION

DEFAULT_PORT = 10000

ENV_PORT = "PORT"
ENV_WEBHOOK_SECRET = "GITHUB_WEBHOOK_SECRET"
ENV_MAX_CONCURRENT = "CONTROLLER_MAX_CONCURRENT"
ENV_OWNER_ID = "RENDER_OWNER_ID"

# Cap accepted webhook bodies (GitHub event payloads are small; anything
# larger is rejected before signature verification wastes time).
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def resolve_port(raw: str | None) -> int:
    """Resolve the HTTP port, preferring Render's $PORT."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PORT
    try:
        port = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("invalid port %r" % raw) from exc
    if not 1 <= port <= 65535:
        raise ValueError("port out of range: %d" % port)
    return port


def resolve_max_concurrent(raw: str | None) -> int:
    """Concurrent eligible issue executions (default 4, no global mutex)."""
    if raw is None or not str(raw).strip():
        return MAX_CONCURRENT_AUTOMATION_JOBS
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("invalid max concurrency %r" % raw) from exc
    if value <= 0:
        raise ValueError("max concurrency must be positive, got %r" % raw)
    return value


def build_controller_from_env(
    *,
    store: DeliveryStore | None = None,
    controller: Controller | None = None,
) -> Controller:
    """Build the controller from environment (no secrets logged)."""
    if controller is not None:
        return controller
    return Controller(
        webhook_secret=os.environ.get(ENV_WEBHOOK_SECRET, ""),
        store=store,
        provider=StaticSnapshotProvider(),
        render_client=None,  # wired by the deployment with Render credentials
        runner_client=None,
        owner_id=os.environ.get(ENV_OWNER_ID, ""),
        max_concurrent=resolve_max_concurrent(
            os.environ.get(ENV_MAX_CONCURRENT)),
    )


class ControllerHandler(BaseHTTPRequestHandler):
    """HTTP handler for the persistent controller. Set .controller first."""

    controller: Controller = None  # type: ignore[assignment]
    server_version = "RuntimeLabController/" + SERVICE_VERSION

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter test logs
        pass

    # -- helpers --------------------------------------------------------

    def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_raw_body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            count = int(length)
        except ValueError:
            return None
        if count < 0 or count > MAX_WEBHOOK_BODY_BYTES:
            return None
        if count == 0:
            return b""
        return self.rfile.read(count)

    def _path_only(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        controller = self.controller
        path = self._path_only()
        if path == CONTROLLER_HEALTH_PATH:
            if controller is None:
                self._send_json(503, {"status": "starting", "ready": False,
                                      "service": SERVICE_NAME})
                return
            deployment_ok, deployment_note = validate_deployment()
            self._send_json(200, {
                "status": "ok",
                "ready": True,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "deployment_pattern": controller.deployment_pattern,
                "deployment_ok": deployment_ok,
                "deployment_note": deployment_note,
                "webhook_ingress": {
                    "path": "/webhooks/github",
                    "ack_budget_seconds": WEBHOOK_ACK_BUDGET_SECONDS,
                    "durable": True,
                    "cold_start_safe": True,
                    "free_sleeping_service_as_sole_receiver": False,
                },
                "concurrency": {
                    "max_concurrent": controller.max_concurrent,
                    "in_flight": controller.in_flight(),
                    "global_single_job_mutex": False,
                },
                "queue": {
                    "pending": controller.queue_depth(),
                },
                "uptime_seconds": round(time.time() - getattr(
                    controller, "_started_at", time.time()), 1),
            })
            return
        if path == DELIVERIES_PATH or path == DELIVERIES_PATH + "/":
            if controller is None:
                self._send_json(503, {"error": "controller is not ready"})
                return
            self._send_json(200, {"deliveries": controller.store.list()})
            return
        if path.startswith(DELIVERIES_PATH + "/"):
            if controller is None:
                self._send_json(503, {"error": "controller is not ready"})
                return
            remainder = path[len(DELIVERIES_PATH + "/"):]
            delivery_id = urllib.parse.unquote(remainder).strip()
            if not delivery_id or "/" in delivery_id:
                self._send_json(404, {"error": "unknown delivery"})
                return
            record = controller.store.get(delivery_id)
            if record is None:
                self._send_json(404, {"error": "unknown delivery %r" % delivery_id})
                return
            self._send_json(200, record)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        controller = self.controller
        path = self._path_only()
        if path in WEBHOOK_PATH_ALIASES:
            if controller is None:
                self._send_json(503, {"error": "controller is not ready"})
                return
            started = time.time()
            raw = self._read_raw_body()
            if raw is None:
                self._send_json(400, {"error": "request body must be valid JSON "
                                               "within %d bytes" % MAX_WEBHOOK_BODY_BYTES})
                return
            # Fast ack path: signature check + duplicate check + one durable
            # append only. No Render/GitHub network I/O happens before the
            # 2xx, so the ack budget holds whenever this process is up
            # (hence the always-on deployment requirement).
            status, response = controller.ingest(
                headers=dict(self.headers), body=raw)
            elapsed = time.time() - started
            response = dict(response)
            response["ack_seconds"] = round(elapsed, 3)
            response["ack_budget_seconds"] = WEBHOOK_ACK_BUDGET_SECONDS
            self._send_json(status, response)
            # Dispatch asynchronously only for newly accepted deliveries;
            # duplicates and rejections never spawn work.
            if status == 202:
                delivery_id = str(response.get("delivery_id", "") or "")
                if delivery_id:
                    thread = threading.Thread(
                        target=self._process_in_background,
                        args=(delivery_id,), daemon=True)
                    thread.start()
            return
        if path.startswith(DELIVERIES_PATH + "/") and path.endswith("/redeliver"):
            if controller is None:
                self._send_json(503, {"error": "controller is not ready"})
                return
            remainder = path[len(DELIVERIES_PATH + "/"): -len("/redeliver")]
            delivery_id = urllib.parse.unquote(remainder).strip().rstrip("/")
            if not delivery_id or "/" in delivery_id:
                self._send_json(404, {"error": "unknown delivery"})
                return
            status, response = controller.redeliver(delivery_id)
            self._send_json(status, response)
            if status == 202:
                thread = threading.Thread(
                    target=self._process_in_background,
                    args=(delivery_id,), daemon=True)
                thread.start()
            return
        self._send_json(404, {"error": "not found"})

    def _process_in_background(self, delivery_id: str) -> None:
        controller = self.controller
        if controller is None:
            return
        try:
            outcome = controller.process_delivery(delivery_id)
            correlation = format_correlation(
                delivery_id=delivery_id,
                issue_number=int(outcome.get("issue_number", 0) or 0),
                worker_service_id=str(outcome.get("worker_service_id", "") or ""),
                job_id=str(outcome.get("job_id", "") or ""),
            )
            print("controller processed %s dispatched=%s reason=%s" % (
                correlation, outcome.get("dispatched", False),
                outcome.get("reason", "")), flush=True)
        except Exception as exc:  # background failures become failed records
            try:
                controller.store.update(delivery_id, status="failed",
                                        reason="background processing failed: %s" % exc)
            except Exception:
                pass

    # Only GET/POST are part of the contract.
    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method not allowed"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method not allowed"})

    def do_PATCH(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method not allowed"})


def create_server(
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    controller: Optional[Controller] = None,
) -> ThreadingHTTPServer:
    """Build the HTTP server; caller owns serve_forever/shutdown."""
    resolved = controller or build_controller_from_env()
    resolved._started_at = time.time()  # type: ignore[attr-defined]
    # Restart recovery: re-queue deliveries accepted but not yet terminal
    # so a restart between durable accept and dispatch cannot lose work.
    try:
        resolved.recover_pending()
    except Exception:
        pass
    handler = type("BoundControllerHandler", (ControllerHandler,),
                   {"controller": resolved})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    """Entrypoint for `python -m automation.controller_server`."""
    port = resolve_port(os.environ.get(ENV_PORT))
    controller = build_controller_from_env()
    deployment_ok, deployment_note = validate_deployment()
    print("controller deployment: %s" % deployment_note, flush=True)
    if not deployment_ok:
        print("WARNING: %s" % deployment_note, flush=True)
    server = create_server(port=port, controller=controller)
    print(
        "runtime-lab controller listening on 0.0.0.0:%d "
        "(deployment=%s max_concurrent=%d wip=%d)" % (
            port, controller.deployment_pattern,
            controller.max_concurrent, controller.wip_limit),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
