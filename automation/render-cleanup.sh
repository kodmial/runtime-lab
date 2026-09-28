#!/usr/bin/env bash
# Unconditional cleanup for the ephemeral Render service.
# Deletion is the primary cleanup mechanism; suspension is only an emergency
# fallback if deletion temporarily fails, and deletion is still retried within
# bounded limits. A successful run must prove the service no longer exists.
#
# This script runs in an always() workflow step, covering success, runner
# failure, timeout and partial provisioning failure whenever a service id
# exists. Auth uses RENDER_API_KEY (mapped from the repository secret KEY
# by the workflow); never printed.
set -euo pipefail

: "${RENDER_API_KEY:?RENDER_API_KEY must be set by the workflow}"

RENDER_STATE_FILE="${RENDER_STATE_FILE:-/tmp/runtime-lab-render-state.json}"
API_BASE="https://api.render.com/v1"
DELETE_MAX_ATTEMPTS=5
DELETE_INTERVAL=10
SUSPEND_FALLBACK_ATTEMPTS=2

if [[ ! -s "$RENDER_STATE_FILE" ]]; then
  echo "No Render state was created; cleanup has nothing to delete."
  exit 0
fi

SERVICE_ID="$(jq -r '.serviceId // empty' "$RENDER_STATE_FILE" 2>/dev/null || true)"
if [[ -z "$SERVICE_ID" ]]; then
  echo "Render state has no service id; nothing to delete."
  exit 0
fi

echo "Deleting ephemeral Render service $SERVICE_ID."

delete_once() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X DELETE \
    "$API_BASE/services/$SERVICE_ID" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" 2>/dev/null || true)"
  echo "$code"
}

verify_gone() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
    "$API_BASE/services/$SERVICE_ID" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" 2>/dev/null || true)"
  echo "$code"
}

DELETE_CODE=""
for ((i = 1; i <= DELETE_MAX_ATTEMPTS; i++)); do
  DELETE_CODE="$(delete_once)"
  # 204 = deleted; 404/410 on DELETE already means the service is gone.
  if [[ "$DELETE_CODE" == "204" || "$DELETE_CODE" == "404" || "$DELETE_CODE" == "410" ]]; then
    break
  fi
  echo "Delete attempt $i/$DELETE_MAX_ATTEMPTS returned HTTP $DELETE_CODE." >&2
  if [[ "$i" -lt "$DELETE_MAX_ATTEMPTS" ]]; then
    sleep "$DELETE_INTERVAL"
  fi
done

VERIFY_CODE="$(verify_gone)"
if [[ "$VERIFY_CODE" == "404" || "$VERIFY_CODE" == "410" ]]; then
  echo "Verified Render service $SERVICE_ID no longer exists (HTTP $VERIFY_CODE)."
  exit 0
fi

# Emergency fallback only: suspend to stop burn, then keep retrying deletion.
echo "Deletion not yet verified (HTTP $VERIFY_CODE); attempting suspend fallback." >&2
for ((i = 1; i <= SUSPEND_FALLBACK_ATTEMPTS; i++)); do
  SUSPEND_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
    "$API_BASE/services/$SERVICE_ID/suspend" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" 2>/dev/null || true)"
  echo "Suspend fallback attempt $i/$SUSPEND_FALLBACK_ATTEMPTS returned HTTP $SUSPEND_CODE." >&2
  if [[ "$SUSPEND_CODE" == "202" || "$SUSPEND_CODE" == "404" || "$SUSPEND_CODE" == "410" ]]; then
    break
  fi
done

for ((i = 1; i <= DELETE_MAX_ATTEMPTS; i++)); do
  DELETE_CODE="$(delete_once)"
  if [[ "$DELETE_CODE" == "204" || "$DELETE_CODE" == "404" || "$DELETE_CODE" == "410" ]]; then
    break
  fi
  sleep "$DELETE_INTERVAL"
done

VERIFY_CODE="$(verify_gone)"
if [[ "$VERIFY_CODE" == "404" || "$VERIFY_CODE" == "410" ]]; then
  echo "Verified Render service $SERVICE_ID no longer exists after fallback (HTTP $VERIFY_CODE)."
  exit 0
fi

echo "::error::Failed to delete ephemeral Render service $SERVICE_ID (verify HTTP $VERIFY_CODE)." >&2
exit 1
