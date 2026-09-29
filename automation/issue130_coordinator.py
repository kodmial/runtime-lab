"""Autonomous qualification and memory-optimization coordinator (issue #130).

Stdlib only, offline by default, no git mutations, no workflow edits, no
Render service creation. This module is the durable, idempotent
orchestration state machine owned by runtime-lab issue #130.

Scope (authoritative: issue #130 body):

- Reuse current workers and tasks; do not implement a competing artifact
  delivery or Docker harness. Delivery stays owned by #128 (with #118
  paused); Docker qualification evidence is consumed from #126; infra
  repair reconciles #125; Render qualification reuses ONE owner from
  #106/#110; cutover/integration reuses #12/#13; fork optimization reuses
  the kodmial/opencode #11/PR #12/#13 line.
- The initial immutable candidate is pinned below (never mutable latest,
  installer, PATH fallback, or silent baseline reuse). Future candidates
  must arrive with their own immutable provenance plus a checked base
  version.
- The acceptance target (whole-service cgroup memory.peak <= 450 MiB
  under a real 512 MiB no-swap limit) is persisted in state before any
  trial is scored and is never relaxed to declare success.
- Robust success needs three consecutive independent full coding passes
  on the SAME immutable candidate and frozen workload/suite; the streak
  resets on any new candidate or any failure. Only then do #12/#13
  advance, with verified end-to-end execution on Render plus cleanup
  before the overall objective is declared achieved.

Wake-up: the existing scheduler envelope
(``.github/workflows/issue-scheduler.yml``: push + 15-minute safety-net
cron + issues/pull_request_target events + explicit ``gh workflow run``
dispatch after merges) wakes automation after the chat closes. This
module is the reconciler that scheduler-dispatched runs invoke; it never
requires workflow-file changes. Scheduler trigger details live in
:func:`scheduler_trigger`.

State lives in ``automation/objective-130-state.json`` (durable,
repository-native). Every transition is a pure function over plain
dicts so tests exercise the full loop with deterministic fixtures and
zero Render workers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from typing import Any, Mapping, Sequence

SCHEMA = "runtime-lab-objective-130/v1"
OBJECTIVE_ISSUE = 130
STATE_PATH = "automation/objective-130-state.json"
SCHEDULER_WORKFLOW = ".github/workflows/issue-scheduler.yml"
OPENCODE_WORKFLOW = ".github/workflows/opencode.yml"
RENDER_EXECUTOR_WORKFLOW = ".github/workflows/render-executor.yml"

MIB = 1024 * 1024
# Conservative acceptance target persisted before trials (issue #130.6):
# whole-service cgroup memory.peak <= 450 MiB under a real 512 MiB
# no-swap limit, controller plus child tools included, no OOM/restart.
ACCEPTANCE_TARGET_BYTES = 450 * MIB
HARD_LIMIT_BYTES = 512 * MIB
REQUIRED_STREAK = 3

# Frozen acceptance workload/suite for the streak (issue #130.7): the
# proven FOO_LIMIT representative coding task (search + read + edit +
# shell/test with an independently checked test outcome).
FROZEN_WORKLOAD_ID = "q1-small-edit"
FROZEN_WORKLOAD_PROMPT = (
    "Search the repository for the constant named FOO_LIMIT, "
    "read the surrounding module, change its value from 10 to 20, "
    "run the module build/tests, fix any failure the change causes, "
    "and report the files changed plus the test result."
)
FROZEN_SUITE = "FOO_LIMIT-pytest-2-pass"

# Initial immutable candidate (issue #130, verified by #126).
INITIAL_CANDIDATE: dict[str, Any] = {
    "repo": "kodmial/opencode",
    "pr": 12,
    "branch": "opencode/issue11-max-headless",
    "source_sha": "a3c748143bbc525a7cef4f9db48e2a779418943c",
    "merge_sha": "84fa724616e1fddeea8e7665e38568928feffdf9",
    "source_run_id": "36498663107",
    "source_workflow": "OpenCode Coding Artifact",
    "artifact_name": "opencode-coding-linux-x64",
    "artifact_id": "11004835952",
    "archive_sha256": "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040",
    "binary_sha256": "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966",
    "expected_version": "1.18.33",
}

# Reused owners (never duplicated by this coordinator).
DELIVERY_ISSUE = 128
PAUSED_DELIVERY_ISSUE = 118
DOCKER_ISSUE = 126
INFRA_ISSUE = 125
RENDER_QUAL_ISSUES = (106, 110)
CUTOVER_ISSUE = 12
E2E_ISSUE = 13
FORK_REPO = "kodmial/opencode"
ALLOWED_TARGET_REPOS = ("kodmial/runtime-lab", "kodmial/opencode")
TRACKING_REPO = "kodmial/runtime-lab"

# Known-superseded artifacts: structurally valid but proven unfit, so the
# coordinator refuses them as candidates instead of re-qualifying them.
# 11001896223 reported version 0.0.0 and fast-failed at the provider gate
# (issue #109); it is evidence, never a candidate.
SUPERSEDED_ARTIFACT_IDS = frozenset({"11001896223"})
# (sequential attempts after fixes are allowed; parallel owners are not).
LEGACY_ONE_SHOT_MARKERS = (
    "one-shot",
    "ONE Render attempt",
    "one-shot reservation",
)

# Failure classes for trial evidence.
FAILURE_MEMORY = "memory"
FAILURE_INFRA = "infrastructure"
FAILURE_FUNCTIONAL = "functional"
FAILURE_NONE = "none"
FAILURE_UNKNOWN = "unknown"

# Objective states (durable status vocabulary).
STATES = (
    "bootstrap",
    "docker-qualifying",
    "profiling-optimizing",
    "awaiting-delivery",
    "render-queued",
    "render-verifying",
    "marginal-optimizing",
    "streak-building",
    "integrating",
    "integrated-success",
    "blocked",
    "recovering",
)

# Action types emitted by reconcile (never executed here).
ACTIONS = (
    "consume-docker",
    "enqueue-render",
    "open-optimization-task",
    "open-repair-task",
    "open-functional-task",
    "advance-integration",
    "declare-success",
    "report-blocker",
    "cleanup-service",
    "dispatch-recovery",
    "update-reservation",
    "noop",
)

# Bounded retry policy for identical candidate+config+workload retries:
# only a classified transient may retry without changed evidence, at
# most this many times with backoff; anything else needs a new premise.
TRANSIENT_MAX_RETRIES = 3
RECOVERY_MAX_IDENTICAL = 3

# Credentials that must never reach an OpenCode child env or logs.
CREDENTIAL_ENV_NAMES = (
    "TARGET_REPO_PAT",
    "TAP_PAT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_INSTALLATION_ID",
    "RENDER_API_KEY",
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MUTABLE_RE = re.compile(r"^(latest|main|master|head|stable|current)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Candidate identity (immutable provenance, fail closed).
# ---------------------------------------------------------------------------

def validate_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless a candidate carries full immutable provenance."""
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate must be a mapping")
    required = (
        "repo", "pr", "branch", "source_sha", "merge_sha",
        "source_run_id", "artifact_id", "archive_sha256",
        "binary_sha256", "expected_version",
    )
    missing = [key for key in required if not candidate.get(key)]
    if missing:
        raise ValueError("candidate missing provenance fields: %s" % ", ".join(missing))
    repo = str(candidate["repo"]).strip()
    if repo != FORK_REPO:
        raise ValueError("candidate repo must be %r, got %r" % (FORK_REPO, repo))
    for key in ("source_sha", "merge_sha"):
        value = str(candidate[key]).strip().lower()
        if _SHA_RE.match(value) is None:
            raise ValueError("candidate %s must be a 40-char lowercase SHA" % key)
    for key in ("archive_sha256", "binary_sha256"):
        value = str(candidate[key]).strip().lower()
        if _SHA256_RE.match(value) is None:
            raise ValueError("candidate %s must be a 64-char hex digest" % key)
    for key in ("source_run_id", "artifact_id"):
        value = str(candidate[key]).strip()
        if not value.isdigit() or len(value) < 5:
            raise ValueError("candidate %s must be a numeric Actions id" % key)
    if str(candidate["artifact_id"]).strip() in SUPERSEDED_ARTIFACT_IDS:
        raise ValueError(
            "candidate artifact %s is superseded (unstamped/blocked build); "
            "use the current immutable candidate with its own provenance"
            % str(candidate["artifact_id"]).strip())
    branch = str(candidate["branch"]).strip()
    if not branch or _MUTABLE_RE.match(branch):
        raise ValueError("candidate branch must be a pinned branch name, got %r" % branch)
    version = str(candidate["expected_version"]).strip()
    if not version or version.startswith("0.0.0"):
        raise ValueError("candidate expected_version must be a stamped release, got %r" % version)
    if "latest" in json.dumps(dict(candidate)).lower().replace(" ", ""):
        # Catches artifact_reference-style mutable pointers smuggled into
        # free-form fields (e.g. "github-release:...@latest").
        text = json.dumps(dict(candidate)).lower()
        if "@latest" in text or '"latest"' in text or ":latest" in text:
            raise ValueError("candidate must never use a mutable 'latest' pointer")
    out = dict(candidate)
    out["source_sha"] = str(candidate["source_sha"]).strip().lower()
    out["merge_sha"] = str(candidate["merge_sha"]).strip().lower()
    out["archive_sha256"] = str(candidate["archive_sha256"]).strip().lower()
    out["binary_sha256"] = str(candidate["binary_sha256"]).strip().lower()
    out["candidate_id"] = candidate_id(out)
    return out


def candidate_id(candidate: Mapping[str, Any]) -> str:
    """Stable identity for one immutable candidate (artifact-centered)."""
    parts = (
        str(candidate.get("repo", "")),
        str(candidate.get("artifact_id", "")),
        str(candidate.get("source_run_id", "")),
        str(candidate.get("source_sha", "")).lower(),
        str(candidate.get("binary_sha256", "")).lower(),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return "candidate-%s-%s" % (str(candidate.get("artifact_id", "unknown")), digest)


def reject_mutable_candidate_ref(ref: object) -> str:
    """Fail closed on mutable candidate pointers (latest/main/installer/PATH)."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("candidate ref must be a non-empty string")
    text = ref.strip()
    if _MUTABLE_RE.match(text) or text.lower() in ("installer", "path", "upstream"):
        raise ValueError("ref %r is mutable; supply immutable provenance" % ref)
    if "opencode.ai/install" in text:
        raise ValueError("installer refs are never a candidate")
    return text


# ---------------------------------------------------------------------------
# Durable state.
# ---------------------------------------------------------------------------

def default_state() -> dict[str, Any]:
    """Bootstrap the durable live objective (idempotent seed)."""
    candidate = validate_candidate(INITIAL_CANDIDATE)
    return {
        "schema": SCHEMA,
        "issue": OBJECTIVE_ISSUE,
        "state": "bootstrap",
        "acceptance_target_bytes": ACCEPTANCE_TARGET_BYTES,
        "hard_limit_bytes": HARD_LIMIT_BYTES,
        "required_streak": REQUIRED_STREAK,
        "frozen_workload_id": FROZEN_WORKLOAD_ID,
        "frozen_suite": FROZEN_SUITE,
        "candidates": [candidate],
        "active_candidate_id": candidate["candidate_id"],
        "trials": [],
        "success_streak": 0,
        "streak_candidate_id": None,
        "render_lock": {"holder": None, "service_id": None, "cleanup": "none"},
        "active_workers": {},
        "integration": {
            "cutover_issue": CUTOVER_ISSUE,
            "e2e_issue": E2E_ISSUE,
            "state": "pending",
            "verified_e2e": False,
            "cleanup_verified": False,
        },
        "delivery": {"owner_issue": DELIVERY_ISSUE, "merged": False, "sha": ""},
        "recovery": {"failures_by_signature": {}, "dispatches": []},
        "seen_events": [],
        "status": {
            "state": "bootstrap",
            "active_tasks": [],
            "latest_evidence": "seeded from #126 marginal + #128 pending",
            "next_action": "consume-docker",
            "blocker": "",
        },
    }


def validate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless durable state keeps the persisted target/streak."""
    if not isinstance(state, Mapping):
        raise ValueError("state must be a mapping")
    if state.get("schema") != SCHEMA:
        raise ValueError("state schema must be %r" % SCHEMA)
    if state.get("issue") != OBJECTIVE_ISSUE:
        raise ValueError("state issue must be %r" % OBJECTIVE_ISSUE)
    if state.get("acceptance_target_bytes") != ACCEPTANCE_TARGET_BYTES:
        raise ValueError(
            "acceptance target must stay %d bytes; refusing to relax it to declare success"
            % ACCEPTANCE_TARGET_BYTES
        )
    if state.get("hard_limit_bytes") != HARD_LIMIT_BYTES:
        raise ValueError("hard limit must stay %d bytes" % HARD_LIMIT_BYTES)
    if state.get("required_streak") != REQUIRED_STREAK:
        raise ValueError("required streak must stay %d" % REQUIRED_STREAK)
    if state.get("state") not in STATES:
        raise ValueError("unknown objective state %r" % (state.get("state"),))
    candidates = state.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("state must carry at least one candidate")
    for candidate in candidates:
        validate_candidate(candidate)
    return dict(state)


def load_state(path: str = STATE_PATH) -> dict[str, Any]:
    """Load and validate durable state (bootstrap when the file is absent)."""
    if not os.path.isfile(path):
        return default_state()
    with open(path, "r", encoding="utf-8") as handle:
        return validate_state(json.load(handle))


def save_state(state: Mapping[str, Any], path: str = STATE_PATH) -> str:
    """Validate and persist durable state deterministically."""
    valid = validate_state(state)
    text = json.dumps(valid, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def scheduler_trigger() -> dict[str, str]:
    """Describe the real scheduler/workflow trigger (no new envelope)."""
    return {
        "workflow": SCHEDULER_WORKFLOW,
        "opencode_workflow": OPENCODE_WORKFLOW,
        "render_executor": RENDER_EXECUTOR_WORKFLOW,
        "state_file": STATE_PATH,
        "wake": "push to main + 15-min safety-net cron (7,22,37,52) + "
        "issues/PR events + explicit 'gh workflow run issue-scheduler.yml' "
        "after merges (scheduler reconciles and dispatches opencode.yml).",
    }


# ---------------------------------------------------------------------------
# Trial evidence classification.
# ---------------------------------------------------------------------------

REQUIRED_TRIAL_FIELDS = (
    "candidate_artifact_id",
    "binary_sha256",
    "track",
    "workload_id",
    "peak_bytes",
    "memory_events",
    "exit_code",
    "correctness",
)


def validate_trial(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless a trial carries the full evidence contract."""
    if not isinstance(trial, Mapping):
        raise ValueError("trial must be a mapping")
    missing = [field for field in REQUIRED_TRIAL_FIELDS if field not in trial]
    if missing:
        raise ValueError("trial missing evidence fields: %s" % ", ".join(missing))
    if trial.get("track") not in ("docker", "render"):
        raise ValueError("trial track must be docker or render, got %r" % (trial.get("track"),))
    peak = trial.get("peak_bytes")
    if isinstance(peak, bool) or not isinstance(peak, int) or peak <= 0:
        raise ValueError("trial peak_bytes must be a positive int")
    events = trial.get("memory_events")
    if not isinstance(events, Mapping):
        raise ValueError("trial memory_events must be a mapping")
    if trial.get("correctness") not in ("pass", "fail", "not-run"):
        raise ValueError("trial correctness must be pass/fail/not-run")
    if trial.get("workload_id") != FROZEN_WORKLOAD_ID:
        raise ValueError(
            "trial workload_id must be the frozen %r, got %r"
            % (FROZEN_WORKLOAD_ID, trial.get("workload_id"))
        )
    return dict(trial)


def classify_trial(trial: Mapping[str, Any]) -> str:
    """Classify one validated trial into a routing verdict.

    Returns one of: ``robust-pass`` | ``marginal`` | ``memory-failure`` |
    ``infrastructure-failure`` | ``functional-failure`` | ``unknown``.

    Classification rules (fail closed toward investigation, never toward
    an unsupported OOM claim):

    - infrastructure: explicit infra markers (version_gate, download,
      identity, provider, timeout-of-harness) or version mismatch, or
      executed SHA differing from the candidate binary SHA.
    - memory: oom_kill/oom_group_kill > 0, exit 137, or nonzero exit at
      the ceiling with real pressure events.
    - functional: correctness fail/timeout with zero memory pressure
      (no OOM counters, peak comfortably under the limit).
    - marginal: correctness pass but over the 450 MiB target or with
      nonzero ``max`` stall events (throttle signature).
    - robust-pass: correctness pass, exit 0, peak <= 450 MiB, zero
      OOM/max pressure, identity verified, cleanup verified.
    - unknown: anything else (missing identity/cleanup proof, ambiguous
      evidence) -- routes to recovery, never to success.
    """
    evidence = validate_trial(trial)
    events = dict(evidence.get("memory_events") or {})
    markers = set()
    for key in ("failure_markers", "markers", "infra_markers"):
        value = evidence.get(key)
        if isinstance(value, (list, tuple)):
            markers.update(str(item).lower() for item in value)
    text_markers = " ".join(sorted(markers))
    infra_tokens = (
        "version_gate", "version-gate", "download", "artifact",
        "identity", "provider", "credentials", "permission",
        "harness-timeout", "deploy", "health",
    )
    if any(token in text_markers for token in infra_tokens):
        return "infrastructure-failure"
    expected_sha = str(evidence.get("expected_binary_sha256") or "").strip().lower()
    executed_sha = str(evidence.get("binary_sha256") or "").strip().lower()
    if expected_sha and executed_sha and expected_sha != executed_sha:
        return "infrastructure-failure"
    version_ok = evidence.get("version_ok", True)
    if version_ok is False:
        return "infrastructure-failure"

    def _int(name: str) -> int:
        try:
            return int(events.get(name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    oom = _int("oom_kill") + _int("oom_group_kill")
    max_events = _int("max")
    exit_code = evidence.get("exit_code")
    correctness = evidence.get("correctness")
    peak = int(evidence["peak_bytes"])
    if oom > 0 or exit_code == 137:
        return "memory-failure"
    if correctness != "pass" or exit_code != 0:
        if max_events > 0 or peak >= HARD_LIMIT_BYTES:
            # Zero-pressure proof is absent; do not claim either side.
            if max_events == 0 and oom == 0 and peak < HARD_LIMIT_BYTES:
                return "functional-failure"
            return "unknown" if correctness == "not-run" else "memory-failure"
        return "functional-failure"
    # Correctness passed with a clean exit: grade the envelope.
    if peak <= ACCEPTANCE_TARGET_BYTES and max_events == 0 and oom == 0:
        identity_ok = evidence.get("identity_verified", False) is True
        cleanup_ok = evidence.get("cleanup_verified", False) is True
        if identity_ok and cleanup_ok:
            return "robust-pass"
        return "unknown"
    return "marginal"


def trial_failure_signature(trial: Mapping[str, Any], verdict: str) -> str:
    """Deduplicated signature for recovery (candidate+config+workload+verdict)."""
    parts = (
        str(trial.get("candidate_artifact_id", "")),
        str(trial.get("track", "")),
        str(trial.get("workload_id", "")),
        str(trial.get("bun_options", "")),
        verdict,
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Locks, workers, cross-repo correlation, credentials.
# ---------------------------------------------------------------------------

def render_lock_holder(state: Mapping[str, Any]) -> str | None:
    """Return the current Render lock holder, if any."""
    lock = state.get("render_lock") or {}
    holder = lock.get("holder")
    return str(holder) if holder else None


def can_create_render_service(state: Mapping[str, Any]) -> bool:
    """True only when no Render service exists and cleanup is verified.

    Unknown cleanup status blocks creation and routes to recovery
    (issue #130 execution policy).
    """
    lock = state.get("render_lock") or {}
    if lock.get("service_id"):
        return False
    return lock.get("cleanup", "none") in ("none", "verified")


def cleanup_unknown(state: Mapping[str, Any]) -> bool:
    """True when cleanup status is unknown (blocks service creation)."""
    lock = state.get("render_lock") or {}
    return lock.get("cleanup") == "unknown"


def claim_render_lock(state: dict[str, Any], holder: str, service_id: str) -> dict[str, Any]:
    """Claim the single ephemeral Render slot (fail closed on contention)."""
    if not holder or not service_id:
        raise ValueError("holder and service_id are required")
    if not can_create_render_service(state):
        raise ValueError("a Render service already exists or cleanup is unverified")
    state["render_lock"] = {"holder": holder, "service_id": service_id, "cleanup": "active"}
    return state


def release_render_lock(state: dict[str, Any], cleanup: str = "verified") -> dict[str, Any]:
    """Release the Render slot; only verified absence frees it."""
    if cleanup not in ("verified", "unknown", "failed"):
        raise ValueError("cleanup must be verified/unknown/failed")
    state["render_lock"] = {"holder": None, "service_id": None, "cleanup": cleanup}
    return state


def claim_worker(state: dict[str, Any], task_key: str, owner: str) -> dict[str, Any]:
    """Claim exactly one active worker per logical task (fail on duplicate)."""
    if not task_key or not owner:
        raise ValueError("task_key and owner are required")
    workers = state.setdefault("active_workers", {})
    if task_key in workers:
        raise ValueError("task %r already has an active worker (%r)" % (task_key, workers[task_key]))
    workers[task_key] = {"owner": owner, "task": task_key}
    return state


def release_worker(state: dict[str, Any], task_key: str) -> dict[str, Any]:
    """Release a logical-task worker slot (idempotent)."""
    workers = state.setdefault("active_workers", {})
    workers.pop(task_key, None)
    return state


def cross_repo_task(task_key: str, source_issue: int = OBJECTIVE_ISSUE,
                    target_repo: str = FORK_REPO) -> dict[str, Any]:
    """Durable cross-repo correlation for one logical task (fail closed)."""
    if target_repo not in ALLOWED_TARGET_REPOS:
        raise ValueError("target repo %r is not allow-listed" % (target_repo,))
    if not isinstance(source_issue, int) or source_issue <= 0:
        raise ValueError("source_issue must be a positive integer")
    if not task_key:
        raise ValueError("task_key is required")
    return {
        "source_repo": TRACKING_REPO,
        "source_issue": source_issue,
        "target_repo": target_repo,
        "task": task_key,
        "branch": "opencode/issue%d-%s" % (source_issue, task_key),
    }


def scrub_worker_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return a child-process env without any GitHub/Render credentials."""
    if not isinstance(environ, Mapping):
        raise ValueError("environ must be a mapping")
    return {str(k): str(v) for k, v in environ.items() if str(k) not in CREDENTIAL_ENV_NAMES}


def assert_no_credentials_in_worker_env(environ: Mapping[str, str]) -> None:
    """Fail closed when a worker child env carries credentials."""
    if not isinstance(environ, Mapping):
        raise ValueError("environ must be a mapping")
    present = [name for name in CREDENTIAL_ENV_NAMES if str(environ.get(name, "") or "").strip()]
    if present:
        raise ValueError("worker env must never carry credentials: %s" % ", ".join(sorted(present)))


# ---------------------------------------------------------------------------
# Recovery helpers (bounded, deduplicated, never endless).
# ---------------------------------------------------------------------------

def should_retry_identical(state: Mapping[str, Any], signature: str,
                           transient: bool = False) -> bool:
    """True only for a classified transient inside the bounded backoff."""
    counts = (state.get("recovery") or {}).get("failures_by_signature") or {}
    seen = int(counts.get(signature, 0) or 0)
    if not transient:
        return False
    return seen < TRANSIENT_MAX_RETRIES


def record_failure(state: dict[str, Any], signature: str) -> int:
    """Record one no-progress recovery attempt; return the new count."""
    recovery = state.setdefault("recovery", {})
    counts = recovery.setdefault("failures_by_signature", {})
    counts[signature] = int(counts.get(signature, 0) or 0) + 1
    return int(counts[signature])


def recovery_exhausted(state: Mapping[str, Any], signature: str) -> bool:
    """True when identical no-progress recovery hit its bound (stop, expose)."""
    counts = (state.get("recovery") or {}).get("failures_by_signature") or {}
    return int(counts.get(signature, 0) or 0) >= RECOVERY_MAX_IDENTICAL


def update_legacy_reservation(note: str = "") -> dict[str, str]:
    """Supersede stale one-shot instructions without enabling multi-owners."""
    return {
        "type": "update-reservation",
        "supersedes": list(LEGACY_ONE_SHOT_MARKERS),
        "policy": "sequential attempts after fixes and for stability verification; "
        "at most ONE ephemeral Render service at a time; Docker independent.",
        "note": note or "stale one-shot reservation retired by the #130 coordinator",
    }


# ---------------------------------------------------------------------------
# Reconciliation: the executable state machine.
# ---------------------------------------------------------------------------

def _is_duplicate(state: Mapping[str, Any], event_id: str) -> bool:
    return bool(event_id) and event_id in list(state.get("seen_events") or [])


def _mark_seen(state: dict[str, Any], event_id: str) -> None:
    if event_id:
        seen = state.setdefault("seen_events", [])
        if event_id not in seen:
            seen.append(event_id)


def _active_candidate(state: Mapping[str, Any]) -> dict[str, Any]:
    wanted = state.get("active_candidate_id")
    for candidate in state.get("candidates") or []:
        if candidate.get("candidate_id") == wanted:
            return candidate
    first = (state.get("candidates") or [])[0]
    if first is None:
        raise ValueError("state has no candidates")
    return first


def reconcile(state: Mapping[str, Any], event: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Idempotently reconcile one event; return (new_state, actions).

    Event shape: ``{"id": <dedupe key>, "type": <type>, ...payload}``.
    Supported types: ``docker-result``, ``render-result``,
    ``delivery-merged``, ``repair-merged``, ``optimization-landed``,
    ``dispatch-failed``, ``merge-conflict``, ``worker-done``,
    ``cleanup-verified``, ``tick``. Unknown payloads yield an
    ``unknown`` trial-style recovery action instead of success.
    """
    next_state: dict[str, Any] = copy.deepcopy(validate_state(state))
    if not isinstance(event, Mapping) or not event.get("type"):
        return next_state, [{"type": "noop", "reason": "empty event"}]
    event_id = str(event.get("id", "") or "")
    if _is_duplicate(next_state, event_id):
        return next_state, [{"type": "noop", "reason": "duplicate event %s" % event_id}]
    _mark_seen(next_state, event_id)
    event_type = str(event.get("type"))
    actions: list[dict[str, Any]] = []

    if event_type in ("docker-result", "render-result"):
        trial = event.get("trial")
        try:
            validated = validate_trial(trial or {})
        except ValueError as exc:
            next_state["state"] = "recovering"
            next_state["status"] = {
                "state": "recovering",
                "active_tasks": sorted((next_state.get("active_workers") or {}).keys()),
                "latest_evidence": "unknown evidence: %s" % exc,
                "next_action": "dispatch-recovery",
                "blocker": "",
            }
            return next_state, [{"type": "dispatch-recovery", "reason": "unknown evidence: %s" % exc}]
        verdict = classify_trial(validated)
        track = validated["track"]
        signature = trial_failure_signature(validated, verdict)
        candidate_artifact = str(validated.get("candidate_artifact_id"))
        active = _active_candidate(next_state)
        new_candidate = candidate_artifact != str(active.get("artifact_id"))
        stored = dict(validated)
        stored["verdict"] = verdict
        stored["signature"] = signature
        next_state.setdefault("trials", []).append(stored)

        if verdict == "robust-pass" and not new_candidate:
            if next_state.get("streak_candidate_id") != active["candidate_id"]:
                next_state["streak_candidate_id"] = active["candidate_id"]
                next_state["success_streak"] = 0
            next_state["success_streak"] = int(next_state.get("success_streak", 0) or 0) + 1
            streak = int(next_state["success_streak"])
            if streak >= REQUIRED_STREAK:
                next_state["state"] = "integrating"
                actions.append({
                    "type": "advance-integration",
                    "cutover_issue": CUTOVER_ISSUE,
                    "e2e_issue": E2E_ISSUE,
                    "candidate_id": active["candidate_id"],
                    "streak": streak,
                    "correlation": cross_repo_task("integrate-%s" % active["artifact_id"]),
                })
            else:
                next_state["state"] = "streak-building"
                if track == "docker":
                    actions.append({"type": "consume-docker", "reason": "streak %d/%d" % (streak, REQUIRED_STREAK)})
                else:
                    if can_create_render_service(next_state):
                        actions.append({"type": "enqueue-render", "candidate_id": active["candidate_id"],
                                        "reason": "streak %d/%d" % (streak, REQUIRED_STREAK)})
                    else:
                        actions.append({"type": "noop", "reason": "render slot busy; streak held"})
            if track == "render":
                release_worker(next_state, "render-qual-%s" % candidate_artifact)
        elif verdict == "robust-pass" and new_candidate:
            # A pass on a different artifact starts its own streak; the
            # active pointer only moves via an explicit landed event.
            actions.append({"type": "noop", "reason": "pass on non-active candidate; awaiting optimization-landed"})
            next_state["state"] = "docker-qualifying"
        elif verdict == "marginal":
            next_state["success_streak"] = 0
            next_state["state"] = "marginal-optimizing"
            actions.append({
                "type": "open-optimization-task",
                "repo": FORK_REPO,
                "candidate_id": active["candidate_id"],
                "reason": "marginal (over target or throttled); optimize, do not integrate",
                "correlation": cross_repo_task("optimize-%s" % active["artifact_id"]),
            })
            if track == "render":
                release_worker(next_state, "render-qual-%s" % candidate_artifact)
        elif verdict == "memory-failure":
            next_state["success_streak"] = 0
            next_state["state"] = "profiling-optimizing"
            actions.append({
                "type": "open-optimization-task",
                "repo": FORK_REPO,
                "candidate_id": active["candidate_id"],
                "reason": "confirmed memory failure (%s); profile then measured change" % track,
                "correlation": cross_repo_task("optimize-%s" % active["artifact_id"]),
                "docker_direct": track == "docker",
            })
            if track == "render":
                release_worker(next_state, "render-qual-%s" % candidate_artifact)
        elif verdict == "infrastructure-failure":
            next_state["success_streak"] = 0
            next_state["state"] = "recovering"
            actions.append({
                "type": "open-repair-task",
                "repo": TRACKING_REPO if track == "render" else FORK_REPO,
                "candidate_id": active["candidate_id"],
                "reason": "infrastructure/provider/version/identity failure; preserve candidate and retry after merge",
                "correlation": cross_repo_task("repair-%s" % active["artifact_id"],
                                               target_repo=TRACKING_REPO),
            })
            if track == "render":
                release_worker(next_state, "render-qual-%s" % candidate_artifact)
        elif verdict == "functional-failure":
            next_state["success_streak"] = 0
            next_state["state"] = "recovering"
            actions.append({
                "type": "open-functional-task",
                "repo": FORK_REPO,
                "candidate_id": active["candidate_id"],
                "reason": "correctness/timeout without memory cause; functional investigation required",
                "correlation": cross_repo_task("functional-%s" % active["artifact_id"]),
            })
            if track == "render":
                release_worker(next_state, "render-qual-%s" % candidate_artifact)
        else:  # unknown
            next_state["success_streak"] = 0
            next_state["state"] = "recovering"
            actions.append({"type": "dispatch-recovery", "reason": "unknown evidence; inspect, do not retry blindly",
                            "signature": signature})
            if track == "render":
                release_worker(next_state, "render-qual-%s" % candidate_artifact)
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "delivery-merged":
        sha = str(event.get("sha", "") or "").strip().lower()
        if _SHA_RE.match(sha) is None:
            return next_state, [{"type": "noop", "reason": "delivery-merged without a valid SHA"}]
        next_state["delivery"] = {"owner_issue": DELIVERY_ISSUE, "merged": True, "sha": sha}
        # Closure of an issue or green CI alone is never evidence; only a
        # real Docker pass enqueues Render. If the last Docker verdict on
        # the active candidate was a pass, enqueue ONE Render check now.
        docker_passes = [
            trial for trial in next_state.get("trials") or []
            if trial.get("track") == "docker"
            and trial.get("verdict") in ("robust-pass", "marginal")
            and str(trial.get("candidate_artifact_id")) == str(_active_candidate(next_state).get("artifact_id"))
        ]
        if cleanup_unknown(next_state):
            next_state["state"] = "recovering"
            actions.append({"type": "dispatch-recovery", "reason": "unknown cleanup blocks Render creation"})
        elif docker_passes and can_create_render_service(next_state):
            next_state["state"] = "render-queued"
            actions.append({"type": "enqueue-render",
                            "candidate_id": _active_candidate(next_state)["candidate_id"],
                            "reason": "exact delivery merged + real Docker pass"})
        else:
            next_state["state"] = "awaiting-delivery"
            actions.append({"type": "consume-docker", "reason": "delivery merged; awaiting a real Docker pass"})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "optimization-landed":
        try:
            candidate = validate_candidate(event.get("candidate") or {})
        except ValueError as exc:
            return next_state, [{"type": "dispatch-recovery", "reason": "bad optimization candidate: %s" % exc}]
        known = {item.get("candidate_id") for item in next_state.get("candidates") or []}
        if candidate["candidate_id"] not in known:
            next_state["candidates"] = list(next_state.get("candidates") or []) + [candidate]
        next_state["active_candidate_id"] = candidate["candidate_id"]
        next_state["success_streak"] = 0
        next_state["streak_candidate_id"] = None
        next_state["state"] = "docker-qualifying"
        actions.append({"type": "consume-docker", "reason": "new immutable artifact; qualify in Docker first",
                        "candidate_id": candidate["candidate_id"]})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "repair-merged":
        task_key = str(event.get("task", "") or "")
        if task_key:
            release_worker(next_state, task_key)
        next_state["state"] = "docker-qualifying"
        actions.append({"type": "consume-docker", "reason": "repair merged; retry the applicable check"})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "dispatch-failed":
        signature = str(event.get("signature", "") or "dispatch")
        transient = bool(event.get("transient", False))
        count = record_failure(next_state, signature)
        if recovery_exhausted(next_state, signature):
            next_state["state"] = "blocked"
            actions.append({"type": "report-blocker",
                            "reason": "identical no-progress recovery %d/%d for %s; exposing evidence"
                            % (count, RECOVERY_MAX_IDENTICAL, signature)})
        elif should_retry_identical(next_state, signature, transient=transient):
            next_state["state"] = "recovering"
            actions.append({"type": "dispatch-recovery",
                            "reason": "classified transient; bounded retry %d/%d"
                            % (count, TRANSIENT_MAX_RETRIES),
                            "signature": signature})
        else:
            next_state["state"] = "recovering"
            actions.append({"type": "dispatch-recovery",
                            "reason": "dispatch exhausted/merge conflict; reconcile branch/PR, dedup by signature",
                            "signature": signature,
                            "inspect_branch": str(event.get("branch", "") or ""),
                            "pr": event.get("pr", "")})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "merge-conflict":
        signature = str(event.get("signature", "") or "merge-conflict")
        count = record_failure(next_state, signature)
        if recovery_exhausted(next_state, signature):
            next_state["state"] = "blocked"
            actions.append({"type": "report-blocker",
                            "reason": "merge-conflict recovery exhausted (%d); exposing evidence" % count})
        else:
            next_state["state"] = "recovering"
            actions.append({"type": "dispatch-recovery",
                            "reason": "inspect/reconcile existing branch/PR and dispatch a concrete recovery action",
                            "signature": signature,
                            "branch": str(event.get("branch", "") or ""),
                            "pr": event.get("pr", "")})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "worker-done":
        task_key = str(event.get("task", "") or "")
        release_worker(next_state, task_key)
        next_state["status"] = status_of(next_state, actions or [{"type": "noop", "reason": "worker released"}])
        return next_state, actions or [{"type": "noop", "reason": "worker released"}]

    if event_type == "cleanup-verified":
        service_id = str(event.get("service_id", "") or "")
        lock = next_state.get("render_lock") or {}
        if service_id and lock.get("service_id") and service_id != lock.get("service_id"):
            return next_state, [{"type": "noop", "reason": "stale cleanup proof ignored"}]
        release_render_lock(next_state, "verified")
        integration = next_state.get("integration") or {}
        if next_state.get("state") == "integrating" and event.get("e2e_ok") is True:
            integration["verified_e2e"] = True
            integration["cleanup_verified"] = True
            integration["state"] = "verified"
            next_state["integration"] = integration
            next_state["state"] = "integrated-success"
            actions.append({"type": "declare-success", "reason": "e2e execution verified on Render with cleanup proof"})
        else:
            next_state["integration"] = integration
            actions.append({"type": "cleanup-service", "reason": "absence verified", "service_id": service_id})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "integration-verified":
        integration = next_state.get("integration") or {}
        integration["verified_e2e"] = True
        integration["cleanup_verified"] = bool(event.get("cleanup_verified", False))
        if integration["cleanup_verified"]:
            integration["state"] = "verified"
            next_state["integration"] = integration
            next_state["state"] = "integrated-success"
            actions.append({"type": "declare-success", "reason": "cutover/e2e verified with cleanup"})
        else:
            integration["state"] = "pending-cleanup"
            next_state["integration"] = integration
            next_state["state"] = "integrating"
            actions.append({"type": "cleanup-service", "reason": "e2e ok; awaiting verified absence"})
        next_state["status"] = status_of(next_state, actions)
        return next_state, actions

    if event_type == "tick":
        # Periodic scheduler wake: never invent evidence; surface the next
        # automatic transition or the precise blocker.
        if next_state.get("state") in ("integrated-success",):
            return next_state, [{"type": "noop", "reason": "objective already achieved"}]
        if next_state.get("state") == "blocked":
            return next_state, [{"type": "noop", "reason": "blocked; awaiting unavailable premise"}]
        actions.append({"type": "noop", "reason": "tick: %s" % status_of(next_state, [] )["next_action"]})
        return next_state, actions

    next_state["state"] = "recovering"
    actions.append({"type": "dispatch-recovery", "reason": "unknown event type %r" % event_type})
    next_state["status"] = status_of(next_state, actions)
    return next_state, actions


def status_of(state: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the concise durable status for the issue (no secrets)."""
    action_types = [str(item.get("type", "")) for item in actions or []]
    priority = ("declare-success", "report-blocker", "enqueue-render", "advance-integration",
                "open-optimization-task", "open-repair-task", "open-functional-task",
                "dispatch-recovery", "cleanup-service", "consume-docker",
                "update-reservation", "noop")
    next_action = next((name for name in priority if name in action_types), "noop")
    trials = list(state.get("trials") or [])
    if trials:
        last = trials[-1]
        latest = "%s %s peak=%d events=%s exit=%s correctness=%s verdict=%s" % (
            last.get("track"), last.get("candidate_artifact_id"),
            last.get("peak_bytes"), last.get("memory_events"),
            last.get("exit_code"), last.get("correctness"), last.get("verdict"))
    else:
        latest = str((state.get("status") or {}).get("latest_evidence", "no trials yet"))
    return {
        "state": state.get("state"),
        "active_tasks": sorted((state.get("active_workers") or {}).keys()),
        "latest_evidence": latest,
        "next_action": next_action,
        "blocker": str((state.get("status") or {}).get("blocker", "") or ""),
    }


def render_status_comment(state: Mapping[str, Any]) -> str:
    """Render the durable issue status comment (concise, secret-free)."""
    status = state.get("status") or status_of(state, [])
    lines = [
        "<!-- runtime-lab-objective-130-status -->",
        "Objective #130 status: **%s**" % status.get("state"),
        "",
        "- Active tasks: %s" % (", ".join(status.get("active_tasks") or []) or "none"),
        "- Latest evidence: %s" % status.get("latest_evidence"),
        "- Next automatic transition: %s" % status.get("next_action"),
        "- Blocker: %s" % (status.get("blocker") or "none"),
        "- Acceptance target: %d MiB cgroup peak (limit 512 MiB, streak %s/%d)" % (
            ACCEPTANCE_TARGET_BYTES // MIB,
            state.get("success_streak", 0), REQUIRED_STREAK),
        "- State file: `%s` | Scheduler: `%s`" % (STATE_PATH, SCHEDULER_WORKFLOW),
    ]
    return "\n".join(lines) + "\n"


def bootstrap_live_objective() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Seed the durable live objective from real #126/#128 evidence.

    Uses the two verified Docker marginal trials from issue #126 (peaks
    538,173,440 / 538,189,824 B, ``max`` 2042/1952, oom 0, exit 0,
    edit+test pass) against the initial immutable candidate, with #128
    delivery still pending. The next automatic transition is a targeted
    fork optimization task (marginal triggers optimization, never
    integration), while Docker measurement stays independent of
    delivery. No Render worker is created here.
    """
    state = default_state()
    active = _active_candidate(state)
    docker_trials = [
        {
            "candidate_artifact_id": active["artifact_id"],
            "binary_sha256": active["binary_sha256"],
            "expected_binary_sha256": active["binary_sha256"],
            "version_ok": True,
            "identity_verified": True,
            "track": "docker",
            "workload_id": FROZEN_WORKLOAD_ID,
            "peak_bytes": 538173440,
            "memory_current_bytes": 63234048,
            "memory_peak_bytes": 538173440,
            "memory_events": {"max": 2042, "oom_kill": 0, "oom_group_kill": 0},
            "swap_current_bytes": 0,
            "swap_max_bytes": 0,
            "exit_code": 0,
            "correctness": "pass",
            "cleanup_verified": True,
        },
        {
            "candidate_artifact_id": active["artifact_id"],
            "binary_sha256": active["binary_sha256"],
            "expected_binary_sha256": active["binary_sha256"],
            "version_ok": True,
            "identity_verified": True,
            "track": "docker",
            "workload_id": FROZEN_WORKLOAD_ID,
            "peak_bytes": 538189824,
            "memory_current_bytes": 50483200,
            "memory_peak_bytes": 538189824,
            "memory_events": {"max": 1952, "oom_kill": 0, "oom_group_kill": 0},
            "swap_current_bytes": 0,
            "swap_max_bytes": 0,
            "exit_code": 0,
            "correctness": "pass",
            "cleanup_verified": True,
        },
    ]
    actions: list[dict[str, Any]] = []
    for index, trial in enumerate(docker_trials):
        state, step = reconcile(state, {"id": "bootstrap-docker-%d" % index, "type": "docker-result", "trial": trial})
        actions.extend(step)
    return state, actions
