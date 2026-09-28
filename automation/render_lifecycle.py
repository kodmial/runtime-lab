"""Render execution lifecycle and GitHub <-> runner contract for issue #1.

This module is the implementation-facing definition of the ephemeral Render
execution flow used during development. It encodes the lifecycle, the exact
official Render API operations, the free-tier guard, the low-burn policy, the
runner HTTP contract, and the job/result schemas so that shell controllers,
the runner app, and tests share one authoritative source.

Official Render API documentation used (authoritative references):
- API overview and authentication: https://render.com/docs/api
- API reference index: https://api-docs.render.com/reference
- Create service (POST /v1/services): https://api-docs.render.com/reference/create-service
- Retrieve service (GET /v1/services/{serviceId}): https://api-docs.render.com/reference/retrieve-service
- List services (GET /v1/services): https://api-docs.render.com/reference/list-services
- Delete service (DELETE /v1/services/{serviceId}): https://api-docs.render.com/reference/delete-service
- Suspend service fallback (POST /v1/services/{serviceId}/suspend):
  https://api-docs.render.com/reference/suspend-service-1
- Resume service (POST /v1/services/{serviceId}/resume, documented for completeness;
  the ephemeral flow never resumes): https://api-docs.render.com/reference/resume-service-1
- Rate limiting incl. headers and 429 semantics: https://api-docs.render.com/reference/rate-limiting
- Regions (Oregon, Ohio, Virginia, Frankfurt, Singapore):
  https://render.com/docs/regions
- Free instances / free compute plan: https://render.com/docs/free
- Web services (public URL, health check, autoDeploy behavior):
  https://render.com/docs/web-services

Lifecycle (ephemeral, unconditional cleanup):
1. Create exactly one temporary Render web service for an execution attempt.
2. Wait for it to deploy and become healthy (bounded polling, no new service).
3. Submit one job to the runner and poll for its result (bounded polling).
4. Collect the result.
5. In an unconditional cleanup/finally path (GitHub workflow step with
   ``if: always()`` reading the state file), delete the Render service.
6. Verify the created service no longer exists (retrieve returns 404 and/or
   the service id is absent from list output).

Deletion is the primary cleanup mechanism. Suspension is only an emergency
fallback when deletion temporarily fails; the workflow still retries deletion
within bounded limits. A successful workflow must not leave the service behind.

Authentication notes:
- Render API authentication uses the repository Actions secret named ``KEY``.
  Workflows reference it only as ``${{ secrets.KEY }}`` mapped to the
  ``RENDER_API_KEY`` environment variable. This module never reads, logs, or
  prints secret values.
- GitHub-side writes use the workflow-provided ``GITHUB_TOKEN``. The Render
  runner itself carries no GitHub credentials in this iteration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Official documentation references (exact URLs used to fix the contract).
# ---------------------------------------------------------------------------

DOC_RENDER_API_OVERVIEW = "https://render.com/docs/api"
DOC_RENDER_API_REFERENCE = "https://api-docs.render.com/reference"
DOC_CREATE_SERVICE = "https://api-docs.render.com/reference/create-service"
DOC_RETRIEVE_SERVICE = "https://api-docs.render.com/reference/retrieve-service"
DOC_LIST_SERVICES = "https://api-docs.render.com/reference/list-services"
DOC_DELETE_SERVICE = "https://api-docs.render.com/reference/delete-service"
DOC_SUSPEND_SERVICE = "https://api-docs.render.com/reference/suspend-service-1"
DOC_RESUME_SERVICE = "https://api-docs.render.com/reference/resume-service-1"
DOC_RATE_LIMITING = "https://api-docs.render.com/reference/rate-limiting"
DOC_REGIONS = "https://render.com/docs/regions"
DOC_FREE_INSTANCES = "https://render.com/docs/free"
DOC_WEB_SERVICES = "https://render.com/docs/web-services"

RENDER_DOC_URLS = (
    DOC_RENDER_API_OVERVIEW,
    DOC_RENDER_API_REFERENCE,
    DOC_CREATE_SERVICE,
    DOC_RETRIEVE_SERVICE,
    DOC_LIST_SERVICES,
    DOC_DELETE_SERVICE,
    DOC_SUSPEND_SERVICE,
    DOC_RESUME_SERVICE,
    DOC_RATE_LIMITING,
    DOC_REGIONS,
    DOC_FREE_INSTANCES,
    DOC_WEB_SERVICES,
)

# ---------------------------------------------------------------------------
# Render API base and operations.
# ---------------------------------------------------------------------------

RENDER_API_BASE = "https://api.render.com/v1"

# Operation identifiers in implementation-facing form. HTTP method + path is
# the canonical Render contract; these constants keep call sites consistent.
OP_CREATE_SERVICE = "POST /v1/services"
OP_RETRIEVE_SERVICE = "GET /v1/services/{serviceId}"
OP_LIST_SERVICES = "GET /v1/services"
OP_DELETE_SERVICE = "DELETE /v1/services/{serviceId}"
OP_SUSPEND_SERVICE = "POST /v1/services/{serviceId}/suspend"
OP_RETRIEVE_DEPLOY = "GET /v1/services/{serviceId}/deploys/{deployId}"
OP_LIST_DEPLOYS = "GET /v1/services/{serviceId}/deploys"

REQUIRED_LIFECYCLE_OPS = (
    OP_CREATE_SERVICE,
    OP_RETRIEVE_SERVICE,
    OP_LIST_SERVICES,
    OP_SUSPEND_SERVICE,
    OP_DELETE_SERVICE,
)


def service_path(service_id: str) -> str:
    """Return the API path used to retrieve or delete one service."""
    _require_service_id(service_id)
    return f"/v1/services/{service_id}"


def suspend_path(service_id: str) -> str:
    """Return the API path for the suspend-fallback operation."""
    _require_service_id(service_id)
    return f"/v1/services/{service_id}/suspend"


def service_url(api_base: str, path: str) -> str:
    """Join an API base and path without leaking credentials."""
    return api_base.rstrip("/") + path


def _require_service_id(service_id: str) -> str:
    if not isinstance(service_id, str) or not service_id.strip():
        raise ValueError("serviceId is required")
    return service_id.strip()


# ---------------------------------------------------------------------------
# Rate limits and headers (from DOC_RATE_LIMITING).
# ---------------------------------------------------------------------------

# POST /v1/services is limited to 20/hour per the official rate-limit table.
RATE_LIMIT_CREATE_PER_HOUR = 20
# Create/update/deploy/resume/suspend operations share a 10/minute/service
# bucket per the official table. Deletion itself falls under
# "All other POST / PATCH / DELETE: 30/minute".
RATE_LIMIT_MUTATE_PER_MINUTE_PER_SERVICE = 10
RATE_LIMIT_DELETE_PER_MINUTE = 30
RATE_LIMIT_GET_PER_MINUTE = 400

RATE_LIMIT_HEADER_LIMIT = "RateLimit-Limit"
RATE_LIMIT_HEADER_REMAINING = "RateLimit-Remaining"
RATE_LIMIT_HEADER_RESET = "RateLimit-Reset"
RATE_LIMIT_HEADER_RETRY_AFTER = "Retry-After"
HTTP_TOO_MANY_REQUESTS = 429

# ---------------------------------------------------------------------------
# Low-burn policy (encoded, not an operational note).
# ---------------------------------------------------------------------------

# At most four automation jobs may be active concurrently across issues.
MAX_CONCURRENT_AUTOMATION_JOBS = 4
# Each issue attempt owns at most one ephemeral Render service.
MAX_SERVICES_PER_ISSUE_ATTEMPT = 1
# One service creation per issue execution attempt; polling/deploy retries
# must reuse the same created service and never create a new one.
MAX_SERVICE_CREATES_PER_ATTEMPT = 1
# No cron job may create Render services merely for health checking.
ALLOW_CRON_CREATED_SERVICES = False
# No unbounded retries: every polling/retry loop is bounded.
MAX_DEPLOY_POLLS = 60
DEPLOY_POLL_INTERVAL_SECONDS = 20
MAX_JOB_POLLS = 90
JOB_POLL_INTERVAL_SECONDS = 20
MAX_DELETE_ATTEMPTS = 5
DELETE_RETRY_DELAY_SECONDS = 10
# Suspend is attempted at most once and only after a deletion failure.
MAX_SUSPEND_ATTEMPTS = 1

# Deploy-failure and runner-failure retries reuse the same service: callers
# must pass the existing service id instead of invoking creation again.
RETRY_REUSES_SAME_SERVICE = True

# ---------------------------------------------------------------------------
# Free-tier guard (explicit and testable; fail closed).
# ---------------------------------------------------------------------------

FREE_PLAN = "free"
# Any plan value other than exactly "free" is treated as paid/unknown and
# must abort the workflow rather than silently run a paid resource.
PAID_PLAN_EXAMPLES = (
    "starter",
    "standard",
    "pro",
    "starter_plus",
    "standard_plus",
    "pro_plus",
    "pro_max",
    "pro_ultra",
    "0.5c-512mb",
    "1c-2g",
)


def is_free_plan(plan: Any) -> bool:
    """Return True only for the exact free-tier plan identifier."""
    return isinstance(plan, str) and plan.strip() == FREE_PLAN


def assert_free_plan(plan: Any) -> str:
    """Fail closed unless the plan is explicitly the free tier."""
    if not is_free_plan(plan):
        raise FreeTierViolation(f"refusing non-free Render plan: {plan!r}")
    return FREE_PLAN


class FreeTierViolation(ValueError):
    """Raised when a Render response indicates a paid/unknown plan."""


def extract_plan(service_payload: Mapping[str, Any]) -> Optional[Any]:
    """Extract the compute plan from a create/retrieve service payload.

    Handles both the flattened ``plan`` key used in tests and the official
    nested ``serviceDetails`` shape (``service.serviceDetails.plan`` or
    ``serviceDetails.plan``).
    """
    if not isinstance(service_payload, Mapping):
        return None
    if service_payload.get("plan") is not None:
        return service_payload.get("plan")
    for key in ("service", "serviceDetails"):
        nested = service_payload.get(key)
        if isinstance(nested, Mapping):
            found = extract_plan(nested)
            if found is not None:
                return found
    details = service_payload.get("serviceDetails")
    if isinstance(details, Mapping) and details.get("plan") is not None:
        return details.get("plan")
    return None


def extract_service_url(service_payload: Mapping[str, Any]) -> Optional[str]:
    """Return the externally reachable service URL, if present.

    The official web-service details expose the public URL as
    ``serviceDetails.url`` (see DOC_CREATE_SERVICE / DOC_WEB_SERVICES).
    """
    if not isinstance(service_payload, Mapping):
        return None
    service = service_payload.get("service")
    if isinstance(service, Mapping):
        url = extract_service_url(service)
        if url:
            return url
    details = service_payload.get("serviceDetails")
    if isinstance(details, Mapping):
        url = details.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    url = service_payload.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


# ---------------------------------------------------------------------------
# Source / Git connection strategy.
# ---------------------------------------------------------------------------

PUBLIC_REPO_URL = "https://github.com/kodmial/runtime-lab"
SERVICE_TYPE_WEB = "web_service"
AUTO_DEPLOY_DISABLED = "no"
DEFAULT_BRANCH = "main"
HEALTH_CHECK_PATH = "/health"

# ---------------------------------------------------------------------------
# OpenCode execution policy.
# ---------------------------------------------------------------------------

PREFERRED_MODEL = "opencode/muse-spark-1.3-contributor-free"
FALLBACK_MODEL = "opencode/space-bunny-free"
ALLOWED_MODELS = (PREFERRED_MODEL, FALLBACK_MODEL)
# Model fallback must happen inside the same worker attempt; provisioning a
# second Render service merely to change models is forbidden.
ALLOW_SECOND_SERVICE_FOR_MODEL_FALLBACK = False

DEFAULT_REGION = "oregon"
FALLBACK_NON_US_REGION = "singapore"
# Ephemeral workers running Muse jobs must stay in the US or Singapore.
ALLOWED_MUSE_REGIONS = ("oregon", "ohio", "virginia", "singapore")
FORBIDDEN_MUSE_REGIONS = ("frankfurt",)
# Full Render region vocabulary from DOC_REGIONS.
RENDER_REGIONS = ("oregon", "ohio", "virginia", "frankfurt", "singapore")


class RegionViolation(ValueError):
    """Raised when a Muse job requests a forbidden region."""


def validate_muse_region(region: Any) -> str:
    """Validate the worker region for Muse execution.

    Returns the normalized region id, defaulting to ``oregon`` when empty.
    Raises :class:`RegionViolation` for forbidden regions (notably
    ``frankfurt``) so callers fail closed before any service is created.
    """
    normalized = (region or "").strip() if isinstance(region, str) else ""
    if not normalized:
        return DEFAULT_REGION
    if normalized in FORBIDDEN_MUSE_REGIONS:
        raise RegionViolation(f"region {normalized!r} is forbidden for Muse jobs")
    if normalized not in ALLOWED_MUSE_REGIONS:
        raise RegionViolation(f"unknown Muse region: {normalized!r}")
    return normalized


def select_model(primary_available: bool = True) -> str:
    """Select the OpenCode model inside the same worker attempt.

    No new Render service may be provisioned for the fallback; callers stay
    on the already-created worker and switch the in-worker model string.
    """
    return PREFERRED_MODEL if primary_available else FALLBACK_MODEL


# ---------------------------------------------------------------------------
# Runner HTTP contract (GitHub -> runner, runner -> GitHub).
# ---------------------------------------------------------------------------

RUNNER_HEALTH_PATH = "/health"
RUNNER_SUBMIT_PATH = "/v1/jobs"
RUNNER_JOB_STATUS_PATH_TEMPLATE = "/v1/jobs/{jobId}"
RUNNER_JOB_ID_FIELD = "jobId"

RUNNER_HTTP_TIMEOUT_SECONDS = 30
RUNNER_JOB_TIMEOUT_SECONDS = 1800

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_TIMEOUT = "timeout"
JOB_STATUSES = (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_TIMEOUT,
)
TERMINAL_JOB_STATUSES = (
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_TIMEOUT,
)


def runner_job_status_path(job_id: str) -> str:
    """Return the status/result path for one runner job identifier."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("jobId is required")
    return RUNNER_JOB_STATUS_PATH_TEMPLATE.format(jobId=job_id.strip())


_ISSUE_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{4,64}|[A-Za-z0-9_.\-/]+)$")


@dataclass(frozen=True)
class ExecutionMetadata:
    """Non-secret execution metadata recorded with every job."""

    issue_number: int
    run_id: str = ""
    attempt: int = 1
    mode: str = "smoke"
    region: str = DEFAULT_REGION
    model: str = PREFERRED_MODEL
    repository: str = PUBLIC_REPO_URL

    def validate(self) -> "ExecutionMetadata":
        if not isinstance(self.issue_number, int) or self.issue_number <= 0:
            raise ValueError("metadata.issue_number must be a positive integer")
        if self.mode not in ("smoke", "e2e"):
            raise ValueError("metadata.mode must be 'smoke' or 'e2e'")
        validate_muse_region(self.region)
        if self.model not in ALLOWED_MODELS:
            raise ValueError(f"unsupported model: {self.model!r}")
        if not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("metadata.attempt must be >= 1")
        return self

    def to_dict(self) -> dict:
        self.validate()
        return {
            "issueNumber": self.issue_number,
            "runId": self.run_id,
            "attempt": self.attempt,
            "mode": self.mode,
            "region": validate_muse_region(self.region),
            "model": self.model,
            "repository": self.repository,
        }


@dataclass(frozen=True)
class JobRequest:
    """Minimum GitHub -> runner submit-job payload.

    Required fields: repository URL, base ref/SHA, task text, issue number,
    and execution metadata. No GitHub tokens are carried in this payload.
    """

    repository_url: str
    base_ref: str
    task_text: str
    issue_number: int
    metadata: ExecutionMetadata
    base_sha: str = ""

    def validate(self) -> "JobRequest":
        if self.repository_url != PUBLIC_REPO_URL:
            raise ValueError(
                "repository_url must be the public development repository: "
                f"{PUBLIC_REPO_URL}"
            )
        if not isinstance(self.base_ref, str) or not self.base_ref.strip():
            raise ValueError("base_ref is required")
        if self.base_sha and (
            not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha.strip())
        ):
            raise ValueError("base_sha is malformed")
        if not isinstance(self.task_text, str) or not self.task_text.strip():
            raise ValueError("task_text is required")
        if not isinstance(self.issue_number, int) or self.issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if not isinstance(self.metadata, ExecutionMetadata):
            raise ValueError("metadata must be ExecutionMetadata")
        self.metadata.validate()
        if self.metadata.issue_number != self.issue_number:
            raise ValueError("metadata.issue_number must match issue_number")
        return self

    def to_dict(self) -> dict:
        self.validate()
        payload = {
            "repositoryUrl": self.repository_url,
            "baseRef": self.base_ref.strip(),
            "taskText": self.task_text.strip(),
            "issueNumber": self.issue_number,
            "metadata": self.metadata.to_dict(),
        }
        if self.base_sha.strip():
            payload["baseSha"] = self.base_sha.strip()
        return payload

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "JobRequest":
        if not isinstance(data, Mapping):
            raise ValueError("job request must be an object")
        metadata_raw = data.get("metadata")
        if not isinstance(metadata_raw, Mapping):
            raise ValueError("job request metadata is required")
        metadata = ExecutionMetadata(
            issue_number=int(metadata_raw.get("issueNumber")),
            run_id=str(metadata_raw.get("runId") or ""),
            attempt=int(metadata_raw.get("attempt") or 1),
            mode=str(metadata_raw.get("mode") or "smoke"),
            region=str(metadata_raw.get("region") or DEFAULT_REGION),
            model=str(metadata_raw.get("model") or PREFERRED_MODEL),
            repository=str(metadata_raw.get("repository") or PUBLIC_REPO_URL),
        )
        return JobRequest(
            repository_url=str(data.get("repositoryUrl") or ""),
            base_ref=str(data.get("baseRef") or ""),
            task_text=str(data.get("taskText") or ""),
            issue_number=int(data.get("issueNumber") or 0),
            metadata=metadata,
            base_sha=str(data.get("baseSha") or ""),
        )


@dataclass(frozen=True)
class JobResult:
    """Runner -> GitHub job status/result payload."""

    job_id: str
    status: str
    issue_number: int
    model: str = PREFERRED_MODEL
    region: str = DEFAULT_REGION
    summary: str = ""
    error: str = ""
    pr_number: Optional[int] = None

    def validate(self) -> "JobResult":
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("job_id is required")
        if self.status not in JOB_STATUSES:
            raise ValueError(f"unknown job status: {self.status!r}")
        if not isinstance(self.issue_number, int) or self.issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if self.model not in ALLOWED_MODELS:
            raise ValueError(f"unsupported model: {self.model!r}")
        validate_muse_region(self.region)
        if self.pr_number is not None and (
            not isinstance(self.pr_number, int) or self.pr_number <= 0
        ):
            raise ValueError("pr_number must be a positive integer when present")
        if self.status in (JOB_STATUS_FAILED, JOB_STATUS_TIMEOUT) and not (
            self.error.strip() if isinstance(self.error, str) else ""
        ):
            raise ValueError(f"status {self.status!r} requires an error message")
        return self

    def to_dict(self) -> dict:
        self.validate()
        payload: dict = {
            "jobId": self.job_id.strip(),
            "status": self.status,
            "issueNumber": self.issue_number,
            "model": self.model,
            "region": validate_muse_region(self.region),
            "summary": self.summary or "",
        }
        if self.error:
            payload["error"] = self.error
        if self.pr_number is not None:
            payload["prNumber"] = self.pr_number
        return payload

    @property
    def succeeded(self) -> bool:
        return self.status == JOB_STATUS_SUCCEEDED

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "JobResult":
        if not isinstance(data, Mapping):
            raise ValueError("job result must be an object")
        return JobResult(
            job_id=str(data.get("jobId") or data.get("job_id") or ""),
            status=str(data.get("status") or ""),
            issue_number=int(data.get("issueNumber") or data.get("issue_number") or 0),
            model=str(data.get("model") or PREFERRED_MODEL),
            region=str(data.get("region") or DEFAULT_REGION),
            summary=str(data.get("summary") or ""),
            error=str(data.get("error") or ""),
            pr_number=data.get("prNumber", data.get("pr_number")),
        )


# ---------------------------------------------------------------------------
# Service creation payload and guards.
# ---------------------------------------------------------------------------


def build_service_name(issue_number: int, run_id: str) -> str:
    """Build a unique, traceable ephemeral service name for one attempt."""
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    suffix = re.sub(r"[^A-Za-z0-9-]", "-", str(run_id or "local").strip())[:24] or "local"
    return f"runtime-lab-issue-{issue_number}-{suffix}".lower()


def build_create_payload(
    *,
    issue_number: int,
    run_id: str,
    owner_id: str,
    region: str = DEFAULT_REGION,
    branch: str = DEFAULT_BRANCH,
    service_name: str = "",
) -> dict:
    """Build the POST /v1/services payload for one ephemeral web service.

    The payload pins the free compute plan, the allowed Muse region, the
    public repository URL, and ``autoDeploy=no`` so Git pushes never trigger
    extra deployments. Callers must create at most once per attempt.
    """
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id is required")
    normalized_region = validate_muse_region(region)
    name = service_name.strip() if isinstance(service_name, str) and service_name.strip() else build_service_name(
        issue_number, run_id
    )
    return {
        "type": SERVICE_TYPE_WEB,
        "name": name,
        "ownerId": owner_id.strip(),
        "repo": PUBLIC_REPO_URL,
        "autoDeploy": AUTO_DEPLOY_DISABLED,
        "branch": (branch or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH,
        "serviceDetails": {
            "runtime": "python",
            "plan": FREE_PLAN,
            "region": normalized_region,
            "numInstances": 1,
            "healthCheckPath": HEALTH_CHECK_PATH,
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "python automation/render_runner.py",
            },
        },
    }


def assert_create_payload_is_free(payload: Mapping[str, Any]) -> None:
    """Fail closed unless a creation payload requests the free tier."""
    if not isinstance(payload, Mapping):
        raise FreeTierViolation("create payload must be an object")
    details = payload.get("serviceDetails")
    plan = details.get("plan") if isinstance(details, Mapping) else None
    assert_free_plan(plan)


# ---------------------------------------------------------------------------
# Cleanup semantics.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupPlan:
    """Bounded cleanup plan once a service id exists.

    Deletion is always attempted first. Suspension is attempted at most once
    and only after a deletion failure, purely as an emergency fallback to
    stop billing/compute while deletion is retried within bounded limits.
    """

    service_id: str
    delete_attempts: int = MAX_DELETE_ATTEMPTS
    suspend_attempts: int = MAX_SUSPEND_ATTEMPTS

    def validate(self) -> "CleanupPlan":
        _require_service_id(self.service_id)
        if self.delete_attempts < 1 or self.delete_attempts > MAX_DELETE_ATTEMPTS:
            raise ValueError("delete_attempts is outside the bounded policy")
        if self.suspend_attempts < 0 or self.suspend_attempts > MAX_SUSPEND_ATTEMPTS:
            raise ValueError("suspend_attempts is outside the bounded policy")
        return self


def should_cleanup(service_id: Any) -> bool:
    """Cleanup runs whenever a service id exists, including partial failures."""
    return isinstance(service_id, str) and bool(service_id.strip())


def deletion_verified(
    retrieve_status: Optional[int],
    service_ids_in_list: Sequence[str] = (),
    service_id: str = "",
) -> bool:
    """Return True only when deletion of the service is proven.

    Verification succeeds when a retrieve returns 404/410, or when the id is
    absent from a list-services response. Any other outcome (including a
    successful retrieve) means the service may still exist and the workflow
    must keep failing closed rather than report success.
    """
    if retrieve_status in (404, 410):
        return True
    if service_id and service_ids_in_list is not None:
        return service_id not in set(service_ids_in_list)
    return False


def parse_service_id_from_create_response(data: Mapping[str, Any]) -> str:
    """Extract the new service id from a 201 create-service response."""
    if not isinstance(data, Mapping):
        raise ValueError("create response must be an object")
    service = data.get("service")
    candidate = None
    if isinstance(service, Mapping):
        candidate = service.get("id")
    if not candidate:
        candidate = data.get("id")
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("create response does not contain a service id")
    return candidate.strip()


def enforce_single_create(existing_service_id: Any) -> None:
    """Enforce one service creation per issue execution attempt.

    Polling, deploy, and job retries must reuse ``existing_service_id`` when
    present instead of creating a second service.
    """
    if should_cleanup(existing_service_id):
        raise ValueError(
            "a service already exists for this attempt; "
            "retries must reuse the same service id"
        )


@dataclass(frozen=True)
class ExecutionAttemptState:
    """State tracked across create -> wait -> run -> collect -> delete."""

    issue_number: int
    service_id: str = ""
    job_id: str = ""
    service_creates: int = 0
    region: str = DEFAULT_REGION
    model: str = PREFERRED_MODEL

    def with_created_service(self, service_id: str) -> "ExecutionAttemptState":
        enforce_single_create(self.service_id)
        if self.service_creates + 1 > MAX_SERVICE_CREATES_PER_ATTEMPT:
            raise ValueError("only one service creation is allowed per attempt")
        return ExecutionAttemptState(
            issue_number=self.issue_number,
            service_id=_require_service_id(service_id),
            job_id=self.job_id,
            service_creates=self.service_creates + 1,
            region=self.region,
            model=self.model,
        )

    def describe_cleanup(self) -> CleanupPlan:
        if not should_cleanup(self.service_id):
            raise ValueError("no service id exists; nothing to clean up")
        return CleanupPlan(service_id=self.service_id).validate()


def redact_secret(value: Any) -> str:
    """Return a fixed placeholder so logs never contain secret material."""
    _ = value
    return "***"
