"""Regression coverage for the issue-#118 Render repair (source issue #106).

The production delivery path for the corrected ``kodmial/opencode`` PR #12
artifact ``11004835952`` lives in ``automation/exact_artifact_delivery.py``
(owned by issue #128). This module pins that same contract to the exact
#118 repair phrasing -- ``runtime-lab-render-repair source=106`` for failed
run ``36498162815`` -- and proves the repair is a working delivery path,
not a strengthened ``infrastructure-blocked`` gate assertion:

- the #106/#118 repair body selects the supported identity (empty blocker);
- the identity flows through the submit body and the create start command;
- verified bytes materialize at the deterministic absolute path;
- exact mode launches by absolute path with zero fallback and records
  ``/proc/<pid>/exe`` identity from a real local ELF child;
- missing/mismatched bytes fail closed before any exec.
"""

import hashlib
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import exact_artifact_delivery as exact
import render_lifecycle as lifecycle
from render_lifecycle import ExecutionMetadata, JobRequest


ISSUE_118_TITLE = "P0: Repair Render execution failure for #106 (run 36498162815)"

ISSUE_118_BODY = """\
<!-- runtime-lab-render-repair source=106 -->
<!-- runtime-lab-render-failed-run=36498162815 -->

## Goal

- repository: `kodmial/opencode`
- PR: `#12`
- source/head SHA: `a3c748143bbc525a7cef4f9db48e2a779418943c`
- PR merge SHA actually built: `84fa724616e1fddeea8e7665e38568928feffdf9`
- source workflow run: `36498663107`
- artifact name: `opencode-coding-linux-x64`
- artifact ID: `11004835952`
- artifact ZIP digest: `sha256:0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040`
- binary SHA-256: `f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966`
- required `--version`: `1.18.33`
"""


def test_118_repair_body_selects_supported_delivery_identity():
    requirement = lifecycle.parse_exact_workflow_artifact_requirement(
        ISSUE_118_TITLE, ISSUE_118_BODY
    )
    assert requirement is not None
    assert requirement["artifact_id"] == "11004835952"
    assert requirement["source_run_id"] == "36498663107"
    assert (
        requirement["archive_sha256"]
        == "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040"
    )
    binary_sha = lifecycle.parse_exact_binary_sha256(ISSUE_118_TITLE, ISSUE_118_BODY)
    assert binary_sha == exact.EXPECTED_BINARY_SHA256
    assert lifecycle.is_supported_exact_workflow_artifact(requirement, binary_sha)
    req, blocker, identity = lifecycle.exact_artifact_gate_decision(
        ISSUE_118_TITLE, ISSUE_118_BODY
    )
    # The repair must DELIVER, not refuse: an infrastructure-blocked
    # blocker here would mean the repair regressed to gate-only behavior.
    assert req is not None
    assert blocker == ""
    assert identity is not None
    assert identity["artifact_id"] == "11004835952"
    assert identity["binary_sha256"] == exact.EXPECTED_BINARY_SHA256
    assert identity["version"] == "1.18.33"


def test_118_identity_flows_through_submit_and_create_payloads():
    _, _, identity = lifecycle.exact_artifact_gate_decision(
        ISSUE_118_TITLE, ISSUE_118_BODY
    )
    assert identity is not None
    req = JobRequest(
        task_text="repair #106 render execution",
        issue_number=118,
        metadata=ExecutionMetadata(issue_number=118),
        exact_artifact=identity,
    )
    body = req.to_dict()
    assert body["exact_artifact"]["artifact_id"] == "11004835952"
    assert body["exact_artifact"]["binary_sha256"] == exact.EXPECTED_BINARY_SHA256
    payload = lifecycle.build_create_service_payload(
        name="runtime-lab-issue118-test",
        owner_id="owner-1",
        exact_artifact=identity,
    )
    start = payload["serviceDetails"]["envSpecificDetails"]["startCommand"]
    assert "OPENCODE_EXACT_ARTIFACT_ID=11004835952" in start
    assert ("OPENCODE_EXACT_ARTIFACT_SHA256=%s" % exact.EXPECTED_BINARY_SHA256) in start
    assert ("OPENCODE_EXACT_ARCHIVE_SHA256=%s" % exact.ARCHIVE_SHA256) in start
    assert "OPENCODE_EXACT_SOURCE_RUN=36498663107" in start


def test_118_absolute_path_has_zero_fallback(tmp_path):
    dest = exact.exact_artifact_abs_path("11004835952", base_dir=str(tmp_path))
    assert os.path.isabs(dest)
    assert dest.endswith(
        os.path.join(".opencode-exact-workflow", "11004835952", "opencode")
    )
    data = b"issue-118-verified-bytes"
    sha = hashlib.sha256(data).hexdigest()
    path, actual = exact.materialize_exact_bytes(data, dest, sha)
    assert path == dest
    assert actual == sha
    assert os.access(dest, os.X_OK)
    # Foreign bytes under the pinned identity fail closed.
    with open(dest, "rb") as handle:
        assert handle.read() == data
    try:
        exact.verify_binary_file(dest, exact.EXPECTED_BINARY_SHA256)
    except ValueError:
        pass
    else:
        raise AssertionError("fixture bytes must not match the pinned digest")
    # The superseded artifact stays rejected at the path layer.
    try:
        exact.exact_artifact_abs_path("11001896223", base_dir=str(tmp_path))
    except ValueError:
        pass
    else:
        raise AssertionError("superseded artifact must stay fail-closed")


def test_118_proc_identity_from_real_elf_child(tmp_path):
    binary = str(tmp_path / "issue-118-probe")
    shutil.copy("/bin/true", binary)
    os.chmod(binary, 0o755)
    digest = hashlib.sha256()
    with open(binary, "rb") as handle:
        digest.update(handle.read())
    sha = digest.hexdigest()
    seen = {}

    class _FakeProc:
        def __init__(self, argv, workdir, env):
            seen["argv"] = argv
            seen["env"] = dict(env)
            import subprocess as _sp

            self._proc = _sp.Popen(
                argv, cwd=workdir, stdout=_sp.PIPE, stderr=_sp.PIPE,
                text=True, env=dict(env),
            )
            self.pid = self._proc.pid

        def communicate(self, timeout=None):
            return self._proc.communicate(timeout=timeout)

        def kill(self):
            return self._proc.kill()

        @property
        def returncode(self):
            return self._proc.returncode

    result = exact.launch_exact_opencode_abs(
        binary_abs_path=binary,
        args=["--version"],
        cwd=str(tmp_path),
        timeout=30.0,
        expected_sha256=sha,
        popen_factory=_FakeProc,
    )
    assert result["pid"] > 0
    assert seen["argv"][0] == binary
    assert os.path.isabs(seen["argv"][0])
    proc_identity = result["proc_identity"]
    assert proc_identity["pid"] == str(result["pid"])
    # File SHA and actually-executing /proc SHA are separate fields but
    # agree for the genuine child.
    assert result["file_sha256"] == sha
    assert proc_identity["exe_sha256"] == sha
    assert os.path.isabs(proc_identity["exe_realpath"])
    # Mismatch and relative-path launches fail closed.
    try:
        exact.launch_exact_opencode_abs(
            binary_abs_path=binary,
            cwd=str(tmp_path),
            expected_sha256="d" * 64,
            popen_factory=_FakeProc,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("executing-SHA mismatch must fail closed")
    try:
        exact.launch_exact_opencode_abs(
            binary_abs_path="relative/opencode",
            cwd=str(tmp_path),
            expected_sha256=sha,
            popen_factory=_FakeProc,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("relative launch path must fail closed")


def test_118_missing_artifact_fails_closed_pre_creation(tmp_path, monkeypatch):
    import runner_server

    monkeypatch.setenv("OPENCODE_EXACT_ARTIFACT_DIR", str(tmp_path / "exact-root"))
    for name in ("OPENCODE_ARTIFACT_ID", "OPENCODE_ARTIFACT_SHA256", "OPENCODE_ARTIFACT_REF"):
        monkeypatch.delenv(name, raising=False)
    manager = runner_server.JobManager(
        workspace_root=str(tmp_path / "ws"),
        command_runner=runner_server.SubprocessCommandRunner(),
        requested_exact_artifact=exact.build_exact_artifact_identity(),
    )
    identity = exact.build_exact_artifact_identity()
    try:
        manager._ensure_exact_binary(identity)
    except FileNotFoundError as exc:
        assert "11004835952" in str(exc)
        assert "/v1/exact-artifact" in str(exc)
    else:
        raise AssertionError("missing exact bytes must fail closed pre-creation")
