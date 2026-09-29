"""Focused regression tests for the exact PR #15 publish + delivery path (#140).

Stdlib-first, no network, no git mutations, no Render service. Proves:

- the trusted publishing path reconstructs only the exact PR #15 head
  (pinned SHA/branch/merge/build command) and fail-closes on the
  published binary SHA-256 ``d9f930c1...45f0c0`` / size ``171218400`` /
  version ``1.18.33`` (never PR #12/main/latest/installer/unstamped merge-ref);
- the staged payload is exactly the normal triple
  (``opencode-coding-linux-x64``, ``.sha256``, ``build-metadata.txt``);
- the publish-time manifest + downstream issue body carry the actual
  artifact/run/archive/binary/version fields for BOTH parsers
  (docker-qualification ``value()`` labels and the render gate);
- the handoff plan updates the downstream issue BEFORE unpausing and
  marks the issue in-progress before unpausing, dispatches exactly one Render executor, and never
  gates on the #134 marginal verdict;
- the exact delivery path accepts the PR #15 identity in addition to
  the corrected PR #12 artifact, preserves all verification/launch/
  /proc guarantees, keeps credentials off the worker, and still fails
  closed on ordinary unsupported artifacts.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import exact_artifact_delivery as exact
import opencode_pr15_publish as publish
import opencode_pr15_qualify as pr15q
import render_lifecycle as lifecycle
from render_lifecycle import ExecutionMetadata, JobRequest


PR15_BINARY = "d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0"
PR12_BINARY = "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966"
PR15_ARCHIVE = "a" * 64
PR15_ARTIFACT = "1100998877"
PR15_RUN = "36508913636"

ISSUE_PR15_BODY = """\
## Exact immutable artifact

- Repository: `kodmial/opencode`
- PR: `#15`
- Branch: `coding-no-mini`
- Source/head SHA: `842157c38db9f8178ed0eee7af32f7536fe2346e`
- Source workflow run: `36508913636`
- Artifact name: `opencode-coding-linux-x64`
- Artifact ID: `1100998877`
- Artifact archive digest: `sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`
- Expected binary SHA-256: `d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0`
- binary SHA-256: `d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0`
- Expected --version: `1.18.33`

Download exact artifact ID `1100998877` from source run `36508913636`.
"""


def _write_binary(path, size=64):
    data = os.urandom(size)
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o755)
    return data


# ---------------------------------------------------------------------------
# Publishing source + binary gates (fail closed on any substitution).
# ---------------------------------------------------------------------------


def test_publish_source_identity_is_exact_pr15():
    identity = publish.validate_publish_source()
    assert identity["source_sha"] == "842157c38db9f8178ed0eee7af32f7536fe2346e"
    assert identity["branch"] == "coding-no-mini"
    assert "script/build.ts --coding --single" in identity["build_command"]
    with pytest.raises(ValueError):
        publish.validate_publish_source(source_sha="a" * 40)
    with pytest.raises(ValueError):
        publish.validate_publish_source(branch="main")
    with pytest.raises(ValueError):
        publish.validate_publish_source(pr=12)
    # Build steps never reference a moving ref and carry the exact command.
    steps = publish.build_steps_for_publish()
    assert any(publish.BUILD_COMMAND in step for step in steps)
    assert not any(
        step.strip() in ("main", "latest") for step in steps
    )


def test_verify_built_binary_requires_sha_size_version(tmp_path):
    binary = str(tmp_path / "opencode-coding-linux-x64")
    data = _write_binary(binary, size=128)
    sha = hashlib.sha256(data).hexdigest()
    # Wrong fingerprint/size/version all fail closed even with matching file.
    with pytest.raises(ValueError):
        publish.verify_built_binary(
            binary, expected_sha256="b" * 64,
            expected_bytes=128, version_output="1.18.33",
        )
    with pytest.raises(ValueError):
        publish.verify_built_binary(
            binary, expected_sha256=sha,
            expected_bytes=999, version_output="1.18.33",
        )
    with pytest.raises(ValueError):
        publish.verify_built_binary(
            binary, expected_sha256=sha,
            expected_bytes=128, version_output="0.0.0--202609290040",
        )
    ok = publish.verify_built_binary(
        binary, expected_sha256=sha, expected_bytes=128,
        expected_version="1.18.33", version_output="1.18.33",
    )
    assert ok["binary_sha256"] == sha
    assert ok["binary_bytes"] == 128


def test_stage_payload_is_normal_triple(tmp_path, monkeypatch):
    binary = str(tmp_path / "src-bin")
    data = _write_binary(binary, size=64)
    sha = hashlib.sha256(data).hexdigest()
    out = str(tmp_path / "payload")
    # Inject the fixture digest through the verify seam so the mechanical
    # staging chain is exercised without a 171 MB binary.
    def _fake_verify(path, **kwargs):
        assert path == binary
        return {"binary_path": path, "binary_sha256": sha,
                "binary_bytes": 64, "version": "1.18.33"}
    monkeypatch.setattr(publish, "verify_built_binary", _fake_verify)
    staged = publish.stage_publish_payload(binary, out)
    assert staged["binary_sha256"] == sha
    assert set(os.listdir(out)) == set(publish.ARTIFACT_PAYLOAD)
    checksum_text = open(staged["checksum"], encoding="utf-8").read()
    assert sha in checksum_text
    assert publish.BINARY_FILENAME in checksum_text
    metadata = open(staged["metadata"], encoding="utf-8").read()
    assert "opencode_version=1.18.33" in metadata
    assert "842157c38db9f8178ed0eee7af32f7536fe2346e" in metadata


def test_manifest_captures_publish_time_transport():
    manifest = publish.build_publish_manifest(
        source_run_id=PR15_RUN, artifact_id=PR15_ARTIFACT,
        archive_sha256=PR15_ARCHIVE,
    )
    assert manifest["artifact_id"] == PR15_ARTIFACT
    assert manifest["source_run_id"] == PR15_RUN
    assert manifest["archive_sha256"] == PR15_ARCHIVE
    assert manifest["binary_sha256"] == PR15_BINARY
    assert manifest["version"] == "1.18.33"
    with pytest.raises(ValueError):
        publish.build_publish_manifest(
            source_run_id=PR15_RUN, artifact_id=PR15_ARTIFACT,
            archive_sha256=PR15_ARCHIVE, binary_sha256="c" * 64,
        )
    with pytest.raises(ValueError):
        publish.build_publish_manifest(
            source_run_id="latest", artifact_id=PR15_ARTIFACT,
            archive_sha256=PR15_ARCHIVE,
        )


def test_downstream_body_serves_both_parsers():
    manifest = publish.build_publish_manifest(
        source_run_id=PR15_RUN, artifact_id=PR15_ARTIFACT,
        archive_sha256=PR15_ARCHIVE,
    )
    body = publish.render_downstream_issue_body(manifest)
    # docker-qualification.yml value('<Label>') labels.
    for label in (
        "- Artifact ID: `1100998877`",
        "- Source workflow run: `36508913636`",
        "- Source/head SHA: `842157c38db9f8178ed0eee7af32f7536fe2346e`",
        "- Expected binary SHA-256: `d9f930",
        "- Expected --version: `1.18.33`",
    ):
        assert label in body
    assert ("sha256:%s" % PR15_ARCHIVE) in body
    # render_lifecycle gate phrasings.
    assert "artifact id" in body.lower()
    assert "workflow run" in body.lower()
    assert "binary SHA-256" in body
    assert "Download exact artifact ID `1100998877` from source run `36508913636`" in body
    # Marginal Docker evidence is cited, never a gate.
    assert "marginal" in body.lower()
    assert "must not block" in body.lower()


def test_handoff_updates_before_unpause_then_dispatches():
    manifest = publish.build_publish_manifest(
        source_run_id=PR15_RUN, artifact_id=PR15_ARTIFACT,
        archive_sha256=PR15_ARCHIVE,
    )
    plan = publish.build_handoff_plan(manifest, 141, mode="e2e")
    kinds = [" ".join(argv) for argv in plan["commands"]]
    body_idx = next(i for i, cmd in enumerate(kinds) if "--body-file" in cmd)
    in_progress_idx = next(i for i, cmd in enumerate(kinds) if "automation:in-progress" in cmd)
    qualification_idx = next(i for i, cmd in enumerate(kinds) if "qualification:render" in cmd)
    unpause_idx = next(i for i, cmd in enumerate(kinds) if "automation:paused" in cmd)
    exec_idx = next(i for i, cmd in enumerate(kinds) if "render-executor.yml" in cmd)
    assert body_idx < qualification_idx < in_progress_idx < unpause_idx < exec_idx
    assert not any("issue-scheduler.yml" in cmd for cmd in kinds)
    assert plan["dispatches"] == [{
        "workflow": "render-executor.yml",
        "ref": "main",
        "inputs": {"issue_number": "141", "mode": "e2e"},
    }]
    assert "1100998877" in plan["issue_body"]
    with pytest.raises(ValueError):
        publish.build_handoff_plan(manifest, 0)
    with pytest.raises(ValueError):
        publish.build_handoff_plan(manifest, 141, execution_label="execution:docker-qualify")
    # Dry-run executes nothing but returns the ordered argv strings.
    rendered = publish.run_handoff_plan(plan, dry_run=True)
    assert len(rendered) == len(plan["commands"])
    assert not any("issue-scheduler.yml" in line for line in rendered)
    assert any("render-executor.yml" in line for line in rendered)


# ---------------------------------------------------------------------------
# Delivery: PR #15 accepted alongside PR #12, unsupported still refused.
# ---------------------------------------------------------------------------


def test_delivery_accepts_pr15_and_preserves_pr12():
    pr12 = exact.build_exact_artifact_identity()
    assert exact.validate_exact_identity(dict(pr12))["artifact_id"] == "11004835952"
    pr15 = exact.build_pr15_artifact_identity(PR15_ARTIFACT, PR15_RUN, PR15_ARCHIVE)
    assert pr15["binary_sha256"] == PR15_BINARY
    assert pr15["version"] == "1.18.33"
    validated = exact.validate_exact_identity(dict(pr15))
    assert validated["artifact_id"] == PR15_ARTIFACT
    assert validated["source_sha"] == "842157c38db9f8178ed0eee7af32f7536fe2346e"
    # Conflicting provenance is rejected, not silently accepted.
    tampered = dict(pr15, source_sha="d" * 40)
    with pytest.raises(ValueError):
        exact.validate_exact_identity(tampered)
    # Ordinary unsupported artifacts still fail closed.
    with pytest.raises(ValueError):
        exact.validate_exact_identity({"artifact_id": "12345678"})
    with pytest.raises(ValueError):
        exact.validate_exact_identity(
            dict(pr15, binary_sha256="e" * 64)
        )
    # A PR #15 binary claim under the PR #12 transport is not supported.
    with pytest.raises(ValueError):
        exact.validate_exact_identity({
            "artifact_id": "11004835952",
            "artifact_name": "opencode-coding-linux-x64",
            "source_run_id": "36498663107",
            "archive_sha256": "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040",
            "binary_sha256": PR15_BINARY,
            "version": "1.18.33",
        })


def test_requirement_gate_needs_pr15_binary():
    requirement = lifecycle.parse_exact_workflow_artifact_requirement("t", ISSUE_PR15_BODY)
    assert requirement is not None
    binary = lifecycle.parse_exact_binary_sha256("t", ISSUE_PR15_BODY)
    assert binary == PR15_BINARY
    assert lifecycle.is_supported_pr15_workflow_artifact(requirement, binary)
    assert lifecycle.is_supported_exact_workflow_artifact(requirement, binary)
    # Without the binary digest the same numeric transport is unsupported.
    assert not lifecycle.is_supported_pr15_workflow_artifact(requirement, "")
    assert not lifecycle.is_supported_exact_workflow_artifact(requirement, "")
    assert not lifecycle.is_supported_exact_workflow_artifact(requirement, "f" * 64)
    req, blocker, identity = lifecycle.exact_artifact_gate_decision("t", ISSUE_PR15_BODY)
    assert req is not None
    assert blocker == ""
    assert identity is not None
    assert identity["artifact_id"] == PR15_ARTIFACT
    assert identity["binary_sha256"] == PR15_BINARY
    assert identity["version"] == "1.18.33"
    # PR #12 still passes through unchanged.
    pr12_body = open("automation/test_exact_artifact_delivery.py", encoding="utf-8").read()
    assert "11004835952" in pr12_body
    # Ordinary smoke stays untouched; random artifacts still refused.
    req2, blocker2, identity2 = lifecycle.exact_artifact_gate_decision(
        "P0: Fresh smoke check", "Run the normal smoke workload. No artifact pinning."
    )
    assert req2 is None and blocker2 == "" and identity2 is None
    bad = (
        "artifact ID: `123456789` source workflow run: `36500000000` "
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    req3, blocker3, identity3 = lifecycle.exact_artifact_gate_decision("t", bad)
    assert req3 is not None and identity3 is None
    assert "infrastructure-blocked" in blocker3


def test_pr15_identity_flows_through_submit_and_create(tmp_path):
    pr15 = exact.build_pr15_artifact_identity(PR15_ARTIFACT, PR15_RUN, PR15_ARCHIVE)
    req = JobRequest(
        task_text="do work", issue_number=141,
        metadata=ExecutionMetadata(issue_number=141), exact_artifact=pr15,
    )
    body = req.to_dict()
    assert body["exact_artifact"]["artifact_id"] == PR15_ARTIFACT
    assert body["exact_artifact"]["binary_sha256"] == PR15_BINARY
    payload = lifecycle.build_create_service_payload(
        name="runtime-lab-issue141-test", owner_id="owner-1", exact_artifact=pr15,
    )
    start = payload["serviceDetails"]["envSpecificDetails"]["startCommand"]
    assert ("OPENCODE_EXACT_ARTIFACT_ID=%s" % PR15_ARTIFACT) in start
    assert ("OPENCODE_EXACT_ARTIFACT_SHA256=%s" % PR15_BINARY) in start
    assert ("OPENCODE_EXACT_ARCHIVE_SHA256=%s" % PR15_ARCHIVE) in start
    assert ("OPENCODE_EXACT_SOURCE_RUN=%s" % PR15_RUN) in start
    with pytest.raises(ValueError):
        JobRequest(
            task_text="x", issue_number=2,
            metadata=ExecutionMetadata(issue_number=2),
            exact_artifact={"artifact_id": "12345678"},
        )
    with pytest.raises(ValueError):
        lifecycle.build_create_service_payload(
            name="n", owner_id="o", exact_artifact={"artifact_id": "12345678"})
    with pytest.raises(ValueError):
        lifecycle.build_create_service_payload(
            name="n", owner_id="o",
            opencode_artifact_id="opencode-x", opencode_artifact_sha256="a" * 64,
            exact_artifact=pr15)


def test_abs_path_and_evidence_cover_pr15(tmp_path):
    path = exact.exact_artifact_abs_path(PR15_ARTIFACT, base_dir=str(tmp_path))
    assert path.endswith(".opencode-exact-workflow/1100998877/opencode")
    with pytest.raises(ValueError):
        exact.exact_artifact_abs_path("../escape", base_dir=str(tmp_path))
    with pytest.raises(ValueError):
        exact.exact_artifact_abs_path("latest", base_dir=str(tmp_path))
    evidence = exact.build_exact_evidence(
        path, PR15_BINARY,
        {"pid": "7", "ppid": "1", "parent_pid": "1",
         "exe_realpath": path, "exe_sha256": PR15_BINARY,
         "cmdline": "%s run" % path},
        [path, "run"], version="1.18.33",
    )
    assert evidence["binary_sha256"] == PR15_BINARY
    assert evidence["source_sha"] == "842157c38db9f8178ed0eee7af32f7536fe2346e"
    assert evidence["version"] == "1.18.33"
    # PR #12 evidence path is unchanged.
    pr12_evidence = exact.build_exact_evidence(
        "/abs/opencode", PR12_BINARY,
        {"pid": "1", "ppid": "0", "parent_pid": "0",
         "exe_realpath": "/abs/opencode", "exe_sha256": PR12_BINARY,
         "cmdline": "/abs/opencode run"},
        ["/abs/opencode", "run"], version="1.18.33",
    )
    assert pr12_evidence["artifact_id"] == "11004835952"


def test_pr15_qualify_fingerprint_still_pinned():
    assert pr15q.BINARY_SHA256 == PR15_BINARY
    assert pr15q.BINARY_BYTES == 171218400
    assert pr15q.SOURCE_SHA == "842157c38db9f8178ed0eee7af32f7536fe2346e"
    assert pr15q.EXPECTED_VERSION == "1.18.33"


# ---------------------------------------------------------------------------
# Issue #141: the published Render qualification transport.
# ---------------------------------------------------------------------------

ISSUE_141_ARTIFACT = "11009286301"
ISSUE_141_RUN = "36512250023"
ISSUE_141_ARCHIVE = (
    "df547ac873c9591bc98e5ef43b9283b77f5a4295fc2f27cda6b280d18c313c46"
)


def test_issue141_exact_transport_passes_gate_and_matches_pins():
    """Issue #141 must ride the exact-artifact path, never the refusal gate.

    The published PR #15 artifact (``11009286301`` from source run
    ``36512250023``, archive ``sha256:df547ac8...313c46``) carries the
    pinned binary ``d9f930c1...45f0c0`` / ``1.18.33`` / 171218400 B, so
    the pre-creation gate must select delivery (empty blocker, full
    identity) instead of refusing before any worker exists. The #134
    Docker ``marginal`` verdict is evidence only and must not block it.
    """
    body = (
        "- Artifact ID: `%s`\n"
        "- Source workflow run: `%s`\n"
        "- Artifact archive digest: `sha256:%s`\n"
        "- Expected binary SHA-256: `%s`\n"
        "- binary SHA-256: `%s`\n"
        "- Expected --version: `1.18.33`\n"
        "Download exact artifact ID `%s` from source run `%s`.\n"
        % (
            ISSUE_141_ARTIFACT, ISSUE_141_RUN, ISSUE_141_ARCHIVE,
            PR15_BINARY, PR15_BINARY,
            ISSUE_141_ARTIFACT, ISSUE_141_RUN,
        )
    )
    requirement = lifecycle.parse_exact_workflow_artifact_requirement("t", body)
    assert requirement is not None
    assert requirement["artifact_id"] == ISSUE_141_ARTIFACT
    assert requirement["source_run_id"] == ISSUE_141_RUN
    assert requirement["archive_sha256"] == ISSUE_141_ARCHIVE
    binary = lifecycle.parse_exact_binary_sha256("t", body)
    assert binary == PR15_BINARY
    assert lifecycle.is_supported_pr15_workflow_artifact(requirement, binary)
    assert lifecycle.is_supported_exact_workflow_artifact(requirement, binary)
    req, blocker, identity = lifecycle.exact_artifact_gate_decision("t", body)
    assert req is not None
    assert blocker == ""
    assert identity is not None
    assert identity["artifact_id"] == ISSUE_141_ARTIFACT
    assert identity["source_run_id"] == ISSUE_141_RUN
    assert identity["archive_sha256"] == ISSUE_141_ARCHIVE
    assert identity["binary_sha256"] == PR15_BINARY
    assert identity["version"] == "1.18.33"
    assert identity["source_sha"] == "842157c38db9f8178ed0eee7af32f7536fe2346e"
    # The validated delivery identity agrees byte-for-byte.
    delivered = exact.validate_exact_identity(dict(identity))
    assert delivered["artifact_id"] == ISSUE_141_ARTIFACT
    assert delivered["binary_sha256"] == PR15_BINARY
    # Size/version pins agree across the publish and qualify contracts.
    assert publish.BINARY_BYTES == 171218400
    assert pr15q.BINARY_BYTES == 171218400
    assert publish.BINARY_SHA256 == pr15q.BINARY_SHA256 == PR15_BINARY
    # The downstream body renderer reproduces a gate-passing body for it.
    manifest = publish.build_publish_manifest(
        source_run_id=ISSUE_141_RUN, artifact_id=ISSUE_141_ARTIFACT,
        archive_sha256=ISSUE_141_ARCHIVE,
    )
    rendered = publish.render_downstream_issue_body(manifest)
    req2, blocker2, identity2 = lifecycle.exact_artifact_gate_decision("t", rendered)
    assert blocker2 == "" and identity2 is not None
    assert identity2["artifact_id"] == ISSUE_141_ARTIFACT
    assert identity2["binary_sha256"] == PR15_BINARY
