"""Offline tests for the schema-validated experiment catalog (issue #34).

All tests are stdlib-only, require no network, GitHub API access, Render
credentials, or running service, and never write into the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from automation.knowledge_catalog import (
    CATALOG_SCHEMA_REF,
    CATALOG_SCHEMA_VERSION,
    EXPERIMENT_SCHEMA_REF,
    EXPERIMENT_SCHEMA_VERSION,
    CatalogError,
    build_catalog,
    check_cross_record_invariants,
    derive_record_id,
    dump_catalog,
    filter_records,
    load_records,
    main,
    parse_record_text,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENTS_DIR = os.path.join(
    REPO_ROOT, "automation", "knowledge", "experiments"
)
SCHEMA_DIR = os.path.join(REPO_ROOT, "automation", "knowledge", "schema")

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

BODY_SECTIONS = (
    "## Hypothesis / objective",
    "## Prior knowledge consulted",
    "## Preconditions / changed premise",
    "## Procedure",
    "## Observations",
    "## Interpretation",
    "## Decision / result",
    "## Validation",
    "## Reusable knowledge",
    "## Unresolved questions / next experiment",
    "## Evidence",
    "## Cleanup proof",
)


def _metadata(issue=9, run_id="r1", **overrides):
    record = {
        "$schema": EXPERIMENT_SCHEMA_REF,
        "base_commit": "a" * 40,
        "issue": issue,
        "outcome": "succeeded",
        "record_id": derive_record_id(issue, str(run_id)),
        "run_id": str(run_id),
        "schema": EXPERIMENT_SCHEMA_VERSION,
        "supersedes": [],
        "topic": "test-topic",
    }
    record.update(overrides)
    return record


def _record_text(issue=9, run_id="r1", title="Test record", **overrides):
    lines = ["---", json.dumps(_metadata(issue, run_id, **overrides),
                               sort_keys=True, indent=2),
             "---", "", "# %s" % title, ""]
    lines.extend(BODY_SECTIONS)
    lines.append("evidence")
    return "\n".join(lines) + "\n"


def _write(directory, issue, rid, **overrides):
    meta_run = overrides.pop("run_id", rid)
    name = "issue-%d-run-%s.md" % (issue, rid)
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        handle.write(_record_text(issue, meta_run, **overrides))
    return name


def _entry(record_id, issue=9, run_id="r1", supersedes=()):
    return {
        "record_id": record_id,
        "issue": issue,
        "run_id": str(run_id),
        "base_commit": "a" * 40,
        "topic": "test-topic",
        "outcome": "succeeded",
        "supersedes": list(supersedes),
        "path": "automation/knowledge/experiments/%s.md" % record_id,
        "sha256": "b" * 64,
        "title": "Test",
    }


# Schema contracts -----------------------------------------------------------


def test_schema_files_are_valid_json_declaring_draft_2020_12():
    for name in ("experiment.json", "catalog.json"):
        path = os.path.join(SCHEMA_DIR, name)
        with open(path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        assert schema["$schema"] == DRAFT_2020_12
    with open(os.path.join(SCHEMA_DIR, "experiment.json"),
              encoding="utf-8") as handle:
        experiment = json.load(handle)
    assert set(("issue", "run_id", "record_id", "base_commit", "topic",
                "outcome", "supersedes", "schema", "$schema")) <= set(
        experiment["required"])
    with open(os.path.join(SCHEMA_DIR, "catalog.json"),
              encoding="utf-8") as handle:
        catalog = json.load(handle)
    assert catalog["required"] == ["$schema", "records", "schema"]


# Live repository records -----------------------------------------------------


def test_all_existing_experiment_records_parse_and_validate():
    entries = load_records(EXPERIMENTS_DIR)
    assert len(entries) >= 5
    for entry in entries:
        assert entry["record_id"] == derive_record_id(
            entry["issue"], entry["run_id"])
        assert entry["path"] == "automation/knowledge/experiments/%s.md" % (
            entry["record_id"])
        assert len(entry["sha256"]) == 64


def test_repeated_build_output_is_byte_identical():
    first = dump_catalog(build_catalog(EXPERIMENTS_DIR)).encode("utf-8")
    second = dump_catalog(build_catalog(EXPERIMENTS_DIR)).encode("utf-8")
    assert first == second
    assert first.endswith(b"\n")
    assert b"generated_at" not in first


def test_deterministic_sorting_independent_of_filesystem_order(monkeypatch):
    real_listdir = os.listdir
    first = dump_catalog(build_catalog(EXPERIMENTS_DIR))
    monkeypatch.setattr(
        os, "listdir",
        lambda path: list(reversed(real_listdir(path))))
    second = dump_catalog(build_catalog(EXPERIMENTS_DIR))
    assert first == second
    catalog = json.loads(first)
    ids = [item["record_id"] for item in catalog["records"]]
    assert ids == sorted(ids)
    assert catalog["$schema"] == CATALOG_SCHEMA_REF
    assert catalog["schema"] == CATALOG_SCHEMA_VERSION


# Query filters ----------------------------------------------------------------


def test_query_filters_and_composed_filters(tmp_path, capsys):
    directory = str(tmp_path)
    _write(directory, 9, "run-a", outcome="failed", topic="render-lifecycle")
    _write(directory, 9, "run-b", outcome="succeeded",
           topic="render-lifecycle")
    _write(directory, 27, "run-c", outcome="succeeded", topic="other-topic")

    def run_query(*cli_args):
        capsys.readouterr()
        assert main(["--experiments-dir", directory, "query",
                     *cli_args]) == 0
        return json.loads(capsys.readouterr().out)

    assert len(run_query()) == 3
    assert [item["run_id"] for item in run_query("--issue", "9")] == [
        "run-a", "run-b"]
    assert [item["issue"] for item in run_query(
        "--topic", "render-lifecycle")] == [9, 9]
    assert [item["run_id"] for item in run_query(
        "--outcome", "failed")] == ["run-a"]
    assert [item["run_id"] for item in run_query(
        "--record-id", "issue-27-run-run-c")] == ["run-c"]
    composed = run_query("--issue", "9", "--outcome", "failed",
                         "--topic", "render-lifecycle")
    assert [item["record_id"] for item in composed] == ["issue-9-run-run-a"]
    assert run_query("--issue", "999") == []
    # No network use: query works with loopback blocked is implied by
    # stdlib-only imports; every result keeps deterministic ordering.
    records = run_query("--topic", "render-lifecycle")
    assert [item["record_id"] for item in records] == sorted(
        item["record_id"] for item in records)


def test_filter_records_compose_with_and_semantics():
    records = [
        _entry("issue-9-run-a", issue=9, run_id="a"),
        _entry("issue-9-run-b", issue=9, run_id="b"),
        _entry("issue-27-run-c", issue=27, run_id="c"),
    ]
    records[1]["outcome"] = "failed"
    assert [item["run_id"] for item in filter_records(records, issue=9)] == [
        "a", "b"]
    assert [item["run_id"] for item in filter_records(
        records, issue=9, outcome="failed")] == ["b"]
    assert filter_records(records, issue=9, outcome="failed",
                          topic="nope") == []


# Integrity rejections ----------------------------------------------------------


def test_duplicate_record_id_rejected():
    entries = [_entry("issue-9-run-a", issue=9, run_id="a"),
               _entry("issue-9-run-a", issue=9, run_id="a")]
    with pytest.raises(CatalogError, match="duplicate record_id"):
        check_cross_record_invariants(entries)


def test_duplicate_issue_run_identity_rejected():
    first = _entry("issue-9-run-a", issue=9, run_id="a")
    second = _entry("issue-9-run-a", issue=9, run_id="a")
    second = dict(second, path="automation/knowledge/experiments/other.md")
    with pytest.raises(CatalogError, match="duplicate"):
        check_cross_record_invariants([first, second])


def test_duplicate_files_rejected_end_to_end(tmp_path, monkeypatch):
    directory = str(tmp_path)
    name = _write(directory, 9, "r1")
    real_listdir = os.listdir
    monkeypatch.setattr(
        os, "listdir", lambda path: real_listdir(path) + [name])
    with pytest.raises(CatalogError, match="duplicate"):
        load_records(directory)


def test_filename_identity_mismatch_rejected(tmp_path):
    directory = str(tmp_path)
    _write(directory, 9, "r1")
    # Metadata issue disagrees with the filename issue (filename kept).
    path = os.path.join(directory, "issue-9-run-r1.md")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    metadata, _ = parse_record_text(text)
    metadata["issue"] = 10
    metadata["record_id"] = derive_record_id(10, "r1")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n%s\n---\n# Mismatch\n%s\n" % (
            json.dumps(metadata), "\n".join(BODY_SECTIONS)))
    with pytest.raises(CatalogError, match="does not match"):
        load_records(directory)


def test_filename_run_id_mismatch_rejected(tmp_path):
    directory = str(tmp_path)
    name = _write(directory, 9, "r1")
    os.rename(os.path.join(directory, name),
              os.path.join(directory, "issue-9-run-r2.md"))
    with pytest.raises(CatalogError, match="does not match"):
        load_records(directory)


def test_unknown_supersedes_rejected(tmp_path):
    directory = str(tmp_path)
    _write(directory, 9, "r1", supersedes=["issue-9-run-does-not-exist"])
    with pytest.raises(CatalogError, match="unknown supersedes target"):
        load_records(directory)
    check_entries = [_entry("issue-9-run-r1", issue=9, run_id="r1",
                            supersedes=["issue-9-run-ghost"])]
    with pytest.raises(CatalogError, match="unknown supersedes target"):
        check_cross_record_invariants(check_entries)


def test_self_superseding_record_rejected(tmp_path):
    directory = str(tmp_path)
    _write(directory, 9, "r1", supersedes=["issue-9-run-r1"])
    with pytest.raises(CatalogError, match="must not supersede itself"):
        load_records(directory)


def test_supersedence_cycle_rejected():
    entries = [
        _entry("issue-9-run-a", issue=9, run_id="a",
               supersedes=["issue-9-run-b"]),
        _entry("issue-9-run-b", issue=9, run_id="b",
               supersedes=["issue-9-run-a"]),
    ]
    with pytest.raises(CatalogError, match="cycle"):
        check_cross_record_invariants(entries)


def test_supersedence_cycle_rejected_end_to_end(tmp_path):
    directory = str(tmp_path)
    _write(directory, 9, "r1", supersedes=["issue-9-run-r2"])
    _write(directory, 9, "r2", supersedes=["issue-9-run-r1"])
    with pytest.raises(CatalogError, match="cycle"):
        load_records(directory)


@pytest.mark.parametrize("bad_front_matter", [
    # Legacy YAML-style front matter is no longer accepted.
    "schema: runtime-lab-experiment/v1\nissue: 9\n",
    # JSON array instead of an object.
    "[]",
    # Truncated JSON.
    '{"issue": 9,',
    # Missing required keys.
    '{"issue": 9}',
])
def test_malformed_metadata_rejected(tmp_path, bad_front_matter):
    directory = str(tmp_path)
    path = os.path.join(directory, "issue-9-run-r1.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n%s---\n\n# Broken\n%s\n" % (
            bad_front_matter, "\n".join(BODY_SECTIONS)))
    with pytest.raises(CatalogError, match="malformed|must contain exactly"):
        load_records(directory)


def test_missing_closing_delimiter_rejected(tmp_path):
    directory = str(tmp_path)
    path = os.path.join(directory, "issue-9-run-r1.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('---\n{"issue": 9}\n\n# Broken\n')
    with pytest.raises(CatalogError, match="malformed"):
        load_records(directory)


def test_invalid_shapes_rejected(tmp_path):
    for field, value in (("outcome", "unknown"), ("topic", "UPPER"),
                         ("base_commit", "short"),
                         ("run_id", "has space")):
        directory = str(tmp_path)
        _write(directory, 9, "r1", **{field: value})
        with pytest.raises(CatalogError, match="invalid"):
            load_records(directory)


def test_records_outside_knowledge_directory_rejected(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(_record_text(9, "r1"), encoding="utf-8")
    with pytest.raises(CatalogError):
        load_records(str(outside))


# Provenance / freshness ---------------------------------------------------------


def test_source_record_sha256_changes_when_record_changes(tmp_path):
    directory = str(tmp_path)
    _write(directory, 9, "r1")
    before = {item["record_id"]: item["sha256"]
              for item in load_records(directory)}
    path = os.path.join(directory, "issue-9-run-r1.md")
    with open(path, "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()
    assert before["issue-9-run-r1"] == expected
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\nExtra observation.\n")
    after = {item["record_id"]: item["sha256"]
             for item in load_records(directory)}
    assert after["issue-9-run-r1"] != before["issue-9-run-r1"]


# No hot-file source of truth -----------------------------------------------------


def test_no_generated_catalog_file_is_required_to_be_committed():
    tracked = subprocess.run(
        ["git", "ls-files", "automation/knowledge"],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    names = tracked.stdout.split()
    assert not any(os.path.basename(name) == "index.json" for name in names)
    assert not os.path.exists(
        os.path.join(REPO_ROOT, "automation", "knowledge", "index.json"))
    # Validation and build succeed purely from per-run records.
    assert load_records(EXPERIMENTS_DIR)
    assert dump_catalog(build_catalog(EXPERIMENTS_DIR))


def test_parallel_unique_records_need_no_shared_catalog_artifact(tmp_path):
    directory = str(tmp_path)
    before = set(os.listdir(directory))
    _write(directory, 34, "agent-a")
    first = dump_catalog(build_catalog(directory))
    assert set(os.listdir(directory)) == before | {"issue-34-run-agent-a.md"}
    # A second parallel agent adds only its own unique file.
    _write(directory, 34, "agent-b")
    second = dump_catalog(build_catalog(directory))
    assert set(os.listdir(directory)) == before | {
        "issue-34-run-agent-a.md", "issue-34-run-agent-b.md"}
    assert "issue-34-run-agent-b" in second
    assert "issue-34-run-agent-a" in second
    assert not any(name == "index.json" for name in os.listdir(directory))


def test_build_output_option_writes_identical_bytes(tmp_path, capsys):
    out = str(tmp_path / "catalog.json")
    assert main(["build", "--output", out]) == 0
    capsys.readouterr()
    with open(out, "r", encoding="utf-8") as handle:
        from_file = handle.read()
    assert main(["build"]) == 0
    assert capsys.readouterr().out == from_file


def test_validate_cli_reports_record_count(capsys):
    assert main(["validate"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["record_count"] >= 5
