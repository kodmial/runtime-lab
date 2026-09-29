"""Ephemeral Render execution lifecycle and GitHub <-> runner contract (issue #1).

P0 foundation: exactly one temporary Render web service per execution attempt,
deleted unconditionally afterwards. Suspension is only an emergency fallback
when deletion temporarily fails; the workflow must still retry deletion within
bounded limits and must never leave the temporary service behind on success.

Temporary development harness note (issue #4): GitHub Actions orchestration in
``automation/render-job.sh`` / ``automation/render-cleanup.sh`` is a
temporary control plane used only to develop and test the Render lifecycle
and runner before the direct GitHub->Render integration exists. The final
production path must not require an OpenCode GitHub Actions job. This module
is the factored, reusable core (pure helpers plus payload/state
classification) so the later Render controller can call it directly instead
of the logic staying permanently embedded in Actions.

Official Render API documentation used (pinned 2026-05-29 / 2025-10-27):
- RENDER_DOC_CREATE_SERVICE
- RENDER_DOC_RETRIEVE_SERVICE
- RENDER_DOC_LIST_SERVICES
- RENDER_DOC_DELETE_SERVICE
- RENDER_DOC_SUSPEND_SERVICE
- RENDER_DOC_RETRIEVE_DEPLOY
- RENDER_DOC_LIST_DEPLOYS
- RENDER_DOC_RATE_LIMITING
- RENDER_DOC_API_OVERVIEW

Authentication: Render API calls use the repository Actions secret named KEY,
referenced in workflows only as ${{ secrets.KEY }} (mapped to RENDER_API_KEY
at runtime) and never printed. GitHub-side writes use the workflow-provided
GITHUB_TOKEN; the Render runner itself holds no GitHub credentials in this
iteration (no manually created PAT or GitHub App token required).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
import re

KNOWLEDGE_PROTOCOL_PATH = "automation/knowledge/PROTOCOL.md"
KNOWLEDGE_TOPICS_DIR = "automation/knowledge/topics"
KNOWLEDGE_EXPERIMENTS_DIR = "automation/knowledge/experiments"

def experiment_record_path(issue_number: int, run_id: object = "") -> str:
    """Return the unique repository path for one automation run record."""
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    raw = str(run_id or "unknown").strip() or "unknown"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw)
    safe = safe.strip("-.")[:96] or "unknown"
    return "%s/issue-%d-run-%s.md" % (KNOWLEDGE_EXPERIMENTS_DIR, issue_number, safe)

def knowledge_handoff_instructions(issue_number: int, run_id: object = "") -> str:
    """Build mandatory repository-memory handoff instructions for an agent."""
    record = experiment_record_path(issue_number, run_id)
    return (
        "Repository knowledge handoff (mandatory):\n"
        "1. Read %s before changing code.\n"
        "2. Read relevant topic notes under %s and prior records under %s for this issue/topic.\n"
        "3. Do not repeat a known failed experiment unless a material premise changed; state that changed premise.\n"
        "4. Before finishing, write exactly one run record at %s. Separate observations, interpretation, and decisions; include evidence/tests and unresolved questions; never include secrets.\n"
        "5. Promote only validated reusable facts into the relevant topic note; preserve superseded history."
        % (KNOWLEDGE_PROTOCOL_PATH, KNOWLEDGE_TOPICS_DIR, KNOWLEDGE_EXPERIMENTS_DIR, record)
    )

EXPERIMENT_RECORD_REQUIRED_SECTIONS = (
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


def validate_experiment_record_text(
    text: str, issue_number: int, run_id: object = ""
) -> bool:
    """Fail closed unless an experiment record has the required identity/schema.

    Records carry strict JSON front matter (a JSON object between the
    leading ``---`` delimiters); the Markdown body keeps the required
    human-narrative sections. Domain invariants are enforced through
    :mod:`knowledge_catalog` so the hard gate and the derived catalog
    agree on what a valid record is.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("experiment record is empty")
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    try:
        from knowledge_catalog import (  # type: ignore[import-not-found]
            check_required_sections,
            parse_record_text,
            validate_metadata_shapes,
        )
    except ImportError:  # pragma: no cover - depends on sys.path layout
        from automation.knowledge_catalog import (  # type: ignore[import-not-found]
            check_required_sections,
            parse_record_text,
            validate_metadata_shapes,
        )
    expected_run = str(run_id or "unknown").strip() or "unknown"
    try:
        metadata, body = parse_record_text(text)
        validate_metadata_shapes(metadata)
        check_required_sections(body)
    except ValueError as exc:
        # Normalize catalog errors to the historic hard-gate messages
        # where they describe the same failure class.
        message = str(exc)
        if "missing required section" in message:
            raise
        raise ValueError("experiment record identity/schema mismatch: %s" % message)
    if metadata.get("issue") != issue_number or metadata.get("run_id") != expected_run:
        raise ValueError("experiment record identity/schema mismatch")
    return True


# ---------------------------------------------------------------------------
# Official Render API references (exact documentation used).
# ---------------------------------------------------------------------------

RENDER_API_BASE = "https://api.render.com/v1"

RENDER_DOC_API_OVERVIEW = "https://render.com/docs/api"
RENDER_DOC_CREATE_SERVICE = "https://api-docs.render.com/reference/create-service"
RENDER_DOC_RETRIEVE_SERVICE = "https://api-docs.render.com/reference/retrieve-service"
RENDER_DOC_LIST_SERVICES = "https://api-docs.render.com/reference/list-services"
RENDER_DOC_LIST_WORKSPACES = "https://api-docs.render.com/reference/list-owners"
RENDER_DOC_DELETE_SERVICE = "https://api-docs.render.com/reference/delete-service"
RENDER_DOC_SUSPEND_SERVICE = "https://api-docs.render.com/reference/suspend-service-1"
RENDER_DOC_RETRIEVE_DEPLOY = "https://api-docs.render.com/reference/retrieve-deploy"
RENDER_DOC_LIST_DEPLOYS = "https://api-docs.render.com/reference/list-deploys"
RENDER_DOC_RATE_LIMITING = "https://api-docs.render.com/reference/rate-limiting"
# Free-tier platform behavior (restart-anytime, ephemeral filesystem,
# spin-down on idle): verified against the live vendor doc during the
# issue #33 repair (run 36409152332). The runner keeps jobs only in
# worker process memory, so a documented anytime-restart wipes the polled
# job id permanently while /health stays healthy.
RENDER_DOC_FREE_TIER = "https://render.com/docs/free"

RENDER_DOC_URLS = (
    RENDER_DOC_API_OVERVIEW,
    RENDER_DOC_CREATE_SERVICE,
    RENDER_DOC_RETRIEVE_SERVICE,
    RENDER_DOC_LIST_SERVICES,
    RENDER_DOC_LIST_WORKSPACES,
    RENDER_DOC_DELETE_SERVICE,
    RENDER_DOC_SUSPEND_SERVICE,
    RENDER_DOC_RETRIEVE_DEPLOY,
    RENDER_DOC_LIST_DEPLOYS,
    RENDER_DOC_RATE_LIMITING,
    RENDER_DOC_FREE_TIER,
)

# ---------------------------------------------------------------------------
# Render API operations (implementation-facing endpoint constants).
# ---------------------------------------------------------------------------

# POST /v1/services -> 201 with {service, deployId}; rate limit 20/hour.
RENDER_CREATE_SERVICE_METHOD = "POST"
RENDER_CREATE_SERVICE_PATH = "/v1/services"

# GET /v1/services/{serviceId} -> 200 service object; 404/410 when gone.
RENDER_RETRIEVE_SERVICE_METHOD = "GET"
RENDER_RETRIEVE_SERVICE_PATH_TEMPLATE = "/v1/services/{serviceId}"

# GET /v1/services?limit=.. -> 200 list; used as deletion-verification fallback.
RENDER_LIST_SERVICES_METHOD = "GET"
RENDER_LIST_SERVICES_PATH = "/v1/services"

# GET /v1/services/{serviceId}/deploys/{deployId} -> 200 deploy object.
RENDER_RETRIEVE_DEPLOY_METHOD = "GET"
RENDER_RETRIEVE_DEPLOY_PATH_TEMPLATE = "/v1/services/{serviceId}/deploys/{deployId}"

# GET /v1/services/{serviceId}/deploys?limit=.. -> deploy list for polling.
RENDER_LIST_DEPLOYS_METHOD = "GET"
RENDER_LIST_DEPLOYS_PATH_TEMPLATE = "/v1/services/{serviceId}/deploys"

# POST /v1/services/{serviceId}/suspend -> 202; EMERGENCY FALLBACK ONLY.
RENDER_SUSPEND_SERVICE_METHOD = "POST"
RENDER_SUSPEND_SERVICE_PATH_TEMPLATE = "/v1/services/{serviceId}/suspend"

# DELETE /v1/services/{serviceId} -> 204; the primary cleanup mechanism.
RENDER_DELETE_SERVICE_METHOD = "DELETE"
RENDER_DELETE_SERVICE_PATH_TEMPLATE = "/v1/services/{serviceId}"

RENDER_DELETE_SUCCESS_STATUS = 204
# Retrieve/list statuses that prove the temporary service no longer exists.
RENDER_DELETION_VERIFIED_STATUSES = frozenset({404, 410})


def render_path(template: str, **params: str) -> str:
    """Expand a Render path template such as /v1/services/{serviceId}."""
    path = template
    for key, value in params.items():
        token = "{" + key + "}"
        if token not in path:
            raise ValueError("unknown path parameter: %s" % key)
        if not value:
            raise ValueError("empty path parameter: %s" % key)
        path = path.replace(token, value)
    if "{" in path or "}" in path:
        raise ValueError("unresolved path template: %s" % path)
    return path


def render_url(path: str) -> str:
    """Join a Render API path with the API base URL."""
    if not path.startswith("/v1/"):
        raise ValueError("Render API path must start with /v1/: %r" % path)
    return RENDER_API_BASE + path


def extract_owner_id(payload: object) -> str:
    """Extract a workspace id from the Render List Workspaces response.

    The documented response shape is a list whose first item contains an
    owner object with an id field. A legacy top-level id is accepted
    defensively, but malformed responses fail closed.
    """
    if (
        not isinstance(payload, Sequence)
        or isinstance(payload, (str, bytes, bytearray))
        or not payload
    ):
        raise ValueError("Render workspaces response must be a non-empty array")
    first = payload[0]
    if not isinstance(first, Mapping):
        raise ValueError("Render workspace entry must be an object")
    owner = first.get("owner")
    value = owner.get("id") if isinstance(owner, Mapping) else first.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Render workspace entry has no owner.id")
    return value.strip()


# ---------------------------------------------------------------------------
# Rate-limit behavior (from RENDER_DOC_RATE_LIMITING).
# ---------------------------------------------------------------------------

# POST /v1/services is limited to 20/hour per user.
RENDER_CREATE_SERVICE_RATE_LIMIT = "20/hour"
# PATCH /v1/services, deploy/resume/suspend per service and deploy hooks:
# 10/minute/service.
RENDER_MUTATING_SERVICE_RATE_LIMIT = "10/minute/service"
# Generic buckets carried over so callers handle 429 correctly.
RENDER_GENERIC_WRITE_RATE_LIMIT = "30/minute"
RENDER_GENERIC_READ_RATE_LIMIT = "400/minute"

# Every Render response carries these headers; 429 responses add Retry-After.
RENDER_RATE_LIMIT_HEADERS = (
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Reset",
    "Retry-After",
)

# HTTP statuses that are safe to retry with bounded backoff. 429 is the
# documented Render rate-limit signal; 5xx are transient server errors. 4xx
# other than 429 (e.g. 400/401/403/404) are not retried, except that the
# shell wrappers treat 404/410 on DELETE/retrieve as "already gone".
RENDER_RATE_LIMITED_STATUS = 429
RENDER_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Bounded retry envelope shared by the shell wrappers so both the job and
# cleanup paths respect 429/Retry-After identically.
API_RETRY_MAX_ATTEMPTS = 5
API_RETRY_BASE_SECONDS = 5
API_RETRY_CAP_SECONDS = 120


def is_rate_limited(http_status: int | None) -> bool:
    """True when the status is Render's documented rate-limit signal (429)."""
    return http_status == RENDER_RATE_LIMITED_STATUS


def is_retryable_render_status(http_status: int | None) -> bool:
    """True for 429 and transient 5xx statuses that merit bounded retry."""
    return http_status in RENDER_RETRYABLE_STATUSES


def parse_retry_after(value: object, default: int = API_RETRY_BASE_SECONDS,
                       cap: int = API_RETRY_CAP_SECONDS) -> int:
    """Parse a Retry-After header value into bounded seconds.

    Returns ``default`` for missing/unparsable values and clamps every
    result into ``[0, cap]`` so callers can sleep without extra checks.
    """
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    if seconds < 0:
        return default
    return min(seconds, cap)


def rate_limit_backoff_seconds(attempt: int, retry_after: int | None = None,
                               base: int = API_RETRY_BASE_SECONDS,
                               cap: int = API_RETRY_CAP_SECONDS) -> int:
    """Bounded backoff for attempt N (1-indexed).

    Uses exponential ``base * 2**(attempt-1)`` capped at ``cap``, but honors
    an explicit server-provided ``retry_after`` (already clamped) when given.
    Attempt numbers below 1 are treated as attempt 1.
    """
    attempt = max(1, int(attempt))
    if retry_after is not None:
        try:
            return max(0, min(int(retry_after), cap))
        except (TypeError, ValueError):
            pass
    return min(cap, base * (2 ** (attempt - 1)))

# ---------------------------------------------------------------------------
# Free-tier guard: fail closed, never silently create a paid resource.
# ---------------------------------------------------------------------------

FREE_PLAN = "free"
# Only this exact plan value is permitted for ephemeral workers.
ALLOWED_SERVICE_PLANS = frozenset({FREE_PLAN})


class PaidPlanError(ValueError):
    """Raised when a Render response/request indicates a non-free plan."""


def assert_free_plan(plan: str) -> str:
    """Fail closed unless the plan is exactly the free tier."""
    if plan not in ALLOWED_SERVICE_PLANS:
        raise PaidPlanError(
            "refusing non-free Render plan %r; only %r is allowed"
            % (plan, FREE_PLAN)
        )
    return plan


def extract_service_plan(service: Mapping[str, Any]) -> str:
    """Extract serviceDetails.plan from a Render service object (or fail)."""
    try:
        details = service["serviceDetails"]
    except KeyError as exc:
        raise PaidPlanError("Render service object has no serviceDetails") from exc
    if not isinstance(details, Mapping) or "plan" not in details:
        raise PaidPlanError("Render service object has no serviceDetails.plan")
    plan = details["plan"]
    if not isinstance(plan, str) or not plan:
        raise PaidPlanError("Render service plan is missing or not a string")
    return plan


def verify_free_plan_response(service: Mapping[str, Any]) -> str:
    """Verify a Render create/retrieve response is free-tier; abort otherwise."""
    return assert_free_plan(extract_service_plan(service))


def get_service_url(service: Mapping[str, Any]) -> str:
    """Return the externally reachable URL of a Render web service."""
    details = service.get("serviceDetails")
    if not isinstance(details, Mapping):
        raise ValueError("Render service object has no serviceDetails mapping")
    url = details.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Render service has no externally reachable https url")
    return url


# ---------------------------------------------------------------------------
# Region / model policy for ephemeral workers.
# ---------------------------------------------------------------------------

DEFAULT_WORKER_REGION = "oregon"
NON_US_FALLBACK_REGION = "singapore"
# Muse jobs may run only in the US or Singapore; frankfurt is forbidden.
ALLOWED_WORKER_REGIONS = frozenset({"oregon", "ohio", "virginia", "singapore"})
FORBIDDEN_WORKER_REGIONS = frozenset({"frankfurt"})

PREFERRED_MODEL = "opencode/muse-spark-1.3-contributor-free"
FALLBACK_MODEL = "opencode/space-bunny-free"
# Model fallback must happen inside the same worker attempt; provisioning a
# second Render service merely to change models is forbidden.
MODEL_FALLBACK_CREATES_NEW_SERVICE = False


class RegionPolicyError(ValueError):
    """Raised when a worker region violates the Muse execution policy."""


def validate_worker_region(region: str) -> str:
    """Reject forbidden regions (fail closed) and unknown region IDs."""
    if region in FORBIDDEN_WORKER_REGIONS:
        raise RegionPolicyError(
            "region %r is forbidden for Muse jobs; use one of %s"
            % (region, sorted(ALLOWED_WORKER_REGIONS))
        )
    if region not in ALLOWED_WORKER_REGIONS:
        raise RegionPolicyError(
            "unknown worker region %r; allowed: %s"
            % (region, sorted(ALLOWED_WORKER_REGIONS))
        )
    return region


def select_model(primary_failed: bool) -> str:
    """Select the OpenCode model inside the current worker attempt."""
    return FALLBACK_MODEL if primary_failed else PREFERRED_MODEL


EXECUTION_MODES = frozenset({"smoke", "e2e"})

# Upper bound for issue body text embedded in a job payload task field.
MAX_TASK_BODY_CHARS = 2000


def validate_execution_mode(mode: str) -> str:
    """Fail closed unless the mode is one of smoke/e2e."""
    if mode not in EXECUTION_MODES:
        raise ValueError("execution_mode must be one of %s, got %r"
                         % (sorted(EXECUTION_MODES), mode))
    return mode


def validate_model_name(model: str) -> str:
    """Fail closed unless the model is the preferred or fallback model."""
    if model not in (PREFERRED_MODEL, FALLBACK_MODEL):
        raise ValueError("unknown model: %r" % (model,))
    return model


def resolve_task_text(issue_number: int, execution_mode: str,
                      title: str = "", body: str = "",
                      run_id: object = "") -> str:
    """Build runner task text plus mandatory repository-memory handoff."""
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    validate_execution_mode(execution_mode)
    title = (title or "").strip()
    body_text = (body or "").strip()
    if len(body_text) > MAX_TASK_BODY_CHARS:
        body_text = body_text[:MAX_TASK_BODY_CHARS] + "...[truncated]"
    if title and body_text:
        task = ("Issue #%d [%s]: %s\n\n%s" % (issue_number, execution_mode, title, body_text))
    elif title:
        task = "Issue #%d [%s]: %s" % (issue_number, execution_mode, title)
    elif body_text:
        task = "Issue #%d [%s]:\n\n%s" % (issue_number, execution_mode, body_text)
    else:
        task = "Execute issue #%d in %s mode." % (issue_number, execution_mode)
    return task + "\n\n" + knowledge_handoff_instructions(issue_number, run_id)

def select_base_sha(*candidates: object) -> str:
    """Return the first non-empty candidate SHA (exact base revision).

    Candidates are tried in order (e.g. GITHUB_SHA, then ``git rev-parse
    HEAD``). Returns "" when every candidate is empty so callers can
    decide whether to proceed without a pinned SHA or fail closed.
    """
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


# ---------------------------------------------------------------------------
# Exact GitHub Actions workflow-artifact requirements (issue #115).
# ---------------------------------------------------------------------------

# GitHub's artifact download endpoint requires an OAuth/PAT credential
# (current contract https://docs.github.com/en/rest/actions/artifacts,
# re-verified 2026-09-28: "Download an artifact" needs an OAuth or
# personal access token). The ephemeral Render worker carries no GitHub
# credential: the start command provisions no token, and the OpenCode
# child environment is credential-scrubbed (issue #39), so neither the
# runner nor the agent can fetch a workflow artifact at runtime.
GITHUB_ARTIFACT_DOWNLOAD_DOC = (
    "https://docs.github.com/en/rest/actions/artifacts"
)

# An exact workflow-artifact contract is the conjunction of a numeric
# Actions artifact id, the numeric source workflow run that produced it,
# and a sha256 archive/checksum identity. All three must be present:
# bodies that merely mention "artifact" in passing (design notes, the
# issue #86 release-fingerprint vocabulary with opencode-<track>-<sha12>
# ids and github-release: refs) must never trip this gate.
EXACT_WORKFLOW_ARTIFACT_ID_RE = re.compile(
    r"artifact\s+id\s*[:=]?\s*`?(\d{5,})`?", re.IGNORECASE
)
EXACT_WORKFLOW_ARTIFACT_RUN_RE = re.compile(
    r"(?:source\s+)?workflow\s+run\s*[:=]?\s*`?(\d{5,})`?",
    re.IGNORECASE,
)
EXACT_WORKFLOW_ARTIFACT_DIGEST_RE = re.compile(
    r"sha256:([0-9a-fA-F]{64})"
)
EXACT_WORKFLOW_ARTIFACT_NAME_RE = re.compile(
    r"artifact\s+name\s*:\s*`?([A-Za-z0-9_.\-]+)`?", re.IGNORECASE
)
EXACT_WORKFLOW_ARTIFACT_SOURCE_SHA_RE = re.compile(
    r"source\s+sha\s*:\s*`?([0-9a-fA-F]{40})`?", re.IGNORECASE
)
EXACT_WORKFLOW_ARTIFACT_REPO_RE = re.compile(
    r"repository\s*:\s*`?([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)`?",
    re.IGNORECASE,
)
EXACT_WORKFLOW_ARTIFACT_BINARY_SHA_RE = re.compile(
    r"binary\s+SHA-?256\s*[:=]?\s*`?([0-9a-fA-F]{64})`?", re.IGNORECASE
)

# Supported exact-artifact contracts (issues #128 + #140): the corrected
# PR #12 artifact and the exact PR #15 artifact share one concrete
# delivery mechanism (automation/exact_artifact_delivery.py:
# controller-side credentialed fetch + verified push to the worker +
# absolute-path launch with /proc identity). Issues pinning exactly one
# of these contracts pass through the infrastructure-blocked gate; every
# other exact-artifact contract still fails closed via
# exact_workflow_artifact_blocker(). PR #12 validation is unchanged.
SUPPORTED_EXACT_ARTIFACT_ID = "11004835952"
SUPPORTED_EXACT_SOURCE_RUN_ID = "36498663107"
SUPPORTED_EXACT_ARCHIVE_SHA256 = (
    "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040"
)
SUPPORTED_EXACT_BINARY_SHA256 = (
    "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966"
)
SUPPORTED_EXACT_VERSION = "1.18.33"
SUPPORTED_EXACT_ARTIFACT_NAME = "opencode-coding-linux-x64"

# Exact PR #15 contract (issue #140; published artifact for issue #141). The binary
# digest/version are pinned; the Actions transport fields (artifact id /
# source run / archive digest) are captured at publish time by
# automation/opencode_pr15_publish.py. The gate requires the PR #15
# binary digest to be present in the issue body so an arbitrary numeric
# artifact can never ride this path.
#
# Transport pinning (issue #146, run 36511901291): artifact IDs are
# repo-scoped (https://docs.github.com/en/rest/actions/artifacts: the
# download endpoint is GET
# /repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip), while the
# controller downloads only from ``kodmial/opencode``. A numeric
# artifact from any other repository (e.g. the runtime-lab-local
# ``11009461073`` from run ``36511798742``) therefore 404s at fetch
# time. The gate pins the published immutable transport triple(s) below
# and refuses anything else pre-creation instead of burning a Render
# service on a fetch that cannot succeed. To route a future re-published
# PR #15 build, add its triple here explicitly (never accept "any
# numeric transport").
SUPPORTED_PR15_ARTIFACTS: dict[tuple[str, str], str] = {
    ("11009286301", "36512250023"): (
        "df547ac873c9591bc98e5ef43b9283b77f5a4295fc2f27cda6b280d18c313c46"
    ),
}
SUPPORTED_PR15_BINARY_SHA256 = (
    "d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0"
)
SUPPORTED_PR15_VERSION = "1.18.33"
SUPPORTED_PR15_ARTIFACT_NAME = "opencode-coding-linux-x64"
SUPPORTED_PR15_SOURCE_SHA = "842157c38db9f8178ed0eee7af32f7536fe2346e"
SUPPORTED_PR15_MERGE_SHA = "0d649350557c5ee3882cc55e5ca65f919ab304c4"
SUPPORTED_PR15_REPO = "kodmial/opencode"
SUPPORTED_PR15_PR = "15"
SUPPORTED_PR15_BRANCH = "coding-no-mini"


def parse_exact_binary_sha256(title: object = "", body: object = "") -> str:
    """Extract a ``binary SHA-256`` digest from issue text, else "".

    Never raises: unparsable input means "no digest".
    """
    try:
        text = "%s\n%s" % (title or "", body or "")
        match = EXACT_WORKFLOW_ARTIFACT_BINARY_SHA_RE.search(text)
        return match.group(1).lower() if match else ""
    except Exception:
        return ""


def parse_exact_artifact_repo(title: object = "", body: object = "") -> str:
    """Extract the ``Repository: <owner/repo>`` declaration, else "".

    Never raises: unparsable input means "no declaration".
    """
    try:
        text = "%s\n%s" % (title or "", body or "")
        match = EXACT_WORKFLOW_ARTIFACT_REPO_RE.search(text)
        return match.group(1).lower() if match else ""
    except Exception:
        return ""


def _is_numeric_id(value: object) -> bool:
    """True for numeric GitHub Actions ids (artifact/run, 5+ digits)."""
    try:
        text = str(value or "").strip()
    except Exception:
        return False
    return text.isdigit() and len(text) >= 5


def is_supported_pr15_workflow_artifact(
    requirement: Mapping[str, Any] | None,
    binary_sha256: object = "",
) -> bool:
    """True only for the exact PR #15 contract (issue #140). Never raises.

    Requires the published PR #15 binary fingerprint to be present in the issue body
    plus a pinned publish-time transport triple from
    ``SUPPORTED_PR15_ARTIFACTS`` (artifact id + source run + archive
    digest). A declared repository other than ``kodmial/opencode`` is
    never accepted (artifact IDs are repo-scoped, so a foreign triple
    404s at the controller fetch). A PR #15 binary claim under the PR
    #12 transport is never accepted.
    """
    try:
        if not isinstance(requirement, Mapping):
            return False
        raw_binary = str(binary_sha256 or "").strip().lower()
        if raw_binary != SUPPORTED_PR15_BINARY_SHA256:
            return False
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        archive = str(requirement.get("archive_sha256", "") or "").strip().lower()
        if not _is_numeric_id(artifact) or not _is_numeric_id(run):
            return False
        if len(archive) != 64 or any(
            ch not in "0123456789abcdef" for ch in archive
        ):
            return False
        if artifact == SUPPORTED_EXACT_ARTIFACT_ID or run == SUPPORTED_EXACT_SOURCE_RUN_ID:
            if archive == SUPPORTED_EXACT_ARCHIVE_SHA256:
                return False
        declared_repo = str(requirement.get("repository", "") or "").strip().lower()
        if declared_repo and declared_repo != SUPPORTED_PR15_REPO.lower():
            return False
        expected_archive = SUPPORTED_PR15_ARTIFACTS.get((artifact, run))
        if expected_archive is None or archive != expected_archive:
            return False
        return True
    except Exception:
        return False


def is_supported_exact_workflow_artifact(
    requirement: Mapping[str, Any] | None,
    binary_sha256: object = "",
) -> bool:
    """True for either supported contract (PR #12 or PR #15). Never raises.

    PR #12 (issue #128) requires artifact ``11004835952`` from source run
    ``36498663107`` with the pinned archive digest; when the issue body
    carries an explicit ``binary SHA-256`` it must equal the pinned binary
    digest (otherwise a different binary could ride the supported archive
    claim). PR #15 (issue #140) requires the published PR #15 binary fingerprint plus
    well-formed publish-time transport ids/digest. Never raises.
    """
    try:
        if not isinstance(requirement, Mapping):
            return False
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        archive = str(requirement.get("archive_sha256", "") or "").strip().lower()
        if (
            artifact == SUPPORTED_EXACT_ARTIFACT_ID
            and run == SUPPORTED_EXACT_SOURCE_RUN_ID
            and archive == SUPPORTED_EXACT_ARCHIVE_SHA256
        ):
            raw_binary = str(binary_sha256 or "").strip().lower()
            if raw_binary and raw_binary != SUPPORTED_EXACT_BINARY_SHA256:
                return False
            return True
        return is_supported_pr15_workflow_artifact(requirement, binary_sha256)
    except Exception:
        return False


def supported_exact_artifact_identity() -> dict[str, str]:
    """Machine-readable identity for the supported exact artifact."""
    try:
        try:
            from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
                build_exact_artifact_identity as _build,
            )
        except ImportError:
            from exact_artifact_delivery import (  # type: ignore[no-redef]
                build_exact_artifact_identity as _build,
            )
        return dict(_build())
    except ImportError:
        return {
            "artifact_id": SUPPORTED_EXACT_ARTIFACT_ID,
            "artifact_name": SUPPORTED_EXACT_ARTIFACT_NAME,
            "source_run_id": SUPPORTED_EXACT_SOURCE_RUN_ID,
            "archive_sha256": SUPPORTED_EXACT_ARCHIVE_SHA256,
            "binary_sha256": SUPPORTED_EXACT_BINARY_SHA256,
            "version": SUPPORTED_EXACT_VERSION,
        }


def pr15_exact_artifact_identity(
    requirement: Mapping[str, Any],
    binary_sha256: str = SUPPORTED_PR15_BINARY_SHA256,
) -> dict[str, str]:
    """Machine-readable identity for the exact PR #15 artifact.

    Transport ids/archive come from the parsed issue requirement
    (publish-time values); every pinned PR #15 field comes from the
    published PR #15 fingerprint. Fail closed on any mismatch.
    """
    if not isinstance(requirement, Mapping):
        raise ValueError("requirement must be a mapping")
    try:
        try:
            from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
                build_pr15_artifact_identity as _build_pr15,
            )
        except ImportError:
            from exact_artifact_delivery import (  # type: ignore[no-redef]
                build_pr15_artifact_identity as _build_pr15,
            )
        return dict(
            _build_pr15(
                str(requirement.get("artifact_id", "") or "").strip(),
                str(requirement.get("source_run_id", "") or "").strip(),
                str(requirement.get("archive_sha256", "") or "").strip().lower(),
            )
        )
    except ImportError:
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        archive = str(requirement.get("archive_sha256", "") or "").strip().lower()
        binary = str(binary_sha256 or "").strip().lower()
        if binary != SUPPORTED_PR15_BINARY_SHA256:
            raise ValueError("PR #15 binary digest mismatch")
        if not _is_numeric_id(artifact) or not _is_numeric_id(run):
            raise ValueError("PR #15 transport ids must be numeric")
        if len(archive) != 64:
            raise ValueError("PR #15 archive digest must be 64 hex chars")
        return {
            "artifact_id": artifact,
            "artifact_name": SUPPORTED_PR15_ARTIFACT_NAME,
            "source_run_id": run,
            "archive_sha256": archive,
            "binary_sha256": SUPPORTED_PR15_BINARY_SHA256,
            "version": SUPPORTED_PR15_VERSION,
            "source_sha": SUPPORTED_PR15_SOURCE_SHA,
            "merge_sha": SUPPORTED_PR15_MERGE_SHA,
            "repo": SUPPORTED_PR15_REPO,
            "pr": SUPPORTED_PR15_PR,
            "branch": SUPPORTED_PR15_BRANCH,
        }


def exact_artifact_gate_decision(
    title: object = "", body: object = ""
) -> tuple[dict[str, Any] | None, str, dict[str, str] | None]:
    """Decide the pre-creation gate for one issue (issues #128 + #140).

    Returns ``(requirement, blocker_message, supported_identity)``:

    - ``(None, "", None)``: no exact-artifact contract; ordinary path.
    - ``(req, "", identity)``: supported exact contract (PR #12 or PR #15);
      the caller must deliver ``identity`` through the submit/create
      payload instead of refusing.
    - ``(req, message, None)``: unsupported exact contract; the caller
      must refuse with ``message`` before any worker exists.
    """
    requirement = parse_exact_workflow_artifact_requirement(title, body)
    if requirement is None:
        return None, "", None
    binary_sha = parse_exact_binary_sha256(title, body)
    if not is_supported_exact_workflow_artifact(requirement, binary_sha):
        blocker = exact_workflow_artifact_blocker(requirement)
        blocker += unsupported_pr15_transport_note(requirement, binary_sha)
        return requirement, blocker, None
    if is_supported_pr15_workflow_artifact(requirement, binary_sha):
        return requirement, "", pr15_exact_artifact_identity(requirement, binary_sha)
    return requirement, "", supported_exact_artifact_identity()


def parse_exact_workflow_artifact_requirement(
    title: object = "", body: object = ""
) -> dict[str, Any] | None:
    """Return the exact workflow-artifact contract in an issue, if any.

    Matches only the strong conjunction used by artifact-qualification
    issues (e.g. issue #106): a numeric Actions artifact id plus the
    numeric source workflow run plus a sha256 archive/checksum digest.
    Returns a small evidence dict (artifact_id, artifact_name or "",
    source_run_id, archive_sha256, source_sha or "", repository or "") or None when the
    issue carries no such contract. Never raises: unparsable input
    means "no contract", never a gate trip.
    """
    try:
        text = "%s\n%s" % (title or "", body or "")
        id_match = EXACT_WORKFLOW_ARTIFACT_ID_RE.search(text)
        run_match = EXACT_WORKFLOW_ARTIFACT_RUN_RE.search(text)
        digest_match = EXACT_WORKFLOW_ARTIFACT_DIGEST_RE.search(text)
        if id_match is None or run_match is None or digest_match is None:
            return None
        name_match = EXACT_WORKFLOW_ARTIFACT_NAME_RE.search(text)
        sha_match = EXACT_WORKFLOW_ARTIFACT_SOURCE_SHA_RE.search(text)
        repo_match = EXACT_WORKFLOW_ARTIFACT_REPO_RE.search(text)
        return {
            "artifact_id": id_match.group(1),
            "artifact_name": name_match.group(1) if name_match else "",
            "source_run_id": run_match.group(1),
            "archive_sha256": digest_match.group(1).lower(),
            "source_sha": sha_match.group(1).lower() if sha_match else "",
            "repository": repo_match.group(1).lower() if repo_match else "",
        }
    except Exception:
        return None


# Validated advisories for known immutable workflow artifacts (issue
# #123): artifact IDs are immutable, so a proven finding about one
# exact artifact never goes stale. Keyed by
# (artifact_id, source_run_id); values are short evidence-backed notes
# appended to the infrastructure-blocked diagnostic so a future
# delivery mechanism cannot misread the artifact's own failure mode
# as a memory result. Data, not logic: adding a future advisory must
# not require touching the blocker itself.
KNOWN_WORKFLOW_ARTIFACT_ADVISORIES = {
    ("11001896223", "36492639568"): (
        " Known-artifact advisory: this exact PR #12 artifact "
        "(opencode-coding-linux-x64, binary SHA-256 "
        "a6dabf731c49999d5c95dd7477cd74f18070615f6a01a8ce36cd8e35b5dce38d) "
        "reports version 0.0.0 (unstamped build: the artifact workflow "
        "runs build:coding with no OPENCODE_VERSION/tag fetch) and every "
        "real-agent trial fast-fails in seconds at the provider free-tier "
        "gate ('OpenCode 1.18.0 or newer is required to use the free "
        "tier') with sub-ceiling peaks and zero memory-pressure events, "
        "so its fast-fail peaks are never memory-fit evidence; a "
        "version-stamped rebuild is required before any memory verdict, "
        "and the feasible qualification path where credentials exist is "
        "automation/opencode_max_headless_qualify.py (see "
        "automation/knowledge/experiments/issue-109-run-36493321957.md)."
    ),
}


def known_workflow_artifact_advisory(
    requirement: Mapping[str, Any] | None,
) -> str:
    """Return the validated advisory for a known artifact, else "".

    Never raises: unparsable input means "no advisory", never a gate
    trip and never a blocker failure.
    """
    try:
        if not isinstance(requirement, Mapping):
            return ""
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        return KNOWN_WORKFLOW_ARTIFACT_ADVISORIES.get((artifact, run), "")
    except Exception:
        return ""


# Superseded workflow artifacts (issue #133): structurally valid exact
# contracts the project has retired on purpose, so redispatching the
# same issue body can never become executable. Keyed by immutable
# (artifact_id, source_run_id), mirroring the coordinator's
# SUPERSEDED_ARTIFACT_IDS (automation/issue130_coordinator.py) plus the
# owner direction on source issue #110 declaring artifact 11001896223
# obsolete in favor of the version-stamped 11004835952 candidate owned
# by the #130 coordinator. Data, not logic: retiring a future artifact
# adds one entry here without touching the blocker itself.
SUPERSEDED_WORKFLOW_ARTIFACTS = {
    ("11001896223", "36492639568"): {
        "successor_artifact_id": "11004835952",
        "successor_source_run_id": "36498663107",
        "successor_archive_sha256": (
            "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040"
        ),
        "successor_binary_sha256": (
            "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966"
        ),
        "successor_version": "1.18.33",
        "owner": "automation/issue130_coordinator.py "
        "(objective-130-state.json, active candidate 11004835952)",
    },
}


def superseded_workflow_artifact_notice(
    requirement: Mapping[str, Any] | None,
) -> str:
    """Return the validated superseded-artifact redirect notice, else "".

    Never raises: unparsable or non-superseded input means "no
    notice", never a gate trip and never a blocker failure.
    """
    try:
        if not isinstance(requirement, Mapping):
            return ""
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact, run))
        if not entry:
            return ""
        return (
            " Superseded-artifact notice: artifact %s (workflow run %s) "
            "is retired and will never execute on Render -- redispatching "
            "this exact contract refuses identically. Use the current "
            "immutable successor artifact %s (workflow run %s, version %s) "
            "via %s instead of retrying this body."
            % (
                artifact,
                run,
                entry["successor_artifact_id"],
                entry["successor_source_run_id"],
                entry["successor_version"],
                entry["owner"],
            )
        )
    except Exception:
        return ""


def superseded_dispatch_guard(
    title: object = "", body: object = ""
) -> dict[str, Any] | None:
    """Pre-dispatch guard for retired exact-artifact contracts (issue #154).

    Run 36629441608 (source issue #106, smoke) proved the remaining
    reusable gap after the #133/#138 repairs: the pre-creation gate plus
    the superseded redirect plus the structured ``permanent`` refusal
    record are all correct, yet a smoke issue carries no
    ``qualification:render`` label, so the workflow finalize path emits
    ``classification=not-chain`` with an empty fingerprint and the
    fingerprint-dedup branch never fires. Every redispatch of the same
    retired body (artifact ``11001896223`` / run ``36492639568``) then
    refuses identically in seconds while minting one more P0 repair
    (up to ``MAX_RENDER_REPAIR_ATTEMPTS``) instead of staying paused.

    This helper is the reusable choke point future scheduler envelopes
    consult before dispatching: it returns a stable machine-readable
    guard dict when the issue text carries a retired
    (``SUPERSEDED_WORKFLOW_ARTIFACTS``) exact-artifact contract, else
    ``None`` for ordinary or supported-contract issues. Data-driven: retiring
    a future artifact adds one ``SUPERSEDED_WORKFLOW_ARTIFACTS`` entry
    without touching this logic. Never raises: unparsable input means
    "no guard", never a dispatch failure.
    """
    try:
        requirement = parse_exact_workflow_artifact_requirement(title, body)
        if requirement is None:
            return None
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact, run))
        if not entry:
            return None
        successor = dict(entry)
        reason = (
            "superseded exact artifact %s (workflow run %s) is retired "
            "and will never execute on Render -- redispatching this exact "
            "contract refuses identically. Use the current immutable "
            "successor artifact %s (workflow run %s, version %s) via %s "
            "instead of redispatching this body."
            % (
                artifact,
                run,
                entry["successor_artifact_id"],
                entry["successor_source_run_id"],
                entry["successor_version"],
                entry["owner"],
            )
        )
        return {
            "artifact_id": artifact,
            "source_run_id": run,
            "successor": successor,
            "reason": reason,
        }
    except Exception:
        return None


# Stable executor hold verdict naming a retired exact-artifact contract
# (repair issue #161, run 36632062583): `render-job.sh` prints this
# single machine-greppable line before the exact-artifact gate so
# provisioned envelopes can key repair minting on it without scraping
# free-form diagnostics. The structured refusal record carries the same
# verdict under `dispatch_hold` (see `superseded_hold_verdict`).
HELD_SUPERSEDED_VERDICT = "held-superseded"


def superseded_hold_verdict(
    requirement: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the machine-readable hold verdict for a parsed requirement.

    Repair issue #165 (failed run 36633812500): that run executed at a
    base already containing the #161 log-only `held-superseded` verdict,
    yet the durable refusal evidence uploaded as
    `render-qualification-106-36633812500` carries no hold marker --
    only `permanent`/`superseded`/`successor`. Any consumer of durable
    evidence (triage, provisioned scheduler envelopes) must therefore
    scrape log text to tell a held retired contract apart from a merely
    undeliverable one. This helper is the single choke point for the
    structured form: `{"held": True, "verdict": "held-superseded", ...}`
    with the retired `(artifact_id, source_run_id)` plus the immutable
    `successor` mapping when the requirement names a retired
    (``SUPERSEDED_WORKFLOW_ARTIFACTS``) contract, else `{"held": False,
    "verdict": "", ...}` with an empty successor so ordinary and
    unknown-contract refusals never misdirect. Data-driven: retiring a
    future artifact adds one registry entry without touching this
    logic. Never raises: unparsable input means "no hold", never a
    verdict failure.
    """
    try:
        if not isinstance(requirement, Mapping):
            raise ValueError("no requirement")
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact, run))
        if not entry:
            raise ValueError("not superseded")
        return {
            "held": True,
            "verdict": HELD_SUPERSEDED_VERDICT,
            "artifact_id": artifact,
            "source_run_id": run,
            "successor": dict(entry),
        }
    except Exception:
        try:
            mapping = requirement if isinstance(requirement, Mapping) else {}
            artifact = str(mapping.get("artifact_id", "") or "").strip()
            run = str(mapping.get("source_run_id", "") or "").strip()
        except Exception:
            artifact, run = "", ""
        return {
            "held": False,
            "verdict": "",
            "artifact_id": artifact,
            "source_run_id": run,
            "successor": {},
        }


# Stable executor hold verdict for a proven ordinary-smoke capacity
# mismatch (repair issue #176, run 36637940250): `render-job.sh` prints
# this single machine-greppable line before any Render service is
# created, so provisioned envelopes can key repair minting on it
# without scraping free-form storm diagnostics. The structured held
# record carries the same verdict under `capacity_hold` (see
# `capacity_hold_verdict` / `build_capacity_hold_result`).
HELD_CAPACITY_VERDICT = "held-capacity-mismatch"


# Ordinary-smoke capacity-mismatch holds (repair issue #176).
#
# Run 36637940250 (source issue #58, smoke, ordinary baseline binary)
# proved the remaining reusable gap after the #75/#159/#164/#170
# repairs: it executed at base `bc76158`, which already contains the
# complete validated config-only profile (production
# `OPENCODE_CONFIG_CONTENT` equals `lowmem_config()` exactly, every
# qualified env kill-switch wired with setdefault semantics), and
# still storm-aborted with the identical signature as the four prior
# storms (4 consecutive proven worker restarts, cgroup pinned at the
# 512 MB limit with usage ratio 1.0, PRESSURE via the replacements
# branch, Python RSS ~33 MB). The storm breaker fired exactly as
# designed, so the harness is not the defect; the pinned baseline
# agent peak (~600-615 MB, issue #52) physically exceeds the Free
# worker (0.1 CPU / 512 MB, `RENDER_DOC_FREE_TIER`), and no further
# config-only switch exists to wire (re-wiring is forbidden by the
# knowledge protocol: the defect each prior repair targeted is
# already absent).
#
# Redispatching the identical ordinary-smoke payload therefore storms
# identically while burning ~13 minutes and four restarts per cycle.
# This registry holds such proven issues at dispatch time until the
# named premise changes. Data-driven: a future smaller qualified
# binary (coding-only/direct-headless/lite with a measured live
# delta, owned by the qualification chain) or a larger worker lifts
# the hold by removing the entry -- never by redispatching the held
# body. Exact-artifact bodies are excluded: they ride the exact gate
# and the superseded path, never this hold.
CAPACITY_MISMATCH_HOLDS: dict[int, dict[str, Any]] = {
    58: {
        "mode": "smoke",
        "contract": "ordinary",
        "signature": (
            "pinned-at-ceiling replacements restart storm: cgroup "
            "limit 512 MB, usage ratio 1.0, 4 consecutive proven "
            "worker restarts, memory-pressure verdict via "
            "replacements, agent peak ~600 MB vs 512 MB budget"
        ),
        "proving_run_id": "36637940250",
        "proving_base_sha": (
            "bc761583a13c875dbeb2b97196985ee9a73e0289"
        ),
        "storm_series_run_ids": (
            "36442675039,36449610030,36629689414,36632841000,"
            "36635571284,36637940250"
        ),
        "successor": {
            "owner": "qualification-chain",
            "condition": (
                "smaller qualified binary with a measured live "
                "memory delta (coding-only/direct-headless/lite) or "
                "a larger worker; remove this entry only when that "
                "premise lands, then redispatch for a real measurement"
            ),
        },
    },
}


def capacity_dispatch_hold(
    issue_number: object = 0, title: object = "", body: object = ""
) -> dict[str, Any] | None:
    """Pre-dispatch hold for proven ordinary-smoke capacity mismatches.

    Run 36637940250 (source issue #58) executed at the full-profile
    base `bc76158` and storm-aborted identically to the four prior
    storms, proving the baseline agent workload cannot fit the 512 MB
    Free worker no matter how often the identical payload is
    redispatched. The storm breaker already fails each attempt fast
    (~13 minutes, four restarts); without a dispatch hold every
    redispatch burns another identical storm while the finalize path
    mints another P0 repair (smoke issues carry no qualification
    fingerprint, so the fingerprint-dedup branch never fires).

    This helper is the reusable choke point scheduler/executor
    envelopes consult before dispatching: it returns a stable
    machine-readable hold dict when the issue number names a
    `CAPACITY_MISMATCH_HOLDS` entry and the issue text is the
    ordinary (non-exact-artifact) shape, else `None`. Data-driven:
    lifting a hold removes its registry entry when the successor
    premise lands, without touching this logic. Never raises:
    unparsable input means "no hold", never a dispatch failure.
    """
    try:
        issue = int(issue_number)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        entry = CAPACITY_MISMATCH_HOLDS.get(issue)
        if not entry:
            return None
        try:
            requirement = parse_exact_workflow_artifact_requirement(
                title, body
            )
        except Exception:
            requirement = None
        if requirement is not None:
            return None
        successor = entry.get("successor", {})
        successor = dict(successor) if isinstance(successor, Mapping) else {}
        reason = (
            "ordinary-smoke issue #%d is held-capacity-mismatch: the "
            "proven %s at full profile cannot fit the 512 MB Free "
            "worker (proving run %s at base %s), so redispatching the "
            "identical payload storms identically. Held until the "
            "successor premise lands via %s: %s."
            % (
                issue,
                entry.get("signature", "restart storm"),
                entry.get("proving_run_id", "?"),
                entry.get("proving_base_sha", "?")[:12],
                successor.get("owner", "?"),
                successor.get("condition", "?"),
            )
        )
        return {
            "issue_number": issue,
            "successor": successor,
            "reason": reason,
        }
    except Exception:
        return None


def capacity_hold_verdict(
    issue_number: object = 0,
) -> dict[str, Any]:
    """Return the machine-readable hold verdict for a held issue.

    Repair issue #176 (failed run 36637940250): a log-only hold is
    not durable evidence (see issue #165 for the retired-contract
    precedent), so the held record written by the executor carries
    this structured form: `{"held": True, "verdict":
    "held-capacity-mismatch", ...}` with the registry `successor`
    premise-change condition when the issue number names a
    `CAPACITY_MISMATCH_HOLDS` entry, else `{"held": False,
    "verdict": "", ...}` with an empty successor so unheld issues
    never misdirect. Data-driven: lifting a hold removes its registry
    entry without touching this logic. Never raises: unparsable input
    means "no hold", never a verdict failure.
    """
    try:
        issue = int(issue_number)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {
            "held": False,
            "verdict": "",
            "issue_number": 0,
            "successor": {},
        }
    try:
        entry = CAPACITY_MISMATCH_HOLDS.get(issue)
        if not entry:
            raise ValueError("not capacity-held")
        successor = entry.get("successor", {})
        successor = dict(successor) if isinstance(successor, Mapping) else {}
        return {
            "held": True,
            "verdict": HELD_CAPACITY_VERDICT,
            "issue_number": issue,
            "successor": successor,
        }
    except Exception:
        try:
            issue = int(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            issue = 0
        return {
            "held": False,
            "verdict": "",
            "issue_number": issue,
            "successor": {},
        }


def build_capacity_hold_result(
    *,
    issue_number: object = 0,
    run_id: object = "",
) -> dict[str, Any]:
    """Build a machine-readable zero-cost capacity-hold record.

    Repair issue #176 (failed run 36637940250): the executor holds a
    proven ordinary-smoke capacity mismatch before any Render service
    is created, so the attempt leaves no worker-executed job result
    behind. This helper is the single choke point for that structured
    shape, mirroring `build_exact_artifact_refusal_result`: it uses
    `status="infrastructure-blocked"` plus `permanent=True` so no
    existing `parse_job_result`/poll consumer can mistake it for a
    worker-executed job, and carries the `capacity_hold` verdict
    (see `capacity_hold_verdict`) with the registry successor so
    durable-evidence consumers never scrape log text.
    `permanent` means redispatch without the successor premise change
    holds identically -- it is a redispatch hint for future
    schedulers, not a workflow directive. Never raises: garbage input
    yields a minimal fail-closed record.
    """
    try:
        hold: dict[str, Any] = capacity_hold_verdict(issue_number)
    except Exception:
        hold = {
            "held": False,
            "verdict": "",
            "issue_number": 0,
            "successor": {},
        }
    try:
        issue = int(issue_number)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issue = 0
    try:
        run_label = "" if run_id is None else str(run_id).strip()
    except Exception:
        run_label = ""
    try:
        entry = CAPACITY_MISMATCH_HOLDS.get(issue, {})
        signature = str(entry.get("signature", "") or "")
        proving_run = str(entry.get("proving_run_id", "") or "")
    except Exception:
        signature, proving_run = "", ""
    if hold.get("held") is True:
        successor = hold.get("successor", {})
        successor = dict(successor) if isinstance(successor, Mapping) else {}
        reason = (
            "held-capacity-mismatch: ordinary-smoke issue #%d proved "
            "a %s (proving run %s); redispatching the identical "
            "payload storms identically, so this attempt held before "
            "any Render service was created with zero Render cost. "
            "Held until the successor premise lands via %s."
            % (
                issue,
                signature or "pinned-at-ceiling restart storm",
                proving_run or "?",
                successor.get("owner", "?"),
            )
        )
    else:
        successor = {}
        reason = (
            "capacity hold requested for issue #%d, which names no "
            "proven ordinary-smoke capacity mismatch; no hold applies."
            % issue
        )
    return {
        "status": "infrastructure-blocked",
        "permanent": True,
        "capacity_mismatch": bool(hold.get("held") is True),
        "reason": reason,
        "capacity_hold": hold,
        "successor": successor,
        "issue_number": issue,
        "run_id": run_label,
        "docs": {
            "render_free": RENDER_DOC_FREE_TIER,
        },
    }


def unsupported_pr15_transport_note(
    requirement: Mapping[str, Any] | None,
    binary_sha256: object = "",
) -> str:
    """Explain a refused PR #15-shaped transport (issue #146). Never raises.

    Returns "" unless the issue claims the pinned PR #15 binary digest
    with a transport triple outside ``SUPPORTED_PR15_ARTIFACTS`` (or a
    foreign repository): the case that run 36511901291 proved, where a
    runtime-lab-local artifact (``11009461073`` from run ``36511798742``)
    passed the old shape-only gate and then 404d at the controller
    fetch because artifact IDs are repo-scoped
    (``GET /repos/{owner}/{repo}/actions/artifacts/{id}/zip``) while
    delivery fetches only from ``kodmial/opencode``.
    """
    try:
        if not isinstance(requirement, Mapping):
            return ""
        raw_binary = str(binary_sha256 or "").strip().lower()
        if raw_binary != SUPPORTED_PR15_BINARY_SHA256:
            return ""
        artifact = str(requirement.get("artifact_id", "") or "").strip() or "unknown"
        run = str(requirement.get("source_run_id", "") or "").strip() or "unknown"
        return (
            " PR #15 transport note: artifact %s (workflow run %s) is not a "
            "pinned %s publish (known: %s); artifact IDs are scoped to "
            "their source repository, and exact delivery fetches only from "
            "%s (see %s), so an unpinned or foreign-repository triple "
            "cannot be fetched and is refused before any worker exists."
            % (
                artifact,
                run,
                SUPPORTED_PR15_REPO,
                ", ".join(
                    "%s/run %s" % (aid, rid)
                    for (aid, rid) in sorted(SUPPORTED_PR15_ARTIFACTS)
                ),
                SUPPORTED_PR15_REPO,
                GITHUB_ARTIFACT_DOWNLOAD_DOC,
            )
        )
    except Exception:
        return ""


def exact_workflow_artifact_blocker(requirement: Mapping[str, Any]) -> str:
    """Explain why an exact workflow artifact cannot run on Render (issue #115).

    Single choke point for the capability gap: the Render execution
    path has no delivery mechanism for GitHub Actions workflow
    artifacts (the submit-job payload carries no artifact selection,
    the worker boots with no OPENCODE_ARTIFACT_ID/SHA256 for workflow
    artifacts, and the worker holds no GitHub credential to download
    one). Substituting the baseline binary would violate the
    contract's own no-rebuild/no-substitution rule, so the only honest
    behavior is to refuse the attempt before any worker exists. When a
    delivery mechanism lands, update this function (and only this
    function) to recognize the new capability.
    """
    try:
        artifact = str(requirement.get("artifact_id", "") or "").strip() or "unknown"
        run = str(requirement.get("source_run_id", "") or "").strip() or "unknown"
        name = str(requirement.get("artifact_name", "") or "").strip()
        digest = str(requirement.get("archive_sha256", "") or "").strip()
    except Exception:
        artifact, run, name, digest = "unknown", "unknown", "", ""
    label = "artifact %s (workflow run %s)" % (artifact, run)
    if name:
        label = "%s %s (workflow run %s)" % (name, artifact, run)
    reason = (
        "infrastructure-blocked: this issue requires the exact GitHub "
        "Actions %s checksum-verified before execution with no rebuild "
        "and no binary substitution, but the Render execution path "
        "cannot deliver workflow artifacts to the ephemeral worker "
        "(no credentialed artifact fetch on the worker, no artifact "
        "selection in the submit-job payload, OpenCode child env is "
        "credential-scrubbed; see %s). Refusing to substitute the "
        "baseline binary and refusing to burn a worker on a run that "
        "cannot test what the issue asks."
        % (label, GITHUB_ARTIFACT_DOWNLOAD_DOC)
    )
    if digest:
        reason += " Expected archive digest sha256:%s." % digest
    reason += known_workflow_artifact_advisory(
        requirement if isinstance(requirement, Mapping) else None
    )
    reason += superseded_workflow_artifact_notice(
        requirement if isinstance(requirement, Mapping) else None
    )
    return reason


def build_exact_artifact_refusal_result(
    requirement: Mapping[str, Any] | None,
    *,
    issue_number: object = 0,
    run_id: object = "",
) -> dict[str, Any]:
    """Build a machine-readable pre-creation refusal record (issue #125).

    Run 36500759174 is the third live seconds-fast zero-cost refusal of
    the same #110 exact-artifact contract (after runs 36498921649 and
    36499977510): the human-readable ``infrastructure-blocked`` log line
    is correct, but the attempt leaves no structured result behind, so
    every future triage/scheduler must scrape log text to tell this
    permanent block apart from a transient failure. This helper is the
    single choke point for that structured shape: ``render-job.sh``
    writes its output to ``$RENDER_RESULT_FILE`` on the gate path while
    still creating no Render service.

    The record is deliberately distinct from runner job results
    (``succeeded``/``failed``/``timed_out`` with ``job_id``): it uses
    ``status="infrastructure-blocked"`` plus ``permanent=True`` so no
    existing ``parse_job_result``/poll consumer can mistake it for a
    worker-executed job. ``permanent`` means redispatch without a
    material premise change (a real artifact-delivery mechanism plus a
    version-stamped rebuild) will refuse identically -- it is a
    redispatch hint for future schedulers, not a workflow directive.
    `dispatch_hold` carries the same retired-contract hold the executor
    names with the `held-superseded` log verdict (issue #165), so
    durable evidence consumers never scrape log text. Never raises:
    garbage input yields a minimal fail-closed record.
    """
    try:
        reason = exact_workflow_artifact_blocker(
            requirement if isinstance(requirement, Mapping) else {}
        )
    except Exception:
        reason = "infrastructure-blocked: exact workflow artifact cannot run on Render."
    try:
        mapping = requirement if isinstance(requirement, Mapping) else {}
        artifact = str(mapping.get("artifact_id", "") or "").strip()
        run = str(mapping.get("source_run_id", "") or "").strip()
        name = str(mapping.get("artifact_name", "") or "").strip()
        digest = str(mapping.get("archive_sha256", "") or "").strip().lower()
        source_sha = str(mapping.get("source_sha", "") or "").strip().lower()
    except Exception:
        artifact, run, name, digest, source_sha = "", "", "", "", ""
    try:
        advisory = known_workflow_artifact_advisory(
            requirement if isinstance(requirement, Mapping) else None
        )
    except Exception:
        advisory = ""
    try:
        notice = superseded_workflow_artifact_notice(
            requirement if isinstance(requirement, Mapping) else None
        )
    except Exception:
        notice = ""
    try:
        successor: dict[str, Any] = {}
        if isinstance(requirement, Mapping):
            artifact_key = str(requirement.get("artifact_id", "") or "").strip()
            run_key = str(requirement.get("source_run_id", "") or "").strip()
            entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact_key, run_key))
            if entry:
                successor = dict(entry)
    except Exception:
        successor = {}
    try:
        hold: dict[str, Any] = superseded_hold_verdict(
            requirement if isinstance(requirement, Mapping) else None
        )
    except Exception:
        hold = {
            "held": False,
            "verdict": "",
            "artifact_id": "",
            "source_run_id": "",
            "successor": {},
        }
    try:
        issue = int(issue_number)
    except (TypeError, ValueError):
        issue = 0
    try:
        run_label = "" if run_id is None else str(run_id).strip()
    except Exception:
        run_label = ""
    return {
        "status": "infrastructure-blocked",
        "permanent": True,
        "reason": reason,
        "artifact_id": artifact,
        "artifact_name": name,
        "source_run_id": run,
        "archive_sha256": digest,
        "source_sha": source_sha,
        "has_known_advisory": bool(advisory),
        "superseded": bool(notice),
        "successor": successor,
        "dispatch_hold": hold,
        "issue_number": issue,
        "run_id": run_label,
        "docs": {
            "github_artifacts": GITHUB_ARTIFACT_DOWNLOAD_DOC,
            "render_free": RENDER_DOC_FREE_TIER,
        },
    }


# ---------------------------------------------------------------------------
# Low-burn / rate-limit policy (encoded, not an operational note).
# ---------------------------------------------------------------------------

# At most four automation jobs may be active concurrently across issues.
MAX_CONCURRENT_AUTOMATION_JOBS = 4
# Each issue attempt owns at most one ephemeral Render service.
MAX_SERVICES_PER_ISSUE_ATTEMPT = 1
# Exactly one service creation per issue execution attempt; polling/deploy
# retries reuse the same created service and never create a new one.
MAX_SERVICE_CREATIONS_PER_ATTEMPT = 1
# A retry of polling/deployment reuses the same service id.
POLL_RETRY_CREATES_NEW_SERVICE = False
# No cron job may create Render services merely for health checking.
CRON_MAY_CREATE_SERVICES = False
# Bounded retries everywhere (no unbounded loops).
DEPLOY_POLL_MAX_ATTEMPTS = 60
DEPLOY_POLL_INTERVAL_SECONDS = 20
# Job polling must cover the full runner execution timeout
# (RUNNER_JOB_TIMEOUT_SECONDS = 45 minutes) plus poll overhead, while still
# fitting inside the 55-minute workflow envelope when deploy/health are fast
# (run 36399649036 failed after only 60*20s=1200s while the runner may
# legitimately work for up to 2700s): 140*20s=2800s covers 2700s + buffer.
JOB_POLL_MAX_ATTEMPTS = 140
JOB_POLL_INTERVAL_SECONDS = 20
# Job-loss / transport-error fail-fast policy for the poll loop.
#
# Run 36402447309 polled an empty job status for the full 140x20s budget
# because the shell treated every unparsable poll (curl failure, HTTP 4xx/5xx
# collapsed by curl -f, empty body) as "still working". Runner jobs live in
# worker process memory, so a worker restart turns the polled job id into a
# permanent 404 ("unknown job") that can never become terminal: waiting out
# the full budget only masks the cause. The controller therefore fails fast
# after this many consecutive unknown-job polls, and re-probes /health after
# every this-many consecutive transport failures.
#
# Run 36430429432 then proved a single failed /health probe after only five
# consecutive transport errors (HTTP 502) is too hair-trigger to be
# terminal: a Free restart/OOM-replacement produces exactly that transient
# signature (Render proxy 502s while the old process is dead plus /health
# failing while the replacement boots, ~60s spin-up per RENDER_DOC_FREE_TIER)
# but the worker usually answers again well inside the unchanged 140-iteration
# budget. The controller therefore fails fast on transport errors only after
# this many CONSECUTIVE failed /health probes; a single failed probe is
# retried within budget, and any healthy probe (or any poll that reaches the
# worker again) resets the streak.
JOB_POLL_UNKNOWN_JOB_THRESHOLD = 3
JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY = 5
JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES = 3
# Same-worker job resubmission policy for proven job loss.
#
# Run 36409152332 submitted a job that polled as pending for ~2 minutes
# and then turned into a permanent unknown-job 404 while the runner
# stayed healthy: Render may restart a Free web service at any time
# (RENDER_DOC_FREE_TIER) and runner jobs live only in worker process
# memory, so a restart wipes the submitted job id forever. The submit
# payload is fully reproducible, so the controller resubmits it on the
# SAME worker (never a second Render service).
#
# Repairs for runs 36417263684, 36422228148, 36425019190, and 36430429432
# grew a fixed resubmission bound one live failure at a time (1 -> 2 ->
# 3 -> 4 -> 5), and run 36434278632 then lost the original job plus all
# five resubmissions to six consecutive proven restarts (instances
# 7126f50f2488 -> 052597e76e74 -> 89d9f277a377 -> f6d90020ee91 ->
# 1e9d729fe0bf -> fb20ff4cd8af -> 7d6acf859f67 at ~3-minute intervals,
# ~18 minutes wall-clock, well inside the unchanged 140x20s poll
# budget), failing fast with "resubmissions used: 5/5". A fixed count is
# the wrong shape for this failure mode: any fixed N is falsified by the
# next (N+1)-restart cluster while poll budget demonstrably remains, so
# the bound must not be incremented a sixth time. Resubmission is
# therefore budget-limited, not count-limited: the controller keeps
# resubmitting the reproducible payload on the same healthy worker while
# poll iterations remain (see should_resubmit_after_job_loss), and stops
# only when the budget is exhausted or the loss is proven deterministic
# (same worker process still healthy but no longer knows the job it
# accepted: an unknown-job 404 proves the poll reached the worker, so a
# loss on the SAME process is a deterministic defect that resubmission
# cannot recover, and failing fast there is also faster than the old
# count-bound behavior). Every existing invariant holds (one service per
# attempt, same-worker policy, unchanged 140-iteration budget, mandatory
# verified cleanup).
# Poll outcome vocabulary for one job-status attempt (controller side).
JOB_POLL_OUTCOMES = frozenset({
    "succeeded",
    "failed",
    "timed_out",
    "pending",
    "unknown_job",
    "transport_error",
    "unknown_status",
})
# Wall-clock skew tolerance for restart detection (issue #41): when both
# wall-clock readings are available, the elapsed wall time must exceed the
# uptime delta by more than this many seconds to prove a replacement
# process. 60s absorbs poll/health timing jitter while remaining far
# below the multi-minute drift seen in run 36410676408 (minutes elapsed
# with only ~23s/~11s uptime deltas).
WORKER_RESTART_WALL_SKEW_TOLERANCE_SECONDS = 60
RUNNER_HEALTH_MAX_ATTEMPTS = 30
RUNNER_HEALTH_INTERVAL_SECONDS = 10
DELETE_MAX_ATTEMPTS = 5
DELETE_RETRY_INTERVAL_SECONDS = 10
# Suspension is only an emergency fallback if deletion temporarily fails.
SUSPEND_IS_PRIMARY_CLEANUP = False
SUSPEND_FALLBACK_MAX_ATTEMPTS = 2
# After a fallback suspend, deletion must still be retried within these bounds.
POST_SUSPEND_DELETE_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class BurnBudget:
    """Testable encoding of the low-burn policy for one execution attempt."""

    max_concurrent_jobs: int = MAX_CONCURRENT_AUTOMATION_JOBS
    max_services_per_attempt: int = MAX_SERVICES_PER_ISSUE_ATTEMPT
    max_creations_per_attempt: int = MAX_SERVICE_CREATIONS_PER_ATTEMPT
    poll_retry_creates_service: bool = POLL_RETRY_CREATES_NEW_SERVICE
    cron_may_create_services: bool = CRON_MAY_CREATE_SERVICES
    delete_max_attempts: int = DELETE_MAX_ATTEMPTS

    def check_attempt(self, creations: int, services_owned: int) -> None:
        if creations > self.max_creations_per_attempt:
            raise ValueError(
                "attempt created %d services; at most %d allowed"
                % (creations, self.max_creations_per_attempt)
            )
        if services_owned > self.max_services_per_attempt:
            raise ValueError(
                "attempt owns %d services; at most %d allowed"
                % (services_owned, self.max_services_per_attempt)
            )


DEFAULT_BURN_BUDGET = BurnBudget()


# ---------------------------------------------------------------------------
# Render source / GitHub connection strategy.
# ---------------------------------------------------------------------------

PUBLIC_REPO_URL = "https://github.com/kodmial/runtime-lab"
PUBLIC_REPO_BRANCH = "main"
# autoDeploy=no so Git pushes do not trigger extra deployments.
REQUIRED_AUTO_DEPLOY = "no"
REQUIRED_SERVICE_TYPE = "web_service"


def build_create_service_payload(
    *,
    name: str,
    owner_id: str,
    region: str = DEFAULT_WORKER_REGION,
    repo: str = PUBLIC_REPO_URL,
    branch: str = PUBLIC_REPO_BRANCH,
    runtime: str = "python",
    build_command: str = "pip install -r requirements.txt && bash automation/install-opencode.sh",
    start_command: str = "RUNNER_ALLOW_RUNTIME_INSTALL=0 python -m automation.runner_server",
    health_check_path: str = "/health",
    opencode_artifact_id: str | None = None,
    opencode_artifact_sha256: str | None = None,
    opencode_artifact_ref: str | None = None,
    exact_artifact: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the POST /v1/services body for an ephemeral free-tier worker.

    Uses the public Git URL explicitly with autoDeploy=no, so no
    Render<->GitHub provider connection is required for this phase. The
    build step installs the OpenCode CLI with the known-good NanoDictate
    pattern pinned to an explicit release (``curl -fsSL
    https://opencode.ai/install | bash -s -- --version <pinned>`` via
    automation/install-opencode.sh; the pin skips the installer's
    unauthenticated api.github.com lookup) and copies the binary into the
    deterministic deploy artifact ``.opencode-bin/opencode`` so runtime
    resolution never depends on build-time $HOME equaling runtime $HOME.
    The start command disables runtime network installation
    (``RUNNER_ALLOW_RUNTIME_INSTALL=0``): a worker whose deploy artifact
    lacks an executable OpenCode binary fails readiness (/health 503)
    instead of running curl|bash inside the job (issue #52).

    Experiment artifacts (issue #86): pass ``opencode_artifact_id`` plus
    ``opencode_artifact_sha256`` (and optionally ``opencode_artifact_ref``)
    to select one exact prebuilt fork artifact. The ids are exported on the
    start command so the runner resolves only
    ``.opencode-artifacts/<artifact-id>/opencode`` with a matching SHA-256
    and fails readiness otherwise -- never the upstream baseline and never
    a network installer. Omitting all three keeps the pinned-baseline
    payload unchanged.

    Exact workflow artifacts (issue #128): pass ``exact_artifact`` (the
    machine-readable identity from ``supported_exact_artifact_identity``)
    to select the corrected GitHub Actions artifact ``11004835952``. The
    identity is exported on the start command as
    ``OPENCODE_EXACT_*`` bindings so the worker boots with the expected
    checksums and rejects missing/mismatched bytes before starting
    OpenCode. Combining an issue-#86 selection with an exact selection is
    rejected fail-closed (one worker, one binary story).
    """
    if not name:
        raise ValueError("service name must not be empty")
    if not owner_id:
        raise ValueError("owner_id (Render workspace id) must not be empty")
    validate_worker_region(region)
    if repo != PUBLIC_REPO_URL:
        raise ValueError(
            "development phase must deploy from %r, got %r"
            % (PUBLIC_REPO_URL, repo)
        )
    if exact_artifact is not None and (
        opencode_artifact_id or opencode_artifact_sha256 or opencode_artifact_ref
    ):
        raise ValueError(
            "cannot combine an issue-86 experiment artifact with an exact "
            "workflow artifact on one worker"
        )
    start = _start_command_with_artifact(
        start_command,
        artifact_id=opencode_artifact_id,
        artifact_sha256=opencode_artifact_sha256,
        artifact_ref=opencode_artifact_ref,
    )
    start = _start_command_with_exact_artifact(start, exact_artifact=exact_artifact)
    return {
        "type": REQUIRED_SERVICE_TYPE,
        "name": name,
        "ownerId": owner_id,
        "repo": repo,
        "branch": branch,
        "autoDeploy": REQUIRED_AUTO_DEPLOY,
        "serviceDetails": {
            "runtime": runtime,
            "region": region,
            "plan": FREE_PLAN,
            "numInstances": 1,
            "healthCheckPath": health_check_path,
            "envSpecificDetails": {
                "buildCommand": build_command,
                "startCommand": start,
            },
        },
    }


_HEX_DIGITS = frozenset("0123456789abcdef")


def _start_command_with_artifact(
    start_command: str,
    *,
    artifact_id: str | None,
    artifact_sha256: str | None,
    artifact_ref: str | None,
) -> str:
    """Prefix a start command with explicit artifact selection (issue #86).

    Returns ``start_command`` unchanged when no artifact field is set
    (upstream-baseline mode). Fails closed on partial selection: an
    artifact id without its SHA-256 (or vice versa) is a configuration
    error, never a silent baseline.
    """
    raw_id = (artifact_id or "").strip()
    raw_sha = (artifact_sha256 or "").strip().lower()
    raw_ref = (artifact_ref or "").strip()
    if not raw_id and not raw_sha and not raw_ref:
        return start_command
    if not raw_id or not raw_sha:
        raise ValueError(
            "partial artifact selection (id=%r sha=%r): an experiment worker "
            "must set both OPENCODE_ARTIFACT_ID and OPENCODE_ARTIFACT_SHA256" % (raw_id, raw_sha)
        )
    if not raw_id.startswith("opencode-") or "/" in raw_id:
        raise ValueError("invalid OPENCODE_ARTIFACT_ID: %r" % raw_id)
    if len(raw_sha) != 64 or any(char not in _HEX_DIGITS for char in raw_sha):
        raise ValueError("invalid OPENCODE_ARTIFACT_SHA256: must be 64 lowercase hex chars")
    if raw_ref and "latest" in raw_ref.lower():
        raise ValueError("artifact ref must never use 'latest'")
    if not isinstance(start_command, str) or not start_command.strip():
        raise ValueError("start_command must be a non-empty string")
    prefix = "OPENCODE_ARTIFACT_ID=%s OPENCODE_ARTIFACT_SHA256=%s" % (raw_id, raw_sha)
    if raw_ref:
        prefix += " OPENCODE_ARTIFACT_REF=%s" % raw_ref
    return "%s %s" % (prefix, start_command.strip())


def _start_command_with_exact_artifact(
    start_command: str,
    *,
    exact_artifact: Mapping[str, Any] | None,
) -> str:
    """Prefix a start command with exact workflow-artifact selection (#128).

    Returns ``start_command`` unchanged when ``exact_artifact`` is None
    (ordinary/baseline mode). Validates the full supported identity via
    ``automation/exact_artifact_delivery.py`` and exports the
    ``OPENCODE_EXACT_*`` bindings; fails closed on any partial or
    unsupported identity, never a silent baseline.
    """
    if exact_artifact is None:
        return start_command
    if not isinstance(start_command, str) or not start_command.strip():
        raise ValueError("start_command must be a non-empty string")
    if not isinstance(exact_artifact, Mapping):
        raise ValueError("exact_artifact must be a mapping")
    try:
        try:
            from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
                validate_exact_identity as _validate,
            )
        except ImportError:
            from exact_artifact_delivery import (  # type: ignore[no-redef]
                validate_exact_identity as _validate,
            )
        data = _validate(dict(exact_artifact))
    except ImportError:
        data = {
            "artifact_id": str(exact_artifact.get("artifact_id", "") or "").strip(),
            "artifact_name": str(exact_artifact.get("artifact_name", "") or "").strip(),
            "source_run_id": str(exact_artifact.get("source_run_id", "") or "").strip(),
            "archive_sha256": str(exact_artifact.get("archive_sha256", "") or "").strip().lower(),
            "binary_sha256": str(exact_artifact.get("binary_sha256", "") or "").strip().lower(),
            "version": str(exact_artifact.get("version", "") or "").strip(),
        }
        pr12_ok = (
            data["artifact_id"] == SUPPORTED_EXACT_ARTIFACT_ID
            and data["source_run_id"] == SUPPORTED_EXACT_SOURCE_RUN_ID
            and data["archive_sha256"] == SUPPORTED_EXACT_ARCHIVE_SHA256
            and data["binary_sha256"] == SUPPORTED_EXACT_BINARY_SHA256
            and data["version"] == SUPPORTED_EXACT_VERSION
        )
        pr15_ok = (
            data["binary_sha256"] == SUPPORTED_PR15_BINARY_SHA256
            and data["version"] == SUPPORTED_PR15_VERSION
            and data["artifact_name"] == SUPPORTED_PR15_ARTIFACT_NAME
            and _is_numeric_id(data["artifact_id"])
            and _is_numeric_id(data["source_run_id"])
            and len(data["archive_sha256"]) == 64
        )
        if not (pr12_ok or pr15_ok):
            raise ValueError("unsupported exact_artifact identity")
    prefix = (
        "OPENCODE_EXACT_ARTIFACT_ID=%s OPENCODE_EXACT_ARTIFACT_SHA256=%s "
        "OPENCODE_EXACT_ARCHIVE_SHA256=%s OPENCODE_EXACT_SOURCE_RUN=%s "
        "OPENCODE_EXACT_VERSION=%s OPENCODE_EXACT_ARTIFACT_NAME=%s"
        % (
            data["artifact_id"],
            data["binary_sha256"],
            data["archive_sha256"],
            data["source_run_id"],
            data["version"],
            data["artifact_name"],
        )
    )
    return "%s %s" % (prefix, start_command.strip())


# ---------------------------------------------------------------------------
# Deploy / service state classification.
# ---------------------------------------------------------------------------

DEPLOY_LIVE_STATUSES = frozenset({"live"})
DEPLOY_FAILED_STATUSES = frozenset(
    {"build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"}
)
DEPLOY_IN_PROGRESS_STATUSES = frozenset(
    {
        "created",
        "queued",
        "build_in_progress",
        "update_in_progress",
        "pre_deploy_in_progress",
    }
)


def classify_deploy_status(status: str) -> str:
    """Classify a Render deploy status as live/failed/in_progress."""
    if status in DEPLOY_LIVE_STATUSES:
        return "live"
    if status in DEPLOY_FAILED_STATUSES:
        return "failed"
    if status in DEPLOY_IN_PROGRESS_STATUSES:
        return "in_progress"
    raise ValueError("unknown Render deploy status: %r" % status)


def is_deletion_verified(http_status: int) -> bool:
    """True when a retrieve/list status proves the service no longer exists."""
    return http_status in RENDER_DELETION_VERIFIED_STATUSES


# ---------------------------------------------------------------------------
# Runner HTTP contract (GitHub Actions <-> Render-hosted runner).
# ---------------------------------------------------------------------------

RUNNER_HEALTH_PATH = "/health"
RUNNER_HEALTH_METHOD = "GET"
RUNNER_SUBMIT_JOB_METHOD = "POST"
RUNNER_SUBMIT_JOB_PATH = "/v1/jobs"
RUNNER_JOB_STATUS_METHOD = "GET"
RUNNER_JOB_STATUS_PATH_TEMPLATE = "/v1/jobs/{jobId}"

RUNNER_REQUEST_TIMEOUT_SECONDS = 30
RUNNER_JOB_TIMEOUT_SECONDS = 45 * 60
RUNNER_RESULT_TIMEOUT_SECONDS = 60

RUNNER_JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "timed_out"})
RUNNER_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out"})


@dataclass(frozen=True)
class ExecutionMetadata:
    """Non-secret execution metadata recorded in logs and payloads."""

    issue_number: int
    attempt: int = 1
    run_id: str = ""
    region: str = DEFAULT_WORKER_REGION
    model: str = PREFERRED_MODEL
    execution_mode: str = "e2e"

    def __post_init__(self) -> None:
        if not isinstance(self.issue_number, int) or self.issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if self.attempt <= 0:
            raise ValueError("attempt must be a positive integer")
        validate_worker_region(self.region)
        if self.model not in (PREFERRED_MODEL, FALLBACK_MODEL):
            raise ValueError("unknown model: %r" % self.model)
        if self.execution_mode not in ("smoke", "e2e"):
            raise ValueError("execution_mode must be smoke or e2e")


@dataclass(frozen=True)
class JobRequest:
    """GitHub -> runner submit-job request (minimum job payload)."""

    repository_url: str = PUBLIC_REPO_URL
    base_ref: str = PUBLIC_REPO_BRANCH
    base_sha: str = ""
    task_text: str = ""
    issue_number: int = 0
    metadata: ExecutionMetadata | None = None
    # Cross-repository execution (issue #85): ``repository_url`` is the
    # *target* checkout (self-target ``kodmial/runtime-lab`` by default,
    # or the allow-listed ``kodmial/opencode`` when the source issue
    # declares ``runtime-lab-target``). ``source_repository`` records the
    # tracking repo that owns the issue lifecycle; ``issue_number``
    # always names the Runtime Lab source issue.
    source_repository: str = PUBLIC_REPO_URL
    target_repository: str = ""
    # Exact workflow artifact (issue #128): machine-readable identity for
    # the supported corrected artifact (artifact_id + source_run_id +
    # archive/binary digests + version). None means ordinary/baseline mode.
    # When present it is validated as the full supported conjunction and
    # carried verbatim in the submit-job body so the worker can verify
    # before starting OpenCode.
    exact_artifact: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        try:  # pragma: no cover - import path depends on entrypoint
            from automation.cross_repo import (
                SOURCE_REPO_URL as _SOURCE_URL,
            )
            from automation.cross_repo import (
                normalize_clone_url as _normalize_url,
            )
        except ImportError:
            try:
                from cross_repo import (  # type: ignore[no-redef]
                    SOURCE_REPO_URL as _SOURCE_URL,
                )
                from cross_repo import (  # type: ignore[no-redef]
                    normalize_clone_url as _normalize_url,
                )
            except ImportError:
                _SOURCE_URL = PUBLIC_REPO_URL
                _normalize_url = None  # type: ignore[assignment]
        allowed_url = False
        if self.repository_url == PUBLIC_REPO_URL:
            allowed_url = True
        elif _normalize_url is not None:
            try:
                _normalize_url(self.repository_url)
                allowed_url = True
            except ValueError:
                allowed_url = False
        if not allowed_url:
            raise ValueError(
                "repository_url %r is not an allow-listed execution target"
                % (self.repository_url,)
            )
        if self.target_repository:
            if _normalize_url is not None:
                try:
                    expected_full = _normalize_url(self.repository_url)
                except ValueError as exc:
                    raise ValueError(str(exc)) from None
                if self.target_repository.strip().lower() != expected_full.lower():
                    raise ValueError(
                        "target_repository %r does not match repository_url %r"
                        % (self.target_repository, self.repository_url)
                    )
            elif self.target_repository not in ("kodmial/runtime-lab",
                                                "kodmial/opencode"):
                raise ValueError(
                    "target_repository %r is not allow-listed"
                    % (self.target_repository,)
                )
        if self.source_repository and self.source_repository != _SOURCE_URL:
            raise ValueError(
                "source_repository must be %r in this phase" % _SOURCE_URL
            )
        if not self.base_ref:
            raise ValueError("base_ref must not be empty")
        if not self.task_text.strip():
            raise ValueError("task_text must not be empty")
        if not isinstance(self.issue_number, int) or self.issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if self.metadata is not None and self.metadata.issue_number != self.issue_number:
            raise ValueError("metadata.issue_number must match issue_number")
        if self.exact_artifact is not None:
            if not isinstance(self.exact_artifact, Mapping):
                raise ValueError("exact_artifact must be a mapping")
            try:
                try:
                    from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
                        validate_exact_identity as _validate_exact,
                    )
                except ImportError:
                    from exact_artifact_delivery import (  # type: ignore[no-redef]
                        validate_exact_identity as _validate_exact,
                    )
                _validate_exact(dict(self.exact_artifact))
            except ImportError:
                fields = {
                    str(k): str(v or "") for k, v in dict(self.exact_artifact).items()
                }
                pr12 = (
                    fields.get("artifact_id", "").strip()
                    == SUPPORTED_EXACT_ARTIFACT_ID
                    and fields.get("source_run_id", "").strip()
                    == SUPPORTED_EXACT_SOURCE_RUN_ID
                    and fields.get("archive_sha256", "").strip().lower()
                    == SUPPORTED_EXACT_ARCHIVE_SHA256
                    and fields.get("binary_sha256", "").strip().lower()
                    == SUPPORTED_EXACT_BINARY_SHA256
                    and fields.get("version", "").strip() == SUPPORTED_EXACT_VERSION
                )
                pr15 = (
                    fields.get("binary_sha256", "").strip().lower()
                    == SUPPORTED_PR15_BINARY_SHA256
                    and fields.get("version", "").strip() == SUPPORTED_PR15_VERSION
                    and fields.get("artifact_name", "").strip()
                    == SUPPORTED_PR15_ARTIFACT_NAME
                    and _is_numeric_id(fields.get("artifact_id", ""))
                    and _is_numeric_id(fields.get("source_run_id", ""))
                    and len(fields.get("archive_sha256", "").strip()) == 64
                )
                if not (pr12 or pr15):
                    raise ValueError(
                        "exact_artifact must be a supported PR #12 or PR #15 identity"
                    )

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "repository_url": self.repository_url,
            "base_ref": self.base_ref,
            "task_text": self.task_text,
            "issue_number": self.issue_number,
            "metadata": {
                "issue_number": self.issue_number,
                "attempt": self.metadata.attempt if self.metadata else 1,
                "run_id": self.metadata.run_id if self.metadata else "",
                "region": self.metadata.region if self.metadata else DEFAULT_WORKER_REGION,
                "model": self.metadata.model if self.metadata else PREFERRED_MODEL,
                "execution_mode": (
                    self.metadata.execution_mode if self.metadata else "e2e"
                ),
            },
        }
        if self.base_sha:
            body["base_sha"] = self.base_sha
        # Cross-repo correlation (issue #85): the tracking repo/issue that
        # owns the lifecycle plus the explicit target repo. Older workers
        # ignore the extra keys; newer workers echo them in the result.
        body["source_repository"] = self.source_repository or PUBLIC_REPO_URL
        if self.target_repository:
            body["target_repository"] = self.target_repository
        # Exact-artifact selection (issues #128 + #140): machine-readable
        # identity the worker verifies before starting OpenCode. Older
        # workers ignore the extra key; exact-aware workers fail closed
        # on missing/mismatched bytes. The validated identity is echoed
        # verbatim so both the PR #12 and PR #15 contracts flow through.
        if self.exact_artifact is not None:
            try:
                try:
                    from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
                        validate_exact_identity as _validate_exact2,
                    )
                except ImportError:
                    from exact_artifact_delivery import (  # type: ignore[no-redef]
                        validate_exact_identity as _validate_exact2,
                    )
                validated = _validate_exact2(dict(self.exact_artifact))
            except ImportError:
                validated = dict(self.exact_artifact)
            body["exact_artifact"] = {
                "artifact_id": str(validated.get("artifact_id", "") or "").strip(),
                "artifact_name": str(
                    validated.get("artifact_name", "") or SUPPORTED_EXACT_ARTIFACT_NAME
                ).strip(),
                "source_run_id": str(validated.get("source_run_id", "") or "").strip(),
                "archive_sha256": str(
                    validated.get("archive_sha256", "") or ""
                ).strip().lower(),
                "binary_sha256": str(
                    validated.get("binary_sha256", "") or ""
                ).strip().lower(),
                "version": str(validated.get("version", "") or "").strip(),
            }
        return body


@dataclass(frozen=True)
class JobResult:
    """Runner -> GitHub job status/result payload."""

    job_id: str
    status: str
    success: bool
    summary: str = ""
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id must not be empty")
        if self.status not in RUNNER_JOB_STATUSES:
            raise ValueError("unknown job status: %r" % self.status)
        if self.status in RUNNER_TERMINAL_STATUSES and not self.summary and not self.error:
            raise ValueError("terminal results must carry a summary or error")
        if self.status == "succeeded" and not self.success:
            raise ValueError("succeeded status requires success=True")
        if self.status in ("failed", "timed_out") and self.success:
            raise ValueError("%s status requires success=False" % self.status)


def is_terminal_job_status(status: str) -> bool:
    return status in RUNNER_TERMINAL_STATUSES


def classify_job_poll_response(http_status: int | None,
                               status_value: object) -> str:
    """Classify one runner job-poll attempt into a poll outcome.

    ``http_status`` is the HTTP status of ``GET /v1/jobs/{jobId}`` (None/0
    when the request never completed). ``status_value`` is the parsed
    ``status`` field of the response body ("" when there was no parseable
    body). Returns one of ``JOB_POLL_OUTCOMES``:

    - ``succeeded`` / ``failed`` / ``timed_out``: terminal runner states.
    - ``pending``: the job is still working (queued/running on HTTP 2xx).
    - ``unknown_job``: HTTP 404/410, the runner's documented "unknown job"
      answer. The job id will never become terminal (e.g. worker restart
      lost the in-memory job); the caller must fail fast, not keep polling.
    - ``transport_error``: no usable answer (connection failure, 429/5xx, or
      HTTP 2xx with a missing/empty status). Retryable within bounds, but
      the caller must track consecutive occurrences and re-probe /health
      instead of treating them as proof the job is still working.
    - ``unknown_status``: HTTP 2xx with an unrecognized status string.
    """
    code: int | None
    try:
        code = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 404 or code == 410:
        return "unknown_job"
    if code is None or code == 0:
        return "transport_error"
    if code == 429 or 500 <= code <= 599:
        return "transport_error"
    if 200 <= code <= 299:
        status = status_value.strip() if isinstance(status_value, str) else ""
        if status in RUNNER_TERMINAL_STATUSES:
            return status
        if status in ("queued", "running"):
            return "pending"
        if not status:
            return "transport_error"
        return "unknown_status"
    if 400 <= code <= 499:
        # Other 4xx on a poll (e.g. malformed job id shape) will never
        # resolve into a terminal job state either.
        return "unknown_job"
    return "transport_error"


def should_fail_fast_on_unknown_job(consecutive_unknown: int) -> bool:
    """True once consecutive unknown-job polls prove the job is gone."""
    try:
        count = int(consecutive_unknown)
    except (TypeError, ValueError):
        return False
    return count >= JOB_POLL_UNKNOWN_JOB_THRESHOLD


def should_probe_runner_health(consecutive_transport_errors: int) -> bool:
    """True every Nth consecutive transport failure (health re-probe point)."""
    try:
        count = int(consecutive_transport_errors)
    except (TypeError, ValueError):
        return False
    return count > 0 and count % JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY == 0


def should_fail_fast_on_transport(consecutive_unhealthy_probes: int) -> bool:
    """True once consecutive failed /health probes prove the worker is down.

    A single failed probe after a handful of transport errors is only proof
    of a transient down-window (run 36430429432: five HTTP 502 polls with
    one failed probe while a Free replacement booted); sustained failed
    probes across JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES re-probe points
    (default 3 x 5 = 15 consecutive transport errors, ~5 minutes) prove
    the worker is not coming back within budget.
    """
    try:
        count = int(consecutive_unhealthy_probes)
    except (TypeError, ValueError):
        return False
    return count >= JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES


def should_resubmit_after_job_loss(*, polls_remaining: object,
                                   worker_restarted: object) -> bool:
    """Budget-limited same-worker resubmission decision (run 36434278632).

    ``polls_remaining`` is the number of job-poll iterations left in the
    unchanged poll budget; ``worker_restarted`` is the
    detect_worker_restart() verdict for the loss (True = proven restart,
    False = proven same process, None = missing evidence). Returns True
    only while another attempt can still be observed (budget remains)
    and the loss is not proven deterministic: a proven same-process
    loss fails fast even with budget left (an unknown-job 404 proves the
    poll reached the worker, so the same process losing its own job is a
    deterministic defect resubmission cannot recover), while missing
    evidence fails open toward recovery inside the budget. Unparsable
    budgets fail closed.
    """
    if worker_restarted is False:
        return False
    try:
        remaining = int(polls_remaining)
    except (TypeError, ValueError):
        return False
    return remaining > 0


# Memory-pressure restart-storm circuit breaker (run 36439192645).
#
# Run 36439192645 lost seven consecutive jobs to seven proven worker
# restarts (~3-minute intervals, instance da61 -> f96d -> 4c14 -> 15e5
# -> 1d8e -> 4e9b -> 25b7 -> b9b4) and resubmitted every one under the
# budget-limited policy above; the eighth job then stayed `running` on
# the healthy worker for 24+ minutes until the shared 140-poll budget
# was exhausted ("did not finish in time (last status 'running')").
# Telemetry proves the driver is systematic, not transient: cgroup
# usage pinned at the 512 MB limit (current/peak 512.0/512.5 MB),
# memory.events max stalls +430,890, eight instance ids. The ~600-615
# MB agent peak (issue #52, irreducible per issue #56) does not fit the
# 512 MB Free worker (RENDER_DOC_FREE_TIER: 0.1 CPU / 512 MB, anytime
# restart, ephemeral filesystem), so each resubmission restarts the
# same oversized workload and guarantees the next restart: zero
# forward progress per attempt while every resubmission also shrinks
# the next attempt's execution window below the runner's 45-minute
# timeout (shared-budget starvation).
#
# The breaker therefore abandons resubmission once a streak of
# CONSECUTIVE proven-restart losses reaches this threshold AND live
# container telemetry proves memory pressure. The pressure gate is
# what keeps this from becoming another fixed-count treadmill (runs
# 36417263684 ... 36434278632 grew 1 -> 2 -> 3 -> 4 -> 5 -> unlimited):
# a restart cluster WITHOUT pressure evidence (genuine transient host
# maintenance) still gets full budget-limited recovery, while a
# pressure-proven storm fails fast with an actionable diagnostic after
# ~10 minutes of zero progress instead of burning the whole 45-minute
# budget. Pending polls between losses do not reset the streak (they
# are the doomed attempt running, not forward progress); a model
# fallback submit resets it (different model, changed premise).
JOB_POLL_RESTART_STORM_THRESHOLD = 3


def should_abandon_restart_storm(*, consecutive_restart_losses: object,
                                  memory_pressure: object,
                                  threshold: object = None) -> bool:
    """True when resubmission is a proven-doomed storm (run 36439192645).

    Abandons only when BOTH hold: ``consecutive_restart_losses``
    (proven-restart resubmissions with no intervening model fallback)
    reaches the storm threshold (``JOB_POLL_RESTART_STORM_THRESHOLD``
    by default; an explicit ``threshold`` overrides it so the shell
    poll loop and this decision cannot diverge -- issue #113 found the
    shell passing its resolved threshold into the storm snippet while
    the snippet ignored it), and ``memory_pressure``
    (live telemetry verdict, see render_memory_sampler
    .detect_memory_pressure) is truthy. Anything else fails open
    toward the pre-existing budget-limited recovery: missing pressure
    evidence (telemetry gaps) never abandons, unparsable streaks never
    abandon, and an unparsable explicit threshold never abandons.
    """
    if not memory_pressure:
        return False
    if threshold is None:
        required = JOB_POLL_RESTART_STORM_THRESHOLD
    else:
        try:
            required = int(threshold)
        except (TypeError, ValueError):
            return False
    try:
        streak = int(consecutive_restart_losses)
    except (TypeError, ValueError):
        return False
    return streak >= required


def detect_worker_restart(prior_uptime: object,
                           current_uptime: object,
                           prior_instance_id: object = None,
                           current_instance_id: object = None,
                           prior_wall_seconds: object = None,
                           current_wall_seconds: object = None,
                           skew_tolerance_seconds: object = None) -> bool | None:
    """Compare runner /health readings across a job loss (issue #41).

    A changed non-empty process instance id proves a restart/replacement
    directly, even when the new uptime is numerically greater than the old
    snapshot (run 36410676408: wall-clock advanced several minutes while
    uptime moved only 25.9 -> 48.9 -> 60.3, so the legacy
    ``current < prior`` check wrongly reported "same worker process
    lifetime"). Never infer identity from uptime order alone when
    instance ids are available.

    Precedence:
    1. Both instance ids present and non-empty: True when different
       (proven restart), False when equal (same process lifetime).
    2. Both uptimes parse and current < prior: True (classic restart).
    3. Both uptimes parse and both wall-clock readings parse: True when
       the wall-clock elapsed exceeds the uptime delta by more than the
       skew tolerance (a replacement process with a larger uptime than
       the old snapshot is still a different process).
    4. Both uptimes parse: False (same lifetime, no restart evidence).
    5. Otherwise: None (missing/unparsable readings, no evidence).
    """
    prior_instance = str(prior_instance_id or "").strip()
    current_instance = str(current_instance_id or "").strip()
    if prior_instance and current_instance:
        return current_instance != prior_instance
    try:
        prior = float(str(prior_uptime).strip())
        current = float(str(current_uptime).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if current < prior:
        return True
    try:
        tolerance = (WORKER_RESTART_WALL_SKEW_TOLERANCE_SECONDS
                     if skew_tolerance_seconds is None
                     else float(str(skew_tolerance_seconds).strip()))
    except (TypeError, ValueError, AttributeError):
        tolerance = float(WORKER_RESTART_WALL_SKEW_TOLERANCE_SECONDS)
    if prior_wall_seconds is not None and current_wall_seconds is not None:
        try:
            prior_wall = float(str(prior_wall_seconds).strip())
            current_wall = float(str(current_wall_seconds).strip())
        except (TypeError, ValueError, AttributeError):
            return False
        elapsed = current_wall - prior_wall
        if elapsed < 0:
            return None
        if elapsed - (current - prior) > tolerance:
            return True
    return False


def format_restart_evidence(prior_uptime: object,
                            current_uptime: object,
                            prior_instance_id: object = None,
                            current_instance_id: object = None,
                            prior_wall_seconds: object = None,
                            current_wall_seconds: object = None,
                            skew_tolerance_seconds: object = None) -> str:
    """Human-readable restart evidence for controller diagnostics."""
    verdict = detect_worker_restart(
        prior_uptime, current_uptime, prior_instance_id,
        current_instance_id, prior_wall_seconds, current_wall_seconds,
        skew_tolerance_seconds,
    )
    prior_label = str(prior_uptime).strip() or "?"
    current_label = str(current_uptime).strip() or "?"
    prior_instance = str(prior_instance_id or "").strip()
    current_instance = str(current_instance_id or "").strip()
    if prior_instance and current_instance and prior_instance != current_instance:
        return ("worker restart observed (instance %s -> %s; uptime %s -> %s)"
                % (prior_instance[:12], current_instance[:12],
                   prior_label, current_label))
    if verdict is True:
        if prior_wall_seconds is not None and current_wall_seconds is not None:
            try:
                elapsed = (float(str(current_wall_seconds).strip())
                           - float(str(prior_wall_seconds).strip()))
                delta = (float(str(current_uptime).strip())
                         - float(str(prior_uptime).strip()))
                return ("worker restart observed (wall-clock elapsed %.0fs "
                        "but uptime delta only %.1fs: uptime %s -> %s)"
                        % (elapsed, delta, prior_label, current_label))
            except (TypeError, ValueError, AttributeError):
                pass
        return ("worker restart observed (uptime %s -> %s)"
                % (prior_label, current_label))
    if verdict is False:
        if prior_instance and current_instance:
            return ("same worker process lifetime (instance %s; uptime %s -> %s)"
                    % (prior_instance[:12], prior_label, current_label))
        return ("same worker process lifetime (uptime %s -> %s)"
                % (prior_label, current_label))
    return ("no uptime evidence (submit=%s current=%s)"
            % (prior_label, current_label))


# ---------------------------------------------------------------------------
# Execution lifecycle definition.
# ---------------------------------------------------------------------------

# The required ephemeral lifecycle, in order:
# 1. create exactly one temporary web service;
# 2. wait for deploy + health;
# 3. run the requested job;
# 4. collect the result;
# 5. delete the service in an unconditional cleanup/finally path;
# 6. verify the service no longer exists.
LIFECYCLE_STEPS = (
    "create",
    "wait_healthy",
    "submit_job",
    "collect_result",
    "delete",
    "verify_deletion",
)

# Cleanup runs after every one of these outcomes whenever a service id exists,
# including partial provisioning failures where the id was returned but the
# deploy or job never completed.
CLEANUP_TRIGGER_EVENTS = frozenset(
    {"success", "runner_failure", "timeout", "partial_provisioning_failure"}
)


def requires_cleanup(service_id: str | None, event: str) -> bool:
    """Deletion is required whenever a service id exists, for any event."""
    if event not in CLEANUP_TRIGGER_EVENTS:
        raise ValueError("unknown lifecycle event: %r" % event)
    return bool(service_id)


def deletion_succeeded(delete_status: int | None, verify_status: int | None) -> bool:
    """A workflow succeeds only if delete returned 204 and deletion verifies.

    A bare 204 without a subsequent 404/410 verification is not sufficient:
    the workflow must prove the temporary service is gone.
    """
    return delete_status == RENDER_DELETE_SUCCESS_STATUS and (
        verify_status is not None and is_deletion_verified(verify_status)
    )


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    return job_id


def submit_job_body(request: JobRequest) -> dict[str, Any]:
    """Serialize the submit-job request body sent to the runner."""
    return request.to_dict()


def parse_job_result(payload: Mapping[str, Any]) -> JobResult:
    """Parse and validate a runner status/result response body."""
    try:
        return JobResult(
            job_id=str(payload["job_id"]),
            status=str(payload["status"]),
            success=bool(payload["success"]),
            summary=str(payload.get("summary", "")),
            error=str(payload.get("error", "")),
            metadata=payload.get("metadata", {}),
        )
    except KeyError as exc:
        raise ValueError("job result missing field: %s" % exc) from exc


def service_name_for_attempt(issue_number: int, run_id: str) -> str:
    """Deterministic ephemeral service name; one service per issue attempt."""
    if issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    if not run_id:
        raise ValueError("run_id must not be empty")
    return "runtime-lab-issue%d-%s" % (issue_number, run_id)


def healthy_service_response(service: Mapping[str, Any]) -> bool:
    """A service counts as healthy when not suspended and URL-reachable."""
    if service.get("suspended") not in ("not_suspended", None):
        # Render returns suspended/not_suspended; anything else is unhealthy.
        if service.get("suspended") != "not_suspended":
            return False
    try:
        get_service_url(service)
    except ValueError:
        return False
    return True


def allowed_to_create_service(creations_this_attempt: Sequence[str]) -> bool:
    """Enforce one service creation per issue execution attempt."""
    return len(creations_this_attempt) < MAX_SERVICE_CREATIONS_PER_ATTEMPT
