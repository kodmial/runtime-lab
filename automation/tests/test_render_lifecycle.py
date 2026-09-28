"""Tests for the issue #1 Render execution lifecycle and runner contract."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import render_lifecycle as r
import render_runner


def make_metadata(**overrides):
    args = {
        "issue_number": 1,
        "run_id": "run-123",
        "attempt": 1,
        "mode": "smoke",
        "region": "oregon",
        "model": r.PREFERRED_MODEL,
        "repository": r.PUBLIC_REPO_URL,
    }
    args.update(overrides)
    return r.ExecutionMetadata(**args)


def test_doc_urls_cover_required_operations():
    assert r.DOC_CREATE_SERVICE.endswith("/create-service")
    assert r.DOC_RETRIEVE_SERVICE.endswith("/retrieve-service")
    assert r.DOC_DELETE_SERVICE.endswith("/delete-service")
    assert "suspend-service" in r.DOC_SUSPEND_SERVICE
    assert r.DOC_RATE_LIMITING.endswith("/rate-limiting")
    for op in r.REQUIRED_LIFECYCLE_OPS:
        assert op.startswith(("POST ", "GET ", "DELETE "))
    assert set(r.REQUIRED_LIFECYCLE_OPS) == {
        r.OP_CREATE_SERVICE,
        r.OP_RETRIEVE_SERVICE,
        r.OP_LIST_SERVICES,
        r.OP_SUSPEND_SERVICE,
        r.OP_DELETE_SERVICE,
    }
    assert r.service_path("srv-1") == "/v1/services/srv-1"
    assert r.suspend_path("srv-1") == "/v1/services/srv-1/suspend"
    assert r.service_url(r.RENDER_API_BASE, "/v1/services") == (
        "https://api.render.com/v1/v1/services"
    )


def test_rate_limit_policy_matches_docs():
    # Official docs: POST /v1/services limited to 20/hour.
    assert r.RATE_LIMIT_CREATE_PER_HOUR == 20
    assert r.RATE_LIMIT_MUTATE_PER_MINUTE_PER_SERVICE == 10
    for header in (
        r.RATE_LIMIT_HEADER_LIMIT,
        r.RATE_LIMIT_HEADER_REMAINING,
        r.RATE_LIMIT_HEADER_RESET,
        r.RATE_LIMIT_HEADER_RETRY_AFTER,
    ):
        assert isinstance(header, str) and header
    assert r.HTTP_TOO_MANY_REQUESTS == 429


def test_low_burn_policy_encoded():
    assert r.MAX_CONCURRENT_AUTOMATION_JOBS == 4
    assert r.MAX_SERVICES_PER_ISSUE_ATTEMPT == 1
    assert r.MAX_SERVICE_CREATES_PER_ATTEMPT == 1
    assert r.ALLOW_CRON_CREATED_SERVICES is False
    assert r.RETRY_REUSES_SAME_SERVICE is True
    # Retries are bounded everywhere.
    assert r.MAX_DEPLOY_POLLS > 0 and r.MAX_JOB_POLLS > 0
    assert r.MAX_DELETE_ATTEMPTS >= 1 and r.MAX_SUSPEND_ATTEMPTS == 1


def test_free_tier_guard_explicit():
    assert r.FREE_PLAN == "free"
    assert r.is_free_plan("free")
    assert not r.is_free_plan("starter")
    assert not r.is_free_plan("0.5c-512mb")
    assert not r.is_free_plan("")
    assert not r.is_free_plan(None)
    with pytest.raises(r.FreeTierViolation):
        r.assert_free_plan("starter")
    with pytest.raises(r.FreeTierViolation):
        r.assert_free_plan("0.5c-512mb")
    assert r.assert_free_plan("free") == "free"


def test_create_payload_pins_free_public_no_autodeploy():
    payload = r.build_create_payload(
        issue_number=1, run_id="run-9", owner_id="owner-1", region="oregon"
    )
    assert payload["type"] == "web_service"
    assert payload["repo"] == "https://github.com/kodmial/runtime-lab"
    assert payload["autoDeploy"] == "no"
    assert payload["serviceDetails"]["plan"] == "free"
    assert payload["serviceDetails"]["region"] == "oregon"
    assert payload["serviceDetails"]["numInstances"] == 1
    assert payload["serviceDetails"]["healthCheckPath"] == "/health"
    r.assert_create_payload_is_free(payload)
    with pytest.raises(r.FreeTierViolation):
        r.assert_create_payload_is_free({"serviceDetails": {"plan": "starter"}})


def test_create_response_plan_guard_and_url():
    created = {
        "service": {
            "id": "srv-abc",
            "serviceDetails": {"plan": "free", "url": "https://x.onrender.com"},
        }
    }
    assert r.parse_service_id_from_create_response(created) == "srv-abc"
    assert r.extract_plan(created) == "free"
    assert r.extract_service_url(created) == "https://x.onrender.com"
    paid = {"service": {"id": "srv-p", "serviceDetails": {"plan": "starter"}}}
    with pytest.raises(r.FreeTierViolation):
        r.assert_free_plan(r.extract_plan(paid))
    with pytest.raises(ValueError):
        r.parse_service_id_from_create_response({"service": {}})


def test_region_policy_forbids_frankfurt():
    assert r.validate_muse_region("oregon") == "oregon"
    assert r.validate_muse_region("singapore") == "singapore"
    assert r.validate_muse_region("") == "oregon"
    assert set(r.ALLOWED_MUSE_REGIONS) == {"oregon", "ohio", "virginia", "singapore"}
    assert r.DEFAULT_REGION == "oregon"
    assert r.FALLBACK_NON_US_REGION == "singapore"
    with pytest.raises(r.RegionViolation):
        r.validate_muse_region("frankfurt")
    with pytest.raises(r.RegionViolation):
        r.validate_muse_region("mars")


def test_model_policy_fallback_inside_same_worker():
    assert r.PREFERRED_MODEL == "opencode/muse-spark-1.3-contributor-free"
    assert r.FALLBACK_MODEL == "opencode/space-bunny-free"
    assert r.select_model(True) == r.PREFERRED_MODEL
    assert r.select_model(False) == r.FALLBACK_MODEL
    assert r.ALLOW_SECOND_SERVICE_FOR_MODEL_FALLBACK is False


def test_single_create_enforced():
    state = r.ExecutionAttemptState(issue_number=1)
    state2 = state.with_created_service("srv-1")
    assert state2.service_id == "srv-1"
    with pytest.raises(ValueError):
        state2.with_created_service("srv-2")
    with pytest.raises(ValueError):
        r.enforce_single_create("srv-1")
    r.enforce_single_create("")


def test_job_request_minimum_payload():
    req = r.JobRequest(
        repository_url=r.PUBLIC_REPO_URL,
        base_ref="main",
        task_text="do the thing",
        issue_number=7,
        metadata=make_metadata(issue_number=7),
        base_sha="abc123",
    )
    payload = req.to_dict()
    assert payload["repositoryUrl"] == r.PUBLIC_REPO_URL
    assert payload["baseRef"] == "main"
    assert payload["issueNumber"] == 7
    assert payload["metadata"]["region"] == "oregon"
    assert payload["metadata"]["model"] == r.PREFERRED_MODEL
    clone = r.JobRequest.from_dict(payload)
    assert clone.to_dict() == payload
    with pytest.raises(ValueError):
        r.JobRequest(
            repository_url="https://github.com/other/repo",
            base_ref="main",
            task_text="x",
            issue_number=1,
            metadata=make_metadata(),
        ).validate()
    with pytest.raises(ValueError):
        r.JobRequest(
            repository_url=r.PUBLIC_REPO_URL,
            base_ref="main",
            task_text="",
            issue_number=1,
            metadata=make_metadata(),
        ).validate()
    with pytest.raises(ValueError):
        r.JobRequest(
            repository_url=r.PUBLIC_REPO_URL,
            base_ref="main",
            task_text="x",
            issue_number=1,
            metadata=make_metadata(issue_number=2),
        ).validate()


def test_job_result_schema_and_error_semantics():
    ok = r.JobResult(
        job_id="job-1", status="succeeded", issue_number=1,
        model=r.PREFERRED_MODEL, region="oregon", summary="done",
    )
    assert ok.succeeded
    assert ok.to_dict()["jobId"] == "job-1"
    assert r.JobResult.from_dict(ok.to_dict()).to_dict() == ok.to_dict()
    bad = r.JobResult(
        job_id="job-2", status="failed", issue_number=1, error="boom",
    )
    assert not bad.succeeded
    with pytest.raises(ValueError):
        r.JobResult(job_id="job-3", status="failed", issue_number=1).validate()
    with pytest.raises(ValueError):
        r.JobResult(job_id="", status="succeeded", issue_number=1).validate()
    with pytest.raises(ValueError):
        r.JobResult(job_id="j", status="nope", issue_number=1).validate()
    assert r.runner_job_status_path("job-1") == "/v1/jobs/job-1"
    assert r.RUNNER_HEALTH_PATH == "/health"
    assert r.RUNNER_SUBMIT_PATH == "/v1/jobs"


def test_cleanup_semantics():
    assert r.should_cleanup("srv-1") is True
    assert r.should_cleanup("") is False
    assert r.should_cleanup(None) is False
    plan = r.CleanupPlan(service_id="srv-1").validate()
    assert plan.delete_attempts == r.MAX_DELETE_ATTEMPTS
    assert plan.suspend_attempts <= r.MAX_SUSPEND_ATTEMPTS
    # Deletion verified only via 404/410 retrieve or absence from list.
    assert r.deletion_verified(404, [], "srv-1") is True
    assert r.deletion_verified(410, [], "srv-1") is True
    assert r.deletion_verified(200, ["srv-1"], "srv-1") is False
    assert r.deletion_verified(200, ["other"], "srv-1") is True
    assert r.deletion_verified(200, [], "") is False
    state = r.ExecutionAttemptState(issue_number=3, service_id="srv-9")
    assert state.describe_cleanup().service_id == "srv-9"
    with pytest.raises(ValueError):
        r.ExecutionAttemptState(issue_number=3).describe_cleanup()


def test_runner_submit_and_status_roundtrip():
    req = r.JobRequest(
        repository_url=r.PUBLIC_REPO_URL,
        base_ref="main",
        task_text="smoke task",
        issue_number=11,
        metadata=make_metadata(issue_number=11, region="singapore"),
    ).to_dict()
    created = render_runner.submit_job(req)
    assert created["jobId"].startswith("job-")
    status = render_runner.job_status(created["jobId"])
    assert status["status"] == "succeeded"
    assert status["issueNumber"] == 11
    assert status["region"] == "singapore"
    assert render_runner.job_status("job-missing") is None
    with pytest.raises(ValueError):
        render_runner.submit_job({"bogus": True})
    assert render_runner.health_payload()["status"] == "ok"
    assert render_runner.health_payload()["region"] in r.ALLOWED_MUSE_REGIONS


def test_runner_contract_paths_match_lifecycle():
    assert r.HEALTH_CHECK_PATH == render_runner.HEALTH_CHECK_PATH
    assert render_runner.RUNNER_SUBMIT_PATH == "/v1/jobs"
    template = render_runner.RUNNER_JOB_STATUS_PATH_TEMPLATE
    assert template == "/v1/jobs/{jobId}"
    assert render_runner.RUNNER_JOB_ID_FIELD == "jobId"


def test_secrets_never_in_payloads():
    req = r.JobRequest(
        repository_url=r.PUBLIC_REPO_URL,
        base_ref="main",
        task_text="task",
        issue_number=2,
        metadata=make_metadata(issue_number=2),
    ).to_dict()
    assert "KEY" not in json.dumps(req)
    assert "GITHUB_TOKEN" not in json.dumps(req)
    assert r.redact_secret("super-secret") == "***"
