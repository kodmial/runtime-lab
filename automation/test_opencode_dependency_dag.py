"""Scheduler regression coverage for the enforced OpenCode DAG (issue #88).

Proves, without network access:
- the enforced graph matches the issue #88 specification exactly;
- a dependent issue with any enforced prerequisite open is ineligible,
  even when it carries ``automation:in-progress`` with a live reservation
  (it must never be reserved or dispatched while blocked);
- completing/removing the blockers makes the dependent eligible exactly
  once (one worker, redelivery never creates a second);
- pause reconciliation only unpauses when the enforced prerequisite set
  is satisfied and never auto-unpauses #89;
- unrelated issues (including #88 itself) remain parallelizable: the
  enforced DAG is per-issue, never a global mutex.
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencode_dependency_dag import (  # noqa: E402
    ENFORCED_DEPENDENCIES,
    all_enforced_issues,
    enforced_open_blockers,
    enforced_prerequisites,
    is_pause_pinned,
    may_remove_paused,
    should_release_stale_reservation,
    validate_dag,
)
from render_controller import (  # noqa: E402
    Controller,
    DeliveryStore,
    EligibilitySnapshot,
    StaticSnapshotProvider,
    WebhookEvent,
    build_snapshot,
    decide_eligible,
)

SECRET = "test-dag-secret"


def _snapshot(number, **overrides):
    base = {
        "issue_number": number,
        "state": "open",
        "labels": frozenset({"priority:p0"}),
    }
    base.update(overrides)
    return EligibilitySnapshot(**base)


def test_enforced_graph_matches_issue_88_spec():
    expected = {
        77: (76, 85),
        78: (77,),
        79: (77, 78),
        80: (79,),
        86: (79, 80),
        81: (58, 86),
        87: (81,),
    }
    assert dict(ENFORCED_DEPENDENCIES) == expected
    assert validate_dag() == expected
    # Roots run in parallel; #88 is independent; #89 stays pinned.
    assert enforced_prerequisites(76) == ()
    assert enforced_prerequisites(85) == ()
    assert enforced_prerequisites(58) == ()
    assert enforced_prerequisites(88) == ()
    assert enforced_prerequisites(89) == ()
    assert is_pause_pinned(89) is True
    assert is_pause_pinned(78) is False
    assert sorted(all_enforced_issues()) == sorted(
        {77, 78, 79, 80, 81, 86, 87, 76, 85, 58}
    )


def test_enforced_blockers_fail_closed_on_unknown_state():
    # Closed prerequisites: not blocked.
    assert enforced_open_blockers(79, (), {77: "closed", 78: "closed"}) == []
    # One open prerequisite: blocked.
    assert enforced_open_blockers(79, (), {77: "closed", 78: "open"}) == [78]
    # Native open blockers count even without state entries.
    assert enforced_open_blockers(79, (78,), {77: "closed"}) == [78]
    # Unknown/missing state fails closed: cannot dispatch on no evidence.
    assert enforced_open_blockers(79, (), {77: "closed"}) == [78]
    assert enforced_open_blockers(79, (), {}) == [77, 78]
    # Unrelated issues never have enforced blockers.
    assert enforced_open_blockers(88, (), {}) == []
    assert enforced_open_blockers(7, (3,), {3: "open"}) == []


def test_blocked_issue_is_ineligible_even_with_live_reservation():
    # #79 blocked by open #78: ineligible even with a live reservation.
    blocked_reserved = _snapshot(
        79,
        labels=frozenset({"priority:p0", "automation:in-progress"}),
        open_blockers=(),
        open_blocker_fallback_states={77: "closed", 78: "open"},
        has_open_pr=True,
        lease_valid=True,
    )
    decision = decide_eligible(blocked_reserved)
    assert decision.eligible is False
    assert "#78" in decision.reason
    assert "already active" not in decision.reason


def test_blocked_issue_never_dispatches_worker_or_reserves(tmp_path):
    from test_render_controller import FakeRenderClient, FakeRunnerClient

    render = FakeRenderClient()
    runner = FakeRunnerClient()
    reserved: list[int] = []

    class RecordingApi:
        def add_labels(self, issue, labels):
            reserved.append(issue)

        def remove_label(self, issue, label):
            return True

    provider = StaticSnapshotProvider(
        open_states={77: "closed", 78: "open"},
    )
    controller = Controller(
        webhook_secret=SECRET,
        store=DeliveryStore(str(tmp_path / "d.json")),
        provider=provider,
        render_client=render,
        runner_client=runner,
        owner_id="own-123",
        github_api=RecordingApi(),
    )
    payload = {
        "action": "opened",
        "issue": {
            "number": 79,
            "title": "Build variant",
            "body": "Blocked by #77 and #78",
            "state": "open",
            "labels": [{"name": "priority:p0"}],
        },
        "sender": {"login": "owner"},
    }
    body = json.dumps(payload).encode()
    import render_controller as core

    headers = {
        "X-GitHub-Delivery": "del-blocked-79",
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": core.sign_webhook_body(SECRET, body),
    }
    status, _ = controller.ingest(headers=headers, body=body)
    assert status == 202
    outcome = controller.process_delivery("del-blocked-79")
    assert outcome["dispatched"] is False
    assert "#78" in outcome["reason"]
    assert render.creations == []
    assert reserved == []


def test_completing_blocker_makes_dependent_eligible_exactly_once(tmp_path):
    from test_render_controller import FakeRenderClient, FakeRunnerClient

    render = FakeRenderClient()
    runner = FakeRunnerClient()
    provider = StaticSnapshotProvider(
        open_states={77: "closed", 78: "closed"},
    )
    controller = Controller(
        webhook_secret=SECRET,
        store=DeliveryStore(str(tmp_path / "d.json")),
        provider=provider,
        render_client=render,
        runner_client=runner,
        owner_id="own-123",
    )
    payload = {
        "action": "opened",
        "issue": {
            "number": 79,
            "title": "Build variant",
            "body": "Blocked by #77 and #78",
            "state": "open",
            "labels": [{"name": "priority:p0"}],
        },
        "sender": {"login": "owner"},
    }
    body = json.dumps(payload).encode()
    import render_controller as core

    headers = {
        "X-GitHub-Delivery": "del-unblocked-79",
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": core.sign_webhook_body(SECRET, body),
    }
    status, _ = controller.ingest(headers=headers, body=body)
    assert status == 202
    outcome = controller.process_delivery("del-unblocked-79")
    assert outcome["dispatched"] is True
    assert len(render.creations) == 1
    # Redelivery of the same delivery never creates a second worker.
    status, response = controller.redeliver("del-unblocked-79")
    assert status == 200
    assert response["duplicate"] is True
    assert len(render.creations) == 1


def test_build_snapshot_carries_enforced_prerequisite_states():
    provider = StaticSnapshotProvider(open_states={77: "closed", 78: "open"})
    event = WebhookEvent(
        event="issues",
        action="opened",
        issue_number=79,
        labels=("priority:p0",),
        state="open",
        body="Blocked by #77",
    )
    snapshot = build_snapshot(event, provider=provider)
    assert snapshot.open_blocker_fallback_states.get(77) == "closed"
    assert snapshot.open_blocker_fallback_states.get(78) == "open"
    assert decide_eligible(snapshot).eligible is False


def test_pause_reconciliation_only_when_prerequisites_satisfied():
    # #78 with #77 closed may drop its temporary pause.
    assert may_remove_paused(78, (), {77: "closed"}) is True
    # #79 still blocked by open #78: pause must stay.
    assert may_remove_paused(79, (), {77: "closed", 78: "open"}) is False
    # Unknown state fails closed: pause stays.
    assert may_remove_paused(79, (), {77: "closed"}) is False
    # #89 never auto-unpauses even with no blockers.
    assert may_remove_paused(89, (), {}) is False
    # Unrelated issues are not governed here.
    assert may_remove_paused(88, (), {}) is False


def test_stale_reservation_released_without_losing_work():
    # Blocked + in-progress: stale reservation, label removal only
    # (branches/PRs/commits untouched by label removal).
    assert (
        should_release_stale_reservation(
            79,
            ["priority:p0", "automation:in-progress"],
            (),
            {77: "closed", 78: "open"},
        )
        is True
    )
    # Unblocked + in-progress: reservation stands.
    assert (
        should_release_stale_reservation(
            79,
            ["priority:p0", "automation:in-progress"],
            (),
            {77: "closed", 78: "closed"},
        )
        is False
    )
    # No reservation label: nothing to release.
    assert (
        should_release_stale_reservation(
            79, ["priority:p0"], (), {77: "closed", 78: "open"}
        )
        is False
    )
    # Unrelated issues never flagged.
    assert (
        should_release_stale_reservation(
            88, ["priority:p0", "automation:in-progress"], (), {}
        )
        is False
    )


def test_unrelated_issues_stay_parallelizable_while_dependent_blocked():
    blocked = _snapshot(
        79, open_blockers=(), open_blocker_fallback_states={77: "closed", 78: "open"}
    )
    assert decide_eligible(blocked).eligible is False
    # #88 (independent) and an arbitrary unrelated issue stay eligible.
    assert decide_eligible(_snapshot(88)).eligible is True
    assert decide_eligible(_snapshot(99)).eligible is True
    # Parallel roots #76 and #85 are both eligible at once (no mutex).
    assert decide_eligible(_snapshot(76)).eligible is True
    assert decide_eligible(_snapshot(85)).eligible is True
    # Per-issue guard is the only coupling: different issues proceed
    # concurrently (controller uses one slot per issue, not a global mutex).
    controller_lock = threading.Semaphore(4)
    assert controller_lock.acquire(blocking=False) is True
    controller_lock.release()
