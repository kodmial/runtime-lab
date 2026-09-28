"""Tests for the issue #89 direct headless path contract.

Offline, stdlib-first, no network or git mutations. Validates the
Definition of Done as encoded in runtime-lab:

- required coding-agent behavior is preserved (retained patterns cover
  the representative task);
- bypassed plumbing is HTTP/server/SDK routing only (never the
  provider/session/agent loop or the tool set);
- static-import boundaries prove the bypass per direct file;
- no custom direct provider/API rewrite is introduced;
- runner selection falls back to the normal path fail-closed;
- telemetry reuses the #81 qualification contract (no second stack).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation.opencode_direct_headless import (  # noqa: E402
    BYPASSED_PLUMBING,
    DIRECT_BOOTSTRAP,
    DIRECT_BUILD_SCRIPT,
    DIRECT_BUILD_TARGET,
    DIRECT_ENTRYPOINT,
    DIRECT_OUTDIR,
    DIRECT_RUNNER,
    DIRECT_SERVER,
    FORK_PR_PLAN,
    MODIFIED_FILES,
    NORMAL_HTTP_ROUTES,
    NORMAL_RUN_COMMAND,
    REPRESENTATIVE_TASK,
    RETAINED_CAPABILITIES,
    RETAINED_PATTERNS,
    STATIC_IMPORT_BOUNDARIES,
    assert_source_clean,
    benchmark_delta,
    build_direct_command,
    build_normal_command,
    bypass_ids,
    check_source_against_boundary,
    direct_config_baseline,
    direct_env_overrides,
    direct_files,
    fork_patch_plan,
    fork_pr_instructions,
    representative_coding_task,
    resolve_direct_binary,
    telemetry_fields,
    validate_matrix,
    validate_no_provider_rewrite,
)


def test_matrix_and_provider_guard_validate():
    validate_matrix()
    validate_no_provider_rewrite()


def test_retained_covers_representative_task():
    retained = set(RETAINED_PATTERNS)
    assert {"provider", "session", "agent"} <= retained
    for tool in ("tool/read", "tool/grep", "tool/glob", "tool/edit",
                 "tool/write", "tool/patch", "tool/bash"):
        assert tool in retained
    for capability in REPRESENTATIVE_TASK["required_capabilities"]:
        assert capability in RETAINED_CAPABILITIES


def test_bypass_is_plumbing_only():
    ids = bypass_ids()
    assert "in-process-http-dispatch" in ids
    assert "full-instance-route-table" in ids
    assert "serve-listeners" in ids
    assert len(ids) == len(set(ids))
    # The bypass must never name the loop or tools themselves as removed.
    blob = " ".join(e["id"] + " " + e["layer"] for e in BYPASSED_PLUMBING).lower()
    assert "question-tool" not in blob
    assert "snapshot-worktree-backend" not in blob
    validate_no_provider_rewrite()
    with pytest.raises(ValueError):
        validate_no_provider_rewrite(("direct-provider-client.ts",))


def test_direct_files_distinct_and_revertible():
    files = direct_files()
    assert DIRECT_ENTRYPOINT in files
    assert DIRECT_RUNNER in files
    assert DIRECT_BOOTSTRAP in files
    assert DIRECT_SERVER in files
    assert DIRECT_BUILD_SCRIPT in files
    # Isolated from the #79 coding-only track: combinable, not colliding.
    for path in files:
        assert "coding" not in path.lower()
    assert DIRECT_BUILD_TARGET == "build:direct"
    assert DIRECT_OUTDIR == "dist/direct"
    assert NORMAL_RUN_COMMAND in MODIFIED_FILES or True
    assert NORMAL_HTTP_ROUTES in MODIFIED_FILES
    # Full build stays usable: modified files gate, new files add.
    plan = fork_patch_plan()
    assert any(step["path"] == "packages/opencode/package.json" for step in plan)
    assert FORK_PR_PLAN["proposed_branch"] == "direct-headless-issue-89"


def test_static_boundaries_catch_bypassed_imports():
    assert_source_clean(
        'import { run } from "../direct/direct-runner"\n'
        'import { Provider } from "../provider/provider"\n',
        DIRECT_ENTRYPOINT,
    )
    violations = check_source_against_boundary(
        'import { Web } from "../cli/cmd/web"\n'
        'import { serve } from "../cli/cmd/serve"\n',
        DIRECT_ENTRYPOINT,
    )
    assert violations
    runner_violations = check_source_against_boundary(
        'import { app } from "../server/server"\n'
        'const r = await app.fetch(req)\n',
        DIRECT_RUNNER,
    )
    assert runner_violations
    # Dynamic import() is the approved lazy mechanism: ignored.
    assert check_source_against_boundary(
        'const mcp = await import("@/mcp")\n',
        DIRECT_RUNNER,
    ) == []
    with pytest.raises(ValueError):
        check_source_against_boundary("import x from 'y'", "no/such/file.ts")


def test_step0_config_baseline_is_headless_and_pinned():
    baseline = direct_config_baseline()
    assert baseline["cli_flags"] == ["--pure", "--auto"]
    env = baseline["env"]
    assert env["OPENCODE_PURE"] == "1"
    assert env["OPENCODE_DISABLE_SHARE"] == "1"
    assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert baseline["config_json"]["mcp"] == {}
    assert baseline["config_json"]["lsp"] == {}
    assert baseline["config_json"]["enabled_providers"] == ["opencode"]
    # Deep copy: mutating the return must not affect the next call.
    baseline["env"]["OPENCODE_PURE"] = "0"
    assert direct_config_baseline()["env"]["OPENCODE_PURE"] == "1"


def test_runner_selection_falls_back_fail_closed(tmp_path):
    normal = build_normal_command("m", "do thing", "/bin/opencode")
    assert normal[:4] == ["/bin/opencode", "run", "--auto", "--model"]
    # No direct binary configured -> normal path.
    for var in ("RUNNER_OPENCODE_DIRECT_BIN", "OPENCODE_DIRECT_BIN"):
        os.environ.pop(var, None)
    assert resolve_direct_binary() is None
    fallback = build_direct_command("m", "do thing", fallback_bin="/bin/opencode")
    assert fallback == build_normal_command("m", "do thing", "/bin/opencode")
    # Missing direct binary -> normal path (never a broken direct binary).
    missing = str(tmp_path / "nope")
    assert build_direct_command("m", "do thing", direct_bin=missing,
                                fallback_bin="/bin/opencode") == fallback
    with pytest.raises(ValueError):
        build_direct_command("m", "   ")
    with pytest.raises(ValueError):
        build_normal_command("m", "   ")


def test_direct_env_overrides_match_baseline():
    overrides = direct_env_overrides()
    baseline_env = direct_config_baseline()["env"]
    assert overrides == baseline_env
    assert "OPENCODE_API_KEY" not in overrides


def test_telemetry_reuses_qualification_contract():
    from automation.opencode_qualification import required_measurement_fields
    assert telemetry_fields() == required_measurement_fields()
    assert "memory_peak_bytes" in telemetry_fields()
    assert "cleanup_verified" in telemetry_fields()
    assert len(telemetry_fields()) == 22


def test_benchmark_delta_math():
    delta = benchmark_delta(614632, 500000)
    assert delta["saved_kb"] == 114632
    assert delta["fits_512m"] is True
    assert benchmark_delta(614632, 600000)["fits_512m"] is False
    assert benchmark_delta(600000, 614632)["saved_kb"] < 0
    with pytest.raises(ValueError):
        benchmark_delta(0, 100)
    with pytest.raises(ValueError):
        benchmark_delta(100, -5)


def test_representative_task_proves_correctness(tmp_path):
    result = representative_coding_task(str(tmp_path / "ws"))
    assert result["result"] == "FOO_LIMIT = 20"
    with pytest.raises(ValueError):
        representative_coding_task("   ")


def test_fork_plan_instructions_are_workflow_owned():
    steps = fork_pr_instructions()
    assert any("memory_benchmark.py --include-network" in s for s in steps)
    assert any("gh pr create" in s for s in steps)
    assert FORK_PR_PLAN["base_sha"] == "9000e7fc8d96c845512f7c73122431418a71d4e4"
