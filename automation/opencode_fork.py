"""OpenCode source-fork baseline helpers (issue #76).

The fork ``kodmial/opencode`` is only the OpenCode source fork used when
OpenCode itself must be changed. All issues, priorities, dependencies,
experiments and acceptance criteria belong in ``kodmial/runtime-lab``.
This module is stdlib-only and performs no network or git mutations; it
validates the machine-readable baseline at
``automation/opencode-fork.baseline.json`` and documents how to verify
the fork builds the same headless CLI artifact we benchmark.

Upstream identity: ``anomalyco/opencode`` (successor of ``sst/opencode``;
the ``sst/opencode`` repository name now resolves to it) is the source
the fork mirrors. History is preserved: the fork contains the upstream
base commit and the pinned release tag commit, so future syncs can
``git fetch upstream`` and rebase/merge onto the recorded SHAs.
"""

from __future__ import annotations

import json
import os
import re

BASELINE_SCHEMA = "runtime-lab-opencode-fork-baseline/v1"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

REQUIRED_KEYS = frozenset(
    (
        "schema",
        "fork_repo",
        "fork_branch",
        "fork_sha",
        "upstream_repo",
        "upstream_base_commit",
        "upstream_tag",
        "upstream_tag_commit",
        "pinned_opencode_version",
        "ownership",
        "history",
        "sync",
        "verified",
        "note",
    )
)

# Files that must exist on the fork default branch for the normal headless
# CLI artifact (``opencode run``) to build. Checked live via the Contents
# API during the bootstrap run; kept here so future pins can re-verify the
# same contract without cloning.
HEADLESS_REQUIRED_PATHS = (
    "packages/opencode/package.json",
    "packages/opencode/src/index.ts",
    "packages/opencode/src/cli/cmd/run.ts",
    "packages/opencode/script/build.ts",
    "bun.lock",
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def baseline_path() -> str:
    """Return the absolute path of the machine-readable fork baseline."""
    return os.path.join(_repo_root(), "automation", "opencode-fork.baseline.json")


def load_baseline(path: str | None = None) -> dict:
    """Load and validate the fork baseline; fail closed on any defect."""
    target = path or baseline_path()
    with open(target, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return validate_baseline(data)


def validate_baseline(data: dict) -> dict:
    """Validate a parsed baseline object and return it on success."""
    if not isinstance(data, dict):
        raise ValueError("baseline must be a JSON object")
    missing = sorted(REQUIRED_KEYS - set(data.keys()))
    extra = sorted(set(data.keys()) - REQUIRED_KEYS)
    if missing or extra:
        raise ValueError(
            "baseline keys mismatch: missing=%s extra=%s" % (missing, extra)
        )
    if data.get("schema") != BASELINE_SCHEMA:
        raise ValueError("invalid baseline schema: %r" % data.get("schema"))
    for key in ("fork_repo", "upstream_repo"):
        value = data.get(key)
        if not isinstance(value, str) or REPO_RE.match(value) is None:
            raise ValueError("invalid repository name for %s: %r" % (key, value))
    branch = data.get("fork_branch")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("fork_branch must be a non-empty string")
    for key in ("fork_sha", "upstream_base_commit", "upstream_tag_commit"):
        value = data.get(key)
        if not isinstance(value, str) or SHA_RE.match(value) is None:
            raise ValueError(
                "invalid 40-char lowercase SHA for %s: %r" % (key, value)
            )
    tag = data.get("upstream_tag")
    version = data.get("pinned_opencode_version")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("upstream_tag must start with 'v': %r" % tag)
    if not isinstance(version, str) or VERSION_RE.match(version) is None:
        raise ValueError("invalid pinned_opencode_version: %r" % version)
    if tag != "v%s" % version:
        raise ValueError(
            "upstream_tag %r does not match pinned version %r" % (tag, version)
        )
    ownership = data.get("ownership")
    if not isinstance(ownership, dict):
        raise ValueError("ownership must be an object")
    if ownership.get("tracking_repo") != "kodmial/runtime-lab":
        raise ValueError("tracking must remain in kodmial/runtime-lab")
    if ownership.get("fork_role") != "source fork only":
        raise ValueError("fork_role must be 'source fork only'")
    if ownership.get("requires_fork_backlog") is not False:
        raise ValueError("fork must not require a separate backlog")
    history = data.get("history")
    if not isinstance(history, dict) or history.get("preserved") is not True:
        raise ValueError("history.preserved must be true")
    if history.get("fork_contains_upstream_base_commit") is not True:
        raise ValueError("fork must contain the upstream base commit")
    if history.get("fork_contains_upstream_tag_commit") is not True:
        raise ValueError("fork must contain the upstream tag commit")
    sync = data.get("sync")
    if not isinstance(sync, dict) or not str(sync.get("upstream_remote", "")).strip():
        raise ValueError("sync.upstream_remote must be a non-empty string")
    verified = data.get("verified")
    if not isinstance(verified, dict):
        raise ValueError("verified must be an object")
    if verified.get("fork_package_version") != version:
        raise ValueError("verified fork package version must equal pinned version")
    note = data.get("note")
    if not isinstance(note, str) or "kodmial/runtime-lab" not in note:
        raise ValueError("note must point tracking at kodmial/runtime-lab")
    return data


def check_pinned_version_consistent(baseline: dict, pinned: str) -> None:
    """Fail closed when the baseline pin differs from the runner pin."""
    expected = baseline.get("pinned_opencode_version")
    if pinned != expected:
        raise ValueError(
            "pinned version mismatch: runner=%r baseline=%r" % (pinned, expected)
        )


def validate_fork_manifest(manifest: dict, pinned_version: str) -> None:
    """Validate a fork ``packages/opencode/package.json`` manifest shape.

    Checks the minimal headless-build contract: version equals the pinned
    release, a ``build`` script exists, the ``opencode`` bin entry exists,
    and the manifest is the workspace ``opencode`` package.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("name") != "opencode":
        raise ValueError("manifest name must be 'opencode'")
    if manifest.get("version") != pinned_version:
        raise ValueError(
            "manifest version %r does not match pinned %r"
            % (manifest.get("version"), pinned_version)
        )
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict) or not str(scripts.get("build", "")).strip():
        raise ValueError("manifest must define scripts.build")
    bin_entry = manifest.get("bin")
    if not isinstance(bin_entry, dict) or not str(
        bin_entry.get("opencode", "")
    ).strip():
        raise ValueError("manifest must define bin.opencode")


def sync_instructions(baseline: dict) -> list[str]:
    """Return rebase/merge-safe sync steps referencing the baseline SHAs."""
    upstream = baseline["upstream_repo"]
    base = baseline["upstream_base_commit"]
    fork_branch = baseline["fork_branch"]
    return [
        "git remote add upstream https://github.com/%s.git" % upstream,
        "git fetch upstream",
        "git checkout %s" % fork_branch,
        "git merge --ff-only upstream/dev || git rebase --onto %s upstream/dev~0" % base,
        "git rev-list --contains %s HEAD # must succeed: history preserved" % base,
    ]
