"""Execution tests for the issue #4 temporary Actions -> Render harness.

These tests invoke the real controller entrypoints
(automation/render-job.sh and automation/render-cleanup.sh) with a stubbed
`curl`/`sleep` on PATH, so no Render network traffic occurs. They prove:

- region validation rejects unsupported worker regions before any service
  creation network call;
- the happy path creates exactly one worker, collects the complete result,
  and records state;
- a re-run with an existing state file never creates a second worker;
- cleanup deletes the worker and verifies deletion, and is a no-op without
  state.

Python-level unit tests cover the factored helpers in
automation/render_lifecycle.py (429/Retry-After handling, task-text
building, base-SHA resolution) so the future persistent Render controller
can reuse them without the Actions shell wrappers.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile

import pytest

from automation.render_lifecycle import (
    API_CALL_MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    MAX_TASK_BODY_CHARS,
    RETRIABLE_HTTP_STATUSES,
    TEMPORARY_HARNESS_MARKER,
    TEMPORARY_HARNESS_NOTE,
    backoff_delay_for_attempt,
    build_task_text,
    is_plausible_base_sha,
    parse_retry_after_seconds,
    resolve_base_sha,
    should_retry_http_status,
)

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
JOB_SCRIPT = os.path.join(REPO_ROOT, "automation", "render-job.sh")
CLEANUP_SCRIPT = os.path.join(REPO_ROOT, "automation", "render-cleanup.sh")

CURL_STUB = """#!/usr/bin/env python3
import os
import sys

stub_dir = os.environ["STUB_DIR"]
mode = os.environ.get("STUB_MODE", "job")
args = sys.argv[1:]

method = "GET"
url = ""
out = None
header = None
wants_code = False
i = 0
while i < len(args):
    a = args[i]
    if a == "-X" and i + 1 < len(args):
        method = args[i + 1]
        i += 2
        continue
    if a == "-o" and i + 1 < len(args):
        out = args[i + 1]
        i += 2
        continue
    if a == "-D" and i + 1 < len(args):
        header = args[i + 1]
        i += 2
        continue
    if "http_code" in a:
        wants_code = True
    if a.startswith("http"):
        url = a
    i += 1

with open(os.path.join(stub_dir, "curl_calls.log"), "a", encoding="utf-8") as handle:
    handle.write("%s %s\\n" % (method, url))

if header and header != "/dev/null":
    with open(header, "w", encoding="utf-8") as handle:
        handle.write("")

body = ""
code = "200"

if url.startswith("https://fake.onrender.com/health"):
    sys.exit(0)
elif method == "GET" and "/owners" in url:
    body = '[{"id":"wrk-test"}]'
    code = "200"
elif method == "POST" and url.rstrip("/").endswith("/v1/services"):
    with open(os.path.join(stub_dir, "creations.log"), "a", encoding="utf-8") as handle:
        handle.write("create\\n")
    body = '{"service":{"id":"srv-123","serviceDetails":{"plan":"free"}},"deployId":"dep-1"}'
    code = "201"
elif "/deploys/" in url:
    body = '{"status":"live"}'
    code = "200"
elif method == "DELETE" and "/v1/services/" in url:
    body = ""
    code = "204"
elif method == "POST" and url.endswith("/suspend"):
    body = ""
    code = "202"
elif method == "POST" and "/v1/jobs" in url:
    body = '{"job_id":"job-1"}'
    code = "200"
elif method == "GET" and "/v1/jobs/" in url:
    body = '{"job_id":"job-1","status":"succeeded","success":true,"summary":"done"}'
    code = "200"
elif method == "GET" and "/v1/services" in url and "limit=" in url:
    body = "[]"
    code = "200"
elif method == "GET" and "/v1/services/" in url:
    if mode == "cleanup":
        body = ""
        code = "404"
    else:
        body = '{"serviceDetails":{"plan":"free","url":"https://fake.onrender.com"}}'
        code = "200"
else:
    sys.stderr.write("stub curl: unexpected %s %s\\n" % (method, url))
    sys.exit(22)

if wants_code:
    if out and out != "/dev/null":
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(body)
    elif out is None:
        sys.stdout.write(body)
    sys.stdout.write(code)
    sys.exit(0)

if out and out != "/dev/null":
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(body)
    sys.exit(0)
if out == "/dev/null":
    sys.exit(0)
sys.stdout.write(body)
sys.exit(0)
"""

SLEEP_STUB = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture()
def stub_env():
    with tempfile.TemporaryDirectory() as directory:
        bin_dir = os.path.join(directory, "bin")
        os.makedirs(bin_dir)
        curl_path = os.path.join(bin_dir, "curl")
        with open(curl_path, "w", encoding="utf-8") as handle:
            handle.write(CURL_STUB)
        os.chmod(curl_path, os.stat(curl_path).st_mode | stat.S_IEXEC)
        sleep_path = os.path.join(bin_dir, "sleep")
        with open(sleep_path, "w", encoding="utf-8") as handle:
            handle.write(SLEEP_STUB)
        os.chmod(sleep_path, os.stat(sleep_path).st_mode | stat.S_IEXEC)
        state_file = os.path.join(directory, "state.json")
        result_file = os.path.join(directory, "result.json")
        env = dict(os.environ)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["STUB_DIR"] = directory
        env["STUB_MODE"] = "job"
        env["RENDER_API_KEY"] = "test-key"
        env["RENDER_STATE_FILE"] = state_file
        env["RENDER_RESULT_FILE"] = result_file
        env["GITHUB_RUN_ID"] = "test-run"
        # Never leak real credentials/tokens into the harness under test.
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GITHUB_SHA", None)
        env.pop("GITHUB_REPOSITORY", None)
        env.pop("OPENCODE_API_KEY", None)
        yield {"dir": directory, "env": env, "state": state_file, "result": result_file}


def _curl_calls(directory):
    path = os.path.join(directory, "curl_calls.log")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def _creations(directory):
    path = os.path.join(directory, "creations.log")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def test_job_rejects_frankfurt_before_any_service_creation(stub_env):
    env = dict(stub_env["env"])
    env.update(
        {
            "ISSUE_NUMBER": "4",
            "EXECUTION_MODE": "smoke",
            "RENDER_REGION": "frankfurt",
            "OPENCODE_MODEL": "opencode/muse-spark-1.3-contributor-free",
        }
    )
    proc = subprocess.run(
        ["bash", JOB_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    calls = _curl_calls(stub_env["dir"])
    # Region policy must fail closed before any Render network call,
    # in particular before POST /v1/services.
    assert calls == []
    assert _creations(stub_env["dir"]) == 0


def test_job_rejects_unknown_region_before_service_creation(stub_env):
    env = dict(stub_env["env"])
    env.update(
        {
            "ISSUE_NUMBER": "4",
            "EXECUTION_MODE": "smoke",
            "RENDER_REGION": "moon",
            "OPENCODE_MODEL": "opencode/muse-spark-1.3-contributor-free",
        }
    )
    proc = subprocess.run(
        ["bash", JOB_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert _curl_calls(stub_env["dir"]) == []


def test_job_happy_path_creates_exactly_one_worker_and_collects(stub_env):
    env = dict(stub_env["env"])
    env.update(
        {
            "ISSUE_NUMBER": "4",
            "EXECUTION_MODE": "smoke",
            "RENDER_REGION": "oregon",
            "OPENCODE_MODEL": "opencode/muse-spark-1.3-contributor-free",
        }
    )
    proc = subprocess.run(
        ["bash", JOB_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _creations(stub_env["dir"]) == 1
    with open(stub_env["state"], encoding="utf-8") as handle:
        state = json.load(handle)
    assert state["serviceId"] == "srv-123"
    assert state["plan"] == "free"
    assert state["region"] == "oregon"
    with open(stub_env["result"], encoding="utf-8") as handle:
        result = json.load(handle)
    assert result["status"] == "succeeded"
    assert result["success"] is True
    assert result["summary"] == "done"


def test_job_rerun_with_existing_state_creates_no_second_worker(stub_env):
    with open(stub_env["state"], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "serviceId": "srv-123",
                "deployId": "",
                "region": "oregon",
                "model": "opencode/muse-spark-1.3-contributor-free",
                "plan": "free",
            },
            handle,
        )
    env = dict(stub_env["env"])
    env.update(
        {
            "ISSUE_NUMBER": "4",
            "EXECUTION_MODE": "smoke",
            "RENDER_REGION": "oregon",
            "OPENCODE_MODEL": "opencode/muse-spark-1.3-contributor-free",
        }
    )
    proc = subprocess.run(
        ["bash", JOB_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    # Reusing the recorded service id: model/poll retries must never
    # provision a second Render service for the same issue attempt.
    assert _creations(stub_env["dir"]) == 0


def test_cleanup_deletes_and_verifies_deletion(stub_env):
    with open(stub_env["state"], "w", encoding="utf-8") as handle:
        json.dump({"serviceId": "srv-123", "plan": "free"}, handle)
    env = dict(stub_env["env"])
    env["STUB_MODE"] = "cleanup"
    proc = subprocess.run(
        ["bash", CLEANUP_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "Verified Render service srv-123 no longer exists" in proc.stdout
    calls = _curl_calls(stub_env["dir"])
    assert any(call.startswith("DELETE ") for call in calls)


def test_cleanup_without_state_is_noop_success(stub_env):
    env = dict(stub_env["env"])
    env["STUB_MODE"] = "cleanup"
    proc = subprocess.run(
        ["bash", CLEANUP_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert _curl_calls(stub_env["dir"]) == []


def test_harness_scripts_are_temporary_not_final_architecture():
    for path in (JOB_SCRIPT, CLEANUP_SCRIPT):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert TEMPORARY_HARNESS_MARKER in text.lower()
        # Credentials come only from the mapped env var, never the secret name.
        assert "RENDER_API_KEY" in text
        assert "secrets.KEY" not in text
        # 429/Retry-After handling is explicit, not an operational note.
        assert "429" in text
        assert "Retry-After" in text or "retry-after" in text.lower()
    with open(JOB_SCRIPT, encoding="utf-8") as handle:
        job = handle.read()
    # GitHub writes stay on the Actions side during this temporary phase:
    # the job controller must not edit issues/PRs itself.
    assert "gh issue edit" not in job
    assert "gh issue close" not in job
    assert "gh pr create" not in job
    # Suspension is an emergency fallback owned by cleanup, and deletion is
    # the primary mechanism verified with 404/410.
    with open(CLEANUP_SCRIPT, encoding="utf-8") as handle:
        cleanup = handle.read()
    assert "DELETE" in cleanup
    assert "404" in cleanup and "410" in cleanup
    assert "suspend" in cleanup
    assert TEMPORARY_HARNESS_NOTE in cleanup


def test_rate_limit_helpers_are_bounded_and_clamped():
    assert 429 in RETRIABLE_HTTP_STATUSES
    assert should_retry_http_status(429)
    assert should_retry_http_status(503)
    assert not should_retry_http_status(200)
    assert not should_retry_http_status(400)
    assert not should_retry_http_status(500)
    assert parse_retry_after_seconds(None) == 0
    assert parse_retry_after_seconds("") == 0
    assert parse_retry_after_seconds("3") == 3
    assert parse_retry_after_seconds("9999") == MAX_RETRY_AFTER_SECONDS
    assert parse_retry_after_seconds("bogus") == 0
    assert backoff_delay_for_attempt(1) == 2
    assert backoff_delay_for_attempt(1, 30) == 30
    assert backoff_delay_for_attempt(10) == MAX_RETRY_AFTER_SECONDS
    assert API_CALL_MAX_ATTEMPTS <= 4
    with pytest.raises(ValueError):
        backoff_delay_for_attempt(0)


def test_task_text_uses_resolved_issue_text_with_fallback():
    text = build_task_text(4, "smoke", title="Fix it", body="Do the thing")
    assert "#4" in text and "smoke" in text
    assert "Fix it" in text and "Do the thing" in text
    fallback = build_task_text(4, "e2e")
    assert "#4" in fallback and "e2e" in fallback
    long_body = "x" * (MAX_TASK_BODY_CHARS + 100)
    truncated = build_task_text(4, "smoke", title="T", body=long_body)
    assert "truncated" in truncated
    assert len(truncated) < len(long_body)
    with pytest.raises(ValueError):
        build_task_text(0, "smoke")
    with pytest.raises(ValueError):
        build_task_text(4, "bogus")


def test_base_sha_resolution_prefers_first_plausible_sha():
    good = "a" * 40
    assert is_plausible_base_sha(good)
    assert is_plausible_base_sha("B" * 64)
    assert not is_plausible_base_sha("")
    assert not is_plausible_base_sha("main")
    assert not is_plausible_base_sha("abc123")
    assert not is_plausible_base_sha(None)
    assert resolve_base_sha("", None, good) == good
    assert resolve_base_sha(good, "b" * 40) == good
    assert resolve_base_sha("", "  ") == ""
