"""Offline tests for the issue-#109 max-headless qualification harness.

Stdlib-first, no network or git mutations. Proves the harness pins the
exact PR #12 artifact, never accepts a mutable pointer or substitute
binary, verifies the archive/binary checksum chain, builds real
512m/no-swap trial commands with a labelled BUN_OPTIONS A/B dimension,
parses cgroup telemetry, and classifies only repeated real coding trials
(never --version alone). Cross-repo download failure maps to
infrastructure-blocked, never to a rebuild.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import opencode_max_headless_qualify as mh


def test_artifact_identity_pins_exact_ids():
    identity = mh.validate_artifact_identity()
    assert identity["artifact_id"] == "11001896223"
    assert identity["source_run_id"] == "36492639568"
    assert identity["source_sha"] == "8ed6c749577d534c55ba9555ba4918ea8be95a97"
    assert identity["branch"] == "opencode/issue11-max-headless"
    with pytest.raises(ValueError):
        mh.validate_artifact_identity(artifact_id="11001896224")
    with pytest.raises(ValueError):
        mh.validate_artifact_identity(source_run_id="36492639569")
    with pytest.raises(ValueError):
        mh.validate_artifact_identity(source_sha="ae343e82aba50b77c5c90996d4d784f955cda2ee")
    with pytest.raises(ValueError):
        mh.validate_artifact_identity(branch="main")
    with pytest.raises(ValueError):
        mh.validate_artifact_identity(pr=10)


def test_mutable_refs_rejected():
    for ref in ("main", "latest", "HEAD", "opencode/issue11-max-headless"):
        with pytest.raises(ValueError):
            mh.reject_mutable_ref(ref)
    assert mh.reject_mutable_ref(mh.SOURCE_SHA) == mh.SOURCE_SHA
    assert mh.reject_mutable_ref(mh.ARCHIVE_SHA256) == mh.ARCHIVE_SHA256


def test_api_endpoints_pin_exact_ids():
    endpoints = mh.artifact_api_endpoints()
    assert "36492639568" in endpoints["list"]
    assert "11001896223" in endpoints["download"]
    assert "latest" not in endpoints["download"]
    steps = mh.download_steps_for_artifact("$WORKDIR/x")
    assert any("11001896223" in step for step in steps)
    assert any(mh.ARCHIVE_SHA256 in step for step in steps)
    assert not any("opencode.ai/install" in step for step in steps)
    assert any("sha256sum -c" in step for step in steps)


def test_bundled_checksum_parsing():
    digest = mh.parse_bundled_checksum("ab" * 32 + "  opencode-coding-linux-x64\n")
    assert digest == "ab" * 32
    with pytest.raises(ValueError):
        mh.parse_bundled_checksum("")
    with pytest.raises(ValueError):
        mh.parse_bundled_checksum("not-a-digest  file\n")


def test_binary_help_boundary():
    ok = mh.check_binary_help("Commands:\n  opencode run [message..]  run opencode with a message\n")
    assert ok["run_exposed"] is True
    with pytest.raises(ValueError):
        mh.check_binary_help("Commands: run tui web serve acp attach mcp\n")
    with pytest.raises(ValueError):
        mh.check_binary_help("")


def test_agent_command_labels_bun_options():
    argv, env = mh.build_agent_command("/b/opencode-coding-linux-x64", "/w", bun_options="")
    assert "run" in argv and env == {}
    assert mh.FREE_MODEL in argv
    argv2, env2 = mh.build_agent_command("/b/opencode-coding-linux-x64", "/w", bun_options="--smol")
    assert env2 == {"BUN_OPTIONS": "--smol"}
    with pytest.raises(ValueError):
        mh.build_agent_command("/b/opencode-coding-linux-x64", "/w", bun_options="--max-old-space-size=512")


def test_docker_trial_is_real_cgroup_limit():
    cmd = mh.docker_trial_command("/host/bin", "/host/repo")
    assert "--memory=512m" in cmd and "--memory-swap=512m" in cmd
    assert "memory.peak" in cmd[-1] and "memory.events" in cmd[-1]
    assert "memory.swap.current" in cmd[-1]
    assert "opencode-coding" in cmd[-1]
    assert "FOO_LIMIT" in cmd[-1]
    smol = mh.docker_trial_command("/host/bin", "/host/repo", bun_options="--smol")
    assert "BUN_OPTIONS=--smol" in smol[-1]


def test_telemetry_parser_reads_oom_and_swap():
    output = ("agent_exit=137\npeak=536870912\ncurrent=536870912\n"
              "swap_current=0\nswap_max=0\n"
              "high 5\nmax 12\noom_kill 1\noom_group_kill 0\n")
    parsed = mh.parse_docker_telemetry(output)
    assert parsed["agent_exit"] == 137
    assert parsed["memory_peak_bytes"] == 536870912
    assert parsed["memory_events"]["oom_kill"] == 1
    assert parsed["memory_events"]["max"] == 12
    assert parsed["swap_current_bytes"] == 0
    assert parsed["swap_max_bytes"] == 0
    with pytest.raises(ValueError):
        mh.parse_docker_telemetry("no telemetry here")


def test_trial_validation_is_fail_closed():
    good = {"trial": "t1", "bun_options": "", "binary_sha256": "ab" * 32,
            "binary_bytes": 10, "peak_tree_kb": 100, "peak_bytes": 102400,
            "memory_current_bytes": 102400, "memory_peak_bytes": 102400,
            "memory_events": {"max": 0, "oom_kill": 0},
            "swap_current_bytes": 0, "swap_max_bytes": 0,
            "wall_seconds": 5.0, "exit_code": 0, "correctness": "pass"}
    assert mh.validate_trial_record(dict(good))["trial"] == "t1"
    slim = dict(good)
    del slim["memory_peak_bytes"]
    with pytest.raises(ValueError):
        mh.validate_trial_record(slim)
    noswap = dict(good)
    del noswap["swap_max_bytes"]
    with pytest.raises(ValueError):
        mh.validate_trial_record(noswap)


def test_classification_needs_two_real_trials():
    def trial(exit_code=0, peak=400 * 1024 * 1024, oom=0, maxv=0, correctness="pass"):
        return {"trial": "t", "bun_options": "", "binary_sha256": "ab" * 32,
                "binary_bytes": 10, "peak_tree_kb": peak // 1024, "peak_bytes": peak,
                "memory_current_bytes": peak, "memory_peak_bytes": peak,
                "memory_events": {"max": maxv, "oom_kill": oom},
                "swap_current_bytes": 0, "swap_max_bytes": 0,
                "wall_seconds": 5.0, "exit_code": exit_code, "correctness": correctness}
    assert mh.classify_result([trial()]) == "inconclusive"
    assert mh.classify_result([trial(), trial()]) == "reliable-under-512m"
    assert mh.classify_result(
        [trial(peak=530 * 1024 * 1024), trial(peak=530 * 1024 * 1024)]) == "marginal"
    assert mh.classify_result(
        [trial(), trial(exit_code=137, oom=1, correctness="fail")]) == "does-not-fit"
    assert mh.classify_result(
        [dict(trial(correctness="not-run")), dict(trial(correctness="not-run"))]) == "inconclusive"


def test_infrastructure_blocked_is_not_memory_result():
    blocked = mh.infrastructure_blocked("artifact download returned 404")
    assert blocked["verdict"] == "infrastructure-blocked"
    assert "404" in blocked["reason"]
    with pytest.raises(ValueError):
        mh.infrastructure_blocked("")


def test_baseline_gap_reports_exact_numbers():
    gap = mh.baseline_gap(600 * 1024 * 1024)
    assert gap["passes_limit"] is False
    assert gap["gap_to_limit_bytes"] == 600 * 1024 * 1024 - 512 * 1024 * 1024
    assert "issue-52" in gap["provenance"]
    table = mh.render_ab_table([{"trial": "t1", "bun_options": "--smol",
                                 "peak_bytes": 400 * 1024 * 1024,
                                 "memory_events": {"max": 0, "oom_kill": 0},
                                 "wall_seconds": 9.0, "exit_code": 0,
                                 "correctness": "pass"}])
    assert "--smol" in table and "pass" in table


def test_representative_repo_materializes():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = mh.create_representative_repo(os.path.join(tmp, "work"))
        assert open(paths["module"]).read().count("FOO_LIMIT = 10") == 1
        assert "FOO_LIMIT" in paths["prompt"]
        caps = mh.REPRESENTATIVE_TASK["required_capabilities"]
        assert "file-edit" in caps and "shell-build-test" in caps
