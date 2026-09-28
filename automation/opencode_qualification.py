"""Qualification gate for the optimized OpenCode fork on Render Free (issue #81).

Stdlib only. This module defines the qualification matrix, the per-run
measurement record contract, budget evaluation, and the before/after table
renderer. It reuses the cgroup telemetry implemented by #57
(``automation/cgroup_memory.py`` field names plus the
``automation/render_memory_sampler.py`` summary shape) and the real Render
execution path refined by #58 (``automation/render-job.sh`` /
``automation/render_lifecycle.py``); it builds no second measurement stack.

Live Render runs are executed by the ``execution:render-smoke`` path, not by
this module. Every validator here fails closed: a run record with missing
measurements, an OOM kill, a worker replacement, a nonzero OpenCode exit, or
unverified worker cleanup is rejected instead of being scored as a pass.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

QUALIFICATION_ISSUE = 81
QUALIFICATION_SCHEMA = "runtime-lab-qualification-run/v1"

MIB = 1024 * 1024
# Preferred engineering target from #81: representative-task peak at or below
# 450 MiB leaves material headroom below the 512 MiB hard limit.
TARGET_PEAK_BYTES = 450 * MIB
# Render Free worker hard memory limit (re-verified 2026-09-28).
HARD_LIMIT_BYTES = 512 * MIB

# ---------------------------------------------------------------------------
# Qualification matrix (#81): five real-Render workloads, each a fresh
# process/session on an actual Free worker with external cgroup sampling.
# ---------------------------------------------------------------------------

QUALIFICATION_MATRIX: tuple[dict[str, str], ...] = (
    {
        "id": "q1-small-edit",
        "title": "Small code edit + focused test",
        "workload": (
            "Inspect one module, make one small concrete code or test-coverage "
            "improvement, run the focused relevant tests, report files and result."
        ),
    },
    {
        "id": "q2-search-multi-edit",
        "title": "Repository search + multi-file edit + test",
        "workload": (
            "Search the repository, edit more than one file coherently, "
            "then run the relevant tests."
        ),
    },
    {
        "id": "q3-large-output",
        "title": "Substantial build/test output",
        "workload": (
            "Run a build or test target that produces substantial stdout/stderr "
            "so tool-output spooling and truncation stay bounded in memory."
        ),
    },
    {
        "id": "q4-fail-fix-rerun",
        "title": "Failure -> inspect logs -> fix -> rerun",
        "workload": (
            "Start from a failing run, inspect the logs, implement the fix, "
            "then rerun to green within the same fresh session discipline."
        ),
    },
    {
        "id": "q5-fresh-sessions",
        "title": "Multiple independent issue jobs, fresh process/session each",
        "workload": (
            "Execute several independent issue jobs back to back, each with a "
            "fresh worker process and session, proving repeatability without "
            "cross-session accumulation."
        ),
    },
)

# Comparison profiles (#81): the same key workload recorded per artifact.
COMPARISON_PROFILES: tuple[dict[str, str], ...] = (
    {
        "id": "upstream-baseline",
        "label": "Upstream/baseline OpenCode (#52/#58)",
        "artifact": "pinned upstream 1.18.33 standalone binary",
        "status": "measured",
    },
    {
        "id": "config-only",
        "label": "Config-only profile (#78)",
        "artifact": "pending: issue #78 is OPEN, no profile exists yet",
        "status": "pending",
    },
    {
        "id": "source-stripped",
        "label": "Source-stripped build (#79)",
        "artifact": "pending: issue #79 is OPEN, no build exists yet",
        "status": "pending",
    },
    {
        "id": "bounded-fork",
        "label": "Final #80-bounded artifact from #86",
        "artifact": "pending: issues #80 and #86 are OPEN, no artifact exists yet",
        "status": "pending",
    },
)

# Every per-run measurement required by #81. Field names reuse the #57
# telemetry vocabulary (cgroup snapshot keys + sampler summary keys) plus
# provisioning/identity fields so no second measurement stack is needed.
REQUIRED_MEASUREMENT_FIELDS: tuple[str, ...] = (
    "fork_commit_sha",
    "upstream_baseline_revision",
    "artifact_sha256",
    "entrypoint",
    "config",
    "environment",
    "memory_limit_bytes",
    "memory_current_bytes",
    "memory_peak_bytes",
    "memory_events",
    "swap_current_bytes",
    "swap_max_bytes",
    "process_rss_kb",
    "process_hwm_kb",
    "instance_ids",
    "instance_changed",
    "restart_transitions",
    "duration_seconds",
    "opencode_exit_code",
    "model",
    "provider",
    "cleanup_verified",
)

_BYTE_FIELDS = frozenset(
    {
        "memory_limit_bytes",
        "memory_current_bytes",
        "memory_peak_bytes",
        "swap_current_bytes",
        "swap_max_bytes",
    }
)


def matrix_ids() -> list[str]:
    """Workload ids of the qualification matrix (stable order)."""
    return [entry["id"] for entry in QUALIFICATION_MATRIX]


def profile_ids() -> list[str]:
    """Comparison profile ids (stable order)."""
    return [entry["id"] for entry in COMPARISON_PROFILES]


def required_measurement_fields() -> list[str]:
    """Required per-run measurement field names from the #81 contract."""
    return list(REQUIRED_MEASUREMENT_FIELDS)


def build_run_record(
    *,
    workload_id: str,
    profile_id: str,
    measurements: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a machine-readable qualification run record skeleton.

    Raises ``ValueError`` when the workload/profile id is unknown or a
    required measurement field is absent. Content validation (OOM, cleanup,
    budget) is left to :func:`validate_run_measurement` so construction and
    verdict stay separate.
    """
    known_workloads = {entry["id"] for entry in QUALIFICATION_MATRIX}
    known_profiles = {entry["id"] for entry in COMPARISON_PROFILES}
    if workload_id not in known_workloads:
        raise ValueError("unknown workload_id: %r" % (workload_id,))
    if profile_id not in known_profiles:
        raise ValueError("unknown profile_id: %r" % (profile_id,))
    missing = [
        field
        for field in REQUIRED_MEASUREMENT_FIELDS
        if field not in measurements
    ]
    if missing:
        raise ValueError("missing measurement fields: %s" % ", ".join(missing))
    return {
        "schema": QUALIFICATION_SCHEMA,
        "issue": QUALIFICATION_ISSUE,
        "workload_id": workload_id,
        "profile_id": profile_id,
        "measurements": dict(measurements),
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_run_measurement(record: Mapping[str, Any]) -> list[str]:
    """Fail-closed validation of one qualification run record.

    Returns a list of error strings; empty means the record satisfies the
    #81 hard gate (completion, no OOM kill, no worker replacement caused by
    memory pressure, verified cleanup). Budget headroom against the 450 MiB
    preferred target is reported by :func:`evaluate_budget`, not here.
    """
    errors: list[str] = []
    if not isinstance(record, Mapping):
        return ["record must be a mapping"]
    if record.get("schema") != QUALIFICATION_SCHEMA:
        errors.append("schema must be %r" % QUALIFICATION_SCHEMA)
    measurements = record.get("measurements")
    if not isinstance(measurements, Mapping):
        return errors + ["measurements must be a mapping"]
    for field in REQUIRED_MEASUREMENT_FIELDS:
        if field not in measurements:
            errors.append("missing measurement field: %s" % field)
    for field in _BYTE_FIELDS:
        value = measurements.get(field)
        if field in measurements and value is not None and not _is_int(value):
            errors.append("%s must be an int byte count or null" % field)
    for field in ("memory_limit_bytes", "memory_peak_bytes"):
        value = measurements.get(field)
        if _is_int(value) and value <= 0:
            errors.append("%s must be positive" % field)
    events = measurements.get("memory_events")
    if "memory_events" in measurements:
        if not isinstance(events, Mapping):
            errors.append("memory_events must be a mapping")
        else:
            for counter in ("oom_kill", "oom_group_kill"):
                value = events.get(counter)
                if value is None:
                    continue
                if not _is_int(value):
                    errors.append("memory_events.%s must be an int" % counter)
                elif value != 0:
                    errors.append(
                        "memory_events.%s is %d: worker was OOM-killed" % (counter, value)
                    )
    instance_ids = measurements.get("instance_ids")
    if "instance_ids" in measurements:
        if (
            not isinstance(instance_ids, list)
            or not instance_ids
            or not all(isinstance(item, str) and item for item in instance_ids)
        ):
            errors.append("instance_ids must be a non-empty list of id strings")
        elif len(set(instance_ids)) > 1:
            errors.append(
                "instance_changed: %d distinct worker instances observed "
                "(worker replacement during the run)" % len(set(instance_ids))
            )
    if measurements.get("instance_changed") is True:
        errors.append(
            "instance_changed is true: worker replacement during the run"
        )
    exit_code = measurements.get("opencode_exit_code")
    if "opencode_exit_code" in measurements and exit_code != 0:
        errors.append("opencode_exit_code is %r, expected 0" % (exit_code,))
    if measurements.get("cleanup_verified") is not True:
        errors.append("cleanup_verified must be true (verified worker deletion)")
    duration = measurements.get("duration_seconds")
    if "duration_seconds" in measurements and (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
    ):
        errors.append("duration_seconds must be a number")
    return errors


def evaluate_budget(
    peak_bytes: int, limit_bytes: int | None = HARD_LIMIT_BYTES
) -> dict[str, Any]:
    """Evaluate one peak against the 450 MiB target and the 512 MiB limit.

    Returns measured peak, headroom/gap figures, and pass flags. A peak above
    the preferred target is reported with its exact remaining gap, never
    masked.
    """
    if isinstance(peak_bytes, bool) or not isinstance(peak_bytes, int):
        raise ValueError("peak_bytes must be an int")
    if peak_bytes <= 0:
        raise ValueError("peak_bytes must be positive")
    result: dict[str, Any] = {
        "peak_bytes": peak_bytes,
        "peak_mib": round(peak_bytes / MIB, 1),
        "target_bytes": TARGET_PEAK_BYTES,
        "target_mib": TARGET_PEAK_BYTES // MIB,
        "passes_target": peak_bytes <= TARGET_PEAK_BYTES,
        "gap_to_target_bytes": peak_bytes - TARGET_PEAK_BYTES,
    }
    if limit_bytes is not None:
        if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int):
            raise ValueError("limit_bytes must be an int or None")
        if limit_bytes <= 0:
            raise ValueError("limit_bytes must be positive")
        result["limit_bytes"] = limit_bytes
        result["limit_mib"] = round(limit_bytes / MIB, 1)
        result["passes_limit"] = peak_bytes <= limit_bytes
        result["gap_to_limit_bytes"] = peak_bytes - limit_bytes
        result["headroom_bytes"] = limit_bytes - peak_bytes
    return result


def baseline_before_rows() -> list[dict[str, Any]]:
    """Validated pre-optimization baseline rows (evidence-backed, not live).

    Numbers come from the checked-in #52 benchmark (Actions host + Docker
    512 MB no-swap stress) and the #56 heap analysis. They are the "before"
    side of the #81 table until live fork profiles exist.
    """
    host_peak_kb = 614632
    host_peak_bytes = host_peak_kb * 1024
    return [
        {
            "profile": "upstream-baseline",
            "workload": "real agent run (host, live free model)",
            "peak_bytes": host_peak_bytes,
            "peak_note": "~600 MiB host process-tree peak, exit 0",
            "provenance": "issue-52-run-36423884312 + "
            "memory-benchmark-issue-52.md",
        },
        {
            "profile": "upstream-baseline",
            "workload": "real agent under Docker 512 MB no-swap",
            "peak_bytes": 536920064,
            "peak_note": "throttled pass at the ceiling with max 140 events; "
            "sibling trial OOM-killed (exit 137, oom_kill 1)",
            "provenance": "issue-52-run-36423884312 "
            "(stress_512m_no_swap.real_agent_under_512m)",
        },
        {
            "profile": "upstream-baseline",
            "workload": "opencode --version (deterministic control)",
            "peak_bytes": 130068480,
            "peak_note": "~124 MiB cgroup peak, exit 0, oom 0",
            "provenance": "memory-benchmark-issue-52.md "
            "(version_under_512m)",
        },
    ]


def dominant_memory_class() -> dict[str, str]:
    """Dominant memory class behind the baseline gap (measured, #56)."""
    return {
        "class": "direct anonymous mmap outside libc malloc "
        "([anon:WKFastMalloc] ~384-389 MB of ~600 MB; plus "
        "[anon:JSGigacage], [anon:JSJITCode], [anon:JSStructureHeap])",
        "why": "The Bun-compiled OpenCode binary bypasses libc malloc by "
        "design, so LD_PRELOAD allocator wrappers move 0-1.4% and peak is "
        "unchanged; only source-level bundle/bootstrap reduction can help.",
        "provenance": "issue-56-run-36429944096 + disk-heap-issue-56.md",
    }


def production_start_command() -> str:
    """Production Render start command for qualification/production workers.

    Mirrors the default in ``render_lifecycle.build_create_service_payload``:
    runtime network installation is disabled so jobs never run curl|bash and
    fail readiness fast when the deploy artifact is absent.
    """
    return "RUNNER_ALLOW_RUNTIME_INSTALL=0 python -m automation.runner_server"


def production_config_fingerprint() -> dict[str, Any]:
    """Production config/fingerprint contract for the selected artifact."""
    return {
        "start_command": production_start_command(),
        "deploy_artifact": ".opencode-bin/opencode",
        "readiness": "GET /health reports opencode_bin/opencode_version/"
        "allow_runtime_install; 503 while not ready",
        "fork_selection": "explicit fork commit SHA + artifact SHA-256 "
        "(immutable reference, never a mutable 'latest' pointer)",
        "fallback": "pinned upstream 1.18.33 binary as reversible A/B "
        "baseline, never a silent fallback during fork qualification",
        "telemetry": "cgroup fields via GET /health resources.cgroup plus "
        "Actions-side ~1s sampler merged as memory_telemetry",
    }


def render_before_after_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render a concise before/after Markdown table from evaluated rows.

    Each row maps ``profile``, ``workload``, ``peak_bytes``,
    ``limit_bytes`` (optional), and ``provenance``. Budget verdicts come
    from :func:`evaluate_budget` so the gap is exact, never masked.
    """
    lines = [
        "| Profile | Workload | Peak (MiB) | <=450 MiB target | <=512 MiB limit | Provenance |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        profile = str(row.get("profile", "?"))
        workload = str(row.get("workload", "?"))
        peak = row.get("peak_bytes")
        if not _is_int(peak) or peak <= 0:
            raise ValueError("each row needs a positive int peak_bytes")
        limit = row.get("limit_bytes", HARD_LIMIT_BYTES)
        if limit is not None and (not _is_int(limit) or limit <= 0):
            raise ValueError("limit_bytes must be a positive int or None")
        verdict = evaluate_budget(peak, limit)
        target_text = (
            "pass" if verdict["passes_target"]
            else ("over by %.1f MiB" % ((peak - TARGET_PEAK_BYTES) / MIB))
        )
        if limit is None:
            limit_text = "n/a"
        else:
            limit_text = (
                "pass" if verdict["passes_limit"]
                else ("over by %.1f MiB" % ((peak - limit) / MIB))
            )
        provenance = str(row.get("provenance", "?"))
        lines.append(
            "| %s | %s | %.1f | %s | %s | %s |"
            % (profile, workload, peak / MIB, target_text, limit_text, provenance)
        )
    return "\n".join(lines) + "\n"
