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


def test_live_workflows_detect_known_envelope_defects():
    # Envelope-provisioning tracker: the workflow envelope is owned
    # separately (this token cannot push .github/workflows/**), so this run
    # repairs the consumer at runtime and locks detection of the three known
    # escaped GH_TOKEN lines. When the envelope provisioning removes the
    # escaping, update this test to assert an empty finding list.
    findings = scan_workflows(REPO_ROOT)
    locations = sorted(
        (
            pathlib.Path(path).relative_to(REPO_ROOT).as_posix(),
            number,
        )
        for path, number, _ in findings
    )
    assert locations == [
        (".github/workflows/docker-qualification.yml", 40),
        (".github/workflows/docker-qualification.yml", 257),
        (".github/workflows/qualification-chain.yml", 39),
    ]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
