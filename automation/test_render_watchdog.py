"""Tests for the stale ephemeral Render worker watchdog (issue #27).

Pure unit tests: no Render credentials, no network. Covers the required
fault/safety cases from the issue specification.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render_watchdog import (  # noqa: E402
    CONTROLLER_SERVICE_NAME,
    DEFAULT_STALE_AFTER_SECONDS,
    build_active_check,
    classify_service,
    format_watchdog_correlation,
    is_automation_owned,
    is_stale_eligible,
    parse_ephemeral_correlation,
    parse_iso_timestamp,
    plan_reconciliation,
    reconcile_stale_workers,
    service_age_seconds,
    validate_stale_after_seconds,
)

NOW = 1_800_000_000.0
THRESHOLD = 3600.0


def _iso(age_seconds):
    return (
        datetime.fromtimestamp(NOW - age_seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _service(name, age_seconds=None, service_id="srv-1", timestamp_key="createdAt",
             raw_timestamp=None, extra=None):
    body = {"id": service_id, "name": name}
    if raw_timestamp is not None:
        body[timestamp_key] = raw_timestamp
    elif age_seconds is not None:
        body[timestamp_key] = _iso(age_seconds)
    if extra:
        body.update(extra)
    return body


# ---------------------------------------------------------------------------
# Scripted cleanup client (no network).
# ---------------------------------------------------------------------------

class ScriptedCleanupClient:
    """Fake Render cleanup surface with scripted statuses per call."""

    def __init__(self, delete_script=(), verify_script=(), suspend_script=()):
        self.delete_script = list(delete_script) or [204]
        self.verify_script = list(verify_script) or [404]
        self.suspend_script = list(suspend_script) or [202]
        self.deletes = []
        self.verifies = []
        self.suspends = []
        assert not hasattr(self, "create_service")

    def _next(self, script, calls):
        index = min(len(calls) - 1, len(script) - 1)
        return script[max(0, index)]

    def delete_service(self, service_id):
        self.deletes.append(service_id)
        return self._next(self.delete_script, self.deletes)

    def verify_gone(self, service_id):
        self.verifies.append(service_id)
        return self._next(self.verify_script, self.verifies)

    def suspend_service(self, service_id):
        self.suspends.append(service_id)
        return self._next(self.suspend_script, self.suspends)


# ---------------------------------------------------------------------------
# Classification: ownership and correlation.
# ---------------------------------------------------------------------------

def test_valid_old_worker_is_selected():
    service = _service("runtime-lab-issue27-run-abc123", 7200, service_id="srv-old")
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD)
    assert decision.eligible is True
    assert decision.issue_number == 27
    assert decision.run_id == "run-abc123"
    assert decision.age_seconds == pytest.approx(7200, abs=2)


def test_valid_but_young_worker_is_not_selected():
    service = _service("runtime-lab-issue27-999", 60, service_id="srv-young")
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD)
    assert decision.eligible is False
    assert "younger" in decision.reason


def test_controller_service_is_never_selected():
    old = _service(CONTROLLER_SERVICE_NAME, 10 * 86400, service_id="srv-ctrl")
    assert CONTROLLER_SERVICE_NAME == "runtime-lab-controller"
    assert classify_service(old).owned is False
    decision = is_stale_eligible(old, now=NOW, stale_after_seconds=THRESHOLD)
    assert decision.eligible is False
    plan = plan_reconciliation([old], now=NOW, stale_after_seconds=THRESHOLD)
    assert plan.candidates == ()
    assert len(plan.skipped) == 1


@pytest.mark.parametrize("name", [
    "runtime-lab-controller",
    "runtime-lab-issue27",          # missing run suffix
    "runtime-lab-issue-27",         # missing issue digits
    "runtime-lab-issue-abc",        # non-numeric issue
    "runtime-lab-issue0-abc",       # issue zero is invalid
    "runtime-lab-issue01-abc",      # leading zero is ambiguous
    "runtime-lab-issue27-",         # empty run suffix
    "runtime-lab-issue27_abc",      # wrong separator
    "runtime-lab-issue27 abc",      # space in name
    "runtime-lab-issueX-1",         # non-numeric issue with prefix look
    "runtime-lab-issuer-27-1",      # similar prefix, wrong shape
    "runtime-lab-issue27-foo_bar",  # underscore in suffix fails closed
    "RUNTIME-LAB-ISSUE27-ABC",      # case-sensitive shape
    "runtime-lab-issue27-abc!",     # illegal suffix character
    "runtime-lab",                  # bare prefix alone
    "runtime-lab-issue",            # broad prefix alone
    "runtime-lab-issue27-abc/nested",
    "",
])
def test_similar_prefix_and_malformed_names_are_never_selected(name):
    assert parse_ephemeral_correlation(name) is None
    assert is_automation_owned(name) is False
    service = _service(name or " ", 7200, service_id="srv-mal")
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD)
    assert decision.eligible is False


def test_unrelated_render_service_is_never_selected():
    service = _service("customer-production-api", 30 * 86400, service_id="srv-cust")
    assert classify_service(service).owned is False
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD)
    assert decision.eligible is False


@pytest.mark.parametrize("timestamp_kwargs", [
    {"age_seconds": None},  # missing timestamp key entirely
    {"age_seconds": None, "raw_timestamp": ""},
    {"age_seconds": None, "raw_timestamp": "not-a-timestamp"},
    {"age_seconds": None, "raw_timestamp": "2026-13-99T99:99:99Z"},
    {"age_seconds": None, "raw_timestamp": True},
    {"age_seconds": None, "raw_timestamp": float("nan")},
    {"age_seconds": None, "raw_timestamp": -5},
])
def test_missing_or_invalid_timestamp_fails_closed(timestamp_kwargs):
    service = _service("runtime-lab-issue27-999", service_id="srv-ts",
                       **timestamp_kwargs)
    assert service_age_seconds(service, now=NOW) is None
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD)
    assert decision.eligible is False
    assert "timestamp" in decision.reason


def test_future_timestamp_fails_closed():
    service = _service("runtime-lab-issue27-999", service_id="srv-future",
                       raw_timestamp=NOW + 3600)
    assert service_age_seconds(service, now=NOW) is None
    assert is_stale_eligible(
        service, now=NOW, stale_after_seconds=THRESHOLD).eligible is False


def test_correlation_extraction():
    correlation = parse_ephemeral_correlation("runtime-lab-issue27-ctrl-delabcd")
    assert correlation is not None
    assert correlation.issue_number == 27
    assert correlation.run_id == "ctrl-delabcd"
    assert parse_ephemeral_correlation(
        "runtime-lab-issue4-999").issue_number == 4


def test_active_lease_prevents_selection():
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-active")
    check = build_active_check(active_issue_numbers=[27])
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD,
                                 is_active=check)
    assert decision.eligible is False
    assert "active" in decision.reason
    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD,
                                 active_issue_numbers=[27])
    assert decision.eligible is False

    def _boom(_issue, _run):
        raise RuntimeError("lease store unavailable")

    decision = is_stale_eligible(service, now=NOW, stale_after_seconds=THRESHOLD,
                                 is_active=_boom)
    assert decision.eligible is False  # lease check failure fails closed


def test_timestamp_shapes_are_accepted():
    assert parse_iso_timestamp(_iso(100)) == pytest.approx(NOW - 100, abs=2)
    assert parse_iso_timestamp(NOW - 50) == pytest.approx(NOW - 50, abs=1)
    assert parse_iso_timestamp(
        datetime.fromtimestamp(NOW - 10, tz=timezone.utc)) == pytest.approx(
        NOW - 10, abs=1)
    assert parse_iso_timestamp(None) is None


def test_wrapped_service_envelope_is_supported():
    inner = _service("runtime-lab-issue27-999", 7200, service_id="srv-wrap")
    wrapped = {"service": inner, "cursor": "next"}
    assert classify_service(wrapped).owned is True
    assert is_stale_eligible(
        wrapped, now=NOW, stale_after_seconds=THRESHOLD).eligible is True


def test_threshold_validation():
    assert validate_stale_after_seconds(60) == 60
    for bad in (0, -1, float("nan"), float("inf"), "soon", None, True):
        with pytest.raises(ValueError):
            validate_stale_after_seconds(bad)


# ---------------------------------------------------------------------------
# Execution: bounded delete-first cleanup with verification.
# ---------------------------------------------------------------------------

def test_deletion_succeeds_and_verifies_absence():
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-del")
    client = ScriptedCleanupClient(delete_script=[204], verify_script=[404])
    result = reconcile_stale_workers([service], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD)
    assert result.ok is True
    assert len(result.actions) == 1
    entry = result.actions[0]
    assert entry.action == "deleted"
    assert entry.cleanup_verified is True
    assert entry.delete_status == 204
    assert entry.verify_status == 404
    assert client.deletes == ["srv-del"]
    assert client.verifies == ["srv-del"]
    assert client.suspends == []  # clean delete+verify never suspends


def test_already_gone_delete_counts_as_verified():
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-gone")
    client = ScriptedCleanupClient(delete_script=[404], verify_script=[410])
    result = reconcile_stale_workers([service], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD)
    assert result.ok is True
    assert result.actions[0].action == "deleted"
    assert client.suspends == []


def test_deletion_failure_uses_suspend_fallback_with_bounded_retry():
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-flaky")
    client = ScriptedCleanupClient(
        delete_script=[500] * 5 + [204],
        verify_script=[200, 404],
        suspend_script=[202],
    )
    result = reconcile_stale_workers([service], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD)
    assert result.ok is True
    entry = result.actions[0]
    assert entry.action == "deleted"
    assert entry.suspend_fallback_used is True
    assert client.suspends == ["srv-flaky"]
    from render_lifecycle import DELETE_MAX_ATTEMPTS  # noqa: E402
    assert len(client.deletes) <= 2 * DELETE_MAX_ATTEMPTS
    assert len(client.deletes) == 6  # 5 failed + 1 post-fallback success
    assert entry.verify_status == 404


def test_unverifiable_absence_reports_failure_never_success():
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-stuck")
    client = ScriptedCleanupClient(
        delete_script=[204], verify_script=[200], suspend_script=[202])
    result = reconcile_stale_workers([service], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD)
    assert result.ok is False
    assert len(result.failed) == 1
    entry = result.actions[0]
    assert entry.action == "delete-failed"
    assert entry.cleanup_verified is False
    assert entry.ok is False
    assert entry.verify_status == 200
    assert client.suspends  # emergency fallback was attempted


def test_retries_are_bounded_when_everything_fails():
    from render_lifecycle import (  # noqa: E402
        DELETE_MAX_ATTEMPTS,
        SUSPEND_FALLBACK_MAX_ATTEMPTS,
    )
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-dead")
    client = ScriptedCleanupClient(
        delete_script=[500], verify_script=[200], suspend_script=[500])
    result = reconcile_stale_workers([service], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD)
    assert result.ok is False
    assert len(client.deletes) == 2 * DELETE_MAX_ATTEMPTS
    assert len(client.suspends) == SUSPEND_FALLBACK_MAX_ATTEMPTS
    assert len(client.verifies) == 2


def test_client_exceptions_stay_bounded():
    class ExplodingClient:
        def __init__(self):
            self.deletes = []
            self.verifies = []
            self.suspends = []

        def delete_service(self, service_id):
            self.deletes.append(service_id)
            raise ConnectionError("boom")

        def verify_gone(self, service_id):
            self.verifies.append(service_id)
            raise ConnectionError("boom")

        def suspend_service(self, service_id):
            self.suspends.append(service_id)
            raise ConnectionError("boom")

    from render_lifecycle import (  # noqa: E402
        DELETE_MAX_ATTEMPTS,
        SUSPEND_FALLBACK_MAX_ATTEMPTS,
    )
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-exp")
    client = ExplodingClient()
    result = reconcile_stale_workers([service], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD)
    assert result.ok is False
    assert len(client.deletes) == 2 * DELETE_MAX_ATTEMPTS
    assert len(client.suspends) == SUSPEND_FALLBACK_MAX_ATTEMPTS


def test_multiple_stale_workers_are_independently_handled_without_creation():
    stale_one = _service("runtime-lab-issue27-aaa", 7200, service_id="srv-1")
    stale_two = _service("runtime-lab-issue28-bbb", 9600, service_id="srv-2")
    young = _service("runtime-lab-issue29-ccc", 60, service_id="srv-3")
    foreign = _service("someone-else", 99999, service_id="srv-4")
    client = ScriptedCleanupClient(delete_script=[204], verify_script=[404])
    result = reconcile_stale_workers(
        [stale_one, stale_two, young, foreign],
        client=client, now=NOW, stale_after_seconds=THRESHOLD)
    assert result.ok is True
    assert {entry.service_id for entry in result.actions} == {"srv-1", "srv-2"}
    assert {entry.service_id for entry in result.skipped} == {"srv-3", "srv-4"}
    assert sorted(client.deletes) == ["srv-1", "srv-2"]
    assert not hasattr(client, "create_service")
    summary = result.summary()
    assert summary == {
        "total": 4,
        "stale": 2,
        "deleted_verified": 2,
        "failed": 0,
        "skipped": 2,
        "dry_run": False,
        "stale_after_seconds": THRESHOLD,
        "ok": True,
    }


def test_dry_run_plan_makes_no_client_calls():
    stale = _service("runtime-lab-issue27-999", 7200, service_id="srv-1")
    young = _service("runtime-lab-issue28-aaa", 60, service_id="srv-2")
    plan = plan_reconciliation([stale, young], now=NOW,
                               stale_after_seconds=THRESHOLD)
    assert [entry.service_id for entry in plan.candidates] == ["srv-1"]
    assert [entry.service_id for entry in plan.skipped] == ["srv-2"]
    assert plan.summary() == {
        "total": 2, "candidates": 1, "skipped": 1,
        "stale_after_seconds": THRESHOLD,
    }

    client = ScriptedCleanupClient()
    result = reconcile_stale_workers([stale, young], client=client, now=NOW,
                                     stale_after_seconds=THRESHOLD, dry_run=True)
    assert result.dry_run is True
    assert result.actions[0].action == "would-delete"
    assert result.actions[0].executed is False
    assert result.skipped[0].action == "skipped"
    assert client.deletes == [] and client.verifies == [] and client.suspends == []


def test_cleanup_requires_a_client_outside_dry_run():
    service = _service("runtime-lab-issue27-999", 7200, service_id="srv-1")
    with pytest.raises(ValueError):
        reconcile_stale_workers([service], client=None, now=NOW,
                                stale_after_seconds=THRESHOLD, dry_run=False)


def test_correlation_format_for_logs():
    text = format_watchdog_correlation(
        service_id="srv-1", issue_number=27, run_id="abc")
    assert "issue=#27" in text and "srv-1" in text and "abc" in text


# ---------------------------------------------------------------------------
# Policy reuse and no-provisioning invariants.
# ---------------------------------------------------------------------------

def test_reuses_lifecycle_cleanup_policy():
    import render_lifecycle as lifecycle  # noqa: E402
    import render_watchdog as watchdog  # noqa: E402
    assert watchdog.DELETE_MAX_ATTEMPTS == lifecycle.DELETE_MAX_ATTEMPTS == 5
    assert (watchdog.SUSPEND_FALLBACK_MAX_ATTEMPTS
            == lifecycle.SUSPEND_FALLBACK_MAX_ATTEMPTS <= 2)
    assert 204 in watchdog.DELETE_GONE_STATUSES
    assert lifecycle.is_deletion_verified(404)
    assert lifecycle.is_deletion_verified(410)
    assert not lifecycle.is_deletion_verified(200)
    # A bare delete success without 404/410 verification is not success.
    assert lifecycle.deletion_succeeded(204, 404) is True
    assert lifecycle.deletion_succeeded(204, 200) is False


def test_controller_identity_matches():
    from render_controller import (  # noqa: E402
        CONTROLLER_SERVICE_NAME as CONTROLLER_CORE_NAME,
    )
    assert CONTROLLER_SERVICE_NAME == CONTROLLER_CORE_NAME


def test_watchdog_never_provisions_replacement_workers():
    source = (Path(__file__).resolve().parent / "render_watchdog.py").read_text(
        encoding="utf-8")
    assert "create_service" not in source
    assert "import subprocess" not in source
    assert "urllib" not in source
    assert DEFAULT_STALE_AFTER_SECONDS >= 3600


def test_no_live_service_created_by_this_module():
    # Static guard: the watchdog module performs no network or Render
    # provisioning calls at import time and exposes no creation API.
    import render_watchdog as watchdog  # noqa: E402
    assert not hasattr(watchdog, "create_service")
    assert not hasattr(watchdog, "provision_worker")
    assert time.time() > 0  # sanity: suite runs offline with fake clocks
