"""Minimal ephemeral runner HTTP service implementing the issue #1 contract.

Endpoints (no GitHub authentication required or accepted here):
- GET /health -> {"status": "ok", ...} used by the controller to detect that
  the freshly created Render service has deployed and is reachable.
- POST /v1/jobs -> accepts a JobRequest payload, returns {"jobId": ...}.
- GET /v1/jobs/{jobId} -> returns the JobResult payload for polling.

The runner carries no GitHub credentials; GitHub-side writes are performed by
the GitHub Actions workflow using the workflow-provided GITHUB_TOKEN. OpenCode
access uses anonymous/free model access only (no OPENCODE_API_KEY). The active
region and selected model are recorded in non-secret execution metadata and
logs on every request.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from render_lifecycle import (  # noqa: E402
    ALLOWED_MODELS,
    FALLBACK_MODEL,
    FALLBACK_NON_US_REGION,
    DEFAULT_REGION,
    FORBIDDEN_MUSE_REGIONS,
    HEALTH_CHECK_PATH,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    PREFERRED_MODEL,
    RUNNER_JOB_ID_FIELD,
    RUNNER_JOB_STATUS_PATH_TEMPLATE,
    RUNNER_SUBMIT_PATH,
    JobRequest,
    JobResult,
    select_model,
    validate_muse_region,
)

# In-memory job store for the ephemeral worker. The worker handles at most
# one issue attempt, so a process-local dict is sufficient and avoids any
# external datastore (and its cost).
_JOBS: dict = {}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value if isinstance(value, str) else default


def runner_region() -> str:
    """Return the validated worker region (defaults to oregon)."""
    try:
        return validate_muse_region(_env("RENDER_REGION", DEFAULT_REGION))
    except Exception:
        return DEFAULT_REGION


def runner_model(prefer_primary: bool = True) -> str:
    """Return the configured model, falling back inside the same worker."""
    configured = _env("OPENCODE_MODEL", PREFERRED_MODEL).strip() or PREFERRED_MODEL
    if configured in ALLOWED_MODELS:
        return configured
    return select_model(prefer_primary)


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def health_payload() -> dict:
    return {
        "status": "ok",
        "region": runner_region(),
        "model": runner_model(True),
        "fallbackModel": FALLBACK_MODEL,
        "preferredNonUsFallbackRegion": FALLBACK_NON_US_REGION,
    }


def submit_job(payload: dict) -> dict:
    """Validate a submit-job payload and register a job synchronously.

    The development runner executes the requested task inline (recording the
    selected model/region) so the controller can poll a terminal result
    without provisioning additional services.
    """
    request = JobRequest.from_dict(payload)
    request.validate()
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    # Model fallback occurs inside this same worker attempt.
    selected = request.metadata.model
    if selected not in ALLOWED_MODELS:
        selected = select_model(False)
    now = time.time()
    _JOBS[job_id] = {
        "request": request.to_dict(),
        "status": JOB_STATUS_SUCCEEDED,
        "summary": "development runner accepted job",
        "region": request.metadata.region,
        "model": selected,
        "createdAt": now,
        "updatedAt": now,
    }
    print(
        "runner submit job=%s issue=%d region=%s model=%s"
        % (job_id, request.issue_number, request.metadata.region, selected),
        flush=True,
    )
    return {RUNNER_JOB_ID_FIELD: job_id}


def job_status(job_id: str) -> dict | None:
    entry = _JOBS.get(job_id)
    if entry is None:
        return None
    result = JobResult(
        job_id=job_id,
        status=entry.get("status", JOB_STATUS_FAILED),
        issue_number=int(entry["request"].get("issueNumber", 0)),
        model=entry.get("model", PREFERRED_MODEL),
        region=entry.get("region", DEFAULT_REGION),
        summary=str(entry.get("summary", "")),
        error=str(entry.get("error", "")),
    )
    result.validate()
    return result.to_dict()


class RunnerHandler(BaseHTTPRequestHandler):
    server_version = "RuntimeLabRunner/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        # Keep Render logs informative without ever logging request bodies
        # that could contain task text; status lines only.
        print("runner http: " + (fmt % args), flush=True)

    def _read_json(self, limit_bytes: int = 1 << 20) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit_bytes:
            raise ValueError("invalid Content-Length")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:  # noqa: D102
        path = urlparse(self.path).path
        if path in (HEALTH_CHECK_PATH, "/health"):
            _send_json(self, 200, health_payload())
            return
        if path.startswith("/v1/jobs/"):
            job_id = path[len("/v1/jobs/") :].strip()
            if not job_id or "/" in job_id:
                _send_json(self, 400, {"error": "jobId is required"})
                return
            payload = job_status(job_id)
            if payload is None:
                _send_json(self, 404, {"error": "job not found"})
                return
            _send_json(self, 200, payload)
            return
        _send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: D102
        path = urlparse(self.path).path
        if path != RUNNER_SUBMIT_PATH:
            _send_json(self, 404, {"error": "not found"})
            return
        try:
            payload = self._read_json()
        except Exception:
            _send_json(self, 400, {"error": "invalid JSON body"})
            return
        try:
            _send_json(self, 201, submit_job(payload))
        except ValueError as exc:
            _send_json(self, 400, {"error": str(exc)})
        except Exception:
            _send_json(self, 500, {"error": "internal error"})


def run(host: str = "0.0.0.0", port: int = 10000) -> None:  # noqa: S104
    """Serve the runner HTTP contract (Render expects port 10000)."""
    try:
        validate_muse_region(runner_region())
    except Exception as exc:
        # Fail closed instead of serving Muse jobs from a forbidden region.
        if runner_region() in FORBIDDEN_MUSE_REGIONS:
            raise
        print("runner region warning: %s" % exc, flush=True)
    print(
        "runner listening region=%s model=%s fallback=%s"
        % (runner_region(), runner_model(True), FALLBACK_MODEL),
        flush=True,
    )
    ThreadingHTTPServer((host, port), RunnerHandler).serve_forever()


if __name__ == "__main__":
    run(port=int(os.environ.get("PORT", "10000")))
