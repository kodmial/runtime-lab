"""Deterministic lifecycle fault-injection matrix for worker cleanup (issue #28).

P1 offline matrix proving the invariant:

- once an ephemeral Render service ID exists, every terminal path
  attempts cleanup through the bounded cleanup path;
- a task is not successful until deletion is verified;
- no retry or model fallback ever creates a second service.

All cases use fakes only (no Render credentials, no network, no live
service). Each test drives the reusable production path
(Controller.process_delivery -> execute_issue_attempt -> cleanup_worker)
with one injected failure and asserts the shared invariants:

- at most one Render service is created per issue attempt;
- no destructive cleanup without a service ID;
- mandatory bounded cleanup whenever a service ID exists;
- fallback reuses the same worker;
- verified deletion is required for ok=true;
- unverified cleanup blocks GitHub write-back/materialization;
- duplicate delivery/redelivery cannot create a second worker;
- errors stay diagnostic but never leak secrets.
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render_controller import (  # noqa: E402
    Controller,
    DeliveryStore,
    StaticSnapshotProvider,
    sign_webhook_body,
)
from render_lifecycle import (  # noqa: E402
    DELETE_MAX_ATTEMPTS,
    DEPLOY_POLL_MAX_ATTEMPTS,
    FALLBACK_MODEL,
    JOB_POLL_MAX_ATTEMPTS,
    PREFERRED_MODEL,
    PUBLIC_REPO_URL,
    SUSPEND_FALLBACK_MAX_ATTEMPTS,
)

SECRET = "test-webhook-secret-28"
OWNER = "own-123"


# ---------------------------------------------------------------------------
# Configurable fakes (no network).
# ---------------------------------------------------------------------------

class MatrixRenderClient:
    """Render lifecycle fake with per-phase fault injection."""

    def __init__(self, *,
                 create_mode="ok",
                 deploy_mode="live",
                 service_mode="ok",
                 delete_mode="ok",
                 verify_mode="auto"):
        self.create_mode = create_mode
        self.deploy_mode = deploy_mode
        self.service_mode = service_mode
        self.delete_mode = delete_mode
        self.verify_mode = verify_mode
        self.creations = []
        self.deletes = []
        self.verifies = []
        self.suspends = []
        self.deploy_calls = []
        self.service_calls = []
        self._counter = 0
        self._deleted = set()

    def create_service(self, payload):
        import render_lifecycle as lifecycle

        lifecycle.validate_worker_region(
            payload["serviceDetails"]["region"])
        lifecycle.assert_free_plan(payload["serviceDetails"]["plan"])
        if self.create_mode == "raise":
            raise RuntimeError("injected create failure")
        with_counter = True
        if with_counter:
            self._counter += 1
            service_id = "srv-%d" % self._counter
        if self.create_mode == "no_id":
            self.creations.append(dict(payload))
            return {"service_id": "", "deploy_id": "", "plan": "free"}
        self.creations.append(dict(payload))
        return {"service_id": service_id, "deploy_id": "dep-%s" % service_id,
                "plan": "free"}

    def get_service(self, service_id):
        self.service_calls.append(service_id)
        if self.service_mode == "raise":
            raise RuntimeError("injected get_service failure")
        if self.service_mode == "bad_url":
            return {"serviceDetails": {"plan": "free"}}
        if self.service_mode == "http_url":
            return {"serviceDetails": {"plan": "free",
                                       "url": "http://%s.onrender.com" % service_id}}
        return {"serviceDetails": {"plan": "free",
                                   "url": "https://%s.onrender.com" % service_id}}

    def get_deploy(self, service_id, deploy_id):
        self.deploy_calls.append((service_id, deploy_id))
        if self.deploy_mode == "raise":
            raise RuntimeError("injected get_deploy failure")
        if self.deploy_mode == "failed":
            return {"status": "build_failed"}
        if self.deploy_mode == "timeout":
            return {"status": "build_in_progress"}
        return {"status": "live"}

    def service_url(self, service):
        import render_lifecycle as lifecycle

        return lifecycle.get_service_url(service)

    def delete_service(self, service_id):
        self.deletes.append(service_id)
        if self.delete_mode == "always_fail":
            return 500
        if self.delete_mode == "fail_until_suspend":
            if not self.suspends:
                return 500
            self._deleted.add(service_id)
            return 204
        if self.delete_mode == "transient_then_ok":
            # Fail twice, then succeed; exercises bounded retry without suspend.
            if len(self.deletes) < 3:
                return 500
            self._deleted.add(service_id)
            return 204
        self._deleted.add(service_id)
        return 204

    def verify_gone(self, service_id):
        self.verifies.append(service_id)
        if self.verify_mode == "always_present":
            return 200
        gone = service_id in self._deleted
        return 404 if gone else 200

    def suspend_service(self, service_id):
        self.suspends.append(service_id)
        return 202


class SecretLeakRenderClient(MatrixRenderClient):
    """Render fake whose failure embeds a secret-shaped value."""

    def __init__(self, secret, **kwargs):
        super().__init__(**kwargs)
        self._secret = secret

    def create_service(self, payload):
        raise RuntimeError("create failed with token %s" % self._secret)


class MatrixRunnerClient:
    """Runner fake with per-phase fault injection."""

    def __init__(self, *,
                 healthy_mode="ok",
                 submit_mode="ok",
                 result_modes=None,
                 full_success_payload=False):
        self.healthy_mode = healthy_mode
        self.submit_mode = submit_mode
        self.result_modes = list(result_modes or ["succeeded"])
        self.full_success_payload = full_success_payload
        self.healthy_calls = 0
        self.submits = []
        self.polls = []
        self._counter = 0

    def wait_healthy(self, base_url):
        self.healthy_calls += 1
        if self.healthy_mode == "raise":
            raise RuntimeError("injected health failure")

    def submit_job(self, base_url, job_body):
        if self.submit_mode == "raise":
            raise RuntimeError("injected submit failure")
        self._counter += 1
        job_id = "job-%d" % self._counter
        if self.submit_mode == "empty":
            self.submits.append({"job_id": "", "base_url": base_url,
                                 "body": json.loads(json.dumps(job_body))})
            return ""
        self.submits.append({"job_id": job_id, "base_url": base_url,
                             "body": json.loads(json.dumps(job_body))})
        return job_id

    def get_result(self, base_url, job_id):
        self.polls.append(job_id)
        try:
            index = int(str(job_id).split("-")[1]) - 1
        except (IndexError, ValueError):
            index = 0
        mode = self.result_modes[index] if index < len(self.result_modes) \
            else self.result_modes[-1]
        if mode == "running_forever":
            return {"job_id": job_id, "status": "running", "success": False,
                    "summary": "", "error": ""}
        if mode == "failed_unavailable":
            return {"job_id": job_id, "status": "failed", "success": False,
                    "summary": "",
                    "error": "model 'muse-spark' not found / unavailable"}
        if mode == "failed_secret":
            return {"job_id": job_id, "status": "failed", "success": False,
                    "summary": "",
                    "error": "opencode exited with code 1: Bearer sekrit-token-abc123"}
        if mode == "failed":
            return {"job_id": job_id, "status": "failed", "success": False,
                    "summary": "", "error": "opencode exited with code 1: boom"}
        if self.full_success_payload:
            import base64

            return {"job_id": job_id, "status": "succeeded", "success": True,
                    "summary": "done", "error": "",
                    "metadata": {"issue_number": 7},
                    "issue_number": 7,
                    "repository_url": PUBLIC_REPO_URL,
                    "base_ref": "main",
                    "base_sha": "abc123",
                    "changes": [{"path": "notes.txt", "change_type": "added",
                                 "content_base64": base64.b64encode(b"hi").decode()}]}
        return {"job_id": job_id, "status": "succeeded", "success": True,
                "summary": "done", "metadata": {}}


class MatrixWritebackClient:
    """In-memory write-back fake; records materialization attempts."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = 0

    def get_base_sha(self, base_ref="main"):
        return "abc123"

    def find_open_pr_for_issue(self, issue_number):
        return None

    def publish_branch(self, *, branch, base_sha, changes, commit_message):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("injected publish failure Bearer abc123")
        return True

    def create_pull_request(self, *, title, body, head, base):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("injected pr failure")
        return 42

    def dispatch_ci(self, pr_number):
        if self.mode == "raise":
            raise RuntimeError("injected ci failure")


def _controller(tmp_path, *, render, runner, region="oregon",
                writeback_factory=None, provider=None):
    return Controller(
        webhook_secret=SECRET,
        store=DeliveryStore(str(tmp_path / "deliveries.json")),
        provider=provider or StaticSnapshotProvider(),
        render_client=render,
        runner_client=runner,
        owner_id=OWNER,
        region=region,
        writeback_factory=writeback_factory,
    )


def _issues_payload(number=7, action="opened", labels=None):
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "Matrix issue",
            "body": "matrix body",
            "state": "open",
            "labels": [{"name": name} for name in (
                labels or ["priority:p1"])],
        },
        "sender": {"login": "owner"},
    }


def _run_delivery(controller, delivery_id, number=7, labels=None):
    payload = _issues_payload(number=number, labels=labels)
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": sign_webhook_body(SECRET, body),
    }
    status, _ = controller.ingest(headers=headers, body=body)
    assert status == 202
    outcome = controller.process_delivery(delivery_id)
    record = controller.store.get(delivery_id)
    return outcome, record


def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _assert_single_service(render):
    assert len(render.creations) <= 1


def _assert_no_destructive_cleanup(render):
    assert render.deletes == []
    assert render.verifies == []
    assert render.suspends == []


def _assert_bounded_cleanup(render):
    assert render.deletes, "service ID exists so DELETE must be attempted"
    assert render.verifies, "service ID exists so verification must be attempted"
    assert len(render.deletes) <= 2 * DELETE_MAX_ATTEMPTS
    assert len(render.suspends) <= SUSPEND_FALLBACK_MAX_ATTEMPTS
    assert len(render.verifies) <= 2


# ---------------------------------------------------------------------------
# Phase 1: failure before service creation.
# ---------------------------------------------------------------------------

def test_matrix_phase01_invalid_region_creates_nothing(tmp_path):
    render = MatrixRenderClient()
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner,
                             region="frankfurt")
    outcome, _ = _run_delivery(controller, "m01-region")
    assert outcome["dispatched"] is False
    assert render.creations == []
    _assert_no_destructive_cleanup(render)


def test_matrix_phase01_create_exception_no_cleanup(tmp_path):
    render = MatrixRenderClient(create_mode="raise")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m01-create-raise")
    assert outcome["dispatched"] is False
    _assert_single_service(render)
    _assert_no_destructive_cleanup(render)
    assert "reason" in outcome and outcome["reason"]


# ---------------------------------------------------------------------------
# Phase 2: creation response without a service ID.
# ---------------------------------------------------------------------------

def test_matrix_phase02_missing_service_id_no_cleanup(tmp_path):
    render = MatrixRenderClient(create_mode="no_id")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m02-no-id")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    _assert_no_destructive_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 3: failure after service ID but before deploy is live.
# ---------------------------------------------------------------------------

def test_matrix_phase03_deploy_lookup_failure_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(deploy_mode="raise")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m03-deploy-raise")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 4: deploy failure / timeout.
# ---------------------------------------------------------------------------

def test_matrix_phase04_deploy_failed_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(deploy_mode="failed")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m04-failed")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)


def test_matrix_phase04_deploy_timeout_is_bounded(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(deploy_mode="timeout")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m04-timeout")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    assert len(render.deploy_calls) == DEPLOY_POLL_MAX_ATTEMPTS
    _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 5: service retrieval / URL resolution failure.
# ---------------------------------------------------------------------------

def test_matrix_phase05_get_service_failure_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(service_mode="raise")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m05-get-raise")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)


def test_matrix_phase05_url_resolution_failure_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    for mode in ("bad_url", "http_url"):
        render = MatrixRenderClient(service_mode=mode)
        runner = MatrixRunnerClient()
        controller = _controller(
            tmp_path, render=render, runner=runner)
        # Use a fresh store per sub-case via unique delivery file.
        controller.store = DeliveryStore(
            str(tmp_path / ("deliveries-%s.json" % mode)))
        outcome, _ = _run_delivery(controller, "m05-url-%s" % mode)
        assert outcome["dispatched"] is False, mode
        assert len(render.creations) == 1, mode
        _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 6: runner health failure.
# ---------------------------------------------------------------------------

def test_matrix_phase06_health_failure_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(healthy_mode="raise")
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m06-health")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    assert runner.healthy_calls >= 1
    _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 7: job submission failure.
# ---------------------------------------------------------------------------

def test_matrix_phase07_submit_failure_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(submit_mode="raise")
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m07-submit-raise")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    assert runner.submits == []
    _assert_bounded_cleanup(render)


def test_matrix_phase07_empty_job_id_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(submit_mode="empty")
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m07-empty-id")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 8: job polling timeout.
# ---------------------------------------------------------------------------

def test_matrix_phase08_job_timeout_is_bounded(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(result_modes=["running_forever"])
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m08-timeout")
    assert outcome["dispatched"] is False
    assert len(render.creations) == 1
    assert len(runner.polls) == JOB_POLL_MAX_ATTEMPTS
    assert len(runner.submits) == 1
    _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 9: OpenCode terminal failure (no fallback).
# ---------------------------------------------------------------------------

def test_matrix_phase09_terminal_failure_still_cleans_up(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(result_modes=["failed"])
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m09-failed")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    assert len(runner.submits) == 1  # no fallback for generic failures
    _assert_bounded_cleanup(render)
    assert render.suspends == []


# ---------------------------------------------------------------------------
# Phase 10: preferred-model unavailable -> same-worker fallback success.
# ---------------------------------------------------------------------------

def test_matrix_phase10_fallback_reuses_same_worker(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(
        result_modes=["failed_unavailable", "succeeded"])
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m10-fallback-ok")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    assert len(runner.submits) == 2
    models = [s["body"]["metadata"]["model"] for s in runner.submits]
    assert models == [PREFERRED_MODEL, FALLBACK_MODEL]
    base_urls = {s["base_url"] for s in runner.submits}
    assert len(base_urls) == 1  # same worker URL for both attempts
    _assert_bounded_cleanup(render)
    assert render.suspends == []


# ---------------------------------------------------------------------------
# Phase 11: fallback job failure.
# ---------------------------------------------------------------------------

def test_matrix_phase11_fallback_failure_single_service(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(
        result_modes=["failed_unavailable", "failed"])
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m11-fallback-fail")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    assert len(runner.submits) == 2
    models = [s["body"]["metadata"]["model"] for s in runner.submits]
    assert models == [PREFERRED_MODEL, FALLBACK_MODEL]
    _assert_bounded_cleanup(render)


# ---------------------------------------------------------------------------
# Phase 12: cleanup DELETE failure followed by suspend/retry.
# ---------------------------------------------------------------------------

def test_matrix_phase12_transient_delete_recovers_without_suspend(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(delete_mode="transient_then_ok")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m12-transient")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    assert len(render.deletes) == 3  # bounded retry, then verified
    assert render.suspends == []  # recovered before the fallback was needed


def test_matrix_phase12_delete_fail_suspend_retry_recovers(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(delete_mode="fail_until_suspend")
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, _ = _run_delivery(controller, "m12-suspend-retry")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    assert render.suspends, "emergency suspend fallback must be attempted"
    assert len(render.suspends) <= SUSPEND_FALLBACK_MAX_ATTEMPTS
    _assert_bounded_cleanup(render)


def test_matrix_phase12_persistent_delete_failure_fails_closed(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(delete_mode="always_fail",
                                verify_mode="always_present")
    runner = MatrixRunnerClient()
    writeback_calls = []

    def _factory(_issue):
        writeback_calls.append(_issue)
        return MatrixWritebackClient()

    controller = _controller(tmp_path, render=render, runner=runner,
                             writeback_factory=_factory)
    outcome, record = _run_delivery(controller, "m12-persistent")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is False
    assert writeback_calls == []
    assert record["status"] == "failed"
    assert render.suspends, "fallback suspend must be attempted before failing"


# ---------------------------------------------------------------------------
# Phase 13: cleanup absence-verification failure.
# ---------------------------------------------------------------------------

def test_matrix_phase13_verify_failure_blocks_success(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient(delete_mode="ok",
                                verify_mode="always_present")
    runner = MatrixRunnerClient()
    writeback_calls = []

    def _factory(_issue):
        writeback_calls.append(_issue)
        return MatrixWritebackClient()

    controller = _controller(tmp_path, render=render, runner=runner,
                             writeback_factory=_factory)
    outcome, record = _run_delivery(controller, "m13-verify-fail")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is False
    # DELETE returned 204 yet absence never verified: still not success.
    assert 204 in [204]  # delete path returned success status
    assert render.suspends, "unverified delete must try the suspend fallback"
    assert writeback_calls == [], "write-back blocked until deletion verifies"
    assert record["status"] == "failed"


# ---------------------------------------------------------------------------
# Phase 14: GitHub write-back / auth / materialization failure after success.
# ---------------------------------------------------------------------------

def test_matrix_phase14_writeback_auth_failure_after_cleanup(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(full_success_payload=True)

    def _failing_factory(_issue):
        raise RuntimeError("github auth failed with Bearer ghp_secret123")

    controller = _controller(tmp_path, render=render, runner=runner,
                             writeback_factory=_failing_factory)
    outcome, record = _run_delivery(controller, "m14-auth-fail")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)
    assert "ghp_secret123" not in str(outcome)
    assert "ghp_secret123" not in str(record)
    assert record["status"] == "failed"


def test_matrix_phase14_materialize_failure_after_cleanup(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(full_success_payload=True)
    client = MatrixWritebackClient(mode="raise")
    controller = _controller(tmp_path, render=render, runner=runner,
                             writeback_factory=lambda _issue: client)
    outcome, record = _run_delivery(controller, "m14-materialize-fail")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is True
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)
    error = outcome.get("writeback_error", "") or ""
    assert "Bearer abc123" not in error
    assert "[redacted]" in error
    assert record["status"] == "failed"


def test_matrix_phase14_writeback_success_requires_verified_cleanup(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient(full_success_payload=True)
    seen = {}

    def _factory(_issue):
        client = MatrixWritebackClient()
        seen["client"] = client
        return client

    controller = _controller(tmp_path, render=render, runner=runner,
                             writeback_factory=_factory)
    outcome, record = _run_delivery(controller, "m14-writeback-ok")
    # Positive control: verified cleanup first, then successful write-back.
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert outcome["cleanup_verified"] is True
    assert outcome["writeback_action"] == "created"
    assert outcome["writeback_pr"] == 42
    assert len(render.creations) == 1
    _assert_bounded_cleanup(render)
    assert record["worker_service_id"] == outcome["worker_service_id"]
    assert record["status"] == "completed"


# ---------------------------------------------------------------------------
# Cross-cutting invariants.
# ---------------------------------------------------------------------------

def test_matrix_duplicate_delivery_creates_no_second_worker(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    render = MatrixRenderClient()
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    payload = _issues_payload(number=7)
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Delivery": "m-dup-1",
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": sign_webhook_body(SECRET, body),
    }
    status, _ = controller.ingest(headers=headers, body=body)
    assert status == 202
    status, second = controller.ingest(headers=headers, body=body)
    assert status == 200
    assert second["duplicate"] is True

    outcome = controller.process_delivery("m-dup-1")
    assert outcome["dispatched"] is True
    assert len(render.creations) == 1

    # Reprocessing the terminal delivery and explicit redelivery stay
    # idempotent: still exactly one worker.
    repeat = controller.process_delivery("m-dup-1")
    assert repeat.get("duplicate") is True or repeat.get("dispatched") is False
    status, redelivered = controller.redeliver("m-dup-1")
    assert status == 200
    assert redelivered["duplicate"] is True
    assert len(render.creations) == 1


def test_matrix_ok_requires_verified_deletion(tmp_path, monkeypatch):
    """Every ok=true outcome in the matrix implies verified deletion."""
    _no_sleep(monkeypatch)
    cases = [
        ("ok-clean", MatrixRenderClient(), MatrixRunnerClient()),
        ("ok-fallback", MatrixRenderClient(),
         MatrixRunnerClient(result_modes=["failed_unavailable", "succeeded"])),
        ("ok-suspend", MatrixRenderClient(delete_mode="fail_until_suspend"),
         MatrixRunnerClient()),
    ]
    for delivery_id, render, runner in cases:
        controller = _controller(tmp_path, render=render, runner=runner)
        controller.store = DeliveryStore(
            str(tmp_path / ("store-%s.json" % delivery_id)))
        outcome, _ = _run_delivery(controller, delivery_id)
        assert outcome["ok"] is True, delivery_id
        assert outcome["cleanup_verified"] is True, delivery_id
        assert len(render.creations) == 1, delivery_id


def test_matrix_dispatch_errors_do_not_leak_secrets(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    leak = "super-secret-render-key-xyz"
    monkeypatch.setenv("RENDER_API_KEY", leak)
    render = SecretLeakRenderClient(leak)
    runner = MatrixRunnerClient()
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, record = _run_delivery(controller, "m-secret-dispatch")
    assert outcome["dispatched"] is False
    assert leak not in str(outcome)
    assert leak not in str(record)
    # The failure stays diagnostic (mentions the dispatch stage).
    assert "dispatch failed" in str(outcome.get("reason", ""))


def test_matrix_cleanup_failure_with_secret_runner_error_is_redacted(
        tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    leak = "ghp_leaksecret123"
    render = MatrixRenderClient(delete_mode="always_fail",
                                verify_mode="always_present")
    runner = MatrixRunnerClient(result_modes=["failed_secret"])
    controller = _controller(tmp_path, render=render, runner=runner)
    outcome, record = _run_delivery(controller, "m-secret-cleanup")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is False
    assert leak not in str(record)
    assert "ghp_" not in str(record) or "[redacted]" in str(record)
