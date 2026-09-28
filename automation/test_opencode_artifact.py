"""Regression tests for parallel OpenCode experiment artifacts (issue #86).

Offline, stdlib-first plus pytest, no network or secrets. Verifies the
Definition of Done without building a real Bun binary:

- checksum/fingerprint verification (manifest + SHA-256, tamper fails);
- wrong/missing artifact fails readiness (manager not ready, 503 fields,
  ``_ensure_opencode_binary`` raises without touching the installer);
- arbitrary approved fork refs build independently (explicit ref validation,
  SHA-keyed ids, Bun compile/release path, immutable GitHub reference);
- concurrent experiment artifacts remain distinguishable (two ids coexist,
  fingerprints differ, selection resolves each);
- upstream baseline mode still works (empty selection, default payload);
- no runtime installer/download fallback occurs (experiment mode never runs
  ``opencode.ai/install``; artifact scripts contain no installer).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import opencode_artifact as artifact
from opencode_artifact import (
    artifact_id_from_sha,
    artifact_paths,
    build_artifact_manifest,
    build_command_fragment,
    bun_build_commands,
    clone_commands,
    compute_file_sha256,
    fingerprint_string,
    immutable_artifact_url,
    resolve_artifact_selection,
    start_env_prefix,
    validate_fork_ref,
    validate_immutable_ref,
    verify_artifact_file,
    verify_experiment_selection,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SHA_A = "a" * 40
SHA_B = "b" * 40
UPSTREAM = "c" * 40


def _clear_artifact_env(monkeypatch):
    for var in (
        "OPENCODE_ARTIFACT_REF",
        "OPENCODE_EXPECTED_FORK_SHA",
        "OPENCODE_EXPECTED_SHA256",
        "OPENCODE_ARTIFACT_ID",
        "OPENCODE_ARTIFACT_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_fake_binary(path: str, version: str = "1.18.33") -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\necho '%s'\n" % version)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_test_artifact(fork_sha: str, version: str = "1.18.33"):
    """Create a real on-disk artifact under the repo root; return paths.

    Uses content-addressed ``.opencode-artifacts/<exp-id>/`` layout so the
    test exercises the same coexistence path as production. Callers must
    remove the created directory afterwards.
    """
    artifact_id = artifact_id_from_sha(fork_sha)
    binary_path, manifest_path = artifact_paths(str(REPO_ROOT), artifact_id)
    os.makedirs(os.path.dirname(binary_path), exist_ok=True)
    _make_fake_binary(binary_path, version)
    digest = compute_file_sha256(binary_path)
    manifest = build_artifact_manifest(
        fork_ref=fork_sha,
        fork_commit_sha=fork_sha,
        upstream_base_revision=UPSTREAM,
        binary_sha256=digest,
        binary_bytes=os.path.getsize(binary_path),
        build_toolchain={"bun": "1.2.0", "os": "linux", "arch": "x64"},
        build_identity={"builder": "test-builder", "build_id": "test-86"},
    )
    with open(manifest_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return artifact_id, binary_path, manifest_path, manifest


def _remove_test_artifact(artifact_id: str) -> None:
    import shutil

    directory = REPO_ROOT / artifact.ARTIFACT_DIRNAME / artifact_id
    shutil.rmtree(str(directory), ignore_errors=True)


# -- fork-ref validation ----------------------------------------------------


def test_fork_ref_accepts_explicit_sha_branch_and_tag():
    assert validate_fork_ref(SHA_A) == SHA_A
    assert validate_fork_ref("abcd123") == "abcd123"
    assert validate_fork_ref("source-stripped-candidate") == "source-stripped-candidate"
    assert validate_fork_ref("exp/bounded-output") == "exp/bounded-output"
    assert validate_fork_ref("v1.18.33-exp1") == "v1.18.33-exp1"


def test_fork_ref_rejects_mutable_latest_and_empty():
    for bad in ("", "   ", "latest", "LATEST", "Latest", "stable", "head"):
        with pytest.raises(ValueError):
            validate_fork_ref(bad)
    with pytest.raises(ValueError):
        validate_fork_ref("has space")
    with pytest.raises(ValueError):
        validate_fork_ref("../escape")


def test_artifact_ids_are_sha_keyed_and_distinct():
    id_a = artifact_id_from_sha(SHA_A)
    id_b = artifact_id_from_sha(SHA_B)
    assert id_a != id_b
    assert id_a.startswith("exp-") and len(id_a) == 16
    with pytest.raises(ValueError):
        artifact_id_from_sha("latest")


# -- Bun compile/release path + immutable reference --------------------------


def test_bun_build_path_reuses_upstream_release_steps():
    steps = bun_build_commands(".")
    text = "\n".join(" ".join(step) for step in steps)
    assert ["bun", "install"] in steps
    assert "packages/opencode/script/build.ts" in text
    assert "latest" not in text.lower()
    assert artifact.release_asset_name() == "opencode-linux-x64.tar.gz"


def test_clone_commands_pin_explicit_ref_without_latest():
    cmds = clone_commands(SHA_A, "/tmp/dest-86")
    text = "\n".join(" ".join(cmd) for cmd in cmds)
    assert SHA_A in text
    assert "kodmial/opencode" in text
    assert "rev-parse" in text
    assert "latest" not in text.lower()
    with pytest.raises(ValueError):
        clone_commands("latest", "/tmp/dest-86")


def test_immutable_reference_is_github_backed_and_sha_pinned():
    url = immutable_artifact_url(SHA_A)
    assert url.startswith("https://github.com/kodmial/opencode/releases/download/")
    assert SHA_A[:12] in url
    assert "latest" not in url
    validate_immutable_ref(url, SHA_A)
    with pytest.raises(ValueError):
        validate_immutable_ref(
            "https://github.com/kodmial/opencode/releases/download/latest/x.tar.gz",
            SHA_A,
        )
    with pytest.raises(ValueError):
        validate_immutable_ref(url, SHA_B)


# -- manifest / fingerprint --------------------------------------------------


def test_manifest_records_full_fingerprint():
    manifest = build_artifact_manifest(
        fork_ref="exp/bounded-output",
        fork_commit_sha=SHA_A,
        upstream_base_revision=UPSTREAM,
        binary_sha256="d" * 64,
        binary_bytes=12345,
        build_toolchain={"bun": "1.2.0", "os": "linux", "arch": "x64"},
        build_identity={"builder": "builder-1", "build_id": "build-1"},
    )
    assert manifest["fork_commit_sha"] == SHA_A
    assert manifest["upstream_base_revision"] == UPSTREAM
    assert manifest["build_toolchain"] == {"bun": "1.2.0", "os": "linux", "arch": "x64"}
    assert manifest["artifact_id"] == artifact_id_from_sha(SHA_A)
    assert SHA_A[:12] in manifest["immutable_ref"]["asset_url"]
    fingerprint = fingerprint_string(manifest)
    assert fingerprint.startswith(SHA_A[:12] + ":")


def test_manifest_rejects_wrong_arch_and_bad_digest():
    with pytest.raises(ValueError):
        build_artifact_manifest(
            fork_ref=SHA_A,
            fork_commit_sha=SHA_A,
            upstream_base_revision=UPSTREAM,
            binary_sha256="d" * 64,
            binary_bytes=10,
            build_toolchain={"bun": "1.2.0", "os": "linux", "arch": "arm64"},
            build_identity={"builder": "b", "build_id": "i"},
        )
    with pytest.raises(ValueError):
        build_artifact_manifest(
            fork_ref=SHA_A,
            fork_commit_sha=SHA_A,
            upstream_base_revision=UPSTREAM,
            binary_sha256="not-a-digest",
            binary_bytes=10,
            build_toolchain={"bun": "1.2.0", "os": "linux", "arch": "x64"},
            build_identity={"builder": "b", "build_id": "i"},
        )


def test_checksum_verification_passes_and_tamper_fails(tmp_path):
    target = str(tmp_path / "opencode")
    _make_fake_binary(target)
    digest = compute_file_sha256(target)
    assert verify_artifact_file(target, digest) is None
    with open(target, "ab") as handle:
        handle.write(b"tamper")
    assert verify_artifact_file(target, digest) is not None
    assert verify_artifact_file(str(tmp_path / "missing"), digest) is not None


# -- selection ---------------------------------------------------------------


def test_empty_env_selects_upstream_baseline(monkeypatch):
    _clear_artifact_env(monkeypatch)
    selection = resolve_artifact_selection(dict(os.environ))
    assert selection["mode"] == "upstream-baseline"


def test_partial_experiment_request_fails_closed(monkeypatch):
    _clear_artifact_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_EXPECTED_SHA256", "d" * 64)
    with pytest.raises(ValueError):
        resolve_artifact_selection(dict(os.environ))


def test_explicit_ref_resolves_experiment_mode(monkeypatch):
    _clear_artifact_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_ARTIFACT_REF", SHA_A)
    monkeypatch.setenv("OPENCODE_EXPECTED_FORK_SHA", SHA_A)
    monkeypatch.setenv("OPENCODE_EXPECTED_SHA256", "d" * 64)
    selection = resolve_artifact_selection(dict(os.environ))
    assert selection["mode"] == "experiment"
    assert selection["artifact_id"] == artifact_id_from_sha(SHA_A)


# -- concurrent artifacts ----------------------------------------------------


def test_concurrent_artifacts_coexist_and_stay_distinguishable():
    created = []
    try:
        id_a, bin_a, _, manifest_a = _write_test_artifact(SHA_A)
        id_b, bin_b, _, manifest_b = _write_test_artifact(SHA_B, version="1.18.34")
        created = [id_a, id_b]
        assert id_a != id_b
        assert os.path.isfile(bin_a) and os.path.isfile(bin_b)
        assert manifest_a["binary_sha256"] != manifest_b["binary_sha256"]
        assert fingerprint_string(manifest_a) != fingerprint_string(manifest_b)
        # Selection resolves each artifact independently.
        for fork_sha, artifact_id in ((SHA_A, id_a), (SHA_B, id_b)):
            binary_path, _ = artifact_paths(str(REPO_ROOT), artifact_id)
            digest = compute_file_sha256(binary_path)
            selection = {
                "mode": "experiment",
                "artifact_ref": fork_sha,
                "expected_fork_sha": fork_sha,
                "expected_sha256": digest,
                "artifact_id": artifact_id,
                "artifact_url": "",
            }
            assert verify_experiment_selection(selection) == []
    finally:
        for artifact_id in created:
            _remove_test_artifact(artifact_id)


def test_wrong_fingerprint_fails_verification():
    created = []
    try:
        artifact_id, _, _, manifest = _write_test_artifact(SHA_A)
        created = [artifact_id]
        selection = {
            "mode": "experiment",
            "artifact_ref": SHA_A,
            "expected_fork_sha": SHA_B,  # wrong fork SHA
            "expected_sha256": manifest["binary_sha256"],
            "artifact_id": artifact_id,
            "artifact_url": "",
        }
        assert verify_experiment_selection(selection) != []
        selection["expected_fork_sha"] = SHA_A
        selection["expected_sha256"] = "e" * 64  # wrong binary digest
        assert verify_experiment_selection(selection) != []
    finally:
        for artifact_id in created:
            _remove_test_artifact(artifact_id)


# -- runner readiness ----------------------------------------------------------


class NoInstallRunner:
    def __init__(self):
        self.calls = []

    def run(self, cmd, cwd, timeout):
        self.calls.append(list(cmd))
        from runner_server import CommandResult

        if "opencode.ai/install" in " ".join(str(part) for part in cmd):
            raise AssertionError("network installer must not run for experiments")
        return CommandResult(returncode=0, stdout="fake-ok", stderr="")


def test_missing_artifact_fails_readiness_without_installer(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_ARTIFACT_REF", SHA_A)
    monkeypatch.setenv("OPENCODE_EXPECTED_FORK_SHA", SHA_A)
    monkeypatch.setenv("OPENCODE_EXPECTED_SHA256", "d" * 64)
    monkeypatch.delenv("RUNNER_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    import runner_server as runner_module

    runner = NoInstallRunner()
    manager = runner_module.JobManager(
        workspace_root=str(tmp_path / "ws-missing"),
        job_timeout_seconds=30.0,
        command_runner=runner,
    )
    assert manager.artifact_selection["mode"] == "experiment"
    assert manager.ready is False
    snapshot = manager.health_snapshot()
    assert snapshot["ready"] is False
    assert snapshot["artifact_mode"] == "experiment"
    assert snapshot["artifact_ready"] is False
    with pytest.raises(FileNotFoundError, match="experiment artifact"):
        manager._ensure_opencode_binary(str(tmp_path), 5.0)
    assert runner.calls == []


def test_matching_artifact_is_ready_and_selected(tmp_path, monkeypatch):
    artifact_id, binary_path, _, manifest = _write_test_artifact(SHA_A)
    try:
        _clear_artifact_env(monkeypatch)
        monkeypatch.setenv("OPENCODE_ARTIFACT_REF", SHA_A)
        monkeypatch.setenv("OPENCODE_EXPECTED_FORK_SHA", SHA_A)
        monkeypatch.setenv("OPENCODE_EXPECTED_SHA256", manifest["binary_sha256"])
        monkeypatch.setenv("OPENCODE_ARTIFACT_ID", artifact_id)
        monkeypatch.delenv("RUNNER_OPENCODE_BIN", raising=False)
        monkeypatch.delenv("OPENCODE_BIN", raising=False)
        import runner_server as runner_module

        manager = runner_module.JobManager(
            workspace_root=str(tmp_path / "ws-ready"),
            job_timeout_seconds=30.0,
            command_runner=NoInstallRunner(),
        )
        assert manager.ready is True
        snapshot = manager.health_snapshot()
        assert snapshot["ready"] is True
        assert snapshot["artifact_id"] == artifact_id
        assert snapshot["artifact_ready"] is True
        assert snapshot["opencode_bin"] == binary_path
        resolved = manager._ensure_opencode_binary(str(tmp_path), 5.0)
        assert resolved == binary_path
    finally:
        _remove_test_artifact(artifact_id)


def test_baseline_mode_still_works(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    monkeypatch.delenv("RUNNER_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    import runner_server as runner_module

    manager = runner_module.JobManager(
        workspace_root=str(tmp_path / "ws-base"),
        job_timeout_seconds=30.0,
        command_runner=NoInstallRunner(),
        opencode_bin="/fake/opencode",
    )
    assert manager.artifact_selection["mode"] == "upstream-baseline"
    assert manager.health_snapshot()["artifact_mode"] == "upstream-baseline"


# -- Render payload ------------------------------------------------------------


def test_baseline_payload_is_unchanged():
    from render_lifecycle import build_create_service_payload

    payload = build_create_service_payload(name="n", owner_id="o")
    details = payload["serviceDetails"]["envSpecificDetails"]
    assert "install-opencode-artifact" not in details["buildCommand"]
    assert "OPENCODE_EXPECTED_SHA256" not in details["startCommand"]
    assert "RUNNER_ALLOW_RUNTIME_INSTALL=0" in details["startCommand"]


def test_experiment_payload_selects_exact_artifact():
    from render_lifecycle import build_create_service_payload

    digest = "d" * 64
    url = immutable_artifact_url(SHA_A)
    payload = build_create_service_payload(
        name="n",
        owner_id="o",
        opencode_artifact_ref=SHA_A,
        opencode_expected_fork_sha=SHA_A,
        opencode_expected_sha256=digest,
        opencode_artifact_url=url,
    )
    details = payload["serviceDetails"]["envSpecificDetails"]
    assert "automation/install-opencode-artifact.sh" in details["buildCommand"]
    assert SHA_A in details["buildCommand"]
    assert "OPENCODE_EXPECTED_FORK_SHA=%s" % SHA_A in details["startCommand"]
    assert ("OPENCODE_EXPECTED_SHA256=%s" % digest) in details["startCommand"]
    assert "RUNNER_ALLOW_RUNTIME_INSTALL=0" in details["startCommand"]
    assert "latest" not in details["buildCommand"].lower()


def test_experiment_payload_rejects_mutable_ref():
    from render_lifecycle import build_create_service_payload

    with pytest.raises(ValueError):
        build_create_service_payload(
            name="n", owner_id="o", opencode_artifact_ref="latest"
        )


def test_start_and_build_fragments_carry_fingerprint():
    selection = {
        "mode": "experiment",
        "artifact_ref": SHA_A,
        "expected_fork_sha": SHA_A,
        "expected_sha256": "d" * 64,
        "artifact_id": artifact_id_from_sha(SHA_A),
        "artifact_url": immutable_artifact_url(SHA_A),
    }
    assert SHA_A in start_env_prefix(selection)
    assert "install-opencode-artifact.sh" in build_command_fragment(selection)
    assert build_command_fragment({"mode": "upstream-baseline"}) == ""


def test_no_installer_fallback_in_artifact_scripts():
    build_text = (REPO_ROOT / "automation" / "build-opencode-artifact.sh").read_text(
        encoding="utf-8"
    )
    install_text = (
        REPO_ROOT / "automation" / "install-opencode-artifact.sh"
    ).read_text(encoding="utf-8")
    assert "opencode.ai/install" not in install_text
    assert "latest" not in install_text.lower() or "never" in install_text.lower()
    assert "bun install" in build_text
    assert "packages/opencode/script/build.ts" in build_text
    assert ".opencode-artifacts" in install_text
    # Builder keys storage by resolved SHA so concurrent refs never collide.
    assert "rev-parse" in build_text
