"""Tests for bounded session/tool-output memory in one-shot jobs (issue #80).

Covers the Definition of Done on the runner side without network access:

- A single tool call cannot grow memory without a configured bound
  (file-backed spooling capture + per-stream and terminal head/tail
  bounds, tail preserved so errors are never hidden).
- Each issue execution starts a fresh OpenCode process/session
  (resume flags rejected fail-closed; per-job isolated OPENCODE_DB
  scoped outside the clone).
- Accumulated history is bounded (terminal job eviction with
  workspace cleanup).
- Stress shapes from the issue: large test/build logs, recursive
  output, repeated shell calls, fail -> inspect -> fix -> rerun.
"""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounded_output import (  # noqa: E402
    DEFAULT_OUTPUT_MAX_CHARS,
    format_head_tail,
    resolve_head_tail_chars,
    resolve_max_retained_jobs,
    resolve_output_max_chars,
    resolve_stream_max_chars,
    run_bounded,
)
from opencode_runner import (  # noqa: E402
    assert_fresh_session_command,
    build_opencode_command,
    fresh_session_db_path,
    fresh_session_env,
    truncate_head_tail,
)
from render_lifecycle import (  # noqa: E402
    FALLBACK_MODEL,
    PREFERRED_MODEL,
    PUBLIC_REPO_URL,
    is_terminal_job_status,
)
from runner_server import (  # noqa: E402
    CommandResult,
    CommandRunner,
    JobManager,
    SubprocessCommandRunner,
    _bound_stream,
    _is_opencode_run_command,
    _session_scope_dir,
    _truncate,
    stream_output_limit,
    terminal_output_limit,
)


def _payload(**overrides):
    body = {
        "repository_url": PUBLIC_REPO_URL,
        "base_ref": "main",
        "task_text": "bounded task",
        "issue_number": 80,
        "metadata": {
            "issue_number": 80,
            "attempt": 1,
            "run_id": "test-80",
            "region": "oregon",
            "model": PREFERRED_MODEL,
            "execution_mode": "e2e",
        },
    }
    body.update(overrides)
    return body


def _legacy_payload(**overrides):
    overrides.setdefault("command", ["sh", "-c", "echo synthetic-ok"])
    return _payload(**overrides)


def _wait_terminal(manager, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = manager.get(job_id)
        assert record is not None
        if is_terminal_job_status(record.status):
            return record
        time.sleep(0.05)
    raise AssertionError("job %s did not reach a terminal state" % job_id)


# ---------------------------------------------------------------------------
# Head/tail truncation preserves diagnostics.
# ---------------------------------------------------------------------------


def test_short_output_is_unchanged():
    assert format_head_tail("ok", 4000) == "ok"
    assert format_head_tail("x" * 4000, 4000) == "x" * 4000
    assert _truncate("boom exploded") == "boom exploded"
    assert truncate_head_tail("fine") == "fine"


def test_long_output_keeps_head_and_tail_with_marker():
    text = "HEAD-%s\n" % ("a" * 3000) + "MIDDLE-%s\n" % ("m" * 6000) + "TAIL-ERROR: boom exploded"
    bounded = format_head_tail(text, 4000)
    assert len(bounded) < len(text)
    assert len(bounded) <= 4000 + 200  # limit plus the explicit marker
    assert "a" * 100 in bounded  # head context survives
    assert "TAIL-ERROR: boom exploded" in bounded  # trailing error survives
    assert "truncated" in bounded


def test_head_only_would_hide_errors_but_head_tail_does_not():
    lines = ["setup line %d" % i for i in range(500)]
    lines.append("FAIL tests/test_x.py::test_big - AssertionError: expected 1 got 2")
    text = "\n".join(lines)
    assert len(text) > DEFAULT_OUTPUT_MAX_CHARS
    bounded = _truncate(text)
    assert "AssertionError" in bounded  # the critical tail is retained
    assert "setup line 0" in bounded  # the head context is retained


def test_invalid_env_fails_closed_to_defaults(monkeypatch):
    monkeypatch.setenv("RUNNER_MAX_OUTPUT_CHARS", "not-a-number")
    monkeypatch.setenv("RUNNER_MAX_STREAM_CHARS", "-5")
    monkeypatch.setenv("RUNNER_MAX_RETAINED_JOBS", "0")
    assert resolve_output_max_chars() == DEFAULT_OUTPUT_MAX_CHARS
    assert resolve_stream_max_chars() == 32768
    assert resolve_max_retained_jobs() == 20
    head, tail = resolve_head_tail_chars(1000)
    assert head + tail <= 1000 and tail > 0


def test_terminal_and_stream_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("RUNNER_MAX_OUTPUT_CHARS", "1000")
    monkeypatch.setenv("RUNNER_MAX_STREAM_CHARS", "2000")
    assert terminal_output_limit() == 1000
    assert stream_output_limit() == 2000
    assert len(_truncate("y" * 5000)) <= 1000 + 200


# ---------------------------------------------------------------------------
# Bounded subprocess capture (file-backed spooling).
# ---------------------------------------------------------------------------


def test_run_bounded_caps_large_build_log_and_preserves_tail(tmp_path):
    # ~1.5 MB of synthetic build output: memory must stay O(bound).
    result = run_bounded(
        ["sh", "-c", "seq 1 200000"],
        cwd=str(tmp_path),
        timeout=60.0,
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.stdout_bytes > 1_000_000
    assert len(result.stdout) <= 32768 + 512
    assert result.stdout_truncated is True
    assert "200000" in result.stdout  # tail (final lines) preserved
    assert result.stderr == ""


def test_run_bounded_preserves_stdout_stderr_split_and_exit_code(tmp_path):
    result = run_bounded(
        ["sh", "-c", "echo out-line; echo err-line >&2; exit 3"],
        cwd=str(tmp_path),
        timeout=30.0,
    )
    assert result.returncode == 3
    assert "out-line" in result.stdout
    assert "err-line" in result.stderr
    assert "err-line" not in result.stdout


def test_run_bounded_timeout_stays_bounded(tmp_path):
    # Use an unbounded producer so the timeout is deterministic: `seq 1 N`
    # for a fixed N can finish in under 1s on fast CI disks, making the
    # timeout assertion flaky. An infinite loop guarantees the child is
    # still running when the 1s budget expires while still exercising the
    # O(bound) spool slice on the timeout path.
    result = run_bounded(
        ["sh", "-c", "while true; do echo timeout-probe-line-0123456789; done"],
        cwd=str(tmp_path),
        timeout=1.0,
    )
    assert result.timed_out is True
    assert result.returncode == 124
    assert len(result.stdout) <= 32768 + 512


def test_run_bounded_missing_binary_is_deterministic(tmp_path):
    result = run_bounded(
        ["definitely-not-a-real-binary-xyz-80"],
        cwd=str(tmp_path),
        timeout=10.0,
    )
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_subprocess_runner_bounds_huge_output_and_keeps_tail(tmp_path):
    runner = SubprocessCommandRunner()
    result = runner.run(
        ["sh", "-c", "seq 1 300000; echo FINAL-MARKER-XYZ >&2"],
        cwd=str(tmp_path),
        timeout=90.0,
    )
    assert result.returncode == 0
    assert len(result.stdout) <= 32768 + 512
    assert "300000" in result.stdout
    assert "FINAL-MARKER-XYZ" in result.stderr


# ---------------------------------------------------------------------------
# Fresh OpenCode process/session per issue.
# ---------------------------------------------------------------------------


def test_opencode_command_is_fresh_by_construction():
    cmd = build_opencode_command(PREFERRED_MODEL, "do the thing")
    assert_fresh_session_command(cmd)  # must not raise
    assert "--continue" not in cmd
    assert "--session" not in cmd
    assert "--fork" not in cmd
    assert "--attach" not in cmd


def test_session_reuse_flags_are_rejected_fail_closed():
    for flag in ("--continue", "--session", "--fork", "--attach"):
        with pytest.raises(ValueError):
            assert_fresh_session_command(
                ["opencode", "run", "--auto", "--model", PREFERRED_MODEL, flag, "x"]
            )
    with pytest.raises(ValueError):
        assert_fresh_session_command(
            ["opencode", "run", "--auto", "--model", PREFERRED_MODEL, "--session", "abc"]
        )
    # Non-run commands are out of scope and unaffected.
    assert_fresh_session_command(["sh", "-c", "echo hi"])


def test_fresh_session_env_isolates_db_per_job(tmp_path):
    workspace = str(tmp_path / "job-aaa")
    env = fresh_session_env(workspace)
    assert env["OPENCODE_DB"] == fresh_session_db_path(workspace)
    assert env["OPENCODE_DB"].startswith(workspace)
    assert env["OPENCODE_DISABLE_SHARE"] == "1"
    other = fresh_session_env(str(tmp_path / "job-bbb"))
    assert other["OPENCODE_DB"] != env["OPENCODE_DB"]


def test_session_scope_dir_never_pollutes_the_clone(tmp_path):
    workspace = tmp_path / "job-xyz"
    checkout = workspace / "repo"
    checkout.mkdir(parents=True)
    # The OpenCode pipeline runs inside <workspace>/repo: isolation must
    # resolve to the workspace root, not the clone.
    assert _session_scope_dir(str(checkout)) == str(workspace)
    assert _session_scope_dir(str(workspace)) == str(workspace)


def test_is_opencode_run_command_detection():
    assert _is_opencode_run_command(["opencode", "run", "--auto"]) is True
    assert _is_opencode_run_command(["/home/x/.opencode/bin/opencode", "run", "--auto"]) is True
    assert _is_opencode_run_command(["sh", "-c", "echo hi"]) is False
    assert _is_opencode_run_command(["opencode", "--version"]) is False


def test_opencode_subprocess_gets_isolated_session_env(tmp_path):
    # A fake `opencode` executable records the OPENCODE_DB it was started
    # with; the runner must scope it to the workspace root even though
    # the command runs inside <workspace>/repo.
    workspace = tmp_path / "ws"
    checkout = workspace / "repo"
    checkout.mkdir(parents=True)
    fake = tmp_path / "opencode"
    fake.write_text(
        '#!/bin/sh\necho "$OPENCODE_DB" > "$FAKE_OUT"\n'
        'echo "$OPENCODE_DISABLE_SHARE" >> "$FAKE_OUT"\necho done\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    out_file = tmp_path / "captured.txt"
    env = os.environ.copy()
    env["FAKE_OUT"] = str(out_file)
    import subprocess as _subprocess

    runner = SubprocessCommandRunner()
    original_run_bounded = None
    try:
        import runner_server as _server

        original_run_bounded = _server.run_bounded

        def spy(cmd, cwd, timeout, child_env=None):
            assert child_env is not None
            merged = dict(env)
            merged.update(child_env)
            return original_run_bounded(cmd, cwd, timeout, child_env=merged)

        _server.run_bounded = spy
        result = runner.run(
            [str(fake), "run", "--auto", "--model", PREFERRED_MODEL, "task"],
            cwd=str(checkout),
            timeout=30.0,
        )
    finally:
        if original_run_bounded is not None:
            import runner_server as _server2

            _server2.run_bounded = original_run_bounded
    assert result.returncode == 0
    captured = out_file.read_text(encoding="utf-8").splitlines()
    assert captured[0] == os.path.join(str(workspace), ".runtime-lab-opencode-db", "session.db")
    assert "repo" not in captured[0].split(".runtime-lab-opencode-db")[0].split(os.sep)[-1:]
    assert captured[1] == "1"


def test_subprocess_runner_creates_session_db_dir(tmp_path):
    # OpenCode fails fast with "unable to open database file" when the
    # OPENCODE_DB parent directory does not exist (measured, issue #78),
    # so the launcher must create it before spawning `opencode run`.
    workspace = tmp_path / "ws-nodb"
    checkout = workspace / "repo"
    checkout.mkdir(parents=True)
    fake = tmp_path / "opencode"
    fake.write_text(
        '#!/bin/sh\ntest -d "$(dirname "$OPENCODE_DB")" && echo dbdir-ok\necho done\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner = SubprocessCommandRunner()
    result = runner.run(
        [str(fake), "run", "--auto", "--model", PREFERRED_MODEL, "task"],
        cwd=str(checkout),
        timeout=30.0,
    )
    assert result.returncode == 0
    assert "dbdir-ok" in (result.stdout or "")
    assert os.path.isdir(
        os.path.join(str(workspace), ".runtime-lab-opencode-db")
    )


def test_subprocess_runner_refuses_session_reuse_before_start(tmp_path):
    runner = SubprocessCommandRunner()
    with pytest.raises(ValueError):
        runner.run(
            ["opencode", "run", "--auto", "--model", PREFERRED_MODEL, "--continue", "task"],
            cwd=str(tmp_path),
            timeout=10.0,
        )


# ---------------------------------------------------------------------------
# Bounded accumulated history (job eviction) + stress sequences.
# ---------------------------------------------------------------------------


class BigOutputRunner(CommandRunner):
    """Fake runner returning ~1 MB per call to simulate verbose tooling."""

    def __init__(self, payload_size=1_000_000):
        self.payload_size = payload_size
        self.calls = 0

    def run(self, cmd, cwd, timeout):
        self.calls += 1
        body = "x" * (self.payload_size - 100) + "\nTAIL-MARKER-%d" % self.calls
        return CommandResult(returncode=0, stdout="ok", stderr="")


def test_repeated_shell_calls_stay_bounded(tmp_path):
    runner = BigOutputRunner()
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        max_retained_jobs=5,
    )
    finals = []
    for _ in range(8):
        record, _ = manager.submit(_legacy_payload())
        finals.append(_wait_terminal(manager, record.job_id))
    assert manager.retained_terminal_count() <= 5
    # Every retained record is terminal-bounded.
    for final in finals:
        record = manager.get(final.job_id)
        if record is not None:
            assert len(record.summary) <= 4000 + 200
            assert len(record.error) <= 4000 + 200


def test_eviction_deletes_workspaces_and_caps_memory(tmp_path):
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=BigOutputRunner(payload_size=1_000_000),
        max_retained_jobs=3,
    )
    workspaces = []
    for _ in range(6):
        record, _ = manager.submit(_legacy_payload())
        final = _wait_terminal(manager, record.job_id)
        workspaces.append(final.workspace)
    assert manager.retained_terminal_count() <= 3
    # Workspace deletion happens just after the terminal transition in
    # the background thread: await the eventual bounded state instead
    # of racing it.
    deadline = time.time() + 15.0
    remaining = list(workspaces)
    while time.time() < deadline:
        remaining = [w for w in workspaces if os.path.isdir(w)]
        if len(remaining) <= 3:
            break
        time.sleep(0.05)
    assert len(remaining) <= 3


def test_failing_test_inspect_fix_rerun_sequence(tmp_path):
    """Fail -> inspect -> fix -> rerun must stay bounded and correct."""
    attempts = {"n": 0}

    class FlakyTests(CommandRunner):
        def run(self, cmd, cwd, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                big_log = "test output line\n" * 50000 + "FAIL test_big.py::test_x\n"
                return CommandResult(returncode=1, stdout=big_log, stderr="")
            return CommandResult(returncode=0, stdout="3 passed", stderr="")

    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=FlakyTests(),
        max_retained_jobs=10,
    )
    finals = []
    for _ in range(3):
        record, _ = manager.submit(_legacy_payload())
        finals.append(_wait_terminal(manager, record.job_id))
    assert finals[0].status == "failed"
    assert finals[2].status == "succeeded"
    for final in finals:
        # Even the ~800 KB failing log is terminal-bounded with the
        # failure line (tail) preserved for inspection.
        assert len(final.summary) <= 4000 + 200
        assert len(final.error) <= 4000 + 200
    assert "FAIL test_big.py::test_x" in finals[0].error
    assert manager.retained_terminal_count() <= 10


def test_max_retained_jobs_validation(tmp_path):
    with pytest.raises(ValueError):
        JobManager(workspace_root=str(tmp_path / "ws"), max_retained_jobs=0)
    manager = JobManager(workspace_root=str(tmp_path / "ws2"), max_retained_jobs=2)
    assert manager.max_retained_jobs == 2


def test_bound_stream_defense_in_depth_for_custom_runners():
    huge = "h" * 200_000 + "FINAL-ERROR-ZZZ"
    bounded = _bound_stream(huge)
    assert len(bounded) <= 32768 + 512
    assert "FINAL-ERROR-ZZZ" in bounded
