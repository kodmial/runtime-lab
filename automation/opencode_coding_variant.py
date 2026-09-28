"""Coding-only low-memory OpenCode variant contract (issue #79).

Stdlib-only, offline, no network or git mutations. This module is the
machine-readable contract for the lightweight ``kodmial/opencode`` build:

- the dedicated coding entrypoint / build target,
- the retained minimal tool set (from #77),
- the retained vs removed capability matrix,
- the static-import boundaries that prove exclusion (not just
  permission-denial),
- the config-only baseline (step 0),
- the ordered removal groups with per-group benchmark gates against #78,
- the representative coding task used for correctness,
- the fork PR plan (base SHAs, branch, files) for the workflow to
  materialize (issue execution never pushes or opens PRs itself).

Source grounding: fork ``kodmial/opencode`` main ``9000e7f`` (baseline
``automation/opencode-fork.baseline.json``; observed ``ae343e8`` at
execution time, recorded as drift) over upstream ``ad6c72c``; full
startup trace in ``automation/audits/issue-77-opencode-headless-
inventory.md``. No custom direct provider/API client is introduced:
the variant keeps ``provider/provider.ts`` and the session/agent loop
and only lazifies unused provider imports.
"""

from __future__ import annotations

import json
import os
import re

SCHEMA = "runtime-lab-opencode-coding-variant/v1"

# Fork revision this spec was authored against. The pinned baseline SHA
# comes from automation/opencode-fork.baseline.json; OBSERVED_FORK_SHA is
# the fork main observed live during this run (drift vs the baseline).
FORK_REPO = "kodmial/opencode"
FORK_BASE_SHA = "9000e7fc8d96c845512f7c73122431418a71d4e4"
OBSERVED_FORK_SHA = "ae343e82aba50b77c5c90996d4d784f955cda2ee"
UPSTREAM_BASE_COMMIT = "ad6c72c7068812d43b31f3cfb9e413356a19d850"
UPSTREAM_REPO = "anomalyco/opencode"
PINNED_VERSION = "1.18.33"

# Dedicated coding-only entrypoint / build target (new files in the fork).
CODING_ENTRYPOINT = "packages/opencode/src/coding-index.ts"
CODING_BOOTSTRAP = "packages/opencode/src/project/bootstrap-coding.ts"
CODING_REGISTRY = "packages/opencode/src/tool/registry-coding.ts"
CODING_SERVER = "packages/opencode/src/server/routes/instance/httpapi/server-coding.ts"
CODING_BUILD_SCRIPT = "packages/opencode/script/build-coding.ts"
CODING_BUILD_TARGET = "build:coding"

# Full-build files the variant modifies (lazy loading / gating only).
MODIFIED_FILES = (
    "packages/opencode/package.json",
    "packages/opencode/src/index.ts",
    "packages/opencode/src/effect/app-runtime.ts",
    "packages/opencode/src/tool/registry.ts",
    "packages/opencode/src/plugin/index.ts",
    "packages/opencode/src/provider/provider.ts",
    "packages/opencode/src/project/bootstrap.ts",
)

# Minimal tool set proven by #77: repository read/search, edit/write/patch,
# shell/build/test, provider/model/session/agent loop. `apply_patch` swaps
# with `write` by model id in registry.ts:tools(); both are retained.
# `task`/`todo`/`invalid` are tiny loop/planning/fallback pieces kept until
# a run proves prompts never invoke them. `skill`/`webfetch` are
# conditional (only with installed skills / prompt need).
RETAINED_TOOLS = (
    "read",
    "glob",
    "grep",
    "edit",
    "write",
    "apply_patch",
    "shell",
    "task",
    "todo",
    "invalid",
)
CONDITIONAL_TOOLS = (
    "skill",
    "webfetch",
)
EXCLUDED_TOOLS = (
    "websearch",
    "question",
    "lsp",
    "plan_enter",
    "plan_exit",
    "code-mode",
)

# Services that must stay reachable from the coding path.
RETAINED_SERVICES = (
    "SessionProcessor",
    "SessionPrompt",
    "SessionRunState",
    "SessionStatus",
    "LLM",
    "Provider",
    "ProviderAuth",
    "Agent",
    "Config",
    "Permission",
    "ToolRegistry",
    "Database",
    "Storage",
    "Snapshot",  # track/patch interface kept; git-worktree backend lazified
    "Truncate",
    "Instruction",
    "InstanceStore",
    "Project",
    "EventV2Bridge",
    "SessionProjector",
)

# Subsystems physically excluded or compile-separated in the coding build.
# Each entry maps to a removal group in REMOVAL_GROUPS below.
EXCLUDED_SUBSYSTEMS = (
    "tui",  # TuiThreadCommand, interactive footer runtime, @opencode-ai/tui, opentui/solid
    "desktop-web-ui",  # WebCommand, ServeCommand web assets, embedded web UI
    "acp",  # AcpCommand, @agentclientprotocol/sdk path
    "serve-listeners",  # Server.listen TCP + bonjour-service mDNS (run uses in-process fetch)
    "share-sync",  # ShareNext sync uploader + SessionShare routes
    "mcp-client",  # MCP client stack + @modelcontextprotocol/sdk under zero-server config
    "lsp-servers",  # LSP servers/downloaders
    "formatter-table",  # formatter definitions table
    "plugin-install",  # plugin npm-install path under --pure
    "internal-auth-plugins",  # 12 internal auth plugins behind OPENCODE_DISABLE_DEFAULT_PLUGINS
    "external-skills",  # external skill discovery behind OPENCODE_DISABLE_EXTERNAL_SKILLS
    "question-tool",  # question tool (already permission-denied in non-interactive run)
    "websearch-tool",  # websearch (default-off gating)
    "snapshot-worktree-backend",  # git worktree backend behind snapshot:false no-op
    "file-watcher-impl",  # @parcel/watcher + chokidar impl behind disable flag
    "update-check",  # upgrade()/installation latest-check
    "models-catalog-fetch",  # ModelsDev catalog network fetch
    "unused-ai-sdk-providers",  # unused @ai-sdk/* imports (pinned provider path kept)
)

# Static-import boundaries for the coding path. Each coding file must not
# contain a top-level static import matching any forbidden pattern; gated
# uses must be dynamic import() behind flags. Patterns are regex fragments
# matched against import source strings.
STATIC_IMPORT_BOUNDARIES: dict[str, tuple[str, ...]] = {
    CODING_ENTRYPOINT: (
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
    ),
    CODING_BOOTSTRAP: (
        r"@/lsp/lsp",
        r"@/share/share-next",
        r"\.\./format",
        r"@/mcp",
        r"@modelcontextprotocol/sdk",
        r"@parcel/watcher",
        r"chokidar",
    ),
    CODING_REGISTRY: (
        r"\./question",
        r"\./websearch",
        r"\./lsp",
        r"\./plan",
        r"codemode",
        r"code-mode",
    ),
    CODING_SERVER: (
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
}

# Config-only baseline (step 0, no fork change) measured first.
CODING_CONFIG_BASELINE: dict[str, object] = {
    "cli_flags": ["--pure", "--auto"],
    "env": {
        "OPENCODE_PURE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_SHARE": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
    },
    "config_json": {
        "mcp": {},
        "lsp": {},
        "formatter": False,
        "snapshot": False,
        "share": False,
        "autoupdate": False,
        "enabled_providers": ["opencode"],
    },
}

# Ordered removal groups; each is benchmarked before the next (no opaque
# bulk change). Benchmark gate: memory_benchmark.py hermetic signals plus
# Docker 512 MB real-agent trials per the #78 procedure.
REMOVAL_GROUPS: tuple[dict[str, object], ...] = (
    {
        "id": "A",
        "title": "command-tree pruning",
        "files": [CODING_ENTRYPOINT, CODING_BUILD_SCRIPT,
                  "packages/opencode/package.json",
                  "packages/opencode/src/index.ts"],
        "excludes": ["tui", "desktop-web-ui", "acp", "serve-listeners"],
        "risk": "low",
        "benchmark_gate": "binary --version peak + startup parse check",
    },
    {
        "id": "B",
        "title": "bootstrap laziness",
        "files": [CODING_BOOTSTRAP,
                  "packages/opencode/src/project/bootstrap.ts"],
        "excludes": ["lsp-servers", "share-sync", "formatter-table",
                     "snapshot-worktree-backend", "file-watcher-impl"],
        "risk": "medium",
        "benchmark_gate": "config-baseline real-run peak vs step-0 baseline",
    },
    {
        "id": "C",
        "title": "registry/route laziness",
        "files": [CODING_REGISTRY, CODING_SERVER,
                  "packages/opencode/src/tool/registry.ts",
                  "packages/opencode/src/server/routes/instance/httpapi/server.ts"],
        "excludes": ["question-tool", "websearch-tool", "mcp-client",
                     "share-sync", "snapshot-worktree-backend"],
        "risk": "medium",
        "benchmark_gate": "representative-task peak + correctness pass",
    },
    {
        "id": "D",
        "title": "dependency pruning",
        "files": ["packages/opencode/package.json",
                  "packages/opencode/src/provider/provider.ts",
                  "packages/opencode/src/plugin/index.ts"],
        "excludes": ["unused-ai-sdk-providers", "mcp-client",
                     "plugin-install", "internal-auth-plugins",
                     "external-skills", "update-check",
                     "models-catalog-fetch"],
        "risk": "medium-high",
        "benchmark_gate": "Docker 512m/no-swap real-agent trials + bundle check",
    },
)

# Representative coding task for correctness: inspect/search, modify,
# run build/tests, react to failures, return the expected result.
REPRESENTATIVE_TASK = {
    "prompt": (
        "Search the repository for the constant named FOO_LIMIT, "
        "read the surrounding module, change its value from 10 to 20, "
        "run the module build/tests, fix any failure the change causes, "
        "and report the files changed plus the test result."
    ),
    "required_capabilities": [
        "repo-search",  # glob/grep/read
        "file-edit",  # edit/write/apply_patch
        "shell-build-test",  # shell
        "provider-agent-loop",  # provider/model/session/agent loop
        "failure-reaction",  # event loop + session status
    ],
}

# Capability -> covering tools/services (used to prove the retained set is
# sufficient for the representative task).
CAPABILITY_COVERAGE: dict[str, tuple[str, ...]] = {
    "repo-search": ("read", "glob", "grep"),
    "file-edit": ("edit", "write", "apply_patch"),
    "shell-build-test": ("shell",),
    "provider-agent-loop": ("Provider", "LLM", "SessionProcessor",
                            "SessionPrompt", "Agent"),
    "failure-reaction": ("SessionStatus", "SessionRunState",
                         "EventV2Bridge", "shell", "read"),
}

FORK_PR_PLAN = {
    "base_repo": FORK_REPO,
    "base_branch": "main",
    "base_sha": FORK_BASE_SHA,
    "observed_sha": OBSERVED_FORK_SHA,
    "proposed_branch": "coding-only-variant-issue-79",
    "title": "Add coding-only low-memory entrypoint and build target (issue #79)",
    "new_files": [CODING_ENTRYPOINT, CODING_BOOTSTRAP, CODING_REGISTRY,
                  CODING_SERVER, CODING_BUILD_SCRIPT],
    "modified_files": list(MODIFIED_FILES),
}

IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def retained_tool_set() -> tuple[str, ...]:
    """Tools always available in the coding build."""
    return RETAINED_TOOLS


def full_coding_tool_set() -> tuple[str, ...]:
    """Retained plus conditional tools (skill/webfetch when needed)."""
    return RETAINED_TOOLS + CONDITIONAL_TOOLS


def validate_matrix() -> None:
    """Fail closed when retained/removed sets overlap or drop requirements."""
    retained = set(RETAINED_TOOLS)
    conditional = set(CONDITIONAL_TOOLS)
    excluded = set(EXCLUDED_TOOLS)
    if retained & excluded:
        raise ValueError(
            "retained/excluded tool overlap: %s" % sorted(retained & excluded)
        )
    if conditional & excluded:
        raise ValueError(
            "conditional/excluded tool overlap: %s"
            % sorted(conditional & excluded)
        )
    for required in ("read", "grep", "glob", "edit", "write", "shell"):
        if required not in retained:
            raise ValueError("minimal tool set missing required tool: %s" % required)
    # question must be excluded (permission-denied in run.ts), task/todo stay.
    if "question" not in excluded:
        raise ValueError("question tool must be excluded from the coding build")
    if "task" not in retained or "todo" not in retained:
        raise ValueError("task/todo are tiny loop pieces and stay retained")
    for capability, covering in CAPABILITY_COVERAGE.items():
        tools = set(full_coding_tool_set()) | set(RETAINED_SERVICES)
        if not any(item in tools for item in covering):
            raise ValueError(
                "capability %r has no covering tool/service" % capability
            )


def validate_removal_groups() -> None:
    """Fail closed when groups are unordered, overlap wrongly, or skip gates."""
    seen_ids = [str(group["id"]) for group in REMOVAL_GROUPS]
    if seen_ids != ["A", "B", "C", "D"]:
        raise ValueError("removal groups must be ordered A-D, got %r" % seen_ids)
    known = set(EXCLUDED_SUBSYSTEMS)
    covered: set[str] = set()
    for group in REMOVAL_GROUPS:
        for key in ("id", "title", "files", "excludes", "risk", "benchmark_gate"):
            if key not in group:
                raise ValueError("group %r missing key %r" % (group.get("id"), key))
        for subsystem in group["excludes"]:  # type: ignore[union-attr]
            if subsystem not in known:
                raise ValueError("unknown excluded subsystem: %r" % subsystem)
            covered.add(subsystem)
    # Every excluded subsystem must be assigned to at least one group.
    missing = known - covered
    if missing:
        raise ValueError("subsystems without a removal group: %s" % sorted(missing))
    # Snapshot backend and watcher must be lazified, never silently dropped.
    group_b = next(g for g in REMOVAL_GROUPS if g["id"] == "B")
    excludes_b = set(group_b["excludes"])  # type: ignore[arg-type]
    for risky in ("snapshot-worktree-backend", "file-watcher-impl"):
        if risky not in excludes_b:
            raise ValueError("%s must be handled in group B" % risky)


def validate_no_provider_rewrite(extra_files: tuple[str, ...] = ()) -> None:
    """Fail closed when the plan introduces a custom provider/API client."""
    forbidden = ("direct-provider-client", "custom-llm-client", "openai-fetch-shim")
    for name in list(FORK_PR_PLAN["new_files"]) + list(extra_files):  # type: ignore[union-attr]
        lowered = str(name).lower()
        for marker in forbidden:
            if marker in lowered:
                raise ValueError("custom provider rewrite is out of scope: %r" % name)
    # The provider service itself must stay retained.
    if "Provider" not in RETAINED_SERVICES or "LLM" not in RETAINED_SERVICES:
        raise ValueError("Provider/LLM services must stay retained")


def check_source_against_boundary(source: str, filename: str) -> list[str]:
    """Return forbidden static imports found in source for a coding file.

    Only top-level static ``import ... from "..."`` lines count; dynamic
    ``import("...")`` is the approved lazy mechanism and is ignored.
    """
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


def assert_source_clean(source: str, filename: str) -> None:
    """Fail closed when a coding-path source pulls an excluded module."""
    violations = check_source_against_boundary(source, filename)
    if violations:
        raise ValueError(
            "coding file %r statically imports excluded modules: %s"
            % (filename, sorted(set(violations)))
        )


def coding_config_baseline() -> dict:
    """Return a deep copy of the step-0 config-only baseline."""
    return json.loads(json.dumps(CODING_CONFIG_BASELINE))


def benchmark_delta(baseline_peak_kb: int, variant_peak_kb: int) -> dict[str, object]:
    """Compute the source-level memory delta between two peaks.

    Positive ``saved_kb`` means the variant saved memory. Fails closed on
    non-positive inputs (a benchmark peak of zero means "not measured").
    """
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
    plan = FORK_PR_PLAN
    return [
        "git fetch origin %s" % plan["base_branch"],
        "git checkout %s" % plan["base_branch"],
        "git checkout -b %s" % plan["proposed_branch"],
        "apply: add %s" % ", ".join(plan["new_files"]),  # type: ignore[union-attr]
        "apply: gate %s" % ", ".join(plan["modified_files"]),  # type: ignore[union-attr]
        "bun run typecheck && bun test coding-only subset",
        "python3 automation/memory_benchmark.py --include-network per group A-D",
        "gh pr create --base %s --head %s --title %r"
        % (plan["base_branch"], plan["proposed_branch"], plan["title"]),
    ]
