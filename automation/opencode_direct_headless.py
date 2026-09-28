"""Direct headless OpenCode path contract (issue #89).

Stdlib-only, offline, no network or git mutations. This module is the
machine-readable contract for the parallel memory hypothesis P0 track:

- a dedicated direct headless entrypoint in ``kodmial/opencode`` that keeps
  the existing OpenCode session/agent/provider/tool loop but bypasses
  unnecessary internal HTTP/server/SDK plumbing;
- the retained capability set (repository inspection/search, edits/patches,
  shell/build/test, provider calls, the normal agent loop);
- the bypassed plumbing set (in-process HTTP dispatch, the full instance
  HTTP route table, TCP/mDNS listeners, share-sync/models-fetch HTTP,
  MCP SDK client stack, lazified unused providers);
- the static-import boundaries that prove the bypass (not just
  permission-denial or config flags);
- the step-0 config-only baseline (no fork change) measured first;
- the representative coding workload and the telemetry contract reused
  from the other memory experiments (no second measurement stack);
- the fork patch plan for the workflow to materialize (issue execution
  never pushes or opens PRs itself).

Source grounding: fork ``kodmial/opencode`` baseline
``automation/opencode-fork.baseline.json`` (fork ``main`` ``9000e7f`` over
upstream ``anomalyco/opencode``); startup trace in
``automation/audits/issue-77-opencode-headless-inventory.md``. The normal
``opencode run`` path always pays ``InstanceBootstrap.run`` and then
dispatches through an in-process HTTP server (``Server.Default().app.fetch``
with ``baseUrl: "http://opencode.internal"``); the direct path calls the
same session/agent/provider/tool services without that HTTP hop.

No custom direct provider/API client is introduced here: all model access
stays through the existing ``provider/provider.ts`` + ``session/llm`` path.
A direct provider rewrite is a separate architectural option and this
module fails closed if one is added.

Orthogonality: issue #79 strips tool/dependency subsystems, issue #80
bounds session/output retention, issue #78 measures the config-only delta.
This track bypasses HTTP/server/SDK plumbing only, so its entrypoint and
build target are distinct (``run-direct`` / ``build:direct`` /
``dist/direct``) and combinable with the other tracks later.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SCHEMA = "runtime-lab-opencode-direct-headless/v1"

# Fork revision this contract was authored against (from
# automation/opencode-fork.baseline.json; upstream successor rename
# sst/opencode -> anomalyco/opencode preserved).
FORK_REPO = "kodmial/opencode"
FORK_BASE_SHA = "9000e7fc8d96c845512f7c73122431418a71d4e4"
UPSTREAM_REPO = "anomalyco/opencode"
UPSTREAM_BASE_COMMIT = "75e1e7ae310dc36c86c920e8997d0e1181e24a88"
PINNED_VERSION = "1.18.33"

# Normal headless path source refs (from the #77 inventory; every claim
# below is traceable to these fork paths at FORK_BASE_SHA).
NORMAL_ENTRYPOINT = "packages/opencode/src/index.ts"
NORMAL_RUN_COMMAND = "packages/opencode/src/cli/cmd/run.ts"
NORMAL_EFFECT_CMD = "packages/opencode/src/cli/effect-cmd.ts"
NORMAL_APP_RUNTIME = "packages/opencode/src/effect/app-runtime.ts"
NORMAL_BOOTSTRAP = "packages/opencode/src/project/bootstrap.ts"
NORMAL_INSTANCE_STORE = "packages/opencode/src/project/instance-store.ts"
NORMAL_SERVER = "packages/opencode/src/server/server.ts"
NORMAL_HTTP_ROUTES = (
    "packages/opencode/src/server/routes/instance/httpapi/server.ts"
)
NORMAL_PROCESSOR = "packages/opencode/src/session/processor.ts"
NORMAL_BUILD_SCRIPT = "packages/opencode/script/build.ts"
IN_PROCESS_BASE_URL = "http://opencode.internal"

# Dedicated direct headless entrypoint / build target (new files in the
# fork; distinct from the #79 coding-only files so tracks stay isolated
# and combinable).
DIRECT_ENTRYPOINT = "packages/opencode/src/cli/cmd/run-direct.ts"
DIRECT_RUNNER = "packages/opencode/src/direct/direct-runner.ts"
DIRECT_BOOTSTRAP = "packages/opencode/src/project/bootstrap-direct.ts"
DIRECT_SERVER = "packages/opencode/src/server/routes/instance/httpapi/server-direct.ts"
DIRECT_BUILD_SCRIPT = "packages/opencode/script/build-direct.ts"
DIRECT_BUILD_TARGET = "build:direct"
DIRECT_OUTDIR = "dist/direct"

# Full-build files the direct variant gates (laziness only; the full
# build keeps working so the experiment is revertible).
MODIFIED_FILES = (
    "packages/opencode/package.json",
    NORMAL_ENTRYPOINT,
    NORMAL_APP_RUNTIME,
    NORMAL_HTTP_ROUTES,
    "packages/opencode/src/tool/registry.ts",
    "packages/opencode/src/provider/provider.ts",
)

# Capabilities the direct path must preserve (issue #89 constraints).
RETAINED_CAPABILITIES = (
    "repo-inspection-search",  # read/glob/grep
    "file-edit-patch",  # edit/write/apply_patch
    "shell-build-test",  # shell
    "provider-calls",  # provider/model invocation
    "agent-loop",  # session/agent/event loop incl. failure reaction
)

# Module specifier fragments that must stay reachable from the direct
# entrypoint (the preserved OpenCode loop).
RETAINED_PATTERNS = (
    "provider",
    "session",
    "agent",
    "model",
    "tool/read",
    "tool/grep",
    "tool/glob",
    "tool/edit",
    "tool/write",
    "tool/patch",
    "tool/bash",
    "cli/cmd/run",
)

# Plumbing bypassed by the direct path. Each entry maps a bypassed layer
# to its mechanism; none touches provider semantics or the tool set.
BYPASSED_PLUMBING: tuple[dict[str, str], ...] = (
    {
        "id": "in-process-http-dispatch",
        "layer": "Server.Default().app.fetch Request/Response serialization "
        "between run.ts and the session services",
        "mechanism": "call SessionProcessor/SessionPrompt/ToolRegistry "
        "functions directly from direct-runner.ts; no fetch() round trip",
    },
    {
        "id": "full-instance-route-table",
        "layer": "httpapi/server.ts full domain route set (Account, Skill, "
        "MCP, LSP, Format, Plugin, Question, ShareNext, SessionShare, ...)",
        "mechanism": "server-direct.ts wires only session/agent/provider/tool "
        "routes; the full table stays in the normal build",
    },
    {
        "id": "serve-listeners",
        "layer": "Server.listen() TCP + bonjour-service mDNS (already unused "
        "by run; still linked into the binary)",
        "mechanism": "compile-excluded from the direct graph",
    },
    {
        "id": "share-sync-http",
        "layer": "ShareNext sync uploader + SessionShare HTTP routes",
        "mechanism": "excluded; OPENCODE_DISABLE_SHARE=1 asserted in step-0 config",
    },
    {
        "id": "models-catalog-fetch",
        "layer": "ModelsDev catalog network fetch over HTTP",
        "mechanism": "excluded; OPENCODE_DISABLE_MODELS_FETCH=1 in step-0 config",
    },
    {
        "id": "mcp-sdk-stack",
        "layer": "MCP client stack + @modelcontextprotocol/sdk under a "
        "zero-server config",
        "mechanism": "compile-excluded from the direct graph (empty mcp map "
        "in step-0 config already suppresses runtime use)",
    },
    {
        "id": "unused-ai-sdk-providers",
        "layer": "unused @ai-sdk/* provider imports (pinned provider path kept)",
        "mechanism": "lazified via dynamic import(); provider/provider.ts "
        "interface unchanged",
    },
)

# Static-import boundaries for the direct path. Each direct file must not
# contain a top-level static import matching any forbidden pattern; gated
# uses must be dynamic import() behind flags. Patterns are regex fragments
# matched against import source strings.
STATIC_IMPORT_BOUNDARIES: dict[str, tuple[str, ...]] = {
    DIRECT_ENTRYPOINT: (
        r"cli/cmd/tui",
        r"cli/cmd/web",
        r"cli/cmd/serve",
        r"cli/cmd/acp",
        r"cli/cmd/attach",
        r"@opencode-ai/tui",
        r"bonjour-service",
        r"@agentclientprotocol/sdk",
    ),
    DIRECT_RUNNER: (
        r"server/server",
        r"httpapi/server",
        r"share/share-next",
        r"share/session",
        r"@/mcp",
        r"@modelcontextprotocol/sdk",
        r"@/lsp/lsp",
    ),
    DIRECT_BOOTSTRAP: (
        r"@/lsp/lsp",
        r"@/share/share-next",
        r"\.\./format",
        r"@/mcp",
        r"@modelcontextprotocol/sdk",
    ),
    DIRECT_SERVER: (
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

# Config-only baseline (step 0, no fork change) measured before any fork
# build. Same flag vocabulary as the #77 inventory and the #79 step-0
# baseline so results compare directly.
DIRECT_CONFIG_BASELINE: dict[str, object] = {
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
        "share": False,
        "autoupdate": False,
        "enabled_providers": ["opencode"],
    },
}

# Representative coding workload: identical prompt to the #79 contract (so
# correctness compares across tracks) and mapped to q1 of the #81
# qualification matrix (small edit + focused test on a real worker).
REPRESENTATIVE_TASK = {
    "prompt": (
        "Search the repository for the constant named FOO_LIMIT, "
        "read the surrounding module, change its value from 10 to 20, "
        "run the module build/tests, fix any failure the change causes, "
        "and report the files changed plus the test result."
    ),
    "required_capabilities": [
        "repo-inspection-search",
        "file-edit-patch",
        "shell-build-test",
        "provider-calls",
        "agent-loop",
    ],
    "qualification_workload": "q1-small-edit",
}

# Capability -> covering retained patterns (proves the retained set is
# sufficient for the representative task).
CAPABILITY_COVERAGE: dict[str, tuple[str, ...]] = {
    "repo-inspection-search": ("tool/read", "tool/grep", "tool/glob"),
    "file-edit-patch": ("tool/edit", "tool/write", "tool/patch"),
    "shell-build-test": ("tool/bash",),
    "provider-calls": ("provider", "model"),
    "agent-loop": ("session", "agent"),
}

FORK_PR_PLAN = {
    "base_repo": FORK_REPO,
    "base_branch": "main",
    "base_sha": FORK_BASE_SHA,
    "proposed_branch": "direct-headless-issue-89",
    "title": "Add direct headless entrypoint bypassing internal HTTP/server routing (issue #89)",
    "new_files": [
        DIRECT_ENTRYPOINT,
        DIRECT_RUNNER,
        DIRECT_BOOTSTRAP,
        DIRECT_SERVER,
        DIRECT_BUILD_SCRIPT,
    ],
    "modified_files": list(MODIFIED_FILES),
}

# Runner-side selection: explicit direct-binary override. When unset or
# pointing at a missing binary, callers must fall back to the normal path
# (fail closed to normal, never to a broken direct binary).
DIRECT_BIN_ENV_VARS = ("RUNNER_OPENCODE_DIRECT_BIN", "OPENCODE_DIRECT_BIN")

IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def bypass_ids() -> list[str]:
    """Bypassed plumbing layer ids in stable order."""
    return [entry["id"] for entry in BYPASSED_PLUMBING]


def direct_files() -> list[str]:
    """New fork files forming the direct headless path."""
    return list(FORK_PR_PLAN["new_files"])  # type: ignore[union-attr]


def direct_config_baseline() -> dict:
    """Return a deep copy of the step-0 config-only baseline."""
    return json.loads(json.dumps(DIRECT_CONFIG_BASELINE))


def direct_env_overrides() -> dict[str, str]:
    """Runner env overrides selecting the direct headless configuration.

    Same kill switches as the step-0 baseline: pure mode, no default
    plugins/skills, no LSP download, no share upload, no autoupdate, no
    models-catalog fetch. Provider/model credentials stay inherited and
    are never set here.
    """
    env = dict(DIRECT_CONFIG_BASELINE["env"])  # type: ignore[union-attr]
    return {str(k): str(v) for k, v in env.items()}


def resolve_direct_binary(raw: str | None = None) -> str | None:
    """Return the direct-binary override, or None when not configured."""
    if raw is not None:
        text = str(raw).strip()
        return text or None
    for env_var in DIRECT_BIN_ENV_VARS:
        value = os.environ.get(env_var, "")
        if value and str(value).strip():
            return str(value).strip()
    return None


def build_normal_command(
    model: str, task_text: str, opencode_bin: str = "opencode"
) -> list[str]:
    """Build the normal headless command (existing path, unchanged)."""
    if not model or not str(model).strip():
        raise ValueError("model must be a non-empty string")
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("task_text must be a non-empty string")
    if not opencode_bin or not str(opencode_bin).strip():
        raise ValueError("opencode_bin must be a non-empty string")
    return [str(opencode_bin), "run", "--auto", "--model", model, task_text.strip()]


def build_direct_command(
    model: str,
    task_text: str,
    direct_bin: str | None = None,
    fallback_bin: str = "opencode",
) -> list[str]:
    """Build the direct headless command with fail-closed fallback.

    When ``direct_bin`` (explicit override or env) names an existing
    executable, the command targets the direct binary; otherwise it falls
    back to the normal path so the experiment is always runnable and
    revertible. The CLI shape (``run --auto --model``) is identical: the
    direct binary exposes the same headless interface, only the internal
    dispatch differs.
    """
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("task_text must be a non-empty string")
    if not model or not str(model).strip():
        raise ValueError("model must be a non-empty string")
    resolved = (direct_bin if direct_bin is not None else resolve_direct_binary()) or ""
    resolved = str(resolved).strip()
    if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
        return [resolved, "run", "--auto", "--model", str(model).strip(), task_text.strip()]
    return build_normal_command(model, task_text, fallback_bin)


def validate_matrix() -> None:
    """Fail closed when retained/bypassed sets overlap or drop requirements."""
    retained = set(RETAINED_PATTERNS)
    bypassed_text = " ".join(
        entry["id"] + " " + entry["layer"] for entry in BYPASSED_PLUMBING
    ).lower()
    # The provider/session/agent loop must never be bypassed.
    for required in ("provider", "session", "agent"):
        if required not in retained:
            raise ValueError("retained set missing required loop marker: %s" % required)
    if "provider/" in bypassed_text and "unused-ai-sdk-providers" not in bypassed_text:
        raise ValueError("bypassed plumbing must not remove the provider path itself")
    for capability in REPRESENTATIVE_TASK["required_capabilities"]:
        if capability not in RETAINED_CAPABILITIES:
            raise ValueError("representative task needs uncovered capability: %r" % capability)
        covering = CAPABILITY_COVERAGE.get(capability, ())
        if not any(item in retained for item in covering):
            raise ValueError("capability %r has no covering retained pattern" % capability)
    seen = [entry["id"] for entry in BYPASSED_PLUMBING]
    if len(set(seen)) != len(seen):
        raise ValueError("duplicate bypassed plumbing ids: %r" % seen)
    for entry in BYPASSED_PLUMBING:
        for key in ("id", "layer", "mechanism"):
            if key not in entry or not str(entry[key]).strip():
                raise ValueError("bypassed entry %r missing %r" % (entry.get("id"), key))


def validate_no_provider_rewrite(extra_files: tuple[str, ...] = ()) -> None:
    """Fail closed when the plan introduces a custom provider/API client."""
    forbidden = ("direct-provider-client", "custom-llm-client", "openai-fetch-shim")
    names = list(FORK_PR_PLAN["new_files"]) + list(extra_files)  # type: ignore[union-attr]
    for name in names:
        lowered = str(name).lower()
        for marker in forbidden:
            if marker in lowered:
                raise ValueError("custom provider rewrite is out of scope: %r" % name)
    # The provider path itself must stay retained.
    if "provider" not in RETAINED_PATTERNS:
        raise ValueError("provider pattern must stay retained")


def check_source_against_boundary(source: str, filename: str) -> list[str]:
    """Return forbidden static imports found in source for a direct file.

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
    """Fail closed when a direct-path source pulls bypassed plumbing."""
    violations = check_source_against_boundary(source, filename)
    if violations:
        raise ValueError(
            "direct file %r statically imports bypassed plumbing: %s"
            % (filename, sorted(set(violations)))
        )


def benchmark_delta(baseline_peak_kb: int, variant_peak_kb: int) -> dict[str, object]:
    """Compute the memory delta between normal-path and direct-path peaks.

    Positive ``saved_kb`` means the direct path saved memory. Fails closed
    on non-positive inputs (a benchmark peak of zero means "not measured").
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


def telemetry_fields() -> list[str]:
    """Per-run measurement fields reused from the #81 qualification gate.

    The direct-headless benchmark records exactly the #81 contract fields
    (cgroup/sampler vocabulary) so its rows compare 1:1 with the other
    memory experiments instead of building a second measurement stack.
    """
    try:
        from opencode_qualification import (  # noqa: E402
            required_measurement_fields as _fields,
        )

        return list(_fields())
    except Exception:
        from automation.opencode_qualification import (  # type: ignore[no-redef]  # noqa: E402
            required_measurement_fields as _fields2,
        )

        return list(_fields2())


def representative_coding_task(workspace: str) -> dict[str, str]:
    """Run the deterministic representative coding task in a workspace.

    Same workload as the #79 contract (inspect/search, modify, build/test
    probe, react to failures) so correctness compares across tracks.
    Stdlib only; raises on any failure.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    os.makedirs(workspace, exist_ok=True)
    target = os.path.join(workspace, "sample.txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("FOO_LIMIT = 10\n")
    with open(target, "r", encoding="utf-8") as handle:
        before = handle.read()
    if "FOO_LIMIT" not in before:
        raise ValueError("representative task: seed content not found")
    matches = [line for line in before.splitlines() if "FOO_LIMIT" in line]
    if not matches:
        raise ValueError("representative task: search found no match")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(before.replace("10", "20"))
    with open(target, "r", encoding="utf-8") as handle:
        after = handle.read()
    if "FOO_LIMIT = 20" not in after:
        raise ValueError("representative task: edit did not persist")
    probe = subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.abspath(__file__)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise ValueError(
            "representative task: build probe failed: %s" % (probe.stderr or probe.stdout)[:300]
        )
    check = subprocess.run(
        [sys.executable, "-c", "print(open(%r).read())" % target],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check.returncode != 0 or "FOO_LIMIT = 20" not in (check.stdout or ""):
        raise ValueError("representative task: test probe failed")
    return {"result": "FOO_LIMIT = 20", "matches": str(len(matches))}


def fork_patch_plan() -> list[dict[str, str]]:
    """Return the additive fork-side patch plan (no existing file rewrites)."""
    return [
        {
            "path": DIRECT_ENTRYPOINT,
            "action": "add",
            "purpose": "Direct headless CLI entry wiring RunCommand to the direct runner.",
        },
        {
            "path": DIRECT_RUNNER,
            "action": "add",
            "purpose": "Direct dispatch: minimal bootstrap then session/agent/provider/tool calls without app.fetch().",
        },
        {
            "path": DIRECT_BOOTSTRAP,
            "action": "add",
            "purpose": "Minimal instance bootstrap (config + minimal plugin + project; gated lsp/share/format).",
        },
        {
            "path": DIRECT_SERVER,
            "action": "add",
            "purpose": "Pruned in-process route surface (session/agent/provider/tool only).",
        },
        {
            "path": DIRECT_BUILD_SCRIPT,
            "action": "add",
            "purpose": "Dedicated bun build target emitting the direct binary to %s." % DIRECT_OUTDIR,
        },
        {
            "path": "packages/opencode/package.json",
            "action": "extend-scripts",
            "purpose": "Add scripts.build:direct alongside the untouched scripts.build.",
        },
    ]


def fork_pr_instructions() -> list[str]:
    """Human steps for the workflow to materialize the fork PR (no git here)."""
    plan = FORK_PR_PLAN
    return [
        "git fetch origin %s" % plan["base_branch"],
        "git checkout %s" % plan["base_branch"],
        "git checkout -b %s" % plan["proposed_branch"],
        "apply: add %s" % ", ".join(plan["new_files"]),  # type: ignore[union-attr]
        "apply: gate %s" % ", ".join(plan["modified_files"]),  # type: ignore[union-attr]
        "bun run typecheck && bun test direct-headless subset",
        "python3 automation/memory_benchmark.py --include-network (normal vs direct binary)",
        "gh pr create --base %s --head %s --title %r"
        % (plan["base_branch"], plan["proposed_branch"], plan["title"]),
    ]
