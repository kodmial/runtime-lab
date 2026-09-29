"""Tests for the persistent Render controller and webhook ingress (issue #10).

Covers the Definition of Done without network access or secrets:
- webhook signature verification (valid/invalid/missing secrets rejected);
- idempotency by X-GitHub-Delivery (duplicates never dispatch twice);
- scheduling decisions matching the repository scheduler semantics
  (priority ordering, blockers incl. DoR fallback, in-progress/lease,
  paused, WIP limits, max attempts, command triggers, event families);
- eligible events dispatch exactly one free ephemeral worker;
- worker cleanup is mandatory and verified (delete + 404/410);
- controller concurrency (default 4, per-issue single worker, no global
  mutex, different issues run concurrently);
- HTTP ingress (health, fast 2xx ack, redelivery recovery, restart
  recovery) without any GitHub Actions run;
- the controller path never relays through GitHub Actions and never
  executes OpenCode in the controller process.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller_server import (  # noqa: E402
    create_server,
    resolve_max_concurrent,
    resolve_port,
)
from render_controller import (  # noqa: E402
    CONTROLLER_HEALTH_PATH,
    DEFAULT_MAX_DISPATCH_ATTEMPTS,
    DEFAULT_WIP_LIMIT,
    WEBHOOK_ACK_BUDGET_SECONDS,
    WEBHOOK_PATH,
    WEBHOOK_PATH_ALIASES,
    Controller,
    DeliveryStore,
    EligibilitySnapshot,
    StaticSnapshotProvider,
    WebhookEvent,
    build_snapshot,
    decide_eligible,
    event_wants_evaluation,
    format_correlation,
    is_command_comment,
    parse_webhook_event,
    priority_rank,
    rank_issues,
    readiness_dependency_numbers,
    resolve_deployment_pattern,
    sign_webhook_body,
    validate_deployment,
    verify_webhook_signature,
)
from render_lifecycle import (  # noqa: E402
    FALLBACK_MODEL,
    MAX_CONCURRENT_AUTOMATION_JOBS,
    PREFERRED_MODEL,
)

SECRET = "test-webhook-secret-123"


# ---------------------------------------------------------------------------
# Fakes (no network).
# ---------------------------------------------------------------------------

class FakeRenderClient:
    """In-memory ephemeral-worker lifecycle; counts service creations."""

    def __init__(self, deploy_status="live"):
        self.creations = []
        self.deletes = []
        self.verifies = []
        self.suspends = []
        self.deploy_status = deploy_status
        self._counter = 0
        self._deleted = set()
        self._lock = threading.Lock()

    def create_service(self, payload):
        import render_lifecycle as lifecycle

        lifecycle.validate_worker_region(
            payload["serviceDetails"]["region"])
        lifecycle.assert_free_plan(payload["serviceDetails"]["plan"])
        with self._lock:
            self._counter += 1
            service_id = "srv-%d" % self._counter
            self.creations.append(dict(payload))
        return {"service_id": service_id, "deploy_id": "dep-%s" % service_id,
                "plan": "free"}

    def get_service(self, service_id):
        return {"serviceDetails": {
            "plan": "free",
            "url": "https://%s.onrender.com" % service_id,
        }}

    def get_deploy(self, service_id, deploy_id):
        return {"status": self.deploy_status}

    def service_url(self, service):
        import render_lifecycle as lifecycle

        return lifecycle.get_service_url(service)

    def delete_service(self, service_id):
        with self._lock:
            self.deletes.append(service_id)
            self._deleted.add(service_id)
        return 204

    def verify_gone(self, service_id):
        with self._lock:
            self.verifies.append(service_id)
            gone = service_id in self._deleted
        return 404 if gone else 200

    def suspend_service(self, service_id):
        with self._lock:
            self.suspends.append(service_id)
        return 202


class FailingCleanupRenderClient(FakeRenderClient):
    """Render fake whose worker cannot be proven deleted."""

    def delete_service(self, service_id):
        with self._lock:
            self.deletes.append(service_id)
        return 500

    def verify_gone(self, service_id):
        with self._lock:
            self.verifies.append(service_id)
        return 200

class FakeRunnerClient:
    """In-memory runner API; scripted terminal results per job."""

    def __init__(self, results=None):
        # results: list of "succeeded"/"failed"/... consumed per submitted job.
        self.results = list(results or ["succeeded"])
        self.healthy_calls = 0
        self.submits = []
        self.polls = []
        self._counter = 0
        self._lock = threading.Lock()

    def wait_healthy(self, base_url):
        with self._lock:
            self.healthy_calls += 1

    def submit_job(self, base_url, job_body):
        with self._lock:
            self._counter += 1
            job_id = "job-%d" % self._counter
            self.submits.append({"job_id": job_id, "base_url": base_url,
                                 "body": json.loads(json.dumps(job_body))})
            return job_id

    def get_result(self, base_url, job_id):
        from render_lifecycle import parse_job_result  # noqa: F401 (shape guard)

        with self._lock:
            self.polls.append(job_id)
            index = int(job_id.split("-")[1]) - 1
            status = self.results[index] if index < len(self.results) else "succeeded"
        if status == "failed-unavailable":
            return {"job_id": job_id, "status": "failed", "success": False,
                    "summary": "", "error": "model 'muse-spark' not found / unavailable"}
        if status == "failed":
            return {"job_id": job_id, "status": "failed", "success": False,
                    "summary": "", "error": "opencode exited with code 1: boom"}
        return {"job_id": job_id, "status": "succeeded", "success": True,
                "summary": "done", "metadata": {}}


def _controller(tmp_path, **overrides):
    overrides.setdefault("webhook_secret", SECRET)
    overrides.setdefault("store", DeliveryStore(str(tmp_path / "deliveries.json")))
    overrides.setdefault("provider", StaticSnapshotProvider())
    overrides.setdefault("owner_id", "own-123")
    return Controller(**overrides)


def _issues_payload(number=7, action="opened", labels=None, title="T", body="B",
                    state="open"):
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": name} for name in (labels or [])],
        },
        "sender": {"login": "owner"},
    }


def _headers(delivery_id, event, secret=SECRET, body=b""):
    return {
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": sign_webhook_body(secret, body),
    }


# ---------------------------------------------------------------------------
# Signature verification.
# ---------------------------------------------------------------------------

def test_signature_accepts_valid_and_rejects_invalid():
    body = b'{"action":"opened"}'
    valid = sign_webhook_body(SECRET, body)
    assert verify_webhook_signature(SECRET, body, valid) is True
    assert verify_webhook_signature(SECRET, body, "sha256=" + "0" * 64) is False
    assert verify_webhook_signature(SECRET, b"tampered", valid) is False
    assert verify_webhook_signature(SECRET, body, None) is False
    assert verify_webhook_signature(SECRET, body, "") is False
    assert verify_webhook_signature(SECRET, body, "sha1=abc") is False
    assert verify_webhook_signature(SECRET, body, "sha256=not-hex!!") is False
    assert verify_webhook_signature("", body, valid) is False


def test_ingest_rejects_invalid_signature_with_401(tmp_path):
    controller = _controller(tmp_path)
    body = json.dumps(_issues_payload()).encode()
    headers = {"X-GitHub-Delivery": "del-1", "X-GitHub-Event": "issues",
               "X-Hub-Signature-256": "sha256=" + "0" * 64}
    status, response = controller.ingest(headers=headers, body=body)
    assert status == 401
    assert controller.store.get("del-1") is None


def test_ingest_rejects_missing_headers_and_bad_json(tmp_path):
    controller = _controller(tmp_path)
    body = json.dumps(_issues_payload()).encode()
    status, _ = controller.ingest(headers={}, body=body)
    assert status == 400
    status, _ = controller.ingest(
        headers={"X-GitHub-Delivery": "d", "X-GitHub-Event": "nope",
                 "X-Hub-Signature-256": sign_webhook_body(SECRET, body)},
        body=body)
    assert status == 400
    bad = b"not json{"
    status, _ = controller.ingest(headers=_headers("d2", "issues", body=bad), body=bad)
    assert status == 400


# ---------------------------------------------------------------------------
# Idempotency by X-GitHub-Delivery.
# ---------------------------------------------------------------------------

def test_duplicate_delivery_ids_do_not_dispatch_twice(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient()
    provider = StaticSnapshotProvider()
    controller = _controller(tmp_path, provider=provider,
                             render_client=render, runner_client=runner)
    payload = _issues_payload(number=7, labels=["priority:p1"])
    body = json.dumps(payload).encode()

    status, first = controller.ingest(headers=_headers("del-dup", "issues", body=body),
                                      body=body)
    assert status == 202
    status, second = controller.ingest(headers=_headers("del-dup", "issues", body=body),
                                       body=body)
    assert status == 200
    assert second["duplicate"] is True

    outcome = controller.process_delivery("del-dup")
    assert outcome["dispatched"] is True
    assert len(render.creations) == 1

    # Redelivering the completed delivery is idempotent: no second worker.
    status, response = controller.redeliver("del-dup")
    assert status == 200
    assert response["duplicate"] is True
    assert len(render.creations) == 1


def test_store_is_durable_across_restart(tmp_path):
    path = str(tmp_path / "deliveries.json")
    first = Controller(webhook_secret=SECRET, store=DeliveryStore(path),
                       provider=StaticSnapshotProvider(), owner_id="own-123")
    payload = _issues_payload(number=9, labels=["priority:p0"])
    body = json.dumps(payload).encode()
    status, _ = first.ingest(headers=_headers("del-restart", "issues", body=body),
                             body=body)
    assert status == 202

    # Simulate a controller restart: a fresh store on the same file keeps
    # the accepted delivery, and recovery re-queues it.
    second = Controller(webhook_secret=SECRET, store=DeliveryStore(path),
                        provider=StaticSnapshotProvider(), owner_id="own-123")
    assert second.store.get("del-restart") is not None
    assert second.store.get("del-restart")["status"] == "accepted"
    assert "del-restart" in second.recover_pending()


def test_redeliver_unknown_delivery_is_404(tmp_path):
    controller = _controller(tmp_path)
    status, _ = controller.redeliver("no-such-delivery")
    assert status == 404


# ---------------------------------------------------------------------------
# Scheduling decisions (mirror of issue-scheduler.yml).
# ---------------------------------------------------------------------------

def _snapshot(**overrides):
    base = {
        "issue_number": 7,
        "state": "open",
        "labels": frozenset({"priority:p1"}),
    }
    base.update(overrides)
    return EligibilitySnapshot(**base)


def test_priority_ordering_matches_scheduler():
    assert priority_rank(["priority:p1"]) == 1
    assert priority_rank(["priority:p2", "priority:p0"]) == 0
    assert priority_rank(["automation:in-progress"]) is None
    assert rank_issues([(3, ["priority:p2"]), (1, ["priority:p0"]),
                        (2, ["priority:p1"]), (9, ["other"])]) == [1, 2, 3]


def test_unprioritized_and_paused_issues_are_ineligible():
    assert decide_eligible(_snapshot(labels=frozenset())).eligible is False
    paused = _snapshot(labels=frozenset({"priority:p0", "automation:paused"}))
    decision = decide_eligible(paused)
    assert decision.eligible is False
    assert "paused" in decision.reason


def test_blockers_and_dor_fallback_defer_dispatch():
    blocked = _snapshot(labels=frozenset({"priority:p0"}), open_blockers=(3,))
    decision = decide_eligible(blocked)
    assert decision.eligible is False
    assert "#3" in decision.reason

    body = "DoR: #3 is completed before this starts."
    fallback = _snapshot(labels=frozenset({"priority:p0"}), body=body,
                         open_blocker_fallback_states={3: "open"})
    assert readiness_dependency_numbers(body, 7) == [3]
    assert decide_eligible(fallback).eligible is False

    closed_dep = _snapshot(labels=frozenset({"priority:p0"}), body=body,
                           open_blocker_fallback_states={3: "closed"})
    assert decide_eligible(closed_dep).eligible is True


def test_in_progress_reservation_and_wip_limits():
    active_pr = _snapshot(labels=frozenset({"priority:p1", "automation:in-progress"}),
                          has_open_pr=True)
    assert decide_eligible(active_pr).eligible is False
    leased = _snapshot(labels=frozenset({"priority:p1", "automation:in-progress"}),
                       lease_valid=True)
    assert decide_eligible(leased).eligible is False
    # Stale reservation without open PR or valid lease is reclaimable.
    stale = _snapshot(labels=frozenset({"priority:p1", "automation:in-progress"}))
    assert decide_eligible(stale).eligible is True

    full = _snapshot(active_count=4, wip_limit=4)
    assert decide_eligible(full).eligible is False
    assert "WIP" in decide_eligible(full).reason
    assert DEFAULT_WIP_LIMIT == 4
    assert DEFAULT_MAX_DISPATCH_ATTEMPTS == 4


def test_max_attempts_pause_instead_of_dispatch():
    exhausted = _snapshot(dispatch_attempts=4, max_attempts=4)
    decision = decide_eligible(exhausted)
    assert decision.eligible is False
    assert "attempt" in decision.reason
    assert decide_eligible(_snapshot(dispatch_attempts=3)).eligible is True


def test_execution_mode_from_labels():
    smoke = _snapshot(labels=frozenset({"priority:p1", "execution:render-smoke"}))
    assert decide_eligible(smoke).execution_mode == "smoke"
    plain = _snapshot()
    assert decide_eligible(plain).execution_mode == "e2e"


def test_superseded_exact_artifact_body_is_ineligible_for_dispatch():
    # Regression for run 36629441608 (repair issue #154): source issue
    # #106 still pins retired artifact 11001896223 / run 36492639568.
    # The executor gate refuses that body in seconds with zero Render
    # cost, but smoke issues carry no qualification fingerprint, so the
    # finalize dedup never fires and each redispatch mints another P0
    # repair. The scheduler mirror must hold such bodies paused with
    # the stable successor redirect instead of dispatching them.
    retired_body = (
        "Do **not rebuild OpenCode** for this task. Consume exactly:\n"
        "- Artifact ID: `11001896223`\n"
        "- Workflow run: `36492639568`\n"
        "- Artifact archive digest: "
        "`sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df`\n"
        "Never silently fall back to another OpenCode binary.\n"
    )
    retired = _snapshot(
        issue_number=106,
        labels=frozenset({"priority:p0", "execution:render-smoke"}),
        title="P0: Qualify the same OpenCode PR #12 artifact on Render Free 512 MiB",
        body=retired_body,
    )
    decision = decide_eligible(retired)
    assert decision.eligible is False
    assert "11001896223" in decision.reason
    assert "11004835952" in decision.reason
    assert "instead of redispatching" in decision.reason
    # Ordinary smoke still dispatches: the guard must not swallow
    # legitimate work.
    assert decide_eligible(_snapshot(body="Run the normal smoke workload.")).eligible is True
    # Paused still short-circuits first: a paused retired body keeps
    # the paused reason, never a guard surprise.
    paused_retired = _snapshot(
        labels=frozenset({"priority:p0", "automation:paused"}),
        body=retired_body,
    )
    paused_decision = decide_eligible(paused_retired)
    assert paused_decision.eligible is False
    assert "paused" in paused_decision.reason
    # Unprioritized issues keep their existing reason: the guard runs
    # after the priority gate so scheduling semantics are preserved.
    unprioritized = _snapshot(labels=frozenset(), body=retired_body)
    unprioritized_decision = decide_eligible(unprioritized)
    assert unprioritized_decision.eligible is False
    assert "no priority" in unprioritized_decision.reason


def test_event_families_and_command_trigger():
    assert is_command_comment("please /oc run this") is True
    assert is_command_comment("/opencode fix it") is True
    assert is_command_comment("just a normal comment") is False

    opened = parse_webhook_event("issues", _issues_payload(action="opened"), "d1")
    wants, _ = event_wants_evaluation(opened)
    assert wants is True
    for action in ("labeled", "unlabeled", "reopened", "edited"):
        event = parse_webhook_event(
            "issues", _issues_payload(action=action), "d-%s" % action)
        assert event_wants_evaluation(event)[0] is True
    closed = parse_webhook_event("issues", _issues_payload(action="closed"), "d2")
    assert event_wants_evaluation(closed)[0] is False

    comment_plain = WebhookEvent(event="issue_comment", action="created",
                                 issue_number=7, comment_body="hello")
    assert event_wants_evaluation(comment_plain)[0] is False
    comment_cmd = WebhookEvent(event="issue_comment", action="created",
                               issue_number=7, comment_body="/oc please")
    assert event_wants_evaluation(comment_cmd)[0] is True
    comment_pr = WebhookEvent(event="issue_comment", action="created",
                              issue_number=7, is_pull_request=True,
                              comment_body="/oc please")
    assert event_wants_evaluation(comment_pr)[0] is False

    ping = parse_webhook_event("ping", {"zen": "hi"}, "d3")
    assert event_wants_evaluation(ping)[0] is False
    pr = parse_webhook_event("pull_request", {"action": "closed", "number": 5,
                                              "pull_request": {"state": "closed"}}, "d4")
    assert event_wants_evaluation(pr)[0] is False


def test_snapshot_defaults_match_scheduler_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTROLLER_WIP_LIMIT", raising=False)
    controller = _controller(tmp_path)
    assert controller.wip_limit == 4
    assert controller.max_attempts == 4
    snapshot = build_snapshot(
        WebhookEvent(event="issues", action="opened", issue_number=7,
                     labels=("priority:p2",), state="open"),
        provider=StaticSnapshotProvider(), wip_limit=controller.wip_limit,
        max_attempts=controller.max_attempts)
    assert decide_eligible(snapshot).eligible is True


# ---------------------------------------------------------------------------
# Dispatch: exactly one free worker, mandatory verified cleanup.
# ---------------------------------------------------------------------------

def test_eligible_event_dispatches_exactly_one_free_worker(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient()
    controller = _controller(tmp_path, render_client=render, runner_client=runner)
    payload = _issues_payload(number=7, labels=["priority:p0"],
                              title="Fix it", body="details here")
    body = json.dumps(payload).encode()
    status, _ = controller.ingest(headers=_headers("del-one", "issues", body=body),
                                  body=body)
    assert status == 202
    outcome = controller.process_delivery("del-one")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert len(render.creations) == 1
    creation = render.creations[0]
    assert creation["serviceDetails"]["plan"] == "free"
    assert creation["serviceDetails"]["region"] == "oregon"
    assert render.deletes == [outcome["worker_service_id"]]
    assert render.verifies
    assert render.suspends == []  # clean delete+verify never suspends
    record = controller.store.get("del-one")
    assert record["status"] == "completed"
    assert record["worker_service_id"] == outcome["worker_service_id"]
    assert record["job_id"] == outcome["job_id"]
    correlation = format_correlation(
        delivery_id="del-one", issue_number=7,
        worker_service_id=record["worker_service_id"], job_id=record["job_id"])
    assert "delivery=del-one" in correlation and "issue=#7" in correlation


def test_region_validated_before_any_creation(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient()
    controller = _controller(tmp_path, region="frankfurt",
                             render_client=render, runner_client=runner)
    payload = _issues_payload(number=7, labels=["priority:p0"])
    body = json.dumps(payload).encode()
    controller.ingest(headers=_headers("del-region", "issues", body=body), body=body)
    outcome = controller.process_delivery("del-region")
    assert outcome["dispatched"] is False
    assert render.creations == []


def test_cleanup_verified_even_when_job_fails(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient(results=["failed"])
    controller = _controller(tmp_path, render_client=render, runner_client=runner)
    payload = _issues_payload(number=8, labels=["priority:p1"])
    body = json.dumps(payload).encode()
    controller.ingest(headers=_headers("del-fail", "issues", body=body), body=body)
    outcome = controller.process_delivery("del-fail")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert len(render.creations) == 1
    assert len(render.deletes) == 1  # unconditional cleanup
    assert 404 in [render.verify_gone(sid) for sid in render.deletes]


def test_unverified_cleanup_fails_closed_and_blocks_writeback(tmp_path):
    render = FailingCleanupRenderClient()
    runner = FakeRunnerClient(results=["succeeded"])
    writeback_calls = []

    def forbidden_writeback(issue_number):
        writeback_calls.append(issue_number)
        raise AssertionError("write-back must not run before verified cleanup")

    controller = _controller(
        tmp_path, render_client=render, runner_client=runner,
        writeback_factory=forbidden_writeback,
    )
    payload = _issues_payload(number=18, labels=["priority:p0"])
    body = json.dumps(payload).encode()
    controller.ingest(headers=_headers("del-cleanup-fail", "issues", body=body),
                      body=body)
    outcome = controller.process_delivery("del-cleanup-fail")

    assert outcome["dispatched"] is True
    assert outcome["ok"] is False
    assert outcome["cleanup_verified"] is False
    assert render.deletes
    assert render.verifies
    assert render.suspends  # emergency safety fallback was attempted
    assert writeback_calls == []
    assert controller.store.get("del-cleanup-fail")["status"] == "failed"

def test_model_fallback_stays_on_same_worker(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient(results=["failed-unavailable", "succeeded"])
    controller = _controller(tmp_path, render_client=render, runner_client=runner)
    payload = _issues_payload(number=7, labels=["priority:p0"])
    body = json.dumps(payload).encode()
    controller.ingest(headers=_headers("del-fallback", "issues", body=body), body=body)
    outcome = controller.process_delivery("del-fallback")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert len(render.creations) == 1  # never a second service for fallback
    assert len(runner.submits) == 2  # preferred + one same-worker fallback
    models = [submit["body"]["metadata"]["model"] for submit in runner.submits]
    assert models == [PREFERRED_MODEL, FALLBACK_MODEL]


def test_ineligible_events_never_create_workers(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient()
    controller = _controller(tmp_path, render_client=render, runner_client=runner)
    cases = [
        ("del-nopri", _issues_payload(number=7, labels=[])),
        ("del-paused", _issues_payload(
            number=7, labels=["priority:p0", "automation:paused"])),
        ("del-closed", _issues_payload(number=7, labels=["priority:p0"],
                                       state="closed", action="closed")),
    ]
    for delivery_id, payload in cases:
        body = json.dumps(payload).encode()
        controller.ingest(headers=_headers(delivery_id, "issues", body=body), body=body)
        outcome = controller.process_delivery(delivery_id)
        assert outcome["dispatched"] is False, delivery_id
    assert render.creations == []


# ---------------------------------------------------------------------------
# Concurrency: 4 concurrent issues, per-issue single worker, no global mutex.
# ---------------------------------------------------------------------------

def test_default_concurrency_is_four_without_global_mutex(tmp_path):
    controller = _controller(tmp_path)
    assert controller.max_concurrent == 4
    assert controller.max_concurrent == MAX_CONCURRENT_AUTOMATION_JOBS
    assert resolve_max_concurrent(None) == 4
    assert resolve_max_concurrent("2") == 2
    with pytest.raises(ValueError):
        resolve_max_concurrent("0")
    assert resolve_port(None) == 10000


def test_different_issues_run_concurrently_same_issue_single_worker(tmp_path):
    gate = threading.Event()
    release = threading.Event()

    class GatedRunner(FakeRunnerClient):
        def wait_healthy(self, base_url):
            super().wait_healthy(base_url)
            gate.set()
            assert release.wait(timeout=15)

    render = FakeRenderClient()
    runner = GatedRunner()
    controller = _controller(tmp_path, render_client=render, runner_client=runner)

    def _ingest(delivery_id, number):
        payload = _issues_payload(number=number, labels=["priority:p1"])
        body = json.dumps(payload).encode()
        status, _ = controller.ingest(
            headers=_headers(delivery_id, "issues", body=body), body=body)
        assert status == 202

    _ingest("del-a", 11)
    _ingest("del-b", 12)
    _ingest("del-a-dup-issue", 11)

    results = {}
    threads = [
        threading.Thread(target=lambda k, d: results.update(
            {k: controller.process_delivery(d)}),
            args=(key, delivery))
        for key, delivery in (("a", "del-a"), ("b", "del-b"))
    ]
    for thread in threads:
        thread.start()
    assert gate.wait(timeout=15)

    # While both issues are in flight, a second delivery for issue 11 must
    # not create a second worker (per-issue single worker, no global mutex
    # blocking issue 12).
    duplicate = controller.process_delivery("del-a-dup-issue")
    assert duplicate["dispatched"] is False
    time.sleep(0.2)
    assert len(render.creations) == 2  # one per issue, concurrently
    assert {c["name"] for c in render.creations} == {
        "runtime-lab-issue11-ctrl-del-a",
        "runtime-lab-issue12-ctrl-del-b",
    }

    release.set()
    for thread in threads:
        thread.join(timeout=15)
    assert results["a"]["dispatched"] is True
    assert results["b"]["dispatched"] is True
    assert results["a"]["worker_service_id"] != results["b"]["worker_service_id"]


# ---------------------------------------------------------------------------
# Deployment posture and correlation.
# ---------------------------------------------------------------------------

def test_deployment_requires_always_on_or_queue(monkeypatch):
    monkeypatch.delenv("CONTROLLER_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CONTROLLER_ALWAYS_ON", raising=False)
    monkeypatch.delenv("CONTROLLER_QUEUE_URL", raising=False)
    assert resolve_deployment_pattern() == "always-on-controller"
    ok, note = validate_deployment()
    assert ok is False
    assert "Free" in note or "free" in note or "always-on" in note

    monkeypatch.setenv("CONTROLLER_ALWAYS_ON", "true")
    ok, _ = validate_deployment()
    assert ok is True

    monkeypatch.delenv("CONTROLLER_ALWAYS_ON", raising=False)
    monkeypatch.setenv("CONTROLLER_QUEUE_URL", "https://queue.example/hook")
    assert resolve_deployment_pattern() == "queued-ingress"
    ok, _ = validate_deployment()
    assert ok is True

    monkeypatch.setenv("CONTROLLER_DEPLOYMENT", "bogus")
    with pytest.raises(ValueError):
        resolve_deployment_pattern()


def test_ack_budget_constant_and_webhook_paths():
    assert WEBHOOK_ACK_BUDGET_SECONDS == 10
    assert WEBHOOK_PATH == "/webhooks/github"
    assert "/webhooks/github" in WEBHOOK_PATH_ALIASES
    assert CONTROLLER_HEALTH_PATH == "/health"


# ---------------------------------------------------------------------------
# HTTP ingress (live server, ephemeral port): health, fast ack, redelivery.
# ---------------------------------------------------------------------------

class _LiveServer:
    def __init__(self, controller):
        self.server = create_server(host="127.0.0.1", port=0,
                                    controller=controller)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        time.sleep(0.1)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _http(method, url, body=None, headers=None):
    data = body if isinstance(body, bytes) else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    request_headers = dict(headers or {})
    request = urllib.request.Request(url, data=data,
                                     headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw}


@pytest.fixture()
def live(tmp_path):
    controller = _controller(tmp_path)
    server = _LiveServer(controller)
    yield server, controller
    server.close()


def test_health_reports_controller_posture(live, monkeypatch):
    server, controller = live
    monkeypatch.setenv("CONTROLLER_ALWAYS_ON", "true")
    status, body = _http("GET", server.base_url + "/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["ready"] is True
    assert body["concurrency"]["max_concurrent"] == 4
    assert body["concurrency"]["global_single_job_mutex"] is False
    assert body["webhook_ingress"]["ack_budget_seconds"] == 10
    assert body["webhook_ingress"]["free_sleeping_service_as_sole_receiver"] is False


def test_webhook_acknowledges_within_budget_without_actions(live):
    server, _ = live
    payload = _issues_payload(number=21, labels=["priority:p2"])
    body = json.dumps(payload).encode()
    started = time.time()
    status, response = _http("POST", server.base_url + "/webhooks/github",
                             body=body, headers=_headers("del-http", "issues",
                                                         body=body))
    elapsed = time.time() - started
    assert status == 202
    assert response["duplicate"] is False
    assert elapsed < WEBHOOK_ACK_BUDGET_SECONDS
    assert response["ack_seconds"] < WEBHOOK_ACK_BUDGET_SECONDS

    # Duplicate over HTTP is idempotent.
    status, response = _http("POST", server.base_url + "/webhooks/github",
                             body=body, headers=_headers("del-http", "issues",
                                                         body=body))
    assert status == 200
    assert response["duplicate"] is True

    # Invalid signatures are rejected over HTTP.
    bad_headers = {"X-GitHub-Delivery": "del-bad", "X-GitHub-Event": "issues",
                   "X-Hub-Signature-256": "sha256=" + "0" * 64}
    status, _ = _http("POST", server.base_url + "/webhooks/github",
                      body=body, headers=bad_headers)
    assert status == 401

    # Recovery endpoints work without any Actions run.
    status, listed = _http("GET", server.base_url + "/v1/deliveries")
    assert status == 200
    assert "del-http" in {item["delivery_id"] for item in listed["deliveries"]}
    status, _ = _http("GET", server.base_url + "/v1/deliveries/del-http")
    assert status == 200
    status, _ = _http("GET", server.base_url + "/v1/deliveries/missing")
    assert status == 404


def test_webhook_alias_paths_accept_deliveries(live):
    server, _ = live
    for path in ("/github/webhook", "/webhook"):
        payload = _issues_payload(number=22, labels=["priority:p1"])
        body = json.dumps(payload).encode()
        delivery = "del-alias-%s" % path.strip("/").replace("/", "-")
        status, _ = _http("POST", server.base_url + path, body=body,
                          headers=_headers(delivery, "issues", body=body))
        assert status == 202, path


def test_http_redeliver_is_idempotent_after_dispatch(tmp_path):
    render = FakeRenderClient()
    runner = FakeRunnerClient()
    controller = _controller(tmp_path, render_client=render, runner_client=runner)
    server = _LiveServer(controller)
    try:
        payload = _issues_payload(number=23, labels=["priority:p0"])
        body = json.dumps(payload).encode()
        status, _ = _http("POST", server.base_url + "/webhooks/github",
                          body=body, headers=_headers("del-rl", "issues", body=body))
        assert status == 202
        deadline = time.time() + 15
        while time.time() < deadline:
            record = controller.store.get("del-rl")
            if record and record.get("status") in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert controller.store.get("del-rl")["status"] == "completed"
        creations = len(render.creations)
        assert creations == 1
        status, response = _http("POST",
                                 server.base_url + "/v1/deliveries/del-rl/redeliver")
        assert status == 200
        assert response["duplicate"] is True
        time.sleep(0.3)
        assert len(render.creations) == creations  # still exactly one worker
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Final-path invariants: no Actions relay, no in-controller OpenCode.
# ---------------------------------------------------------------------------

def test_controller_path_uses_no_actions_relay_and_no_local_opencode():
    repo_root = Path(__file__).resolve().parents[1]
    core = (repo_root / "automation" / "render_controller.py").read_text(
        encoding="utf-8")
    server = (repo_root / "automation" / "controller_server.py").read_text(
        encoding="utf-8")
    for text in (core, server):
        assert "createWorkflowDispatch" not in text
        assert "gh workflow run" not in text
        assert "opencode run" not in text
        assert "OPENCODE_API_KEY" not in text
    assert "import subprocess" not in core
    assert "import subprocess" not in server
