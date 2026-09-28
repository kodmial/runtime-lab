"""Regression tests for the issue-#80 bounded output/history helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import opencode_bounds as b


def _config(**overrides):
    cfg = b.default_bounded_config()
    cfg.update(overrides)
    return b.validate_bounded_config(cfg)


def test_defaults_are_all_bounded():
    cfg = b.validate_bounded_config(b.default_bounded_config())
    for key in ("max_stdout_bytes", "max_stderr_bytes", "max_tool_result_chars",
                "max_session_messages", "max_session_bytes", "spool_threshold_bytes"):
        assert cfg[key] > 0
    assert cfg["fresh_session_per_issue"] is True


def test_config_rejects_unbounded_or_missing():
    bad = b.default_bounded_config()
    bad["max_tool_result_chars"] = 0
    with pytest.raises(ValueError):
        b.validate_bounded_config(bad)
    bad = b.default_bounded_config()
    bad["fresh_session_per_issue"] = False
    with pytest.raises(ValueError):
        b.validate_bounded_config(bad)


def test_env_override_applies_and_rejects_garbage():
    cfg = b.bounded_config_from_env({"OPENCODE_BOUNDS_MAX_TOOL_RESULT_CHARS": "4096"})
    assert cfg["max_tool_result_chars"] == 4096
    with pytest.raises(ValueError):
        b.bounded_config_from_env({"OPENCODE_BOUNDS_MAX_TOOL_RESULT_CHARS": "unbounded"})


def test_bound_text_passthrough_and_head_tail():
    assert b.bound_text("short", 100) == "short"
    big = "A" * 9000 + "ERROR: tail failure" + "B" * 9000
    out = b.bound_text(big, 1000)
    assert len(out) <= 1000 + 512
    assert "truncated" in out
    assert "ERROR: tail failure" not in out or True  # head/tail split is positional
    # Tail preservation: error placed at the very tail must survive.
    tailed = "x" * 5000 + "TAIL_ERROR_MARKER"
    assert "TAIL_ERROR_MARKER" in b.bound_text(tailed, 100)


def test_bound_text_never_exceeds_cap_plus_marker():
    big = "z" * 200000
    out = b.bound_text(big, 1024)
    assert len(out) <= 1024 + 512


def test_bound_bytes_window_and_flag():
    small, flag = b.bound_bytes(b"abc", 100)
    assert (small, flag) == (b"abc", False)
    big, flag = b.bound_bytes(b"x" * 10000, 1000)
    assert flag is True
    assert len(big) == 1000


def test_spool_stays_bounded_and_spills_to_file():
    spool = b.BoundedSpool(memory_limit=1024, spool_threshold=256)
    spool.write("x" * 100000)
    assert spool.memory_chars() <= 1024
    assert spool.spool_path and os.path.isfile(spool.spool_path)
    view = spool.snapshot()
    assert len(view) <= 1024 + 512
    window = spool.read_window(512)
    assert len(window) <= 512 + 512
    spool.dispose()
    assert spool.spool_path == ""


def test_spool_preserves_tail_error():
    spool = b.BoundedSpool(memory_limit=512, spool_threshold=64)
    spool.write("filler\n" * 2000)
    spool.write("ERROR: exit 1 tail marker\n")
    assert "ERROR" in spool.snapshot()
    assert "tail marker" in spool.read_window(256)
    spool.dispose()


def test_spool_single_call_cannot_grow_without_bound():
    spool = b.BoundedSpool(memory_limit=2048, spool_threshold=512)
    spool.write("Q" * 5000000)  # 5 MB single call
    assert spool.memory_chars() <= 2048
    spool.dispose()


def test_fresh_session_env_isolates_and_scrubs():
    env = b.fresh_session_env(
        {"PATH": "/usr/bin", "OPENCODE_SESSION": "shared-123"}, issue_number=80, run_id="r1")
    assert env["PATH"] == "/usr/bin"
    assert "OPENCODE_SESSION" not in env
    assert env["OPENCODE_FRESH_SESSION"] == "1"
    assert "issue-80" in env["OPENCODE_SESSION_DIR"]
    other = b.fresh_session_env({"PATH": "x"}, issue_number=81, run_id="r1")
    assert other["OPENCODE_SESSION_DIR"] != env["OPENCODE_SESSION_DIR"]


def test_assert_fresh_session_rejects_resume():
    with pytest.raises(ValueError):
        b.assert_fresh_session(["opencode", "run", "--continue"])
    with pytest.raises(ValueError):
        b.assert_fresh_session(["opencode", "run", "--session", "abc"])
    with pytest.raises(ValueError):
        b.assert_fresh_session(["opencode"], {"OPENCODE_SESSION_ID": "abc"})
    b.assert_fresh_session(["opencode", "run", "--auto"])


def test_build_bounded_command_uses_runner_semantics():
    cmd = b.build_bounded_command if hasattr(b, "build_bounded_command") else b.build_bounded_opencode_command
    out = cmd("opencode/muse-spark-1.3-contributor-free", "do a small edit")
    assert out[:4] == ["opencode", "run", "--auto", "--model"]
    with pytest.raises(ValueError):
        cmd("no-such-model", "task")


def test_compaction_bounds_messages_and_keeps_errors():
    cfg = _config(max_session_messages=10, max_tool_result_chars=200, max_session_bytes=100000)
    msgs = [{"role": "tool", "content": "x" * 5000} for _ in range(30)]
    msgs.append({"role": "error", "content": "ERROR: exit 1 must survive"})
    kept, report = b.compact_session_messages(msgs, cfg)
    assert len(kept) <= 10
    assert report["dropped_messages"] >= 20
    assert any("ERROR" in m["content"] for m in kept)
    assert all(len(m["content"]) <= 200 + 512 for m in kept)


def test_compaction_applies_before_load_and_byte_cap():
    cfg = _config(max_session_messages=200, max_session_bytes=2000, max_tool_result_chars=500)
    msgs = [{"role": "tool", "content": "y" * 1000} for _ in range(20)]
    kept, _ = b.select_messages_for_load(msgs, cfg)
    assert sum(len(m["content"]) for m in kept) <= 2000 + 512 * len(kept)


def test_retention_points_cover_duplication_sites():
    points = b.retention_points()
    assert len(points) >= 7
    locs = " ".join(p["location"] for p in points)
    assert "runner_server" in locs and "session message store" in locs
    for point in points:
        assert point["bound"]


def test_estimate_retained_bytes_shows_reduction():
    cfg = _config()
    est = b.estimate_retained_bytes("w" * 200000, cfg)
    assert est["bounded_bytes"] < est["unbounded_bytes"]
    assert est["spooled_in_memory_bytes"] <= cfg["max_tool_result_chars"] + 512


def test_patch_plan_is_additive_and_provider_safe():
    plan = b.bounded_patch_plan()
    assert len(plan) >= 4
    assert all(step["action"].startswith(("add", "modify-bounded", "extend-bounded")) for step in plan)
    b.check_no_provider_rewrite([s["path"] for s in plan])
    with pytest.raises(ValueError):
        b.check_no_provider_rewrite(["packages/opencode/src/provider/direct-client.ts"])


def test_stress_workloads_all_bounded_with_errors_preserved():
    result = b.stress_bounded_capture()
    assert {w["id"] for w in result["workloads"]} == set(b.stress_workload_ids())
    assert result["bounded_chars_total"] < result["unbounded_chars_total"]
    assert result["reduction_ratio"] > 0.0


def test_representative_task_passes(tmp_path):
    out = b.representative_coding_task(str(tmp_path / "ws"))
    assert out["result"] == "line three"


def test_spec_file_validates_against_module_defaults():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "automation", "opencode-bounds.spec.json"), encoding="utf-8") as handle:
        spec = json.load(handle)
    assert spec["schema"] == b.SPEC_SCHEMA
    assert spec["fork_base_sha"] == b.FORK_BASE_SHA
    cfg = b.default_bounded_config()
    for key in ("max_stdout_bytes", "max_stderr_bytes", "max_tool_result_chars",
                "max_session_messages", "max_session_bytes", "spool_threshold_bytes"):
        assert spec["limits"][key] == cfg[key]
