"""Schema-validated experiment catalog and query CLI (issue #34).

Derived, deterministic, machine-readable discovery index over the immutable
per-run experiment records in ``automation/knowledge/experiments/``.

Design notes (authoritative: GitHub issue #34):

- Each experiment record is one Markdown file whose front matter is strict
  JSON (a JSON object between the leading ``---`` delimiters). JSON is the
  canonical machine representation; the Markdown body is the human narrative.
  Per-run Git records remain canonical; this catalog is always derived.
- Standards basis: JSON Schema Draft 2020-12 for the metadata contract
  (``automation/knowledge/schema/``), W3C PROV only as the conceptual
  provenance model (run -> artifact -> decision), RO-Crate only as
  architectural precedent. No PROV serialization, JSON-LD, RDF, or RO-Crate
  is implemented.
- Stdlib only: ``json``, ``hashlib``, ``argparse``, ``pathlib``, ``re``.
  No PyYAML, jsonschema, database, or network access. The schema files are
  the documentation/interoperability contract; this module enforces the
  small set of domain invariants Runtime Lab actually needs instead of
  embedding a generic JSON-Schema engine.
- Deterministic output: stable record ordering, stable key ordering,
  UTF-8, deterministic whitespace/newline, no wall-clock fields. Repeated
  builds over identical record contents are byte-for-byte identical.
- Never a hot-file source of truth: ``build`` writes to stdout by default
  and optionally to an explicitly supplied output path (for example a
  temp/cache path). No generated shared ``index.json`` is committed;
  parallel agents only add unique per-run record files.

Usage::

    python automation/knowledge_catalog.py validate
    python automation/knowledge_catalog.py build [--output PATH]
    python automation/knowledge_catalog.py query --issue 9
    python automation/knowledge_catalog.py query --topic render-lifecycle
    python automation/knowledge_catalog.py query --outcome failed
    python automation/knowledge_catalog.py query --record-id <id>

Query filters compose (logical AND) and results keep deterministic
record_id ordering. No network, GitHub API, Render credentials, or
running service is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

EXPERIMENT_SCHEMA_VERSION = "runtime-lab-experiment/v1"
CATALOG_SCHEMA_VERSION = "runtime-lab-catalog/v1"

EXPERIMENT_SCHEMA_REF = "automation/knowledge/schema/experiment.json"
CATALOG_SCHEMA_REF = "automation/knowledge/schema/catalog.json"

# Fallback when the module is executed from a source checkout where
# __file__ already points inside <repo>/automation/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENTS_DIR = os.path.join(
    _REPO_ROOT, "automation", "knowledge", "experiments"
)
_DEFAULT_EXPERIMENTS_DIR = EXPERIMENTS_DIR

RECORD_FILENAME_RE = re.compile(
    r"^issue-(?P<issue>[1-9][0-9]*)-run-(?P<run_id>[A-Za-z0-9._-]+)\.md$"
)
RECORD_ID_RE = re.compile(r"^issue-[1-9][0-9]*-run-[A-Za-z0-9._-]+$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
BASE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

OUTCOMES = ("succeeded", "failed", "inconclusive")

REQUIRED_METADATA_KEYS = frozenset(
    (
        "$schema",
        "schema",
        "record_id",
        "issue",
        "run_id",
        "base_commit",
        "topic",
        "outcome",
        "supersedes",
    )
)

# Required Markdown body sections. Mirrors
# render_lifecycle.EXPERIMENT_RECORD_REQUIRED_SECTIONS; the catalog only
# checks presence so the human narrative contract stays in one place.
REQUIRED_BODY_SECTIONS = (
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


class CatalogError(ValueError):
    """Fail-closed validation error for experiment records or the catalog."""


def derive_record_id(issue: int, run_id: str) -> str:
    """Return the deterministic record id for an issue/run pair."""
    return "issue-%d-run-%s" % (issue, run_id)


def parse_record_text(text: str, source: str = "<record>") -> tuple[dict, str]:
    """Split a record into (metadata, body).

    The record must start with a ``---`` line, followed by a strict JSON
    object, followed by a closing ``---`` line and the Markdown body.
    Raises CatalogError on malformed or non-JSON front matter.
    """
    if not isinstance(text, str) or not text.strip():
        raise CatalogError("%s: experiment record is empty" % source)
    # Normalize newlines but require the delimiter structure.
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise CatalogError(
            "%s: malformed front matter: record must start with '---'" % source
        )
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise CatalogError(
            "%s: malformed front matter: missing closing '---'" % source
        )
    raw = "\n".join(lines[1:closing])
    if not raw.strip():
        raise CatalogError("%s: malformed front matter: empty metadata" % source)
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogError(
            "%s: malformed front matter: metadata is not strict JSON: %s"
            % (source, exc)
        ) from exc
    if not isinstance(metadata, dict):
        raise CatalogError(
            "%s: malformed front matter: metadata must be a JSON object" % source
        )
    body = "\n".join(lines[closing + 1 :])
    return metadata, body


def validate_metadata_shapes(metadata: dict, source: str = "<record>") -> dict:
    """Enforce per-record domain invariants (no cross-record checks)."""
    if set(metadata.keys()) != set(REQUIRED_METADATA_KEYS):
        missing = sorted(set(REQUIRED_METADATA_KEYS) - set(metadata.keys()))
        extra = sorted(set(metadata.keys()) - set(REQUIRED_METADATA_KEYS))
        raise CatalogError(
            "%s: metadata must contain exactly %s; missing=%s extra=%s"
            % (source, sorted(REQUIRED_METADATA_KEYS), missing, extra)
        )
    if metadata.get("$schema") != EXPERIMENT_SCHEMA_REF:
        raise CatalogError(
            "%s: invalid $schema: expected %r" % (source, EXPERIMENT_SCHEMA_REF)
        )
    if metadata.get("schema") != EXPERIMENT_SCHEMA_VERSION:
        raise CatalogError(
            "%s: invalid schema: expected %r" % (source, EXPERIMENT_SCHEMA_VERSION)
        )
    issue = metadata.get("issue")
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise CatalogError("%s: invalid issue: must be a positive integer" % source)
    run_id = metadata.get("run_id")
    if (
        not isinstance(run_id, str)
        or not (1 <= len(run_id) <= 96)
        or RUN_ID_RE.match(run_id) is None
    ):
        raise CatalogError(
            "%s: invalid run_id: must match [A-Za-z0-9._-]{1,96}" % source
        )
    expected_record_id = derive_record_id(issue, run_id)
    if metadata.get("record_id") != expected_record_id:
        raise CatalogError(
            "%s: record_id %r does not agree with issue/run_id (expected %r)"
            % (source, metadata.get("record_id"), expected_record_id)
        )
    base_commit = metadata.get("base_commit")
    if not isinstance(base_commit, str) or BASE_COMMIT_RE.match(base_commit) is None:
        raise CatalogError(
            "%s: invalid base_commit: must be a 40-character lowercase hex SHA"
            % source
        )
    topic = metadata.get("topic")
    if not isinstance(topic, str) or TOPIC_RE.match(topic) is None:
        raise CatalogError(
            "%s: invalid topic: must match [a-z0-9][a-z0-9-]{0,63}" % source
        )
    if metadata.get("outcome") not in OUTCOMES:
        raise CatalogError(
            "%s: invalid outcome: must be one of %s" % (source, list(OUTCOMES))
        )
    supersedes = metadata.get("supersedes")
    if not isinstance(supersedes, list) or any(
        not isinstance(item, str) for item in supersedes
    ):
        raise CatalogError(
            "%s: invalid supersedes: must be an array of record IDs" % source
        )
    if len(set(supersedes)) != len(supersedes):
        raise CatalogError(
            "%s: invalid supersedes: duplicate entries" % source
        )
    for item in supersedes:
        if RECORD_ID_RE.match(item) is None:
            raise CatalogError(
                "%s: invalid supersedes entry %r: must match %s"
                % (source, item, RECORD_ID_RE.pattern)
            )
    if metadata.get("record_id") in supersedes:
        raise CatalogError(
            "%s: record must not supersede itself" % source
        )
    return metadata


def extract_title(body: str) -> str:
    """Return the first Markdown H1 of the body, or an empty string."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def check_required_sections(body: str, source: str = "<record>") -> None:
    """Fail closed when the human narrative misses a required section."""
    for heading in REQUIRED_BODY_SECTIONS:
        if heading not in body:
            raise CatalogError(
                "%s: experiment record is missing required section: %s"
                % (source, heading)
            )


def check_cross_record_invariants(entries: list[dict]) -> None:
    """Fail closed on duplicate identity, dangling supersedes, and cycles."""
    seen_record_ids: dict[str, str] = {}
    seen_identity: dict[tuple[int, str], str] = {}
    for entry in entries:
        record_id = entry["record_id"]
        identity = (entry["issue"], entry["run_id"])
        if record_id in seen_record_ids:
            raise CatalogError(
                "%s: duplicate record_id %r (also in %s)"
                % (entry["path"], record_id, seen_record_ids[record_id])
            )
        if identity in seen_identity:
            raise CatalogError(
                "%s: duplicate (issue, run_id) %r (also in %s)"
                % (entry["path"], identity, seen_identity[identity])
            )
        seen_record_ids[record_id] = entry["path"]
        seen_identity[identity] = entry["path"]
    known = set(seen_record_ids)
    for entry in entries:
        for target in entry["supersedes"]:
            if target not in known:
                raise CatalogError(
                    "%s: unknown supersedes target %r"
                    % (entry["path"], target)
                )
    _check_supersedence_cycles(entries)


def _resolve_within(path: str, base: str, source: str) -> str:
    """Resolve path and fail closed on traversal outside base."""
    resolved = os.path.realpath(path)
    base_resolved = os.path.realpath(base)
    if resolved != base_resolved and not resolved.startswith(base_resolved + os.sep):
        raise CatalogError("%s: path escapes the knowledge directory" % source)
    return resolved


def load_records(experiments_dir: str | None = None) -> list[dict]:
    """Scan, parse, and integrity-check every experiment record.

    Returns normalized catalog entries sorted by record_id. Raises
    CatalogError (fail closed) on any integrity violation.
    """
    directory = os.path.abspath(experiments_dir or _DEFAULT_EXPERIMENTS_DIR)
    if not os.path.isdir(directory):
        raise CatalogError("experiments directory not found: %s" % directory)
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise CatalogError("cannot list experiments directory: %s" % exc) from exc

    entries: list[dict] = []
    for name in names:
        full = os.path.join(directory, name)
        _resolve_within(full, directory, name)
        if os.path.isdir(full):
            raise CatalogError(
                "%s: unexpected directory inside experiments directory" % name
            )
        if not name.endswith(".md"):
            continue
        match = RECORD_FILENAME_RE.match(name)
        if match is None:
            raise CatalogError(
                "%s: filename must match issue-<ISSUE>-run-<RUN_ID>.md" % name
            )
        with open(full, "rb") as handle:
            raw = handle.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CatalogError("%s: record is not valid UTF-8" % name) from exc
        source = "automation/knowledge/experiments/%s" % name
        metadata, body = parse_record_text(text, source)
        validate_metadata_shapes(metadata, source)
        check_required_sections(body, source)
        # Filename/metadata agreement: fail closed on mismatch.
        if int(match.group("issue")) != metadata["issue"]:
            raise CatalogError(
                "%s: filename issue does not match metadata issue" % source
            )
        if match.group("run_id") != metadata["run_id"]:
            raise CatalogError(
                "%s: filename run_id does not match metadata run_id" % source
            )
        if name != "%s.md" % metadata["record_id"]:
            raise CatalogError(
                "%s: filename does not match metadata record_id" % source
            )
        record_id = metadata["record_id"]
        entries.append(
            {
                "record_id": record_id,
                "issue": metadata["issue"],
                "run_id": metadata["run_id"],
                "base_commit": metadata["base_commit"],
                "topic": metadata["topic"],
                "outcome": metadata["outcome"],
                "supersedes": list(metadata["supersedes"]),
                "path": source,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "title": extract_title(body),
            }
        )

    # Cross-record provenance checks.
    check_cross_record_invariants(entries)
    entries.sort(key=lambda item: item["record_id"])
    return entries


def _check_supersedence_cycles(entries: list[dict]) -> None:
    """Fail closed when supersedes edges form a cycle."""
    edges = {entry["record_id"]: list(entry["supersedes"]) for entry in entries}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join(stack + [node])
            raise CatalogError("supersedence cycle detected: %s" % cycle)
        visiting.add(node)
        for target in edges.get(node, []):
            visit(target, stack + [node])
        visiting.discard(node)
        visited.add(node)

    for node in sorted(edges):
        visit(node, [])


def build_catalog(experiments_dir: str | None = None) -> dict:
    """Build the normalized, deterministic catalog object."""
    return {
        "$schema": CATALOG_SCHEMA_REF,
        "schema": CATALOG_SCHEMA_VERSION,
        "records": load_records(experiments_dir),
    }


def dump_catalog(catalog: dict) -> str:
    """Serialize a catalog deterministically (UTF-8, sorted keys)."""
    return (
        json.dumps(catalog, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


def filter_records(
    records: list[dict],
    issue: int | None = None,
    topic: str | None = None,
    outcome: str | None = None,
    record_id: str | None = None,
) -> list[dict]:
    """Apply composable AND filters, preserving deterministic ordering."""
    result = records
    if issue is not None:
        result = [item for item in result if item["issue"] == issue]
    if topic is not None:
        result = [item for item in result if item["topic"] == topic]
    if outcome is not None:
        result = [item for item in result if item["outcome"] == outcome]
    if record_id is not None:
        result = [item for item in result if item["record_id"] == record_id]
    return result


def _repo_relative_experiments(value: str | None) -> str | None:
    if value is None:
        return None
    if os.path.isabs(value):
        return value
    return os.path.abspath(os.path.join(_REPO_ROOT, value))


def build_parser() -> argparse.ArgumentParser:
    """Create the offline query CLI parser (no network use)."""
    parser = argparse.ArgumentParser(
        description=(
            "Derived experiment catalog and query CLI for "
            "automation/knowledge/. Offline; no network, GitHub API, "
            "Render credentials, or running service required."
        )
    )
    parser.add_argument(
        "--experiments-dir",
        default=None,
        help=(
            "Experiments directory (default: "
            "automation/knowledge/experiments relative to the repository root)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="Fail closed unless every record validates.")

    build_cmd = sub.add_parser(
        "build", help="Print the deterministic catalog JSON to stdout."
    )
    build_cmd.add_argument(
        "--output",
        default=None,
        help=(
            "Optional explicit output path (for example a temp/cache path). "
            "Nothing is written into the repository by default."
        ),
    )

    query_cmd = sub.add_parser(
        "query", help="Print matching catalog entries as JSON."
    )
    query_cmd.add_argument("--issue", type=int, default=None)
    query_cmd.add_argument("--topic", default=None)
    query_cmd.add_argument(
        "--outcome",
        default=None,
        choices=list(OUTCOMES),
    )
    query_cmd.add_argument("--record-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    experiments_dir = _repo_relative_experiments(args.experiments_dir)
    try:
        if args.command == "validate":
            records = load_records(experiments_dir)
            sys.stdout.write(
                json.dumps(
                    {"status": "ok", "record_count": len(records)},
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            return 0
        if args.command == "build":
            catalog = build_catalog(experiments_dir)
            text = dump_catalog(catalog)
            if args.output:
                parent = os.path.dirname(os.path.abspath(args.output))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as handle:
                    handle.write(text)
            else:
                sys.stdout.write(text)
            return 0
        if args.command == "query":
            catalog = build_catalog(experiments_dir)
            matches = filter_records(
                catalog["records"],
                issue=args.issue,
                topic=args.topic,
                outcome=args.outcome,
                record_id=args.record_id,
            )
            sys.stdout.write(
                json.dumps(matches, sort_keys=True, indent=2, ensure_ascii=False)
                + "\n"
            )
            return 0
    except CatalogError as exc:
        sys.stderr.write("knowledge_catalog: error: %s\n" % exc)
        return 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
