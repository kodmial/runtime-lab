"""Result materialization: Render/OpenCode result -> branch + PR (issue #5).

P0 temporary development-loop completion: consumes the self-contained job
result produced by the Render worker (issue #3) through the temporary
harness (issue #4), validates it, reproduces the file changes
deterministically, creates an issue-specific branch and opens (or reuses)
a PR, then explicitly dispatches ``ci.yml`` for that PR.

This module is the factored, reusable core (pure helpers plus a small
``GitOps`` interface). The thin temporary Actions wrapper lives in
``automation/render-materialize.sh``; the future Render controller
(issue #11, GitHub App installation authentication) reuses this module
directly with its own ``GitOps`` implementation, so nothing here makes
GitHub Actions a permanent requirement.

Materialization reads only the collected result file and never contacts
the ephemeral Render worker, so it stays valid after the worker is
deleted. Only successful results are materialized; failures, timeouts,
base-SHA mismatches, malformed payloads and out-of-scope paths all fail
closed without creating branches or PRs.

Stdlib only, mirroring automation/render_lifecycle.py and
automation/opencode_runner.py.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import PUBLIC_REPO_URL
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import PUBLIC_REPO_URL  # type: ignore[no-redef]

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

# Exact convention: opencode/issue<ISSUE_NUMBER>-<unique-suffix>.
BRANCH_PREFIX_TEMPLATE = "opencode/issue{issue_number}-"
_BRANCH_RE = re.compile(r"^opencode/issue(\d+)-(.+)$")
_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Paths the Actions token must never publish (mirrors the guards in
# .github/workflows/opencode.yml): workflow files require a separate
# Workflows permission, so worker results touching them are rejected.
FORBIDDEN_PATH_PREFIXES = (".github/workflows/", ".git/")

ALLOWED_CHANGE_TYPES = frozenset({"added", "modified", "deleted"})

CI_WORKFLOW_ID = "ci.yml"
CI_PR_NUMBER_INPUT = "pr_number"
CI_STATUS_CONTEXT = "runtime-lab/ci"

GIT_USER_NAME = "github-actions[bot]"
GIT_USER_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class MaterializeError(ValueError):
    """Raised when a result must not be materialized (fail closed)."""


def branch_name_for_issue(issue_number: int, suffix: str) -> str:
    """Build the issue-specific branch name for a unique suffix."""
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise MaterializeError("issue_number must be a positive integer")
    suffix = (suffix or "").strip()
    if not suffix:
        raise MaterializeError("branch suffix must not be empty")
    if not _SAFE_SUFFIX_RE.match(suffix):
        raise MaterializeError(
            "branch suffix must match [A-Za-z0-9._-], got %r" % suffix
        )
    return "opencode/issue%d-%s" % (issue_number, suffix)


def parse_branch_issue_number(branch: str) -> int:
    """Return the source issue number encoded in an opencode/* branch."""
    match = _BRANCH_RE.match((branch or "").strip())
    if not match:
        raise MaterializeError(
            "branch %r does not match opencode/issue<NUMBER>-<suffix>" % branch
        )
    return int(match.group(1))


def is_issue_branch(branch: str, issue_number: int) -> bool:
    """True when the branch follows the convention for this issue."""
    try:
        return parse_branch_issue_number(branch) == issue_number
    except MaterializeError:
        return False


def commit_message_for_issue(issue_number: int) -> str:
    """Deterministic commit message for a materialized result."""
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise MaterializeError("issue_number must be a positive integer")
    return "fix: implement issue #%d" % issue_number


def default_pr_body(issue_number: int) -> str:
    """Default PR body mirroring the issue-mode publisher."""
    return "Automated implementation for #%d.\n\nCloses #%d" % (
        issue_number,
        issue_number,
    )


def shas_match(first: str, second: str) -> bool:
    """Compare SHAs tolerating short-SHA prefixes (exact otherwise)."""
    first = (first or "").strip()
    second = (second or "").strip()
    if not first or not second:
        return False
    if first == second:
        return True
    return first.startswith(second) or second.startswith(first)


# ---------------------------------------------------------------------------
# Result validation (issues #3/#9 payload shape, fail closed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedResult:
    """Validated, normalized view of a worker result payload."""

    job_id: str
    issue_number: int
    repository_url: str
    base_ref: str
    base_sha: str
    changes: list[dict[str, Any]] = field(default_factory=list)


def _issue_from_payload(payload: Mapping[str, Any]) -> int:
    candidate = payload.get("issue_number")
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            candidate = metadata.get("issue_number")
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise MaterializeError("result carries no integer issue_number")
    return candidate


def _check_safe_path(path: str) -> str:
    """Validate a repo-relative path; raise MaterializeError when unsafe."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise MaterializeError("result contains an empty or invalid path")
    if "\\" in path:
        raise MaterializeError("result path must use forward slashes: %r" % path)
    normalized = path.strip()
    if not normalized or normalized.startswith("/") or normalized in (".", "./"):
        raise MaterializeError("result path is out of scope: %r" % path)
    segments = normalized.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise MaterializeError("result path escapes the repository: %r" % path)
    lowered = normalized.lower()
    for prefix in FORBIDDEN_PATH_PREFIXES:
        if lowered == prefix.rstrip("/") or lowered.startswith(prefix):
            raise MaterializeError(
                "result path is not publishable with this token: %r" % path
            )
    return normalized


def _decode_change_content(change: Mapping[str, Any], path: str) -> bytes:
    encoded = change.get("content_base64", "")
    if encoded is None:
        encoded = ""
    if not isinstance(encoded, str):
        raise MaterializeError(
            "change %r has a non-string content_base64" % path
        )
    if not encoded:
        return b""
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise MaterializeError(
            "change %r has malformed base64 content" % path
        ) from exc


def validate_result(
    payload: Mapping[str, Any],
    *,
    expected_issue_number: int,
    expected_repository_url: str = PUBLIC_REPO_URL,
    expected_base_sha: str = "",
    expected_job_id: str = "",
) -> NormalizedResult:
    """Validate a collected worker result; fail closed on any mismatch.

    Only successful (``status == "succeeded"`` with truthy ``success``)
    results validate. Repository URL, issue number, base SHA (when an
    expectation is given) and job ID (when given) must all agree, every
    change entry must be well-formed and in scope, and producer size
    limits are re-enforced so a malformed result cannot overflow the
    publisher.
    """
    if not isinstance(payload, Mapping):
        raise MaterializeError("result payload must be a JSON object")
    if not isinstance(expected_issue_number, int) or expected_issue_number <= 0:
        raise MaterializeError("expected_issue_number must be a positive integer")

    job_id = payload.get("job_id", "")
    if not isinstance(job_id, str) or not job_id.strip():
        raise MaterializeError("result carries no job_id")
    job_id = job_id.strip()
    if expected_job_id and job_id != expected_job_id.strip():
        raise MaterializeError(
            "result job_id %r does not match expected %r"
            % (job_id, expected_job_id.strip())
        )

    status = payload.get("status", "")
    if status != "succeeded" or not payload.get("success"):
        raise MaterializeError(
            "only successful results are materialized (status=%r success=%r)"
            % (status, payload.get("success"))
        )

    issue_number = _issue_from_payload(payload)
    if issue_number != expected_issue_number:
        raise MaterializeError(
            "result issue_number %d does not match expected %d"
            % (issue_number, expected_issue_number)
        )

    repository_url = payload.get("repository_url", "")
    if not isinstance(repository_url, str) or not repository_url.strip():
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            repository_url = metadata.get("repository_url", "")
    if not isinstance(repository_url, str):
        repository_url = ""
    repository_url = repository_url.strip()
    if repository_url != (expected_repository_url or "").strip():
        raise MaterializeError(
            "result repository_url %r does not match expected %r"
            % (repository_url, expected_repository_url)
        )

    base_sha = payload.get("base_sha", "")
    if not isinstance(base_sha, str) or not base_sha.strip():
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            base_sha = metadata.get("base_sha", "")
    if not isinstance(base_sha, str):
        base_sha = ""
    base_sha = base_sha.strip()
    if not base_sha:
        raise MaterializeError("result carries no base_sha")
    if expected_base_sha and not shas_match(base_sha, expected_base_sha):
        raise MaterializeError(
            "result base_sha %r does not match expected %r"
            % (base_sha, expected_base_sha.strip())
        )

    base_ref = payload.get("base_ref", "")
    if not isinstance(base_ref, str):
        base_ref = ""
    base_ref = base_ref.strip()

    raw_changes = payload.get("changes", [])
    if raw_changes is None:
        raw_changes = []
    if not isinstance(raw_changes, list):
        raise MaterializeError("result changes must be a list")
    if len(raw_changes) > MAX_FILES:
        raise MaterializeError(
            "too many changed files (%d > %d)" % (len(raw_changes), MAX_FILES)
        )

    changes: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    total_bytes = 0
    for entry in raw_changes:
        if not isinstance(entry, Mapping):
            raise MaterializeError("result change entries must be objects")
        change_type = entry.get("change_type", "")
        if change_type not in ALLOWED_CHANGE_TYPES:
            raise MaterializeError(
                "unknown change_type %r (path=%r)"
                % (change_type, entry.get("path", ""))
            )
        path = _check_safe_path(str(entry.get("path", "")))
        previous = seen.get(path)
        if previous is not None:
            if previous != change_type:
                raise MaterializeError(
                    "conflicting change entries for path %r" % path
                )
            continue  # exact duplicate: keep the first deterministically
        seen[path] = change_type
        normalized: dict[str, Any] = {"path": path, "change_type": change_type}
        if change_type == "deleted":
            changes.append(normalized)
            continue
        content = _decode_change_content(entry, path)
        if len(content) > MAX_FILE_BYTES:
            raise MaterializeError(
                "file %r too large (%d > %d bytes)"
                % (path, len(content), MAX_FILE_BYTES)
            )
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_BYTES:
            raise MaterializeError(
                "total changed bytes exceed %d bytes" % MAX_TOTAL_BYTES
            )
        normalized["content_base64"] = base64.b64encode(content).decode("ascii")
        changes.append(normalized)

    changes.sort(key=lambda item: item["path"])
    return NormalizedResult(
        job_id=job_id,
        issue_number=issue_number,
        repository_url=repository_url,
        base_ref=base_ref,
        base_sha=base_sha,
        changes=changes,
    )


def has_changes(result: NormalizedResult) -> bool:
    """True when the validated result carries file changes."""
    return bool(result.changes)


# ---------------------------------------------------------------------------
# Deterministic change application (additions, modifications, deletions).
# ---------------------------------------------------------------------------


def _confined_abs_path(repo_dir: str, path: str) -> str:
    """Resolve a validated path inside repo_dir (blocks symlink escapes)."""
    _check_safe_path(path)
    repo_real = os.path.realpath(repo_dir)
    full = os.path.realpath(os.path.join(repo_real, path))
    if full != repo_real and not full.startswith(repo_real + os.sep):
        raise MaterializeError("result path escapes the repository: %r" % path)
    git_dir = os.path.join(repo_real, ".git")
    if full == git_dir or full.startswith(git_dir + os.sep):
        raise MaterializeError("result path targets git metadata: %r" % path)
    return full


def apply_changes(
    changes: Sequence[Mapping[str, Any]], repo_dir: str
) -> list[dict[str, str]]:
    """Reproduce validated changes inside repo_dir, deterministically.

    Entries are applied in sorted path order: added/modified files are
    written with their exact bytes (parents created), deleted paths are
    removed when present (already-absent counts as applied). Returns the
    applied operations in the same deterministic order.
    """
    if not os.path.isdir(repo_dir):
        raise MaterializeError("repo_dir does not exist: %r" % repo_dir)
    applied: list[dict[str, str]] = []
    for entry in sorted(list(changes), key=lambda item: str(item.get("path", ""))):
        if not isinstance(entry, Mapping):
            raise MaterializeError("change entries must be objects")
        change_type = entry.get("change_type", "")
        if change_type not in ALLOWED_CHANGE_TYPES:
            raise MaterializeError("unknown change_type %r" % (change_type,))
        path = _check_safe_path(str(entry.get("path", "")))
        full = _confined_abs_path(repo_dir, path)
        if change_type == "deleted":
            if os.path.isdir(full) and not os.path.islink(full):
                raise MaterializeError(
                    "refusing to delete directory %r" % path
                )
            try:
                os.remove(full)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise MaterializeError(
                    "cannot delete %r: %s" % (path, exc)
                ) from exc
            applied.append({"path": path, "change_type": "deleted"})
            continue
        content = _decode_change_content(entry, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(full, "wb") as handle:
                handle.write(content)
        except OSError as exc:
            raise MaterializeError(
                "cannot write %r: %s" % (path, exc)
            ) from exc
        applied.append({"path": path, "change_type": change_type})
    return applied


# ---------------------------------------------------------------------------
# PR reuse helpers (duplicate attempts must not create duplicate PRs).
# ---------------------------------------------------------------------------


def _pr_head_ref(entry: Mapping[str, Any]) -> str:
    for key in ("headRefName", "head_ref", "headRef", "head"):
        value = entry.get(key)
        if isinstance(value, Mapping):
            ref = value.get("ref", "")
            if isinstance(ref, str) and ref:
                return ref
        elif isinstance(value, str) and value and key != "head":
            return value
    return ""


def select_open_pr_for_branch(
    prs: Sequence[Mapping[str, Any]], branch: str
) -> str:
    """Return the open PR number for an exact head-branch match, else "".

    Accepts ``gh pr list --json`` shapes (``headRefName``) as well as
    REST shapes (``head.ref``). Entries without an explicit state are
    treated as open only when they come from a state-filtered listing is
    not assumed: a non-open state never matches.
    """
    branch = (branch or "").strip()
    if not branch:
        return ""
    for entry in prs:
        if not isinstance(entry, Mapping):
            continue
        if _pr_head_ref(entry) != branch:
            continue
        state = entry.get("state", "")
        if isinstance(state, str) and state and state.lower() not in ("open", ""):
            if state.lower() != "open":
                continue
        number = entry.get("number", "")
        if isinstance(number, bool):
            continue
        if isinstance(number, int) and number > 0:
            return str(number)
        if isinstance(number, str) and number.strip().isdigit():
            return number.strip()
    return ""


# ---------------------------------------------------------------------------
# GitHub write-back interface (reusable from Actions or the Render
# controller in #11 with GitHub App installation authentication).
# ---------------------------------------------------------------------------


class GitOps(ABC):
    """Abstract GitHub write-back operations for one materialization.

    The Actions harness uses :class:`SubprocessGitOps` (``git`` + ``gh``
    with the workflow-provided ``GITHUB_TOKEN``). The future Render
    controller implements the same interface with GitHub App
    installation authentication while reusing every pure helper above.
    """

    @abstractmethod
    def current_main_sha(self) -> str:
        """Return the current main-branch SHA ("" when unresolvable)."""

    @abstractmethod
    def ensure_branch_at(self, branch: str, sha: str) -> str:
        """Create or reset ``branch`` to ``sha``; return created/reset."""

    @abstractmethod
    def checkout_branch(self, branch: str) -> None:
        """Check out ``branch`` in the working repository."""

    @abstractmethod
    def commit_all(self, message: str) -> str:
        """Stage all changes and commit; "" when there is nothing to commit."""

    @abstractmethod
    def push_branch(self, branch: str) -> None:
        """Publish ``branch`` to the origin remote."""

    @abstractmethod
    def branch_diff_empty(self, branch: str, base_sha: str) -> bool:
        """True when ``branch`` carries no diff versus ``base_sha``."""

    @abstractmethod
    def find_open_pr(self, branch: str) -> str:
        """Return the open PR number for ``branch``, or "" when none."""

    @abstractmethod
    def create_pr(self, branch: str, title: str, body: str) -> str:
        """Create a main-based PR for ``branch``; return the PR number."""

    @abstractmethod
    def dispatch_ci(self, pr_number: str) -> None:
        """Explicitly dispatch ci.yml for ``pr_number``."""


def _run(
    cmd: Sequence[str], cwd: str, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class SubprocessGitOps(GitOps):
    """``GitOps`` backed by ``git`` and ``gh`` subprocesses (stdlib only).

    Authentication is environment-driven: ``gh`` picks up ``GH_TOKEN``
    / ``GITHUB_TOKEN`` automatically (workflow token in Actions,
    installation token in the future Render controller). No secret value
    is ever logged.
    """

    def __init__(
        self,
        repo_dir: str,
        repository: str = "",
        *,
        git_bin: str = "git",
        gh_bin: str = "gh",
        base_ref: str = "main",
        remote: str = "origin",
    ) -> None:
        if not os.path.isdir(repo_dir):
            raise MaterializeError("repo_dir does not exist: %r" % repo_dir)
        self.repo_dir = os.path.abspath(repo_dir)
        self.repository = (repository or os.environ.get("GITHUB_REPOSITORY", "")).strip()
        self.git_bin = git_bin or "git"
        self.gh_bin = gh_bin or "gh"
        self.base_ref = (base_ref or "main").strip() or "main"
        self.remote = (remote or "origin").strip() or "origin"
        self._remote_shas: dict[str, str] = {}

    def _git(self, *args: str, timeout: float = 120.0) -> str:
        proc = _run([self.git_bin, *args], self.repo_dir, timeout=timeout)
        if proc.returncode != 0:
            raise MaterializeError(
                "git %s failed (code %d): %s"
                % (" ".join(args[:3]), proc.returncode, proc.stderr.strip()[:500])
            )
        return proc.stdout

    def _git_code(self, *args: str, timeout: float = 60.0) -> tuple[int, str, str]:
        proc = _run([self.git_bin, *args], self.repo_dir, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr

    def _gh(self, *args: str, timeout: float = 120.0) -> str:
        if not self.repository:
            raise MaterializeError(
                "repository (owner/repo) is required for gh operations"
            )
        proc = _run(
            [self.gh_bin, *args, "--repo", self.repository],
            self.repo_dir,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise MaterializeError(
                "gh %s failed (code %d): %s"
                % (args[0] if args else "", proc.returncode, proc.stderr.strip()[:500])
            )
        return proc.stdout

    def current_main_sha(self) -> str:
        for ref in ("%s/%s" % (self.remote, self.base_ref), self.base_ref):
            code, stdout, _ = self._git_code("rev-parse", "--verify", ref)
            if code == 0 and stdout.strip():
                return stdout.strip()
        return ""

    def _remote_branch_sha(self, branch: str) -> str:
        if branch in self._remote_shas:
            return self._remote_shas[branch]
        code, stdout, _ = self._git_code(
            "ls-remote", "--heads", self.remote, branch
        )
        sha = ""
        if code == 0:
            for line in stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "refs/heads/" + branch:
                    sha = parts[0].strip()
                    break
        self._remote_shas[branch] = sha
        return sha

    def ensure_branch_at(self, branch: str, sha: str) -> str:
        if not sha.strip():
            raise MaterializeError("base SHA must not be empty")
        parse_branch_issue_number(branch)  # fail closed on bad convention
        remote_sha = self._remote_branch_sha(branch)
        code, _, _ = self._git_code("rev-parse", "--verify", "refs/heads/" + branch)
        created = code != 0 and not remote_sha
        self._git("checkout", "-B", branch, sha.strip())
        return "created" if created else "reset"

    def checkout_branch(self, branch: str) -> None:
        self._git("checkout", branch)

    def commit_all(self, message: str) -> str:
        if not message.strip():
            raise MaterializeError("commit message must not be empty")
        self._git("add", "-A")
        code, stdout, _ = self._git_code("status", "--porcelain")
        if code != 0:
            raise MaterializeError("git status failed after staging")
        if not stdout.strip():
            return ""
        self._git(
            "-c", "user.name=%s" % GIT_USER_NAME,
            "-c", "user.email=%s" % GIT_USER_EMAIL,
            "commit", "-m", message.strip(),
        )
        return self._git("rev-parse", "HEAD").strip()

    def push_branch(self, branch: str) -> None:
        remote_sha = self._remote_shas.get(branch, "")
        if remote_sha:
            # The branch existed remotely and was just reset to the base:
            # force-push guarded by the observed remote SHA so concurrent
            # updates are never silently clobbered.
            lease = "refs/heads/%s:%s" % (branch, remote_sha)
            self._git(
                "push",
                "--force-with-lease=%s" % lease,
                "-u",
                self.remote,
                "HEAD:%s" % branch,
            )
        else:
            self._git("push", "-u", self.remote, "HEAD:%s" % branch)
        # Refresh the cached remote SHA after a successful push.
        self._remote_shas.pop(branch, None)

    def branch_diff_empty(self, branch: str, base_sha: str) -> bool:
        code, _, _ = self._git_code("diff", "--quiet", base_sha, branch, "--")
        return code == 0

    def find_open_pr(self, branch: str) -> str:
        stdout = self._gh(
            "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json", "number,headRefName,state",
        )
        try:
            prs = json.loads(stdout or "[]")
        except ValueError as exc:
            raise MaterializeError("could not parse gh pr list output") from exc
        if not isinstance(prs, list):
            raise MaterializeError("unexpected gh pr list output")
        return select_open_pr_for_branch(prs, branch)

    def create_pr(self, branch: str, title: str, body: str) -> str:
        if not title.strip():
            raise MaterializeError("PR title must not be empty")
        stdout = self._gh(
            "pr", "create",
            "--base", self.base_ref,
            "--head", branch,
            "--title", title.strip(),
            "--body", body or "",
            "--json", "number",
        )
        try:
            payload = json.loads(stdout or "{}")
        except ValueError as exc:
            raise MaterializeError("could not parse gh pr create output") from exc
        number = payload.get("number", "") if isinstance(payload, dict) else ""
        if isinstance(number, int) and number > 0:
            return str(number)
        raise MaterializeError("gh pr create returned no PR number")

    def dispatch_ci(self, pr_number: str) -> None:
        pr_number = (pr_number or "").strip()
        if not pr_number.isdigit():
            raise MaterializeError("pr_number must be numeric, got %r" % pr_number)
        self._gh(
            "workflow", "run", CI_WORKFLOW_ID,
            "--ref", self.base_ref,
            "-f", "%s=%s" % (CI_PR_NUMBER_INPUT, pr_number),
        )


# ---------------------------------------------------------------------------
# Orchestration: exactly one branch + PR per successful result.
# ---------------------------------------------------------------------------


@dataclass
class MaterializeOutcome:
    """Result of one materialization attempt."""

    branch: str = ""
    pr_number: str = ""
    created_pr: bool = False
    dispatched_ci: bool = False
    no_change: bool = False
    applied: list[dict[str, str]] = field(default_factory=list)
    commit_sha: str = ""
    job_id: str = ""


def materialize(
    result: Mapping[str, Any],
    *,
    issue_number: int,
    branch: str,
    repo_dir: str,
    ops: GitOps,
    repository_url: str = PUBLIC_REPO_URL,
    expected_base_sha: str = "",
    expected_job_id: str = "",
    pr_title: str = "",
    pr_body: str = "",
) -> MaterializeOutcome:
    """Materialize one collected worker result into a branch and PR.

    Fails closed (``MaterializeError``, no branch/PR/CI side effects)
    for unsuccessful, mismatched, malformed or out-of-scope results. A
    validated result with no changes returns a ``no_change`` outcome
    without creating an empty PR. Otherwise exactly one branch is
    published, the pre-existing open PR for that branch is reused when
    present (no duplicate PRs), and ``ci.yml`` is always explicitly
    dispatched for the PR number.
    """
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise MaterializeError("issue_number must be a positive integer")
    if not is_issue_branch(branch, issue_number):
        raise MaterializeError(
            "branch %r does not match opencode/issue%d-..." % (branch, issue_number)
        )

    normalized = validate_result(
        result,
        expected_issue_number=issue_number,
        expected_repository_url=repository_url,
        expected_base_sha=expected_base_sha,
        expected_job_id=expected_job_id,
    )

    if not has_changes(normalized):
        return MaterializeOutcome(branch=branch, no_change=True, job_id=normalized.job_id)

    main_sha = ops.current_main_sha()
    if not main_sha:
        raise MaterializeError("could not resolve the current main SHA")
    if not shas_match(normalized.base_sha, main_sha):
        raise MaterializeError(
            "result base_sha %r does not match current main %r; "
            "refusing to publish a stale result" % (normalized.base_sha, main_sha)
        )

    ops.ensure_branch_at(branch, main_sha)
    ops.checkout_branch(branch)
    applied = apply_changes(normalized.changes, repo_dir)
    commit_sha = ops.commit_all(commit_message_for_issue(issue_number))
    if not commit_sha and ops.branch_diff_empty(branch, main_sha):
        # Changes reproduce content already on main: no empty PR.
        return MaterializeOutcome(
            branch=branch, no_change=True, applied=applied, job_id=normalized.job_id
        )

    title = pr_title.strip() or ("Automated implementation for #%d" % issue_number)
    body = pr_body if pr_body else default_pr_body(issue_number)
    pr_number = ops.find_open_pr(branch)
    created = False
    if not pr_number:
        pr_number = ops.create_pr(branch, title, body)
        created = True
    # A GITHUB_TOKEN push may not emit pull_request events, so CI is
    # always dispatched explicitly for the created/updated PR.
    ops.dispatch_ci(pr_number)
    return MaterializeOutcome(
        branch=branch,
        pr_number=pr_number,
        created_pr=created,
        dispatched_ci=True,
        no_change=False,
        applied=applied,
        commit_sha=commit_sha,
        job_id=normalized.job_id,
    )


# ---------------------------------------------------------------------------
# File helpers + CLI (used by the temporary shell harness).
# ---------------------------------------------------------------------------


def load_result_file(path: str) -> dict[str, Any]:
    """Load a collected result JSON file (no worker contact)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise MaterializeError("result file not found: %r" % path) from exc
    except ValueError as exc:
        raise MaterializeError("result file is not valid JSON: %r" % path) from exc
    if not isinstance(payload, dict):
        raise MaterializeError("result file must contain a JSON object")
    return payload


def outcome_to_dict(outcome: MaterializeOutcome) -> dict[str, Any]:
    """Serialize an outcome for the shell harness / logs."""
    return {
        "branch": outcome.branch,
        "pr_number": outcome.pr_number,
        "created_pr": outcome.created_pr,
        "dispatched_ci": outcome.dispatched_ci,
        "no_change": outcome.no_change,
        "applied": [dict(item) for item in outcome.applied],
        "commit_sha": outcome.commit_sha,
        "job_id": outcome.job_id,
    }


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize a collected Render/OpenCode result into a branch + PR."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--result", required=True, help="collected result JSON file")
    common.add_argument("--issue", required=True, type=int, help="issue number")
    common.add_argument("--repo-url", default=PUBLIC_REPO_URL)
    common.add_argument("--base-sha", default="")
    common.add_argument("--job-id", default="")

    validate = sub.add_parser("validate", parents=[common])
    validate.add_argument("--json", action="store_true", help="print normalized result")

    branch = sub.add_parser("branch-name")
    branch.add_argument("--issue", required=True, type=int)
    branch.add_argument("--suffix", required=True)

    apply = sub.add_parser("apply", parents=[common])
    apply.add_argument("--repo-dir", required=True)

    mat = sub.add_parser("materialize", parents=[common])
    mat.add_argument("--repo-dir", required=True)
    mat.add_argument("--branch", required=True)
    mat.add_argument("--repository", default="")
    mat.add_argument("--pr-title", default="")
    mat.add_argument("--pr-body", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "branch-name":
            print(branch_name_for_issue(args.issue, args.suffix))
            return 0
        payload = load_result_file(args.result)
        if args.command == "validate":
            normalized = validate_result(
                payload,
                expected_issue_number=args.issue,
                expected_repository_url=args.repo_url,
                expected_base_sha=args.base_sha,
                expected_job_id=args.job_id,
            )
            if args.json:
                print(json.dumps({
                    "job_id": normalized.job_id,
                    "issue_number": normalized.issue_number,
                    "repository_url": normalized.repository_url,
                    "base_ref": normalized.base_ref,
                    "base_sha": normalized.base_sha,
                    "changes": normalized.changes,
                }, indent=2, sort_keys=True))
            else:
                print(
                    "ok: job %s issue #%d base %s changes=%d"
                    % (
                        normalized.job_id,
                        normalized.issue_number,
                        normalized.base_sha,
                        len(normalized.changes),
                    )
                )
            return 0
        if args.command == "apply":
            normalized = validate_result(
                payload,
                expected_issue_number=args.issue,
                expected_repository_url=args.repo_url,
                expected_base_sha=args.base_sha,
                expected_job_id=args.job_id,
            )
            applied = apply_changes(normalized.changes, args.repo_dir)
            print(json.dumps({"applied": applied}, indent=2, sort_keys=True))
            return 0
        if args.command == "materialize":
            ops = SubprocessGitOps(args.repo_dir, repository=args.repository)
            outcome = materialize(
                payload,
                issue_number=args.issue,
                branch=args.branch,
                repo_dir=args.repo_dir,
                ops=ops,
                repository_url=args.repo_url,
                expected_base_sha=args.base_sha,
                expected_job_id=args.job_id,
                pr_title=args.pr_title,
                pr_body=args.pr_body,
            )
            print(json.dumps(outcome_to_dict(outcome), indent=2, sort_keys=True))
            return 0
    except MaterializeError as exc:
        print("::error::%s" % exc, file=sys.stderr)
        return 1
    raise AssertionError("unreachable command: %r" % (args.command,))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
