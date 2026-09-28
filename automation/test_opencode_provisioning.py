"""Regression tests for deterministic OpenCode provisioning (issue #52).

Covers the Definition of Done without network access or secrets:
- the pinned binary resolves from a deterministic deploy-artifact path and
  never depends on build-time $HOME equaling runtime $HOME;
- production workers disable runtime network installation and fail
  readiness/startup when the binary is absent or non-executable;
- no installer runs inside a job when runtime installation is disabled;
- the Render payload carries the strict start command while keeping the
  pinned build step;
- the memory benchmark harness emits rerunnable machine-readable output.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import opencode_runner as opencode_module  # noqa: E402
from opencode_runner import (  # noqa: E402
    OPENCODE_DEPLOY_BIN_SUBPATH,
    OPENCODE_PINNED_VERSION,
    deploy_binary_candidates,
    find_opencode_binary,
    opencode_runtime_install_allowed,
    probe_opencode_readiness,
)
from render_lifecycle import build_create_service_payload  # noqa: E402
from runner_server import JobManager  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_executable(path: str, version_output: str = "1.18.33\n") -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\necho '%s'\n" % version_output.strip())
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_deploy_artifact_path_is_repo_relative_and_home_independent():
    candidates = deploy_binary_candidates()
    assert candidates
    assert any(
        candidate.endswith(OPENCODE_DEPLOY_BIN_SUBPATH) for candidate in candidates
    )
    # At least one candidate lives under the source tree, never under $HOME.
    assert any(str(REPO_ROOT) in candidate for candidate in candidates)
    for candidate in candidates:
        assert os.path.expanduser("~") not in candidate or str(REPO_ROOT) in candidate


def test_runtime_install_flag_parsing(monkeypatch):
    for var in ("RUNNER_ALLOW_RUNTIME_INSTALL", "OPENCODE_ALLOW_RUNTIME_INSTALL"):
        monkeypatch.delenv(var, raising=False)
    assert opencode_runtime_install_allowed(None) is True
    for truthy in ("1", "true", "yes", "on", "enabled", ""):
        # Empty means "not configured" only via env lookup; explicit empty
        # string falls back to the default-allow branch in callers, while a
        # direct value of "" here is treated as allow.
        assert opencode_runtime_install_allowed(truthy) is True
    for falsy in ("0", "false", "no", "off", "disabled", "FALSE", " 0 "):
        assert opencode_runtime_install_allowed(falsy) is False


def test_explicit_override_wins_when_executable(tmp_path, monkeypatch):
    fake = _make_executable(str(tmp_path / "opencode"))
    monkeypatch.setenv("RUNNER_OPENCODE_BIN", fake)
    assert find_opencode_binary() == fake


def test_missing_override_falls_through_to_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_OPENCODE_BIN", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    found = find_opencode_binary()
    # Must not return the missing override; PATH/HOME discovery decides.
    assert found != str(tmp_path / "does-not-exist")


def test_probe_readiness_reports_version_for_executable(tmp_path):
    fake = _make_executable(str(tmp_path / "opencode"), "9.9.9\n")
    ready, resolved, version = probe_opencode_readiness(fake)
    assert ready is True
    assert resolved == fake
    assert version == "9.9.9"


def test_probe_readiness_fails_closed_for_missing_binary(tmp_path):
    ready, resolved, detail = probe_opencode_readiness(
        str(tmp_path / "does-not-exist")
    )
    assert ready is False
    assert detail


def test_probe_readiness_fails_for_non_executable(tmp_path):
    plain = str(tmp_path / "opencode")
    with open(plain, "w", encoding="utf-8") as handle:
        handle.write("not executable\n")
    ready, _, detail = probe_opencode_readiness(plain)
    assert ready is False
    assert "non-executable" in detail or "absent" in detail


class NoInstallRunner:
    """Command runner that records calls and fails any installer use."""

    def __init__(self):
        self.calls = []

    def run(self, cmd, cwd, timeout):
        self.calls.append(list(cmd))
        from runner_server import CommandResult

        if len(cmd) >= 2 and cmd[0] == "sh" and "opencode.ai/install" in cmd[1]:
            raise AssertionError("network installer must not run in strict mode")
        return CommandResult(returncode=0, stdout="fake-ok", stderr="")


def test_strict_mode_is_not_ready_without_binary_and_never_installs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RUNNER_ALLOW_RUNTIME_INSTALL", "0")
    monkeypatch.delenv("RUNNER_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    import runner_server as runner_module

    monkeypatch.setattr(runner_module, "find_opencode_binary", lambda: None)
    # Probe must also miss so readiness is genuinely absent.
    monkeypatch.setattr(
        runner_module, "probe_opencode_readiness",
        lambda binary=None, timeout=20.0: (False, "", "opencode binary not found"),
    )
    runner = NoInstallRunner()
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        opencode_bin="opencode",
    )
    assert manager.allow_runtime_install is False
    assert manager.ready is False
    assert manager.health_snapshot()["ready"] is False
    with pytest.raises(FileNotFoundError, match="runtime installation is disabled"):
        manager._ensure_opencode_binary(str(tmp_path), 5.0)
    assert runner.calls == []


def test_strict_mode_is_ready_when_binary_present(tmp_path, monkeypatch):
    fake = _make_executable(str(tmp_path / "opencode"), "1.18.33\n")
    monkeypatch.setenv("RUNNER_ALLOW_RUNTIME_INSTALL", "0")
    monkeypatch.delenv("RUNNER_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    import runner_server as runner_module

    monkeypatch.setattr(runner_module, "find_opencode_binary", lambda: fake)
    runner = NoInstallRunner()
    manager = JobManager(
        workspace_root=str(tmp_path / "ws2"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        opencode_bin="opencode",
    )
    assert manager.ready is True
    snapshot = manager.health_snapshot()
    assert snapshot["ready"] is True
    assert snapshot["opencode_version"] == "1.18.33"
    assert snapshot["allow_runtime_install"] is False


def test_default_mode_still_allows_lazy_provisioning(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNER_ALLOW_RUNTIME_INSTALL", raising=False)
    monkeypatch.delenv("OPENCODE_ALLOW_RUNTIME_INSTALL", raising=False)
    manager = JobManager(
        workspace_root=str(tmp_path / "ws3"),
        job_timeout_seconds=30.0,
        command_runner=NoInstallRunner(),
        opencode_bin="/fake/opencode",
    )
    assert manager.allow_runtime_install is True
    assert manager.ready is True


def test_render_payload_disables_runtime_install_and_keeps_pinned_build():
    payload = build_create_service_payload(name="n", owner_id="o")
    details = payload["serviceDetails"]["envSpecificDetails"]
    assert "RUNNER_ALLOW_RUNTIME_INSTALL=0" in details["startCommand"]
    assert "python -m automation.runner_server" in details["startCommand"]
    assert "automation/install-opencode.sh" in details["buildCommand"]
    assert "free" in payload["serviceDetails"]["plan"]


def test_install_script_copies_deterministic_artifact():
    text = (REPO_ROOT / "automation" / "install-opencode.sh").read_text(
        encoding="utf-8"
    )
    assert ".opencode-bin/opencode" in text
    assert 'test -x "$DEPLOY_DIR/opencode"' in text
    assert OPENCODE_PINNED_VERSION in text
    assert "--version" in text


def test_memory_benchmark_emits_machine_readable_result(tmp_path):
    output = str(tmp_path / "mem.json")
    proc = subprocess.run(
        [sys.executable, "automation/memory_benchmark.py",
         "--run-id", "test-provisioning", "--output", output],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    with open(output, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["schema"] == "runtime-lab-memory-benchmark/v1"
    assert data["issue"] == 52
    assert data["pinned_opencode_version"] == OPENCODE_PINNED_VERSION
    assert "variant_b_standalone" in data["deterministic"]
    assert "stress_512m_no_swap" in data["deterministic"] or "stress_512m_no_swap" in data
    assert isinstance(data["budget"]["fits_512m"], bool)
