#!/usr/bin/env bash
# TEMPORARY development harness only (issue #4): GitHub Actions -> Render.
#
# Unconditional cleanup for the ephemeral Render service created by
# automation/render-job.sh. Part of the temporary Actions control plane used
# while the direct GitHub->Render integration does not exist yet; it is NOT
# the final runtime architecture (the final production path must not require
# an OpenCode GitHub Actions job). Deletion is the primary cleanup mechanism;
# suspension is only an emergency fallback if deletion temporarily fails, and
# deletion is still retried within bounded limits. A successful run must
# prove the service no longer exists (GET -> 404/410).
#
# This script runs in an always() workflow step, covering success, runner
# failure, timeout and partial provisioning failure whenever a service id
# exists. Render 429 responses are honored via Retry-After with bounded
# retries (see RENDER_DOC_RATE_LIMITING). Auth uses RENDER_API_KEY (mapped
# from the repository secret KEY by the workflow); never printed.
set -euo pipefail

: "${RENDER_API_KEY:?RENDER_API_KEY must be set by the workflow}"

RENDER_STATE_FILE="${RENDER_STATE_FILE:-/tmp/runtime-lab-render-state.json}"
API_BASE="https://api.render.com/v1"
DELETE_MAX_ATTEMPTS=5
DELETE_INTERVAL=10
SUSPEND_FALLBACK_ATTEMPTS=2
API_RETRY_CAP_SECONDS=120

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

# raw_http <METHOD> <URL> [extra curl args...]: single attempt printing the
# numeric HTTP code. Unlike the job script's api_request, cleanup manages its
# own bounded retry loop below so a suspend fallback can run between two
# bounded deletion windows; 429/Retry-After is honored on every attempt.
raw_http() {
  local method="$1" url="$2"
  shift 2
  local header_file code retry_after
  header_file="$(mktemp)"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -D "$header_file" \
    -X "$method" "$url" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $RENDER_API_KEY" "$@" 2>/dev/null || true)"
  code="$(tr -dc '0-9' <<<"$code" || true)"
  [[ -z "$code" ]] && code="000"
  retry_after="$(grep -i '^retry-after:' "$header_file" 2>/dev/null | tail -n 1 | cut -d: -f2- | tr -d ' \r\n' || true)"
  rm -f "$header_file"
  if [[ "$code" == "429" && "$retry_after" =~ ^[0-9]+$ ]]; then
    if [[ "$retry_after" -gt "$API_RETRY_CAP_SECONDS" ]]; then
      retry_after="$API_RETRY_CAP_SECONDS"
    fi
    echo "Render API returned 429 with Retry-After=${retry_after}s; waiting before retry." >&2
    sleep "$retry_after"
  fi
  echo "$code"
}

delete_once() {
  raw_http DELETE "$API_BASE/services/$SERVICE_ID"
}

verify_gone() {
  raw_http GET "$API_BASE/services/$SERVICE_ID"
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
  SUSPEND_CODE="$(raw_http POST "$API_BASE/services/$SERVICE_ID/suspend")"
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
