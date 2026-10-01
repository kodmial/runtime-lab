"""Enforced OpenCode-fork optimization dependency DAG (issue #88).

The Actions scheduler (``.github/workflows/continuum-issue-scheduler.yml``) and the
persistent controller (``automation/render_controller.py``) honor native
GitHub issue dependencies plus the Definition-of-Ready ``#N is completed``
fallback. Free-form body text such as ``Blocked by #76`` is NOT honored by
either path, which previously allowed #77 to be reserved before #76
completed.

This module is the single machine-readable source of truth for the real
optimization DAG. The controller merges these prerequisites into every
eligibility decision so a dependent issue cannot receive
``automation:in-progress`` (reservation/dispatch) while any enforced
prerequisite is still open -- regardless of body wording or native-API
availability. Native ``blockedBy`` edges remain the live mechanism the
Actions scheduler honors; this module documents the exact edge set that
must exist live and lets the controller enforce the same graph offline.

Required graph (issue #88 spec; #88 itself is independent):
- #77 blocked by #76, #85
- #78 blocked by #77
- #79 blocked by #77, #78
- #80 blocked by #79
- #86 blocked by #79, #80
- #81 blocked by #58, #86
- #87 blocked by #81
- #89 stays paused; enabled only if #81 misses the memory/reliability target

#76 and #85 may run in parallel. Unknown prerequisite state fails closed
(blocked) so a dependent can never dispatch on missing evidence. There is
no global mutex: issues outside this graph are unaffected and remain
parallelizable.
"""

from __future__ import annotations

from typing import Mapping, Sequence

# Dependent issue -> sorted tuple of prerequisite issue numbers.
ENFORCED_DEPENDENCIES: dict[int, tuple[int, ...]] = {
    77: (76, 85),
    78: (77,),
    79: (77, 78),
    80: (79,),
    86: (79, 80),
    81: (58, 86),
    87: (81,),
}

# Issues that must never be auto-unpaused by reconciliation. #89 is a
# conditional contingency only; it is enabled manually when #81 misses the
# memory/reliability target.
PAUSE_PINNED_ISSUES = frozenset({89})


def enforced_prerequisites(issue_number: int) -> tuple[int, ...]:
    """Return the enforced prerequisites for an issue (empty when none)."""
    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return ()
    return ENFORCED_DEPENDENCIES.get(number, ())


def all_enforced_issues() -> list[int]:
    """Return every issue number mentioned in the enforced graph, sorted."""
    names: set[int] = set(ENFORCED_DEPENDENCIES.keys())
    for prereqs in ENFORCED_DEPENDENCIES.values():
        names.update(prereqs)
    return sorted(names)


def validate_dag(
    graph: Mapping[int, Sequence[int]] | None = None,
) -> dict[int, tuple[int, ...]]:
    """Validate the DAG (acyclic, no self-edges, positive numbers)."""
    target = dict(graph) if graph is not None else dict(ENFORCED_DEPENDENCIES)
    normalized: dict[int, tuple[int, ...]] = {}
    for key, value in target.items():
        number = int(key)
        prereqs = tuple(sorted({int(item) for item in value}))
        if number <= 0 or any(item <= 0 for item in prereqs):
            raise ValueError("issue numbers must be positive integers")
        if number in prereqs:
            raise ValueError("issue #%d cannot depend on itself" % number)
        normalized[number] = prereqs
    # Depth-first cycle check over dependents -> prerequisites.
    visiting: set[int] = set()
    visited: set[int] = set()

    def _visit(node: int, stack: tuple[int, ...]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join(["#%d" % n for n in stack + (node,)])
            raise ValueError("dependency cycle detected: %s" % cycle)
        visiting.add(node)
        for prereq in normalized.get(node, ()):
            _visit(prereq, stack + (node,))
        visiting.discard(node)
        visited.add(node)

    for node in sorted(normalized):
        _visit(node, ())
    return normalized


def enforced_open_blockers(
    issue_number: int,
    open_blockers: Sequence[int] = (),
    open_states: Mapping[int, str] | None = None,
) -> list[int]:
    """Return the enforced prerequisites of an issue that are still open.

    A prerequisite counts as open when it appears in the native
    ``open_blockers`` list, when ``open_states`` reports ``"open"`` for it,
    or when its state is unknown/missing (fail closed: a dependent can
    never dispatch on missing evidence). Closed prerequisites are not
    blockers. Issues outside the enforced graph never have enforced
    blockers.
    """
    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return []
    prereqs = ENFORCED_DEPENDENCIES.get(number, ())
    if not prereqs:
        return []
    native = set()
    for item in open_blockers or ():
        try:
            native.add(int(item))
        except (TypeError, ValueError):
            continue
    states = dict(open_states or {})
    blocked: list[int] = []
    for prereq in prereqs:
        if prereq in native:
            blocked.append(prereq)
            continue
        state = str(states.get(prereq, "") or "").strip().lower()
        if state == "closed":
            continue
        # "open" or unknown/missing/anything-else fails closed.
        blocked.append(prereq)
    return sorted(blocked)


def is_pause_pinned(issue_number: int) -> bool:
    """True when an issue must never be auto-unpaused (#89)."""
    try:
        return int(issue_number) in PAUSE_PINNED_ISSUES
    except (TypeError, ValueError):
        return False


def may_remove_paused(
    issue_number: int,
    open_blockers: Sequence[int] = (),
    open_states: Mapping[int, str] | None = None,
) -> bool:
    """True when a temporary pause label may be removed for an issue.

    Allowed only when the issue has no enforced open blockers and is not
    pause-pinned (#89). Issues outside the enforced graph are not governed
    by this helper (returns False so unrelated pause handling is untouched).
    """
    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return False
    if number not in ENFORCED_DEPENDENCIES:
        return False
    if number in PAUSE_PINNED_ISSUES:
        return False
    return not enforced_open_blockers(number, open_blockers, open_states)


def should_release_stale_reservation(
    issue_number: int,
    labels: Sequence[str] = (),
    open_blockers: Sequence[int] = (),
    open_states: Mapping[int, str] | None = None,
) -> bool:
    """True when an in-progress label is stale because of open prerequisites.

    A blocked issue must never hold ``automation:in-progress``: the label
    is reservation state only, so removing it loses no useful work
    (branches/PRs/commits are untouched). Unblocked or unrelated issues are
    never flagged here.
    """
    names = {str(name) for name in (labels or [])}
    if "automation:in-progress" not in names:
        return False
    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return False
    if number not in ENFORCED_DEPENDENCIES:
        return False
    return bool(enforced_open_blockers(number, open_blockers, open_states))
