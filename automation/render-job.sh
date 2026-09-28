#!/usr/bin/env bash
# Ephemeral Render execution step for issue #1.
#
# Lifecycle covered here: create exactly one temporary web service, wait for a
# healthy deploy, submit one runner job, collect the result. Deletion and
# deletion-verification live in automation/render-cleanup.sh, which the
# render-executor.yml workflow always runs afterwards (if: always()) reading
# the same state file. Together they implement the unconditional
# cleanup/finally path: whenever a service id exists, cleanup deletes it.
#
# Guards encoded here (fail closed, no secrets printed):
# - Render auth uses RENDER_API_KEY from the repository secret KEY only.
# - Free-tier plan is requested and the create response is verified to be
#   exactly "free"; any other plan triggers immediate deletion + failure.
# - Muse region is validated before creation (oregon/ohio/virginia/singapore;
#   frankfurt is forbidden, default oregon).
# - At most one service creation per attempt: an existing service id in the
#   state file is reused, never duplicated. Retries never create a service.
# - All polling loops are bounded; cron health checks never create services.
set -euo pipefail

: "${ISSUE_NUMBER:?ISSUE_NUMBER is required}"
: "${EXECUTION_MODE:?EXECUTION_MODE is required}"
: "${RENDER_STATE_FILE:?RENDER_STATE_FILE is required}"
: "${RENDER_RESULT_FILE:?RENDER_RESULT_FILE is required}"
: "${RENDER_API_KEY:?RENDER_API_KEY (repository secret KEY) is required}"

RENDER_REGION="${RENDER_REGION:-oregon}"
OPENCODE_MODEL="${OPENCODE_MODEL:-opencode/muse-spark-1.3-contributor-free}"
RENDER_API_BASE="${RENDER_API_BASE:-https://api.render.com/v1}"
RENDER_OWNER_ID="${RENDER_OWNER_ID:-}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-local}"
MAX_DEPLOY_POLLS="${MAX_DEPLOY_POLLS:-60}"
DEPLOY_POLL_INTERVAL="${DEPLOY_POLL_INTERVAL:-20}"
MAX_JOB_POLLS="${MAX_JOB_POLLS:-90}"
JOB_POLL_INTERVAL="${JOB_POLL_INTERVAL:-20}"

if [[ "$EXECUTION_MODE" != "smoke" && "$EXECUTION_MODE" != "e2e" ]]; then
  echo "::error::Unsupported EXECUTION_MODE: $EXECUTION_MODE" >&2
  exit 2
fi

# Validate region and model before any billable call (fail closed).
REGION="$(PYTHONPATH=automation python3 -c \
  'import os,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; print(r.validate_muse_region(os.environ.get("RENDER_REGION","")))' )"
MODEL="$(PYTHONPATH=automation python3 -c \
  'import os,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; m=os.environ.get("OPENCODE_MODEL",r.PREFERRED_MODEL); print(m if m in r.ALLOWED_MODELS else r.PREFERRED_MODEL)' )"
echo "Render execution issue=$ISSUE_NUMBER mode=$EXECUTION_MODE region=$REGION model=$MODEL run=$GITHUB_RUN_ID"

api() {
  # Generic Render API caller without ever printing the API key.
  local method="$1" path="$2" data_file="${3:-}"
  local args=(-sS --fail-with-body -X "$method"
    -H "Accept: application/json"
    -H "Content-Type: application/json"
    -H "Authorization: Bearer $RENDER_API_KEY"
    --max-time 30)
  if [[ -n "$data_file" ]]; then
    args+=(--data @"$data_file")
  fi
  curl "${args[@]}" "$RENDER_API_BASE$path"
}

existing_service_id() {
  if [[ -s "$RENDER_STATE_FILE" ]]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("serviceId",""))' "$RENDER_STATE_FILE" 2>/dev/null || true
  fi
}

resolve_owner_id() {
  if [[ -n "$RENDER_OWNER_ID" ]]; then
    printf '%s' "$RENDER_OWNER_ID"
    return 0
  fi
  local owners
  owners="$(api GET "/owners?limit=1")"
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); o=d[0] if isinstance(d,list) and d else d.get("owner",d); print(o.get("id",""))' "$owners"
}

SERVICE_ID="$(existing_service_id)"
SERVICE_URL=""
if [[ -n "$SERVICE_ID" ]]; then
  echo "Reusing existing ephemeral service for this attempt (no new creation)."
  SERVICE_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("serviceUrl",""))' "$RENDER_STATE_FILE" 2>/dev/null || true)"
else
  OWNER_ID="$(resolve_owner_id)"
  if [[ -z "$OWNER_ID" ]]; then
    echo "::error::Unable to resolve Render owner id" >&2
    exit 1
  fi
  # Enforce single creation per attempt at the contract level.
  PYTHONPATH=automation python3 -c 'import sys; sys.path.insert(0,"automation"); import render_lifecycle as r; r.enforce_single_create("")'
  PAYLOAD_FILE="$(mktemp)"
  PYTHONPATH=automation OWNER_ID="$OWNER_ID" ISSUE_NUMBER="$ISSUE_NUMBER" \
    GITHUB_RUN_ID="$GITHUB_RUN_ID" REGION="$REGION" python3 -c \
    'import json,os,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; p=r.build_create_payload(issue_number=int(os.environ["ISSUE_NUMBER"]), run_id=os.environ["GITHUB_RUN_ID"], owner_id=os.environ["OWNER_ID"], region=os.environ["REGION"]); r.assert_create_payload_is_free(p); print(json.dumps(p))' \
    > "$PAYLOAD_FILE"
  CREATE_RESP="$(api POST "/services" "$PAYLOAD_FILE")"
  rm -f "$PAYLOAD_FILE"
  SERVICE_ID="$(python3 -c 'import json,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; print(r.parse_service_id_from_create_response(json.loads(sys.argv[1])))' "$CREATE_RESP")"
  # Free-tier guard on the response: abort (after cleanup can delete) if paid.
  if ! python3 -c 'import json,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; r.assert_free_plan(r.extract_plan(json.loads(sys.argv[1])))' "$CREATE_RESP"; then
    echo "::error::Render create response is not the free tier; failing closed (cleanup will delete)." >&2
    python3 -c 'import json; print(json.dumps({"serviceId": sys.argv[1], "issueNumber": int(sys.argv[2]), "region": sys.argv[3], "model": sys.argv[4], "mode": sys.argv[5], "runId": sys.argv[6]}))' \
      "$SERVICE_ID" "$ISSUE_NUMBER" "$REGION" "$MODEL" "$EXECUTION_MODE" "$GITHUB_RUN_ID" > "$RENDER_STATE_FILE"
    exit 1
  fi
  SERVICE_URL="$(python3 -c 'import json,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; print(r.extract_service_url(json.loads(sys.argv[1])) or "")' "$CREATE_RESP")"
  if [[ -z "$SERVICE_URL" ]]; then
    # Fall back to retrieve when the create response omits the URL.
    RETRIEVED="$(api GET "/services/$SERVICE_ID" || true)"
    SERVICE_URL="$(python3 -c 'import json,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; print(r.extract_service_url(json.loads(sys.argv[1])) or "")' "$RETRIEVED" 2>/dev/null || true)"
  fi
  python3 -c 'import json; print(json.dumps({"serviceId": sys.argv[1], "serviceUrl": sys.argv[2], "issueNumber": int(sys.argv[3]), "region": sys.argv[4], "model": sys.argv[5], "mode": sys.argv[6], "runId": sys.argv[7]}))' \
    "$SERVICE_ID" "$SERVICE_URL" "$ISSUE_NUMBER" "$REGION" "$MODEL" "$EXECUTION_MODE" "$GITHUB_RUN_ID" > "$RENDER_STATE_FILE"
  echo "Created ephemeral Render service (single creation for this attempt)."
  echo "Service URL recorded; region=$REGION model=$MODEL"
fi

if [[ -z "$SERVICE_URL" ]]; then
  echo "::error::No externally reachable service URL available" >&2
  exit 1
fi

# Wait for deploy + runner health with bounded polling (reuse, never recreate).
poll=0
while (( poll < MAX_DEPLOY_POLLS )); do
  poll=$((poll + 1))
  if curl -sS --fail --max-time 30 "$SERVICE_URL/health" >/dev/null 2>&1; then
    echo "Runner healthy after $poll poll(s)."
    break
  fi
  if (( poll >= MAX_DEPLOY_POLLS )); then
    echo "::error::Runner did not become healthy within the bounded wait" >&2
    exit 1
  fi
  sleep "$DEPLOY_POLL_INTERVAL"
done

# Build the minimum job payload: repo URL, base ref/SHA, task text, issue
# number, execution metadata. GitHub credentials are never sent to the runner.
BASE_SHA="${GITHUB_SHA:-}"
BASE_REF="${GITHUB_REF_NAME:-main}"
ISSUE_TITLE=""
ISSUE_BODY=""
if command -v gh >/dev/null 2>&1 && [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
  ISSUE_JSON="$(gh issue view "$ISSUE_NUMBER" --repo "$GITHUB_REPOSITORY" --json title,body 2>/dev/null || echo '{}')"
  ISSUE_TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("title",""))' "$ISSUE_JSON" 2>/dev/null || true)"
  ISSUE_BODY="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("body","") or "")' "$ISSUE_JSON" 2>/dev/null || true)"
fi
TASK_TEXT="Implement GitHub issue #$ISSUE_NUMBER (${ISSUE_TITLE}). Mode: $EXECUTION_MODE. ${ISSUE_BODY:0:6000}"

JOB_PAYLOAD_FILE="$(mktemp)"
PYTHONPATH=automation ISSUE_NUMBER="$ISSUE_NUMBER" BASE_REF="$BASE_REF" BASE_SHA="$BASE_SHA" \
  TASK_TEXT="$TASK_TEXT" REGION="$REGION" MODEL="$MODEL" EXECUTION_MODE="$EXECUTION_MODE" \
  GITHUB_RUN_ID="$GITHUB_RUN_ID" python3 -c \
  'import json,os,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; md=r.ExecutionMetadata(issue_number=int(os.environ["ISSUE_NUMBER"]), run_id=os.environ.get("GITHUB_RUN_ID",""), attempt=1, mode=os.environ.get("EXECUTION_MODE","smoke"), region=os.environ.get("REGION",r.DEFAULT_REGION), model=os.environ.get("MODEL",r.PREFERRED_MODEL)); req=r.JobRequest(repository_url=r.PUBLIC_REPO_URL, base_ref=os.environ.get("BASE_REF","main"), task_text=os.environ.get("TASK_TEXT",""), issue_number=int(os.environ["ISSUE_NUMBER"]), metadata=md, base_sha=os.environ.get("BASE_SHA","")); print(json.dumps(req.to_dict()))' \
  > "$JOB_PAYLOAD_FILE"

SUBMIT_RESP="$(curl -sS --fail-with-body --max-time 30 -X POST \
  -H "Content-Type: application/json" --data @"$JOB_PAYLOAD_FILE" "$SERVICE_URL/v1/jobs")"
rm -f "$JOB_PAYLOAD_FILE"
JOB_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("jobId",""))' "$SUBMIT_RESP")"
if [[ -z "$JOB_ID" ]]; then
  echo "::error::Runner did not return a job identifier" >&2
  exit 1
fi
echo "Submitted runner job; region=$REGION model=$MODEL"

# Poll job status/result with bounded retries and timeout semantics.
job_poll=0
while (( job_poll < MAX_JOB_POLLS )); do
  job_poll=$((job_poll + 1))
  STATUS_RESP="$(curl -sS --fail-with-body --max-time 30 "$SERVICE_URL/v1/jobs/$JOB_ID" || true)"
  STATUS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("status",""))' "$STATUS_RESP" 2>/dev/null || true)"
  case "$STATUS" in
    succeeded)
      python3 -c 'import json,sys; json.dump(json.loads(sys.argv[1]), open(sys.argv[2],"w"), indent=2)' "$STATUS_RESP" "$RENDER_RESULT_FILE"
      echo "Job completed successfully (region=$REGION model=$MODEL)."
      exit 0
      ;;
    failed|timeout)
      python3 -c 'import json,sys; json.dump(json.loads(sys.argv[1]), open(sys.argv[2],"w"), indent=2)' "$STATUS_RESP" "$RENDER_RESULT_FILE" 2>/dev/null || printf '%s' "$STATUS_RESP" > "$RENDER_RESULT_FILE"
      echo "::error::Runner job ended with status: $STATUS" >&2
      exit 1
      ;;
    pending|running|"")
      ;;
    *)
      echo "::error::Unknown runner job status: $STATUS" >&2
      exit 1
      ;;
  esac
  if (( job_poll >= MAX_JOB_POLLS )); then
    echo "::error::Runner job did not reach a terminal status within bounds" >&2
    exit 1
  fi
  sleep "$JOB_POLL_INTERVAL"
done
