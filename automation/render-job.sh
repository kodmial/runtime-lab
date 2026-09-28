#!/usr/bin/env bash
# Ephemeral Render execution: create -> wait healthy -> run job -> collect.
# Cleanup (delete + verify) lives in automation/render-cleanup.sh, which the
# render-executor workflow always runs afterwards, even when this script fails.
#
# Cost guards encoded here (see automation/render_lifecycle.py):
# - exactly one service creation per issue execution attempt;
# - polling/deploy retries reuse the same service id, never create a new one;
# - bounded retries only; no cron path in this script creates services.
#
# Auth: RENDER_API_KEY is mapped from the repository secret KEY by the stable
# workflow envelope. This script never prints it.
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

# Validate region/model against the single source of truth (fail closed).
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

# One service creation per attempt: reuse an existing state file service id.
EXISTING_SERVICE_ID=""
if [[ -s "$RENDER_STATE_FILE" ]]; then
  EXISTING_SERVICE_ID="$(jq -r '.serviceId // empty' "$RENDER_STATE_FILE" 2>/dev/null || true)"
fi
if [[ -n "$EXISTING_SERVICE_ID" ]]; then
  echo "Reusing existing ephemeral service $EXISTING_SERVICE_ID; refusing to create a second one."
  SERVICE_ID="$EXISTING_SERVICE_ID"
else
  # Resolve the Render workspace (owner) id without extra secrets.
  OWNERS_JSON="$(curl -fsSL --max-time 30 "$API_BASE/owners?limit=20" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY")"
  OWNER_ID="$(jq -r '.[0].id // empty' <<<"$OWNERS_JSON")"
  if [[ -z "$OWNER_ID" ]]; then
    echo "::error::Could not resolve a Render owner id for service creation." >&2
    exit 1
  fi

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
  CREATE_RESPONSE="$(curl -fsSL --max-time 60 -X POST "$API_BASE/services" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" \
    -d "$CREATE_PAYLOAD")"
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
    --arg plan "$PLAN" \
    '{serviceId: $sid, deployId: $dep, region: $region, model: $model, plan: $plan}' \
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
    DEPLOY_JSON="$(curl -fsSL --max-time 30 \
      "$API_BASE/services/$SERVICE_ID/deploys/$DEPLOY_ID" \
      -H "Accept: application/json" \
      -H "Authorization: Bearer $RENDER_API_KEY" || true)"
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
SERVICE_JSON="$(curl -fsSL --max-time 30 "$API_BASE/services/$SERVICE_ID" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $RENDER_API_KEY")"
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
BASE_SHA="${GITHUB_SHA:-}"
if [[ -z "$BASE_SHA" ]] && git rev-parse --verify HEAD >/dev/null 2>&1; then
  BASE_SHA="$(git rev-parse HEAD)"
fi
TASK_TEXT="Execute issue #${ISSUE_NUMBER} in ${EXECUTION_MODE} mode."
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
FALLBACK_MODEL="opencode/space-bunny-free"
TRIED_FALLBACK="no"
for ((i = 1; i <= 60; i++)); do
  RESULT_JSON="$(curl -fsSL --max-time 30 "$SERVICE_URL/v1/jobs/$JOB_ID" \
    -H "Accept: application/json" 2>/dev/null || true)"
  STATUS="$(jq -r '.status // empty' <<<"$RESULT_JSON" 2>/dev/null || true)"
  case "$STATUS" in
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
        continue
      fi
      printf '%s\n' "$RESULT_JSON" > "$RENDER_RESULT_FILE"
      echo "::error::Runner job $JOB_ID ended with status '$STATUS': $ERROR_TEXT" >&2
      exit 1
      ;;
    queued|running|"")
      if [[ "$i" -eq 60 ]]; then
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
