#!/usr/bin/env bash
# TEMPORARY development harness only (issue #5): GitHub Actions -> branch/PR.
#
# This script is the result-materialization half of the temporary Actions
# control plane used to develop and test the Render lifecycle before the
# direct GitHub->Render integration exists. It is NOT the final runtime
# architecture: the final production path must not require an OpenCode
# GitHub Actions job, and GitHub write-back will move into the Render
# controller using GitHub App installation authentication (issue #11).
# The reusable core lives in automation/render_materialize.py so the future
# Render controller can call it directly instead of keeping this
# orchestration permanently embedded in Actions.
#
# The script consumes ONLY the already-collected result file
# ($RENDER_RESULT_FILE, written before the ephemeral service is deleted)
# and never contacts the Render worker or the Render API: materialization
# is fully independent of the worker after result collection/deletion.
#
# During this temporary phase, commit/push/PR creation use the workflow
# GITHUB_TOKEN via git + gh. The issue branch always follows the exact
# convention opencode/issue<ISSUE_NUMBER>-<unique-suffix> (enforced by
# automation/render_materialize.py) so the scheduler and auto-merge
# controller can associate the PR with its source issue. After
# creating/updating the PR, ci.yml is
# explicitly dispatched with its pr_number; nothing relies on a
# pull_request event from a push performed with GITHUB_TOKEN.
set -euo pipefail

: "${ISSUE_NUMBER:?ISSUE_NUMBER must be set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set (owner/repo)}"

RENDER_RESULT_FILE="${RENDER_RESULT_FILE:-/tmp/runtime-lab-render-result.json}"
RENDER_STATE_FILE="${RENDER_STATE_FILE:-/tmp/runtime-lab-render-state.json}"
PR_SUFFIX="${PR_SUFFIX:-${GITHUB_RUN_ID:-}}"

if [[ ! -s "$RENDER_RESULT_FILE" ]]; then
  echo "::error::No collected Render result at $RENDER_RESULT_FILE; nothing to materialize." >&2
  exit 1
fi

# Authenticate the local git remote from the workflow token so pushes work
# without embedding credentials in the remote URL. Never prints the token.
if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
  gh auth setup-git >/dev/null 2>&1 || true
fi

# Expected base SHA / job ID recorded when the job was submitted (read-only
# cross-check; the Python core additionally verifies the result against the
# live main SHA and fails safely on any mismatch).
EXPECTED_BASE_SHA=""
EXPECTED_JOB_ID=""
if [[ -s "$RENDER_STATE_FILE" ]]; then
  EXPECTED_BASE_SHA="$(jq -r '.baseSha // empty' "$RENDER_STATE_FILE" 2>/dev/null || true)"
  EXPECTED_JOB_ID="$(jq -r '.jobId // empty' "$RENDER_STATE_FILE" 2>/dev/null || true)"
fi

# Validate the collected result before touching any Git state. Rejects
# failed/timed_out, malformed, out-of-scope and mismatched results.
VALIDATED_FILE="$(mktemp)"
python3 automation/render_materialize.py validate \
  --result "$RENDER_RESULT_FILE" \
  --issue "$ISSUE_NUMBER" \
  ${EXPECTED_BASE_SHA:+--base-sha "$EXPECTED_BASE_SHA"} \
  ${EXPECTED_JOB_ID:+--job-id "$EXPECTED_JOB_ID"} \
  --json > "$VALIDATED_FILE"
JOB_ID="$(jq -r '.job_id // empty' "$VALIDATED_FILE")"
SUMMARY="$(jq -r '.changes | length' "$VALIDATED_FILE")"
echo "Validated Render result for issue #$ISSUE_NUMBER (job $JOB_ID, files=$SUMMARY)."

if [[ -z "$PR_SUFFIX" ]]; then
  # Fall back to a short job-id suffix so the branch stays unique per attempt.
  PR_SUFFIX="${JOB_ID:0:8}"
fi
if [[ -z "$PR_SUFFIX" ]]; then
  echo "::error::Cannot derive a unique branch suffix (no PR_SUFFIX, GITHUB_RUN_ID or job id)." >&2
  exit 1
fi

BRANCH="$(python3 automation/render_materialize.py branch-name \
  --issue "$ISSUE_NUMBER" --suffix "$PR_SUFFIX")"
echo "Materializing into branch $BRANCH."

# Resolve the issue title/body read-only for the PR title/body.
ISSUE_TITLE=""
if command -v gh >/dev/null 2>&1; then
  ISSUE_JSON="$(gh issue view "$ISSUE_NUMBER" --repo "$GITHUB_REPOSITORY" \
    --json title,body 2>/dev/null || true)"
  if [[ -n "$ISSUE_JSON" ]]; then
    ISSUE_TITLE="$(jq -r '.title // empty' <<<"$ISSUE_JSON" 2>/dev/null || true)"
  fi
fi
PR_TITLE="${ISSUE_TITLE:-Automated implementation for #$ISSUE_NUMBER}"
PR_BODY="Automated implementation for #$ISSUE_NUMBER.

Closes #$ISSUE_NUMBER

Materialized from Render job ${JOB_ID:-unknown} (run ${GITHUB_RUN_ID:-local})."

# Refresh the base so staleness is detected against live main; the Python
# core refuses to publish when the worker base no longer matches.
git fetch origin main --quiet
MAIN_SHA="$(git rev-parse --verify origin/main 2>/dev/null || true)"
if [[ -z "$MAIN_SHA" ]]; then
  echo "::error::Could not resolve origin/main for staleness check." >&2
  exit 1
fi
if [[ -z "$EXPECTED_BASE_SHA" ]]; then
  EXPECTED_BASE_SHA="$MAIN_SHA"
fi

OUTCOME_FILE="$(mktemp)"
python3 automation/render_materialize.py materialize \
  --result "$RENDER_RESULT_FILE" \
  --repo-dir "." \
  --issue "$ISSUE_NUMBER" \
  --branch "$BRANCH" \
  --repository "$GITHUB_REPOSITORY" \
  --base-sha "$EXPECTED_BASE_SHA" \
  ${EXPECTED_JOB_ID:+--job-id "$EXPECTED_JOB_ID"} \
  --pr-title "$PR_TITLE" \
  --pr-body "$PR_BODY" > "$OUTCOME_FILE"

if [[ "$(jq -r '.no_change // false' "$OUTCOME_FILE")" == "true" ]]; then
  echo "Worker produced no changes; no branch or PR was created."
  rm -f "$VALIDATED_FILE" "$OUTCOME_FILE"
  exit 0
fi

PR_NUMBER="$(jq -r '.pr_number // empty' "$OUTCOME_FILE")"
CREATED="$(jq -r '.created_pr // false' "$OUTCOME_FILE")"
echo "Branch $BRANCH published (new PR: $CREATED); PR #$PR_NUMBER updated and ci.yml dispatched."
rm -f "$VALIDATED_FILE" "$OUTCOME_FILE"
