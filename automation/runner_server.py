"""Render-hosted ephemeral runner HTTP service (issues #2 and #3).

Minimal application that runs inside a temporary free Render web service and
accepts OpenCode jobs over HTTP.

Contract (authoritative, from issue #1 / automation/render_lifecycle.py):
  - GET  /health          -> readiness probe (Render healthCheckPath).
  - POST /v1/jobs         -> submit a job, returns quickly with a job ID.
  - GET  /v1/jobs/{jobId} -> job status / final result (self-contained: the
    full file-change set is embedded, so GitHub can collect everything
    before the ephemeral service is deleted).

Lifecycle properties implemented here:
  - Explicit job state machine: queued -> running -> succeeded/failed/timed_out.
  - Asynchronous execution: submit returns immediately, work happens on a
    background thread inside this service process.
  - Per-job isolated temporary workspaces (no shared working directory).
  - For each job (issue #3): clone the requested public repository into an
    isolated workspace subdirectory, checkout the exact requested base
    ref/SHA, invoke OpenCode non-interactively with the supplied task
    (``opencode run --auto --model <model> <task>``), capture exit code and
    sanitized output, and detect repository changes (added/modified/deleted)
    via ``git status`` with base64-embedded contents.
  - Deterministic terminal results: non-zero OpenCode exit -> failed,
    timeout -> timed_out, zero exit with no changes -> succeeded with an
    explicit empty change set. Every terminal result embeds the full change
    list so no workspace persistence is needed after service deletion.
  - In-worker model fallback: when the preferred Muse model fails for a
    model/provider availability reason, retry once with the Space Bunny
    fallback in the same worker/process lifecycle (never a second Render
    service) and record which model actually executed.
  - Configurable execution timeout (env RUNNER_JOB_TIMEOUT_SECONDS).
  - Idempotency: duplicate submits with the same idempotency/job key return
    the original job without starting a second execution.
  - No GitHub credentials required; no OPENCODE_API_KEY required. The
    runner never pushes branches or creates PRs; provider/model credentials
    stay entirely environment-driven and secrets are never logged.
  - Command-execution abstraction (CommandRunner) keeps git/OpenCode
    invocations injectable for tests. An explicit ``command`` field in the
    submit payload still selects the legacy single-command path (used by
    unit tests); production jobs without it run the OpenCode pipeline.
  - OpenCode CLI provisioning uses the known-good NanoDictate pattern
    (``curl -fsSL https://opencode.ai/install | bash`` with bounded
    retries; see automation/install-opencode.sh), without extra
    integrations or release-specific behavior.
  - Binds to Render's provided $PORT (falls back to 10000/8000 locally).
  - No durable state: jobs live in process memory, workspaces live under the
    system temp dir. The whole service is disposable and may be deleted
    immediately after the result is collected.
  - Health/readiness: /health returns HTTP 200 with {"ready": true} only when
    the service is ready to accept jobs; HTTP 503 with {"ready": false}
    otherwise, so GitHub can distinguish "deployed but not ready".

Model/region policy (from #1, enforced here before starting any command):
  - Default model is opencode/muse-spark-1.3-contributor-free, fallback is
    opencode/space-bunny-free. The model that actually executed is recorded
    in job metadata and in the result (executed_model).
  - Muse jobs are valid only in oregon, ohio, virginia, singapore.
    frankfurt is rejected before starting any command execution, and the
    region is re-asserted immediately before invoking Muse.
  - Model fallback reuses this same worker process; this service never
    creates another Render service.

Stdlib only: no third-party dependencies, so the Render free-tier build
(`pip install -r requirements.txt`) and the minimal CI image both work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional, Sequence

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import (
        ALLOWED_WORKER_REGIONS,
        FALLBACK_MODEL,
        FORBIDDEN_WORKER_REGIONS,
        PREFERRED_MODEL,
        PUBLIC_REPO_URL,
        RUNNER_HEALTH_PATH,
        RUNNER_JOB_STATUSES,
        RUNNER_JOB_STATUS_PATH_TEMPLATE,
        RUNNER_JOB_TIMEOUT_SECONDS,
        RUNNER_SUBMIT_JOB_PATH,
        RUNNER_TERMINAL_STATUSES,
        RegionPolicyError,
        is_terminal_job_status,
        render_path,
        validate_worker_region,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import (  # type: ignore[no-redef]
        ALLOWED_WORKER_REGIONS,
        FALLBACK_MODEL,
        FORBIDDEN_WORKER_REGIONS,
        PREFERRED_MODEL,
        PUBLIC_REPO_URL,
        RUNNER_HEALTH_PATH,
        RUNNER_JOB_STATUSES,
        RUNNER_JOB_STATUS_PATH_TEMPLATE,
        RUNNER_JOB_TIMEOUT_SECONDS,
        RUNNER_SUBMIT_JOB_PATH,
        RUNNER_TERMINAL_STATUSES,
        RegionPolicyError,
        is_terminal_job_status,
        render_path,
        validate_worker_region,
    )

try:  # pragma: no cover - import path depends on entrypoint
    from automation.opencode_runner import (
        CHECKOUT_SUBDIR,
        OPENCODE_CONFIG_CONTENT,
        OPENCODE_INSTALL_COMMAND,
        OPENCODE_INSTALL_MAX_ATTEMPTS,
        build_changes,
        build_checkout_command,
        build_clone_command,
        build_opencode_command,
        build_rev_parse_command,
        build_status_command,
        find_opencode_binary,
        is_model_unavailable_error,
        sanitize_output,
        summarize_changes,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from opencode_runner import (  # type: ignore[no-redef]
        CHECKOUT_SUBDIR,
        OPENCODE_CONFIG_CONTENT,
        OPENCODE_INSTALL_COMMAND,
        OPENCODE_INSTALL_MAX_ATTEMPTS,
        build_changes,
        build_checkout_command,
        build_clone_command,
        build_opencode_command,
        build_rev_parse_command,
        build_status_command,
        find_opencode_binary,
        is_model_unavailable_error,
        sanitize_output,
        summarize_changes,
    )

# ---------------------------------------------------------------------------
# Configuration (environment-driven; all optional, documented for Render).
# ---------------------------------------------------------------------------

SERVICE_NAME = "runtime-lab-runner"
SERVICE_VERSION = "issue3-1"

# Render injects PORT; default matches Render's local/dev convention.
DEFAULT_PORT = 10000
LOCAL_FALLBACK_PORT = 8000

ENV_PORT = "PORT"
ENV_JOB_TIMEOUT = "RUNNER_JOB_TIMEOUT_SECONDS"
ENV_WORKSPACE_ROOT = "RUNNER_WORKSPACE_ROOT"
ENV_REGION = "RENDER_REGION"  # also accept WORKER_REGION below
ENV_REGION_ALT = "WORKER_REGION"
ENV_DEFAULT_MODEL = "RUNNER_DEFAULT_MODEL"
ENV_OPENCODE_BIN = "RUNNER_OPENCODE_BIN"

ALLOWED_MODELS = frozenset({PREFERRED_MODEL, FALLBACK_MODEL})

# Default base ref when the submit payload omits one (public repo branch).
DEFAULT_BASE_REF = "main"

# Allowed state transitions for the explicit job state machine.
_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running"}),
    "running": frozenset({"succeeded", "failed", "timed_out"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "timed_out": frozenset(),
}

_MAX_OUTPUT_CHARS = 4000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


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


def resolve_job_timeout(raw: str | None) -> float:
    """Resolve the configurable execution timeout in seconds."""
    if raw is None or not str(raw).strip():
        return float(RUNNER_JOB_TIMEOUT_SECONDS)
    try:
        timeout = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError("invalid job timeout %r" % raw) from exc
    if timeout <= 0:
        raise ValueError("job timeout must be positive, got %r" % raw)
    return timeout


def resolve_region(raw: str | None) -> str:
    """Resolve this worker's region; defaults to oregon for local dev."""
    if raw is None or not str(raw).strip():
        return "oregon"
    return str(raw).strip().lower()


def resolve_default_model(raw: str | None) -> str:
    """Resolve the default model; never requires OPENCODE_API_KEY."""
    if raw is None or not str(raw).strip():
        return PREFERRED_MODEL
    model = str(raw).strip()
    if model not in ALLOWED_MODELS:
        raise ValueError("unknown default model: %r" % model)
    return model


# ---------------------------------------------------------------------------
# Command-execution abstraction (integration seam for issue #3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command execution inside a job workspace."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner:
    """Abstraction over command execution so OpenCode can plug in via #3."""

    def run(self, cmd: Sequence[str], cwd: str, timeout: float) -> CommandResult:
        """Execute cmd in cwd, enforcing timeout seconds."""
        raise NotImplementedError


class SubprocessCommandRunner(CommandRunner):
    """Default implementation based on subprocess.run (no shell)."""

    def run(self, cmd: Sequence[str], cwd: str, timeout: float) -> CommandResult:
        try:
            completed = subprocess.run(
                list(cmd),
                cwd=cwd,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(returncode=124, stdout=str(stdout), stderr=str(stderr), timed_out=True)
        except FileNotFoundError as exc:
            return CommandResult(returncode=127, stdout="", stderr="command not found: %s" % exc)
        except OSError as exc:
            return CommandResult(returncode=127, stdout="", stderr="execution failed: %s" % exc)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
        )


def default_command_for_job(payload: Mapping[str, Any], workspace: str) -> list[str]:
    """Build the legacy single command for a job (test seam).

    An explicit ``command`` field in the payload selects the legacy
    single-command path used by unit tests. Production OpenCode jobs carry
    no ``command`` field and run the full pipeline in JobManager._execute:
    clone the public repository, checkout the exact base ref/SHA, invoke
    ``opencode run --auto --model <model> <task>`` (see
    automation/opencode_runner.py), and embed the resulting file changes.
    The no-command default here is a deterministic synthetic command that
    succeeds without credentials.
    """
    _ = workspace  # workspace file is written by the manager before running.
    command = payload.get("command")
    if command is not None:
        if isinstance(command, str):
            if not command.strip():
                raise ValueError("command must not be empty")
            return ["sh", "-c", command]
        if isinstance(command, (list, tuple)) and command and all(
            isinstance(part, str) for part in command
        ):
            return list(command)
        raise ValueError("command must be a string or a non-empty list of strings")
    return ["sh", "-c", "echo synthetic-ok"]


def resolve_opencode_bin(raw: str | None) -> str:
    """Resolve the OpenCode binary (env override or PATH discovery)."""
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    found = find_opencode_binary()
    return found or "opencode"


# ---------------------------------------------------------------------------
# Job state machine + manager.
# ---------------------------------------------------------------------------


@dataclass
class JobRecord:
    """Mutable per-job record; transitions guarded by JobManager lock."""

    job_id: str
    status: str
    success: bool = False
    summary: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    workspace: str = ""
    idempotency_key: str = ""
    task_text: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    exit_code: Optional[int] = None
    # Issue #3 OpenCode result fields (self-contained for GitHub collection
    # before the ephemeral service is deleted; no workspace persistence).
    output: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)
    executed_model: str = ""
    repository_url: str = ""
    base_ref: str = ""
    base_sha: str = ""


def check_transition(old: str, new: str) -> None:
    """Validate one state-machine edge; raise ValueError on illegal jumps."""
    if old not in _JOB_TRANSITIONS:
        raise ValueError("unknown job status: %r" % old)
    if new not in RUNNER_JOB_STATUSES:
        raise ValueError("unknown job status: %r" % new)
    if new not in _JOB_TRANSITIONS[old]:
        raise ValueError("illegal job transition %r -> %r" % (old, new))


class JobManager:
    """Owns job lifecycle, workspaces, idempotency and timeouts."""

    def __init__(
        self,
        *,
        workspace_root: Optional[str] = None,
        job_timeout_seconds: float = float(RUNNER_JOB_TIMEOUT_SECONDS),
        command_runner: Optional[CommandRunner] = None,
        region: str = "oregon",
        default_model: str = PREFERRED_MODEL,
        command_builder: Callable[[Mapping[str, Any], str], Sequence[str]] = default_command_for_job,
        start_time: Optional[float] = None,
        opencode_bin: Optional[str] = None,
    ) -> None:
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be positive")
        if default_model not in ALLOWED_MODELS:
            raise ValueError("unknown default model: %r" % default_model)
        self.workspace_root = workspace_root or os.path.join(
            tempfile.gettempdir(), "runtime-lab-runner-workspaces"
        )
        os.makedirs(self.workspace_root, exist_ok=True)
        self.job_timeout_seconds = float(job_timeout_seconds)
        self.command_runner = command_runner or SubprocessCommandRunner()
        self.region = resolve_region(region)
        self.default_model = default_model
        self.command_builder = command_builder
        self.opencode_bin = resolve_opencode_bin(
            opencode_bin or os.environ.get(ENV_OPENCODE_BIN)
        )
        self.start_time = start_time if start_time is not None else time.time()
        self.ready = True
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[str, str] = {}
        # Confine every OpenCode subprocess to read-only git inspection.
        # Credentials stay inherited from the process environment (Render
        # env vars); only confinement settings are defaulted here so
        # concurrent jobs never race on global state.
        os.environ.setdefault("OPENCODE_CONFIG_CONTENT", OPENCODE_CONFIG_CONTENT)
        os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

    # -- introspection ----------------------------------------------------

    def job_counts(self) -> dict[str, int]:
        with self._lock:
            counts = {"total": len(self._jobs)}
            for status in sorted(RUNNER_JOB_STATUSES):
                counts[status] = sum(1 for job in self._jobs.values() if job.status == status)
            return counts

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_by_idempotency_key(self, key: str) -> Optional[JobRecord]:
        with self._lock:
            job_id = self._idempotency.get(key)
            if job_id is None:
                return None
            return self._jobs.get(job_id)

    # -- validation -------------------------------------------------------

    def _effective_region(self, payload: Mapping[str, Any]) -> str:
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("region"):
            return str(metadata["region"]).strip().lower()
        if payload.get("region"):
            return str(payload["region"]).strip().lower()
        return self.region

    def _effective_model(self, payload: Mapping[str, Any]) -> str:
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("model"):
            return str(metadata["model"]).strip()
        if payload.get("model"):
            return str(payload["model"]).strip()
        return self.default_model

    def _validate_submit_payload(self, payload: Mapping[str, Any]) -> tuple[str, int, str, str, float, str, str, str]:
        """Validate shape.

        Returns (task_text, issue_number, region, model, timeout,
        repository_url, base_ref, base_sha). repository_url defaults to the
        public repo; base_ref defaults to main; base_sha defaults to "".
        """
        if not isinstance(payload, Mapping):
            raise ValueError("job payload must be a JSON object")
        task_text = payload.get("task_text", "")
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("task_text must be a non-empty string")
        issue_number = payload.get("issue_number", 0)
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        repository_url = payload.get("repository_url", PUBLIC_REPO_URL)
        if repository_url is None:
            repository_url = PUBLIC_REPO_URL
        if not isinstance(repository_url, str) or repository_url != PUBLIC_REPO_URL:
            raise ValueError("repository_url must be %r in this phase" % PUBLIC_REPO_URL)
        base_ref = payload.get("base_ref", DEFAULT_BASE_REF)
        if base_ref is None:
            base_ref = DEFAULT_BASE_REF
        if not isinstance(base_ref, str) or not base_ref.strip():
            raise ValueError("base_ref must be a non-empty string when provided")
        base_ref = base_ref.strip()
        base_sha = payload.get("base_sha", "")
        if base_sha is None:
            base_sha = ""
        if not isinstance(base_sha, str):
            raise ValueError("base_sha must be a string when provided")
        base_sha = base_sha.strip()
        model = self._effective_model(payload)
        if model not in ALLOWED_MODELS:
            raise ValueError("unknown model: %r" % model)
        region = self._effective_region(payload)
        # Timeout: per-job override allowed but must be positive; server cap is
        # the configured default so a client cannot force unbounded execution.
        timeout_raw = payload.get("timeout_seconds", self.job_timeout_seconds)
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be a number") from exc
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        timeout = min(timeout, self.job_timeout_seconds)
        return task_text.strip(), issue_number, region, model, timeout, repository_url, base_ref, base_sha

    # -- submission --------------------------------------------------------

    def submit(
        self, payload: Mapping[str, Any], idempotency_key: str = ""
    ) -> tuple[JobRecord, bool]:
        """Submit a job; returns (record, created).

        Duplicate idempotency keys return the original record with
        created=False and never start a second execution.
        """
        key = (idempotency_key or "").strip()
        with self._lock:
            if key and key in self._idempotency:
                existing = self._jobs.get(self._idempotency[key])
                if existing is not None:
                    return existing, False

        task_text, issue_number, region, model, timeout, repository_url, base_ref, base_sha = self._validate_submit_payload(payload)

        # Region policy is enforced BEFORE any command execution. A forbidden
        # region produces a deterministic failed job; the command runner is
        # never invoked and no OpenCode process is started.
        try:
            validate_worker_region(region)
        except RegionPolicyError as exc:
            record = self._store_terminal_rejection(
                payload=payload,
                key=key,
                task_text=task_text,
                issue_number=issue_number,
                region=region,
                model=model,
                timeout=timeout,
                repository_url=repository_url,
                base_ref=base_ref,
                base_sha=base_sha,
                error="region %r is forbidden for Muse jobs; use one of %s (%s)"
                % (region, sorted(ALLOWED_WORKER_REGIONS), exc),
            )
            return record, True

        job_id = uuid.uuid4().hex
        workspace = tempfile.mkdtemp(
            prefix="job-%s-" % job_id[:8], dir=self.workspace_root
        )
        metadata = self._build_metadata(
            payload, issue_number, region, model, timeout,
            repository_url=repository_url, base_ref=base_ref, base_sha=base_sha,
        )
        now = time.time()
        record = JobRecord(
            job_id=job_id,
            status="queued",
            success=False,
            summary="",
            error="",
            metadata=metadata,
            workspace=workspace,
            idempotency_key=key,
            task_text=task_text,
            created_at=now,
            updated_at=now,
            repository_url=repository_url,
            base_ref=base_ref,
            base_sha=base_sha,
        )
        # Record the task inside the isolated workspace before execution so
        # isolation is observable even for the default synthetic command.
        try:
            with open(os.path.join(workspace, "task.txt"), "w", encoding="utf-8") as handle:
                handle.write(task_text)
        except OSError:
            pass
        with self._lock:
            # Re-check idempotency under lock: a concurrent duplicate submit
            # must not create a second job.
            if key and key in self._idempotency:
                existing = self._jobs.get(self._idempotency[key])
                if existing is not None:
                    shutil.rmtree(workspace, ignore_errors=True)
                    return existing, False
            self._jobs[job_id] = record
            if key:
                self._idempotency[key] = job_id

        thread = threading.Thread(
            target=self._execute, args=(job_id, dict(payload), timeout), daemon=True
        )
        thread.start()
        return record, True

    def _store_terminal_rejection(
        self,
        *,
        payload: Mapping[str, Any],
        key: str,
        task_text: str,
        issue_number: int,
        region: str,
        model: str,
        timeout: float,
        repository_url: str = "",
        base_ref: str = "",
        base_sha: str = "",
        error: str,
    ) -> JobRecord:
        job_id = uuid.uuid4().hex
        workspace = tempfile.mkdtemp(prefix="job-%s-" % job_id[:8], dir=self.workspace_root)
        metadata = self._build_metadata(payload, issue_number, region, model, timeout)
        now = time.time()
        record = JobRecord(
            job_id=job_id,
            status="failed",
            success=False,
            summary="",
            error=error,
            metadata=metadata,
            workspace=workspace,
            idempotency_key=key,
            task_text=task_text,
            created_at=now,
            updated_at=now,
            repository_url=repository_url or str(payload.get("repository_url", "")),
            base_ref=base_ref or str(payload.get("base_ref", "")),
            base_sha=base_sha or str(payload.get("base_sha", "")),
        )
        with self._lock:
            if key and key in self._idempotency:
                existing = self._jobs.get(self._idempotency[key])
                if existing is not None:
                    shutil.rmtree(workspace, ignore_errors=True)
                    return existing
            self._jobs[job_id] = record
            if key:
                self._idempotency[key] = job_id
        return record

    def _build_metadata(
        self,
        payload: Mapping[str, Any],
        issue_number: int,
        region: str,
        model: str,
        timeout: float,
        repository_url: str = "",
        base_ref: str = "",
        base_sha: str = "",
    ) -> dict[str, Any]:
        incoming = payload.get("metadata")
        metadata: dict[str, Any] = {}
        if isinstance(incoming, Mapping):
            for key in ("attempt", "run_id", "execution_mode"):
                if key in incoming:
                    metadata[key] = incoming[key]
        metadata.setdefault("attempt", 1)
        metadata.setdefault("run_id", "")
        metadata.setdefault("execution_mode", "e2e")
        metadata["issue_number"] = issue_number
        metadata["region"] = region
        metadata["model"] = model
        metadata["executed_model"] = model
        metadata["timeout_seconds"] = timeout
        metadata["service"] = SERVICE_NAME
        if repository_url:
            metadata["repository_url"] = repository_url
        if base_ref:
            metadata["base_ref"] = base_ref
        if base_sha:
            metadata["base_sha"] = base_sha
        return metadata

    # -- state machine ------------------------------------------------------

    def transition(self, job_id: str, new_status: str) -> JobRecord:
        """Apply one guarded state-machine edge and return the record."""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise KeyError("unknown job: %r" % job_id)
            check_transition(record.status, new_status)
            record.status = new_status
            record.updated_at = time.time()
            if new_status in RUNNER_TERMINAL_STATUSES:
                record.success = new_status == "succeeded"
            return record

    # -- background execution -------------------------------------------------

    def _uses_legacy_path(self, payload: Mapping[str, Any]) -> bool:
        """True when the payload/command seam selects the legacy path.

        An explicit ``command`` field (unit-test override) or a custom
        injected ``command_builder`` keeps the historical single-command
        behavior. Production OpenCode jobs carry neither and run the full
        clone/checkout/opencode/diff pipeline below.
        """
        if isinstance(payload, Mapping) and payload.get("command") is not None:
            return True
        return self.command_builder is not default_command_for_job

    def _execute(self, job_id: str, payload: dict[str, Any], timeout: float) -> None:
        try:
            self.transition(job_id, "running")
        except (KeyError, ValueError):
            return
        record = self.get(job_id)
        if record is None:
            return
        if self._uses_legacy_path(payload):
            self._execute_legacy(job_id, payload, timeout)
            return
        self._execute_opencode(job_id, payload, timeout)

    def _execute_legacy(self, job_id: str, payload: dict[str, Any], timeout: float) -> None:
        record = self.get(job_id)
        if record is None:
            return
        try:
            cmd = list(self.command_builder(payload, record.workspace))
        except Exception as exc:  # deterministic failure, never a crash
            self._finish(job_id, "failed", summary="", error="invalid command: %s" % exc)
            return
        try:
            result = self.command_runner.run(cmd, cwd=record.workspace, timeout=timeout)
        except Exception as exc:  # runner bugs also become failed jobs
            self._finish(job_id, "failed", summary="", error="execution failed: %s" % exc)
            return
        if result.timed_out:
            self._finish(
                job_id,
                "timed_out",
                summary="",
                error="job timed out after %.1f seconds" % timeout,
                exit_code=result.returncode,
            )
            return
        if result.returncode == 0:
            model = (self.get(job_id).metadata.get("model") if self.get(job_id) else self.default_model)
            summary = _truncate(result.stdout.strip() or "synthetic-ok")
            summary = "%s [model=%s]" % (summary, model)
            self._finish(job_id, "succeeded", summary=summary, error="", exit_code=0)
            return
        detail = _truncate((result.stderr.strip() or result.stdout.strip() or "command failed"))
        self._finish(
            job_id,
            "failed",
            summary="",
            error="command exited with code %d: %s" % (result.returncode, detail),
            exit_code=result.returncode,
        )

    # -- OpenCode pipeline (issue #3) ----------------------------------------

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - time.time())

    def _ensure_opencode_binary(self, cwd: str, timeout: float) -> str:
        """Resolve the OpenCode binary, provisioning with the known-good
        pattern when missing: ``curl -fsSL https://opencode.ai/install | bash``
        with bounded retries, then verify executability.

        An explicit RUNNER_OPENCODE_BIN/manager override is used verbatim
        (tests inject fakes this way) and skips provisioning.
        """
        override = (self.opencode_bin or "").strip()
        if override and override != "opencode":
            return override
        found = find_opencode_binary()
        if found:
            return found
        last_error = "opencode CLI not found"
        attempts = max(1, int(OPENCODE_INSTALL_MAX_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            try:
                result = self.command_runner.run(
                    ["sh", "-c", OPENCODE_INSTALL_COMMAND],
                    cwd=cwd,
                    timeout=timeout,
                )
            except Exception as exc:
                last_error = "opencode install failed: %s" % exc
                continue
            if result.timed_out:
                last_error = "opencode install timed out"
                break
            if result.returncode == 0:
                found = find_opencode_binary()
                if found:
                    return found
                last_error = "opencode installer succeeded but no binary found"
            else:
                detail = (result.stderr.strip() or result.stdout.strip() or "installer failed")
                last_error = "opencode install failed (code %d): %s" % (
                    result.returncode, sanitize_output(detail)[:500],
                )
        raise FileNotFoundError(
            "%s; provision via automation/install-opencode.sh "
            "(curl -fsSL https://opencode.ai/install | bash)" % last_error
        )

    def _execute_opencode(self, job_id: str, payload: dict[str, Any], timeout: float) -> None:
        """Clone, checkout, run OpenCode (with same-worker fallback), diff."""
        record = self.get(job_id)
        if record is None:
            return
        deadline = time.time() + max(0.1, float(timeout))
        task_text = record.task_text
        requested_model = str(record.metadata.get("model") or self.default_model)
        region = str(record.metadata.get("region") or self.region).lower()
        repository_url = record.repository_url or PUBLIC_REPO_URL
        base_ref = record.base_ref or DEFAULT_BASE_REF
        base_sha = record.base_sha or ""
        checkout_dir = os.path.join(record.workspace, CHECKOUT_SUBDIR)

        # Re-assert the Muse region immediately before any OpenCode work.
        try:
            validate_worker_region(region)
        except RegionPolicyError as exc:
            self._finish(
                job_id, "failed", summary="",
                error="region %r is forbidden for Muse jobs (%s)" % (region, exc),
                exit_code=None,
            )
            return

        def run(cmd: Sequence[str], cwd: str) -> Any:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise TimeoutError("job timed out")
            return self.command_runner.run(list(cmd), cwd=cwd, timeout=remaining)

        # -- clone the requested public repository (no credentials) ---------
        try:
            clone_cmd = build_clone_command(repository_url, checkout_dir)
        except ValueError as exc:
            self._finish(job_id, "failed", summary="", error="invalid repository: %s" % exc)
            return
        try:
            result = run(clone_cmd, record.workspace)
        except TimeoutError:
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=124)
            return
        except Exception as exc:
            self._finish(job_id, "failed", summary="",
                         error="execution failed: %s" % sanitize_output(str(exc))[:1000])
            return
        if result.timed_out:
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=result.returncode)
            return
        if result.returncode != 0:
            detail = sanitize_output(result.stderr.strip() or result.stdout.strip() or "clone failed")
            self._finish(job_id, "failed", summary="",
                         error="clone failed (code %d): %s" % (result.returncode, _truncate(detail)),
                         exit_code=result.returncode)
            return

        # -- checkout the exact requested base ref/SHA -----------------------
        checkout_ref = base_sha or base_ref
        try:
            checkout_cmd = build_checkout_command(checkout_ref)
        except ValueError as exc:
            self._finish(job_id, "failed", summary="", error="invalid base ref: %s" % exc)
            return
        try:
            result = run(checkout_cmd, checkout_dir)
        except TimeoutError:
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=124)
            return
        except Exception as exc:
            self._finish(job_id, "failed", summary="",
                         error="execution failed: %s" % sanitize_output(str(exc))[:1000])
            return
        if result.timed_out:
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=result.returncode)
            return
        if result.returncode != 0:
            detail = sanitize_output(result.stderr.strip() or result.stdout.strip() or "checkout failed")
            self._finish(job_id, "failed", summary="",
                         error="checkout of %r failed (code %d): %s"
                         % (checkout_ref, result.returncode, _truncate(detail)),
                         exit_code=result.returncode)
            return
        # Record the exact checked-out SHA; fail closed on mismatch.
        checked_sha = ""
        try:
            rev = run(build_rev_parse_command(), checkout_dir)
            if not rev.timed_out and rev.returncode == 0:
                checked_sha = sanitize_output(rev.stdout.strip())[:100]
        except (TimeoutError, Exception):
            checked_sha = ""
        if base_sha:
            expected = base_sha.strip()
            if checked_sha and checked_sha != expected and not checked_sha.startswith(expected):
                self._finish(job_id, "failed", summary="",
                             error="checkout mismatch: requested %r but HEAD is %r"
                             % (expected, checked_sha),
                             exit_code=None)
                return

        # -- provision/resolve the OpenCode CLI ------------------------------
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise TimeoutError("job timed out")
            opencode_bin = self._ensure_opencode_binary(record.workspace, remaining)
        except TimeoutError:
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=124)
            return
        except FileNotFoundError as exc:
            self._finish(job_id, "failed", summary="",
                         error="%s" % _truncate(sanitize_output(str(exc))),
                         exit_code=127)
            return
        except Exception as exc:
            self._finish(job_id, "failed", summary="",
                         error="execution failed: %s" % sanitize_output(str(exc))[:1000])
            return

        # -- invoke OpenCode (preferred, then one same-worker fallback) ------
        models_to_try = [requested_model]
        if requested_model == PREFERRED_MODEL:
            models_to_try.append(FALLBACK_MODEL)
        executed_model = requested_model
        last_output = ""
        last_exit: Optional[int] = None
        first_error = ""
        opencode_timed_out = False
        for attempt_index, model in enumerate(models_to_try):
            try:
                validate_worker_region(region)
            except RegionPolicyError as exc:
                self._finish(job_id, "failed", summary="",
                             error="region %r is forbidden for Muse jobs (%s)" % (region, exc))
                return
            try:
                cmd = build_opencode_command(model, task_text, opencode_bin=opencode_bin)
            except ValueError as exc:
                self._finish(job_id, "failed", summary="",
                             error="invalid command: %s" % exc)
                return
            try:
                result = run(cmd, checkout_dir)
            except TimeoutError:
                opencode_timed_out = True
                break
            except Exception as exc:
                self._finish(job_id, "failed", summary="",
                             error="execution failed: %s" % sanitize_output(str(exc))[:1000])
                return
            if result.timed_out:
                opencode_timed_out = True
                last_exit = result.returncode
                last_output = sanitize_output(
                    (result.stdout or "") + ("\n" if result.stdout or result.stderr else "") + (result.stderr or "")
                )
                break
            combined = sanitize_output(
                (result.stdout or "") + ("\n" if result.stdout or result.stderr else "") + (result.stderr or "")
            )
            last_output = combined
            last_exit = result.returncode
            executed_model = model
            if result.returncode == 0:
                first_error = ""
                break
            # Non-zero: retry once with the fallback on availability errors.
            if (
                attempt_index == 0
                and model == PREFERRED_MODEL
                and len(models_to_try) > 1
                and is_model_unavailable_error(combined)
            ):
                first_error = combined
                continue
            first_error = combined
            break
        else:
            executed_model = requested_model

        if opencode_timed_out:
            changes = self._best_effort_changes(checkout_dir)
            self._finish(
                job_id, "timed_out", summary="",
                error="job timed out after %.1f seconds" % timeout,
                exit_code=last_exit if last_exit is not None else 124,
                output=_truncate(last_output, _MAX_OUTPUT_CHARS),
                changes=changes, executed_model=executed_model,
            )
            return

        output = _truncate(last_output or "", _MAX_OUTPUT_CHARS)
        if last_exit not in (0, None) and last_exit != 0:
            # Non-zero OpenCode exit: failed, but still embed any partial
            # changes so the failure is debuggable from the result alone.
            changes = self._best_effort_changes(checkout_dir)
            detail = _truncate((last_output.strip() or "opencode failed"), _MAX_OUTPUT_CHARS)
            if first_error and executed_model == FALLBACK_MODEL and first_error != last_output:
                detail = _truncate(
                    "primary model %s unavailable; fallback %s also failed: %s"
                    % (requested_model, FALLBACK_MODEL, detail), _MAX_OUTPUT_CHARS,
                )
            self._finish(
                job_id, "failed", summary="",
                error="opencode exited with code %d: %s" % (last_exit, detail),
                exit_code=last_exit, output=output,
                changes=changes, executed_model=executed_model,
            )
            return

        # -- detect repository changes (added/modified/deleted) --------------
        try:
            result = run(build_status_command(), checkout_dir)
        except TimeoutError:
            changes = self._best_effort_changes(checkout_dir)
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=124, output=output,
                         changes=changes, executed_model=executed_model)
            return
        except Exception as exc:
            self._finish(job_id, "failed", summary="",
                         error="execution failed: %s" % sanitize_output(str(exc))[:1000],
                         exit_code=None, output=output,
                         changes=[], executed_model=executed_model)
            return
        if result.timed_out:
            changes = self._best_effort_changes(checkout_dir)
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=result.returncode, output=output,
                         changes=changes, executed_model=executed_model)
            return
        if result.returncode != 0:
            detail = sanitize_output(result.stderr.strip() or result.stdout.strip() or "status failed")
            self._finish(job_id, "failed", summary="",
                         error="change detection failed (code %d): %s"
                         % (result.returncode, _truncate(detail)),
                         exit_code=result.returncode, output=output,
                         changes=[], executed_model=executed_model)
            return
        try:
            changes = build_changes(result.stdout or "", checkout_dir)
        except ValueError as exc:
            self._finish(job_id, "failed", summary="",
                         error="change extraction failed: %s" % sanitize_output(str(exc))[:1000],
                         exit_code=None, output=output,
                         changes=[], executed_model=executed_model)
            return
        change_summary = summarize_changes(changes)
        if not changes:
            summary = _truncate("no changes [model=%s]" % executed_model)
        else:
            summary = _truncate("%s [model=%s]" % (change_summary, executed_model))
        self._finish(
            job_id, "succeeded", summary=summary, error="", exit_code=0,
            output=output, changes=changes, executed_model=executed_model,
        )

    def _best_effort_changes(self, checkout_dir: str) -> list[dict[str, Any]]:
        """Collect changes without failing the job on extraction errors."""
        try:
            status = self.command_runner.run(
                build_status_command(), cwd=checkout_dir, timeout=10.0,
            )
            if status.timed_out or status.returncode != 0:
                return []
            return list(build_changes(status.stdout or "", checkout_dir))
        except Exception:
            return []

    def _finish(
        self, job_id: str, status: str, summary: str, error: str, exit_code: Optional[int] = None,
        output: str = "", changes: Optional[list[dict[str, Any]]] = None,
        executed_model: str = "",
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            try:
                check_transition(record.status, status)
            except ValueError:
                return
            record.status = status
            record.success = status == "succeeded"
            record.summary = summary
            record.error = error
            record.exit_code = exit_code
            record.output = output or ""
            if changes is not None:
                record.changes = list(changes)
            if executed_model:
                record.executed_model = executed_model
                record.metadata["executed_model"] = executed_model
            record.updated_at = time.time()

    # -- serialization ---------------------------------------------------------

    def to_result_dict(self, record: JobRecord) -> dict[str, Any]:
        executed = record.executed_model or str(record.metadata.get("model", ""))
        return {
            "job_id": record.job_id,
            "status": record.status,
            "success": record.success,
            "summary": record.summary,
            "error": record.error,
            "metadata": dict(record.metadata),
            "exit_code": record.exit_code,
            "output": record.output,
            "changes": [dict(item) for item in record.changes],
            "executed_model": executed,
            "repository_url": record.repository_url,
            "base_ref": record.base_ref,
            "base_sha": record.base_sha,
        }


# ---------------------------------------------------------------------------
# HTTP layer.
# ---------------------------------------------------------------------------


def extract_idempotency_key(headers: Mapping[str, str], payload: Mapping[str, Any]) -> str:
    """Idempotency key from headers first, then body aliases."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in ("idempotency-key", "x-idempotency-key"):
        value = lowered.get(name)
        if value and str(value).strip():
            return str(value).strip()
    if isinstance(payload, Mapping):
        for field_name in ("idempotency_key", "job_key", "idempotencyKey", "jobKey"):
            value = payload.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class RunnerHandler(BaseHTTPRequestHandler):
    """HTTP handler exposing the #1 runner contract. Set .manager first."""

    manager: JobManager = None  # type: ignore[assignment]
    server_version = "RuntimeLabRunner/" + SERVICE_VERSION

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter test logs
        pass

    # -- helpers ---------------------------------------------------------

    def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Any:
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            count = int(length)
        except ValueError:
            return None
        if count <= 0:
            return {}
        if count > 4 * 1024 * 1024:
            return None
        raw = self.rfile.read(count)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def _path_only(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        manager = self.manager
        path = self._path_only()
        if path == RUNNER_HEALTH_PATH:
            if manager is None or not manager.ready:
                self._send_json(503, {"status": "starting", "ready": False, "service": SERVICE_NAME})
                return
            counts = manager.job_counts()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "ready": True,
                    "service": SERVICE_NAME,
                    "version": SERVICE_VERSION,
                    "region": manager.region,
                    "default_model": manager.default_model,
                    "fallback_model": FALLBACK_MODEL,
                    "uptime_seconds": round(time.time() - manager.start_time, 1),
                    "jobs": counts,
                    "job_timeout_seconds": manager.job_timeout_seconds,
                },
            )
            return
        if path.startswith("/v1/jobs/"):
            job_id = path[len("/v1/jobs/"):]
            job_id = urllib.parse.unquote(job_id).strip()
            if not job_id or "/" in job_id:
                self._send_json(404, {"error": "unknown job"})
                return
            record = manager.get(job_id) if manager is not None else None
            if record is None:
                self._send_json(404, {"error": "unknown job: %s" % job_id})
                return
            self._send_json(200, manager.to_result_dict(record))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        manager = self.manager
        path = self._path_only()
        if path != RUNNER_SUBMIT_JOB_PATH:
            self._send_json(404, {"error": "not found"})
            return
        if manager is None or not manager.ready:
            self._send_json(503, {"error": "runner is not ready", "ready": False})
            return
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "request body must be valid JSON"})
            return
        if not isinstance(payload, Mapping):
            self._send_json(400, {"error": "job payload must be a JSON object"})
            return
        key = extract_idempotency_key(dict(self.headers), payload)
        try:
            record, created = manager.submit(dict(payload), key)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        body = manager.to_result_dict(record)
        if not created:
            body["duplicate"] = True
            self._send_json(200, body)
        else:
            self._send_json(201, body)

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
    manager: Optional[JobManager] = None,
) -> ThreadingHTTPServer:
    """Build the HTTP server; caller owns serve_forever/shutdown."""
    resolved_manager = manager or build_manager_from_env()
    handler = type("BoundRunnerHandler", (RunnerHandler,), {"manager": resolved_manager})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def build_manager_from_env(
    *,
    command_runner: Optional[CommandRunner] = None,
    command_builder: Callable[[Mapping[str, Any], str], Sequence[str]] = default_command_for_job,
    opencode_bin: Optional[str] = None,
) -> JobManager:
    """Build a JobManager from environment. Requires no secrets.

    Deliberately never reads GITHUB_TOKEN, GH_TOKEN or OPENCODE_API_KEY: the
    runner holds no GitHub credentials and runs OpenCode with entirely
    environment-driven provider credentials. Production jobs without an
    explicit ``command`` field clone the public repository and invoke
    ``opencode run --auto --model <model> <task>``; an explicit ``command``
    selects the legacy single-command path used by unit tests.
    """
    region_raw = os.environ.get(ENV_REGION, os.environ.get(ENV_REGION_ALT, ""))
    return JobManager(
        workspace_root=os.environ.get(ENV_WORKSPACE_ROOT) or None,
        job_timeout_seconds=resolve_job_timeout(os.environ.get(ENV_JOB_TIMEOUT)),
        command_runner=command_runner,
        region=resolve_region(region_raw or None),
        default_model=resolve_default_model(os.environ.get(ENV_DEFAULT_MODEL)),
        command_builder=command_builder,
        opencode_bin=opencode_bin or os.environ.get(ENV_OPENCODE_BIN),
    )


def main() -> None:
    """Entrypoint for `python -m automation.runner_server` (Render start cmd)."""
    port = resolve_port(os.environ.get(ENV_PORT))
    manager = build_manager_from_env()
    server = create_server(port=port, manager=manager)
    print(
        "runtime-lab runner listening on 0.0.0.0:%d (region=%s model=%s timeout=%.0fs)"
        % (port, manager.region, manager.default_model, manager.job_timeout_seconds),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
