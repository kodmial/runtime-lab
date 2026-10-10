"""Fail-closed contract tests for variable-only parent/child audit.

All repository identities below are synthetic fixtures, not infrastructure
bindings. No live API access or secrets are needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "automation" / "verify-child-bindings.sh"
PARENT = "fixture/control"
CHILD_A = "fixture/alpha"
CHILD_B = "fixture/beta"


def _fixture(*, second=True):
    allowed = ["opaque-one", "opaque-two"] if second else ["opaque-one"]
    data = {
        "repositories": [PARENT, CHILD_A, CHILD_B],
        "vars": {
            PARENT: {"CONTINUUM_ROLE": "parent", "CONTINUUM_CHILDREN": json.dumps(allowed)},
            CHILD_A: {
                "CONTINUUM_ROLE": "child",
                "CONTINUUM_CHILD_ID": "opaque-one",
                "CONTINUUM_PARENT": PARENT,
            },
            CHILD_B: {
                "CONTINUUM_ROLE": "child" if second else "",
                "CONTINUUM_CHILD_ID": "opaque-two" if second else "",
                "CONTINUUM_PARENT": PARENT if second else "",
            },
        },
    }
    return data


def _run(tmp_path, data, *, fail=""):
    executable = tmp_path / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

data = json.loads(os.environ["FAKE_ACTIONS_VARIABLES"])
args = sys.argv[1:]
if len(args) < 3 or args[:2] != ["api", "--paginate"]:
    sys.exit(2)
resource = args[2]
if os.environ.get("FAKE_FAIL_RESOURCE") == resource:
    sys.exit(1)
if resource == "/user/repos?affiliation=owner&per_page=100":
    for name in data["repositories"]:
        print(name)
elif resource.startswith("repos/") and resource.endswith("/actions/variables?per_page=100"):
    name = resource[len("repos/"):-len("/actions/variables?per_page=100")]
    values = data["vars"].get(name)
    if values is None:
        sys.exit(1)
    print(json.dumps({"variables": [
        {"name": key, "value": value} for key, value in values.items()
    ]}))
else:
    sys.exit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    env = dict(os.environ)
    env.update(
        GH_TOKEN="mock-token",
        GITHUB_REPOSITORY=PARENT,
        FAKE_ACTIONS_VARIABLES=json.dumps(data),
        FAKE_FAIL_RESOURCE=fail,
        PATH=f"{tmp_path}:{env['PATH']}",
    )
    return subprocess.run(
        ["bash", str(VERIFY)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_variable_only_audit_success(tmp_path):
    result = _run(tmp_path, _fixture())
    assert result.returncode == 0, result.stderr
    assert "Verified 2 child binding(s)" in result.stdout


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["vars"][PARENT].update(CONTINUUM_ROLE="child"),
        lambda d: d["vars"][PARENT].update(CONTINUUM_CHILDREN="not-json"),
        lambda d: d["vars"][PARENT].update(
            CONTINUUM_CHILDREN='["opaque-one", "opaque-one"]'
        ),
        lambda d: d["vars"][CHILD_A].update(CONTINUUM_PARENT="fixture/other"),
        lambda d: d["vars"][CHILD_A].update(CONTINUUM_ROLE="parent"),
        lambda d: d["vars"][CHILD_B].update(CONTINUUM_CHILD_ID="opaque-one"),
        lambda d: d["vars"][CHILD_A].update(CONTINUUM_CHILD_ID="unknown-id"),
        lambda d: d["vars"][CHILD_A].update(CONTINUUM_CHILD_ID=""),
    ],
)
def test_mismatch_fails_closed(tmp_path, change):
    data = _fixture()
    change(data)
    result = _run(tmp_path, data)
    assert result.returncode != 0
    assert "::error::" in result.stderr


@pytest.mark.parametrize(
    "resource",
    [
        "/user/repos?affiliation=owner&per_page=100",
        f"repos/{PARENT}/actions/variables?per_page=100",
        f"repos/{CHILD_A}/actions/variables?per_page=100",
    ],
)
def test_api_outage_fails_unavailable(tmp_path, resource):
    result = _run(tmp_path, _fixture(), fail=resource)
    assert result.returncode == 4
    assert "unavailable" in result.stderr.lower()
    assert CHILD_A not in result.stderr


def test_no_hardcoded_child_inventory_in_active_workflows():
    assert not (
        ROOT / ".github/workflows/continuum-bootstrap-provider-children.yml"
    ).exists()
    verifier = VERIFY.read_text(encoding="utf-8")
    assert "repo_ids=(" not in verifier
    assert "child_ids=(" not in verifier
    assert "upsert_var" not in verifier
    assert "CONTINUUM_CHILDREN" in verifier
    assert "GITHUB_REPOSITORY" in verifier
