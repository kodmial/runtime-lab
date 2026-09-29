"""Qualification contract for the corrected OpenCode PR #12 artifact (issue #126).

Stdlib only, offline by default, no git mutations, no workflow edits, no
Render service. This module pins the exact immutable corrected artifact from
``kodmial/opencode#12`` and reuses the proven Docker/cgroup measurement
vocabulary from issues #52/#105/#109 without designing a new memory harness.

Exact immutable artifact under test (fail closed, never rebuilt):

- repository ``kodmial/opencode``, PR ``#12``,
  branch ``opencode/issue11-max-headless``;
- source/head SHA ``a3c748143bbc525a7cef4f9db48e2a779418943c``;
- PR merge SHA actually built ``84fa724616e1fddeea8e7665e38568928feffdf9``;
- source workflow run ``36498663107`` (``OpenCode Coding Artifact``);
- artifact name ``opencode-coding-linux-x64``,
  artifact ID ``11004835952``;
- artifact archive digest
  ``sha256:0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040``;
- expected binary SHA-256
  ``f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966``;
- expected ``--version`` ``1.18.33``.

Material premise change vs issue #109 (which qualified artifact
``11001896223`` and recorded a provider version gate on the unstamped
``0.0.0`` binary): this is the version-stamped rebuild
(``OPENCODE_VERSION=1.18.33`` in the artifact workflow), so the free-tier
minimum-version gate no longer rejects the client before any tool runs.

Memory-harness reuse (no new harness is designed here): Docker
``--memory=512m --memory-swap=512m`` as the real cgroup-equivalent limit,
cgroup ``memory.current``/``memory.peak``/``memory.events``/swap fields,
the deterministic FOO_LIMIT representative workload, the labelled
``BUN_OPTIONS`` A/B dimension, fail-closed trial validation, and the
baseline-gap/verdict vocabulary are all imported from
:mod:`automation.opencode_max_headless_qualify`. This module only pins the
new artifact identity plus the expected binary digest/version checks.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)))
)

import opencode_max_headless_qualify as _mh

SCHEMA = "runtime-lab-corrected-qualify/v1"

FORK_REPO = "kodmial/opencode"
FORK_PR = 12
FORK_BRANCH = "opencode/issue11-max-headless"
SOURCE_SHA = "a3c748143bbc525a7cef4f9db48e2a779418943c"
MERGE_SHA = "84fa724616e1fddeea8e7665e38568928feffdf9"
SOURCE_RUN_ID = "36498663107"
SOURCE_WORKFLOW_NAME = "OpenCode Coding Artifact"
ARTIFACT_NAME = "opencode-coding-linux-x64"
ARTIFACT_ID = "11004835952"
ARCHIVE_SHA256 = "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040"
EXPECTED_BINARY_SHA256 = (
    "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966"
)
EXPECTED_VERSION = "1.18.33"
ARTIFACT_PAYLOAD = (
    "opencode-coding-linux-x64",
    "opencode-coding-linux-x64.sha256",
    "build-metadata.txt",
)
RETENTION_EXPIRY = "2026-10-28"

# Reused measurement vocabulary (single source of truth stays in _mh).
LIMIT_BYTES = _mh.LIMIT_BYTES
DOCKER_LIMITS = _mh.DOCKER_LIMITS
FREE_MODEL = _mh.FREE_MODEL
BASELINE_HOST_PEAK_KB = _mh.BASELINE_HOST_PEAK_KB
BASELINE_HOST_PEAK_BYTES = _mh.BASELINE_HOST_PEAK_BYTES
BASELINE_PROVENANCE = _mh.BASELINE_PROVENANCE

VERDICTS = _mh.VERDICTS


def validate_artifact_identity(
    artifact_id: str = ARTIFACT_ID,
    source_run_id: str = SOURCE_RUN_ID,
    source_sha: str = SOURCE_SHA,
    branch: str = FORK_BRANCH,
    pr: int = FORK_PR,
) -> dict[str, str]:
    """Fail closed unless the exact corrected artifact identity is supplied.

    The caller must pass the exact corrected artifact ID, source run ID,
    source SHA and branch. Mutable pointers (``main``, ``latest``, bare
    branch without pinned IDs) and the superseded issue-#109 artifact
    (``11001896223``) are never accepted: a substituted binary is a
    different experiment, not this qualification.
    """
    if str(artifact_id).strip() != ARTIFACT_ID:
        raise ValueError(
            "artifact_id %r does not equal the required corrected artifact %r; "
            "never substitute main/upstream/a rebuilt head or the superseded "
            "issue-109 artifact" % (artifact_id, ARTIFACT_ID))
    if str(source_run_id).strip() != SOURCE_RUN_ID:
        raise ValueError(
            "source_run_id %r does not equal the required source run %r"
            % (source_run_id, SOURCE_RUN_ID))
    if not isinstance(source_sha, str) or _mh.SHA_RE.match(source_sha.strip()) is None:
        raise ValueError("source_sha must be a 40-char lowercase SHA")
    if source_sha.strip() != SOURCE_SHA:
        raise ValueError(
            "source_sha %r does not equal the required PR #12 source %r; "
            "never silently test main/latest/upstream" % (source_sha, SOURCE_SHA))
    if branch != FORK_BRANCH:
        raise ValueError("branch must be %r, got %r" % (FORK_BRANCH, branch))
    if pr != FORK_PR:
        raise ValueError("pr must be %r, got %r" % (FORK_PR, pr))
    return {"repo": FORK_REPO, "pr": str(pr), "branch": branch,
            "source_sha": source_sha.strip(),
            "merge_sha": MERGE_SHA,
            "source_run_id": source_run_id.strip(),
            "artifact_id": ARTIFACT_ID, "artifact_name": ARTIFACT_NAME}


def artifact_api_endpoints(
    artifact_id: str = ARTIFACT_ID,
    source_run_id: str = SOURCE_RUN_ID,
) -> dict[str, str]:
    """GitHub API endpoints for the exact corrected artifact (no mutable refs)."""
    identity = validate_artifact_identity(artifact_id, source_run_id, SOURCE_SHA)
    run = identity["source_run_id"]
    artifact = identity["artifact_id"]
    return {
        "list": "repos/%s/actions/runs/%s/artifacts" % (FORK_REPO, run),
        "download": "repos/%s/actions/artifacts/%s/zip" % (FORK_REPO, artifact),
    }


def download_steps_for_artifact(workdir: str = "$WORKDIR/opencode-126") -> list[str]:
    """Reproducible shell steps downloading the exact artifact (no rebuild)."""
    endpoints = artifact_api_endpoints()
    return [
        "gh api \"%s\" --jq '.artifacts[] | {id, name, expired}'" % endpoints["list"],
        "test \"$(gh api \"%s\" --jq '.artifacts[] | select(.id == %s) | .name')\" = \"%s\""
        % (endpoints["list"], ARTIFACT_ID, ARTIFACT_NAME),
        "mkdir -p %s && gh api \"%s\" > %s/artifact.zip" % (workdir, endpoints["download"], workdir),
        "echo \"%s  %s/artifact.zip\" | sha256sum -c -" % (ARCHIVE_SHA256, workdir),
        "unzip -o %s/artifact.zip -d %s/extract" % (workdir, workdir),
        "test -f %s/extract/%s" % (workdir, _mh.BINARY_FILENAME),
        "test -f %s/extract/%s" % (workdir, _mh.CHECKSUM_FILENAME),
        "cd %s/extract && sha256sum -c %s" % (workdir, _mh.CHECKSUM_FILENAME),
        "chmod +x %s/extract/%s && %s/extract/%s --version" % (workdir, _mh.BINARY_FILENAME, workdir, _mh.BINARY_FILENAME),
    ]


def verify_expected_binary_sha256(actual_sha256: str) -> str:
    """Fail closed unless the executed binary matches the expected digest.

    The expected digest is the issue-specified constant for the corrected
    artifact; the bundled ``.sha256`` check in :mod:`_mh` remains the
    launch-time gate. Both links must hold before any memory verdict.
    """
    if not isinstance(actual_sha256, str):
        raise ValueError("binary SHA-256 must be a string")
    actual = actual_sha256.strip().lower()
    if _mh.SHA256_RE.match(actual) is None:
        raise ValueError("binary SHA-256 must be a 64-char hex digest")
    if actual != EXPECTED_BINARY_SHA256:
        raise ValueError(
            "executed binary SHA-256 %s does not equal the expected corrected "
            "digest %s; never substitute another binary" % (actual, EXPECTED_BINARY_SHA256))
    return actual


def check_version_output(version_text: str) -> str:
    """Fail closed unless ``--version`` reports the expected release."""
    if not isinstance(version_text, str) or not version_text.strip():
        raise ValueError("binary --version output is empty")
    if version_text.strip() != EXPECTED_VERSION:
        raise ValueError(
            "binary --version %r does not equal the expected %r; an unstamped "
            "build trips the free-tier minimum-version gate" % (version_text.strip(), EXPECTED_VERSION))
    return EXPECTED_VERSION


# Direct re-exports of the proven harness vocabulary (no new harness).
parse_bundled_checksum = _mh.parse_bundled_checksum
verify_binary_against_bundled = _mh.verify_binary_against_bundled
fingerprint_binary = _mh.fingerprint_binary
check_binary_help = _mh.check_binary_help
create_representative_repo = _mh.create_representative_repo
build_agent_command = _mh.build_agent_command
docker_trial_command = _mh.docker_trial_command
parse_docker_telemetry = _mh.parse_docker_telemetry
validate_trial_record = _mh.validate_trial_record
classify_result = _mh.classify_result
infrastructure_blocked = _mh.infrastructure_blocked
baseline_gap = _mh.baseline_gap
render_ab_table = _mh.render_ab_table
reject_mutable_ref = _mh.reject_mutable_ref
