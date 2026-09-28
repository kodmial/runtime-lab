"""Regression tests for the coding-only OpenCode variant (issue #79).

Offline, stdlib-first, no network or git mutations. Proves the
initialization boundaries from the issue Definition of Done:

- the lite entrypoint/build target exist in the spec and differ from the
  full build, which stays usable;
- excluded subsystems are absent from the lightweight runtime path
  (static import graph), staged per removal group, not merely
  permission-disabled;
- the retained provider/agent/tool loop stays reachable;
- no custom direct provider/API rewrite is introduced;
- the representative coding task still completes.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from automation.opencode_lite import (
    all_excluded_patterns,
    check_group_removed,
    check_no_direct_provider_client,
    check_retained_present,
    group_patterns,
    lite_closure,
    lite_patch_plan,
    load_spec,
    parse_ts_imports,
    removal_group_names,
    representative_coding_task,
    validate_lite_graph,
    validate_spec,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_FILE = os.path.join(REPO_ROOT, "automation", "opencode-lite.spec.json")


def _lite_files() -> dict[str, str]:
    entry = "packages/opencode/src/cli/cmd/run-coding.ts"
    return {
        entry: (
            "import { runCodingAgent } from '../../lite/agent';\n"
            "import { codingTools } from '../../lite/tools';\n"
            "runCodingAgent(codingTools);\n"
        ),
        "packages/opencode/src/lite/agent.ts": (
            "import { createSession } from '../session/session';\n"
            "import { loadProvider } from '../provider/provider';\n"
            "import { stepAgentLoop } from '../agent/agent';\n"
            "import { selectedModel } from '../model/model';\n"
            "export async function runCodingAgent(tools: unknown) {\n"
            "  const provider = loadProvider(selectedModel);\n"
            "  const session = createSession(provider);\n"
            "  return stepAgentLoop(session, tools);\n"
            "}\n"
        ),
        "packages/opencode/src/lite/tools.ts": (
            "export { readTool } from '../tool/read';\n"
            "export { grepTool } from '../tool/grep';\n"
            "export { globTool } from '../tool/glob';\n"
            "export { editTool } from '../tool/edit';\n"
            "export { writeTool } from '../tool/write';\n"
            "export { patchTool } from '../tool/patch';\n"
            "export { bashTool } from '../tool/bash';\n"
            "import { runCommand } from '../cli/cmd/run';\n"
            "export const entry = runCommand;\n"
        ),
        "packages/opencode/src/session/session.ts": "export function createSession(p: unknown) { return p; }\n",
        "packages/opencode/src/provider/provider.ts": "export function loadProvider(m: unknown) { return m; }\n",
        "packages/opencode/src/agent/agent.ts": "export function stepAgentLoop(s: unknown, t: unknown) { return [s, t]; }\n",
        "packages/opencode/src/model/model.ts": "export const selectedModel = 'explicit';\n",
        "packages/opencode/src/tool/read.ts": "export const readTool = 1;\n",
        "packages/opencode/src/tool/grep.ts": "export const grepTool = 1;\n",
        "packages/opencode/src/tool/glob.ts": "export const globTool = 1;\n",
        "packages/opencode/src/tool/edit.ts": "export const editTool = 1;\n",
        "packages/opencode/src/tool/write.ts": "export const writeTool = 1;\n",
        "packages/opencode/src/tool/patch.ts": "export const patchTool = 1;\n",
        "packages/opencode/src/tool/bash.ts": "export const bashTool = 1;\n",
        "packages/opencode/src/cli/cmd/run.ts": "export const runCommand = 1;\n",
    }


def test_spec_file_validates_and_pins_fork():
    spec = load_spec()
    assert spec["fork_repo"] == "kodmial/opencode"
    assert spec["upstream_repo"] == "anomalyco/opencode"
    assert spec["pinned_opencode_version"] == "1.18.33"
    assert spec["no_custom_provider_rewrite"] is True
    assert spec["variant"]["entrypoint"].endswith("run-coding.ts")
    assert spec["variant"]["build_script"].endswith("build-lite.ts")
    assert spec["variant"]["entrypoint"] != spec["full"]["entrypoint"]
    assert spec["full"]["build_script"].endswith("build.ts")


def test_spec_matches_baseline_pin():
    from automation.opencode_runner import OPENCODE_PINNED_VERSION

    spec = load_spec()
    assert spec["pinned_opencode_version"] == OPENCODE_PINNED_VERSION
    baseline = json.load(
        open(os.path.join(REPO_ROOT, "automation", "opencode-fork.baseline.json"), encoding="utf-8")
    )
    assert spec["fork_base_sha"] == baseline["fork_sha"]
    assert spec["pinned_opencode_version"] == baseline["pinned_opencode_version"]


def test_removal_groups_cover_issue_candidates():
    spec = load_spec()
    names = removal_group_names(spec)
    assert names == ["A-ui-share-update", "B-tools-registry", "C-agent-adjacent"]
    patterns = all_excluded_patterns(spec)
    joined = "\n".join(patterns)
    for candidate in (
        "tui",
        "share",
        "mcp",
        "lsp",
        "plugin",
        "skill",
        "webfetch",
        "todo",
        "subagent",
        "watch",
        "snapshot",
        "undo",
        "model-catalog",
    ):
        assert candidate in joined
    # Full build contract from the fork baseline stays intact.
    for path in (
        "packages/opencode/package.json",
        "packages/opencode/src/index.ts",
        "packages/opencode/src/cli/cmd/run.ts",
        "packages/opencode/script/build.ts",
        "bun.lock",
    ):
        assert path in spec["full"]["required_paths"]


def test_retained_capability_matrix():
    spec = load_spec()
    capabilities = spec["retained"]["capabilities"]
    assert "repository read/search" in capabilities
    assert "edit/write/patch" in capabilities
    assert "shell/build/test" in capabilities
    assert "provider/model/session/agent loop" in capabilities


def test_lite_graph_passes_all_boundaries():
    spec = load_spec()
    files = _lite_files()
    summary = validate_lite_graph(files, spec)
    assert summary["entrypoint"] == spec["variant"]["entrypoint"]
    assert summary["reachable_count"] >= 10


def test_each_removal_group_passes_independently():
    spec = load_spec()
    files = _lite_files()
    entry = spec["variant"]["entrypoint"]
    for name in removal_group_names(spec):
        check_group_removed(files, entry, group_patterns(spec, name))


def test_excluded_tui_module_fails_group_a():
    spec = load_spec()
    files = _lite_files()
    files["packages/opencode/src/lite/agent.ts"] += "import { renderTui } from '../tui/tui';\n"
    files["packages/opencode/src/tui/tui.ts"] = "export const renderTui = 1;\n"
    with pytest.raises(ValueError, match="tui"):
        check_group_removed(
            files, spec["variant"]["entrypoint"], group_patterns(spec, "A-ui-share-update")
        )
    with pytest.raises(ValueError, match="tui"):
        validate_lite_graph(files, spec)


def test_excluded_mcp_module_fails_group_b():
    spec = load_spec()
    files = _lite_files()
    files["packages/opencode/src/lite/tools.ts"] += "import { mcpClient } from '../mcp/mcp';\n"
    files["packages/opencode/src/mcp/mcp.ts"] = "export const mcpClient = 1;\n"
    with pytest.raises(ValueError, match="mcp"):
        check_group_removed(
            files, spec["variant"]["entrypoint"], group_patterns(spec, "B-tools-registry")
        )


def test_excluded_watcher_and_snapshot_fail_group_c():
    spec = load_spec()
    entry = spec["variant"]["entrypoint"]
    watcher_files = _lite_files()
    watcher_files["packages/opencode/src/lite/agent.ts"] += (
        "import { watchRepo } from '../watcher/watcher';\n"
    )
    watcher_files["packages/opencode/src/watcher/watcher.ts"] = "export const watchRepo = 1;\n"
    with pytest.raises(ValueError, match="watch"):
        check_group_removed(watcher_files, entry, group_patterns(spec, "C-agent-adjacent"))
    snapshot_files = _lite_files()
    snapshot_files["packages/opencode/src/lite/agent.ts"] += (
        "import { takeSnapshot } from '../snapshot/snapshot';\n"
    )
    snapshot_files["packages/opencode/src/snapshot/snapshot.ts"] = (
        "export const takeSnapshot = 1;\n"
    )
    with pytest.raises(ValueError, match="snapshot"):
        check_group_removed(snapshot_files, entry, group_patterns(spec, "C-agent-adjacent"))


def test_dynamic_import_of_excluded_module_also_fails():
    spec = load_spec()
    files = _lite_files()
    files["packages/opencode/src/lite/agent.ts"] += "const m = await import('../mcp/mcp');\n"
    files["packages/opencode/src/mcp/mcp.ts"] = "export const m = 1;\n"
    with pytest.raises(ValueError, match="mcp"):
        validate_lite_graph(files, spec)


def test_top_level_static_import_detection():
    text = "import { runCodingAgent } from '../../lite/agent';\n"
    assert "../../lite/agent" in parse_ts_imports(text)
    dynamic = "const plugin = await import('../plugin/plugin');\n"
    assert "../plugin/plugin" in parse_ts_imports(dynamic)


def test_broken_provider_loop_fails():
    spec = load_spec()
    files = _lite_files()
    files["packages/opencode/src/lite/agent.ts"] = "export async function runCodingAgent() { return 1; }\n"
    with pytest.raises(ValueError, match="provider|session|agent|retained"):
        check_retained_present(files, spec["variant"]["entrypoint"], spec)


def test_direct_provider_rewrite_fails():
    spec = load_spec()
    files = _lite_files()
    files["packages/opencode/src/lite/agent.ts"] += "import OpenAI from 'openai';\n"
    with pytest.raises(ValueError, match="direct provider"):
        check_no_direct_provider_client(files, spec["variant"]["entrypoint"])
    files2 = _lite_files()
    files2["packages/opencode/src/lite/direct-client.ts"] = "export const x = 1;\n"
    files2["packages/opencode/src/lite/agent.ts"] += "import './direct-client';\n"
    with pytest.raises(ValueError, match="direct provider"):
        check_no_direct_provider_client(files2, spec["variant"]["entrypoint"])


def test_lite_closure_resolves_relative_graph():
    files = _lite_files()
    reachable, external = lite_closure(files, "packages/opencode/src/cli/cmd/run-coding.ts")
    assert "packages/opencode/src/lite/agent.ts" in reachable
    assert "packages/opencode/src/provider/provider.ts" in reachable
    assert external == []


def test_patch_plan_is_additive_and_keeps_full_build():
    spec = load_spec()
    plan = lite_patch_plan(spec)
    paths = [step["path"] for step in plan]
    assert spec["variant"]["entrypoint"] in paths
    assert spec["variant"]["build_script"] in paths
    assert "packages/opencode/package.json" in paths
    # No existing source file is rewritten: only additions plus a
    # package.json scripts extension.
    for step in plan:
        assert step["action"] in ("add", "extend-scripts")
    assert not any(step["path"] == spec["full"]["entrypoint"] for step in plan)
    assert not any(step["path"] == spec["full"]["build_script"] for step in plan)


def test_representative_coding_task_completes(tmp_path):
    outcome = representative_coding_task(str(tmp_path / "ws"))
    assert outcome["result"] == "line three"


def test_spec_rejects_custom_provider_rewrite_flag():
    spec = load_spec()
    bad = copy.deepcopy(spec)
    bad["no_custom_provider_rewrite"] = False
    with pytest.raises(ValueError):
        validate_spec(bad)


def test_unknown_removal_group_fails_closed():
    spec = load_spec()
    with pytest.raises(ValueError):
        group_patterns(spec, "Z-nope")
