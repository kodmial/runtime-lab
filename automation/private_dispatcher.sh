#!/usr/bin/env bash
set -euo pipefail

MARKER_PATH="automation/runtime-target.marker"
MARKER_VALUE="primary-v1"

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

run_active() {
  local workflow="$1"
  gh run list --repo "$GITHUB_REPOSITORY" --workflow "$workflow" --limit 20     --json status --jq 'any(.[]; .status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "pending")'     2>/dev/null | grep -qx true
}

discover_target || {
  echo "No private runtime target is available."
  exit 2
}

if run_active "private-worker.yml" || run_active "private-review.yml"; then
  echo "Worker already active."
  exit 0
fi

open_prs="$(gh pr list --repo "$TARGET_REPO" --state open --limit 100   --json number,headRefName --jq '.[] | select(.headRefName | startswith("runtime-worker/task-")) | [.number,.headRefName] | @tsv' 2>/dev/null || true)"

if [[ -n "$open_prs" ]]; then
  while IFS=$'\t' read -r pr_number head_ref; do
    [[ -n "$pr_number" ]] || continue
    task_number="$(sed -nE 's#^runtime-worker/task-([0-9]+)-.*#\1#p' <<<"$head_ref")"
    [[ -n "$task_number" ]] || continue
    gh workflow run private-review.yml --repo "$GITHUB_REPOSITORY"       -f task_number="$task_number" -f pr_number="$pr_number" >/dev/null
    echo "Dispatched review for worker task #$task_number."
    exit 0
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

  if gh pr list --repo "$TARGET_REPO" --state open --limit 100       --json headRefName --jq "any(.[]; .headRefName | startswith(\"runtime-worker/task-$issue_number-\"))"       2>/dev/null | grep -qx true; then
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
      gh workflow run private-worker.yml --repo "$GITHUB_REPOSITORY"         -f task_number="$issue_number" >/dev/null
      echo "Dispatched worker task #$issue_number."
      exit 0
    fi
  done < <(choose_issue "$priority")
done

echo "No ready worker task found."
