"""Offline tests for the issue-#126 corrected-artifact qualification contract.

Stdlib-first, no network or git mutations. Proves the contract pins the
exact corrected PR #12 artifact (never the superseded issue-#109 artifact,
a mutable pointer, or a substitute binary), verifies the expected binary
digest/version gates, and reuses the proven 512m/no-swap measurement
vocabulary (trial commands, telemetry parsing, fail-closed validation,
baseline gap, verdict classification) without designing a new harness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import opencode_corrected_qualify as cq


def test_artifact_identity_pins_corrected_ids():
    identity = cq.validate_artifact_identity()
    assert identity["artifact_id"] == "11004835952"
    assert identity["source_run_id"] == "36498663107"
    assert identity["source_sha"] == "a3c748143bbc525a7cef4f9db48e2a779418943c"
    assert identity["merge_sha"] == "84fa724616e1fddeea8e7665e38568928feffdf9"
    assert identity["branch"] == "opencode/issue11-max-headless"
    # Superseded issue-109 artifact and mutable pointers are rejected.
    with pytest.raises(ValueError):
        cq.validate_artifact_identity(artifact_id="11001896223")
    with pytest.raises(ValueError):
        cq.validate_artifact_identity(source_run_id="36492639568")
    with pytest.raises(ValueError):
        cq.validate_artifact_identity(source_sha="8ed6c749577d534c55ba9555ba4918ea8be95a97")
    with pytest.raises(ValueError):
        cq.validate_artifact_identity(branch="main")
    with pytest.raises(ValueError):
        cq.validate_artifact_identity(pr=10)


def test_api_endpoints_pin_corrected_ids():
    endpoints = cq.artifact_api_endpoints()
    assert "36498663107" in endpoints["list"]
    assert "11004835952" in endpoints["download"]
    assert "11001896223" not in endpoints["download"]
    assert "latest" not in endpoints["download"]
    steps = cq.download_steps_for_artifact("$WORKDIR/x")
    assert any("11004835952" in step for step in steps)
    assert any(cq.ARCHIVE_SHA256 in step for step in steps)
    assert not any("opencode.ai/install" in step for step in steps)
    assert any("sha256sum -c" in step for step in steps)


def test_expected_binary_and_version_gates():
    assert cq.verify_expected_binary_sha256(cq.EXPECTED_BINARY_SHA256) == cq.EXPECTED_BINARY_SHA256
    assert cq.verify_expected_binary_sha256("  " + cq.EXPECTED_BINARY_SHA256.upper() + "\n") == cq.EXPECTED_BINARY_SHA256
    with pytest.raises(ValueError):
        cq.verify_expected_binary_sha256("a6dabf731c49999d5c95dd7477cd74f18070615f6a01a8ce36cd8e35b5dce38d")
    with pytest.raises(ValueError):
        cq.verify_expected_binary_sha256("not-a-digest")
    assert cq.check_version_output("1.18.33\n") == "1.18.33"
    with pytest.raises(ValueError):
        cq.check_version_output("0.0.0--202609282229")
    with pytest.raises(ValueError):
        cq.check_version_output("")


def test_reused_harness_vocabulary():
    # No new memory harness: limits, model, and baseline come from the
    # proven module unchanged.
    assert cq.LIMIT_BYTES == 512 * 1024 * 1024
    assert "--memory=512m" in list(cq.DOCKER_LIMITS)
    assert "--memory-swap=512m" in list(cq.DOCKER_LIMITS)
    assert cq.FREE_MODEL == "opencode/muse-spark-1.3-contributor-free"
    cmd = cq.docker_trial_command("/host/bin", "/host/repo")
    assert "--memory=512m" in cmd and "--memory-swap=512m" in cmd
    assert "memory.peak" in cmd[-1] and "memory.events" in cmd[-1]
    assert "FOO_LIMIT" in cmd[-1]
    ok = cq.check_binary_help("Commands:\n  opencode run [message..]  run opencode with a message\n")
    assert ok["run_exposed"] is True
    with pytest.raises(ValueError):
        cq.check_binary_help("Commands: run tui web serve acp attach mcp\n")


def test_classification_of_recorded_trials_is_marginal():
    def trial(peak, maxv, wall):
        return {"trial": "t", "bun_options": "", "binary_sha256": "ab" * 32,
                "binary_bytes": 10, "peak_tree_kb": peak // 1024, "peak_bytes": peak,
                "memory_current_bytes": peak, "memory_peak_bytes": peak,
                "memory_events": {"max": maxv, "oom_kill": 0},
                "swap_current_bytes": 0, "swap_max_bytes": 0,
                "wall_seconds": wall, "exit_code": 0, "correctness": "pass"}
    # Both recorded Docker trials succeed (exit 0, edit + test pass) but ride
    # the ceiling with heavy max-stall pressure: marginal, never reliable.
    assert cq.classify_result(
        [trial(538173440, 2042, 55.0), trial(538189824, 1952, 49.0)]) == "marginal"
    gap = cq.baseline_gap(538173440)
    assert gap["passes_limit"] is False
    assert gap["gap_to_limit_bytes"] == 538173440 - 512 * 1024 * 1024
    assert gap["delta_vs_baseline_bytes"] < 0
    table = cq.render_ab_table([{"trial": "docker-t1", "bun_options": "",
                                 "peak_bytes": 538173440,
                                 "memory_events": {"max": 2042, "oom_kill": 0},
                                 "wall_seconds": 55.0, "exit_code": 0,
                                 "correctness": "pass"}])
    assert "docker-t1" in table and "pass" in table


def test_trial_validation_is_fail_closed():
    good = {"trial": "t1", "bun_options": "", "binary_sha256": "ab" * 32,
            "binary_bytes": 10, "peak_tree_kb": 100, "peak_bytes": 102400,
            "memory_current_bytes": 102400, "memory_peak_bytes": 102400,
            "memory_events": {"max": 0, "oom_kill": 0},
            "swap_current_bytes": 0, "swap_max_bytes": 0,
            "wall_seconds": 5.0, "exit_code": 0, "correctness": "pass"}
    assert cq.validate_trial_record(dict(good))["trial"] == "t1"
    slim = dict(good)
    del slim["memory_peak_bytes"]
    with pytest.raises(ValueError):
        cq.validate_trial_record(slim)
    blocked = cq.infrastructure_blocked("artifact download returned 404")
    assert blocked["verdict"] == "infrastructure-blocked"


def test_representative_repo_materializes():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = cq.create_representative_repo(os.path.join(tmp, "work"))
        assert open(paths["module"]).read().count("FOO_LIMIT = 10") == 1
        assert "FOO_LIMIT" in paths["prompt"]
