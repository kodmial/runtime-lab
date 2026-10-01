"""Tests for the issue #14 architecture-audit helpers.

Covers both unit behavior (fixtures) and live repository invariants. The live
checks are read-only and must pass on the current main branch.

Since commit fbf4b79 the five core automation workflows are thin Continuum
caller stubs, so the live checks are written against the *caller boundary*
rather than against job bodies that no longer live in this repository:
each one asserts which reusable workflow the stub delegates to, which inputs
it forwards bare, and that nothing local reintroduces what Continuum now
owns (a global concurrency mutex, a pinned region/model, a hardcoded harness
path). See ``continuum_stub_contract`` for the parser behind them.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, os.path.join(REPO_ROOT, "automation", "tests"))
sys.path.insert(0, REPO_ROOT)

from automation.render_lifecycle import (  # noqa: E402
    ALLOWED_WORKER_REGIONS,
    FORBIDDEN_WORKER_REGIONS,
    RegionPolicyError,
    validate_worker_region,
)
from automation.runtime_lab_audit import (  # noqa: E402
    ALLOWED_MODELS,
    ALLOWED_RENDER_REGIONS,
    FALLBACK_MODEL,
    PREFERRED_MODEL,
    RENDER_HARNESS_FILES,
    has_global_render_mutex,
    has_per_issue_opencode_concurrency,
    has_per_issue_render_concurrency,
    has_serialized_render_concurrency,
    has_valid_render_concurrency,
    is_allowed_model,
    is_allowed_region,
    is_muse_model,
    load_control_plane,
    references_render_harness_scripts,
    render_harness_files_present,
    scheduler_max_attempts_default,
    scheduler_wip_default,
)
from continuum_stub_contract import (  # noqa: E402
    caller_stub_text,
    local_concurrency_groups,
    parse_caller_stub,
)

WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")


def _read_workflow(name: str) -> str:
    with open(os.path.join(WORKFLOWS, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _stub(name: str):
    """Parse one live Continuum caller stub (raises if it is not one)."""
    return parse_caller_stub(caller_stub_text(REPO_ROOT, name))


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
    """Fixture coverage for the concurrency text detectors.

    These detectors are deliberately *not* applied to the live tree. Since
    commit fbf4b79 every caller is a thin stub with no `concurrency:` block --
    concurrency groups live in Continuum's reusable workflows -- so there is
    nothing in this repository for them to read, and a live assertion would
    pass on the absence of the very thing it claims to check. What is still
    enforceable live is that no caller reintroduces a local block, which
    `test_live_render_executor_has_no_global_mutex` and
    `test_live_opencode_workflow_is_per_issue_serialized` do via
    `local_concurrency_groups`.

    The detectors are kept because they describe the policy Continuum enforces
    (per-issue for render, per-issue for OpenCode, either of those never a
    global mutex). They are exercised here against synthetic text in both
    directions, so a change that inverts one of them fails instead of quietly
    agreeing with anything.
    """
    per_issue = "concurrency:\n  group: runtime-lab-render-${{ inputs.issue_number }}"
    assert has_per_issue_render_concurrency(per_issue)
    assert not has_global_render_mutex(per_issue)
    assert has_global_render_mutex("concurrency:\n  group: runtime-lab-render")
    serialized = "concurrency:\n  group: runtime-lab-render-single-service"
    assert has_serialized_render_concurrency(serialized)
    assert has_valid_render_concurrency(serialized)
    assert has_valid_render_concurrency(per_issue)
    assert not has_global_render_mutex(serialized)
    assert has_per_issue_opencode_concurrency(
        "group: opencode-${{ inputs.issue_number || github.run_id }}"
    )

    # Negative direction: each detector must reject the shapes it forbids,
    # and the combined detector must accept only the two sanctioned groups.
    global_mutex = "concurrency:\n  group: runtime-lab-render"
    assert not has_per_issue_render_concurrency(global_mutex)
    assert not has_serialized_render_concurrency(per_issue)
    assert not has_per_issue_render_concurrency("")
    assert not has_serialized_render_concurrency("")
    assert not has_per_issue_opencode_concurrency("")
    assert not has_per_issue_opencode_concurrency(
        "group: opencode-${{ github.run_id }}"
    )
    # A bare global mutex is not a valid render concurrency shape either.
    assert not has_valid_render_concurrency(global_mutex)
    assert not has_valid_render_concurrency("")
    assert not has_global_render_mutex("")


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


def test_live_scheduler_permits_six_concurrent_issues():
    """The scheduler's WIP budget is Continuum's; the caller must not narrow it.

    ``WIP_LIMIT: '6'`` and ``MAX_DISPATCH_ATTEMPTS: '4'`` live in
    Continuum's reusable ``continuum-issue-scheduler.yml`` since fbf4b79, so
    this repository can no longer read the number off a workflow file. What it
    still owns -- and what would silently throttle the fleet to a single
    issue -- is the caller: it must delegate to the scheduler and forward both
    knobs as bare passthroughs, so the engine default and the consumer's
    ``vars.AUTOMATION_*`` override both stay reachable, and it must not pin a
    literal WIP limit that would override those variables for every caller.
    """
    text = _read_workflow("continuum-issue-scheduler.yml")
    stub = _stub("continuum-issue-scheduler")
    assert stub.delegates_to("continuum-issue-scheduler.yml", "main")
    assert stub.secrets_inherit
    for knob in ("wip_limit", "lease_minutes", "max_dispatch_attempts"):
        assert knob in stub.declared_inputs, knob
        assert stub.forwards_bare(knob), (
            "%s must stay a bare passthrough so Continuum's own default and the "
            "consumer's vars.AUTOMATION_* override remain reachable" % knob
        )
    # A reintroduced literal budget is the regression this guards.
    assert scheduler_wip_default(text) is None
    assert scheduler_max_attempts_default(text) is None


def test_live_render_executor_has_no_global_mutex():
    """The caller declares no concurrency; a bare passthrough is forwarded.

    Concurrency moved into Continuum's reusable workflow, where the group is
    built from ``inputs.issue_number`` (or the intentional
    ``single-service`` serialization). A consumer can still reintroduce the
    forbidden global ``runtime-lab-render`` mutex by adding a top-level
    ``concurrency:`` block to its own caller, which is what this now guards:
    the executor stub must carry no local group, must still expose the
    ``concurrency_group`` knob that parameterizes Continuum's group, and must
    forward both that knob and the issue identity it is derived from bare.
    """
    text = _read_workflow("continuum-render-executor.yml")
    stub = _stub("continuum-render-executor")
    assert stub.delegates_to("continuum-render-executor.yml", "main")
    assert stub.local_concurrency_group is None
    assert local_concurrency_groups(text) == ()
    assert not has_global_render_mutex(text)
    for knob in ("concurrency_group", "issue_number"):
        assert knob in stub.declared_inputs, knob
        assert stub.forwards_bare(knob), knob


def test_live_opencode_workflow_is_per_issue_serialized():
    """The OpenCode caller's per-issue identity must survive the delegation.

    Per-issue serialization lives in Continuum's reusable workflow and is
    keyed on ``inputs.issue_number``, so this repository cannot assert the
    group any more -- but it does own whether that identity reaches the
    callee. The caller must declare no local concurrency block (which would
    serialize the whole fleet globally) and must forward ``issue_number``
    unrewritten, since a hardcoded or dropped value collapses every run into
    one serial group.
    """
    text = _read_workflow("continuum-opencode.yml")
    stub = _stub("continuum-opencode")
    assert stub.delegates_to("continuum-opencode.yml", "main")
    assert stub.local_concurrency_group is None
    assert local_concurrency_groups(text) == ()
    assert "issue_number" in stub.declared_inputs
    assert stub.forwards_bare("issue_number"), (
        "issue_number is what Continuum's per-issue concurrency group is "
        "built from; forwarding anything else serializes every issue together"
    )
    # The scheduler-written markers the per-issue flow keys on are forwarded
    # too; a caller that dropped one would re-enter a claimable issue.
    for knob in ("dispatch_marker", "in_progress_label", "pause_marker"):
        assert knob in stub.declared_inputs, knob
        assert stub.forwards_bare(knob), knob


def _code_level_script_mentions(module_path: str) -> set:
    """Script entrypoint names bound by *executable* code, not by prose.

    Walks the module's AST and keeps only string constants that are not a
    docstring, so a module that merely narrates the harness in its header
    comment does not count as binding it. This is what separates a real
    entrypoint binding from a documentation mention.
    """
    import ast

    with open(module_path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            text = ast.get_docstring(node, clean=False)
            if text is not None:
                docstrings.add(text)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value in docstrings:
            continue
        for script in RENDER_HARNESS_FILES:
            if script in node.value:
                names.add(script)
    return names


def test_live_render_executor_references_harness_entrypoints():
    """The executor runs in Continuum; the scripts it runs still live here.

    ``automation/render-job.sh`` and ``automation/render-cleanup.sh`` were
    never migrated -- they are consumer scripts, resolved at run time inside
    this repository. Two real bindings connect the executor to them, and both
    are asserted here:

    1. the caller declares and forwards the three script knobs *bare*, so the
       callee resolves them against this checkout's paths instead of a
       hardcoded location, and
    2. ``runtime_lab_audit`` names the same two entrypoints in a module-level
       constant that its own presence check consumes -- a code binding, so a
       rename of either script has to be reflected in the audit rather than
       silently un-detecting it.

    The prior version of this test additionally asserted that
    ``render_lifecycle.py`` referenced both scripts. That was vacuous: the
    module's only occurrence of either path is the module docstring at line 9,
    so the assertion held on prose alone. ``render_lifecycle`` genuinely does
    not bind the entrypoints (it is the HTTP/state contract, not the script
    runner), so there is nothing there to assert; the bindings above are the
    real ones.
    """
    stub = _stub("continuum-render-executor")
    for knob in ("job_script", "cleanup_script", "qualification_script"):
        assert knob in stub.declared_inputs, knob
        assert stub.forwards_bare(knob), (
            "%s must stay a bare passthrough so the callee resolves the "
            "consumer's own script paths" % knob
        )
    # Binding 2: the audit names both entrypoints in code, and that constant
    # is what the presence check is driven from (so it cannot rot in a
    # hardcoded duplicate inside the function body).
    assert RENDER_HARNESS_FILES == (
        "automation/render-job.sh",
        "automation/render-cleanup.sh",
    )
    audit_path = os.path.join(REPO_ROOT, "automation", "runtime_lab_audit.py")
    assert _code_level_script_mentions(audit_path) == set(RENDER_HARNESS_FILES)
    assert render_harness_files_present(REPO_ROOT) == {
        script: True for script in RENDER_HARNESS_FILES
    }
    # And the same names reach the check as plain text, which is what the
    # audit helper is for -- proven here on the real constant, not on a
    # docstring.
    assert references_render_harness_scripts("\n".join(RENDER_HARNESS_FILES))


def test_render_harness_script_reference_check_is_not_vacuous():
    """``references_render_harness_scripts`` must reject partial references.

    A substring check that accepts text naming only one of the two
    entrypoints cannot fail on a partial rename, which is the drift it exists
    to catch.
    """
    for missing in RENDER_HARNESS_FILES:
        partial = "\n".join(s for s in RENDER_HARNESS_FILES if s != missing)
        assert not references_render_harness_scripts(partial), missing
    assert not references_render_harness_scripts("")
    assert not references_render_harness_scripts("automation/render-job.sh")


def test_live_render_executor_pins_allowed_region_and_model():
    """Region and model are forwarded, never pinned, and stay policy-clean.

    The policy itself is enforced locally by ``render_lifecycle.py`` (and, in
    the reusable workflow, by Continuum) against a value that arrives from the
    consumer's ``vars.``. So the caller must forward ``render_region`` and
    ``model`` bare, and must not name a region at all: a literal here would
    override the repository variable of every installed caller, and any name
    it did pin would bypass the allowlist below.
    """
    text = _read_workflow("continuum-render-executor.yml")
    lowered = text.lower()
    stub = _stub("continuum-render-executor")
    for knob in ("render_region", "model"):
        assert knob in stub.declared_inputs, knob
        assert stub.forwards_bare(knob), knob
    assert "frankfurt" not in lowered
    for region in ALLOWED_RENDER_REGIONS:
        assert region not in lowered, (
            "the caller must not pin region %r; it overrides the consumer's "
            "vars.RENDER_REGION for every installed caller" % region
        )
    # The local policy the forwarded value is validated against is unchanged,
    # and still agrees with the audit's own allowlist.
    assert frozenset(ALLOWED_RENDER_REGIONS) == set(ALLOWED_WORKER_REGIONS)
    assert frozenset(FORBIDDEN_WORKER_REGIONS) == {"frankfurt"}
    for region in ALLOWED_RENDER_REGIONS:
        assert validate_worker_region(region) == region
    with pytest.raises(RegionPolicyError):
        validate_worker_region("frankfurt")


def test_live_harness_entrypoints_exist():
    """Issue #4 owns automation/render-job.sh and render-cleanup.sh.

    The Render executor workflow references both entrypoints, and both
    files now exist, so render-executor runs no longer fail closed with
    "Missing automation/render-job.sh".
    """
    presence = render_harness_files_present(REPO_ROOT)
    assert presence == {
        "automation/render-job.sh": True,
        "automation/render-cleanup.sh": True,
    }
