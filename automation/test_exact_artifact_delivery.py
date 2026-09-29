"""Offline tests for exact OpenCode artifact delivery + /proc identity (#128).

Stdlib-first, no network, no git mutations, no Render service. Proves the
production delivery/identity implementation for the exact corrected
artifact ``11004835952`` (never a rebuild, never a substituted binary):

- gate: the supported contract passes through ``infrastructure-blocked``
  while ordinary smoke stays untouched and unsupported exact contracts
  (e.g. the superseded ``11001896223``) still fail closed;
- controller acquisition: credentialed download requires a token, verifies
  the archive digest before extraction/use plus the binary checksum and
  the exact ``--version``, and never exposes credentials to the child;
- transport: verified bytes materialize at a deterministic absolute path
  and the machine-readable identity rides the submit/create payload while
  the worker rejects missing/mismatched bytes before starting OpenCode;
- execution: exact mode invokes the absolute path directly with zero
  fallback (no PATH/``.opencode-bin``/HOME/installer/baseline lookup);
- identity: ``/proc/<pid>/exe`` realpath + SHA + cmdline + parent
  evidence is captured from the actually executing process and fails
  closed on mismatch;
- evidence: artifact/process identity survives in the terminal job result
  (controller-persisted outside the ephemeral worker).
"""

import hashlib
import json
import os
import stat
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import exact_artifact_delivery as exact
import render_lifecycle as lifecycle
import runner_server
from render_lifecycle import ExecutionMetadata, JobRequest


ISSUE_128_BODY = """\
## Exact artifact contract

- repository: `kodmial/opencode`
- PR: `#12`
- source/head SHA: `a3c748143bbc525a7cef4f9db48e2a779418943c`
- PR merge SHA actually built: `84fa724616e1fddeea8e7665e38568928feffdf9`
- source workflow run: `36498663107`
- artifact name: `opencode-coding-linux-x64`
- artifact ID: `11004835952`
- artifact ZIP digest: `sha256:0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040`
- binary SHA-256: `f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966`
- exact `--version`: `1.18.33`

Download exact artifact ID `11004835952` from source run `36498663107`.
Never rebuild OpenCode and never substitute another binary.
"""


def _write_executable(path, text="#!/bin/sh\necho hi\n"):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o755)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _copy_elf(path):
    """Copy a real ELF binary so /proc/<pid>/exe is the file itself.

    A ``#!/bin/sh`` script would execute via the interpreter (exe would
    be the shell, not the script); the production OpenCode artifact is
    ELF, so identity tests must use ELF too.
    """
    import shutil as _shutil
    _shutil.copy("/bin/true", path)
    os.chmod(path, 0o755)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Gate: supported passes through, ordinary untouched, unsupported blocked.
# ---------------------------------------------------------------------------


def test_gate_supports_exact_128_contract():
    requirement = lifecycle.parse_exact_workflow_artifact_requirement(
        "P0: Implement exact OpenCode artifact delivery and /proc identity",
        ISSUE_128_BODY,
    )
    assert requirement is not None
    assert requirement["artifact_id"] == "11004835952"
    assert requirement["source_run_id"] == "36498663107"
    binary_sha = lifecycle.parse_exact_binary_sha256("t", ISSUE_128_BODY)
    assert binary_sha == exact.EXPECTED_BINARY_SHA256
    assert lifecycle.is_supported_exact_workflow_artifact(requirement, binary_sha)
    req, blocker, identity = lifecycle.exact_artifact_gate_decision("t", ISSUE_128_BODY)
    assert req is not None
    assert blocker == ""
    assert identity is not None
    assert identity["artifact_id"] == "11004835952"
    assert identity["binary_sha256"] == exact.EXPECTED_BINARY_SHA256
    assert identity["version"] == "1.18.33"


def test_gate_still_blocks_unsupported_exact_contract():
    body = (
        "artifact ID: `11001896223` source workflow run: `36492639568` "
        "sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
    )
    req, blocker, identity = lifecycle.exact_artifact_gate_decision("t", body)
    assert req is not None
    assert identity is None
    assert "infrastructure-blocked" in blocker
    assert "11001896223" in blocker
    # A supported archive claim with a smuggled foreign binary SHA is not
    # the supported contract either.
    supported = lifecycle.parse_exact_workflow_artifact_requirement("t", ISSUE_128_BODY)
    assert supported is not None
    assert not lifecycle.is_supported_exact_workflow_artifact(
        supported, "a" * 64
    )
    _, blocker2, identity2 = lifecycle.exact_artifact_gate_decision(
        "t", ISSUE_128_BODY.replace(exact.EXPECTED_BINARY_SHA256, "a" * 64)
    )
    # The archive conjunction still parses; the binary mismatch keeps it
    # out of the supported path (blocker names the gap, no delivery).
    assert identity2 is None
    assert "infrastructure-blocked" in blocker2


def test_gate_leaves_ordinary_smoke_untouched():
    req, blocker, identity = lifecycle.exact_artifact_gate_decision(
        "P0: Fresh smoke check",
        "Run the normal smoke workload. No artifact pinning.",
    )
    assert req is None
    assert blocker == ""
    assert identity is None


# ---------------------------------------------------------------------------
# Selection/delivery path: identity rides submit + create payloads.
# ---------------------------------------------------------------------------


def test_supported_identity_flows_through_submit_and_create_payloads():
    identity = lifecycle.supported_exact_artifact_identity()
    assert identity["artifact_id"] == "11004835952"
    req = JobRequest(
        task_text="do work",
        issue_number=128,
        metadata=ExecutionMetadata(issue_number=128),
        exact_artifact=identity,
    )
    body = req.to_dict()
    assert body["exact_artifact"]["artifact_id"] == "11004835952"
    assert body["exact_artifact"]["source_run_id"] == "36498663107"
    assert body["exact_artifact"]["archive_sha256"] == exact.ARCHIVE_SHA256
    assert body["exact_artifact"]["binary_sha256"] == exact.EXPECTED_BINARY_SHA256
    assert body["exact_artifact"]["version"] == "1.18.33"
    payload = lifecycle.build_create_service_payload(
        name="runtime-lab-issue128-test", owner_id="owner-1",
        exact_artifact=identity,
    )
    start = payload["serviceDetails"]["envSpecificDetails"]["startCommand"]
    assert "OPENCODE_EXACT_ARTIFACT_ID=11004835952" in start
    assert ("OPENCODE_EXACT_ARTIFACT_SHA256=%s" % exact.EXPECTED_BINARY_SHA256) in start
    assert ("OPENCODE_EXACT_ARCHIVE_SHA256=%s" % exact.ARCHIVE_SHA256) in start
    assert "OPENCODE_EXACT_SOURCE_RUN=36498663107" in start
    # Ordinary payloads carry no exact selection.
    plain = JobRequest(
        task_text="do work", issue_number=1,
        metadata=ExecutionMetadata(issue_number=1),
    ).to_dict()
    assert "exact_artifact" not in plain
    baseline = lifecycle.build_create_service_payload(
        name="runtime-lab-issue1-test", owner_id="owner-1")
    assert "OPENCODE_EXACT_" not in baseline["serviceDetails"]["envSpecificDetails"]["startCommand"]
    # Unsupported identities are rejected fail-closed, never a silent baseline.
    with pytest.raises(ValueError):
        JobRequest(task_text="x", issue_number=2,
                   metadata=ExecutionMetadata(issue_number=2),
                   exact_artifact={"artifact_id": "11001896223"})
    with pytest.raises(ValueError):
        lifecycle.build_create_service_payload(
            name="n", owner_id="o",
            exact_artifact={"artifact_id": "11001896223"})
    with pytest.raises(ValueError):
        lifecycle.build_create_service_payload(
            name="n", owner_id="o",
            opencode_artifact_id="opencode-x", opencode_artifact_sha256="a" * 64,
            exact_artifact=identity)


# ---------------------------------------------------------------------------
# Controller acquisition: credentialed fetch + archive/binary verification.
# ---------------------------------------------------------------------------


def test_controller_download_requires_credential_and_verifies_archive(tmp_path):
    with pytest.raises(ValueError):
        exact.download_exact_artifact_zip(
            dest_path=str(tmp_path / "a.zip"), token="")
    # Injectable transport serves fixed bytes; the archive digest gate runs
    # before any extraction/use.
    served = b"exact-bytes-fixture"
    expected = hashlib.sha256(served).hexdigest()

    class _Response:
        def __init__(self, data):
            self._data = data
            self.captured = {}

        def read(self, *args):
            chunk, self._data = self._data[:8192], self._data[8192:]
            return chunk

    seen = {}

    def _urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        seen["url"] = request.full_url
        assert "11004835952" in request.full_url
        return _Response(served)

    dest = str(tmp_path / "artifact.zip")
    exact.download_exact_artifact_zip(
        dest_path=dest, token="controller-token", urlopen=_urlopen)
    assert seen["auth"] == "Bearer controller-token"
    assert "11004835952" in seen["url"]
    assert exact.verify_archive_file(dest, expected) == expected
    with pytest.raises(ValueError):
        exact.verify_archive_file(dest, "b" * 64)
    # Credential lookup itself never logs values: only the token string.
    assert exact.controller_token_from_env({"GH_TOKEN": "tok-1"}) == "tok-1"
    assert exact.controller_token_from_env({"GITHUB_TOKEN": "tok-2"}) == "tok-2"
    assert exact.controller_token_from_env({}) == ""


def test_extract_and_verify_checks_full_chain(tmp_path):
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    binary = payload_dir / exact.BINARY_FILENAME
    binary_bytes = b"fake-opencode-binary-bytes"
    binary.write_bytes(binary_bytes)
    binary_sha = hashlib.sha256(binary_bytes).hexdigest()
    (payload_dir / exact.CHECKSUM_FILENAME).write_text(
        "%s  %s\n" % (binary_sha, exact.BINARY_FILENAME))
    (payload_dir / "build-metadata.txt").write_text("opencode_version=1.18.33\n")
    zip_path = str(tmp_path / "artifact.zip")
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name in exact.ARTIFACT_PAYLOAD:
            archive.write(str(payload_dir / name), name)
    # The fixture digest differs from the pinned contract, so the pinned
    # chain rejects it; the mechanical chain (archive -> bundled checksum
    # -> binary) is what this exercises via explicit digests.
    fixture_archive_sha = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
    assert exact.verify_archive_file(zip_path, fixture_archive_sha) == fixture_archive_sha
    with pytest.raises(ValueError):
        exact.verify_archive_file(zip_path, exact.ARCHIVE_SHA256)
    # Missing payload members fail closed.
    thin_zip = str(tmp_path / "thin.zip")
    with zipfile.ZipFile(thin_zip, "w") as archive:
        archive.writestr(exact.BINARY_FILENAME, binary_bytes)
    thin_sha = hashlib.sha256(open(thin_zip, "rb").read()).hexdigest()
    with pytest.raises(ValueError):
        exact.extract_and_verify(thin_zip, str(tmp_path / "out-thin"),
                                 binary_sha, thin_sha)
    out = exact.extract_and_verify(
        zip_path, str(tmp_path / "out"), binary_sha, fixture_archive_sha)
    assert os.path.isfile(out)
    assert os.access(out, os.X_OK)
    with pytest.raises(ValueError):
        exact.extract_and_verify(
            zip_path, str(tmp_path / "out2"), "c" * 64, fixture_archive_sha)
    assert exact.check_version_output("1.18.33") == "1.18.33"
    with pytest.raises(ValueError):
        exact.check_version_output("0.0.0--202609282229")


# ---------------------------------------------------------------------------
# Transport: deterministic absolute path, verified materialization.
# ---------------------------------------------------------------------------


def test_absolute_path_is_deterministic_and_absolute(tmp_path):
    first = exact.exact_artifact_abs_path("test-demo", base_dir=str(tmp_path))
    second = exact.exact_artifact_abs_path("test-demo", base_dir=str(tmp_path))
    assert first == second
    assert os.path.isabs(first)
    assert first.endswith(os.path.join(".opencode-exact-workflow", "test-demo", "opencode"))
    assert "test-demo2" not in first
    assert exact.exact_artifact_abs_path("test-demo", base_dir=str(tmp_path)) != \
        exact.exact_artifact_abs_path("test-other", base_dir=str(tmp_path))
    with pytest.raises(ValueError):
        exact.exact_artifact_abs_path("11001896223", base_dir=str(tmp_path))
    with pytest.raises(ValueError):
        exact.exact_artifact_abs_path("../escape", base_dir=str(tmp_path))
    # Production id resolves under the repo root by default.
    prod = exact.exact_artifact_abs_path()
    assert os.path.isabs(prod)
    assert prod.endswith(os.path.join(
        ".opencode-exact-workflow", "11004835952", "opencode"))


def test_materialize_bytes_verifies_before_use(tmp_path):
    data = b"verified-exact-bytes"
    sha = hashlib.sha256(data).hexdigest()
    dest = exact.exact_artifact_abs_path("test-mat", base_dir=str(tmp_path))
    path, actual = exact.materialize_exact_bytes(data, dest, sha)
    assert path == dest
    assert actual == sha
    assert os.access(dest, os.X_OK)
    with pytest.raises(ValueError):
        exact.materialize_exact_bytes(b"other-bytes", dest + "-other", sha)
    with pytest.raises(ValueError):
        exact.materialize_exact_bytes(data, "relative/path/opencode", sha)
    with pytest.raises(ValueError):
        exact.materialize_exact_bytes(b"", dest, sha)


def test_worker_push_transport_rejects_mismatch(tmp_path):
    identity = exact.build_exact_artifact_identity()
    with pytest.raises(ValueError):
        runner_server.store_exact_artifact_bytes(b"x", {"artifact_id": "nope"})
    dest_dir = str(tmp_path / "worker")
    # Production-identity push of foreign bytes fails closed on checksum.
    with pytest.raises(ValueError):
        runner_server.store_exact_artifact_bytes(
            b"foreign-bytes", identity, base_dir=dest_dir)
    # Oversized pushes are refused before any write.
    with pytest.raises(ValueError):
        runner_server.store_exact_artifact_bytes(
            b"x" * (runner_server.EXACT_PUSH_MAX_BYTES + 1),
            identity, base_dir=dest_dir)


# ---------------------------------------------------------------------------
# Worker: missing/mismatch fail closed with zero fallback.
# ---------------------------------------------------------------------------


def _exact_manager(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("OPENCODE_EXACT_ARTIFACT_DIR", str(tmp_path / "exact-root"))
    for name in ("OPENCODE_ARTIFACT_ID", "OPENCODE_ARTIFACT_SHA256",
                 "OPENCODE_ARTIFACT_REF"):
        monkeypatch.delenv(name, raising=False)
    return runner_server.JobManager(
        workspace_root=str(tmp_path / "ws"),
        command_runner=kwargs.pop("command_runner", None)
        or runner_server.SubprocessCommandRunner(),
        requested_exact_artifact=exact.build_exact_artifact_identity(),
        **kwargs,
    )


def test_worker_rejects_missing_exact_before_start(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    identity = exact.build_exact_artifact_identity()
    with pytest.raises(FileNotFoundError) as excinfo:
        manager._ensure_exact_binary(identity)
    assert "11004835952" in str(excinfo.value)
    assert "/v1/exact-artifact" in str(excinfo.value)


def test_worker_rejects_mismatched_exact_before_start(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    identity = exact.build_exact_artifact_identity()
    dest = exact.exact_artifact_abs_path(identity["artifact_id"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as handle:
        handle.write(b"wrong-binary-bytes")
    os.chmod(dest, 0o755)
    with pytest.raises(ValueError) as excinfo:
        manager._ensure_exact_binary(identity)
    assert "mismatch" in str(excinfo.value).lower()


def test_exact_ensure_never_consults_fallbacks(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    identity = exact.build_exact_artifact_identity()

    def _forbidden(*args, **kwargs):
        raise AssertionError("fallback consulted during exact resolution")

    monkeypatch.setattr(runner_server, "find_opencode_binary", _forbidden)
    monkeypatch.setattr("opencode_runner.find_opencode_binary", _forbidden)
    with pytest.raises(FileNotFoundError):
        manager._ensure_exact_binary(identity)


def test_invalid_job_identity_fails_closed_at_submit(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    payload = {
        "task_text": "do work",
        "issue_number": 128,
        "repository_url": runner_server.PUBLIC_REPO_URL,
        "exact_artifact": {"artifact_id": "11001896223"},
    }
    record, created = manager.submit(payload)
    assert created is True
    assert record.status == "failed"
    assert "exact_artifact" in record.error


# ---------------------------------------------------------------------------
# /proc identity: capture, mismatch, and absolute-path launch.
# ---------------------------------------------------------------------------


def test_proc_identity_capture_and_mismatch():
    identity = exact.capture_proc_identity(os.getpid())
    assert identity["pid"] == str(os.getpid())
    assert os.path.isabs(identity["exe_realpath"])
    assert len(identity["exe_sha256"]) == 64
    assert identity["parent_pid"] != ""
    assert exact.verify_proc_exe_sha(identity, identity["exe_sha256"]) == identity["exe_sha256"]
    tampered = dict(identity, exe_sha256="d" * 64)
    with pytest.raises(ValueError):
        exact.verify_proc_exe_sha(tampered, identity["exe_sha256"])
    with pytest.raises(ValueError):
        exact.verify_proc_exe_sha(identity, "d" * 64)
    with pytest.raises((ValueError, FileNotFoundError)):
        exact.capture_proc_identity(0)


def test_launch_uses_popen_absolute_path_with_proc_proof(tmp_path):
    binary = str(tmp_path / "fake-opencode")
    sha = _copy_elf(binary)
    seen = {}

    class _FakeProc:
        def __init__(self, argv, workdir, env):
            seen["argv"] = argv
            seen["env"] = dict(env)
            import subprocess as _sp
            self._proc = _sp.Popen(
                argv, cwd=workdir, stdout=_sp.PIPE, stderr=_sp.PIPE,
                text=True, env=dict(env))
            self.pid = self._proc.pid

        def communicate(self, timeout=None):
            return self._proc.communicate(timeout=timeout)

        def kill(self):
            return self._proc.kill()

        @property
        def returncode(self):
            return self._proc.returncode

    result = exact.launch_exact_opencode_abs(
        binary_abs_path=binary, args=["--version"], cwd=str(tmp_path),
        timeout=30.0, expected_sha256=sha,
        extra_env={"OPENCODE_CONFIG_CONTENT": "{}"},
        popen_factory=_FakeProc,
    )
    assert result["pid"] > 0
    assert seen["argv"][0] == binary
    assert os.path.isabs(seen["argv"][0])
    proc_identity = result["proc_identity"]
    assert proc_identity["pid"] == str(result["pid"])
    assert proc_identity["exe_sha256"] == sha
    assert result["evidence"]["artifact_id"] == "11004835952"
    assert result["evidence"]["file_sha256"] == sha
    assert result["evidence"]["pid"] == str(result["pid"])
    # Relative paths are refused before any exec.
    with pytest.raises(ValueError):
        exact.launch_exact_opencode_abs(
            binary_abs_path="relative/opencode", cwd=str(tmp_path),
            expected_sha256=sha, popen_factory=_FakeProc)


def test_worker_exact_attempt_is_scrubbed_and_absolute(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    binary = str(tmp_path / "scrub-probe")
    sha = _copy_elf(binary)
    monkeypatch.setenv("GH_TOKEN", "must-never-reach-child")
    monkeypatch.setenv("TARGET_REPO_PAT", "must-never-reach-child")
    captured = {}
    real_popen = runner_server.subprocess.Popen

    def _capturing_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env", {}) or {})
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(runner_server.subprocess, "Popen", _capturing_popen)
    result, info = manager._run_exact_opencode_attempt(
        binary_abs_path=binary,
        model=runner_server.PREFERRED_MODEL,
        task_text="probe task",
        cwd=str(tmp_path),
        workspace=str(tmp_path / "ws-job"),
        timeout=30.0,
        expected_sha256=sha,
    )
    assert captured["argv"][0] == binary
    assert os.path.isabs(captured["argv"][0])
    for secret in ("GH_TOKEN", "GITHUB_TOKEN", "TAP_PAT", "TARGET_REPO_PAT",
                   "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
                   "GITHUB_APP_INSTALLATION_ID"):
        assert captured["env"].get(secret, "") == ""
    assert info["proc_identity"]["exe_sha256"] == sha
    assert info["evidence"]["file_sha256"] == sha
    assert result.returncode == 0


def test_scrubbed_child_env_drops_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "dirty-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "dirty-github")
    monkeypatch.setenv("TAP_PAT", "dirty-tap")
    monkeypatch.setenv("TARGET_REPO_PAT", "dirty-target")
    cleaned = exact.scrubbed_child_env({"OPENCODE_CONFIG_CONTENT": "{}"})
    for secret in ("GH_TOKEN", "GITHUB_TOKEN", "TAP_PAT", "TARGET_REPO_PAT",
                   "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
                   "GITHUB_APP_INSTALLATION_ID"):
        assert cleaned.get(secret, "") == ""
    assert cleaned["OPENCODE_CONFIG_CONTENT"] == "{}"
    # Callers must not inject credentials via overrides either.
    with pytest.raises(ValueError):
        exact.scrubbed_child_env({"GH_TOKEN": "leaked"})


# ---------------------------------------------------------------------------
# Evidence survival: terminal results carry artifact + process identity.
# ---------------------------------------------------------------------------


def test_terminal_result_carries_exact_evidence(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    identity = exact.build_exact_artifact_identity()
    record = runner_server.JobRecord(
        job_id="job-1", status="running", metadata={"issue_number": 128},
        workspace=str(tmp_path), exact_artifact=dict(identity))
    manager._jobs["job-1"] = record
    evidence = {
        "schema": exact.SCHEMA, "artifact_id": "11004835952",
        "artifact_name": exact.ARTIFACT_NAME,
        "source_run_id": "36498663107",
        "archive_sha256": exact.ARCHIVE_SHA256,
        "binary_sha256": exact.EXPECTED_BINARY_SHA256,
        "version": "1.18.33", "file_path": "/abs/opencode",
        "file_sha256": exact.EXPECTED_BINARY_SHA256,
        "pid": "1234", "ppid": "1", "parent_pid": "1",
        "exe_realpath": "/abs/opencode",
        "exe_sha256": exact.EXPECTED_BINARY_SHA256,
        "cmdline": "/abs/opencode run", "argv0": "/abs/opencode",
    }
    manager._finish("job-1", "succeeded", summary="ok", error="",
                    exit_code=0, executed_model=runner_server.PREFERRED_MODEL,
                    exact_evidence=evidence)
    result = manager.to_result_dict(manager.get("job-1"))
    # Top-level + metadata copies: the controller persists the whole
    # result outside the worker, so restarts cannot erase proof.
    assert result["exact_artifact"]["artifact_id"] == "11004835952"
    assert result["exact_evidence"]["exe_sha256"] == exact.EXPECTED_BINARY_SHA256
    assert result["exact_evidence"]["pid"] == "1234"
    assert result["metadata"]["exact_evidence"]["exe_realpath"] == "/abs/opencode"
    assert result["metadata"]["exact_artifact"]["source_run_id"] == "36498663107"


def test_health_exposes_exact_expectation(tmp_path, monkeypatch):
    manager = _exact_manager(tmp_path, monkeypatch)
    snapshot = manager.health_snapshot()
    assert snapshot["exact_artifact"]["artifact_id"] == "11004835952"
    assert snapshot["exact_artifact"]["binary_sha256"] == exact.EXPECTED_BINARY_SHA256
    assert snapshot["exact_materialized"]["expected"] is True


def test_render_job_gate_allows_supported_contract():
    job = open("automation/render-job.sh", encoding="utf-8").read()
    assert "parse_exact_workflow_artifact_requirement" in job
    assert "exact_workflow_artifact_blocker" in job
    assert "EXACT_ARTIFACT_BLOCKER" in job
    assert "EXACT_IDENTITY_JSON" in job
    assert "/v1/exact-artifact" in job
    assert job.index("EXACT_ARTIFACT_BLOCKER") < job.index(
        "One service creation per attempt")
