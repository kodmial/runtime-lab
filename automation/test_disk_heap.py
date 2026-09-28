"""Tests for the transparent disk-backed heap experiment (issue #56).

Offline only: no network, no Docker, no OpenCode binary required.
- smaps pool-path detection, cgroup events parsing, exit classification;
- memtier tier-string builder and per-mode env construction;
- shim compiles warning-free and passes a malloc/calloc/realloc/free/
  posix_memalign correctness probe under LD_PRELOAD (skipped without gcc);
- the sampler runs a trivial local command and writes a valid artifact;
- the benchmark parser/report helpers behave on fixtures.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = REPO_ROOT / "automation"
sys.path.insert(0, str(AUTOMATION))
sys.path.insert(0, str(REPO_ROOT))

from disk_heap_snap import (  # noqa: E402
    classify_exit,
    is_pool_path,
    parse_events,
)
from disk_heap_benchmark import (  # noqa: E402
    build_parser,
    build_shim,
    memtier_tiers,
    mode_env,
    render_report,
    run_snap,
)

SHIM_SRC = AUTOMATION / "disk_heap_shim" / "shim.c"

C_PROBE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void) {
    char *a = malloc(1024);
    if (!a) return 1;
    memset(a, 0xAB, 1024);
    char *b = calloc(64, 64);
    if (!b) return 2;
    char *c = realloc(a, 8192);
    if (!c) return 3;
    if (c[1000] != (char)0xAB) return 4;
    void *p = NULL;
    if (posix_memalign(&p, 64, 512) != 0) return 5;
    if (((unsigned long)p % 64) != 0) return 6;
    memset(p, 1, 512);
    free(c); free(b); free(p);
    /* foreign pointer forwarding must not crash */
    char *s = strdup("foreign");
    if (!s || strcmp(s, "foreign") != 0) return 7;
    free(s);
    printf("PROBE-OK\n");
    return 0;
}
"""


def test_is_pool_path_detects_allocator_pools():
    assert is_pool_path("/tmp/vmp4/#2401095 (deleted)")
    assert is_pool_path("/tmp/mtpool3/memkind.0e0GcE (deleted)")
    assert is_pool_path("/tmp/shimpool3/diskheap.BICWyV (deleted)")
    assert is_pool_path("/usr/lib/x86_64-linux-gnu/libvmmalloc.so.1.0.0")
    assert not is_pool_path("/usr/lib/x86_64-linux-gnu/libc.so.6")
    assert not is_pool_path("[heap]")
    assert not is_pool_path("")


def test_parse_events_counts():
    events = parse_events("low 0\nhigh 5\noom 1\noom_kill 1\n")
    assert events == {"low": 0, "high": 5, "oom": 1, "oom_kill": 1}
    assert parse_events(None) == {}
    assert parse_events("") == {}


def test_classify_exitOOM_and_crash():
    assert classify_exit("agent_exit=0", 0, False) == "successful_completion"
    assert classify_exit("", -9, False) == "oom_kill_or_sigkill"
    assert classify_exit("", 137, False) == "oom_kill_or_sigkill"
    assert classify_exit("", -11, False) == "sigsegv_crash"
    assert classify_exit("", 0, True) == "timeout"
    assert classify_exit("provider 429 unavailable", 1,
                         False) == "provider_or_model_failure"


def test_memtier_tiers_format():
    tiers = memtier_tiers("/pool")
    assert "KIND:FS_DAX" in tiers
    assert "PATH:/pool" in tiers
    assert "KIND:DRAM" in tiers
    assert tiers.rstrip().endswith("POLICY:STATIC_RATIO")


def test_mode_env_baseline_and_smol_need_no_libs():
    env, avail = mode_env("baseline", {}, "/pool")
    assert avail["available"] and env == {}
    env, avail = mode_env("smol", {}, "/pool")
    assert avail["available"] and env == {"BUN_OPTIONS": "--smol"}


def test_mode_env_wrappers_require_libs():
    for mode, key in (("vmmalloc", "VMMALLOC_POOL_DIR"),
                      ("memtier", "MEMKIND_MEM_TIERS"),
                      ("shim", "DISKHEAP_DIR")):
        env, avail = mode_env(mode, {}, "/pool")
        assert not avail["available"]
        assert "reason" in avail and avail["reason"]
    lib = "/tmp/fake-lib.so"
    Path(lib).touch()
    try:
        env, avail = mode_env(
            "vmmalloc", {"vmmalloc": lib, "memtier": lib, "shim": lib},
            "/pool")
        assert avail["available"]
        assert env["LD_PRELOAD"] == lib
        assert env["VMMALLOC_POOL_DIR"] == "/pool"
        env, avail = mode_env(
            "memtier", {"vmmalloc": lib, "memtier": lib, "shim": lib},
            "/pool")
        assert avail["available"] and "FS_DAX" in env["MEMKIND_MEM_TIERS"]
        env, avail = mode_env(
            "shim", {"vmmalloc": lib, "memtier": lib, "shim": lib}, "/pool")
        assert avail["available"] and env["DISKHEAP_DIR"] == "/pool"
    finally:
        if os.path.exists(lib):
            os.unlink(lib)


def test_parser_modes_subset():
    args = build_parser().parse_args(["--modes", "baseline,shim"])
    assert args.modes == "baseline,shim"
    assert not args.include_network
    assert not args.docker


def test_render_report_handles_unavailable_and_error():
    result = {
        "pinned_opencode_version": "1.18.33",
        "opencode_bin": "/usr/local/bin/opencode",
        "run_id": "x",
        "base_commit": "abc",
        "host": {"shim": {"available": False, "reason": "no gcc"}},
        "docker": {},
        "verdicts": {"shim": "incompatible: gcc missing"},
    }
    report = render_report(result)
    assert "UNAVAILABLE" in report
    assert "incompatible" in report


def test_run_snap_trivial_command(tmp_path):
    out = str(tmp_path / "snap.json")
    snap = run_snap(str(tmp_path), out, 30.0, ["true"], {})
    assert snap["exit_code"] == 0
    assert snap["classification"] == "successful_completion"
    assert snap["peak_tree_kb"] >= 0
    assert os.path.isfile(out)


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_shim_builds_and_passes_probe(tmp_path):
    workdir = str(tmp_path)
    lib = build_shim(workdir)
    assert lib and os.path.isfile(lib)
    probe_c = tmp_path / "probe.c"
    probe = tmp_path / "probe"
    probe_c.write_text(C_PROBE, encoding="utf-8")
    build = subprocess.run(
        ["gcc", "-O2", "-Wall", "-Werror", "-o", str(probe), str(probe_c)],
        capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stderr
    pool = tmp_path / "pool"
    pool.mkdir()
    env = dict(os.environ)
    env["LD_PRELOAD"] = lib
    env["DISKHEAP_DIR"] = str(pool)
    env["DISKHEAP_SIZE_MB"] = "64"
    run = subprocess.run([str(probe)], capture_output=True, text=True,
                         timeout=60, env=env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PROBE-OK" in run.stdout


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_shim_build_is_warning_free():
    assert SHIM_SRC.is_file()
    proc = subprocess.run(
        ["gcc", "-O2", "-Wall", "-Werror", "-shared", "-fPIC",
         "-o", "/dev/null", str(SHIM_SRC), "-ldl", "-lpthread"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_render_report_handles_skipped_sections():
    result = {
        "pinned_opencode_version": "?",
        "opencode_bin": "",
        "run_id": "x",
        "base_commit": "abc",
        "host": {"status": "skipped", "reason": "rerun with --include-network"},
        "docker": {"status": "skipped", "reason": "docker not installed"},
        "verdicts": {},
    }
    report = render_report(result)
    assert "rerun with --include-network" in report
    assert "docker not installed" in report


def test_benchmark_offline_lists_unavailable_without_network(tmp_path):
    from disk_heap_benchmark import main
    out = str(tmp_path / "offline.json")
    rc = main(["--output", out, "--modes", "baseline"])
    assert rc == 0
    with open(out, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["issue"] == 56
    assert data["host"]["status"] == "skipped"
