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


def test_render_executor_cleanup_is_unconditional_and_gates_success():
    workflow = (REPO_ROOT / ".github" / "workflows" / "render-executor.yml").read_text(
        encoding="utf-8"
    )
    assert "- name: Delete ephemeral Render service" in workflow
    assert "if: always()" in workflow
    assert "bash automation/render-cleanup.sh" in workflow
    assert 'CLEANUP_OUTCOME: ${{ steps.cleanup.outcome }}' in workflow
    assert '"$CLEANUP_OUTCOME" != "success"' in workflow
    assert "mandatory cleanup failed" in workflow.lower()

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


def test_job_poll_resubmits_on_same_worker_while_budget_remains():
    # Regression for run 36409152332: a submitted job polled as pending
    # for ~2 minutes, then turned into a permanent unknown-job 404 while
    # the runner stayed healthy (documented anytime-restart of Free
    # workers wipes the in-memory job). The loop must resubmit the same
    # payload on the SAME worker while poll budget remains (run
    # 36434278632 lost six consecutive jobs to six proven restarts
    # inside the budget and exhausted the old fixed bound of five, so a
    # fixed count no longer gates recovery) and only then fail fast --
    # unless the loss is proven deterministic (same healthy worker
    # process no longer knows the job it accepted), which fails fast
    # immediately.
    job = _read("render-job.sh")
    assert "JOB_POLL_MAX_JOB_RESUBMITS" not in job
    assert "should_resubmit_after_job_loss" in job or "POLLS_REMAINING" in job
    assert "POLL_MAX_ATTEMPTS" in job
    assert "RESTART_VERDICT" in job
    assert "same-process" in job
    # Issue #41: identity comes from the instance id first, wall-clock
    # drift second; uptime order alone is not proof of identity.
    assert "format_restart_evidence" in job
    assert "detect_worker_restart" in job
    assert "SUBMIT_INSTANCE" in job and "CURRENT_INSTANCE" in job
    assert "SUBMIT_WALL" in job and "CURRENT_WALL" in job
    assert "instance_id" in job
    assert "Resubmitted runner job" in job
    assert "resubmissions used" in job
    # The resubmission posts to the worker's own job endpoint (same
    # service); it must never provision a second Render service.
    assert "$SERVICE_URL/v1/jobs" in job
    assert "no new service" in job
    # Run 36439192645: a memory-pressure restart storm abandons
    # resubmission instead of burning the whole poll budget and then
    # starving a still-running job. The streak counts consecutive
    # proven-restart resubmissions (pending polls do not reset it;
    # fallback does), the pressure verdict comes from the Actions-side
    # sampler evidence, and without pressure evidence the streak alone
    # never abandons (transient clusters keep budget-limited recovery).
    assert "POLL_RESTART_STREAK" in job
    assert "POLL_RESTART_STREAK=0" in job
    assert "should_abandon_restart_storm" in job
    assert "detect_memory_pressure" in job
    assert "restart storm" in job
    # The fallback path now tracks the in-flight payload so a later
    # loss-resubmission retries the fallback model, not the primary one.
    assert 'JOB_PAYLOAD="$RETRY_PAYLOAD"' in job
    # Exact-artifact jobs must survive a Render worker replacement too.
    # A restart wipes the worker-local materialized binary, so recovery
    # must re-push the already verified artifact before POSTing /v1/jobs.
    redeliver_idx = job.find("Re-delivered exact artifact after worker restart")
    resubmit_idx = job.find('RESUBMIT_RESPONSE="$(curl -fsSL', redeliver_idx)
    assert redeliver_idx != -1
    assert resubmit_idx > redeliver_idx
    restart_window = job[max(0, redeliver_idx - 5000):resubmit_idx]
    assert "$SERVICE_URL/v1/exact-artifact" in restart_window
    assert "exact_artifact_redelivered" in restart_window
    assert "refusing to resubmit without the pinned artifact" in restart_window
    # /proc proof must leave the ephemeral worker before a restart. The
    # controller stores verified evidence from ordinary pending/running
    # polls and merges it into the durable result on every EXIT path.
    assert "RENDER_EXACT_EVIDENCE_FILE" in job
    assert "polled exact process evidence SHA mismatch" in job
    assert "exact_evidence" in job
    assert "worker disappeared after verified exact OpenCode process start" in job


def test_job_poll_envelope_honors_resolved_lifecycle_constants():
    # Regression for run 36493364897: while verifying that run's
    # storm path, the poll loop resolved POLL_MAX_ATTEMPTS (plus the
    # transport/storm thresholds) from render_lifecycle but then
    # ignored the resolved budget in the loop bound, the three
    # terminal-iteration checks, and the poll sleeps (hardcoded
    # `140` / `sleep 20`), and POLL_UNKNOWN_THRESHOLD had no numeric
    # fallback at all. Any future change to JOB_POLL_MAX_ATTEMPTS or
    # JOB_POLL_INTERVAL_SECONDS would then silently diverge the loop
    # from the reported budget -- the same resolved-but-ignored class
    # as the issue #113 storm-threshold fix. The envelope must honor
    # the resolved values, with lifecycle defaults on corrupt values.
    job = _read("render-job.sh")
    assert "JOB_POLL_MAX_ATTEMPTS" in job
    assert "JOB_POLL_INTERVAL_SECONDS" in job
    assert "POLL_INTERVAL_SECONDS" in job
    assert "for ((i = 1; i <= POLL_MAX_ATTEMPTS; i++))" in job
    assert "i <= 140" not in job
    assert '"$i" -eq 140' not in job
    assert job.count('"$i" -eq "$POLL_MAX_ATTEMPTS"') == 3
    assert "sleep 20" not in job
    assert job.count('sleep "$POLL_INTERVAL_SECONDS"') == 3
    assert '[[ "$POLL_MAX_ATTEMPTS" =~ ^[0-9]+$ ]] || POLL_MAX_ATTEMPTS=140' in job
    assert '[[ "$POLL_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || POLL_INTERVAL_SECONDS=20' in job
    assert '[[ "$POLL_UNKNOWN_THRESHOLD" =~ ^[0-9]+$ ]] || POLL_UNKNOWN_THRESHOLD=3' in job
    # Resolved values stay consistent with the lifecycle module.
    assert JOB_POLL_MAX_ATTEMPTS == 140
    assert JOB_POLL_INTERVAL_SECONDS == 20


def test_job_poll_loop_distinguishes_empty_from_pending():
    # Regression for run 36402447309: the poll loop treated an empty status
    # (curl -f collapsing 404/5xx/connection errors into "") as
    # queued/running and waited out the full budget with a bare
    # "last status 'empty'" error that masked whether the job was lost.
    job = _read("render-job.sh")
    assert 'queued|running|"")' not in job
    assert "classify_job_poll_response" in job
    assert "unknown_job" in job
    assert "transport_error" in job
    assert "no longer knows job" in job
    # Unknown-job answers fail fast with a health-qualified diagnostic;
    # transport failures re-probe /health instead of polling blindly.
    assert "POLL_UNKNOWN_THRESHOLD" in job or "UNKNOWN_JOB_THRESHOLD" in job
    assert job.count("$SERVICE_URL/health") >= 2


def _write_lost_job_bin(directory, curl_log):
    """Fake bin where the runner loses every submitted job after 404s.

    Reproduces run 36409152332 at the HTTP-contract level: each submit
    returns a fresh job id, but every GET /v1/jobs/<id> is the runner's
    documented unknown-job 404 (worker restart wiped the in-memory job)
    while /health stays healthy. /health reports a shrinking uptime
    (300s before the loss, 9s after) for the first loss and a fresh
    rotating instance id for every later loss, so the script's restart
    evidence must conclude a worker restart was observed on every loss
    (with budget-limited resubmission the run only stops when the poll
    budget is exhausted).
    """
    bin_dir = Path(directory) / "bin-lost"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -D) i=$((i+2));;\n'
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
        'emit() { local code="$1" body="$2";'
        ' if [[ -n "$OUT" ]]; then printf "%s" "$body" > "$OUT";'
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
  '  */v1/jobs/job-lost|*/v1/jobs/job-lost-2|*/v1/jobs/job-lost-3|*/v1/jobs/job-lost-4|*/v1/jobs/job-lost-5|*/v1/jobs/job-lost-6)'
  ' emit 404 \'{"error":"unknown job"}\';;\n'
  '  */v1/jobs)'
  ' if [[ "$METHOD" == "POST" ]]; then'
  ' N=$(next_count "$LOG.post-count");'
  ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-lost"}\';'
  ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-lost-2"}\';'
  ' elif [[ "$N" -le 3 ]]; then emit 201 \'{"job_id":"job-lost-3"}\';'
  ' elif [[ "$N" -le 4 ]]; then emit 201 \'{"job_id":"job-lost-4"}\';'
  ' elif [[ "$N" -le 5 ]]; then emit 201 \'{"job_id":"job-lost-5"}\';'
  ' else emit 201 \'{"job_id":"job-lost-6"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
  '  */health)'
  ' H=$(next_count "$LOG.health-count");'
  ' if [[ "$H" -le 2 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"uptime_seconds":300,"jobs":{"total":1}}\';'
  ' else'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-restart-\'"$H"\'","uptime_seconds":9,"jobs":{"total":0}}\';'
  ' fi;;\n'
  '  */services/srv-existing)'
  ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_fails_fast_when_runner_forgets_job(tmp_path):
    # End-to-end at the shell/HTTP level: endless proven restart losses
    # are retried on the same worker while poll budget remains
    # (regression for run 36434278632, which lost six consecutive jobs
    # to six proven restarts inside the budget and exhausted the old
    # fixed bound of five -- a fixed count no longer gates recovery),
    # and the attempt then ends with a budget diagnostic -- never a
    # generic "did not finish in time ... empty" timeout without loss
    # context, and never a second Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-lost.log"
    env["PATH"] = _write_lost_job_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    # Hermetic telemetry: the fake /health answers carry no cgroup
    # fields, so the sampler records gaps only and the storm pressure
    # gate stays shut for the whole run.
    env["RENDER_MEMORY_SAMPLES_FILE"] = str(tmp_path / "gaps-only.jsonl")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    # Each proven loss triggers a same-worker resubmission while budget
    # remains: losses strike at every third poll (unknown-job threshold),
    # so iterations 3..138 yield 46 resubmissions (47 worker POSTs) and
    # the 140-poll budget then ends the attempt.
    assert "Resubmitted runner job job-lost-2 (replaces lost job job-lost)" in combined
    assert "Resubmitted runner job job-lost-3 (replaces lost job job-lost-2)" in combined
    assert "Resubmitted runner job job-lost-4 (replaces lost job job-lost-3)" in combined
    assert "Resubmitted runner job job-lost-5 (replaces lost job job-lost-4)" in combined
    assert "Resubmitted runner job job-lost-6 (replaces lost job job-lost-5)" in combined
    assert "poll(s) of budget remaining, no new service" in combined
    # The sixth consecutive loss is now another retry, not a terminal
    # failure: no fixed-bound fail-fast diagnostic appears.
    assert "resubmissions used: 5/5" not in combined
    # Without pressure evidence the streak alone never abandons (run
    # 36439192645 companion): no storm diagnostic appears even after
    # 46 proven-restart resubmissions.
    assert "restart storm" not in combined
    # The budget-exhausted ending carries the loss context: unknown-job
    # polls plus the resubmission count.
    assert "did not finish in time" in combined
    assert "unknown-job polls" in combined
    assert "46 resubmission(s) used" in combined
    assert "worker restart observed" in combined
    assert not result.exists() or result.read_text().strip() == ""
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 47
    assert "POST https://api.render.com/v1/services" not in calls


def _write_recovered_job_bin(directory, curl_log):
    """Fake bin where the first job is lost but the resubmission succeeds.

    Reproduces the transient-restart case of run 36409152332: submit #1
    returns job-lost (every poll is an unknown-job 404), the single
    same-worker resubmission returns job-retry, and job-retry polls as
    succeeded. Both submits must carry the identical task payload.
    """
    bin_dir = Path(directory) / "bin-recovered"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
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
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-lost)'
        ' emit 404 \'{"error":"unknown job: job-lost"}\';;\n'
        '  */v1/jobs/job-retry)'
        ' emit 200 \'{"job_id":"job-retry","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' N=$(next_count "$LOG.post-count");'
        ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-lost"}\';'
        ' else emit 201 \'{"job_id":"job-retry"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' if [[ "$H" -le 2 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"uptime_seconds":300,"jobs":{"total":1}}\';'
        ' else'
        ' emit 200 \'{"status":"ok","ready":true,"uptime_seconds":9,"jobs":{"total":0}}\';'
        ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_recovers_via_one_same_worker_resubmission(tmp_path):
    # The repair itself, end-to-end: a transient worker restart loses
    # the first job id, the loop resubmits once on the same worker, and
    # the attempt succeeds with the resubmitted job's result.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-recovered.log"
    env["PATH"] = _write_recovered_job_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "Resubmitted runner job job-retry (replaces lost job job-lost)" in combined
    assert "worker restart observed" in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-retry"
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 2
    assert "POST https://api.render.com/v1/services" not in calls
    # Both submits carry the identical task payload (same issue text).
    bodies = (tmp_path / "curl-recovered.log.bodies").read_text()
    assert bodies.count("Harness title") == 2


def _write_double_restart_recovered_bin(directory, curl_log):
    """Fake bin reproducing run 36417263684 with a successful third attempt.

    Two consecutive proven worker restarts lose job-r1 and job-r2 (each
    poll is an unknown-job 404 while /health stays healthy with a
    rotating instance id), and the second same-worker resubmission
    (job-r3) polls as succeeded. All three submits carry the identical
    task payload; no Render service is ever created.
    """
    bin_dir = Path(directory) / "bin-double-recovered"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
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
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-r1|*/v1/jobs/job-r2)'
        ' emit 404 \'{"error":"unknown job"}\';;\n'
        '  */v1/jobs/job-r3)'
        ' emit 200 \'{"job_id":"job-r3","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' N=$(next_count "$LOG.post-count");'
        ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-r1"}\';'
        ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-r2"}\';'
        ' else emit 201 \'{"job_id":"job-r3"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' if [[ "$H" -le 2 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-fe21","uptime_seconds":11.1,"jobs":{"total":1}}\';'
        ' elif [[ "$H" -le 4 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-b7c7","uptime_seconds":52.5,"jobs":{"total":0}}\';'
        ' else'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-4e9b","uptime_seconds":43.2,"jobs":{"total":0}}\';'
        ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_recovers_after_two_consecutive_restarts(tmp_path):
    # Regression for run 36417263684: the original job and the first
    # same-worker resubmission were both lost to consecutive proven
    # worker restarts (instance fe21 -> b7c7 -> 4e9b). With a bound of
    # two resubmissions the second resubmission succeeds on the same
    # worker, with no second Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-double-recovered.log"
    env["PATH"] = _write_double_restart_recovered_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "Resubmitted runner job job-r2 (replaces lost job job-r1)" in combined
    assert "Resubmitted runner job job-r3 (replaces lost job job-r2)" in combined
    assert "worker restart observed" in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-r3"
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 3
    assert "POST https://api.render.com/v1/services" not in calls
    bodies = (tmp_path / "curl-double-recovered.log.bodies").read_text()
    assert bodies.count("Harness title") == 3


def _write_triple_restart_recovered_bin(directory, curl_log):
    """Fake bin reproducing run 36422228148 with a successful fourth attempt.

    Three consecutive proven worker restarts lose job-t1, job-t2, and
    job-t3 (each poll is an unknown-job 404 while /health stays healthy
    with a rotating instance id mirroring 67de -> 5a33 -> 8187 ->
    3133), and the third same-worker resubmission (job-t4) polls as
    succeeded. All four submits carry the identical task payload; no
    Render service is ever created.
    """
    bin_dir = Path(directory) / "bin-triple-recovered"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
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
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-t1|*/v1/jobs/job-t2|*/v1/jobs/job-t3)'
        ' emit 404 \'{"error":"unknown job"}\';;\n'
        '  */v1/jobs/job-t4)'
        ' emit 200 \'{"job_id":"job-t4","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' N=$(next_count "$LOG.post-count");'
        ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-t1"}\';'
        ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-t2"}\';'
        ' elif [[ "$N" -le 3 ]]; then emit 201 \'{"job_id":"job-t3"}\';'
        ' else emit 201 \'{"job_id":"job-t4"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' if [[ "$H" -le 2 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-67de","uptime_seconds":29.2,"jobs":{"total":1}}\';'
        ' elif [[ "$H" -le 4 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-5a33","uptime_seconds":53.0,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 6 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-8187","uptime_seconds":60.8,"jobs":{"total":0}}\';'
        ' else'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-3133","uptime_seconds":61.2,"jobs":{"total":0}}\';'
        ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_recovers_after_three_consecutive_restarts(tmp_path):
    # Regression for run 36422228148: the original job plus both
    # resubmissions were lost to three consecutive proven worker
    # restarts (instance 67de -> 5a33 -> 8187 -> 3133; uptimes
    # 29.2 -> 53.0 -> 60.8 -> 61.2). With a bound of three
    # resubmissions the third resubmission succeeds on the same worker,
    # with no second Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-triple-recovered.log"
    env["PATH"] = _write_triple_restart_recovered_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "Resubmitted runner job job-t2 (replaces lost job job-t1)" in combined
    assert "Resubmitted runner job job-t3 (replaces lost job job-t2)" in combined
    assert "Resubmitted runner job job-t4 (replaces lost job job-t3)" in combined
    assert "worker restart observed" in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-t4"
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 4
    assert "POST https://api.render.com/v1/services" not in calls
    bodies = (tmp_path / "curl-triple-recovered.log.bodies").read_text()
    assert bodies.count("Harness title") == 4


def _write_quad_restart_recovered_bin(directory, curl_log):
    """Fake bin reproducing run 36425019190 with a successful fifth attempt.

    Four consecutive proven worker restarts lose job-q1, job-q2, job-q3,
    and job-q4 (each poll is an unknown-job 404 while /health stays
    healthy with a rotating instance id mirroring cf0c -> 3b56 -> 57c5
    -> e0e8 -> 9838 and uptimes 26.9 -> 53.0 -> 57.6 -> 61.7 -> 62.7),
    and the fourth same-worker resubmission (job-q5) polls as
    succeeded. All five submits carry the identical task payload; no
    Render service is ever created.
    """
    bin_dir = Path(directory) / "bin-quad-recovered"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
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
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-q1|*/v1/jobs/job-q2|*/v1/jobs/job-q3|*/v1/jobs/job-q4)'
        ' emit 404 \'{"error":"unknown job"}\';;\n'
        '  */v1/jobs/job-q5)'
        ' emit 200 \'{"job_id":"job-q5","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' N=$(next_count "$LOG.post-count");'
        ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-q1"}\';'
        ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-q2"}\';'
        ' elif [[ "$N" -le 3 ]]; then emit 201 \'{"job_id":"job-q3"}\';'
        ' elif [[ "$N" -le 4 ]]; then emit 201 \'{"job_id":"job-q4"}\';'
        ' else emit 201 \'{"job_id":"job-q5"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' if [[ "$H" -le 2 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-cf0c","uptime_seconds":26.9,"jobs":{"total":1}}\';'
        ' elif [[ "$H" -le 4 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-3b56","uptime_seconds":53.0,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 6 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-57c5","uptime_seconds":57.6,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 8 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-e0e8","uptime_seconds":61.7,"jobs":{"total":0}}\';'
        ' else'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-9838","uptime_seconds":62.7,"jobs":{"total":0}}\';'
        ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_recovers_after_four_consecutive_restarts(tmp_path):
    # Regression for run 36425019190: the original job plus all three
    # resubmissions were lost to four consecutive proven worker
    # restarts (instance cf0c -> 3b56 -> 57c5 -> e0e8 -> 9838; uptimes
    # 26.9 -> 53.0 -> 57.6 -> 61.7 -> 62.7). With a bound of four
    # resubmissions the fourth resubmission succeeds on the same worker,
    # with no second Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-quad-recovered.log"
    env["PATH"] = _write_quad_restart_recovered_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "Resubmitted runner job job-q2 (replaces lost job job-q1)" in combined
    assert "Resubmitted runner job job-q3 (replaces lost job job-q2)" in combined
    assert "Resubmitted runner job job-q4 (replaces lost job job-q3)" in combined
    assert "Resubmitted runner job job-q5 (replaces lost job job-q4)" in combined
    assert "worker restart observed" in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-q5"
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 5
    assert "POST https://api.render.com/v1/services" not in calls
    bodies = (tmp_path / "curl-quad-recovered.log.bodies").read_text()
    assert bodies.count("Harness title") == 5


def _write_quint_restart_recovered_bin(directory, curl_log):
    """Fake bin reproducing run 36430429432 with a successful sixth attempt.

    Five consecutive proven worker restarts lose job-f1, job-f2, job-f3,
    job-f4, and job-f5 (each poll is an unknown-job 404 while /health
    stays healthy with a rotating instance id mirroring 7b01 -> 9eba ->
    d5ae -> 91c8 -> 9adf -> c0de and uptimes 26.3 -> 55.3 -> 54.4 ->
    53.3 -> 57.4 -> 58.1), and the fifth same-worker resubmission
    (job-f6) polls as succeeded. All six submits carry the identical
    task payload; no Render service is ever created.
    """
    bin_dir = Path(directory) / "bin-quint-recovered"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
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
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-f1|*/v1/jobs/job-f2|*/v1/jobs/job-f3|*/v1/jobs/job-f4|*/v1/jobs/job-f5)'
        ' emit 404 \'{"error":"unknown job"}\';;\n'
        '  */v1/jobs/job-f6)'
        ' emit 200 \'{"job_id":"job-f6","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' N=$(next_count "$LOG.post-count");'
        ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-f1"}\';'
        ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-f2"}\';'
        ' elif [[ "$N" -le 3 ]]; then emit 201 \'{"job_id":"job-f3"}\';'
        ' elif [[ "$N" -le 4 ]]; then emit 201 \'{"job_id":"job-f4"}\';'
        ' elif [[ "$N" -le 5 ]]; then emit 201 \'{"job_id":"job-f5"}\';'
        ' else emit 201 \'{"job_id":"job-f6"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' if [[ "$H" -le 2 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-7b01","uptime_seconds":26.3,"jobs":{"total":1}}\';'
        ' elif [[ "$H" -le 4 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-9eba","uptime_seconds":55.3,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 6 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-d5ae","uptime_seconds":54.4,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 8 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-91c8","uptime_seconds":53.3,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 10 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-9adf","uptime_seconds":57.4,"jobs":{"total":0}}\';'
        ' else'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-c0de","uptime_seconds":58.1,"jobs":{"total":0}}\';'
        ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_recovers_after_five_consecutive_restarts(tmp_path):
    # Regression for run 36430429432: the original job plus all four
    # resubmissions were lost to a five-restart cluster (instance 7b01
    # -> 9eba -> d5ae -> 91c8 -> 9adf -> c0de; uptimes 26.3 -> 55.3 ->
    # 54.4 -> 53.3 -> 57.4 -> 58.1, with live cgroup telemetry pinning
    # the driver to 512 MB memory pressure). With a bound of five
    # resubmissions the fifth resubmission succeeds on the same worker,
    # with no second Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-quint-recovered.log"
    env["PATH"] = _write_quint_restart_recovered_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "Resubmitted runner job job-f2 (replaces lost job job-f1)" in combined
    assert "Resubmitted runner job job-f3 (replaces lost job job-f2)" in combined
    assert "Resubmitted runner job job-f4 (replaces lost job job-f3)" in combined
    assert "Resubmitted runner job job-f5 (replaces lost job job-f4)" in combined
    assert "Resubmitted runner job job-f6 (replaces lost job job-f5)" in combined
    assert "worker restart observed" in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-f6"
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 6
    assert "POST https://api.render.com/v1/services" not in calls
    bodies = (tmp_path / "curl-quint-recovered.log.bodies").read_text()
    assert bodies.count("Harness title") == 6


def _write_sext_restart_recovered_bin(directory, curl_log):
    """Fake bin reproducing run 36434278632 with a successful seventh attempt.

    Six consecutive proven worker restarts lose job-s1, job-s2, job-s3,
    job-s4, job-s5, and job-s6 (each poll is an unknown-job 404 while
    /health stays healthy with a rotating instance id mirroring the
    failed run: 7126f50f2488 -> 052597e76e74 -> 89d9f277a377 ->
    f6d90020ee91 -> 1e9d729fe0bf -> fb20ff4cd8af -> 7d6acf859f67 and
    uptimes 25.5 -> 68.1 -> 66.8 -> 67.1 -> 67.6 -> 68.5 -> 50.9), and
    the sixth same-worker resubmission (job-s7) polls as succeeded. All
    seven submits carry the identical task payload; no Render service is
    ever created. The old fixed bound of five resubmissions fails this
    scenario on the sixth loss; budget-limited resubmission recovers.
    """
    bin_dir = Path(directory) / "bin-sext-recovered"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
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
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-s1|*/v1/jobs/job-s2|*/v1/jobs/job-s3|*/v1/jobs/job-s4|*/v1/jobs/job-s5|*/v1/jobs/job-s6)'
        ' emit 404 \'{"error":"unknown job"}\';;\n'
        '  */v1/jobs/job-s7)'
        ' emit 200 \'{"job_id":"job-s7","status":"succeeded","success":true,"summary":"done","metadata":{}}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then'
        ' N=$(next_count "$LOG.post-count");'
        ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-s1"}\';'
        ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-s2"}\';'
        ' elif [[ "$N" -le 3 ]]; then emit 201 \'{"job_id":"job-s3"}\';'
        ' elif [[ "$N" -le 4 ]]; then emit 201 \'{"job_id":"job-s4"}\';'
        ' elif [[ "$N" -le 5 ]]; then emit 201 \'{"job_id":"job-s5"}\';'
        ' elif [[ "$N" -le 6 ]]; then emit 201 \'{"job_id":"job-s6"}\';'
        ' else emit 201 \'{"job_id":"job-s7"}\'; fi;'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' if [[ "$H" -le 2 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-7126f50f2488","uptime_seconds":25.5,"jobs":{"total":1}}\';'
        ' elif [[ "$H" -le 4 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-052597e76e74","uptime_seconds":68.1,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 6 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-89d9f277a377","uptime_seconds":66.8,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 8 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-f6d90020ee91","uptime_seconds":67.1,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 10 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-1e9d729fe0bf","uptime_seconds":67.6,"jobs":{"total":0}}\';'
        ' elif [[ "$H" -le 12 ]]; then'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-fb20ff4cd8af","uptime_seconds":68.5,"jobs":{"total":0}}\';'
        ' else'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-7d6acf859f67","uptime_seconds":50.9,"jobs":{"total":0}}\';'
        ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_recovers_after_six_consecutive_restarts(tmp_path):
    # Regression for run 36434278632: the original job plus all five
    # resubmissions were lost to six consecutive proven worker restarts
    # (instances 7126 -> 0525 -> 89d9 -> f6d9 -> 1e9d -> fb20 -> 7d6a;
    # uptimes 25.5 -> 68.1 -> 66.8 -> 67.1 -> 67.6 -> 68.5 -> 50.9, well
    # inside the poll budget), failing fast with "resubmissions used:
    # 5/5" under the old fixed bound. With budget-limited resubmission
    # the sixth resubmission succeeds on the same worker, with no second
    # Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-sext-recovered.log"
    env["PATH"] = _write_sext_restart_recovered_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "Resubmitted runner job job-s2 (replaces lost job job-s1)" in combined
    assert "Resubmitted runner job job-s3 (replaces lost job job-s2)" in combined
    assert "Resubmitted runner job job-s4 (replaces lost job job-s3)" in combined
    assert "Resubmitted runner job job-s5 (replaces lost job job-s4)" in combined
    assert "Resubmitted runner job job-s6 (replaces lost job job-s5)" in combined
    assert "Resubmitted runner job job-s7 (replaces lost job job-s6)" in combined
    assert "worker restart observed" in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-s7"
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 7
    assert "POST https://api.render.com/v1/services" not in calls
    bodies = (tmp_path / "curl-sext-recovered.log.bodies").read_text()
    assert bodies.count("Harness title") == 7


def _write_same_process_lost_bin(directory, curl_log):
    """Fake bin where a healthy worker deterministically loses its own job.

    Every GET /v1/jobs/job-det is an unknown-job 404, but /health stays
    healthy on the SAME worker process (stable instance id, advancing
    uptime). An unknown-job 404 proves the poll reached the worker, so
    this loss cannot be a restart down-window: the loop must fail fast
    on the first proven loss with a deterministic-loss diagnostic and
    must not resubmit (exactly one worker POST, no second service).
    """
    bin_dir = Path(directory) / "bin-same-process"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
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
        'emit() { local code="$1" body="$2";'
        ' if [[ -n "$OUT" ]]; then printf "%s" "$body" > "$OUT";'
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-det)'
        ' emit 404 \'{"error":"unknown job: job-det"}\';;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then emit 201 \'{"job_id":"job-det"}\';'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' H=$(next_count "$LOG.health-count");'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-stable","uptime_seconds":\'$((300 + H))\',"jobs":{"total":1}}\';;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_fails_fast_on_deterministic_same_process_loss(tmp_path):
    # A healthy worker that no longer knows the job it accepted on the
    # SAME process is a deterministic defect, not a restart: an
    # unknown-job 404 proves the poll reached the worker, so
    # resubmission cannot recover it and the loop must fail fast on the
    # first proven loss instead of burning the poll budget.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-same-process.log"
    env["PATH"] = _write_same_process_lost_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "no longer knows job job-det" in combined
    assert "runner health: healthy" in combined
    assert "deterministic" in combined
    assert "same worker process lifetime" in combined
    assert "Resubmitted runner job" not in combined
    assert "did not finish in time" not in combined
    assert not result.exists() or result.read_text().strip() == ""
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 1
    assert "POST https://api.render.com/v1/services" not in calls


def _write_pressure_samples_file(path, instances=("storm-a", "storm-b", "storm-c")):
    """Seed sampler JSONL with pinned-at-limit pressure evidence.

    Mirrors the telemetry of run 36439192645 at the sample level:
    cgroup usage pinned at the 512 MB limit across rotating instance
    ids, so detect_memory_pressure() reports pressure from this file
    even after the background sampler appends its own gap samples.
    """
    limit = 512 * 1024 * 1024
    lines = []
    index = 0
    for instance in instances:
        for i in range(10):
            lines.append(json.dumps({
                "type": "sample",
                "timestamp": 1790607000.0 + index,
                "ok": True,
                "instance_id": instance,
                "memory_limit_bytes": limit,
                "memory_current_bytes": limit,
                "memory_events": {"high": 0, "max": i * 1000},
            }, sort_keys=True))
            index += 1
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_job_poll_abandons_memory_pressure_restart_storm(tmp_path):
    # Regression for run 36439192645 at the shell/HTTP level: seven
    # consecutive jobs were lost to seven proven worker restarts and
    # every one was resubmitted under the budget-limited policy; the
    # eighth job then stayed `running` until the shared budget ran out
    # ("did not finish in time (last status 'running')") with cgroup
    # usage pinned at 512 MB and +430,890 max-stall growth. With live
    # pressure evidence the loop must abandon after the streak reaches
    # the lifecycle threshold (initial submit + 3 resubmissions, then
    # fail fast on the fourth proven loss) instead of burning the full
    # budget -- with a storm diagnostic, no second Render service, and
    # no result file.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-storm.log"
    env["PATH"] = _write_lost_job_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    env["RENDER_MEMORY_SAMPLES_FILE"] = _write_pressure_samples_file(
        tmp_path / "storm-samples.jsonl")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "restart storm" in combined
    assert "consecutive proven worker restarts" in combined
    assert "memory pressure" in combined
    assert "resubmissions used: 3" in combined
    # Issue #113: the storm diagnostic must carry the auditable
    # decision inputs (the pre-seeded file pins usage at the limit
    # across three rotating instances with per-group surges of 9000,
    # below the 10000 surge threshold, so the verdict travels via the
    # replacements branch -- the same branch as live run 36493316814).
    assert "storm_threshold=3" in combined
    assert "via=replacements" in combined
    assert "pinned_ratio=1.0000" in combined
    assert "did not finish in time" not in combined
    assert "Resubmitted runner job" in combined
    assert not result.exists() or result.read_text().strip() == ""
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 4
    assert "POST https://api.render.com/v1/services" not in calls


def _write_transport_bin(directory, curl_log, *, fail_polls, fail_probes):
    """Fake bin where job polls return HTTP 502 and /health probes fail.

    Reproduces the terminal signature of run 36430429432 at the
    HTTP-contract level: GET /v1/jobs/<id> answers 502 for the first
    ``fail_polls`` polls (then succeeds), while the exit-code form of
    /health (the wait loop plus the periodic transport re-probes) fails
    for the first ``fail_probes`` probe calls after the initial healthy
    wait-loop call (then succeeds). The stdout form of /health always
    returns the submit-time baseline snapshot. ``fail_polls=10**9``
    with ``fail_probes=10**9`` models a permanently dead worker.
    """
    bin_dir = Path(directory) / "bin-transport"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'FAIL_POLLS=%d\n' % fail_polls +
        'FAIL_PROBES=%d\n' % fail_probes +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -D) i=$((i+2));;\n'
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
        'emit() { local code="$1" body="$2";'
        ' if [[ -n "$OUT" ]]; then printf "%s" "$body" > "$OUT";'
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
        '  */v1/jobs/job-tport)'
        ' P=$(next_count "$LOG.poll-count");'
        ' if [[ "$P" -le "$FAIL_POLLS" ]]; then emit 502 \'{"error":"bad gateway"}\';'
        ' else emit 200 \'{"job_id":"job-tport","status":"succeeded","success":true,"summary":"done","metadata":{}}\'; fi;;\n'
        '  */v1/jobs)'
        ' if [[ "$METHOD" == "POST" ]]; then emit 201 \'{"job_id":"job-tport"}\';'
        ' else emit 404 \'{"error":"x"}\'; fi;;\n'
        '  */health)'
        ' if [[ -n "$WANT_CODE" || -n "$OUT" ]]; then'
        ' H=$(next_count "$LOG.health-probe-count");'
        ' if [[ "$H" -le 1 ]]; then exit 0; fi;'
        ' if [[ "$((H - 1))" -le "$FAIL_PROBES" ]]; then exit 1; fi;'
        ' exit 0;'
        ' fi;'
        ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-base","uptime_seconds":26.3,"jobs":{"total":1}}\';;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_survives_transient_transport_down_window(tmp_path):
    # Regression for run 36430429432: the attempt failed on the FIRST
    # failed /health probe after only five consecutive HTTP 502 polls,
    # but a Free restart produces exactly that transient signature while
    # the replacement boots. Fourteen consecutive 502s with the first
    # two probes failing must NOT fail fast: the third probe succeeds
    # and the fifteenth poll succeeds on the same worker.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-transport-transient.log"
    env["PATH"] = _write_transport_bin(
        tmp_path, log, fail_polls=14, fail_probes=2) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "failed probe 1/3" in combined
    assert "failed probe 2/3" in combined
    assert "mid-restart" in combined
    assert "failing fast" not in combined
    payload = json.loads(result.read_text())
    assert payload["status"] == "succeeded"
    assert payload["job_id"] == "job-tport"
    calls = log.read_text()
    assert "POST https://api.render.com/v1/services" not in calls


def test_job_poll_fails_fast_after_sustained_transport_unhealthiness(tmp_path):
    # The other half of run 36430429432: a worker that never comes back
    # must still fail fast with a diagnostic -- after sustained
    # unhealthiness (3 consecutive failed probes = 15 consecutive 502
    # polls, ~5 minutes), never by waiting out the full 140-poll
    # budget, and never by provisioning a second Render service.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-transport-sustained.log"
    env["PATH"] = _write_transport_bin(
        tmp_path, log, fail_polls=10 ** 9, fail_probes=10 ** 9) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "3 consecutive probes" in combined
    assert "15 consecutive times" in combined
    assert "failing fast" in combined
    assert not result.exists() or result.read_text().strip() == ""
    calls = log.read_text()
    assert calls.count("GET http://fake-runner.local/v1/jobs/job-tport") == 15
    assert "POST https://api.render.com/v1/services" not in calls


def _write_instance_change_bin(directory, curl_log):
    """Fake bin reproducing run 36410676408 (issue #41).

    Five jobs are lost to consecutive worker replacements while /health
    stays healthy, but the replacement uptimes are numerically GREATER
    than the old snapshot (25.9 -> 48.9 -> 60.3 -> 12.7 -> 18.4 -> 22.1):
    only the instance id (and wall-clock drift) proves the restart. The
    script must report "worker restart observed" with instance evidence
    -- never "same worker process lifetime" -- and keep resubmitting on
    the same worker while budget remains (run 36434278632 retired the
    old fixed bound of five). Past the six modeled instances every
    further /health answer carries a fresh instance id, so every later
    loss still proves a restart and the run ends only when the poll
    budget is exhausted -- without a second service.
    """
    bin_dir = Path(directory) / "bin-instance"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'OUT=""; METHOD="GET"; DATA=""; URL=""; WANT_CODE=""\n'
        'ARGS=("$@")\n'
        'i=0\n'
        'while [[ $i -lt ${#ARGS[@]} ]]; do\n'
        '  case "${ARGS[$i]}" in\n'
        '    -o) OUT="${ARGS[$((i+1))]}"; i=$((i+2));;\n'
        '    -D) i=$((i+2));;\n'
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
        'emit() { local code="$1" body="$2";'
        ' if [[ -n "$OUT" ]]; then printf "%s" "$body" > "$OUT";'
        ' else printf "%s" "$body"; fi;'
        ' if [[ -n "$WANT_CODE" ]]; then printf "%s" "$code"; fi; }\n'
        'next_count() { local f="$1" n=0;'
        ' [[ -f "$f" ]] && n=$(cat "$f"); n=$((n+1)); echo "$n" > "$f";'
        ' printf "%s" "$n"; }\n'
        'case "$URL" in\n'
        '  */owners*) emit 200 \'[{"id":"own-1"}]\';;\n'
        '  */v1/services/deploys/*|*/deploys/*) emit 200 \'{"status":"live"}\';;\n'
  '  */v1/jobs/job-41a|*/v1/jobs/job-41b|*/v1/jobs/job-41c|*/v1/jobs/job-41d|*/v1/jobs/job-41e|*/v1/jobs/job-41f)'
  ' emit 404 \'{"error":"unknown job"}\';;\n'
  '  */v1/jobs)'
  ' if [[ "$METHOD" == "POST" ]]; then'
  ' N=$(next_count "$LOG.post-count");'
  ' if [[ "$N" -le 1 ]]; then emit 201 \'{"job_id":"job-41a"}\';'
  ' elif [[ "$N" -le 2 ]]; then emit 201 \'{"job_id":"job-41b"}\';'
  ' elif [[ "$N" -le 3 ]]; then emit 201 \'{"job_id":"job-41c"}\';'
  ' elif [[ "$N" -le 4 ]]; then emit 201 \'{"job_id":"job-41d"}\';'
  ' elif [[ "$N" -le 5 ]]; then emit 201 \'{"job_id":"job-41e"}\';'
  ' else emit 201 \'{"job_id":"job-41f"}\'; fi;'
  ' else emit 404 \'{"error":"x"}\'; fi;;\n'
  '  */health)'
  ' H=$(next_count "$LOG.health-count");'
  ' if [[ "$H" -le 2 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-aaa111","uptime_seconds":25.9,"jobs":{"total":1}}\';'
  ' elif [[ "$H" -le 4 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-bbb222","uptime_seconds":48.9,"jobs":{"total":0}}\';'
  ' elif [[ "$H" -le 6 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-ccc333","uptime_seconds":60.3,"jobs":{"total":0}}\';'
  ' elif [[ "$H" -le 8 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-ddd444","uptime_seconds":12.7,"jobs":{"total":0}}\';'
  ' elif [[ "$H" -le 10 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-eee555","uptime_seconds":18.4,"jobs":{"total":0}}\';'
  ' elif [[ "$H" -le 12 ]]; then'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-fff666","uptime_seconds":22.1,"jobs":{"total":0}}\';'
  ' else'
  ' emit 200 \'{"status":"ok","ready":true,"instance_id":"instance-late-\'"$H"\'","uptime_seconds":9,"jobs":{"total":0}}\';'
  ' fi;;\n'
        '  */services/srv-existing)'
        ' emit 200 \'{"serviceDetails":{"plan":"free","url":"http://fake-runner.local"}}\';;\n'
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


def test_job_poll_proves_restart_from_instance_change_despite_greater_uptime(tmp_path):
    # Regression for run 36410676408 (issue #41): uptimes 25.9 -> 48.9
    # -> 60.3 are all numerically increasing, so the legacy
    # "current < prior" detector wrongly reported "same worker process
    # lifetime". The instance id must prove the replacement instead.
    # Extended for run 36417263684 (two consecutive proven restarts),
    # run 36422228148 (three consecutive proven restarts), run
    # 36425019190 (four consecutive proven restarts), run 36430429432
    # (five consecutive proven restarts) and run 36434278632 (six
    # consecutive proven restarts retired the fixed bound of five: the
    # sixth loss is now another same-worker retry, and an endless
    # restart cluster ends only when the poll budget is exhausted).
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-instance.log"
    env["PATH"] = _write_instance_change_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    state.write_text(json.dumps({
        "serviceId": "srv-existing",
        "deployId": "dep-1",
        "region": "oregon",
        "model": PREFERRED_MODEL,
        "plan": "free",
    }))
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "Resubmitted runner job job-41b (replaces lost job job-41a)" in combined
    assert "Resubmitted runner job job-41c (replaces lost job job-41b)" in combined
    assert "Resubmitted runner job job-41d (replaces lost job job-41c)" in combined
    assert "Resubmitted runner job job-41e (replaces lost job job-41d)" in combined
    assert "Resubmitted runner job job-41f (replaces lost job job-41e)" in combined
    # No fixed-bound fail-fast: the sixth consecutive loss is retried.
    assert "resubmissions used: 5/5" not in combined
    # The restart is proven by instance identity despite greater uptime.
    assert "worker restart observed" in combined
    assert "instance" in combined
    assert "same worker process lifetime" not in combined
    # The endless cluster ends when the poll budget is exhausted.
    assert "did not finish in time" in combined
    assert "46 resubmission(s) used" in combined
    calls = log.read_text()
    assert calls.count("POST http://fake-runner.local/v1/jobs") == 47
    assert "POST https://api.render.com/v1/services" not in calls


def test_job_poll_loop_matches_lifecycle_budget_and_covers_runner_timeout():
    # Regression for run 36399649036: render-job.sh polled only 60x20s
    # while the runner may work up to RUNNER_JOB_TIMEOUT_SECONDS, so the
    # shell must track JOB_POLL_MAX_ATTEMPTS instead of drifting.
    # Regression for run 36493364897: the bound must honor the
    # shell-resolved POLL_MAX_ATTEMPTS (validated numeric, lifecycle
    # default on corrupt values) instead of duplicating the lifecycle
    # constant as a literal -- a resolved value and its use must never
    # silently diverge.

    job = _read("render-job.sh")
    assert "JOB_POLL_MAX_ATTEMPTS" in job
    assert "POLL_MAX_ATTEMPTS" in job
    assert "for ((i = 1; i <= POLL_MAX_ATTEMPTS; i++))" in job
    assert "for ((i = 1; i <= 140; i++))" not in job, \
        "poll loop bound must honor POLL_MAX_ATTEMPTS, not a literal"
    assert JOB_POLL_MAX_ATTEMPTS * JOB_POLL_INTERVAL_SECONDS >= (
        RUNNER_JOB_TIMEOUT_SECONDS + 60
    )
    # The terminal poll guards must use the same resolved bound,
    # otherwise the loop exits early or never reports the timeout.
    assert '"$i" -eq "$POLL_MAX_ATTEMPTS"' in job
    assert '"$i" -eq %d' % JOB_POLL_MAX_ATTEMPTS not in job


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


def _write_exact_artifact_bin(directory, curl_log):
    """Fake bin whose issue declares an exact workflow-artifact contract.

    Reproduces the issue #106 shape behind run 36495681860 (issue
    #115): the body pins one numeric Actions artifact id, its source
    # workflow run, and a sha256 archive digest with a no-substitution
    # rule. The Render path cannot deliver workflow artifacts, so the
    # pre-creation gate must refuse the attempt.
    """
    bin_dir = Path(directory) / "bin-artifact"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'echo "GET $*" >> "$LOG"\n'
        'printf "%s" "{}"\n'
        "exit 0\n",
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
        "  printf '%s' "
        "'{\"title\":\"Qualify the same OpenCode PR artifact on Render\","
        "\"body\":\"Exact artifact under test. Do not rebuild OpenCode. "
        "Workflow run: 36492639568. "
        "Artifact name: opencode-coding-linux-x64. "
        "Artifact ID: 11001896223. "
        "Artifact archive digest: "
        "sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df. "
        "Download artifact ID 11001896223 from source workflow run 36492639568. "
        "Verify before launch. "
        "Never silently fall back to another OpenCode binary.\"}'\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return str(bin_dir)


def _write_exact_artifact_bin_issue110(directory, curl_log):
    """Fake bin whose issue declares the exact contract in #110 phrasing.

    Reproduces the source-issue #110 shape behind run 36498921649
    (repair issue #121): the same numeric Actions artifact id, source
    workflow run, and sha256 archive digest as #106, but phrased as
    "Source workflow run" / "Artifact ID" / "Artifact archive digest"
    with a "Source SHA" line and a retention-expiry line. The Render
    path cannot deliver workflow artifacts, so the pre-creation gate
    must refuse the attempt before any Render service is created --
    in seconds with zero Render cost, instead of the ~11-minute
    substituted-binary storm the pre-gate base burned.
    """
    bin_dir = Path(directory) / "bin-artifact-110"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="%s"\n' % curl_log +
        'echo "GET $*" >> "$LOG"\n'
        'printf "%s" "{}"\n'
        "exit 0\n",
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
        "  printf '%s' "
        "'{\"title\":\"P0: Fresh Render run -- execute exact OpenCode PR #12 artifact\","
        "\"body\":\"Exact immutable artifact under test. Do not rebuild OpenCode. "
        "Source SHA: 8ed6c749577d534c55ba9555ba4918ea8be95a97. "
        "Source workflow run: 36492639568. "
        "Artifact name: opencode-coding-linux-x64. "
        "Artifact ID: 11001896223. "
        "Artifact archive digest: "
        "sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df. "
        "Artifact retention expiry: 2026-10-28. "
        "Download exact artifact ID 11001896223 from source run 36492639568. "
        "Verify before launch. "
        "Never silently fall back to another OpenCode binary.\"}'\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return str(bin_dir)


def test_job_gate_references_exact_artifact_helpers_before_creation():
    # Static placement: the exact-artifact gate must decide before any
    # Render service can be created, so a blocked attempt costs nothing.
    job = _read("render-job.sh")
    assert "parse_exact_workflow_artifact_requirement" in job
    assert "exact_workflow_artifact_blocker" in job
    assert "build_exact_artifact_refusal_result" in job
    assert "EXACT_ARTIFACT_BLOCKER" in job
    assert job.index("EXACT_ARTIFACT_BLOCKER") < job.index(
        "One service creation per attempt")


def test_job_refuses_exact_workflow_artifact_before_creation(tmp_path):
    # Regression for run 36495681860 (issue #115): without the gate the
    # worker silently substituted the baseline binary, whose ~600 MB
    # agent OOM-restarted four times and storm-aborted after ~13
    # minutes on a run that never tested the required artifact. With
    # the gate the attempt fails closed before any Render call.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-artifact.log"
    env["PATH"] = _write_exact_artifact_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "infrastructure-blocked" in combined
    assert "11001896223" in combined
    assert "baseline binary" in combined
    # No Render service was created and no state was recorded.
    assert not log.exists() or "api.render.com/v1/services" not in log.read_text()
    assert not state.exists() or "srv-" not in state.read_text()
    # Repair issue #125 (run 36500759174): the gated refusal must also
    # leave a stable machine-readable record so triage/schedulers need
    # not scrape log text to tell a permanent block from a transient
    # failure. The record is distinct from runner job results.
    payload = json.loads(result.read_text())
    assert payload["status"] == "infrastructure-blocked"
    assert payload["permanent"] is True
    assert payload["artifact_id"] == "11001896223"
    assert payload["source_run_id"] == "36492639568"
    assert "infrastructure-blocked" in payload["reason"]


def test_job_refuses_issue110_exact_artifact_before_creation(tmp_path):
    # Regression for run 36498921649 (repair issue #121): source issue
    # #110 pins the same exact PR #12 artifact as #106 but with
    # distinct phrasing ("Source workflow run", "Artifact ID",
    # "Artifact archive digest", "Source SHA", plus a retention-expiry
    # line). The pre-gate base burned ~11 minutes and four restarts on
    # the substituted baseline binary for this contract; the gated
    # path must refuse it in seconds with zero Render cost, exactly as
    # the live run did (execute=failure, cleanup=success, no service).
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-artifact-110.log"
    env["PATH"] = _write_exact_artifact_bin_issue110(tmp_path, log) + os.pathsep + env.get("PATH", "")
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "infrastructure-blocked" in combined
    assert "11001896223" in combined
    assert "36492639568" in combined
    assert "baseline binary" in combined
    # Repair issue #123 (run 36499977510): the refusal for this known
    # immutable artifact must also carry the validated 0.0.0
    # version-gate advisory so a future delivery mechanism cannot
    # misread the artifact's seconds-fast provider-gate fast-fail as
    # a memory result.
    assert "0.0.0" in combined
    assert "opencode_max_headless_qualify" in combined
    # No Render service was created and no state was recorded.
    assert not log.exists() or "api.render.com/v1/services" not in log.read_text()
    assert not state.exists() or "srv-" not in state.read_text()
    # Repair issue #125 (run 36500759174): the #110 refusal must also
    # leave the stable structured record (permanent block marker plus
    # the known 0.0.0 advisory flag) with zero Render cost.
    payload = json.loads(result.read_text())
    assert payload["status"] == "infrastructure-blocked"
    assert payload["permanent"] is True
    assert payload["artifact_id"] == "11001896223"
    assert payload["source_run_id"] == "36492639568"
    assert payload["has_known_advisory"] is True
    assert "infrastructure-blocked" in payload["reason"]
    # Repair issue #133 (run 36503746345): the fourth live refusal of
    # this retired contract proved the loop itself is the defect, so
    # the #110 refusal must carry the superseded redirect end to end
    # (log line plus structured successor pointer) instead of reading
    # like a merely undeliverable contract.
    assert "Superseded-artifact notice" in combined
    assert "11004835952" in combined
    assert payload["superseded"] is True
    assert payload["successor"]["successor_artifact_id"] == "11004835952"
    assert payload["successor"]["successor_source_run_id"] == "36498663107"


def test_job_names_superseded_hold_before_exact_gate():
    # Static placement for repair issue #161 (run 36632062583): that
    # run executed at a base already containing the #154
    # superseded_dispatch_guard, yet the retired #106 body
    # redispatched and refused identically while minting one more P0
    # repair, because the scheduler envelope and the repair-reset
    # unpause never consult decide_eligible. The executor -- the one
    # production chokepoint this repository owns -- must therefore
    # name the retired-contract hold FIRST with one stable
    # machine-greppable verdict line, while the exact-artifact gate
    # stays the single refusal choke point. Ordinary and
    # supported-contract bodies must never trip the hold (locked at
    # the Python level in test_render_lifecycle.py).
    job = _read("render-job.sh")
    assert "superseded_dispatch_guard" in job
    assert "held-superseded" in job
    assert "SUPERSEDED_HOLD_JSON" in job
    assert job.index("superseded_dispatch_guard") < job.index(
        "EXACT_ARTIFACT_BLOCKER")
    assert job.index("held-superseded") < job.index(
        "One service creation per attempt")
    # The hold is advisory and read-only: no GitHub writes are added.
    assert "gh issue edit" not in job
    assert "gh issue comment" not in job
    assert "gh issue close" not in job


def test_job_emits_hold_verdict_for_retired_contract(tmp_path):
    # Live regression for run 36632062583 (repair issue #161): the
    # retired #106-shaped body must emit the stable hold verdict naming
    # both the retired contract and its successor, then refuse through
    # the unchanged exact-artifact gate with zero Render cost.
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-hold.log"
    env["PATH"] = _write_exact_artifact_bin(tmp_path, log) + os.pathsep + env.get("PATH", "")
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "held-superseded" in combined
    assert "11001896223" in combined
    assert "36492639568" in combined
    assert "11004835952" in combined
    assert "36498663107" in combined
    # The hold names the verdict; the gate still owns the refusal.
    assert combined.index("held-superseded") < combined.index(
        "infrastructure-blocked")
    assert "baseline binary" in combined
    assert not log.exists() or "api.render.com/v1/services" not in log.read_text()
    assert not state.exists() or "srv-" not in state.read_text()
    payload = json.loads(result.read_text())
    assert payload["status"] == "infrastructure-blocked"
    assert payload["permanent"] is True
    assert payload["superseded"] is True
    assert payload["successor"]["successor_artifact_id"] == "11004835952"


def test_job_emits_hold_verdict_for_retired_contract_issue110(tmp_path):
    # Same hold verdict for the #110 phrasing of the identical retired
    # contract (run 36632062583 pins the #106 body; the #110 body pins
    # the same artifact/run pair with distinct wording and must hold
    # identically instead of reading like a merely undeliverable
    # contract).
    env, state, result = _base_env(tmp_path)
    log = tmp_path / "curl-hold-110.log"
    env["PATH"] = _write_exact_artifact_bin_issue110(tmp_path, log) + os.pathsep + env.get("PATH", "")
    proc = _run("render-job.sh", env, str(REPO_ROOT))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "held-superseded" in combined
    assert "11001896223" in combined
    assert "11004835952" in combined
    assert combined.index("held-superseded") < combined.index(
        "infrastructure-blocked")
    assert not log.exists() or "api.render.com/v1/services" not in log.read_text()
    assert not state.exists() or "srv-" not in state.read_text()
    payload = json.loads(result.read_text())
    assert payload["superseded"] is True
    assert payload["successor"]["successor_artifact_id"] == "11004835952"
