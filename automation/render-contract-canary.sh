#!/usr/bin/env bash
set -euo pipefail

: "${ISSUE_NUMBER:?ISSUE_NUMBER must be set}"
: "${RENDER_API_KEY:?RENDER_API_KEY must be set}"

RENDER_STATE_FILE="${RENDER_STATE_FILE:-/tmp/runtime-lab-render-state.json}"
RENDER_REGION="${RENDER_REGION:-oregon}"
API_BASE="https://api.render.com/v1"

api() {
  local method="$1" url="$2" data="${3:-}" out="$4"
  local code
  if [[ -n "$data" ]]; then
    code="$(curl -sS --max-time 30 -o "$out" -w '%{http_code}' -X "$method" \
      -H 'Accept: application/json' -H 'Content-Type: application/json' \
      -H "Authorization: Bearer $RENDER_API_KEY" -d "$data" "$url")"
  else
    code="$(curl -sS --max-time 30 -o "$out" -w '%{http_code}' -X "$method" \
      -H 'Accept: application/json' -H "Authorization: Bearer $RENDER_API_KEY" "$url")"
  fi
  [[ "$code" =~ ^2 ]] || {
    echo "::error::Render API $method failed with HTTP $code" >&2
    return 1
  }
}

owners="$(mktemp)"
api GET "$API_BASE/owners?limit=20" "" "$owners"
owner_id="$(python3 - "$owners" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import extract_owner_id
with open(sys.argv[1], encoding="utf-8") as f:
    print(extract_owner_id(json.load(f)))
PY
)"
rm -f "$owners"

name="runtime-lab-contract-${ISSUE_NUMBER}-${GITHUB_RUN_ID:-local}"
payload="$(python3 - "$name" "$owner_id" "$RENDER_REGION" <<'PY'
import json, sys
sys.path.insert(0, "automation")
from render_lifecycle import build_create_service_payload
print(json.dumps(build_create_service_payload(
    name=sys.argv[1],
    owner_id=sys.argv[2],
    region=sys.argv[3],
    build_command="true",
    start_command='python -m http.server "$PORT"',
    health_check_path="/",
)))
PY
)"

created="$(mktemp)"
api POST "$API_BASE/services" "$payload" "$created"
service_id="$(jq -r '.service.id // empty' "$created")"
deploy_id="$(jq -r '.deployId // empty' "$created")"
plan="$(jq -r '.service.serviceDetails.plan // empty' "$created")"
rm -f "$created"
[[ -n "$service_id" && "$plan" == "free" ]] || {
  echo "::error::Render did not create the expected free service." >&2
  exit 1
}

jq -n --arg sid "$service_id" --arg dep "$deploy_id" --arg plan "$plan" \
  '{serviceId:$sid,deployId:$dep,plan:$plan}' > "$RENDER_STATE_FILE"
echo "Created real ephemeral Render contract-canary service on the free plan."

for attempt in {1..36}; do
  deploy="$(mktemp)"
  if api GET "$API_BASE/services/$service_id/deploys/$deploy_id" "" "$deploy"; then
    status="$(jq -r '.status // empty' "$deploy")"
    rm -f "$deploy"
    if [[ "$status" == "live" ]]; then
      echo "Render contract-canary deploy is live."
      exit 0
    fi
    if [[ "$status" == "build_failed" || "$status" == "update_failed" || "$status" == "canceled" ]]; then
      echo "::error::Render contract-canary deploy failed with status $status." >&2
      exit 1
    fi
  else
    rm -f "$deploy"
  fi
  sleep 10
done

echo "::error::Render contract-canary deploy did not become live in time." >&2
exit 1
