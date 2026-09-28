"""Tests for the issue #77 headless inventory artifact.

Validates that automation/audits/issue-77-opencode-headless-inventory.md
exists and satisfies the issue Definition of Done at the document level:
startup trace, config-vs-source separation, specific removal candidates,
and explicit out-of-scope boundary. All assertions are offline and
secret-free.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ARTIFACT = (
    Path(__file__).resolve().parents[0]
    / "audits"
    / "issue-77-opencode-headless-inventory.md"
)

FORK_COMMIT = "9000e7fc8d96c845512f7c73122431418a71d4e4"
UPSTREAM_COMMIT = "ad6c72c7068812d43b31f3cfb9e413356a19d850"

REQUIRED_SECTIONS = (
    "startup",
    "disable switch",
    "static-import",
    "minimal required tool",
    "removal plan",
    "risk",
    "benchmark",
)

# Concrete source paths every conclusion must be traceable to.
REQUIRED_SOURCE_REFS = (
    "packages/opencode/src/index.ts",
    "packages/opencode/src/cli/cmd/run.ts",
    "packages/opencode/src/cli/effect-cmd.ts",
    "packages/opencode/src/effect/app-runtime.ts",
    "packages/opencode/src/project/bootstrap.ts",
    "packages/opencode/src/project/instance-store.ts",
    "packages/opencode/src/tool/registry.ts",
    "packages/opencode/src/server/server.ts",
)


def _text() -> str:
    assert ARTIFACT.is_file(), "missing inventory artifact: %s" % ARTIFACT
    return ARTIFACT.read_text(encoding="utf-8")


def test_artifact_references_exact_fork_commits():
    body = _text()
    assert FORK_COMMIT in body
    assert UPSTREAM_COMMIT in body
    assert "sst/opencode" in body


def test_artifact_covers_required_sections():
    lowered = _text().lower()
    for section in REQUIRED_SECTIONS:
        assert section in lowered, "artifact missing section: %s" % section


def test_artifact_points_to_concrete_source_paths():
    body = _text()
    for ref in REQUIRED_SOURCE_REFS:
        assert ref in body, "artifact missing source ref: %s" % ref


def test_artifact_separates_config_from_source_savings():
    lowered = _text().lower()
    assert "config-only" in lowered or "configuration-only" in lowered
    assert "source" in lowered
    assert "OPENCODE_PURE" in _text()
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" in _text()


def test_artifact_declares_provider_reimplementation_out_of_scope():
    lowered = _text().lower()
    assert "out of scope" in lowered
    assert "provider" in lowered


def test_artifact_records_blocked_by_status():
    body = _text()
    assert "#76" in body
