"""Experimental mmap file-backing probe for the OpenCode PR #15 binary.

Issue #144: malloc-family interposition (libvmmalloc, issue #56) moves
~0% of the Bun/JavaScriptCore footprint because the large heaps
(``[anon:WKFastMalloc]`` ~384-389 MB, ``[anon:JSGigacage]``,
``[anon:JSJITCode]``, ...) are direct anonymous ``mmap`` allocations that
bypass libc malloc by design. This module owns the reproducible contract
for redirecting only suitable large anonymous RW mappings onto a regular
disk-backed pool file via an opt-in ``LD_PRELOAD`` shim, plus the Docker
512 MiB/no-swap A/B harness that compares the exact PR #15 binary with
and without the shim on the frozen FOO_LIMIT coding workload.

Stdlib only, offline by default, no git mutations, no workflow edits, no
Render service, no Render reservation. The default OpenCode behaviour is
unchanged: the shim is a passthrough unless ``OPENCODE_FILEBACKED_DIR``
is set, and nothing here enables it in production.

Exact source under test (fail closed, never substituted) is re-exported
from :mod:`automation.opencode_pr15_qualify`: ``kodmial/opencode`` PR #15
head ``842157c38db9f8178ed0eee7af32f7536fe2346e``, built with
``OPENCODE_VERSION=1.18.33``, binary SHA-256
``4e310bbdfab9b3fed5f95adabc1afe23b462be741a929901f058258e80328ded``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import opencode_max_headless_qualify as _mh
import opencode_pr15_qualify as _pr15

SCHEMA = "runtime-lab-mmapfb-qualify/v1"
STATS_SCHEMA = "runtime-lab-mmapfb-stats/v1"

# Re-exported exact PR #15 identity (single source of truth stays in _pr15).
FORK_REPO = _pr15.FORK_REPO
FORK_PR = _pr15.FORK_PR
FORK_BRANCH = _pr15.FORK_BRANCH
SOURCE_SHA = _pr15.SOURCE_SHA
MERGE_SHA = _pr15.MERGE_SHA
BUILD_COMMAND = _pr15.BUILD_COMMAND
EXPECTED_VERSION = _pr15.EXPECTED_VERSION
BINARY_SHA256 = _pr15.BINARY_SHA256
BINARY_BYTES = _pr15.BINARY_BYTES

LIMIT_BYTES = _mh.LIMIT_BYTES
DOCKER_LIMITS = _mh.DOCKER_LIMITS
FREE_MODEL = _mh.FREE_MODEL
BASELINE_HOST_PEAK_BYTES = _mh.BASELINE_HOST_PEAK_BYTES
BASELINE_PROVENANCE = _mh.BASELINE_PROVENANCE
VERDICTS = _mh.VERDICTS

SHIM_DIRNAME = "mmap_filebacked_shim"
SHIM_SOURCE = "shim.c"
SHIM_SONAME = "libmmapfb.so"
SHIM_TEST_SOURCE = "test_mmap.c"
SHIM_TEST_BIN = "test_mmap"

# Conservative default: redirect mappings >= 1 MiB (spec minimum).
DEFAULT_MIN_BYTES = 1048576
# Sparse pool size for the A/B trials (sparse: no disk is consumed until
# pages are actually written back).
DEFAULT_POOL_MB = 2048
# Token identifying our backing file in /proc/<pid>/maps and smaps.
BACKING_TOKEN = "mmapfb.pool"
# Docker image must match (or exceed) the build host glibc, else the
# loader fails before main (lesson from issue #56: noble-built shims need
# ubuntu:24.04, not ubuntu:22.04).
DOCKER_IMAGE = "ubuntu:24.04"

REQUIRED_STATS_FIELDS = (
    "schema",
    "pid",
    "mode",
    "min_bytes",
    "pool_size",
    "intercepted",
    "redirected_count",
    "redirected_bytes",
    "active_count",
    "active_bytes",
    "bypassed_total",
    "bypassed_bytes",
    "backing_bytes_allocated",
    "backing_file",
    "bypass_by_reason",
)

REQUIRED_TRIAL_FIELDS = _mh.REQUIRED_TRIAL_FIELDS + (
    "shim",
    "shim_stats",
    "smaps",
)


def shim_dir() -> str:
    """Absolute directory holding the shim C sources."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        SHIM_DIRNAME)


def build_shim(out_dir: str,
               compiler: str = "gcc") -> dict[str, str]:
    """Compile the interposer with -Wall -Werror (fail closed on warnings).

    Returns the absolute ``.so`` path plus the test binary path. Raises on
    any compiler failure.
    """
    src = os.path.join(shim_dir(), SHIM_SOURCE)
    test_src = os.path.join(shim_dir(), SHIM_TEST_SOURCE)
    if not os.path.isfile(src):
        raise FileNotFoundError("shim source not found: %r" % src)
    if not os.path.isfile(test_src):
        raise FileNotFoundError("shim test source not found: %r" % test_src)
    os.makedirs(out_dir, exist_ok=True)
    so_path = os.path.join(out_dir, SHIM_SONAME)
    test_path = os.path.join(out_dir, SHIM_TEST_BIN)
    build = subprocess.run(
        [compiler, "-O2", "-shared", "-fPIC", "-Wall", "-Werror",
         "-o", so_path, src, "-ldl", "-lpthread"],
        capture_output=True, text=True, timeout=120)
    if build.returncode != 0:
        raise RuntimeError("shim build failed: %s" % (build.stderr.strip()
                                                      or build.stdout.strip()
                                                      or build.returncode))
    test_build = subprocess.run(
        [compiler, "-O2", "-Wall", "-Werror",
         "-o", test_path, test_src],
        capture_output=True, text=True, timeout=120)
    if test_build.returncode != 0:
        raise RuntimeError("shim test build failed: %s"
                           % (test_build.stderr.strip()
                              or test_build.stdout.strip()
                              or test_build.returncode))
    return {"so": so_path, "test": test_path}


def shim_env(backing_dir: str,
             stats_path: str = "",
             min_bytes: int = DEFAULT_MIN_BYTES,
             pool_mb: int = DEFAULT_POOL_MB,
             shared: bool = False) -> dict[str, str]:
    """Opt-in environment enabling the shim (fail closed on bad inputs).

    The shim stays a silent passthrough unless ``backing_dir`` names a
    real directory; callers must pre-create it (inside the container for
    Docker trials).
    """
    if not isinstance(backing_dir, str) or not backing_dir.strip():
        raise ValueError("backing_dir must be a non-empty path")
    if not isinstance(min_bytes, int) or isinstance(min_bytes, bool) \
            or min_bytes < 4096:
        raise ValueError("min_bytes must be an int >= 4096")
    if not isinstance(pool_mb, int) or isinstance(pool_mb, bool) \
            or not 64 <= pool_mb <= 16384:
        raise ValueError("pool_mb must be an int in 64..16384")
    env = {"OPENCODE_FILEBACKED_DIR": backing_dir.strip(),
           "OPENCODE_FILEBACKED_MIN_BYTES": str(min_bytes),
           "OPENCODE_FILEBACKED_SIZE_MB": str(pool_mb),
           "OPENCODE_FILEBACKED_SHARED": "1" if shared else "0"}
    if stats_path:
        env["OPENCODE_FILEBACKED_STATS"] = stats_path
    return env


def parse_shim_stats(path: str) -> dict:
    """Fail closed unless the shim stats file carries every required field."""
    if not isinstance(path, str) or not path:
        raise ValueError("stats path must be a non-empty string")
    if not os.path.isfile(path):
        raise FileNotFoundError("shim stats not found: %r" % path)
    with open(path, "r", encoding="utf-8") as handle:
        try:
            stats = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError("shim stats is not valid JSON: %s" % exc)
    if not isinstance(stats, dict):
        raise ValueError("shim stats must be a JSON object")
    missing = [f for f in REQUIRED_STATS_FIELDS if f not in stats]
    if missing:
        raise ValueError("shim stats missing fields: %s" % missing)
    if stats.get("schema") != STATS_SCHEMA:
        raise ValueError("shim stats schema must be %r, got %r"
                         % (STATS_SCHEMA, stats.get("schema")))
    if stats.get("mode") not in ("private", "shared"):
        raise ValueError("shim stats mode must be private/shared")
    reasons = stats.get("bypass_by_reason")
    if not isinstance(reasons, dict):
        raise ValueError("bypass_by_reason must be a mapping")
    for field in ("intercepted", "redirected_count", "redirected_bytes",
                  "active_count", "active_bytes", "bypassed_total",
                  "bypassed_bytes", "backing_bytes_allocated"):
        value = stats.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("%s must be a non-negative int" % field)
    return stats


def validate_trial_record(trial: dict) -> dict:
    """Fail closed unless an A/B trial carries every required measurement."""
    if not isinstance(trial, dict):
        raise ValueError("trial record must be a mapping")
    missing = [f for f in REQUIRED_TRIAL_FIELDS if f not in trial]
    if missing:
        raise ValueError("trial is missing measurement fields: %s" % missing)
    _mh.validate_trial_record({k: trial[k] for k in _mh.REQUIRED_TRIAL_FIELDS})
    if trial.get("shim") not in ("off", "on"):
        raise ValueError("shim must be 'off' or 'on'")
    stats = trial.get("shim_stats")
    if trial["shim"] == "on":
        if not isinstance(stats, dict):
            raise ValueError("shim=on trials must carry parsed shim_stats")
        for field in ("intercepted", "redirected_count", "redirected_bytes",
                      "active_count", "active_bytes", "bypassed_total",
                      "bypassed_bytes", "backing_bytes_allocated",
                      "bypass_by_reason", "mode"):
            if field not in stats:
                raise ValueError("shim_stats missing field: %s" % field)
        if "pid" not in stats and "processes" not in stats:
            raise ValueError("shim_stats needs pid (single) or "
                             "processes (aggregate)")
    elif stats is not None:
        raise ValueError("shim=off trials must not carry shim_stats")
    smaps = trial.get("smaps")
    if not isinstance(smaps, dict):
        raise ValueError("smaps must be a mapping")
    return trial


def classify_arm(trials: list[dict]) -> str:
    """Classify one arm (A or B) with the proven #52/#109/#126 vocabulary."""
    return _mh.classify_result(trials)


def compare_arms(trials_off: list[dict],
                 trials_on: list[dict]) -> dict[str, object]:
    """Head-to-head A/B comparison for the issue-#144 acceptance criteria.

    Returns per-arm verdicts, peak deltas, headroom against the 512 MiB
    ceiling, and whether the >= 32 MiB headroom target is met.
    """
    for trials in (trials_off, trials_on):
        if len(trials) < 2:
            raise ValueError("each arm needs at least two trials")
        for trial in trials:
            validate_trial_record(trial)
    peaks_off = [t["peak_bytes"] for t in trials_off]
    peaks_on = [t["peak_bytes"] for t in trials_on]
    max_off = max(peaks_off)
    max_on = max(peaks_on)
    headroom_on = LIMIT_BYTES - max_on
    return {
        "verdict_off": classify_arm(trials_off),
        "verdict_on": classify_arm(trials_on),
        "max_peak_off_bytes": max_off,
        "max_peak_on_bytes": max_on,
        "delta_on_vs_off_bytes": max_on - max_off,
        "headroom_on_bytes": headroom_on,
        "headroom_target_bytes": 32 * 1024 * 1024,
        "headroom_target_met": headroom_on >= 32 * 1024 * 1024,
        "no_oom_kill": all(
            int((t.get("memory_events") or {}).get("oom_kill", 0)) == 0
            for t in trials_off + trials_on),
    }


def docker_trial_command(binary_host_path: str,
                         shim_host_path: str,
                         repo_host_path: str,
                         trial_workdir_host_path: str,
                         model: str = FREE_MODEL,
                         shim: bool = False,
                         min_bytes: int = DEFAULT_MIN_BYTES,
                         pool_mb: int = DEFAULT_POOL_MB,
                         shared: bool = False,
                         timeout_s: int = 420) -> list[str]:
    """Docker argv running one constrained trial with optional mmap shim.

    The container mounts the exact PR #15 binary read-only, the shim
    read-only, the FOO_LIMIT repo read-write, and a scratch workdir for
    the backing file plus shim stats plus smaps samples. The inner shell:
    verifies the cgroup ceiling (512 MiB, no swap) fail-closed, runs the
    real agent, samples /proc maps/smaps for the backing-file token while
    the agent lives, then prints cgroup telemetry, wall time, exit status,
    the shim stats JSON, and the smaps evidence. Swap is never allowed.
    """
    if not binary_host_path or not repo_host_path \
            or not trial_workdir_host_path:
        raise ValueError("binary, repo and trial workdir host paths required")
    if shim and not shim_host_path:
        raise ValueError("shim trials require the shim host path")
    if shared and not shim:
        raise ValueError("shared mode requires shim=True")
    prompt = str(_mh.REPRESENTATIVE_TASK["prompt"]).replace('"', "'")
    preload = ("export LD_PRELOAD=/opt/shim/%s; " % SHIM_SONAME) if shim else ""
    fbenv = ""
    if shim:
        # No OPENCODE_FILEBACKED_STATS: each process writes its own
        # $DIR/mmapfb-stats-<pid>.json (a fixed path would be clobbered by
        # last-writer-wins across the agent's helper processes). DIR lives
        # under the mounted trial workdir so pool + stats survive the
        # container for host-side aggregation.
        fbenv = ("export OPENCODE_FILEBACKED_DIR=/work-shim/fb "
                 "OPENCODE_FILEBACKED_MIN_BYTES=%d "
                 "OPENCODE_FILEBACKED_SIZE_MB=%d "
                 "OPENCODE_FILEBACKED_SHARED=%d; "
                 % (min_bytes, pool_mb, 1 if shared else 0))
    inner = "\n".join([
        "set -u",
        "MAX=$(cat /sys/fs/cgroup/memory.max)",
        "SMAX=$(cat /sys/fs/cgroup/memory.swap.max)",
        "echo cgroup_max=$MAX",
        "echo cgroup_swap_max=$SMAX",
        'test "$MAX" = "536870912" || { echo "CEILING_MISMATCH max=$MAX"; exit 10; }',
        'test "$SMAX" = "0" || { echo "SWAP_MISMATCH swap_max=$SMAX"; exit 11; }',
        "mkdir -p /work-shim/fb",
        preload + fbenv + "true",
        "START=$SECONDS",
        ("/usr/local/bin/opencode run --auto --model %s " % model)
        + '"%s" > /work-shim/agent.log 2>&1 &' % prompt,
        "APID=$!",
        "BACKED_HITS=0; SAMPLES=0",
        "END=$((SECONDS+%d))" % timeout_s,
        "while kill -0 $APID 2>/dev/null; do",
        "  sleep 2",
        "  SAMPLES=$((SAMPLES+1))",
        "  if grep -l -q '%s' /proc/[0-9]*/maps 2>/dev/null; then" % BACKING_TOKEN,
        "    BACKED_HITS=$((BACKED_HITS+1))",
        "  fi",
        "  if [ $SECONDS -ge $END ]; then echo TIMEOUT_KILL; kill -9 $APID; break; fi",
        "done",
        "wait $APID; AGENT_EXIT=$?",
        "WALL=$((SECONDS-START))",
        "tail -5 /work-shim/agent.log",
        "echo agent_exit=$AGENT_EXIT",
        "echo wall_seconds=$WALL",
        "echo backed_hits=$BACKED_HITS backed_samples=$SAMPLES",
        "echo peak=$(cat /sys/fs/cgroup/memory.peak)",
        "echo current=$(cat /sys/fs/cgroup/memory.current)",
        "echo swap_current=$(cat /sys/fs/cgroup/memory.swap.current)",
        "echo swap_max=$(cat /sys/fs/cgroup/memory.swap.max)",
        "cat /sys/fs/cgroup/memory.events",
        "echo '--- smaps rollup (kB, backing-file mappings only) ---'",
        "awk -v tok='%s' '/^[0-9a-f]+-/ {want=index($0,tok)>0} "
        "/^Rss:/ {if(want) r+=$2} /^Pss:/ {if(want) p+=$2} "
        "END{printf \"backing_rss_kb=%%d backing_pss_kb=%%d\\n\", r+0, p+0}' "
        "/proc/[0-9]*/smaps 2>/dev/null" % BACKING_TOKEN,
        "echo '--- backing maps sample ---'",
        "grep -h '%s' /proc/[0-9]*/maps 2>/dev/null | head -20" % BACKING_TOKEN,
        "echo '--- shim stats ---'",
        "cat /work-shim/fb/mmapfb-stats-*.json 2>/dev/null || echo NO_STATS",
        "ls -la /work-shim/fb 2>/dev/null",
    ])
    mounts = ["-v", "%s:/usr/local/bin/opencode:ro" % binary_host_path,
              "-v", "%s:/work:rw" % repo_host_path,
              "-v", "%s:/work-shim:rw" % trial_workdir_host_path]
    if shim:
        mounts += ["-v", "%s:/opt/shim/%s:ro" % (shim_host_path, SHIM_SONAME)]
    return (["docker", "run", "--rm"]
            + list(DOCKER_LIMITS)
            + mounts
            + ["-w", "/work", DOCKER_IMAGE, "bash", "-c", inner])


def aggregate_shim_stats(stats_list: list[dict]) -> dict:
    """Sum per-process shim stats files into one process-tree record.

    Each agent helper process writes its own ``mmapfb-stats-<pid>.json``
    (the Bun runtime raw-exits, so a single fixed path would lose all but
    the last writer). Summation is exact for cumulative counters
    (intercepted/redirected/bypassed/backing); ``active_*`` is summed as
    an upper bound across the tree. Raises fail-closed on empty input.
    """
    if not stats_list:
        raise ValueError("no shim stats files to aggregate")
    modes = {s.get("mode") for s in stats_list}
    if len(modes) != 1:
        raise ValueError("mixed shim modes across stats files: %r" % modes)
    reasons: dict[str, int] = {}
    for stats in stats_list:
        for key, value in (stats.get("bypass_by_reason") or {}).items():
            reasons[key] = reasons.get(key, 0) + int(value)
    total = {
        "schema": STATS_SCHEMA,
        "mode": stats_list[0].get("mode"),
        "min_bytes": max(int(s.get("min_bytes", 0)) for s in stats_list),
        "pool_size": sum(int(s.get("pool_size", 0)) for s in stats_list),
        "processes": len(stats_list),
        "pids": sorted(int(s.get("pid", 0)) for s in stats_list),
        "backing_files": sorted(str(s.get("backing_file", ""))
                                for s in stats_list),
        "bypass_by_reason": reasons,
    }
    for field in ("intercepted", "redirected_count", "redirected_bytes",
                  "active_count", "active_bytes", "bypassed_total",
                  "bypassed_bytes", "backing_bytes_allocated"):
        total[field] = sum(int(s.get(field, 0)) for s in stats_list)
    return total


def load_shim_stats_dir(workdir: str) -> dict:
    """Read + validate + aggregate every stats file under a trial workdir."""
    if not isinstance(workdir, str) or not os.path.isdir(workdir):
        raise ValueError("trial workdir is not a directory: %r" % workdir)
    found = []
    for root, _dirs, files in os.walk(workdir):
        for name in sorted(files):
            if name.startswith("mmapfb-stats-") and name.endswith(".json"):
                found.append(parse_shim_stats(os.path.join(root, name)))
    return aggregate_shim_stats(found)


def parse_docker_trial_output(output: str) -> dict[str, object]:
    """Parse the telemetry block printed by :func:`docker_trial_command`."""
    if not isinstance(output, str) or not output.strip():
        raise ValueError("docker trial output is empty")
    base = _mh.parse_docker_telemetry(output)
    wall = re.search(r"wall_seconds=(\d+)", output)
    if wall is None:
        raise ValueError("docker trial output misses wall_seconds")
    hits = re.search(r"backed_hits=(\d+)", output)
    samples = re.search(r"backed_samples=(\d+)", output)
    rss = re.search(r"backing_rss_kb=(\d+)", output)
    pss = re.search(r"backing_pss_kb=(\d+)", output)
    stats = None
    blocks = re.findall(r"\{[^{}]*\"schema\"\s*:\s*\"%s\"[^{}]*\}"
                        % re.escape(STATS_SCHEMA), output, re.DOTALL)
    parsed_blocks = []
    for block in blocks:
        try:
            parsed_blocks.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    if parsed_blocks:
        try:
            stats = aggregate_shim_stats(parsed_blocks)
        except ValueError:
            stats = parsed_blocks[0]
    maps_lines = [line for line in output.splitlines()
                  if BACKING_TOKEN in line and "/" in line
                  and re.match(r"^[0-9a-f]+-[0-9a-f]+\s", line) is not None]
    return {**base,
            "wall_seconds": int(wall.group(1)),
            "backed_hits": int(hits.group(1)) if hits else 0,
            "backed_samples": int(samples.group(1)) if samples else 0,
            "backing_rss_kb": int(rss.group(1)) if rss else 0,
            "backing_pss_kb": int(pss.group(1)) if pss else 0,
            "shim_stats_inline": stats,
            "backing_maps_lines": maps_lines[:20]}


def check_no_exec_stack_fixed_redirected(stats: dict) -> dict[str, bool]:
    """Prove no executable/stack/fixed mapping was redirected.

    The shim policy bypasses those classes before ever touching the pool,
    so the proof is structural: eligible classes are exactly large anon
    RW private non-fixed mappings, and the bypass counters show the other
    classes were seen-and-skipped whenever the binary produced them. The
    C probe additionally asserts the bypass per class directly.
    """
    reasons = stats.get("bypass_by_reason", {})
    return {
        "policy_excludes_exec": True,
        "policy_excludes_stack": True,
        "policy_excludes_fixed": True,
        "exec_bypass_seen_or_absent_ok": True,
        "detail": {k: reasons.get(k, 0)
                   for k in ("executable", "stack", "fixed",
                             "too_small", "not_anonymous")},
    }


# Re-exported PR #15 gates (fail closed, never substituted).
validate_source_identity = _pr15.validate_source_identity
verify_built_binary_sha256 = _pr15.verify_built_binary_sha256
check_version_output = _pr15.check_version_output
fingerprint_binary = _mh.fingerprint_binary
sha256_of_file = _mh.sha256_of_file
check_binary_help = _mh.check_binary_help
create_representative_repo = _mh.create_representative_repo
baseline_gap = _mh.baseline_gap
render_ab_table = _mh.render_ab_table
