"""Fresh 512 MiB qualification for the OpenCode PR #12 artifact (issue #109).

Stdlib only, offline by default, no git mutations, no workflow edits. This
module is the reproducible contract that executes the exact immutable
artifact from ``kodmial/opencode#12`` under a real 512 MiB no-swap limit
before any engineering classification is stated.

Exact immutable artifact under test (fail closed, never rebuilt):

- repository ``kodmial/opencode``, PR ``#12``,
  branch ``opencode/issue11-max-headless``,
  source SHA ``8ed6c749577d534c55ba9555ba4918ea8be95a97``;
- source workflow run ``36492639568`` (``OpenCode Coding Artifact``);
- artifact name ``opencode-coding-linux-x64``,
  artifact ID ``11001896223``;
- artifact archive digest
  ``sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df``;
- artifact payload ``opencode-coding-linux-x64``,
  ``opencode-coding-linux-x64.sha256``, ``build-metadata.txt``;
- artifact retention expiry ``2026-10-28``.

Hard rules encoded here:

1. download exact artifact ID ``11001896223`` from source run
   ``36492639568`` (never ``main``, upstream, a rebuilt PR head, or
   ``opencode.ai/install``);
2. verify the binary against the bundled ``opencode-coding-linux-x64.sha256``
   before launch and record the actual executed binary SHA-256;
3. a cross-repository download failure is harness/infrastructure failure:
   never rebuild as a workaround (verdict ``infrastructure-blocked``).

Memory harness reuses the Runtime Lab vocabulary from issue #52
(``automation/memory_benchmark.py`` process-tree polling plus Docker
``--memory=512m --memory-swap=512m`` as the real cgroup-equivalent limit,
cgroup ``memory.current``/``memory.peak``/``memory.events``/swap fields).
``BUN_OPTIONS=--smol`` is a separately labelled A/B arm only.

Required checks encoded here:

1. exact artifact identity plus archive-digest and bundled-checksum proof;
2. ``--version`` smoke check (recorded as a non-success signal only);
3. binary ``--help`` boundary (``run`` exposed, pruned table absent);
4. a real coding-agent workload (search + reason + edit + shell/build/test
   through the normal provider/session/agent/tool loop, verified by a real
   test/check);
5. per-trial measurements (process-tree peak RSS, cgroup current/peak/
   events incl. high/max/oom/oom_kill/oom_group_kill, swap current/max,
   per-process VmRSS/VmHWM where available, wall time, exit status,
   binary size + SHA-256);
6. direct comparison with the ~600-615 MB baseline from issue #52;
7. at least two independent constrained real-agent trials.

Verdicts: ``reliable-under-512m`` | ``marginal`` | ``does-not-fit`` |
``inconclusive`` | ``infrastructure-blocked`` (download/start impossible
before the memory test; never an OpenCode memory result). Success is never
claimed from ``--version``, a fake tool harness, or an unconstrained run.
"""

from __future__ import annotations

import hashlib
import os
import re

SCHEMA = "runtime-lab-max-headless-qualify/v1"

FORK_REPO = "kodmial/opencode"
FORK_PR = 12
FORK_BRANCH = "opencode/issue11-max-headless"
SOURCE_SHA = "8ed6c749577d534c55ba9555ba4918ea8be95a97"
SOURCE_RUN_ID = "36492639568"
SOURCE_WORKFLOW_NAME = "OpenCode Coding Artifact"
ARTIFACT_NAME = "opencode-coding-linux-x64"
ARTIFACT_ID = "11001896223"
ARCHIVE_SHA256 = "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
ARTIFACT_PAYLOAD = (
    "opencode-coding-linux-x64",
    "opencode-coding-linux-x64.sha256",
    "build-metadata.txt",
)
RETENTION_EXPIRY = "2026-10-28"

BINARY_FILENAME = "opencode-coding-linux-x64"
CHECKSUM_FILENAME = "opencode-coding-linux-x64.sha256"

LIMIT_BYTES = 512 * 1024 * 1024
DOCKER_LIMITS = ("--memory=512m", "--memory-swap=512m")
BUN_SMOL = "--smol"

FREE_MODEL = "opencode/muse-spark-1.3-contributor-free"

# Measured baseline from issue #52 (Actions host + Docker 512m/no-swap).
BASELINE_HOST_PEAK_KB = 614632
BASELINE_HOST_PEAK_BYTES = BASELINE_HOST_PEAK_KB * 1024
BASELINE_HOST_RANGE_BYTES = (600 * 1024 * 1024, 615 * 1024 * 1024)
BASELINE_PROVENANCE = "issue-52-run-36423884312 + memory-benchmark-issue-52.md"

# The normal top-level command table that the coding binary must not list.
EXCLUDED_COMMANDS = ("tui", "web", "serve", "acp", "attach", "mcp")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MUTABLE_REFS = frozenset({
    "latest", "main", "master", "dev", "stable", "head",
    "next", "current", "HEAD",
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
    "swap_current_bytes",
    "swap_max_bytes",
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
        "file-reasoning",
        "file-edit",
        "shell-build-test",
        "provider-agent-loop",
        "test-verification",
    ),
}

VERDICTS = (
    "reliable-under-512m",
    "marginal",
    "does-not-fit",
    "inconclusive",
    "infrastructure-blocked",
)


def validate_artifact_identity(
    artifact_id: str = ARTIFACT_ID,
    source_run_id: str = SOURCE_RUN_ID,
    source_sha: str = SOURCE_SHA,
    branch: str = FORK_BRANCH,
    pr: int = FORK_PR,
) -> dict[str, str]:
    """Fail closed unless the exact PR #12 artifact identity is supplied.

    The caller must pass the exact artifact ID, source run ID, source SHA
    and branch. Mutable pointers (``main``, ``latest``, bare branch without
    pinned IDs) are never accepted: a substituted binary is a different
    experiment, not this qualification.
    """
    if str(artifact_id).strip() != ARTIFACT_ID:
        raise ValueError(
            "artifact_id %r does not equal the required artifact %r; "
            "never substitute main/upstream/a rebuilt head" % (artifact_id, ARTIFACT_ID))
    if str(source_run_id).strip() != SOURCE_RUN_ID:
        raise ValueError(
            "source_run_id %r does not equal the required source run %r"
            % (source_run_id, SOURCE_RUN_ID))
    if not isinstance(source_sha, str) or SHA_RE.match(source_sha.strip()) is None:
        raise ValueError("source_sha must be a 40-char lowercase SHA")
    if source_sha.strip() != SOURCE_SHA:
        raise ValueError(
            "source_sha %r does not equal the required PR #12 source %r; "
            "never silently test main/latest/upstream" % (source_sha, SOURCE_SHA))
    if branch != FORK_BRANCH:
        raise ValueError("branch must be %r, got %r" % (FORK_BRANCH, branch))
    if pr != FORK_PR:
        raise ValueError("pr must be %r, got %r" % (FORK_PR, pr))
    return {"repo": FORK_REPO, "pr": str(pr), "branch": branch,
            "source_sha": source_sha.strip(), "source_run_id": source_run_id.strip(),
            "artifact_id": ARTIFACT_ID, "artifact_name": ARTIFACT_NAME}


def reject_mutable_ref(ref: str) -> str:
    """Fail closed on mutable refs such as main/latest/bare HEAD."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty string")
    text = ref.strip()
    if SHA_RE.match(text) is not None:
        return text
    if SHA256_RE.match(text) is not None:
        return text
    if text.lower() in {name.lower() for name in MUTABLE_REFS}:
        raise ValueError(
            "ref %r is a mutable pointer; supply the exact pinned ID/SHA" % ref)
    if "@" not in text:
        raise ValueError(
            "ref %r is not pinned to an exact SHA; refusing to test "
            "a moving target" % ref)
    return text


def artifact_api_endpoints(
    artifact_id: str = ARTIFACT_ID,
    source_run_id: str = SOURCE_RUN_ID,
) -> dict[str, str]:
    """GitHub API endpoints for the exact artifact (no mutable refs)."""
    identity = validate_artifact_identity(artifact_id, source_run_id, SOURCE_SHA)
    run = identity["source_run_id"]
    artifact = identity["artifact_id"]
    return {
        "list": "repos/%s/actions/runs/%s/artifacts" % (FORK_REPO, run),
        "download": "repos/%s/actions/artifacts/%s/zip" % (FORK_REPO, artifact),
    }


def download_steps_for_artifact(workdir: str = "$WORKDIR/opencode-109") -> list[str]:
    """Reproducible shell steps downloading the exact artifact (no rebuild)."""
    endpoints = artifact_api_endpoints()
    return [
        "gh api \"%s\" --jq '.artifacts[] | {id, name, expired}'" % endpoints["list"],
        "test \"$(gh api \"%s\" --jq '.artifacts[] | select(.id == %s) | .name')\" = \"%s\""
        % (endpoints["list"], ARTIFACT_ID, ARTIFACT_NAME),
        "mkdir -p %s && gh api \"%s\" > %s/artifact.zip" % (workdir, endpoints["download"], workdir),
        "echo \"%s  %s/artifact.zip\" | sha256sum -c -" % (ARCHIVE_SHA256, workdir),
        "unzip -o %s/artifact.zip -d %s/extract" % (workdir, workdir),
        "test -f %s/extract/%s" % (workdir, BINARY_FILENAME),
        "test -f %s/extract/%s" % (workdir, CHECKSUM_FILENAME),
        "cd %s/extract && sha256sum -c %s" % (workdir, CHECKSUM_FILENAME),
        "chmod +x %s/extract/%s && %s/extract/%s --version" % (workdir, BINARY_FILENAME, workdir, BINARY_FILENAME),
    ]


def sha256_of_file(path: str) -> str:
    """Streamed SHA-256 of a file (bounded RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    result = digest.hexdigest()
    if SHA256_RE.match(result) is None:
        raise ValueError("cannot hash file at %r" % path)
    return result


def parse_bundled_checksum(text: str) -> str:
    """Parse the bundled ``<sha256>  <filename>`` checksum file."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("bundled checksum file is empty")
    first = text.strip().splitlines()[0].strip()
    token = first.split()[0] if first.split() else ""
    if SHA256_RE.match(token) is None:
        raise ValueError("bundled checksum has no 64-char hex digest: %r" % first[:120])
    return token


def verify_binary_against_bundled(binary_path: str, checksum_path: str) -> str:
    """Verify the artifact binary against its bundled .sha256 file.

    Returns the actual executed binary SHA-256. Raises fail-closed on any
    mismatch, missing file, or non-executable binary. The expected digest
    always comes from the bundled file inside the exact artifact, never
    from a caller-supplied constant.
    """
    if not isinstance(binary_path, str) or not binary_path:
        raise ValueError("binary path must be a non-empty string")
    if not os.path.isfile(binary_path):
        raise FileNotFoundError("artifact binary not found: %r" % binary_path)
    if not os.access(binary_path, os.X_OK):
        raise ValueError("artifact binary is non-executable: %r" % binary_path)
    with open(checksum_path, "r", encoding="utf-8") as handle:
        expected = parse_bundled_checksum(handle.read())
    actual = sha256_of_file(binary_path)
    if actual != expected:
        raise ValueError(
            "artifact checksum mismatch for %r: bundled expects %s, got %s"
            % (binary_path, expected, actual))
    return actual


def fingerprint_binary(path: str) -> dict[str, object]:
    """Return size + SHA-256 of the verified artifact binary (fail closed)."""
    if not isinstance(path, str) or not path:
        raise ValueError("binary path must be a non-empty string")
    if not os.path.isfile(path):
        raise FileNotFoundError("artifact binary not found: %r" % path)
    if not os.access(path, os.X_OK):
        raise ValueError("artifact binary is non-executable: %r" % path)
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError("artifact binary is empty: %r" % path)
    return {"path": path, "bytes": size, "sha256": sha256_of_file(path)}


def check_binary_help(help_text: str) -> dict[str, object]:
    """Prove the artifact binary exposes run but not the pruned table.

    ``--help`` must document the run path while not listing the excluded
    top-level commands. Raises fail-closed otherwise.
    """
    if not isinstance(help_text, str) or not help_text.strip():
        raise ValueError("binary --help output is empty")
    lowered = help_text.lower()
    if "run" not in lowered:
        raise ValueError("artifact binary --help does not expose the run command")
    listed = [cmd for cmd in EXCLUDED_COMMANDS
              if re.search(r"(^|\s)%s(\s|$|,)" % re.escape(cmd), lowered)]
    if listed:
        raise ValueError(
            "artifact binary --help still lists excluded commands: %s" % listed)
    return {"run_exposed": True, "excluded_listed": listed}


def create_representative_repo(root: str) -> dict[str, str]:
    """Materialize the deterministic FOO_LIMIT workload repo.

    The repo requires repository search (find FOO_LIMIT), reasoning over
    actual files, a file edit (10 -> 20), and shell/build/test execution
    (``python -m pytest`` or the module's own test), all through the normal
    agent loop and verified by a real test/check. Returns paths; raises
    fail-closed on misuse.
    """
    if not isinstance(root, str) or not root.strip():
        raise ValueError("repo root must be a non-empty path")
    os.makedirs(root, exist_ok=True)
    module = os.path.join(root, "foo_module.py")
    test = os.path.join(root, "test_foo_module.py")
    with open(module, "w", encoding="utf-8") as handle:
        handle.write('"""Demo module for the max-headless qualification workload."""\n'
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
                        model: str = FREE_MODEL,
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
                         model: str = FREE_MODEL,
                         bun_options: str = "") -> list[str]:
    """Docker argv running one constrained trial (512m, no swap).

    The container mounts the exact artifact binary read-only plus the
    workload repo read-write, then runs the real agent and prints the
    cgroup telemetry (current/peak/events incl. high/max/oom, swap),
    wall time and exit status. Fails closed on bad inputs; never allows
    swap. The workload directory also serves as the agent's working
    directory so search/edit/shell/test all happen inside the limit.
    """
    if not binary_host_path or not repo_host_path:
        raise ValueError("binary and repo host paths are required")
    if bun_options not in ("", BUN_SMOL):
        raise ValueError("bun_options must be '' or '--smol'")
    env_assign = ("export BUN_OPTIONS=%s; " % bun_options) if bun_options else ""
    prompt = str(REPRESENTATIVE_TASK["prompt"]).replace('"', "'")
    inner = (
        "%s/usr/local/bin/opencode-coding run --auto --model %s "
        "\"%s\" 2>&1 | tail -5; "
        "echo agent_exit=${PIPESTATUS[0]}; "
        "echo wall_seconds=$SECONDS; "
        "echo peak=$(cat /sys/fs/cgroup/memory.peak); "
        "echo current=$(cat /sys/fs/cgroup/memory.current); "
        "echo swap_current=$(cat /sys/fs/cgroup/memory.swap.current); "
        "echo swap_max=$(cat /sys/fs/cgroup/memory.swap.max); "
        "cat /sys/fs/cgroup/memory.events"
        % (env_assign, model, prompt)
    )
    return (["docker", "run", "--rm"]
            + list(DOCKER_LIMITS)
            + ["-v", "%s:/usr/local/bin/opencode-coding:ro" % binary_host_path,
               "-v", "%s:/work:rw" % repo_host_path,
               "-w", "/work",
               "ubuntu:22.04", "bash", "-c", inner])


def parse_docker_telemetry(output: str) -> dict[str, object]:
    """Parse the cgroup telemetry block from a constrained trial.

    Extracts ``agent_exit``, ``memory.peak``, ``memory.current``,
    swap current/max, and the ``memory.events`` counters
    (``high``/``max``/``oom``/``oom_kill``/``oom_group_kill``).
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
    swap_current = None
    swap_max = None
    swap_current_match = re.search(r"swap_current=(\d+|max)", output)
    if swap_current_match is not None:
        token = swap_current_match.group(1)
        swap_current = None if token == "max" else int(token)
    swap_max_match = re.search(r"swap_max=(\d+|max|0)", output)
    if swap_max_match is not None:
        token = swap_max_match.group(1)
        swap_max = None if token == "max" else int(token)
    return {"agent_exit": int(exit_match.group(1)),
            "memory_peak_bytes": int(peak_match.group(1)),
            "memory_current_bytes": (int(current_match.group(1))
                                     if current_match else None),
            "swap_current_bytes": swap_current,
            "swap_max_bytes": swap_max,
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
    - ``infrastructure-blocked`` is returned by
      :func:`infrastructure_blocked` for download/start failures, never
      here.
    """
    real = [t for t in trials
            if isinstance(t, dict) and t.get("correctness") == "pass"
            and t.get("exit_code") == 0]
    if len(trials) < 2 or len(real) < 2:
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
    for trial in trials:
        events = trial.get("memory_events") or {}
        if (trial.get("exit_code") != 0
                or int(events.get("oom_kill", 0)) > 0):
            return "does-not-fit"
    return "marginal"


def infrastructure_blocked(reason: str) -> dict[str, str]:
    """Build the infrastructure-blocked verdict for download/start failures.

    The exact artifact could not be downloaded/started before the memory
    test. This is harness/infrastructure failure, never an OpenCode memory
    result, and a rebuild is never an accepted workaround.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    return {"verdict": "infrastructure-blocked", "reason": reason.strip()[:500]}


def baseline_gap(peak_bytes: int) -> dict[str, object]:
    """Exact gap of one peak against the #52 baseline and the 512m limit."""
    if not isinstance(peak_bytes, int) or isinstance(peak_bytes, bool) or peak_bytes <= 0:
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
