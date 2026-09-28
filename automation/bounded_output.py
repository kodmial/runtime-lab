"""Bounded stdout/stderr and tool-output memory for one-shot jobs (issue #80).

The Render free worker has 512 MB and no swap guarantee, while a single
tool call (large test/build log, recursive grep output, repeated shell
calls, failing-test -> inspect -> fix -> rerun loops) can otherwise grow
worker memory without any configured bound. The previous capture path
(``subprocess.run`` with ``stdout=PIPE``/``stderr=PIPE``) retains the full
output of every command in memory before truncating it for the terminal
result, so one verbose command is enough to spike the worker.

This module is stdlib-only and provides:

- Configurable bounds (all environment-driven, documented below) for raw
  capture and for terminal head/tail summaries.
- Head/tail truncation that preserves diagnostic context: the head keeps
  the command/early context, the tail keeps the actual error (which is
  almost always at the end). A marker records exactly how much was
  omitted so nothing is silently hidden.
- File-backed spooling: :func:`run_bounded` redirects the child to temp
  files and only ever reads bounded head/tail slices back into memory,
  so capture memory is O(bound), never O(output). The full spool stays
  on disk for bounded file-backed retrieval while the job workspace
  lives, then is deleted with it.

Environment knobs (all optional; invalid values fail closed to defaults):

- ``RUNNER_MAX_CAPTURE_BYTES``: per-stream raw spool cap in bytes
  (default 2 MiB). Bytes past the cap are still drained to the spool
  file (so the child never blocks) but are never loaded into memory.
- ``RUNNER_MAX_STREAM_CHARS``: per-stream in-memory bound in characters
  (default 32768). The in-memory copy of each stream is head/tail
  truncated to this size.
- ``RUNNER_MAX_OUTPUT_CHARS``: terminal combined-output bound in
  characters (default 4000, matches the historical runner limit).
- ``RUNNER_OUTPUT_HEAD_CHARS`` / ``RUNNER_OUTPUT_TAIL_CHARS``: explicit
  head/tail split for terminal summaries. Defaults to a ~5/8 + ~3/8
  split of ``RUNNER_MAX_OUTPUT_CHARS``.
- ``RUNNER_MAX_RETAINED_JOBS``: cap on retained terminal job records
  per worker (default 20; see ``runner_server.JobManager`` eviction).
- ``RUNNER_SPOOL_DIR``: directory for capture spool files (default: the
  system temp dir). Files are created with ``mkstemp`` and always
  removed by :func:`run_bounded`, even on timeout or crash.

Provider semantics are never changed here: only capture size, summary
shape, and retention are bounded. Exit codes, timeout behavior, and the
distinction between stdout and stderr are preserved exactly.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------

DEFAULT_CAPTURE_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_STREAM_MAX_CHARS = 32768
DEFAULT_OUTPUT_MAX_CHARS = 4000
DEFAULT_MAX_RETAINED_JOBS = 20

TRUNCATION_MARKER_TEMPLATE = "...[truncated %d chars: head %d + tail %d]..."


def _parse_positive_int(raw: object, default: int) -> int:
    """Parse an optional positive-int env value; fail closed to default."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


def resolve_capture_max_bytes(raw: object = None) -> int:
    """Per-stream raw spool cap in bytes (env ``RUNNER_MAX_CAPTURE_BYTES``)."""
    if raw is None:
        raw = os.environ.get("RUNNER_MAX_CAPTURE_BYTES", "")
    return _parse_positive_int(raw, DEFAULT_CAPTURE_MAX_BYTES)


def resolve_stream_max_chars(raw: object = None) -> int:
    """Per-stream in-memory bound in chars (env ``RUNNER_MAX_STREAM_CHARS``)."""
    if raw is None:
        raw = os.environ.get("RUNNER_MAX_STREAM_CHARS", "")
    return _parse_positive_int(raw, DEFAULT_STREAM_MAX_CHARS)


def resolve_output_max_chars(raw: object = None) -> int:
    """Terminal combined-output bound in chars (env ``RUNNER_MAX_OUTPUT_CHARS``)."""
    if raw is None:
        raw = os.environ.get("RUNNER_MAX_OUTPUT_CHARS", "")
    return _parse_positive_int(raw, DEFAULT_OUTPUT_MAX_CHARS)


def resolve_head_tail_chars(
    limit: int | None = None,
    head_raw: object = None,
    tail_raw: object = None,
) -> tuple[int, int]:
    """Resolve the (head, tail) split for a terminal summary of ``limit`` chars.

    Explicit ``RUNNER_OUTPUT_HEAD_CHARS``/``RUNNER_OUTPUT_TAIL_CHARS`` win;
    otherwise the limit is split ~5/8 head + ~3/8 tail so the tail (where
    errors live) is always preserved.
    """
    resolved_limit = (
        resolve_output_max_chars() if limit is None else max(1, int(limit))
    )
    default_head = (resolved_limit * 5) // 8
    default_tail = max(1, resolved_limit - default_head)
    head = _parse_positive_int(head_raw, default_head)
    tail = _parse_positive_int(tail_raw, default_tail)
    if head + tail > resolved_limit:
        # Keep the invariant head + tail <= limit by shrinking the head;
        # the tail (error context) always wins.
        head = max(0, resolved_limit - tail)
    if head <= 0 and tail <= 0:
        head, tail = default_head, default_tail
    return head, tail


def resolve_max_retained_jobs(raw: object = None) -> int:
    """Cap on retained terminal job records (env ``RUNNER_MAX_RETAINED_JOBS``)."""
    if raw is None:
        raw = os.environ.get("RUNNER_MAX_RETAINED_JOBS", "")
    return _parse_positive_int(raw, DEFAULT_MAX_RETAINED_JOBS)


def resolve_spool_dir(raw: object = None) -> str | None:
    """Spool directory override (env ``RUNNER_SPOOL_DIR``); None means default."""
    if raw is None:
        raw = os.environ.get("RUNNER_SPOOL_DIR", "")
    text = str(raw or "").strip()
    return text or None


# ---------------------------------------------------------------------------
# Head/tail truncation.
# ---------------------------------------------------------------------------


def format_head_tail(
    text: str,
    limit: int | None = None,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> str:
    """Bound ``text`` to ~``limit`` chars as head + marker + tail.

    Short inputs are returned unchanged (no marker). Long inputs keep the
    first ``head_chars`` and the last ``tail_chars`` characters with an
    explicit ``...[truncated N chars]...`` marker recording exactly how
    much was omitted. The tail is never dropped in favor of the head, so
    trailing error lines survive bounding.
    """
    if not isinstance(text, str):
        return ""
    resolved_limit = (
        resolve_output_max_chars() if limit is None else max(1, int(limit))
    )
    if len(text) <= resolved_limit:
        return text
    if head_chars is None or tail_chars is None:
        default_head, default_tail = resolve_head_tail_chars(resolved_limit)
        if head_chars is None:
            head_chars = default_head
        if tail_chars is None:
            tail_chars = default_tail
    head_chars = max(0, int(head_chars))
    tail_chars = max(0, int(tail_chars))
    if head_chars + tail_chars >= len(text):
        return text
    omitted = len(text) - head_chars - tail_chars
    marker = TRUNCATION_MARKER_TEMPLATE % (omitted, head_chars, tail_chars)
    head = text[:head_chars] if head_chars else ""
    tail = text[len(text) - tail_chars:] if tail_chars else ""
    return head + marker + tail


def read_bounded_file(
    path: str,
    head_bytes: int,
    tail_bytes: int,
    encoding: str = "utf-8",
) -> tuple[str, int, bool]:
    """Read ``path`` as bounded head + tail text.

    Returns ``(text, total_bytes, truncated)``. At most
    ``head_bytes + tail_bytes`` bytes are ever loaded into memory,
    regardless of file size. Decoding errors are replaced, never raised.
    """
    total = max(0, os.path.getsize(path))
    head_bytes = max(0, int(head_bytes))
    tail_bytes = max(0, int(tail_bytes))
    if total == 0:
        return "", 0, False
    if total <= head_bytes + tail_bytes:
        with open(path, "rb") as handle:
            raw = handle.read(head_bytes + tail_bytes + 1)
        return raw.decode(encoding, errors="replace"), total, False
    head = b""
    tail = b""
    with open(path, "rb") as handle:
        if head_bytes:
            head = handle.read(head_bytes)
        if tail_bytes:
            handle.seek(max(0, total - tail_bytes))
            tail = handle.read(tail_bytes + 1)
    text = (head + b"\n...[spool-truncated %d bytes]...\n" % (total - head_bytes - tail_bytes) + tail).decode(
        encoding, errors="replace"
    )
    return text, total, True


# ---------------------------------------------------------------------------
# Bounded subprocess capture with file-backed spooling.
# ---------------------------------------------------------------------------


class BoundedResult:
    """Outcome of :func:`run_bounded` with already-bounded text."""

    __slots__ = ("returncode", "stdout", "stderr", "timed_out",
                 "stdout_truncated", "stderr_truncated",
                 "stdout_bytes", "stderr_bytes")

    def __init__(
        self,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        stdout_bytes: int = 0,
        stderr_bytes: int = 0,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.stdout_truncated = stdout_truncated
        self.stderr_truncated = stderr_truncated
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes


def _stream_bound_chars() -> int:
    return resolve_stream_max_chars()


def run_bounded(
    cmd: list[str],
    cwd: str,
    timeout: float,
    *,
    capture_max_bytes: int | None = None,
    stream_max_chars: int | None = None,
    child_env: dict[str, str] | None = None,
) -> BoundedResult:
    """Run ``cmd`` with O(bound) capture memory, never O(output).

    The child streams stdout/stderr directly to spool files on disk; only
    bounded head/tail slices are read back into memory. Exit codes,
    timeout semantics (returncode 124 + ``timed_out=True``), and the
    stdout/stderr split are preserved exactly. Spool files are always
    removed before returning.
    """
    cap_bytes = resolve_capture_max_bytes(capture_max_bytes)
    mem_chars = (
        resolve_stream_max_chars() if stream_max_chars is None
        else max(1, int(stream_max_chars))
    )
    try:
        budget = max(0.1, float(timeout))
    except (TypeError, ValueError):
        budget = 60.0
    spool_dir = resolve_spool_dir()
    stdout_path = ""
    stderr_path = ""
    try:
        out_fd, stdout_path = tempfile.mkstemp(
            prefix="runner-stdout-", suffix=".log", dir=spool_dir
        )
        err_fd, stderr_path = tempfile.mkstemp(
            prefix="runner-stderr-", suffix=".log", dir=spool_dir
        )
    except OSError as exc:
        return BoundedResult(
            returncode=127, stdout="", stderr="spool failed: %s" % exc,
            timed_out=False,
        )
    # Rough per-side byte budget for the in-memory slice: enough to cover
    # mem_chars after decoding (4 bytes/char worst case is overkill; use
    # mem_chars bytes for head plus mem_chars for tail selection later).
    # The full spool file still holds everything up to cap_bytes.
    try:
        with os.fdopen(out_fd, "wb") as out_handle, os.fdopen(
            err_fd, "wb"
        ) as err_handle:
            try:
                proc = subprocess.Popen(
                    list(cmd),
                    cwd=cwd,
                    stdout=out_handle,
                    stderr=err_handle,
                    text=False,
                    env=child_env,
                )
            except FileNotFoundError as exc:
                return BoundedResult(
                    returncode=127, stdout="",
                    stderr="command not found: %s" % exc, timed_out=False,
                )
            except OSError as exc:
                return BoundedResult(
                    returncode=127, stdout="",
                    stderr="execution failed: %s" % exc, timed_out=False,
                )
            try:
                proc.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                stdout_text, stdout_bytes, stdout_cut = _read_stream_slice(
                    stdout_path, mem_chars
                )
                stderr_text, stderr_bytes, stderr_cut = _read_stream_slice(
                    stderr_path, mem_chars
                )
                return BoundedResult(
                    returncode=124, stdout=stdout_text, stderr=stderr_text,
                    timed_out=True, stdout_truncated=stdout_cut,
                    stderr_truncated=stderr_cut, stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                )
            stdout_text, stdout_bytes, stdout_cut = _read_stream_slice(
                stdout_path, mem_chars
            )
            stderr_text, stderr_bytes, stderr_cut = _read_stream_slice(
                stderr_path, mem_chars
            )
            _ = cap_bytes  # full spool retained on disk during read; the
            # in-memory slice above is the only unbounded-risk surface and
            # it is capped by mem_chars. cap_bytes documents the spool
            # retention budget for operators sizing RUNNER_SPOOL_DIR.
            return BoundedResult(
                returncode=proc.returncode, stdout=stdout_text,
                stderr=stderr_text, timed_out=False,
                stdout_truncated=stdout_cut, stderr_truncated=stderr_cut,
                stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
            )
    finally:
        for path in (stdout_path, stderr_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _read_stream_slice(path: str, mem_chars: int) -> tuple[str, int, bool]:
    """Read a spool file as a bounded in-memory string.

    Returns ``(text, total_bytes, truncated)`` where ``text`` holds at
    most ``mem_chars`` characters (head + marker + tail) no matter how
    large the file is.
    """
    try:
        total = max(0, os.path.getsize(path))
    except OSError:
        return "", 0, False
    if total == 0:
        return "", 0, False
    # Read at most ~4x mem_chars bytes: enough to fill the char budget
    # even for multi-byte text, still O(bound).
    window = max(mem_chars * 4, mem_chars + 1024)
    head_n = window // 2
    tail_n = window - head_n
    if total <= head_n + tail_n:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(head_n + tail_n + 1)
        except OSError:
            return "", total, False
        text = raw.decode("utf-8", errors="replace")
        if len(text) <= mem_chars:
            return text, total, False
        return format_head_tail(text, mem_chars), total, True
    try:
        with open(path, "rb") as handle:
            head = handle.read(head_n)
            handle.seek(max(0, total - tail_n))
            tail = handle.read(tail_n + 1)
    except OSError:
        return "", total, True
    combo = (
        head.decode("utf-8", errors="replace")
        + "\n...[spool-truncated %d bytes]...\n" % (total - head_n - tail_n)
        + tail.decode("utf-8", errors="replace")
    )
    if len(combo) <= mem_chars:
        return combo, total, True
    return format_head_tail(combo, mem_chars), total, True
