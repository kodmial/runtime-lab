"""Qualification contract for the OpenCode PR #15 coding build (issue #134).

Stdlib only, offline by default, no git mutations, no workflow edits, no
Render service, no Render reservation. This module pins the exact source
under test from ``kodmial/opencode#15`` and reuses the proven Docker/cgroup
measurement vocabulary from issues #52/#105/#109/#126 without designing a
new memory harness.

Exact source under test (fail closed, never substituted):

- repository ``kodmial/opencode``, PR ``#15``,
  branch ``coding-no-mini``;
- PR head SHA ``842157c38db9f8178ed0eee7af32f7536fe2346e``
  (the commit actually built here);
- PR merge SHA ``0d649350557c5ee3882cc55e5ca65f919ab304c4``
  (context only; the merge ref built by Continuum CI is unstamped and
  therefore NOT the qualification binary);
- source package version ``1.18.33``.

Build rule (fail closed): the exact PR head is compiled with
``OPENCODE_VERSION=1.18.33`` and the resulting binary must report exactly
``1.18.33`` via ``--version``. The Continuum CI merge-ref binary reports
``0.0.0--202609290040`` and trips the free-tier minimum-version gate, so
it is never accepted here. No PR #15 source file is modified for this
qualification.

Memory-harness reuse (no new harness is designed here): Docker
``--memory=512m --memory-swap=512m`` as the real cgroup-equivalent limit,
cgroup ``memory.current``/``memory.peak``/``memory.events``/swap fields,
the deterministic FOO_LIMIT representative workload, fail-closed trial
validation, and the baseline-gap/verdict vocabulary are all imported from
:mod:`automation.opencode_max_headless_qualify`. This module only pins the
PR #15 source identity plus the build/version gates.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)))
)

import opencode_max_headless_qualify as _mh

SCHEMA = "runtime-lab-pr15-qualify/v1"

FORK_REPO = "kodmial/opencode"
FORK_PR = 15
FORK_BRANCH = "coding-no-mini"
SOURCE_SHA = "842157c38db9f8178ed0eee7af32f7536fe2346e"
MERGE_SHA = "0d649350557c5ee3882cc55e5ca65f919ab304c4"
SOURCE_PACKAGE_VERSION = "1.18.33"
EXPECTED_VERSION = "1.18.33"

# Exact build command executed from the extracted PR-head source root
# (``/tmp/opencode15/src/kodmial-opencode-842157c``). ``--coding`` enables
# the PR #15 mini-runtime fold-out in ``script/build.ts``; ``--single``
# restricts the matrix to the current platform (linux-x64) without
# changing the binary content for that target.
BUILD_COMMAND = (
    "OPENCODE_VERSION=1.18.33 bun run --cwd packages/opencode "
    "script/build.ts --coding --single"
)
BUILD_TOOLCHAIN_BUN = "1.4.2"

# Fingerprinted qualification binary (built, never downloaded).
BINARY_SHA256 = (
    "d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0"
)
BINARY_BYTES = 171222496

# Reused measurement vocabulary (single source of truth stays in _mh).
LIMIT_BYTES = _mh.LIMIT_BYTES
DOCKER_LIMITS = _mh.DOCKER_LIMITS
FREE_MODEL = _mh.FREE_MODEL
BASELINE_HOST_PEAK_KB = _mh.BASELINE_HOST_PEAK_KB
BASELINE_HOST_PEAK_BYTES = _mh.BASELINE_HOST_PEAK_BYTES
BASELINE_PROVENANCE = _mh.BASELINE_PROVENANCE

VERDICTS = _mh.VERDICTS


def validate_source_identity(
    source_sha: str = SOURCE_SHA,
    merge_sha: str = MERGE_SHA,
    branch: str = FORK_BRANCH,
    pr: int = FORK_PR,
) -> dict[str, str]:
    """Fail closed unless the exact PR #15 source identity is supplied.

    The caller must pass the exact PR head SHA, the observed merge SHA
    (context), branch and PR number. Mutable pointers (``main``,
    ``latest``, bare branch without pinned SHAs) and any other PR/ref are
    never accepted: a substituted source is a different experiment, not
    this qualification.
    """
    if not isinstance(source_sha, str) or _mh.SHA_RE.match(source_sha.strip()) is None:
        raise ValueError("source_sha must be a 40-char lowercase SHA")
    if source_sha.strip() != SOURCE_SHA:
        raise ValueError(
            "source_sha %r does not equal the required PR #15 head %r; "
            "never silently test main/latest/upstream/another PR"
            % (source_sha, SOURCE_SHA)
        )
    if not isinstance(merge_sha, str) or _mh.SHA_RE.match(merge_sha.strip()) is None:
        raise ValueError("merge_sha must be a 40-char lowercase SHA")
    if merge_sha.strip() != MERGE_SHA:
        raise ValueError(
            "merge_sha %r does not equal the observed PR #15 merge %r"
            % (merge_sha, MERGE_SHA)
        )
    if branch != FORK_BRANCH:
        raise ValueError("branch must be %r, got %r" % (FORK_BRANCH, branch))
    if pr != FORK_PR:
        raise ValueError("pr must be %r, got %r" % (FORK_PR, pr))
    return {
        "repo": FORK_REPO,
        "pr": str(pr),
        "branch": branch,
        "source_sha": source_sha.strip(),
        "merge_sha": merge_sha.strip(),
        "source_package_version": SOURCE_PACKAGE_VERSION,
        "build_command": BUILD_COMMAND,
    }


def build_steps_for_source(workdir: str = "$WORKDIR/opencode-134") -> list[str]:
    """Reproducible shell steps building the exact PR head (no PR edits)."""
    return [
        "gh api repos/%s/pulls/%d --jq '{head: .head.sha, merge: .merge_commit_sha}'"
        % (FORK_REPO, FORK_PR),
        "test \"$(gh api repos/%s/pulls/%d --jq '.head.sha')\" = \"%s\""
        % (FORK_REPO, FORK_PR, SOURCE_SHA),
        "download the exact-head tarball "
        "https://api.github.com/repos/%s/tarball/%s "
        "(no git clone/checkout; verify the PR #15 file markers after extract)"
        % (FORK_REPO, SOURCE_SHA),
        "bun install --frozen-lockfile  # from the extracted source root",
        BUILD_COMMAND,
        "%s --version  # must print exactly %s"
        % ("<built-binary>", EXPECTED_VERSION),
    ]


def verify_built_binary_sha256(actual_sha256: str) -> str:
    """Fail closed unless the built binary matches the fingerprinted digest."""
    if not isinstance(actual_sha256, str):
        raise ValueError("binary SHA-256 must be a string")
    actual = actual_sha256.strip().lower()
    if _mh.SHA256_RE.match(actual) is None:
        raise ValueError("binary SHA-256 must be a 64-char hex digest")
    if actual != BINARY_SHA256:
        raise ValueError(
            "built binary SHA-256 %s does not equal the fingerprinted %s; "
            "never substitute another binary" % (actual, BINARY_SHA256)
        )
    return actual


def check_version_output(version_text: str) -> str:
    """Fail closed unless ``--version`` reports exactly ``1.18.33``.

    The unstamped Continuum CI merge-ref binary reports
    ``0.0.0--202609290040`` and is rejected here: it trips the free-tier
    minimum-version gate before any tool runs.
    """
    if not isinstance(version_text, str) or not version_text.strip():
        raise ValueError("binary --version output is empty")
    if version_text.strip() != EXPECTED_VERSION:
        raise ValueError(
            "binary --version %r does not equal the expected %r; an unstamped "
            "build trips the free-tier minimum-version gate" % (version_text.strip(), EXPECTED_VERSION)
        )
    return EXPECTED_VERSION


# Direct re-exports of the proven harness vocabulary (no new harness).
fingerprint_binary = _mh.fingerprint_binary
sha256_of_file = _mh.sha256_of_file
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
