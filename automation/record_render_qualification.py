#!/usr/bin/env python3
"""Classify one Render qualification attempt from durable controller-side evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any, Mapping

LIMIT_BYTES = 512 * 1024 * 1024
MIN_HEADROOM_BYTES = 32 * 1024 * 1024
MAX_PEAK_BYTES = LIMIT_BYTES - MIN_HEADROOM_BYTES


def load(path: str) -> dict[str, Any]:
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def event_delta(summary: Mapping[str, Any], name: str) -> int:
    changes = summary.get("event_counter_changes")
    if not isinstance(changes, Mapping):
        return 0
    row = changes.get(name)
    if not isinstance(row, Mapping):
        return 0
    value = row.get("delta")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def nested_identity(result: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = result.get("executable_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    exact = result.get("exact_evidence")
    exact = exact if isinstance(exact, Mapping) else {}
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    meta_exact = metadata.get("exact_evidence")
    meta_exact = meta_exact if isinstance(meta_exact, Mapping) else {}

    # The runner's native exact-artifact contract exposes file_sha256
    # (controller/worker materialized bytes) and exe_sha256/exe_realpath
    # (/proc proof). Read that contract directly instead of requiring an
    # obsolete executable_identity translation layer.
    downloaded = str(
        result.get("downloaded_binary_sha256")
        or result.get("materialized_binary_sha256")
        or identity.get("downloaded_binary_sha256")
        or identity.get("materialized_binary_sha256")
        or exact.get("file_sha256")
        or exact.get("binary_sha256")
        or meta_exact.get("file_sha256")
        or meta_exact.get("binary_sha256")
        or ""
    )
    proc_sha = str(
        result.get("executed_binary_sha256")
        or result.get("proc_exe_sha256")
        or identity.get("proc_exe_sha256")
        or identity.get("executed_binary_sha256")
        or exact.get("exe_sha256")
        or meta_exact.get("exe_sha256")
        or ""
    )
    proc_path = str(
        result.get("proc_exe_realpath")
        or identity.get("proc_exe_realpath")
        or exact.get("exe_realpath")
        or meta_exact.get("exe_realpath")
        or ""
    )
    return downloaded, proc_sha, proc_path


def refusal_retirement(result: Mapping[str, Any]) -> tuple[bool, bool, dict[str, Any]]:
    """Extract the pre-creation refusal retirement signal (issue #138).

    ``render-job.sh`` writes the structured refusal from
    ``render_lifecycle.build_exact_artifact_refusal_result`` to the result
    file when the exact-artifact gate refuses before any worker exists.
    That record carries ``permanent`` (redispatch without a material
    premise change refuses identically), ``superseded`` (the pinned
    contract is retired on purpose), and ``successor`` (the immutable
    replacement artifact mapping, empty when not superseded).

    ``classify`` must propagate these three fields so the durable
    qualification comment carries the retirement signal machine-readably
    instead of forcing triage/schedulers to scrape the log line. Never
    raises: unparsable input means "no retirement signal", never a
    classification failure.
    """
    try:
        if not isinstance(result, Mapping):
            return False, False, {}
        try:
            permanent = bool(result.get("permanent"))
        except Exception:
            permanent = False
        try:
            superseded = bool(result.get("superseded"))
        except Exception:
            superseded = False
        try:
            successor = result.get("successor")
            successor = dict(successor) if isinstance(successor, Mapping) else {}
        except Exception:
            successor = {}
        return permanent, superseded, successor
    except Exception:
        return False, False, {}


def classify(
    result: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    artifact_id: int,
    expected_sha: str,
    execute_outcome: str,
    cleanup_outcome: str,
) -> dict[str, Any]:
    peak_values = [
        memory.get("max_memory_peak_bytes"),
        memory.get("max_memory_current_bytes"),
    ]
    peak = max(
        [int(v) for v in peak_values if isinstance(v, int) and not isinstance(v, bool)],
        default=None,
    )
    headroom = LIMIT_BYTES - peak if isinstance(peak, int) else None
    pressure = memory.get("memory_pressure")
    pressure = pressure if isinstance(pressure, Mapping) else {}

    downloaded_sha, proc_sha, proc_path = nested_identity(result)
    identity_ok = (
        bool(expected_sha)
        and downloaded_sha == expected_sha
        and proc_sha == expected_sha
        and proc_path.startswith("/")
    )

    max_delta = event_delta(memory, "max")
    oom_delta = event_delta(memory, "oom")
    oom_kill_delta = event_delta(memory, "oom_kill")
    instance_changed = bool(memory.get("instance_changed"))

    memory_failure = (
        oom_delta > 0
        or oom_kill_delta > 0
        or bool(pressure.get("verdict"))
        or (isinstance(peak, int) and peak > MAX_PEAK_BYTES)
    )

    status = str(result.get("status") or "")
    exit_code = result.get("exit_code")
    changes = result.get("changes")
    has_changes = isinstance(changes, list) and bool(changes)
    verification = result.get("verification")
    verification_ok = isinstance(verification, Mapping) and verification.get("status") == "passed"
    coding_ok = status == "succeeded" and exit_code in (0, None) and (has_changes or verification_ok)

    if cleanup_outcome != "success":
        classification = "infrastructure"
        subtype = "cleanup"
        reason = "mandatory service deletion/absence verification failed"
    elif not identity_ok:
        classification = "infrastructure"
        subtype = "identity"
        reason = "downloaded/materialized and /proc executable identity is not proven against the pinned SHA"
    elif memory_failure:
        classification = "memory"
        subtype = "service-memory"
        reason = "whole-service memory violates the precommitted 32 MiB headroom/OOM contract"
    elif execute_outcome != "success":
        classification = "infrastructure"
        subtype = "execution"
        reason = "Render execution failed without memory-pressure evidence"
    elif not coding_ok:
        classification = "correctness"
        subtype = "coding"
        reason = "real coding task or deterministic verification did not pass"
    elif instance_changed:
        classification = "infrastructure"
        subtype = "instance-replacement"
        reason = "worker instance changed without a memory-pressure verdict"
    elif not isinstance(peak, int):
        classification = "infrastructure"
        subtype = "telemetry"
        reason = "whole-service peak memory evidence is missing"
    elif max_delta > 0:
        classification = "memory"
        subtype = "cgroup-max"
        reason = "memory.events max increased, so the pass does not satisfy the clean headroom contract"
    else:
        classification = "pass"
        subtype = "qualified"
        reason = "identity, coding result, cleanup and memory headroom all passed"

    payload: dict[str, Any] = {
        "schema": "runtime-lab-qualification-result/v1",
        "kind": "render",
        "classification": classification,
        "subtype": subtype,
        "reason": reason,
        "artifact_id": artifact_id,
        "binary_sha256": expected_sha,
        "downloaded_binary_sha256": downloaded_sha,
        "executed_binary_sha256": proc_sha,
        "proc_exe_realpath": proc_path,
        "peak_bytes": peak,
        "headroom_bytes": headroom,
        "limit_bytes": LIMIT_BYTES,
        "min_headroom_bytes": MIN_HEADROOM_BYTES,
        "memory_events": {
            "max": max_delta,
            "oom": oom_delta,
            "oom_kill": oom_kill_delta,
        },
        "memory_pressure": pressure,
        "instance_changed": instance_changed,
        "instance_ids": memory.get("instance_ids") or [],
        "execute_outcome": execute_outcome,
        "cleanup_outcome": cleanup_outcome,
        "coding_status": status,
        "coding_exit_code": exit_code,
        "changes_count": len(changes) if isinstance(changes, list) else 0,
        "verification": verification if isinstance(verification, Mapping) else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["fingerprint"] = hashlib.sha256(raw).hexdigest()
    # Retirement propagation (issue #138): the refusal record's
    # permanent/superseded/successor fields ride the durable qualification
    # payload so triage, the fingerprint-dedup check, and the
    # qualification chain can see a retired contract without scraping log
    # text. They are attached AFTER the fingerprint is computed on purpose:
    # the fingerprint inputs stay byte-identical to the pre-#138 shape, so
    # the already-posted marker for the retired #110 contract
    # (17aee62ac5b68966042acecc14b0f68000374241b7943d1041afad37d3d0c32f)
    # keeps deduping the next identical redispatch instead of minting
    # another repair for the same failure mode.
    permanent, superseded, successor = refusal_retirement(result)
    payload["permanent"] = permanent
    payload["superseded"] = superseded
    payload["successor"] = successor
    return payload


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--result", required=True)
    p.add_argument("--memory", required=True)
    p.add_argument("--artifact-id", type=int, required=True)
    p.add_argument("--binary-sha256", required=True)
    p.add_argument("--execute-outcome", required=True)
    p.add_argument("--cleanup-outcome", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    payload = classify(
        load(args.result),
        load(args.memory),
        artifact_id=args.artifact_id,
        expected_sha=args.binary_sha256,
        execute_outcome=args.execute_outcome,
        cleanup_outcome=args.cleanup_outcome,
    )
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
    print(json.dumps(payload, sort_keys=True))
    print("classification=" + payload["classification"])
    print("subtype=" + payload["subtype"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
