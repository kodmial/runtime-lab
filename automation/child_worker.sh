#!/usr/bin/env bash
set -euo pipefail

TASK_NUMBER="${TASK_NUMBER:?TASK_NUMBER is required}"
MARKER_PATH="automation/runtime-target.marker"
MARKER_VALUE="primary-v1"
WORKDIR="${RUNNER_TEMP}/child-target"
TRANSCRIPT="${RUNNER_TEMP}/agent-child.log"
RESULT_FILE="automation/runtime-results/task-${TASK_NUMBER}.md"
MAX_AGENT_PASSES="${MAX_AGENT_PASSES:-4}"

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

child_comment() {
  gh issue comment "$TASK_NUMBER" --repo "$TARGET_REPO" --body "$1" >/dev/null 2>&1 || true
}

discover_target || {
  echo "Child target discovery failed."
  exit 2
}

issue_state="$(gh api "repos/$TARGET_REPO/issues/$TASK_NUMBER" --jq '.state' 2>/dev/null || true)"
if [[ "$issue_state" != "open" ]]; then
  echo "Worker task #$TASK_NUMBER is no longer open."
  exit 0
fi

echo "Worker task #$TASK_NUMBER started."

gh auth setup-git >/dev/null 2>&1
rm -rf "$WORKDIR"
gh repo clone "$TARGET_REPO" "$WORKDIR" -- --quiet >/dev/null 2>&1
cd "$WORKDIR"
git fetch --all --tags --prune --quiet >/dev/null 2>&1
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

BRANCH="runtime-worker/task-${TASK_NUMBER}-${GITHUB_RUN_ID}"
git switch -c "$BRANCH" origin/main >/dev/null 2>&1
BASE_SHA="$(git rev-parse HEAD)"

for attempt in 1 2 3; do
  if curl -fsSL https://opencode.ai/install | bash >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    child_comment "<!-- runtime-worker-infra --> Worker task #$TASK_NUMBER could not install the agent runtime."
    exit 3
  fi
  sleep $((attempt * 5))
done
export PATH="$HOME/.opencode/bin:$PATH"
test -x "$HOME/.opencode/bin/opencode"

ISSUE_JSON="$(gh issue view "$TASK_NUMBER" --repo "$TARGET_REPO" --json title,body,url 2>/dev/null)"
ISSUE_TITLE="$(jq -r '.title' <<<"$ISSUE_JSON")"
ISSUE_BODY="$(jq -r '.body // ""' <<<"$ISSUE_JSON")"

mkdir -p "$(dirname "$RESULT_FILE")"
BASE_PROMPT="${RUNNER_TEMP}/task-prompt.txt"
cat >"$BASE_PROMPT" <<EOF
Execute child repository task #$TASK_NUMBER to completion.

Rules:
- Work only in the private repository in the current directory.
- Treat the full private issue body appended below as the authoritative specification.
- Inspect current main before changing anything.
- Continue through diagnose -> implement -> test -> repair until the issue acceptance criteria are genuinely satisfied.
- Do not stop at a likely cause, partial implementation, or report-only answer when the issue requires code or repair.
- Run the relevant tests and any live/integration verification explicitly required by the issue.
- Never print, persist, or expose credentials or secret values.
- Do not upload private source, diffs, issue text, test logs, or agent transcript to the public control repository.
- You may use normal local git operations, including commits. Do not push; the outer worker publishes the branch.
- Before you finish, create or update exactly this private result record:
  $RESULT_FILE
- The result record must contain: scope completed, files/areas changed, verification actually executed, observed results, remaining risks, and a final standalone line:
  Acceptance: PASS
- Write Acceptance: PASS only if every mandatory issue acceptance condition you can execute in this environment is satisfied. If something fails, keep working rather than marking PASS.
- Do not modify the target marker file $MARKER_PATH.

--- CHILD ISSUE TITLE ---
$ISSUE_TITLE

--- CHILD ISSUE BODY ---
$ISSUE_BODY
EOF

accepted=false
for pass in $(seq 1 "$MAX_AGENT_PASSES"); do
  echo "Worker task #$TASK_NUMBER agent pass $pass/$MAX_AGENT_PASSES."

  PASS_PROMPT="${RUNNER_TEMP}/task-pass-${pass}.txt"
  cp "$BASE_PROMPT" "$PASS_PROMPT"

  if [[ "$pass" -gt 1 ]]; then
    cat >>"$PASS_PROMPT" <<EOF

Continuation pass $pass:
- The prior pass did not reach durable accepted completion.
- Inspect the current working tree, local commits, and the prior child transcript at $TRANSCRIPT.
- Continue from the existing state; do not restart from scratch.
- Resolve the concrete remaining blocker, rerun verification, and update $RESULT_FILE.
EOF
  fi

  set +e
  opencode run --auto --model "$OPENCODE_MODEL" "$(cat "$PASS_PROMPT")" >"$TRANSCRIPT" 2>&1
  agent_rc=$?
  set -e

  if [[ -f "$RESULT_FILE" ]] && grep -qx 'Acceptance: PASS' "$RESULT_FILE"; then
    accepted=true
    break
  fi

  if [[ "$agent_rc" -ne 0 ]]; then
    echo "Worker task #$TASK_NUMBER pass $pass ended non-zero; continuing."
  else
    echo "Worker task #$TASK_NUMBER pass $pass incomplete; continuing."
  fi
done

if [[ "$accepted" != true ]]; then
  child_comment "<!-- runtime-worker-needs-retry --> Worker task #$TASK_NUMBER exhausted this runner cycle without verified acceptance. A fresh cycle may continue."
  exit 30
fi

if git diff --no-ext-diff -- . | grep -Eiq '(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|rnd_[A-Za-z0-9]+)'; then
  child_comment "<!-- runtime-worker-security --> Worker task #$TASK_NUMBER produced token-like material in the diff; publication was blocked."
  exit 31
fi

if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git commit -m "task #$TASK_NUMBER: automated implementation" >/dev/null 2>&1
fi

ahead="$(git rev-list --count "$BASE_SHA"..HEAD)"
if [[ "$ahead" -eq 0 ]]; then
  child_comment "<!-- runtime-worker-needs-retry --> Worker task #$TASK_NUMBER marked acceptance but produced no durable commit."
  exit 32
fi

git push -u origin "HEAD:$BRANCH" >/dev/null 2>&1

PR_BODY="$(cat <<EOF
Automated implementation for #$TASK_NUMBER executed on the external runtime worker.

Private source, issue text, test logs, and agent transcript were not uploaded to the public control repository.

<!-- runtime-worker-accepted -->
Closes #$TASK_NUMBER
EOF
)"

PR_URL="$(gh pr create --repo "$TARGET_REPO" --base main --head "$BRANCH"   --title "$ISSUE_TITLE" --body "$PR_BODY" 2>/dev/null)"

child_comment "<!-- runtime-worker-pr --> Worker task #$TASK_NUMBER produced an accepted implementation PR: $PR_URL"
echo "Worker task #$TASK_NUMBER produced a child-repository PR."
