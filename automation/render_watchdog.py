"""Stale ephemeral Render worker reconciliation watchdog (issue #27).

Second line of defense for automation-owned ephemeral Render services in
cases where the process that owns normal cleanup is itself terminated
(hard cancellation, job timeout, runner crash, controller process crash,
host loss).

Safety invariants (fail closed everywhere):

- Only services provably owned by Runtime Lab automation are eligible.
  Ephemeral workers are named ``runtime-lab-issue<NUMBER>-<RUN_ID>`` (see
  ``service_name_for_attempt`` in ``automation/render_lifecycle.py``).
- The persistent ``runtime-lab-controller`` service is never selected.
- Unknown, malformed, manually-created, or ambiguous services are left
  untouched.
- A service is stale only after a configurable age threshold and only
  when there is no valid active ownership/lease signal.
- Reconciliation is delete-first. Suspension is only an emergency safety
  fallback while bounded deletion retries continue (mirrors
  ``automation/render-cleanup.sh`` and ``cleanup_worker`` in
  ``automation/render_controller.py``).
- Every attempted deletion is followed by authoritative absence
  verification (404/410). Unverified absence is reported as failure,
  never success.
- Reconciliation never provisions a replacement worker.
- Services are never deleted by broad prefix alone: the full
  automation-owned name shape and the age threshold are both validated.

Cleanup policy (retry bounds, success/verify statuses) is reused from
``automation/render_lifecycle.py`` rather than duplicated here.

Stdlib only. Pure unit tests require no Render credentials or network:
pass in-memory service dicts and a scripted cleanup client. The
component can later be invoked by the persistent controller or a
watchdog loop without redesign by wiring ``list_services`` output and
the existing Render worker client into :func:`reconcile_stale_workers`.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import (
        DELETE_MAX_ATTEMPTS,
        RENDER_DELETE_SUCCESS_STATUS,
        SUSPEND_FALLBACK_MAX_ATTEMPTS,
        is_deletion_verified,
        service_name_for_attempt,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import (  # type: ignore[no-redef]
        DELETE_MAX_ATTEMPTS,
        RENDER_DELETE_SUCCESS_STATUS,
        SUSPEND_FALLBACK_MAX_ATTEMPTS,
        is_deletion_verified,
        service_name_for_attempt,
    )

# ---------------------------------------------------------------------------
# Ownership: name shape.
# ---------------------------------------------------------------------------

# Must stay identical to the persistent controller identity in
# automation/render_controller.py (asserted by unit tests). The watchdog
# keeps a local copy so importing the heavier controller module is never
# required to run reconciliation.
CONTROLLER_SERVICE_NAME = "runtime-lab-controller"

# Full automation-owned ephemeral shape: runtime-lab-issue<NUMBER>-<RUN_ID>.
# The issue number is a positive integer without leading zeros (issue 0 and
# "01" are ambiguous and fail closed). The run suffix is 1-64 chars of
# letters, digits, and dashes, starting with an alphanumeric character;
# this covers numeric GitHub run ids ("999"), controller labels
# ("ctrl-<id>"), and attempt labels ("r1") while rejecting manual names
# with spaces, underscores, or other separators. Fullmatch only: a broad
# prefix such as "runtime-lab-issue" alone never qualifies.
EPHEMERAL_SERVICE_RE = re.compile(
    r"^runtime-lab-issue([1-9][0-9]*)-([A-Za-z0-9][A-Za-z0-9-]{0,63})$"
)

# Default age after which an unleased automation-owned worker counts as
# stale: 2 hours. Normal execution (45-minute runner timeout plus
# deploy/health/poll overhead) plus the 90-minute controller lease fit
# comfortably inside this window, so healthy workers are never selected.
# Always pass an explicit threshold in tests; production callers may tune
# this value but must keep it well above the worst-case healthy runtime.
DEFAULT_STALE_AFTER_SECONDS = 2 * 60 * 60

# Render delete/suspend/verify statuses shared with render-cleanup.sh and
# cleanup_worker: DELETE primary success, suspend fallback success, and
# authoritative absence proof. 404/410 on DELETE already means gone, but a
# final 404/410 verification read is still mandatory.
DELETE_GONE_STATUSES = frozenset(
    {RENDER_DELETE_SUCCESS_STATUS, 404, 410}
)
SUSPEND_OK_STATUSES = frozenset({202, 404, 410})


@dataclass(frozen=True)
class ServiceCorrelation:
    """Issue/attempt correlation extracted from an ephemeral service name."""

    issue_number: int
    run_id: str


@dataclass(frozen=True)
class ServiceClassification:
    """Ownership verdict for one Render service object."""

    owned: bool
    reason: str
    service_id: str = ""
    service_name: str = ""
    issue_number: int | None = None
    run_id: str = ""


@dataclass(frozen=True)
class StaleDecision:
    """Stale-eligibility verdict for one Render service object."""

    eligible: bool
    reason: str
    age_seconds: float | None = None
    issue_number: int | None = None
    run_id: str = ""


def parse_ephemeral_correlation(name: object) -> ServiceCorrelation | None:
    """Extract (issue_number, run_id) from an ephemeral worker name.

    Returns None for anything that is not exactly
    ``runtime-lab-issue<NUMBER>-<RUN_ID>`` (fail closed), including the
    persistent controller name, truncated prefixes, and malformed names.
    """
    if not isinstance(name, str) or not name:
        return None
    if name == CONTROLLER_SERVICE_NAME:
        return None
    match = EPHEMERAL_SERVICE_RE.fullmatch(name)
    if match is None:
        return None
    try:
        issue_number = int(match.group(1))
    except ValueError:
        return None
    if issue_number <= 0:
        return None
    return ServiceCorrelation(issue_number=issue_number, run_id=match.group(2))


def is_ephemeral_name(name: object) -> bool:
    """True only for the full automation-owned ephemeral name shape."""
    return parse_ephemeral_correlation(name) is not None


def _unwrap_service_dict(service: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the inner service object for wrapped Render API envelopes.

    Render list/create responses may wrap the service object as
    ``{"service": {...}}``. When that wrapper is present it is used;
    otherwise the mapping itself is the service object.
    """
    nested = service.get("service")
    if isinstance(nested, Mapping):
        return nested
    return service


def extract_service_name(service: object) -> str:
    """Extract the service name from a Render service object (or "")."""
    if not isinstance(service, Mapping):
        return ""
    inner = _unwrap_service_dict(service)
    for source in (inner, service):
        if not isinstance(source, Mapping):
            continue
        name = source.get("name")
        if isinstance(name, str) and name:
            return name
    return ""


def extract_service_id(service: object) -> str:
    """Extract the service id from a Render service object (or "")."""
    if not isinstance(service, Mapping):
        return ""
    inner = _unwrap_service_dict(service)
    for source in (inner, service):
        if not isinstance(source, Mapping):
            continue
        for key in ("id", "serviceId", "service_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def classify_service(service: object) -> ServiceClassification:
    """Decide whether a Render service is automation-owned and ephemeral.

    Fail-closed: missing/invalid names, the persistent controller, and
    any name that does not match the full ephemeral shape are reported
    as not owned with a reason and must be left untouched.
    """
    name = extract_service_name(service)
    service_id = extract_service_id(service)
    if not name:
        return ServiceClassification(
            owned=False,
            reason="missing or invalid service name; fail closed",
            service_id=service_id,
            service_name="",
        )
    if name == CONTROLLER_SERVICE_NAME:
        return ServiceClassification(
            owned=False,
            reason="persistent controller service is never eligible",
            service_id=service_id,
            service_name=name,
        )
    correlation = parse_ephemeral_correlation(name)
    if correlation is None:
        return ServiceClassification(
            owned=False,
            reason="name does not match automation-owned ephemeral shape",
            service_id=service_id,
            service_name=name,
        )
    if not service_id:
        return ServiceClassification(
            owned=False,
            reason="automation-shaped name but missing service id; fail closed",
            service_id="",
            service_name=name,
            issue_number=correlation.issue_number,
            run_id=correlation.run_id,
        )
    return ServiceClassification(
        owned=True,
        reason="automation-owned ephemeral worker",
        service_id=service_id,
        service_name=name,
        issue_number=correlation.issue_number,
        run_id=correlation.run_id,
    )


def is_automation_owned(service_or_name: object) -> bool:
    """True only for provably automation-owned ephemeral workers.

    Accepts either a Render service object or a bare service name.
    """
    if isinstance(service_or_name, str):
        return is_ephemeral_name(service_or_name)
    return classify_service(service_or_name).owned


# ---------------------------------------------------------------------------
# Timestamps and stale eligibility.
# ---------------------------------------------------------------------------

_CREATED_KEYS = ("createdAt", "created_at", "created")


def parse_iso_timestamp(value: object) -> float | None:
    """Parse a Render timestamp into epoch seconds (None when unusable).

    Accepts ISO-8601 strings (including trailing "Z"), epoch numbers,
    and datetime objects. Booleans, empty strings, garbage, and
    non-finite numbers return None so callers fail closed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        try:
            moment = value
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            stamp = moment.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
        if not math.isfinite(stamp) or stamp <= 0:
            return None
        return stamp
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp) or stamp <= 0:
            return None
        return stamp
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        stamp = float(text)
    except ValueError:
        pass
    else:
        if math.isfinite(stamp) and stamp > 0:
            return stamp
        return None
    iso = text
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    try:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        stamp = moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    return stamp


def extract_service_created_epoch(service: object) -> float | None:
    """Extract the service creation time as epoch seconds (None if unknown).

    Only creation timestamps are honored. Falling back to an update time
    would underestimate age and risk deleting a worker whose real
    creation time is unknown, so a missing/invalid creation time fails
    closed instead.
    """
    if not isinstance(service, Mapping):
        return None
    inner = _unwrap_service_dict(service)
    for source in (inner, service):
        if not isinstance(source, Mapping):
            continue
        for key in _CREATED_KEYS:
            if key in source:
                stamp = parse_iso_timestamp(source.get(key))
                if stamp is not None:
                    return stamp
                return None
    return None


def service_age_seconds(service: object, *, now: float | None = None) -> float | None:
    """Age of a Render service in seconds (None when not provable).

    Returns None for missing/invalid timestamps and for timestamps in
    the future (clock skew), so callers fail closed.
    """
    current = time.time() if now is None else float(now)
    if not math.isfinite(current) or current <= 0:
        return None
    created = extract_service_created_epoch(service)
    if created is None:
        return None
    age = current - created
    if not math.isfinite(age) or age < 0:
        return None
    return age


def validate_stale_after_seconds(value: object) -> float:
    """Validate the stale age threshold (raises for misconfiguration)."""
    if isinstance(value, bool):
        raise ValueError("stale_after_seconds must be a positive number")
    try:
        threshold = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "stale_after_seconds must be a positive number, got %r" % (value,)
        ) from exc
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError(
            "stale_after_seconds must be a positive number, got %r" % (value,)
        )
    return threshold


def build_active_check(
    *,
    active_issue_numbers: Sequence[int] = (),
    active_run_ids: Sequence[str] = (),
) -> Callable[[int, str], bool]:
    """Build an ownership/lease predicate from active issue/run sets.

    The returned callable takes ``(issue_number, run_id)`` and reports
    True while the worker is still actively owned (dispatched, leased,
    or in flight). Reconciliation treats active workers as never stale.
    Suitable for wiring ``controller.in_flight()`` keys and lease state
    without redesign.
    """

    active_issues = {int(number) for number in active_issue_numbers}
    active_runs = {str(run_id) for run_id in active_run_ids if str(run_id)}

    def _is_active(issue_number: int, run_id: str) -> bool:
        if issue_number in active_issues:
            return True
        if run_id and run_id in active_runs:
            return True
        return False

    return _is_active


def is_stale_eligible(
    service: object,
    *,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    is_active: Callable[[int, str], bool] | None = None,
    active_issue_numbers: Sequence[int] | None = None,
) -> StaleDecision:
    """Decide whether a Render service is a stale ephemeral worker.

    Eligible only when ALL hold: the service is provably
    automation-owned with a service id, no valid active
    ownership/lease signal covers it, its creation timestamp parses,
    and its age meets ``stale_after_seconds``. Everything else --
    including young workers, the controller, malformed names, missing
    timestamps, and active leases -- is not eligible (fail closed).
    """
    threshold = validate_stale_after_seconds(stale_after_seconds)
    classification = classify_service(service)
    if not classification.owned:
        return StaleDecision(
            eligible=False,
            reason="not eligible: %s" % classification.reason,
            age_seconds=None,
            issue_number=classification.issue_number,
            run_id=classification.run_id,
        )
    issue_number = classification.issue_number or 0
    run_id = classification.run_id
    if active_issue_numbers is not None and issue_number in set(
        int(number) for number in active_issue_numbers
    ):
        return StaleDecision(
            eligible=False,
            reason="active ownership signal for issue #%d" % issue_number,
            age_seconds=None,
            issue_number=issue_number,
            run_id=run_id,
        )
    if is_active is not None:
        try:
            active = bool(is_active(issue_number, run_id))
        except Exception:
            return StaleDecision(
                eligible=False,
                reason="active ownership check failed; fail closed",
                age_seconds=None,
                issue_number=issue_number,
                run_id=run_id,
            )
        if active:
            return StaleDecision(
                eligible=False,
                reason="active ownership/lease signal present",
                age_seconds=None,
                issue_number=issue_number,
                run_id=run_id,
            )
    age = service_age_seconds(service, now=now)
    if age is None:
        return StaleDecision(
            eligible=False,
            reason="missing or invalid creation timestamp; fail closed",
            age_seconds=None,
            issue_number=issue_number,
            run_id=run_id,
        )
    if age < threshold:
        return StaleDecision(
            eligible=False,
            reason="younger than stale threshold (age %.0fs < %.0fs)"
            % (age, threshold),
            age_seconds=age,
            issue_number=issue_number,
            run_id=run_id,
        )
    return StaleDecision(
        eligible=True,
        reason="stale automation-owned worker (age %.0fs >= %.0fs)"
        % (age, threshold),
        age_seconds=age,
        issue_number=issue_number,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Dry-run reconciliation plan.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedService:
    """One service entry in a dry-run reconciliation plan (no side effects)."""

    service_id: str
    service_name: str
    issue_number: int | None
    run_id: str
    age_seconds: float | None
    eligible: bool
    reason: str
    correlation: str


@dataclass(frozen=True)
class ReconciliationPlan:
    """Dry-run outcome: which services would be reconciled and why."""

    candidates: tuple[PlannedService, ...] = ()
    skipped: tuple[PlannedService, ...] = ()
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS

    @property
    def total(self) -> int:
        """Total services examined."""
        return len(self.candidates) + len(self.skipped)

    def summary(self) -> dict[str, Any]:
        """Structured counts suitable for controller logs/metrics."""
        return {
            "total": self.total,
            "candidates": len(self.candidates),
            "skipped": len(self.skipped),
            "stale_after_seconds": self.stale_after_seconds,
        }


def format_watchdog_correlation(
    *, service_id: str = "", issue_number: int | None = None, run_id: str = ""
) -> str:
    """One-line structured correlation for watchdog logs and records."""
    issue = "#%d" % issue_number if issue_number else "-"
    return "issue=%s run=%s worker=%s" % (
        issue, run_id or "-", service_id or "-"
    )


def plan_reconciliation(
    services: Sequence[Mapping[str, Any]],
    *,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    is_active: Callable[[int, str], bool] | None = None,
    active_issue_numbers: Sequence[int] | None = None,
) -> ReconciliationPlan:
    """Produce a dry-run reconciliation plan without touching any service.

    Every input service is classified as a deletion candidate or as
    skipped with a reason. No client calls are made and no service is
    created, suspended, or deleted.
    """
    threshold = validate_stale_after_seconds(stale_after_seconds)
    candidates: list[PlannedService] = []
    skipped: list[PlannedService] = []
    for service in services:
        decision = is_stale_eligible(
            service,
            now=now,
            stale_after_seconds=threshold,
            is_active=is_active,
            active_issue_numbers=active_issue_numbers,
        )
        classification = classify_service(service)
        entry = PlannedService(
            service_id=classification.service_id,
            service_name=classification.service_name,
            issue_number=decision.issue_number,
            run_id=decision.run_id,
            age_seconds=decision.age_seconds,
            eligible=decision.eligible,
            reason=decision.reason,
            correlation=format_watchdog_correlation(
                service_id=classification.service_id,
                issue_number=decision.issue_number,
                run_id=decision.run_id,
            ),
        )
        if decision.eligible:
            candidates.append(entry)
        else:
            skipped.append(entry)
    return ReconciliationPlan(
        candidates=tuple(candidates),
        skipped=tuple(skipped),
        stale_after_seconds=threshold,
    )


# ---------------------------------------------------------------------------
# Bounded cleanup execution (delete-first, suspend fallback, verify).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CleanupAttempt:
    """Outcome of the bounded delete-first cleanup for one service."""

    delete_status: int | None
    verify_status: int | None
    delete_attempts: int
    suspend_attempts: int
    suspend_fallback_used: bool
    cleanup_verified: bool


def cleanup_one_stale_worker(
    client: Any,
    service_id: str,
) -> CleanupAttempt:
    """Delete one stale worker with bounded retries and verify absence.

    Mirrors ``cleanup_worker`` (automation/render_controller.py) and
    ``automation/render-cleanup.sh``: DELETE is primary within
    ``DELETE_MAX_ATTEMPTS``, then authoritative verification (404/410);
    only when still present, an emergency suspend fallback runs within
    ``SUSPEND_FALLBACK_MAX_ATTEMPTS`` while bounded deletion retries
    continue, followed by a final verification read. Client exceptions
    count as failed attempts (status 0) so a flaky client cannot cause
    unbounded retries. Success requires verified absence.
    """
    delete_status: int | None = None
    verify_status: int | None = None
    delete_attempts = 0
    for _ in range(max(1, int(DELETE_MAX_ATTEMPTS))):
        try:
            delete_status = int(client.delete_service(service_id))
        except Exception:
            delete_status = 0
        delete_attempts += 1
        if delete_status in DELETE_GONE_STATUSES:
            break
    try:
        verify_status = int(client.verify_gone(service_id))
    except Exception:
        verify_status = 0
    if verify_status is not None and is_deletion_verified(verify_status):
        return CleanupAttempt(
            delete_status=delete_status,
            verify_status=verify_status,
            delete_attempts=delete_attempts,
            suspend_attempts=0,
            suspend_fallback_used=False,
            cleanup_verified=True,
        )
    # Emergency safety fallback only: suspend to stop burn, then keep
    # retrying deletion within bounds before the final verification.
    suspend_attempts = 0
    for _ in range(max(1, int(SUSPEND_FALLBACK_MAX_ATTEMPTS))):
        try:
            suspend_status = int(client.suspend_service(service_id))
        except Exception:
            suspend_status = 0
        suspend_attempts += 1
        if suspend_status in SUSPEND_OK_STATUSES:
            break
    for _ in range(max(1, int(DELETE_MAX_ATTEMPTS))):
        try:
            delete_status = int(client.delete_service(service_id))
        except Exception:
            delete_status = 0
        delete_attempts += 1
        if delete_status in DELETE_GONE_STATUSES:
            break
    try:
        verify_status = int(client.verify_gone(service_id))
    except Exception:
        verify_status = 0
    verified = verify_status is not None and is_deletion_verified(verify_status)
    return CleanupAttempt(
        delete_status=delete_status,
        verify_status=verify_status,
        delete_attempts=delete_attempts,
        suspend_attempts=suspend_attempts,
        suspend_fallback_used=True,
        cleanup_verified=verified,
    )


@dataclass(frozen=True)
class ServiceReconciliation:
    """Per-service reconciliation outcome for controller logs/metrics."""

    service_id: str
    service_name: str
    issue_number: int | None
    run_id: str
    age_seconds: float | None
    eligible: bool
    decision_reason: str
    executed: bool
    action: str
    delete_status: int | None = None
    verify_status: int | None = None
    delete_attempts: int = 0
    suspend_attempts: int = 0
    suspend_fallback_used: bool = False
    cleanup_verified: bool = False
    ok: bool = True
    correlation: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    """Structured outcome of one watchdog reconciliation pass."""

    actions: tuple[ServiceReconciliation, ...] = ()
    skipped: tuple[ServiceReconciliation, ...] = ()
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    dry_run: bool = False

    @property
    def total(self) -> int:
        """Total services examined."""
        return len(self.actions) + len(self.skipped)

    @property
    def deleted(self) -> tuple[ServiceReconciliation, ...]:
        """Stale workers whose absence was verified."""
        return tuple(entry for entry in self.actions if entry.ok)

    @property
    def failed(self) -> tuple[ServiceReconciliation, ...]:
        """Stale workers that could not be verified as gone."""
        return tuple(entry for entry in self.actions if not entry.ok)

    @property
    def ok(self) -> bool:
        """True when no attempted reconciliation failed."""
        return all(entry.ok for entry in self.actions)

    def summary(self) -> dict[str, Any]:
        """Structured counts suitable for controller logs/metrics."""
        return {
            "total": self.total,
            "stale": len(self.actions),
            "deleted_verified": len(self.deleted),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "dry_run": self.dry_run,
            "stale_after_seconds": self.stale_after_seconds,
            "ok": self.ok,
        }


def reconcile_stale_workers(
    services: Sequence[Mapping[str, Any]],
    *,
    client: Any = None,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    is_active: Callable[[int, str], bool] | None = None,
    active_issue_numbers: Sequence[int] | None = None,
    dry_run: bool = False,
) -> ReconciliationResult:
    """Reconcile stale ephemeral workers with bounded delete-first cleanup.

    Each input service is classified fail-closed; only stale,
    automation-owned workers without an active ownership/lease signal
    are deleted. Deletion uses the shared Render cleanup semantics
    (bounded retries, suspend only as emergency fallback, mandatory
    404/410 absence verification). No replacement worker is ever
    provisioned: this function only reads the ``delete`` / ``verify`` /
    ``suspend`` surface of ``client``.

    With ``dry_run=True`` no client calls are made; stale candidates are
    reported with action ``"would-delete"`` instead.
    """
    threshold = validate_stale_after_seconds(stale_after_seconds)
    if not dry_run and client is None:
        raise ValueError("a cleanup client is required unless dry_run=True")
    actions: list[ServiceReconciliation] = []
    skipped: list[ServiceReconciliation] = []
    for service in services:
        decision = is_stale_eligible(
            service,
            now=now,
            stale_after_seconds=threshold,
            is_active=is_active,
            active_issue_numbers=active_issue_numbers,
        )
        classification = classify_service(service)
        correlation = format_watchdog_correlation(
            service_id=classification.service_id,
            issue_number=decision.issue_number,
            run_id=decision.run_id,
        )
        if not decision.eligible:
            skipped.append(
                ServiceReconciliation(
                    service_id=classification.service_id,
                    service_name=classification.service_name,
                    issue_number=decision.issue_number,
                    run_id=decision.run_id,
                    age_seconds=decision.age_seconds,
                    eligible=False,
                    decision_reason=decision.reason,
                    executed=False,
                    action="skipped",
                    ok=True,
                    correlation=correlation,
                )
            )
            continue
        if dry_run:
            actions.append(
                ServiceReconciliation(
                    service_id=classification.service_id,
                    service_name=classification.service_name,
                    issue_number=decision.issue_number,
                    run_id=decision.run_id,
                    age_seconds=decision.age_seconds,
                    eligible=True,
                    decision_reason=decision.reason,
                    executed=False,
                    action="would-delete",
                    ok=True,
                    correlation=correlation,
                )
            )
            continue
        attempt = cleanup_one_stale_worker(client, classification.service_id)
        verified = attempt.cleanup_verified
        actions.append(
            ServiceReconciliation(
                service_id=classification.service_id,
                service_name=classification.service_name,
                issue_number=decision.issue_number,
                run_id=decision.run_id,
                age_seconds=decision.age_seconds,
                eligible=True,
                decision_reason=decision.reason,
                executed=True,
                action="deleted" if verified else "delete-failed",
                delete_status=attempt.delete_status,
                verify_status=attempt.verify_status,
                delete_attempts=attempt.delete_attempts,
                suspend_attempts=attempt.suspend_attempts,
                suspend_fallback_used=attempt.suspend_fallback_used,
                cleanup_verified=verified,
                ok=verified,
                correlation=correlation,
            )
        )
    return ReconciliationResult(
        actions=tuple(actions),
        skipped=tuple(skipped),
        stale_after_seconds=threshold,
        dry_run=dry_run,
    )


# Keep the canonical naming helper import referenced so future refactors
# that rename the lifecycle helper break loudly here instead of drifting.
assert callable(service_name_for_attempt)

__all__ = [
    "CONTROLLER_SERVICE_NAME",
    "DEFAULT_STALE_AFTER_SECONDS",
    "DELETE_GONE_STATUSES",
    "SUSPEND_OK_STATUSES",
    "EPHEMERAL_SERVICE_RE",
    "CleanupAttempt",
    "PlannedService",
    "ReconciliationPlan",
    "ReconciliationResult",
    "ServiceClassification",
    "ServiceCorrelation",
    "ServiceReconciliation",
    "StaleDecision",
    "build_active_check",
    "classify_service",
    "cleanup_one_stale_worker",
    "extract_service_created_epoch",
    "extract_service_id",
    "extract_service_name",
    "format_watchdog_correlation",
    "is_automation_owned",
    "is_ephemeral_name",
    "is_stale_eligible",
    "parse_ephemeral_correlation",
    "parse_iso_timestamp",
    "plan_reconciliation",
    "reconcile_stale_workers",
    "service_age_seconds",
    "validate_stale_after_seconds",
]
