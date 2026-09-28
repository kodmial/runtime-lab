"""Bounded OpenCode session history and tool-output memory (issue #80).

Stdlib only. Runtime-lab side of the P0 parallel peak-memory hypothesis:
large session history, shell/test output, tool results, or stale task state
must never grow memory without a configured bound during one-shot coding
jobs.

What this module provides:

- Fresh process/session discipline: each issue execution starts a fresh
  OpenCode process/session by default; history is never reused across
  GitHub issues. Helpers build an isolated session env and fail closed
  when a command or env tries to resume shared history.
- Safe configurable bounds for stdout/stderr and tool results that keep
  head+tail diagnostic context (never silently drop errors) plus a
  bounded file-backed retrieval path for the full payload.
- Bounded streaming/spooling: :class:`BoundedSpool` keeps only a bounded
  in-memory head+tail window while spilling the full byte stream to a
  temp file, instead of retaining unbounded strings/buffers.
- Compaction/filtering before load: :func:`compact_session_messages` and
  :func:`select_messages_for_load` truncate and drop obsolete payloads
  before they are loaded or retained where the architecture permits.
- Retention audit: :func:`retention_points` traces where tool
  outputs/session messages are duplicated or retained in this repo and in
  the ``kodmial/opencode`` fork, so each point can carry a bound.
- Stress workloads: deterministic generators for large build/test logs,
  recursive/search output, repeated shell calls, and
  fail-inspect-fix-rerun sequences, with bounded-vs-unbounded accounting.
- Fork patch plan: additive ``kodmial/opencode`` change list implementing
  the same bounds in the fork (isolated branch/ref owned by the workflow);
  provider semantics are never changed to reduce memory.

Memory model: every public helper guarantees an in-memory upper bound
derived from the active config. Full payloads live on disk (spool files)
and are read back only through bounded windows.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

SPEC_SCHEMA = "runtime-lab-opencode-bounds-spec/v1"
FORK_REPO = "kodmial/opencode"
UPSTREAM_REPO = "anomalyco/opencode"
PINNED_OPENCODE_VERSION = "1.18.33"
FORK_BASE_SHA = "9000e7fc8d96c845512f7c73122431418a71d4e4"

# ---------------------------------------------------------------------------
# Configurable bounds (safe defaults, all overridable via explicit config or
# OPENCODE_BOUNDS_* env vars; never unbounded).
# ---------------------------------------------------------------------------

DEFAULT_MAX_STDOUT_BYTES = 262144  # 256 KiB per stream capture
DEFAULT_MAX_STDERR_BYTES = 262144  # 256 KiB per stream capture
DEFAULT_MAX_TOOL_RESULT_CHARS = 32768  # 32k chars kept in memory per result
DEFAULT_MAX_SESSION_MESSAGES = 200  # messages kept after compaction
DEFAULT_MAX_SESSION_BYTES = 1048576  # 1 MiB total session content in memory
DEFAULT_SPOOL_THRESHOLD_BYTES = 65536  # spill to disk above 64 KiB
DEFAULT_HEAD_FRACTION = 0.5  # head share of any bounded window

TRUNCATION_MARKER = "\n...[truncated %d of %d chars; full payload spooled%s]\n"

# Session/history reuse must never happen silently across issues.
FORBIDDEN_SESSION_FLAGS = ("--continue", "--session", "--resume", "--session-id")
FORBIDDEN_SESSION_ENV = (
    "OPENCODE_SESSION",
    "OPENCODE_SESSION_ID",
    "OPENCODE_CONTINUE",
    "OPENCODE_RESUME",
)
FRESH_SESSION_ID_ENV = "OPENCODE_FRESH_SESSION"
SESSION_DIR_ENV_VARS = ("OPENCODE_SESSION_DIR", "OPENCODE_DATA_DIR", "XDG_DATA_HOME")

# Fork-side provider tree: bounds work must never rewrite provider semantics.
PROVIDER_GUARD_PATH_MARKERS = ("src/provider/", "src/model/")
PROVIDER_GUARD_CONTENT_MARKERS = ("api.openai.com", "api.anthropic.com")


def default_bounded_config() -> dict:
    """Return the default bounded-memory config (all bounds finite)."""
    return {
        "schema": SPEC_SCHEMA,
        "max_stdout_bytes": DEFAULT_MAX_STDOUT_BYTES,
        "max_stderr_bytes": DEFAULT_MAX_STDERR_BYTES,
        "max_tool_result_chars": DEFAULT_MAX_TOOL_RESULT_CHARS,
        "max_session_messages": DEFAULT_MAX_SESSION_MESSAGES,
        "max_session_bytes": DEFAULT_MAX_SESSION_BYTES,
        "spool_threshold_bytes": DEFAULT_SPOOL_THRESHOLD_BYTES,
        "head_fraction": DEFAULT_HEAD_FRACTION,
        "fresh_session_per_issue": True,
    }


def validate_bounded_config(config: dict) -> dict:
    """Validate a bounded config; fail closed on any missing/unbounded value."""
    if not isinstance(config, dict):
        raise ValueError("bounded config must be a mapping")
    for key in (
        "max_stdout_bytes",
        "max_stderr_bytes",
        "max_tool_result_chars",
        "max_session_messages",
        "max_session_bytes",
        "spool_threshold_bytes",
    ):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("%s must be a positive int" % key)
    head = config.get("head_fraction")
    if not isinstance(head, (int, float)) or isinstance(head, bool):
        raise ValueError("head_fraction must be a number")
    if not 0.0 < float(head) < 1.0:
        raise ValueError("head_fraction must be in (0, 1)")
    if config.get("fresh_session_per_issue") is not True:
        raise ValueError("fresh_session_per_issue must be true")
    if config.get("schema", SPEC_SCHEMA) != SPEC_SCHEMA:
        raise ValueError("invalid bounded config schema")
    return config


def bounded_config_from_env(
    environ: object | None = None, base: dict | None = None
) -> dict:
    """Overlay OPENCODE_BOUNDS_* env vars onto a validated config."""
    source = os.environ if environ is None else environ
    if not hasattr(source, "get"):
        raise ValueError("environ must be a mapping")
    config = dict(base) if base is not None else default_bounded_config()
    mapping = {
        "OPENCODE_BOUNDS_MAX_STDOUT_BYTES": "max_stdout_bytes",
        "OPENCODE_BOUNDS_MAX_STDERR_BYTES": "max_stderr_bytes",
        "OPENCODE_BOUNDS_MAX_TOOL_RESULT_CHARS": "max_tool_result_chars",
        "OPENCODE_BOUNDS_MAX_SESSION_MESSAGES": "max_session_messages",
        "OPENCODE_BOUNDS_MAX_SESSION_BYTES": "max_session_bytes",
        "OPENCODE_BOUNDS_SPOOL_THRESHOLD_BYTES": "spool_threshold_bytes",
    }
    for env_name, key in mapping.items():
        raw = source.get(env_name, "")  # type: ignore[attr-defined]
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ValueError("invalid %s: %r" % (env_name, raw)) from exc
        config[key] = value
    raw_head = source.get("OPENCODE_BOUNDS_HEAD_FRACTION", "")  # type: ignore[attr-defined]
    if raw_head is not None and str(raw_head).strip() != "":
        try:
            config["head_fraction"] = float(str(raw_head).strip())
        except ValueError as exc:
            raise ValueError("invalid OPENCODE_BOUNDS_HEAD_FRACTION") from exc
    return validate_bounded_config(config)


# ---------------------------------------------------------------------------
# Head/tail bounding (errors and tail context are never hidden).
# ---------------------------------------------------------------------------


def _split_head_tail(text: str, limit: int, head_fraction: float) -> tuple[str, str]:
    head_len = int(limit * float(head_fraction))
    head_len = max(0, min(limit, head_len))
    tail_len = limit - head_len
    return text[:head_len], text[len(text) - tail_len :] if tail_len else ""


def bound_text(
    text: str,
    limit: int,
    head_fraction: float = DEFAULT_HEAD_FRACTION,
    spool_path: str = "",
) -> str:
    """Bound text to at most ``limit`` chars, keeping head+tail context.

    Short inputs pass through untouched (no marker). Long inputs keep a
    head window and a tail window (errors usually surface at the tail)
    plus a marker recording how much was elided and where the full
    payload lives. The result never exceeds ``limit`` plus the marker.
    """
    if not isinstance(text, str):
        return ""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive int")
    if len(text) <= limit:
        return text
    head, tail = _split_head_tail(text, limit, head_fraction)
    where = (" at %s" % spool_path) if spool_path else ""
    marker = TRUNCATION_MARKER % (len(text) - limit, len(text), where)
    return head + marker + tail


def bound_bytes(
    payload: bytes,
    limit_bytes: int,
    head_fraction: float = DEFAULT_HEAD_FRACTION,
) -> tuple[bytes, bool]:
    """Bound a byte payload; return (window, truncated)."""
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("payload must be bytes")
    if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int):
        raise ValueError("limit_bytes must be a positive int")
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be a positive int")
    raw = bytes(payload)
    if len(raw) <= limit_bytes:
        return raw, False
    head_len = int(limit_bytes * float(head_fraction))
    head_len = max(0, min(limit_bytes, head_len))
    tail_len = limit_bytes - head_len
    window = raw[:head_len] + (raw[len(raw) - tail_len :] if tail_len else b"")
    return window, True


# ---------------------------------------------------------------------------
# Bounded streaming/spooling: bounded memory regardless of stream size.
# ---------------------------------------------------------------------------


class BoundedSpool:
    """Stream-capture with a bounded in-memory window plus a spool file.

    ``write()`` accepts bytes or str chunks of any size. At most
    ``memory_limit`` chars are ever retained in memory (head+tail); the
    full stream spills to a temp file once it exceeds ``spool_threshold``.
    Use :meth:`snapshot` for the bounded agent-visible window and
    :meth:`read_window` for bounded file-backed retrieval.
    """

    def __init__(
        self,
        memory_limit: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
        spool_threshold: int = DEFAULT_SPOOL_THRESHOLD_BYTES,
        head_fraction: float = DEFAULT_HEAD_FRACTION,
        spool_dir: str | None = None,
    ) -> None:
        if isinstance(memory_limit, bool) or memory_limit <= 0:
            raise ValueError("memory_limit must be a positive int")
        if isinstance(spool_threshold, bool) or spool_threshold <= 0:
            raise ValueError("spool_threshold must be a positive int")
        self.memory_limit = int(memory_limit)
        self.spool_threshold = int(spool_threshold)
        self.head_fraction = float(head_fraction)
        if not 0.0 < self.head_fraction < 1.0:
            raise ValueError("head_fraction must be in (0, 1)")
        self._total_chars = 0
        self._head: list[str] = []
        self._head_len = 0
        self._tail: str = ""
        self._spool_path = ""
        self._spool_handle = None
        self._spool_dir = spool_dir
        self._closed = False

    @property
    def total_chars(self) -> int:
        return self._total_chars

    @property
    def spool_path(self) -> str:
        return self._spool_path

    def _ensure_spool(self) -> None:
        if self._spool_handle is not None:
            return
        fd, path = tempfile.mkstemp(
            prefix="opencode-bounds-spool-", suffix=".log", dir=self._spool_dir
        )
        handle = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
        self._spool_path = path
        self._spool_handle = handle

    def write(self, chunk: str | bytes) -> int:
        """Append one chunk; return total chars so far. Memory stays bounded."""
        if self._closed:
            raise ValueError("spool is closed")
        if isinstance(chunk, (bytes, bytearray)):
            text = bytes(chunk).decode("utf-8", errors="replace")
        else:
            text = chunk if isinstance(chunk, str) else str(chunk)
        if not text:
            return self._total_chars
        self._total_chars += len(text)
        if self._total_chars > self.spool_threshold or self._spool_handle is not None:
            self._ensure_spool()
            assert self._spool_handle is not None
            self._spool_handle.write(text)
        # In-memory window: bounded head plus a bounded tail of the stream.
        head_budget = int(self.memory_limit * self.head_fraction)
        if self._head_len < head_budget:
            room = head_budget - self._head_len
            self._head.append(text[:room])
            self._head_len += min(room, len(text))
        tail_budget = self.memory_limit - head_budget
        if tail_budget <= 0:
            self._tail = ""
        else:
            self._tail = (self._tail + text)[-tail_budget:]
        return self._total_chars

    def memory_chars(self) -> int:
        """Current in-memory footprint (chars); always <= memory_limit."""
        return len("".join(self._head)) + len(self._tail)

    def snapshot(self) -> str:
        """Bounded in-memory head+tail view with a truncation marker."""
        head = "".join(self._head)
        if self._total_chars <= self.memory_limit:
            return head + self._tail[len(head) :] if self._tail.startswith("") else head
        # Reconstruct: full head budget + tail budget with marker.
        tail_budget = self.memory_limit - len(head)
        tail = self._tail[-tail_budget:] if tail_budget > 0 else ""
        where = (" at %s" % self._spool_path) if self._spool_path else ""
        marker = TRUNCATION_MARKER % (
            self._total_chars - self.memory_limit,
            self._total_chars,
            where,
        )
        return head + marker + tail

    def read_window(self, max_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS) -> str:
        """Bounded file-backed retrieval (head+tail of the spooled payload)."""
        if isinstance(max_chars, bool) or max_chars <= 0:
            raise ValueError("max_chars must be a positive int")
        if self._spool_handle is None:
            return bound_text(
                "".join(self._head) + self._tail,
                max_chars,
                self.head_fraction,
                self._spool_path,
            )
        try:
            self._spool_handle.flush()
            with open(self._spool_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            return self.snapshot()
        return bound_text(content, max_chars, self.head_fraction, self._spool_path)

    def close(self) -> str:
        """Flush and close the spool file; return its path (or '')."""
        self._closed = True
        if self._spool_handle is not None:
            try:
                self._spool_handle.close()
            except OSError:
                pass
            self._spool_handle = None
        return self._spool_path

    def dispose(self) -> None:
        """Close and delete the spool file; never raises."""
        self.close()
        if self._spool_path:
            try:
                os.unlink(self._spool_path)
            except OSError:
                pass
            self._spool_path = ""


# ---------------------------------------------------------------------------
# Fresh process/session discipline (one-shot jobs never reuse history).
# ---------------------------------------------------------------------------


def fresh_session_env(
    environ: object | None = None,
    issue_number: int = 0,
    run_id: str = "",
    session_dir: str | None = None,
) -> dict[str, str]:
    """Build a child env that starts a fresh OpenCode session.

    Copies the base env, drops every history-resume variable, marks the
    session fresh, and isolates the session/data dir per issue+run so two
    GitHub issues can never share retained state.
    """
    source = os.environ if environ is None else environ
    if not hasattr(source, "items"):
        raise ValueError("environ must be a mapping")
    cleaned = {str(k): str(v) for k, v in dict(source).items()}  # type: ignore[attr-defined]
    for name in FORBIDDEN_SESSION_ENV:
        cleaned.pop(name, None)
    cleaned[FRESH_SESSION_ID_ENV] = "1"
    if session_dir:
        target = session_dir
    else:
        suffix = "issue-%s-run-%s" % (
            str(issue_number) if issue_number else "adhoc",
            str(run_id) if run_id else "local",
        )
        target = os.path.join(tempfile.gettempdir(), "opencode-fresh-%s" % suffix)
    cleaned[SESSION_DIR_ENV_VARS[0]] = target
    return cleaned


def assert_fresh_session(command: list[str] | tuple, environ: object | None = None) -> None:
    """Fail closed when a command/env would reuse history across issues."""
    parts = [str(p) for p in (command or [])]
    lowered = [p.lower() for p in parts]
    for forbidden in FORBIDDEN_SESSION_FLAGS:
        if forbidden in lowered:
            raise ValueError(
                "OpenCode invocation must start a fresh session; "
                "forbidden flag %r" % forbidden
            )
        for part in lowered:
            if part.startswith(forbidden + "="):
                raise ValueError(
                    "OpenCode invocation must start a fresh session; "
                    "forbidden flag %r" % part
                )
    source = os.environ if environ is None else environ
    if source is not None and hasattr(source, "get"):
        present = [
            n
            for n in FORBIDDEN_SESSION_ENV
            if str(source.get(n, "") or "").strip()  # type: ignore[attr-defined]
        ]
        if present:
            raise ValueError(
                "worker env must not carry session-resume variables "
                "(present: %s)" % ", ".join(sorted(present))
            )


def build_bounded_opencode_command(
    model: str,
    task_text: str,
    opencode_bin: str = "opencode",
    environ: object | None = None,
) -> list[str]:
    """Build the fresh-session ``opencode run`` command and validate it.

    Delegates model/task validation to ``automation.opencode_runner`` so
    provider semantics stay identical; this layer only enforces the
    fresh-session invariant (no ``--continue``/``--session``/``--resume``).
    """
    try:
        try:
            from automation.opencode_runner import build_opencode_command
        except ImportError:
            from opencode_runner import build_opencode_command  # type: ignore[no-redef]
    except ImportError as exc:
        raise ValueError("opencode_runner is required for command construction") from exc
    command = build_opencode_command(model, task_text, opencode_bin=opencode_bin)
    assert_fresh_session(command, environ)
    return command


# ---------------------------------------------------------------------------
# Compaction / filtering before load (obsolete payloads never load first).
# ---------------------------------------------------------------------------


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _message_is_error(message: dict) -> bool:
    if message.get("is_error") is True or message.get("role") == "error":
        return True
    text = _message_text(message).lower()
    return ("error" in text and "exit" in text) or "traceback" in text or "failed" in text


def compact_session_messages(
    messages: list[dict], config: dict | None = None
) -> tuple[list[dict], dict]:
    """Compact session history before it is loaded or retained.

    - Truncates every message content to ``max_tool_result_chars``
      (head+tail, errors preserved at the tail).
    - Keeps error messages plus the most recent messages up to
      ``max_session_messages`` / ``max_session_bytes``.
    - Returns (compacted, report) where report counts dropped messages and
      elided chars so callers can prove the bound.
    """
    active = validate_bounded_config(dict(config) if config else default_bounded_config())
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    truncated: list[dict] = []
    elided_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be a mapping")
        text = _message_text(message)
        bounded = bound_text(text, active["max_tool_result_chars"], active["head_fraction"])
        elided_chars += max(0, len(text) - len(bounded))
        item = dict(message)
        item["content"] = bounded
        truncated.append(item)
    # Partition: errors are always kept; the rest is recency-filtered.
    # (Index-based: messages may carry identical content, so identity by
    # position is required -- dict equality would match every duplicate.)
    error_idx = {i for i, m in enumerate(truncated) if _message_is_error(m)}
    rest_idx = [i for i in range(len(truncated)) if i not in error_idx]
    keep_count = max(0, active["max_session_messages"] - len(error_idx))
    selected_idx = sorted(error_idx | set(rest_idx[-keep_count:] if keep_count else []))
    selected = [truncated[i] for i in selected_idx]
    error_ids = {id(truncated[i]) for i in error_idx if i in selected_idx}
    total = sum(len(_message_text(m)) for m in selected)
    while len(selected) > 0 and total > active["max_session_bytes"]:
        # Drop the oldest non-error first; errors are the last resort.
        dropped = False
        for candidate in list(selected):
            if id(candidate) not in error_ids:
                selected.remove(candidate)
                total -= len(_message_text(candidate))
                dropped = True
                break
        if not dropped:
            victim = selected.pop(0)
            total -= len(_message_text(victim))
    dropped_messages = len(truncated) - len(selected)
    kept_ids = {id(m) for m in selected}
    report = {
        "input_messages": len(messages),
        "kept_messages": len(selected),
        "dropped_messages": dropped_messages,
        "kept_error_messages": sum(1 for m in selected if _message_is_error(m)),
        "elided_chars": elided_chars,
        "kept_bytes": sum(len(_message_text(m)) for m in selected),
        "max_session_messages": active["max_session_messages"],
        "max_session_bytes": active["max_session_bytes"],
    }
    return selected, report


def select_messages_for_load(
    messages: list[dict], config: dict | None = None
) -> tuple[list[dict], dict]:
    """Filter obsolete payloads before loading (compaction-first entrypoint)."""
    return compact_session_messages(messages, config)


# ---------------------------------------------------------------------------
# Retention audit: where outputs/messages are duplicated or retained.
# ---------------------------------------------------------------------------


def retention_points() -> list[dict[str, str]]:
    """Trace every place tool outputs/session messages are retained.

    Covers the runtime-lab controller path plus the fork-side bounds
    targets. Each point names the bound that now applies.
    """
    return [
        {
            "location": "automation/runner_server.py:SubprocessCommandRunner.run",
            "retained": "full stdout/stderr strings per command (PIPE capture)",
            "bound": "route OpenCode/tool output through BoundedSpool with "
            "max_stdout/max_stderr_bytes head+tail windows",
        },
        {
            "location": "automation/runner_server.py:JobRecord.output",
            "retained": "sanitized terminal output duplicated from CommandResult",
            "bound": "store bound_text window + spool path, never the full string twice",
        },
        {
            "location": "automation/opencode_runner.py:truncate/sanitize_output",
            "retained": "8000-char terminal truncation (head-only, tail errors lost)",
            "bound": "replace with bound_text head+tail windows so tail errors survive",
        },
        {
            "location": "automation/memory_benchmark.py:run_with_peak stdout/stderr",
            "retained": "first 500 chars of stdout/stderr per probe (duplicated tree)",
            "bound": "bounded spool snapshot; full logs only via bounded file windows",
        },
        {
            "location": "kodmial/opencode: session message store",
            "retained": "unbounded per-message tool results accumulated per session",
            "bound": "max_tool_result_chars per message + max_session_messages/bytes "
            "compaction before load (bounded_patch_plan step 1)",
        },
        {
            "location": "kodmial/opencode: shell/tool stdout/stderr buffers",
            "retained": "unbounded in-memory command output strings",
            "bound": "streaming BoundedSpool capture with spool_threshold spill "
            "and bounded in-memory head+tail (bounded_patch_plan step 2)",
        },
        {
            "location": "kodmial/opencode: task/subagent transcript fan-out",
            "retained": "stale subagent transcripts re-loaded on every rerun",
            "bound": "fresh process/session per issue plus compaction-first load "
            "that drops obsolete transcripts (bounded_patch_plan step 3)",
        },
    ]


def estimate_retained_bytes(text: str, config: dict | None = None) -> dict[str, int]:
    """Compare unbounded vs bounded retention for one payload (bytes)."""
    active = validate_bounded_config(dict(config) if config else default_bounded_config())
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    unbounded = len(text.encode("utf-8", errors="replace"))
    bounded = len(
        bound_text(text, active["max_tool_result_chars"], active["head_fraction"]).encode(
            "utf-8", errors="replace"
        )
    )
    spool = BoundedSpool(
        memory_limit=active["max_tool_result_chars"],
        spool_threshold=active["spool_threshold_bytes"],
        head_fraction=active["head_fraction"],
    )
    spool.write(text)
    in_memory = len(spool.snapshot().encode("utf-8", errors="replace"))
    spool.dispose()
    return {
        "unbounded_bytes": unbounded,
        "bounded_bytes": bounded,
        "spooled_in_memory_bytes": in_memory,
    }


# ---------------------------------------------------------------------------
# Fork patch plan (isolated branch/ref; additive; no provider rewrite).
# ---------------------------------------------------------------------------


def bounded_patch_plan() -> list[dict[str, str]]:
    """Additive fork-side patch plan implementing the bounds in kodmial/opencode."""
    return [
        {
            "path": "packages/opencode/src/session/bounds.ts",
            "action": "add",
            "purpose": "Central bounded-memory config (max stdout/stderr bytes, "
            "max tool-result chars, max session messages/bytes, spool "
            "threshold, head fraction) with env overrides; all bounds "
            "required positive so no call site can grow without a cap.",
        },
        {
            "path": "packages/opencode/src/session/spool.ts",
            "action": "add",
            "purpose": "Bounded streaming spool: bounded in-memory head+tail "
            "window plus temp-file spill; agent-visible snapshot and "
            "file-backed read_window replace unbounded string buffers.",
        },
        {
            "path": "packages/opencode/src/session/store.ts",
            "action": "modify-bounded",
            "purpose": "Cap the session message store per message and per "
            "session; compact/filter obsolete tool outputs before load; "
            "fresh process/session per one-shot job (no cross-issue reuse).",
        },
        {
            "path": "packages/opencode/src/tool/output.ts",
            "action": "modify-bounded",
            "purpose": "Stream tool stdout/stderr through the spool; keep "
            "head+tail diagnostic context (tail errors survive) and expose "
            "the spool path for bounded retrieval instead of full strings.",
        },
        {
            "path": "packages/opencode/src/cli/cmd/run-coding.ts",
            "action": "extend-bounded",
            "purpose": "Wire fresh-session defaults and bounded capture into "
            "the coding-only entrypoint; provider/model/session/agent loop "
            "is reused unchanged.",
        },
    ]


def check_no_provider_rewrite(paths: list[str]) -> None:
    """Fail closed when the bounds patch touches provider semantics."""
    for path in paths:
        lowered = str(path).lower()
        if "provider-direct" in lowered or "direct-client" in lowered:
            raise ValueError("bounds patch must not add a direct provider client")
        for marker in PROVIDER_GUARD_PATH_MARKERS:
            if marker in lowered and "bounds" not in lowered and "spool" not in lowered:
                raise ValueError(
                    "bounds patch must not rewrite provider semantics: %s" % path
                )


# ---------------------------------------------------------------------------
# Stress workloads (large logs, search output, repeated calls, reruns).
# ---------------------------------------------------------------------------


STRESS_WORKLOADS: tuple[dict[str, str], ...] = (
    {"id": "s1-large-build-log", "title": "Large build/test log output"},
    {"id": "s2-recursive-search", "title": "Recursive/search output flood"},
    {"id": "s3-repeated-shell", "title": "Repeated shell calls accumulation"},
    {"id": "s4-fail-fix-rerun", "title": "Fail-inspect-fix-rerun sequence"},
)


def stress_workload_ids() -> list[str]:
    return [entry["id"] for entry in STRESS_WORKLOADS]


def generate_large_log(num_lines: int = 5000) -> str:
    """Deterministic large build/test log with the error at the tail."""
    if num_lines <= 0:
        raise ValueError("num_lines must be positive")
    lines = ["[build] step %05d ok" % i for i in range(max(0, num_lines - 3))]
    lines += [
        "[test] 512 passed, 1 failed",
        "ERROR: exit 1 in packages/opencode/src/tool/output.ts line 42",
        "Traceback: assertion failed in bounded spool snapshot",
    ]
    return "\n".join(lines) + "\n"


def generate_search_output(num_matches: int = 3000) -> str:
    """Deterministic recursive-search flood ending with the real hit."""
    if num_matches <= 0:
        raise ValueError("num_matches must be positive")
    lines = ["src/noise/file%05d.ts:1: noise match" % i for i in range(num_matches - 1)]
    lines.append("packages/opencode/src/session/store.ts:88: SESSION_BOUND_HIT")
    return "\n".join(lines) + "\n"


def simulate_repeated_shell_calls(
    num_calls: int = 50, per_call_chars: int = 20000
) -> list[dict]:
    """Build a synthetic accumulated transcript of repeated shell calls."""
    if num_calls <= 0 or per_call_chars <= 0:
        raise ValueError("num_calls and per_call_chars must be positive")
    messages: list[dict] = []
    for i in range(num_calls):
        body = ("call %d output " % i + "x" * 32 + "\n") * max(1, per_call_chars // 48)
        messages.append({"role": "tool", "content": body[:per_call_chars]})
    messages.append({"role": "error", "content": "ERROR: exit 1 on final call; see tail"})
    return messages


def simulate_fail_fix_rerun() -> list[dict]:
    """Fail-inspect-fix-rerun transcript: stale failure logs then the fix."""
    failing_log = generate_large_log(2000)
    return [
        {"role": "tool", "content": failing_log},
        {"role": "tool", "content": failing_log},
        {"role": "agent", "content": "inspected tail error; patching spool snapshot"},
        {"role": "tool", "content": "applied fix to packages/opencode/src/session/spool.ts"},
        {"role": "tool", "content": "rerun: 513 passed, 0 failed"},
    ]


def stress_bounded_capture(config: dict | None = None) -> dict:
    """Run all stress workloads through the bounds; return the accounting.

    Verifies: every bounded window respects its cap, every tail error
    survives, and a single call or accumulation cannot exceed the
    configured in-memory bound.
    """
    active = validate_bounded_config(dict(config) if config else default_bounded_config())
    results: dict = {"workloads": [], "config": active}
    unbounded_total = 0
    bounded_total = 0

    def _check(name: str, payload: str) -> None:
        nonlocal unbounded_total, bounded_total
        unbounded_total += len(payload)
        spool = BoundedSpool(
            memory_limit=active["max_tool_result_chars"],
            spool_threshold=active["spool_threshold_bytes"],
            head_fraction=active["head_fraction"],
        )
        spool.write(payload)
        view = spool.snapshot()
        assert spool.memory_chars() <= active["max_tool_result_chars"], name
        assert len(view) <= active["max_tool_result_chars"] + 512, name
        # Critical error context must survive bounding.
        for needle in ("ERROR", "Traceback", "SESSION_BOUND_HIT"):
            if needle in payload:
                assert needle in view, "%s lost %s" % (name, needle)
        bounded_total += len(view)
        spool.dispose()
        results["workloads"].append(
            {
                "id": name,
                "unbounded_chars": len(payload),
                "bounded_chars": len(view),
            }
        )

    _check("s1-large-build-log", generate_large_log())
    _check("s2-recursive-search", generate_search_output())
    repeated = simulate_repeated_shell_calls()
    compacted, report = compact_session_messages(repeated, active)
    assert len(compacted) <= active["max_session_messages"]
    assert report["kept_error_messages"] >= 1
    unbounded_total += sum(len(_message_text(m)) for m in repeated)
    bounded_total += sum(len(_message_text(m)) for m in compacted)
    results["workloads"].append(
        {
            "id": "s3-repeated-shell",
            "unbounded_chars": sum(len(_message_text(m)) for m in repeated),
            "bounded_chars": sum(len(_message_text(m)) for m in compacted),
            "kept_messages": len(compacted),
            "dropped_messages": report["dropped_messages"],
        }
    )
    rerun = simulate_fail_fix_rerun()
    compacted2, _ = compact_session_messages(rerun, active)
    assert any("rerun" in _message_text(m) for m in compacted2)
    unbounded_total += sum(len(_message_text(m)) for m in rerun)
    bounded_total += sum(len(_message_text(m)) for m in compacted2)
    results["workloads"].append(
        {
            "id": "s4-fail-fix-rerun",
            "unbounded_chars": sum(len(_message_text(m)) for m in rerun),
            "bounded_chars": sum(len(_message_text(m)) for m in compacted2),
        }
    )
    results["unbounded_chars_total"] = unbounded_total
    results["bounded_chars_total"] = bounded_total
    results["reduction_ratio"] = (
        round(1.0 - bounded_total / unbounded_total, 3) if unbounded_total else 0.0
    )
    return results


def representative_coding_task(workspace: str) -> dict[str, str]:
    """Deterministic representative task (bounded variant of the #79 probe).

    Mirrors the issue correctness clause: inspect/search the repository,
    modify files, run build/tests, react to failures. Bounded capture is
    used for every probe so the task itself proves the bound.
    """
    import subprocess as _subprocess

    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    os.makedirs(workspace, exist_ok=True)
    target = os.path.join(workspace, "sample.txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("line one\nline two\n")
    with open(target, "r", encoding="utf-8") as handle:
        before = handle.read()
    if "line two" not in before:
        raise ValueError("representative task: seed content not found")
    matches = [line for line in before.splitlines() if "two" in line]
    if not matches:
        raise ValueError("representative task: search found no match")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("line three\n")
    probe = _subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.abspath(__file__)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    spool = BoundedSpool()
    spool.write(probe.stdout or "")
    spool.write(probe.stderr or "")
    _ = spool.snapshot()
    spool.dispose()
    if probe.returncode != 0:
        raise ValueError("representative task: build probe failed")
    with open(target, "r", encoding="utf-8") as handle:
        after = handle.read()
    if "line three" not in after:
        raise ValueError("representative task: edit did not persist")
    return {"result": "line three", "matches": str(len(matches))}
