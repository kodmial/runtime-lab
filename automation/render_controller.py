"""Persistent Render controller core for direct GitHub webhook ingress (issue #10).

Final architecture (P0, first issue of the final no-Actions architecture):

    GitHub issue/event -> repository webhook -> Render controller
        -> ephemeral Render worker -> OpenCode -> Render controller
        -> GitHub branch/PR.

GitHub Actions may still exist for ordinary CI, but is NOT required to
start or run OpenCode in this path. This module (plus
``automation/controller_server.py``) is the persistent controller side.
It never executes OpenCode itself and never dispatches GitHub Actions
workflows; it owns webhook acceptance, scheduling decisions and
ephemeral-worker dispatch through the reusable lifecycle core from
``automation/render_lifecycle.py`` (built under the temporary #4
harness).

Webhook availability invariant (enforced by the HTTP layer, encoded here
as constants and durable-store semantics):

- The ingress acknowledges GitHub with a 2xx response within
  ``WEBHOOK_ACK_BUDGET_SECONDS`` (10s) without doing any Render/network
  work first: signature check, duplicate check and one durable append,
  then respond. Dispatch happens on background threads afterwards.
- A sleeping Render Free web service must NOT be the sole webhook
  receiver. The supported deployments are ``always-on-controller`` (a
  controller service that does not sleep) or ``queued-ingress`` (an
  independent always-on ingress/queue that durably accepts the webhook
  first and forwards/wakes the controller). GitHub Actions must not be
  used as that relay. See ``DEPLOYMENT_PATTERNS`` and
  ``validate_deployment()``.
- Because failed GitHub webhook deliveries are not automatically
  redelivered, every accepted delivery is persisted before the 2xx, the
  store is reloaded on startup (restart recovery), and redelivery of the
  same ``X-GitHub-Delivery`` ID is idempotent (never dispatches a second
  worker).

Scheduling semantics reuse the repository's existing scheduler behavior
(``.github/workflows/issue-scheduler.yml``), not a second incompatible
workflow: priority:p0/p1/p2 ordering, native blocked-by dependencies
(with the DoR ``#N is completed`` fallback), automation:in-progress
reservation with lease/open-PR handling, automation:paused handling and
WIP/concurrency limits (default 4 concurrent issues, per-issue single
worker, no global single-job mutex).

Worker dispatch policy (preserved after Actions are removed):
anonymous/free OpenCode access, Muse Spark 1.3 Contributor Free
preferred with Space Bunny Free fallback inside the SAME worker attempt
(never a second service for fallback), Muse workers restricted to US
Render regions or Singapore.

Stdlib only, like the rest of automation/.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import (
        DEFAULT_WORKER_REGION,
        DELETE_MAX_ATTEMPTS,
        DEPLOY_POLL_INTERVAL_SECONDS,
        DEPLOY_POLL_MAX_ATTEMPTS,
        FALLBACK_MODEL,
        JOB_POLL_INTERVAL_SECONDS,
        JOB_POLL_MAX_ATTEMPTS,
        MAX_CONCURRENT_AUTOMATION_JOBS,
        MAX_SERVICE_CREATIONS_PER_ATTEMPT,
        MAX_SERVICES_PER_ISSUE_ATTEMPT,
        PREFERRED_MODEL,
        PUBLIC_REPO_BRANCH,
        PUBLIC_REPO_URL,
        RUNNER_HEALTH_INTERVAL_SECONDS,
        RUNNER_HEALTH_MAX_ATTEMPTS,
        SUSPEND_FALLBACK_MAX_ATTEMPTS,
        ExecutionMetadata,
        JobRequest,
        build_create_service_payload,
        classify_deploy_status,
        deletion_succeeded,
        is_deletion_verified,
        is_terminal_job_status,
        parse_job_result,
        resolve_task_text,
        select_base_sha,
        service_name_for_attempt,
        validate_execution_mode,
        validate_model_name,
        validate_worker_region,
        verify_free_plan_response,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import (  # type: ignore[no-redef]
        DEFAULT_WORKER_REGION,
        DELETE_MAX_ATTEMPTS,
        DEPLOY_POLL_INTERVAL_SECONDS,
        DEPLOY_POLL_MAX_ATTEMPTS,
        FALLBACK_MODEL,
        JOB_POLL_INTERVAL_SECONDS,
        JOB_POLL_MAX_ATTEMPTS,
        MAX_CONCURRENT_AUTOMATION_JOBS,
        MAX_SERVICE_CREATIONS_PER_ATTEMPT,
        MAX_SERVICES_PER_ISSUE_ATTEMPT,
        PREFERRED_MODEL,
        PUBLIC_REPO_BRANCH,
        PUBLIC_REPO_URL,
        RUNNER_HEALTH_INTERVAL_SECONDS,
        RUNNER_HEALTH_MAX_ATTEMPTS,
        SUSPEND_FALLBACK_MAX_ATTEMPTS,
        ExecutionMetadata,
        JobRequest,
        build_create_service_payload,
        classify_deploy_status,
        deletion_succeeded,
        is_deletion_verified,
        is_terminal_job_status,
        parse_job_result,
        resolve_task_text,
        select_base_sha,
        service_name_for_attempt,
        validate_execution_mode,
        validate_model_name,
        validate_worker_region,
        verify_free_plan_response,
    )

try:  # pragma: no cover - import path depends on entrypoint
    from automation.opencode_runner import is_model_unavailable_error
except ImportError:  # pytest inserts automation/ on sys.path
    from opencode_runner import (  # type: ignore[no-redef]
        is_model_unavailable_error,
    )

# ---------------------------------------------------------------------------
# Controller identity / ingress contract.
# ---------------------------------------------------------------------------

CONTROLLER_SERVICE_NAME = "runtime-lab-controller"
CONTROLLER_VERSION = "issue10-1"

# Canonical webhook path. The HTTP server also accepts the /github/webhook
# and /webhook aliases for operator convenience; all three share semantics.
WEBHOOK_PATH = "/webhooks/github"
WEBHOOK_PATH_ALIASES = frozenset({"/webhooks/github", "/github/webhook", "/webhook"})
CONTROLLER_HEALTH_PATH = "/health"
DELIVERIES_PATH = "/v1/deliveries"

# GitHub must receive a 2xx within this budget even after a long idle
# period. The request handler performs no Render/GitHub network I/O before
# responding, so the bound holds whenever the controller process itself is
# reachable (hence the always-on deployment requirement below).
WEBHOOK_ACK_BUDGET_SECONDS = 10

# ---------------------------------------------------------------------------
# Deployment patterns for the availability invariant.
# ---------------------------------------------------------------------------

# A sleeping Render Free web service must NOT be the sole webhook receiver.
# Supported patterns:
# - "always-on-controller": the controller service itself never sleeps
#   (e.g. a paid/always-on Render service or equivalent host).
# - "queued-ingress": an independent always-on ingress/queue durably accepts
#   the GitHub webhook first and forwards/wakes the Render controller.
# GitHub Actions must not be used as that relay in the final path.
DEPLOYMENT_ALWAYS_ON = "always-on-controller"
DEPLOYMENT_QUEUED_INGRESS = "queued-ingress"
DEPLOYMENT_PATTERNS = (DEPLOYMENT_ALWAYS_ON, DEPLOYMENT_QUEUED_INGRESS)

ENV_DEPLOYMENT = "CONTROLLER_DEPLOYMENT"
ENV_ALWAYS_ON = "CONTROLLER_ALWAYS_ON"
ENV_QUEUE_URL = "CONTROLLER_QUEUE_URL"

# For this public-repository development stage the persistent controller
# itself may be deployed directly from the public source with auto-deploy
# disabled (no Render Git-provider credentials required for that source
# mode). A future move to a private source repository must explicitly add
# Render Git-provider credentials or switch to a prebuilt image. GitHub App
# authentication (issue #11) is a separate concern: it is for GitHub events
# and GitHub API/write-back access, not for Render source cloning.
CONTROLLER_SOURCE_REPO = PUBLIC_REPO_URL
CONTROLLER_SOURCE_BRANCH = PUBLIC_REPO_BRANCH
CONTROLLER_SOURCE_AUTO_DEPLOY = "no"


def resolve_deployment_pattern(
    *,
    deployment: str | None = None,
    always_on: str | None = None,
    queue_url: str | None = None,
) -> str:
    """Resolve which supported ingress pattern this controller uses.

    Explicit ``CONTROLLER_DEPLOYMENT`` wins; otherwise ``always-on`` is
    inferred from ``CONTROLLER_ALWAYS_ON=true``, and ``queued-ingress``
    from ``CONTROLLER_QUEUE_URL``. Defaults to ``always-on-controller``
    (the operator must actually deploy it that way; see
    ``validate_deployment``).
    """
    raw_deployment = (deployment if deployment is not None
                      else os.environ.get(ENV_DEPLOYMENT, "")).strip()
    if raw_deployment:
        if raw_deployment not in DEPLOYMENT_PATTERNS:
            raise ValueError(
                "unknown controller deployment %r; expected one of %s"
                % (raw_deployment, list(DEPLOYMENT_PATTERNS))
            )
        return raw_deployment
    raw_always_on = (always_on if always_on is not None
                     else os.environ.get(ENV_ALWAYS_ON, "")).strip().lower()
    raw_queue = (queue_url if queue_url is not None
                 else os.environ.get(ENV_QUEUE_URL, "")).strip()
    if raw_queue:
        return DEPLOYMENT_QUEUED_INGRESS
    if raw_always_on in ("1", "true", "yes", "on"):
        return DEPLOYMENT_ALWAYS_ON
    # Default declares the required posture; validate_deployment() tells the
    # operator what is still missing for a real deployment.
    return DEPLOYMENT_ALWAYS_ON


def validate_deployment(
    *,
    deployment: str | None = None,
    always_on: str | None = None,
    queue_url: str | None = None,
) -> tuple[bool, str]:
    """Check the deployment satisfies the availability invariant.

    Returns ``(ok, note)``. ``ok`` is True only when the operator has
    explicitly declared an always-on posture (``CONTROLLER_ALWAYS_ON=true``
    or ``CONTROLLER_DEPLOYMENT=always-on-controller``) or an external
    always-on queue (``CONTROLLER_QUEUE_URL`` / queued-ingress). A default
    Render Free web service with no declaration is NOT ok as the sole
    receiver, because its cold start can exceed the 10s ack budget and the
    first webhook would disappear (failed GitHub deliveries are not
    automatically redelivered).
    """
    pattern = resolve_deployment_pattern(
        deployment=deployment, always_on=always_on, queue_url=queue_url
    )
    raw_always_on = (always_on if always_on is not None
                     else os.environ.get(ENV_ALWAYS_ON, "")).strip().lower()
    raw_queue = (queue_url if queue_url is not None
                 else os.environ.get(ENV_QUEUE_URL, "")).strip()
    raw_deployment = (deployment if deployment is not None
                      else os.environ.get(ENV_DEPLOYMENT, "")).strip()
    declared = bool(raw_deployment or raw_always_on or raw_queue)
    if pattern == DEPLOYMENT_QUEUED_INGRESS and raw_queue:
        return True, (
            "queued-ingress: independent always-on ingress durably accepts "
            "webhooks at %s and forwards/wakes the controller" % raw_queue
        )
    if pattern == DEPLOYMENT_ALWAYS_ON and (
        raw_always_on in ("1", "true", "yes", "on")
        or raw_deployment == DEPLOYMENT_ALWAYS_ON
    ):
        return True, (
            "always-on-controller: controller never sleeps; Render Free "
            "sleeping service is not the sole receiver"
        )
    return False, (
        "controller deployment not declared always-on: set "
        "CONTROLLER_ALWAYS_ON=true (always-on-controller) or "
        "CONTROLLER_QUEUE_URL=<always-on ingress> (queued-ingress); a "
        "sleeping Render Free web service must not be the sole webhook "
        "receiver because its cold start exceeds the %ds ack budget"
        % WEBHOOK_ACK_BUDGET_SECONDS
    )


# ---------------------------------------------------------------------------
# Webhook signature verification (X-Hub-Signature-256).
# ---------------------------------------------------------------------------

SIGNATURE_HEADER = "x-hub-signature-256"
DELIVERY_HEADER = "x-github-delivery"
EVENT_HEADER = "x-github-event"


def verify_webhook_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Verify the GitHub HMAC-SHA256 webhook signature (fail closed).

    ``header_value`` is the raw ``X-Hub-Signature-256`` header
    (``sha256=<hex>``). Returns False for missing/malformed headers,
    empty secrets, or digest mismatches. Uses ``hmac.compare_digest``.
    """
    if not secret or not isinstance(body, (bytes, bytearray)):
        return False
    if not header_value or not isinstance(header_value, str):
        return False
    value = header_value.strip()
    if not value.startswith("sha256="):
        return False
    presented = value[len("sha256="):].strip().lower()
    if not presented or any(ch not in "0123456789abcdef" for ch in presented):
        return False
    expected = hmac.new(secret.encode("utf-8"), bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


def sign_webhook_body(secret: str, body: bytes) -> str:
    """Build a valid X-Hub-Signature-256 header value (tests/local dev)."""
    digest = hmac.new(secret.encode("utf-8"), bytes(body), hashlib.sha256).hexdigest()
    return "sha256=" + digest


def normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Lowercase header names for case-insensitive lookup."""
    return {str(key).lower(): value for key, value in dict(headers).items()}


# ---------------------------------------------------------------------------
# Scheduling semantics (mirror of issue-scheduler.yml).
# ---------------------------------------------------------------------------

PRIORITY_LABELS = ("priority:p0", "priority:p1", "priority:p2")
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITY_LABELS)}
IN_PROGRESS_LABEL = "automation:in-progress"
PAUSED_LABEL = "automation:paused"
SMOKE_LABEL = "execution:render-smoke"
E2E_LABEL = "execution:render-e2e"

SCHEDULER_DISPATCH_MARKER = "<!-- runtime-lab-scheduler-dispatch -->"
SCHEDULER_PAUSE_MARKER = "<!-- runtime-lab-scheduler-pause -->"

# Command/comment trigger retained by the final design: an issue comment
# containing one of these tokens requests automation for that issue (still
# subject to priority/blocker/pause/WIP eligibility below).
COMMAND_TOKENS = ("/oc", "/opencode")

# Event families the controller accepts (reproduces the NanoDictate issue
# automation semantics): issue lifecycle/label changes plus the
# command/comment trigger plus PR-close observation (releases the
# reservation; never dispatches by itself).
ISSUES_DISPATCH_ACTIONS = frozenset(
    {"opened", "reopened", "labeled", "unlabeled", "edited", "assigned", "unassigned"}
)
ISSUES_OBSERVED_ACTIONS = frozenset({"closed", "deleted", "transferred", "milestoned",
                                     "demilestoned"})
ISSUE_COMMENT_ACTIONS = frozenset({"created"})
PULL_REQUEST_OBSERVED_ACTIONS = frozenset(
    {"closed", "merged", "opened", "reopened", "synchronize", "edited",
     "labeled", "unlabeled"}
)
SUPPORTED_WEBHOOK_EVENTS = frozenset(
    {"ping", "issues", "issue_comment", "pull_request"}
)

DEFAULT_WIP_LIMIT = MAX_CONCURRENT_AUTOMATION_JOBS  # 4
DEFAULT_MAX_DISPATCH_ATTEMPTS = 4
DEFAULT_LEASE_MINUTES = 90

ENV_WIP_LIMIT = "CONTROLLER_WIP_LIMIT"
ENV_MAX_ATTEMPTS = "CONTROLLER_MAX_DISPATCH_ATTEMPTS"
ENV_DEFAULT_REGION = "RENDER_REGION"
ENV_DEFAULT_MODEL = "OPENCODE_MODEL"


def positive_int_env(name: str, fallback: int) -> int:
    """Read a positive-int env var, falling back (and failing closed)."""
    raw = os.environ.get(name, "")
    if raw is None or not str(raw).strip():
        return fallback
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("%s must be a positive integer; got %r" % (name, raw)) from exc
    if value <= 0:
        raise ValueError("%s must be a positive integer; got %r" % (name, raw))
    return value


def resolve_wip_limit(raw: str | None = None) -> int:
    """Controller concurrency default: up to 4 concurrent issue executions."""
    if raw is None:
        raw = os.environ.get(ENV_WIP_LIMIT, "")
    if raw is None or not str(raw).strip():
        return DEFAULT_WIP_LIMIT
    value = int(str(raw).strip())
    if value <= 0:
        raise ValueError("WIP limit must be positive, got %r" % raw)
    return value


def priority_rank(label_names: Sequence[str]) -> int | None:
    """Best (lowest) priority rank present, or None when unprioritized."""
    best: int | None = None
    for name in label_names:
        rank = PRIORITY_RANK.get(name)
        if rank is not None and (best is None or rank < best):
            best = rank
    return best


def rank_issues(issue_numbers_with_labels: Sequence[tuple[int, Sequence[str]]]) -> list[int]:
    """Order issues by priority (p0 first) then issue number (scheduler order)."""
    ranked: list[tuple[int, int]] = []
    for number, labels in issue_numbers_with_labels:
        rank = priority_rank(list(labels))
        if rank is None:
            continue
        ranked.append((rank, number))
    ranked.sort()
    return [number for _, number in ranked]


def readiness_dependency_numbers(body: str | None, own_number: int) -> list[int]:
    """Fallback DAG from Definition of Ready ("#N is completed").

    Mirrors the scheduler fallback: until native GitHub issue dependencies
    are populated, references of the form ``#N is completed`` in the issue
    body are honored as blockers.
    """
    if not body:
        return []
    found: list[int] = []
    seen: set[int] = set()
    for match in re.finditer(r"#(\d+)\s+is completed\b", body, flags=re.IGNORECASE):
        try:
            number = int(match.group(1))
        except ValueError:
            continue
        if number <= 0 or number == own_number or number in seen:
            continue
        seen.add(number)
        found.append(number)
    return found


def is_command_comment(comment_body: str | None) -> bool:
    """True when an issue comment carries the retained command trigger."""
    if not comment_body:
        return False
    lowered = comment_body.lower()
    return any(token in lowered for token in COMMAND_TOKENS)


@dataclass(frozen=True)
class EligibilitySnapshot:
    """Repository state needed for one scheduling decision.

    Normally assembled from the webhook payload (issue labels/state) plus
    the controller-local active set and an optional GitHub read provider
    (native blocked-by, open PRs, dispatch attempts). Pure data: the
    decision itself (``decide_eligible``) has no I/O.
    """

    issue_number: int
    state: str = "open"  # "open" | "closed"
    labels: frozenset[str] = field(default_factory=frozenset)
    title: str = ""
    body: str = ""
    active_count: int = 0
    wip_limit: int = DEFAULT_WIP_LIMIT
    open_blockers: tuple[int, ...] = ()
    open_blocker_fallback_states: Mapping[int, str] = field(default_factory=dict)
    dispatch_attempts: int = 0
    max_attempts: int = DEFAULT_MAX_DISPATCH_ATTEMPTS
    has_open_pr: bool = False
    lease_valid: bool = False

    def execution_mode(self) -> str:
        if SMOKE_LABEL in self.labels:
            return "smoke"
        return "e2e"


@dataclass(frozen=True)
class EligibilityDecision:
    """Outcome of ``decide_eligible``."""

    eligible: bool
    reason: str
    execution_mode: str = "e2e"
    priority: str = ""


def decide_eligible(snapshot: EligibilitySnapshot) -> EligibilityDecision:
    """Apply the scheduler semantics to one issue snapshot (pure, no I/O).

    Mirrors issue-scheduler.yml: paused short-circuits everything, only
    explicitly prioritized work enters the queue, open blockers (native or
    DoR-fallback) defer, an issue with a live reservation (open PR or valid
    lease under automation:in-progress) is already active, the WIP limit
    caps concurrent issues, and attempts beyond the maximum pause instead
    of dispatching.
    """
    labels = set(snapshot.labels)
    mode = snapshot.execution_mode()

    if snapshot.state != "open":
        return EligibilityDecision(False, "issue is not open", mode, "")
    if PAUSED_LABEL in labels:
        return EligibilityDecision(False, "automation:paused is set", mode, "")

    ranked = sorted(
        (label for label in labels if label in PRIORITY_RANK),
        key=lambda name: PRIORITY_RANK[name],
    )
    if not ranked:
        return EligibilityDecision(
            False, "no priority:p0/p1/p2 label", mode, ""
        )
    priority = ranked[0]

    blockers = list(snapshot.open_blockers)
    for number in readiness_dependency_numbers(snapshot.body, snapshot.issue_number):
        if number in blockers:
            continue
        state = snapshot.open_blocker_fallback_states.get(number, "")
        if state == "open" and number not in blockers:
            blockers.append(number)
    if blockers:
        return EligibilityDecision(
            False,
            "blocked by " + ", ".join("#%d" % n for n in sorted(blockers)),
            mode,
            priority,
        )

    if IN_PROGRESS_LABEL in labels and (snapshot.has_open_pr or snapshot.lease_valid):
        return EligibilityDecision(False, "already active (reservation held)", mode, priority)

    if snapshot.active_count >= snapshot.wip_limit:
        return EligibilityDecision(
            False,
            "WIP limit reached (%d/%d)" % (snapshot.active_count, snapshot.wip_limit),
            mode,
            priority,
        )

    attempt = snapshot.dispatch_attempts + 1
    if attempt > snapshot.max_attempts:
        return EligibilityDecision(
            False,
            "maximum of %d dispatch attempts already reached" % snapshot.max_attempts,
            mode,
            priority,
        )

    return EligibilityDecision(True, "eligible (%s, attempt %d)" % (priority, attempt),
                               mode, priority)


@dataclass(frozen=True)
class WebhookEvent:
    """Parsed GitHub webhook envelope."""

    event: str
    action: str = ""
    delivery_id: str = ""
    issue_number: int = 0
    is_pull_request: bool = False
    labels: tuple[str, ...] = ()
    state: str = ""
    title: str = ""
    body: str = ""
    comment_body: str = ""
    sender: str = ""


def parse_webhook_event(event: str, payload: Mapping[str, Any],
                        delivery_id: str = "") -> WebhookEvent:
    """Parse a GitHub webhook payload into a WebhookEvent (no I/O)."""
    action = str(payload.get("action", "") or "")
    sender_obj = payload.get("sender", {})
    sender = sender_obj.get("login", "") if isinstance(sender_obj, Mapping) else ""
    if event == "ping":
        return WebhookEvent(event="ping", delivery_id=delivery_id, sender=sender)
    if event == "issues":
        issue = payload.get("issue", {}) if isinstance(payload.get("issue"), Mapping) else {}
        number = issue.get("number", 0) or 0
        raw_labels = issue.get("labels", []) or []
        labels: list[str] = []
        for item in raw_labels:
            if isinstance(item, str):
                labels.append(item)
            elif isinstance(item, Mapping) and item.get("name"):
                labels.append(str(item["name"]))
        return WebhookEvent(
            event="issues",
            action=action,
            delivery_id=delivery_id,
            issue_number=int(number) if isinstance(number, int) else 0,
            is_pull_request=bool(issue.get("pull_request")),
            labels=tuple(labels),
            state=str(issue.get("state", "") or ""),
            title=str(issue.get("title", "") or ""),
            body=str(issue.get("body", "") or ""),
            sender=sender,
        )
    if event == "issue_comment":
        issue = payload.get("issue", {}) if isinstance(payload.get("issue"), Mapping) else {}
        comment = payload.get("comment", {}) if isinstance(payload.get("comment"), Mapping) else {}
        number = issue.get("number", 0) or 0
        raw_labels = issue.get("labels", []) or []
        labels = []
        for item in raw_labels:
            if isinstance(item, str):
                labels.append(item)
            elif isinstance(item, Mapping) and item.get("name"):
                labels.append(str(item["name"]))
        return WebhookEvent(
            event="issue_comment",
            action=action,
            delivery_id=delivery_id,
            issue_number=int(number) if isinstance(number, int) else 0,
            is_pull_request=bool(issue.get("pull_request")),
            labels=tuple(labels),
            state=str(issue.get("state", "") or ""),
            title=str(issue.get("title", "") or ""),
            body=str(issue.get("body", "") or ""),
            comment_body=str(comment.get("body", "") or ""),
            sender=sender,
        )
    if event == "pull_request":
        pr = payload.get("pull_request", {}) if isinstance(payload.get("pull_request"), Mapping) else {}
        number = payload.get("number", 0) or pr.get("number", 0) or 0
        return WebhookEvent(
            event="pull_request",
            action=action,
            delivery_id=delivery_id,
            issue_number=int(number) if isinstance(number, int) else 0,
            is_pull_request=True,
            state=str(pr.get("state", "") or ""),
            title=str(pr.get("title", "") or ""),
            body=str(pr.get("body", "") or ""),
            sender=sender,
        )
    return WebhookEvent(event=event, action=action, delivery_id=delivery_id, sender=sender)


def event_wants_evaluation(event: WebhookEvent) -> tuple[bool, str]:
    """Decide whether a webhook event merits a scheduling evaluation.

    Returns (wants, reason). Unsupported families and pure observations
    are acknowledged but never dispatch. The retained command trigger is
    ``issue_comment.created`` containing /oc or /opencode on a non-PR
    issue; issue lifecycle actions (opened/reopened/labeled/unlabeled/...)
    always evaluate.
    """
    if event.event == "ping":
        return False, "ping acknowledged"
    if event.event == "issues":
        if event.is_pull_request:
            return False, "pull-request issue event ignored"
        if not event.issue_number:
            return False, "issue event without issue number"
        if event.action in ISSUES_DISPATCH_ACTIONS:
            return True, "issue %s" % event.action
        if event.action in ISSUES_OBSERVED_ACTIONS or not event.action:
            return False, "issue %s observed (no dispatch)" % (event.action or "event")
        return False, "unsupported issue action %r" % event.action
    if event.event == "issue_comment":
        if event.action not in ISSUE_COMMENT_ACTIONS:
            return False, "unsupported issue_comment action %r" % event.action
        if event.is_pull_request or not event.issue_number:
            return False, "comment is not on an issue"
        if is_command_comment(event.comment_body):
            return True, "command comment trigger"
        return False, "comment without command trigger"
    if event.event == "pull_request":
        return False, "pull_request %s observed (no dispatch)" % (event.action or "event")
    return False, "unsupported event %r" % event.event


# ---------------------------------------------------------------------------
# Structured correlation.
# ---------------------------------------------------------------------------

def format_correlation(*, delivery_id: str = "", issue_number: int = 0,
                       worker_service_id: str = "", job_id: str = "") -> str:
    """One-line structured correlation for logs and records."""
    return "delivery=%s issue=%s worker=%s job=%s" % (
        delivery_id or "-",
        ("#%d" % issue_number) if issue_number else "-",
        worker_service_id or "-",
        job_id or "-",
    )


# ---------------------------------------------------------------------------
# Durable delivery store (idempotency by X-GitHub-Delivery).
# ---------------------------------------------------------------------------

DELIVERY_STATUSES = frozenset(
    {"accepted", "processing", "dispatched", "completed", "failed", "ignored"}
)
# Redelivery may re-queue these; dispatched/completed/processing keep their
# single worker (idempotent recovery, never a duplicate dispatch).
REDELIVERABLE_STATUSES = frozenset({"accepted", "failed", "ignored"})


class DeliveryStore:
    """Durable, thread-safe delivery registry keyed by GitHub delivery ID.

    Every accepted webhook is appended to a JSON file BEFORE the 2xx
    response, so a crash/restart between accept and dispatch cannot lose
    it: the controller reloads pending deliveries on startup and the
    redelivery endpoint re-queues them idempotently.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get(
            "CONTROLLER_QUEUE_FILE",
            os.path.join(tempfile.gettempdir(), "runtime-lab-controller-deliveries.json"),
        )
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, ValueError, OSError):
            return
        if isinstance(data, dict):
            with self._lock:
                for key, value in data.items():
                    if isinstance(value, dict):
                        self._records[str(key)] = value

    def _persist_locked(self) -> None:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        tmp_path = self.path + ".tmp-%s" % os.getpid()
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self._records, handle, sort_keys=True)
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def contains(self, delivery_id: str) -> bool:
        with self._lock:
            return delivery_id in self._records

    def get(self, delivery_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(delivery_id)
            return dict(record) if record is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for _, record in sorted(self._records.items())]

    def pending(self) -> list[dict[str, Any]]:
        """Deliveries accepted but not yet terminal (restart recovery set)."""
        with self._lock:
            return [dict(record) for record in self._records.values()
                    if record.get("status") in ("accepted", "processing", "failed")]

    def accept(self, *, delivery_id: str, event: str, action: str,
               issue_number: int, payload: Mapping[str, Any],
               event_obj: WebhookEvent | None = None) -> tuple[dict[str, Any], bool]:
        """Durably record a delivery; duplicates return the original.

        Returns (record, created). Duplicate delivery IDs never create a
        second record and never dispatch a second worker. Scalar event
        fields (labels/state/title/body/comment) are stored alongside the
        payload so ``process_delivery`` can reconstruct the event after a
        restart without refetching anything.
        """
        with self._lock:
            existing = self._records.get(delivery_id)
            if existing is not None:
                return dict(existing), False
            now = time.time()
            record: dict[str, Any] = {
                "delivery_id": delivery_id,
                "event": event,
                "action": action,
                "issue_number": issue_number,
                "status": "accepted",
                "worker_service_id": "",
                "job_id": "",
                "execution_mode": "",
                "reason": "",
                "correlation": format_correlation(
                    delivery_id=delivery_id, issue_number=issue_number),
                "created_at": now,
                "updated_at": now,
            }
            if event_obj is not None:
                record["labels"] = list(event_obj.labels)
                record["issue_state"] = event_obj.state
                record["title"] = event_obj.title[:500]
                record["body"] = event_obj.body[:4000]
                record["comment_body"] = event_obj.comment_body[:4000]
                record["is_pull_request"] = event_obj.is_pull_request
            try:
                summary = json.dumps(payload)[:65536]
            except (TypeError, ValueError):
                summary = ""
            record["payload_summary"] = summary
            self._records[delivery_id] = record
            self._persist_locked()
            return dict(record), True

    def update(self, delivery_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(delivery_id)
            if record is None:
                return None
            for key, value in fields.items():
                record[key] = value
            record["updated_at"] = time.time()
            record["correlation"] = format_correlation(
                delivery_id=delivery_id,
                issue_number=int(record.get("issue_number") or 0),
                worker_service_id=str(record.get("worker_service_id") or ""),
                job_id=str(record.get("job_id") or ""),
            )
            self._persist_locked()
            return dict(record)

    def mark_redeliverable(self, delivery_id: str) -> tuple[dict[str, Any] | None, bool]:
        """Re-queue a delivery for recovery; idempotent for dispatched ones.

        Returns (record, requeued). Completed/dispatched/processing
        deliveries return requeued=False (the original worker stands).
        """
        with self._lock:
            record = self._records.get(delivery_id)
            if record is None:
                return None, False
            if record.get("status") not in REDELIVERABLE_STATUSES:
                return dict(record), False
            record["status"] = "accepted"
            record["reason"] = "redelivered for recovery"
            record["updated_at"] = time.time()
            self._persist_locked()
            return dict(record), True


# ---------------------------------------------------------------------------
# Render / runner client interfaces (injectable; stdlib default included).
# ---------------------------------------------------------------------------

class RenderWorkerClient:
    """Interface the controller uses for the ephemeral-worker lifecycle."""

    def create_service(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create one worker; return {service_id, deploy_id, plan}."""
        raise NotImplementedError

    def get_service(self, service_id: str) -> dict[str, Any]:
        """Return the Render service object (must include serviceDetails)."""
        raise NotImplementedError

    def get_deploy(self, service_id: str, deploy_id: str) -> dict[str, Any]:
        """Return the deploy object (must include status)."""
        raise NotImplementedError

    def service_url(self, service: Mapping[str, Any]) -> str:
        """Externally reachable worker URL (https)."""
        try:  # pragma: no cover - import path depends on entrypoint
            from automation.render_lifecycle import (
                get_service_url as _get_service_url,
            )
        except ImportError:
            from render_lifecycle import (  # type: ignore[no-redef]
                get_service_url as _get_service_url,
            )
        return _get_service_url(service)

    def delete_service(self, service_id: str) -> int:
        """DELETE the worker; return the HTTP status."""
        raise NotImplementedError

    def verify_gone(self, service_id: str) -> int:
        """GET the worker; return HTTP status (404/410 proves deletion)."""
        raise NotImplementedError

    def suspend_service(self, service_id: str) -> int:
        """Emergency suspend fallback; return HTTP status."""
        raise NotImplementedError


class RunnerJobClient:
    """Interface the controller uses to drive the worker's runner API."""

    def wait_healthy(self, base_url: str) -> None:
        raise NotImplementedError

    def submit_job(self, base_url: str, job_body: Mapping[str, Any]) -> str:
        """Submit an OpenCode job; return the job id."""
        raise NotImplementedError

    def get_result(self, base_url: str, job_id: str) -> Mapping[str, Any]:
        """Poll one job result payload."""
        raise NotImplementedError


class IssueSnapshotProvider:
    """GitHub reads enriching the webhook payload (optional for #10).

    The real GitHub App read client lands in #11; until then the
    controller decides from the webhook payload plus its local active set,
    with an injectable provider (or explicit overrides) for blockers, open
    PRs, leases and attempt counts in tests and queued-ingress forwards.
    """

    def open_blockers(self, issue_number: int) -> list[int]:
        return []

    def open_issue_states(self, numbers: Sequence[int]) -> dict[int, str]:
        return {}

    def has_open_pr(self, issue_number: int) -> bool:
        return False

    def lease_valid(self, issue_number: int) -> bool:
        return False

    def dispatch_attempts(self, issue_number: int) -> int:
        return 0

    def active_count(self) -> int:
        return 0


class StaticSnapshotProvider(IssueSnapshotProvider):
    """In-memory provider for tests and offline operation."""

    def __init__(self, *, blockers: Mapping[int, Sequence[int]] | None = None,
                 open_states: Mapping[int, str] | None = None,
                 open_prs: Sequence[int] = (), leased: Sequence[int] = (),
                 attempts: Mapping[int, int] | None = None,
                 active: int = 0) -> None:
        self._blockers = {int(k): list(v) for k, v in dict(blockers or {}).items()}
        self._open_states = {int(k): v for k, v in dict(open_states or {}).items()}
        self._open_prs = set(open_prs)
        self._leased = set(leased)
        self._attempts = {int(k): v for k, v in dict(attempts or {}).items()}
        self._active = active

    def open_blockers(self, issue_number: int) -> list[int]:
        return list(self._blockers.get(issue_number, []))

    def open_issue_states(self, numbers: Sequence[int]) -> dict[int, str]:
        return {n: self._open_states[n] for n in numbers if n in self._open_states}

    def has_open_pr(self, issue_number: int) -> bool:
        return issue_number in self._open_prs

    def lease_valid(self, issue_number: int) -> bool:
        return issue_number in self._leased

    def dispatch_attempts(self, issue_number: int) -> int:
        return self._attempts.get(issue_number, 0)

    def active_count(self) -> int:
        return self._active


def build_snapshot(event: WebhookEvent, *,
                   provider: IssueSnapshotProvider | None = None,
                   wip_limit: int = DEFAULT_WIP_LIMIT,
                   max_attempts: int = DEFAULT_MAX_DISPATCH_ATTEMPTS,
                   active_count: int | None = None) -> EligibilitySnapshot:
    """Assemble an EligibilitySnapshot for one webhook event."""
    provider = provider or StaticSnapshotProvider()
    blockers = provider.open_blockers(event.issue_number) if event.issue_number else []
    fallback_numbers = readiness_dependency_numbers(event.body, event.issue_number)
    states = provider.open_issue_states(fallback_numbers) if fallback_numbers else {}
    return EligibilitySnapshot(
        issue_number=event.issue_number,
        state=(event.state or "open") if event.event in ("issues", "issue_comment") else "open",
        labels=frozenset(event.labels),
        title=event.title,
        body=event.body,
        active_count=provider.active_count() if active_count is None else active_count,
        wip_limit=wip_limit,
        open_blockers=tuple(blockers),
        open_blocker_fallback_states=dict(states),
        dispatch_attempts=provider.dispatch_attempts(event.issue_number),
        max_attempts=max_attempts,
        has_open_pr=provider.has_open_pr(event.issue_number),
        lease_valid=provider.lease_valid(event.issue_number),
    )


# ---------------------------------------------------------------------------
# Ephemeral-worker execution (create -> healthy -> job -> result ->
# delete -> verify), reusing automation/render_lifecycle.py.
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    """Outcome of one issue execution attempt."""

    ok: bool
    issue_number: int
    delivery_id: str = ""
    worker_service_id: str = ""
    job_id: str = ""
    execution_mode: str = "e2e"
    region: str = DEFAULT_WORKER_REGION
    model: str = PREFERRED_MODEL
    status: str = ""
    reason: str = ""
    delete_status: int | None = None
    verify_status: int | None = None
    cleanup_verified: bool = False
    # Full terminal runner payload (job_id/status/changes/...), attached so
    # the controller can hand it to the GitHub write-back stage (#11)
    # without re-contacting the (already deleted) worker.
    result_payload: dict[str, Any] = field(default_factory=dict)
    # GitHub write-back proof (filled by Controller when a writeback
    # factory is configured): action/branch/pr_number/dispatched_ci.
    writeback_action: str = ""
    writeback_branch: str = ""
    writeback_pr: int | None = None
    writeback_ci_dispatched: bool = False
    writeback_error: str = ""


def cleanup_worker(client: RenderWorkerClient, service_id: str) -> tuple[int | None, int | None, bool]:
    """Mandatory delete + verify with suspend fallback (bounded).

    Mirrors automation/render-cleanup.sh: DELETE is primary (up to
    DELETE_MAX_ATTEMPTS), 404/410 on DELETE already means gone, then GET
    must prove absence (404/410); only then, as an emergency fallback,
    suspend (bounded) and retry deletion within bounds. Never leaves the
    temporary service behind on success.
    """
    if not service_id:
        return None, None, True
    delete_status: int | None = None
    for _ in range(max(1, DELETE_MAX_ATTEMPTS)):
        try:
            delete_status = client.delete_service(service_id)
        except Exception:
            delete_status = 0
        if delete_status in (204, 404, 410):
            break
    try:
        verify_status: int | None = client.verify_gone(service_id)
    except Exception:
        verify_status = 0
    if verify_status is not None and is_deletion_verified(verify_status):
        return delete_status, verify_status, True
    # Emergency fallback only: suspend to stop burn, then retry deletion.
    for _ in range(max(1, SUSPEND_FALLBACK_MAX_ATTEMPTS)):
        try:
            suspend_status = client.suspend_service(service_id)
        except Exception:
            suspend_status = 0
        if suspend_status in (202, 404, 410):
            break
    for _ in range(max(1, DELETE_MAX_ATTEMPTS)):
        try:
            delete_status = client.delete_service(service_id)
        except Exception:
            delete_status = 0
        if delete_status in (204, 404, 410):
            break
    try:
        verify_status = client.verify_gone(service_id)
    except Exception:
        verify_status = 0
    ok = deletion_succeeded(delete_status, verify_status)
    return delete_status, verify_status, ok


def _poll_deploy_live(client: RenderWorkerClient, service_id: str,
                      deploy_id: str) -> None:
    """Wait for the deploy to become live (bounded, same service)."""
    if not deploy_id:
        return
    attempts = max(1, DEPLOY_POLL_MAX_ATTEMPTS)
    for index in range(1, attempts + 1):
        deploy = client.get_deploy(service_id, deploy_id)
        status = str(deploy.get("status", "") or "")
        kind = classify_deploy_status(status)
        if kind == "live":
            return
        if kind == "failed":
            raise RuntimeError("render deploy %s failed with status %r" % (deploy_id, status))
        if index >= attempts:
            raise TimeoutError("render deploy %s did not go live in time" % deploy_id)
        time.sleep(DEPLOY_POLL_INTERVAL_SECONDS)


def _poll_job_terminal(runner: RunnerJobClient, base_url: str,
                       job_id: str) -> Mapping[str, Any]:
    """Poll one runner job until a terminal status (bounded, same worker)."""
    attempts = max(1, JOB_POLL_MAX_ATTEMPTS)
    last: Mapping[str, Any] = {}
    for index in range(1, attempts + 1):
        payload = runner.get_result(base_url, job_id)
        last = payload
        result = parse_job_result(payload)
        if is_terminal_job_status(result.status):
            return payload
        if index >= attempts:
            raise TimeoutError("runner job %s did not finish in time" % job_id)
        time.sleep(JOB_POLL_INTERVAL_SECONDS)
    return last


def execute_issue_attempt(
    *,
    delivery_id: str,
    issue_number: int,
    execution_mode: str,
    title: str = "",
    body: str = "",
    base_sha: str = "",
    region: str = DEFAULT_WORKER_REGION,
    model: str = PREFERRED_MODEL,
    owner_id: str = "",
    run_id: str = "",
    render_client: RenderWorkerClient,
    runner_client: RunnerJobClient,
    health_attempts: int | None = None,
) -> DispatchResult:
    """Run one eligible issue on exactly one free ephemeral worker.

    Lifecycle (in order): validate region/model/mode BEFORE any creation;
    create exactly one free worker; wait for deploy live + runner health;
    submit the OpenCode job; collect the terminal result (with one
    same-worker model fallback when the preferred Muse model is
    unavailable); unconditionally delete the worker and verify deletion.
    The controller never executes OpenCode itself: all OpenCode work
    happens inside the ephemeral worker via its runner API.
    """
    validate_execution_mode(execution_mode)
    validate_worker_region(region)
    validate_model_name(model)
    if issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")
    if not owner_id:
        raise ValueError("owner_id (Render workspace id) must not be empty")

    label_run = run_id or ("ctrl-%s" % (delivery_id[:8] if delivery_id else uuid.uuid4().hex[:8]))
    service_name = service_name_for_attempt(issue_number, label_run)
    payload = build_create_service_payload(
        name=service_name, owner_id=owner_id, region=region)
    creations = 1
    if creations > MAX_SERVICE_CREATIONS_PER_ATTEMPT:
        raise ValueError("attempt would create more than one service")
    _ = MAX_SERVICES_PER_ISSUE_ATTEMPT  # one issue attempt owns at most one worker

    service_id = ""
    created: dict[str, Any] = {}
    cleanup: tuple[int | None, int | None, bool] = (None, None, True)
    outcome: DispatchResult | None = None
    try:
        created = render_client.create_service(payload)
        service_id = str(created.get("service_id", "") or "")
        if not service_id:
            raise RuntimeError("render service creation returned no service id")
        plan = str(created.get("plan", "") or "")
        if plan:
            verify_free_plan_response({"serviceDetails": {"plan": plan}})
        deploy_id = str(created.get("deploy_id", "") or "")

        _poll_deploy_live(render_client, service_id, deploy_id)

        service = render_client.get_service(service_id)
        service_details = service.get("serviceDetails", service)
        if isinstance(service_details, Mapping) and service_details.get("plan"):
            verify_free_plan_response({"serviceDetails": {"plan": service_details["plan"]}})
        base_url = render_client.service_url(service)

        healthy_attempts = max(1, health_attempts or RUNNER_HEALTH_MAX_ATTEMPTS)
        healthy_interval = RUNNER_HEALTH_INTERVAL_SECONDS
        last_error: Exception | None = None
        for _ in range(healthy_attempts):
            try:
                runner_client.wait_healthy(base_url)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(healthy_interval)
        if last_error is not None:
            raise RuntimeError("runner health check failed: %s" % last_error)

        task_text = resolve_task_text(issue_number, execution_mode, title=title, body=body)
        metadata = ExecutionMetadata(
            issue_number=issue_number,
            attempt=1,
            run_id=label_run,
            region=region,
            model=model,
            execution_mode=execution_mode,
        )
        request = JobRequest(
            repository_url=PUBLIC_REPO_URL,
            base_ref=PUBLIC_REPO_BRANCH,
            base_sha=select_base_sha(base_sha, ""),
            task_text=task_text,
            issue_number=issue_number,
            metadata=metadata,
        )
        job_body = request.to_dict()
        job_id = runner_client.submit_job(base_url, job_body)
        if not job_id:
            raise RuntimeError("runner did not return a job identifier")
        result_payload = _poll_job_terminal(runner_client, base_url, job_id)
        result = parse_job_result(result_payload)

        # Same-worker model fallback: when the preferred Muse model fails
        # for an availability reason, retry once with Space Bunny on the
        # SAME worker (never a second Render service).
        if (result.status == "failed" and model == PREFERRED_MODEL
                and is_model_unavailable_error(
                    str(result.error or "") + "\n" + str(result.summary or ""))):
            fallback_body = json.loads(json.dumps(job_body))
            if isinstance(fallback_body.get("metadata"), dict):
                fallback_body["metadata"]["model"] = FALLBACK_MODEL
            else:
                fallback_body["metadata"] = {"model": FALLBACK_MODEL}
            fallback_id = runner_client.submit_job(base_url, fallback_body)
            if fallback_id:
                job_id = fallback_id
                result_payload = _poll_job_terminal(runner_client, base_url, job_id)
                result = parse_job_result(result_payload)

        outcome = DispatchResult(
            ok=result.status == "succeeded",
            issue_number=issue_number,
            delivery_id=delivery_id,
            worker_service_id=service_id,
            job_id=job_id,
            execution_mode=execution_mode,
            region=region,
            model=model,
            status=result.status,
            reason=str(result.summary or result.error or "")[:500],
            result_payload=dict(result_payload) if isinstance(
                result_payload, Mapping) else {},
        )
        return outcome
    finally:
        if service_id:
            # Mandatory cleanup with verification (delete primary, suspend
            # only as emergency fallback, deletion retried within bounds).
            cleanup = cleanup_worker(render_client, service_id)
        if outcome is not None:
            outcome.delete_status, outcome.verify_status, outcome.cleanup_verified = cleanup
            if not outcome.cleanup_verified:
                outcome.ok = False
                outcome.status = "cleanup_failed"
                cleanup_reason = (
                    "mandatory ephemeral worker deletion was not verified "
                    "(delete=%s verify=%s)"
                    % (outcome.delete_status, outcome.verify_status)
                )
                outcome.reason = (
                    (outcome.reason + "; " + cleanup_reason)
                    if outcome.reason else cleanup_reason
                )
        # Attach cleanup proof to the in-flight result via the store update
        # performed by the caller (Controller.process_delivery).


# ---------------------------------------------------------------------------
# Controller: idempotent ingress + concurrency + dispatch ownership.
# ---------------------------------------------------------------------------

_WRITEBACK_SECRET_PATTERNS = None  # compiled lazily via github_app when present


def _redact_controller_error(exc: BaseException) -> str:
    """Redact secret material from a dispatch/write-back failure reason."""
    try:  # pragma: no cover - import path depends on entrypoint
        from automation.github_app import redact_secrets as _redact
    except ImportError:
        try:
            from github_app import redact_secrets as _redact  # type: ignore[no-redef]
        except ImportError:
            _redact = None  # type: ignore[assignment]
    text = "%s: %s" % (type(exc).__name__, exc)
    if _redact is not None:
        try:
            return _redact(text)[:500]
        except Exception:
            pass
    import re as _re

    redacted = _re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+", "[redacted]", text)
    redacted = _re.sub(r"gh[pousr]_[A-Za-z0-9]+", "[redacted]", redacted)
    redacted = _re.sub(r"ghs_[A-Za-z0-9]+", "[redacted]", redacted)
    redacted = _re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
                       "[redacted]", redacted)
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(marker in upper for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
            continue
        if isinstance(value, str) and len(value) >= 4 and value in redacted:
            redacted = redacted.replace(value, "[redacted]")
    return redacted[:500]


def _redact_text(text: str) -> str:
    """Redact secret material from an arbitrary diagnostic string."""
    try:  # pragma: no cover - import path depends on entrypoint
        from automation.github_app import redact_secrets as _redact
    except ImportError:
        try:
            from github_app import redact_secrets as _redact  # type: ignore[no-redef]
        except ImportError:
            _redact = None  # type: ignore[assignment]
    raw = str(text or "")
    if _redact is not None:
        try:
            return _redact(raw)[:500]
        except Exception:
            pass
    return _redact_controller_error(RuntimeError(raw))[:500]


def _best_effort_reserve(github_api: Any | None, issue: int) -> None:
    """Apply the automation:in-progress reservation label (never blocks)."""
    if github_api is None or issue <= 0:
        return
    try:
        add = getattr(github_api, "add_labels", None)
        if callable(add):
            add(issue, ["automation:in-progress"])
            return
        reserve = getattr(github_api, "reserve_issue", None)
        if callable(reserve):
            reserve(issue)
    except Exception:
        pass


def _best_effort_release(github_api: Any | None, issue: int) -> None:
    """Remove the reservation label (never blocks dispatch/cleanup)."""
    if github_api is None or issue <= 0:
        return
    try:
        remove = getattr(github_api, "remove_label", None)
        if callable(remove):
            remove(issue, "automation:in-progress")
            return
        release = getattr(github_api, "release_reservation", None)
        if callable(release):
            release(issue)
    except Exception:
        pass


class Controller:
    """Owns webhook acceptance, scheduling and worker dispatch.

    Concurrency: up to ``max_concurrent`` eligible issue executions run at
    once (default 4, mirroring the scheduler WIP limit). There is NO global
    single-job mutex: different issues run concurrently, while one issue
    attempt owns at most one worker (per-issue in-flight guard) and one
    GitHub delivery dispatches at most once (store idempotency).
    """

    def __init__(
        self,
        *,
        webhook_secret: str = "",
        store: DeliveryStore | None = None,
        provider: IssueSnapshotProvider | None = None,
        render_client: RenderWorkerClient | None = None,
        runner_client: RunnerJobClient | None = None,
        owner_id: str = "",
        region: str = DEFAULT_WORKER_REGION,
        model: str = PREFERRED_MODEL,
        wip_limit: int = DEFAULT_WIP_LIMIT,
        max_attempts: int = DEFAULT_MAX_DISPATCH_ATTEMPTS,
        max_concurrent: int = MAX_CONCURRENT_AUTOMATION_JOBS,
        writeback_factory: Any | None = None,
        github_api: Any | None = None,
    ) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        if wip_limit <= 0:
            raise ValueError("wip_limit must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.webhook_secret = webhook_secret or os.environ.get(
            "GITHUB_WEBHOOK_SECRET", "")
        self.store = store or DeliveryStore()
        self.provider = provider or StaticSnapshotProvider()
        self.render_client = render_client
        self.runner_client = runner_client
        self.owner_id = owner_id or os.environ.get("RENDER_OWNER_ID", "")
        self.region = (region or os.environ.get(ENV_DEFAULT_REGION,
                                                DEFAULT_WORKER_REGION)).strip().lower() \
            or DEFAULT_WORKER_REGION
        self.model = (model or os.environ.get(ENV_DEFAULT_MODEL,
                                              PREFERRED_MODEL)).strip() or PREFERRED_MODEL
        self.wip_limit = wip_limit
        self.max_attempts = max_attempts
        self.max_concurrent = max_concurrent
        # Issue #11: GitHub App write-back from the Render controller.
        # ``writeback_factory`` maps an issue number to a #5-compatible
        # WritebackClient (App installation-token implementation in
        # automation/github_app.py), or returns None when GitHub writes are
        # disabled. ``github_api`` is the optional installation-token API
        # client used for best-effort reservation labels. Both are
        # optional: without them the controller still dispatches workers
        # and verifies cleanup exactly as in #10.
        self.writeback_factory = writeback_factory
        self.github_api = github_api
        self.deployment_pattern = resolve_deployment_pattern()
        self._slots = threading.Semaphore(max_concurrent)
        self._lock = threading.Lock()
        self._in_flight_issues: dict[int, str] = {}  # issue -> delivery_id
        self._active_count = 0

    # -- introspection ----------------------------------------------------

    def in_flight(self) -> dict[int, str]:
        with self._lock:
            return dict(self._in_flight_issues)

    def queue_depth(self) -> int:
        return len(self.store.pending())

    # -- ingress (fast path: no network I/O before durable accept) --------

    def ingest(self, *, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        """Validate and durably accept one webhook delivery (fast ack path).

        Returns (http_status, response_body). Performs signature
        verification, JSON parsing, duplicate detection and one durable
        store append only -- never Render/GitHub calls -- so the caller
        can respond within the ack budget. Returns 401 for invalid
        signatures, 400 for malformed bodies, 200 for duplicates, 202 for
        newly accepted deliveries.
        """
        lowered = normalize_headers(headers)
        delivery_id = str(lowered.get(DELIVERY_HEADER, "") or "").strip()
        event_name = str(lowered.get(EVENT_HEADER, "") or "").strip().lower()
        if not delivery_id:
            return 400, {"error": "missing X-GitHub-Delivery"}
        if not event_name:
            return 400, {"error": "missing X-GitHub-Event"}
        if event_name not in SUPPORTED_WEBHOOK_EVENTS:
            return 400, {"error": "unsupported event %r" % event_name}
        if not verify_webhook_signature(
                self.webhook_secret, body, lowered.get(SIGNATURE_HEADER)):
            return 401, {"error": "invalid webhook signature"}

        existing = self.store.get(delivery_id)
        if existing is not None:
            return 200, {"ok": True, "duplicate": True,
                         "delivery_id": delivery_id,
                         "status": existing.get("status", "")}

        try:
            payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        except (ValueError, UnicodeDecodeError):
            return 400, {"error": "request body must be valid JSON"}
        if not isinstance(payload, Mapping):
            return 400, {"error": "webhook payload must be a JSON object"}

        event = parse_webhook_event(event_name, payload, delivery_id)
        record, created = self.store.accept(
            delivery_id=delivery_id, event=event.event, action=event.action,
            issue_number=event.issue_number, payload=payload, event_obj=event)
        if not created:
            return 200, {"ok": True, "duplicate": True, "delivery_id": delivery_id,
                         "status": record.get("status", "")}
        return 202, {"ok": True, "duplicate": False, "delivery_id": delivery_id,
                     "event": event.event, "issue_number": event.issue_number}

    def redeliver(self, delivery_id: str) -> tuple[int, dict[str, Any]]:
        """Recovery endpoint: re-queue a delivery idempotently.

        Returns 404 for unknown IDs, 200 with ``duplicate: True`` when the
        delivery already dispatched (the original worker stands -- never a
        second worker), and 202 with ``requeued: True`` when it may be
        processed (again). Failed GitHub deliveries that never reached
        durable acceptance are recovered by GitHub redelivering the same
        delivery ID through the webhook path (which ``ingest`` dedupes).
        """
        record = self.store.get(delivery_id)
        if record is None:
            return 404, {"error": "unknown delivery %r" % delivery_id}
        updated, requeued = self.store.mark_redeliverable(delivery_id)
        if not requeued:
            return 200, {"ok": True, "duplicate": True, "delivery_id": delivery_id,
                         "status": (updated or {}).get("status", "")}
        return 202, {"ok": True, "requeued": True, "delivery_id": delivery_id,
                     "status": "accepted"}

    # -- background processing (eligibility + dispatch) --------------------

    def _active_snapshot_count(self) -> int:
        with self._lock:
            local_active = self._active_count
        try:
            provider_active = self.provider.active_count()
        except Exception:
            provider_active = 0
        return max(local_active, provider_active)

    def process_delivery(self, delivery_id: str) -> dict[str, Any]:
        """Evaluate and (when eligible) dispatch one accepted delivery.

        Idempotent per delivery: a delivery that already reached a
        terminal dispatch state is not dispatched twice. Eligible issues
        acquire one concurrency slot (max 4) and one per-issue guard;
        different issues proceed concurrently.
        """
        record = self.store.get(delivery_id)
        if record is None:
            return {"delivery_id": delivery_id, "processed": False,
                    "reason": "unknown delivery"}
        if record.get("status") not in ("accepted", "failed"):
            return {"delivery_id": delivery_id, "processed": False,
                    "duplicate": True, "status": record.get("status", "")}

        self.store.update(delivery_id, status="processing")

        event = self._event_from_record(record)
        outcome = self.process_event(event, payload_full=None, delivery_id=delivery_id)
        return outcome

    @staticmethod
    def _event_from_record(record: Mapping[str, Any]) -> WebhookEvent:
        """Reconstruct a WebhookEvent from a durable store record.

        Prefers the full stored payload when it survived intact; falls
        back to the scalar event fields persisted at accept time so a
        restart never loses the scheduling inputs.
        """
        delivery_id = str(record.get("delivery_id", "") or "")
        event_name = str(record.get("event", "") or "")
        raw_payload = record.get("payload_summary", "") or ""
        try:
            payload = json.loads(raw_payload) if raw_payload else None
        except ValueError:
            payload = None
        if isinstance(payload, Mapping) and payload.get("issue") is not None:
            try:
                return parse_webhook_event(event_name, payload, delivery_id)
            except Exception:
                pass
        labels = record.get("labels", [])
        return WebhookEvent(
            event=event_name,
            action=str(record.get("action", "") or ""),
            delivery_id=delivery_id,
            issue_number=int(record.get("issue_number") or 0),
            is_pull_request=bool(record.get("is_pull_request", False)),
            labels=tuple(str(item) for item in labels) if isinstance(labels, list) else (),
            state=str(record.get("issue_state", "") or ""),
            title=str(record.get("title", "") or ""),
            body=str(record.get("body", "") or ""),
            comment_body=str(record.get("comment_body", "") or ""),
        )

    def process_event(self, event: WebhookEvent,
                      payload_full: Mapping[str, Any] | None = None,
                      delivery_id: str = "") -> dict[str, Any]:
        """Evaluate one parsed event and dispatch when eligible.

        ``payload_full`` (when available from the live request path) feeds
        title/body/labels; otherwise the event's own fields are used.
        """
        delivery = delivery_id or event.delivery_id
        if payload_full is not None:
            event = parse_webhook_event(event.event, payload_full, delivery)

        wants, want_reason = event_wants_evaluation(event)
        if not wants:
            if delivery:
                self.store.update(delivery, status="ignored", reason=want_reason)
            return {"delivery_id": delivery, "processed": True,
                    "dispatched": False, "reason": want_reason}

        snapshot = build_snapshot(
            event, provider=self.provider, wip_limit=self.wip_limit,
            max_attempts=self.max_attempts,
            active_count=self._active_snapshot_count(),
        )
        decision = decide_eligible(snapshot)
        if not decision.eligible:
            if delivery:
                self.store.update(delivery, status="ignored", reason=decision.reason,
                                  execution_mode=decision.execution_mode)
            return {"delivery_id": delivery, "processed": True,
                    "dispatched": False, "reason": decision.reason}

        return self._dispatch_eligible(event, snapshot, decision, delivery)

    def _dispatch_eligible(self, event: WebhookEvent, snapshot: EligibilitySnapshot,
                           decision: EligibilityDecision, delivery: str) -> dict[str, Any]:
        issue = event.issue_number
        with self._lock:
            if issue in self._in_flight_issues:
                if delivery:
                    self.store.update(
                        delivery, status="ignored",
                        reason="duplicate dispatch for issue #%d (owned by %s)"
                        % (issue, self._in_flight_issues[issue]),
                        execution_mode=decision.execution_mode)
                return {"delivery_id": delivery, "processed": True,
                        "dispatched": False,
                        "reason": "issue already has an active worker"}
            self._in_flight_issues[issue] = delivery
            self._active_count += 1

        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._in_flight_issues.pop(issue, None)
                self._active_count = max(0, self._active_count - 1)
            if delivery:
                self.store.update(delivery, status="accepted",
                                  reason="concurrency full (%d); deferred"
                                  % self.max_concurrent,
                                  execution_mode=decision.execution_mode)
            return {"delivery_id": delivery, "processed": True,
                    "dispatched": False, "reason": "concurrency full; deferred"}

        try:
            if self.render_client is None or self.runner_client is None:
                if delivery:
                    self.store.update(
                        delivery, status="failed",
                        reason="controller has no worker clients configured",
                        execution_mode=decision.execution_mode)
                return {"delivery_id": delivery, "processed": True,
                        "dispatched": False,
                        "reason": "no worker clients configured"}
            if not self.owner_id:
                if delivery:
                    self.store.update(
                        delivery, status="failed",
                        reason="RENDER_OWNER_ID is not configured",
                        execution_mode=decision.execution_mode)
                return {"delivery_id": delivery, "processed": True,
                        "dispatched": False,
                        "reason": "render owner id not configured"}

            if delivery:
                self.store.update(delivery, status="dispatched",
                                  execution_mode=decision.execution_mode,
                                  reason="dispatching %s" % decision.reason)
            region = self.region
            model = self.model
            # Best-effort reservation label before the worker starts; label
            # failures never block dispatch or cleanup.
            _best_effort_reserve(self.github_api, issue)
            result = execute_issue_attempt(
                delivery_id=delivery,
                issue_number=issue,
                execution_mode=decision.execution_mode,
                title=event.title,
                body=event.body,
                base_sha="",
                region=region,
                model=model,
                owner_id=self.owner_id,
                run_id="ctrl-%s" % (delivery[:8] if delivery else uuid.uuid4().hex[:8]),
                render_client=self.render_client,
                runner_client=self.runner_client,
            )
            # Cleanup verification is a hard success gate. A successful
            # OpenCode result must never be materialized into GitHub while
            # its ephemeral Render worker may still exist.
            if not result.cleanup_verified:
                redacted_detail = _redact_text(result.reason)
                if delivery:
                    self.store.update(
                        delivery,
                        status="failed",
                        worker_service_id=result.worker_service_id,
                        job_id=result.job_id,
                        execution_mode=result.execution_mode,
                        reason="mandatory worker cleanup was not verified: %s"
                        % redacted_detail,
                    )
                _best_effort_release(self.github_api, issue)
                return {"delivery_id": delivery, "processed": True,
                        "dispatched": True, "ok": False,
                        "worker_service_id": result.worker_service_id,
                        "job_id": result.job_id,
                        "cleanup_verified": False,
                        "correlation": format_correlation(
                            delivery_id=delivery, issue_number=issue,
                            worker_service_id=result.worker_service_id,
                            job_id=result.job_id)}
            # execute_issue_attempt guarantees deletion+verification in its
            # finally path, so the worker is already gone before any
            # GitHub write-back below runs. Write-back failures therefore
            # never leak a worker and never expose secrets (redacted).
            writeback_summary = ""
            if self.writeback_factory is not None:
                try:
                    client = self.writeback_factory(issue)
                except Exception as exc:
                    redacted = _redact_controller_error(exc)
                    result.writeback_error = redacted
                    if delivery:
                        self.store.update(
                            delivery,
                            status="failed",
                            worker_service_id=result.worker_service_id,
                            job_id=result.job_id,
                            execution_mode=result.execution_mode,
                            reason="github auth failed (worker cleaned up): %s"
                            % redacted,
                        )
                    _best_effort_release(self.github_api, issue)
                    return {"delivery_id": delivery, "processed": True,
                            "dispatched": True, "ok": False,
                            "worker_service_id": result.worker_service_id,
                            "job_id": result.job_id,
                            "cleanup_verified": result.cleanup_verified,
                            "writeback_error": redacted,
                            "correlation": format_correlation(
                                delivery_id=delivery, issue_number=issue,
                                worker_service_id=result.worker_service_id,
                                job_id=result.job_id)}
                if client is not None and result.result_payload:
                    try:
                        try:
                            from automation.result_materialize import (
                                materialize_result as _materialize,
                            )
                        except ImportError:
                            from result_materialize import (  # type: ignore[no-redef]
                                materialize_result as _materialize,
                            )
                        payload = dict(result.result_payload)
                        payload.setdefault("issue_number", issue)
                        metadata = payload.get("metadata")
                        if isinstance(metadata, dict):
                            metadata = dict(metadata)
                            metadata.setdefault("issue_number", issue)
                            payload["metadata"] = metadata
                        else:
                            payload["metadata"] = {"issue_number": issue}
                        suffix = "ctrl-%s" % (
                            delivery[:12] if delivery else uuid.uuid4().hex[:12])
                        outcome = _materialize(
                            payload, client=client,
                            unique_suffix=re.sub(r"[^A-Za-z0-9._-]",
                                                "-", suffix).strip("-") or "ctrl",
                            issue_title=event.title,
                        )
                        result.writeback_action = outcome.action
                        result.writeback_branch = outcome.branch
                        result.writeback_pr = outcome.pr_number
                        result.writeback_ci_dispatched = outcome.dispatched_ci
                        writeback_summary = " writeback=%s branch=%s pr=%s" % (
                            outcome.action, outcome.branch or "-",
                            outcome.pr_number if outcome.pr_number is not None else "-")
                        if outcome.action in ("created", "updated"):
                            pass  # reservation held by the open PR
                        else:
                            _best_effort_release(self.github_api, issue)
                    except Exception as exc:
                        redacted = _redact_controller_error(exc)
                        result.writeback_error = redacted
                        if delivery:
                            self.store.update(
                                delivery,
                                status="failed",
                                worker_service_id=result.worker_service_id,
                                job_id=result.job_id,
                                execution_mode=result.execution_mode,
                                reason="github write-back failed (worker cleaned up): %s%s"
                                % (redacted, writeback_summary),
                            )
                        _best_effort_release(self.github_api, issue)
                        return {"delivery_id": delivery, "processed": True,
                                "dispatched": True, "ok": False,
                                "worker_service_id": result.worker_service_id,
                                "job_id": result.job_id,
                                "cleanup_verified": result.cleanup_verified,
                                "writeback_error": redacted,
                                "correlation": format_correlation(
                                    delivery_id=delivery, issue_number=issue,
                                    worker_service_id=result.worker_service_id,
                                    job_id=result.job_id)}
            # execute_issue_attempt guarantees deletion+verification in its
            # finally path; record the correlation here.
            if delivery:
                self.store.update(
                    delivery,
                    status="completed" if result.ok else "failed",
                    worker_service_id=result.worker_service_id,
                    job_id=result.job_id,
                    execution_mode=result.execution_mode,
                    reason="%s worker=%s job=%s%s" % (
                        result.status or ("ok" if result.ok else "failed"),
                        result.worker_service_id or "-",
                        result.job_id or "-",
                        writeback_summary),
                )
            return {"delivery_id": delivery, "processed": True,
                    "dispatched": True, "ok": result.ok,
                    "worker_service_id": result.worker_service_id,
                    "job_id": result.job_id,
                    "cleanup_verified": result.cleanup_verified,
                    "writeback_action": result.writeback_action,
                    "writeback_branch": result.writeback_branch,
                    "writeback_pr": result.writeback_pr,
                    "correlation": format_correlation(
                        delivery_id=delivery, issue_number=issue,
                        worker_service_id=result.worker_service_id,
                        job_id=result.job_id)}
        except Exception as exc:
            redacted = _redact_controller_error(exc)
            if delivery:
                self.store.update(delivery, status="failed",
                                  reason="dispatch failed: %s" % redacted,
                                  execution_mode=decision.execution_mode)
            return {"delivery_id": delivery, "processed": True,
                    "dispatched": False, "reason": "dispatch failed: %s" % redacted}
        finally:
            with self._lock:
                self._in_flight_issues.pop(issue, None)
                self._active_count = max(0, self._active_count - 1)
            self._slots.release()

    def recover_pending(self) -> list[str]:
        """Re-queue accepted/processing/failed deliveries after a restart."""
        pending = self.store.pending()
        requeued: list[str] = []
        for record in pending:
            delivery_id = str(record.get("delivery_id", "") or "")
            if not delivery_id:
                continue
            if record.get("status") in ("processing",):
                self.store.update(delivery_id, status="accepted",
                                  reason="recovered after restart")
            requeued.append(delivery_id)
        return requeued
