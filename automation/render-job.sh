#!/usr/bin/env bash
# Ephemeral Render execution: create -> wait healthy -> run job -> collect.
#
# TEMPORARY DEVELOPMENT HARNESS (issue #4).
# GitHub Actions orchestration is a temporary development harness, not the final runtime architecture.
# GitHub Actions is used here only as a temporary control plane so the Render lifecycle and runner can be
# developed and tested before the direct GitHub -> Render integration exists.
# This orchestration is a development harness, not the final runtime
# architecture. The final production path must not require an OpenCode GitHub
# Actions job; final cutover is implemented by later issues. Lifecycle and
# runner logic lives in automation/render_lifecycle.py so the future
# persistent Render controller can import and call it directly instead of
# going through this Actions shell wrapper.
#
# Cleanup (delete + verify) lives in automation/render-cleanup.sh, which the
# render-executor workflow always runs afterwards, even when this script fails.
#
# Cost guards encoded here (see automation/render_lifecycle.py):
# - exactly one service creation per issue execution attempt;
# - polling/deploy retries reuse the same service id, never create a new one;
# - model fallback runs inside the same worker, never a second service;
# - bounded retries only; no cron path in this script creates services.
# - Render 429/5xx responses are honored with Retry-After backoff per call;
#   a retried creation first reuses the uniquely named service if it exists.
#
# Auth: RENDER_API_KEY is mapped from the repository secret KEY by the stable
# workflow envelope. This script never prints it. OPENCODE_API_KEY is never a
# prerequisite and is never required here. GitHub writes stay on the Actions
# side (the workflow finalize step); this script performs read-only `gh`
# resolution of issue text and base SHA at most, and never edits issues/PRs.
set -euo pipefail

: "${ISSUE_NUMBER:?ISSUE_NUMBER must be set}"
: "${EXECUTION_MODE:?EXECUTION_MODE must be set}"
: "${RENDER_API_KEY:?RENDER_API_KEY must be set by the workflow}"

RENDER_STATE_FILE="${RENDER_STATE_FILE:-/tmp/runtime-lab-render-state.json}"
RENDER_RESULT_FILE="${RENDER_RESULT_FILE:-/tmp/runtime-lab-render-result.json}"
RENDER_REGION="${RENDER_REGION:-oregon}"
OPENCODE_MODEL="${OPENCODE_MODEL:-opencode/muse-spark-1.3-contributor-free}"
REPO_URL="https://github.com/kodmial/runtime-lab"
API_BASE="https://api.render.com/v1"

if [[ "$EXECUTION_MODE" != "smoke" && "$EXECUTION_MODE" != "e2e" ]]; then
  echo "::error::Unsupported execution mode: $EXECUTION_MODE" >&2
  exit 2
fi

if ! [[ "$ISSUE_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
  echo "::error::ISSUE_NUMBER must be a positive integer, got '$ISSUE_NUMBER'." >&2
  exit 2
fi

# Validate region/model against the single source of truth (fail closed).
# This runs before any Render network call so unsupported regions are
# rejected before service creation.
python3 - "$RENDER_REGION" "$OPENCODE_MODEL" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import validate_worker_region, PREFERRED_MODEL, FALLBACK_MODEL
region = sys.argv[1]
model = sys.argv[2]
validate_worker_region(region)
if model not in (PREFERRED_MODEL, FALLBACK_MODEL):
    raise SystemExit("unknown model: %r" % model)
PY

# ---------------------------------------------------------------------------
# 429/5xx-aware Render API caller.
# Usage: api_call <method> <url> [curl-args...]
# Sets: API_HTTP_CODE, API_BODY, API_RETRY_AFTER. Returns 0 on 2xx.
# Retries 429/502/503/504 with bounded backoff honoring Retry-After
# (clamped to 60s). Never provisions a second service itself; callers reuse
# the recorded service id for every retry after creation.
# ---------------------------------------------------------------------------
API_HTTP_CODE=""
API_BODY=""
API_RETRY_AFTER=""

api_call() {
  local method="$1"
  local url="$2"
  shift 2
  local attempt=1
  local max_attempts=4
  local backoff
  while true; do
    local header_file
    header_file="$(mktemp)"
    local body_file
    body_file="$(mktemp)"
    API_HTTP_CODE="$(curl -sS -o "$body_file" -D "$header_file" -w '%{http_code}' \
      --max-time 60 -X "$method" "$url" \
      -H "Accept: application/json" \
      -H "Authorization: Bearer $RENDER_API_KEY" "$@" 2>/dev/null || true)"
    API_BODY="$(cat "$body_file")"
    # Extract Retry-After case-insensitively; default to empty.
    API_RETRY_AFTER="$(grep -i '^retry-after:' "$header_file" 2>/dev/null \
      | tail -n 1 | cut -d: -f2- | tr -d ' \r\n' || true)"
    rm -f "$header_file" "$body_file"
    if [[ "$API_HTTP_CODE" =~ ^2[0-9][0-9]$ ]]; then
      return 0
    fi
    local retryable="no"
    case "$API_HTTP_CODE" in
      429|502|503|504) retryable="yes" ;;
    esac
    if [[ "$retryable" == "yes" && "$attempt" -lt "$max_attempts" ]]; then
      backoff="$(python3 - "$attempt" "${API_RETRY_AFTER:-}" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import backoff_delay_for_attempt, parse_retry_after_seconds
attempt = int(sys.argv[1])
retry_after = parse_retry_after_seconds(sys.argv[2] if len(sys.argv) > 2 else "")
print(backoff_delay_for_attempt(attempt, retry_after))
PY
)"
      echo "Render API $method $url returned HTTP $API_HTTP_CODE (attempt $attempt/$max_attempts); backing off ${backoff}s per Retry-After policy." >&2
      sleep "$backoff"
      attempt=$((attempt + 1))
      continue
    fi
    return 1
  done
}

# One service creation per attempt: reuse an existing state file service id.
EXISTING_SERVICE_ID=""
if [[ -s "$RENDER_STATE_FILE" ]]; then
  EXISTING_SERVICE_ID="$(jq -r '.serviceId // empty' "$RENDER_STATE_FILE" 2>/dev/null || true)"
fi
if [[ -n "$EXISTING_SERVICE_ID" ]]; then
  echo "Reusing existing ephemeral service $EXISTING_SERVICE_ID; refusing to create a second one."
  SERVICE_ID="$EXISTING_SERVICE_ID"
else
  RUN_ID_SAFE="${GITHUB_RUN_ID:-local}"
  SERVICE_NAME="runtime-lab-issue${ISSUE_NUMBER}-${RUN_ID_SAFE}"

  # Resolve the Render workspace (owner) id without extra secrets.
  if ! api_call GET "$API_BASE/owners?limit=20"; then
    echo "::error::Could not resolve a Render owner id (HTTP $API_HTTP_CODE)." >&2
    exit 1
  fi
  OWNER_ID="$(jq -r '.[0].id // empty' <<<"$API_BODY" 2>/dev/null || true)"
  if [[ -z "$OWNER_ID" ]]; then
    echo "::error::Could not resolve a Render owner id for service creation." >&2
    exit 1
  fi

  CREATE_PAYLOAD="$(python3 - "$SERVICE_NAME" "$OWNER_ID" "$RENDER_REGION" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import build_create_service_payload
print(json.dumps(build_create_service_payload(
    name=sys.argv[1], owner_id=sys.argv[2], region=sys.argv[3])))
PY
)"

  # Create exactly one temporary web service (POST /v1/services -> 201).
  # On ambiguous failures, first check whether our uniquely named service
  # already exists and reuse it rather than issuing a second creation.
  CREATE_OK="no"
  if api_call POST "$API_BASE/services" -H "Content-Type: application/json" -d "$CREATE_PAYLOAD"; then
    CREATE_OK="yes"
    CREATE_RESPONSE="$API_BODY"
  else
    echo "Service creation attempt returned HTTP $API_HTTP_CODE; checking for an existing uniquely named service before any retry." >&2
    if api_call GET "$API_BASE/services?limit=100"; then
      REUSED_ID="$(jq -r --arg n "$SERVICE_NAME" '.[] | select(.service.name == $n or .name == $n) | (.service.id // .id // empty)' <<<"$API_BODY" 2>/dev/null | head -n 1 || true)"
      if [[ -n "$REUSED_ID" ]]; then
        echo "Found existing service $REUSED_ID with our unique name; reusing it instead of creating a second worker."
        SERVICE_ID="$REUSED_ID"
        DEPLOY_ID=""
        PLAN="free"
        jq -n --arg sid "$SERVICE_ID" --arg dep "$DEPLOY_ID" \
          --arg region "$RENDER_REGION" --arg model "$OPENCODE_MODEL" \
          --arg plan "$PLAN" \
          '{serviceId: $sid, deployId: $dep, region: $region, model: $model, plan: $plan}' \
          > "$RENDER_STATE_FILE"
        echo "Reusing ephemeral Render service $SERVICE_ID (plan=free, region=$RENDER_REGION)."
      else
        echo "::error::Render service creation failed (HTTP $API_HTTP_CODE) and no uniquely named service exists to reuse." >&2
        exit 1
      fi
    else
      echo "::error::Render service creation failed (HTTP $API_HTTP_CODE) and the reuse check also failed." >&2
      exit 1
    fi
  fi

  if [[ "$CREATE_OK" == "yes" ]]; then
    CREATE_RESPONSE="$API_BODY"
    SERVICE_ID="$(jq -r '.service.id // empty' <<<"$CREATE_RESPONSE" 2>/dev/null || true)"
    DEPLOY_ID="$(jq -r '.deployId // empty' <<<"$CREATE_RESPONSE" 2>/dev/null || true)"
    PLAN="$(jq -r '.service.serviceDetails.plan // empty' <<<"$CREATE_RESPONSE" 2>/dev/null || true)"
    if [[ -z "$SERVICE_ID" ]]; then
      echo "::error::Render service creation returned no service id." >&2
      exit 1
    fi
    # Free-tier guard: fail closed rather than silently keep a paid resource.
    if [[ "$PLAN" != "free" ]]; then
      echo "::error::Render service $SERVICE_ID reported non-free plan '$PLAN'; aborting." >&2
      printf '{"serviceId":%s,"plan":%s}\n' \
        "$(jq -Rn --arg v "$SERVICE_ID" '$v')" \
        "$(jq -Rn --arg v "$PLAN" '$v')" > "$RENDER_STATE_FILE"
      exit 1
    fi
    if [[ -z "$DEPLOY_ID" || "$DEPLOY_ID" == "null" ]]; then
      DEPLOY_ID=""
    fi
    jq -n --arg sid "$SERVICE_ID" --arg dep "$DEPLOY_ID" \
      --arg region "$RENDER_REGION" --arg model "$OPENCODE_MODEL" \
      --arg plan "$PLAN" \
      '{serviceId: $sid, deployId: $dep, region: $region, model: $model, plan: $plan}' \
      > "$RENDER_STATE_FILE"
    echo "Created ephemeral Render service $SERVICE_ID (plan=free, region=$RENDER_REGION)."
  fi
fi

SERVICE_ID="$(jq -r '.serviceId // empty' "$RENDER_STATE_FILE")"
DEPLOY_ID="$(jq -r '.deployId // empty' "$RENDER_STATE_FILE")"
[[ -n "$SERVICE_ID" ]] || { echo "::error::Render state has no service id." >&2; exit 1; }

# Wait for the deploy to become live, reusing the same service (bounded).
DEPLOY_ATTEMPTS=60
DEPLOY_INTERVAL=20
if [[ -n "$DEPLOY_ID" ]]; then
  for ((i = 1; i <= DEPLOY_ATTEMPTS; i++)); do
    if ! api_call GET "$API_BASE/services/$SERVICE_ID/deploys/$DEPLOY_ID"; then
      echo "Deploy poll attempt $i/$DEPLOY_ATTEMPTS returned HTTP $API_HTTP_CODE; continuing on the same service." >&2
      if [[ "$i" -eq "$DEPLOY_ATTEMPTS" ]]; then
        echo "::error::Render deploy $DEPLOY_ID did not go live in time." >&2
        exit 1
      fi
      sleep "$DEPLOY_INTERVAL"
      continue
    fi
    DEPLOY_JSON="$API_BODY"
    STATUS="$(jq -r '.status // empty' <<<"$DEPLOY_JSON" 2>/dev/null || true)"
    CLASS="$(python3 - "$STATUS" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "automation")
from render_lifecycle import classify_deploy_status
try:
    print(classify_deploy_status(sys.argv[1]))
except ValueError:
    print("unknown")
PY
)"
    if [[ "$CLASS" == "live" ]]; then
      echo "Deploy $DEPLOY_ID is live."
      break
    fi
    if [[ "$CLASS" == "failed" ]]; then
      echo "::error::Render deploy $DEPLOY_ID failed with status '$STATUS'." >&2
      exit 1
    fi
    if [[ "$i" -eq "$DEPLOY_ATTEMPTS" ]]; then
      echo "::error::Render deploy $DEPLOY_ID did not go live in time." >&2
      exit 1
    fi
    sleep "$DEPLOY_INTERVAL"
  done
else
  echo "No deploy id recorded; continuing to service health check."
fi

# Inspect service state and obtain the externally reachable service URL.
if ! api_call GET "$API_BASE/services/$SERVICE_ID"; then
  echo "::error::Could not retrieve Render service $SERVICE_ID (HTTP $API_HTTP_CODE)." >&2
  exit 1
fi
SERVICE_JSON="$API_BODY"
PLAN_NOW="$(jq -r '.serviceDetails.plan // .plan // empty' <<<"$SERVICE_JSON" 2>/dev/null || true)"
if [[ -n "$PLAN_NOW" && "$PLAN_NOW" != "free" ]]; then
  echo "::error::Render service $SERVICE_ID reports non-free plan '$PLAN_NOW'." >&2
  exit 1
fi
SERVICE_URL="$(jq -r '.serviceDetails.url // empty' <<<"$SERVICE_JSON" 2>/dev/null || true)"
if [[ -z "$SERVICE_URL" || "$SERVICE_URL" == "null" ]]; then
  echo "::error::Render service $SERVICE_ID has no externally reachable URL yet." >&2
  exit 1
fi
echo "Ephemeral service URL: $SERVICE_URL"

# Wait for the runner health endpoint (bounded; same service, no recreation).
for ((i = 1; i <= 30; i++)); do
  if curl -fsSL --max-time 10 "$SERVICE_URL/health" -o /dev/null 2>/dev/null; then
    echo "Runner is healthy."
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    echo "::error::Runner health check failed for $SERVICE_URL." >&2
    exit 1
  fi
  sleep 10
done

# Resolve issue/task text (read-only; GitHub writes stay in the workflow).
# Prefer the exact issue title/body via gh; fall back to a synthetic
# instruction when the API or credentials are unavailable (offline tests).
ISSUE_TITLE=""
ISSUE_BODY_TEXT=""
if command -v gh >/dev/null 2>&1 && [[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]]; then
  ISSUE_JSON="$(gh issue view "$ISSUE_NUMBER" --json title,body 2>/dev/null || true)"
  if [[ -n "$ISSUE_JSON" ]]; then
    ISSUE_TITLE="$(jq -r '.title // ""' <<<"$ISSUE_JSON" 2>/dev/null || true)"
    ISSUE_BODY_TEXT="$(jq -r '.body // ""' <<<"$ISSUE_JSON" 2>/dev/null || true)"
  fi
fi

# Resolve the exact base SHA in preference order: workflow-provided
# GITHUB_SHA, local git HEAD (main checkout), then the main ref API.
MAIN_REF_SHA=""
if command -v gh >/dev/null 2>&1 && [[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]]; then
  MAIN_REF_SHA="$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq '.object.sha' 2>/dev/null || true)"
fi
LOCAL_HEAD_SHA=""
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  LOCAL_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
fi
BASE_SHA="$(python3 - "${GITHUB_SHA:-}" "$LOCAL_HEAD_SHA" "$MAIN_REF_SHA" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import resolve_base_sha
print(resolve_base_sha(*sys.argv[1:]))
PY
)"
if [[ -n "$BASE_SHA" ]]; then
  echo "Resolved exact base SHA $BASE_SHA."
else
  echo "No exact base SHA resolvable; continuing without base_sha (runner treats it as absent)."
fi

# Build the minimum job payload and submit it to the runner.
TASK_TEXT="$(python3 - "$ISSUE_NUMBER" "$EXECUTION_MODE" "$ISSUE_TITLE" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import build_task_text
issue = int(sys.argv[1])
mode = sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else ""
body = sys.stdin.read()
print(build_task_text(issue, mode, title=title, body=body))
PY
<<<"$ISSUE_BODY_TEXT")"
JOB_PAYLOAD="$(python3 - "$ISSUE_NUMBER" "$REPO_URL" "$BASE_SHA" \
  "$RENDER_REGION" "$OPENCODE_MODEL" "$EXECUTION_MODE" "${GITHUB_RUN_ID:-}" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import JobRequest, ExecutionMetadata
issue = int(sys.argv[1])
task_text = sys.stdin.read()
meta = ExecutionMetadata(
    issue_number=issue,
    region=sys.argv[4],
    model=sys.argv[5],
    execution_mode=sys.argv[6],
    run_id=sys.argv[7] or "",
)
req = JobRequest(
    repository_url=sys.argv[2],
    base_ref="main",
    base_sha=sys.argv[3],
    task_text=task_text,
    issue_number=issue,
    metadata=meta,
)
print(json.dumps(req.to_dict()))
PY
<<<"$TASK_TEXT")"

SUBMIT_RESPONSE="$(curl -fsSL --max-time 30 -X POST "$SERVICE_URL/v1/jobs" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -d "$JOB_PAYLOAD")"
JOB_ID="$(jq -r '.job_id // .jobId // empty' <<<"$SUBMIT_RESPONSE" 2>/dev/null || true)"
if [[ -z "$JOB_ID" ]]; then
  echo "::error::Runner did not return a job identifier." >&2
  exit 1
fi
echo "Submitted runner job $JOB_ID."

# Poll the job status/result (bounded; same worker, no new service).
FALLBACK_MODEL="opencode/space-bunny-free"
TRIED_FALLBACK="no"
for ((i = 1; i <= 60; i++)); do
  RESULT_JSON="$(curl -fsSL --max-time 30 "$SERVICE_URL/v1/jobs/$JOB_ID" \
    -H "Accept: application/json" 2>/dev/null || true)"
  STATUS="$(jq -r '.status // empty' <<<"$RESULT_JSON" 2>/dev/null || true)"
  case "$STATUS" in
    succeeded)
      printf '%s\n' "$RESULT_JSON" > "$RENDER_RESULT_FILE"
      # Validate the complete result against the runner contract before
      # cleanup runs, so a malformed payload fails the attempt loudly.
      python3 - "$RENDER_RESULT_FILE" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import parse_job_result
with open(sys.argv[1], encoding="utf-8") as handle:
    parse_job_result(json.load(handle))
PY
      echo "Runner job $JOB_ID succeeded; complete result collected."
      exit 0
      ;;
    failed|timed_out)
      ERROR_TEXT="$(jq -r '.error // ""' <<<"$RESULT_JSON")"
      # Model fallback occurs inside the same worker attempt (no new service).
      if [[ "$TRIED_FALLBACK" == "no" && "$OPENCODE_MODEL" != "$FALLBACK_MODEL" ]] && \
         grep -qiE 'model|unavailable|not found' <<<"$ERROR_TEXT"; then
        echo "Primary model failed ($ERROR_TEXT); retrying with $FALLBACK_MODEL on the same worker."
        TRIED_FALLBACK="yes"
        RETRY_PAYLOAD="$(jq --arg m "$FALLBACK_MODEL" '.metadata.model = $m' <<<"$JOB_PAYLOAD")"
        SUBMIT_RESPONSE="$(curl -fsSL --max-time 30 -X POST "$SERVICE_URL/v1/jobs" \
          -H "Accept: application/json" \
          -H "Content-Type: application/json" \
          -d "$RETRY_PAYLOAD")"
        JOB_ID="$(jq -r '.job_id // .jobId // empty' <<<"$SUBMIT_RESPONSE")"
        [[ -n "$JOB_ID" ]] || { echo "::error::Fallback submit returned no job id." >&2; exit 1; }
        echo "Submitted fallback runner job $JOB_ID."
        continue
      fi
      printf '%s\n' "$RESULT_JSON" > "$RENDER_RESULT_FILE"
      echo "::error::Runner job $JOB_ID ended with status '$STATUS': $ERROR_TEXT" >&2
      exit 1
      ;;
    queued|running|"")
      if [[ "$i" -eq 60 ]]; then
        # Record the timeout before cleanup so the outcome is attributable.
        jq -n --arg jid "$JOB_ID" --arg sid "$SERVICE_ID" \
          --arg issue "$ISSUE_NUMBER" --arg mode "$EXECUTION_MODE" \
          '{job_id: $jid, status: "timed_out", success: false,
             error: "Runner job did not finish in time.",
             serviceId: $sid, issue_number: ($issue | tonumber),
             execution_mode: $mode}' > "$RENDER_RESULT_FILE"
        echo "::error::Runner job $JOB_ID did not finish in time." >&2
        exit 1
      fi
      sleep 20
      ;;
    *)
      echo "::error::Runner returned unknown job status '$STATUS'." >&2
      exit 1
      ;;
  esac
done
