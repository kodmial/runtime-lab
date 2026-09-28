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

# ---------------------------------------------------------------------------
# Official Render API references (exact documentation used).
# ---------------------------------------------------------------------------

RENDER_API_BASE = "https://api.render.com/v1"

RENDER_DOC_API_OVERVIEW = "https://render.com/docs/api"
RENDER_DOC_CREATE_SERVICE = "https://api-docs.render.com/reference/create-service"
RENDER_DOC_RETRIEVE_SERVICE = "https://api-docs.render.com/reference/retrieve-service"
RENDER_DOC_LIST_SERVICES = "https://api-docs.render.com/reference/list-services"
RENDER_DOC_DELETE_SERVICE = "https://api-docs.render.com/reference/delete-service"
RENDER_DOC_SUSPEND_SERVICE = "https://api-docs.render.com/reference/suspend-service-1"
RENDER_DOC_RETRIEVE_DEPLOY = "https://api-docs.render.com/reference/retrieve-deploy"
RENDER_DOC_LIST_DEPLOYS = "https://api-docs.render.com/reference/list-deploys"
RENDER_DOC_RATE_LIMITING = "https://api-docs.render.com/reference/rate-limiting"

RENDER_DOC_URLS = (
    RENDER_DOC_API_OVERVIEW,
    RENDER_DOC_CREATE_SERVICE,
    RENDER_DOC_RETRIEVE_SERVICE,
    RENDER_DOC_LIST_SERVICES,
    RENDER_DOC_DELETE_SERVICE,
    RENDER_DOC_SUSPEND_SERVICE,
    RENDER_DOC_RETRIEVE_DEPLOY,
    RENDER_DOC_LIST_DEPLOYS,
    RENDER_DOC_RATE_LIMITING,
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
                      title: str = "", body: str = "") -> str:
    """Build the runner task text for an issue execution.

    Uses the real issue title/body when provided (read-only resolution on
    the Actions side), otherwise falls back to a deterministic placeholder
    so offline tests and local runs still produce a valid job payload.
    Pure helper: no network access, reusable by the future controller.
    """
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    validate_execution_mode(execution_mode)
    title = (title or "").strip()
    body_text = (body or "").strip()
    if not title and not body_text:
        return "Execute issue #%d in %s mode." % (issue_number, execution_mode)
    if len(body_text) > MAX_TASK_BODY_CHARS:
        body_text = body_text[:MAX_TASK_BODY_CHARS] + "...[truncated]"
    if title and body_text:
        return ("Issue #%d [%s]: %s\n\n%s"
                % (issue_number, execution_mode, title, body_text))
    if title:
        return "Issue #%d [%s]: %s" % (issue_number, execution_mode, title)
    return "Issue #%d [%s]:\n\n%s" % (issue_number, execution_mode, body_text)


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
JOB_POLL_MAX_ATTEMPTS = 60
JOB_POLL_INTERVAL_SECONDS = 20
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
    start_command: str = "python -m automation.runner_server",
    health_check_path: str = "/health",
) -> dict[str, Any]:
    """Build the POST /v1/services body for an ephemeral free-tier worker.

    Uses the public Git URL explicitly with autoDeploy=no, so no
    Render<->GitHub provider connection is required for this phase. The
    build step installs the OpenCode CLI with the known-good NanoDictate
    pattern (``curl -fsSL https://opencode.ai/install | bash`` via
    automation/install-opencode.sh) so every worker can execute jobs.
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
                "startCommand": start_command,
            },
        },
    }


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

    def __post_init__(self) -> None:
        if self.repository_url != PUBLIC_REPO_URL:
            raise ValueError(
                "repository_url must be %r in this phase" % PUBLIC_REPO_URL
            )
        if not self.base_ref:
            raise ValueError("base_ref must not be empty")
        if not self.task_text.strip():
            raise ValueError("task_text must not be empty")
        if not isinstance(self.issue_number, int) or self.issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if self.metadata is not None and self.metadata.issue_number != self.issue_number:
            raise ValueError("metadata.issue_number must match issue_number")

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
