"""Tests for the issue #79 coding-only variant contract.

Offline, stdlib-first, no network or git mutations. Validates the
Definition of Done as encoded in runtime-lab:

- required coding-agent behavior is preserved (retained set covers the
  representative task);
- excluded modules are absent from the lightweight runtime path spec
  (static-import boundaries), not just permission-disabled;
- removal groups are ordered with per-group benchmark gates (no opaque
  bulk change);
- no custom direct provider/API rewrite is introduced.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation.opencode_coding_variant import (  # noqa: E402
    CODING_BOOTSTRAP,
    CODING_BUILD_SCRIPT,
    CODING_BUILD_TARGET,
    CODING_CONFIG_BASELINE,
    CODING_ENTRYPOINT,
    CODING_REGISTRY,
    CODING_SERVER,
    CONDITIONAL_TOOLS,
    EXCLUDED_SUBSYSTEMS,
    EXCLUDED_TOOLS,
    FORK_PR_PLAN,
    MODIFIED_FILES,
    REMOVAL_GROUPS,
    REPRESENTATIVE_TASK,
    RETAINED_SERVICES,
    RETAINED_TOOLS,
    STATIC_IMPORT_BOUNDARIES,
    assert_source_clean,
    benchmark_delta,
    check_source_against_boundary,
    coding_config_baseline,
    full_coding_tool_set,
    validate_matrix,
    validate_no_provider_rewrite,
    validate_removal_groups,
)

ARTIFACT = (
    Path(__file__).resolve().parents[0]
    / "audits"
    / "issue-79-coding-variant-matrix.md"
)


def test_matrix_validates():
    validate_matrix()
    validate_removal_groups()
    validate_no_provider_rewrite()


def test_retained_set_covers_representative_task():
    tools = set(full_coding_tool_set()) | set(RETAINED_SERVICES)
    assert "read" in tools and "grep" in tools and "glob" in tools
    assert "edit" in tools and "write" in tools and "shell" in tools
    assert "Provider" in tools and "LLM" in tools
    for capability in REPRESENTATIVE_TASK["required_capabilities"]:
        assert capability


def test_excluded_tools_absent_from_coding_set():
    coding = set(full_coding_tool_set())
    for tool in EXCLUDED_TOOLS:
        assert tool not in coding, "excluded tool leaked: %s" % tool
    assert "question" in EXCLUDED_TOOLS
    assert "websearch" in EXCLUDED_TOOLS
    # task/todo stay: tiny loop pieces, drop only with disuse proof.
    assert "task" in coding and "todo" in coding


def test_entrypoint_and_build_target_defined():
    assert CODING_ENTRYPOINT.endswith("coding-index.ts")
    assert CODING_BOOTSTRAP.endswith("bootstrap-coding.ts")
    assert CODING_REGISTRY.endswith("registry-coding.ts")
    assert CODING_SERVER.endswith("server-coding.ts")
    assert CODING_BUILD_SCRIPT.endswith("build-coding.ts")
    assert CODING_BUILD_TARGET == "build:coding"
    assert "packages/opencode/package.json" in MODIFIED_FILES
    assert "packages/opencode/src/index.ts" in MODIFIED_FILES


def test_static_boundaries_cover_all_coding_files():
    for path in (CODING_ENTRYPOINT, CODING_BOOTSTRAP,
                 CODING_REGISTRY, CODING_SERVER):
        assert path in STATIC_IMPORT_BOUNDARIES
        assert STATIC_IMPORT_BOUNDARIES[path]


def test_pruned_coding_sources_pass_boundaries():
    pruned_entry = (
        'import yargs from "yargs"\n'
        'import { RunCommand } from "./cli/cmd/run"\n'
        'import { UI } from "./cli/ui"\n'
        'import { Heap } from "./cli/heap"\n'
    )
    assert check_source_against_boundary(pruned_entry, CODING_ENTRYPOINT) == []
    assert_source_clean(pruned_entry, CODING_ENTRYPOINT)

    pruned_registry = (
        'import { ReadTool } from "./read"\n'
        'import { GrepTool } from "./grep"\n'
        'import { ShellTool } from "./shell"\n'
        'import { EditTool } from "./edit"\n'
        "const skill = await import(\"./skill\")\n"
    )
    assert check_source_against_boundary(pruned_registry, CODING_REGISTRY) == []


def test_full_build_sources_fail_coding_boundaries():
    full_entry = (
        'import { RunCommand } from "./cli/cmd/run"\n'
        'import { TuiThreadCommand } from "./cli/cmd/tui"\n'
        'import { WebCommand } from "./cli/cmd/web"\n'
        'import { ServeCommand } from "./cli/cmd/serve"\n'
    )
    violations = check_source_against_boundary(full_entry, CODING_ENTRYPOINT)
    assert violations, "full entry must violate the coding boundary"

    full_registry = (
        'import { QuestionTool } from "./question"\n'
        'import { WebSearchTool } from "./websearch"\n'
        'import { LspTool } from "./lsp"\n'
    )
    violations = check_source_against_boundary(full_registry, CODING_REGISTRY)
    assert set(violations) == {"./question", "./websearch", "./lsp"}


def test_dynamic_import_is_not_a_static_violation():
    source = (
        'import { ReadTool } from "./read"\n'
        "async function load() { return import(\"./question\") }\n"
    )
    # Dynamic import() is the approved lazy mechanism.
    assert check_source_against_boundary(source, CODING_REGISTRY) == []


def test_config_baseline_carries_kill_switches():
    baseline = coding_config_baseline()
    env = baseline["env"]
    for var in ("OPENCODE_PURE", "OPENCODE_DISABLE_DEFAULT_PLUGINS",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS",
                "OPENCODE_DISABLE_LSP_DOWNLOAD", "OPENCODE_DISABLE_SHARE",
                "OPENCODE_DISABLE_AUTOUPDATE",
                "OPENCODE_DISABLE_MODELS_FETCH"):
        assert env.get(var) == "1", "missing kill switch: %s" % var
    config = baseline["config_json"]
    assert config["mcp"] == {}
    assert config["lsp"] == {}
    assert config["formatter"] is False
    assert config["enabled_providers"] == ["opencode"]
    # Mutating the copy must not affect the module constant.
    baseline["env"]["OPENCODE_PURE"] = "0"
    assert CODING_CONFIG_BASELINE["env"]["OPENCODE_PURE"] == "1"


def test_removal_groups_ordered_with_gates():
    assert [g["id"] for g in REMOVAL_GROUPS] == ["A", "B", "C", "D"]
    for group in REMOVAL_GROUPS:
        assert group["files"]
        assert group["excludes"]
        assert group["benchmark_gate"]
    covered = {s for g in REMOVAL_GROUPS for s in g["excludes"]}
    for subsystem in EXCLUDED_SUBSYSTEMS:
        assert subsystem in covered, "unassigned subsystem: %s" % subsystem


def test_no_provider_rewrite_markers():
    with pytest.raises(ValueError):
        validate_no_provider_rewrite(("direct-provider-client.ts",))
    validate_no_provider_rewrite(())


def test_benchmark_delta_math():
    delta = benchmark_delta(614632, 500000)
    assert delta["saved_kb"] == 114632
    assert delta["saved_pct"] > 0
    assert delta["fits_512m"] is True
    heavy = benchmark_delta(614632, 600000)
    assert heavy["fits_512m"] is False
    with pytest.raises(ValueError):
        benchmark_delta(0, 100)
    with pytest.raises(ValueError):
        benchmark_delta(100, 0)


def test_conditional_tools_are_lazy_not_static():
    assert "skill" in CONDITIONAL_TOOLS
    assert "webfetch" in CONDITIONAL_TOOLS
    assert "skill" not in RETAINED_TOOLS


def test_fork_pr_plan_references_fork_contract():
    assert FORK_PR_PLAN["base_repo"] == "kodmial/opencode"
    assert FORK_PR_PLAN["base_branch"] == "main"
    assert len(FORK_PR_PLAN["base_sha"]) == 40
    assert CODING_ENTRYPOINT in FORK_PR_PLAN["new_files"]
    assert "packages/opencode/src/provider/provider.ts" in FORK_PR_PLAN["modified_files"]


def test_audit_artifact_exists_and_covers_matrix():
    assert ARTIFACT.is_file(), "missing matrix artifact: %s" % ARTIFACT
    body = ARTIFACT.read_text(encoding="utf-8")
    lowered = body.lower()
    for token in ("retained", "removed", "benchmark", "entrypoint",
                  "representative", "provider"):
        assert token in lowered, "matrix missing: %s" % token
    assert CODING_ENTRYPOINT in body
    assert "9000e7f" in body
    assert "out of scope" in lowered or "no custom" in lowered or "no provider" in lowered
