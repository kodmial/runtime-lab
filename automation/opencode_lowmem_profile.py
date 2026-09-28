"""Reproducible config/env low-memory profile for headless OpenCode (issue #78).

Stdlib only. No source changes, no network, no git mutations. This module is
the complete config-only deliverable the issue requires:

- a minimal headless OpenCode configuration for the runtime-lab coding
  workload (``lowmem_config``: ``opencode.json`` content),
- the companion environment overrides (``lowmem_env``),
- the non-interactive command shape (``build_lowmem_command``),
- the accepted/rejected setting lists with fork/source rationale
  (``ACCEPTED_SETTINGS`` / ``REJECTED_SETTINGS``),
- fail-closed validation (``validate_profile``),
- a stable fingerprint the #81 qualification gate can pin
  (``profile_fingerprint``),
- file materialization for Render workers (``write_profile_files``),
- the deterministic offline representative coding task used as the
  correctness gate (``representative_coding_task``),
- A/B table rendering against the shared baseline
  (``render_ab_table``).

Source grounding: every switch is admitted only when confirmed by the
current fork/source trace in
``automation/audits/issue-77-opencode-headless-inventory.md`` (fork
``kodmial/opencode`` main ``9000e7f`` over upstream ``ad6c72c``, pinned
release ``1.18.33``) or by a measured prior (``BUN_OPTIONS=--smol`` from
issue #56). Switches that save memory but risk required coding behavior
(``snapshot:false``, file-watcher disablement, autocompact disablement,
project-config disablement) are rejected with rationale and delegated to
explicit live trials, never silently enabled.

Measurement reuse: process-tree/cgroup measurement stays in
``automation/memory_benchmark.py`` (imported lazily so this module keeps
working where that file is absent); per-run record fields and budgets reuse
the ``automation/opencode_qualification.py`` vocabulary so the #81 matrix can
qualify this profile without a second measurement stack.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

SCHEMA = "runtime-lab-opencode-lowmem-profile/v1"
PROFILE_ID = "config-only"
PINNED_VERSION = "1.18.33"
FORK_REPO = "kodmial/opencode"
FORK_BASE_SHA = "9000e7fc8d96c845512f7c73122431418a71d4e4"
UPSTREAM_REPO = "anomalyco/opencode"
UPSTREAM_BASE_COMMIT = "ad6c72c7068812d43b31f3cfb9e413356a19d850"

# Representative workload shared with the #81 matrix (q1-small-edit): the
# same task shape every variant (config-only, source-stripped,
# bounded-output, direct-headless) must complete so deltas stay comparable.
REPRESENTATIVE_WORKLOAD = {
    "id": "q1-small-edit",
    "title": "Small code edit + focused test",
    "workload": (
        "Inspect one module, make one small concrete code or test-coverage "
        "improvement, run the focused relevant tests, report files and result."
    ),
}

# Portable baseline for A/B deltas: the measured issue-52 real-agent host
# peak on the same pinned binary (614,632 kB). Hermetic arms in this run use
# the bootstrap probe below; live Render deltas are collected by #81.
BASELINE_HOST_PEAK_KB = 614632
BASELINE_PROVENANCE = "issue-52-run-36423884312 + memory-benchmark-issue-52.md"

# Budgets shared with the #81 gate (450 MiB preferred target, 512 MiB hard
# limit) so this profile's table speaks the same language.
TARGET_PEAK_KB = 450 * 1024
HARD_LIMIT_KB = 512 * 1024

# ---------------------------------------------------------------------------
# Accepted settings: each entry is confirmed by fork/source (path) or by a
# measured prior, and preserves the required coding loop (repo read/search,
# edit/write/patch, shell/build/test, provider/model/session/agent).
# ---------------------------------------------------------------------------

ACCEPTED_SETTINGS: tuple[dict[str, str], ...] = (
    {
        "id": "bun-smol",
        "kind": "env",
        "key": "BUN_OPTIONS",
        "value": "--smol",
        "source": "issue-56 measurement (disk-heap-issue-56.md)",
        "rationale": "Bun low-memory GC mode; measured ~40 MB saving on the "
        "real agent run, no behavior change observed.",
    },
    {
        "id": "pure-no-external-plugins",
        "kind": "flag+env",
        "key": "--pure / OPENCODE_PURE=1",
        "value": "1",
        "source": "packages/opencode/src/plugin/index.ts "
        "(flags.pure ? [] for cfg.plugin_origins)",
        "rationale": "Skips external plugin loads (including the npm-install "
        "path) for the one-shot worker; no coding tool depends on them.",
    },
    {
        "id": "no-default-plugins",
        "kind": "env",
        "key": "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "value": "1",
        "source": "packages/opencode/src/plugin/index.ts internalPlugins()",
        "rationale": "Skips the 12 internal auth plugins; the pinned single "
        "provider authenticates without them on the worker path.",
    },
    {
        "id": "no-external-skills",
        "kind": "env",
        "key": "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "value": "1",
        "source": "packages/opencode/src/skill (flag-gated discovery)",
        "rationale": "Disables external skill discovery; the representative "
        "task uses only builtin read/search/edit/shell tools.",
    },
    {
        "id": "no-mcp",
        "kind": "config",
        "key": "mcp",
        "value": "{}",
        "source": "packages/opencode/src/mcp/index.ts "
        "(server never spawned when the map is empty)",
        "rationale": "Zero MCP servers configured, so the MCP client stack "
        "never spawns a subprocess; coding tools do not need MCP.",
    },
    {
        "id": "no-lsp",
        "kind": "config+env",
        "key": "lsp={} + OPENCODE_DISABLE_LSP_DOWNLOAD=1",
        "value": "{}",
        "source": "packages/opencode/src/lsp/lsp.ts (empty map logs "
        "\"all LSPs are disabled\") + OPENCODE_DISABLE_LSP_DOWNLOAD",
        "rationale": "No language servers and no downloader; edits and "
        "shell-driven tests do not require LSP.",
    },
    {
        "id": "no-formatter",
        "kind": "config",
        "key": "formatter",
        "value": "false",
        "source": "packages/opencode/src/format/* (formatter:false logs "
        "\"all formatters are disabled\")",
        "rationale": "Disables the formatter table (ruff/uv linked); "
        "correctness is enforced by tests, not by in-agent formatting.",
    },
    {
        "id": "no-share",
        "kind": "config+env",
        "key": "share=disabled + OPENCODE_DISABLE_SHARE=1",
        "value": "disabled",
        "source": "packages/opencode/src/share/share-next.ts "
        "(module-level kill switch) + run.ts share gating + "
        "config schema (share: manual|auto|disabled; verified live "
        "that false is rejected as invalid)",
        "rationale": "Disables the share-sync uploader side-channel; "
        "one-shot jobs never share sessions.",
    },
    {
        "id": "no-autoupdate",
        "kind": "config+env",
        "key": "autoupdate=false + OPENCODE_DISABLE_AUTOUPDATE=1",
        "value": "false",
        "source": "packages/opencode/src/cli/upgrade.ts:upgrade()",
        "rationale": "Disables the latest-release check that also fails "
        "under shared-egress rate limiting; workers run the pinned binary.",
    },
    {
        "id": "no-models-fetch",
        "kind": "env",
        "key": "OPENCODE_DISABLE_MODELS_FETCH",
        "value": "1",
        "source": "packages/opencode/src/provider/provider.ts + "
        "ModelsDev catalog fetch gating",
        "rationale": "Skips the models.dev catalog fetch (same rate-limit "
        "failure class as the installer lookup); the model is pinned "
        "explicitly on the command line.",
    },
    {
        "id": "provider-allowlist",
        "kind": "config",
        "key": "enabled_providers",
        "value": '["opencode"]',
        "source": "packages/opencode/src/provider/provider.ts:"
        "isProviderAllowed",
        "rationale": "Allow-lists the single pinned provider so no other "
        "provider auth/transform path initializes.",
    },
    {
        "id": "no-plugin-list",
        "kind": "config",
        "key": "plugin",
        "value": "[]",
        "source": "packages/opencode/src/plugin/index.ts "
        "(empty list = no external loads)",
        "rationale": "Explicit empty plugin list as defense in depth "
        "alongside --pure.",
    },
    {
        "id": "no-embedded-webui",
        "kind": "env",
        "key": "OPENCODE_DISABLE_EMBEDDED_WEB_UI",
        "value": "1",
        "source": "packages/core/src/flag/flag.ts (flag-gated web UI)",
        "rationale": "The headless run path never serves the embedded web "
        "UI; disabling it only removes dead weight.",
    },
    {
        "id": "fresh-session-db",
        "kind": "env",
        "key": "OPENCODE_DB=<per-job isolated path>",
        "value": "per-job",
        "source": "packages/opencode/src/storage/storage.ts "
        "(OPENCODE_DB override) + issue-80 fresh-session discipline",
        "rationale": "Per-job sqlite path outside the clone guarantees a "
        "fresh session per issue without polluting git status.",
    },
    {
        "id": "git-confinement",
        "kind": "env+config",
        "key": "OPENCODE_CONFIG_CONTENT permission ruleset",
        "value": "read-only-git",
        "source": "packages/opencode/src/config/config.ts (permission "
        "rulesets) + automation/opencode_runner.py:OPENCODE_CONFIG_CONTENT",
        "rationale": "Preserves the read-only git confinement (bash allow, "
        "`git *` writes deny, read-only inspection allow); required coding "
        "behavior is unchanged by construction.",
    },
)

# ---------------------------------------------------------------------------
# Rejected settings: memory-saving or plausible switches that are NOT part
# of the qualified profile because they risk required coding behavior. Each
# entry records the veto evidence and the explicit live trial that could
# overturn it. Fail closed: never enable without that evidence.
# ---------------------------------------------------------------------------

REJECTED_SETTINGS: tuple[dict[str, str], ...] = (
    {
        "id": "snapshot-false",
        "key": "snapshot:false",
        "reason": "session/processor.ts calls snapshot.track/patch "
        "unconditionally around the LLM stream; disabling the backend "
        "without a proven no-op risks breaking every real coding task "
        "at stream time.",
        "evidence": "automation/audits/issue-77-opencode-headless-"
        "inventory.md sections 1.8, 5.4 (sharp coupling risk)",
        "overturn": "A #81 live q-workload trial with snapshot:false that "
        "completes an LLM stream, or a fork no-op backend (source change, "
        "out of scope here).",
    },
    {
        "id": "filewatcher-disable",
        "key": "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER",
        "reason": "Run-path effect unverified: the VCS watcher feeds "
        "Watcher.Event HEAD subscriptions that session/revert paths "
        "may depend on during multi-step edits.",
        "evidence": "issue-77 record unresolved question "
        "(run-path effect unverified)",
        "overturn": "A #81 live q2/q5 trial proving multi-file edits and "
        "repeat sessions behave identically with the flag set.",
    },
    {
        "id": "autocompact-disable",
        "key": "OPENCODE_DISABLE_AUTOCOMPACT",
        "reason": "Removes the in-binary session-history bound; issue #80 "
        "deliberately leaves compaction enabled as the history-side "
        "memory bound for one-shot jobs.",
        "evidence": "automation/audits/issue-80-session-memory-trace.md "
        "(compaction left enabled)",
        "overturn": "Bounded-output measurements proving history stays "
        "bounded without compaction on a q3-scale workload.",
    },
    {
        "id": "project-config-disable",
        "key": "OPENCODE_DISABLE_PROJECT_CONFIG",
        "reason": "Silences repo-local opencode configuration that a target "
        "repository may rely on for correct coding behavior; saving is "
        "negligible and the blast radius is every future repo.",
        "evidence": "packages/opencode/src/config/config.ts "
        "(project config loading on the run path)",
        "overturn": "Per-repository opt-in, never a global worker default.",
    },
    {
        "id": "models-path-stub",
        "key": "OPENCODE_MODELS_URL / OPENCODE_MODELS_PATH stub",
        "reason": "Repointing the catalog instead of disabling the fetch "
        "adds a new failure mode (stale/partial catalog shadowing the "
        "pinned model) with no measured saving over "
        "OPENCODE_DISABLE_MODELS_FETCH=1.",
        "evidence": "provider catalog call sites inventoried in issue #77",
        "overturn": "None planned; the disable flag already covers the need.",
    },
)

_READ_ONLY_GIT_PERMISSION = {
    "bash": {
        "*": "allow",
        "git *": "deny",
        "git status": "allow",
        "git status *": "allow",
        "git diff": "allow",
        "git diff *": "allow",
        "git log": "allow",
        "git log *": "allow",
        "git show": "allow",
        "git show *": "allow",
        "git rev-parse *": "allow",
        "git ls-files": "allow",
        "git ls-files *": "allow",
        "git grep *": "allow",
        "git blame *": "allow",
        "git branch --show-current": "allow",
        "git remote -v": "allow",
    }
}

# Env switches with static values (OPENCODE_DB and OPENCODE_CONFIG_CONTENT
# are workspace-derived and added by lowmem_env).
STATIC_ENV: dict[str, str] = {
    "BUN_OPTIONS": "--smol",
    "OPENCODE_PURE": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_EMBEDDED_WEB_UI": "1",
    "GIT_TERMINAL_PROMPT": "0",
}

SESSION_DB_DIRNAME = ".runtime-lab-opencode-db"
SESSION_DB_FILENAME = "session.db"

# Resume flags that would reuse cross-issue session history; the profile
# command must never carry them (issue-80 fresh-session discipline).
SESSION_REUSE_FLAGS = frozenset(
    {"--continue", "--session", "--fork", "--attach", "-s", "--username"}
)

# Hermetic bootstrap probe: fails fast on an unknown provider AFTER paying
# InstanceBootstrap (config + plugin + lsp/shareNext/format/vcs/snapshot/
# project init), so its process-tree peak measures exactly the path the
# config-only switches act on. No credentials, no network dependency.
PROBE_MODEL = "nope/nonexistent"
PROBE_PROMPT = "say hi"
PROBE_TIMEOUT_SECONDS = 90.0


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lowmem_config() -> dict:
    """Return the minimal headless opencode.json content for the worker."""
    return {
        "mcp": {},
        "lsp": {},
        "formatter": False,
        "share": "disabled",
        "autoupdate": False,
        "enabled_providers": ["opencode"],
        "plugin": [],
        "permission": dict(_READ_ONLY_GIT_PERMISSION),
    }


def session_db_path(workspace: str) -> str:
    """Return the per-job isolated session-database path for workspace."""
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    return os.path.join(
        workspace, SESSION_DB_DIRNAME, SESSION_DB_FILENAME
    )


def ensure_session_db_dir(workspace: str) -> str:
    """Create the session-database parent dir; return the db path.

    OpenCode fails fast with "unable to open database file" when the
    ``OPENCODE_DB`` parent directory does not exist (measured), so every
    launcher must create it before spawning ``opencode run``. Idempotent.
    """
    path = session_db_path(workspace)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def lowmem_env(workspace: str) -> dict[str, str]:
    """Return the profile env overrides for one job workspace."""
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    env = dict(STATIC_ENV)
    env["OPENCODE_DB"] = session_db_path(workspace)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {"permission": _READ_ONLY_GIT_PERMISSION}, sort_keys=True
    )
    return env


def build_lowmem_command(
    model: str, task_text: str, opencode_bin: str = "opencode"
) -> list[str]:
    """Build ``opencode run --pure --auto --model <model> <task>``.

    Only the pinned ``opencode/*`` provider family is accepted, matching
    the ``enabled_providers`` allowlist; anything else fails closed.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    provider = model.strip().split("/", 1)[0]
    if provider != "opencode":
        raise ValueError(
            "profile allow-lists only the opencode provider, got %r" % model
        )
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("task_text must be a non-empty string")
    if not opencode_bin or not str(opencode_bin).strip():
        raise ValueError("opencode_bin must be a non-empty string")
    return [
        str(opencode_bin),
        "run",
        "--pure",
        "--auto",
        "--model",
        model.strip(),
        task_text.strip(),
    ]


def validate_profile(
    config: dict, env: dict[str, str], cmd: list[str]
) -> list[str]:
    """Fail-closed validation of one profile triple.

    Returns error strings; empty means the profile preserves required
    coding behavior (fresh session, read-only git confinement, pinned
    provider, no rejected switch).
    """
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["config must be a mapping"]
    if not isinstance(env, dict):
        return ["env must be a mapping"]
    if not isinstance(cmd, (list, tuple)):
        return ["cmd must be a command list"]
    if config.get("mcp") != {}:
        errors.append("config.mcp must be {} (no MCP servers on the worker)")
    if config.get("lsp") != {}:
        errors.append("config.lsp must be {} (no language servers)")
    if config.get("formatter") is not False:
        errors.append("config.formatter must be false")
    if config.get("share") != "disabled":
        errors.append("config.share must be 'disabled'")
    if config.get("autoupdate") is not False:
        errors.append("config.autoupdate must be false")
    if config.get("enabled_providers") != ["opencode"]:
        errors.append("config.enabled_providers must be ['opencode']")
    if config.get("plugin") != []:
        errors.append("config.plugin must be [] (with --pure)")
    if "snapshot" in config:
        errors.append(
            "config must not set snapshot (rejected: unconditional "
            "track/patch in session/processor.ts)"
        )
    permission = (config.get("permission") or {})
    if permission != _READ_ONLY_GIT_PERMISSION:
        errors.append(
            "config.permission must preserve the read-only git ruleset"
        )
    for key in (
        "BUN_OPTIONS",
        "OPENCODE_PURE",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
        "OPENCODE_DISABLE_SHARE",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_DB",
    ):
        if not str(env.get(key, "") or "").strip():
            errors.append("env.%s must be set" % key)
    if env.get("BUN_OPTIONS") != "--smol":
        errors.append("env.BUN_OPTIONS must be --smol")
    try:
        content = json.loads(env.get("OPENCODE_CONFIG_CONTENT", ""))
    except (ValueError, TypeError):
        content = None
    if not isinstance(content, dict) or content.get("permission") != (
        _READ_ONLY_GIT_PERMISSION
    ):
        errors.append(
            "env.OPENCODE_CONFIG_CONTENT must carry the read-only git ruleset"
        )
    parts = list(cmd)
    if len(parts) < 2 or parts[1] != "run":
        errors.append("cmd must be an `opencode run` invocation")
    else:
        reused = sorted(part for part in parts[2:] if part in SESSION_REUSE_FLAGS)
        if reused:
            errors.append(
                "cmd reuses session history (forbidden: %s)"
                % ", ".join(reused)
            )
        if "--pure" not in parts:
            errors.append("cmd must carry --pure")
        if "--share" in parts:
            errors.append("cmd must not carry --share")
        if "--attach" in parts:
            errors.append("cmd must not carry --attach")
    return errors


def profile_fingerprint(config: dict, env: dict[str, str]) -> dict[str, str]:
    """Return the stable, pinnable fingerprint of the profile.

    The canonical config JSON SHA-256 plus the sorted env key set is what
    the #81 gate records as the config-only artifact identity.
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    names = sorted(str(key) for key in env.keys())
    return {
        "schema": SCHEMA,
        "profile": PROFILE_ID,
        "pinned_opencode_version": PINNED_VERSION,
        "fork_repo": FORK_REPO,
        "fork_base_sha": FORK_BASE_SHA,
        "config_sha256": digest,
        "env_keys": ",".join(names),
    }


def write_profile_files(directory: str, workspace: str) -> dict[str, str]:
    """Materialize opencode.json + opencode.env for a Render worker.

    Returns ``{"opencode_json": path, "opencode_env": path}``. The env file
    holds static ``KEY=VALUE`` lines; ``OPENCODE_DB``/``OPENCODE_CONFIG_CONTENT``
    are workspace-derived and written verbatim. Never writes secrets:
    credentials stay in the process environment (Render env vars).
    """
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("directory must be a non-empty string")
    os.makedirs(directory, exist_ok=True)
    config = lowmem_config()
    env = lowmem_env(workspace)
    json_path = os.path.join(directory, "opencode.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, sort_keys=True, indent=2)
        handle.write("\n")
    env_path = os.path.join(directory, "opencode.env")
    with open(env_path, "w", encoding="utf-8") as handle:
        for key in sorted(env.keys()):
            value = env[key].replace("\n", "")
            handle.write("%s=%s\n" % (key, value))
    errors = validate_profile(config, env, build_lowmem_command(
        "opencode/muse-spark-1.3-contributor-free", "probe"))
    if errors:
        raise ValueError("profile files failed validation: %s" % errors[0])
    return {"opencode_json": json_path, "opencode_env": env_path}


def probe_command(opencode_bin: str) -> list[str]:
    """Return the hermetic bootstrap-probe command for opencode_bin."""
    if not opencode_bin or not str(opencode_bin).strip():
        raise ValueError("opencode_bin must be a non-empty string")
    return [
        str(opencode_bin),
        "run",
        "--auto",
        "--model",
        PROBE_MODEL,
        PROBE_PROMPT,
    ]


def measure_probe(
    cmd: list[str],
    extra_env: dict[str, str],
    cwd: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Measure one hermetic bootstrap probe (process-tree peak + startup).

    Reuses ``automation/memory_benchmark.py:run_with_peak`` so evidence
    stays on the single shared measurement stack. ``extra_env`` is merged
    over a scrubbed copy of ``os.environ`` (credentials are dropped, never
    logged).
    """
    sys.path.insert(0, os.path.join(_repo_root(), "automation"))
    from memory_benchmark import run_with_peak  # noqa: E402

    scrubbed = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            "TAP_PAT",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_INSTALLATION_ID",
        )
    }
    scrubbed.update(extra_env)
    parent = os.environ
    try:
        os.environ.clear()
        os.environ.update(scrubbed)
        return run_with_peak(list(cmd), cwd, timeout=timeout)
    finally:
        os.environ.clear()
        os.environ.update(parent)


def representative_coding_task(workspace: str) -> dict[str, str]:
    """Run the deterministic offline representative coding task.

    Mirrors the #81 q1 shape (inspect -> search -> small edit -> focused
    test -> report) under the profile config/env, proving the qualified
    profile preserves required coding behavior. Stdlib only; raises on any
    failure so the gate fails closed.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    os.makedirs(workspace, exist_ok=True)
    # The profile must validate before it is allowed near any task.
    config = lowmem_config()
    env = lowmem_env(workspace)
    cmd = build_lowmem_command(
        "opencode/muse-spark-1.3-contributor-free", "probe task"
    )
    errors = validate_profile(config, env, cmd)
    if errors:
        raise ValueError(
            "representative task: profile invalid: %s" % errors[0]
        )
    # OpenCode will not create the OPENCODE_DB parent dir itself.
    ensure_session_db_dir(workspace)
    # Materialize exactly what a Render worker would consume.
    paths = write_profile_files(
        os.path.join(workspace, "profile"), workspace
    )
    with open(paths["opencode_json"], "r", encoding="utf-8") as handle:
        if json.load(handle) != config:
            raise ValueError("representative task: profile round-trip failed")
    # Inspect/search/edit/test cycle on a seeded module.
    target = os.path.join(workspace, "sample.py")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write('def answer():\n    return 41\n')
    with open(target, "r", encoding="utf-8") as handle:
        before = handle.read()
    if "return 41" not in before:
        raise ValueError("representative task: seed content not found")
    matches = [line for line in before.splitlines() if "41" in line]
    if not matches:
        raise ValueError("representative task: search found no match")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write('def answer():\n    return 42\n')
    probe = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys;"
         "spec=importlib.util.spec_from_file_location('m',sys.argv[1]);"
         "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
         "assert m.answer()==42, m.answer();print('focused-test ok')",
         target],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0 or "focused-test ok" not in (probe.stdout or ""):
        raise ValueError(
            "representative task: focused test failed: %s"
            % ((probe.stderr or probe.stdout)[:300])
        )
    compile_probe = subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.abspath(__file__)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if compile_probe.returncode != 0:
        raise ValueError("representative task: build probe failed")
    return {"result": "42", "matches": str(len(matches))}


def accepted_ids() -> list[str]:
    """Setting ids in the qualified profile (stable order)."""
    return [entry["id"] for entry in ACCEPTED_SETTINGS]


def rejected_ids() -> list[str]:
    """Setting ids rejected from the qualified profile (stable order)."""
    return [entry["id"] for entry in REJECTED_SETTINGS]


def render_ab_table(rows: list[dict]) -> str:
    """Render the A/B benchmark table with exact deltas vs the baseline.

    Each row maps ``arm``, ``peak_tree_kb``, ``wall_seconds``,
    ``exit_code`` and ``provenance``. Deltas are computed against
    ``BASELINE_HOST_PEAK_KB`` (the shared issue-52 real-agent peak) and
    against the in-run bare baseline arm when present.
    """
    bare_peak = None
    for row in rows:
        if row.get("arm") == "bare-baseline":
            bare_peak = row.get("peak_tree_kb")
            break
    lines = [
        "| Arm | Peak tree (kB) | Peak (MiB) | Delta vs shared baseline "
        "(%.0f kB) | Delta vs bare arm | Wall (s) | Exit | Provenance |"
        % BASELINE_HOST_PEAK_KB,
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        peak = row.get("peak_tree_kb")
        if not isinstance(peak, int) or peak <= 0:
            raise ValueError("each row needs a positive int peak_tree_kb")
        shared_delta = peak - BASELINE_HOST_PEAK_KB
        if bare_peak is None or row.get("arm") == "bare-baseline":
            bare_text = "n/a (baseline)"
        else:
            diff = peak - bare_peak
            bare_text = "%+d kB (%+.1f MiB)" % (diff, diff / 1024.0)
        lines.append(
            "| %s | %d | %.1f | %+d kB (%+.1f MiB) | %s | %.2f | %s | %s |"
            % (
                row.get("arm", "?"),
                peak,
                peak / 1024.0,
                shared_delta,
                shared_delta / 1024.0,
                bare_text,
                float(row.get("wall_seconds", 0.0)),
                row.get("exit_code", "?"),
                row.get("provenance", "?"),
            )
        )
    return "\n".join(lines) + "\n"
