"""Tests for the issue #14 architecture-audit helpers.

Covers both unit behavior (fixtures) and live repository invariants. The live
checks are read-only and must pass on the current main branch; the documented
gap test for the missing #4 harness entrypoints asserts the gap explicitly
instead of failing CI with an unclear error.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from automation.runtime_lab_audit import (
    ALLOWED_MODELS,
    FALLBACK_MODEL,
    PREFERRED_MODEL,
    has_global_render_mutex,
    has_per_issue_opencode_concurrency,
    has_per_issue_render_concurrency,
    is_allowed_model,
    is_allowed_region,
    is_muse_model,
    load_control_plane,
    references_render_harness_scripts,
    render_harness_files_present,
    scheduler_max_attempts_default,
    scheduler_wip_default,
)

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")


def _read_workflow(name: str) -> str:
    with open(os.path.join(WORKFLOWS, name), "r", encoding="utf-8") as handle:
        return handle.read()


def test_allowed_regions_are_us_plus_singapore():
    for region in ("oregon", "ohio", "virginia", "singapore"):
        assert is_allowed_region(region)
        assert is_allowed_region(region.upper())


def test_frankfurt_is_forbidden_for_muse_workers():
    assert not is_allowed_region("frankfurt")
    assert not is_allowed_region("")
    assert not is_allowed_region(None)


def test_model_policy_prefers_muse_with_bunny_fallback():
    assert is_allowed_model(PREFERRED_MODEL)
    assert is_allowed_model(FALLBACK_MODEL)
    assert is_muse_model(PREFERRED_MODEL)
    assert not is_muse_model(FALLBACK_MODEL)
    assert not is_allowed_model("opencode/gpt-5")
    assert set(ALLOWED_MODELS) == {PREFERRED_MODEL, FALLBACK_MODEL}


def test_load_control_plane_accepts_both_backends(tmp_path=None):
    with tempfile.TemporaryDirectory() as directory:
        for backend in ("actions", "render-controller"):
            path = os.path.join(directory, "control-plane.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"execution_backend": "%s"}' % backend)
            assert load_control_plane(path)["execution_backend"] == backend


def test_load_control_plane_rejects_unknown_backend():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "control-plane.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"execution_backend": "direct"}')
        with pytest.raises(ValueError):
            load_control_plane(path)


def test_concurrency_text_helpers():
    per_issue = "concurrency:\n  group: runtime-lab-render-${{ inputs.issue_number }}"
    assert has_per_issue_render_concurrency(per_issue)
    assert not has_global_render_mutex(per_issue)
    assert has_global_render_mutex("concurrency:\n  group: runtime-lab-render")
    assert has_per_issue_opencode_concurrency(
        "group: opencode-${{ inputs.issue_number || github.run_id }}"
    )


def test_scheduler_text_helpers():
    text = (
        "WIP_LIMIT: ${{ vars.AUTOMATION_WIP_LIMIT || '4' }}\n"
        "MAX_DISPATCH_ATTEMPTS: ${{ vars.AUTOMATION_MAX_DISPATCH_ATTEMPTS || '4' }}"
    )
    assert scheduler_wip_default(text) == "4"
    assert scheduler_max_attempts_default(text) == "4"


# Live repository invariants (read-only; must hold on current main).


def test_live_control_plane_uses_temporary_actions_backend():
    config = load_control_plane(
        os.path.join(REPO_ROOT, "automation", "control-plane.json")
    )
    # Cutover to render-controller is owned by issue #12; until then the
    # temporary Actions harness backend must remain explicit and valid.
    assert config["execution_backend"] == "actions"


def test_live_scheduler_permits_four_concurrent_issues():
    text = _read_workflow("issue-scheduler.yml")
    assert scheduler_wip_default(text) == "4"
    assert scheduler_max_attempts_default(text) == "4"


def test_live_render_executor_has_no_global_mutex():
    text = _read_workflow("render-executor.yml")
    assert has_per_issue_render_concurrency(text)
    assert not has_global_render_mutex(text)


def test_live_opencode_workflow_is_per_issue_serialized():
    text = _read_workflow("opencode.yml")
    assert has_per_issue_opencode_concurrency(text)


def test_live_render_executor_references_harness_entrypoints():
    text = _read_workflow("render-executor.yml")
    assert references_render_harness_scripts(text)


def test_live_render_executor_pins_allowed_region_and_model():
    text = _read_workflow("render-executor.yml")
    assert "oregon" in text
    assert "frankfurt" not in text
    assert PREFERRED_MODEL in text


def test_live_missing_harness_gap_is_documented():
    """Issue #4 owns automation/render-job.sh and render-cleanup.sh.

    The Render executor workflow references both entrypoints, but neither
    file exists on main yet, so every render-executor run currently fails
    closed with "Missing automation/render-job.sh". This test records that
    known gap explicitly instead of letting CI fail obscurely.
    """
    presence = render_harness_files_present(REPO_ROOT)
    assert presence == {
        "automation/render-job.sh": False,
        "automation/render-cleanup.sh": False,
    }
