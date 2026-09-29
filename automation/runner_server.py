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
  pinned to an explicit release (``curl -fsSL https://opencode.ai/install
  | bash -s -- --version <pinned>`` with bounded retries plus backoff;
  see automation/install-opencode.sh), without extra
  integrations or release-specific behavior. The pin skips the
  installer's unauthenticated api.github.com lookup.
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
        experiment_record_path,
        validate_experiment_record_text,
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
    from automation.cgroup_memory import read_cgroup_snapshot
except ImportError:  # pytest inserts automation/ on sys.path
    try:
        from cgroup_memory import read_cgroup_snapshot  # type: ignore[no-redef]
    except ImportError:  # last resort: telemetry degrades, health stays up
        def read_cgroup_snapshot() -> dict[str, Any]:  # type: ignore[misc]
            return {"cgroup_version": "unknown", "memory_events": {}}

try:  # pragma: no cover - import path depends on entrypoint
    from automation.opencode_runner import (
        CHECKOUT_SUBDIR,
        OPENCODE_CONFIG_CONTENT,
        OPENCODE_INSTALL_MAX_ATTEMPTS,
        OPENCODE_INSTALL_RETRY_DELAYS,
        OPENCODE_LOW_MEMORY_BUN_OPTIONS,
        OPENCODE_LOW_MEMORY_ENV_VAR,
        apply_opencode_env_overrides,
        assert_fresh_session_command,
        build_changes,
        build_checkout_command,
        build_clone_command,
        build_opencode_command,
        build_opencode_install_command,
        build_rev_parse_command,
        build_status_command,
        find_opencode_binary,
        fresh_session_env,
        is_model_unavailable_error,
        opencode_runtime_install_allowed,
        probe_opencode_readiness,
        resolve_opencode_bin_override,
        sanitize_output,
        summarize_changes,
        truncate_head_tail,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from opencode_runner import (  # type: ignore[no-redef]
        CHECKOUT_SUBDIR,
        OPENCODE_CONFIG_CONTENT,
        OPENCODE_INSTALL_MAX_ATTEMPTS,
        OPENCODE_INSTALL_RETRY_DELAYS,
        OPENCODE_LOW_MEMORY_BUN_OPTIONS,
        OPENCODE_LOW_MEMORY_ENV_VAR,
        apply_opencode_env_overrides,
        assert_fresh_session_command,
        build_changes,
        build_checkout_command,
        build_clone_command,
        build_opencode_command,
        build_opencode_install_command,
        build_rev_parse_command,
        build_status_command,
        find_opencode_binary,
        fresh_session_env,
        is_model_unavailable_error,
        opencode_runtime_install_allowed,
        probe_opencode_readiness,
        resolve_opencode_bin_override,
        sanitize_output,
        summarize_changes,
        truncate_head_tail,
    )

try:  # pragma: no cover - import path depends on entrypoint
    from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
        EXACT_ARTIFACT_POST_PATH,
        EXACT_ENV_ARCHIVE_SHA,
        EXACT_ENV_BINARY_SHA,
        EXACT_ENV_ID,
        EXACT_ENV_NAME,
        EXACT_ENV_RUN,
        EXACT_ENV_VERSION,
        EXACT_PUSH_MAX_BYTES,
        build_exact_artifact_identity,
        build_exact_evidence,
        capture_proc_identity,
        exact_artifact_abs_path,
        materialize_exact_bytes,
        sha256_of_file,
        validate_exact_identity,
        verify_binary_file,
        verify_proc_exe_sha,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    try:
        from exact_artifact_delivery import (  # type: ignore[no-redef]
            EXACT_ARTIFACT_POST_PATH,
            EXACT_ENV_ARCHIVE_SHA,
            EXACT_ENV_BINARY_SHA,
            EXACT_ENV_ID,
            EXACT_ENV_NAME,
            EXACT_ENV_RUN,
            EXACT_ENV_VERSION,
            EXACT_PUSH_MAX_BYTES,
            build_exact_artifact_identity,
            build_exact_evidence,
            capture_proc_identity,
            exact_artifact_abs_path,
            materialize_exact_bytes,
            sha256_of_file,
            validate_exact_identity,
            verify_binary_file,
            verify_proc_exe_sha,
        )
    except ImportError:  # last resort: exact mode unavailable, fail closed per job
        EXACT_ARTIFACT_POST_PATH = "/v1/exact-artifact"
        EXACT_PUSH_MAX_BYTES = 256 * 1024 * 1024
        EXACT_ENV_ID = "OPENCODE_EXACT_ARTIFACT_ID"
        EXACT_ENV_BINARY_SHA = "OPENCODE_EXACT_ARTIFACT_SHA256"
        EXACT_ENV_ARCHIVE_SHA = "OPENCODE_EXACT_ARCHIVE_SHA256"
        EXACT_ENV_RUN = "OPENCODE_EXACT_SOURCE_RUN"
        EXACT_ENV_VERSION = "OPENCODE_EXACT_VERSION"
        EXACT_ENV_NAME = "OPENCODE_EXACT_ARTIFACT_NAME"

        def build_exact_artifact_identity():  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def validate_exact_identity(identity):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def exact_artifact_abs_path(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def materialize_exact_bytes(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def verify_binary_file(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def capture_proc_identity(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def verify_proc_exe_sha(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def build_exact_evidence(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")

        def sha256_of_file(*args, **kwargs):  # type: ignore[misc]
            raise ValueError("exact artifact support unavailable")
try:  # pragma: no cover - import path depends on entrypoint
    from automation.opencode_artifacts import (
        ENV_ARTIFACT_ID,
        ENV_ARTIFACT_REF,
        ENV_ARTIFACT_SHA256,
        artifact_binary_candidates,
        check_artifact_readiness,
        resolve_requested_artifact,
        verify_binary_checksum,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    try:
        from opencode_artifacts import (  # type: ignore[no-redef]
            ENV_ARTIFACT_ID,
            ENV_ARTIFACT_REF,
            ENV_ARTIFACT_SHA256,
            artifact_binary_candidates,
            check_artifact_readiness,
            resolve_requested_artifact,
            verify_binary_checksum,
        )
    except ImportError:  # last resort: baseline-only mode, no artifacts
        ENV_ARTIFACT_ID = "OPENCODE_ARTIFACT_ID"
        ENV_ARTIFACT_REF = "OPENCODE_ARTIFACT_REF"
        ENV_ARTIFACT_SHA256 = "OPENCODE_ARTIFACT_SHA256"

        def artifact_binary_candidates(repo_root, artifact_id):  # type: ignore[misc]
            return []

        def check_artifact_readiness(binary_path, expected_sha256):  # type: ignore[misc]
            return False, binary_path or "", "artifact support unavailable"

        def resolve_requested_artifact(env=None):  # type: ignore[misc]
            return None

        def verify_binary_checksum(path, expected_sha256):  # type: ignore[misc]
            raise ValueError("artifact support unavailable")

try:  # pragma: no cover - import path depends on entrypoint
    from automation.bounded_output import (
        resolve_max_retained_jobs,
        resolve_output_max_chars,
        resolve_stream_max_chars,
        run_bounded,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    try:
        from bounded_output import (  # type: ignore[no-redef]
            resolve_max_retained_jobs,
            resolve_output_max_chars,
            resolve_stream_max_chars,
            run_bounded,
        )
    except ImportError:  # last resort: head-only fallback, still bounded
        def resolve_max_retained_jobs(raw=None):  # type: ignore[misc]
            return 20

        def resolve_output_max_chars(raw=None):  # type: ignore[misc]
            return 4000

        def resolve_stream_max_chars(raw=None):  # type: ignore[misc]
            return 32768

        run_bounded = None  # type: ignore[assignment]

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
ENV_ALLOW_RUNTIME_INSTALL = "RUNNER_ALLOW_RUNTIME_INSTALL"
# Exact workflow artifact selection (issue #128): expected checksums boot
# with the worker via the create-service start command; the verified bytes
# arrive via POST /v1/exact-artifact before the job is submitted. The
# worker never holds a GitHub credential for this path.
ENV_EXACT_ARTIFACT_ID = "OPENCODE_EXACT_ARTIFACT_ID"
ENV_EXACT_BINARY_SHA = "OPENCODE_EXACT_ARTIFACT_SHA256"
ENV_EXACT_ARCHIVE_SHA = "OPENCODE_EXACT_ARCHIVE_SHA256"
ENV_EXACT_SOURCE_RUN = "OPENCODE_EXACT_SOURCE_RUN"
ENV_EXACT_VERSION = "OPENCODE_EXACT_VERSION"
ENV_EXACT_NAME = "OPENCODE_EXACT_ARTIFACT_NAME"
# Optional exact-version pin for a requested experiment artifact (issue #86):
# when set alongside OPENCODE_ARTIFACT_ID/SHA256, readiness additionally
# requires the running artifact binary to report this version string.
ENV_EXPECTED_ARTIFACT_VERSION = "OPENCODE_ARTIFACT_VERSION"

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


def terminal_output_limit() -> int:
    """Terminal combined-output bound (env ``RUNNER_MAX_OUTPUT_CHARS``)."""
    try:
        return max(1, int(resolve_output_max_chars()))
    except Exception:
        return _MAX_OUTPUT_CHARS


def stream_output_limit() -> int:
    """Per-stream in-memory bound (env ``RUNNER_MAX_STREAM_CHARS``)."""
    try:
        return max(1, int(resolve_stream_max_chars()))
    except Exception:
        return 32768


def _truncate(text: str, limit: int | None = None) -> str:
    """Bound text as head + marker + tail (issue #80).

    Keeps the head (command context) and the tail (where errors surface)
    with an explicit omission marker. Short inputs are unchanged, so all
    historical small-output behavior is preserved.
    """
    if limit is None:
        limit = terminal_output_limit()
    try:
        return truncate_head_tail(text, limit)
    except Exception:
        if not isinstance(text, str):
            return ""
        if len(text) <= limit:
            return text
        return text[:limit] + "...[truncated]"


def _bound_stream(text: str) -> str:
    """Bound one raw command stream to the configured in-memory limit.

    This is the defense-in-depth layer behind the spooling capture in
    :class:`SubprocessCommandRunner`: even a custom ``CommandRunner``
    returning an unbounded string cannot grow worker memory past the
    configured bound.
    """
    return _truncate(text, stream_output_limit())


def _session_scope_dir(cwd: str) -> str:
    """Return the workspace root scoping per-job session isolation.

    The OpenCode pipeline runs inside ``<workspace>/repo`` (the clone);
    the session database must live in the workspace root, never inside
    the clone, or it would pollute ``git status`` change detection.
    """
    try:
        if os.path.basename(os.path.normpath(cwd)) == CHECKOUT_SUBDIR:
            return os.path.dirname(os.path.normpath(cwd))
    except Exception:
        pass
    return cwd


def _is_opencode_run_command(cmd: Sequence[str]) -> bool:
    """True when ``cmd`` is an ``opencode run`` invocation (any binary path)."""
    try:
        parts = list(cmd)
    except Exception:
        return False
    if len(parts) < 2:
        return False
    try:
        binary = os.path.basename(str(parts[0]))
    except Exception:
        return False
    return binary == "opencode" and "run" in parts[1:3]


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
    """Default implementation with O(bound) capture memory (issue #80).

    The child streams stdout/stderr to spool files on disk and only
    bounded head/tail slices are read back into memory, so one verbose
    command (large test log, recursive grep, failing-test loop) cannot
    spike the 512 MB worker. ``opencode run`` invocations additionally
    execute with a per-job isolated session env (fresh ``OPENCODE_DB``
    under the workspace root, never inside the clone) after a
    fail-closed fresh-session assertion; history is never reused across
    GitHub issues.
    """

    def run(self, cmd: Sequence[str], cwd: str, timeout: float) -> CommandResult:
        argv = list(cmd)
        if _is_opencode_run_command(argv):
            # Fail closed before starting any process with stale state.
            assert_fresh_session_command(argv)
        child_env: dict[str, str] | None = None
        try:
            try:
                from automation.opencode_runner import scrubbed_env_for_worker
            except ImportError:
                from opencode_runner import scrubbed_env_for_worker  # type: ignore[no-redef]
            child_env = scrubbed_env_for_worker()
        except Exception:
            child_env = None
        if child_env is not None:
            # Validated low-memory default (issue #75): BUN_OPTIONS=--smol
            # trims ~40 MB off the agent peak (issue #56). Setdefault
            # semantics keep explicit operator values intact.
            try:
                apply_opencode_env_overrides(child_env)
            except Exception:
                pass
        if _is_opencode_run_command(argv) and child_env is not None:
            # Fresh OpenCode session per issue: isolate the session sqlite
            # database to this job's workspace (issue #80). Scoped to the
            # workspace root so the clone used for change detection stays
            # clean. Parent-env overrides win deterministically for the
            # isolation keys only.
            try:
                session_env = fresh_session_env(_session_scope_dir(cwd))
                child_env.update(session_env)
                # OpenCode fails fast with "unable to open database file"
                # when the OPENCODE_DB parent directory does not exist
                # (measured, issue #78), so the launcher owns its creation.
                db_path = session_env.get("OPENCODE_DB", "")
                if db_path:
                    os.makedirs(os.path.dirname(db_path), exist_ok=True)
            except Exception:
                pass
        if run_bounded is not None:
            try:
                bounded = run_bounded(argv, cwd=cwd, timeout=timeout, child_env=child_env)
            except FileNotFoundError as exc:
                return CommandResult(returncode=127, stdout="", stderr="command not found: %s" % exc)
            except OSError as exc:
                return CommandResult(returncode=127, stdout="", stderr="execution failed: %s" % exc)
            except Exception as exc:
                return CommandResult(returncode=127, stdout="", stderr="execution failed: %s" % exc)
            return CommandResult(
                returncode=bounded.returncode,
                stdout=bounded.stdout,
                stderr=bounded.stderr,
                timed_out=bounded.timed_out,
            )
        # Fallback when the bounded module is unavailable (kept bounded:
        # PIPE path is only for environments where spooling cannot load).
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(
                returncode=124,
                stdout=_bound_stream(str(stdout)),
                stderr=_bound_stream(str(stderr)),
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return CommandResult(returncode=127, stdout="", stderr="command not found: %s" % exc)
        except OSError as exc:
            return CommandResult(returncode=127, stdout="", stderr="execution failed: %s" % exc)
        return CommandResult(
            returncode=completed.returncode,
            stdout=_bound_stream(completed.stdout or ""),
            stderr=_bound_stream(completed.stderr or ""),
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


def _read_spool_bounded(path: str) -> str:
    """Read a spool file with O(bound) memory (issue #80 stream bound).

    Reads at most ~4x the stream bound in bytes, then head/tail truncates
    to the stream bound in chars. Never raises: unreadable spools read
    as "".
    """
    try:
        limit = max(1, int(stream_output_limit()))
    except Exception:
        limit = 32768
    try:
        total = max(0, os.path.getsize(path))
    except OSError:
        return ""
    if total == 0:
        return ""
    window = max(limit * 4, limit + 1024)
    head_n = window // 2
    tail_n = window - head_n
    try:
        with open(path, "rb") as handle:
            if total <= head_n + tail_n:
                raw = handle.read(head_n + tail_n + 1)
                text = raw.decode("utf-8", errors="replace")
                return _truncate(text, limit)
            head = handle.read(head_n)
            handle.seek(max(0, total - tail_n))
            tail = handle.read(tail_n + 1)
        combo = (
            head.decode("utf-8", errors="replace")
            + "\n...[spool-truncated %d bytes]...\n" % (total - head_n - tail_n)
            + tail.decode("utf-8", errors="replace")
        )
        return _truncate(combo, limit)
    except OSError:
        return ""


def read_resource_diagnostics() -> dict[str, Any]:
    """Best-effort lightweight process/resource signals (never secrets).

    Reads Linux /proc signals available on Render workers (VmRSS/VmHWM
    from /proc/self/status, load average) plus the live thread count, and
    the container-level cgroup snapshot (issue #57: total Render cgroup
    memory charged to the whole worker, including the OpenCode child
    process -- not just this Python process's RSS).

    Process RSS/HWM and container cgroup usage are kept as separate
    fields; RSS must never be confused with container usage. Returns {}
    entries only for signals that could be read; never raises, so
    /health stays available under pressure.
    """
    diagnostics: dict[str, Any] = {}
    try:
        diagnostics["threads"] = threading.active_count()
    except Exception:
        pass
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        diagnostics["rss_kb"] = int(parts[1])
                elif line.startswith("VmHWM:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        diagnostics["peak_rss_kb"] = int(parts[1])
                elif line.startswith("Threads:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        diagnostics["proc_threads"] = int(parts[1])
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as handle:
            diagnostics["loadavg_1m"] = handle.read().strip().split()[0]
    except (OSError, ValueError, IndexError):
        pass
    try:
        diagnostics["cgroup"] = read_cgroup_snapshot()
    except Exception:
        diagnostics["cgroup"] = {"cgroup_version": "unknown", "memory_events": {}}
    return diagnostics


def resolve_requested_exact_artifact(env: object = None) -> dict[str, str] | None:
    """Resolve the expected exact workflow artifact from the environment.

    Returns ``None`` in ordinary mode (no ``OPENCODE_EXACT_*`` selection).
    Fails closed on partial selection: any exact env key without the full
    supported conjunction is a configuration error, never a silent
    baseline. The validated identity carries no credential material.
    """
    source = os.environ if env is None else env
    if not hasattr(source, "get"):
        raise ValueError("env must be a mapping")
    get = lambda name: str(source.get(name, "") or "").strip()
    artifact_id = get(ENV_EXACT_ARTIFACT_ID)
    binary_sha = get(ENV_EXACT_BINARY_SHA).lower()
    archive_sha = get(ENV_EXACT_ARCHIVE_SHA).lower()
    run = get(ENV_EXACT_SOURCE_RUN)
    version = get(ENV_EXACT_VERSION)
    name = get(ENV_EXACT_NAME)
    if not any([artifact_id, binary_sha, archive_sha, run, version, name]):
        return None
    candidate = {
        "artifact_id": artifact_id,
        "artifact_name": name or "opencode-coding-linux-x64",
        "source_run_id": run,
        "archive_sha256": archive_sha,
        "binary_sha256": binary_sha,
        "version": version,
    }
    return dict(validate_exact_identity(candidate))


def exact_job_identity_from_payload(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """Extract and validate the per-job exact identity, if the job selects it.

    Returns ``None`` for ordinary jobs. Raises ``ValueError`` on partial
    or unsupported identities (fail closed before any OpenCode start).
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("exact_artifact")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("exact_artifact must be a mapping")
    return dict(validate_exact_identity(dict(raw)))


def store_exact_artifact_bytes(
    data: bytes, identity: Mapping[str, str], base_dir: str | None = None
) -> tuple[str, str]:
    """Materialize pushed bytes at the deterministic absolute path.

    Validates ``identity`` (full supported conjunction), writes ``data``
    with :func:`materialize_exact_bytes`, and returns ``(path, sha256)``.
    Enforces the push size bound. Never touches credentials.
    """
    validated = validate_exact_identity(dict(identity))
    if not isinstance(data, (bytes, bytearray)) or not bytes(data):
        raise ValueError("exact artifact push body must be non-empty bytes")
    if len(bytes(data)) > EXACT_PUSH_MAX_BYTES:
        raise ValueError(
            "exact artifact push body too large (%d > %d bytes)"
            % (len(bytes(data)), EXACT_PUSH_MAX_BYTES)
        )
    try:
        from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
            EXACT_PUSH_MAX_BYTES as _MAX,
        )
    except ImportError:
        try:
            from exact_artifact_delivery import (  # type: ignore[no-redef]
                EXACT_PUSH_MAX_BYTES as _MAX,
            )
        except ImportError:
            _MAX = EXACT_PUSH_MAX_BYTES
    if len(bytes(data)) > int(_MAX):
        raise ValueError("exact artifact push body exceeds the transport bound")
    dest = exact_artifact_abs_path(validated["artifact_id"], base_dir=base_dir)
    return materialize_exact_bytes(bytes(data), dest, validated["binary_sha256"])


def probe_exact_version(binary_abs_path: str, expected_version: str) -> str:
    """Run ``<abs> --version`` and require the exact expected version."""
    if not os.path.isabs(binary_abs_path):
        raise ValueError("exact binary path must be absolute")
    completed = subprocess.run(
        [binary_abs_path, "--version"],
        timeout=60.0,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = ((completed.stdout or "") + " " + (completed.stderr or "")).strip()
    version = output.split()[0] if output else ""
    if completed.returncode != 0 or not version:
        raise ValueError("exact --version probe failed: %s" % output[:200])
    if version != expected_version:
        raise ValueError(
            "exact --version %r does not equal the expected %r" % (version, expected_version)
        )
    return version


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
    # Exact workflow artifact (issue #128): per-job machine-readable
    # identity plus durable process-identity evidence. Both ride in the
    # terminal result so the controller preserves proof outside the
    # ephemeral worker.
    exact_artifact: dict[str, Any] = field(default_factory=dict)
    exact_evidence: dict[str, Any] = field(default_factory=dict)


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
        instance_id: Optional[str] = None,
        pid: Optional[int] = None,
        allow_runtime_install: Optional[bool] = None,
        max_retained_jobs: Optional[int] = None,
        requested_artifact: Optional[Mapping[str, str]] = None,
        requested_artifact_version: Optional[str] = None,
        requested_exact_artifact: Optional[Mapping[str, str]] = None,
    ) -> None:
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be positive")
        if default_model not in ALLOWED_MODELS:
            raise ValueError("unknown default model: %r" % default_model)
        if max_retained_jobs is None:
            try:
                max_retained_jobs = int(resolve_max_retained_jobs())
            except Exception:
                max_retained_jobs = 20
        if int(max_retained_jobs) <= 0:
            raise ValueError("max_retained_jobs must be positive")
        self.max_retained_jobs = int(max_retained_jobs)
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
        if allow_runtime_install is None:
            self.allow_runtime_install = opencode_runtime_install_allowed(
                os.environ.get(ENV_ALLOW_RUNTIME_INSTALL)
                if os.environ.get(ENV_ALLOW_RUNTIME_INSTALL) not in (None, "")
                else None
            )
        else:
            self.allow_runtime_install = bool(allow_runtime_install)
        # Explicit experiment-artifact selection (issue #86): an artifact
        # id plus SHA-256 selects that exact binary. ``None`` (no env
        # selection either) keeps the upstream-baseline mode unchanged.
        # Partial configuration fails closed here, never as a silent
        # baseline at job time.
        if requested_artifact is not None:
            if not isinstance(requested_artifact, Mapping):
                raise ValueError("requested_artifact must be a mapping")
            selection = {
                "artifact_id": str(requested_artifact.get("artifact_id", "") or "").strip(),
                "artifact_sha256": str(
                    requested_artifact.get("artifact_sha256", "") or ""
                ).strip().lower(),
                "artifact_ref": str(requested_artifact.get("artifact_ref", "") or "").strip(),
            }
            if not selection["artifact_id"] or not selection["artifact_sha256"]:
                raise ValueError(
                    "requested_artifact must carry artifact_id and artifact_sha256"
                )
            self.requested_artifact: Optional[dict[str, str]] = selection
        else:
            self.requested_artifact = resolve_requested_artifact()
        if requested_artifact_version is not None:
            self.requested_artifact_version = str(requested_artifact_version).strip()
        else:
            self.requested_artifact_version = str(
                os.environ.get(ENV_EXPECTED_ARTIFACT_VERSION, "") or ""
            ).strip()
        # Exact workflow-artifact selection (issue #128): expected
        # checksums boot with the worker via OPENCODE_EXACT_* env. The
        # verified bytes arrive via POST /v1/exact-artifact before the job
        # is submitted, so env selection alone never gates readiness --
        # each exact job verifies presence + checksum before starting
        # OpenCode and fails closed otherwise. Partial env selection
        # fails closed here, never as a silent baseline at job time.
        if requested_exact_artifact is not None:
            if not isinstance(requested_exact_artifact, Mapping):
                raise ValueError("requested_exact_artifact must be a mapping")
            try:
                self.requested_exact_artifact: Optional[dict[str, str]] = dict(
                    validate_exact_identity(dict(requested_exact_artifact))
                )
            except ValueError as exc:
                raise ValueError("invalid requested_exact_artifact: %s" % exc) from exc
        else:
            self.requested_exact_artifact = resolve_requested_exact_artifact()
        self.start_time = start_time if start_time is not None else time.time()
        # Unique process/manager identity (issue #41): a fresh uuid per
        # manager startup proves a restart/replacement directly, even when
        # the new process uptime is numerically greater than the old
        # snapshot. Wall-clock drift alone was ambiguous; this is not.
        raw_instance = (instance_id or "").strip() if instance_id else ""
        self.instance_id = raw_instance or uuid.uuid4().hex
        self.pid = int(pid) if pid is not None else os.getpid()
        self.started_at = float(self.start_time)
        # Deterministic OpenCode readiness (issue #52): in strict mode
        # (runtime installation disabled) the worker is not ready when the
        # expected binary is absent or non-executable, so Render health
        # checks fail fast instead of accepting jobs that can only fail.
        # The resolved path/version are logged by main() and exposed in
        # /health; no secrets are ever included.
        #
        # Experiment-artifact mode (issue #86) is stricter still: a
        # requested artifact must be present with a matching SHA-256 (and,
        # when pinned, the requested version) or the worker is not ready.
        # There is no fallback to the upstream baseline and no network
        # installer inside this path.
        if self.requested_artifact is not None:
            self._init_artifact_readiness()
        else:
            explicit_override = resolve_opencode_bin_override()
            probe_target = (
                self.opencode_bin
                if self.opencode_bin and self.opencode_bin != "opencode"
                else (explicit_override or find_opencode_binary() or None)
            )
            ready, resolved_path, version_detail = probe_opencode_readiness(
                probe_target if (probe_target and os.path.isabs(probe_target)) else None
            )
            self.opencode_resolved_bin = resolved_path
            self.opencode_version = version_detail if ready else ""
            self.opencode_ready_detail = (
                version_detail if ready else ("not ready: %s" % version_detail)
            )
            self.artifact_ready = False
            self.artifact_detail = "baseline mode: no experiment artifact requested"
            if self.allow_runtime_install:
                self.ready = True
            else:
                self.ready = bool(ready)
                if not ready:
                    self.opencode_bin = resolved_path or self.opencode_bin
        self._lock = threading.Lock()
        # Serializes OpenCode CLI provisioning (curl|bash installer) so
        # concurrent jobs never run concurrent heavyweight installers on
        # the 0.1 CPU / 512 MB free worker (issue #41: peak-pressure
        # reduction; execution itself stays concurrent for isolation).
        self._install_lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[str, str] = {}
        # Confine every OpenCode subprocess to read-only git inspection.
        # Credentials stay inherited from the process environment (Render
        # env vars); only confinement settings are defaulted here so
        # concurrent jobs never race on global state.
        os.environ.setdefault("OPENCODE_CONFIG_CONTENT", OPENCODE_CONFIG_CONTENT)
        os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
        # Validated low-memory default (issue #75): BUN_OPTIONS=--smol
        # trims ~40 MB off the ~600 MB agent peak (issue #56). Explicit
        # operator values win; the per-child setdefault in
        # SubprocessCommandRunner covers workers regardless.
        try:
            os.environ.setdefault(
                OPENCODE_LOW_MEMORY_ENV_VAR, OPENCODE_LOW_MEMORY_BUN_OPTIONS
            )
        except Exception:
            pass

    # -- experiment artifacts (issue #86) -----------------------------------

    def _resolve_artifact_binary(self) -> Optional[str]:
        """Return the per-artifact binary path, or None when absent."""
        selection = self.requested_artifact or {}
        artifact_id = str(selection.get("artifact_id", "") or "").strip()
        if not artifact_id:
            return None
        try:
            candidates = artifact_binary_candidates(None, artifact_id)
        except ValueError:
            return None
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _init_artifact_readiness(self) -> None:
        """Strict readiness for one explicitly requested artifact.

        Ready only when the exact per-artifact binary is present,
        executable, checksum-identical to the requested fingerprint and
        (when ``OPENCODE_ARTIFACT_VERSION`` pins one) version-identical to
        the running binary. Never falls back to the upstream baseline and
        never probes the network.
        """
        selection = self.requested_artifact or {}
        expected_sha = str(selection.get("artifact_sha256", "") or "").strip().lower()
        binary = self._resolve_artifact_binary()
        ready, resolved, detail = check_artifact_readiness(binary or "", expected_sha)
        version = ""
        if ready:
            probe_ok, _, probe_version = probe_opencode_readiness(resolved)
            if not probe_ok:
                ready = False
                detail = "experiment artifact --version probe failed: %s" % probe_version
            else:
                version = probe_version
                if self.requested_artifact_version and version != self.requested_artifact_version:
                    ready = False
                    detail = (
                        "experiment artifact version mismatch: requested %r, running %r"
                        % (self.requested_artifact_version, version)
                    )
        self.opencode_resolved_bin = resolved
        self.opencode_version = version if ready else ""
        if ready:
            self.opencode_ready_detail = "artifact %s ready (%s)" % (
                selection.get("artifact_id", ""),
                version or detail,
            )
        else:
            self.opencode_ready_detail = "artifact not ready: %s" % detail
        self.opencode_bin = resolved or self.opencode_bin
        self.artifact_ready = bool(ready)
        self.artifact_detail = detail
        self.ready = bool(ready)

    def _ensure_artifact_binary(self) -> str:
        """Resolve the requested artifact binary for a job (no fallback).

        Raises ``FileNotFoundError`` when the artifact binary is absent and
        ``ValueError`` on checksum mismatch. The network installer is never
        consulted here: experiment artifacts come only from the immutable
        per-artifact deploy path.
        """
        selection = self.requested_artifact or {}
        artifact_id = str(selection.get("artifact_id", "") or "").strip()
        expected_sha = str(selection.get("artifact_sha256", "") or "").strip().lower()
        binary = self._resolve_artifact_binary()
        if binary is None:
            raise FileNotFoundError(
                "experiment artifact %r not found under %s; experiment artifacts "
                "are provisioned only via automation/build-opencode-artifact.sh "
                "and never via the runtime network installer"
                % (artifact_id, ".opencode-artifacts/<artifact-id>/opencode")
            )
        verify_binary_checksum(binary, expected_sha)
        if self.requested_artifact_version:
            probe_ok, _, probe_version = probe_opencode_readiness(binary)
            if not probe_ok or probe_version != self.requested_artifact_version:
                raise ValueError(
                    "experiment artifact version mismatch: requested %r, running %r"
                    % (self.requested_artifact_version, probe_version)
                )
        return binary

    # -- exact workflow artifacts (issue #128) ------------------------------

    def _ensure_exact_binary(self, identity: Mapping[str, str]) -> str:
        """Resolve the exact binary at its deterministic absolute path.

        Zero fallback: only ``exact_artifact_abs_path`` for this identity
        is consulted (absolute, executable, checksum-verified). Missing or
        mismatched bytes raise before any OpenCode process starts. PATH,
        the repo ``.opencode-bin``, ``$HOME/.opencode/bin``, installers,
        the issue-#86 per-artifact path and the upstream baseline are
        never consulted here.
        """
        validated = validate_exact_identity(dict(identity))
        expected_sha = validated["binary_sha256"]
        expected_version = validated["version"]
        binary = exact_artifact_abs_path(validated["artifact_id"])
        if not os.path.isabs(binary):
            raise ValueError("exact binary path must be absolute: %r" % binary)
        if not os.path.isfile(binary):
            raise FileNotFoundError(
                "exact artifact %s not materialized at %s; deliver it via "
                "POST %s before submitting the job (controller-side "
                "credentialed fetch, verified push, no rebuild)"
                % (validated["artifact_id"], binary, EXACT_ARTIFACT_POST_PATH)
            )
        verify_binary_file(binary, expected_sha)
        version = probe_exact_version(binary, expected_version)
        if version != expected_version:
            raise ValueError(
                "exact artifact version mismatch: expected %r, running %r"
                % (expected_version, version)
            )
        return binary

    def _exact_materialized_state(self) -> dict[str, Any]:
        """Best-effort materialization state for /health (never raises)."""
        identity = getattr(self, "requested_exact_artifact", None)
        if not identity:
            return {"expected": False}
        try:
            path = exact_artifact_abs_path(str(identity.get("artifact_id", "")))
        except ValueError as exc:
            return {"expected": True, "present": False, "detail": str(exc)[:200]}
        try:
            actual = sha256_of_file(path)
            return {
                "expected": True,
                "present": True,
                "path": path,
                "sha256": actual,
                "matches": actual == str(identity.get("binary_sha256", "")).lower(),
            }
        except OSError as exc:
            return {"expected": True, "present": False, "path": path,
                    "detail": str(exc)[:200]}

    def _publish_running_exact_evidence(
        self, job_id: str, evidence: Mapping[str, Any]
    ) -> None:
        """Persist verified /proc identity while the exact child is still alive.

        Render may replace a Free worker under memory pressure before OpenCode
        reaches a terminal state. Publishing the already verified identity
        immediately makes GET /v1/jobs/<id> carry durable proof that the pinned
        binary actually executed, so the controller can retain it before a
        restart wipes worker memory.
        """
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status != "running":
                return
            current = dict(evidence)
            record.exact_evidence = current
            record.metadata["exact_evidence"] = current
            for key in (
                "exe_realpath",
                "exe_sha256",
                "pid",
                "file_sha256",
                "binary_sha256",
                "artifact_id",
                "cmdline",
            ):
                value = current.get(key, "")
                if value:
                    record.metadata["exact_" + key] = value
            record.updated_at = time.time()

    def _run_exact_opencode_attempt(
        self,
        *,
        binary_abs_path: str,
        model: str,
        task_text: str,
        cwd: str,
        workspace: str,
        timeout: float,
        expected_sha256: str,
        exact_identity: Optional[Mapping[str, str]] = None,
        on_identity: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Run one exact OpenCode attempt by absolute path with /proc proof.

        Uses ``Popen`` directly (never the generic command-runner fallback
        paths) so the child PID is available, captures
        ``/proc/<pid>/exe`` realpath + SHA + cmdline + parent evidence
        while the child runs, and fails closed when the executing SHA
        differs from ``expected_sha256``. Output is spool-backed and
        bounded (issue #80). Returns ``(CommandResult-like, evidence)``.
        """
        if not os.path.isabs(binary_abs_path):
            raise ValueError("exact OpenCode must be launched by absolute path")
        argv = build_opencode_command(model, task_text, opencode_bin=binary_abs_path)
        if not os.path.isabs(argv[0]) or argv[0] != binary_abs_path:
            raise ValueError("exact OpenCode argv[0] must be the absolute binary path")
        assert_fresh_session_command(argv)
        try:
            try:
                from automation.opencode_runner import (  # type: ignore[import-not-found]
                    scrubbed_env_for_worker as _scrubbed,
                )
                from automation.opencode_runner import (  # type: ignore[import-not-found]
                    assert_worker_env_clean as _assert_clean,
                )
                from automation.opencode_runner import (  # type: ignore[import-not-found]
                    fresh_session_env as _fresh_env,
                )
                from automation.opencode_runner import (  # type: ignore[import-not-found]
                    sanitize_output as _sanitize,
                )
            except ImportError:
                from opencode_runner import (  # type: ignore[no-redef]
                    scrubbed_env_for_worker as _scrubbed,
                )
                from opencode_runner import (  # type: ignore[no-redef]
                    assert_worker_env_clean as _assert_clean,
                )
                from opencode_runner import (  # type: ignore[no-redef]
                    fresh_session_env as _fresh_env,
                )
                from opencode_runner import (  # type: ignore[no-redef]
                    sanitize_output as _sanitize,
                )
        except ImportError:
            from opencode_runner import scrubbed_env_for_worker as _scrubbed  # type: ignore[no-redef]
            from opencode_runner import assert_worker_env_clean as _assert_clean  # type: ignore[no-redef]
            from opencode_runner import fresh_session_env as _fresh_env  # type: ignore[no-redef]
            from opencode_runner import sanitize_output as _sanitize  # type: ignore[no-redef]
        child_env = _scrubbed()
        try:
            session_env = _fresh_env(_session_scope_dir(workspace))
            child_env.update(session_env)
            db_path = session_env.get("OPENCODE_DB", "")
            if db_path:
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
        except Exception:
            pass
        try:
            # Validated low-memory default (issue #75): never clobbers
            # explicit operator values; BUN_OPTIONS is not a credential
            # so _assert_clean still holds below.
            apply_opencode_env_overrides(child_env)
        except Exception:
            pass
        _assert_clean(child_env)
        try:
            budget = max(0.1, float(timeout))
        except (TypeError, ValueError):
            budget = 60.0
        out_fd, out_path = tempfile.mkstemp(prefix="exact-stdout-", suffix=".log")
        err_fd, err_path = tempfile.mkstemp(prefix="exact-stderr-", suffix=".log")
        os.close(out_fd)
        os.close(err_fd)
        proc_identity: dict[str, str] = {}
        file_sha = sha256_of_file(binary_abs_path)
        proc = None
        try:
            with open(out_path, "wb") as out_handle, open(err_path, "wb") as err_handle:
                proc = subprocess.Popen(
                    argv, cwd=cwd, stdout=out_handle, stderr=err_handle,
                    text=False, env=child_env,
                )
                pid = int(proc.pid)
                try:
                    proc_identity = dict(capture_proc_identity(pid))
                except FileNotFoundError:
                    proc_identity = {
                        "pid": str(pid),
                        "ppid": str(os.getpid()),
                        "parent_pid": str(os.getpid()),
                        "exe_realpath": os.path.realpath(binary_abs_path),
                        "exe_sha256": file_sha,
                        "cmdline": " ".join(argv)[:4000],
                        "exe_source": "file (process exited before /proc read)",
                    }
                verify_proc_exe_sha(proc_identity, expected_sha256)
                # Build and publish the verified identity BEFORE waiting for
                # OpenCode to finish. A Render OOM/replacement can kill this
                # process and erase all worker state while the child is still
                # running; controller polls must be able to retain this proof.
                evidence = build_exact_evidence(
                    binary_abs_path,
                    file_sha,
                    proc_identity,
                    argv,
                    version="",
                    identity=exact_identity,
                )
                evidence["cmdline"] = str(proc_identity.get("cmdline", ""))[:4000]
                if on_identity is not None:
                    on_identity(evidence)
                try:
                    proc.wait(timeout=budget)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=10)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                    timed_out = True
                returncode = int(proc.returncode)
        finally:
            pass
        try:
            stdout_text = _read_spool_bounded(out_path)
            stderr_text = _read_spool_bounded(err_path)
        finally:
            for path in (out_path, err_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        combined = _sanitize((stdout_text or "") + ("\n" if stdout_text or stderr_text else "") + (stderr_text or ""))
        bounded = _bound_stream(combined)
        result = CommandResult(
            returncode=returncode, stdout=stdout_text, stderr=stderr_text,
            timed_out=timed_out,
        )
        # Attach the bounded combined view plus identity for the caller.
        result_combined = bounded
        # evidence was constructed and published immediately after /proc
        # verification, before the potentially long-running wait above.
        return result, {"evidence": evidence, "proc_identity": proc_identity,
                        "file_sha256": file_sha, "combined": result_combined,
                        "pid": pid}


    # -- introspection ----------------------------------------------------

    def uptime_seconds(self, now: Optional[float] = None) -> float:
        """Seconds since this manager process started."""
        return max(0.0, (now if now is not None else time.time()) - self.started_at)

    def health_snapshot(self) -> dict[str, Any]:
        """Process-identity + readiness snapshot for GET /health."""
        counts = self.job_counts()
        return {
            "status": "ok",
            "ready": bool(self.ready),
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "region": self.region,
            "default_model": self.default_model,
            "fallback_model": FALLBACK_MODEL,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "jobs": counts,
            "job_timeout_seconds": self.job_timeout_seconds,
            "resources": read_resource_diagnostics(),
            "opencode_bin": getattr(self, "opencode_resolved_bin", ""),
            "opencode_version": getattr(self, "opencode_version", ""),
            "opencode_ready": bool(getattr(self, "opencode_version", "")),
            "opencode_detail": getattr(self, "opencode_ready_detail", ""),
            "opencode_artifact_id": str(
                (getattr(self, "requested_artifact", None) or {}).get("artifact_id", "")
            ),
            "opencode_artifact_sha256": str(
                (getattr(self, "requested_artifact", None) or {}).get("artifact_sha256", "")
            ),
            "opencode_artifact_ref": str(
                (getattr(self, "requested_artifact", None) or {}).get("artifact_ref", "")
            ),
            "opencode_expected_version": str(
                getattr(self, "requested_artifact_version", "") or ""
            ),
            "opencode_artifact_ready": bool(getattr(self, "artifact_ready", False)),
            "opencode_artifact_detail": str(getattr(self, "artifact_detail", "")),
            "allow_runtime_install": bool(
                getattr(self, "allow_runtime_install", True)
            ),
            # Exact workflow artifact (issue #128): booted expectation plus
            # materialization state. Bytes arrive via POST /v1/exact-artifact
            # after boot, so expectation alone never gates readiness -- each
            # exact job verifies presence + checksum before starting.
            "exact_artifact": dict(getattr(self, "requested_exact_artifact", None) or {}),
            "exact_materialized": self._exact_materialized_state(),
        }

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

    def _exact_identity_for_job(self, payload: Mapping[str, Any]) -> dict[str, str] | None:
        """Effective exact identity for one job (payload wins, env is fallback).

        Returns ``None`` for ordinary jobs. Raises ``ValueError`` when a
        job selects an exact artifact but the selection is partial,
        unsupported, or disagrees with the worker's booted expectation --
        fail closed before any OpenCode start, never a silent baseline.
        """
        payload_identity = exact_job_identity_from_payload(payload)
        env_identity = getattr(self, "requested_exact_artifact", None)
        if payload_identity is None and env_identity is None:
            return None
        if payload_identity is not None and env_identity is not None:
            if (
                payload_identity.get("artifact_id") != env_identity.get("artifact_id")
                or payload_identity.get("binary_sha256", "").lower()
                != str(env_identity.get("binary_sha256", "")).lower()
            ):
                raise ValueError(
                    "job exact_artifact disagrees with the worker's expected "
                    "exact artifact; refusing to substitute"
                )
            return dict(payload_identity)
        return dict(payload_identity) if payload_identity is not None else dict(
            env_identity or {}
        )

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
        if not isinstance(repository_url, str):
            raise ValueError("repository_url must be a string")
        try:  # allow-listed cross-repo targets (issue #85), self by default
            try:
                from automation.cross_repo import normalize_clone_url as _normalize_url
            except ImportError:
                from cross_repo import normalize_clone_url as _normalize_url  # type: ignore[no-redef]
            _normalize_url(repository_url)
        except ImportError:
            if repository_url != PUBLIC_REPO_URL:
                raise ValueError("repository_url must be %r in this phase" % PUBLIC_REPO_URL)
        except ValueError as exc:
            raise ValueError(
                "repository_url %r is not an allow-listed execution target (%s)"
                % (repository_url, exc)
            ) from None
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
        # Exact-artifact selection is validated before any worker process
        # starts (fail closed on partial/unsupported identity, never a
        # silent baseline). Invalid selection becomes a terminal
        # rejection, not an execution.
        try:
            job_exact = self._exact_identity_for_job(payload)
        except ValueError as exc:
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
                error="invalid exact_artifact selection: %s" % exc,
            )
            return record, True

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
            exact_artifact=job_exact,
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
            exact_artifact=dict(job_exact) if job_exact else {},
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
        exact_artifact: Mapping[str, str] | None = None,
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
        # Process identity on every job (issue #41): the controller can
        # prove which worker process accepted the job, and a changed
        # instance id on the next /health proves a restart directly.
        metadata["runner_instance_id"] = self.instance_id
        metadata["runner_pid"] = self.pid
        metadata["runner_started_at"] = self.started_at
        if repository_url:
            metadata["repository_url"] = repository_url
        # Cross-repo correlation passthrough (issue #85): never secrets.
        for _key in ("target_repository", "source_repository"):
            _value = payload.get(_key, "")
            if isinstance(_value, str) and _value.strip():
                metadata[_key] = _value.strip()
        if base_ref:
            metadata["base_ref"] = base_ref
        if base_sha:
            metadata["base_sha"] = base_sha
        # Exact-artifact identity (issue #128): machine-readable proof of
        # which immutable bytes the job must run. Survives in the terminal
        # result so the controller preserves it outside the worker.
        if exact_artifact:
            metadata["exact_artifact"] = dict(exact_artifact)
        elif isinstance(payload.get("exact_artifact"), Mapping):
            try:
                metadata["exact_artifact"] = dict(
                    validate_exact_identity(dict(payload["exact_artifact"]))
                )
            except ValueError:
                pass
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
        """Resolve the OpenCode binary, provisioning with the version-pinned
        pattern when missing: ``curl -fsSL https://opencode.ai/install |
        bash -s -- --version <pinned>`` with bounded retries and backoff,
        then verify executability.

        The pinned ``--version`` form skips the installer's unauthenticated
        ``api.github.com`` latest-version lookup, which fails closed with
        "Failed to fetch version information" under Render shared-egress
        rate limiting or transient network errors (run 36421205678).
        ``$OPENCODE_VERSION`` overrides the pin; an explicit
        RUNNER_OPENCODE_BIN/manager override is used verbatim (tests
        inject fakes this way) and skips provisioning.

        Issue #52 strict mode: when runtime installation is disabled
        (``RUNNER_ALLOW_RUNTIME_INSTALL=0``, the production Render start
        command), a missing binary fails fast with FileNotFoundError and
        never runs the network installer inside the job. The binary must
        come from the deterministic deploy artifact
        (``.opencode-bin/opencode`` copied at build time by
        automation/install-opencode.sh) so build-time $HOME never needs
        to equal runtime $HOME.

        Issue #86 experiment mode: when an explicit artifact was requested,
        only that exact per-artifact binary is used (checksum-verified);
        the baseline and the network installer are never consulted.
        """
        if getattr(self, "requested_artifact", None) is not None:
            return self._ensure_artifact_binary()
        override = (self.opencode_bin or "").strip()
        if override and override != "opencode":
            return override
        found = find_opencode_binary()
        if found:
            return found
        if not getattr(self, "allow_runtime_install", True):
            raise FileNotFoundError(
                "opencode binary not found and runtime installation is disabled "
                "(RUNNER_ALLOW_RUNTIME_INSTALL=0); provision via "
                "automation/install-opencode.sh at build time so "
                ".opencode-bin/opencode ships inside the deploy artifact"
            )
        # Serialize provisioning: concurrent jobs must not run concurrent
        # curl|bash installers on the small free worker (issue #41 peak
        # memory/process pressure reduction). Re-check inside the lock so
        # only the first waiter actually installs.
        with self._install_lock:
            found = find_opencode_binary()
            if found:
                return found
            try:
                install_command = build_opencode_install_command()
            except ValueError as exc:
                raise FileNotFoundError(
                    "invalid OpenCode version (%s); provision via "
                    "automation/install-opencode.sh" % exc
                )
            last_error = "opencode CLI not found"
            attempts = max(1, int(OPENCODE_INSTALL_MAX_ATTEMPTS))
            for attempt in range(1, attempts + 1):
                try:
                    result = self.command_runner.run(
                        ["sh", "-c", install_command],
                        cwd=cwd,
                        timeout=timeout,
                    )
                except Exception as exc:
                    last_error = "opencode install failed: %s" % exc
                    self._sleep_between_install_attempts(attempt, timeout)
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
                self._sleep_between_install_attempts(attempt, timeout)
            raise FileNotFoundError(
                "%s; provision via automation/install-opencode.sh "
                "(curl -fsSL https://opencode.ai/install | bash -s -- --version <pinned>)" % last_error
            )

    def _sleep_between_install_attempts(self, attempt: int, timeout: float) -> None:
        """Backoff sleep between install attempts (no sleep after last)."""
        delays = OPENCODE_INSTALL_RETRY_DELAYS
        index = attempt - 1  # attempt is 1-indexed; delay precedes next try
        if index < 0 or index >= len(delays):
            return
        try:
            delay = float(delays[index])
        except (TypeError, ValueError):
            return
        if delay <= 0:
            return
        try:
            remaining = float(timeout)
        except (TypeError, ValueError):
            remaining = delay
        time.sleep(min(delay, max(0.0, remaining)))

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

        # -- resolve exact vs ordinary mode before provisioning ---------------
        # Exact mode (issue #128) is selected by the per-job identity
        # recorded at submit time. Ordinary jobs keep the historical
        # resolution order (override > deploy artifact > PATH > HOME >
        # installer/baseline) unchanged.
        job_exact: dict[str, str] | None = None
        try:
            stored = getattr(record, "exact_artifact", None)
            if stored:
                job_exact = dict(validate_exact_identity(dict(stored)))
            else:
                job_exact = self._exact_identity_for_job(payload)
                if job_exact:
                    record.exact_artifact = dict(job_exact)
                    with self._lock:
                        record.metadata["exact_artifact"] = dict(job_exact)
        except ValueError as exc:
            self._finish(job_id, "failed", summary="",
                         error="invalid exact_artifact selection: %s"
                         % sanitize_output(str(exc))[:1000],
                         exit_code=None)
            return

        # -- provision/resolve the OpenCode CLI ------------------------------
        opencode_bin = ""
        exact_evidence: dict[str, Any] = {}
        if job_exact is not None:
            # Zero fallback: only the deterministic absolute path with a
            # matching SHA-256 (plus the exact --version) is accepted.
            # Missing/mismatched bytes fail closed before any start.
            try:
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    raise TimeoutError("job timed out")
                opencode_bin = self._ensure_exact_binary(job_exact)
                if not os.path.isabs(opencode_bin):
                    raise ValueError("exact binary path must be absolute")
            except TimeoutError:
                self._finish(job_id, "timed_out", summary="",
                             error="job timed out after %.1f seconds" % timeout,
                             exit_code=124)
                return
            except (FileNotFoundError, ValueError) as exc:
                self._finish(job_id, "failed", summary="",
                             error="exact artifact not runnable: %s"
                             % _truncate(sanitize_output(str(exc))),
                             exit_code=127)
                return
            except Exception as exc:
                self._finish(job_id, "failed", summary="",
                             error="execution failed: %s" % sanitize_output(str(exc))[:1000])
                return
        else:
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
        # Exact mode launches the absolute path via Popen with /proc
        # identity capture per attempt; ordinary mode keeps the generic
        # command-runner path unchanged.
        models_to_try = [requested_model]
        if requested_model == PREFERRED_MODEL:
            models_to_try.append(FALLBACK_MODEL)
        executed_model = requested_model
        last_output = ""
        last_exit: Optional[int] = None
        first_error = ""
        opencode_timed_out = False
        exact_attempt_evidence: dict[str, Any] = {}
        for attempt_index, model in enumerate(models_to_try):
            try:
                validate_worker_region(region)
            except RegionPolicyError as exc:
                self._finish(job_id, "failed", summary="",
                             error="region %r is forbidden for Muse jobs (%s)" % (region, exc))
                return
            if job_exact is not None:
                try:
                    remaining = self._remaining(deadline)
                    if remaining <= 0:
                        raise TimeoutError("job timed out")
                    attempt_result, attempt_info = self._run_exact_opencode_attempt(
                        binary_abs_path=opencode_bin,
                        model=model,
                        task_text=task_text,
                        cwd=checkout_dir,
                        workspace=record.workspace,
                        timeout=remaining,
                        expected_sha256=str(job_exact.get("binary_sha256", "")),
                        exact_identity=job_exact,
                        on_identity=lambda ev: self._publish_running_exact_evidence(
                            job_id, ev
                        ),
                    )
                except TimeoutError:
                    opencode_timed_out = True
                    break
                except ValueError as exc:
                    # /proc mismatch, credential leak, or non-absolute
                    # launch: fail closed, never fall back to another
                    # binary.
                    self._finish(job_id, "failed", summary="",
                                 error="exact process identity failure: %s"
                                 % sanitize_output(str(exc))[:1000],
                                 exit_code=None,
                                 exact_evidence=exact_attempt_evidence or None)
                    return
                except Exception as exc:
                    self._finish(job_id, "failed", summary="",
                                 error="execution failed: %s" % sanitize_output(str(exc))[:1000])
                    return
                combined = str(attempt_info.get("combined", "") or "")
                last_output = combined
                last_exit = int(attempt_result.returncode)
                executed_model = model
                exact_attempt_evidence = dict(attempt_info.get("evidence", {}) or {})
                exact_attempt_evidence["attempt_model"] = model
                if attempt_result.timed_out:
                    opencode_timed_out = True
                    break
                if attempt_result.returncode == 0:
                    first_error = ""
                    break
                if (
                    attempt_index == 0
                    and model == PREFERRED_MODEL
                    and len(models_to_try) > 1
                    and is_model_unavailable_error(combined)
                ):
                    first_error = _truncate(combined)
                    continue
                first_error = _truncate(combined)
                break
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
                # Bound at receipt: a custom CommandRunner may return an
                # unbounded string, so the per-attempt buffer is capped here
                # (stream bound) and only the terminal slice (output bound)
                # is retained in the job record below. Head + tail keeps
                # both the command context and the trailing error lines.
                last_output = _bound_stream(sanitize_output(
                    (result.stdout or "") + ("\n" if result.stdout or result.stderr else "") + (result.stderr or "")
                ))
                break
            combined = _bound_stream(sanitize_output(
                (result.stdout or "") + ("\n" if result.stdout or result.stderr else "") + (result.stderr or "")
            ))
            last_output = combined
            last_exit = result.returncode
            executed_model = model
            if result.returncode == 0:
                first_error = ""
                break
            # Non-zero: retry once with the fallback on availability errors.
            # first_error is stored at the terminal bound (not the stream
            # bound): it only feeds the failure message, so keeping a
            # second full-size copy of the attempt output would double
            # retention for no diagnostic gain (issue #80 duplication).
            if (
                attempt_index == 0
                and model == PREFERRED_MODEL
                and len(models_to_try) > 1
                and is_model_unavailable_error(combined)
            ):
                first_error = _truncate(combined)
                continue
            first_error = _truncate(combined)
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
                exact_evidence=exact_attempt_evidence or None,
            )
            return

        output = _truncate(last_output or "", _MAX_OUTPUT_CHARS)
        if last_exit not in (0, None) and last_exit != 0:
            # Non-zero OpenCode exit: failed, but still embed any partial
            # changes so the failure is debuggable from the result alone.
            changes = self._best_effort_changes(checkout_dir)
            detail = _truncate((last_output.strip() or "opencode failed"), _MAX_OUTPUT_CHARS)
            if first_error and executed_model == FALLBACK_MODEL and first_error != _truncate(last_output):
                detail = _truncate(
                    "primary model %s unavailable; fallback %s also failed: %s"
                    % (requested_model, FALLBACK_MODEL, detail), _MAX_OUTPUT_CHARS,
                )
            self._finish(
                job_id, "failed", summary="",
                error="opencode exited with code %d: %s" % (last_exit, detail),
                exit_code=last_exit, output=output,
                changes=changes, executed_model=executed_model,
                exact_evidence=exact_attempt_evidence or None,
            )
            return

        # -- enforce durable agent knowledge handoff --------------------------
        # Self-target only (issue #85): the handoff record lives in the
        # tracking repo (kodmial/runtime-lab). A cross-repo checkout (e.g.
        # kodmial/opencode) has no runtime-lab knowledge tree, so the
        # in-checkout record gate is skipped there -- requiring it would
        # force a runtime-lab record file into the target PR. Target-side
        # evidence is recorded controller-side instead (see
        # automation/cross_repo.py:build_execution_evidence).
        _is_self_target = (repository_url or PUBLIC_REPO_URL) == PUBLIC_REPO_URL
        if "Repository knowledge handoff (mandatory)" in task_text and _is_self_target:
            issue_number = int(record.metadata.get("issue_number") or 0)
            run_id = record.metadata.get("run_id", "")
            relative_record = experiment_record_path(issue_number, run_id)
            record_path = os.path.join(checkout_dir, relative_record)
            try:
                with open(record_path, "r", encoding="utf-8") as handle:
                    record_text = handle.read(256 * 1024 + 1)
                if len(record_text) > 256 * 1024:
                    raise ValueError("experiment record exceeds 256 KiB")
                validate_experiment_record_text(record_text, issue_number, run_id)
            except (OSError, ValueError) as exc:
                changes = self._best_effort_changes(checkout_dir)
                self._finish(
                    job_id, "failed", summary="",
                    error="knowledge handoff validation failed: %s"
                    % sanitize_output(str(exc))[:1000],
                    exit_code=None, output=output,
                    changes=changes, executed_model=executed_model,
                    exact_evidence=exact_attempt_evidence or None,
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
                         changes=changes, executed_model=executed_model,
                         exact_evidence=exact_attempt_evidence or None)
            return
        except Exception as exc:
            self._finish(job_id, "failed", summary="",
                         error="execution failed: %s" % sanitize_output(str(exc))[:1000],
                         exit_code=None, output=output,
                         changes=[], executed_model=executed_model,
                         exact_evidence=exact_attempt_evidence or None)
            return
        if result.timed_out:
            changes = self._best_effort_changes(checkout_dir)
            self._finish(job_id, "timed_out", summary="",
                         error="job timed out after %.1f seconds" % timeout,
                         exit_code=result.returncode, output=output,
                         changes=changes, executed_model=executed_model,
                         exact_evidence=exact_attempt_evidence or None)
            return
        if result.returncode != 0:
            detail = sanitize_output(result.stderr.strip() or result.stdout.strip() or "status failed")
            self._finish(job_id, "failed", summary="",
                         error="change detection failed (code %d): %s"
                         % (result.returncode, _truncate(detail)),
                         exit_code=result.returncode, output=output,
                         changes=[], executed_model=executed_model,
                         exact_evidence=exact_attempt_evidence or None)
            return
        try:
            changes = build_changes(result.stdout or "", checkout_dir)
        except ValueError as exc:
            self._finish(job_id, "failed", summary="",
                         error="change extraction failed: %s" % sanitize_output(str(exc))[:1000],
                         exit_code=None, output=output,
                         changes=[], executed_model=executed_model,
                         exact_evidence=exact_attempt_evidence or None)
            return
        change_summary = summarize_changes(changes)
        if not changes:
            summary = _truncate("no changes [model=%s]" % executed_model)
        else:
            summary = _truncate("%s [model=%s]" % (change_summary, executed_model))
        self._finish(
            job_id, "succeeded", summary=summary, error="", exit_code=0,
            output=output, changes=changes, executed_model=executed_model,
            exact_evidence=exact_attempt_evidence or None,
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
        exact_evidence: Optional[Mapping[str, Any]] = None,
    ) -> None:
        evicted_workspaces: list[str] = []
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
            # Exact/process identity evidence (issue #128): durable proof
            # returned in the terminal result so the controller preserves
            # it outside the ephemeral worker.
            if exact_evidence is not None:
                record.exact_evidence = dict(exact_evidence)
                record.metadata["exact_evidence"] = dict(exact_evidence)
                for key in ("exe_realpath", "exe_sha256", "pid", "file_sha256",
                            "binary_sha256", "artifact_id", "cmdline"):
                    value = dict(exact_evidence).get(key, "")
                    if value:
                        record.metadata["exact_" + key] = value
            if record.exact_artifact and "exact_artifact" not in record.metadata:
                record.metadata["exact_artifact"] = dict(record.exact_artifact)
            record.updated_at = time.time()
            # Bound accumulated history: without eviction every terminal
            # job's output/error/changes plus its workspace clone stays
            # resident, so repeated shell calls and rerun sequences grow
            # worker memory without a bound (issue #80).
            evicted_workspaces = self._evict_old_terminal_jobs_locked()
        for workspace in evicted_workspaces:
            shutil.rmtree(workspace, ignore_errors=True)

    def _evict_old_terminal_jobs_locked(self) -> list[str]:
        """Drop oldest terminal jobs past the retention cap; return workspaces.

        Caller must hold ``self._lock``; workspace deletion happens after
        the lock is released. The idempotency index is cleaned alongside
        so evicted jobs never shadow new submits.
        """
        terminal = [
            job for job in self._jobs.values()
            if job.status in RUNNER_TERMINAL_STATUSES
        ]
        excess = len(terminal) - max(1, int(self.max_retained_jobs))
        if excess <= 0:
            return []
        terminal.sort(key=lambda job: job.updated_at)
        workspaces: list[str] = []
        for job in terminal[:excess]:
            self._jobs.pop(job.job_id, None)
            if job.idempotency_key and self._idempotency.get(job.idempotency_key) == job.job_id:
                self._idempotency.pop(job.idempotency_key, None)
            if job.workspace:
                workspaces.append(job.workspace)
        return workspaces

    def retained_terminal_count(self) -> int:
        """Number of retained terminal jobs (bounded by the cap)."""
        with self._lock:
            return sum(
                1 for job in self._jobs.values()
                if job.status in RUNNER_TERMINAL_STATUSES
            )

    # -- serialization ---------------------------------------------------------

    def to_result_dict(self, record: JobRecord) -> dict[str, Any]:
        executed = record.executed_model or str(record.metadata.get("model", ""))
        metadata = dict(record.metadata)
        # Exact/process identity evidence (issue #128) is part of the
        # durable terminal result: the controller persists the whole
        # result outside the ephemeral worker, so restarts/OOMs cannot
        # erase proof. Kept compatible with the cgroup/instance
        # telemetry path (memory_telemetry merges alongside, never
        # overwriting these keys).
        if record.exact_artifact and "exact_artifact" not in metadata:
            metadata["exact_artifact"] = dict(record.exact_artifact)
        if record.exact_evidence and "exact_evidence" not in metadata:
            metadata["exact_evidence"] = dict(record.exact_evidence)
        return {
            "job_id": record.job_id,
            "status": record.status,
            "success": record.success,
            "summary": record.summary,
            "error": record.error,
            "metadata": metadata,
            "exit_code": record.exit_code,
            "output": record.output,
            "changes": [dict(item) for item in record.changes],
            "executed_model": executed,
            "repository_url": record.repository_url,
            "base_ref": record.base_ref,
            "base_sha": record.base_sha,
            # Top-level process identity mirrors the metadata copy so
            # controllers need not dig into metadata to prove restarts.
            "runner_instance_id": str(record.metadata.get("runner_instance_id", "")),
            "exact_artifact": dict(record.exact_artifact),
            "exact_evidence": dict(record.exact_evidence),
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
            self._send_json(200, manager.health_snapshot())
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
        if path == EXACT_ARTIFACT_POST_PATH:
            self._handle_exact_artifact_post()
            return
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

    def _handle_exact_artifact_post(self) -> None:
        """Accept verified exact-artifact bytes (controller push transport).

        Request: ``application/octet-stream`` body (the exact binary
        bytes) with identity headers (``X-Exact-Artifact-Id``,
        ``X-Exact-Artifact-Sha256``, ``X-Exact-Source-Run``,
        ``X-Exact-Archive-Sha256``, ``X-Exact-Version``,
        ``X-Exact-Artifact-Name``). The handler validates the full
        supported identity, materializes the bytes at the deterministic
        absolute path, verifies the SHA-256, and returns the path + sha.
        Missing/mismatched artifacts fail closed with 400/409; the body
        is bounded by the transport limit. No GitHub credential is
        accepted or required here -- the controller fetched with its own
        credential before pushing bytes.
        """
        manager = self.manager
        if manager is None:
            self._send_json(503, {"error": "runner is not ready", "ready": False})
            return
        lowered = {str(k).lower(): v for k, v in dict(self.headers).items()}
        identity = {
            "artifact_id": str(lowered.get("x-exact-artifact-id", "") or "").strip(),
            "artifact_name": str(lowered.get("x-exact-artifact-name", "") or "").strip()
            or "opencode-coding-linux-x64",
            "source_run_id": str(lowered.get("x-exact-source-run", "") or "").strip(),
            "archive_sha256": str(lowered.get("x-exact-archive-sha256", "") or "").strip().lower(),
            "binary_sha256": str(lowered.get("x-exact-artifact-sha256", "") or "").strip().lower(),
            "version": str(lowered.get("x-exact-version", "") or "").strip(),
        }
        try:
            validated = validate_exact_identity(identity)
        except ValueError as exc:
            self._send_json(400, {"error": "invalid exact artifact identity: %s" % exc})
            return
        expected = getattr(manager, "requested_exact_artifact", None)
        if expected and (
            validated.get("artifact_id") != expected.get("artifact_id")
            or validated.get("binary_sha256", "").lower()
            != str(expected.get("binary_sha256", "")).lower()
        ):
            self._send_json(
                409,
                {"error": "pushed exact artifact disagrees with the worker expectation"},
            )
            return
        length = self.headers.get("Content-Length")
        try:
            count = int(str(length or "").strip())
        except ValueError:
            self._send_json(400, {"error": "exact push requires Content-Length"})
            return
        if count <= 0 or count > EXACT_PUSH_MAX_BYTES:
            self._send_json(400, {"error": "exact push body size out of bounds"})
            return
        try:
            data = self.rfile.read(count)
        except Exception as exc:
            self._send_json(400, {"error": "could not read exact push body: %s" % exc})
            return
        if not data or len(data) != count:
            self._send_json(400, {"error": "short exact push body"})
            return
        try:
            path, sha = store_exact_artifact_bytes(bytes(data), validated)
        except ValueError as exc:
            self._send_json(409, {"error": "exact artifact rejected: %s" % exc})
            return
        except OSError as exc:
            self._send_json(500, {"error": "could not materialize exact artifact: %s" % exc})
            return
        self._send_json(201, {
            "ok": True,
            "artifact_id": validated["artifact_id"],
            "source_run_id": validated["source_run_id"],
            "path": path,
            "sha256": sha,
            "version": validated["version"],
        })

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
        allow_runtime_install=opencode_runtime_install_allowed(
            os.environ.get(ENV_ALLOW_RUNTIME_INSTALL)
            if os.environ.get(ENV_ALLOW_RUNTIME_INSTALL) not in (None, "")
            else None
        ),
        requested_exact_artifact=resolve_requested_exact_artifact(),
    )


def main() -> None:
    """Entrypoint for `python -m automation.runner_server` (Render start cmd)."""
    port = resolve_port(os.environ.get(ENV_PORT))
    manager = build_manager_from_env()
    server = create_server(port=port, manager=manager)
    # Safe startup log (issue #52): resolved binary path plus version, or
    # the not-ready reason. Never logs secrets; provider credentials stay
    # environment-driven and are never printed here.
    print(
        "runtime-lab runner listening on 0.0.0.0:%d (region=%s model=%s timeout=%.0fs)"
        % (port, manager.region, manager.default_model, manager.job_timeout_seconds),
        flush=True,
    )
    print(
        "opencode binary: path=%s version=%s ready=%s allow_runtime_install=%s detail=%s"
        % (
            getattr(manager, "opencode_resolved_bin", "") or manager.opencode_bin,
            getattr(manager, "opencode_version", "") or "-",
            manager.ready,
            getattr(manager, "allow_runtime_install", True),
            getattr(manager, "opencode_ready_detail", ""),
        ),
        flush=True,
    )
    if getattr(manager, "requested_artifact", None) is not None:
        print(
            "opencode artifact: id=%s sha256=%s ref=%s expected_version=%s ready=%s detail=%s"
            % (
                (manager.requested_artifact or {}).get("artifact_id", ""),
                (manager.requested_artifact or {}).get("artifact_sha256", ""),
                (manager.requested_artifact or {}).get("artifact_ref", ""),
                getattr(manager, "requested_artifact_version", "") or "-",
                getattr(manager, "artifact_ready", False),
                getattr(manager, "artifact_detail", ""),
            ),
            flush=True,
        )
    if not manager.ready and not getattr(manager, "allow_runtime_install", True):
        print(
            "WARNING: opencode binary is absent or non-executable and "
            "RUNNER_ALLOW_RUNTIME_INSTALL=0; /health reports 503 until the "
            "deploy artifact provides .opencode-bin/opencode "
            "(see automation/install-opencode.sh).",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
