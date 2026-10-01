#!/usr/bin/env python3
"""Regression tests for the issue-#142 workflow credential guard.

The guard fails closed when production workflow credential env values use
escaped GitHub expression syntax (``\\${{ ... }}``), which corrupts
``GH_TOKEN`` with a leading backslash and causes 401 "Bad credentials".
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from workflow_credential_guard import (  # noqa: E402
    is_escaped_credential_line,
    sanitize_github_token,
    scan_text,
    scan_workflows,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_sanitize_strips_single_envelope_escape_backslash():
    assert sanitize_github_token("\\ghp_secrettoken") == "ghp_secrettoken"


def test_sanitize_leaves_clean_token_untouched():
    assert sanitize_github_token("ghp_secrettoken") == "ghp_secrettoken"
    assert sanitize_github_token("github_pat_abc123") == "github_pat_abc123"


def test_sanitize_handles_empty_input():
    assert sanitize_github_token("") == ""
    assert sanitize_github_token(None) == ""


def test_sanitize_strips_exactly_one_backslash():
    assert sanitize_github_token("\\\\token") == "\\token"


def test_detector_flags_escaped_credential_expression():
    assert is_escaped_credential_line(
        "          GH_TOKEN: \\${{ secrets.TAP_PAT || github.token }}"
    )
    assert is_escaped_credential_line(
        "  GITHUB_TOKEN: \\${{ secrets.TAP_PAT }}"
    )


def test_detector_accepts_evaluated_credential_expression():
    assert not is_escaped_credential_line(
        "          GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}"
    )
    assert not is_escaped_credential_line(
        "          GH_TOKEN: ${{ github.token }}"
    )


def test_detector_ignores_escaped_non_credential_fields():
    # Only genuine credential-expression defects are in scope; escaped
    # expressions in artifact names or plain inputs must not fail the check.
    assert not is_escaped_credential_line(
        "      ISSUE_NUMBER: \\${{ inputs.issue_number }}"
    )
    assert not is_escaped_credential_line(
        "          name: docker-qualification-\\${{ inputs.issue_number }}"
    )


def test_scan_text_reports_line_numbers():
    text = (
        "jobs:\n"
        "  GH_TOKEN: ${{ secrets.TAP_PAT }}\n"
        "  GH_TOKEN: \\${{ secrets.TAP_PAT }}\n"
    )
    assert scan_text(text) == [3]


def test_guard_cli_fails_closed_on_escaped_fixture(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "broken.yml").write_text(
        "jobs:\n  x:\n    env:\n      GH_TOKEN: \\${{ secrets.TAP_PAT }}\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "automation/workflow_credential_guard.py",
         "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 1
    assert "ESCAPED-CREDENTIAL" in proc.stdout


def test_guard_cli_passes_on_clean_fixture(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "clean.yml").write_text(
        "jobs:\n  x:\n    env:\n      GH_TOKEN: ${{ secrets.TAP_PAT }}\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "automation/workflow_credential_guard.py",
         "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0
    assert "OK" in proc.stdout


def test_live_workflows_have_no_known_envelope_defects():
    # Envelope-provisioning tracker, closed out. The last two escaped
    # GH_TOKEN lines lived in docker-qualification.yml; they were unescaped and
    # the file is now the Continuum caller stub
    # continuum-docker-qualification.yml, which carries no credential env at
    # all. The live tree must therefore scan clean. The detector's ability to
    # still catch the defect is covered by
    # test_scan_workflows_detects_a_reintroduced_envelope_defect below, so a
    # future reintroduction fails loudly instead of being asserted here.
    findings = scan_workflows(REPO_ROOT)
    locations = sorted(
        (
            pathlib.Path(path).relative_to(REPO_ROOT).as_posix(),
            number,
        )
        for path, number, _ in findings
    )
    assert locations == []


def test_scan_workflows_detects_a_reintroduced_envelope_defect(tmp_path):
    # Positive control for the detector itself: a synthetic caller stub that
    # re-carries the escaped-credential pattern must still be reported, so the
    # empty live finding list above stays a statement about the tree rather
    # than about a detector that quietly stopped matching.
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    broken = workflows / "continuum-docker-qualification.yml"
    broken.write_text(
        "jobs:\n"
        "  call:\n"
        "    env:\n"
        "      GH_TOKEN: \\${{ secrets.TAP_PAT || github.token }}\n",
        encoding="utf-8",
    )
    findings = scan_workflows(tmp_path)
    locations = sorted(
        (
            pathlib.Path(path).relative_to(tmp_path).as_posix(),
            number,
        )
        for path, number, _ in findings
    )
    assert locations == [
        (".github/workflows/continuum-docker-qualification.yml", 4),
    ]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
