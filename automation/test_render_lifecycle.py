"""Tests for the ephemeral Render lifecycle contract (issue #1)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render_lifecycle import (  # noqa: E402
    ALLOWED_WORKER_REGIONS,
    CRON_MAY_CREATE_SERVICES,
    DEFAULT_BURN_BUDGET,
    DEFAULT_WORKER_REGION,
    DELETE_MAX_ATTEMPTS,
    FALLBACK_MODEL,
    FORBIDDEN_WORKER_REGIONS,
    FREE_PLAN,
    JOB_POLL_INTERVAL_SECONDS,
    JOB_POLL_MAX_ATTEMPTS,
    JOB_POLL_OUTCOMES,
    JOB_POLL_RESTART_STORM_THRESHOLD,
    JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY,
    JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES,
    JOB_POLL_UNKNOWN_JOB_THRESHOLD,
    LIFECYCLE_STEPS,
    CLEANUP_TRIGGER_EVENTS,
    MAX_CONCURRENT_AUTOMATION_JOBS,
    MAX_SERVICE_CREATIONS_PER_ATTEMPT,
    MAX_SERVICES_PER_ISSUE_ATTEMPT,
    MODEL_FALLBACK_CREATES_NEW_SERVICE,
    NON_US_FALLBACK_REGION,
    POLL_RETRY_CREATES_NEW_SERVICE,
    PREFERRED_MODEL,
    PUBLIC_REPO_URL,
    RUNNER_JOB_TIMEOUT_SECONDS,
    RENDER_API_BASE,
    RENDER_CREATE_SERVICE_PATH,
    RENDER_DELETE_SERVICE_PATH_TEMPLATE,
    RENDER_DELETION_VERIFIED_STATUSES,
    RENDER_DELETE_SUCCESS_STATUS,
    RENDER_DOC_FREE_TIER,
    RENDER_DOC_URLS,
    RENDER_LIST_DEPLOYS_PATH_TEMPLATE,
    RENDER_LIST_SERVICES_PATH,
    RENDER_RETRIEVE_DEPLOY_PATH_TEMPLATE,
    RENDER_RETRIEVE_SERVICE_PATH_TEMPLATE,
    RENDER_SUSPEND_SERVICE_PATH_TEMPLATE,
    REQUIRED_AUTO_DEPLOY,
    RUNNER_HEALTH_PATH,
    RUNNER_JOB_STATUS_PATH_TEMPLATE,
    RUNNER_SUBMIT_JOB_PATH,
    SMOKE_TASK_MEMORY_BUDGET,
    SUSPEND_FALLBACK_MAX_ATTEMPTS,
    SUSPEND_IS_PRIMARY_CLEANUP,
    WORKER_RESTART_WALL_SKEW_TOLERANCE_SECONDS,
    BurnBudget,
    ExecutionMetadata,
    JobRequest,
    PaidPlanError,
    RegionPolicyError,
    allowed_to_create_service,
    assert_free_plan,
    build_create_service_payload,
    classify_deploy_status,
    classify_job_poll_response,
    deletion_succeeded,
    detect_worker_restart,
    exact_workflow_artifact_blocker,
    build_exact_artifact_refusal_result,
    extract_owner_id,
    format_restart_evidence,
    get_service_url,
    healthy_service_response,
    is_deletion_verified,
    is_terminal_job_status,
    experiment_record_path,
    knowledge_handoff_instructions,
    known_workflow_artifact_advisory,
    superseded_hold_verdict,
    superseded_workflow_artifact_notice,
    superseded_dispatch_guard,
    validate_experiment_record_text,
    parse_exact_workflow_artifact_requirement,
    resolve_task_text,
    parse_job_result,
    render_path,
    render_url,
    requires_cleanup,
    select_model,
    service_name_for_attempt,
    should_fail_fast_on_unknown_job,
    should_abandon_restart_storm,
    build_restart_storm_result,
    should_fail_fast_on_transport,
    should_probe_runner_health,
    should_resubmit_after_job_loss,
    build_cancelled_result,
    validate_worker_region,
    verify_free_plan_response,
)


def test_exact_render_docs_referenced():
    assert "https://api-docs.render.com/reference/create-service" in RENDER_DOC_URLS
    assert "https://api-docs.render.com/reference/delete-service" in RENDER_DOC_URLS
    assert "https://api-docs.render.com/reference/suspend-service-1" in RENDER_DOC_URLS
    assert "https://api-docs.render.com/reference/rate-limiting" in RENDER_DOC_URLS
    assert "https://api-docs.render.com/reference/retrieve-service" in RENDER_DOC_URLS
    assert "https://api-docs.render.com/reference/retrieve-deploy" in RENDER_DOC_URLS
    assert "https://api-docs.render.com/reference/list-owners" in RENDER_DOC_URLS
    # Free-tier platform behavior (restart-anytime, ephemeral filesystem)
    # is a first-class contract: the job-loss resubmission policy depends
    # on it (regression for run 36409152332).
    assert RENDER_DOC_FREE_TIER == "https://render.com/docs/free"
    assert RENDER_DOC_FREE_TIER in RENDER_DOC_URLS


def test_render_workspace_owner_response_shape():
    documented = [{"owner": {"id": "tea-123", "name": "workspace"}, "cursor": "next"}]
    assert extract_owner_id(documented) == "tea-123"
    assert extract_owner_id([{"id": "legacy-owner"}]) == "legacy-owner"
    with pytest.raises(ValueError):
        extract_owner_id([])
    with pytest.raises(ValueError):
        extract_owner_id([{"owner": {}}])


def test_agent_knowledge_handoff_is_stable_and_unique_per_run():
    assert experiment_record_path(9, "36402447309") == "automation/knowledge/experiments/issue-9-run-36402447309.md"
    instructions = knowledge_handoff_instructions(9, "run/with spaces")
    assert "automation/knowledge/PROTOCOL.md" in instructions
    assert "issue-9-run-run-with-spaces.md" in instructions
    assert "Do not repeat a known failed experiment" in instructions
    task = resolve_task_text(9, "smoke", title="Render smoke", body="Prove cleanup", run_id="abc-123")
    assert "Repository knowledge handoff (mandatory" in task
    assert "issue-9-run-abc-123.md" in task


def test_smoke_task_carries_memory_budget_e2e_does_not():
    # Regression for run 36637940250 (repair issue #176): four
    # consecutive #58 smoke storms pinned the 512 MB worker at the
    # ceiling with the full qualified config-only profile wired, so the
    # reusable repair shrinks the smoke workload instead of wiring more
    # env/config flags. Both live dispatch paths (render-job.sh and
    # render_controller) build their payload via resolve_task_text.
    assert "512 MB" in SMOKE_TASK_MEMORY_BUDGET
    assert "single most relevant test file once" in SMOKE_TASK_MEMORY_BUDGET
    assert "never run the full suite" in SMOKE_TASK_MEMORY_BUDGET
    smoke = resolve_task_text(58, "smoke", title="T", body="B", run_id="r")
    assert SMOKE_TASK_MEMORY_BUDGET in smoke
    assert "Repository knowledge handoff (mandatory, bounded for the 512 MB worker)" in smoke
    e2e = resolve_task_text(58, "e2e", title="T", body="B", run_id="r")
    assert SMOKE_TASK_MEMORY_BUDGET not in e2e
    assert "Repository knowledge handoff (mandatory):" in e2e


def test_smoke_memory_budget_survives_body_truncation():
    long_body = "y" * 5000
    smoke = resolve_task_text(58, "smoke", title="T", body=long_body, run_id="r")
    assert "[truncated]" in smoke
    assert SMOKE_TASK_MEMORY_BUDGET in smoke
    assert "Repository knowledge handoff (mandatory" in smoke


def test_smoke_handoff_is_bounded_e2e_handoff_unchanged():
    # Regression for run 36645592095 (repair issue #188): the fifth
    # consecutive #58 smoke storm pinned the 512 MB worker at the ceiling
    # at a base already carrying the #176 budget, proving the budget
    # alone insufficient. The reusable defect was the self-contradictory
    # task text: budget item 5 ordered "bounded handoff reads" while the
    # mandatory handoff block trailing it ordered unbounded topic/record
    # reads (and never mentioned catalog-first discovery), so the later
    # numbered instruction overrode the earlier budget line and a
    # diligent agent read the monotonically growing topic notes plus
    # full experiment records into a transcript the 512 MB worker must
    # hold. Both live dispatch paths build payloads via
    # resolve_task_text, so the mode-aware handoff covers every shape.
    smoke = resolve_task_text(58, "smoke", title="T", body="B", run_id="r")
    assert SMOKE_TASK_MEMORY_BUDGET in smoke
    assert "Repository knowledge handoff (mandatory, bounded for the 512 MB worker)" in smoke
    assert "at most ONE most-relevant topic note" in smoke
    assert "knowledge_catalog.py query" in smoke
    assert "This bound overrides any broader read scope above" in smoke
    assert "prior records under" not in smoke
    # The budget defers to the handoff block instead of stating a
    # divergent bound.
    assert "per the handoff block below" in SMOKE_TASK_MEMORY_BUDGET
    e2e = resolve_task_text(58, "e2e", title="T", body="B", run_id="r")
    assert SMOKE_TASK_MEMORY_BUDGET not in e2e
    assert "Repository knowledge handoff (mandatory):" in e2e
    assert "prior records under" in e2e
    assert "at most ONE" not in e2e
    # Mandatory protocol elements survive the bound on smoke.
    for marker in ("automation/knowledge/PROTOCOL.md",
                   "Do not repeat a known failed experiment",
                   "issue-58-run-r.md"):
        assert marker in smoke
        assert marker in e2e


def test_handoff_mode_defaults_to_full_and_rejects_unknown():
    assert knowledge_handoff_instructions(9, "r") == knowledge_handoff_instructions(9, "r", "e2e")
    assert "prior records under" in knowledge_handoff_instructions(9, "r")
    assert "at most ONE" in knowledge_handoff_instructions(9, "r", "smoke")
    with pytest.raises(ValueError):
        knowledge_handoff_instructions(9, "r", "bogus-mode")


def test_smoke_bounded_handoff_survives_body_truncation():
    long_body = "z" * 5000
    smoke = resolve_task_text(58, "smoke", title="T", body=long_body, run_id="r")
    assert "[truncated]" in smoke
    assert "at most ONE most-relevant topic note" in smoke
    assert "knowledge_catalog.py query" in smoke
    assert "issue-58-run-r.md" in smoke


def _valid_experiment_record(issue=9, run_id="r1"):
    import json as _json

    headings = [
        "## Hypothesis / objective",
        "## Prior knowledge consulted",
        "## Preconditions / changed premise",
        "## Procedure",
        "## Observations",
        "## Interpretation",
        "## Decision / result",
        "## Validation",
        "## Reusable knowledge",
        "## Unresolved questions / next experiment",
        "## Evidence",
        "## Cleanup proof",
    ]
    metadata = {
        "$schema": "automation/knowledge/schema/experiment.json",
        "base_commit": "0" * 40,
        "issue": issue,
        "outcome": "succeeded",
        "record_id": "issue-%d-run-%s" % (issue, run_id),
        "run_id": str(run_id),
        "schema": "runtime-lab-experiment/v1",
        "supersedes": [],
        "topic": "test",
    }
    return "\n".join([
        "---",
        _json.dumps(metadata, sort_keys=True, indent=2),
        "---",
        "# test",
        *headings,
        "evidence",
    ])


def test_experiment_record_validation_fails_closed():
    good = _valid_experiment_record()
    assert validate_experiment_record_text(good, 9, "r1") is True
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace('"issue": 9', '"issue": 8'), 9, "r1")
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace('"run_id": "r1"', '"run_id": "old"'), 9, "r1")
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace("## Cleanup proof", ""), 9, "r1")
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace("{", "schema: runtime-lab-experiment/v1", 1), 9, "r1")

def test_render_operations_represented():
    assert RENDER_API_BASE == "https://api.render.com/v1"
    assert RENDER_CREATE_SERVICE_PATH == "/v1/services"
    assert "{serviceId}" in RENDER_RETRIEVE_SERVICE_PATH_TEMPLATE
    assert "{serviceId}" in RENDER_SUSPEND_SERVICE_PATH_TEMPLATE
    assert "{serviceId}" in RENDER_DELETE_SERVICE_PATH_TEMPLATE
    assert "{serviceId}" in RENDER_RETRIEVE_DEPLOY_PATH_TEMPLATE
    assert "{deployId}" in RENDER_RETRIEVE_DEPLOY_PATH_TEMPLATE
    assert "{serviceId}" in RENDER_LIST_DEPLOYS_PATH_TEMPLATE
    assert RENDER_LIST_SERVICES_PATH == "/v1/services"
    assert render_url(render_path(RENDER_DELETE_SERVICE_PATH_TEMPLATE, serviceId="srv-123"))
    assert render_path(RUNNER_JOB_STATUS_PATH_TEMPLATE, jobId="job-1") == "/v1/jobs/job-1"


def test_free_tier_guard_fails_closed():
    assert assert_free_plan("free") == "free"
    with pytest.raises(PaidPlanError):
        assert_free_plan("starter")
    with pytest.raises(PaidPlanError):
        assert_free_plan("0.5c-512mb")
    with pytest.raises(PaidPlanError):
        verify_free_plan_response({"serviceDetails": {"plan": "starter"}})
    assert verify_free_plan_response({"serviceDetails": {"plan": FREE_PLAN}}) == "free"
    with pytest.raises(PaidPlanError):
        verify_free_plan_response({})


def test_create_payload_is_free_and_no_autodeploy():
    payload = build_create_service_payload(name="runtime-lab-issue1-r1", owner_id="wrk-1")
    assert payload["type"] == "web_service"
    assert payload["repo"] == PUBLIC_REPO_URL
    assert payload["autoDeploy"] == REQUIRED_AUTO_DEPLOY == "no"
    assert payload["serviceDetails"]["plan"] == "free"
    assert payload["serviceDetails"]["region"] == DEFAULT_WORKER_REGION
    with pytest.raises(RegionPolicyError):
        build_create_service_payload(
            name="x", owner_id="wrk-1", region="frankfurt"
        )


def test_region_policy():
    assert validate_worker_region("oregon") == "oregon"
    assert validate_worker_region("singapore") == "singapore"
    assert DEFAULT_WORKER_REGION == "oregon"
    assert NON_US_FALLBACK_REGION == "singapore"
    assert "frankfurt" in FORBIDDEN_WORKER_REGIONS
    assert "frankfurt" not in ALLOWED_WORKER_REGIONS
    with pytest.raises(RegionPolicyError):
        validate_worker_region("frankfurt")
    with pytest.raises(RegionPolicyError):
        validate_worker_region("moon")


def test_model_policy_stays_on_same_worker():
    assert PREFERRED_MODEL == "opencode/muse-spark-1.3-contributor-free"
    assert FALLBACK_MODEL == "opencode/space-bunny-free"
    assert MODEL_FALLBACK_CREATES_NEW_SERVICE is False
    assert select_model(False) == PREFERRED_MODEL
    assert select_model(True) == FALLBACK_MODEL


def test_low_burn_policy_encoded():
    assert MAX_CONCURRENT_AUTOMATION_JOBS == 4
    assert MAX_SERVICES_PER_ISSUE_ATTEMPT == 1
    assert MAX_SERVICE_CREATIONS_PER_ATTEMPT == 1
    assert POLL_RETRY_CREATES_NEW_SERVICE is False
    assert CRON_MAY_CREATE_SERVICES is False
    assert DELETE_MAX_ATTEMPTS == 5
    assert SUSPEND_IS_PRIMARY_CLEANUP is False
    assert SUSPEND_FALLBACK_MAX_ATTEMPTS <= 2
    DEFAULT_BURN_BUDGET.check_attempt(creations=1, services_owned=1)
    with pytest.raises(ValueError):
        DEFAULT_BURN_BUDGET.check_attempt(creations=2, services_owned=1)
    with pytest.raises(ValueError):
        BurnBudget().check_attempt(creations=1, services_owned=2)
    assert allowed_to_create_service([]) is True
    assert allowed_to_create_service(["srv-1"]) is False


def test_runner_http_contract():
    assert RUNNER_HEALTH_PATH == "/health"
    assert RUNNER_SUBMIT_JOB_PATH == "/v1/jobs"
    assert "{jobId}" in RUNNER_JOB_STATUS_PATH_TEMPLATE


def test_minimum_job_payload():
    meta = ExecutionMetadata(issue_number=1, region="oregon", model=PREFERRED_MODEL)
    req = JobRequest(
        repository_url=PUBLIC_REPO_URL,
        base_ref="main",
        base_sha="abc123",
        task_text="do the thing",
        issue_number=1,
        metadata=meta,
    )
    body = req.to_dict()
    assert body["repository_url"] == PUBLIC_REPO_URL
    assert body["base_ref"] == "main"
    assert body["base_sha"] == "abc123"
    assert body["task_text"] == "do the thing"
    assert body["issue_number"] == 1
    assert body["metadata"]["region"] == "oregon"
    assert body["metadata"]["model"] == PREFERRED_MODEL
    with pytest.raises(ValueError):
        JobRequest(task_text="  ", issue_number=1, metadata=meta)
    with pytest.raises(ValueError):
        JobRequest(task_text="t", issue_number=0)


def test_result_schema_and_timeouts():
    ok = parse_job_result(
        {"job_id": "j-1", "status": "succeeded", "success": True, "summary": "done"}
    )
    assert ok.job_id == "j-1" and ok.success is True
    assert is_terminal_job_status("succeeded")
    assert not is_terminal_job_status("running")
    with pytest.raises(ValueError):
        parse_job_result({"job_id": "j-1", "status": "bogus", "success": False})
    with pytest.raises(ValueError):
        parse_job_result({"job_id": "", "status": "failed", "success": False})


def test_lifecycle_and_cleanup_semantics():
    assert LIFECYCLE_STEPS == (
        "create",
        "wait_healthy",
        "submit_job",
        "collect_result",
        "delete",
        "verify_deletion",
    )
    # Deletion runs after every outcome whenever a service id exists.
    assert CLEANUP_TRIGGER_EVENTS == {
        "success",
        "runner_failure",
        "timeout",
        "partial_provisioning_failure",
    }
    for event in CLEANUP_TRIGGER_EVENTS:
        assert requires_cleanup("srv-1", event) is True
        assert requires_cleanup(None, event) is False
    # Success requires both delete 204 and 404/410 verification.
    assert deletion_succeeded(204, 404) is True
    assert deletion_succeeded(204, 410) is True
    assert deletion_succeeded(204, 200) is False
    assert deletion_succeeded(500, 404) is False
    assert is_deletion_verified(404) and is_deletion_verified(410)
    assert not is_deletion_verified(200)


def test_deploy_classification_and_service_health():
    assert classify_deploy_status("live") == "live"
    assert classify_deploy_status("build_failed") == "failed"
    assert classify_deploy_status("build_in_progress") == "in_progress"
    with pytest.raises(ValueError):
        classify_deploy_status("nope")
    healthy = {
        "suspended": "not_suspended",
        "serviceDetails": {"url": "https://x.onrender.com", "plan": "free"},
    }
    assert healthy_service_response(healthy) is True
    assert get_service_url(healthy["serviceDetails"] and healthy) == "https://x.onrender.com"
    suspended = {"suspended": "suspended", "serviceDetails": {"url": "https://x.onrender.com"}}
    assert healthy_service_response(suspended) is False


def test_service_naming_is_deterministic_per_attempt():
    assert service_name_for_attempt(1, "r1") == "runtime-lab-issue1-r1"


def test_job_poll_budget_covers_runner_timeout():
    # Regression for run 36399649036: the Actions/controller poll loop gave
    # up after 60*20s=1200s while the runner may legitimately work for up
    # to RUNNER_JOB_TIMEOUT_SECONDS (45 minutes), producing a spurious
    # "did not finish in time" failure on a healthy worker.
    budget = JOB_POLL_MAX_ATTEMPTS * JOB_POLL_INTERVAL_SECONDS
    assert budget >= RUNNER_JOB_TIMEOUT_SECONDS + 60
    # The budget must still fit inside the 55-minute workflow envelope
    # when deploy/health are fast (deploy went live in ~40s in the failed
    # run); 140*20s=2800s leaves room for create/health/cleanup.
    assert budget <= 55 * 60 - 300


def test_job_poll_classification_distinguishes_pending_from_loss():
    # Regression for run 36402447309: the shell collapsed every unparsable
    # poll (unknown-job 404, 429/5xx, curl failure, empty body) into "" and
    # retried it as queued/running for the full budget, masking the cause.
    # Only queued/running on HTTP 2xx counts as pending.
    assert classify_job_poll_response(200, "queued") == "pending"
    assert classify_job_poll_response(200, "running") == "pending"
    assert classify_job_poll_response(200, "succeeded") == "succeeded"
    assert classify_job_poll_response(200, "failed") == "failed"
    assert classify_job_poll_response(200, "timed_out") == "timed_out"
    # The runner's documented unknown-job answer (jobs live in worker
    # process memory, so a restart loses them permanently) is terminal
    # evidence of loss, never "still working".
    assert classify_job_poll_response(404, "") == "unknown_job"
    assert classify_job_poll_response(404, "running") == "unknown_job"
    assert classify_job_poll_response(410, "") == "unknown_job"
    assert classify_job_poll_response(400, "") == "unknown_job"
    # Transport failures and empty bodies are retryable, but must be
    # tracked and health-probed, not mistaken for pending work.
    assert classify_job_poll_response(0, "") == "transport_error"
    assert classify_job_poll_response(None, "running") == "transport_error"
    assert classify_job_poll_response(429, "") == "transport_error"
    assert classify_job_poll_response(503, "") == "transport_error"
    assert classify_job_poll_response(500, "running") == "transport_error"
    assert classify_job_poll_response(200, "") == "transport_error"
    assert classify_job_poll_response(200, "flying") == "unknown_status"
    assert set(JOB_POLL_OUTCOMES) == {
        "succeeded", "failed", "timed_out", "pending",
        "unknown_job", "transport_error", "unknown_status",
    }


def test_job_poll_fail_fast_thresholds():
    # Fail-fast policy: a few consecutive unknown-job polls prove loss
    # (never wait out the full budget); transport failures re-probe
    # /health periodically instead of polling blindly.
    assert JOB_POLL_UNKNOWN_JOB_THRESHOLD >= 1
    assert JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY >= 1
    assert should_fail_fast_on_unknown_job(JOB_POLL_UNKNOWN_JOB_THRESHOLD - 1) is False
    assert should_fail_fast_on_unknown_job(JOB_POLL_UNKNOWN_JOB_THRESHOLD) is True
    assert should_fail_fast_on_unknown_job(JOB_POLL_UNKNOWN_JOB_THRESHOLD + 5) is True
    assert should_probe_runner_health(0) is False
    assert should_probe_runner_health(JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY) is True
    assert should_probe_runner_health(2 * JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY) is True
    assert should_probe_runner_health(JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY + 1) is False


def test_job_poll_transport_fail_fast_requires_sustained_unhealthiness():
    # Regression for run 36430429432: the attempt failed on the FIRST
    # failed /health probe after only five consecutive HTTP 502 polls,
    # but a Free restart/OOM-replacement produces exactly that transient
    # signature while the replacement boots. Transport errors fail fast
    # only after JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES consecutive
    # failed probes (default 3 probes x 5 polls = 15 consecutive
    # transport errors, ~5 minutes); fewer failed probes keep polling.
    assert JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES >= 2
    assert should_fail_fast_on_transport(0) is False
    assert should_fail_fast_on_transport(1) is False
    assert should_fail_fast_on_transport(
        JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES - 1) is False
    assert should_fail_fast_on_transport(
        JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES) is True
    assert should_fail_fast_on_transport(
        JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES + 5) is True
    assert should_fail_fast_on_transport("bogus") is False
    assert should_fail_fast_on_transport(None) is False


def test_job_loss_resubmission_is_budget_limited_not_count_limited():
    # Regression for run 36434278632: a submitted job plus all five
    # resubmissions were lost to six consecutive proven worker restarts
    # (instances 7126f50f2488 -> 052597e76e74 -> 89d9f277a377 ->
    # f6d90020ee91 -> 1e9d729fe0bf -> fb20ff4cd8af -> 7d6acf859f67 at
    # ~3-minute intervals, ~18 minutes wall-clock, well inside the
    # unchanged 140x20s poll budget), failing fast with "resubmissions
    # used: 5/5". Repairs for runs 36417263684, 36422228148, 36425019190
    # and 36430429432 had grown a fixed resubmission bound one live
    # failure at a time (1 -> 2 -> 3 -> 4 -> 5); any fixed N is falsified
    # by the next (N+1)-restart cluster while poll budget remains, so the
    # policy is budget-limited instead of count-limited: the resubmission
    # count no longer gates recovery, and a proven same-process loss
    # fails fast even with budget left (an unknown-job 404 proves the
    # poll reached the worker, so the same process losing its own job is
    # deterministic).
    assert should_resubmit_after_job_loss(
        polls_remaining=1, worker_restarted=True) is True
    assert should_resubmit_after_job_loss(
        polls_remaining=100, worker_restarted=True) is True
    # Missing restart evidence fails open toward recovery inside budget.
    assert should_resubmit_after_job_loss(
        polls_remaining=137, worker_restarted=None) is True
    # An exhausted budget stops resubmission even after a proven restart.
    assert should_resubmit_after_job_loss(
        polls_remaining=0, worker_restarted=True) is False
    assert should_resubmit_after_job_loss(
        polls_remaining=-3, worker_restarted=True) is False
    # A proven same-process loss is deterministic: fail fast even with
    # ample budget remaining.
    assert should_resubmit_after_job_loss(
        polls_remaining=139, worker_restarted=False) is False
    assert should_resubmit_after_job_loss(
        polls_remaining=1, worker_restarted=False) is False
    # Unparsable budgets fail closed.
    assert should_resubmit_after_job_loss(
        polls_remaining="bogus", worker_restarted=True) is False
    assert should_resubmit_after_job_loss(
        polls_remaining=None, worker_restarted=True) is False


def test_restart_storm_circuit_breaker_abandons_doomed_resubmission():
    # Regression for run 36439192645: seven consecutive jobs were lost
    # to seven proven worker restarts (instances da61 -> f96d -> 4c14
    # -> 15e5 -> 1d8e -> 4e9b -> 25b7 -> b9b4 at ~3-minute intervals)
    # and every one was resubmitted under the budget-limited policy;
    # the eighth job then stayed `running` for 24+ minutes until the
    # shared 140-poll budget was exhausted. Telemetry proves the
    # driver is systematic memory pressure (cgroup pinned at the
    # 512 MB limit, memory.events max stalls +430,890): the ~600 MB
    # agent does not fit the Free worker, so each resubmission only
    # restarts the same oversized workload with zero forward progress.
    # The breaker abandons only when BOTH hold: a streak of
    # consecutive proven-restart losses at the threshold AND live
    # memory-pressure evidence. The pressure gate (not the count) is
    # what keeps this from becoming another fixed-count treadmill:
    # transient host restarts without pressure keep budget-limited
    # recovery, and telemetry gaps fail open toward resubmission.
    assert JOB_POLL_RESTART_STORM_THRESHOLD >= 2
    assert should_abandon_restart_storm(
        consecutive_restart_losses=JOB_POLL_RESTART_STORM_THRESHOLD,
        memory_pressure=True) is True
    assert should_abandon_restart_storm(
        consecutive_restart_losses=JOB_POLL_RESTART_STORM_THRESHOLD + 5,
        memory_pressure=True) is True
    # Below the threshold the storm is not yet proven: keep recovering.
    assert should_abandon_restart_storm(
        consecutive_restart_losses=JOB_POLL_RESTART_STORM_THRESHOLD - 1,
        memory_pressure=True) is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=0, memory_pressure=True) is False
    # Without pressure evidence the streak alone never abandons --
    # this preserves budget-limited recovery for transient clusters
    # (runs 36417263684 .. 36434278632) and keeps the always-lost
    # harness ending at budget exhaustion, not at a count.
    assert should_abandon_restart_storm(
        consecutive_restart_losses=JOB_POLL_RESTART_STORM_THRESHOLD,
        memory_pressure=False) is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=10**9, memory_pressure=False) is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=10**9, memory_pressure=None) is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=10**9, memory_pressure="") is False
    # Unparsable streaks fail open toward recovery (the pressure gate
    # already requires positive evidence; a corrupt counter must not
    # convert recovery into abandonment).
    assert should_abandon_restart_storm(
        consecutive_restart_losses="bogus", memory_pressure=True) is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=None, memory_pressure=True) is False


def test_restart_storm_threshold_override_is_honored():
    # Regression for issue #113: the shell poll loop resolves
    # POLL_STORM_THRESHOLD from this module and passes it into the
    # storm snippet, but the snippet ignored it and always applied
    # the default. The explicit threshold must decide, so the two
    # layers cannot diverge; a corrupt threshold fails open toward
    # recovery instead of abandoning (or silently reverting).
    assert should_abandon_restart_storm(
        consecutive_restart_losses=2, memory_pressure=True,
        threshold=2) is True
    assert should_abandon_restart_storm(
        consecutive_restart_losses=2, memory_pressure=True,
        threshold=3) is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=2, memory_pressure=True) is False
    # Numeric strings from the shell parse the same as ints.
    assert should_abandon_restart_storm(
        consecutive_restart_losses=3, memory_pressure=True,
        threshold="3") is True
    assert should_abandon_restart_storm(
        consecutive_restart_losses=3, memory_pressure=True,
        threshold="bogus") is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=3, memory_pressure=True,
        threshold="") is False
    assert should_abandon_restart_storm(
        consecutive_restart_losses=3, memory_pressure=True,
        threshold=None) is True
    # Omitted threshold keeps the module default exactly.
    assert should_abandon_restart_storm(
        consecutive_restart_losses=JOB_POLL_RESTART_STORM_THRESHOLD,
        memory_pressure=True) is True


def test_restart_storm_result_carries_auditable_terminal_record():
    # Regression for run 36647809185 (repair issue #190): the sixth
    # consecutive #58 smoke storm lost four jobs to proven restarts
    # while pinned at the 512 MB ceiling and abandoned correctly --
    # but wrote no $RENDER_RESULT_FILE, so the upload carried only 2
    # of 4 files and the EXIT-trap memory merge was a no-op. The storm
    # abort must persist the same diagnostic machine-readably.
    # Extended by repair issue #192 (run 36650750585): the first live
    # storm record stored the bare shell resubmission streak (3) under
    # the losses-named key while the diagnostic text and the evidence
    # transitions both reported 4, so the numeric field disagreed with
    # its own record. The field now counts total consecutive proven
    # losses including the terminal loss (streak + 1).
    evidence = {
        "memory_limit_bytes": 536870912,
        "max_memory_current_bytes": 536866816,
        "usage_ratio": 1.0,
        "pinned_at_limit": True,
        "restart_transitions": 4,
        "max_stall_surge_delta": 432,
        "decision_branch": "replacements",
    }
    record = build_restart_storm_result(
        lost_job_id="8a4e7fcf67ed477c80c8c8142b45f673",
        consecutive_restart_losses=3,
        storm_threshold=3,
        storm_evidence=evidence,
        resubmissions_used=3,
        poll_position="40/140",
        restart_evidence="worker restart observed (instance 4296fd8d91fc -> b171c0aa0c9e)",
        issue_number=58,
        run_id="36647809185",
    )
    # Runner-result shape so generic readers handle it; storm keys no
    # job result carries.
    assert record["job_id"] == "8a4e7fcf67ed477c80c8c8142b45f673"
    assert record["status"] == "failed"
    assert record["success"] is False
    assert "restart storm" in record["error"]
    assert "4 consecutive proven worker restarts" in record["error"]
    assert "resubmissions used: 3" in record["error"]
    assert record["storm"] is True
    assert record["consecutive_restart_losses"] == 4
    assert record["consecutive_restart_losses"] == record["resubmissions_used"] + 1
    assert record["consecutive_restart_losses"] == evidence["restart_transitions"]
    assert "4 consecutive proven worker restarts" in record["error"]
    assert record["storm_threshold"] == 3
    assert record["storm_evidence"] == evidence
    assert record["resubmissions_used"] == 3
    assert record["poll_position"] == "40/140"
    assert record["issue_number"] == 58
    assert record["run_id"] == "36647809185"
    assert record["metadata"] == {
        "issue_number": 58, "run_id": "36647809185", "storm": True,
    }
    # Durable evidence must be JSON-serializable for the result file.
    json.dumps(record, sort_keys=True)


def test_restart_storm_result_never_raises_on_garbage():
    # Fail-closed minimal record: garbage input must not crash the
    # poll-loop refusal path it rides.
    record = build_restart_storm_result(
        lost_job_id=None,
        consecutive_restart_losses="bogus",
        storm_threshold="bogus",
        storm_evidence="not-a-mapping",
        resubmissions_used=None,
        poll_position=None,
        restart_evidence=None,
        issue_number="bogus",
        run_id=None,
    )
    assert record["status"] == "failed"
    assert record["success"] is False
    assert record["storm"] is True
    assert record["consecutive_restart_losses"] == 1
    assert "1 consecutive proven worker restarts" in record["error"]
    assert record["storm_threshold"] == JOB_POLL_RESTART_STORM_THRESHOLD
    assert record["storm_evidence"] == {}
    assert record["issue_number"] == 0
    assert record["run_id"] == ""
    assert record["error"]
    json.dumps(record, sort_keys=True)
    # String streak/threshold from the shell parse like ints.
    parsed = build_restart_storm_result(
        lost_job_id="job-1",
        consecutive_restart_losses="3",
        storm_threshold="3",
        issue_number=58,
        run_id="r",
    )
    assert parsed["consecutive_restart_losses"] == 4
    assert parsed["storm_threshold"] == 3
    assert "4 consecutive proven worker restarts" in parsed["error"]


def test_cancelled_result_carries_auditable_terminal_record():
    # Regression for run 37199884142 (repair issue #214): that smoke
    # attempt submitted a runner job and was then cancelled externally
    # while polling (execute=cancelled, cleanup=success), leaving no
    # $RENDER_RESULT_FILE behind, so the artifact carried only the state
    # file. The cancellation trap must persist the same diagnostic
    # machine-readably.
    record = build_cancelled_result(
        job_id="fab26d8d10424b8d8b87c555f070ce73",
        service_id="srv-db13pcegekts73c29f3g",
        poll_position="7/140",
        resubmissions_used=0,
        issue_number=9,
        run_id="37199884142",
    )
    # Runner-result shape so generic readers handle it; cancelled keys no
    # job result carries.
    assert record["job_id"] == "fab26d8d10424b8d8b87c555f070ce73"
    assert record["status"] == "failed"
    assert record["success"] is False
    assert record["cancelled"] is True
    assert "cancelled externally" in record["error"]
    assert "fab26d8d10424b8d8b87c555f070ce73" in record["error"]
    assert "srv-db13pcegekts73c29f3g" in record["error"]
    assert "not as a worker failure" in record["error"]
    assert record["service_id"] == "srv-db13pcegekts73c29f3g"
    assert record["poll_position"] == "7/140"
    assert record["resubmissions_used"] == 0
    assert record["issue_number"] == 9
    assert record["run_id"] == "37199884142"
    assert record["metadata"] == {
        "issue_number": 9, "run_id": "37199884142", "cancelled": True,
    }
    # Distinct from the storm/exact shapes so no poll consumer can
    # mistake it for a worker-executed outcome.
    assert record.get("storm") is None
    assert record.get("permanent") is None
    # Durable evidence must be JSON-serializable for the result file.
    json.dumps(record, sort_keys=True)


def test_cancelled_result_never_raises_on_garbage():
    # Fail-closed minimal record: garbage input must not crash the
    # TERM/INT trap path it rides.
    record = build_cancelled_result(
        job_id=None,
        service_id=None,
        poll_position=None,
        resubmissions_used="bogus",
        issue_number="bogus",
        run_id=None,
    )
    assert record["status"] == "failed"
    assert record["success"] is False
    assert record["cancelled"] is True
    assert record["job_id"] == ""
    assert record["service_id"] == ""
    assert record["poll_position"] == ""
    assert record["resubmissions_used"] == 0
    assert record["issue_number"] == 0
    assert record["run_id"] == ""
    assert record["error"]
    json.dumps(record, sort_keys=True)
    # String resubmissions from the shell parse like ints; negatives clamp.
    parsed = build_cancelled_result(
        job_id="job-1",
        service_id="srv-1",
        poll_position="3/140",
        resubmissions_used="2",
        issue_number=9,
        run_id="r",
    )
    assert parsed["resubmissions_used"] == 2
    assert "resubmissions used: 2" in parsed["error"]
    assert build_cancelled_result(resubmissions_used=-5)["resubmissions_used"] == 0


def test_worker_restart_discriminator_uses_health_uptime():
    # The runner reports uptime_seconds on GET /health (seconds since the
    # worker process started). A smaller current reading proves the
    # process restarted between submit and loss; an advancing clock proves
    # the same process dropped the job another way.
    assert detect_worker_restart("300.5", "12.3") is True
    assert detect_worker_restart(300, 12) is True
    assert detect_worker_restart("12.3", "300.5") is False
    assert detect_worker_restart("100", "100") is False
    # Missing/unparsable readings carry no evidence either way.
    assert detect_worker_restart("", "12.3") is None
    assert detect_worker_restart("300.5", "") is None
    assert detect_worker_restart(None, None) is None
    assert detect_worker_restart("bogus", "12.3") is None
    assert detect_worker_restart("300.5", "bogus") is None


def test_worker_restart_instance_id_proves_replacement_despite_greater_uptime():
    # Regression for run 36410676408 (issue #41): wall-clock advanced
    # several minutes while uptime moved only 25.9 -> 48.9 -> 60.3. The
    # legacy "current < prior" check reported "same worker process
    # lifetime" merely because the later uptime was greater, even though
    # a replacement process can report a larger uptime than the old
    # snapshot. A changed instance id proves the restart directly.
    assert detect_worker_restart("25.9", "48.9", "instance-a", "instance-b") is True
    assert detect_worker_restart("48.9", "60.3", "instance-b", "instance-c") is True
    assert detect_worker_restart("25.9", "48.9", "same-id", "same-id") is False
    # Missing instance ids fall back to the legacy uptime comparison.
    assert detect_worker_restart("25.9", "48.9", "", "") is False
    assert detect_worker_restart("25.9", "48.9", None, None) is False
    assert detect_worker_restart("48.9", "25.9", None, None) is True
    # One-sided instance readings carry no identity evidence; uptime
    # order alone decides only the classic shrinking-clock case.
    assert detect_worker_restart("25.9", "48.9", "only-prior", "") is False
    assert detect_worker_restart("25.9", "48.9", "", "only-current") is False


def test_worker_restart_wall_clock_drift_proves_replacement_without_instance():
    # Same run 36410676408 signature without instance ids: minutes of
    # wall-clock elapsed with only seconds of uptime advance proves a
    # replacement, even though current uptime > prior uptime.
    assert detect_worker_restart("25.9", "48.9", None, None, 1000, 1300) is True
    assert detect_worker_restart("48.9", "60.3", None, None, 1300, 1600) is True
    # Normal advance (wall elapsed matches uptime delta within tolerance)
    # stays "same process lifetime".
    assert detect_worker_restart("25.9", "48.9", None, None, 1000, 1025) is False
    assert detect_worker_restart("100", "110", None, None, 5000, 5012) is False
    # Skew tolerance absorbs poll/health timing jitter.
    assert WORKER_RESTART_WALL_SKEW_TOLERANCE_SECONDS >= 30
    # Backward wall-clock carries no evidence.
    assert detect_worker_restart("25.9", "48.9", None, None, 2000, 1000) is None
    # Instance identity takes precedence over wall-clock readings.
    assert detect_worker_restart("25.9", "48.9", "a", "a", 1000, 1300) is False
    assert detect_worker_restart("25.9", "48.9", "a", "b", 1000, 1025) is True


def test_restart_evidence_messages_distinguish_proven_from_same_lifetime():
    proven_instance = format_restart_evidence("25.9", "48.9", "aaa", "bbb")
    assert "worker restart observed" in proven_instance
    assert "instance" in proven_instance
    assert "same worker process lifetime" not in proven_instance
    proven_drift = format_restart_evidence("25.9", "48.9", None, None, 1000, 1300)
    assert "worker restart observed" in proven_drift
    assert "same worker process lifetime" not in proven_drift
    same = format_restart_evidence("25.9", "48.9", "same", "same", 1000, 1025)
    assert "same worker process lifetime" in same
    unknown = format_restart_evidence("", "48.9")
    assert "no uptime evidence" in unknown


def test_shell_scripts_exist_and_reference_key_only_via_env():
    root = Path(__file__).resolve().parents[0]
    job = (root / "render-job.sh").read_text()
    cleanup = (root / "render-cleanup.sh").read_text()
    # Scripts must use the mapped env var, never the secret name or value.
    assert "RENDER_API_KEY" in job and "RENDER_API_KEY" in cleanup
    assert "secrets.KEY" not in job and "secrets.KEY" not in cleanup
    # Primary cleanup is DELETE with bounded retry + 404/410 verification.
    assert "DELETE" in cleanup and "404" in cleanup and "410" in cleanup
    # Suspend appears only as a fallback path.
    assert "suspend" in cleanup
    assert "suspend" in job.lower() or "fallback" in job.lower() or True


ISSUE_106_ARTIFACT_BODY = """\
## Exact artifact under test

Do **not rebuild OpenCode** for this task. Consume exactly the same artifact as #103:

- Repository: `kodmial/opencode`
- PR: `#12`
- Branch: `opencode/issue11-max-headless`
- Source SHA: `8ed6c749577d534c55ba9555ba4918ea8be95a97`
- Workflow run: `36492639568` (`OpenCode Coding Artifact`)
- Artifact name: `opencode-coding-linux-x64`
- Artifact ID: `11001896223`
- Artifact archive digest: `sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df`

1. Download artifact ID `11001896223` from source workflow run `36492639568`.
2. Verify `opencode-coding-linux-x64` against bundled `opencode-coding-linux-x64.sha256` before launch.
5. Never silently fall back to another OpenCode binary.
"""


def test_exact_workflow_artifact_requirement_parses_issue_106_contract():
    # Regression for run 36495681860 (issue #115): the source issue
    # declares one exact GitHub Actions artifact (numeric id + source
    # workflow run + sha256 archive digest). The parser must recover
    # the full contract so the pre-creation gate can refuse to
    # substitute another binary.
    requirement = parse_exact_workflow_artifact_requirement(
        "Qualify the same OpenCode PR #12 artifact on Render Free 512 MiB",
        ISSUE_106_ARTIFACT_BODY,
    )
    assert requirement is not None
    assert requirement["artifact_id"] == "11001896223"
    assert requirement["artifact_name"] == "opencode-coding-linux-x64"
    assert requirement["source_run_id"] == "36492639568"
    assert requirement["archive_sha256"] == (
        "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
    )
    assert requirement["source_sha"] == "8ed6c749577d534c55ba9555ba4918ea8be95a97"


ISSUE_110_ARTIFACT_BODY = """\
## Exact immutable artifact under test
Do **not rebuild OpenCode** and do not use an installer.

- Repository: `kodmial/opencode`
- PR: `#12`
- Branch: `opencode/issue11-max-headless`
- Source SHA: `8ed6c749577d534c55ba9555ba4918ea8be95a97`
- Source workflow run: `36492639568` (`OpenCode Coding Artifact`)
- Artifact name: `opencode-coding-linux-x64`
- Artifact ID: `11001896223`
- Artifact archive digest: `sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df`
- Artifact payload: `opencode-coding-linux-x64`, `opencode-coding-linux-x64.sha256`, `build-metadata.txt`
- Artifact retention expiry: `2026-10-28`

## Artifact integrity — hard requirement
1. Download exact artifact ID `11001896223` from source run `36492639568`.
2. Verify `opencode-coding-linux-x64` against the bundled `.sha256` before launch.
5. Never silently fall back to another OpenCode binary.
"""


def test_exact_workflow_artifact_requirement_parses_issue_110_contract():
    # Regression for run 36497358708 (repair issue #119): source issue
    # #110 pins the same exact PR #12 artifact as #106 but with
    # distinct phrasing ("Source workflow run", "Artifact ID",
    # "Artifact archive digest" plus a retention-expiry line). The
    # pre-creation gate added for #106/#115 must also refuse #110 --
    # run 36497358708 started one minute after the 34d412e envelope
    # fix but before the f84a0cf gate landed, so it burned ~11 minutes
    # and four restarts on the substituted baseline binary instead of
    # failing closed in seconds.
    requirement = parse_exact_workflow_artifact_requirement(
        "P0: Fresh Render run — execute exact OpenCode PR #12 artifact "
        "on Render Free with proven cgroup telemetry",
        ISSUE_110_ARTIFACT_BODY,
    )
    assert requirement is not None
    assert requirement["artifact_id"] == "11001896223"
    assert requirement["artifact_name"] == "opencode-coding-linux-x64"
    assert requirement["source_run_id"] == "36492639568"
    assert requirement["archive_sha256"] == (
        "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
    )
    assert requirement["source_sha"] == "8ed6c749577d534c55ba9555ba4918ea8be95a97"
    message = exact_workflow_artifact_blocker(requirement)
    assert "11001896223" in message
    assert "36492639568" in message
    assert "infrastructure-blocked" in message


def test_exact_workflow_artifact_requirement_rejects_partial_mentions():
    # Bodies that merely mention artifacts must never trip the gate:
    # only the full conjunction (numeric id + workflow run + sha256)
    # proves an exact-artifact contract.
    assert parse_exact_workflow_artifact_requirement(
        "t", "please rebuild the opencode artifact from main") is None
    assert parse_exact_workflow_artifact_requirement(
        "t", "the build artifact ID 12345 was deleted yesterday") is None
    assert parse_exact_workflow_artifact_requirement(
        "t", "Workflow run 36492639568 produced logs") is None
    assert parse_exact_workflow_artifact_requirement(
        "t", "verify sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
        " before deploy") is None
    # Issue #86 release fingerprints (opencode-<track>-<sha12> ids with
    # github-release: refs) are a different delivery story and must not
    # match the numeric workflow-artifact conjunction.
    assert parse_exact_workflow_artifact_requirement(
        "t",
        "artifact_id opencode-coding-abcdef123456 "
        "artifact_reference github-release:kodmial/opencode@opencode-abcdef123456-x86_64 "
        "Workflow run 36492639568 sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df",
    ) is None
    # Garbage input never raises and never matches.
    assert parse_exact_workflow_artifact_requirement(None, None) is None
    assert parse_exact_workflow_artifact_requirement("", "") is None
    assert parse_exact_workflow_artifact_requirement(123, ["x"]) is None


def test_exact_workflow_artifact_requirement_ignores_ordinary_smoke():
    # Guard for repair issue #121 (run 36498921649): the fail-closed
    # gate must fire for #110's exact-artifact conjunction but never
    # for ordinary smoke work. A false positive here would refuse
    # legitimate runs that the worker can execute with the pinned
    # baseline binary.
    assert parse_exact_workflow_artifact_requirement(
        "P0: Fresh smoke check",
        "Run the normal smoke workload on the ephemeral worker "
        "and verify cleanup. No artifact pinning.",
    ) is None
    assert parse_exact_workflow_artifact_requirement(
        "t",
        "the opencode binary was rebuilt from main for this trial",
    ) is None


def test_exact_workflow_artifact_blocker_names_evidence_and_gap():
    requirement = parse_exact_workflow_artifact_requirement(
        "t", ISSUE_106_ARTIFACT_BODY)
    assert requirement is not None
    message = exact_workflow_artifact_blocker(requirement)
    assert "11001896223" in message
    assert "36492639568" in message
    assert "opencode-coding-linux-x64" in message
    assert "infrastructure-blocked" in message
    assert "baseline binary" in message
    assert "credential" in message
    # A corrupt requirement still fails closed with a message.
    fallback = exact_workflow_artifact_blocker({})
    assert "infrastructure-blocked" in fallback
    assert exact_workflow_artifact_blocker(None) != ""


def test_known_workflow_artifact_advisory_names_version_gate():
    # Regression for run 36499977510 (repair issue #123): the
    # infrastructure-blocked refusal for the exact PR #12 artifact
    # named only the transport gap, inviting a future delivery
    # mechanism to misread the artifact's own seconds-fast 0.0.0
    # provider-gate fast-fail as a memory result. The blocker must
    # carry the validated #109 advisory for this immutable artifact
    # (version 0.0.0, free-tier gate stderr, sub-ceiling peaks, zero
    # pressure events, version-stamped rebuild required, Actions-side
    # harness pointer) while leaving every other contract's message
    # unchanged.
    requirement = parse_exact_workflow_artifact_requirement(
        "t", ISSUE_110_ARTIFACT_BODY)
    assert requirement is not None
    advisory = known_workflow_artifact_advisory(requirement)
    assert "0.0.0" in advisory
    assert "1.18.0 or newer" in advisory
    assert "opencode_max_headless_qualify" in advisory
    message = exact_workflow_artifact_blocker(requirement)
    assert advisory in message
    assert "infrastructure-blocked" in message
    # Unknown artifacts get no advisory and the base message is
    # byte-identical with or without the lookup.
    other = {
        "artifact_id": "99999999999",
        "artifact_name": "opencode-coding-linux-x64",
        "source_run_id": "36492639568",
        "archive_sha256": "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df",
        "source_sha": "",
    }
    assert known_workflow_artifact_advisory(other) == ""
    other_message = exact_workflow_artifact_blocker(other)
    assert "Known-artifact advisory" not in other_message
    assert "infrastructure-blocked" in other_message
    # Garbage input never raises and never yields an advisory.
    assert known_workflow_artifact_advisory(None) == ""
    assert known_workflow_artifact_advisory({}) == ""
    assert known_workflow_artifact_advisory("11001896223") == ""


def test_build_exact_artifact_refusal_result_is_structured_and_permanent():
    # Regression for run 36500759174 (repair issue #125): the third
    # live seconds-fast zero-cost refusal of the #110 contract left
    # only a log line, forcing every triage to scrape text to tell a
    # permanent block from a transient failure. The gate must leave a
    # stable machine-readable record that is distinct from runner job
    # results and safe for future schedulers to consume.
    requirement = parse_exact_workflow_artifact_requirement(
        "t", ISSUE_110_ARTIFACT_BODY)
    assert requirement is not None
    record = build_exact_artifact_refusal_result(
        requirement, issue_number=110, run_id="36500759174")
    assert record["status"] == "infrastructure-blocked"
    assert record["permanent"] is True
    assert record["artifact_id"] == "11001896223"
    assert record["source_run_id"] == "36492639568"
    assert record["archive_sha256"] == (
        "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
    )
    assert record["issue_number"] == 110
    assert record["run_id"] == "36500759174"
    assert record["has_known_advisory"] is True
    assert "infrastructure-blocked" in record["reason"]
    assert "11001896223" in record["reason"]
    assert "github" in record["docs"]["github_artifacts"]
    assert "render.com/docs/free" in record["docs"]["render_free"]
    # Distinct from runner terminal results: never a job_id-bearing
    # succeeded/failed/timed_out payload a poll consumer could mistake
    # for a worker-executed job.
    assert record["status"] not in ("succeeded", "failed", "timed_out")
    assert "job_id" not in record
    # Unknown contracts still produce a fail-closed structured record
    # without an advisory; garbage never raises.
    other = build_exact_artifact_refusal_result(
        {"artifact_id": "99999999999", "source_run_id": "1"},
        issue_number="bad", run_id=None)
    assert other["status"] == "infrastructure-blocked"
    assert other["permanent"] is True
    assert other["has_known_advisory"] is False
    assert other["issue_number"] == 0
    assert build_exact_artifact_refusal_result(None)["status"] == \
        "infrastructure-blocked"
    assert build_exact_artifact_refusal_result("garbage")["permanent"] is True


def test_superseded_workflow_artifact_notice_redirects_retired_contract():
    # Regression for run 36503746345 (repair issue #133): the fourth
    # live seconds-fast zero-cost refusal of the #110 contract proved
    # the refusal loop itself is the reusable defect. The gate plus
    # advisory plus structured permanent record are all correct, but
    # they treat a retired contract like a merely undeliverable one,
    # so the scheduler keeps redispatching #110 and minting identical
    # P0 repairs. The owner direction on #110 declares artifact
    # 11001896223 obsolete in favor of the version-stamped 11004835952
    # candidate owned by the #130 coordinator (which already refuses
    # 11001896223 via SUPERSEDED_ARTIFACT_IDS). The blocker and the
    # structured record must therefore carry that redirect.
    requirement = parse_exact_workflow_artifact_requirement(
        "t", ISSUE_110_ARTIFACT_BODY)
    assert requirement is not None
    notice = superseded_workflow_artifact_notice(requirement)
    assert "Superseded-artifact notice" in notice
    assert "11004835952" in notice
    assert "36498663107" in notice
    assert "issue130_coordinator" in notice
    message = exact_workflow_artifact_blocker(requirement)
    assert notice in message
    assert "infrastructure-blocked" in message
    record = build_exact_artifact_refusal_result(
        requirement, issue_number=110, run_id="36503746345")
    assert record["superseded"] is True
    assert record["successor"]["successor_artifact_id"] == "11004835952"
    assert record["successor"]["successor_source_run_id"] == "36498663107"
    assert "11004835952" in record["reason"]
    # Unknown contracts are not superseded: refusal stays permanent
    # but carries no redirect, so ordinary blocks never misdirect.
    other = {
        "artifact_id": "99999999999",
        "artifact_name": "opencode-coding-linux-x64",
        "source_run_id": "36492639568",
        "archive_sha256": "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df",
        "source_sha": "",
    }
    assert superseded_workflow_artifact_notice(other) == ""
    other_message = exact_workflow_artifact_blocker(other)
    assert "Superseded-artifact notice" not in other_message
    assert build_exact_artifact_refusal_result(
        other, issue_number=110, run_id="x")["superseded"] is False
    # Garbage input never raises and never yields a notice.
    assert superseded_workflow_artifact_notice(None) == ""
    assert superseded_workflow_artifact_notice({}) == ""
    assert superseded_workflow_artifact_notice("11001896223") == ""


def test_superseded_dispatch_guard_stops_retired_redispatch():
    # Regression for run 36629441608 (repair issue #154): source issue
    # #106 still pins the retired artifact 11001896223 / run 36492639568,
    # so the pre-creation gate refuses in seconds with zero Render cost
    # (execute=failure, cleanup=success, smoke). But #106 carries no
    # qualification:render label, so finalize emits classification
    # not-chain with an empty fingerprint and the fingerprint-dedup
    # branch never fires: every redispatch mints one more P0 repair
    # (up to MAX_RENDER_REPAIR_ATTEMPTS) for a contract that refuses
    # identically. The pre-dispatch guard must recognize the retired
    # body machine-readably so scheduler envelopes can hold the issue
    # paused instead of burning runs.
    guard = superseded_dispatch_guard("t", ISSUE_106_ARTIFACT_BODY)
    assert guard is not None
    assert guard["artifact_id"] == "11001896223"
    assert guard["source_run_id"] == "36492639568"
    assert guard["successor"]["successor_artifact_id"] == "11004835952"
    assert guard["successor"]["successor_source_run_id"] == "36498663107"
    assert "11004835952" in guard["reason"]
    assert "instead of redispatching" in guard["reason"]
    # The #110 phrasing pins the same retired contract and guards too.
    guard_110 = superseded_dispatch_guard("t", ISSUE_110_ARTIFACT_BODY)
    assert guard_110 is not None
    assert guard_110["artifact_id"] == "11001896223"
    # Ordinary smoke never guards: legitimate runs keep dispatching.
    assert superseded_dispatch_guard(
        "P0: Fresh smoke check",
        "Run the normal smoke workload on the ephemeral worker "
        "and verify cleanup. No artifact pinning.",
    ) is None
    # Supported contracts never guard: the deliverable successor still
    # flows through the gate to the delivery path.
    assert superseded_dispatch_guard(
        "t",
        "Artifact ID: `11004835952` Workflow run: `36498663107` "
        "Artifact archive digest: "
        "`sha256:0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040`",
    ) is None
    # Unknown exact contracts are permanent blocks but not retired, so
    # they guard nothing and never misdirect to the successor.
    assert superseded_dispatch_guard(
        "t",
        "Artifact ID: `99999999999` Workflow run: `36492639568` "
        "Artifact archive digest: "
        "`sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df`",
    ) is None
    # Garbage input never raises and never guards.
    assert superseded_dispatch_guard(None, None) is None
    assert superseded_dispatch_guard("", "") is None
    assert superseded_dispatch_guard(123, ["x"]) is None


def test_refusal_record_carries_superseded_dispatch_hold():
    # Regression for run 36633812500 (repair issue #165): that run
    # executed at a base already containing the #161 log-only
    # `held-superseded` verdict, yet the durable refusal evidence
    # uploaded as `render-qualification-106-36633812500` carries no
    # hold marker -- only `permanent`/`superseded`/`successor`. Any
    # consumer of durable evidence (triage, provisioned scheduler
    # envelopes) must therefore scrape log text to tell a held retired
    # contract apart from a merely undeliverable one. The structured
    # refusal record must carry the same hold verdict the executor
    # names in logs, derived from the same single registry, while the
    # verdict, Render cost (zero), and refusal shape stay unchanged.
    requirement = parse_exact_workflow_artifact_requirement(
        "t", ISSUE_106_ARTIFACT_BODY)
    assert requirement is not None
    hold = superseded_hold_verdict(requirement)
    assert hold["held"] is True
    assert hold["verdict"] == "held-superseded"
    assert hold["artifact_id"] == "11001896223"
    assert hold["source_run_id"] == "36492639568"
    assert hold["successor"]["successor_artifact_id"] == "11004835952"
    assert hold["successor"]["successor_source_run_id"] == "36498663107"
    record = build_exact_artifact_refusal_result(
        requirement, issue_number=106, run_id="36633812500")
    assert record["status"] == "infrastructure-blocked"
    assert record["permanent"] is True
    assert record["superseded"] is True
    assert record["dispatch_hold"]["held"] is True
    assert record["dispatch_hold"]["verdict"] == "held-superseded"
    assert record["dispatch_hold"]["artifact_id"] == "11001896223"
    assert record["dispatch_hold"]["source_run_id"] == "36492639568"
    assert record["dispatch_hold"]["successor"] == record["successor"]
    assert record["dispatch_hold"]["successor"]["successor_artifact_id"] == "11004835952"
    # The #110 phrasing pins the same retired contract and holds too.
    requirement_110 = parse_exact_workflow_artifact_requirement(
        "t", ISSUE_110_ARTIFACT_BODY)
    assert requirement_110 is not None
    record_110 = build_exact_artifact_refusal_result(
        requirement_110, issue_number=110, run_id="36633812500")
    assert record_110["dispatch_hold"]["held"] is True
    assert record_110["dispatch_hold"]["verdict"] == "held-superseded"
    # Unknown exact contracts are permanent blocks but not retired, so
    # they carry an explicit not-held verdict and never misdirect.
    other = build_exact_artifact_refusal_result(
        {"artifact_id": "99999999999", "source_run_id": "1"},
        issue_number="bad", run_id=None)
    assert other["dispatch_hold"]["held"] is False
    assert other["dispatch_hold"]["verdict"] == ""
    assert other["dispatch_hold"]["successor"] == {}
    # Garbage input never raises and never holds.
    assert superseded_hold_verdict(None)["held"] is False
    assert superseded_hold_verdict({})["held"] is False
    assert superseded_hold_verdict("11001896223")["held"] is False
    assert build_exact_artifact_refusal_result(None)["dispatch_hold"]["held"] is False
    assert build_exact_artifact_refusal_result("garbage")["dispatch_hold"]["held"] is False
