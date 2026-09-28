"""Regression tests for the parallel OpenCode experiment artifact pipeline (issue #86).

Covers the Definition of Done without network access, Bun, or secrets:
- fingerprints record fork SHA, upstream base, toolchain, binary SHA-256
  and build identity, and reject mutable ``latest``-style references;
- checksum/fingerprint verification passes for intact binaries and fails
  closed on mismatch;
- wrong/missing artifacts fail readiness with no silent baseline fallback;
- arbitrary approved fork refs build independently (distinct identities,
  explicit-SHA builder steps, no ``latest`` anywhere);
- concurrent experiment artifacts remain distinguishable and coexist under
  per-artifact paths;
- upstream-baseline mode still works unchanged;
- no runtime installer/download fallback ever runs for a requested artifact.
"""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import opencode_artifacts as artifacts  # noqa: E402
from opencode_artifacts import (  # noqa: E402
    ARTIFACTS_DIRNAME,
    DEFAULT_BUILD_SCRIPT,
    REQUIRED_ARCH,
    SUPPORTED_ARCHS,
    artifact_binary_candidates,
    artifact_binary_path,
    artifact_id_for,
    artifact_tag_for,
    baseline_binary_path,
    build_steps_for,
    check_artifact_readiness,
    fingerprint_from_json,
    fingerprint_to_json,
    github_reference_for,
    make_fingerprint,
    resolve_requested_artifact,
    sha256_of_file,
    start_command_with_artifact,
    validate_fingerprint,
    validate_fork_ref,
    verify_binary_checksum,
)
from render_lifecycle import build_create_service_payload  # noqa: E402
from runner_server import JobManager  # noqa: E402

SHA_A = "9000e7fc8d96c845512f7c73122431418a71d4e4"
SHA_B = "ae343e8f" + "0" * 32
SHA_C = "1" * 40
UPSTREAM_BASE = "75e1e7ae310dc36c86c920e8997d0e1181e24a88"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _make_executable(path: str, version_output: str = "1.18.33\n") -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\necho '%s'\n" % version_output.strip())
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fingerprint(track="bounded-output", sha=SHA_A, ref="exp/bounded-output", arch=REQUIRED_ARCH,
                 digest=None, script=DEFAULT_BUILD_SCRIPT):
    return make_fingerprint(
        fork_ref=ref,
        fork_commit_sha=sha,
        binary_sha256=digest or _digest(b"binary-%s-%s" % (track.encode(), sha.encode())),
        track=track,
        arch=arch,
        build_script=script,
        build_toolchain={
            "bun_version": "1.2.0",
            "build_script": script,
            "build_command": "bun run %s" % script,
        },
        build_identity={"builder": "test-builder", "run_id": "test-run"},
    )


def _clear_artifact_env(monkeypatch):
    for var in ("OPENCODE_ARTIFACT_ID", "OPENCODE_ARTIFACT_SHA256",
                "OPENCODE_ARTIFACT_REF", "OPENCODE_ARTIFACT_VERSION"):
        monkeypatch.delenv(var, raising=False)


# -- fingerprints -----------------------------------------------------------


def test_fingerprint_roundtrip_records_all_identity():
    fp = _fingerprint()
    assert fp["fork_repo"] == "kodmial/opencode"
    assert fp["fork_commit_sha"] == SHA_A
    assert fp["upstream_base_commit"] == UPSTREAM_BASE
    assert fp["arch"] == REQUIRED_ARCH
    assert "bun" in fp["build_toolchain"]["build_command"]
    assert len(fp["binary_sha256"]) == 64
    assert fp["build_identity"]["builder"] == "test-builder"
    assert "latest" not in fp["artifact_reference"]
    assert validate_fingerprint(fp) == fp
    assert fingerprint_from_json(fingerprint_to_json(fp)) == fp


def test_mutable_refs_rejected():
    for bad in ("latest", "Latest", "main", "master", "dev", "stable",
                "HEAD", "next", "current", "", "   "):
        with pytest.raises(ValueError):
            validate_fork_ref(bad)
    with pytest.raises(ValueError):
        make_fingerprint(
            fork_ref="latest", fork_commit_sha=SHA_A,
            binary_sha256=_digest(b"x"), track="bounded-output",
        )


def test_explicit_sha_and_branch_refs_accepted():
    assert validate_fork_ref(SHA_A) == SHA_A
    assert validate_fork_ref("exp/source-stripped") == "exp/source-stripped"
    fp = _fingerprint(ref=SHA_A)
    assert fp["fork_ref"] == SHA_A


def test_fingerprint_rejects_tampered_identity():
    fp = _fingerprint()
    # A sibling experiment's id cannot masquerade as this fingerprint.
    swapped = dict(fp, artifact_id=artifact_id_for(SHA_B, "bounded-output"))
    with pytest.raises(ValueError):
        validate_fingerprint(swapped)
    # A commit SHA that disagrees with the embedded id prefix is rejected.
    moved = dict(fp, fork_commit_sha=SHA_B)
    with pytest.raises(ValueError):
        validate_fingerprint(moved)
    mutable = dict(fp, artifact_tag="opencode-latest-linux-x64")
    with pytest.raises(ValueError):
        validate_fingerprint(mutable)


def test_unsupported_arch_and_build_script_rejected():
    with pytest.raises(ValueError):
        _fingerprint(arch="linux-mips")
    with pytest.raises(ValueError):
        _fingerprint(script="packages/opencode/script/build-custom.sh")
    for script in ("packages/opencode/script/build-lite.ts",
                   "packages/opencode/script/build-coding.ts",
                   "packages/opencode/script/build-direct.ts"):
        assert _fingerprint(script=script)["build_script"] == script


# -- independent + concurrent builds ------------------------------------------


def test_arbitrary_approved_refs_build_independently():
    fp_a = _fingerprint(track="source-stripped", sha=SHA_A, ref="exp/source-stripped")
    fp_b = _fingerprint(track="bounded-output", sha=SHA_B, ref="exp/bounded-output")
    fp_c = _fingerprint(track="direct-headless", sha=SHA_C, ref="exp/direct-headless")
    ids = {fp_a["artifact_id"], fp_b["artifact_id"], fp_c["artifact_id"]}
    refs = {fp_a["artifact_reference"], fp_b["artifact_reference"], fp_c["artifact_reference"]}
    assert len(ids) == 3 and len(refs) == 3
    for fp in (fp_a, fp_b, fp_c):
        steps = build_steps_for(fp, workdir="/tmp/w")
        blob = "\n".join(steps)
        assert fp["fork_commit_sha"] in blob
        assert "latest" not in blob.lower()
        assert "bun install" in blob and "bun run" in blob
        assert UPSTREAM_BASE in blob


def test_concurrent_artifacts_coexist_without_overwrite(tmp_path):
    fp_a = _fingerprint(track="source-stripped", sha=SHA_A, ref="exp/a")
    fp_b = _fingerprint(track="bounded-output", sha=SHA_A, ref="exp/b")
    path_a = artifact_binary_path(str(tmp_path), fp_a["artifact_id"])
    path_b = artifact_binary_path(str(tmp_path), fp_b["artifact_id"])
    assert path_a != path_b
    # Same commit on two tracks stays distinguishable; different commits do too.
    assert artifact_id_for(SHA_A, "source-stripped") != artifact_id_for(SHA_B, "source-stripped")
    assert ARTIFACTS_DIRNAME in path_a and fp_a["artifact_id"] in path_a
    base = baseline_binary_path(str(tmp_path))
    assert base != path_a and base != path_b
    assert base.endswith(os.path.join(".opencode-bin", "opencode"))


def test_github_reference_is_immutable():
    tag = artifact_tag_for(SHA_A, "linux-x64")
    ref = github_reference_for(tag)
    assert "latest" not in ref and SHA_A[:12] in ref
    with pytest.raises(ValueError):
        github_reference_for("opencode-latest-linux-x64")


# -- checksums -----------------------------------------------------------------


def test_checksum_verify_pass_and_mismatch(tmp_path):
    binary = str(tmp_path / "opencode")
    with open(binary, "wb") as handle:
        handle.write(b"fake-standalone-binary")
    expected = sha256_of_file(binary)
    assert verify_binary_checksum(binary, expected) == expected
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_binary_checksum(binary, _digest(b"other-bytes"))
    with pytest.raises(FileNotFoundError):
        verify_binary_checksum(str(tmp_path / "missing"), expected)


def test_check_artifact_readiness_never_raises(tmp_path):
    binary = _make_executable(str(tmp_path / "opencode"))
    expected = sha256_of_file(binary)
    ready, resolved, _ = check_artifact_readiness(binary, expected)
    assert ready is True and resolved == binary
    ready, _, detail = check_artifact_readiness(binary, _digest(b"wrong"))
    assert ready is False and "mismatch" in detail
    ready, _, detail = check_artifact_readiness(str(tmp_path / "missing"), expected)
    assert ready is False and "not found" in detail
    plain = str(tmp_path / "plain")
    with open(plain, "w", encoding="utf-8") as handle:
        handle.write("not executable\n")
    ready, _, detail = check_artifact_readiness(plain, sha256_of_file(plain))
    assert ready is False and "non-executable" in detail


# -- runner readiness ------------------------------------------------------------


class NoInstallRunner:
    """Command runner that records calls and fails any installer use."""

    def __init__(self):
        self.calls = []

    def run(self, cmd, cwd, timeout):
        self.calls.append(list(cmd))
        from runner_server import CommandResult

        if len(cmd) >= 2 and cmd[0] == "sh" and "opencode.ai/install" in cmd[1]:
            raise AssertionError("network installer must not run for experiment artifacts")
        return CommandResult(returncode=0, stdout="fake-ok", stderr="")


def test_missing_artifact_fails_readiness_without_installer(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    import runner_server as runner_module

    monkeypatch.setattr(runner_module, "artifact_binary_candidates", lambda root, aid: [])
    selection = {"artifact_id": "opencode-bounded-output-9000e7fc8d96",
                 "artifact_sha256": _digest(b"absent"), "artifact_ref": ""}
    runner = NoInstallRunner()
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        opencode_bin="opencode",
        allow_runtime_install=False,
        requested_artifact=selection,
    )
    assert manager.ready is False
    assert manager.health_snapshot()["ready"] is False
    assert manager.health_snapshot()["opencode_artifact_ready"] is False
    with pytest.raises(FileNotFoundError, match="never via the runtime network installer"):
        manager._ensure_opencode_binary(str(tmp_path), 5.0)
    assert runner.calls == []


def test_wrong_sha_fails_readiness_and_never_falls_back(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    import runner_server as runner_module

    fake = _make_executable(str(tmp_path / "opencode"), "1.18.33\n")
    monkeypatch.setattr(
        runner_module, "artifact_binary_candidates", lambda root, aid: [fake]
    )
    selection = {"artifact_id": "opencode-bounded-output-9000e7fc8d96",
                 "artifact_sha256": _digest(b"some-other-binary"), "artifact_ref": ""}
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=NoInstallRunner(),
        opencode_bin="opencode",
        allow_runtime_install=False,
        requested_artifact=selection,
    )
    assert manager.ready is False
    assert "mismatch" in manager.health_snapshot()["opencode_artifact_detail"]
    with pytest.raises(ValueError, match="checksum mismatch"):
        manager._ensure_opencode_binary(str(tmp_path), 5.0)


def test_correct_artifact_is_ready_and_selected(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    import runner_server as runner_module

    fake = _make_executable(str(tmp_path / "opencode"), "1.18.33\n")
    digest = sha256_of_file(fake)
    monkeypatch.setattr(
        runner_module, "artifact_binary_candidates", lambda root, aid: [fake]
    )
    runner = NoInstallRunner()
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        opencode_bin="opencode",
        allow_runtime_install=False,
        requested_artifact={"artifact_id": "opencode-bounded-output-9000e7fc8d96",
                            "artifact_sha256": digest, "artifact_ref": ""},
    )
    assert manager.ready is True
    snapshot = manager.health_snapshot()
    assert snapshot["ready"] is True
    assert snapshot["opencode_artifact_ready"] is True
    assert snapshot["opencode_bin"] == fake
    assert manager._ensure_opencode_binary(str(tmp_path), 5.0) == fake
    assert runner.calls == []


def test_version_mismatch_fails_readiness(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    import runner_server as runner_module

    fake = _make_executable(str(tmp_path / "opencode"), "9.9.9\n")
    digest = sha256_of_file(fake)
    monkeypatch.setattr(
        runner_module, "artifact_binary_candidates", lambda root, aid: [fake]
    )
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=NoInstallRunner(),
        opencode_bin="opencode",
        allow_runtime_install=False,
        requested_artifact={"artifact_id": "opencode-bounded-output-9000e7fc8d96",
                            "artifact_sha256": digest, "artifact_ref": ""},
        requested_artifact_version="1.18.33",
    )
    assert manager.ready is False
    assert "version mismatch" in manager.health_snapshot()["opencode_artifact_detail"]


def test_partial_selection_fails_closed(monkeypatch):
    _clear_artifact_env(monkeypatch)
    assert resolve_requested_artifact({}) is None
    with pytest.raises(ValueError, match="partial artifact selection"):
        resolve_requested_artifact({"OPENCODE_ARTIFACT_ID": "opencode-x",
                                    "OPENCODE_ARTIFACT_SHA256": ""})
    with pytest.raises(ValueError, match="partial artifact selection"):
        resolve_requested_artifact({"OPENCODE_ARTIFACT_ID": "",
                                    "OPENCODE_ARTIFACT_SHA256": _digest(b"x")})
    selection = resolve_requested_artifact(
        {"OPENCODE_ARTIFACT_ID": "opencode-bounded-output-9000e7fc8d96",
         "OPENCODE_ARTIFACT_SHA256": _digest(b"x"),
         "OPENCODE_ARTIFACT_REF": "github-release:kodmial/opencode@opencode-9000e7fc8d96-linux-x64"})
    assert selection["artifact_id"].startswith("opencode-")
    with pytest.raises(ValueError):
        resolve_requested_artifact(
            {"OPENCODE_ARTIFACT_ID": "opencode-x",
             "OPENCODE_ARTIFACT_SHA256": _digest(b"x"),
             "OPENCODE_ARTIFACT_REF": "latest"})


def test_baseline_mode_still_works(tmp_path, monkeypatch):
    _clear_artifact_env(monkeypatch)
    monkeypatch.delenv("RUNNER_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    import runner_server as runner_module

    fake = _make_executable(str(tmp_path / "opencode"), "1.18.33\n")
    monkeypatch.setattr(runner_module, "find_opencode_binary", lambda: fake)
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=NoInstallRunner(),
        opencode_bin="opencode",
        allow_runtime_install=False,
    )
    assert manager.requested_artifact is None
    assert manager.ready is True
    snapshot = manager.health_snapshot()
    assert snapshot["opencode_artifact_id"] == ""
    assert snapshot["opencode_artifact_ready"] is False
    candidates = artifact_binary_candidates(str(tmp_path), "opencode-x-123")
    assert all(ARTIFACTS_DIRNAME in candidate for candidate in candidates)


# -- Render provisioning ----------------------------------------------------------


def test_render_payload_baseline_unchanged():
    payload = build_create_service_payload(name="n", owner_id="o")
    details = payload["serviceDetails"]["envSpecificDetails"]
    assert details["startCommand"] == (
        "RUNNER_ALLOW_RUNTIME_INSTALL=0 python -m automation.runner_server"
    )
    assert "OPENCODE_ARTIFACT" not in details["startCommand"]


def test_render_payload_selects_exact_artifact():
    digest = _digest(b"artifact-binary")
    payload = build_create_service_payload(
        name="n", owner_id="o",
        opencode_artifact_id="opencode-bounded-output-9000e7fc8d96",
        opencode_artifact_sha256=digest,
        opencode_artifact_ref="github-release:kodmial/opencode@opencode-9000e7fc8d96-linux-x64",
    )
    start = payload["serviceDetails"]["envSpecificDetails"]["startCommand"]
    assert "OPENCODE_ARTIFACT_ID=opencode-bounded-output-9000e7fc8d96" in start
    assert "OPENCODE_ARTIFACT_SHA256=%s" % digest in start
    assert "RUNNER_ALLOW_RUNTIME_INSTALL=0" in start
    assert "python -m automation.runner_server" in start
    assert "latest" not in start


def test_render_payload_rejects_partial_or_mutable_artifact():
    with pytest.raises(ValueError, match="partial artifact selection"):
        build_create_service_payload(
            name="n", owner_id="o",
            opencode_artifact_id="opencode-bounded-output-9000e7fc8d96")
    with pytest.raises(ValueError, match="64 lowercase hex"):
        build_create_service_payload(
            name="n", owner_id="o",
            opencode_artifact_id="opencode-bounded-output-9000e7fc8d96",
            opencode_artifact_sha256="not-a-digest")
    with pytest.raises(ValueError, match="'latest'"):
        build_create_service_payload(
            name="n", owner_id="o",
            opencode_artifact_id="opencode-bounded-output-9000e7fc8d96",
            opencode_artifact_sha256=_digest(b"x"),
            opencode_artifact_ref="latest")


def test_start_command_helper_prefixes_selection():
    fp = _fingerprint()
    start = start_command_with_artifact(
        "RUNNER_ALLOW_RUNTIME_INSTALL=0 python -m automation.runner_server", fp)
    assert start.startswith("OPENCODE_ARTIFACT_ID=%s " % fp["artifact_id"])
    assert fp["binary_sha256"] in start
    assert start.endswith("python -m automation.runner_server")


# -- builder + spec -----------------------------------------------------------------


def test_builder_script_rejects_mutable_refs_and_bad_arch():
    text = (REPO_ROOT / "automation" / "build-opencode-artifact.sh").read_text(
        encoding="utf-8"
    )
    assert "bun install --frozen-lockfile" in text
    assert "bun run" in text
    assert "latest" in text  # mutable-ref guard text
    assert "git checkout \"$sha\"" in text or 'git checkout "$sha"' in text
    assert "merge-base --is-ancestor" in text
    assert ".opencode-artifacts" in text or ".opencode-artifacts/<artifact-id>" in text


def test_spec_agrees_with_module():
    with open(REPO_ROOT / "automation" / "opencode-artifacts.spec.json",
              encoding="utf-8") as handle:
        spec = json.load(handle)
    assert sorted(spec["required_fingerprint_keys"]) == sorted(
        artifacts.REQUIRED_FINGERPRINT_KEYS)
    assert spec["build"]["required_arch"] == REQUIRED_ARCH
    assert spec["deploy_layout"]["artifacts_dir"] == ARTIFACTS_DIRNAME
    assert set(spec["archs"]) == set(SUPPORTED_ARCHS)
    assert spec["upstream_base_commit"] == UPSTREAM_BASE
