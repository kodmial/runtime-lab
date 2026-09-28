"""Regression tests for real coding-only source stripping (issue #101).

Offline, stdlib-first, no network or git mutations. Proves the fork
patch in `automation/patches/issue-101-fork-change.md` is a real
compile-graph separation (not a permission/spec sketch):

- every required new fork file is present with exact content;
- no coding file statically imports an excluded subsystem;
- the retained provider/agent/tool loop and minimal tool set stay wired;
- the full build is preserved (additive only, no rewrites);
- no custom direct provider/API client is introduced;
- the deterministic representative coding task still completes.
"""

from __future__ import annotations

import os

import pytest

from automation.opencode_lite import (
    check_group_removed,
    group_patterns,
    lite_closure,
    load_spec,
    removal_group_names,
    representative_coding_task,
)
from automation.opencode_source_stripped import (
    CODING_BUILD_SCRIPT,
    CODING_ENTRYPOINT,
    FULL_BUILD_SCRIPT,
    FULL_ENTRYPOINT,
    LITE_BUILD_SCRIPT,
    LITE_ENTRYPOINT,
    MODIFIED_FILES,
    NEW_FILES,
    PINNED_VERSION,
    assert_patch_boundaries,
    assert_preserved_behavior,
    benchmark_delta,
    contract_summary,
    load_patch_sources,
    validate_contract,
    validate_patch_inventory,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_DOC = os.path.join(
    REPO_ROOT, "automation", "patches", "issue-101-fork-change.md"
)


def _sources() -> dict[str, str]:
    return load_patch_sources(PATCH_DOC)


def test_patch_inventory_is_complete_and_additive():
    sources = _sources()
    validate_patch_inventory(sources)
    for path in NEW_FILES:
        assert path in sources, path
    assert FULL_ENTRYPOINT not in sources
    assert FULL_BUILD_SCRIPT not in sources
    assert "packages/opencode/package.json" in MODIFIED_FILES


def test_patch_cites_fork_base_and_pin():
    text = open(PATCH_DOC, encoding="utf-8").read()
    assert "ae343e82aba50b77c5c90996d4d784f955cda2ee" in text
    assert PINNED_VERSION in text
    assert "coding-only-lite-issue-101" in text


def test_coding_boundaries_hold():
    sources = _sources()
    assert_patch_boundaries(sources)


def test_preserved_coding_behavior():
    sources = _sources()
    assert_preserved_behavior(sources)


def test_lite_entrypoint_closure_stays_minimal():
    # Closure over the patch's own three core files plus stub leaves for
    # the retained modules: proves the entry graph reaches only the loop +
    # minimal tools and no excluded subsystem. The lite spec's retained
    # patterns use fixture shorthand (`tool/bash`, `tool/patch`) while the
    # real fork spells them `tool/shell` / `tool/apply_patch`, so this
    # test asserts group exclusion (the real compile-graph proof) plus
    # explicit retained markers instead of reusing the fixture-bound
    # `validate_lite_graph` retained check.
    sources = _sources()
    entry_src = sources[LITE_ENTRYPOINT]
    assert "../../lite/agent" in entry_src
    assert "../../lite/tools" in entry_src
    files = {
        LITE_ENTRYPOINT: entry_src,
        "packages/opencode/src/lite/agent.ts": sources[
            "packages/opencode/src/lite/agent.ts"
        ],
        "packages/opencode/src/lite/tools.ts": sources[
            "packages/opencode/src/lite/tools.ts"
        ],
        "packages/opencode/src/session/session.ts": "export function x() {} // session\n",
        "packages/opencode/src/provider/provider.ts": "export function x() {} // provider\n",
        "packages/opencode/src/agent/agent.ts": "export function x() {} // agent\n",
        "packages/opencode/src/model/model.ts": "export const x = 1 // model\n",
        "packages/opencode/src/tool/read.ts": "export const readTool = 1\n",
        "packages/opencode/src/tool/grep.ts": "export const grepTool = 1\n",
        "packages/opencode/src/tool/glob.ts": "export const globTool = 1\n",
        "packages/opencode/src/tool/edit.ts": "export const editTool = 1\n",
        "packages/opencode/src/tool/write.ts": "export const writeTool = 1\n",
        "packages/opencode/src/tool/shell.ts": "export const bashTool = 1\n",
        "packages/opencode/src/tool/apply_patch.ts": "export const patchTool = 1\n",
    }
    spec = load_spec()
    for name in removal_group_names(spec):
        check_group_removed(files, LITE_ENTRYPOINT, group_patterns(spec, name))
    reachable, _ = lite_closure(files, LITE_ENTRYPOINT)
    assert "packages/opencode/src/lite/agent.ts" in reachable
    blob = "\n".join(files[path] for path in reachable).lower()
    for marker in ("session", "provider", "agent", "model"):
        assert marker in blob, marker
    tools_src = sources["packages/opencode/src/lite/tools.ts"]
    for tool in ("tool/read", "tool/grep", "tool/glob", "tool/edit",
                 "tool/write", "tool/apply_patch", "tool/shell"):
        assert tool in tools_src, tool


def test_tui_import_in_entry_fails_boundary():
    sources = _sources()
    bad = dict(sources)
    bad[LITE_ENTRYPOINT] += 'import { x } from "./tui";\n'
    with pytest.raises(ValueError, match="tui"):
        assert_patch_boundaries(bad)


def test_mcp_import_in_tools_fails_boundary():
    sources = _sources()
    bad = dict(sources)
    bad["packages/opencode/src/lite/tools.ts"] += (
        'import { mcpClient } from "../mcp/mcp";\n'
    )
    with pytest.raises(ValueError, match="mcp"):
        assert_patch_boundaries(bad)


def test_question_import_in_registry_fails_boundary():
    sources = _sources()
    bad = dict(sources)
    bad["packages/opencode/src/tool/registry-coding.ts"] += (
        'import { QuestionTool } from "./question";\n'
    )
    with pytest.raises(ValueError, match="question"):
        assert_patch_boundaries(bad)


def test_full_build_scripts_preserved_in_patch():
    text = open(PATCH_DOC, encoding="utf-8").read()
    assert '"build": "bun run script/build.ts"' in text
    assert '"build:lite": "bun run script/build-lite.ts"' in text
    assert '"build:coding": "bun run script/build-coding.ts"' in text
    assert LITE_BUILD_SCRIPT in text
    assert CODING_BUILD_SCRIPT in text
    assert CODING_ENTRYPOINT in text


def test_no_provider_rewrite_in_patch():
    text = open(PATCH_DOC, encoding="utf-8").read().lower()
    assert "direct-provider-client" not in text
    assert "custom-llm-client" not in text
    assert "openai-fetch-shim" not in text


def test_benchmark_delta_math():
    delta = benchmark_delta(614632, 500000)
    assert delta["saved_kb"] == 114632
    assert delta["fits_512m"] is True
    delta2 = benchmark_delta(614632, 600000)
    assert delta2["fits_512m"] is False
    with pytest.raises(ValueError):
        benchmark_delta(0, 100)


def test_representative_coding_task_completes(tmp_path):
    outcome = representative_coding_task(str(tmp_path / "ws101"))
    assert outcome["result"] == "line three"


def test_contract_summary_validates():
    summary = validate_contract()
    assert summary["fork_repo"] == "kodmial/opencode"
    assert summary["pinned_version"] == "1.18.33"
    assert LITE_ENTRYPOINT in summary["new_files"]
    assert contract_summary()["lite_build_target"] == "build:lite"
