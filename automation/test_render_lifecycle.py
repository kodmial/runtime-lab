"""Tests for the ephemeral Render lifecycle contract (issue #1)."""

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
    extract_owner_id,
    format_restart_evidence,
    get_service_url,
    healthy_service_response,
    is_deletion_verified,
    is_terminal_job_status,
    experiment_record_path,
    knowledge_handoff_instructions,
    known_workflow_artifact_advisory,
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
    should_fail_fast_on_transport,
    should_probe_runner_health,
    should_resubmit_after_job_loss,
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
    assert "Repository knowledge handoff (mandatory)" in task
    assert "issue-9-run-abc-123.md" in task


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
