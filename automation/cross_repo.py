"""Cross-repository task execution/write-back contract (issue #85).

P0: allow a Runtime Lab issue (tracking stays in ``kodmial/runtime-lab``)
to explicitly target the allow-listed source repository
``kodmial/opencode``, run the coding task against that repository in the
ephemeral worker, and materialize the resulting branch/PR there while the
issue, priority, dependency state, experiment record and automation
ownership remain in ``kodmial/runtime-lab``.

Contract (machine-readable, explicit, fail closed):

- The target repository is declared in the Runtime Lab issue body with
  ``runtime-lab-target: <owner/repo>`` (an HTML comment of the form
  ``<!-- runtime-lab-target: <owner/repo> -->`` is preferred; a bare
  ``runtime-lab-target: <owner/repo>`` line is also accepted). When no
  marker is present the target defaults to ``kodmial/runtime-lab``
  (existing self-target behavior, unchanged).
- Only exact allow-listed repositories are accepted; initially exactly
  ``kodmial/runtime-lab`` (self) and ``kodmial/opencode``. Any other
  caller-supplied repository or URL is rejected with
  :class:`TargetRepoError` before any worker is created.
- The exact target base SHA is resolved and pinned before execution
  (controller side via the GitHub API when available) and re-checked at
  materialization time; repo or base mismatches fail closed with no
  branch/PR/CI side effects.
- The worker clones/checks out the *target* repository; issue/task text
  and lifecycle stay sourced from the Runtime Lab issue.
- Write-back lands in the target repository on an issue-correlated
  branch (``opencode/issue<N>-<suffix>`` where ``N`` is the Runtime Lab
  source issue number) with at most one PR, linking back to the source
  issue. The cross-repo PR body never uses a bare ``Closes #N`` (which
  would close the wrong issue in the target repo); it links the full
  ``kodmial/runtime-lab#N`` source instead.
- Authentication reuses short-lived GitHub App installation credentials
  where available; a PAT is only a temporary/bootstrap path when already
  configured. Target-repository write credentials are never present in
  the ephemeral OpenCode child environment.

Stdlib only, like the rest of automation/.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Allow-list and canonical URLs.
# ---------------------------------------------------------------------------

SOURCE_REPO_FULL = "kodmial/runtime-lab"
SOURCE_REPO_URL = "https://github.com/kodmial/runtime-lab"

TARGET_OPENCODE_FULL = "kodmial/opencode"
TARGET_OPENCODE_URL = "https://github.com/kodmial/opencode"

ALLOWED_TARGET_REPOS = (SOURCE_REPO_FULL, TARGET_OPENCODE_FULL)

TARGET_REPO_URLS = {
    SOURCE_REPO_FULL: SOURCE_REPO_URL,
    TARGET_OPENCODE_FULL: TARGET_OPENCODE_URL,
}

TARGET_BASE_REF_DEFAULT = "main"


class TargetRepoError(ValueError):
    """Raised when a target repository declaration is missing/invalid."""


def normalize_target_repo(raw: object) -> str:
    """Validate an ``owner/repo`` declaration against the allow-list.

    Returns the canonical allow-listed spelling. Matching is exact
    after stripping whitespace (case-insensitive); anything else raises
    :class:`TargetRepoError` (fail closed: arbitrary caller-supplied
    repositories or URLs are never accepted).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TargetRepoError("target repository must be a non-empty 'owner/repo'")
    candidate = raw.strip()
    if "://" in candidate or candidate.startswith("git@"):
        raise TargetRepoError(
            "target repository must be 'owner/repo', not a URL: %r" % raw
        )
    lowered = candidate.lower()
    for allowed in ALLOWED_TARGET_REPOS:
        if lowered == allowed.lower():
            return allowed
    raise TargetRepoError(
        "target repository %r is not allow-listed (allowed: %s)"
        % (candidate, list(ALLOWED_TARGET_REPOS))
    )


def target_repo_url(full: object) -> str:
    """Return the canonical clone URL for an allow-listed repository."""
    normalized = normalize_target_repo(full)
    return TARGET_REPO_URLS[normalized]


def is_cross_repo_target(full: object) -> bool:
    """True when the target is a different repo than the tracking repo."""
    return normalize_target_repo(full) != SOURCE_REPO_FULL


def normalize_clone_url(url: object) -> str:
    """Map a clone URL back to its allow-listed ``owner/repo`` (fail closed)."""
    if not isinstance(url, str) or not url.strip():
        raise TargetRepoError("repository URL must be a non-empty string")
    candidate = url.strip().rstrip("/")
    lowered = candidate.lower()
    if lowered.endswith(".git"):
        lowered = lowered[:-4]
    for full, repo_url in TARGET_REPO_URLS.items():
        base = repo_url.lower().rstrip("/")
        if lowered == base or lowered == base + ".git":
            return full
    raise TargetRepoError(
        "repository URL %r is not allow-listed (allowed: %s)"
        % (url, sorted(TARGET_REPO_URLS.values()))
    )


# ---------------------------------------------------------------------------
# Machine-readable target declaration in the Runtime Lab issue body.
# ---------------------------------------------------------------------------

# Preferred: <!-- runtime-lab-target: kodmial/opencode -->
# Accepted:  runtime-lab-target: kodmial/opencode (bare line)
_TARGET_MARKER_RE = re.compile(
    r"runtime-lab-target\s*:\s*(?P<repo>[A-Za-z0-9_.\-]+\s*/\s*[A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)


def parse_target_repo(issue_body: object, default: str = SOURCE_REPO_FULL) -> str:
    """Extract the explicit target repository from an issue body.

    Returns the normalized allow-listed ``owner/repo``. When no marker is
    present, returns the normalized ``default`` (self-target). When a
    marker is present but names an unapproved repository or URL, raises
    :class:`TargetRepoError` instead of silently defaulting (fail closed).
    The last marker in the body wins so a corrected declaration supersedes
    an earlier one.
    """
    normalized_default = normalize_target_repo(default)
    if not isinstance(issue_body, str) or not issue_body:
        return normalized_default
    matches = list(_TARGET_MARKER_RE.finditer(issue_body))
    if not matches:
        return normalized_default
    raw_repo = matches[-1].group("repo")
    raw_repo = "".join(raw_repo.split())
    return normalize_target_repo(raw_repo)


@dataclass(frozen=True)
class TargetSpec:
    """Pinned cross-repository execution target for one issue attempt."""

    source_issue: int
    source_repo: str = SOURCE_REPO_FULL
    target_repo: str = SOURCE_REPO_FULL
    target_base_ref: str = TARGET_BASE_REF_DEFAULT
    target_base_sha: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_issue, int) or self.source_issue <= 0:
            raise TargetRepoError("source_issue must be a positive integer")
        object.__setattr__(self, "source_repo",
                            normalize_target_repo(self.source_repo))
        object.__setattr__(self, "target_repo",
                            normalize_target_repo(self.target_repo))
        if not (self.target_base_ref or "").strip():
            raise TargetRepoError("target_base_ref must not be empty")
        object.__setattr__(self, "target_base_ref",
                            self.target_base_ref.strip())
        if not isinstance(self.target_base_sha, str):
            raise TargetRepoError("target_base_sha must be a string")
        object.__setattr__(self, "target_base_sha",
                            self.target_base_sha.strip())

    @property
    def target_url(self) -> str:
        return target_repo_url(self.target_repo)

    @property
    def cross_repo(self) -> bool:
        return self.target_repo != self.source_repo


def resolve_target_spec(
    *,
    issue_body: object = "",
    source_issue: int,
    target_base_ref: str = TARGET_BASE_REF_DEFAULT,
    target_base_sha: str = "",
    default: str = SOURCE_REPO_FULL,
) -> TargetSpec:
    """Parse the issue body marker and build the pinned target spec."""
    return TargetSpec(
        source_issue=source_issue,
        source_repo=SOURCE_REPO_FULL,
        target_repo=parse_target_repo(issue_body, default=default),
        target_base_ref=(target_base_ref or TARGET_BASE_REF_DEFAULT),
        target_base_sha=(target_base_sha or ""),
    )


def pin_target_base_sha(
    spec: TargetSpec,
    *,
    resolve_fn: Any = None,
    repository: str = "",
) -> TargetSpec:
    """Return ``spec`` with the exact target base SHA pinned.

    ``resolve_fn`` is ``(base_ref, repository) -> sha`` (e.g.
    ``GitHubApiClient.get_base_sha`` or ``WritebackClient.get_base_sha``
    bound to the target). When ``resolve_fn`` is None the spec is
    returned unchanged (offline path; the worker checks out the base ref
    and materialization re-checks the live base). Empty resolutions fail
    closed.
    """
    if resolve_fn is None:
        return spec
    repo = (repository or spec.target_repo).strip() or spec.target_repo
    sha = resolve_fn(spec.target_base_ref, repo)
    sha = (sha or "").strip()
    if not sha:
        raise TargetRepoError(
            "could not resolve target base SHA for %r@%r"
            % (spec.target_repo, spec.target_base_ref)
        )
    return TargetSpec(
        source_issue=spec.source_issue,
        source_repo=spec.source_repo,
        target_repo=spec.target_repo,
        target_base_ref=spec.target_base_ref,
        target_base_sha=sha,
    )


def validate_result_matches_target(
    result: Mapping[str, Any], spec: TargetSpec
) -> None:
    """Fail closed unless a worker result matches the pinned target spec."""
    if not isinstance(result, Mapping):
        raise TargetRepoError("result payload must be a JSON object")
    repository_url = str(result.get("repository_url", "") or "")
    try:
        result_repo = normalize_clone_url(repository_url)
    except TargetRepoError as exc:
        raise TargetRepoError("result repository mismatch: %s" % exc) from None
    if result_repo != spec.target_repo:
        raise TargetRepoError(
            "result repository mismatch: target %r, result %r"
            % (spec.target_repo, repository_url)
        )
    if spec.target_base_sha:
        result_sha = str(result.get("base_sha", "") or "").strip()
        if not result_sha:
            raise TargetRepoError(
                "result has no base_sha; expected %r" % spec.target_base_sha
            )
        if result_sha != spec.target_base_sha:
            raise TargetRepoError(
                "base SHA mismatch: result %r != pinned target %r"
                % (result_sha, spec.target_base_sha)
            )


# ---------------------------------------------------------------------------
# Cross-repository PR body (links back; never a bare "Closes #N").
# ---------------------------------------------------------------------------

def build_target_pr_body(
    *,
    source_issue: int,
    job_id: str = "",
    target_repo: str = TARGET_OPENCODE_FULL,
    target_base_ref: str = TARGET_BASE_REF_DEFAULT,
    target_base_sha: str = "",
    target_branch: str = "",
    summary: str = "",
    executed_model: str = "",
    source_repo: str = SOURCE_REPO_FULL,
) -> str:
    """Deterministic target PR body linking back to the source issue."""
    if not isinstance(source_issue, int) or source_issue <= 0:
        raise TargetRepoError("source_issue must be a positive integer")
    source = normalize_target_repo(source_repo)
    target = normalize_target_repo(target_repo)
    lines = [
        "Automated implementation for %s#%d." % (source, source_issue),
        "",
        "Source issue: https://github.com/%s/issues/%d" % (source, source_issue),
        "Target repository: %s" % target,
    ]
    details: list[str] = []
    if target_branch:
        details.append("branch: %s" % target_branch)
    if target_base_ref:
        details.append("base: %s" % target_base_ref)
    if target_base_sha:
        details.append("base_sha: %s" % target_base_sha)
    if job_id:
        details.append("job: %s" % job_id)
    if executed_model:
        details.append("model: %s" % executed_model)
    if summary:
        details.append("result: %s" % summary[:500])
    if details:
        lines += ["", "Cross-repository execution details:"] + [
            "- " + item for item in details
        ]
    return "\n".join(lines) + "\n"


def assert_pr_body_links_source(body: str, source_issue: int) -> None:
    """Fail closed unless a target PR body links the source issue safely."""
    if not isinstance(source_issue, int) or source_issue <= 0:
        raise TargetRepoError("source_issue must be a positive integer")
    text = body if isinstance(body, str) else ""
    expected_link = "%s#%d" % (SOURCE_REPO_FULL, source_issue)
    if expected_link not in text:
        raise TargetRepoError(
            "target PR body does not link source issue %s" % expected_link
        )
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^Closes\s+#\d+\s*$", stripped, re.IGNORECASE):
            raise TargetRepoError(
                "target PR body must not use a bare 'Closes #N' "
                "(would close the wrong issue in the target repo)"
            )


# ---------------------------------------------------------------------------
# Execution evidence (auditable, non-secret).
# ---------------------------------------------------------------------------

def build_execution_evidence(
    *,
    spec: TargetSpec,
    job_id: str = "",
    target_head_sha: str = "",
    target_branch: str = "",
    target_pr: int | None = None,
    writeback_action: str = "",
) -> dict[str, Any]:
    """Auditable non-secret evidence for one cross-repo attempt."""
    return {
        "source_repo": spec.source_repo,
        "source_issue": spec.source_issue,
        "target_repo": spec.target_repo,
        "target_repo_url": spec.target_url,
        "target_base_ref": spec.target_base_ref,
        "target_base_sha": spec.target_base_sha,
        "target_branch": target_branch or "",
        "target_head_sha": (target_head_sha or "").strip(),
        "target_pr": target_pr,
        "job_id": job_id or "",
        "writeback_action": writeback_action or "",
        "cross_repo": spec.cross_repo,
    }


def format_evidence_line(evidence: Mapping[str, Any]) -> str:
    """One-line redacted evidence summary for logs and store reasons."""
    return "source=%s#%s target=%s base=%s head=%s branch=%s pr=%s job=%s" % (
        evidence.get("source_repo", "-"),
        evidence.get("source_issue", "-"),
        evidence.get("target_repo", "-"),
        (str(evidence.get("target_base_sha", ""))[:12] or "-"),
        (str(evidence.get("target_head_sha", ""))[:12] or "-"),
        evidence.get("target_branch", "-") or "-",
        evidence.get("target_pr", "-") if evidence.get("target_pr") is not None else "-",
        evidence.get("job_id", "-") or "-",
    )


# ---------------------------------------------------------------------------
# Credential policy: App first, PAT only as temporary/bootstrap path, and
# write credentials never reach the ephemeral OpenCode child process.
# ---------------------------------------------------------------------------

# Temporary/bootstrap PAT for target-repository writes when a GitHub App
# installation token is unavailable. Never logged; never passed to workers.
TARGET_WRITE_PAT_ENV = "TARGET_REPO_PAT"

# Bootstrap PAT candidates, most-specific first. TAP_PAT/GH_TOKEN/
# GITHUB_TOKEN reuse follows the knowledge-store bootstrap precedent
# (automation/knowledge_store.py): only when already configured and
# scoped appropriately, never created or required here.
BOOTSTRAP_PAT_ENV_NAMES = (
    TARGET_WRITE_PAT_ENV,
    "TAP_PAT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

# Every env name that must never appear in an ephemeral OpenCode child
# environment: GitHub App key material plus every bootstrap PAT spelling.
# Mirrors automation/opencode_runner.py:WORKER_SCRUB_ENV_NAMES and
# automation/knowledge_store.py:STORAGE_CREDENTIAL_ENV_NAMES (both
# extended for TARGET_REPO_PAT in this issue).
TARGET_WRITE_CREDENTIAL_ENV_NAMES = (
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_INSTALLATION_ID",
    TARGET_WRITE_PAT_ENV,
    "TAP_PAT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def resolve_bootstrap_pat(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the bootstrap PAT value, or "" when none is configured.

    Most-specific spelling wins (``TARGET_REPO_PAT`` first). The value
    is returned for trusted-layer use only; callers must never log it or
    place it in a worker child environment.
    """
    source = environ if environ is not None else os.environ
    try:
        getter = source.get  # type: ignore[attr-defined]
    except AttributeError:
        raise TargetRepoError("environ must be a mapping")
    for name in BOOTSTRAP_PAT_ENV_NAMES:
        try:
            value = getter(name, "")
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def scrub_target_credentials(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child-process env without target write credentials."""
    source = environ if environ is not None else os.environ
    try:
        items = dict(source)  # type: ignore[arg-type]
    except Exception:
        raise TargetRepoError("environ must be a mapping")
    for name in TARGET_WRITE_CREDENTIAL_ENV_NAMES:
        items.pop(name, None)
    return {str(k): str(v) for k, v in items.items()}


def assert_no_target_credentials_in_worker_env(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail closed when a worker child env carries write credentials."""
    source = environ if environ is not None else os.environ
    try:
        getter = source.get  # type: ignore[attr-defined]
    except AttributeError:
        raise TargetRepoError("environ must be a mapping")
    present = [
        name
        for name in TARGET_WRITE_CREDENTIAL_ENV_NAMES
        if str(getter(name, "") or "").strip()
    ]
    if present:
        raise TargetRepoError(
            "worker environment must never carry target write credentials "
            "(present: %s)" % ", ".join(sorted(present))
        )
