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
# Cross-repository execution target (issue #85): explicit allow-listed
# "owner/repo" (default: kodmial/runtime-lab self-target). The worker
# clones the target repo; issue/task text stays sourced from this repo.
TARGET_REPO="${TARGET_REPO:-kodmial/runtime-lab}"
TARGET_BASE_SHA="${TARGET_BASE_SHA:-}"
REPO_URL="$(python3 - "$TARGET_REPO" <<'PY'
import sys
sys.path.insert(0, "automation")
from cross_repo import normalize_target_repo, target_repo_url
full = normalize_target_repo(sys.argv[1])
print(target_repo_url(full))
PY
)" || { echo "::error::TARGET_REPO '$TARGET_REPO' is not allow-listed." >&2; exit 2; }
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
# Cross-repo base SHA rule (issue #85): the local checkout HEAD is the
# self-target SHA only. For a cross-repo target the exact base SHA must
# come from TARGET_BASE_SHA (pinned by the caller) or from the target
# remote HEAD via ls-remote; never reuse the local runtime-lab SHA.
if [[ "$TARGET_REPO" != "kodmial/runtime-lab" && -z "$TARGET_BASE_SHA" ]]; then
  TARGET_BASE_SHA="$(git ls-remote "$REPO_URL" HEAD 2>/dev/null | awk '{print $1}' || true)"
fi
if [[ "$TARGET_REPO" != "kodmial/runtime-lab" && -z "$TARGET_BASE_SHA" ]]; then
  echo "::error::Cross-repo target '$TARGET_REPO' requires TARGET_BASE_SHA (could not resolve target HEAD)." >&2
  exit 2
fi
BASE_SHA_INPUT_1="${GITHUB_SHA:-}"
BASE_SHA_INPUT_2="$GIT_HEAD_SHA"
if [[ -n "$TARGET_BASE_SHA" ]]; then
  BASE_SHA_INPUT_1="$TARGET_BASE_SHA"
  BASE_SHA_INPUT_2="$TARGET_BASE_SHA"
fi
BASE_SHA="$(ISSUE_TITLE="$ISSUE_TITLE" ISSUE_BODY_TEXT="$ISSUE_BODY_TEXT" python3 - "$BASE_SHA_INPUT_1" "$BASE_SHA_INPUT_2" <<'PY'
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

# Exact workflow-artifact gate (regression for run 36495681860, issue
# #115; supported path for issue #128): when the issue demands one exact
# GitHub Actions artifact checksum-verified with no rebuild and no binary
# substitution, unsupported contracts still fail closed here, before any
# Render service is created, instead of burning a worker on a run that
# cannot test what the issue asks (run 36495681860 silently substituted
# the baseline pinned binary, whose ~600 MB agent OOM-restarted four
# times and storm-aborted after ~13 minutes). The supported corrected
# artifact (11004835952 from run 36498663107) passes through this gate:
# the controller downloads it with its own credential, verifies archive
# + binary checksums, pushes the verified bytes to the worker after boot,
# and selects it machine-readably in the submit/create payload, while the
# worker rejects missing/mismatched bytes and launches the absolute path
# with /proc identity proof (see automation/exact_artifact_delivery.py).
EXACT_GATE_FILE="$(mktemp)"
ISSUE_TITLE="$ISSUE_TITLE" ISSUE_BODY_TEXT="$ISSUE_BODY_TEXT" python3 - <<'PY' > "$EXACT_GATE_FILE"
import json, os, sys
sys.path.insert(0, "automation")
from render_lifecycle import (
    exact_artifact_gate_decision,
    exact_workflow_artifact_blocker,
    parse_exact_workflow_artifact_requirement,
)
title = os.environ.get("ISSUE_TITLE", "")
body = os.environ.get("ISSUE_BODY_TEXT", "")
requirement, blocker, identity = exact_artifact_gate_decision(title, body)
# Keep the historic helper references so the pre-creation placement
# regression keeps proving the gate runs before any Render API call.
_ = (parse_exact_workflow_artifact_requirement, exact_workflow_artifact_blocker)
print(json.dumps({"blocker": blocker or "", "identity": identity or {}}))
PY
EXACT_ARTIFACT_BLOCKER="$(python3 - "$EXACT_GATE_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["blocker"])
PY
)"
EXACT_IDENTITY_JSON="$(python3 - "$EXACT_GATE_FILE" <<'PY'
import json, sys
identity = json.load(open(sys.argv[1]))["identity"]
print(json.dumps(identity, sort_keys=True) if identity else "")
PY
)"
rm -f "$EXACT_GATE_FILE"
EXACT_MODE="no"
if [[ -n "$EXACT_ARTIFACT_BLOCKER" ]]; then
  echo "::error::$EXACT_ARTIFACT_BLOCKER" >&2
  # Structured refusal record (issue #125, run 36500759174): the log
  # line above is correct but machine-unreadable, so every gated
  # refusal re-mints an identical repair investigation from scraped
  # text. Write the stable infrastructure-blocked record for future
  # triage/schedulers while still creating no Render service. Best
  # effort: a write failure must never mask the refusal itself.
  ISSUE_TITLE="$ISSUE_TITLE" ISSUE_BODY_TEXT="$ISSUE_BODY_TEXT" python3 - "$ISSUE_NUMBER" "${GITHUB_RUN_ID:-}" "$RENDER_RESULT_FILE" <<'PY' 2>/dev/null || true
import json, os, sys
sys.path.insert(0, "automation")
from render_lifecycle import (
    build_exact_artifact_refusal_result,
    parse_exact_workflow_artifact_requirement,
)
requirement = parse_exact_workflow_artifact_requirement(
    os.environ.get("ISSUE_TITLE", ""),
    os.environ.get("ISSUE_BODY_TEXT", ""),
)
record = build_exact_artifact_refusal_result(
    requirement, issue_number=sys.argv[1], run_id=sys.argv[2])
target = sys.argv[3] if len(sys.argv) > 3 else ""
if target:
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
    except OSError:
        pass
PY
  exit 1
fi
if [[ -n "$EXACT_IDENTITY_JSON" ]]; then
  EXACT_MODE="yes"
  echo "Exact-artifact mode: supported contract selected (${EXACT_IDENTITY_JSON:0:120}...)."
fi

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
  CREATE_PAYLOAD="$(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 - "$SERVICE_NAME" "$OWNER_ID" "$RENDER_REGION" <<'PY'
import json, os, sys
sys.path.insert(0, "automation")
from render_lifecycle import build_create_service_payload
raw = os.environ.get("EXACT_IDENTITY_JSON", "").strip()
exact = json.loads(raw) if raw else None
print(json.dumps(build_create_service_payload(
    name=sys.argv[1], owner_id=sys.argv[2], region=sys.argv[3],
    exact_artifact=exact)))
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

# ---------------------------------------------------------------------------
# Container memory telemetry (issue #57): external continuous sampling.
# Starts here -- after the worker is healthy and before the OpenCode job is
# submitted -- and runs until the job reaches a terminal state or cleanup
# begins. The sampler lives on the GitHub Actions side (outside the Render
# container) and stores JSONL samples there, so pre-restart telemetry
# survives a Render restart that wipes in-process worker state. Sampling is
# bounded (1s interval, self-terminating well inside the poll budget) and
# stops on every terminal path via the EXIT trap below. The sampler only
# fetches the unauthenticated /health endpoint and never handles secrets.
# ---------------------------------------------------------------------------
RENDER_MEMORY_SAMPLES_FILE="${RENDER_MEMORY_SAMPLES_FILE:-/tmp/runtime-lab-memory-samples-${ISSUE_NUMBER}-${GITHUB_RUN_ID:-local}.jsonl}"
RENDER_MEMORY_SUMMARY_FILE="${RENDER_MEMORY_SUMMARY_FILE:-/tmp/runtime-lab-memory-summary-${ISSUE_NUMBER}-${GITHUB_RUN_ID:-local}.json}"
RENDER_MEMORY_STOP_FILE="${RENDER_MEMORY_STOP_FILE:-/tmp/runtime-lab-memory-stop-${ISSUE_NUMBER}-${GITHUB_RUN_ID:-local}}"
RENDER_MEMORY_INTERVAL="${RENDER_MEMORY_SAMPLE_INTERVAL_SECONDS:-1}"
MEMORY_SAMPLER_PID=""
memory_sampler_record_event() {
  # Best-effort harness event marker (e.g. same-worker resubmission) with a
  # timestamp around the transition; never fails the attempt.
  local name="$1" detail="${2:-}"
  python3 - "$RENDER_MEMORY_SAMPLES_FILE" "$name" "$detail" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "automation")
from render_memory_sampler import event_marker
event_marker(sys.argv[1], sys.argv[2], sys.argv[3])
PY
}
memory_sampler_stop_and_summarize() {
  # Stop the background sampler (bounded wait, then SIGKILL), emit the
  # machine-readable + human-readable summary to the Actions log and the
  # summary file, and best-effort merge the summary into the collected
  # job result as memory_telemetry (extra keys are ignored by the result
  # contract parser). Runs on every terminal path via the EXIT trap.
  if [[ -n "$MEMORY_SAMPLER_PID" ]] && kill -0 "$MEMORY_SAMPLER_PID" 2>/dev/null; then
    touch "$RENDER_MEMORY_STOP_FILE" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$MEMORY_SAMPLER_PID" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$MEMORY_SAMPLER_PID" 2>/dev/null || true
    wait "$MEMORY_SAMPLER_PID" 2>/dev/null || true
  fi
  MEMORY_SAMPLER_PID=""
  if [[ -n "${RENDER_MEMORY_SAMPLES_FILE:-}" ]]; then
    python3 - "$RENDER_MEMORY_SAMPLES_FILE" "$RENDER_MEMORY_SUMMARY_FILE" "$RENDER_MEMORY_INTERVAL" <<'PY' 2>&1 || true
import sys
sys.path.insert(0, "automation")
from render_memory_sampler import read_samples, summarize_samples, render_human_summary
import json
samples = read_samples(sys.argv[1])
summary = summarize_samples(samples, interval_seconds=float(sys.argv[3] or 1))
print(render_human_summary(summary))
if sys.argv[2]:
    try:
        with open(sys.argv[2], "w", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, sort_keys=True, indent=2) + "\n")
        print("Memory summary written to %s (%d samples)." % (sys.argv[2], summary.get("samples", 0)))
    except OSError as exc:
        print("Could not write memory summary file: %s" % exc)
PY
    # Best-effort evidence merge: attach the summary to the collected job
    # result without changing its status/success contract fields.
    if [[ -s "$RENDER_MEMORY_SUMMARY_FILE" && -s "${RENDER_RESULT_FILE:-}" ]]; then
      python3 - "$RENDER_RESULT_FILE" "$RENDER_MEMORY_SUMMARY_FILE" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        result = json.load(handle)
    with open(sys.argv[2], "r", encoding="utf-8") as handle:
        summary = json.load(handle)
    if isinstance(result, dict) and isinstance(summary, dict):
        result["memory_telemetry"] = summary
        with open(sys.argv[1], "w", encoding="utf-8") as handle:
            handle.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
except (OSError, ValueError):
    pass
PY
    fi
  fi
}
trap memory_sampler_stop_and_summarize EXIT
rm -f "$RENDER_MEMORY_STOP_FILE" 2>/dev/null || true
python3 automation/render_memory_sampler.py sample \
  --base-url "$SERVICE_URL" \
  --output "$RENDER_MEMORY_SAMPLES_FILE" \
  --interval "$RENDER_MEMORY_INTERVAL" \
  --max-seconds 2900 \
  --stop-file "$RENDER_MEMORY_STOP_FILE" \
  >/tmp/runtime-lab-memory-sampler-${ISSUE_NUMBER}-${GITHUB_RUN_ID:-local}.log 2>&1 &
MEMORY_SAMPLER_PID="$!"
echo "Memory sampler started (pid $MEMORY_SAMPLER_PID, interval ${RENDER_MEMORY_INTERVAL}s)."

# Exact-artifact delivery (issue #128, supported contract only): the
# controller downloads artifact 11004835952 with its own GitHub
# credential, verifies the archive digest before extraction/use plus the
# bundled binary checksum and the expected binary SHA-256 (never
# rebuilding, never substituting), then pushes the verified bytes to the
# worker's POST /v1/exact-artifact transport. The worker materializes
# them at the deterministic absolute path and verifies again; the job
# below carries the machine-readable identity so the worker rejects
# missing/mismatched bytes before starting OpenCode. GitHub credentials
# never leave the controller: the push carries only bytes + checksums,
# and the OpenCode child env is credential-scrubbed.
EXACT_BINARY_FILE=""
if [[ "$EXACT_MODE" == "yes" ]]; then
  EXACT_WORKDIR="$(mktemp -d)"
  EXACT_ZIP="$EXACT_WORKDIR/artifact.zip"
  EXACT_EXTRACT="$EXACT_WORKDIR/extract"
  mkdir -p "$EXACT_EXTRACT"
  if ! EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" EXACT_ZIP="$EXACT_ZIP" python3 - <<'PY'; then
import json, os, sys
sys.path.insert(0, "automation")
from exact_artifact_delivery import (
    controller_token_from_env,
    download_exact_artifact_zip,
    verify_archive_file,
)
identity = json.loads(os.environ.get("EXACT_IDENTITY_JSON", "") or "{}")
token = controller_token_from_env()
if not token:
    print("::error::exact-artifact mode requires a controller GitHub credential.", flush=True)
    raise SystemExit(1)
download_exact_artifact_zip(
    artifact_id=identity.get("artifact_id", "11004835952"),
    dest_path=os.environ["EXACT_ZIP"],
    token=token,
)
verify_archive_file(os.environ["EXACT_ZIP"], identity.get("archive_sha256", ""))
print("controller: exact artifact zip downloaded and archive-verified.")
PY
    echo "::error::Exact-artifact controller fetch/verify failed; refusing to substitute another binary." >&2
    rm -rf "$EXACT_WORKDIR"
    exit 1
  fi
  if ! EXACT_ZIP="$EXACT_ZIP" EXACT_EXTRACT="$EXACT_EXTRACT" EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 - <<'PY'; then
import json, os, sys
sys.path.insert(0, "automation")
from exact_artifact_delivery import extract_and_verify
identity = json.loads(os.environ.get("EXACT_IDENTITY_JSON", "") or "{}")
binary = extract_and_verify(
    os.environ["EXACT_ZIP"], os.environ["EXACT_EXTRACT"],
    identity.get("binary_sha256", ""))
print("controller: exact binary extracted and checksum-verified: %s" % binary)
PY
    echo "::error::Exact-artifact extraction/verification failed; refusing to substitute another binary." >&2
    rm -rf "$EXACT_WORKDIR"
    exit 1
  fi
  EXACT_BINARY_FILE="$EXACT_EXTRACT/opencode-coding-linux-x64"
  if [[ ! -x "$EXACT_BINARY_FILE" ]]; then
    echo "::error::Exact binary missing after verified extraction." >&2
    rm -rf "$EXACT_WORKDIR"
    exit 1
  fi
  # Exact --version gate on the controller (fail closed before transport).
  EXACT_VERSION_OBSERVED="$("$EXACT_BINARY_FILE" --version 2>/dev/null | head -n 1 | awk '{print $1}' || true)"
  EXACT_VERSION_EXPECTED="$(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["version"])')"
  if [[ "$EXACT_VERSION_OBSERVED" != "$EXACT_VERSION_EXPECTED" ]]; then
    echo "::error::Exact binary --version '$EXACT_VERSION_OBSERVED' != expected '$EXACT_VERSION_EXPECTED'; refusing to deliver." >&2
    rm -rf "$EXACT_WORKDIR"
    exit 1
  fi
  echo "controller: exact binary --version $EXACT_VERSION_OBSERVED verified."
  # Push the verified bytes to the worker transport (bytes + checksums
  # only; no GitHub credential crosses this boundary).
  PUSH_CODE="$(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" curl -sS --max-time 300 -o /tmp/exact-push-response.json -w '%{http_code}' -X POST "$SERVICE_URL/v1/exact-artifact" \
    -H "Content-Type: application/octet-stream" \
    -H "X-Exact-Artifact-Id: $(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["artifact_id"])')" \
    -H "X-Exact-Artifact-Name: $(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["artifact_name"])')" \
    -H "X-Exact-Source-Run: $(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["source_run_id"])')" \
    -H "X-Exact-Archive-Sha256: $(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["archive_sha256"])')" \
    -H "X-Exact-Artifact-Sha256: $(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["binary_sha256"])')" \
    -H "X-Exact-Version: $(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 -c 'import json,os; print(json.loads(os.environ["EXACT_IDENTITY_JSON"])["version"])')" \
    --data-binary "@$EXACT_BINARY_FILE" 2>/dev/null || true)"
  PUSH_CODE="$(tr -dc '0-9' <<<"$PUSH_CODE" || true)"
  if [[ "$PUSH_CODE" != "201" ]]; then
    echo "::error::Exact-artifact push to worker failed (HTTP ${PUSH_CODE:-000}); refusing to run without verified bytes." >&2
    cat /tmp/exact-push-response.json 2>/dev/null || true
    rm -rf "$EXACT_WORKDIR"
    exit 1
  fi
  echo "Exact-artifact bytes delivered and worker-verified: $(cat /tmp/exact-push-response.json 2>/dev/null | head -c 300)"
fi

# Build the minimum job payload and submit it to the runner.
JOB_PAYLOAD="$(EXACT_IDENTITY_JSON="$EXACT_IDENTITY_JSON" python3 - "$ISSUE_NUMBER" "$REPO_URL" "$TASK_TEXT" "$BASE_SHA" \
  "$RENDER_REGION" "$OPENCODE_MODEL" "$EXECUTION_MODE" "${GITHUB_RUN_ID:-}" "$TARGET_REPO" <<'PY'
import json, os, sys
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
raw_exact = os.environ.get("EXACT_IDENTITY_JSON", "").strip()
exact = json.loads(raw_exact) if raw_exact else None
req = JobRequest(
    repository_url=sys.argv[2],
    base_ref="main",
    base_sha=sys.argv[4],
    task_text=sys.argv[3],
    issue_number=issue,
    metadata=meta,
    target_repository=sys.argv[9],
    exact_artifact=exact,
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
# still legitimately working. The loop bound, the terminal-iteration
# checks, and the poll sleeps all honor the shell-resolved
# POLL_MAX_ATTEMPTS / POLL_INTERVAL_SECONDS (validated numeric, lifecycle
# defaults on corrupt values) instead of duplicating the lifecycle
# constants as literals: a resolved value and its use must never
# silently diverge (regression for run 36493364897, same class as the
# issue #113 storm-threshold fix).
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
# Transport fail-fast discipline (regression for run 36430429432): that
# run failed on the FIRST failed /health probe after only five
# consecutive HTTP 502 polls, but a Free restart/OOM-replacement produces
# exactly that transient signature (proxy 502s while the old process is
# dead, /health failing while the replacement boots) and usually answers
# again inside the unchanged budget. Transport errors therefore fail fast
# only after JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES consecutive failed
# /health probes; earlier failed probes keep polling within budget, and
# any healthy probe (or any poll that reaches the worker) resets the
# streak.
#
# Job-loss recovery (regression for run 36409152332, extended through
# run 36434278632): that first run polled a submitted job as pending
# for ~2 minutes before it turned into a permanent unknown-job 404 with
# a healthy runner. Render may restart a Free web service at any time
# (see RENDER_DOC_FREE_TIER in automation/render_lifecycle.py) and the
# runner keeps jobs only in worker process memory, so a restart wipes
# the submitted job id while /health answers 200 again on the fresh
# process. Because the submit payload is fully reproducible,
# budget-limited same-worker resubmissions (never a second Render
# service) convert transient restarts into retries: runs 36417263684,
# 36422228148, 36425019190 and 36430429432 each exhausted a fixed
# resubmission bound (1, 2, 3, 4) with a larger consecutive-restart
# cluster, and run 36434278632 then lost the original job plus all five
# resubmissions to six consecutive proven restarts inside the unchanged
# poll budget, so a fixed count is no longer incremented -- the loop
# resubmits while poll iterations remain (should_resubmit_after_job_loss
# in automation/render_lifecycle.py) and only fails fast on an
# exhausted budget, a proven deterministic loss (same healthy worker
# process no longer knows the job it accepted; an unknown-job 404
# proves the poll reached the worker, so resubmission cannot recover
# it), or a proven memory-pressure restart storm (run 36439192645:
# should_abandon_restart_storm in automation/render_lifecycle.py plus
# detect_memory_pressure in automation/render_memory_sampler.py --
# without pressure evidence the streak alone never abandons, so
# transient host restarts keep budget-limited recovery). A submit-time /health snapshot (uptime_seconds) is compared with
# the loss-time reading via detect_worker_restart() so the diagnostic
# states whether a restart was actually observed.
FALLBACK_MODEL="opencode/space-bunny-free"
TRIED_FALLBACK="no"
POLL_UNKNOWN_COUNT=0
POLL_EMPTY_COUNT=0
POLL_UNHEALTHY_COUNT=0
POLL_LAST_CODE="000"
POLL_RESUBMITS=0
# Consecutive proven-restart resubmissions with no intervening model
# fallback (regression for run 36439192645): pending polls between
# losses do not reset this -- they are the doomed attempt still
# running, not forward progress -- while a fallback submit resets it
# (different model, changed premise).
POLL_RESTART_STREAK=0
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
POLL_MAX_UNHEALTHY="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES
print(JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES)
PY
)"
POLL_MAX_ATTEMPTS="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_MAX_ATTEMPTS
print(JOB_POLL_MAX_ATTEMPTS)
PY
)"
POLL_INTERVAL_SECONDS="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_INTERVAL_SECONDS
print(JOB_POLL_INTERVAL_SECONDS)
PY
)"
POLL_STORM_THRESHOLD="$(python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import JOB_POLL_RESTART_STORM_THRESHOLD
print(JOB_POLL_RESTART_STORM_THRESHOLD)
PY
)"
[[ "$POLL_MAX_ATTEMPTS" =~ ^[0-9]+$ ]] || POLL_MAX_ATTEMPTS=140
[[ "$POLL_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || POLL_INTERVAL_SECONDS=20
[[ "$POLL_UNKNOWN_THRESHOLD" =~ ^[0-9]+$ ]] || POLL_UNKNOWN_THRESHOLD=3
[[ "$POLL_MAX_UNHEALTHY" =~ ^[0-9]+$ ]] || POLL_MAX_UNHEALTHY=1
[[ "$POLL_HEALTH_EVERY" =~ ^[0-9]+$ ]] || POLL_HEALTH_EVERY=5
[[ "$POLL_STORM_THRESHOLD" =~ ^[0-9]+$ ]] || POLL_STORM_THRESHOLD=3
# Submit-time runner health snapshot (best-effort restart-evidence
# baseline; never fails the attempt when the body is unavailable).
# Issue #41: capture the unique process instance id plus the wall-clock
# time alongside uptime_seconds. Uptime order alone cannot prove identity
# (run 36410676408 advanced minutes of wall-clock with only ~23s/~11s
# uptime deltas, so a replacement process can report a larger uptime than
# the old snapshot and still be a different process).
SUBMIT_HEALTH_JSON="$(curl -sS --max-time 10 "$SERVICE_URL/health" \
  -H "Accept: application/json" 2>/dev/null || true)"
SUBMIT_UPTIME="$(jq -r '.uptime_seconds // empty' <<<"$SUBMIT_HEALTH_JSON" 2>/dev/null || true)"
SUBMIT_INSTANCE="$(jq -r '.instance_id // .runner_instance_id // empty' <<<"$SUBMIT_HEALTH_JSON" 2>/dev/null || true)"
SUBMIT_WALL="$(date +%s 2>/dev/null || true)"
for ((i = 1; i <= POLL_MAX_ATTEMPTS; i++)); do
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
        memory_sampler_record_event "fallback_submitted" "job=$JOB_ID model=$FALLBACK_MODEL"
        # Later same-worker resubmissions must reuse the payload that is
        # actually in flight (fallback model), not the primary payload.
        JOB_PAYLOAD="$RETRY_PAYLOAD"
        POLL_UNKNOWN_COUNT=0
        POLL_EMPTY_COUNT=0
        POLL_UNHEALTHY_COUNT=0
        # A fallback submit is a changed premise (different model), so
        # the restart-storm streak restarts with it.
        POLL_RESTART_STREAK=0
        continue
      fi
      printf '%s\n' "$RESULT_JSON" > "$RENDER_RESULT_FILE"
      echo "::error::Runner job $JOB_ID ended with status '$POLL_OUTCOME': $ERROR_TEXT" >&2
      exit 1
      ;;
    pending)
      POLL_UNKNOWN_COUNT=0
      POLL_EMPTY_COUNT=0
      POLL_UNHEALTHY_COUNT=0
      if [[ "$i" -eq "$POLL_MAX_ATTEMPTS" ]]; then
        echo "::error::Runner job $JOB_ID did not finish in time (last status '${STATUS:-empty}')." >&2
        exit 1
      fi
      sleep "$POLL_INTERVAL_SECONDS"
      ;;
    unknown_job)
      POLL_UNKNOWN_COUNT=$((POLL_UNKNOWN_COUNT + 1))
      POLL_EMPTY_COUNT=0
      POLL_UNHEALTHY_COUNT=0
      if [[ "$POLL_UNKNOWN_COUNT" -ge "$POLL_UNKNOWN_THRESHOLD" ]]; then
        CURRENT_HEALTH_JSON="$(curl -sS --max-time 10 "$SERVICE_URL/health" \
          -H "Accept: application/json" 2>/dev/null || true)"
        if curl -fsSL --max-time 10 "$SERVICE_URL/health" -o /dev/null 2>/dev/null; then
          RUNNER_HEALTH="healthy"
        else
          RUNNER_HEALTH="unreachable"
        fi
        CURRENT_UPTIME="$(jq -r '.uptime_seconds // empty' <<<"$CURRENT_HEALTH_JSON" 2>/dev/null || true)"
        CURRENT_INSTANCE="$(jq -r '.instance_id // .runner_instance_id // empty' <<<"$CURRENT_HEALTH_JSON" 2>/dev/null || true)"
        CURRENT_WALL="$(date +%s 2>/dev/null || true)"
        RESTART_EVIDENCE="$(python3 - "$SUBMIT_UPTIME" "$CURRENT_UPTIME" "$SUBMIT_INSTANCE" "$CURRENT_INSTANCE" "$SUBMIT_WALL" "$CURRENT_WALL" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import format_restart_evidence
prior, current, prior_inst, current_inst, prior_wall, current_wall = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
)
print(format_restart_evidence(prior, current, prior_inst, current_inst, prior_wall, current_wall))
PY
)"
        # Machine-readable restart verdict for the budget-limited
        # resubmission policy (should_resubmit_after_job_loss): a proven
        # restart ("restarted") or missing evidence ("unknown") keeps
        # retrying while poll budget remains; a proven same-process loss
        # ("same-process") is deterministic and fails fast. An
        # unknown-job 404 proves the poll reached the worker, so a loss
        # on the same process that accepted the job cannot be a restart
        # down-window.
        RESTART_VERDICT="$(python3 - "$SUBMIT_UPTIME" "$CURRENT_UPTIME" "$SUBMIT_INSTANCE" "$CURRENT_INSTANCE" "$SUBMIT_WALL" "$CURRENT_WALL" <<'PY'
import sys
sys.path.insert(0, "automation")
from render_lifecycle import detect_worker_restart
verdict = detect_worker_restart(
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
)
print("restarted" if verdict is True else ("same-process" if verdict is False else "unknown"))
PY
)"
        POLLS_REMAINING=$((POLL_MAX_ATTEMPTS - i))
        # Restart-storm circuit breaker (regression for run
        # 36439192645, which lost seven consecutive jobs to seven
        # proven restarts and then starved the eighth `running` job
        # when the shared budget ran out): when the loss is a proven
        # restart AND the streak of consecutive proven-restart losses
        # reached the lifecycle threshold AND live container
        # telemetry proves memory pressure, resubmitting the
        # identical payload cannot succeed -- the ~600 MB agent does
        # not fit the 512 MB Free worker, so every resubmission only
        # restarts the same oversized workload and burns budget with
        # zero forward progress. Fail fast with a storm diagnostic
        # instead. Without pressure evidence the streak alone never
        # abandons (transient host restarts keep budget-limited
        # recovery), and telemetry gaps fail open toward resubmission.
        # The storm diagnostic carries the auditable decision inputs
        # (pinned ratio, transitions, surge, resolved threshold, and
        # the deciding branch -- run 36493316814 abandoned via the
        # replacements branch with nearly quiet stall counters, which
        # the old bare-counters line made look unsupported). The
        # resolved streak threshold is honored by the decision helper,
        # never ignored; a corrupt threshold fails open.
        STORM_EVIDENCE=""
        if [[ "$RESTART_VERDICT" == "restarted" ]]; then
          STORM_CHECK="$(python3 - "$POLL_RESTART_STREAK" "$POLL_STORM_THRESHOLD" "${RENDER_MEMORY_SAMPLES_FILE:-}" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "automation")
from render_lifecycle import should_abandon_restart_storm
from render_memory_sampler import (
    detect_memory_pressure_file,
    memory_pressure_evidence,
    pressure_decision_branch,
    read_samples,
)
try:
    streak = int(sys.argv[1])
except ValueError:
    streak = -1
# The resolved streak threshold travels as a raw string on purpose:
# should_abandon_restart_storm() parses it, so a corrupt value fails
# open toward resubmission instead of abandoning (or silently
# reverting to the default and diverging from the shell).
threshold_arg = sys.argv[2] if len(sys.argv) > 2 else ""
pressure = detect_memory_pressure_file(sys.argv[3]) if sys.argv[3] else False
if should_abandon_restart_storm(
    consecutive_restart_losses=streak,
    memory_pressure=pressure,
    threshold=threshold_arg,
):
    try:
        evidence = memory_pressure_evidence(read_samples(sys.argv[3]))
        ratio = evidence.get("usage_ratio")
        print("STORM limit=%s current=%s pinned_ratio=%s restarts=%s stall_surge=%s storm_threshold=%s via=%s" % (
            evidence.get("memory_limit_bytes"), evidence.get("max_memory_current_bytes"),
            ("%.4f" % ratio) if isinstance(ratio, float) else ratio,
            evidence.get("restart_transitions"), evidence.get("max_stall_surge_delta"),
            threshold_arg, pressure_decision_branch(evidence)))
    except Exception:
        print("STORM")
else:
    print("OK")
PY
)"
          if [[ "$STORM_CHECK" == STORM* ]]; then
            STORM_EVIDENCE="${STORM_CHECK#STORM}"
            echo "::error::Runner restart storm: job $JOB_ID lost to $((POLL_RESTART_STREAK + 1)) consecutive proven worker restarts with container memory pressure (${STORM_EVIDENCE# }); resubmitting the identical payload cannot succeed on this 512 MB worker (agent peak ~600 MB); failing fast instead of burning the poll budget (resubmissions used: $POLL_RESUBMITS; poll $i/$POLL_MAX_ATTEMPTS; $RESTART_EVIDENCE). Jobs live in worker process memory, so each restart wipes the job; provision a larger worker or shrink the workload instead of retrying here." >&2
            exit 1
          fi
        fi
        # Budget-limited same-worker resubmission (regression for run
        # 36434278632, which lost six consecutive jobs to six proven
        # restarts inside the poll budget and exhausted the old fixed
        # bound of five): the payload is fully reproducible and the
        # worker is healthy again, so resubmit on the SAME worker while
        # poll iterations remain instead of failing the whole attempt
        # on transient restarts. A sixth (or Nth) consecutive restart
        # loss is another retry, not a terminal failure -- unless the
        # storm breaker above proved the worker cannot host this
        # workload. Never creates a second service.
        if [[ "$RUNNER_HEALTH" == "healthy" && "$RESTART_VERDICT" != "same-process" && "$POLLS_REMAINING" -gt 0 ]]; then
          echo "Runner lost job $JOB_ID ($RESTART_EVIDENCE); resubmitting the same payload on the same worker (resubmission $((POLL_RESUBMITS + 1)), $POLLS_REMAINING poll(s) of budget remaining, no new service)."
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
          memory_sampler_record_event "job_resubmitted" "lost=$LOST_JOB_ID resubmitted=$JOB_ID count=$POLL_RESUBMITS"
          POLL_RESUBMITS=$((POLL_RESUBMITS + 1))
          # Only proven restarts extend the storm streak; an
          # inconclusive ("unknown") verdict resubmits without
          # strengthening or clearing the storm evidence.
          if [[ "$RESTART_VERDICT" == "restarted" ]]; then
            POLL_RESTART_STREAK=$((POLL_RESTART_STREAK + 1))
          fi
          POLL_UNKNOWN_COUNT=0
          POLL_EMPTY_COUNT=0
          POLL_UNHEALTHY_COUNT=0
          SUBMIT_UPTIME="$CURRENT_UPTIME"
          SUBMIT_INSTANCE="$CURRENT_INSTANCE"
          SUBMIT_WALL="$CURRENT_WALL"
          continue
        fi
        # Fail fast with a qualified diagnostic. A proven same-process
        # loss is deterministic (the poll reached the worker, so the
        # worker losing its own job cannot be recovered by resubmission);
        # any other terminal loss means the poll budget is exhausted or
        # the worker is unreachable.
        if [[ "$RUNNER_HEALTH" == "healthy" && "$RESTART_VERDICT" == "same-process" ]]; then
          echo "::error::Runner no longer knows job $JOB_ID (HTTP $POLL_LAST_CODE on $POLL_UNKNOWN_COUNT consecutive polls; runner health: $RUNNER_HEALTH; resubmissions used: $POLL_RESUBMITS; $RESTART_EVIDENCE). An unknown-job answer proves the poll reached the worker, so a loss on the same process that accepted the job is deterministic and resubmission cannot recover it; failing fast instead of burning the poll budget." >&2
          exit 1
        fi
        echo "::error::Runner no longer knows job $JOB_ID (HTTP $POLL_LAST_CODE on $POLL_UNKNOWN_COUNT consecutive polls; runner health: $RUNNER_HEALTH; resubmissions used: $POLL_RESUBMITS; poll $i/$POLL_MAX_ATTEMPTS, budget exhausted; $RESTART_EVIDENCE). Jobs live in worker process memory, so a worker restart loses the job permanently; failing fast instead of waiting out the full poll budget." >&2
        exit 1
      fi
      if [[ "$i" -eq "$POLL_MAX_ATTEMPTS" ]]; then
        echo "::error::Runner job $JOB_ID did not finish in time (last status '${STATUS:-empty}', HTTP $POLL_LAST_CODE, $POLL_UNKNOWN_COUNT consecutive unknown-job polls, $POLL_RESUBMITS resubmission(s) used)." >&2
        exit 1
      fi
      sleep "$POLL_INTERVAL_SECONDS"
      ;;
    transport_error)
      POLL_EMPTY_COUNT=$((POLL_EMPTY_COUNT + 1))
      POLL_UNKNOWN_COUNT=0
      if (( POLL_EMPTY_COUNT % POLL_HEALTH_EVERY == 0 )); then
        if curl -fsSL --max-time 10 "$SERVICE_URL/health" -o /dev/null 2>/dev/null; then
          POLL_UNHEALTHY_COUNT=0
          echo "Job $JOB_ID poll hit $POLL_EMPTY_COUNT consecutive transport failures (last HTTP $POLL_LAST_CODE); runner still healthy, continuing within the poll budget." >&2
        else
          # Sustained-unhealthiness gate (regression for run 36430429432):
          # one failed probe is only a transient restart down-window, so
          # keep polling within budget until
          # JOB_POLL_TRANSPORT_MAX_UNHEALTHY_PROBES consecutive probes
          # fail before calling the worker dead.
          POLL_UNHEALTHY_COUNT=$((POLL_UNHEALTHY_COUNT + 1))
          if [[ "$POLL_UNHEALTHY_COUNT" -ge "$POLL_MAX_UNHEALTHY" ]]; then
            echo "::error::Runner job $JOB_ID poll failed $POLL_EMPTY_COUNT consecutive times (last HTTP $POLL_LAST_CODE) and the runner health check failed on $POLL_UNHEALTHY_COUNT consecutive probes; failing fast instead of waiting out the full poll budget." >&2
            exit 1
          fi
          echo "Job $JOB_ID poll hit $POLL_EMPTY_COUNT consecutive transport failures (last HTTP $POLL_LAST_CODE) and the runner health check is failing (failed probe $POLL_UNHEALTHY_COUNT/$POLL_MAX_UNHEALTHY); continuing within the poll budget in case the worker is mid-restart." >&2
        fi
      fi
      if [[ "$i" -eq "$POLL_MAX_ATTEMPTS" ]]; then
        echo "::error::Runner job $JOB_ID did not finish in time (last HTTP $POLL_LAST_CODE, last status '${STATUS:-empty}', $POLL_EMPTY_COUNT consecutive transport failures)." >&2
        exit 1
      fi
      sleep "$POLL_INTERVAL_SECONDS"
      ;;
    *)
      echo "::error::Runner returned unknown job status '$STATUS' (HTTP $POLL_LAST_CODE)." >&2
      exit 1
      ;;
  esac
done
