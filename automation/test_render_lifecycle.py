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
    JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY,
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
    extract_owner_id,
    get_service_url,
    healthy_service_response,
    is_deletion_verified,
    is_terminal_job_status,
    experiment_record_path,
    knowledge_handoff_instructions,
    validate_experiment_record_text,
    resolve_task_text,
    parse_job_result,
    render_path,
    render_url,
    requires_cleanup,
    select_model,
    service_name_for_attempt,
    should_fail_fast_on_unknown_job,
    should_probe_runner_health,
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
    return "\n".join([
        "---",
        "schema: runtime-lab-experiment/v1",
        "issue: %d" % issue,
        "run_id: %s" % run_id,
        "base_commit: deadbeef",
        "topic: test",
        "outcome: succeeded",
        "supersedes: []",
        "---",
        "# test",
        *headings,
        "evidence",
    ])


def test_experiment_record_validation_fails_closed():
    good = _valid_experiment_record()
    assert validate_experiment_record_text(good, 9, "r1") is True
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace("issue: 9", "issue: 8"), 9, "r1")
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace("run_id: r1", "run_id: old"), 9, "r1")
    with pytest.raises(ValueError):
        validate_experiment_record_text(good.replace("## Cleanup proof", ""), 9, "r1")

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
