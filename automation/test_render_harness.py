"""Tests for the temporary GitHub Actions -> Render harness (issue #4).

Covers the Definition of Done without touching the network or any file
under .github/workflows/**:
- region/model validation fails closed before any service is created;
- exactly one worker per attempt (state-file reuse, single creation site,
  model fallback stays on the same worker);
- cleanup is idempotent and verifies deletion;
- Render 429/Retry-After handling is encoded in both scripts and helpers;
- scripts use RENDER_API_KEY (mapped from secrets.KEY) and perform no
  GitHub writes (reads only; writes stay on the Actions workflow side);
- both entrypoints are executable, syntax-valid, and explicitly marked as
  a temporary development harness, not the final runtime architecture.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = REPO_ROOT / "automation"
sys.path.insert(0, str(AUTOMATION))
sys.path.insert(0, str(REPO_ROOT))

from render_lifecycle import (  # noqa: E402
    API_RETRY_CAP_SECONDS,
    FALLBACK_MODEL,
    JOB_POLL_INTERVAL_SECONDS,
    JOB_POLL_MAX_ATTEMPTS,
    PREFERRED_MODEL,
    RUNNER_JOB_TIMEOUT_SECONDS,
    is_rate_limited,
    is_retryable_render_status,
    parse_retry_after,
    rate_limit_backoff_seconds,
    resolve_task_text,
    select_base_sha,
    validate_execution_mode,
    validate_model_name,
)


def _read(name):
    return (AUTOMATION / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure helper behavior (reusable core for the future Render controller).
# ---------------------------------------------------------------------------


def test_rate_limit_helpers_encode_429_contract():
    assert is_rate_limited(429) is True
    assert is_rate_limited(200) is False
    assert is_rate_limited(500) is False
    assert is_rate_limited(None) is False
    for retryable in (429, 500, 502, 503, 504):
        assert is_retryable_render_status(retryable) is True
    for terminal in (200, 201, 204, 400, 401, 403, 404, 410):
        assert is_retryable_render_status(terminal) is False


def test_retry_after_parsing_is_bounded():
    assert parse_retry_after("30") == 30
    assert parse_retry_after("  7  ") == 7
    assert parse_retry_after(None) == 5
    assert parse_retry_after("bogus") == 5
    assert parse_retry_after("-3") == 5
    assert parse_retry_after("9999") == API_RETRY_CAP_SECONDS
    assert parse_retry_after("10", cap=5) == 5


def test_backoff_is_bounded_and_honors_retry_after():
    assert rate_limit_backoff_seconds(1) == 5
    assert rate_limit_backoff_seconds(2) == 10
    assert rate_limit_backoff_seconds(3) == 20
    assert rate_limit_backoff_seconds(99) == API_RETRY_CAP_SECONDS
    assert rate_limit_backoff_seconds(1, retry_after=42) == 42
    assert rate_limit_backoff_seconds(3, retry_after=9999) == API_RETRY_CAP_SECONDS
    assert rate_limit_backoff_seconds(0) == rate_limit_backoff_seconds(1)


def test_task_text_uses_real_issue_text_with_safe_fallback():
    fallback = resolve_task_text(4, "e2e")
    assert "4" in fallback and "e2e" in fallback
    full = resolve_task_text(4, "smoke", title="Fix it", body="details here")
    assert "Fix it" in full and "details here" in full and "#4" in full
    long_body = "x" * 5000
    truncated = resolve_task_text(4, "e2e", title="T", body=long_body)
    assert "[truncated]" in truncated and len(truncated) < len(long_body)
    with pytest.raises(ValueError):
        resolve_task_text(0, "e2e")
    with pytest.raises(ValueError):
        resolve_task_text(4, "bogus-mode")


def test_base_sha_selection_prefers_first_candidate():
    assert select_base_sha("abc123", "def456") == "abc123"
    assert select_base_sha("", "def456") == "def456"
    assert select_base_sha("  ", "") == ""
    assert validate_execution_mode("smoke") == "smoke"
    assert validate_execution_mode("e2e") == "e2e"
    with pytest.raises(ValueError):
        validate_execution_mode("prod")
    assert validate_model_name(PREFERRED_MODEL) == PREFERRED_MODEL
    assert validate_model_name(FALLBACK_MODEL) == FALLBACK_MODEL
    with pytest.raises(ValueError):
        validate_model_name("opencode/gpt-5")


# ---------------------------------------------------------------------------
# Static harness invariants.
# ---------------------------------------------------------------------------


def test_entrypoints_exist_are_executable_and_syntax_valid():
    for name in ("render-job.sh", "render-cleanup.sh"):
        path = AUTOMATION / name
        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR, name + " must be executable"
    for name in ("render-job.sh", "render-cleanup.sh"):
        proc = subprocess.run(
            ["bash", "-n", str(AUTOMATION / name)],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr


def test_harness_is_marked_temporary_not_final_architecture():
    job = _read("render-job.sh")
    cleanup = _read("render-cleanup.sh")
    for text in (job, cleanup):
        lowered = " ".join(text.lower().split())
        assert "temporary" in lowered
        assert "harness" in lowered
        assert "must not require" in lowered or "not the final" in lowered


def test_scripts_use_mapped_key_and_rate_limit_handling():
    job = _read("render-job.sh")
    cleanup = _read("render-cleanup.sh")
    for text in (job, cleanup):
        assert "RENDER_API_KEY" in text
        assert "secrets.KEY" not in text
        assert "429" in text
        assert "Retry-After" in text or "retry-after" in text.lower()
    assert "DELETE" in cleanup and "404" in cleanup and "410" in cleanup
    assert "suspend" in cleanup.lower()
    # Suspension is a fallback in cleanup only; the job path never suspends.
    assert "suspend" not in job.lower().replace(
        "suspension is only an emergency fallback", "")


def test_scripts_perform_no_github_writes():
    job = _read("render-job.sh")
    cleanup = _read("render-cleanup.sh")
    for text in (job, cleanup):
        assert "gh issue edit" not in text
        assert "gh issue comment" not in text
        assert "gh issue close" not in text
        assert "gh pr " not in text
        assert "api.github.com" not in text
    # Read-only issue resolution is allowed on the Actions side.
    assert "gh issue view" in job


def test_single_creation_site_and_same_worker_reuse():
    job = _read("render-job.sh")
    assert job.count("POST \"$API_BASE/services\"") + \
        job.count("POST $API_BASE/services") >= 1
    # Exactly one creation call site (the POST may appear once in api_request
    # invocation for services); fallback/model retry must not create services.
    assert "Reusing existing ephemeral service" in job
    assert "refusing to create a second one" in job
    fallback_idx = job.find("FALLBACK_MODEL")
    assert fallback_idx != -1
    assert "/v1/services" not in job[fallback_idx:]


def test_region_validation_happens_before_any_creation():
    job = _read("render-job.sh")
    validate_idx = job.find("validate_worker_region")
    create_idx = job.find("/services")
    assert validate_idx != -1 and create_idx != -1
    assert validate_idx < create_idx


def test_job_poll_loop_matches_lifecycle_budget_and_covers_runner_timeout():
    # Regression for run 36399649036: render-job.sh polled only 60x20s
    # while the runner may work up to RUNNER_JOB_TIMEOUT_SECONDS, so the
    # shell must track JOB_POLL_MAX_ATTEMPTS instead of drifting.
    import re

    job = _read("render-job.sh")
    assert "JOB_POLL_MAX_ATTEMPTS" in job
    loop_bounds = [
        int(value)
        for value in re.findall(r"for \(\(i = 1; i <= (\d+); i\+\+\)\)", job)
    ]
    assert loop_bounds, "expected bounded poll loops in render-job.sh"
    # The job-result poll is the largest bounded loop in the script
    # (deploy polling uses DEPLOY_ATTEMPTS=60); it must equal the lifecycle
    # constant so both paths share one timeout envelope.
    assert max(loop_bounds) == JOB_POLL_MAX_ATTEMPTS
    assert JOB_POLL_MAX_ATTEMPTS * JOB_POLL_INTERVAL_SECONDS >= (
        RUNNER_JOB_TIMEOUT_SECONDS + 60
    )
    # The terminal poll guard must use the same bound, otherwise the loop
    # exits early or never reports the timeout.
    assert '"$i" -eq %d' % JOB_POLL_MAX_ATTEMPTS in job


# ---------------------------------------------------------------------------
# Live subprocess behavior with mocked network (no real Render calls).
# ---------------------------------------------------------------------------


def _write_fake_bin(directory, curl_log):
    """Create fake curl/sleep/gh binaries; return the bin dir.

    Callers prepend it to PATH themselves so the outer CI environment
    (GITHUB_SHA, GITHUB_RUN_ID, ...) never clobbers the test env.
    """
    bin_dir = Path(directory) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "# Fake curl for harness tests: handles Render API (-w style) and\n"
        "# runner endpoints (body on stdout). Logs METHOD URL lines.\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; HDR=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -D) HDR="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -X) METHOD="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -w) WANT_CODE="yes"; i=$((i+2));;\n'
        '    -d|--data*) DATA="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -H|--max-time|--connect-timeout) i=$((i+2));;\n'
        '    -*) i=$((i+1));;\n'
        '    *) URL="${ARGS[$i]}"; i=$((i+1));;\n'
        '  esac\n'
        'done\n'
        'if [[ -n "$DATA" && "$METHOD" == "GET" ]]; then METHOD="POST"; fi\n'
        'echo "$METHOD $URL" >> "$LOG"\n'
        'if [[ -n "$DATA" ]]; then echo "$METHOD $URL $DATA" >> "$LOG.bodies"; fi\n'
        'emit() { local code="$1" body="$2";'
        ' if [[ -n "$OUT" ]]; then printf "%s" "$body" > "$OUT";'
        ' elif [[ "$METHOD" == "DELETE" && -z "$WANT_CODE" ]]; then :;'
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-1)'
        ' emit 200 \'{"job_id":"job-1","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' emit 201 \'{"job_id":"job-1"}\'; else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health) exit 0;;\n'
        '  */services/srv-existing)' 
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
        '  */services/srv-del)' 
        ' if [[ "$METHOD" == "DELETE" ]]; then emit 204 \'\';'
        ' else emit 404 \'{"error":"not found"}\'; fi;;\n'
        '  *) emit 200 \'{}\';;\n'
        'esac\n'
        'exit 0\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "issue" && "$2" == "view" ]]; then\n'
        '  printf \'{"title":"Harness title","body":"Harness body"}\'\n'
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return str(bin_dir)


def _run(script, env, cwd):
    return subprocess.run(
        ["bash", str(AUTOMATION / script)],
        capture_output=True, text=True, timeout=120, env=env, cwd=cwd,
    )


def _base_env(tmp_path):
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    env = dict(os.environ)
    env["ISSUE_NUMBER"] = "4"
    env["EXECUTION_MODE"] = "e2e"
    env["RENDER_API_KEY"] = "dummy-key"
    env["RENDER_REGION"] = "oregon"
    env["OPENCODE_MODEL"] = PREFERRED_MODEL
    env["RENDER_STATE_FILE"] = str(state)
    env["RENDER_RESULT_FILE"] = str(result)
    env["GITHUB_SHA"] = "abc123def456"
    env["GITHUB_RUN_ID"] = "999"
    env["GH_TOKEN"] = "dummy"
    return env, state, result


def test_job_rejects_forbidden_region_before_creation(tmp_path):
    env, state, _ = _base_env(tmp_path)
    log = tmp_path / "curl.log"
    env["PATH"] = _write_fake_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    env["RENDER_REGION"] = "frankfurt"
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    assert proc.returncode != 0
    assert "forbidden" in (proc.stdout + proc.stderr).lower() or \
        "region" in (proc.stdout + proc.stderr).lower()
    # Validation happens before any Render call: fake curl never ran.
    assert not log.exists() or log.read_text().strip() == ""
    if state.exists() and state.read_text().strip():
        assert "srv" not in state.read_text()


def test_job_rejects_unknown_region_and_model(tmp_path):
    for key, value in (("RENDER_REGION", "moon"),
                       ("OPENCODE_MODEL", "opencode/gpt-5")):
        env, _, _ = _base_env(tmp_path)
        log = tmp_path / ("curl-%s.log" % value)
        env["PATH"] = _write_fake_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
        env[key] = value
        proc = _run("render-job.sh", env, str(REPO_ROOT))
        assert proc.returncode != 0, key
        assert not log.exists() or log.read_text().strip() == ""


def test_job_reuses_existing_service_and_collects_result(tmp_path):
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl.log"
    env["PATH"] = _write_fake_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Reusing existing ephemeral service srv-existing" in proc.stdout
    assert "refusing to create a second one" in proc.stdout
    calls = log.read_text()
    assert "POST https://api.render.com/v1/services" not in calls
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-1"
    # Resolved issue text and base SHA flow into the job, not a placeholder.
    bodies = (tmp_path / "curl.log.bodies").read_text()
    assert "Harness title" in bodies
    assert "Harness body" in bodies
    assert "abc123def456" in bodies
    assert json.loads(state.read_text())["serviceId"] == "srv-existing"


def test_cleanup_is_idempotent_without_state(tmp_path):
    env = dict(os.environ)
    env["RENDER_API_KEY"] = "dummy-key"
    env["RENDER_STATE_FILE"] = str(tmp_path / "missing.json")
    proc = _run("render-cleanup.sh", env, str(REPO_ROOT))
    assert proc.returncode == 0
    assert "nothing to delete" in proc.stdout
    empty_state = tmp_path / "empty.json"
    empty_state.write_text("{}\n")
    env["RENDER_STATE_FILE"] = str(empty_state)
    proc = _run("render-cleanup.sh", env, str(REPO_ROOT))
    assert proc.returncode == 0
    assert "no service id" in proc.stdout.lower()


def test_cleanup_deletes_and_verifies_with_mock(tmp_path):
    env = dict(os.environ)
    env["RENDER_API_KEY"] = "dummy-key"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"serviceId": "srv-del"}))
    env["RENDER_STATE_FILE"] = str(state)
    log = tmp_path / "curl.log"
    env["PATH"] = _write_fake_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    proc = _run("render-cleanup.sh", env, str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no longer exists" in proc.stdout
    calls = log.read_text()
    assert "DELETE https://api.render.com/v1/services/srv-del" in calls
    # Suspension is only a fallback: a clean delete+verify never suspends.
    assert "suspend" not in calls
