"""External container-memory sampler for the live Render experiment (issue #57).

Runs on the GitHub Actions side (NOT inside the Render container), sampling
the worker's ``GET /health`` resource endpoint approximately every second
until the OpenCode job reaches a terminal state or worker cleanup begins.
Because a Render restart erases in-process state, samples are stored on the
Actions side as JSONL so pre-restart telemetry survives.

Usage from the temporary harness (``automation/render-job.sh``)::

    python3 automation/render_memory_sampler.py sample \\
        --base-url "$SERVICE_URL" --output "$SAMPLES_FILE" --interval 1.0

    python3 automation/render_memory_sampler.py summarize \\
        --input "$SAMPLES_FILE" --output "$SUMMARY_FILE"

Stdlib only. Never handles secrets: the health endpoint carries no
credentials, and this module never reads tokens, keys, or headers.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Any, Callable, Mapping, Optional, Sequence

DEFAULT_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_SECONDS = 2900.0
REQUEST_TIMEOUT_SECONDS = 10.0


def fetch_health(base_url: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    """GET one /health document; raise on transport/parse failures."""
    url = base_url.rstrip("/") + "/health"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_sample(health: Mapping[str, Any], timestamp: Optional[float] = None) -> dict[str, Any]:
    """Flatten one /health body into a telemetry sample (never raises)."""
    now = timestamp if timestamp is not None else time.time()
    sample: dict[str, Any] = {
        "type": "sample",
        "timestamp": now,
        "ok": True,
    }
    try:
        sample["instance_id"] = str(health.get("instance_id", "") or "")
        resources = health.get("resources", {})
        if not isinstance(resources, Mapping):
            resources = {}
        sample["rss_kb"] = resources.get("rss_kb")
        sample["peak_rss_kb"] = resources.get("peak_rss_kb")
        cgroup = resources.get("cgroup", {})
        if not isinstance(cgroup, Mapping):
            cgroup = {}
        for key in (
            "cgroup_version",
            "memory_limit_bytes",
            "memory_current_bytes",
            "memory_current_mb",
            "memory_usage_percent",
            "memory_peak_bytes",
            "swap_current_bytes",
            "swap_max_bytes",
        ):
            sample[key] = cgroup.get(key)
        events = cgroup.get("memory_events", {})
        sample["memory_events"] = dict(events) if isinstance(events, Mapping) else {}
    except Exception as exc:  # defensive: a sample must never crash the loop
        sample["ok"] = False
        sample["error"] = "extract failed: %s" % exc
    return sample


def error_sample(timestamp: float, error: str) -> dict[str, Any]:
    """Gap marker for a failed poll (e.g. worker restarting); keeps the gap."""
    return {
        "type": "sample",
        "timestamp": timestamp,
        "ok": False,
        "error": str(error)[:300],
    }


def append_sample(path: str, sample: Mapping[str, Any]) -> None:
    """Append one JSON line; directory is created best-effort."""
    import os

    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, sort_keys=True) + "\n")


def read_samples(path: str) -> list[dict[str, Any]]:
    """Read back JSONL samples (plus harness event markers); skips bad lines."""
    samples: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    samples.append(entry)
    except (OSError, UnicodeError):
        pass
    return samples


def event_marker(
    path: str,
    name: str,
    detail: str = "",
    timestamp: Optional[float] = None,
) -> None:
    """Append a harness event marker (e.g. resubmission) into the sample log."""
    try:
        append_sample(
            path,
            {
                "type": "event",
                "timestamp": timestamp if timestamp is not None else time.time(),
                "name": name,
                "detail": str(detail)[:500],
            },
        )
    except OSError:
        pass


def sample_loop(
    base_url: str,
    output_path: str,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    stop_path: str = "",
    fetcher: Optional[Callable[[], Mapping[str, Any]]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> int:
    """Poll /health until the stop file appears or the bound is reached.

    Returns the number of samples stored. Transport failures are recorded
    as gap samples (ok=false) so restart windows stay visible. Bounded:
    at most ``max_seconds / interval`` samples; non-positive bounds stop
    immediately. ``fetcher``/``clock``/``sleeper`` are injectable seams
    for unit tests.
    """
    import os

    now = clock or time.time
    sleep = sleeper or time.sleep
    interval = max(0.05, min(float(interval_seconds or 0) or DEFAULT_INTERVAL_SECONDS,
                             MAX_INTERVAL_SECONDS))
    budget = max(0.0, float(max_seconds or 0))
    deadline = now() + budget
    count = 0
    while True:
        if stop_path and os.path.exists(stop_path):
            break
        if now() >= deadline:
            break
        started = now()
        try:
            health = fetcher() if fetcher is not None else fetch_health(base_url)
            sample = extract_sample(health, timestamp=started)
        except Exception as exc:
            sample = error_sample(started, "health poll failed: %s" % exc)
        try:
            append_sample(output_path, sample)
        except OSError:
            break
        count += 1
        elapsed = now() - started
        remaining = interval - elapsed
        if remaining > 0:
            try:
                sleep(remaining)
            except Exception:
                break
    return count


_EVENT_COUNTERS = ("high", "max", "oom", "oom_kill", "oom_group_kill")

# Memory-pressure restart-storm signal (issue #71, run 36439192645).
#
# The ~600-615 MB agent peak (issue #52) systematically exceeds the 512 MB
# Free worker (vendor contract https://render.com/docs/free, re-verified
# 2026-09-28: free = 0.1 CPU / 512 MB, anytime restart, ephemeral
# filesystem). Under that mismatch the worker thrashes: cgroup usage pins
# at the limit, memory.events max stalls surge, and the process is
# replaced every few minutes, wiping the in-memory job each time. A
# single pinned reading is not proof (a healthy run briefly touches the
# limit); pressure requires sustained pinning PLUS replacement or stall
# evidence. Per-container event counters reset on every replacement
# (run 36434278632 showed first/last delta 0 across six replacements),
# so the stall surge is measured within same-instance groups, never as
# a global first-to-last delta.
MEMORY_PRESSURE_USAGE_RATIO = 0.95
MEMORY_PRESSURE_MIN_RESTARTS = 2
MEMORY_PRESSURE_STALL_SURGE_DELTA = 10000


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def summarize_samples(
    samples: Sequence[Mapping[str, Any]],
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Build the machine-readable run summary from retained samples."""
    data = [s for s in samples if isinstance(s, Mapping) and s.get("type", "sample") == "sample"]
    events = [s for s in samples if isinstance(s, Mapping) and s.get("type") == "event"]
    ok = [s for s in data if s.get("ok")]
    summary: dict[str, Any] = {
        "schema": "runtime-lab-memory-summary/v1",
        "samples": len(data),
        "ok_samples": len(ok),
        "failed_polls": len(data) - len(ok),
        "sampling_interval_seconds": interval_seconds,
        "started_at": None,
        "ended_at": None,
        "memory_limit_bytes": None,
        "max_memory_current_bytes": None,
        "max_memory_peak_bytes": None,
        "max_rss_kb": None,
        "max_swap_current_bytes": None,
        "swap_max_bytes": None,
        "instance_ids": [],
        "instance_changed": False,
        "event_counter_changes": {},
        "restart_transitions": [],
        "harness_events": [
            {
                "timestamp": e.get("timestamp"),
                "name": e.get("name"),
                "detail": e.get("detail"),
            }
            for e in events
        ],
    }
    if not data:
        summary["memory_pressure"] = {
            "verdict": False,
            "branch": "none",
            "usage_ratio": None,
            "usage_ratio_threshold": MEMORY_PRESSURE_USAGE_RATIO,
            "pinned_at_limit": False,
            "restart_transitions": 0,
            "min_restarts": MEMORY_PRESSURE_MIN_RESTARTS,
            "max_stall_surge_delta": 0,
            "stall_surge_delta": MEMORY_PRESSURE_STALL_SURGE_DELTA,
            "stall_surge": False,
        }
        return summary
    timestamps = [s.get("timestamp") for s in data
                  if isinstance(s.get("timestamp"), (int, float))]
    if timestamps:
        summary["started_at"] = min(timestamps)
        summary["ended_at"] = max(timestamps)

    def _max(key: str) -> Optional[int]:
        best: Optional[int] = None
        for sample in data:
            value = _as_int(sample.get(key))
            if value is not None and (best is None or value > best):
                best = value
        return best

    summary["max_memory_current_bytes"] = _max("memory_current_bytes")
    summary["max_memory_peak_bytes"] = _max("memory_peak_bytes")
    summary["max_rss_kb"] = _max("rss_kb")
    summary["max_swap_current_bytes"] = _max("swap_current_bytes")
    for sample in data:
        limit = _as_int(sample.get("memory_limit_bytes"))
        if limit is not None:
            summary["memory_limit_bytes"] = limit
            break
    for sample in data:
        swap_max = _as_int(sample.get("swap_max_bytes"))
        if swap_max is not None:
            summary["swap_max_bytes"] = swap_max
            break

    instance_ids: list[str] = []
    for sample in data:
        instance = sample.get("instance_id")
        if isinstance(instance, str) and instance and instance not in instance_ids:
            instance_ids.append(instance)
    summary["instance_ids"] = instance_ids
    summary["instance_changed"] = len(instance_ids) > 1

    # Restart transitions: consecutive ok samples whose instance id differs,
    # with the timestamps around the transition.
    previous: Optional[str] = None
    previous_time: Optional[float] = None
    for sample in data:
        instance = sample.get("instance_id")
        stamp = sample.get("timestamp")
        if not sample.get("ok") or not isinstance(instance, str) or not instance:
            continue
        if previous is not None and instance != previous:
            summary["restart_transitions"].append(
                {
                    "from_instance_id": previous,
                    "to_instance_id": instance,
                    "previous_timestamp": previous_time,
                    "timestamp": stamp if isinstance(stamp, (int, float)) else None,
                }
            )
        previous = instance
        previous_time = stamp if isinstance(stamp, (int, float)) else previous_time

    first_events: Optional[Mapping[str, Any]] = None
    last_events: Optional[Mapping[str, Any]] = None
    for sample in data:
        events_map = sample.get("memory_events")
        if isinstance(events_map, Mapping) and events_map:
            if first_events is None:
                first_events = events_map
            last_events = events_map
    changes: dict[str, Any] = {}
    if isinstance(first_events, Mapping) and isinstance(last_events, Mapping):
        for name in _EVENT_COUNTERS:
            first = first_events.get(name)
            last = last_events.get(name)
            if isinstance(first, int) and isinstance(last, int) and not isinstance(first, bool):
                changes[name] = {
                    "first": first,
                    "last": last,
                    "delta": last - first,
                }
    summary["event_counter_changes"] = changes
    # Storm-verdict audit trail (issue #113): the global first/last
    # deltas above reset to ~0 on every container replacement by
    # design, so they cannot adjudicate a storm verdict after the
    # fact. Record the deciding inputs alongside them -- the
    # gap-tolerant within-group evidence, the thresholds applied, the
    # deciding branch, and the boolean verdict -- so the durable log
    # states why pressure was (or was not) declared.
    pressure = memory_pressure_evidence(data)
    summary["memory_pressure"] = {
        "verdict": detect_memory_pressure(
            data,
            min_restarts=MEMORY_PRESSURE_MIN_RESTARTS,
            usage_ratio=MEMORY_PRESSURE_USAGE_RATIO,
            stall_surge_delta=MEMORY_PRESSURE_STALL_SURGE_DELTA,
        ),
        "branch": pressure_decision_branch(
            pressure, min_restarts=MEMORY_PRESSURE_MIN_RESTARTS),
        "usage_ratio": pressure.get("usage_ratio"),
        "usage_ratio_threshold": MEMORY_PRESSURE_USAGE_RATIO,
        "pinned_at_limit": pressure.get("pinned_at_limit"),
        "restart_transitions": pressure.get("restart_transitions"),
        "min_restarts": MEMORY_PRESSURE_MIN_RESTARTS,
        "max_stall_surge_delta": pressure.get("max_stall_surge_delta"),
        "stall_surge_delta": MEMORY_PRESSURE_STALL_SURGE_DELTA,
        "stall_surge": pressure.get("stall_surge"),
    }
    return summary


def render_human_summary(summary: Mapping[str, Any]) -> str:
    """Concise human-readable rendering of :func:`summarize_samples`."""

    def _mb(value: Any) -> str:
        if isinstance(value, int):
            return "%.1f MB" % (value / (1024 * 1024))
        return "unknown"

    lines = [
        "Container memory summary (issue #57):",
        "- samples: %s ok / %s total (interval %.1fs)" % (
            summary.get("ok_samples"), summary.get("samples"),
            summary.get("sampling_interval_seconds") or 0),
        "- cgroup limit: %s" % _mb(summary.get("memory_limit_bytes")),
        "- highest observed memory.current: %s"
        % _mb(summary.get("max_memory_current_bytes")),
        "- highest observed memory.peak: %s"
        % _mb(summary.get("max_memory_peak_bytes")),
        "- highest Python process RSS: %s"
        % (
            ("%d kB" % summary["max_rss_kb"])
            if isinstance(summary.get("max_rss_kb"), int)
            else "unknown"
        ),
        "- swap observed: current max %s / swap max %s"
        % (
            _mb(summary.get("max_swap_current_bytes")),
            _mb(summary.get("swap_max_bytes")),
        ),
        "- instance ids seen: %s (changed=%s)"
        % (
            ", ".join(str(i)[:12] for i in summary.get("instance_ids", [])) or "none",
            summary.get("instance_changed"),
        ),
    ]
    changes = summary.get("event_counter_changes", {})
    if isinstance(changes, Mapping) and changes:
        for name in _EVENT_COUNTERS:
            entry = changes.get(name)
            if isinstance(entry, Mapping):
                lines.append(
                    "- memory.events %s: first=%s last=%s delta=%s"
                    % (name, entry.get("first"), entry.get("last"), entry.get("delta"))
                )
    else:
        lines.append("- memory.events: no counters observed")
    transitions = summary.get("restart_transitions", [])
    if transitions:
        for item in transitions:
            lines.append(
                "- restart: %s -> %s around t=%s"
                % (
                    str(item.get("from_instance_id", ""))[:12],
                    str(item.get("to_instance_id", ""))[:12],
                    item.get("timestamp"),
                )
            )
    else:
        lines.append("- restarts observed: none")
    # Storm-verdict audit trail (issue #113): state the deciding inputs
    # explicitly, because the global event deltas above are ~0 by design
    # across replacements while the verdict rests on the gap-tolerant
    # within-group evidence (run 36493316814 abandoned via the
    # replacements branch with a within-group surge of only 448).
    pressure = summary.get("memory_pressure", {})
    if isinstance(pressure, Mapping) and pressure:
        ratio = pressure.get("usage_ratio")
        lines.append(
            "- memory-pressure verdict: %s (via %s; pinned %s at ratio %s >= %s; "
            "transitions %s >= %s; surge %s >= %s is %s)"
            % (
                "PRESSURE" if pressure.get("verdict") else "NO PRESSURE",
                pressure.get("branch"),
                pressure.get("pinned_at_limit"),
                ("%.4f" % ratio) if isinstance(ratio, float) else ratio,
                pressure.get("usage_ratio_threshold"),
                pressure.get("restart_transitions"),
                pressure.get("min_restarts"),
                pressure.get("max_stall_surge_delta"),
                pressure.get("stall_surge_delta"),
                "surge" if pressure.get("stall_surge") else "quiet",
            )
        )
    harness_events = summary.get("harness_events", [])
    if harness_events:
        lines.append("- harness events: %d recorded" % len(harness_events))
    return "\n".join(lines)


def memory_pressure_evidence(
    samples: Sequence[Mapping[str, Any]],
    *,
    usage_ratio: float = MEMORY_PRESSURE_USAGE_RATIO,
    stall_surge_delta: int = MEMORY_PRESSURE_STALL_SURGE_DELTA,
) -> dict[str, Any]:
    """Summarize the memory-pressure restart-storm signal (issue #71).

    Returns a small evidence dict (never raises): the observed cgroup
    limit, the highest current usage, whether usage ever pinned at the
    limit (>= ``usage_ratio``), how many instance replacements were
    observed, and the largest same-instance memory.events max-counter
    surge. Gaps (ok=false), event markers, and samples without numeric
    cgroup fields are ignored without breaking continuity: transition
    and surge groups survive restart down-windows, matching
    summarize_samples (which preserves the previous instance across
    gaps). Per-instance grouping matters because per-container event
    counters reset on every replacement.
    """
    evidence: dict[str, Any] = {
        "samples_considered": 0,
        "memory_limit_bytes": None,
        "max_memory_current_bytes": None,
        "usage_ratio": None,
        "pinned_at_limit": False,
        "restart_transitions": 0,
        "max_stall_surge_delta": 0,
    }
    try:
        limit: Optional[int] = None
        peak = 0
        considered = 0
        instances: list[str] = []
        transitions = 0
        previous: Optional[str] = None
        # Same-instance max-counter tracking for the stall surge: reset
        # only when the instance id changes. Gaps (ok=false), event
        # markers, and samples without numeric cgroup fields preserve
        # the open group so a restart down-window never splits a surge
        # or hides a replacement (run 36442675039: every live restart
        # is separated by gap samples plus a job_resubmitted event
        # marker, so resetting on gaps reported zero transitions live
        # while the end-of-run summary counted ten).
        group_first: Optional[int] = None
        group_last: Optional[int] = None
        max_surge = 0
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            if sample.get("type", "sample") != "sample":
                continue
            if not sample.get("ok"):
                continue
            raw_instance = sample.get("instance_id")
            instance = raw_instance if isinstance(raw_instance, str) and raw_instance else ""
            if instance:
                if instance not in instances:
                    instances.append(instance)
                if previous is not None and instance != previous:
                    transitions += 1
                    if group_first is not None and group_last is not None:
                        surge = group_last - group_first
                        if surge > max_surge:
                            max_surge = surge
                    group_first = None
                    group_last = None
                previous = instance
            current = sample.get("memory_current_bytes")
            if isinstance(current, bool):
                current = None
            sample_limit = sample.get("memory_limit_bytes")
            if isinstance(sample_limit, bool):
                sample_limit = None
            if not isinstance(sample_limit, int) or not isinstance(current, int):
                continue
            if limit is None:
                limit = sample_limit
            considered += 1
            if current > peak:
                peak = current
            events = sample.get("memory_events")
            stalls = events.get("max") if isinstance(events, Mapping) else None
            if isinstance(stalls, bool):
                stalls = None
            if isinstance(stalls, int):
                if group_first is None:
                    group_first = stalls
                group_last = stalls
        if group_first is not None and group_last is not None:
            surge = group_last - group_first
            if surge > max_surge:
                max_surge = surge
        evidence["samples_considered"] = considered
        evidence["memory_limit_bytes"] = limit
        evidence["max_memory_current_bytes"] = peak if considered else None
        evidence["restart_transitions"] = transitions
        evidence["max_stall_surge_delta"] = max_surge
        if limit is not None and limit > 0 and considered:
            ratio = peak / limit
            evidence["usage_ratio"] = ratio
            evidence["pinned_at_limit"] = ratio >= float(usage_ratio)
        evidence["stall_surge"] = max_surge >= int(stall_surge_delta)
    except Exception:
        pass
    return evidence


def detect_memory_pressure(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_restarts: int = MEMORY_PRESSURE_MIN_RESTARTS,
    usage_ratio: float = MEMORY_PRESSURE_USAGE_RATIO,
    stall_surge_delta: int = MEMORY_PRESSURE_STALL_SURGE_DELTA,
) -> bool:
    """True when telemetry proves memory-pressure thrash (issue #71).

    Requires usage pinned at the cgroup limit AND either repeated
    instance replacements (>= ``min_restarts`` transitions) or a large
    same-instance max-stall surge. Either corroborating signal excludes
    a healthy run that merely brushes the limit once. Never raises:
    missing/unparsable input reports no pressure (callers fail open
    toward the pre-existing budget-limited recovery).
    """
    try:
        evidence = memory_pressure_evidence(
            samples,
            usage_ratio=usage_ratio,
            stall_surge_delta=stall_surge_delta,
        )
        if not evidence.get("pinned_at_limit"):
            return False
        try:
            required = int(min_restarts)
        except (TypeError, ValueError):
            return False
        if int(evidence.get("restart_transitions", 0)) >= required:
            return True
        return bool(evidence.get("stall_surge"))
    except Exception:
        return False


def detect_memory_pressure_file(path: str, **kwargs: Any) -> bool:
    """Best-effort file form of :func:`detect_memory_pressure`.

    A missing/unreadable/unparsable samples file reports no pressure so
    the poll loop keeps its pre-existing behavior instead of failing on
    telemetry gaps.
    """
    try:
        return detect_memory_pressure(read_samples(path), **kwargs)
    except Exception:
        return False


def pressure_decision_branch(
    evidence: Mapping[str, Any],
    *,
    min_restarts: int = MEMORY_PRESSURE_MIN_RESTARTS,
) -> str:
    """Name the evidence branch behind a pressure verdict (issue #113).

    Run 36493316814 abandoned via the replacements branch with nearly
    quiet stall counters (within-group surge 448 against a 10000
    threshold, global first/last deltas 0), yet the durable log showed
    only those quiet counters -- inviting a misreading that no pressure
    existed. This helper makes the deciding branch explicit for the
    human summary and the storm diagnostic: ``"replacements"`` when
    pinned usage plus repeated instance transitions carry the verdict,
    ``"stall_surge"`` when a same-instance max-stall surge carries it
    without enough transitions, otherwise ``"none"``. Never raises.
    """
    try:
        if not evidence.get("pinned_at_limit"):
            return "none"
        try:
            required = int(min_restarts)
        except (TypeError, ValueError):
            return "none"
        try:
            transitions = int(evidence.get("restart_transitions", 0))
        except (TypeError, ValueError):
            transitions = 0
        if transitions >= required:
            return "replacements"
        if bool(evidence.get("stall_surge")):
            return "stall_surge"
        return "none"
    except Exception:
        return "none"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="External Render worker memory sampler (issue #57)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="Continuously sample /health to JSONL.")
    sample.add_argument("--base-url", required=True)
    sample.add_argument("--output", required=True)
    sample.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    sample.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    sample.add_argument("--stop-file", default="")
    summarize = sub.add_parser("summarize", help="Summarize retained JSONL samples.")
    summarize.add_argument("--input", required=True)
    summarize.add_argument("--output", default="")
    summarize.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sample":
        count = sample_loop(
            args.base_url,
            args.output,
            interval_seconds=args.interval,
            max_seconds=args.max_seconds,
            stop_path=args.stop_file,
        )
        print("memory sampler stored %d samples in %s" % (count, args.output), flush=True)
        return 0
    if args.command == "summarize":
        samples = read_samples(args.input)
        summary = summarize_samples(samples, interval_seconds=args.interval)
        text = json.dumps(summary, sort_keys=True, indent=2) + "\n"
        if args.output:
            import os

            directory = os.path.dirname(os.path.abspath(args.output))
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text)
        else:
            print(text, end="")
        print(render_human_summary(summary), flush=True)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
