"""Process sampler for disk-backed heap experiments (issue #56).

Runs one command, polling once per second until exit or a time budget:
- full descendant-family VmRSS peak (never parent RSS alone);
- /proc/<pid>/smaps classification per process: anonymous-private RSS,
  file-backed RSS, [heap] RSS, unlinked-file (deleted) RSS, and RSS in
  allocator pool mappings (memkind/vmem/vmmalloc/shim pool files);
- top anonymous-private mappings by RSS at the end (to name the mappings
  LD_PRELOAD cannot intercept);
- cgroup v2 memory.peak / memory.events / swap.current surrounding the run
  when those files exist (proves reclaim/OOM behaviour under pressure).

Stdlib only. Safe to bind-mount into the 512 MB Docker stress container.
Usage:
    python3 disk_heap_snap.py <budget_s> <workdir> <out.json> VAR=val ... -- cmd...
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import time


def read_proc_status(pid: int) -> dict:
    try:
        out: dict = {}
        with open("/proc/%d/status" % pid, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    out["rss_kb"] = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    out["hwm_kb"] = int(line.split()[1])
        return out
    except (OSError, ValueError):
        return {}


def process_family(root_pid: int) -> set:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return {root_pid}
    mapping = []
    for line in proc.stdout.splitlines()[1:]:
        try:
            pid, ppid = map(int, line.split())
            mapping.append((pid, ppid))
        except ValueError:
            continue
    family = {root_pid}
    grew = True
    while grew:
        grew = False
        for pid, ppid in mapping:
            if ppid in family and pid not in family:
                family.add(pid)
                grew = True
    return family


def is_pool_path(path: str) -> bool:
    base = path.split("(")[0].strip()
    return (
        "memkind" in path
        or "vmem." in path
        or "vmmalloc" in path
        or "diskheap" in path
        or ("/#" in base)
    )


def classify_smaps(pid: int) -> dict | None:
    """Classify one process's RSS from smaps. None when already reaped."""
    anon = fileb = heap = deleted = pool = 0
    pool_paths: set = set()
    try:
        current = ""
        with open("/proc/%d/smaps" % pid, "r", encoding="utf-8") as handle:
            for line in handle:
                if line[0] not in " \t" and "-" in line.split()[0]:
                    parts = line.split()
                    current = parts[5] if len(parts) >= 6 else ""
                elif line.startswith("Rss:"):
                    rss = int(line.split()[1])
                    if is_pool_path(current):
                        pool += rss
                        pool_paths.add(current)
                    if "(deleted)" in current:
                        deleted += rss
                    if current.startswith("/") or current.startswith("[v"):
                        fileb += rss
                    else:
                        anon += rss
                        if current == "[heap]":
                            heap += rss
    except (OSError, ValueError):
        return None
    return {
        "anon_kb": anon,
        "file_kb": fileb,
        "heap_kb": heap,
        "deleted_kb": deleted,
        "pool_kb": pool,
        "pool_paths": sorted(pool_paths),
    }


def top_anon_mappings(pid: int, limit: int = 15) -> list:
    """Largest anonymous-private mappings by RSS: (rss_kb, perms, pathname)."""
    try:
        buckets: dict = collections.defaultdict(int)
        current = ""
        perms = ""
        with open("/proc/%d/smaps" % pid, "r", encoding="utf-8") as handle:
            for line in handle:
                if line[0] not in " \t" and "-" in line.split()[0]:
                    parts = line.split()
                    perms = parts[1] if len(parts) > 1 else ""
                    current = parts[5] if len(parts) >= 6 else ""
                elif line.startswith("Rss:"):
                    rss = int(line.split()[1])
                    if not current.startswith("/") and not current.startswith("[v"):
                        buckets[(current, perms)] += rss
        ranked = sorted(
            ((rss, path, pm) for (path, pm), rss in buckets.items() if rss > 0),
            reverse=True,
        )
        return [
            {"rss_kb": rss, "path": path, "perms": pm}
            for rss, path, pm in ranked[:limit]
        ]
    except (OSError, ValueError):
        return []


def read_cgroup_file(name: str):
    try:
        with open("/sys/fs/cgroup/%s" % name, "r", encoding="utf-8") as handle:
            return handle.read().strip()[:4000]
    except OSError:
        return None


def parse_events(text: str | None) -> dict:
    out: dict = {}
    if not text:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def read_cgroup_snapshot() -> dict:
    snap: dict = {}
    for key, name in (
        ("max", "memory.max"),
        ("current", "memory.current"),
        ("peak", "memory.peak"),
        ("swap_current", "memory.swap.current"),
        ("swap_max", "memory.swap.max"),
    ):
        raw = read_cgroup_file(name)
        if raw is None:
            continue
        first = raw.split()[0] if raw.split() else ""
        try:
            snap[key] = int(first)
        except ValueError:
            snap[key] = first
    snap["events"] = parse_events(read_cgroup_file("memory.events"))
    return snap


def classify_exit(text: str, returncode: int | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if returncode == 0:
        return "successful_completion"
    if returncode in (-9, 137):
        return "oom_kill_or_sigkill"
    lowered = (text or "").lower()
    if "oom_kill 1" in lowered:
        return "oom_kill"
    if any(
        term in lowered
        for term in ("model", "provider", "unavailable", "unauthorized", "401",
                     "403", "429", "not found")
    ):
        return "provider_or_model_failure"
    if returncode in (-11, 139):
        return "sigsegv_crash"
    return "application_crash_or_unknown"


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    budget = float(args[0])
    workdir = args[1]
    outpath = args[2]
    rest = args[3:]
    sep = rest.index("--")
    env_vars = rest[:sep]
    cmd = rest[sep + 1:]
    env = dict(os.environ)
    for item in env_vars:
        key, value = item.split("=", 1)
        env[key] = value

    cgroup_before = read_cgroup_snapshot()
    start = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    root = proc.pid
    peak_tree = 0
    max_family = 1
    aggregate = {"anon_kb": 0, "file_kb": 0, "heap_kb": 0,
                 "deleted_kb": 0, "pool_kb": 0}
    pool_paths: set = set()
    series = []
    timed_out = False
    last_top_anon: list = []
    while True:
        done = proc.poll() is not None
        try:
            family = process_family(root)
        except Exception:
            family = {root}
        max_family = max(max_family, len(family))
        total = sum(read_proc_status(pid).get("rss_kb", 0) for pid in family)
        peak_tree = max(peak_tree, total)
        current = {"anon_kb": 0, "file_kb": 0, "heap_kb": 0,
                   "deleted_kb": 0, "pool_kb": 0}
        for pid in family:
            sample = classify_smaps(pid)
            if sample is None:
                continue
            for key in current:
                current[key] += sample[key]
            pool_paths |= set(sample["pool_paths"])
        aggregate = current
        series.append(
            {"t_s": round(time.time() - start, 1), "tree_kb": total, **current}
        )
        try:
            live_top = top_anon_mappings(root, 15)
            if live_top:
                last_top_anon = live_top
        except Exception:
            pass
        if done:
            break
        if time.time() - start > budget:
            proc.kill()
            timed_out = True
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            break
        time.sleep(1.0)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except Exception:
        stdout, stderr = "", ""
    cgroup_after = read_cgroup_snapshot()
    top_anon = last_top_anon
    combined = ((stdout or "") + "\n" + (stderr or ""))[-800:]
    result = {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "classification": classify_exit(combined, proc.returncode, timed_out),
        "wall_s": round(time.time() - start, 2),
        "peak_tree_kb": peak_tree,
        "max_family_size": max_family,
        "final_class_kb": aggregate,
        "pool_paths": sorted(pool_paths)[:10],
        "top_anon_mappings": top_anon,
        "cgroup_before": cgroup_before,
        "cgroup_after": cgroup_after,
        "samples": len(series),
        "series_tail": series[-3:],
        "stdout": (stdout or "")[:400],
        "stderr": (stderr or "")[:400],
    }
    parent = os.path.dirname(os.path.abspath(outpath))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "exit_code": result["exit_code"],
                "classification": result["classification"],
                "wall_s": result["wall_s"],
                "peak_tree_kb": result["peak_tree_kb"],
                "final_class_kb": result["final_class_kb"],
                "pool_paths": result["pool_paths"],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
