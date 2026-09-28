"""Regression tests for the OpenCode source-fork baseline (issue #76).

Offline, stdlib-first, no network or git mutations. Verifies the
Definition of Done as encoded in runtime-lab:

- kodmial/opencode is the source fork only; tracking stays in
  kodmial/runtime-lab with no separate backlog required;
- the imported revision is explicitly recorded (upstream repo + base
  commit + tag commit + fork SHA);
- the recorded pin matches the headless CLI artifact we benchmark
  (opencode_runner.OPENCODE_PINNED_VERSION and install-opencode.sh);
- the headless build contract (manifest shape + required paths) holds.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from automation.opencode_fork import (
    HEADLESS_REQUIRED_PATHS,
    check_pinned_version_consistent,
    load_baseline,
    sync_instructions,
    validate_baseline,
    validate_fork_manifest,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_FILE = os.path.join(REPO_ROOT, "automation", "opencode-fork.baseline.json")


def _baseline_dict() -> dict:
    with open(BASELINE_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_baseline_file_validates():
    data = load_baseline()
    assert data["fork_repo"] == "kodmial/opencode"
    assert data["upstream_repo"] == "anomalyco/opencode"
    assert data["ownership"]["tracking_repo"] == "kodmial/runtime-lab"
    assert data["ownership"]["requires_fork_backlog"] is False


def test_baseline_records_exact_revision():
    data = _baseline_dict()
    assert len(data["fork_sha"]) == 40
    assert len(data["upstream_base_commit"]) == 40
    assert len(data["upstream_tag_commit"]) == 40
    assert data["fork_sha"] != data["upstream_base_commit"]
    assert data["upstream_tag"] == "v%s" % data["pinned_opencode_version"]


def test_baseline_pin_matches_runner_and_installer():
    from automation.opencode_runner import OPENCODE_PINNED_VERSION

    data = load_baseline()
    assert data["pinned_opencode_version"] == OPENCODE_PINNED_VERSION
    check_pinned_version_consistent(data, OPENCODE_PINNED_VERSION)
    text = open(
        os.path.join(REPO_ROOT, "automation", "install-opencode.sh"),
        encoding="utf-8",
    ).read()
    assert OPENCODE_PINNED_VERSION in text
    with pytest.raises(ValueError):
        check_pinned_version_consistent(data, "0.0.0")


def test_ownership_rule_rejects_fork_backlog():
    data = _baseline_dict()
    bad = copy.deepcopy(data)
    bad["ownership"]["requires_fork_backlog"] = True
    with pytest.raises(ValueError):
        validate_baseline(bad)
    bad = copy.deepcopy(data)
    bad["ownership"]["tracking_repo"] = "kodmial/opencode"
    with pytest.raises(ValueError):
        validate_baseline(bad)


def test_history_preservation_required():
    data = _baseline_dict()
    for key in (
        "preserved",
        "fork_contains_upstream_base_commit",
        "fork_contains_upstream_tag_commit",
    ):
        bad = copy.deepcopy(data)
        bad["history"][key] = False
        with pytest.raises(ValueError):
            validate_baseline(bad)


def test_headless_manifest_contract():
    data = load_baseline()
    pinned = data["pinned_opencode_version"]
    good = {
        "name": "opencode",
        "version": pinned,
        "scripts": {"build": "bun run script/build.ts"},
        "bin": {"opencode": "./bin/opencode"},
    }
    validate_fork_manifest(good, pinned)
    bad_version = dict(good, version="0.0.0")
    with pytest.raises(ValueError):
        validate_fork_manifest(bad_version, pinned)
    bad_build = {"name": "opencode", "version": pinned, "scripts": {}, "bin": good["bin"]}
    with pytest.raises(ValueError):
        validate_fork_manifest(bad_build, pinned)
    bad_bin = {"name": "opencode", "version": pinned, "scripts": good["scripts"], "bin": {}}
    with pytest.raises(ValueError):
        validate_fork_manifest(bad_bin, pinned)


def test_headless_required_paths_cover_cli_and_build():
    joined = "\n".join(HEADLESS_REQUIRED_PATHS)
    assert "packages/opencode/package.json" in HEADLESS_REQUIRED_PATHS
    assert "packages/opencode/src/index.ts" in HEADLESS_REQUIRED_PATHS
    assert "packages/opencode/script/build.ts" in HEADLESS_REQUIRED_PATHS
    assert "bun.lock" in HEADLESS_REQUIRED_PATHS
    assert "run" in joined


def test_sync_instructions_reference_baseline():
    data = load_baseline()
    steps = sync_instructions(data)
    text = "\n".join(steps)
    assert data["upstream_repo"] in text
    assert data["upstream_base_commit"] in text
    assert data["fork_branch"] in text
