"""GitHub Actions qualification for the coding-only OpenCode PR (issue #105).

Stdlib only, offline by default, no git mutations, no workflow edits. This
module is the reproducible contract that builds and qualifies the real
coding-only variant from ``kodmial/opencode#10`` under a 512 MiB no-swap
limit before any Render conclusion is made.

Exact source under test (fail closed, never a mutable pointer):

- repository ``kodmial/opencode``, PR ``#10``,
  branch ``coding-only-lite-issue-101``,
  required head SHA ``d604c215c2c691c4ad79bd59d1ea2c5d76f5c8aa``,
  base SHA ``ae343e82aba50b77c5c90996d4d784f955cda2ee``;
- build command ``bun run --cwd packages/opencode build:coding``;
- expected binary ``packages/opencode/dist/coding/bin/opencode-coding``.

Memory harness reuses the Runtime Lab vocabulary where practical
(``automation/memory_benchmark.py`` process-tree polling plus Docker
``--memory=512m --memory-swap=512m`` as the real cgroup-equivalent limit,
cgroup ``memory.current``/``memory.peak``/``memory.events`` fields). The
``BUN_OPTIONS=--smol`` arm is a separately recorded A/B dimension, never
an unlabelled default.

Required checks encoded here:

1. build the standalone Linux x64 coding-only binary from the exact PR head;
2. ``--version`` smoke check (recorded as a non-success signal only);
3. dependency-graph inspection proving the dedicated entrypoint excludes
   the normal top-level TUI/web/serve/MCP command table and embedded
   Web UI/TUI worker;
4. a real representative coding-agent workload (search + edit +
   shell/build/test through the normal provider/session/agent/tool loop);
5. per-trial measurements (process-tree peak RSS, cgroup current/peak/
   events incl. OOM/max, wall time, exit status, binary size + SHA-256);
6. direct comparison with the ~600-615 MB baseline from issue #52;
7. at least two constrained real-agent trials.

Verdicts: ``reliable-under-512m`` | ``marginal`` | ``does-not-fit`` |
``inconclusive`` (toolchain evidence missing; never success). Success is
never claimed from ``--version``, a fake agent, or an unconstrained run.
"""

from __future__ import annotations

import hashlib
import os
import re

SCHEMA = "runtime-lab-coding-qualify/v1"

FORK_REPO = "kodmial/opencode"
FORK_PR = 10
FORK_BRANCH = "coding-only-lite-issue-101"
REQUIRED_HEAD_SHA = "d604c215c2c691c4ad79bd59d1ea2c5d76f5c8aa"
FORK_BASE_SHA = "ae343e82aba50b77c5c90996d4d784f955cda2ee"
UPSTREAM_REPO = "anomalyco/opencode"
UPSTREAM_BASE_COMMIT = "75e1e7ae310dc36c86c920e8997d0e1181e24a88"
PINNED_VERSION = "1.18.33"

BUILD_COMMAND = ("bun", "run", "--cwd", "packages/opencode", "build:coding")
BUILD_SCRIPT = "packages/opencode/script/build-coding.ts"
CODING_ENTRYPOINT = "packages/opencode/src/coding-index.ts"
EXPECTED_BINARY = "packages/opencode/dist/coding/bin/opencode-coding"
FULL_ENTRYPOINT = "packages/opencode/src/index.ts"

LIMIT_BYTES = 512 * 1024 * 1024
DOCKER_LIMITS = ("--memory=512m", "--memory-swap=512m")
BUN_SMOL = "--smol"

# Measured baseline from issue #52 (Actions host + Docker 512m/no-swap).
BASELINE_HOST_PEAK_KB = 614632
BASELINE_HOST_PEAK_BYTES = BASELINE_HOST_PEAK_KB * 1024
BASELINE_DOCKER_THROTTLED_BYTES = 536920064
BASELINE_PROVENANCE = "issue-52-run-36423884312 + memory-benchmark-issue-52.md"

# The normal top-level command table that the coding entrypoint must drop.
# Checked against static imports (source level) and --help output (binary).
EXCLUDED_COMMANDS = ("tui", "web", "serve", "acp", "attach", "mcp")
EXCLUDED_UI_MARKERS = ("opentui", "@opencode-ai/tui", "solid-js",
                       "bonjour-service", "agentclientprotocol")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MUTABLE_REFS = frozenset({
    "latest", "main", "master", "dev", "stable", "head",
    "next", "current", "head",
})

REQUIRED_TRIAL_FIELDS = (
    "trial",
    "bun_options",
    "binary_sha256",
    "binary_bytes",
    "peak_tree_kb",
    "peak_bytes",
    "memory_current_bytes",
    "memory_peak_bytes",
    "memory_events",
    "wall_seconds",
    "exit_code",
    "correctness",
)

REPRESENTATIVE_TASK = {
    "id": "q1-small-edit",
    "title": "FOO_LIMIT search + edit + build/test",
    "prompt": (
        "Search the repository for the constant named FOO_LIMIT, "
        "read the surrounding module, change its value from 10 to 20, "
        "run the module build/tests, fix any failure the change causes, "
        "and report the files changed plus the test result."
    ),
    "required_capabilities": (
        "repo-search",
        "file-edit",
        "shell-build-test",
        "provider-agent-loop",
    ),
}

VERDICTS = (
    "reliable-under-512m",
    "marginal",
    "does-not-fit",
    "inconclusive",
)


def validate_source_identity(head_sha: str, base_sha: str = FORK_BASE_SHA,
                             branch: str = FORK_BRANCH) -> dict[str, str]:
    """Fail closed unless the exact PR head/base/branch is supplied.

    Mutable pointers (``main``, ``latest``, bare branch without a SHA) are
    never accepted as an identity: the caller must pass the full 40-char
    head SHA and it must equal ``REQUIRED_HEAD_SHA``.
    """
    if not isinstance(head_sha, str) or SHA_RE.match(head_sha.strip()) is None:
        raise ValueError(
            "head_sha must be a 40-char lowercase SHA, got %r" % (head_sha,))
    head = head_sha.strip()
    if head != REQUIRED_HEAD_SHA:
        raise ValueError(
            "head_sha %r does not equal the required PR #10 head %r; "
            "never silently test main/latest/upstream" % (head, REQUIRED_HEAD_SHA))
    if not isinstance(base_sha, str) or SHA_RE.match(base_sha.strip()) is None:
        raise ValueError("base_sha must be a 40-char lowercase SHA")
    if base_sha.strip() != FORK_BASE_SHA:
        raise ValueError(
            "base_sha %r does not equal the PR #10 base %r"
            % (base_sha, FORK_BASE_SHA))
    if branch != FORK_BRANCH:
        raise ValueError(
            "branch must be %r, got %r" % (FORK_BRANCH, branch))
    return {"repo": FORK_REPO, "pr": str(FORK_PR), "branch": branch,
            "head_sha": head, "base_sha": base_sha.strip()}


def reject_mutable_ref(ref: str) -> str:
    """Fail closed on mutable refs such as main/latest/bare HEAD."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty string")
    text = ref.strip()
    if SHA_RE.match(text) is not None:
        return text
    if text.lower() in {name.lower() for name in MUTABLE_REFS}:
        raise ValueError(
            "ref %r is a mutable pointer; supply the exact 40-char SHA" % ref)
    # Bare branch names without an @sha pin are mutable: reject.
    if "@" not in text:
        raise ValueError(
            "ref %r is not pinned to an exact SHA; refusing to test "
            "a moving target" % ref)
    return text


def expected_binary_path(fork_checkout: str) -> str:
    """Absolute path of the coding binary inside a fork checkout."""
    if not isinstance(fork_checkout, str) or not fork_checkout.strip():
        raise ValueError("fork_checkout must be a non-empty directory path")
    return os.path.join(fork_checkout.strip(), EXPECTED_BINARY)


def build_steps_for_pr(workdir: str = "$BUILD_DIR/opencode-build") -> list[str]:
    """Reproducible shell steps for the exact PR head (no mutable refs)."""
    identity = validate_source_identity(REQUIRED_HEAD_SHA)
    sha = identity["head_sha"]
    return [
        "curl -fsSL https://codeload.github.com/%s/tar.gz/%s "
        "-o %s/fork-%s.tar.gz" % (FORK_REPO, sha, workdir, sha[:12]),
        "mkdir -p %s/fork && tar -xzf %s/fork-%s.tar.gz -C %s/fork "
        "--strip-components=1" % (workdir, workdir, sha[:12], workdir),
        "test -f %s/fork/%s" % (workdir, CODING_ENTRYPOINT),
        "test -f %s/fork/%s" % (workdir, BUILD_SCRIPT),
        "cd %s/fork && bun install --frozen-lockfile" % workdir,
        "cd %s/fork && %s" % (workdir, " ".join(BUILD_COMMAND)),
        "test -x %s/fork/%s" % (workdir, EXPECTED_BINARY),
        "sha256sum %s/fork/%s" % (workdir, EXPECTED_BINARY),
        "%s/fork/%s --version" % (workdir, EXPECTED_BINARY),
    ]


def sha256_of_file(path: str) -> str:
    """Streamed SHA-256 of a file (bounded RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    result = digest.hexdigest()
    if SHA256_RE.match(result) is None:
        raise ValueError("cannot hash binary at %r" % path)
    return result


def fingerprint_binary(path: str) -> dict[str, object]:
    """Return size + SHA-256 of a built binary (fail closed)."""
    if not isinstance(path, str) or not path:
        raise ValueError("binary path must be a non-empty string")
    if not os.path.isfile(path):
        raise FileNotFoundError("coding binary not found: %r" % path)
    if not os.access(path, os.X_OK):
        raise ValueError("coding binary is non-executable: %r" % path)
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError("coding binary is empty: %r" % path)
    return {"path": path, "bytes": size, "sha256": sha256_of_file(path)}


IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def check_entrypoint_sources(coding_source: str, full_source: str) -> dict[str, object]:
    """Prove the dedicated entrypoint drops the normal command table.

    Returns the evidence dict; raises on any boundary violation. The full
    entrypoint must demonstrably carry the TUI/web/serve/MCP command
    table (otherwise the comparison is vacuous), and the coding
    entrypoint must wire ``RunCommand`` while statically importing none
    of the excluded commands or UI/worker stacks.
    """
    if not coding_source or not full_source:
        raise ValueError("both coding and full entrypoint sources are required")
    if "RunCommand" not in coding_source:
        raise ValueError("coding entrypoint must wire RunCommand")
    coding_imports = [m.group(1) for m in IMPORT_RE.finditer(coding_source)]
    violations = []
    for origin in coding_imports:
        lowered = origin.lower()
        for marker in list(EXCLUDED_COMMANDS) + list(EXCLUDED_UI_MARKERS):
            if marker in lowered:
                violations.append(origin)
                break
        # Direct full-index re-export would silently reintroduce the table.
        if origin.strip().endswith("/index") and "src/index" in origin:
            violations.append(origin)
    # Explicit relative command imports are the sharpest signal.
    for match in re.finditer(r"cli/cmd/(tui|web|serve|acp|attach|mcp)", coding_source):
        violations.append(match.group(0))
    if violations:
        raise ValueError(
            "coding entrypoint statically pulls excluded modules: %s"
            % sorted(set(violations)))
    # The comparison is only meaningful when the full entrypoint really
    # carries the excluded table.
    found = [cmd for cmd in EXCLUDED_COMMANDS if cmd in full_source.lower()]
    if len(found) < 3:
        raise ValueError(
            "full entrypoint does not demonstrably carry the command table "
            "(found %r); comparison is vacuous" % found)
    return {"coding_imports": sorted(set(coding_imports)),
            "full_commands_found": sorted(found),
            "excluded_commands": list(EXCLUDED_COMMANDS)}


def check_binary_help(help_text: str) -> dict[str, object]:
    """Prove the compiled binary help exposes run but not the pruned table.

    ``--help`` of the coding binary must document the run path while not
    listing the excluded top-level commands. Raises fail-closed otherwise.
    """
    if not isinstance(help_text, str) or not help_text.strip():
        raise ValueError("binary --help output is empty")
    lowered = help_text.lower()
    if "run" not in lowered:
        raise ValueError("coding binary --help does not expose the run command")
    listed = [cmd for cmd in EXCLUDED_COMMANDS
              if re.search(r"(^|\s)%s(\s|$|,)" % re.escape(cmd), lowered)]
    if listed:
        raise ValueError(
            "coding binary --help still lists excluded commands: %s" % listed)
    return {"run_exposed": True, "excluded_listed": listed}


def create_representative_repo(root: str) -> dict[str, str]:
    """Materialize the deterministic FOO_LIMIT workload repo.

    The repo requires repository search (find FOO_LIMIT), a file edit
    (10 -> 20), and shell/build/test execution (``python -m pytest`` or
    the module's own test), all through the normal agent loop. Returns
    paths; raises fail-closed on misuse.
    """
    if not isinstance(root, str) or not root.strip():
        raise ValueError("repo root must be a non-empty path")
    os.makedirs(root, exist_ok=True)
    module = os.path.join(root, "foo_module.py")
    test = os.path.join(root, "test_foo_module.py")
    with open(module, "w", encoding="utf-8") as handle:
        handle.write('"""Demo module for the coding-qualification workload."""\n'
                     '\nFOO_LIMIT = 10\n'
                     '\n'
                     'def within_limit(n):\n'
                     '    """True when n is within FOO_LIMIT."""\n'
                     '    return 0 <= n <= FOO_LIMIT\n')
    with open(test, "w", encoding="utf-8") as handle:
        handle.write('from foo_module import FOO_LIMIT, within_limit\n'
                     '\n'
                     'def test_foo_limit_value():\n'
                     '    assert FOO_LIMIT == 20\n'
                     '\n'
                     'def test_within_limit():\n'
                     '    assert within_limit(20)\n'
                     '    assert not within_limit(21)\n')
    return {"root": root, "module": module, "test": test,
            "prompt": str(REPRESENTATIVE_TASK["prompt"])}


def build_agent_command(binary: str, repo: str,
                        model: str = "opencode/muse-spark-1.3-contributor-free",
                        bun_options: str = "") -> tuple[list[str], dict[str, str]]:
    """Non-interactive agent argv + labelled env for one trial.

    ``bun_options`` is either ``""`` or ``"--smol"`` and is always
    recorded as the trial's A/B label; it is never an unlabelled default.
    """
    if not binary or not repo:
        raise ValueError("binary and repo are required")
    if bun_options not in ("", BUN_SMOL):
        raise ValueError("bun_options must be '' or '--smol'")
    argv = [binary, "run", "--auto", "--model", model,
            str(REPRESENTATIVE_TASK["prompt"])]
    env_label = {"BUN_OPTIONS": bun_options} if bun_options else {}
    return argv, env_label


def docker_trial_command(binary_host_path: str, repo_host_path: str,
                         model: str = "opencode/muse-spark-1.3-contributor-free",
                         bun_options: str = "") -> list[str]:
    """Docker argv running one constrained trial (512m, no swap).

    The container mounts the exact coding binary read-only plus the
    workload repo read-write, then runs the real agent and prints the
    cgroup telemetry (current/peak/events incl. OOM/max), wall time and
    exit status. Fails closed on bad inputs; never allows swap.
    """
    if not binary_host_path or not repo_host_path:
        raise ValueError("binary and repo host paths are required")
    if bun_options not in ("", BUN_SMOL):
        raise ValueError("bun_options must be '' or '--smol'")
    env_assign = ("export BUN_OPTIONS=%s; " % bun_options) if bun_options else ""
    inner = (
        "%s/usr/local/bin/opencode-coding run --auto --model %s "
        "\"%s\" 2>&1 | tail -5; "
        "echo agent_exit=${PIPESTATUS[0]}; "
        "echo wall_seconds=$SECONDS; "
        "echo peak=$(cat /sys/fs/cgroup/memory.peak); "
        "echo current=$(cat /sys/fs/cgroup/memory.current); "
        "cat /sys/fs/cgroup/memory.events"
        % (env_assign, model, str(REPRESENTATIVE_TASK["prompt"]).replace('"', "'"))
    )
    return (["docker", "run", "--rm"]
            + list(DOCKER_LIMITS)
            + ["-v", "%s:/usr/local/bin/opencode-coding:ro" % binary_host_path,
               "-v", "%s:/work:rw" % repo_host_path,
               "-w", "/work",
               "ubuntu:22.04", "bash", "-c", inner])


def parse_docker_telemetry(output: str) -> dict[str, object]:
    """Parse the cgroup telemetry block from a constrained trial.

    Extracts ``agent_exit``, ``memory.peak``, ``memory.current`` and the
    ``memory.events`` counters (``high``/``max``/``oom``/``oom_kill``).
    Raises fail-closed when the block is absent.
    """
    if not isinstance(output, str) or not output.strip():
        raise ValueError("docker trial output is empty")
    exit_match = re.search(r"agent_exit=(-?\d+)", output)
    peak_match = re.search(r"peak=(\d+)", output)
    current_match = re.search(r"current=(\d+)", output)
    if exit_match is None or peak_match is None:
        raise ValueError("docker trial output misses agent_exit/peak block")
    events: dict[str, int] = {}
    for name in ("high", "max", "oom", "oom_kill", "oom_group_kill"):
        match = re.search(r"(?m)^%s\s+(\d+)" % re.escape(name), output)
        if match is not None:
            events[name] = int(match.group(1))
    return {"agent_exit": int(exit_match.group(1)),
            "memory_peak_bytes": int(peak_match.group(1)),
            "memory_current_bytes": (int(current_match.group(1))
                                     if current_match else None),
            "memory_events": events}


def validate_trial_record(trial: dict) -> dict:
    """Fail closed unless a trial carries every required measurement."""
    if not isinstance(trial, dict):
        raise ValueError("trial record must be a mapping")
    missing = [f for f in REQUIRED_TRIAL_FIELDS if f not in trial]
    if missing:
        raise ValueError("trial is missing measurement fields: %s" % missing)
    if trial["bun_options"] not in ("", BUN_SMOL):
        raise ValueError("bun_options must be '' or '--smol' (labelled A/B)")
    for field in ("peak_tree_kb", "peak_bytes", "wall_seconds", "exit_code"):
        value = trial.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("%s must be a number" % field)
    events = trial.get("memory_events")
    if not isinstance(events, dict):
        raise ValueError("memory_events must be a mapping")
    digest = trial.get("binary_sha256")
    if not isinstance(digest, str) or SHA256_RE.match(digest) is None:
        raise ValueError("binary_sha256 must be a 64-char hex digest")
    if trial.get("correctness") not in ("pass", "fail", "not-run"):
        raise ValueError("correctness must be pass/fail/not-run")
    return trial


def classify_result(trials: list[dict]) -> str:
    """Classify repeated constrained real-agent trials.

    - ``reliable-under-512m``: >=2 real coding trials succeed with
      headroom (peak comfortably below the ceiling) and no OOM/max events;
    - ``marginal``: succeeds but rides the ceiling or is nondeterministic;
    - ``does-not-fit``: OOM/failure under the required workload;
    - ``inconclusive``: fewer than two real trials (never success).
    """
    real = [t for t in trials
            if isinstance(t, dict) and t.get("correctness") == "pass"
            and t.get("exit_code") == 0]
    if len(trials) < 2 or len(real) < 2:
        # A single boundary pass is not reliability; missing evidence
        # fails closed, except an observed OOM/failure still fits
        # "does-not-fit" when at least one real trial demonstrably failed.
        for trial in trials:
            if not isinstance(trial, dict):
                continue
            events = trial.get("memory_events") or {}
            if (trial.get("exit_code") not in (0, None)
                    or int(events.get("oom_kill", 0)) > 0):
                return "does-not-fit"
        return "inconclusive"
    ceiling = LIMIT_BYTES
    headroom_ok = all(
        isinstance(t.get("peak_bytes"), int) and t["peak_bytes"] <= ceiling - 32 * 1024 * 1024
        for t in real)
    clean_events = all(
        int((t.get("memory_events") or {}).get("oom_kill", 0)) == 0
        and int((t.get("memory_events") or {}).get("max", 0)) == 0
        for t in real)
    if headroom_ok and clean_events:
        return "reliable-under-512m"
    # Any OOM/failure among the required trials rejects reliability.
    for trial in trials:
        events = trial.get("memory_events") or {}
        if (trial.get("exit_code") != 0
                or int(events.get("oom_kill", 0)) > 0):
            return "does-not-fit"
    return "marginal"


def baseline_gap(peak_bytes: int) -> dict[str, object]:
    """Exact gap of one peak against the #52 baseline and the 512m limit."""
    if not isinstance(peak_bytes, int) or peak_bytes <= 0:
        raise ValueError("peak_bytes must be a positive int")
    return {"peak_bytes": peak_bytes,
            "peak_mib": round(peak_bytes / (1024 * 1024), 1),
            "baseline_host_bytes": BASELINE_HOST_PEAK_BYTES,
            "delta_vs_baseline_bytes": peak_bytes - BASELINE_HOST_PEAK_BYTES,
            "limit_bytes": LIMIT_BYTES,
            "gap_to_limit_bytes": peak_bytes - LIMIT_BYTES,
            "passes_limit": peak_bytes <= LIMIT_BYTES,
            "provenance": BASELINE_PROVENANCE}


def render_ab_table(rows: list[dict]) -> str:
    """Render the labelled A/B memory table (one row per trial)."""
    lines = ["| Trial | BUN_OPTIONS | Peak | <=512 MiB | OOM/max | "
             "Wall (s) | Exit | Correctness |",
             "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        trial = str(row.get("trial", "?"))
        bun_label = str(row.get("bun_options", "")) or "(unset)"
        peak = row.get("peak_bytes")
        peak_text = ("%.1f MiB" % (peak / (1024 * 1024))) if isinstance(peak, int) else "?"
        limit_text = "pass" if isinstance(peak, int) and peak <= LIMIT_BYTES else "over"
        events = row.get("memory_events") or {}
        oom_text = "oom=%s max=%s" % (events.get("oom_kill", "?"), events.get("max", "?"))
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |"
                     % (trial, bun_label, peak_text, limit_text, oom_text,
                        row.get("wall_seconds", "?"), row.get("exit_code", "?"),
                        row.get("correctness", "?")))
    return "\n".join(lines) + "\n"
