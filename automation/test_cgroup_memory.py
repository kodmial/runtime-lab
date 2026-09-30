"""Tests for container-level cgroup telemetry and the external sampler (issue #57).

Covers the kodmai-derived semantics without touching the network:
- cgroup v2 normal case with a 512 MB limit;
- v2 root fallback;
- v1 fallback;
- unlimited markers;
- missing/unreadable files degrade only affected fields;
- memory.events parsing;
- memory.peak unavailable;
- swap unavailable;
- health endpoint includes the container metrics alongside process RSS/HWM;
- external sampler survives an instance-id change and retains pre-restart
  samples;
- sampler cleanup is bounded;
- the temporary harness starts/stops/summarizes the external sampler on
  the Actions side without secrets.
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = REPO_ROOT / "automation"
sys.path.insert(0, str(AUTOMATION))
sys.path.insert(0, str(REPO_ROOT))

from cgroup_memory import (  # noqa: E402
    parse_memory_events,
    read_cgroup_snapshot,
    resolve_cgroup_relative_path,
    summarize_cgroup_snapshot,
)
from render_memory_sampler import (  # noqa: E402
    MAX_INTERVAL_SECONDS,
    MEMORY_PRESSURE_MIN_RESTARTS,
    MEMORY_PRESSURE_STALL_SURGE_DELTA,
    MEMORY_PRESSURE_USAGE_RATIO,
    detect_memory_pressure,
    detect_memory_pressure_file,
    error_sample,
    event_marker,
    extract_sample,
    memory_pressure_evidence,
    pressure_decision_branch,
    read_samples,
    render_human_summary,
    sample_loop,
    summarize_samples,
)

LIMIT_512M = 512 * 1024 * 1024


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _v2_tree(root: Path, rel: str = "kubepods/pod-abc") -> tuple[Path, Path]:
    """Fake /proc/self/cgroup file + cgroup fs root for a v2 container."""
    proc_cgroup = root / "proc-self-cgroup"
    proc_cgroup.write_text("0::/%s\n" % rel, encoding="utf-8")
    fs_root = root / "cgroup"
    _write(fs_root / rel / "memory.max", str(LIMIT_512M))
    _write(fs_root / rel / "memory.current", "123456789")
    _write(fs_root / rel / "memory.peak", "234567890")
    _write(fs_root / rel / "memory.swap.current", "1048576")
    _write(fs_root / rel / "memory.swap.max", "0")
    _write(
        fs_root / rel / "memory.events",
        "low 0\nhigh 7\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n",
    )
    _write(fs_root / rel / "cpu.max", "10000 100000")
    _write(fs_root / rel / "cpu.stat", "usage_usec 12345\nuser_usec 10000\nsystem_usec 2345\n")
    return proc_cgroup, fs_root


def test_v2_normal_case_with_512m_limit(tmp_path):
    proc_cgroup, fs_root = _v2_tree(tmp_path)
    snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert snapshot["cgroup_version"] == "v2"
    assert snapshot["memory_limit_bytes"] == LIMIT_512M
    assert snapshot["memory_current_bytes"] == 123456789
    assert snapshot["memory_current_mb"] == pytest.approx(123456789 / 1048576, rel=1e-3)
    assert snapshot["memory_usage_percent"] == pytest.approx(
        123456789 / LIMIT_512M * 100, rel=1e-3
    )
    assert snapshot["memory_peak_bytes"] == 234567890
    assert snapshot["swap_current_bytes"] == 1048576
    assert snapshot["swap_max_bytes"] == 0
    assert snapshot["memory_events"]["high"] == 7
    assert snapshot["memory_events"]["oom_kill"] == 1
    assert snapshot["memory_events"]["oom_group_kill"] == 0
    assert "10000 100000" in snapshot["cpu_max"]
    assert snapshot["cpu_stat"]["usage_usec"] == 12345
    assert "cgroup v2" in summarize_cgroup_snapshot(snapshot)


def test_v2_root_fallback(tmp_path):
    proc_cgroup = tmp_path / "cgroup-text"
    proc_cgroup.write_text("0::/\n", encoding="utf-8")
    fs_root = tmp_path / "cgroup"
    _write(fs_root / "memory.max", str(LIMIT_512M))
    _write(fs_root / "memory.current", "1000")
    snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert snapshot["cgroup_version"] == "v2"
    assert snapshot["memory_limit_bytes"] == LIMIT_512M
    assert snapshot["memory_current_bytes"] == 1000


def test_v1_fallback(tmp_path):
    proc_cgroup = tmp_path / "cgroup-text"
    proc_cgroup.write_text("2:memory:/docker/abc123\n", encoding="utf-8")
    fs_root = tmp_path / "cgroup"
    rel = "docker/abc123"
    _write(fs_root / rel / "memory.limit_in_bytes", str(LIMIT_512M))
    _write(fs_root / rel / "memory.usage_in_bytes", "99999999")
    _write(fs_root / rel / "memory.max_usage_in_bytes", "111111111")
    _write(fs_root / rel / "memory.memsw.limit_in_bytes", "1073741824")
    _write(fs_root / rel / "memory.memsw.usage_in_bytes", "2222222")
    _write(fs_root / rel / "memory.oom_control", "oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
    snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert snapshot["cgroup_version"] == "v1"
    assert snapshot["memory_limit_bytes"] == LIMIT_512M
    assert snapshot["memory_current_bytes"] == 99999999
    assert snapshot["memory_peak_bytes"] == 111111111
    assert snapshot["swap_current_bytes"] == 2222222
    assert snapshot["swap_max_bytes"] == 1073741824
    assert snapshot["memory_events"]["oom_kill"] == 2


def test_unlimited_markers_become_none(tmp_path):
    proc_cgroup = tmp_path / "cgroup-text"
    proc_cgroup.write_text("0::/\n", encoding="utf-8")
    fs_root = tmp_path / "cgroup"
    _write(fs_root / "memory.max", "max")
    _write(fs_root / "memory.current", "1000")
    _write(fs_root / "memory.swap.max", "max")
    snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert snapshot["memory_limit_bytes"] is None
    assert snapshot["memory_usage_percent"] is None
    assert snapshot["memory_current_bytes"] == 1000
    assert snapshot["swap_max_bytes"] is None
    # v1 near-2^63 marker is also unlimited, not a real limit.
    proc_v1 = tmp_path / "cgroup-v1"
    proc_v1.write_text("2:memory:/\n", encoding="utf-8")
    fs_v1 = tmp_path / "cgroup-v1-fs"
    _write(fs_v1 / "memory.limit_in_bytes", "9223372036854771712")
    _write(fs_v1 / "memory.usage_in_bytes", "1000")
    snapshot_v1 = read_cgroup_snapshot(str(proc_v1), str(fs_v1))
    assert snapshot_v1["cgroup_version"] == "v1"
    assert snapshot_v1["memory_limit_bytes"] is None


def test_missing_files_degrade_only_affected_fields(tmp_path):
    proc_cgroup = tmp_path / "cgroup-text"
    proc_cgroup.write_text("0::/partial\n", encoding="utf-8")
    fs_root = tmp_path / "cgroup"
    _write(fs_root / "partial" / "memory.current", "42000")
    snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert snapshot["cgroup_version"] == "v2"
    assert snapshot["memory_current_bytes"] == 42000
    assert snapshot["memory_limit_bytes"] is None
    assert snapshot["memory_peak_bytes"] is None
    assert snapshot["swap_current_bytes"] is None
    assert snapshot["memory_events"] == {}
    # Nothing at all: unknown version, no raise.
    empty = read_cgroup_snapshot(
        str(tmp_path / "missing-proc"), str(tmp_path / "missing-fs")
    )
    assert empty["cgroup_version"] == "unknown"
    assert empty["memory_current_bytes"] is None


def test_unreadable_file_degrades_only_its_field(tmp_path):
    proc_cgroup, fs_root = _v2_tree(tmp_path)
    peak = fs_root / "kubepods/pod-abc" / "memory.peak"
    peak.chmod(0)
    try:
        snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    finally:
        peak.chmod(stat.S_IRUSR | stat.S_IWUSR)
    # Root can still read mode-0 files; assert the degradation contract
    # structurally instead: removing the file degrades only its field.
    os.unlink(peak)
    degraded = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert degraded["memory_peak_bytes"] is None
    assert degraded["memory_current_bytes"] == 123456789
    assert degraded["memory_limit_bytes"] == LIMIT_512M


def test_memory_events_parsing():
    events = parse_memory_events("low 0\nhigh 12\nmax 4\noom 2\noom_kill 2\nbogus\n")
    assert events == {"low": 0, "high": 12, "max": 4, "oom": 2, "oom_kill": 2}
    assert parse_memory_events(None) == {}
    assert parse_memory_events("") == {}


def test_peak_and_swap_unavailable(tmp_path):
    proc_cgroup = tmp_path / "cgroup-text"
    proc_cgroup.write_text("0::/bare\n", encoding="utf-8")
    fs_root = tmp_path / "cgroup"
    _write(fs_root / "bare" / "memory.max", str(LIMIT_512M))
    _write(fs_root / "bare" / "memory.current", "5000")
    snapshot = read_cgroup_snapshot(str(proc_cgroup), str(fs_root))
    assert snapshot["memory_peak_bytes"] is None
    assert snapshot["swap_current_bytes"] is None
    assert snapshot["swap_max_bytes"] is None
    assert snapshot["memory_current_bytes"] == 5000


def test_cgroup_path_resolution_prefers_v2_then_v1_then_root():
    assert resolve_cgroup_relative_path("0::/kubepods/x\n") == "/kubepods/x"
    assert resolve_cgroup_relative_path("2:memory:/docker/y\n") == "/docker/y"
    assert resolve_cgroup_relative_path("") == "/"
    assert resolve_cgroup_relative_path(None) == "/"


# ---------------------------------------------------------------------------
# Health-endpoint exposure.
# ---------------------------------------------------------------------------


def test_health_includes_container_metrics_beside_process_rss(tmp_path):
    from runner_server import JobManager, read_resource_diagnostics

    diagnostics = read_resource_diagnostics()
    assert "cgroup" in diagnostics
    cgroup = diagnostics["cgroup"]
    assert cgroup.get("cgroup_version") in ("v2", "v1", "unknown")
    assert "memory_current_bytes" in cgroup
    assert "memory_limit_bytes" in cgroup
    assert "memory_events" in cgroup
    # Process-level signals remain available separately.
    assert "rss_kb" in diagnostics or "peak_rss_kb" in diagnostics
    manager = JobManager(workspace_root=str(tmp_path / "ws"), job_timeout_seconds=10.0)
    health = manager.health_snapshot()
    assert health["resources"]["cgroup"]["cgroup_version"] in ("v2", "v1", "unknown")
    assert "memory_current_bytes" in health["resources"]["cgroup"]


def test_resource_diagnostics_never_raise_when_cgroup_broken(monkeypatch):
    import runner_server

    monkeypatch.setattr(
        runner_server, "read_cgroup_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("denied"))
    )
    diagnostics = runner_server.read_resource_diagnostics()
    assert diagnostics["cgroup"]["cgroup_version"] == "unknown"


# ---------------------------------------------------------------------------
# External sampler behavior.
# ---------------------------------------------------------------------------


def _health(instance_id, current, peak, rss=22000, hwm=30000, events=None):
    return {
        "instance_id": instance_id,
        "resources": {
            "rss_kb": rss,
            "peak_rss_kb": hwm,
            "cgroup": {
                "cgroup_version": "v2",
                "memory_limit_bytes": LIMIT_512M,
                "memory_current_bytes": current,
                "memory_peak_bytes": peak,
                "swap_current_bytes": 0,
                "swap_max_bytes": 0,
                "memory_events": dict(events or {}),
            },
        },
    }


def test_extract_sample_covers_required_fields():
    sample = extract_sample(
        _health("instance-a", 100000000, 200000000,
                events={"high": 1, "max": 0, "oom_kill": 0}),
        timestamp=1234.5,
    )
    assert sample["timestamp"] == 1234.5
    assert sample["instance_id"] == "instance-a"
    assert sample["memory_current_bytes"] == 100000000
    assert sample["memory_peak_bytes"] == 200000000
    assert sample["rss_kb"] == 22000
    assert sample["peak_rss_kb"] == 30000
    assert sample["memory_events"]["high"] == 1


def test_sampler_survives_instance_change_and_retains_pre_restart_samples(tmp_path):
    out = str(tmp_path / "samples.jsonl")
    feeds = [
        _health("instance-first", 100000000, 150000000,
                events={"high": 0, "max": 0, "oom_kill": 0}),
        RuntimeError("connection refused (worker restarting)"),
        _health("instance-second", 300000000, 350000000,
                events={"high": 5, "max": 1, "oom_kill": 0}),
        _health("instance-second", 310000000, 360000000,
                events={"high": 6, "max": 1, "oom_kill": 0}),
    ]
    state = {"i": 0}

    def fetcher():
        item = feeds[state["i"]]
        state["i"] = min(state["i"] + 1, len(feeds) - 1)
        if isinstance(item, Exception):
            raise item
        return item

    clock = {"t": 1000.0}

    def fake_clock():
        return clock["t"]

    def fake_sleep(seconds):
        clock["t"] += seconds

    count = sample_loop(
        "http://fake-runner.local",
        out,
        interval_seconds=1.0,
        max_seconds=4.0,
        fetcher=fetcher,
        clock=fake_clock,
        sleeper=fake_sleep,
    )
    assert count == 4
    samples = read_samples(out)
    assert len(samples) == 4
    # Pre-restart samples are retained, the gap is a visible failed poll.
    assert samples[0]["ok"] is True
    assert samples[0]["instance_id"] == "instance-first"
    assert samples[1]["ok"] is False
    assert samples[2]["instance_id"] == "instance-second"
    summary = summarize_samples(samples, interval_seconds=1.0)
    assert summary["samples"] == 4
    assert summary["ok_samples"] == 3
    assert summary["failed_polls"] == 1
    assert summary["memory_limit_bytes"] == LIMIT_512M
    assert summary["max_memory_current_bytes"] == 310000000
    assert summary["max_memory_peak_bytes"] == 360000000
    assert summary["max_rss_kb"] == 22000
    assert summary["instance_changed"] is True
    assert summary["instance_ids"] == ["instance-first", "instance-second"]
    assert len(summary["restart_transitions"]) == 1
    transition = summary["restart_transitions"][0]
    assert transition["from_instance_id"] == "instance-first"
    assert transition["to_instance_id"] == "instance-second"
    assert transition["timestamp"] is not None
    assert summary["event_counter_changes"]["high"]["delta"] == 6
    human = render_human_summary(summary)
    assert "295.6 MB" in human  # 310000000 bytes rendered as MB
    assert "instance-fir" in human
    assert "changed=True" in human


def test_sampler_cleanup_is_bounded(tmp_path):
    out = str(tmp_path / "samples.jsonl")
    stop = tmp_path / "stop"
    stop.write_text("stop\n", encoding="utf-8")
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return _health("instance-a", 1, 1)

    started = time.time()
    count = sample_loop(
        "http://fake-runner.local", out, stop_path=str(stop), fetcher=fetcher
    )
    assert count == 0
    assert calls["n"] == 0
    assert time.time() - started < 10
    # Zero/negative budgets also stop immediately.
    assert sample_loop("http://x", out, max_seconds=0, fetcher=fetcher) == 0
    # Intervals above 2s are clamped to the 2s maximum.
    assert MAX_INTERVAL_SECONDS == 2.0
    # Error samples keep restart gaps visible.
    gap = error_sample(999.0, "boom")
    assert gap["ok"] is False and gap["timestamp"] == 999.0
    event_marker(out, "job_resubmitted", "lost=a resubmitted=b")
    entries = read_samples(out)
    assert entries and entries[-1]["type"] == "event"
    summary = summarize_samples(entries)
    assert summary["harness_events"] and summary["samples"] == 0


def test_summarize_empty_samples():
    summary = summarize_samples([])
    assert summary["samples"] == 0
    assert summary["instance_changed"] is False
    assert summary["max_memory_current_bytes"] is None
    assert "Container memory summary" in render_human_summary(summary)


def _pressure_sample(index, instance, current=LIMIT_512M, stalls=0):
    return {
        "type": "sample",
        "timestamp": 1790607000.0 + index,
        "ok": True,
        "instance_id": instance,
        "memory_limit_bytes": LIMIT_512M,
        "memory_current_bytes": current,
        "memory_events": {"high": 0, "max": stalls},
    }


def test_memory_pressure_detector_requires_pinning_plus_replacement():
    # Mirrors run 36439192645: usage pinned at the 512 MB limit across
    # eight instance ids with a +430,890 max-stall delta. Pinning plus
    # repeated replacements is pressure.
    samples = []
    for group, instance in enumerate(("da612faaaa53", "f96d4c16e369", "4c1448fcadb6")):
        for i in range(10):
            samples.append(_pressure_sample(
                group * 10 + i, instance, stalls=group * 200000 + i * 1000))
    assert detect_memory_pressure(samples) is True
    evidence = memory_pressure_evidence(samples)
    assert evidence["pinned_at_limit"] is True
    assert evidence["restart_transitions"] == 2
    # Pinning alone (one healthy instance brushing the limit) is not
    # pressure: a single hot run must not trip the storm breaker.
    assert detect_memory_pressure(samples[:10]) is False
    # Replacements alone at low usage are not pressure either (genuine
    # transient host maintenance keeps budget-limited recovery).
    cool = [_pressure_sample(i, "inst-%d" % (i // 5), current=LIMIT_512M // 2)
            for i in range(15)]
    assert detect_memory_pressure(cool) is False
    # Gaps, event markers, and samples without cgroup fields never
    # report pressure (callers fail open toward resubmission).
    assert detect_memory_pressure([]) is False
    assert detect_memory_pressure([error_sample(1.0, "boom")]) is False
    assert detect_memory_pressure(
        [{"type": "event", "timestamp": 1.0, "name": "x"}]) is False
    # A lone pinned sample proves nothing (no replacement, no surge).
    assert detect_memory_pressure(
        [_pressure_sample(0, "a", current=LIMIT_512M)]) is False
    no_fields = [dict(_pressure_sample(0, "a"))]
    for sample in no_fields:
        del sample["memory_current_bytes"]
    assert detect_memory_pressure(no_fields) is False


def test_memory_pressure_stall_surge_counts_within_instances_only():
    # Per-container counters reset on every replacement (run
    # 36434278632 showed first/last delta 0 across six replacements),
    # so a global first-to-last delta must never drive the surge: the
    # surge is measured within same-instance groups.
    reset = []
    for group, instance in enumerate(("a", "b", "c")):
        # Each replacement resets max stalls to ~0; global first->last
        # delta is 0, but every group surges 50,000 within itself.
        for i in range(5):
            reset.append(_pressure_sample(
                group * 5 + i, instance, stalls=i * 12500))
    evidence = memory_pressure_evidence(reset)
    assert evidence["restart_transitions"] == 2
    assert evidence["max_stall_surge_delta"] == 50000
    assert evidence["stall_surge"] is True
    assert detect_memory_pressure(reset) is True
    # Same global shape without any within-group surge is not a surge
    # (flat counters); restarts still carry it via the replacement
    # signal, but the surge flag itself must stay False.
    flat = []
    for group, instance in enumerate(("a", "b")):
        for i in range(5):
            flat.append(_pressure_sample(group * 5 + i, instance, stalls=7))
    flat_evidence = memory_pressure_evidence(flat)
    assert flat_evidence["max_stall_surge_delta"] == 0
    assert flat_evidence["stall_surge"] is False


def test_memory_pressure_counts_transitions_across_gaps_and_events():
    # Regression for run 36442675039: every live restart is separated
    # by sampler gap samples (ok=false during the restart down-window)
    # plus a job_resubmitted harness event marker, so a detector that
    # resets continuity on gaps reports zero transitions live while the
    # end-of-run summary (which preserves previous across gaps) counts
    # ten. Transitions and surge groups must survive gaps/events.
    samples = []
    index = 0
    for group, instance in enumerate(("live-a", "live-b", "live-c")):
        for i in range(5):
            samples.append(_pressure_sample(
                index, instance, stalls=group * 20000 + i * 1000))
            index += 1
        samples.append(error_sample(float(index), "health poll failed: 502"))
        index += 1
        samples.append({"type": "event", "timestamp": float(index),
                        "name": "job_resubmitted", "detail": "lost=x"})
        index += 1
    evidence = memory_pressure_evidence(samples)
    assert evidence["pinned_at_limit"] is True
    assert evidence["restart_transitions"] == 2
    assert detect_memory_pressure(samples) is True
    # Same interleaved shape through the file helper (live path).
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "live.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for entry in samples:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        assert detect_memory_pressure_file(path) is True


def test_memory_pressure_surge_survives_gaps_within_instance():
    # A gap inside one instance lifetime must not split its stall
    # surge: the group stays open across ok=false samples.
    samples = [
        _pressure_sample(0, "same-inst", stalls=0),
        _pressure_sample(1, "same-inst", stalls=5000),
        error_sample(2.0, "health poll failed: 502"),
        _pressure_sample(3, "same-inst", stalls=60000),
    ]
    evidence = memory_pressure_evidence(samples)
    assert evidence["restart_transitions"] == 0
    assert evidence["max_stall_surge_delta"] == 60000
    assert evidence["stall_surge"] is True


def test_memory_pressure_file_helper_fails_open(tmp_path):
    assert detect_memory_pressure_file(str(tmp_path / "missing.jsonl")) is False
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{broken\n", encoding="utf-8")
    assert detect_memory_pressure_file(str(bad)) is False
    good = tmp_path / "pressure.jsonl"
    lines = []
    for group, instance in enumerate(("i-1", "i-2", "i-3")):
        for i in range(4):
            sample = _pressure_sample(group * 4 + i, instance)
            lines.append(json.dumps(sample, sort_keys=True))
    good.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert detect_memory_pressure_file(str(good)) is True


def _run_36493316814_samples():
    """Live telemetry shape of failed run 36493316814 (issue #113).

    Five worker instances in ~13 minutes (f6960b19 -> 9df9d90b ->
    95503138 -> 76db2200 -> 7338f38f at ~3-minute intervals), cgroup
    usage pinned at the 512 MB limit (peak current 536854528 of
    536870912), Python RSS ~33 MB, swap 0, memory.events counters
    nearly quiet (within-group max-stall rise 448 against the 10000
    surge threshold; global first/last deltas 0), every replacement
    separated by sampler gap samples plus a job_resubmitted event
    marker. The storm breaker abandoned at the fourth proven loss via
    the replacements branch -- this shape must keep reporting
    pressure (pinned plus repeated replacements) even though no
    stall surge exists.
    """
    samples = []
    index = 0
    stall = 0
    instances = ("f6960b19f65f", "9df9d90b9c1e", "95503138bc6c",
                 "76db2200c588", "7338f38fa851")
    # Within-group max-stall rises per instance lifetime; the largest
    # single-group rise is 448, far below the 10000 surge threshold.
    rises = (0, 0, 448, 0, 0)
    for group, instance in enumerate(instances):
        for i in range(8):
            if i == 6:
                stall += rises[group]
            samples.append({
                "type": "sample",
                "timestamp": 1790635000.0 + index,
                "ok": True,
                "instance_id": instance,
                "memory_limit_bytes": LIMIT_512M,
                "memory_current_bytes": LIMIT_512M - 16384,
                "memory_events": {"high": 0, "max": stall, "oom": 0,
                                  "oom_kill": 0, "oom_group_kill": 0},
            })
            index += 20
        samples.append(error_sample(float(index), "health poll failed: 502"))
        index += 20
        samples.append({"type": "event", "timestamp": float(index),
                        "name": "job_resubmitted", "detail": "lost=x resubmitted=y"})
        index += 1
        stall = 0  # per-container counters reset on replacement
    return samples


def test_memory_pressure_quiet_counters_abandon_via_replacements():
    # Regression for run 36493316814: quiet stall counters must not
    # mask a pinned-usage replacement storm. The within-group surge
    # (448) stays far below the surge threshold and the global
    # first/last deltas read 0, yet four instance transitions with
    # usage pinned at the limit is pressure via the replacements
    # branch -- the exact verdict the live breaker reached.
    samples = _run_36493316814_samples()
    assert detect_memory_pressure(samples) is True
    evidence = memory_pressure_evidence(samples)
    assert evidence["pinned_at_limit"] is True
    assert evidence["restart_transitions"] == 4
    assert evidence["max_stall_surge_delta"] == 448
    assert evidence["stall_surge"] is False
    assert pressure_decision_branch(evidence) == "replacements"
    # Same restart cadence without pinning keeps budget-limited
    # recovery (transient host-maintenance shape, never a storm).
    cool = [dict(s, memory_current_bytes=LIMIT_512M // 2)
            if s.get("ok") else dict(s) for s in samples]
    assert detect_memory_pressure(cool) is False
    assert pressure_decision_branch(
        memory_pressure_evidence(cool)) == "none"


def _run_36493364897_samples():
    """Live telemetry shape of failed run 36493364897 (issue #112).

    Five worker instances in ~11 minutes (6d24e521 -> ef17e15f ->
    f0c1085b -> f00ba6aa -> 240045cb at ~143s intervals), cgroup
    usage pinned at EXACTLY the 512 MB limit (peak current 536870912
    of 536870912, ratio 1.0), Python RSS ~33 MB, swap 0,
    memory.events counters fully quiet (within-group max-stall rise
    162 against the 10000 surge threshold; global first/last deltas
    0), every replacement separated by sampler gap samples plus a
    job_resubmitted event marker. The storm breaker abandoned at the
    fourth proven loss (poll 33/140) via the replacements branch --
    an even quieter counter shape than run 36493316814 (surge 448),
    so this second live shape locks the pure-replacements path where
    no stall movement at all is needed for a pressure verdict.
    """
    samples = []
    index = 0
    stall = 0
    instances = ("6d24e5213968", "ef17e15f0f3c", "f0c1085bd3fe",
                 "f00ba6aa767b", "240045cbeb49")
    # Within-group max-stall rises per instance lifetime; the largest
    # single-group rise is 162, far below the 10000 surge threshold.
    rises = (0, 0, 0, 0, 162)
    for group, instance in enumerate(instances):
        for i in range(8):
            if i == 6:
                stall += rises[group]
            samples.append({
                "type": "sample",
                "timestamp": 1790635000.0 + index,
                "ok": True,
                "instance_id": instance,
                "memory_limit_bytes": LIMIT_512M,
                "memory_current_bytes": LIMIT_512M,
                "memory_events": {"high": 0, "max": stall, "oom": 0,
                                  "oom_kill": 0, "oom_group_kill": 0},
            })
            index += 20
        samples.append(error_sample(float(index), "health poll failed: 502"))
        index += 20
        samples.append({"type": "event", "timestamp": float(index),
                        "name": "job_resubmitted", "detail": "lost=x resubmitted=y"})
        index += 1
        stall = 0  # per-container counters reset on replacement
    return samples


def test_memory_pressure_exact_pinned_quiet_counters_abandon_via_replacements():
    # Regression for run 36493364897: usage pinned at exactly the
    # limit (ratio 1.0) with fully quiet stall counters (surge 162)
    # must still abandon via the replacements branch -- four instance
    # transitions with pinned usage is pressure even when no stall
    # movement of any significance exists.
    samples = _run_36493364897_samples()
    assert detect_memory_pressure(samples) is True
    evidence = memory_pressure_evidence(samples)
    assert evidence["pinned_at_limit"] is True
    assert evidence["usage_ratio"] == 1.0
    assert evidence["restart_transitions"] == 4
    assert evidence["max_stall_surge_delta"] == 162
    assert evidence["stall_surge"] is False
    assert pressure_decision_branch(evidence) == "replacements"
    # Same restart cadence without pinning keeps budget-limited
    # recovery (transient host-maintenance shape, never a storm).
    cool = [dict(s, memory_current_bytes=LIMIT_512M // 2)
            if s.get("ok") else dict(s) for s in samples]
    assert detect_memory_pressure(cool) is False
    assert pressure_decision_branch(
        memory_pressure_evidence(cool)) == "none"


def test_pressure_decision_branch_names_deciding_evidence():
    pinned = {"pinned_at_limit": True, "restart_transitions": 3,
              "stall_surge": False}
    assert pressure_decision_branch(pinned) == "replacements"
    surge_only = {"pinned_at_limit": True, "restart_transitions": 0,
                  "stall_surge": True}
    assert pressure_decision_branch(surge_only) == "stall_surge"
    assert pressure_decision_branch(
        {"pinned_at_limit": True, "restart_transitions": 1,
         "stall_surge": False}) == "none"
    assert pressure_decision_branch(
        {"pinned_at_limit": False, "restart_transitions": 9,
         "stall_surge": True}) == "none"
    # Missing/corrupt evidence never names a branch (fails open).
    assert pressure_decision_branch({}) == "none"
    assert pressure_decision_branch(None) == "none"
    assert pressure_decision_branch(
        {"pinned_at_limit": True, "restart_transitions": "bogus",
         "stall_surge": False}) == "none"


def test_summary_embeds_auditable_pressure_verdict():
    # The durable summary must carry the storm decision inputs, not
    # just the global first/last deltas (which read 0 across
    # replacements by design and made run 36493316814 look
    # unsupported after the fact).
    samples = _run_36493316814_samples()
    summary = summarize_samples(samples)
    pressure = summary["memory_pressure"]
    assert pressure["verdict"] is True
    assert pressure["branch"] == "replacements"
    assert pressure["usage_ratio_threshold"] == MEMORY_PRESSURE_USAGE_RATIO
    assert pressure["min_restarts"] == MEMORY_PRESSURE_MIN_RESTARTS
    assert pressure["stall_surge_delta"] == MEMORY_PRESSURE_STALL_SURGE_DELTA
    assert pressure["restart_transitions"] == 4
    assert pressure["max_stall_surge_delta"] == 448
    text = render_human_summary(summary)
    assert "memory-pressure verdict: PRESSURE" in text
    assert "via replacements" in text
    # Empty input still carries an explicit negative verdict.
    empty = summarize_samples([])
    assert empty["memory_pressure"]["verdict"] is False
    assert empty["memory_pressure"]["branch"] == "none"
    assert "memory-pressure verdict: NO PRESSURE" in render_human_summary(empty)


# ---------------------------------------------------------------------------
# Harness static contract (no .github/workflows changes; logic in automation/).
# ---------------------------------------------------------------------------


def _job_script() -> str:
    return (AUTOMATION / "render-job.sh").read_text(encoding="utf-8")


def test_harness_starts_bounded_sampler_after_healthy_before_submit():
    job = _job_script()
    healthy_idx = job.find("Runner is healthy.")
    sampler_idx = job.find("render_memory_sampler.py sample")
    submit_idx = job.find("Submitted runner job")
    assert healthy_idx != -1 and sampler_idx != -1 and submit_idx != -1
    assert healthy_idx < sampler_idx < submit_idx
    assert "--interval" in job and "--max-seconds" in job and "--stop-file" in job
    assert "RENDER_MEMORY_SAMPLES_FILE" in job
    assert "RENDER_MEMORY_SUMMARY_FILE" in job


def test_harness_sampler_interval_within_two_seconds_and_bounded():
    job = _job_script()
    assert "RENDER_MEMORY_SAMPLE_INTERVAL_SECONDS:-1" in job
    assert "MAX_INTERVAL_SECONDS" in (
        (AUTOMATION / "render_memory_sampler.py").read_text(encoding="utf-8")
    )


def test_harness_stops_sampler_and_summarizes_on_every_terminal_path():
    job = _job_script()
    assert "trap memory_sampler_stop_and_summarize EXIT" in job
    assert "kill -9" in job  # bounded SIGKILL after a short graceful wait
    assert "memory_telemetry" in job  # merged into job evidence best-effort
    assert "job_resubmitted" in job  # transition timestamps around job loss


def test_human_summary_header_has_no_stale_issue_attribution():
    # Run 36652760209 for source issue #58 printed
    # "Container memory summary (issue #57):" because the header hardcoded
    # the originating issue. The sampler is reused across issues, so the
    # durable log header must stay generic and never attribute telemetry
    # to a fixed prior issue number.
    summary = summarize_samples([])
    header = render_human_summary(summary).splitlines()[0]
    assert header == "Container memory summary:"
    assert "(issue #" not in render_human_summary(summary)


def test_harness_sampler_handles_no_secrets():
    job = _job_script()
    sampler = (AUTOMATION / "render_memory_sampler.py").read_text(encoding="utf-8")
    for secret in ("RENDER_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "OPENCODE_API_KEY"):
        assert secret not in sampler
    # The sampler only fetches the unauthenticated health endpoint.
    assert "/health" in sampler
    assert "Authorization" not in sampler
    # The trap/sampler block never echoes keys or tokens.
    sampler_block = job[job.find("Container memory telemetry"):]
    for secret in ("RENDER_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "OPENCODE_API_KEY"):
        assert secret not in sampler_block
