"""Container-level cgroup memory telemetry for the Render worker (issue #57).

Ports the proven measurement approach from the ``kodmial/kodmai`` reference
(``metrics.py`` / ``/debug/state``): resolve this process's cgroup from
``/proc/self/cgroup``, then read container limits/usage from the cgroup
filesystem (v2 preferred, v1 fallback) instead of host RAM or the Python
runner's own RSS.

Stdlib only. Every file read fails independently: a missing or unreadable
source degrades only its own fields, never the whole snapshot, so
``GET /health`` stays cheap and available under memory pressure.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

PROC_CGROUP_PATH = "/proc/self/cgroup"
CGROUP_ROOT = "/sys/fs/cgroup"

# v1 "unlimited" markers cluster near 2^63 / LONG_MAX; anything above 2^60
# is not a real container limit (Render Free workers are 512 MB).
_UNLIMITED_THRESHOLD = 1 << 60


def _read_text(path: str) -> Optional[str]:
    """Best-effort file read; None when missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return None


def _parse_int(text: Optional[str]) -> Optional[int]:
    """Parse the first whitespace-separated token as int; None on failure."""
    if text is None:
        return None
    try:
        return int(text.strip().split()[0])
    except (ValueError, IndexError):
        return None


def _limit_or_none(value: Optional[int]) -> Optional[int]:
    """Normalize unlimited markers (None stays None; huge v1 values -> None)."""
    if value is None:
        return None
    if value >= _UNLIMITED_THRESHOLD:
        return None
    return value


def parse_memory_events(text: Optional[str]) -> dict[str, int]:
    """Parse cgroup v2 ``memory.events`` (``high``, ``max``, ``oom`` ...)."""
    events: dict[str, int] = {}
    if not text:
        return events
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            events[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return events


def parse_cpu_stat(text: Optional[str]) -> dict[str, int]:
    """Parse cgroup v2 ``cpu.stat`` into a counter dict (best effort)."""
    stats: dict[str, int] = {}
    if not text:
        return stats
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            stats[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return stats


def resolve_cgroup_relative_path(proc_cgroup_text: Optional[str]) -> str:
    """Extract this process's cgroup directory relative to the cgroup root.

    Prefers the v2 entry (``0::/path``); falls back to the v1 memory
    controller entry (``<id>:memory:/path``) and then to the filesystem
    root (``/``) when nothing parseable is present.
    """
    if proc_cgroup_text:
        fallback = ""
        for line in proc_cgroup_text.splitlines():
            parts = line.strip().split(":")
            if len(parts) != 3:
                continue
            _hid, controllers, path = parts
            rel = (path or "").strip() or "/"
            if _hid == "0" and controllers == "":
                return rel
            if "memory" in controllers.split(",") and not fallback:
                fallback = rel
        if fallback:
            return fallback
    return "/"


def resolve_cgroup_dir(
    proc_cgroup_path: str = PROC_CGROUP_PATH,
    cgroup_root: str = CGROUP_ROOT,
) -> str:
    """Absolute cgroup directory for this process (v2 path or v1 fallback)."""
    text = _read_text(proc_cgroup_path)
    rel = resolve_cgroup_relative_path(text)
    candidate = os.path.join(cgroup_root, rel.lstrip("/"))
    if os.path.isdir(candidate):
        return candidate
    # The relative path may not exist under the visible mount (e.g. host
    # paths leaked into containers); fall back to the mount root itself.
    return cgroup_root


def _read_v2(directory: str, snapshot: dict[str, Any]) -> bool:
    """Fill v2 fields; return True when v2 files were the active source."""
    current_raw = _read_text(os.path.join(directory, "memory.current"))
    limit_raw = _read_text(os.path.join(directory, "memory.max"))
    if current_raw is None and limit_raw is None:
        return False
    snapshot["cgroup_version"] = "v2"
    current = _parse_int(current_raw)
    limit: Optional[int] = None
    if limit_raw is not None and limit_raw.strip() != "max":
        limit = _limit_or_none(_parse_int(limit_raw))
    snapshot["memory_current_bytes"] = current
    snapshot["memory_limit_bytes"] = limit
    snapshot["memory_peak_bytes"] = _parse_int(
        _read_text(os.path.join(directory, "memory.peak"))
    )
    snapshot["swap_current_bytes"] = _parse_int(
        _read_text(os.path.join(directory, "memory.swap.current"))
    )
    swap_max_raw = _read_text(os.path.join(directory, "memory.swap.max"))
    swap_max: Optional[int] = None
    if swap_max_raw is not None and swap_max_raw.strip() != "max":
        swap_max = _parse_int(swap_max_raw)
    snapshot["swap_max_bytes"] = swap_max
    snapshot["memory_events"] = parse_memory_events(
        _read_text(os.path.join(directory, "memory.events"))
    )
    cpu_max_raw = _read_text(os.path.join(directory, "cpu.max"))
    if cpu_max_raw is not None:
        snapshot["cpu_max"] = " ".join(cpu_max_raw.split())
    snapshot["cpu_stat"] = parse_cpu_stat(
        _read_text(os.path.join(directory, "cpu.stat"))
    )
    return True


def _read_v1(directory: str, snapshot: dict[str, Any]) -> bool:
    """Fill v1 fallback fields; return True when v1 files were the source."""
    current_raw = _read_text(os.path.join(directory, "memory.usage_in_bytes"))
    limit_raw = _read_text(os.path.join(directory, "memory.limit_in_bytes"))
    if current_raw is None and limit_raw is None:
        return False
    snapshot["cgroup_version"] = "v1"
    snapshot["memory_current_bytes"] = _parse_int(current_raw)
    snapshot["memory_limit_bytes"] = _limit_or_none(_parse_int(limit_raw))
    snapshot["memory_peak_bytes"] = _parse_int(
        _read_text(os.path.join(directory, "memory.max_usage_in_bytes"))
    )
    snapshot["swap_current_bytes"] = _parse_int(
        _read_text(os.path.join(directory, "memory.memsw.usage_in_bytes"))
    )
    snapshot["swap_max_bytes"] = _limit_or_none(
        _parse_int(_read_text(os.path.join(directory, "memory.memsw.limit_in_bytes")))
    )
    events: dict[str, int] = {}
    oom_control = _read_text(os.path.join(directory, "memory.oom_control"))
    if oom_control:
        for line in oom_control.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in (
                "oom_kill",
                "under_oom",
            ):
                try:
                    events[parts[0]] = int(parts[1])
                except ValueError:
                    continue
    snapshot["memory_events"] = events
    snapshot["cpu_quota_us"] = _parse_int(
        _read_text(os.path.join(directory, "cpu.cfs_quota_us"))
    )
    snapshot["cpu_period_us"] = _parse_int(
        _read_text(os.path.join(directory, "cpu.cfs_period_us"))
    )
    snapshot["cpu_usage_ns"] = _parse_int(
        _read_text(os.path.join(directory, "cpuacct.usage"))
    )
    return True


def read_cgroup_snapshot(
    proc_cgroup_path: str = PROC_CGROUP_PATH,
    cgroup_root: str = CGROUP_ROOT,
) -> dict[str, Any]:
    """Best-effort container memory snapshot; never raises.

    Reports cgroup (container) limits/usage, not host RAM and not the
    Python process RSS (those stay separate in
    :func:`runner_server.read_resource_diagnostics`). Missing or
    unreadable sources degrade only their own fields (None / {}).
    """
    snapshot: dict[str, Any] = {
        "cgroup_version": "unknown",
        "cgroup_path": "",
        "memory_limit_bytes": None,
        "memory_current_bytes": None,
        "memory_current_mb": None,
        "memory_usage_percent": None,
        "memory_peak_bytes": None,
        "swap_current_bytes": None,
        "swap_max_bytes": None,
        "memory_events": {},
    }
    try:
        directory = resolve_cgroup_dir(proc_cgroup_path, cgroup_root)
        snapshot["cgroup_path"] = directory
        if _read_v2(directory, snapshot):
            pass
        elif _read_v1(directory, snapshot):
            pass
        current = snapshot.get("memory_current_bytes")
        limit = snapshot.get("memory_limit_bytes")
        if isinstance(current, int) and current is not None:
            snapshot["memory_current_mb"] = round(current / (1024 * 1024), 2)
        if (
            isinstance(current, int)
            and isinstance(limit, int)
            and limit
            and limit > 0
        ):
            snapshot["memory_usage_percent"] = round(current / limit * 100.0, 1)
    except Exception:
        # Never let telemetry break the caller (e.g. GET /health).
        pass
    return snapshot


def summarize_cgroup_snapshot(snapshot: Mapping[str, Any]) -> str:
    """One-line human summary of a snapshot (no secrets involved)."""
    current = snapshot.get("memory_current_bytes")
    limit = snapshot.get("memory_limit_bytes")
    peak = snapshot.get("memory_peak_bytes")

    def _mb(value: Any) -> str:
        if isinstance(value, int):
            return "%.1f MB" % (value / (1024 * 1024))
        return "?"

    percent = snapshot.get("memory_usage_percent")
    percent_text = ("%.1f%%" % percent) if isinstance(percent, (int, float)) else "?"
    events = snapshot.get("memory_events") or {}
    oom_kill = events.get("oom_kill", 0) if isinstance(events, dict) else 0
    return (
        "cgroup %s: current=%s limit=%s (%s) peak=%s oom_kill=%s"
        % (
            snapshot.get("cgroup_version", "unknown"),
            _mb(current),
            _mb(limit),
            percent_text,
            _mb(peak),
            oom_kill,
        )
    )
