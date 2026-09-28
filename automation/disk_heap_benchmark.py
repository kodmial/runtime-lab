"""Transparent disk-backed heap experiment for OpenCode under 512 MB (issue #56).

Tests whether user-space file-backed allocators let the pinned OpenCode
binary run reliably under a 512 MB hard RAM limit with swap disabled,
without kernel swap or privileges. Candidates:

- baseline              pinned standalone binary, no wrapper;
- smol                  baseline + BUN_OPTIONS=--smol;
- vmmalloc              LD_PRELOAD libvmmalloc with a pool on ordinary disk;
- vmmalloc_smol         vmmalloc + BUN_OPTIONS=--smol;
- memtier               LD_PRELOAD libmemtier with an FS_DAX file tier;
- shim                  minimal custom MAP_SHARED malloc shim (repo source);
- shim_smol             shim + BUN_OPTIONS=--smol.

Each mode runs the issue #52 workload (``opencode run`` against the free
model) through ``disk_heap_snap.py`` (tree-RSS peak + smaps anon/file/pool
classification + top anonymous mappings), on the host and -- optionally --
inside Docker under ``--memory=512m --memory-swap=512m`` with cgroup
peak/events/swap evidence.

Reproducible entry points::

    python3 automation/disk_heap_benchmark.py --output ... --report ...
    python3 automation/disk_heap_benchmark.py --include-network --docker ...

Network-backed agent runs are gated behind --include-network (default off);
Docker stress is gated behind --docker (default off). Stdlib only; never
needs secrets or a paid Render plan. libvmmalloc has no binary package on
current distros: pass --vmmalloc-so with a locally built copy (see
automation/benchmark-results/disk-heap-issue-56.md for the pinned source
build used here), or leave it unset to record that candidate as
unavailable on this machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "automation"))

DOCKER_512_NO_SWAP = ["--memory=512m", "--memory-swap=512m"]
DOCKER_IMAGE = "diskheap-bench"
AGENT_MODEL = "opencode/muse-spark-1.3-contributor-free"
AGENT_PROMPT = "say hi in one word"
CODE_PROMPT = (
    "In the current directory there is calc.py with a failing test in "
    "test_calc.py. Fix calc.py so the tests pass, run the tests with "
    "python3 -m pytest test_calc.py -q, and report the result in one line. "
    "Do not touch any other files."
)
CALC_PY = "def add(a, b):\n    return a - b  # BUG: should add\n"
CALC_TEST = (
    "from calc import add\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
)

MODES = ("baseline", "smol", "vmmalloc", "vmmalloc_smol", "memtier",
         "shim", "shim_smol")


def find_opencode() -> str | None:
    try:
        from memory_benchmark import find_opencode as _find
        return _find()
    except Exception:
        found = shutil.which("opencode")
        return found


def pinned_version() -> str:
    try:
        from opencode_runner import OPENCODE_PINNED_VERSION
        return OPENCODE_PINNED_VERSION
    except Exception:
        return "unknown"


def find_vmmalloc(explicit: str | None) -> str | None:
    if explicit and os.path.isfile(explicit):
        return explicit
    for candidate in (
        "/usr/lib/x86_64-linux-gnu/libvmmalloc.so.1",
        "/usr/lib/libvmmalloc.so.1",
        "/usr/local/lib/libvmmalloc.so.1",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def find_memtier() -> str | None:
    for candidate in (
        "/usr/lib/x86_64-linux-gnu/libmemtier.so.0",
        "/usr/lib/libmemtier.so.0",
        "/usr/local/lib/libmemtier.so.0",
    ):
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("libmemtier.so.0")
    return found


def build_shim(workdir: str) -> str | None:
    """Compile the repo shim with gcc; None when gcc is unavailable."""
    if shutil.which("gcc") is None:
        return None
    src = os.path.join(REPO_ROOT, "automation", "disk_heap_shim", "shim.c")
    out = os.path.join(workdir, "libdiskheap_shim.so")
    proc = subprocess.run(
        ["gcc", "-O2", "-Wall", "-shared", "-fPIC", "-o", out, src,
         "-ldl", "-lpthread"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not os.path.isfile(out):
        return None
    return out


def memtier_tiers(pool_dir: str) -> str:
    return ("KIND:FS_DAX,PATH:%s,RATIO:1;KIND:DRAM,RATIO:1;"
            "POLICY:STATIC_RATIO" % pool_dir)


def mode_env(mode: str, paths: dict, pool_dir: str) -> tuple[dict, dict]:
    """Return (env_overrides, availability) for a mode."""
    env: dict = {}
    avail: dict = {"available": True, "reason": ""}
    if mode in ("smol", "vmmalloc_smol", "shim_smol"):
        env["BUN_OPTIONS"] = "--smol"
    if mode in ("vmmalloc", "vmmalloc_smol"):
        lib = paths.get("vmmalloc")
        if not lib:
            avail = {"available": False,
                     "reason": "no libvmmalloc.so on this machine "
                               "(pass --vmmalloc-so)"}
        else:
            env["LD_PRELOAD"] = lib
            env["VMMALLOC_POOL_DIR"] = pool_dir
            env["VMMALLOC_POOL_SIZE"] = str(1024 * 1024 * 1024)
    elif mode == "memtier":
        lib = paths.get("memtier")
        if not lib:
            avail = {"available": False,
                     "reason": "no libmemtier.so on this machine"}
        else:
            env["LD_PRELOAD"] = lib
            env["MEMKIND_MEM_TIERS"] = memtier_tiers(pool_dir)
    elif mode in ("shim", "shim_smol"):
        lib = paths.get("shim")
        if not lib:
            avail = {"available": False,
                     "reason": "gcc unavailable; custom shim not built"}
        else:
            env["LD_PRELOAD"] = lib
            env["DISKHEAP_DIR"] = pool_dir
            env["DISKHEAP_SIZE_MB"] = "1024"
    return env, avail


def run_snap(workdir: str, outpath: str, budget: float, cmd: list,
             env: dict) -> dict:
    snap = os.path.join(REPO_ROOT, "automation", "disk_heap_snap.py")
    full = ([sys.executable, snap, str(budget), workdir, outpath]
            + ["%s=%s" % (k, v) for k, v in env.items()] + ["--"] + cmd)
    proc = subprocess.run(full, capture_output=True, text=True,
                          timeout=budget + 120)
    try:
        with open(outpath, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"error": "snap failed",
                "returncode": proc.returncode,
                "stdout": proc.stdout[-500:],
                "stderr": proc.stderr[-500:]}


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_run_512(image: str, mounts: list,
                   container_cmd: str, timeout: float) -> dict:
    """Run one bash payload under the 512 MB no-swap ceiling."""
    cmd = (["docker", "run", "--rm"] + DOCKER_512_NO_SWAP
           + mounts + [image, "bash", "-c", container_cmd])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        return {"docker_exit": proc.returncode,
                "output": ((proc.stdout or "")
                           + (proc.stderr or ""))[-3000:]}
    except subprocess.TimeoutExpired:
        return {"docker_exit": 124, "output": "docker timeout"}
    except Exception as exc:
        return {"docker_exit": 127, "output": str(exc)[:300]}


def parse_docker_output(text: str) -> dict:
    info: dict = {}
    for line in text.splitlines():
        if line.startswith("CG_PEAK="):
            try:
                info["cgroup_peak_bytes"] = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("CG_SWAP="):
            try:
                info["cgroup_swap_bytes"] = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("CG_MAX="):
            try:
                info["cgroup_max_bytes"] = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("CG_OOMKILL="):
            try:
                info["cgroup_oom_kill"] = int(line.split("=", 1)[1])
            except ValueError:
                pass
    return info


def run_docker_mode(opencode_bin: str, env: dict, task: str,
                    ws_host: str, snap_host: str, image: str,
                    extra_mounts: list, prelude: str) -> dict:
    os.makedirs(ws_host, exist_ok=True)
    snap_repo = os.path.join(REPO_ROOT, "automation", "disk_heap_snap.py")
    mounts = ["-v", "%s:/usr/local/bin/opencode:ro" % opencode_bin,
              "-v", "%s:/snap/disk_heap_snap.py:ro" % snap_repo,
              "-v", "%s:/ws" % ws_host] + extra_mounts
    if task == "code":
        with open(os.path.join(ws_host, "calc.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(CALC_PY)
        with open(os.path.join(ws_host, "test_calc.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(CALC_TEST)
        prompt = CODE_PROMPT
    else:
        prompt = AGENT_PROMPT
    env_assign = " ".join("'%s=%s'" % (k, v) for k, v in env.items())
    # Container-side pool lives on the container's own writable layer
    # (ordinary disk-backed overlayfs, like Render ephemeral disk).
    payload = (
        "set -u; mkdir -p /pool; "
        "%s; "
        "CG_MAX=$(cat /sys/fs/cgroup/memory.max); "
        "env %s python3 /snap/disk_heap_snap.py 220 /ws /ws/snap.json -- "
        "/usr/local/bin/opencode run --auto --model %s '%s' > /ws/stdout.txt 2> /ws/stderr.txt; "
        "echo CG_MAX=$CG_MAX; "
        "echo CG_PEAK=$(cat /sys/fs/cgroup/memory.peak); "
        "echo CG_SWAP=$(cat /sys/fs/cgroup/memory.swap.current); "
        "echo CG_OOMKILL=$(grep -E '^oom_kill' /sys/fs/cgroup/memory.events | awk '{print $2}'); "
        "cat /sys/fs/cgroup/memory.events"
    ) % (prelude, env_assign, AGENT_MODEL, prompt)
    out = docker_run_512(image, mounts, payload, timeout=300.0)
    try:
        with open(snap_host, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
    except (OSError, ValueError):
        snap = {"error": "no snap.json from container"}
    parsed = parse_docker_output(out.get("output", ""))
    try:
        with open(os.path.join(ws_host, "stdout.txt"), "r",
                  encoding="utf-8") as handle:
            snap["container_stdout"] = handle.read()[:300]
    except OSError:
        pass
    return {"docker": out, "cgroup": parsed, "snap": snap}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Disk-backed heap experiment (issue #56).")
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--include-network", action="store_true")
    parser.add_argument("--docker", action="store_true",
                        help="Run the 512 MB no-swap Docker stress matrix.")
    parser.add_argument("--docker-image", default=DOCKER_IMAGE)
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--modes", default=",".join(MODES),
                        help="Comma-separated subset of modes.")
    parser.add_argument("--tasks", default="hi",
                        help="Comma-separated subset of {hi,code}.")
    parser.add_argument("--vmmalloc-so", default=None,
                        help="Path to a locally built libvmmalloc.so.1.")
    parser.add_argument("--budget", type=float, default=220.0)
    return parser


def render_report(result: dict) -> str:
    lines = [
        "# Transparent disk-backed heap experiment (issue #56)",
        "",
        "- Pinned OpenCode: %s" % result.get("pinned_opencode_version", "?"),
        "- Binary: %s" % (result.get("opencode_bin") or "not found"),
        "- Run: %s | base: %s" % (result.get("run_id"),
                                  result.get("base_commit", "?")[:12]),
        "- Constraint under test: 512 MB cgroup, swap.max=0, ordinary disk",
        "",
        "## Modes (peak_tree_kb / pool_kb / classification)",
        "",
    ]
    host = result.get("host", {})
    if isinstance(host, dict) and host.get("status") == "skipped":
        lines.append("- host matrix: %s" % host.get("reason", "skipped"))
    else:
        for name, entry in host.items():
            if not isinstance(entry, dict):
                continue
            snap = entry.get("snap", {})
            if not entry.get("available", True):
                lines.append("- %s: UNAVAILABLE (%s)"
                             % (name, entry.get("reason", "?")))
                continue
            if "error" in snap or "exit_code" not in snap:
                lines.append("- %s: ERROR %s"
                             % (name, snap.get("error", "no data")))
                continue
            final = snap.get("final_class_kb", {})
            lines.append(
                "- %s: exit=%s class=%s wall=%ss peak_tree=%s anon=%s pool=%s"
                % (name, snap.get("exit_code"), snap.get("classification"),
                   snap.get("wall_s"), snap.get("peak_tree_kb"),
                   final.get("anon_kb"), final.get("pool_kb")))
    lines += ["", "## Docker 512 MB no-swap (exit / cgroup peak / oom_kill)",
              ""]
    docker = result.get("docker", {})
    if isinstance(docker, dict) and docker.get("status") == "skipped":
        lines.append("- docker matrix: %s" % docker.get("reason", "skipped"))
    else:
        for name, entry in docker.items():
            if not isinstance(entry, dict):
                continue
            snap = entry.get("snap", {})
            if not entry.get("available", True):
                lines.append("- %s: UNAVAILABLE (%s)"
                             % (name, entry.get("reason", "?")))
                continue
            cg = entry.get("cgroup", {})
            lines.append(
                "- %s: exit=%s class=%s peak=%s oom_kill=%s swap=%s" % (
                    name, snap.get("exit_code"), snap.get("classification"),
                    cg.get("cgroup_peak_bytes"), cg.get("cgroup_oom_kill"),
                    cg.get("cgroup_swap_bytes")))
    lines += ["", "## Verdicts", ""]
    for name, verdict in result.get("verdicts", {}).items():
        lines.append("- %s: %s" % (name, verdict))
    lines += ["", "Full machine-readable data: see the JSON artifact.", ""]
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    try:
        base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        base_commit = "unknown"
    opencode_bin = find_opencode()
    result: dict = {
        "schema": "runtime-lab-disk-heap-benchmark/v1",
        "issue": 56,
        "run_id": args.run_id,
        "base_commit": base_commit,
        "pinned_opencode_version": pinned_version(),
        "opencode_bin": opencode_bin or "",
        "constraint": {"ram_mb": 512, "swap": "none",
                       "disk": "ordinary ephemeral filesystem"},
        "modes": modes,
        "tasks": tasks,
        "host": {},
        "docker": {},
        "verdicts": {},
    }
    if not opencode_bin:
        result["error"] = "no opencode binary found"
    build_dir = tempfile.mkdtemp(prefix="diskheap-build-")
    try:
        paths = {
            "vmmalloc": find_vmmalloc(args.vmmalloc_so),
            "memtier": find_memtier(),
            "shim": build_shim(build_dir),
        }
        result["libs"] = paths
        if args.include_network and opencode_bin:
            for mode in modes:
                pool_dir = tempfile.mkdtemp(prefix="diskheap-pool-")
                env, avail = mode_env(mode, paths, pool_dir)
                entry: dict = {"env_keys": sorted(env.keys()),
                               "pool_dir": pool_dir}
                if not avail["available"]:
                    entry.update(avail)
                    entry["available"] = False
                    result["host"][mode] = entry
                    continue
                entry["available"] = True
                ws = tempfile.mkdtemp(prefix="diskheap-ws-")
                snap_path = os.path.join(ws, "snap.json")
                snap = run_snap(ws, snap_path, args.budget,
                                [opencode_bin, "run", "--auto", "--model",
                                 AGENT_MODEL, AGENT_PROMPT], env)
                entry["snap"] = snap
                result["host"][mode] = entry
        else:
            result["host"] = {"status": "skipped",
                              "reason": "rerun with --include-network"}
        if args.docker and args.include_network and opencode_bin:
            if not docker_available():
                result["docker"] = {"status": "skipped",
                                    "reason": "docker not installed"}
            else:
                for mode in modes:
                    env, avail = mode_env(mode, paths, "/pool")
                    if not avail["available"]:
                        result["docker"][mode] = {
                            "available": False, "reason": avail["reason"],
                            "snap": {}, "cgroup": {}}
                        continue
                    # Remap pool paths into the container filesystem.
                    cenv = dict(env)
                    for key in ("VMMALLOC_POOL_DIR", "DISKHEAP_DIR"):
                        if key in cenv:
                            cenv[key] = "/pool"
                    if "MEMKIND_MEM_TIERS" in cenv:
                        cenv["MEMKIND_MEM_TIERS"] = memtier_tiers("/pool")
                    extra: list = []
                    prelude = ":"
                    for key in ("LD_PRELOAD",):
                        host_lib = cenv.get(key)
                        if host_lib and os.path.isfile(host_lib):
                            cont = "/snap/%s" % os.path.basename(host_lib)
                            extra += ["-v", "%s:%s:ro" % (host_lib, cont)]
                            cenv[key] = cont
                    if mode == "memtier":
                        # Use the container's own libmemtier (same SONAME,
                        # distro-native dependencies) instead of mounting
                        # the host build: mounting one .so without its full
                        # dependency closure is fragile across releases.
                        cenv["LD_PRELOAD"] = (
                            "/usr/lib/x86_64-linux-gnu/libmemtier.so.0")
                        extra = []
                        prelude = ("apt-get update -qq && "
                                   "apt-get install -y -qq libmemkind0 "
                                   "> /dev/null 2>&1; :")
                    ws_host = tempfile.mkdtemp(prefix="diskheap-dockws-")
                    snap_host = os.path.join(ws_host, "snap.json")
                    for task in tasks:
                        label = mode if len(tasks) == 1 else "%s+%s" % (mode,
                                                                       task)
                        result["docker"][label] = run_docker_mode(
                            opencode_bin, cenv, task, ws_host,
                            snap_host, args.docker_image, extra, prelude)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
    text = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        parent = os.path.dirname(os.path.abspath(args.output))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    if args.report:
        parent = os.path.dirname(os.path.abspath(args.report))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(render_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
