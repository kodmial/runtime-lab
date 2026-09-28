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
    harness_events = summary.get("harness_events", [])
    if harness_events:
        lines.append("- harness events: %d recorded" % len(harness_events))
    return "\n".join(lines)


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
