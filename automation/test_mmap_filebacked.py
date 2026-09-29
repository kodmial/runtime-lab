"""Offline tests for the issue-#144 mmap file-backing probe.

Stdlib only, no network, no Docker, no git mutations. The compiled-shim
tests run the real C interposer against the real C eligibility probe, so
interception eligibility and fallback are proven against the shipped
``shim.c`` rather than a Python mirror. Tests requiring gcc are skipped
fail-open when no compiler is present (offline lint environments), but
they run on the Actions worker where the benchmark itself runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mmap_filebacked as mfb

GCC = shutil.which("gcc")


def run_probe(so_path, test_path, extra_env):
    env = dict(os.environ)
    env["LD_PRELOAD"] = so_path
    env.update(extra_env)
    return subprocess.run([test_path, mfb.BACKING_TOKEN],
                          capture_output=True, text=True, timeout=60, env=env)


class ShimSourceTest(unittest.TestCase):
    def test_sources_present(self):
        self.assertTrue(os.path.isfile(
            os.path.join(mfb.shim_dir(), mfb.SHIM_SOURCE)))
        self.assertTrue(os.path.isfile(
            os.path.join(mfb.shim_dir(), mfb.SHIM_TEST_SOURCE)))


@unittest.skipUnless(GCC, "gcc not available")
class ShimBuildTest(unittest.TestCase):
    def test_build_wall_werror(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-build-") as tmp:
            built = mfb.build_shim(tmp)
            self.assertTrue(os.path.isfile(built["so"]))
            self.assertTrue(os.path.isfile(built["test"]))


@unittest.skipUnless(GCC, "gcc not available")
class ShimProbePrivateTest(unittest.TestCase):
    """Private mode: the smallest safe design must pass every probe."""

    def test_private_mode_passes_and_redirects(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-priv-") as tmp:
            built = mfb.build_shim(tmp)
            fbdir = os.path.join(tmp, "fb")
            os.makedirs(fbdir)
            stats = os.path.join(tmp, "stats.json")
            proc = run_probe(built["so"], built["test"],
                             {"OPENCODE_FILEBACKED_DIR": fbdir,
                              "OPENCODE_FILEBACKED_MIN_BYTES": "1048576",
                              "OPENCODE_FILEBACKED_SIZE_MB": "64",
                              "OPENCODE_FILEBACKED_SHARED": "0",
                              "OPENCODE_FILEBACKED_STATS": stats})
            self.assertEqual(proc.returncode, 0,
                             msg="probe output:\n%s\n%s"
                             % (proc.stdout, proc.stderr))
            self.assertIn("RESULT PASS", proc.stdout)
            self.assertIn("parent-bytes-unchanged-after-fork", proc.stdout)
            parsed = mfb.parse_shim_stats(stats)
            self.assertEqual(parsed["mode"], "private")
            # The 2 MiB RW mapping must be redirected (material coverage).
            self.assertGreaterEqual(parsed["redirected_bytes"], 2 * 1024 * 1024)
            self.assertGreaterEqual(parsed["intercepted"], 1)
            reasons = parsed["bypass_by_reason"]
            for reason in ("too_small", "no_write", "executable", "fixed",
                           "not_anonymous", "shared_anon"):
                self.assertIn(reason, reasons, msg=reason)
                self.assertGreaterEqual(reasons[reason], 1, msg=reason)
            self.assertGreaterEqual(parsed["backing_bytes_allocated"],
                                    2 * 1024 * 1024)
            backing = [f for f in os.listdir(fbdir)
                       if f.startswith("mmapfb.pool.")]
            self.assertEqual(len(backing), 1)

    def test_disabled_is_silent_passthrough(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-off-") as tmp:
            built = mfb.build_shim(tmp)
            stats = os.path.join(tmp, "stats.json")
            env = dict(os.environ)
            env["LD_PRELOAD"] = built["so"]
            env.pop("OPENCODE_FILEBACKED_DIR", None)
            env["OPENCODE_FILEBACKED_STATS"] = stats
            proc = subprocess.run(
                [built["test"], mfb.BACKING_TOKEN],
                capture_output=True, text=True, timeout=60, env=env)
            # Without the backing dir every mapping falls back to anonymous:
            # the ONLY failing check must be the positive-redirection one,
            # and no stats file may be created (silent passthrough).
            self.assertNotEqual(proc.returncode, 0)
            fails = [line for line in proc.stdout.splitlines()
                     if line.startswith("FAIL")]
            self.assertEqual(len(fails), 1, msg=proc.stdout)
            self.assertIn("big-anon-rw-file-backed", fails[0])
            self.assertIn("parent-bytes-unchanged-after-fork", proc.stdout)
            self.assertFalse(os.path.exists(stats))


@unittest.skipUnless(GCC, "gcc not available")
class ShimProbeSharedHazardTest(unittest.TestCase):
    """Shared mode must demonstrably break fork CoW (documented hazard)."""

    def test_shared_mode_breaks_fork_cow(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-shared-") as tmp:
            built = mfb.build_shim(tmp)
            fbdir = os.path.join(tmp, "fb")
            os.makedirs(fbdir)
            stats = os.path.join(tmp, "stats.json")
            proc = run_probe(built["so"], built["test"],
                             {"OPENCODE_FILEBACKED_DIR": fbdir,
                              "OPENCODE_FILEBACKED_MIN_BYTES": "1048576",
                              "OPENCODE_FILEBACKED_SIZE_MB": "64",
                              "OPENCODE_FILEBACKED_SHARED": "1",
                              "OPENCODE_FILEBACKED_STATS": stats})
            self.assertNotEqual(proc.returncode, 0,
                                msg="shared mode must fail the CoW check")
            self.assertIn("FAIL parent-bytes-unchanged-after-fork",
                          proc.stdout)
            parsed = mfb.parse_shim_stats(stats)
            self.assertEqual(parsed["mode"], "shared")


class ShimEnvTest(unittest.TestCase):
    def test_defaults(self):
        env = mfb.shim_env("/fb")
        self.assertEqual(env["OPENCODE_FILEBACKED_DIR"], "/fb")
        self.assertEqual(env["OPENCODE_FILEBACKED_MIN_BYTES"], "1048576")
        self.assertEqual(env["OPENCODE_FILEBACKED_SHARED"], "0")

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            mfb.shim_env("")
        with self.assertRaises(ValueError):
            mfb.shim_env("/fb", min_bytes=512)
        with self.assertRaises(ValueError):
            mfb.shim_env("/fb", pool_mb=8)
        with self.assertRaises(ValueError):
            mfb.shim_env("/fb", pool_mb=99999)


class StatsParseTest(unittest.TestCase):
    def _good(self):
        stats = {f: 0 for f in mfb.REQUIRED_STATS_FIELDS}
        stats.update({"schema": mfb.STATS_SCHEMA, "pid": 1, "mode": "private",
                      "min_bytes": 1048576, "pool_size": 1,
                      "backing_file": "/fb/x",
                      "bypass_by_reason": {"too_small": 1}})
        return stats

    def test_good_stats(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-stats-") as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._good(), handle)
            self.assertEqual(mfb.parse_shim_stats(path)["mode"], "private")

    def test_bad_stats(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-stats-") as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"schema": "nope"}')
            with self.assertRaises(ValueError):
                mfb.parse_shim_stats(path)
            with self.assertRaises(FileNotFoundError):
                mfb.parse_shim_stats(os.path.join(tmp, "missing.json"))


class DockerCommandTest(unittest.TestCase):
    def test_shim_off_has_no_preload(self):
        cmd = mfb.docker_trial_command("/b/opencode", "", "/b/repo", "/b/wd",
                                       shim=False)
        text = " ".join(cmd)
        self.assertNotIn("LD_PRELOAD", text)
        self.assertIn("536870912", text)
        self.assertIn("--memory-swap=512m", text)
        self.assertIn(mfb.DOCKER_IMAGE, text)

    def test_shim_on_mounts_and_verifies(self):
        cmd = mfb.docker_trial_command("/b/opencode", "/b/libmmapfb.so",
                                       "/b/repo", "/b/wd", shim=True)
        text = " ".join(cmd)
        self.assertIn("LD_PRELOAD", text)
        self.assertIn("OPENCODE_FILEBACKED_DIR", text)
        self.assertIn(mfb.BACKING_TOKEN, text)
        self.assertIn("/opt/shim/%s:ro" % mfb.SHIM_SONAME, text)

    def test_shared_requires_shim(self):
        with self.assertRaises(ValueError):
            mfb.docker_trial_command("/b/opencode", "", "/b/repo", "/b/wd",
                                     shim=False, shared=True)

    def test_parse_telemetry(self):
        sample = ("\n".join([
            "cgroup_max=536870912", "cgroup_swap_max=0",
            "agent_exit=0", "wall_seconds=57",
            "backed_hits=12 backed_samples=20",
            "peak=537804800", "current=62066688",
            "swap_current=0", "swap_max=0",
            "high 0", "max 4272", "oom 0", "oom_kill 0",
            "backing_rss_kb=123456 backing_pss_kb=120000",
            '{"schema": "%s", "mode": "private"}' % mfb.STATS_SCHEMA,
            "/fb/mmapfb.pool.7 rw-s 123",
            "7fab12600000-7fb12600000 rw-p 00000000 00:00 0 /fb/mmapfb.pool.7",
        ]))
        parsed = mfb.parse_docker_trial_output(sample)
        self.assertEqual(parsed["agent_exit"], 0)
        self.assertEqual(parsed["memory_peak_bytes"], 537804800)
        self.assertEqual(parsed["wall_seconds"], 57)
        self.assertEqual(parsed["backed_hits"], 12)
        self.assertEqual(parsed["backing_rss_kb"], 123456)
        # Only genuine /proc maps lines (address-range shape) count as
        # maps evidence; stats-JSON echoes mentioning the pool path do not.
        self.assertEqual(len(parsed["backing_maps_lines"]), 1)
        self.assertTrue(
            parsed["backing_maps_lines"][0].startswith("7fab12600000"))


class TrialRecordTest(unittest.TestCase):
    def _base(self, shim="off"):
        trial = {f: 0 for f in
                 ("peak_tree_kb", "peak_bytes", "wall_seconds", "exit_code")}
        trial.update({
            "trial": "t1", "bun_options": "", "binary_sha256": "0" * 64,
            "binary_bytes": 1, "memory_current_bytes": 1,
            "memory_peak_bytes": 1, "memory_events": {"oom_kill": 0, "max": 0},
            "swap_current_bytes": 0, "swap_max_bytes": 0,
            "correctness": "pass", "shim": shim, "smaps": {},
            "shim_stats": None, "exit_code": 0, "wall_seconds": 1,
            "peak_bytes": 100, "peak_tree_kb": 100})
        return trial

    def test_off_ok(self):
        mfb.validate_trial_record(self._base("off"))

    def test_on_requires_stats(self):
        trial = self._base("on")
        with self.assertRaises(ValueError):
            mfb.validate_trial_record(trial)
        trial["shim_stats"] = {f: 0 for f in mfb.REQUIRED_STATS_FIELDS}
        trial["shim_stats"].update(
            {"schema": mfb.STATS_SCHEMA, "mode": "private", "pid": 1,
             "min_bytes": 1, "pool_size": 1, "backing_file": "/fb/x",
             "bypass_by_reason": {}})
        mfb.validate_trial_record(trial)

    def test_compare_arms_headroom(self):
        off = [self._base("off"), self._base("off")]
        on = [self._base("on"), self._base("on")]
        for t in off + on:
            t["peak_bytes"] = 500 * 1024 * 1024
            t["memory_events"] = {"oom_kill": 0, "max": 5}
            if t["shim"] == "on":
                t["shim_stats"] = {f: 0 for f in mfb.REQUIRED_STATS_FIELDS}
                t["shim_stats"].update(
                    {"schema": mfb.STATS_SCHEMA, "mode": "private", "pid": 1,
                     "min_bytes": 1, "pool_size": 1, "backing_file": "x",
                     "bypass_by_reason": {}})
        cmpd = mfb.compare_arms(off, on)
        self.assertFalse(cmpd["headroom_target_met"])
        on[0]["peak_bytes"] = 400 * 1024 * 1024
        on[1]["peak_bytes"] = 400 * 1024 * 1024
        cmpd = mfb.compare_arms(off, on)
        self.assertTrue(cmpd["headroom_target_met"])
        self.assertTrue(cmpd["no_oom_kill"])


class SourceIdentityTest(unittest.TestCase):
    def test_exact_identity_accepted(self):
        ident = mfb.validate_source_identity()
        self.assertEqual(ident["source_sha"], mfb.SOURCE_SHA)

    def test_wrong_sha_rejected(self):
        with self.assertRaises(ValueError):
            mfb.validate_source_identity(
                source_sha="0" * 40, merge_sha=mfb.MERGE_SHA,
                branch=mfb.FORK_BRANCH, pr=mfb.FORK_PR)

    def test_binary_digest_gate(self):
        self.assertEqual(mfb.verify_built_binary_sha256(mfb.BINARY_SHA256),
                         mfb.BINARY_SHA256)
        with self.assertRaises(ValueError):
            mfb.verify_built_binary_sha256("0" * 64)


class StatsAggregateTest(unittest.TestCase):
    def _one(self, pid, redirected, mode="private"):
        stats = {f: 0 for f in mfb.REQUIRED_STATS_FIELDS}
        stats.update({"schema": mfb.STATS_SCHEMA, "pid": pid, "mode": mode,
                      "min_bytes": 1048576, "pool_size": 1,
                      "backing_file": "/fb/mmapfb.pool.%d" % pid,
                      "bypass_by_reason": {"too_small": 1},
                      "redirected_bytes": redirected,
                      "redirected_count": 1 if redirected else 0,
                      "intercepted": 2})
        return stats

    def test_aggregate_sums(self):
        total = mfb.aggregate_shim_stats([self._one(7, 100), self._one(9, 200)])
        self.assertEqual(total["processes"], 2)
        self.assertEqual(total["redirected_bytes"], 300)
        self.assertEqual(total["intercepted"], 4)
        self.assertEqual(total["bypass_by_reason"], {"too_small": 2})
        self.assertEqual(total["pids"], [7, 9])

    def test_aggregate_rejects_empty_and_mixed(self):
        with self.assertRaises(ValueError):
            mfb.aggregate_shim_stats([])
        with self.assertRaises(ValueError):
            mfb.aggregate_shim_stats(
                [self._one(7, 0), self._one(9, 0, mode="shared")])

    def test_load_dir(self):
        with tempfile.TemporaryDirectory(prefix="mmapfb-loaddir-") as tmp:
            for pid in (7, 9):
                with open(os.path.join(tmp, "mmapfb-stats-%d.json" % pid),
                          "w", encoding="utf-8") as handle:
                    json.dump(self._one(pid, 50), handle)
            with open(os.path.join(tmp, "agent.log"), "w") as handle:
                handle.write("noise")
            total = mfb.load_shim_stats_dir(tmp)
            self.assertEqual(total["processes"], 2)
            self.assertEqual(total["redirected_bytes"], 100)
            with self.assertRaises(ValueError):
                mfb.load_shim_stats_dir(os.path.join(tmp, "nope"))


if __name__ == "__main__":
    unittest.main()
