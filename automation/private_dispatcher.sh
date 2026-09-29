#!/usr/bin/env bash
set -euo pipefail

MARKER_PATH="automation/runtime-target.marker"
MARKER_VALUE="primary-v1"

cleanup_private_runtime_runs() {
  local workflow run_id

  # Public Runtime Lab is only the execution control plane for these external
  # private tasks. Remove completed worker/review logs and prior dispatcher
  # runs so private-project execution metadata is not retained publicly.
  for workflow in private-worker.yml private-review.yml private-dispatcher.yml; do
    while IFS= read -r run_id; do
      [[ -n "$run_id" ]] || continue
      [[ "$run_id" == "$GITHUB_RUN_ID" ]] && continue
      gh api --method DELETE \
        "repos/$GITHUB_REPOSITORY/actions/runs/$run_id" \
        >/dev/null 2>&1 || true
    done < <(
      gh run list --repo "$GITHUB_REPOSITORY" --workflow "$workflow" --limit 100 \
        --json databaseId,status \
        --jq '.[] | select(.status == "completed") | .databaseId' \
        2>/dev/null || true
    )
  done
}

# One-time removal of a now-obsolete public coordination comment whose
# previous revision named external private-project repositories.
gh api --method DELETE \
  "repos/$GITHUB_REPOSITORY/issues/comments/5872183905" \
  >/dev/null 2>&1 || true

# The dispatcher is invoked by workflow_run as soon as a worker/review
# completes, so this removes the finished public run promptly. The same sweep
# also removes any historical completed private-runtime runs.
cleanup_private_runtime_runs

discover_target() {
  local candidate marker
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    marker="$(gh api "repos/$candidate/contents/$MARKER_PATH" --jq '.content' 2>/dev/null | tr -d '\n' | base64 -d 2>/dev/null || true)"
    if [[ "$marker" == "$MARKER_VALUE" ]]; then
      TARGET_REPO="$candidate"
      return 0
    fi
  done < <(gh api --paginate '/user/repos?visibility=private&affiliation=owner&per_page=100' --jq '.[].full_name' 2>/dev/null)
  return 1
}

active_task_numbers() {
  local workflow="$1"
  gh run list --repo "$GITHUB_REPOSITORY" --workflow "$workflow" --limit 100 \
    --json status,displayTitle \
    --jq '.[] | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "pending") | .displayTitle' \
    2>/dev/null |
    sed -nE 's/^Worker (task|review) #([0-9]+)$/\2/p'
}

task_in_set() {
  local task_number="$1"
  local task_set="$2"
  [[ -n "$task_set" ]] && grep -qx "$task_number" <<<"$task_set"
}

discover_target || {
  echo "No private runtime target is available."
  exit 2
}

# No repository-level worker slot limit: independent tasks and reviews may run
# concurrently. The per-task concurrency groups in the worker/review workflows
# remain the duplicate/race guard for the same task.
ACTIVE_WORKER_TASKS="$(active_task_numbers "private-worker.yml" || true)"
ACTIVE_REVIEW_TASKS="$(active_task_numbers "private-review.yml" || true)"
dispatched=0

open_prs="$(gh pr list --repo "$TARGET_REPO" --state open --limit 100 \
  --json number,headRefName \
  --jq '.[] | select(.headRefName | startswith("runtime-worker/task-")) | [.number,.headRefName] | @tsv' 2>/dev/null || true)"

OPEN_PR_TASKS="$(cut -f2 <<<"$open_prs" | sed -nE 's#^runtime-worker/task-([0-9]+)-.*#\1#p')"
declare -A REVIEW_DISPATCHED=()

if [[ -n "$open_prs" ]]; then
  while IFS=$'\t' read -r pr_number head_ref; do
    [[ -n "$pr_number" ]] || continue
    task_number="$(sed -nE 's#^runtime-worker/task-([0-9]+)-.*#\1#p' <<<"$head_ref")"
    [[ -n "$task_number" ]] || continue

    if task_in_set "$task_number" "$ACTIVE_WORKER_TASKS"; then
      echo "Worker task #$task_number is still active; review will wait."
      continue
    fi
    if task_in_set "$task_number" "$ACTIVE_REVIEW_TASKS"; then
      echo "Review for worker task #$task_number is already active."
      continue
    fi
    if [[ -n "${REVIEW_DISPATCHED[$task_number]:-}" ]]; then
      continue
    fi

    gh workflow run private-review.yml --repo "$GITHUB_REPOSITORY" \
      -f task_number="$task_number" -f pr_number="$pr_number" >/dev/null
    REVIEW_DISPATCHED["$task_number"]=1
    dispatched=$((dispatched + 1))
    echo "Dispatched review for worker task #$task_number."
  done <<<"$open_prs"
fi

issues_json="$(gh api --paginate "repos/$TARGET_REPO/issues?state=open&per_page=100" 2>/dev/null)"

choose_issue() {
  local priority="$1"
  jq -r --arg p "$priority" '
    .[]
    | select(.pull_request == null)
    | select(any(.labels[]?.name; . == $p))
    | select(all(.labels[]?.name; . != "automation:paused"))
    | select((.body // "") | contains("<!-- runtime-worker-owned -->"))
    | [.number, (.body // "")]
    | @base64
  ' <<<"$issues_json"
}

is_ready() {
  local issue_number="$1" body="$2" deps dep state

  if task_in_set "$issue_number" "$ACTIVE_WORKER_TASKS" ||
     task_in_set "$issue_number" "$ACTIVE_REVIEW_TASKS" ||
     task_in_set "$issue_number" "$OPEN_PR_TASKS"; then
    return 1
  fi

  deps="$(sed -nE 's/.*<!-- *automation-blocked-by: *([^>]*) *-->.*/\1/p' <<<"$body" | head -1 | tr ',' ' ')"
  for dep in $deps; do
    dep="${dep//#/}"
    dep="${dep//[[:space:]]/}"
    [[ "$dep" =~ ^[0-9]+$ ]] || continue
    state="$(gh api "repos/$TARGET_REPO/issues/$dep" --jq '.state' 2>/dev/null || echo open)"
    [[ "$state" == "closed" ]] || return 1
  done
  return 0
}

for priority in priority:p0 priority:p1 priority:p2; do
  while IFS= read -r encoded; do
    [[ -n "$encoded" ]] || continue
    row="$(base64 -d <<<"$encoded")"
    issue_number="$(jq -r '.[0]' <<<"$row")"
    body="$(jq -r '.[1]' <<<"$row")"

    if is_ready "$issue_number" "$body"; then
      gh workflow run private-worker.yml --repo "$GITHUB_REPOSITORY" \
        -f task_number="$issue_number" >/dev/null
      dispatched=$((dispatched + 1))
      echo "Dispatched worker task #$issue_number."
    fi
  done < <(choose_issue "$priority")
done

if [[ "$dispatched" -eq 0 ]]; then
  echo "No new ready worker or review task found."
else
  echo "Dispatched $dispatched independent worker/review run(s)."
fi
