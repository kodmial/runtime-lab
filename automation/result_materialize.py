"""Materialize temporary runner results into a branch and PR (issue #5).

P0 temporary development-loop completion: consume the Render/OpenCode
result produced by issue #3 through the temporary harness from #4 (and
validated by #9), safely reproduce the changes, create an issue-specific
branch and open a PR.

Temporary phase notes:

- GitHub Actions may perform the GitHub write-back using the workflow
  ``GITHUB_TOKEN`` (see ``GhCliWritebackClient``). The final architecture
  moves GitHub write-back into the Render controller using GitHub App
  installation authentication (issue #11).
- Result materialization is independent of the Render worker after result
  collection/deletion: this module only consumes an already-collected
  result payload (dict or JSON file). It never contacts a Render worker.
- After creating/updating the PR, ``ci.yml`` is explicitly dispatched
  with its ``pr_number``. A ``pull_request`` event from a ``GITHUB_TOKEN``
  push must not be relied on.
- Nothing here makes Actions a permanent requirement: the pure helpers
  (branch naming, result validation, deterministic file application) plus
  the ``WritebackClient`` interface are reusable from the Render
  controller in #11 with a GitHub App backed implementation.

Stdlib only, like the rest of automation/.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import (
        PUBLIC_REPO_BRANCH,
        PUBLIC_REPO_URL,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import (  # type: ignore[no-redef]
        PUBLIC_REPO_BRANCH,
        PUBLIC_REPO_URL,
    )

try:  # pragma: no cover - import path depends on entrypoint
    from automation.opencode_runner import (
        MAX_FILE_BYTES,
        MAX_FILES,
        MAX_TOTAL_BYTES,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from opencode_runner import (  # type: ignore[no-redef]
        MAX_FILE_BYTES,
        MAX_FILES,
        MAX_TOTAL_BYTES,
    )


# ---------------------------------------------------------------------------
# Branch convention (shared with the scheduler and auto-merge controller).
# ---------------------------------------------------------------------------

def branch_prefix_for_issue(issue_number: int) -> str:
    """Return the required ``opencode/issue<N>-`` prefix for an issue."""
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise ValueError("issue_number must be a positive integer")
    if issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    return "opencode/issue%d-" % issue_number


_SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH_PATTERN = re.compile(r"^opencode/issue(\d+)-([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")


def sanitize_suffix(raw: str) -> str:
    """Validate a caller-provided unique suffix for branch naming."""
    if not isinstance(raw, str):
        raise ValueError("branch suffix must be a string")
    value = raw.strip()
    if not value:
        raise ValueError("branch suffix must not be empty")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError("branch suffix must not contain path separators: %r" % raw)
    if not _SUFFIX_PATTERN.match(value):
        raise ValueError(
            "branch suffix must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}, got %r" % raw
        )
    return value


def branch_name_for_issue(issue_number: int, unique_suffix: str) -> str:
    """Build the exact issue branch name ``opencode/issue<N>-<suffix>``."""
    prefix = branch_prefix_for_issue(issue_number)
    suffix = sanitize_suffix(unique_suffix)
    branch = prefix + suffix
    if not _BRANCH_PATTERN.match(branch):
        raise ValueError("constructed invalid branch name: %r" % branch)
    return branch


def parse_issue_number_from_branch(branch: str) -> int | None:
    """Return the issue number encoded in a branch, or None when foreign."""
    if not isinstance(branch, str):
        return None
    match = _BRANCH_PATTERN.match(branch.strip())
    if not match:
        return None
    try:
        number = int(match.group(1))
    except ValueError:
        return None
    return number if number > 0 else None


def branch_matches_issue(branch: str, issue_number: int) -> bool:
    """True when the branch carries the exact ``opencode/issue<N>-`` prefix."""
    if not isinstance(branch, str):
        return False
    return branch.strip().startswith(branch_prefix_for_issue(issue_number))


# ---------------------------------------------------------------------------
# Errors, constants and small builders.
# ---------------------------------------------------------------------------

class MaterializeError(ValueError):
    """Raised when a result is malformed, out of scope, or stale."""


CI_WORKFLOW_ID = "ci.yml"
CI_STATUS_CONTEXT = "runtime-lab/ci"

# Paths that can never be written through result materialization. The
# workflow-file exclusion mirrors .github/workflows/opencode.yml: the
# temporary GITHUB_TOKEN cannot safely push workflow files, and the final
# GitHub App path must also keep workflow writes out of task results.
WORKFLOW_DIR_PREFIX = ".github/workflows/"
GIT_DIR_PREFIX = ".git/"

_ALLOWED_CHANGE_TYPES = frozenset({"added", "modified", "deleted"})


def build_commit_message(issue_number: int) -> str:
    """Deterministic task commit message for an issue branch."""
    branch_prefix_for_issue(issue_number)  # validates eagerly
    return "fix: implement issue #%d" % issue_number


def build_pr_title(issue_title: str = "", issue_number: int = 0) -> str:
    """PR title: the source issue title, or a deterministic fallback."""
    title = (issue_title or "").strip()
    if title:
        return title
    if issue_number > 0:
        return "Automated implementation for #%d" % issue_number
    return "Automated implementation"


def build_pr_body(
    *,
    issue_number: int,
    job_id: str = "",
    base_sha: str = "",
    summary: str = "",
    executed_model: str = "",
) -> str:
    """Deterministic PR body linking the PR to its source issue."""
    branch_prefix_for_issue(issue_number)  # validates eagerly
    lines = [
        "Automated implementation for #%d." % issue_number,
        "",
        "Closes #%d" % issue_number,
    ]
    details: list[str] = []
    if job_id:
        details.append("job: %s" % job_id)
    if base_sha:
        details.append("base: %s" % base_sha)
    if executed_model:
        details.append("model: %s" % executed_model)
    if summary:
        details.append("result: %s" % summary[:500])
    if details:
        lines += ["", "Materialization details:"] + ["- " + item for item in details]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Result validation (repository / base SHA / job ID / change set).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatedChange:
    """One validated file operation from a worker result."""

    path: str
    change_type: str  # added | modified | deleted
    content: bytes | None = None  # None for deletions


@dataclass(frozen=True)
class ValidatedResult:
    """A structurally valid, in-scope, base-pinned worker result."""

    job_id: str
    issue_number: int
    repository_url: str
    base_ref: str
    base_sha: str
    status: str
    success: bool
    summary: str = ""
    error: str = ""
    executed_model: str = ""
    changes: tuple[ValidatedChange, ...] = ()


def _reject_path(path: str) -> str | None:
    """Return a rejection reason for an out-of-scope path, else None."""
    if not isinstance(path, str) or not path.strip():
        return "path must be a non-empty string"
    value = path.strip()
    if value.startswith("/") or value.startswith("\\"):
        return "absolute paths are out of scope: %r" % path
    if "\\" in value:
        return "backslashes are out of scope: %r" % path
    normalized = os.path.normpath(value)
    if normalized == "." or normalized.startswith(".."):
        return "path traversal is out of scope: %r" % path
    parts = normalized.split("/")
    if ".." in parts or "" in parts or "." in parts:
        return "unsafe path: %r" % path
    if normalized.startswith(GIT_DIR_PREFIX) or normalized == ".git":
        return "git internals are out of scope: %r" % path
    if normalized.startswith(WORKFLOW_DIR_PREFIX):
        return "workflow files are out of scope: %r" % path
    return None


def validate_change_entry(change: Mapping[str, Any]) -> ValidatedChange:
    """Validate one raw change entry from a worker result payload."""
    if not isinstance(change, Mapping):
        raise MaterializeError("change entry must be a JSON object")
    raw_path = change.get("path", "")
    if not isinstance(raw_path, str):
        raise MaterializeError("change path must be a string")
    reason = _reject_path(raw_path)
    if reason is not None:
        raise MaterializeError(reason)
    path = os.path.normpath(raw_path.strip())
    change_type = change.get("change_type", "")
    if change_type not in _ALLOWED_CHANGE_TYPES:
        raise MaterializeError(
            "change %r has unknown change_type %r" % (path, change_type)
        )
    if change_type == "deleted":
        return ValidatedChange(path=path, change_type="deleted", content=None)
    encoded = change.get("content_base64", "")
    if not isinstance(encoded, str) or not encoded:
        raise MaterializeError(
            "change %r of type %r requires content_base64" % (path, change_type)
        )
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise MaterializeError(
            "change %r has invalid base64 content: %s" % (path, exc)
        ) from exc
    if len(content) > MAX_FILE_BYTES:
        raise MaterializeError(
            "file %r too large (%d > %d bytes)" % (path, len(content), MAX_FILE_BYTES)
        )
    return ValidatedChange(path=path, change_type=change_type, content=content)


def normalize_changes(changes: Sequence[Any]) -> list[ValidatedChange]:
    """Validate and deterministically order a raw change list."""
    if changes is None:
        raise MaterializeError("result has no changes field")
    if not isinstance(changes, (list, tuple)):
        raise MaterializeError("result changes must be a list")
    if len(changes) > MAX_FILES:
        raise MaterializeError(
            "too many changed files (%d > %d)" % (len(changes), MAX_FILES)
        )
    validated = [validate_change_entry(item) for item in changes]
    seen: dict[str, str] = {}
    total_bytes = 0
    for item in validated:
        previous = seen.get(item.path)
        if previous is not None and previous != item.change_type:
            raise MaterializeError(
                "conflicting change types for %r: %r vs %r"
                % (item.path, previous, item.change_type)
            )
        if previous is not None:
            raise MaterializeError("duplicate change entry for %r" % item.path)
        seen[item.path] = item.change_type
        if item.content is not None:
            total_bytes += len(item.content)
    if total_bytes > MAX_TOTAL_BYTES:
        raise MaterializeError(
            "total changed bytes exceed %d bytes" % MAX_TOTAL_BYTES
        )
    validated.sort(key=lambda item: item.path)
    return validated


def validate_result_payload(
    result: Mapping[str, Any],
    *,
    expected_issue_number: int = 0,
    expected_repository_url: str = PUBLIC_REPO_URL,
    expected_base_sha: str = "",
    expected_job_id: str = "",
) -> ValidatedResult:
    """Validate a collected worker result payload (no worker contact).

    Checks repository URL, issue number, job ID and base SHA pinning plus
    the full change set (additions/modifications/deletions). Raises
    ``MaterializeError`` for malformed or out-of-scope results and for
    base-SHA mismatches (fail safely: the caller must not create any
    branch or PR afterwards).
    """
    if not isinstance(result, Mapping):
        raise MaterializeError("result payload must be a JSON object")
    job_id = result.get("job_id", "")
    if not isinstance(job_id, str) or not job_id.strip():
        raise MaterializeError("result has no job_id")
    job_id = job_id.strip()
    if expected_job_id and job_id != expected_job_id.strip():
        raise MaterializeError(
            "job id mismatch: expected %r, got %r" % (expected_job_id, job_id)
        )
    repository_url = result.get("repository_url", "")
    if expected_repository_url:
        if repository_url != expected_repository_url:
            raise MaterializeError(
                "repository mismatch: expected %r, got %r"
                % (expected_repository_url, repository_url)
            )
    elif not isinstance(repository_url, str) or not repository_url.strip():
        raise MaterializeError("result has no repository_url")
    metadata = result.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise MaterializeError("result metadata must be an object")
    raw_issue = result.get("issue_number", metadata.get("issue_number", 0))
    try:
        issue_number = int(raw_issue)
    except (TypeError, ValueError) as exc:
        raise MaterializeError("result has invalid issue_number") from exc
    if isinstance(raw_issue, bool) or issue_number <= 0:
        raise MaterializeError("result has invalid issue_number")
    if expected_issue_number and issue_number != expected_issue_number:
        raise MaterializeError(
            "issue mismatch: expected #%d, got #%d"
            % (expected_issue_number, issue_number)
        )
    base_ref = str(result.get("base_ref", "") or "").strip()
    base_sha = str(result.get("base_sha", "") or "").strip()
    if not base_ref:
        base_ref = PUBLIC_REPO_BRANCH
    if expected_base_sha:
        expected = expected_base_sha.strip()
        if not base_sha:
            raise MaterializeError(
                "result has no base_sha; expected %r" % expected
            )
        if base_sha != expected:
            raise MaterializeError(
                "base SHA mismatch: result %r != expected %r" % (base_sha, expected)
            )
    status = str(result.get("status", "") or "").strip()
    if not status:
        raise MaterializeError("result has no status")
    success = bool(result.get("success", False))
    summary = str(result.get("summary", "") or "")
    error = str(result.get("error", "") or "")
    executed_model = str(
        result.get("executed_model", "")
        or (metadata.get("executed_model", "") if isinstance(metadata, Mapping) else "")
        or (metadata.get("model", "") if isinstance(metadata, Mapping) else "")
        or ""
    ).strip()
    raw_changes = result.get("changes", None)
    if raw_changes is None:
        raise MaterializeError("result has no changes field")
    changes = tuple(normalize_changes(raw_changes))
    return ValidatedResult(
        job_id=job_id,
        issue_number=issue_number,
        repository_url=str(repository_url or expected_repository_url),
        base_ref=base_ref,
        base_sha=base_sha,
        status=status,
        success=success,
        summary=summary,
        error=error,
        executed_model=executed_model,
        changes=changes,
    )


def load_result_file(path: str) -> dict[str, Any]:
    """Load a collected result JSON file (post worker deletion)."""
    if not path or not str(path).strip():
        raise MaterializeError("result file path must not be empty")
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise MaterializeError("result file not found: %s" % path) from exc
    except ValueError as exc:
        raise MaterializeError("result file is not valid JSON: %s" % exc) from exc
    except OSError as exc:
        raise MaterializeError("cannot read result file: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise MaterializeError("result file must contain a JSON object")
    return payload


# ---------------------------------------------------------------------------
# Deterministic file application (local checkout, no worker contact).
# ---------------------------------------------------------------------------

def apply_changes_to_directory(
    repo_dir: str, changes: Sequence[ValidatedChange | Mapping[str, Any]]
) -> dict[str, int]:
    """Reproduce validated changes inside a local checkout directory.

    Writes additions/modifications (creating parent directories) and
    removes deletions in deterministic path order. Returns counts
    ``{"added":.., "modified":.., "deleted":..}``. Raises
    ``MaterializeError`` for out-of-scope paths; raises ``ValueError``
    when the directory does not exist.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        raise ValueError("repo_dir does not exist: %r" % repo_dir)
    normalized: list[ValidatedChange] = []
    for item in changes:
        if isinstance(item, ValidatedChange):
            reason = _reject_path(item.path)
            if reason is not None:
                raise MaterializeError(reason)
            normalized.append(item)
        elif isinstance(item, Mapping):
            normalized.append(validate_change_entry(item))
        else:
            raise MaterializeError("change entry must be a JSON object")
    normalized.sort(key=lambda item: item.path)
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for item in normalized:
        full = os.path.normpath(os.path.join(repo_dir, item.path))
        if not full.startswith(os.path.normpath(repo_dir) + os.sep) and full != os.path.normpath(repo_dir):
            raise MaterializeError("change escapes repo dir: %r" % item.path)
        if item.change_type == "deleted":
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    raise MaterializeError(
                        "refusing to delete directory %r" % item.path
                    )
                os.unlink(full)
            except FileNotFoundError:
                pass
            counts["deleted"] += 1
            continue
        assert item.content is not None
        existed = os.path.exists(full)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(item.content)
        counts["modified" if existed else "added"] += 1
    return counts


# ---------------------------------------------------------------------------
# Reusable GitHub write-back interface (Actions today, Render + App in #11).
# ---------------------------------------------------------------------------

class WritebackClient:
    """Interface for the GitHub write-back side of materialization.

    The temporary Actions phase implements this with ``GITHUB_TOKEN``
    via git/gh CLI (``GhCliWritebackClient``). Issue #11 implements the
    same interface from the Render controller with GitHub App
    installation authentication. The orchestrator
    (:func:`materialize_result`) only uses these five methods, so both
    callers share exactly the same branch/PR/CI behavior.
    """

    def get_base_sha(self, base_ref: str = PUBLIC_REPO_BRANCH) -> str:
        """Return the current base SHA for ``base_ref`` (e.g. main)."""
        raise NotImplementedError

    def find_open_pr_for_issue(self, issue_number: int) -> Mapping[str, Any] | None:
        """Return the open PR for an issue, if any.

        The mapping must include ``number`` (int) and ``head_ref`` (str).
        Association is by the ``opencode/issue<N>-`` branch prefix, which
        the scheduler and auto-merge controller also rely on.
        """
        raise NotImplementedError

    def publish_branch(
        self,
        *,
        branch: str,
        base_sha: str,
        changes: Sequence[ValidatedChange],
        commit_message: str,
    ) -> bool:
        """Apply changes, commit and push ``branch`` from ``base_sha``.

        Returns True when a new commit was pushed and False when the
        branch already contained exactly these changes (nothing to
        commit). Must not create more than one branch.
        """
        raise NotImplementedError

    def create_pull_request(
        self, *, title: str, body: str, head: str, base: str
    ) -> int:
        """Create a PR for an already-pushed head branch; return its number."""
        raise NotImplementedError

    def dispatch_ci(self, pr_number: int) -> None:
        """Explicitly dispatch ``ci.yml`` for a PR (no pull_request reliance)."""
        raise NotImplementedError


@dataclass
class MaterializeOutcome:
    """Outcome of one :func:`materialize_result` call."""

    action: str  # created | updated | no-changes | skipped
    issue_number: int
    job_id: str = ""
    base_sha: str = ""
    branch: str = ""
    pr_number: int | None = None
    dispatched_ci: bool = False
    reason: str = ""
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0


def _summarize_validated(changes: Sequence[ValidatedChange]) -> tuple[int, int, int]:
    added = sum(1 for item in changes if item.change_type == "added")
    modified = sum(1 for item in changes if item.change_type == "modified")
    deleted = sum(1 for item in changes if item.change_type == "deleted")
    return added, modified, deleted


def materialize_result(
    result: Mapping[str, Any],
    *,
    client: WritebackClient,
    issue_number: int = 0,
    unique_suffix: str = "",
    base_ref: str = PUBLIC_REPO_BRANCH,
    expected_base_sha: str = "",
    expected_job_id: str = "",
    issue_title: str = "",
    commit_message: str = "",
) -> MaterializeOutcome:
    """Materialize one collected worker result into a branch and PR.

    Exactly one branch and at most one PR are touched per call:

    - failed/non-success results return ``skipped`` (no branch/PR/CI);
    - successful results with no changes return ``no-changes`` (no
      branch/PR/CI, so no empty PR is ever opened);
    - a successful result with changes creates exactly one branch
      ``opencode/issue<N>-<suffix>`` (reusing the existing open PR
      branch for duplicate attempts) and exactly one PR, then
      explicitly dispatches ``ci.yml`` with its ``pr_number``.

    Base-SHA mismatches raise :class:`MaterializeError` before any
    branch, PR or CI dispatch happens (fail safely).
    """
    if client is None:
        raise ValueError("client (WritebackClient) must not be None")
    if not isinstance(result, Mapping):
        raise MaterializeError("result payload must be a JSON object")
    if issue_number:
        if not isinstance(issue_number, int) or isinstance(issue_number, bool):
            raise ValueError("issue_number must be a positive integer")
        if issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
    expected_issue = int(issue_number) if issue_number else 0
    validated = validate_result_payload(
        result,
        expected_issue_number=expected_issue,
        expected_repository_url=PUBLIC_REPO_URL,
        expected_base_sha=expected_base_sha,
        expected_job_id=expected_job_id,
    )
    if validated.status != "succeeded" or not validated.success:
        return MaterializeOutcome(
            action="skipped",
            issue_number=validated.issue_number,
            job_id=validated.job_id,
            base_sha=validated.base_sha,
            reason="result status %r is not successful; no branch or PR created"
            % validated.status,
        )
    if not validated.changes:
        return MaterializeOutcome(
            action="no-changes",
            issue_number=validated.issue_number,
            job_id=validated.job_id,
            base_sha=validated.base_sha,
            reason="worker reported no changes; no branch or PR created",
        )
    live_base = client.get_base_sha(base_ref or PUBLIC_REPO_BRANCH)
    live_base = (live_base or "").strip()
    if not live_base:
        raise MaterializeError("could not resolve current base SHA for %r" % base_ref)
    if validated.base_sha and validated.base_sha != live_base:
        raise MaterializeError(
            "base SHA mismatch: result %r != current %r; refusing to materialize"
            % (validated.base_sha, live_base)
        )
    effective_base = validated.base_sha or live_base
    existing = client.find_open_pr_for_issue(validated.issue_number)
    if existing is not None:
        try:
            existing_number = int(existing.get("number", 0))
        except (TypeError, ValueError) as exc:
            raise MaterializeError("existing PR has invalid number") from exc
        existing_head = str(existing.get("head_ref", "") or "").strip()
        if not existing_number or not existing_head:
            raise MaterializeError("existing PR record is incomplete")
        if not branch_matches_issue(existing_head, validated.issue_number):
            raise MaterializeError(
                "existing PR head %r does not match issue #%d"
                % (existing_head, validated.issue_number)
            )
        branch = existing_head
        message = commit_message.strip() if commit_message else build_commit_message(
            validated.issue_number
        )
        pushed = client.publish_branch(
            branch=branch,
            base_sha=effective_base,
            changes=list(validated.changes),
            commit_message=message,
        )
        added, modified, deleted = _summarize_validated(validated.changes)
        if not pushed:
            return MaterializeOutcome(
                action="no-changes",
                issue_number=validated.issue_number,
                job_id=validated.job_id,
                base_sha=effective_base,
                branch=branch,
                pr_number=existing_number,
                dispatched_ci=False,
                reason="branch already contains these changes; PR #%d reused"
                % existing_number,
                files_added=added,
                files_modified=modified,
                files_deleted=deleted,
            )
        client.dispatch_ci(existing_number)
        return MaterializeOutcome(
            action="updated",
            issue_number=validated.issue_number,
            job_id=validated.job_id,
            base_sha=effective_base,
            branch=branch,
            pr_number=existing_number,
            dispatched_ci=True,
            reason="existing PR #%d branch %r updated and ci.yml dispatched"
            % (existing_number, branch),
            files_added=added,
            files_modified=modified,
            files_deleted=deleted,
        )
    if not unique_suffix or not str(unique_suffix).strip():
        raise ValueError("unique_suffix is required to create a new issue branch")
    branch = branch_name_for_issue(validated.issue_number, unique_suffix)
    message = commit_message.strip() if commit_message else build_commit_message(
        validated.issue_number
    )
    pushed = client.publish_branch(
        branch=branch,
        base_sha=effective_base,
        changes=list(validated.changes),
        commit_message=message,
    )
    added, modified, deleted = _summarize_validated(validated.changes)
    if not pushed:
        return MaterializeOutcome(
            action="no-changes",
            issue_number=validated.issue_number,
            job_id=validated.job_id,
            base_sha=effective_base,
            branch=branch,
            pr_number=None,
            dispatched_ci=False,
            reason="changes already present on base; no branch or PR created",
            files_added=added,
            files_modified=modified,
            files_deleted=deleted,
        )
    title = build_pr_title(issue_title, validated.issue_number)
    body = build_pr_body(
        issue_number=validated.issue_number,
        job_id=validated.job_id,
        base_sha=effective_base,
        summary=validated.summary,
        executed_model=validated.executed_model,
    )
    pr_number = client.create_pull_request(
        title=title, body=body, head=branch, base=base_ref or PUBLIC_REPO_BRANCH
    )
    try:
        pr_number_int = int(pr_number)
    except (TypeError, ValueError) as exc:
        raise MaterializeError("create_pull_request returned invalid number") from exc
    if pr_number_int <= 0:
        raise MaterializeError("create_pull_request returned invalid number")
    client.dispatch_ci(pr_number_int)
    return MaterializeOutcome(
        action="created",
        issue_number=validated.issue_number,
        job_id=validated.job_id,
        base_sha=effective_base,
        branch=branch,
        pr_number=pr_number_int,
        dispatched_ci=True,
        reason="branch %r and PR #%d created and ci.yml dispatched"
        % (branch, pr_number_int),
        files_added=added,
        files_modified=modified,
        files_deleted=deleted,
    )


def materialize_from_file(
    result_file: str,
    *,
    client: WritebackClient,
    issue_number: int = 0,
    unique_suffix: str = "",
    base_ref: str = PUBLIC_REPO_BRANCH,
    expected_base_sha: str = "",
    expected_job_id: str = "",
    issue_title: str = "",
    commit_message: str = "",
) -> MaterializeOutcome:
    """Load a collected result JSON file and materialize it (no worker)."""
    payload = load_result_file(result_file)
    return materialize_result(
        payload,
        client=client,
        issue_number=issue_number,
        unique_suffix=unique_suffix,
        base_ref=base_ref,
        expected_base_sha=expected_base_sha,
        expected_job_id=expected_job_id,
        issue_title=issue_title,
        commit_message=commit_message,
    )


# ---------------------------------------------------------------------------
# Temporary Actions implementation (GITHUB_TOKEN via git/gh CLI).
# ---------------------------------------------------------------------------

def _run_cli(
    cmd: Sequence[str], *, cwd: str = "", timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command without ever logging secret values."""
    try:
        return subprocess.run(
            list(cmd),
            cwd=cwd or None,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("command timed out: %s" % list(cmd)[0]) from exc
    except FileNotFoundError as exc:
        raise RuntimeError("command not found: %s" % list(cmd)[0]) from exc
    except OSError as exc:
        raise RuntimeError("command failed: %s" % exc) from exc


class GhCliWritebackClient(WritebackClient):
    """Temporary Actions write-back using git/gh CLI and GITHUB_TOKEN.

    Used only by the temporary harness: the stable workflow envelope
    checks out the repository, then instantiates this client with the
    checkout directory. All GitHub writes use the workflow-provided
    ``GITHUB_TOKEN`` through ``gh`` (never printed). The final #11
    controller reuses :func:`materialize_result` with a GitHub App
    implementation of :class:`WritebackClient` instead.
    """

    def __init__(
        self,
        repo_dir: str = ".",
        *,
        base_ref: str = PUBLIC_REPO_BRANCH,
        repository: str = "",
    ) -> None:
        if not repo_dir or not os.path.isdir(str(repo_dir)):
            raise ValueError("repo_dir does not exist: %r" % repo_dir)
        self.repo_dir = os.path.abspath(str(repo_dir))
        self.base_ref = (base_ref or PUBLIC_REPO_BRANCH).strip() or PUBLIC_REPO_BRANCH
        self.repository = (repository or os.environ.get("GITHUB_REPOSITORY", "")).strip()

    def _gh(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        cmd = ["gh"] + list(args)
        if self.repository and "api" not in args:
            pass  # gh infers the repo from the checkout remote
        return _run_cli(cmd, cwd=self.repo_dir, timeout=timeout)

    def get_base_sha(self, base_ref: str = PUBLIC_REPO_BRANCH) -> str:
        ref = (base_ref or self.base_ref).strip() or PUBLIC_REPO_BRANCH
        # Prefer the remote ref so a stale local checkout cannot pin a
        # wrong base; fall back to the local ref for offline/test setups.
        for args in (
            ["api", "repos/%s/git/ref/heads/%s" % (self.repository, ref),
             "--jq", ".object.sha"],
            ["rev-parse", "origin/%s" % ref],
            ["rev-parse", ref],
            ["rev-parse", "HEAD"],
        ):
            if args[0] == "api" and not self.repository:
                continue
            try:
                if args[0] == "api":
                    proc = self._gh(*args)
                else:
                    proc = _run_cli(["git"] + args, cwd=self.repo_dir)
            except RuntimeError:
                continue
            if proc.returncode != 0:
                continue
            sha = (proc.stdout or "").strip()
            if sha and re.match(r"^[0-9a-fA-F]{4,64}$", sha):
                return sha
        raise MaterializeError("could not resolve current base SHA for %r" % ref)

    def find_open_pr_for_issue(self, issue_number: int) -> Mapping[str, Any] | None:
        branch_prefix_for_issue(issue_number)  # validates eagerly
        proc = self._gh(
            "pr", "list", "--state", "open", "--limit", "100",
            "--json", "number,headRefName,state",
        )
        if proc.returncode != 0:
            raise MaterializeError(
                "gh pr list failed: %s" % (proc.stderr or proc.stdout or "unknown")[:500]
            )
        try:
            items = json.loads(proc.stdout or "[]")
        except ValueError as exc:
            raise MaterializeError("gh pr list returned invalid JSON") from exc
        if not isinstance(items, list):
            raise MaterializeError("gh pr list returned invalid JSON")
        prefix = branch_prefix_for_issue(issue_number)
        for item in items:
            if not isinstance(item, dict):
                continue
            head = str(item.get("headRefName", "") or "")
            if not head.startswith(prefix):
                continue
            try:
                number = int(item.get("number", 0))
            except (TypeError, ValueError):
                continue
            if number > 0:
                return {"number": number, "head_ref": head, "state": "open"}
        return None

    def publish_branch(
        self,
        *,
        branch: str,
        base_sha: str,
        changes: Sequence[ValidatedChange],
        commit_message: str,
    ) -> bool:
        parsed_issue = parse_issue_number_from_branch(branch)
        if not parsed_issue or not branch_matches_issue(branch, parsed_issue):
            raise MaterializeError("refusing to publish foreign branch %r" % branch)
        if not base_sha or not base_sha.strip():
            raise MaterializeError("base_sha must not be empty")
        if not commit_message or not commit_message.strip():
            raise MaterializeError("commit message must not be empty")
        validated = list(changes)
        if not validated:
            return False
        repo = self.repo_dir

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            proc = _run_cli(["git"] + list(args), cwd=repo)
            if proc.returncode != 0:
                raise MaterializeError(
                    "git %s failed: %s" % (args[0], (proc.stderr or proc.stdout or "")[:500])
                )
            return proc

        git("fetch", "origin", self.base_ref, "--quiet")
        # Start from the exact base SHA when it is available locally;
        # otherwise start from the fetched base ref (same commit in a
        # fresh checkout). Never rebase or touch other branches.
        started = False
        for ref in (base_sha.strip(), "origin/%s" % self.base_ref):
            try:
                git("checkout", "--detach", ref, "--quiet")
                started = True
                break
            except MaterializeError:
                continue
        if not started:
            raise MaterializeError("could not check out base %r" % base_sha)
        # Reuse the existing remote branch when it exists; otherwise
        # create the issue branch from this exact base.
        existing = _run_cli(
            ["git", "ls-remote", "--heads", "origin", branch], cwd=repo
        )
        remote_exists = (
            existing.returncode == 0 and branch in (existing.stdout or "")
        )
        if remote_exists:
            git("fetch", "origin", branch, "--quiet")
            git("checkout", branch, "--quiet")
        else:
            # Recreate deterministically: delete a stale local branch of
            # the same name left by a previous attempt, then branch off.
            _run_cli(["git", "branch", "-D", branch], cwd=repo)
            git("checkout", "-b", branch, "--quiet")
        apply_changes_to_directory(repo, validated)
        git("add", "-A")
        status = _run_cli(["git", "status", "--porcelain"], cwd=repo)
        if status.returncode != 0:
            raise MaterializeError("git status failed")
        if not (status.stdout or "").strip():
            return False
        # Guard: task commits must never touch workflow files (the
        # GITHUB_TOKEN cannot push them and Actions must stay stable).
        staged = _run_cli(["git", "status", "--porcelain", "--", WORKFLOW_DIR_PREFIX],
                          cwd=repo)
        if staged.returncode == 0 and (staged.stdout or "").strip():
            raise MaterializeError(
                "refusing to commit .github/workflows/** changes"
            )
        git("config", "user.name", "github-actions[bot]")
        git(
            "config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        )
        git("commit", "-m", commit_message.strip(), "--quiet")
        pushed = _run_cli(
            ["git", "push", "-u", "origin", "HEAD:%s" % branch], cwd=repo
        )
        if pushed.returncode != 0:
            raise MaterializeError(
                "git push failed: %s" % (pushed.stderr or pushed.stdout or "")[:500]
            )
        return True

    def create_pull_request(
        self, *, title: str, body: str, head: str, base: str
    ) -> int:
        if not title or not title.strip():
            raise MaterializeError("PR title must not be empty")
        proc = self._gh(
            "pr", "create",
            "--base", (base or self.base_ref).strip(),
            "--head", head.strip(),
            "--title", title.strip(),
            "--body", body or "",
            "--json", "number",
        )
        if proc.returncode != 0:
            # A concurrent attempt may have created the PR first: reuse
            # the now-existing open PR instead of failing duplicates.
            number = self._reuse_existing_after_create_failure(head)
            if number:
                return number
            raise MaterializeError(
                "gh pr create failed: %s" % (proc.stderr or proc.stdout or "")[:500]
            )
        try:
            payload = json.loads(proc.stdout or "{}")
        except ValueError as exc:
            raise MaterializeError("gh pr create returned invalid JSON") from exc
        number = payload.get("number", 0) if isinstance(payload, dict) else 0
        try:
            number_int = int(number)
        except (TypeError, ValueError) as exc:
            raise MaterializeError("gh pr create returned invalid number") from exc
        if number_int <= 0:
            raise MaterializeError("gh pr create returned invalid number")
        return number_int

    def _reuse_existing_after_create_failure(self, head: str) -> int:
        match = _BRANCH_PATTERN.match((head or "").strip())
        if not match:
            return 0
        try:
            issue_number = int(match.group(1))
        except ValueError:
            return 0
        proc = self._gh(
            "pr", "list", "--head", head.strip(), "--state", "all",
            "--limit", "5", "--json", "number",
        )
        if proc.returncode != 0:
            return 0
        try:
            items = json.loads(proc.stdout or "[]")
        except ValueError:
            return 0
        if isinstance(items, list) and items and isinstance(items[0], dict):
            try:
                return int(items[0].get("number", 0) or 0)
            except (TypeError, ValueError):
                return 0
        _ = issue_number
        return 0

    def dispatch_ci(self, pr_number: int) -> None:
        try:
            number = int(pr_number)
        except (TypeError, ValueError) as exc:
            raise MaterializeError("invalid pr_number") from exc
        if number <= 0:
            raise MaterializeError("invalid pr_number")
        proc = self._gh(
            "workflow", "run", CI_WORKFLOW_ID, "--ref", "main",
            "-f", "pr_number=%d" % number,
        )
        if proc.returncode != 0:
            raise MaterializeError(
                "gh workflow run ci.yml failed: %s"
                % (proc.stderr or proc.stdout or "")[:500]
            )


# ---------------------------------------------------------------------------
# CLI for the stable workflow envelope (temporary harness calls this).
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize a collected Render worker result into a branch and PR."
    )
    parser.add_argument("--result-file", required=True,
                        help="Collected worker result JSON file (post worker deletion).")
    parser.add_argument("--issue-number", type=int, default=0,
                        help="Expected issue number (validated against the result).")
    parser.add_argument("--unique-suffix", default="",
                        help="Unique branch suffix (e.g. GITHUB_RUN_ID) for new branches.")
    parser.add_argument("--base-ref", default=PUBLIC_REPO_BRANCH,
                        help="Base branch (default: main).")
    parser.add_argument("--expected-base-sha", default="",
                        help="Expected base SHA; mismatches fail safely.")
    parser.add_argument("--expected-job-id", default="",
                        help="Expected job ID (validated when provided).")
    parser.add_argument("--issue-title", default="",
                        help="Source issue title for the PR title.")
    parser.add_argument("--repo-dir", default=".",
                        help="Local repository checkout to publish from.")
    parser.add_argument("--json-output", default="",
                        help="Optional path to write the materialize outcome as JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    client = GhCliWritebackClient(repo_dir=args.repo_dir, base_ref=args.base_ref)
    try:
        outcome = materialize_from_file(
            args.result_file,
            client=client,
            issue_number=args.issue_number,
            unique_suffix=args.unique_suffix,
            base_ref=args.base_ref,
            expected_base_sha=args.expected_base_sha,
            expected_job_id=args.expected_job_id,
            issue_title=args.issue_title,
        )
    except (MaterializeError, ValueError, RuntimeError) as exc:
        print("::error::result materialization failed: %s" % exc)
        return 1
    print(
        "materialize action=%s issue=#%d branch=%s pr=%s ci_dispatched=%s reason=%s"
        % (
            outcome.action,
            outcome.issue_number,
            outcome.branch or "-",
            outcome.pr_number if outcome.pr_number is not None else "-",
            outcome.dispatched_ci,
            outcome.reason,
        )
    )
    if args.json_output:
        try:
            with open(args.json_output, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "action": outcome.action,
                        "issue_number": outcome.issue_number,
                        "job_id": outcome.job_id,
                        "base_sha": outcome.base_sha,
                        "branch": outcome.branch,
                        "pr_number": outcome.pr_number,
                        "dispatched_ci": outcome.dispatched_ci,
                        "reason": outcome.reason,
                        "files_added": outcome.files_added,
                        "files_modified": outcome.files_modified,
                        "files_deleted": outcome.files_deleted,
                    },
                    handle,
                    sort_keys=True,
                    indent=2,
                )
        except OSError as exc:
            print("::error::could not write outcome JSON: %s" % exc)
            return 1
    if outcome.action in ("created", "updated", "no-changes", "skipped"):
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
