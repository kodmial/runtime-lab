"""Direct tests for the caller-stub reader in ``continuum_stub_contract``.

``test_runtime_lab_audit.py`` exercises the parser only through the live
workflows, which means it can confirm a well-formed stub parses but says
nothing about how the parser reacts to anything else. Every guard below is a
``raise ValueError``, and an untested guard is a guard that cannot be trusted
to fire: a refactor that drops one silently turns a malformed consumer stub
into a stub that parses clean.

So this module drives the parser from inline YAML fixtures written to
``tmp_path`` -- no dependency on the live tree, so a failure here always points
at the parser rather than at whatever the workflows happen to look like today.
"""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "automation", "tests"))
sys.path.insert(0, REPO_ROOT)

from continuum_stub_contract import (  # noqa: E402
    CONTINUUM_OWNER,
    caller_stub_text,
    local_concurrency_groups,
    parse_caller_stub,
)


VALID_STUB = """\
name: Continuum render executor

on:
  workflow_dispatch:
    inputs:
      issue_number:
        description: "Issue number"
        required: true
        type: string
      concurrency_group:
        description: "Concurrency group"
        required: false
        type: string
      render_region:
        description: "Region"
        required: false
        type: string
      pinned_literal:
        description: "Pinned to a literal, not a passthrough"
        required: false
        type: string

jobs:
  call:
    uses: kodmial/continuum/.github/workflows/continuum-render-executor.yml@main
    secrets: inherit
    with:
      issue_number: ${{ inputs.issue_number }}
      concurrency_group: ${{ inputs.concurrency_group }}
      render_region: ${{ inputs.render_region }}
      pinned_literal: always-this-value
"""


def _stub(tmp_path, text, name="continuum-fixture"):
    """Write ``text`` as a stub under ``tmp_path`` and parse what was written.

    The reader resolves ``<repo_root>/.github/workflows/<name>.yml``, so the
    fixture has to land in that shape rather than directly in ``tmp_path``.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / (name + ".yml")).write_text(text, encoding="utf-8")
    return parse_caller_stub(caller_stub_text(str(tmp_path), name))


def _without(text, start_line, end_line=None):
    """Drop ``start_line`` (1-based) through ``end_line`` inclusive."""
    lines = text.splitlines()
    end = end_line if end_line is not None else start_line
    return "\n".join(lines[: start_line - 1] + lines[end:]) + "\n"


# ---------------------------------------------------------------------------
# The happy path: what a well-formed caller stub parses into.
# ---------------------------------------------------------------------------


def test_parses_a_well_formed_caller(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.workflow_name == "Continuum render executor"
    assert stub.callee_owner == CONTINUUM_OWNER
    assert stub.callee_workflow == "continuum-render-executor.yml"
    assert stub.callee_ref == "main"
    assert stub.job_name == "call"
    assert stub.job_count == 1
    assert stub.secrets_inherit is True
    assert stub.local_concurrency_group is None


def test_declared_inputs_come_only_from_workflow_dispatch(tmp_path):
    """Inputs are the dispatch surface, not every key under ``on:``.

    A real stub also triggers on ``issue_comment:`` and friends. Those keys
    carry no inputs, so a parser that scanned the whole ``on:`` block rather
    than the ``workflow_dispatch`` subtree would report them.
    """
    text = VALID_STUB.replace(
        "on:\n  workflow_dispatch:",
        "on:\n  issue_comment:\n    types: [created]\n  workflow_dispatch:",
    )
    stub = _stub(tmp_path, text)
    assert stub.declared_inputs == (
        "issue_number",
        "concurrency_group",
        "render_region",
        "pinned_literal",
    )
    assert "issue_comment" not in stub.declared_inputs


def test_a_job_without_secrets_inherit_reports_false(tmp_path):
    stub = _stub(tmp_path, VALID_STUB.replace("    secrets: inherit\n", ""))
    assert stub.secrets_inherit is False


# ---------------------------------------------------------------------------
# The ValueError guards. Each one must fire on its own defect.
# ---------------------------------------------------------------------------


def test_rejects_missing_top_level_name(tmp_path):
    with pytest.raises(ValueError, match="no top-level `name:`"):
        _stub(tmp_path, _without(VALID_STUB, 1, 2))


def test_rejects_missing_on_block(tmp_path):
    with pytest.raises(ValueError, match="no top-level `on:` block"):
        _stub(tmp_path, _without(VALID_STUB, 3, 21))


def test_rejects_missing_jobs_block(tmp_path):
    text = VALID_STUB.split("\njobs:")[0] + "\n"
    with pytest.raises(ValueError, match="no top-level `jobs:` block"):
        _stub(tmp_path, text)


def test_rejects_two_delegating_jobs(tmp_path):
    """A stub that fans out to two callees is not a thin caller."""
    duplicate = """
  second:
    uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main
    secrets: inherit
    with:
      issue_number: ${{ inputs.issue_number }}
"""
    with pytest.raises(ValueError, match="exactly one delegating job"):
        _stub(tmp_path, VALID_STUB + duplicate)


def test_rejects_a_job_that_delegates_to_nothing(tmp_path):
    """Zero delegating jobs is the same defect seen from the other side."""
    text = VALID_STUB.replace(
        "    uses: kodmial/continuum/.github/workflows/"
        "continuum-render-executor.yml@main\n",
        "",
    )
    with pytest.raises(ValueError, match="exactly one delegating job"):
        _stub(tmp_path, text)


def test_rejects_a_uses_that_is_not_a_reusable_workflow(tmp_path):
    text = VALID_STUB.replace(
        "uses: kodmial/continuum/.github/workflows/continuum-render-executor.yml@main",
        "uses: ./local-script.sh",
    )
    with pytest.raises(ValueError, match="does not use a Continuum reusable"):
        _stub(tmp_path, text)


def test_rejects_a_uses_with_no_owner(tmp_path):
    text = VALID_STUB.replace(
        "uses: kodmial/continuum/.github/workflows/continuum-render-executor.yml@main",
        "uses: /.github/workflows/continuum-render-executor.yml@main",
    )
    with pytest.raises(ValueError, match="names no owner repository"):
        _stub(tmp_path, text)


def test_rejects_a_uses_with_no_ref(tmp_path):
    """An unpinned ref floats to whatever the default branch is tomorrow."""
    text = VALID_STUB.replace(
        "uses: kodmial/continuum/.github/workflows/continuum-render-executor.yml@main",
        "uses: kodmial/continuum/.github/workflows/continuum-render-executor.yml",
    )
    with pytest.raises(ValueError, match="pins no ref"):
        _stub(tmp_path, text)


def test_rejects_a_uses_naming_another_owner(tmp_path):
    """A fork of Continuum vendored under this repo must not read as a stub."""
    text = VALID_STUB.replace(
        "uses: kodmial/continuum/.github/workflows/continuum-render-executor.yml@main",
        "uses: someone/fork/.github/workflows/continuum-render-executor.yml@main",
    )
    with pytest.raises(ValueError, match="must delegate to %s" % CONTINUUM_OWNER):
        _stub(tmp_path, text)


def test_rejects_inline_job_steps(tmp_path):
    """The thin-caller invariant: a delegating job cannot also carry steps.

    This is the guard that replaced the removed ``has_inline_steps`` field --
    the object is never constructed, so the test has to prove the raise, not
    inspect an attribute.
    """
    # A job that delegates *and* carries steps is one delegating job with a
    # body -- the fork the invariant exists to reject.
    text = VALID_STUB.replace(
        "    secrets: inherit\n",
        "    steps:\n      - run: echo hi\n    secrets: inherit\n",
    )
    with pytest.raises(ValueError, match="must not carry inline job steps"):
        _stub(tmp_path, text)

    inline_beside_the_caller = """
  setup:
    steps:
      - run: echo hi
"""
    text = VALID_STUB + inline_beside_the_caller
    with pytest.raises(ValueError, match="must not carry inline job steps"):
        _stub(tmp_path, text)


# ---------------------------------------------------------------------------
# delegates_to: ref is optional, and a wrong ref is not a match.
# ---------------------------------------------------------------------------


def test_delegates_to_checks_the_workflow_name(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.delegates_to("continuum-render-executor.yml")
    assert not stub.delegates_to("continuum-opencode.yml")


def test_delegates_to_ref_none_ignores_the_ref(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.callee_ref == "main"
    assert stub.delegates_to("continuum-render-executor.yml", None)
    assert stub.delegates_to("continuum-render-executor.yml")


def test_delegates_to_ref_matches_only_that_ref(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.delegates_to("continuum-render-executor.yml", "main")
    assert not stub.delegates_to("continuum-render-executor.yml", "v2.1.0")
    # A ref that is a prefix of, or a superstring of, the real one is not a
    # match -- otherwise a loosened check would accept "main-retry".
    assert not stub.delegates_to("continuum-render-executor.yml", "main-retry")
    assert not stub.delegates_to("continuum-render-executor.yml", "mai")
    assert not stub.delegates_to("continuum-render-executor.yml", "")


def test_a_wrong_ref_on_the_workflow_is_detected(tmp_path):
    text = VALID_STUB.replace("continuum-render-executor.yml@main",
                              "continuum-render-executor.yml@v2.1.0")
    stub = _stub(tmp_path, text)
    assert stub.callee_ref == "v2.1.0"
    assert stub.delegates_to("continuum-render-executor.yml", "v2.1.0")
    assert not stub.delegates_to("continuum-render-executor.yml", "main")


# ---------------------------------------------------------------------------
# forwards_bare: the passthrough contract.
# ---------------------------------------------------------------------------


def test_forwards_bare_accepts_only_an_exact_passthrough(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.forwards_bare("issue_number")
    assert stub.forwarded["issue_number"] == "${{ inputs.issue_number }}"
    # A literal is not a passthrough -- it would override the consumer's
    # repository variable for every installed caller.
    assert not stub.forwards_bare("pinned_literal")
    # Not declared at all.
    assert not stub.forwards_bare("qualification_script")
    # Interpolated rather than bare: still overrides, just indirectly.
    text = VALID_STUB.replace(
        "      render_region: ${{ inputs.render_region }}",
        "      render_region: region-${{ inputs.render_region }}",
    )
    assert not _stub(tmp_path, text).forwards_bare("render_region")
    # A bare passthrough carrying a suffix expression.
    text = VALID_STUB.replace(
        "      render_region: ${{ inputs.render_region }}",
        "      render_region: ${{ inputs.render_region }}-eu",
    )
    assert not _stub(tmp_path, text).forwards_bare("render_region")


def test_bare_passthrough_inputs_lists_only_the_bare_ones(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.bare_passthrough_inputs() == (
        "issue_number",
        "concurrency_group",
        "render_region",
    )
    assert "pinned_literal" not in stub.bare_passthrough_inputs()


def test_bare_passthrough_inputs_is_empty_when_nothing_is_bare(tmp_path):
    text = VALID_STUB.replace("${{ inputs.", "${{ vars.")
    assert _stub(tmp_path, text).bare_passthrough_inputs() == ()


# ---------------------------------------------------------------------------
# Local concurrency: both the field and the standalone reader.
# ---------------------------------------------------------------------------


CONCURRENCY_HEADER = """\
concurrency:
  group: ${{ inputs.concurrency_group }}
  cancel-in-progress: false

"""


def test_local_concurrency_group_reads_a_top_level_block(tmp_path):
    stub = _stub(tmp_path, CONCURRENCY_HEADER + VALID_STUB)
    assert stub.local_concurrency_group == "${{ inputs.concurrency_group }}"
    assert local_concurrency_groups(CONCURRENCY_HEADER + VALID_STUB) == (
        "${{ inputs.concurrency_group }}",
    )


def test_local_concurrency_group_is_none_without_a_block(tmp_path):
    stub = _stub(tmp_path, VALID_STUB)
    assert stub.local_concurrency_group is None
    assert local_concurrency_groups(VALID_STUB) == ()


def test_local_concurrency_group_handles_a_group_with_no_cancel(tmp_path):
    header = "concurrency:\n  group: runtime-lab-render\n"
    assert _stub(tmp_path, header + VALID_STUB).local_concurrency_group == (
        "runtime-lab-render"
    )
    assert local_concurrency_groups(header + VALID_STUB) == ("runtime-lab-render",)


def test_local_concurrency_ignores_a_quoted_group_inside_a_comment(tmp_path):
    """A group string in prose must not read as a declared group.

    The audit asserts the caller declares *no* concurrency block. A body-wide
    regex would be fooled by exactly the kind of comment explaining why, and
    this file's own docstring discusses forbidden group names.
    """
    text = "# concurrency:\n#   group: runtime-lab-render\n" + VALID_STUB
    assert local_concurrency_groups(text) == ()
    assert _stub(tmp_path, text).local_concurrency_group is None


def test_local_concurrency_finds_the_real_block_after_a_comment(tmp_path):
    """A commented mention must not become the block the reader starts at.

    Searching for the first line *containing* "concurrency:" instead of the
    first top-level ``concurrency:`` key anchors on the comment, and then the
    real block below it is never reached -- the audit would read "no local
    concurrency" for a stub that has one.
    """
    text = (
        "# The old fork declared its own concurrency:\n"
        "#   group: runtime-lab-render\n"
        + CONCURRENCY_HEADER
        + VALID_STUB
    )
    assert local_concurrency_groups(text) == ("${{ inputs.concurrency_group }}",)
    assert _stub(tmp_path, text).local_concurrency_group == (
        "${{ inputs.concurrency_group }}"
    )


def test_local_concurrency_stops_at_the_next_top_level_key(tmp_path):
    """``group:`` under ``jobs:`` is not a top-level concurrency group."""
    text = "concurrency:\n  group: real-group\njobs:\n  call:\n    group: fake\n"
    assert local_concurrency_groups(text) == ("real-group",)