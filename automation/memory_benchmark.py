"""Reproducible Render worker memory benchmark (issue #52).

Rerun manually with::

    python3 automation/memory_benchmark.py --output /tmp/mem-result.json --report /tmp/mem-report.md
    python3 automation/memory_benchmark.py --include-network --output /tmp/mem-result.json

Stdlib only. Never requires secrets or a paid Render plan. Network-backed
OpenCode agent measurements are separated from deterministic local
measurements and are gated behind --include-network (default off for the
fast hermetic path; the checked-in report records a run with them on).

Measures total process-tree memory (never parent RSS alone): per-process
VmRSS polling over the full descendant family, cgroup v2 memory.current /
memory.peak / memory.events when available, and /usr/bin/time -v maximum
RSS as a secondary signal.
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

OPENCODE_INSTALL_URL = "https://opencode.ai/install"
OPENCODE_DOCS_URL = "https://opencode.ai/docs/"
OPENCODE_RELEASES_URL = "https://github.com/anomalyco/opencode/releases"
RENDER_FREE_DOC = "https://render.com/docs/free"

DOCKER_512_NO_SWAP = ["--memory=512m", "--memory-swap=512m"]


def read_proc_status(pid: int) -> dict:
    """Best-effort VmRSS/VmHWM/Threads for one pid (empty dict on failure)."""
    try:
        with open("/proc/%d/status" % pid, "r", encoding="utf-8") as handle:
            out: dict = {}
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        out["rss_kb"] = int(parts[1])
                elif line.startswith("VmHWM:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        out["hwm_kb"] = int(parts[1])
                elif line.startswith("Threads:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        out["threads"] = int(parts[1])
            return out
    except (OSError, ValueError):
        return {}


def read_cgroup() -> dict:
    """Best-effort cgroup v2 memory signals for this process's cgroup."""
    out: dict = {}
    for name, path in (
        ("cgroup_current_bytes", "/sys/fs/cgroup/memory.current"),
        ("cgroup_peak_bytes", "/sys/fs/cgroup/memory.peak"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                out[name] = int(handle.read().strip().split()[0])
        except (OSError, ValueError, IndexError):
            pass
    try:
        with open("/sys/fs/cgroup/memory.events", "r", encoding="utf-8") as handle:
            events: dict = {}
            for line in handle:
                parts = line.split()
                if len(parts) == 2:
                    try:
                        events[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
            if events:
                out["cgroup_events"] = events
    except OSError:
        pass
    return out


def process_family(root_pid: int) -> set[int]:
    """All pids in the root's descendant family (root included)."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return {root_pid}
    mapping: list[tuple[int, int]] = []
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


def run_with_peak(
    cmd: list[str],
    cwd: str,
    timeout: float,
    poll_interval: float = 0.15,
) -> dict:
    """Run cmd, polling the full process-tree RSS until exit.

    Returns peak_main_kb, peak_tree_kb, max_family_size, wall_seconds,
    exit_code, timed_out, and first bytes of stdout/stderr (truncated).
    """
    start = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "cmd": cmd,
            "started": False,
            "error": "command not found: %s" % exc,
            "peak_main_kb": 0,
            "peak_tree_kb": 0,
            "max_family_size": 0,
            "wall_seconds": 0.0,
            "exit_code": 127,
            "timed_out": False,
        }
    peak_main = 0
    peak_tree = 0
    max_family = 1
    root = proc.pid
    while True:
        exited = proc.poll() is not None
        try:
            family = process_family(root)
            max_family = max(max_family, len(family))
            total = 0
            main = 0
            for pid in family:
                info = read_proc_status(pid)
                rss = info.get("rss_kb", 0)
                total += rss
                if pid == root:
                    main = rss
            peak_tree = max(peak_tree, total)
            peak_main = max(peak_main, main)
        except Exception:
            pass
        if exited:
            break
        if time.time() - start > timeout:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            return {
                "cmd": cmd,
                "started": True,
                "peak_main_kb": peak_main,
                "peak_tree_kb": peak_tree,
                "max_family_size": max_family,
                "wall_seconds": round(time.time() - start, 2),
                "exit_code": 124,
                "timed_out": True,
                "stdout": "",
                "stderr": "benchmark timeout after %.1fs" % timeout,
            }
        time.sleep(poll_interval)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except Exception:
        stdout, stderr = "", ""
    # One last sweep: reaped children may already be gone, so the polled
    # peak above is the authoritative number (VmHWM is unavailable post-exit).
    return {
        "cmd": cmd,
        "started": True,
        "peak_main_kb": peak_main,
        "peak_tree_kb": peak_tree,
        "max_family_size": max_family,
        "wall_seconds": round(time.time() - start, 2),
        "exit_code": proc.returncode,
        "timed_out": False,
        "stdout": (stdout or "")[:500],
        "stderr": (stderr or "")[:500],
    }


def time_v_peak(cmd: list[str], cwd: str, timeout: float) -> dict:
    """Secondary signal: /usr/bin/time -v maximum RSS (main process)."""
    if shutil.which("time") is None and not os.path.exists("/usr/bin/time"):
        return {"available": False, "reason": "/usr/bin/time not installed"}
    try:
        proc = subprocess.run(
            ["/usr/bin/time", "-v"] + cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        max_rss = None
        for line in proc.stderr.splitlines():
            if "Maximum resident set size" in line:
                try:
                    max_rss = int(line.split(":")[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
        return {
            "available": True,
            "max_rss_kb": max_rss,
            "exit_code": proc.returncode,
        }
    except FileNotFoundError:
        return {"available": False, "reason": "/usr/bin/time not installed"}
    except subprocess.TimeoutExpired:
        return {"available": True, "timed_out": True}


def find_opencode() -> str | None:
    """Locate the standalone OpenCode binary (PATH then HOME fallback)."""
    found = shutil.which("opencode")
    if found:
        return found
    candidate = os.path.join(
        os.path.expanduser("~"), ".opencode", "bin", "opencode"
    )
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    # Deterministic deploy artifact (repo-relative).
    candidate = os.path.join(REPO_ROOT, ".opencode-bin", "opencode")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def measure_python_idle() -> dict:
    """Idle controller footprint: bare python plus JobManager construction."""
    out: dict = {}
    sleeper = run_with_peak(
        [sys.executable, "-c", "import time; time.sleep(3)"],
        REPO_ROOT,
        timeout=20.0,
    )
    out["bare_python_sleep_peak_tree_kb"] = sleeper["peak_tree_kb"]
    try:
        from runner_server import JobManager  # noqa: E402
        from opencode_runner import OPENCODE_PINNED_VERSION  # noqa: E402

        before = read_proc_status(os.getpid())
        manager = JobManager(
            workspace_root=tempfile.mkdtemp(prefix="bench-ws-"),
            job_timeout_seconds=60.0,
            opencode_bin="/nonexistent-bench-opencode",
        )
        after = read_proc_status(os.getpid())
        snapshot = manager.health_snapshot()
        out["jobmanager_construct_rss_kb"] = after.get("rss_kb", 0)
        out["jobmanager_construct_rss_delta_kb"] = max(
            0, after.get("rss_kb", 0) - before.get("rss_kb", 0)
        )
        out["health_resources"] = snapshot.get("resources", {})
        out["pinned_version"] = OPENCODE_PINNED_VERSION
        shutil.rmtree(manager.workspace_root, ignore_errors=True)
    except Exception as exc:
        out["error"] = str(exc)[:300]
    # Live runner_server on an ephemeral port (real HTTP stack, no fixed port).
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'automation');"
                "from runner_server import create_server;"
                "s=create_server(host='127.0.0.1', port=0);"
                "import time; time.sleep(4)",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.0)
        info = read_proc_status(proc.pid)
        family = process_family(proc.pid)
        total = sum(read_proc_status(p).get("rss_kb", 0) for p in family)
        out["runner_server_process_rss_kb"] = info.get("rss_kb", 0)
        out["runner_server_process_hwm_kb"] = info.get("hwm_kb", 0)
        out["runner_server_tree_kb"] = total
        out["runner_server_threads"] = info.get("threads", 0)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    except Exception as exc:
        out["live_server_error"] = str(exc)[:200]
    return out


def measure_git_phase() -> dict:
    """Deterministic local clone/checkout proxy (no network, no credentials)."""
    out: dict = {}
    with tempfile.TemporaryDirectory(prefix="bench-git-") as tmp:
        dest = os.path.join(tmp, "clone")
        clone = run_with_peak(
            ["git", "clone", "--depth", "1", "file://%s" % REPO_ROOT, dest],
            tmp,
            timeout=120.0,
        )
        out["clone"] = {
            "peak_tree_kb": clone["peak_tree_kb"],
            "peak_main_kb": clone["peak_main_kb"],
            "max_family_size": clone["max_family_size"],
            "wall_seconds": clone["wall_seconds"],
            "exit_code": clone["exit_code"],
            "timed_out": clone["timed_out"],
            "time_v": time_v_peak(
                ["git", "clone", "--depth", "1", "file://%s" % REPO_ROOT,
                 os.path.join(tmp, "clone2")],
                tmp,
                timeout=120.0,
            ),
        }
        if clone["exit_code"] == 0 and os.path.isdir(dest):
            checkout = run_with_peak(
                ["git", "checkout", "HEAD"],
                dest,
                timeout=60.0,
            )
            out["checkout"] = {
                "peak_tree_kb": checkout["peak_tree_kb"],
                "peak_main_kb": checkout["peak_main_kb"],
                "exit_code": checkout["exit_code"],
            }
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=dest,
                capture_output=True,
                text=True,
                timeout=30,
            )
            out["status_exit"] = status.returncode
    return out


def measure_variant_a(opencode_bin: str | None) -> dict:
    """Variant A: minimal Python runner + subprocess (fake agent, hermetic)."""
    out: dict = {"concept": "python controller + subprocess agent (fake)"}
    with tempfile.TemporaryDirectory(prefix="bench-a-") as tmp:
        fake = os.path.join(tmp, "fake-opencode")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                "echo \"fake agent working\"\n"
                "sleep 2\n"
                "echo done > agent-output.txt\n"
            )
        os.chmod(fake, 0o755)
        result = run_with_peak(
            [sys.executable, "-c",
             "import subprocess,sys,time;"
             "p=subprocess.Popen(sys.argv[1:],cwd=%r);"
             "peak=0;"
             "import os;"
             "s=open('/proc/self/status').read if os.path.exists('/proc/self/status') else None;"
             "p.wait(timeout=60);print('rc',p.returncode)" % tmp,
             fake, "--auto", "--model", "fake-model", "fake task"],
            tmp,
            timeout=60.0,
        )
        out["controller_plus_subprocess"] = {
            "peak_tree_kb": result["peak_tree_kb"],
            "peak_main_kb": result["peak_main_kb"],
            "max_family_size": result["max_family_size"],
            "exit_code": result["exit_code"],
        }
    return out


def measure_variant_b(opencode_bin: str | None) -> dict:
    """Variant B: direct standalone baseline (deterministic --version)."""
    out: dict = {"concept": "standalone binary, no Python controller"}
    if not opencode_bin:
        out["skipped"] = "no opencode binary found on this machine"
        return out
    version = run_with_peak([opencode_bin, "--version"], REPO_ROOT, timeout=60.0)
    out["version"] = {
        "peak_tree_kb": version["peak_tree_kb"],
        "peak_main_kb": version["peak_main_kb"],
        "max_family_size": version["max_family_size"],
        "wall_seconds": version["wall_seconds"],
        "exit_code": version["exit_code"],
        "stdout": version.get("stdout", "")[:100],
        "time_v": time_v_peak([opencode_bin, "--version"], REPO_ROOT, 60.0),
    }
    try:
        size = os.path.getsize(opencode_bin)
        out["binary_bytes"] = size
        proc = subprocess.run(
            ["file", opencode_bin], capture_output=True, text=True, timeout=10
        )
        out["file_type"] = (proc.stdout or "").strip()[:200]
    except Exception:
        pass
    return out


def measure_variant_c() -> dict:
    """Variant C: package/runtime forms genuinely supported upstream."""
    out: dict = {
        "concept": "npm/Node wrapper and Bun runtime (docs-listed forms only)",
        "npm_package": "opencode-ai",
    }
    node = shutil.which("node")
    out["node_available"] = bool(node)
    if node:
        probe = run_with_peak([node, "--version"], REPO_ROOT, timeout=30.0)
        out["node_version_peak_tree_kb"] = probe["peak_tree_kb"]
        out["node_version_stdout"] = probe.get("stdout", "")[:50]
        out["node_version_time_v"] = time_v_peak(
            [node, "--version"], REPO_ROOT, 30.0
        )
        if not probe["peak_tree_kb"]:
            out["node_version_note"] = (
                "short-lived node --version exited between RSS polls; "
                "see node_version_time_v for the secondary signal"
            )
    else:
        out["node_note"] = "node not installed; npm wrapper cannot launch here"
    out["bun_available"] = bool(shutil.which("bun"))
    if not shutil.which("bun"):
        out["bun_note"] = "bun not installed; Bun package launch not measurable here"
    # The npm tarball is a 3 kB downloader wrapper (bin/opencode.exe +
    # postinstall.mjs) that fetches the same standalone binary, so the
    # steady-state agent footprint equals variant B plus a transient node
    # runtime (~30-50 MB) during install/launch. No artificial variant is
    # benchmarked beyond this documented accounting.
    out["conclusion"] = (
        "npm/bun/pnpm/yarn forms are install shims around the same "
        "standalone release binary; they add a Node/Bun runtime process "
        "instead of replacing the binary, so they cannot beat variant B."
    )
    return out


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_stress(include_network: bool, opencode_bin: str | None) -> dict:
    """512 MB no-swap stress test using Docker as the limit harness."""
    out: dict = {
        "harness": "docker",
        "limits": DOCKER_512_NO_SWAP,
        "meaning": "equivalent to --memory=512m --memory-swap=512m",
    }
    if not docker_available():
        out["status"] = "skipped"
        out["reason"] = (
            "docker is not installed on this runner, so the exact cgroup "
            "limit could not be created here; rerun where docker exists"
        )
        return out
    # Prove the harness can hold 512m/no-swap: read the enforced ceiling.
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm"] + DOCKER_512_NO_SWAP + [
                "ubuntu:22.04", "bash", "-c",
                "cat /sys/fs/cgroup/memory.max; "
                "cat /sys/fs/cgroup/memory.swap.max",
            ],
            capture_output=True, text=True, timeout=120,
        )
        out["ceiling_probe"] = {
            "exit_code": proc.returncode,
            "output": (proc.stdout or "")[:200],
        }
    except Exception as exc:
        out["ceiling_probe"] = {"error": str(exc)[:200]}
    # Positive control: --version must pass well under the ceiling.
    if opencode_bin:
        try:
            proc = subprocess.run(
                ["docker", "run", "--rm"] + DOCKER_512_NO_SWAP + [
                    "-v", "%s:/usr/local/bin/opencode:ro" % opencode_bin,
                    "ubuntu:22.04", "bash", "-c",
                    "/usr/local/bin/opencode --version; echo exit=$?; "
                    "echo peak=$(cat /sys/fs/cgroup/memory.peak); "
                    "cat /sys/fs/cgroup/memory.events",
                ],
                capture_output=True, text=True, timeout=120,
            )
            out["version_under_512m"] = {
                "exit_code": proc.returncode,
                "output": ((proc.stdout or "") + (proc.stderr or ""))[:800],
            }
        except Exception as exc:
            out["version_under_512m"] = {"error": str(exc)[:200]}
    # Negative control: a hog over the ceiling must OOM (proves detection).
    # Uses perl (present in ubuntu:22.04) because that image has no python3.
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm"] + DOCKER_512_NO_SWAP + [
                "ubuntu:22.04", "bash", "-c",
                "perl -e '$a = \"x\" x 700000000; sleep 5; print length($a)'; "
                "echo exit=$?; echo peak=$(cat /sys/fs/cgroup/memory.peak); "
                "cat /sys/fs/cgroup/memory.events",
            ],
            capture_output=True, text=True, timeout=120,
        )
        out["hog_over_512m"] = {
            "exit_code": proc.returncode,
            "output": ((proc.stdout or "") + (proc.stderr or ""))[:800],
        }
    except Exception as exc:
        out["hog_over_512m"] = {"error": str(exc)[:200]}
    # Realistic candidate under the ceiling (network-backed, separated).
    if include_network and opencode_bin:
        try:
            proc = subprocess.run(
                ["docker", "run", "--rm"] + DOCKER_512_NO_SWAP + [
                    "-v", "%s:/usr/local/bin/opencode:ro" % opencode_bin,
                    "ubuntu:22.04", "bash", "-c",
                    "/usr/local/bin/opencode run --auto --model "
                    "opencode/muse-spark-1.3-contributor-free "
                    "\"say hi in one word\" 2>&1 | head -5; "
                    "echo agent_exit=${PIPESTATUS[0]}; "
                    "echo peak=$(cat /sys/fs/cgroup/memory.peak); "
                    "cat /sys/fs/cgroup/memory.events",
                ],
                capture_output=True, text=True, timeout=240,
            )
            text = ((proc.stdout or "") + (proc.stderr or ""))[:1200]
            out["real_agent_under_512m"] = {
                "docker_exit_code": proc.returncode,
                "output": text,
                "classification": classify_stress_output(text),
            }
        except Exception as exc:
            out["real_agent_under_512m"] = {"error": str(exc)[:200]}
    elif not include_network:
        out["real_agent_under_512m"] = {
            "status": "skipped",
            "reason": "rerun with --include-network for the live agent stress",
        }
    return out


def classify_stress_output(text: str) -> str:
    """Distinguish success / OOM / crash / provider failure from signals."""
    lowered = text.lower()
    if "oom_kill 1" in lowered or "oom 1" in lowered:
        return "oom_kill"
    if "agent_exit=137" in lowered or "exit code 137" in lowered:
        return "oom_kill"
    if "agent_exit=0" in lowered:
        return "successful_completion"
    if "agent_exit=124" in lowered or "timed out" in lowered:
        return "timeout"
    if any(term in lowered for term in ("model", "provider", "unavailable",
                                        "not found", "unauthorized", "401",
                                        "403", "429")):
        return "provider_or_model_failure"
    return "application_crash_or_unknown"


def measure_network_agent(opencode_bin: str | None) -> dict:
    """Real network-backed agent run on the host (separated, best-effort)."""
    out: dict = {"model": "opencode/muse-spark-1.3-contributor-free"}
    if not opencode_bin:
        out["status"] = "skipped"
        out["reason"] = "no opencode binary found"
        return out
    result = run_with_peak(
        [opencode_bin, "run", "--auto", "--model",
         "opencode/muse-spark-1.3-contributor-free", "say hi in one word"],
        tempfile.gettempdir(),
        timeout=180.0,
    )
    out.update({
        "peak_tree_kb": result["peak_tree_kb"],
        "peak_main_kb": result["peak_main_kb"],
        "max_family_size": result["max_family_size"],
        "wall_seconds": result["wall_seconds"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "stdout": result.get("stdout", "")[:200],
        "stderr": result.get("stderr", "")[:300],
        "time_v": time_v_peak(
            [opencode_bin, "run", "--auto", "--model",
             "opencode/muse-spark-1.3-contributor-free", "say hi in one word"],
            tempfile.gettempdir(),
            timeout=180.0,
        ),
    })
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render worker memory benchmark (issue #52)."
    )
    parser.add_argument("--output", default=None,
                        help="Machine-readable JSON output path.")
    parser.add_argument("--report", default=None,
                        help="Markdown report output path.")
    parser.add_argument("--include-network", action="store_true",
                        help="Include live network-backed agent measurements.")
    parser.add_argument("--run-id", default="local",
                        help="Run identifier recorded in the outputs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from opencode_runner import OPENCODE_PINNED_VERSION  # noqa: E402
        pinned = OPENCODE_PINNED_VERSION
    except Exception:
        pinned = "unknown"
    opencode_bin = find_opencode()
    try:
        base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        base_commit = "unknown"

    result: dict = {
        "schema": "runtime-lab-memory-benchmark/v1",
        "issue": 52,
        "run_id": args.run_id,
        "base_commit": base_commit,
        "pinned_opencode_version": pinned,
        "opencode_bin": opencode_bin or "",
        "sources": {
            "installer": OPENCODE_INSTALL_URL,
            "docs": OPENCODE_DOCS_URL,
            "releases": OPENCODE_RELEASES_URL,
            "render_free": RENDER_FREE_DOC,
        },
        "constraint": {"cpu": "0.1", "ram_mb": 512, "swap": "none assumed"},
        "host": {
            "cgroup": read_cgroup(),
            "python_version": sys.version.split()[0],
        },
        "deterministic": {
            "python_idle": measure_python_idle(),
            "git_phase": measure_git_phase(),
            "variant_a_python_controller": measure_variant_a(opencode_bin),
            "variant_b_standalone": measure_variant_b(opencode_bin),
            "variant_c_packages": measure_variant_c(),
        },
        "stress_512m_no_swap": docker_stress(args.include_network, opencode_bin),
    }
    if args.include_network:
        result["network_backed"] = {"real_agent_host": measure_network_agent(opencode_bin)}
    else:
        result["network_backed"] = {
            "status": "skipped",
            "reason": "rerun with --include-network",
        }

    # Budget synthesis from the strongest available peak signal.
    peaks: list[int] = []
    try:
        peaks.append(
            result["deterministic"]["variant_b_standalone"]["version"]["peak_tree_kb"]
        )
    except (KeyError, TypeError):
        pass
    try:
        peaks.append(result["network_backed"]["real_agent_host"]["peak_tree_kb"])
    except (KeyError, TypeError):
        pass
    measured_peak = max([p for p in peaks if isinstance(p, int) and p > 0] or [0])
    result["budget"] = {
        "measured_peak_kb": measured_peak,
        "recommended_headroom_kb": 128 * 1024,
        "pass_threshold_kb": 512 * 1024,
        "fits_512m": bool(measured_peak) and measured_peak < 512 * 1024,
    }

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
        report = render_report(result)
        parent = os.path.dirname(os.path.abspath(args.report))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report)
    return 0


def render_report(result: dict) -> str:
    """Concise human-readable summary of the machine-readable result."""
    lines = [
        "# Render worker memory benchmark (issue #52)",
        "",
        "- Pinned OpenCode: %s" % result.get("pinned_opencode_version", "?"),
        "- Binary: %s" % (result.get("opencode_bin") or "not found"),
        "- Constraint: 0.1 CPU / 512 MB RAM, no swap assumed",
        "",
        "## Deterministic measurements (kB)",
        "",
    ]
    det = result.get("deterministic", {})
    idle = det.get("python_idle", {})
    lines.append(
        "- Python idle: runner_server tree=%s, bare python=%s" % (
            idle.get("runner_server_tree_kb", "?"),
            idle.get("bare_python_sleep_peak_tree_kb", "?"),
        )
    )
    git = det.get("git_phase", {})
    if isinstance(git.get("clone"), dict):
        lines.append(
            "- git clone --depth 1 (file:// proxy): tree=%s main=%s" % (
                git["clone"].get("peak_tree_kb", "?"),
                git["clone"].get("peak_main_kb", "?"),
            )
        )
    var_b = det.get("variant_b_standalone", {})
    if isinstance(var_b.get("version"), dict):
        lines.append(
            "- opencode --version: tree=%s main=%s family=%s" % (
                var_b["version"].get("peak_tree_kb", "?"),
                var_b["version"].get("peak_main_kb", "?"),
                var_b["version"].get("max_family_size", "?"),
            )
        )
    net = result.get("network_backed", {})
    agent = net.get("real_agent_host", {})
    if isinstance(agent.get("peak_tree_kb"), int):
        lines.append(
            "- opencode run (live free model): tree=%s main=%s exit=%s" % (
                agent.get("peak_tree_kb"), agent.get("peak_main_kb"),
                agent.get("exit_code"),
            )
        )
    else:
        lines.append("- opencode run (live): %s" % net.get("reason", net.get("status", "?")))
    stress = result.get("stress_512m_no_swap", {})
    lines += ["", "## 512 MB no-swap stress (docker harness)", ""]
    for key in ("version_under_512m", "hog_over_512m", "real_agent_under_512m"):
        entry = stress.get(key, {})
        output = str(entry.get("output", entry.get("reason", entry.get("status", "?"))))
        lines.append("- %s: %s" % (key, output[:300].replace("\n", " | ")))
    budget = result.get("budget", {})
    lines += [
        "",
        "## Budget",
        "",
        "- measured peak kB: %s" % budget.get("measured_peak_kb", "?"),
        "- pass threshold kB: %s" % budget.get("pass_threshold_kb", "?"),
        "- fits in 512 MB: %s" % budget.get("fits_512m", "?"),
        "",
        "Full machine-readable data: see the JSON artifact.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
