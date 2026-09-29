"""A/B benchmark driver for the issue-#144 mmap file-backing experiment.

Runs the exact PR #15 binary on the frozen FOO_LIMIT coding workload
under Docker ``--memory=512m --memory-swap=512m`` (verified
``memory.max=536870912``, ``memory.swap.max=0``), with the LD_PRELOAD
mmap shim OFF (arm A) and ON (arm B), two independent trials per arm.

Stdlib only. No git mutations, no workflow edits, no Render service.
Usage::

    python3 automation/mmap_filebacked_bench.py \
        --binary /path/to/opencode --shim /path/to/libmmapfb.so \
        --work-root /tmp/mmapfb-bench --out automation/benchmark-results/mmapfb-issue-144.json \
        [--arms off,on] [--trials 2] [--timeout 420]

The SAME binary must be used for both arms (the driver fingerprints it
once and stamps every trial; pass ``--expected-sha256`` to fail closed
on substitution).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mmap_filebacked as mfb

SCHEMA = "runtime-lab-mmapfb-bench/v1"


def run_one_trial(binary: str, shim_so: str, work_root: str, trial: str,
                  use_shim: bool, timeout_s: int) -> dict:
    """Run one constrained trial; return the validated trial record."""
    repo_dir = os.path.join(work_root, "repo-%s" % trial)
    trial_dir = os.path.join(work_root, "trial-%s" % trial)
    os.makedirs(repo_dir, exist_ok=True)
    os.makedirs(trial_dir, exist_ok=True)
    mfb.create_representative_repo(repo_dir)
    # Fresh repo proof: the agent must perform the 10 -> 20 edit itself.
    with open(os.path.join(repo_dir, "foo_module.py"), encoding="utf-8") as h:
        assert "FOO_LIMIT = 10" in h.read(), "workload repo not fresh"
    fp = mfb.fingerprint_binary(binary)
    cmd = mfb.docker_trial_command(
        binary, shim_so, repo_dir, trial_dir, shim=use_shim,
        timeout_s=timeout_s)
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout_s + 180)
    wall = round(time.time() - start, 1)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tele = mfb.parse_docker_trial_output(output)
    peak = int(tele["memory_peak_bytes"])
    current = tele.get("memory_current_bytes")
    # Host-side edit verification (the agent must land FOO_LIMIT = 20).
    correctness = "fail"
    with open(os.path.join(repo_dir, "foo_module.py"), encoding="utf-8") as h:
        content = h.read()
    if "FOO_LIMIT = 20" in content and tele.get("agent_exit") == 0:
        correctness = "pass"
    shim_stats = None
    if use_shim:
        try:
            shim_stats = mfb.load_shim_stats_dir(trial_dir)
        except ValueError:
            shim_stats = None
    smaps = {
        "backing_rss_kb": tele.get("backing_rss_kb", 0),
        "backing_pss_kb": tele.get("backing_pss_kb", 0),
        "backed_hits": tele.get("backed_hits", 0),
        "backed_samples": tele.get("backed_samples", 0),
        "backing_maps_lines": tele.get("backing_maps_lines", []),
    }
    record = {
        "trial": trial,
        "bun_options": "",
        "binary_sha256": fp["sha256"],
        "binary_bytes": fp["bytes"],
        "peak_tree_kb": peak // 1024,
        "peak_bytes": peak,
        "memory_current_bytes": current,
        "memory_peak_bytes": peak,
        "memory_events": tele.get("memory_events", {}),
        "swap_current_bytes": tele.get("swap_current_bytes"),
        "swap_max_bytes": tele.get("swap_max_bytes"),
        "wall_seconds": tele.get("wall_seconds", wall),
        "host_wall_seconds": wall,
        "docker_returncode": proc.returncode,
        "exit_code": int(tele.get("agent_exit", -1)),
        "correctness": correctness,
        "shim": "on" if use_shim else "off",
        "shim_stats": shim_stats,
        "smaps": smaps,
    }
    if use_shim and shim_stats is None:
        record["correctness"] = "fail"
        record["note"] = "no shim stats files recovered from trial workdir"
    return mfb.validate_trial_record(record)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--shim", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--arms", default="off,on")
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--expected-sha256", default="")
    args = parser.parse_args(argv)

    mfb.validate_source_identity()
    fp = mfb.fingerprint_binary(args.binary)
    if args.expected_sha256:
        mfb.verify_built_binary_sha256(fp["sha256"])
        if fp["sha256"] != args.expected_sha256.strip().lower():
            raise SystemExit("binary substitution: fingerprint mismatch")

    os.makedirs(args.work_root, exist_ok=True)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    result: dict = {
        "schema": SCHEMA,
        "source": mfb.validate_source_identity(),
        "binary": {"bytes": fp["bytes"], "sha256": fp["sha256"]},
        "limit_bytes": mfb.LIMIT_BYTES,
        "model": mfb.FREE_MODEL,
        "arms": {},
    }
    for arm in arms:
        if arm not in ("off", "on"):
            raise SystemExit("arms must be a subset of off,on")
        trials = []
        for i in range(1, args.trials + 1):
            label = "%s-t%d" % ("noshim" if arm == "off" else "shim", i)
            print("=== trial %s (shim=%s) ===" % (label, arm), flush=True)
            record = run_one_trial(args.binary, args.shim, args.work_root,
                                   label, arm == "on", args.timeout)
            print("exit=%s correctness=%s peak=%d max=%s oom_kill=%s" % (
                record["exit_code"], record["correctness"],
                record["peak_bytes"],
                (record["memory_events"] or {}).get("max"),
                (record["memory_events"] or {}).get("oom_kill")), flush=True)
            if arm == "on" and record.get("shim_stats"):
                print("redirected=%dB bypassed=%dB" % (
                    record["shim_stats"]["redirected_bytes"],
                    record["shim_stats"]["bypassed_bytes"]), flush=True)
            trials.append(record)
        result["arms"][arm] = {
            "verdict": mfb.classify_arm(trials),
            "trials": trials,
        }
    if set(arms) == {"off", "on"}:
        result["comparison"] = mfb.compare_arms(result["arms"]["off"]["trials"],
                                                result["arms"]["on"]["trials"])
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("wrote %s" % args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
