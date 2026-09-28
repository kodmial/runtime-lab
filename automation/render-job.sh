#!/usr/bin/env bash
# TEMPORARY development harness only (issue #4): GitHub Actions -> Render.
#
# This script is the create -> readiness -> job -> result half of the
# temporary Actions control plane used to develop and test the Render
# lifecycle and runner before the direct GitHub->Render integration exists.
# It is NOT the final runtime architecture: the final production path must
# not require an OpenCode GitHub Actions job (cutover is owned by later
# issues). The reusable lifecycle core lives in automation/render_lifecycle.py
# so the future Render controller can call it directly instead of keeping
# this orchestration permanently embedded in Actions.
#
# Cleanup (delete + verify) lives in automation/render-cleanup.sh, which the
# stable render-executor workflow always runs afterwards (if: always()), even
# when this script fails or times out.
#
# Cost guards encoded here (see automation/render_lifecycle.py):
# - exactly one service creation per issue execution attempt (state-file
#   service id is reused; polling/deploy retries never create a new one);
# - concurrency: the workflow serializes per issue
#   (group runtime-lab-render-<issue>) so the same issue never owns more
#   than one worker, while the scheduler WIP limit allows up to four
#   different issues concurrently;
# - bounded retries only; no cron path in this script creates services;
# - model fallback reuses the same worker (no second service for fallback).
#
# Rate limits: Render answers 429 with a Retry-After header (see
# RENDER_DOC_RATE_LIMITING). api_request() below retries 429/5xx with
# bounded backoff honoring Retry-After instead of failing immediately.
#
# GitHub access from this script is READ-ONLY (gh issue view for task text,
# git rev-parse for the base SHA). All GitHub writes stay on the Actions
# workflow side (Finalize step) during this temporary phase.
#
# Auth: RENDER_API_KEY is mapped from the repository secret KEY by the stable
# workflow envelope. This script never prints it. OPENCODE_API_KEY is
# deliberately NOT required: the runner accepts jobs without it.
set -euo pipefail

: "${ISSUE_NUMBER:?ISSUE_NUMBER must be set}"
: "${EXECUTION_MODE:?EXECUTION_MODE must be set}"
: "${RENDER_API_KEY:?RENDER_API_KEY must be set by the workflow}"
: "${OPENCODE_API_KEY:-}"

RENDER_STATE_FILE="${RENDER_STATE_FILE:-/tmp/runtime-lab-render-state.json}"
RENDER_RESULT_FILE="${RENDER_RESULT_FILE:-/tmp/runtime-lab-render-result.json}"
RENDER_REGION="${RENDER_REGION:-oregon}"
OPENCODE_MODEL="${OPENCODE_MODEL:-opencode/muse-spark-1.3-contributor-free}"
REPO_URL="https://github.com/kodmial/runtime-lab"
API_BASE="https://api.render.com/v1"

# Bounded Render API retry envelope (mirrors render_lifecycle constants).
API_RETRY_MAX_ATTEMPTS="${API_RETRY_MAX_ATTEMPTS:-5}"
API_RETRY_BASE_SECONDS=5
API_RETRY_CAP_SECONDS=120
API_HTTP_CODE="000"

if [[ "$EXECUTION_MODE" != "smoke" && "$EXECUTION_MODE" != "e2e" ]]; then
  echo "::error::Unsupported execution mode: $EXECUTION_MODE" >&2
  exit 2
fi

# Validate region/model against the single source of truth (fail closed,
# before any Render service is created).
python3 - "$RENDER_REGION" "$OPENCODE_MODEL" "$EXECUTION_MODE" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import (
    validate_execution_mode,
    validate_model_name,
    validate_worker_region,
)
validate_worker_region(sys.argv[1])
validate_model_name(sys.argv[2])
validate_execution_mode(sys.argv[3])
PY

# ---------------------------------------------------------------------------
# Render API helper: bounded retry on 429/5xx honoring Retry-After.
# Usage: api_request <GET|POST> <url> [json-data] [out-file]
# On success (2xx) writes the response body to out-file (or stdout) and
# returns 0. On terminal failure returns non-zero with API_HTTP_CODE set.
# Never prints the Authorization header or key material.
# ---------------------------------------------------------------------------
retry_wait_secs() {
  local attempt="$1" retry_after="${2:-}" wait_secs=0
  if [[ "$retry_after" =~ ^[0-9]+$ ]]; then
    wait_secs="$retry_after"
    if [[ "$wait_secs" -gt "$API_RETRY_CAP_SECONDS" ]]; then
      wait_secs="$API_RETRY_CAP_SECONDS"
    fi
    echo "$wait_secs"
    return 0
  fi
  wait_secs=$((API_RETRY_BASE_SECONDS * (1 << (attempt - 1))))
  if [[ "$wait_secs" -gt "$API_RETRY_CAP_SECONDS" ]]; then
    wait_secs="$API_RETRY_CAP_SECONDS"
  fi
  echo "$wait_secs"
}

api_request() {
  local method="$1" url="$2" data="${3:-}" out_file="${4:-}"
  local attempt code body_file header_file retry_after wait_secs
  body_file="$(mktemp)"
  header_file="$(mktemp)"
  API_HTTP_CODE="000"
  for ((attempt = 1; attempt <= API_RETRY_MAX_ATTEMPTS; attempt++)); do
    : > "$body_file"
    : > "$header_file"
    if [[ -n "$data" ]]; then
      code="$(curl -sS --max-time 30 -D "$header_file" -o "$body_file" \
        -w '%{http_code}' -X "$method" \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $RENDER_API_KEY" \
        -d "$data" "$url" 2>/dev/null || true)"
    else
      code="$(curl -sS --max-time 30 -D "$header_file" -o "$body_file" \
        -w '%{http_code}' -X "$method" \
        -H "Accept: application/json" \
        -H "Authorization: Bearer $RENDER_API_KEY" \
        "$url" 2>/dev/null || true)"
    fi
    code="$(tr -dc '0-9' <<<"$code" || true)"
    [[ -z "$code" ]] && code="000"
    API_HTTP_CODE="$code"
    if [[ "$code" == "429" || "$code" == "500" || "$code" == "502" || "$code" == "503" || "$code" == "504" ]]; then
      retry_after="$(grep -i '^retry-after:' "$header_file" 2>/dev/null | tail -n 1 | cut -d: -f2- | tr -d ' \r\n' || true)"
      if [[ "$attempt" -lt "$API_RETRY_MAX_ATTEMPTS" ]]; then
        wait_secs="$(retry_wait_secs "$attempt" "$retry_after")"
        echo "Render API $method returned HTTP $code; respecting Retry-After and retrying in ${wait_secs}s (attempt $attempt/$API_RETRY_MAX_ATTEMPTS)." >&2
        sleep "$wait_secs"
        continue
      fi
      echo "::error::Render API $method failed with HTTP $code after $API_RETRY_MAX_ATTEMPTS attempts." >&2
      rm -f "$body_file" "$header_file"
      return 1
    fi
    break
  done
  if [[ -n "$out_file" ]]; then
    cat "$body_file" > "$out_file"
  else
    cat "$body_file"
  fi
  rm -f "$body_file" "$header_file"
  case "$API_HTTP_CODE" in
    2*) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Resolve the real issue/task text (read-only) and the exact base SHA.
# GitHub writes stay in the workflow; failures here fall back to safe
# defaults so a read outage cannot silently change what gets executed.
# ---------------------------------------------------------------------------
ISSUE_TITLE=""
ISSUE_BODY_TEXT=""
if command -v gh >/dev/null 2>&1 && [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
  ISSUE_JSON="$(gh issue view "$ISSUE_NUMBER" --json title,body 2>/dev/null || true)"
  if [[ -n "$ISSUE_JSON" ]]; then
    ISSUE_TITLE="$(jq -r '.title // empty' <<<"$ISSUE_JSON" 2>/dev/null || true)"
    ISSUE_BODY_TEXT="$(jq -r '.body // empty' <<<"$ISSUE_JSON" 2>/dev/null || true)"
  fi
fi

GIT_HEAD_SHA=""
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  GIT_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
fi
BASE_SHA="$(ISSUE_TITLE="$ISSUE_TITLE" ISSUE_BODY_TEXT="$ISSUE_BODY_TEXT" python3 - "${GITHUB_SHA:-}" "$GIT_HEAD_SHA" <<'PY'
import os, sys
sys.path.insert(0, "automation")
from render_lifecycle import select_base_sha
print(select_base_sha(sys.argv[1], sys.argv[2]))
PY
)"
TASK_TEXT="$(ISSUE_TITLE="$ISSUE_TITLE" ISSUE_BODY_TEXT="$ISSUE_BODY_TEXT" python3 - "$ISSUE_NUMBER" "$EXECUTION_MODE" "${GITHUB_RUN_ID:-}" <<'PY'
import os, sys
sys.path.insert(0, "automation")
from render_lifecycle import resolve_task_text
print(resolve_task_text(int(sys.argv[1]), sys.argv[2],
      title=os.environ.get("ISSUE_TITLE", ""),
      body=os.environ.get("ISSUE_BODY_TEXT", ""),
      run_id=sys.argv[3]))
PY
)"

# One service creation per attempt: reuse an existing state file service id.
# This is the "never retry by creating a second worker" enforcement: any
# retry path below reuses SERVICE_ID from RENDER_STATE_FILE.
EXISTING_SERVICE_ID=""
if [[ -s "$RENDER_STATE_FILE" ]]; then
  EXISTING_SERVICE_ID="$(jq -r '.serviceId // empty' "$RENDER_STATE_FILE" 2>/dev/null || true)"
fi
if [[ -n "$EXISTING_SERVICE_ID" ]]; then
  echo "Reusing existing ephemeral service $EXISTING_SERVICE_ID; refusing to create a second one."
  SERVICE_ID="$EXISTING_SERVICE_ID"
else
  # Resolve the Render workspace (owner) id without extra secrets.
  OWNERS_FILE="$(mktemp)"
  if ! api_request GET "$API_BASE/owners?limit=20" "" "$OWNERS_FILE"; then
    echo "::error::Could not resolve a Render owner id for service creation (HTTP $API_HTTP_CODE)." >&2
    rm -f "$OWNERS_FILE"
    exit 1
  fi
  OWNERS_JSON="$(cat "$OWNERS_FILE")"
  if ! OWNER_ID="$(python3 - "$OWNERS_FILE" <<'PY'
import json
import sys
sys.path.insert(0, "automation")
from render_lifecycle import extract_owner_id

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(extract_owner_id(payload))
PY
  )"; then
    OWNER_SHAPE="$(jq -c '
      if type == "array" and length > 0 and (.[0] | type) == "object" then
        {type: type, first_keys: (.[0] | keys), owner_keys: ((.[0].owner // {}) | keys)}
      else
        {type: type, length: (if type == "array" then length else null end)}
      end
    ' "$OWNERS_FILE" 2>/dev/null || echo '{"shape":"unavailable"}')"
    rm -f "$OWNERS_FILE"
    echo "::error::Could not resolve a Render owner id from the documented List Workspaces response shape: $OWNER_SHAPE" >&2
    exit 1
  fi
  rm -f "$OWNERS_FILE"

  RUN_ID_SAFE="${GITHUB_RUN_ID:-local}"
  SERVICE_NAME="runtime-lab-issue${ISSUE_NUMBER}-${RUN_ID_SAFE}"
  CREATE_PAYLOAD="$(python3 - "$SERVICE_NAME" "$OWNER_ID" "$RENDER_REGION" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import build_create_service_payload
print(json.dumps(build_create_service_payload(
    name=sys.argv[1], owner_id=sys.argv[2], region=sys.argv[3])))
PY
)"

  # Create exactly one temporary web service (POST /v1/services -> 201).
  CREATE_FILE="$(mktemp)"
  if ! api_request POST "$API_BASE/services" "$CREATE_PAYLOAD" "$CREATE_FILE"; then
    echo "::error::Render service creation failed (HTTP $API_HTTP_CODE)." >&2
    rm -f "$CREATE_FILE"
    exit 1
  fi
  CREATE_RESPONSE="$(cat "$CREATE_FILE")"
  rm -f "$CREATE_FILE"
  SERVICE_ID="$(jq -r '.service.id // empty' <<<"$CREATE_RESPONSE")"
  DEPLOY_ID="$(jq -r '.deployId // empty' <<<"$CREATE_RESPONSE")"
  PLAN="$(jq -r '.service.serviceDetails.plan // empty' <<<"$CREATE_RESPONSE")"
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
    --arg plan "$PLAN" --arg issue "$ISSUE_NUMBER" \
    --arg mode "$EXECUTION_MODE" --arg sha "$BASE_SHA" \
    '{serviceId: $sid, deployId: $dep, region: $region, model: $model, plan: $plan, issue: $issue, mode: $mode, baseSha: $sha}' \
    > "$RENDER_STATE_FILE"
  echo "Created ephemeral Render service $SERVICE_ID (plan=free, region=$RENDER_REGION)."
fi

SERVICE_ID="$(jq -r '.serviceId // empty' "$RENDER_STATE_FILE")"
DEPLOY_ID="$(jq -r '.deployId // empty' "$RENDER_STATE_FILE")"
[[ -n "$SERVICE_ID" ]] || { echo "::error::Render state has no service id." >&2; exit 1; }

# Wait for the deploy to become live, reusing the same service (bounded).
DEPLOY_ATTEMPTS=60
DEPLOY_INTERVAL=20
if [[ -n "$DEPLOY_ID" ]]; then
  for ((i = 1; i <= DEPLOY_ATTEMPTS; i++)); do
    DEPLOY_FILE="$(mktemp)"
    DEPLOY_JSON="$(api_request GET \
      "$API_BASE/services/$SERVICE_ID/deploys/$DEPLOY_ID" "" "$DEPLOY_FILE" 2>/dev/null \
      && cat "$DEPLOY_FILE" || true)"
    rm -f "$DEPLOY_FILE"
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
SERVICE_FILE="$(mktemp)"
if ! api_request GET "$API_BASE/services/$SERVICE_ID" "" "$SERVICE_FILE"; then
  echo "::error::Could not retrieve Render service $SERVICE_ID (HTTP $API_HTTP_CODE)." >&2
  rm -f "$SERVICE_FILE"
  exit 1
fi
SERVICE_JSON="$(cat "$SERVICE_FILE")"
rm -f "$SERVICE_FILE"
PLAN_NOW="$(jq -r '.serviceDetails.plan // .plan // empty' <<<"$SERVICE_JSON" 2>/dev/null || true)"
if [[ -n "$PLAN_NOW" && "$PLAN_NOW" != "free" ]]; then
  echo "::error::Render service $SERVICE_ID reports non-free plan '$PLAN_NOW'." >&2
  exit 1
fi
SERVICE_URL="$(jq -r '.serviceDetails.url // empty' <<<"$SERVICE_JSON")"
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

# Build the minimum job payload and submit it to the runner.
JOB_PAYLOAD="$(python3 - "$ISSUE_NUMBER" "$REPO_URL" "$TASK_TEXT" "$BASE_SHA" \
  "$RENDER_REGION" "$OPENCODE_MODEL" "$EXECUTION_MODE" "${GITHUB_RUN_ID:-}" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import JobRequest, ExecutionMetadata
issue = int(sys.argv[1])
meta = ExecutionMetadata(
    issue_number=issue,
    region=sys.argv[5],
    model=sys.argv[6],
    execution_mode=sys.argv[7],
    run_id=sys.argv[8] or "",
)
req = JobRequest(
    repository_url=sys.argv[2],
    base_ref="main",
    base_sha=sys.argv[4],
    task_text=sys.argv[3],
    issue_number=issue,
    metadata=meta,
)
print(json.dumps(req.to_dict()))
PY
)"

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
# Budget 140x20s=2800s covers the runner execution timeout of 45 minutes
# (see JOB_POLL_MAX_ATTEMPTS in automation/render_lifecycle.py); run
# 36399649036 failed prematurely with only 60x20s=1200s while the runner was
# still legitimately working.
#
# Poll-outcome discipline (regression for run 36402447309): that run polled
# an empty status for the full budget because `curl -fsSL ... || true`
# collapsed every failure (unknown-job 404, 429/5xx, connection errors,
# empty bodies) into "" and the loop treated "" as queued/running. Empty is
# not evidence the job is still working. Each attempt is therefore
# classified with classify_job_poll_response() from the HTTP status plus
# the parsed status field: terminal states finish, queued/running waits, a
# persistent unknown-job 404/410 fails fast (jobs live in worker process
# memory, so a worker restart loses the job permanently and the id can
# never become terminal), and transport failures are retried within the
# same budget with periodic /health re-probes plus a diagnostic final
# error instead of a bare "last status 'empty'".
#
# Job-loss recovery (regression for run 36409152332): that run polled a
# submitted job as pending for ~2 minutes before it turned into a
# permanent unknown-job 404 with a healthy runner. Render may restart a
# Free web service at any time (see RENDER_DOC_FREE_TIER in
# automation/render_lifecycle.py) and the runner keeps jobs only in
# worker process memory, so a restart wipes the submitted job id while
# /health answers 200 again on the fresh process. Because the submit
# payload is fully reproducible, one bounded same-worker resubmission
# (JOB_POLL_MAX_JOB_RESUBMITS, never a second Render service) converts a
# transient restart into a retry; a second consecutive loss still fails
# fast. A submit-time /health snapshot (uptime_seconds) is compared with
# the loss-time reading via detect_worker_restart() so the diagnostic
# states whether a restart was actually observed.
FALLBACK_MODEL="opencode/space-bunny-free"
TRIED_FALLBACK="no"
POLL_UNKNOWN_COUNT=0
POLL_EMPTY_COUNT=0
POLL_LAST_CODE="000"
POLL_RESUBMITS=0
POLL_UNKNOWN_THRESHOLD="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_UNKNOWN_JOB_THRESHOLD
print(JOB_POLL_UNKNOWN_JOB_THRESHOLD)
PY
)"
POLL_HEALTH_EVERY="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY
print(JOB_POLL_TRANSPORT_HEALTH_CHECK_EVERY)
PY
)"
POLL_MAX_RESUBMITS="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_MAX_JOB_RESUBMITS
print(JOB_POLL_MAX_JOB_RESUBMITS)
PY
)"
[[ "$POLL_MAX_RESUBMITS" =~ ^[0-9]+$ ]] || POLL_MAX_RESUBMITS=1
# Submit-time runner health snapshot (best-effort restart-evidence
# baseline; never fails the attempt when the body is unavailable).
SUBMIT_HEALTH_JSON="$(curl -sS --max-time 10 "$SERVICE_URL/health" \
  -H "Accept: application/json" 2>/dev/null || true)"
SUBMIT_UPTIME="$(jq -r '.uptime_seconds // empty' <<<"$SUBMIT_HEALTH_JSON" 2>/dev/null || true)"
for ((i = 1; i <= 140; i++)); do
  POLL_BODY_FILE="$(mktemp)"
  POLL_LAST_CODE="$(curl -sS -o "$POLL_BODY_FILE" -w '%{http_code}' --max-time 30 \
    "$SERVICE_URL/v1/jobs/$JOB_ID" -H "Accept: application/json" 2>/dev/null || true)"
  POLL_LAST_CODE="$(tr -dc '0-9' <<<"$POLL_LAST_CODE" || true)"
  [[ -z "$POLL_LAST_CODE" ]] && POLL_LAST_CODE="000"
  RESULT_JSON="$(cat "$POLL_BODY_FILE" 2>/dev/null || true)"
  rm -f "$POLL_BODY_FILE"
  STATUS="$(jq -r '.status // empty' <<<"$RESULT_JSON" 2>/dev/null || true)"
  POLL_OUTCOME="$(python3 - "$POLL_LAST_CODE" "$STATUS" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import classify_job_poll_response
try:
    code = int(sys.argv[1])
except ValueError:
    code = None
print(classify_job_poll_response(code, sys.argv[2]))
PY
)"
  case "$POLL_OUTCOME" in
    succeeded)
      printf '%s\n' "$RESULT_JSON" > "$RENDER_RESULT_FILE"
      echo "Runner job $JOB_ID succeeded."
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
        # Later same-worker resubmissions must reuse the payload that is
        # actually in flight (fallback model), not the primary payload.
        JOB_PAYLOAD="$RETRY_PAYLOAD"
        POLL_UNKNOWN_COUNT=0
        POLL_EMPTY_COUNT=0
        continue
      fi
      printf '%s\n' "$RESULT_JSON" > "$RENDER_RESULT_FILE"
      echo "::error::Runner job $JOB_ID ended with status '$POLL_OUTCOME': $ERROR_TEXT" >&2
      exit 1
      ;;
    pending)
      POLL_UNKNOWN_COUNT=0
      POLL_EMPTY_COUNT=0
      if [[ "$i" -eq 140 ]]; then
        echo "::error::Runner job $JOB_ID did not finish in time (last status '${STATUS:-empty}')." >&2
        exit 1
      fi
      sleep 20
      ;;
    unknown_job)
      POLL_UNKNOWN_COUNT=$((POLL_UNKNOWN_COUNT + 1))
      POLL_EMPTY_COUNT=0
      if [[ "$POLL_UNKNOWN_COUNT" -ge "$POLL_UNKNOWN_THRESHOLD" ]]; then
        CURRENT_HEALTH_JSON="$(curl -sS --max-time 10 "$SERVICE_URL/health" \
          -H "Accept: application/json" 2>/dev/null || true)"
        if curl -fsSL --max-time 10 "$SERVICE_URL/health" -o /dev/null 2>/dev/null; then
          RUNNER_HEALTH="healthy"
        else
          RUNNER_HEALTH="unreachable"
        fi
        CURRENT_UPTIME="$(jq -r '.uptime_seconds // empty' <<<"$CURRENT_HEALTH_JSON" 2>/dev/null || true)"
        RESTART_EVIDENCE="$(python3 - "$SUBMIT_UPTIME" "$CURRENT_UPTIME" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import detect_worker_restart
prior, current = sys.argv[1], sys.argv[2]
result = detect_worker_restart(prior, current)
if result is True:
    print("worker restart observed (uptime %s -> %s)" % (prior, current))
elif result is False:
    print("same worker process lifetime (uptime %s -> %s)" % (prior, current))
else:
    print("no uptime evidence (submit=%s current=%s)"
          % (prior or "?", current or "?"))
PY
)"
        # Same-worker resubmission (regression for run 36409152332): the
        # payload is fully reproducible and the worker is healthy again,
        # so retry once on the SAME worker instead of failing the whole
        # attempt on a transient restart. Never creates a second service.
        if [[ "$RUNNER_HEALTH" == "healthy" && "$POLL_RESUBMITS" -lt "$POLL_MAX_RESUBMITS" ]]; then
          echo "Runner lost job $JOB_ID ($RESTART_EVIDENCE); resubmitting the same payload on the same worker (resubmission $((POLL_RESUBMITS + 1))/$POLL_MAX_RESUBMITS, no new service)."
          LOST_JOB_ID="$JOB_ID"
          RESUBMIT_RESPONSE="$(curl -fsSL --max-time 30 -X POST "$SERVICE_URL/v1/jobs" \
            -H "Accept: application/json" \
            -H "Content-Type: application/json" \
            -d "$JOB_PAYLOAD" || true)"
          JOB_ID="$(jq -r '.job_id // .jobId // empty' <<<"$RESUBMIT_RESPONSE" 2>/dev/null || true)"
          if [[ -z "$JOB_ID" ]]; then
            echo "::error::Runner lost job $LOST_JOB_ID ($RESTART_EVIDENCE) and same-worker resubmission returned no job id; failing instead of polling a dead id." >&2
            exit 1
          fi
          echo "Resubmitted runner job $JOB_ID (replaces lost job $LOST_JOB_ID)."
          POLL_RESUBMITS=$((POLL_RESUBMITS + 1))
          POLL_UNKNOWN_COUNT=0
          POLL_EMPTY_COUNT=0
          SUBMIT_UPTIME="$CURRENT_UPTIME"
          continue
        fi
        echo "::error::Runner no longer knows job $JOB_ID (HTTP $POLL_LAST_CODE on $POLL_UNKNOWN_COUNT consecutive polls; runner health: $RUNNER_HEALTH; resubmissions used: $POLL_RESUBMITS/$POLL_MAX_RESUBMITS; $RESTART_EVIDENCE). Jobs live in worker process memory, so a worker restart loses the job permanently; failing fast instead of waiting out the full poll budget." >&2
        exit 1
      fi
      if [[ "$i" -eq 140 ]]; then
        echo "::error::Runner job $JOB_ID did not finish in time (last status '${STATUS:-empty}', HTTP $POLL_LAST_CODE, $POLL_UNKNOWN_COUNT consecutive unknown-job polls, $POLL_RESUBMITS resubmission(s) used)." >&2
        exit 1
      fi
      sleep 20
      ;;
    transport_error)
      POLL_EMPTY_COUNT=$((POLL_EMPTY_COUNT + 1))
      POLL_UNKNOWN_COUNT=0
      if (( POLL_EMPTY_COUNT % POLL_HEALTH_EVERY == 0 )); then
        if curl -fsSL --max-time 10 "$SERVICE_URL/health" -o /dev/null 2>/dev/null; then
          echo "Job $JOB_ID poll hit $POLL_EMPTY_COUNT consecutive transport failures (last HTTP $POLL_LAST_CODE); runner still healthy, continuing within the poll budget." >&2
        else
          echo "::error::Runner job $JOB_ID poll failed $POLL_EMPTY_COUNT consecutive times (last HTTP $POLL_LAST_CODE) and the runner health check is also failing; failing fast instead of waiting out the full poll budget." >&2
          exit 1
        fi
      fi
      if [[ "$i" -eq 140 ]]; then
        echo "::error::Runner job $JOB_ID did not finish in time (last HTTP $POLL_LAST_CODE, last status '${STATUS:-empty}', $POLL_EMPTY_COUNT consecutive transport failures)." >&2
        exit 1
      fi
      sleep 20
      ;;
    *)
      echo "::error::Runner returned unknown job status '$STATUS' (HTTP $POLL_LAST_CODE)." >&2
      exit 1
      ;;
  esac
done
