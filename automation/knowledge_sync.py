#!/usr/bin/env python3
"""Sync Runtime Lab knowledge into the central private agent-knowledge ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping

from knowledge_store import (
    GitHubApiKnowledgeBackend,
    GitHubBackendConflict,
    ImmutableOverwriteError,
    KnowledgeConflictError,
    KnowledgeStoreError,
    validate_project_slug,
)

SYNC_SCHEMA = "agent-knowledge-sync/v1"
MAX_ATTEMPTS = 5


class GhApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GhApi:
    """Adapter from gh api to GitHubApiKnowledgeBackend."""

    def __call__(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        endpoint = path if path.startswith("/") else "/" + path
        cmd = ["gh", "api", "--method", method.upper(), endpoint]
        input_text = None
        if body is not None:
            cmd += ["--input", "-"]
            input_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode:
            error = (proc.stderr or "gh api failed").strip()[:1200]
            match = re.search(r"\bHTTP\s+(\d{3})\b", error, re.IGNORECASE)
            raise GhApiError(error, int(match.group(1)) if match else None)
        output = (proc.stdout or "").strip()
        return json.loads(output) if output else {}


def load_state(text: str | None, project: str, source_repo: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KnowledgeStoreError(f"sync state is invalid JSON: {exc}") from None
    if not isinstance(state, dict) or state.get("schema") != SYNC_SCHEMA:
        raise KnowledgeStoreError("unsupported central sync-state schema")
    if state.get("project") != project or state.get("source_repo") != source_repo:
        raise KnowledgeStoreError("central sync-state identity mismatch")
    if not isinstance(state.get("topics", {}), dict):
        raise KnowledgeStoreError("central sync-state topics must be an object")
    return state


def read_source(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    experiments_dir = root / "experiments"
    topics_dir = root / "topics"
    if not experiments_dir.is_dir() or not topics_dir.is_dir():
        raise KnowledgeStoreError("source knowledge directories are missing")

    import knowledge_catalog
    knowledge_catalog.load_records(str(experiments_dir))

    experiments = {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(experiments_dir.glob("issue-*-run-*.md"))
    }
    topics = {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(topics_dir.glob("*.md"))
        if p.read_text(encoding="utf-8").strip()
    }
    return experiments, topics


def build_changes(
    backend: GitHubApiKnowledgeBackend,
    project: str,
    source_repo: str,
    source_commit: str,
    root: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    experiments, topics = read_source(root)
    head = backend.get_ref("main")
    if not head:
        raise KnowledgeStoreError("central knowledge main branch is missing")

    state_path = f"projects/{project}/.sync-state.json"
    old_state = load_state(backend.get_content(state_path, head), project, source_repo)
    previous_topics = old_state.get("topics", {})
    changes: dict[str, str] = {}
    added = verified = changed_topics = 0

    for filename, content in experiments.items():
        target = f"projects/{project}/experiments/{filename}"
        current = backend.get_content(target, head)
        if current is None:
            changes[target] = content
            added += 1
        elif current == content:
            verified += 1
        else:
            raise ImmutableOverwriteError(
                f"immutable central experiment differs from source: {target}"
            )

    topic_hashes: dict[str, str] = {}
    for filename, content in topics.items():
        target = f"projects/{project}/topics/{filename}"
        current = backend.get_content(target, head)
        wanted_hash = sha256(content)
        topic_hashes[filename] = wanted_hash

        if current == content:
            continue
        if current is None:
            changes[target] = content
            changed_topics += 1
            continue

        previous_hash = previous_topics.get(filename)
        if not isinstance(previous_hash, str) or sha256(current) != previous_hash:
            raise KnowledgeConflictError(
                f"central topic changed independently since last sync: {target}"
            )
        changes[target] = content
        changed_topics += 1

    # Deletions are intentionally not propagated until tombstone semantics exist.
    new_state = {
        "schema": SYNC_SCHEMA,
        "project": project,
        "source_repo": source_repo,
        "source_ref": "main",
        "source_commit": source_commit,
        "experiments_count": len(experiments),
        "topics": topic_hashes,
    }
    state_text = json.dumps(new_state, sort_keys=True, indent=2) + "\n"
    if backend.get_content(state_path, head) != state_text:
        changes[state_path] = state_text

    return changes, {
        "project": project,
        "source_commit": source_commit,
        "source_experiments": len(experiments),
        "added_experiments": added,
        "verified_experiments": verified,
        "source_topics": len(topics),
        "changed_topics": changed_topics,
        "changed_files": len(changes),
    }


def sync(
    backend: GitHubApiKnowledgeBackend,
    project: str,
    source_repo: str,
    source_commit: str,
    root: Path,
    attempts: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        head = backend.get_ref("main")
        if not head:
            raise KnowledgeStoreError("central knowledge main branch is missing")

        changes, summary = build_changes(
            backend, project, source_repo, source_commit, root
        )
        summary["attempt"] = attempt
        if not changes:
            summary.update(result="already-synchronized", target_commit=head)
            return summary

        commit = backend.commit_files(
            head,
            changes,
            f"knowledge: sync {project} from {source_repo}@{source_commit[:12]}",
        )
        try:
            backend.cas_update_ref("main", commit, head)
        except GitHubBackendConflict as exc:
            last_error = exc
            continue

        summary.update(result="synchronized", target_commit=commit)
        return summary

    raise KnowledgeConflictError(
        f"sync CAS retry budget exhausted after {attempts} attempts: {last_error}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--source-root",
        default=str(Path(__file__).resolve().parent / "knowledge"),
    )
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = parser.parse_args(argv)

    try:
        project = validate_project_slug(args.project)
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit or ""):
            raise KnowledgeStoreError("source commit must be a full lowercase git SHA")
        if not 1 <= args.max_attempts <= 20:
            raise KnowledgeStoreError("max attempts must be between 1 and 20")
        if not os.environ.get("GH_TOKEN"):
            raise KnowledgeStoreError("GH_TOKEN is required for central sync")

        result = sync(
            GitHubApiKnowledgeBackend(GhApi()),
            project,
            args.source_repo,
            args.source_commit,
            Path(args.source_root).resolve(),
            args.max_attempts,
        )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (KnowledgeStoreError, ValueError) as exc:
        print(f"knowledge_sync: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
