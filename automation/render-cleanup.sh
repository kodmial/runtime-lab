#!/usr/bin/env bash
# Unconditional cleanup for the ephemeral Render service (issue #1).
#
# The render-executor.yml workflow runs this step with `if: always()` after
# the execution step, so deletion is attempted after success, runner failure,
# timeout, and partial provisioning failure whenever a service id exists.
# Deletion is the primary mechanism; suspension (POST
# /v1/services/{serviceId}/suspend) is only an emergency fallback when
# deletion temporarily fails, and deletion is still retried within bounded
# limits. This script exits nonzero unless deletion is verified, so a
# successful workflow can never silently leave the temporary service behind.
#
# The Render API key comes from the repository secret KEY via RENDER_API_KEY
# and is never printed.
set -euo pipefail

: "${RENDER_STATE_FILE:?RENDER_STATE_FILE is required}"
: "${RENDER_API_KEY:?RENDER_API_KEY (repository secret KEY) is required}"

RENDER_API_BASE="${RENDER_API_BASE:-https://api.render.com/v1}"
MAX_DELETE_ATTEMPTS="${MAX_DELETE_ATTEMPTS:-5}"
DELETE_RETRY_DELAY="${DELETE_RETRY_DELAY:-10}"

if [[ ! -s "$RENDER_STATE_FILE" ]]; then
  echo "No Render state was created; cleanup has nothing to delete."
  exit 0
fi

SERVICE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("serviceId",""))' "$RENDER_STATE_FILE" 2>/dev/null || true)"
if [[ -z "$SERVICE_ID" ]]; then
  echo "State file contains no service id; cleanup has nothing to delete."
  exit 0
fi

# Validate the bounded cleanup plan at the contract level.
PYTHONPATH=automation SERVICE_ID="$SERVICE_ID" python3 -c \
  'import os,sys; sys.path.insert(0,"automation"); import render_lifecycle as r; r.CleanupPlan(service_id=os.environ["SERVICE_ID"]).validate()'

render_call() {
  local method="$1" path="$2"
  curl -sS -o /dev/null -w "%{http_code}" -X "$method" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" \
    --max-time 30 "$RENDER_API_BASE$path" || printf '000'
}

service_exists() {
  local code list_code list_body
  code="$(render_call GET "/services/$SERVICE_ID")"
  if [[ "$code" == "404" || "$code" == "410" ]]; then
    return 1
  fi
  if [[ "$code" == "200" ]]; then
    return 0
  fi
  # On ambiguous retrieve outcomes, cross-check the list operation before
  # declaring verification; fail closed (assume it exists).
  list_body="$(curl -sS -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" --max-time 30 \
    "$RENDER_API_BASE/services?limit=100" || echo '')"
  if [[ -n "$list_body" ]] && python3 -c 'import json,sys; ids=[s.get("id","") for s in (json.loads(sys.argv[1]) if isinstance(json.loads(sys.argv[1]),list) else json.loads(sys.argv[1]).get("services",[]))]; sys.exit(0 if sys.argv[2] not in ids else 1)' \
    "$list_body" "$SERVICE_ID" 2>/dev/null; then
    return 1
  fi
  return 0
}

attempt=0
suspend_tried=0
while (( attempt < MAX_DELETE_ATTEMPTS )); do
  attempt=$((attempt + 1))
  code="$(render_call DELETE "/services/$SERVICE_ID")"
  if [[ "$code" == "204" || "$code" == "404" || "$code" == "410" ]]; then
    if ! service_exists; then
      echo "Verified deletion of ephemeral service (attempt $attempt/$MAX_DELETE_ATTEMPTS)."
      exit 0
    fi
  else
    echo "Delete attempt $attempt/$MAX_DELETE_ATTEMPTS returned HTTP $code." >&2
  fi

  if service_exists; then
    # Emergency fallback only: suspend once to stop compute while deletion is
    # still retried within bounded limits.
    if (( suspend_tried < 1 )); then
      suspend_code="$(render_call POST "/services/$SERVICE_ID/suspend")"
      echo "Suspend fallback attempt returned HTTP $suspend_code." >&2
      suspend_tried=1
    fi
  else
    echo "Verified deletion of ephemeral service (attempt $attempt/$MAX_DELETE_ATTEMPTS)."
    exit 0
  fi

  if (( attempt >= MAX_DELETE_ATTEMPTS )); then
    break
  fi
  sleep "$DELETE_RETRY_DELAY"
done

if service_exists; then
  echo "::error::Ephemeral Render service still exists after $MAX_DELETE_ATTEMPTS bounded delete attempts." >&2
  exit 1
fi

echo "Verified deletion of ephemeral service."
exit 0
