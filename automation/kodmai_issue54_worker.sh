#!/usr/bin/env bash
set -euo pipefail

TARGET_REPO="kodmial/kodmai"
TARGET_ISSUE="54"
HISTORICAL_ZEN_REF="dbc43c18945b0f37125a57f7cc6c6206808cc412"
WORKDIR="${RUNNER_TEMP}/private-target"
BRANCH="runtime-lab/issue54-${GITHUB_RUN_ID}"
MAX_AGENT_PASSES="${MAX_AGENT_PASSES:-4}"
ACCEPTANCE_FILE="docs/historical-zen-live-smoke.md"
TRANSCRIPT="${RUNNER_TEMP}/private-agent.log"

private_comment() {
  gh issue comment "$TARGET_ISSUE" --repo "$TARGET_REPO" --body "$1" >/dev/null
}

report_failure() {
  set +e
  private_comment "<!-- bridge-task-attempt-failed:54 --> Runtime worker attempt ${GITHUB_RUN_ID} ended without a publishable accepted result. The private transcript stayed on the ephemeral runner."
}
trap report_failure ERR

echo "Private access preflight..."
gh api "repos/$TARGET_REPO" --jq '.private' | grep -qx true
gh api "repos/$TARGET_REPO/issues/$TARGET_ISSUE" --jq '.number' | grep -qx "$TARGET_ISSUE"
echo "Private access OK."

gh auth setup-git >/dev/null 2>&1
rm -rf "$WORKDIR"
gh repo clone "$TARGET_REPO" "$WORKDIR" -- --quiet >/dev/null 2>&1
cd "$WORKDIR"
git fetch --all --tags --prune --quiet >/dev/null 2>&1
test "$(git rev-parse "$HISTORICAL_ZEN_REF^{commit}")" = "$HISTORICAL_ZEN_REF"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git switch -c "$BRANCH" >/dev/null 2>&1

for attempt in 1 2 3; do
  if curl -fsSL https://opencode.ai/install | bash >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    echo "Agent installation failed."
    exit 1
  fi
  sleep $((attempt * 5))
done
export PATH="$HOME/.opencode/bin:$PATH"
test -x "$HOME/.opencode/bin/opencode"

ISSUE_BODY="$(gh api "repos/$TARGET_REPO/issues/$TARGET_ISSUE" --jq '.body')"
BASE_PROMPT="${RUNNER_TEMP}/private-task.txt"
cat >"$BASE_PROMPT" <<'EOF'
Execute private task #54 to completion.

Rules:
- Work only in the private repository in the current directory.
- Read the complete task body appended below and satisfy it literally.
- Use a separate historical worktree at exactly dbc43c18945b0f37125a57f7cc6c6206808cc412.
- Run the complete historical application; reconstructed request-shape probes are not acceptance.
- Do not modify historical application/provider/router logic.
- Use exactly muse-spark-1.3-contributor-free.
- Prove plain curl -> historical application -> real Zen upstream -> real LLM -> application.
- Continue diagnosing setup/runtime/dependency problems until the live proof succeeds.
- Never print or persist secrets.
- Do not modify .github/workflows/**.
- Durable deliverables belong on the current private branch.
- docs/historical-zen-live-smoke.md is mandatory.
- Only after the real live positive proof AND negative control AND restored clean rerun have succeeded, put a standalone line exactly "Acceptance: PASS" in docs/historical-zen-live-smoke.md.
- If acceptance is not yet achieved, do not stop merely because you found a likely cause. Continue the task.
EOF
printf '\n\n--- PRIVATE TASK BODY ---\n%s\n' "$ISSUE_BODY" >>"$BASE_PROMPT"

accepted=false
for pass in $(seq 1 "$MAX_AGENT_PASSES"); do
  echo "Agent pass $pass/$MAX_AGENT_PASSES..."

  PASS_PROMPT="${RUNNER_TEMP}/private-task-pass-${pass}.txt"
  cp "$BASE_PROMPT" "$PASS_PROMPT"

  if [[ "$pass" -gt 1 ]]; then
    cat >>"$PASS_PROMPT" <<EOF

This is continuation pass $pass. The previous pass did not produce verified acceptance.
Inspect the current working tree and the previous private transcript at:
$TRANSCRIPT
Continue from the existing state. Do not restart analysis from scratch. Resolve the concrete blocker and keep working until the mandatory live proof, negative control, restored rerun, and durable documentation are complete.
EOF
  fi

  set +e
  opencode run --auto --model "$OPENCODE_MODEL" "$(cat "$PASS_PROMPT")" >"$TRANSCRIPT" 2>&1
  agent_rc=$?
  set -e

  if [[ -f "$ACCEPTANCE_FILE" ]] && grep -qx 'Acceptance: PASS' "$ACCEPTANCE_FILE"; then
    accepted=true
    break
  fi

  if [[ "$agent_rc" -ne 0 ]]; then
    echo "Agent pass ended non-zero; continuing with private diagnostics."
  else
    echo "Agent pass ended without verified acceptance; continuing."
  fi
done

if [[ "$accepted" != true ]]; then
  private_comment "<!-- bridge-task-needs-retry:54 --> Runtime worker ${GITHUB_RUN_ID} exhausted ${MAX_AGENT_PASSES} agent passes without verified acceptance. A fresh worker should continue the task."
  exit 30
fi

if [[ -n "$(git status --porcelain .github/workflows)" ]]; then
  echo "Rejected: workflow files changed."
  exit 31
fi

# Basic leak guard for durable files. The agent transcript itself is never published.
if git diff --no-ext-diff -- . ':!.github/workflows' | grep -Eiq '(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|rnd_[A-Za-z0-9]+)'; then
  echo "Rejected: token-like material detected in changes."
  exit 32
fi

if [[ "$(git branch --show-current)" != "$BRANCH" ]]; then
  echo "Rejected: agent changed the primary branch unexpectedly."
  exit 33
fi

# The agent may already have committed its result locally. A clean working tree
# is therefore not evidence that nothing was produced. Accept either uncommitted
# durable changes or commits ahead of origin/main.
dirty=false
if [[ -n "$(git status --porcelain)" ]]; then
  dirty=true
  git add -A
  git commit -m "test: reproduce historical Zen live path" >/dev/null 2>&1
fi

ahead="$(git rev-list --count origin/main..HEAD)"
if [[ "$ahead" -eq 0 ]]; then
  private_comment "<!-- bridge-task-needs-retry:54 --> Acceptance marker existed but the private branch contains no commit beyond current main. A fresh worker should continue."
  exit 34
fi

git push -u origin "HEAD:$BRANCH" >/dev/null 2>&1

PR_URL="$(gh pr create   --repo "$TARGET_REPO"   --base main   --head "$BRANCH"   --title "Prove historical Zen live path from clean runner"   --body "External worker result for #54. Private source and agent transcript were never uploaded to the public control repository. Sanitized acceptance evidence is contained in this private PR.\n\nCloses #54")"

private_comment "<!-- bridge-task-complete:54 --> External worker produced the accepted private result: $PR_URL"

trap - ERR
echo "Accepted private result published."
