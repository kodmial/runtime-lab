"""Regression tests for the OpenCode fork-maintenance contract (issue #87).

Offline, stdlib-first, no network or git mutations. Verifies the
Definition of Done as encoded in runtime-lab:

- the upstream remote/base revision and the small intentional delta set
  are recorded and consistent with the fork baseline and variant specs;
- a deterministic upstream-sync/rebase procedure exists (no mutable
  refs, throwaway eval branch, per-delta reapplication, rebuild +
  correctness + smoke + budget gates);
- the lightweight artifact is rebuilt after an upstream refresh;
- focused coding-agent correctness plus the memory smoke benchmark run
  after sync, and over-budget peaks block the merge (never auto-merge);
- upstream removal of a forked subsystem is detected so obsolete
  patches can be dropped;
- the patch/delta inventory is concise and by purpose;
- refresh failures render an actionable Runtime Lab task.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from automation.opencode_fork_maintenance import (
    assert_merge_allowed,
    build_commands,
    check_consistent_with_baseline,
    check_consistent_with_specs,
    correctness_plan,
    delta_by_id,
    delta_ids,
    detect_obsolete_deltas,
    evaluate_sync_budget,
    load_maintenance,
    memory_smoke_plan,
    render_delta_inventory,
    render_refresh_failure_task,
    sync_plan,
    sync_steps,
    validate_delta,
    validate_maintenance,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORK_SHA = "9000e7fc8d96c845512f7c73122431418a71d4e4"
UPSTREAM_BASE = "75e1e7ae310dc36c86c920e8997d0e1181e24a88"
CANDIDATE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _maintenance() -> dict:
    return load_maintenance()


def _baseline() -> dict:
    with open(
        os.path.join(REPO_ROOT, "automation", "opencode-fork.baseline.json"),
        encoding="utf-8",
    ) as handle:
        return json.load(handle)


def _spec(name: str) -> dict:
    with open(
        os.path.join(REPO_ROOT, "automation", name), encoding="utf-8"
    ) as handle:
        return json.load(handle)


def test_inventory_loads_and_records_remote_and_deltas():
    data = _maintenance()
    assert data["fork_repo"] == "kodmial/opencode"
    assert data["upstream_repo"] == "anomalyco/opencode"
    assert "anomalyco/opencode" in data["upstream_remote"]
    assert data["upstream_base_commit"] == UPSTREAM_BASE
    assert data["pinned_opencode_version"] == "1.18.33"
    assert delta_ids(data) == [
        "D1-source-stripped",
        "D2-bounded-output",
        "D3-direct-headless",
    ]
    assert set(data["sync_order"]) == set(delta_ids(data))


def test_inventory_consistent_with_baseline_and_specs():
    data = _maintenance()
    check_consistent_with_baseline(data, _baseline())
    check_consistent_with_specs(
        data,
        _spec("opencode-lite.spec.json"),
        _spec("opencode-bounds.spec.json"),
        _spec("opencode-artifacts.spec.json"),
    )
    bad = copy.deepcopy(data)
    bad["upstream_base_commit"] = "0" * 40
    with pytest.raises(ValueError):
        check_consistent_with_baseline(bad, _baseline())


def test_delta_validation_rejects_empty_probes_and_purpose():
    data = _maintenance()
    delta = copy.deepcopy(delta_by_id(data, "D1-source-stripped"))
    assert delta["patch_doc"].endswith(".md")
    assert delta["touches"]["added"] or delta["touches"]["modified"]
    bad = copy.deepcopy(delta)
    bad["upstream_probes"] = []
    with pytest.raises(ValueError):
        validate_delta(bad)
    bad = copy.deepcopy(delta)
    bad["purpose"] = "  "
    with pytest.raises(ValueError):
        validate_delta(bad)
    with pytest.raises(ValueError):
        delta_by_id(data, "D9-nope")


def test_sync_steps_deterministic_no_mutable_refs():
    data = _maintenance()
    steps = sync_steps(data, CANDIDATE, FORK_SHA)
    text = "\n".join(steps)
    assert "upstream" in text.lower()
    assert UPSTREAM_BASE in text
    assert CANDIDATE in text
    assert "fork-sync/%s" % CANDIDATE[:12] in text
    for delta_id in delta_ids(data):
        assert delta_id in text
    lowered = text.lower()
    assert "checkout latest" not in lowered
    assert "checkout main" not in text
    assert "checkout --" not in text
    # Same inputs render the same procedure (no wall clock, no scratch).
    assert sync_steps(data, CANDIDATE, FORK_SHA) == steps
    plan = sync_plan(data, CANDIDATE, FORK_SHA)
    assert plan["upstream_candidate_sha"] == CANDIDATE
    assert plan["delta_order"] == list(data["sync_order"])
    assert "budget-gate" in plan["stages"]
    with pytest.raises(ValueError):
        sync_steps(data, "main")
    with pytest.raises(ValueError):
        sync_steps(data, UPSTREAM_BASE, FORK_SHA)


def test_rebuild_covers_lightweight_artifacts():
    data = _maintenance()
    commands = build_commands(data)
    joined = "\n".join(commands)
    assert "bun install --frozen-lockfile" in joined
    assert "build-coding" in joined
    assert "build-lite" in joined
    assert "build-direct" in joined
    assert "build.ts" in joined


def test_correctness_and_smoke_plans():
    data = _maintenance()
    correctness = correctness_plan(data)
    assert "automation/test_opencode_fork_maintenance.py" in correctness["pytest_subset"]
    assert "automation/test_opencode_lite.py" in correctness["pytest_subset"]
    assert "q1-small-edit" in correctness["representative_task"]
    smoke = memory_smoke_plan(data, "fork-sync-test")
    assert "memory_benchmark.py" in smoke["hermetic_command"]
    assert "fork-sync-test" in smoke["hermetic_command"]


def test_budget_gate_blocks_over_limit_merge():
    data = _maintenance()
    limit = data["budgets"]["hard_limit_bytes"]
    target = data["budgets"]["target_bytes"]
    assert target < limit
    ok = evaluate_sync_budget(target - 1, data)
    assert ok["passes_target"] is True
    assert ok["merge_blocked"] is False
    assert_merge_allowed(target - 1, data)
    reviewable = evaluate_sync_budget(limit - 1, data)
    assert reviewable["passes_target"] is False
    assert reviewable["passes_limit"] is True
    assert reviewable["merge_blocked"] is False
    assert_merge_allowed(limit - 1, data)
    blocked = evaluate_sync_budget(limit + 1, data)
    assert blocked["passes_limit"] is False
    assert blocked["merge_blocked"] is True
    with pytest.raises(ValueError):
        assert_merge_allowed(limit + 1, data)


def test_obsolete_scan_detects_upstream_removal():
    data = _maintenance()
    all_probes: list[str] = []
    for delta in data["deltas"]:
        all_probes += list(delta["upstream_probes"])
    active = detect_obsolete_deltas(all_probes, data)
    assert [row["status"] for row in active] == ["active"] * 3
    gone = detect_obsolete_deltas(["unrelated/file.ts"], data)
    assert [row["status"] for row in gone] == ["obsolete"] * 3
    assert "can be dropped" in gone[0]["reason"]
    # Partial presence (subsystem moved) needs review, never silent.
    first = data["deltas"][0]
    partial = detect_obsolete_deltas([first["upstream_probes"][0]], data)
    by_id = {row["delta_id"]: row for row in partial}
    assert by_id[first["id"]]["status"] == "needs-review"
    with pytest.raises(ValueError):
        detect_obsolete_deltas("not-a-list", data)  # type: ignore[arg-type]


def test_inventory_rendered_by_purpose():
    data = _maintenance()
    text = render_delta_inventory(data)
    for delta_id in delta_ids(data):
        assert delta_id in text
    for delta in data["deltas"]:
        assert delta["purpose"] in text
        assert delta["patch_doc"] in text
    assert "Sync order" in text


def test_refresh_failure_task_is_actionable():
    data = _maintenance()
    task = render_refresh_failure_task(
        data, "rebuild-artifacts", FORK_SHA, CANDIDATE, "bun build exit 1: ..."
    )
    assert "rebuild-artifacts" in task["title"]
    assert FORK_SHA[:12] in task["title"]
    assert CANDIDATE[:12] in task["title"]
    assert "Evidence" in task["body"]
    assert "bun build exit 1" in task["body"]
    assert "never silently left stale" in task["body"]
    assert task["labels"] == ["priority:p1"]
    with pytest.raises(ValueError):
        render_refresh_failure_task(
            data, "no-such-stage", FORK_SHA, CANDIDATE, "evidence"
        )
    with pytest.raises(ValueError):
        render_refresh_failure_task(
            data, "rebuild-artifacts", FORK_SHA, CANDIDATE, "  "
        )


def test_fork_sync_workflow_provisions_pytest_before_correctness_gate():
    # Regression for runtime-lab#219 (run 37204786294): the refresh check
    # failed at "Focused coding-agent correctness gate" with
    # "No module named pytest" because ubuntu-latest does not preinstall
    # it. The workflow must set up Python and install pytest first.
    workflow = os.path.join(
        REPO_ROOT, ".github", "workflows", "opencode-fork-sync.yml"
    )
    with open(workflow, encoding="utf-8") as handle:
        text = handle.read()
    assert "setup-python" in text
    assert "pip install" in text and "pytest" in text
    setup_pos = text.index("setup-python")
    pip_pos = text.index("pip install")
    gate_pos = text.index("Focused coding-agent correctness gate")
    pytest_pos = text.index("python3 -m pytest")
    assert setup_pos < gate_pos
    assert pip_pos < pytest_pos


def test_maintenance_json_referenced_specs_exist():
    data = _maintenance()
    for delta in data["deltas"]:
        patch = os.path.join(REPO_ROOT, delta["patch_doc"])
        assert os.path.isfile(patch), "missing patch doc %r" % patch
    for name in (
        "opencode-fork.baseline.json",
        "opencode-lite.spec.json",
        "opencode-bounds.spec.json",
        "opencode-artifacts.spec.json",
    ):
        assert os.path.isfile(os.path.join(REPO_ROOT, "automation", name))
