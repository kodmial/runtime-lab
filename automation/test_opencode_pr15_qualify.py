"""Tests for the PR #15 Docker qualification contract (issue #134).

Stdlib + pytest only, fully offline. Locks the exact source identity,
build/version fail-closed gates, and the reused harness vocabulary.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import opencode_pr15_qualify as pr15


def test_source_identity_exact():
    identity = pr15.validate_source_identity()
    assert identity["repo"] == "kodmial/opencode"
    assert identity["pr"] == "15"
    assert identity["branch"] == "coding-no-mini"
    assert identity["source_sha"] == "842157c38db9f8178ed0eee7af32f7536fe2346e"
    assert identity["merge_sha"] == "0d649350557c5ee3882cc55e5ca65f919ab304c4"
    assert identity["source_package_version"] == "1.18.33"
    assert "OPENCODE_VERSION=1.18.33" in identity["build_command"]
    assert "--coding" in identity["build_command"]


def test_source_identity_rejects_other_sha():
    with pytest.raises(ValueError):
        pr15.validate_source_identity(
            source_sha="0d649350557c5ee3882cc55e5ca65f919ab304c4"
        )


def test_source_identity_rejects_other_pr():
    with pytest.raises(ValueError):
        pr15.validate_source_identity(pr=12)


def test_source_identity_rejects_branch():
    with pytest.raises(ValueError):
        pr15.validate_source_identity(branch="main")


def test_build_command_pins_version_and_coding_flag():
    assert pr15.BUILD_COMMAND.startswith("OPENCODE_VERSION=1.18.33 ")
    assert "script/build.ts" in pr15.BUILD_COMMAND
    assert "--coding" in pr15.BUILD_COMMAND
    assert "build:coding" not in pr15.BUILD_COMMAND


def test_version_gate_accepts_stamped():
    assert pr15.check_version_output("1.18.33\n") == "1.18.33"


def test_version_gate_rejects_unstamped_ci_binary():
    with pytest.raises(ValueError):
        pr15.check_version_output("0.0.0--202609290040")


def test_binary_digest_gate():
    assert pr15.verify_built_binary_sha256(pr15.BINARY_SHA256) == pr15.BINARY_SHA256
    with pytest.raises(ValueError):
        pr15.verify_built_binary_sha256("0" * 64)


def test_binary_size_positive():
    assert isinstance(pr15.BINARY_BYTES, int) and pr15.BINARY_BYTES > 0


def test_reused_harness_vocabulary():
    assert pr15.LIMIT_BYTES == 512 * 1024 * 1024
    assert pr15.DOCKER_LIMITS == ("--memory=512m", "--memory-swap=512m")
    assert pr15.BASELINE_HOST_PEAK_KB == 614632
    gap = pr15.baseline_gap(537804800)
    assert gap["delta_vs_baseline_bytes"] == 537804800 - 614632 * 1024
    assert gap["gap_to_limit_bytes"] == 537804800 - 512 * 1024 * 1024


def test_classifier_marginal_for_recorded_trials():
    trials = [
        {"trial": "docker-t1", "exit_code": 0, "correctness": "pass",
         "peak_bytes": 537804800, "memory_events": {"max": 4272, "oom_kill": 0}},
        {"trial": "docker-t2", "exit_code": 0, "correctness": "pass",
         "peak_bytes": 547512320, "memory_events": {"max": 126848, "oom_kill": 0}},
    ]
    assert pr15.classify_result(trials) == "marginal"


def test_mutable_refs_rejected():
    with pytest.raises(ValueError):
        pr15.reject_mutable_ref("main")
