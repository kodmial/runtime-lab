"""Offline tests for the issue-#105 coding-only qualification harness.

Stdlib-first, no network or git mutations. Proves the harness pins the
exact PR #10 head, never accepts a mutable pointer, validates the
entrypoint/binary boundary, builds real 512m/no-swap trial commands with
a labelled BUN_OPTIONS A/B dimension, parses cgroup telemetry, and
classifies only repeated real coding trials (never --version alone).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import opencode_coding_qualify as cq


CODING_SRC = (
    'import yargs from "yargs"\n'
    'import { RunCommand } from "./cli/cmd/run"\n'
    'const cli = yargs().command(RunCommand)\n'
)

FULL_SRC = (
    'import { TuiCommand } from "./cli/cmd/tui"\n'
    'import { WebCommand } from "./cli/cmd/web"\n'
    'import { ServeCommand } from "./cli/cmd/serve"\n'
    'import { AcpCommand } from "./cli/cmd/acp"\n'
    'import { AttachCommand } from "./cli/cmd/attach"\n'
    'import { McpCommand } from "./cli/cmd/mcp"\n'
)


def test_source_identity_pins_exact_head():
    identity = cq.validate_source_identity(cq.REQUIRED_HEAD_SHA)
    assert identity["head_sha"] == "d604c215c2c691c4ad79bd59d1ea2c5d76f5c8aa"
    assert identity["branch"] == "coding-only-lite-issue-101"
    with pytest.raises(ValueError):
        cq.validate_source_identity("ae343e82aba50b77c5c90996d4d784f955cda2ee")
    with pytest.raises(ValueError):
        cq.validate_source_identity(cq.REQUIRED_HEAD_SHA, branch="main")
    with pytest.raises(ValueError):
        cq.validate_source_identity("main")


def test_mutable_refs_rejected():
    for ref in ("main", "latest", "HEAD", "coding-only-lite-issue-101"):
        with pytest.raises(ValueError):
            cq.reject_mutable_ref(ref)
    assert cq.reject_mutable_ref(cq.REQUIRED_HEAD_SHA) == cq.REQUIRED_HEAD_SHA


def test_expected_binary_and_build_steps():
    path = cq.expected_binary_path("/tmp/fork")
    assert path.endswith("packages/opencode/dist/coding/bin/opencode-coding")
    steps = cq.build_steps_for_pr("$BUILD_DIR/x")
    assert any("build:coding" in step for step in steps)
    assert any(cq.REQUIRED_HEAD_SHA in step for step in steps)
    assert not any("latest" in step for step in steps)


def test_entrypoint_boundary_proves_cut():
    evidence = cq.check_entrypoint_sources(CODING_SRC, FULL_SRC)
    assert "RunCommand" not in str(evidence.get("coding_imports", ""))
    assert len(evidence["full_commands_found"]) >= 3
    tainted = CODING_SRC + 'import { TuiCommand } from "./cli/cmd/tui"\n'
    with pytest.raises(ValueError):
        cq.check_entrypoint_sources(tainted, FULL_SRC)
    with pytest.raises(ValueError):
        cq.check_entrypoint_sources(CODING_SRC, "import { RunCommand } from './run'\n")


def test_binary_help_boundary():
    ok = cq.check_binary_help("Usage: opencode-coding run [options]\nCommands: run\n")
    assert ok["run_exposed"] is True
    with pytest.raises(ValueError):
        cq.check_binary_help("Commands: run tui web serve acp attach mcp\n")


def test_agent_command_labels_bun_options():
    argv, env = cq.build_agent_command("/b/opencode-coding", "/w", bun_options="")
    assert "run" in argv and env == {}
    argv2, env2 = cq.build_agent_command("/b/opencode-coding", "/w", bun_options="--smol")
    assert env2 == {"BUN_OPTIONS": "--smol"}
    with pytest.raises(ValueError):
        cq.build_agent_command("/b/opencode-coding", "/w", bun_options="--max-old-space-size=512")


def test_docker_trial_is_real_cgroup_limit():
    cmd = cq.docker_trial_command("/host/bin", "/host/repo")
    assert "--memory=512m" in cmd and "--memory-swap=512m" in cmd
    assert "memory.peak" in cmd[-1] and "memory.events" in cmd[-1]
    assert "opencode-coding" in cmd[-1]
    smol = cq.docker_trial_command("/host/bin", "/host/repo", bun_options="--smol")
    assert "BUN_OPTIONS=--smol" in smol[-1]


def test_telemetry_parser_reads_oom_signals():
    output = ("agent_exit=137\npeak=536870912\ncurrent=536870912\n"
              "high 5\nmax 12\noom_kill 1\noom_group_kill 0\n")
    parsed = cq.parse_docker_telemetry(output)
    assert parsed["agent_exit"] == 137
    assert parsed["memory_peak_bytes"] == 536870912
    assert parsed["memory_events"]["oom_kill"] == 1
    assert parsed["memory_events"]["max"] == 12
    with pytest.raises(ValueError):
        cq.parse_docker_telemetry("no telemetry here")


def test_trial_validation_is_fail_closed():
    good = {"trial": "t1", "bun_options": "", "binary_sha256": "ab" * 32,
            "binary_bytes": 10, "peak_tree_kb": 100, "peak_bytes": 102400,
            "memory_current_bytes": 102400, "memory_peak_bytes": 102400,
            "memory_events": {"max": 0, "oom_kill": 0},
            "wall_seconds": 5.0, "exit_code": 0, "correctness": "pass"}
    assert cq.validate_trial_record(dict(good))["trial"] == "t1"
    slim = dict(good)
    del slim["memory_peak_bytes"]
    with pytest.raises(ValueError):
        cq.validate_trial_record(slim)


def test_classification_needs_two_real_trials():
    def trial(exit_code=0, peak=400 * 1024 * 1024, oom=0, maxv=0, correctness="pass"):
        return {"trial": "t", "bun_options": "", "binary_sha256": "ab" * 32,
                "binary_bytes": 10, "peak_tree_kb": peak // 1024, "peak_bytes": peak,
                "memory_current_bytes": peak, "memory_peak_bytes": peak,
                "memory_events": {"max": maxv, "oom_kill": oom},
                "wall_seconds": 5.0, "exit_code": exit_code, "correctness": correctness}
    assert cq.classify_result([trial()]) == "inconclusive"
    assert cq.classify_result([trial(), trial()]) == "reliable-under-512m"
    assert cq.classify_result(
        [trial(peak=530 * 1024 * 1024), trial(peak=530 * 1024 * 1024)]) == "marginal"
    assert cq.classify_result(
        [trial(), trial(exit_code=137, oom=1, correctness="fail")]) == "does-not-fit"
    # --version-only evidence (not-run) is never success.
    assert cq.classify_result(
        [dict(trial(correctness="not-run")), dict(trial(correctness="not-run"))]) == "inconclusive"


def test_baseline_gap_reports_exact_numbers():
    gap = cq.baseline_gap(600 * 1024 * 1024)
    assert gap["passes_limit"] is False
    assert gap["gap_to_limit_bytes"] == 600 * 1024 * 1024 - 512 * 1024 * 1024
    assert "issue-52" in gap["provenance"]
    table = cq.render_ab_table([{"trial": "t1", "bun_options": "--smol",
                                 "peak_bytes": 400 * 1024 * 1024,
                                 "memory_events": {"max": 0, "oom_kill": 0},
                                 "wall_seconds": 9.0, "exit_code": 0,
                                 "correctness": "pass"}])
    assert "--smol" in table and "pass" in table


def test_representative_repo_materializes():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = cq.create_representative_repo(os.path.join(tmp, "work"))
        assert open(paths["module"]).read().count("FOO_LIMIT = 10") == 1
        assert "FOO_LIMIT" in paths["prompt"]
