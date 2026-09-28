"""Real coding-only source stripping contract (issue #101).

Stdlib-only, offline, no network or git mutations. This module is the
machine-readable contract for the actual `kodmial/opencode` fork change
specified in `automation/patches/issue-101-fork-change.md`:

- the dedicated coding-only entrypoint/build targets,
- the retained minimal tool set and the excluded subsystems,
- the static-import boundaries that prove exclusion from the compile
  graph (not permission flags),
- the preserved provider/model/session/agent loop,
- the fork patch file inventory plus the build/measurement procedure.

Source grounding: fork `kodmial/opencode` main `ae343e8` (live at
execution time; spec base `9000e7f` in
`automation/opencode-fork.baseline.json`) over upstream
`anomalyco/opencode` base `75e1e7a`, pin `1.18.33`. Startup trace in
`automation/audits/issue-77-opencode-headless-inventory.md`; lite paths
in `automation/opencode-lite.spec.json`; staging in
`automation/opencode_coding_variant.py`.

No custom direct provider/API client is introduced anywhere here; all
model access stays through the existing provider/session/agent loop.
"""

from __future__ import annotations

import json
import os
import re

SCHEMA = "runtime-lab-opencode-source-stripped/v1"

FORK_REPO = "kodmial/opencode"
FORK_BASE_SHA = "ae343e82aba50b77c5c90996d4d784f955cda2ee"
FORK_SPEC_BASE_SHA = "9000e7fc8d96c845512f7c73122431418a71d4e4"
UPSTREAM_REPO = "anomalyco/opencode"
UPSTREAM_BASE_COMMIT = "75e1e7ae310dc36c86c920e8997d0e1181e24a88"
UPSTREAM_TAG = "v1.18.33"
PINNED_VERSION = "1.18.33"

PROPOSED_BRANCH = "coding-only-lite-issue-101"
PATCH_DOC = "automation/patches/issue-101-fork-change.md"

# Canonical lite entrypoint/build target (matches opencode-lite.spec.json
# so the existing lite validators apply directly), plus the `build:coding`
# alias track from the coding-variant contract (identical exclusion set).
LITE_ENTRYPOINT = "packages/opencode/src/cli/cmd/run-coding.ts"
LITE_BUILD_SCRIPT = "packages/opencode/script/build-lite.ts"
LITE_BUILD_TARGET = "build:lite"
LITE_OUTDIR = "dist/lite"
CODING_ENTRYPOINT = "packages/opencode/src/coding-index.ts"
CODING_BUILD_SCRIPT = "packages/opencode/script/build-coding.ts"
CODING_BUILD_TARGET = "build:coding"
CODING_OUTDIR = "dist/coding"
SUPPORT_FILES = (
    "packages/opencode/src/lite/tools.ts",
    "packages/opencode/src/lite/agent.ts",
    "packages/opencode/src/project/bootstrap-coding.ts",
    "packages/opencode/src/tool/registry-coding.ts",
    "packages/opencode/src/server/routes/instance/httpapi/server-coding.ts",
)

NEW_FILES = (
    LITE_ENTRYPOINT,
    "packages/opencode/src/lite/tools.ts",
    "packages/opencode/src/lite/agent.ts",
    "packages/opencode/src/project/bootstrap-coding.ts",
    "packages/opencode/src/tool/registry-coding.ts",
    "packages/opencode/src/server/routes/instance/httpapi/server-coding.ts",
    CODING_ENTRYPOINT,
    LITE_BUILD_SCRIPT,
    CODING_BUILD_SCRIPT,
)

MODIFIED_FILES = ("packages/opencode/package.json",)

FULL_ENTRYPOINT = "packages/opencode/src/index.ts"
FULL_BUILD_SCRIPT = "packages/opencode/script/build.ts"

# Minimal retained tools: repo read/search, edit/write/patch, shell.
RETAINED_TOOLS = ("read", "grep", "glob", "edit", "write", "patch", "bash")

# Static-import boundaries for the new coding files. Each file must not
# contain a static (or dynamic, per the lite validator) import whose
# specifier matches any forbidden pattern; gated uses must be absent from
# the lite closure entirely (no-op local backends instead of importing the
# excluded subsystem, even dynamically).
STATIC_IMPORT_BOUNDARIES: dict[str, tuple[str, ...]] = {
    LITE_ENTRYPOINT: (
        r"tui",
        r"cli/cmd/tui",
        r"cli/cmd/web",
        r"cli/cmd/serve",
        r"cli/cmd/acp",
        r"cli/cmd/attach",
        r"@opencode-ai/tui",
        r"@opentui/",
        r"solid-js",
        r"bonjour-service",
        r"@agentclientprotocol/sdk",
        r"@modelcontextprotocol/sdk",
        r"mcp",
        r"@/lsp",
        r"@/mcp",
        r"@/share/",
        r"share/share-next",
    ),
    "packages/opencode/src/lite/tools.ts": (
        r"question",
        r"websearch",
        r"webfetch",
        r"mcp",
        r"\./lsp",
        r"\./plan",
        r"codemode",
        r"code-mode",
        r"@/mcp",
        r"@modelcontextprotocol/sdk",
        r"plugin",
        r"skill",
        r"formatter",
        r"snapshot",
        r"watcher",
        r"chokidar",
    ),
    "packages/opencode/src/lite/agent.ts": (
        r"@/mcp",
        r"@modelcontextprotocol/sdk",
        r"@/lsp",
        r"tui",
        r"share",
        r"openai",
        r"anthropic",
        r"direct-client",
        r"provider-direct",
    ),
    "packages/opencode/src/project/bootstrap-coding.ts": (
        r"@/lsp/lsp",
        r"@/share/share-next",
        r"\.\./format",
        r"@/mcp",
        r"@modelcontextprotocol/sdk",
        r"@parcel/watcher",
        r"chokidar",
    ),
    "packages/opencode/src/tool/registry-coding.ts": (
        r"\./question",
        r"\./websearch",
        r"\./lsp",
        r"\./plan",
        r"codemode",
        r"code-mode",
    ),
    "packages/opencode/src/server/routes/instance/httpapi/server-coding.ts": (
        r"ShareNext",
        r"share/session",
        r"Skill",
        r"MCP",
        r"McpAuth",
        r"LSP",
        r"Format",
        r"Plugin",
        r"Question",
    ),
    CODING_ENTRYPOINT: (
        r"cli/cmd/tui",
        r"cli/cmd/web",
        r"cli/cmd/serve",
        r"cli/cmd/acp",
        r"cli/cmd/attach",
        r"@opencode-ai/tui",
        r"bonjour-service",
        r"@agentclientprotocol/sdk",
    ),
}

IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def patch_doc_path() -> str:
    """Absolute path of the fork-change patch document."""
    return os.path.join(_repo_root(), PATCH_DOC)


def extract_patch_sources(patch_text: str) -> dict[str, str]:
    """Extract ``### N. <path>`` fenced ```ts blocks from the patch doc.

    Returns a mapping of fork-relative path to file content. Fails closed
    when no sources are found so an empty patch can never validate.
    """
    sources: dict[str, str] = {}
    current: str | None = None
    in_block = False
    buf: list[str] = []
    for line in patch_text.splitlines():
        header = re.match(r"^###\s+\d+\.\s+`([^`]+)`\s*$", line.strip())
        if header is not None:
            if in_block and current is not None:
                raise ValueError("unterminated code block for %r" % current)
            current = header.group(1).strip()
            continue
        if line.strip() == "```ts" and current is not None and not in_block:
            in_block = True
            buf = []
            continue
        if line.strip() == "```" and in_block and current is not None:
            sources[current] = "\n".join(buf) + "\n"
            current = None
            in_block = False
            buf = []
            continue
        if in_block and current is not None:
            buf.append(line)
    if not sources:
        raise ValueError("patch doc contains no extractable ```ts sources")
    return sources


def load_patch_sources(path: str | None = None) -> dict[str, str]:
    """Load and parse the fork-change patch document."""
    target = path or patch_doc_path()
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()
    return extract_patch_sources(text)


def validate_patch_inventory(sources: dict[str, str]) -> None:
    """Fail closed unless the patch carries every required fork file."""
    missing = [path for path in NEW_FILES if path not in sources]
    if missing:
        raise ValueError("fork patch is missing new file(s): %s" % sorted(missing))
    # The full build must not be rewritten by the patch.
    for forbidden in (FULL_ENTRYPOINT, FULL_BUILD_SCRIPT):
        if forbidden in sources:
            raise ValueError(
                "fork patch must not rewrite the full build file %r" % forbidden
            )


def check_source_against_boundary(source: str, filename: str) -> list[str]:
    """Return forbidden static imports found in a coding-path source."""
    patterns = STATIC_IMPORT_BOUNDARIES.get(filename)
    if patterns is None:
        raise ValueError("no static-import boundary defined for %r" % filename)
    violations: list[str] = []
    for match in IMPORT_RE.finditer(source):
        origin = match.group(1)
        for pattern in patterns:
            if re.search(pattern, origin):
                violations.append(origin)
                break
    return violations


def assert_patch_boundaries(sources: dict[str, str]) -> None:
    """Fail closed when any patch source pulls an excluded module."""
    problems: list[str] = []
    for filename, patterns in STATIC_IMPORT_BOUNDARIES.items():
        source = sources.get(filename, "")
        if not source:
            raise ValueError("patch source missing for %r" % filename)
        for match in IMPORT_RE.finditer(source):
            origin = match.group(1)
            for pattern in patterns:
                if re.search(pattern, origin):
                    problems.append("%s imports %r" % (filename, origin))
                    break
    if problems:
        raise ValueError(
            "coding files statically import excluded modules: %s"
            % sorted(set(problems))
        )


def assert_preserved_behavior(sources: dict[str, str]) -> None:
    """Fail closed when required coding behavior is not wired in the patch."""
    entry = sources.get(LITE_ENTRYPOINT, "")
    agent = sources.get("packages/opencode/src/lite/agent.ts", "")
    tools = sources.get("packages/opencode/src/lite/tools.ts", "")
    coding_index = sources.get(CODING_ENTRYPOINT, "")
    build_lite = sources.get(LITE_BUILD_SCRIPT, "")
    build_coding = sources.get(CODING_BUILD_SCRIPT, "")
    if "RunCodingCommand" not in entry or "runCodingAgent" not in entry:
        raise ValueError("run-coding entrypoint must wire runCodingAgent")
    for marker in ("session", "provider", "agent", "model"):
        if marker not in agent.lower():
            raise ValueError("agent loop wiring missing marker %r" % marker)
    for tool in ("tool/read", "tool/grep", "tool/glob", "tool/edit",
                 "tool/write", "tool/apply_patch", "tool/shell"):
        if tool not in tools:
            raise ValueError("minimal tool set missing %r" % tool)
    if "RunCodingCommand" not in coding_index:
        raise ValueError("coding-index must register RunCodingCommand")
    for name, content in (("build-lite", build_lite), ("build-coding", build_coding)):
        if "Bun.build" not in content or "compile" not in content:
            raise ValueError("%s must use the Bun compile path" % name)
        if "CODING_ONLY" not in content:
            raise ValueError("%s must define CODING_ONLY" % name)
    # No custom provider rewrite anywhere in the patch.
    blob = "\n".join(sources.values()).lower()
    for marker in ("direct-provider-client", "custom-llm-client",
                   "openai-fetch-shim", "api.openai.com", "api.anthropic.com"):
        if marker in blob and "provider/" not in blob.split(marker)[0][-200:]:
            raise ValueError("custom provider rewrite marker is out of scope: %r" % marker)


def benchmark_delta(baseline_peak_kb: int, variant_peak_kb: int) -> dict[str, object]:
    """Compute the memory delta between two peaks (fail closed on misuse)."""
    if not isinstance(baseline_peak_kb, int) or baseline_peak_kb <= 0:
        raise ValueError("baseline_peak_kb must be a positive int")
    if not isinstance(variant_peak_kb, int) or variant_peak_kb <= 0:
        raise ValueError("variant_peak_kb must be a positive int")
    saved = baseline_peak_kb - variant_peak_kb
    return {
        "baseline_peak_kb": baseline_peak_kb,
        "variant_peak_kb": variant_peak_kb,
        "saved_kb": saved,
        "saved_pct": round(100.0 * saved / baseline_peak_kb, 2),
        "fits_512m": variant_peak_kb < 512 * 1024,
    }


def fork_pr_instructions() -> list[str]:
    """Human steps for the workflow to materialize the fork PR (no git here)."""
    return [
        "git fetch origin main",
        "git checkout main",
        "git checkout -b %s" % PROPOSED_BRANCH,
        "apply: add %s" % ", ".join(NEW_FILES),
        "apply: extend packages/opencode/package.json scripts with build:lite + build:coding",
        "bun run typecheck && bun test coding-only subset",
        "python3 automation/memory_benchmark.py --include-network (both binaries)",
        "gh pr create --base main --head %s --title %r"
        % (PROPOSED_BRANCH, "Add coding-only low-memory entrypoint and build target (issue #101)"),
    ]


def contract_summary() -> dict[str, object]:
    """Machine-readable summary of this contract (no wall clock)."""
    return {
        "schema": SCHEMA,
        "fork_repo": FORK_REPO,
        "fork_base_sha": FORK_BASE_SHA,
        "fork_spec_base_sha": FORK_SPEC_BASE_SHA,
        "upstream_repo": UPSTREAM_REPO,
        "upstream_base_commit": UPSTREAM_BASE_COMMIT,
        "pinned_version": PINNED_VERSION,
        "proposed_branch": PROPOSED_BRANCH,
        "patch_doc": PATCH_DOC,
        "new_files": list(NEW_FILES),
        "modified_files": list(MODIFIED_FILES),
        "lite_entrypoint": LITE_ENTRYPOINT,
        "lite_build_script": LITE_BUILD_SCRIPT,
        "lite_build_target": LITE_BUILD_TARGET,
        "coding_entrypoint": CODING_ENTRYPOINT,
        "coding_build_script": CODING_BUILD_SCRIPT,
        "coding_build_target": CODING_BUILD_TARGET,
        "retained_tools": list(RETAINED_TOOLS),
    }


def validate_contract() -> dict[str, object]:
    """Load the patch doc and run every offline check; return the summary."""
    with open(patch_doc_path(), "r", encoding="utf-8") as handle:
        text = handle.read()
    if FORK_BASE_SHA not in text or PINNED_VERSION not in text:
        raise ValueError("patch doc must cite the fork base SHA and pin")
    sources = extract_patch_sources(text)
    validate_patch_inventory(sources)
    assert_patch_boundaries(sources)
    assert_preserved_behavior(sources)
    summary = contract_summary()
    summary["patch_sources"] = sorted(sources.keys())
    return summary
