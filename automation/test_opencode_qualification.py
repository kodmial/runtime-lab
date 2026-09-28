"""Offline tests for the issue-#81 qualification gate helper."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import opencode_qualification as q


def _measurements(**overrides):
    base = {
        "fork_commit_sha": "9000e7fc8d96c845512f7c73122431418a71d4e4",
        "upstream_baseline_revision": "75e1e7ae310dc36c86c920e8997d0e1181e24a88",
        "artifact_sha256": "ab" * 32,
        "entrypoint": "RUNNER_ALLOW_RUNTIME_INSTALL=0 python -m automation.runner_server",
        "config": {"pure": True},
        "environment": {"plan": "free", "region": "oregon"},
        "memory_limit_bytes": 512 * 1024 * 1024,
        "memory_current_bytes": 100 * 1024 * 1024,
        "memory_peak_bytes": 400 * 1024 * 1024,
        "memory_events": {"high": 0, "max": 0, "oom_kill": 0},
        "swap_current_bytes": 0,
        "swap_max_bytes": 0,
        "process_rss_kb": 22000,
        "process_hwm_kb": 23000,
        "instance_ids": ["abc123"],
        "instance_changed": False,
        "restart_transitions": [],
        "duration_seconds": 123.4,
        "opencode_exit_code": 0,
        "model": "opencode/muse-spark-1.3-contributor-free",
        "provider": "opencode",
        "cleanup_verified": True,
    }
    base.update(overrides)
    return base


def test_matrix_has_five_workloads_with_unique_ids():
    assert len(q.QUALIFICATION_MATRIX) == 5
    ids = q.matrix_ids()
    assert len(set(ids)) == 5
    assert set(ids) == {
        "q1-small-edit",
        "q2-search-multi-edit",
        "q3-large-output",
        "q4-fail-fix-rerun",
        "q5-fresh-sessions",
    }


def test_profiles_cover_all_comparison_artifacts():
    assert q.profile_ids() == [
        "upstream-baseline",
        "config-only",
        "source-stripped",
        "bounded-fork",
    ]
    pending = {p["id"] for p in q.COMPARISON_PROFILES if p["status"] == "pending"}
    assert pending == {"config-only", "source-stripped", "bounded-fork"}


def test_required_fields_cover_issue_measurements():
    fields = q.required_measurement_fields()
    for expected in (
        "fork_commit_sha", "upstream_baseline_revision", "artifact_sha256",
        "entrypoint", "config", "environment", "memory_limit_bytes",
        "memory_current_bytes", "memory_peak_bytes", "memory_events",
        "swap_current_bytes", "swap_max_bytes", "process_rss_kb",
        "process_hwm_kb", "instance_ids", "instance_changed",
        "restart_transitions", "duration_seconds", "opencode_exit_code",
        "model", "provider", "cleanup_verified",
    ):
        assert expected in fields


def test_build_record_rejects_unknown_ids_and_missing_fields():
    with pytest.raises(ValueError):
        q.build_run_record(
            workload_id="nope", profile_id="upstream-baseline",
            measurements=_measurements(),
        )
    with pytest.raises(ValueError):
        q.build_run_record(
            workload_id="q1-small-edit", profile_id="nope",
            measurements=_measurements(),
        )
    slim = _measurements()
    del slim["memory_peak_bytes"]
    with pytest.raises(ValueError):
        q.build_run_record(
            workload_id="q1-small-edit", profile_id="upstream-baseline",
            measurements=slim,
        )


def test_valid_record_passes_gate():
    record = q.build_run_record(
        workload_id="q1-small-edit", profile_id="upstream-baseline",
        measurements=_measurements(),
    )
    assert q.validate_run_measurement(record) == []


def test_gate_fails_closed_on_oom_cleanup_exit_and_replacement():
    assert any("OOM-killed" in e for e in q.validate_run_measurement(
        {"schema": q.QUALIFICATION_SCHEMA,
         "measurements": _measurements(
             memory_events={"oom_kill": 1, "max": 5})}))
    assert any("cleanup_verified" in e for e in q.validate_run_measurement(
        {"schema": q.QUALIFICATION_SCHEMA,
         "measurements": _measurements(cleanup_verified=False)}))
    assert any("exit_code" in e for e in q.validate_run_measurement(
        {"schema": q.QUALIFICATION_SCHEMA,
         "measurements": _measurements(opencode_exit_code=137)}))
    assert any("replacement" in e for e in q.validate_run_measurement(
        {"schema": q.QUALIFICATION_SCHEMA,
         "measurements": _measurements(
             instance_ids=["a", "b"], instance_changed=True,
             restart_transitions=[{"from_instance_id": "a"}])}))
    assert any("missing measurement field" in e for e in q.validate_run_measurement(
        {"schema": q.QUALIFICATION_SCHEMA, "measurements": {}}))


def test_budget_reports_exact_gap_without_masking():
    ok = q.evaluate_budget(400 * 1024 * 1024)
    assert ok["passes_target"] and ok["passes_limit"]
    assert ok["gap_to_target_bytes"] < 0
    over = q.evaluate_budget(629383168)  # ~600.2 MiB host baseline peak
    assert not over["passes_target"] and not over["passes_limit"]
    assert over["gap_to_target_bytes"] == 629383168 - 450 * 1024 * 1024
    assert over["gap_to_limit_bytes"] == 629383168 - 512 * 1024 * 1024
    with pytest.raises(ValueError):
        q.evaluate_budget(0)
    with pytest.raises(ValueError):
        q.evaluate_budget(-5)


def test_before_after_table_renders_exact_gaps():
    table = q.render_before_after_table(q.baseline_before_rows())
    assert table.startswith("| Profile |")
    assert "over by 150.2 MiB" in table  # host peak vs 450 MiB target
    assert "over by 88.2 MiB" in table  # host peak vs 512 MiB limit
    with pytest.raises(ValueError):
        q.render_before_after_table([{"profile": "x"}])


def test_production_start_command_matches_lifecycle_default():
    from render_lifecycle import build_create_service_payload
    payload = build_create_service_payload(name="probe", owner_id="owner-1")
    start = payload["serviceDetails"]["envSpecificDetails"]["startCommand"]
    assert q.production_start_command() == start
    fingerprint = q.production_config_fingerprint()
    assert fingerprint["deploy_artifact"] == ".opencode-bin/opencode"
    assert "SHA-256" in fingerprint["fork_selection"]


def test_baseline_rows_are_evidence_backed():
    rows = q.baseline_before_rows()
    assert len(rows) >= 3
    for row in rows:
        assert row["peak_bytes"] > 0
        assert "issue-52" in row["provenance"]
    dom = q.dominant_memory_class()
    assert "WKFastMalloc" in dom["class"]
    assert "issue-56" in dom["provenance"]
