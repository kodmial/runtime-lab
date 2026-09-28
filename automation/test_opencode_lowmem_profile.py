"""Regression tests for the config-only low-memory profile (issue #78).

Offline, stdlib-first, no network or git mutations. Proves the Definition
of Done slices enforceable without a live Render worker:

- the profile uses only fork/source-confirmed switches (accepted list is
  non-empty and stable; every rejected switch records reason + overturn);
- the headless config preserves the required coding loop and confinement;
- the command shape stays fresh-session, pure, and provider-allowlisted;
- validation fails closed on every rejected switch and on stale sessions;
- the fingerprint is stable and the files round-trip;
- the representative coding task still succeeds under the profile;
- the A/B table renders exact deltas.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import opencode_lowmem_profile as p
import opencode_qualification as q


def _triple(workspace="/tmp/lowmem-test-ws"):
    config = p.lowmem_config()
    env = p.lowmem_env(workspace)
    cmd = p.build_lowmem_command(
        "opencode/muse-spark-1.3-contributor-free", "do the task"
    )
    return config, env, cmd


def test_accepted_settings_are_source_grounded():
    ids = p.accepted_ids()
    assert len(ids) == len(set(ids)) >= 10
    for entry in p.ACCEPTED_SETTINGS:
        assert entry["source"] and entry["rationale"]
    keys = {entry["key"] for entry in p.ACCEPTED_SETTINGS}
    for expected in (
        "BUN_OPTIONS",
        "--pure / OPENCODE_PURE=1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "mcp",
        "lsp={} + OPENCODE_DISABLE_LSP_DOWNLOAD=1",
        "formatter",
        "share=disabled + OPENCODE_DISABLE_SHARE=1",
        "autoupdate=false + OPENCODE_DISABLE_AUTOUPDATE=1",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "enabled_providers",
    ):
        assert expected in keys


def test_rejected_settings_record_reason_and_overturn():
    ids = p.rejected_ids()
    assert set(ids) == {
        "snapshot-false",
        "filewatcher-disable",
        "autocompact-disable",
        "project-config-disable",
        "models-path-stub",
    }
    for entry in p.REJECTED_SETTINGS:
        assert entry["reason"] and entry["overturn"]


def test_config_is_minimal_headless_and_preserves_loop():
    config = p.lowmem_config()
    assert config["mcp"] == {}
    assert config["lsp"] == {}
    assert config["formatter"] is False
    assert config["share"] == "disabled"
    assert config["autoupdate"] is False
    assert config["enabled_providers"] == ["opencode"]
    assert config["plugin"] == []
    assert "snapshot" not in config
    permission = config["permission"]["bash"]
    assert permission["*"] == "allow"
    assert permission["git *"] == "deny"
    assert permission["git status"] == "allow"
    assert permission["git diff"] == "allow"


def test_env_overrides_are_complete_and_workspace_scoped(tmp_path):
    workspace = str(tmp_path / "ws")
    env = p.lowmem_env(workspace)
    for key in (
        "BUN_OPTIONS",
        "OPENCODE_PURE",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
        "OPENCODE_DISABLE_SHARE",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_DB",
        "OPENCODE_CONFIG_CONTENT",
    ):
        assert env[key]
    assert env["BUN_OPTIONS"] == "--smol"
    assert env["OPENCODE_DB"].startswith(workspace)
    assert env["OPENCODE_DB"].endswith("session.db")
    assert workspace not in p.lowmem_env(str(tmp_path / "other"))["OPENCODE_DB"]
    with pytest.raises(ValueError):
        p.lowmem_env("")


def test_command_is_pure_fresh_session_allowlisted():
    cmd = p.build_lowmem_command(
        "opencode/space-bunny-free", "do the task"
    )
    assert cmd[:5] == ["opencode", "run", "--pure", "--auto", "--model"]
    assert "--share" not in cmd and "--attach" not in cmd
    with pytest.raises(ValueError):
        p.build_lowmem_command("anthropic/claude-x", "task")
    with pytest.raises(ValueError):
        p.build_lowmem_command("opencode/m", "")
    with pytest.raises(ValueError):
        p.build_lowmem_command("opencode/m", "task", opencode_bin="")


def test_validation_accepts_profile_and_rejects_violations(tmp_path):
    config, env, cmd = _triple(str(tmp_path / "ws"))
    assert p.validate_profile(config, env, cmd) == []
    bad_config = dict(config)
    bad_config["snapshot"] = False
    assert any("snapshot" in e for e in p.validate_profile(bad_config, env, cmd))
    bad_cmd = list(cmd) + ["--continue"]
    assert any("session" in e for e in p.validate_profile(config, env, bad_cmd))
    bad_cmd2 = [part for part in cmd if part != "--pure"]
    assert any("--pure" in e for e in p.validate_profile(config, env, bad_cmd2))
    bad_env = dict(env)
    del bad_env["BUN_OPTIONS"]
    assert any("BUN_OPTIONS" in e for e in p.validate_profile(config, bad_env, cmd))


def test_fingerprint_is_stable_and_pinned():
    config, env, _ = _triple()
    first = p.profile_fingerprint(config, env)
    second = p.profile_fingerprint(p.lowmem_config(), p.lowmem_env("/tmp/lowmem-test-ws"))
    assert first == second
    assert len(first["config_sha256"]) == 64
    assert first["pinned_opencode_version"] == "1.18.33"
    assert first["fork_base_sha"] == "9000e7fc8d96c845512f7c73122431418a71d4e4"


def test_profile_files_round_trip_without_secrets(tmp_path):
    workspace = str(tmp_path / "ws")
    outdir = str(tmp_path / "profile")
    paths = p.write_profile_files(outdir, workspace)
    with open(paths["opencode_json"], encoding="utf-8") as handle:
        assert json.load(handle) == p.lowmem_config()
    with open(paths["opencode_env"], encoding="utf-8") as handle:
        text = handle.read()
    assert "BUN_OPTIONS=--smol" in text
    for secret in ("TAP_PAT", "GH_TOKEN", "GITHUB_TOKEN", "sk-"):
        assert secret not in text


def test_ensure_session_db_dir_is_idempotent(tmp_path):
    workspace = str(tmp_path / "ws")
    first = p.ensure_session_db_dir(workspace)
    assert first == p.session_db_path(workspace)
    assert os.path.isdir(os.path.dirname(first))
    assert p.ensure_session_db_dir(workspace) == first


def test_representative_coding_task_succeeds(tmp_path):
    result = p.representative_coding_task(str(tmp_path / "ws"))
    assert result["result"] == "42"
    with pytest.raises(ValueError):
        p.representative_coding_task("")


def test_budgets_match_qualification_gate():
    assert p.TARGET_PEAK_KB * 1024 == q.TARGET_PEAK_BYTES
    assert p.HARD_LIMIT_KB * 1024 == q.HARD_LIMIT_BYTES
    assert p.REPRESENTATIVE_WORKLOAD["id"] in q.matrix_ids()


def test_ab_table_renders_exact_deltas():
    rows = [
        {"arm": "bare-baseline", "peak_tree_kb": 540000,
         "wall_seconds": 1.6, "exit_code": 1, "provenance": "probe"},
        {"arm": "cumulative", "peak_tree_kb": 500000,
         "wall_seconds": 1.5, "exit_code": 1, "provenance": "probe"},
    ]
    table = p.render_ab_table(rows)
    assert "| Arm |" in table
    assert "-40000 kB" in table
    assert str(p.BASELINE_HOST_PEAK_KB) in table
    with pytest.raises(ValueError):
        p.render_ab_table([{"arm": "broken"}])
