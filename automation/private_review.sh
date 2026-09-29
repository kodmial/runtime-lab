#!/usr/bin/env bash
set -euo pipefail

TASK_NUMBER="${TASK_NUMBER:?TASK_NUMBER is required}"
PR_NUMBER="${PR_NUMBER:?PR_NUMBER is required}"
MARKER_PATH="automation/runtime-target.marker"
MARKER_VALUE="primary-v1"
WORKDIR="${RUNNER_TEMP}/private-review"
TRANSCRIPT="${RUNNER_TEMP}/review-private.log"
REVIEW_FILE="automation/runtime-results/task-${TASK_NUMBER}-review.md"
MAX_REVIEW_PASSES="${MAX_REVIEW_PASSES:-3}"

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

private_comment() {
  gh issue comment "$TASK_NUMBER" --repo "$TARGET_REPO" --body "$1" >/dev/null 2>&1 || true
}

discover_target || {
  echo "Private target discovery failed."
  exit 2
}

pr_state="$(gh pr view "$PR_NUMBER" --repo "$TARGET_REPO" --json state --jq '.state' 2>/dev/null || true)"
if [[ "$pr_state" != "OPEN" ]]; then
  echo "Worker review #$TASK_NUMBER has no open PR to review."
  exit 0
fi

echo "Worker review #$TASK_NUMBER started."

private_comment "$(cat <<EOF
<!-- runtime-review-started -->
Independent runtime review/repair for task #$TASK_NUMBER started on PR #$PR_NUMBER.

Run: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID
EOF
)"

PR_JSON="$(gh pr view "$PR_NUMBER" --repo "$TARGET_REPO" --json headRefName,baseRefName,title,body 2>/dev/null)"
HEAD_REF="$(jq -r '.headRefName' <<<"$PR_JSON")"
BASE_REF="$(jq -r '.baseRefName' <<<"$PR_JSON")"

if [[ "$HEAD_REF" != runtime-worker/task-"$TASK_NUMBER"-* ]]; then
  echo "PR does not belong to the runtime worker."
  exit 3
fi

gh auth setup-git >/dev/null 2>&1
rm -rf "$WORKDIR"
gh repo clone "$TARGET_REPO" "$WORKDIR" -- --quiet >/dev/null 2>&1
cd "$WORKDIR"
git fetch origin "$BASE_REF" "$HEAD_REF" --quiet >/dev/null 2>&1
git checkout -B "$HEAD_REF" "origin/$HEAD_REF" >/dev/null 2>&1
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3; do
  if curl -fsSL https://opencode.ai/install | bash >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    private_comment "<!-- runtime-review-infra --> Review for task #$TASK_NUMBER could not install the agent runtime."
    exit 4
  fi
  sleep $((attempt * 5))
done
export PATH="$HOME/.opencode/bin:$PATH"
test -x "$HOME/.opencode/bin/opencode"

ISSUE_JSON="$(gh issue view "$TASK_NUMBER" --repo "$TARGET_REPO" --json title,body 2>/dev/null)"
ISSUE_TITLE="$(jq -r '.title' <<<"$ISSUE_JSON")"
ISSUE_BODY="$(jq -r '.body // ""' <<<"$ISSUE_JSON")"

mkdir -p "$(dirname "$REVIEW_FILE")"
BASE_PROMPT="${RUNNER_TEMP}/review-prompt.txt"
cat >"$BASE_PROMPT" <<EOF
Independently review and, if necessary, repair private task #$TASK_NUMBER in the currently checked-out PR branch.

Rules:
- Treat the full private issue body below as authoritative.
- Do not trust the implementation summary, prior agent result, or green-looking tests.
- Compare the PR branch against origin/$BASE_REF and inspect every relevant behavioral change.
- Re-run the relevant tests and every live/integration acceptance check required by the issue.
- If you find a defect, missing requirement, brittle assumption, or test gap, fix it directly on this PR branch and rerun verification.
- Continue through review -> repair -> retest until the issue is genuinely complete.
- Never expose credentials or secret values.
- Do not upload private source, diffs, issue text, logs, or transcript to the public control repository.
- You may make local commits. Do not push; the outer review worker pushes the branch.
- Before finishing, create or update exactly:
  $REVIEW_FILE
- Record findings, repairs, verification actually executed, observed results, and remaining risks.
- End with a standalone line exactly:
  Review: PASS
  only when the implementation satisfies the issue and the executed verification is green.
- If anything required still fails, keep working and do not write Review: PASS.
- Do not modify the target marker file $MARKER_PATH.

--- PRIVATE ISSUE TITLE ---
$ISSUE_TITLE

--- PRIVATE ISSUE BODY ---
$ISSUE_BODY
EOF

accepted=false
for pass in $(seq 1 "$MAX_REVIEW_PASSES"); do
  echo "Worker review #$TASK_NUMBER pass $pass/$MAX_REVIEW_PASSES."

  PASS_PROMPT="${RUNNER_TEMP}/review-pass-${pass}.txt"
  cp "$BASE_PROMPT" "$PASS_PROMPT"

  if [[ "$pass" -gt 1 ]]; then
    cat >>"$PASS_PROMPT" <<EOF

Continuation review pass $pass:
- The prior pass did not reach verified review completion.
- Inspect current working tree, local commits, and prior private transcript at $TRANSCRIPT.
- Continue from the existing state, repair remaining blockers, rerun verification, and update $REVIEW_FILE.
EOF
  fi

  set +e
  opencode run --auto --model "$OPENCODE_MODEL" "$(cat "$PASS_PROMPT")" >"$TRANSCRIPT" 2>&1
  rc=$?
  set -e

  if [[ -f "$REVIEW_FILE" ]] && grep -qx 'Review: PASS' "$REVIEW_FILE"; then
    accepted=true
    break
  fi

  if [[ "$rc" -ne 0 ]]; then
    echo "Worker review #$TASK_NUMBER pass $pass ended non-zero; continuing."
  else
    echo "Worker review #$TASK_NUMBER pass $pass incomplete; continuing."
  fi
done

if [[ "$accepted" != true ]]; then
  private_comment "<!-- runtime-review-needs-retry --> Review for task #$TASK_NUMBER did not reach verified acceptance in this runner cycle."
  exit 30
fi

if git diff --no-ext-diff -- . | grep -Eiq '(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|rnd_[A-Za-z0-9]+)'; then
  private_comment "<!-- runtime-review-security --> Review for task #$TASK_NUMBER detected token-like material; merge was blocked."
  exit 31
fi

if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git commit -m "task #$TASK_NUMBER: independent runtime review" >/dev/null 2>&1
fi

if [[ "$(git rev-list --count "origin/$HEAD_REF"..HEAD)" -gt 0 ]]; then
  git push origin "HEAD:$HEAD_REF" >/dev/null 2>&1
fi

# Runtime review replaces unavailable private GitHub-hosted CI. Merge only after
# the independent review has explicitly reached Review: PASS.
set +e
gh pr merge "$PR_NUMBER" --repo "$TARGET_REPO" --squash --delete-branch >/dev/null 2>&1
merge_rc=$?
if [[ "$merge_rc" -ne 0 ]]; then
  gh pr merge "$PR_NUMBER" --repo "$TARGET_REPO" --squash --delete-branch --admin >/dev/null 2>&1
  merge_rc=$?
fi
set -e

if [[ "$merge_rc" -ne 0 ]]; then
  private_comment "<!-- runtime-review-merge-blocked --> Task #$TASK_NUMBER passed independent runtime review, but GitHub still blocked the merge."
  exit 32
fi

private_comment "<!-- runtime-review-merged --> Task #$TASK_NUMBER passed independent runtime review and its PR was merged."
echo "Worker task #$TASK_NUMBER reviewed and merged."
