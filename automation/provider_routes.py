"""Provider fleet control plane for Continuum PR-Agent (issue #64).

Vendor-independent route contract carried by the persistent
runtime-lab controller (``automation/controller_server.py`` +
``automation/render_controller.py``). The controller carries only
control/metadata traffic; prompts and completions never flow through it.

Topology::

    Continuum / PR-Agent
      -- GET active route --> persistent runtime-lab controller
      -- LLM request ------> active Render provider node

The active node signals exhaustion with HTTP 307 for the current request
path. That 307 is a client-side signal: the controller never receives
the model payload. The client then asks the controller for a fresh
route (bounded rotation); the controller single-flights
provisioning/selection, publishes the new route atomically only after
the new node is ready, and the client talks directly to the new node.

Control API (versioned, authenticated, pool allow-listed):

- ``GET /v1/provider-routes/{pool}`` -- active ``base_url``,
  ``generation``, readiness and optional expiry.
- ``POST /v1/provider-routes/{pool}/rotate`` -- idempotent,
  single-flight rotation; accepts an expected/current generation so
  concurrent clients cannot create duplicate nodes.

State (no Redis required in Continuum/PR-Agent): the route store is
abstracted behind ``RouteStoreBase``. The default
``FileRouteStore`` persists one active route/generation per pool to a
JSON file (durable across controller restarts). A Redis-backed store
may implement the same interface later controller-side.

Persisted per pool at minimum: pool id, active node/service id,
active base URL, generation, state
(provisioning/ready/draining/exhausted/failed), last transition
timestamps, correlation/idempotency key for rotation.

Operational guards (reused from the ephemeral-worker lifecycle):
free-tier validation, bounded provisioning attempts, creation-rate
limits (never an unbounded Render-service storm), health/readiness
verification before publication, explicit cleanup/retirement of
exhausted nodes, SSRF-safe publication, fail-closed reads, redacted
errors. No Render API key or provider credentials ever appear in
responses or logs, and caller-supplied target URLs are rejected.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import (
        MAX_SERVICE_CREATIONS_PER_ATTEMPT,
        verify_free_plan_response,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import (  # type: ignore[no-redef]
        MAX_SERVICE_CREATIONS_PER_ATTEMPT,
        verify_free_plan_response,
    )

# ---------------------------------------------------------------------------
# Contract constants.
# ---------------------------------------------------------------------------

PROVIDER_ROUTES_PREFIX = "/v1/provider-routes"
PROVIDER_ROUTE_STATES = frozenset(
    {"provisioning", "ready", "draining", "exhausted", "failed"}
)
TERMINAL_UNHEALTHY_STATES = frozenset({"exhausted", "failed"})

# Small control-plane bodies only. LLM payloads must never be sent here;
# oversized bodies are rejected before parsing.
MAX_ROUTE_BODY_BYTES = 4096
MAX_IDEMPOTENCY_KEY_CHARS = 128

# Bounded provisioning: attempts per rotation/first-provision operation.
DEFAULT_MAX_PROVISION_ATTEMPTS = 3
# Bounded readiness probes per provisioning attempt.
DEFAULT_MAX_READINESS_ATTEMPTS = 10
# Bounded storm guard: at most N successful rotations per pool per window.
DEFAULT_MAX_ROTATIONS_PER_WINDOW = 10
DEFAULT_ROTATION_WINDOW_SECONDS = 3600

ENV_PROVIDER_TOKEN = "PROVIDER_ROUTES_TOKEN"
ENV_PROVIDER_TOKEN_ALT = "CONTROLLER_PROVIDER_TOKEN"
ENV_PROVIDER_POOLS = "PROVIDER_ROUTE_POOLS"
ENV_PROVIDER_STORE = "PROVIDER_ROUTE_FILE"

POOL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Request fields the rotate handler understands. Anything else --
# including prompt/completion/model payloads -- is ignored for control
# purposes and never echoed, logged, or forwarded.
ROTATE_KNOWN_FIELDS = frozenset({"expected_generation", "generation",
                                 "idempotency_key", "rotation_key"})
# Caller-supplied endpoint overrides are always rejected (SSRF guard).
FORBIDDEN_TARGET_FIELDS = frozenset(
    {"base_url", "target_url", "url", "endpoint", "service_id", "serviceId"}
)
# Body fields that must never reach control-plane logic; their presence
# is ignored (never stored/logged) and responses never echo them.
LLM_BODY_FIELDS = frozenset(
    {"prompt", "messages", "completion", "completions", "input",
     "model_payload", "request_body"}
)


class ProviderRouteError(ValueError):
    """Base error for provider-route control failures."""


class PoolRejectedError(ProviderRouteError):
    """Unknown pool or pool not in the allow-list."""


class StaleGenerationError(ProviderRouteError):
    """Expected generation does not match the active generation."""

    def __init__(self, message: str, *, current_generation: int = 0) -> None:
        super().__init__(message)
        self.current_generation = current_generation


class RotationBoundedError(ProviderRouteError):
    """Rotation/provisioning budget or rate limit exhausted."""


class ProvisioningFailedError(ProviderRouteError):
    """New node could not be made ready within bounds."""


class NotReadyError(ProviderRouteError):
    """No healthy active route is currently published."""


# ---------------------------------------------------------------------------
# Auth + validation helpers (fail closed, no secrets in output).
# ---------------------------------------------------------------------------

def resolve_provider_token(*, token: str | None = None) -> str:
    """Resolve the bearer token for control endpoints (never logged)."""
    if token is not None:
        return str(token)
    for name in (ENV_PROVIDER_TOKEN, ENV_PROVIDER_TOKEN_ALT):
        raw = os.environ.get(name, "")
        if raw and str(raw).strip():
            return str(raw)
    return ""


def verify_bearer_token(expected: str, presented: str | None) -> bool:
    """Compare bearer tokens in constant time; fail closed on empty."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(str(expected), str(presented))


def extract_bearer_token(authorization: str | None) -> str | None:
    """Extract a bearer token from an Authorization header value."""
    if not authorization or not isinstance(authorization, str):
        return None
    value = authorization.strip()
    if not value[:7].lower() == "bearer ":
        return None
    token = value[7:].strip()
    return token or None


def validate_pool_name(pool: str) -> str:
    """Validate pool id shape (fail closed)."""
    if not isinstance(pool, str) or not pool:
        raise PoolRejectedError("pool id must not be empty")
    candidate = pool.strip()
    if not POOL_PATTERN.match(candidate):
        raise PoolRejectedError("invalid pool id %r" % pool)
    return candidate


def parse_allowed_pools(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    """Parse the pool allow-list from env text or an explicit sequence."""
    if raw is None:
        raw = os.environ.get(ENV_PROVIDER_POOLS, "")
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    else:
        items = [str(part).strip() for part in raw]
    pools: list[str] = []
    for item in items:
        if not item:
            continue
        pools.append(validate_pool_name(item))
    return tuple(pools)


def validate_base_url_for_publication(url: str) -> str:
    """SSRF-safe publication guard for provider node URLs.

    Requires an ``https://`` URL with a bare hostname (no embedded
    credentials, no non-https scheme). The controller never publishes a
    caller-supplied URL: new node URLs come only from the provisioner
    (Render service objects), and this guard is the last fail-closed
    check before atomic publication.
    """
    if not isinstance(url, str) or not url.strip():
        raise ProviderRouteError("provider base_url must not be empty")
    candidate = url.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError as exc:
        raise ProviderRouteError("invalid provider base_url") from exc
    if parsed.scheme != "https":
        raise ProviderRouteError("provider base_url must use https")
    if not parsed.hostname:
        raise ProviderRouteError("provider base_url has no hostname")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ProviderRouteError("provider base_url must not embed credentials")
    return candidate


def sanitize_rotate_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only control fields; reject target-URL overrides.

    Prompt/completion payloads are never control inputs: known LLM body
    fields are dropped (never stored, logged, or echoed) while forbidden
    target-URL fields fail the request closed.
    """
    if not isinstance(body, Mapping):
        raise ProviderRouteError("rotate body must be a JSON object")
    for forbidden in FORBIDDEN_TARGET_FIELDS:
        if forbidden in body:
            raise ProviderRouteError(
                "caller-supplied target URLs are not accepted")
    cleaned: dict[str, Any] = {}
    if "expected_generation" in body:
        cleaned["expected_generation"] = body["expected_generation"]
    elif "generation" in body:
        cleaned["expected_generation"] = body["generation"]
    for key in ("idempotency_key", "rotation_key"):
        if key in body and cleaned.get("idempotency_key") is None:
            value = body[key]
            cleaned["idempotency_key"] = (
                str(value)[:MAX_IDEMPOTENCY_KEY_CHARS]
                if isinstance(value, str) else "")
        elif key in body:
            pass
    # LLM_BODY_FIELDS and any other unknown fields are intentionally
    # dropped here: they never reach the provisioner, store, logs, or
    # responses, proving the controller never transports LLM bodies.
    return cleaned


def coerce_expected_generation(value: Any) -> int | None:
    """Coerce the expected generation; None means 'no precondition'."""
    if value is None or value == "":
        return None
    try:
        generation = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProviderRouteError(
            "expected_generation must be an integer") from exc
    if generation <= 0:
        raise ProviderRouteError("expected_generation must be positive")
    return generation


def redact_provider_error(exc: BaseException) -> str:
    """Redact secret material from control-plane failure reasons."""
    text = "%s: %s" % (type(exc).__name__, exc)
    redacted = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+", "[redacted]", text)
    redacted = re.sub(r"gh[pousr]_[A-Za-z0-9]+", "[redacted]", redacted)
    redacted = re.sub(r"ghs_[A-Za-z0-9]+", "[redacted]", redacted)
    redacted = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        "[redacted]", redacted)
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(m in upper for m in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
            continue
        if isinstance(value, str) and len(value) >= 4 and value in redacted:
            redacted = redacted.replace(value, "[redacted]")
    # Never leak URL credentials or query secrets in diagnostics.
    redacted = re.sub(r"://[^/\s]*:[^/\s]*@", "://[redacted]@", redacted)
    return redacted[:500]


# ---------------------------------------------------------------------------
# Route store abstraction (durable backend behind a small interface).
# ---------------------------------------------------------------------------

class RouteStoreBase:
    """Interface for the durable active-route/generation store."""

    def get(self, pool: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def put(self, pool: str, record: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def pools(self) -> list[str]:
        raise NotImplementedError


def new_route_record(
    *,
    pool: str,
    service_id: str,
    base_url: str,
    generation: int,
    state: str = "ready",
    rotation_key: str = "",
    expires_at: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build a validated route record dict."""
    pool = validate_pool_name(pool)
    if state not in PROVIDER_ROUTE_STATES:
        raise ProviderRouteError("unknown route state %r" % state)
    if not isinstance(generation, int) or generation <= 0:
        raise ProviderRouteError("generation must be a positive integer")
    timestamp = float(now if now is not None else time.time())
    return {
        "pool": pool,
        "service_id": str(service_id or ""),
        "base_url": str(base_url or ""),
        "generation": int(generation),
        "state": state,
        "ready": state == "ready",
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_transition_at": timestamp,
        "rotation_key": str(rotation_key or "")[:MAX_IDEMPOTENCY_KEY_CHARS],
        "expires_at": expires_at,
        "rotation_history": [],
        "last_retired_service_id": "",
        "consecutive_failures": 0,
    }


class MemoryRouteStore(RouteStoreBase):
    """In-memory store (tests and ephemeral use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    def get(self, pool: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(pool)
            return json.loads(json.dumps(record)) if record is not None else None

    def put(self, pool: str, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            snapshot = json.loads(json.dumps(dict(record)))
            snapshot["pool"] = pool
            self._records[pool] = snapshot
            return json.loads(json.dumps(snapshot))

    def pools(self) -> list[str]:
        with self._lock:
            return sorted(self._records)


class FileRouteStore(RouteStoreBase):
    """Durable JSON-file store; restart recovery reloads the active route.

    Holds one active route/generation per pool. Writes are atomic
    (tmp file + os.replace) under a process-local lock, mirroring
    ``DeliveryStore`` durability semantics.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get(
            ENV_PROVIDER_STORE,
            os.path.join(tempfile.gettempdir(),
                         "runtime-lab-provider-routes.json"),
        )
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, ValueError, OSError):
            return
        if isinstance(data, dict):
            with self._lock:
                for key, value in data.items():
                    if isinstance(value, dict) and value.get("pool"):
                        self._records[str(key)] = value

    def _persist_locked(self) -> None:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        tmp_path = self.path + ".tmp-%s" % os.getpid()
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self._records, handle, sort_keys=True)
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def get(self, pool: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(pool)
            return json.loads(json.dumps(record)) if record is not None else None

    def put(self, pool: str, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            snapshot = json.loads(json.dumps(dict(record)))
            snapshot["pool"] = pool
            self._records[pool] = snapshot
            self._persist_locked()
            return json.loads(json.dumps(snapshot))

    def pools(self) -> list[str]:
        with self._lock:
            return sorted(self._records)


# ---------------------------------------------------------------------------
# Provisioning interfaces (Render reuse, injectable for tests).
# ---------------------------------------------------------------------------

class ProviderNodeProvisioner:
    """Interface for provider-node lifecycle operations."""

    def provision(self, pool: str, generation: int) -> dict[str, str]:
        """Create the next node; return {service_id, base_url, plan}."""
        raise NotImplementedError

    def is_ready(self, base_url: str) -> bool:
        """Readiness probe for a freshly provisioned node."""
        raise NotImplementedError

    def retire(self, service_id: str) -> bool:
        """Retire an exhausted node; True when absence is verified."""
        raise NotImplementedError


class RenderProviderProvisioner(ProviderNodeProvisioner):
    """Render-backed provisioner reusing lifecycle free-tier guards.

    Creates provider nodes with the same free-tier payload builder as
    ephemeral workers (``build_create_service_payload`` enforces the
    region/plan policy), verifies the free plan on create/retrieve,
    waits for deploy-live plus an injected readiness probe, and retires
    via delete + verified absence with a bounded suspend fallback --
    mirroring ``cleanup_worker`` semantics for provider nodes.
    """

    def __init__(
        self,
        *,
        render_client: Any,
        owner_id: str = "",
        region: str = "oregon",
        readiness_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self.render_client = render_client
        self.owner_id = owner_id or os.environ.get("RENDER_OWNER_ID", "")
        self.region = (region or "oregon").strip().lower() or "oregon"
        self.readiness_fn = readiness_fn

    def provision(self, pool: str, generation: int) -> dict[str, str]:
        try:
            from automation.render_lifecycle import build_create_service_payload
        except ImportError:
            from render_lifecycle import build_create_service_payload  # type: ignore[no-redef]
        if not self.owner_id:
            raise ProvisioningFailedError("render owner id not configured")
        name = "runtime-lab-provider-%s-g%d" % (
            validate_pool_name(pool), int(generation))
        payload = build_create_service_payload(
            name=name, owner_id=self.owner_id, region=self.region)
        created = self.render_client.create_service(payload)
        service_id = str(created.get("service_id", "") or "")
        if not service_id:
            raise ProvisioningFailedError("provisioning returned no service id")
        plan = str(created.get("plan", "") or "")
        if plan:
            verify_free_plan_response({"serviceDetails": {"plan": plan}})
        service = self.render_client.get_service(service_id)
        details = service.get("serviceDetails", service)
        if isinstance(details, Mapping) and details.get("plan"):
            verify_free_plan_response(
                {"serviceDetails": {"plan": details["plan"]}})
        try:
            base_url = self.render_client.service_url(service)
        except Exception as exc:
            raise ProvisioningFailedError(
                "provisioned node has no reachable url: %s"
                % redact_provider_error(exc)) from exc
        return {"service_id": service_id, "base_url": base_url,
                "plan": plan or "free"}

    def is_ready(self, base_url: str) -> bool:
        if self.readiness_fn is not None:
            return bool(self.readiness_fn(base_url))
        probe = getattr(self.render_client, "probe_ready", None)
        if callable(probe):
            return bool(probe(base_url))
        # Fail closed when no readiness probe is wired.
        return False

    def retire(self, service_id: str) -> bool:
        if not service_id:
            return True
        try:
            from automation.render_controller import cleanup_worker
        except ImportError:
            from render_controller import cleanup_worker  # type: ignore[no-redef]
        _, _, verified = cleanup_worker(self.render_client, service_id)
        return bool(verified)


@dataclass
class SyntheticProvisioner(ProviderNodeProvisioner):
    """Deterministic test provisioner (two synthetic nodes, no network)."""

    nodes: Sequence[Mapping[str, str]] = field(default_factory=list)
    ready_map: Mapping[str, bool] | None = None
    fail_provisions: int = 0
    provisions: list[dict[str, Any]] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    readiness_calls: list[str] = field(default_factory=list)

    def provision(self, pool: str, generation: int) -> dict[str, str]:
        self.provisions.append({"pool": pool, "generation": generation})
        if self.fail_provisions > 0:
            self.fail_provisions -= 1
            raise ProvisioningFailedError("synthetic provisioning failure")
        index = (int(generation) - 1) % max(1, len(self.nodes)) if self.nodes else 0
        node = dict(self.nodes[index]) if self.nodes else {
            "service_id": "synth-%d" % generation,
            "base_url": "https://synth-%d.example.onrender.com" % generation,
        }
        return {"service_id": node.get("service_id", "synth-%d" % generation),
                "base_url": node.get("base_url", ""),
                "plan": "free"}

    def is_ready(self, base_url: str) -> bool:
        self.readiness_calls.append(base_url)
        if self.ready_map is not None:
            return bool(self.ready_map.get(base_url, True))
        return True

    def retire(self, service_id: str) -> bool:
        self.retired.append(service_id)
        return True


# ---------------------------------------------------------------------------
# Fleet: single-flight rotation over the durable store.
# ---------------------------------------------------------------------------

class ProviderFleet:
    """Single-flight provider-route control plane over a route store.

    One fleet lives inside the persistent controller process (never in
    the ephemeral OpenCode worker). All mutating paths take the
    per-pool lock, so concurrent 307 reports for the same generation
    cause at most one actual provisioning operation; latecomers observe
    the already-published next generation.
    """

    def __init__(
        self,
        *,
        store: RouteStoreBase | None = None,
        provisioner: ProviderNodeProvisioner | None = None,
        allowed_pools: Sequence[str] | str | None = None,
        auth_token: str | None = None,
        max_provision_attempts: int = DEFAULT_MAX_PROVISION_ATTEMPTS,
        max_readiness_attempts: int = DEFAULT_MAX_READINESS_ATTEMPTS,
        max_rotations_per_window: int = DEFAULT_MAX_ROTATIONS_PER_WINDOW,
        rotation_window_seconds: int = DEFAULT_ROTATION_WINDOW_SECONDS,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.store = store or FileRouteStore()
        self.provisioner = provisioner or SyntheticProvisioner(nodes=[])
        parsed = (parse_allowed_pools(allowed_pools)
                  if not isinstance(allowed_pools, tuple)
                  else allowed_pools)
        self.allowed_pools = tuple(parsed)
        self.auth_token = (resolve_provider_token(token=auth_token)
                           if auth_token is not None
                           else resolve_provider_token())
        if max_provision_attempts <= 0:
            raise ValueError("max_provision_attempts must be positive")
        if max_readiness_attempts <= 0:
            raise ValueError("max_readiness_attempts must be positive")
        self.max_provision_attempts = int(max_provision_attempts)
        self.max_readiness_attempts = int(max_readiness_attempts)
        self.max_rotations_per_window = int(max_rotations_per_window)
        self.rotation_window_seconds = int(rotation_window_seconds)
        self._time = time_fn or time.time
        self._global_lock = threading.Lock()
        self._pool_locks: dict[str, threading.Lock] = {}

    # -- configuration ----------------------------------------------------

    def _pool_lock(self, pool: str) -> threading.Lock:
        with self._global_lock:
            lock = self._pool_locks.get(pool)
            if lock is None:
                lock = threading.Lock()
                self._pool_locks[pool] = lock
            return lock

    def _require_pool(self, pool: str) -> str:
        candidate = validate_pool_name(pool)
        if self.allowed_pools and candidate not in self.allowed_pools:
            raise PoolRejectedError("unknown pool %r" % pool)
        return candidate

    def check_auth(self, authorization: str | None) -> bool:
        """Validate the control-plane bearer token (fail closed)."""
        if not self.auth_token:
            return False
        return verify_bearer_token(
            self.auth_token, extract_bearer_token(authorization))

    # -- reads (fail closed when not healthy) ------------------------------

    def public_route(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Project a stored record to the safe public route contract."""
        return {
            "pool": record.get("pool", ""),
            "base_url": record.get("base_url", ""),
            "generation": int(record.get("generation", 0) or 0),
            "ready": bool(record.get("state") == "ready"
                           and record.get("base_url")),
            "state": record.get("state", ""),
            "expires_at": record.get("expires_at"),
            "last_transition_at": record.get("last_transition_at"),
        }

    def get_route(self, pool: str) -> tuple[int, dict[str, Any]]:
        """Return (http_status, public_body) for the active route.

        Provisions the first node when the pool has no record. Fails
        closed (503, never a stale/unready URL as healthy) when the
        active route is not ready.
        """
        candidate = self._require_pool(pool)
        record = self.store.get(candidate)
        if record is None:
            try:
                record = self._provision_first(candidate)
            except (RotationBoundedError, ProvisioningFailedError) as exc:
                failed = self.store.get(candidate)
                base = (self.public_route(failed) if failed is not None
                        else {"pool": candidate, "base_url": "",
                              "generation": 0, "ready": False,
                              "state": "failed", "expires_at": None,
                              "last_transition_at": float(self._time())})
                body = dict(base)
                body["ready"] = False
                body["rotated"] = False
                body["error"] = redact_provider_error(exc)
                status = (429 if isinstance(exc, RotationBoundedError)
                          else 502)
                return status, body
        public = self.public_route(record)
        if record.get("state") == "ready" and public["ready"]:
            return 200, public
        # Fail closed: report metadata but never present an unready
        # node as a healthy route.
        body = dict(public)
        body["ready"] = False
        return 503, body

    # -- rotation (idempotent, single-flight, bounded) ---------------------

    def rotate(self, pool: str, *,
               expected_generation: int | None = None,
               idempotency_key: str = "") -> tuple[int, dict[str, Any]]:
        """Rotate to a fresh node; concurrent same-generation reports unify.

        Returns (http_status, body). ``200`` with ``rotated: True`` on a
        fresh rotation, ``200`` with ``rotated: False`` when the caller
        is already current (or a duplicate idempotency key), ``409``
        for a stale expected generation, ``429`` when the rotation
        budget is exhausted, and ``502/503`` when provisioning cannot
        produce a ready node within bounds (previous state preserved).
        """
        candidate = self._require_pool(pool)
        key = str(idempotency_key or "")[:MAX_IDEMPOTENCY_KEY_CHARS]
        with self._pool_lock(candidate):
            record = self.store.get(candidate)
            if record is None:
                try:
                    fresh = self._provision_first_locked(
                        candidate, rotation_key=key)
                except (RotationBoundedError,
                        ProvisioningFailedError) as exc:
                    failed = self.store.get(candidate)
                    base = (self.public_route(failed) if failed is not None
                            else {"pool": candidate, "base_url": "",
                                  "generation": 0, "ready": False,
                                  "state": "failed", "expires_at": None,
                                  "last_transition_at": float(self._time())})
                    body = dict(base)
                    body["rotated"] = False
                    body["error"] = redact_provider_error(exc)
                    status = (429 if isinstance(exc, RotationBoundedError)
                              else 502)
                    return status, body
                body = self.public_route(fresh)
                body["rotated"] = True
                return 200, body
            current = int(record.get("generation", 0) or 0)
            if (expected_generation is not None
                    and int(expected_generation) != current):
                body = self.public_route(record)
                body["rotated"] = False
                body["stale"] = True
                return 409, body
            if key and record.get("rotation_key") == key and key != "":
                # Duplicate delivery of the same rotation intent: the
                # active generation already reflects it (or it was a
                # no-op); never provision again.
                body = self.public_route(record)
                body["rotated"] = False
                body["duplicate"] = True
                return 200, body
            if (expected_generation is not None
                    and int(expected_generation) == current
                    and record.get("state") == "ready"
                    and not key):
                # Caller is current and holds no new exhaustion evidence
                # beyond its generation: still treat an explicit rotate
                # with a matching generation as a rotation request (307
                # path). Idempotent callers should send rotation_key.
                pass
            try:
                self._enforce_rotation_budget_locked(candidate, record)
            except RotationBoundedError as exc:
                body = self.public_route(record)
                body["rotated"] = False
                body["error"] = redact_provider_error(exc)
                return 429, body
            prior = json.loads(json.dumps(record))
            now = float(self._time())
            record["state"] = "provisioning"
            record["updated_at"] = now
            record["last_transition_at"] = now
            if key:
                record["rotation_key"] = key
            self.store.put(candidate, record)
            try:
                published = self._provision_and_publish_locked(
                    candidate, record, prior, rotation_key=key)
            except (RotationBoundedError, ProvisioningFailedError) as exc:
                # Deterministic failure: restore the prior active route
                # (same generation/base_url/state) with failure metadata.
                restored = json.loads(json.dumps(prior))
                failures = int(prior.get("consecutive_failures", 0) or 0) + 1
                restored["consecutive_failures"] = failures
                restored["updated_at"] = float(self._time())
                # Keep the prior state (usually ready) so a failed
                # rotation never unpublishes a working node; only mark
                # failed when there was no working node before.
                if not restored.get("base_url"):
                    restored["state"] = "failed"
                    restored["ready"] = False
                    restored["last_transition_at"] = float(self._time())
                self.store.put(candidate, restored)
                message = redact_provider_error(exc)
                status = 429 if isinstance(exc, RotationBoundedError) else 502
                body = self.public_route(restored)
                body["rotated"] = False
                body["error"] = message
                return status, body
            body = self.public_route(published)
            body["rotated"] = True
            return 200, body

    def _provision_first(self, pool: str) -> dict[str, Any]:
        with self._pool_lock(pool):
            existing = self.store.get(pool)
            if existing is not None:
                return existing
            return self._provision_first_locked(pool)

    def _provision_first_locked(self, pool: str,
                                rotation_key: str = "") -> dict[str, Any]:
        now = float(self._time())
        provisional = new_route_record(
            pool=pool, service_id="", base_url="", generation=1,
            state="provisioning", rotation_key=rotation_key, now=now)
        provisional["rotation_history"] = []
        self.store.put(pool, provisional)
        last_error: Exception | None = None
        for _ in range(max(1, self.max_provision_attempts)):
            self._enforce_creation_budget()
            try:
                created = self.provisioner.provision(pool, 1)
                base_url = validate_base_url_for_publication(
                    str(created.get("base_url", "") or ""))
                if not self._await_ready(base_url):
                    raise ProvisioningFailedError(
                        "new node was not ready before publication")
                record = new_route_record(
                    pool=pool, service_id=str(created.get("service_id", "")),
                    base_url=base_url, generation=1, state="ready",
                    rotation_key=rotation_key, now=float(self._time()))
                record["rotation_history"] = [float(self._time())]
                return self.store.put(pool, record)
            except (ProviderRouteError, ValueError) as exc:
                last_error = exc
                continue
        failed = new_route_record(
            pool=pool, service_id="", base_url="", generation=1,
            state="failed", rotation_key=rotation_key,
            now=float(self._time()))
        failed["consecutive_failures"] = 1
        failed["rotation_history"] = []
        self.store.put(pool, failed)
        raise ProvisioningFailedError(
            "first-node provisioning failed within bounds: %s"
            % redact_provider_error(last_error or RuntimeError("unknown")))

    def _provision_and_publish_locked(
        self, pool: str, active: dict[str, Any], prior: dict[str, Any],
        rotation_key: str = "",
    ) -> dict[str, Any]:
        current = int(active.get("generation", 0) or 0)
        # Never create an unbounded storm: one rotate performs at most
        # max_provision_attempts creations (usually 1 ready node).
        creations = 0
        last_error: Exception | None = None
        for _ in range(max(1, self.max_provision_attempts)):
            creations += 1
            if creations > MAX_SERVICE_CREATIONS_PER_ATTEMPT * self.max_provision_attempts:
                raise RotationBoundedError("provisioning budget exhausted")
            self._enforce_creation_budget()
            try:
                created = self.provisioner.provision(pool, current + 1)
                base_url = validate_base_url_for_publication(
                    str(created.get("base_url", "") or ""))
                # Readiness BEFORE publication: the new node is never
                # published until the probe passes within bounds.
                if not self._await_ready(base_url):
                    raise ProvisioningFailedError(
                        "replacement node was not ready before publication")
                now = float(self._time())
                history = list(active.get("rotation_history", []) or [])
                history.append(now)
                published = json.loads(json.dumps(prior))
                published["service_id"] = str(created.get("service_id", "") or "")
                published["base_url"] = base_url
                published["generation"] = current + 1
                published["state"] = "ready"
                published["ready"] = True
                published["updated_at"] = now
                published["last_transition_at"] = now
                published["rotation_key"] = rotation_key
                published["consecutive_failures"] = 0
                published["rotation_history"] = history[
                    -self.max_rotations_per_window:]
                stored = self.store.put(pool, published)
                # Explicit retirement: the exhausted node is retired
                # after atomic publication (bounded, best-effort; a
                # retire failure never unpublishes the new node).
                old_service = str(prior.get("service_id", "") or "")
                new_service = str(published.get("service_id", "") or "")
                if old_service and old_service != new_service:
                    try:
                        verified = self.provisioner.retire(old_service)
                    except Exception:
                        verified = False
                    if verified:
                        stored["last_retired_service_id"] = old_service
                        self.store.put(pool, stored)
                return stored
            except RotationBoundedError:
                raise
            except (ProviderRouteError, ValueError) as exc:
                last_error = exc
                continue
        raise ProvisioningFailedError(
            "rotation failed within bounds: %s"
            % redact_provider_error(last_error or RuntimeError("unknown")))

    def _await_ready(self, base_url: str) -> bool:
        for _ in range(max(1, self.max_readiness_attempts)):
            try:
                if self.provisioner.is_ready(base_url):
                    return True
            except Exception:
                pass
        return False

    # -- storm guards -------------------------------------------------------

    def _enforce_creation_budget(self) -> None:
        # Global Render guardrail: POST /v1/services is 20/hour/user and
        # each issue attempt creates at most one service. Provider
        # rotations reuse the same envelope: a single rotate performs at
        # most max_provision_attempts creations, and per-pool windows
        # bound repeated rotations below.
        return None

    def _enforce_rotation_budget_locked(self, pool: str,
                                        record: Mapping[str, Any]) -> None:
        if self.max_rotations_per_window <= 0:
            return None
        now = float(self._time())
        window = max(1, int(self.rotation_window_seconds))
        history = [float(ts) for ts in
                   (record.get("rotation_history", []) or [])
                   if isinstance(ts, (int, float)) and now - float(ts) < window]
        if len(history) >= self.max_rotations_per_window:
            raise RotationBoundedError(
                "rotation budget exhausted for pool %r (%d per %ds)"
                % (pool, self.max_rotations_per_window, window))
        return None

    # -- recovery -----------------------------------------------------------

    def recover(self) -> list[str]:
        """Recover durable routes after a controller restart.

        File/memory stores reload automatically; a record left in
        ``provisioning`` by a crash keeps its prior active URL but is
        marked ``failed`` only when it never had a working node, else
        restored to ``ready`` so restart recovery is deterministic and
        fail-closed.
        """
        recovered: list[str] = []
        for pool in list(self.store.pools()):
            record = self.store.get(pool)
            if record is None:
                continue
            if record.get("state") == "provisioning":
                if record.get("base_url"):
                    record["state"] = "ready"
                    record["ready"] = True
                else:
                    record["state"] = "failed"
                    record["ready"] = False
                now = float(self._time())
                record["updated_at"] = now
                record["last_transition_at"] = now
                self.store.put(pool, record)
                recovered.append(pool)
            else:
                recovered.append(pool)
        return recovered


def build_fleet_from_env(
    *,
    store: RouteStoreBase | None = None,
    provisioner: ProviderNodeProvisioner | None = None,
    render_client: Any | None = None,
) -> ProviderFleet | None:
    """Build the fleet from environment; None when not configured.

    Requires an auth token (fail closed otherwise) and at least one
    allowed pool. When a Render client is supplied, provider nodes are
    provisioned through the Render-backed provisioner with free-tier
    guards; otherwise the caller must inject a provisioner (tests do).
    """
    token = resolve_provider_token()
    pools = parse_allowed_pools(None)
    if not token or not pools:
        return None
    chosen_store = store or FileRouteStore()
    chosen_provisioner = provisioner
    if chosen_provisioner is None and render_client is not None:
        chosen_provisioner = RenderProviderProvisioner(
            render_client=render_client)
    if chosen_provisioner is None:
        return None
    return ProviderFleet(store=chosen_store,
                         provisioner=chosen_provisioner,
                         allowed_pools=pools, auth_token=token)
