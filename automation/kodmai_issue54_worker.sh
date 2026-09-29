#!/usr/bin/env bash
set -euo pipefail

TARGET_REPO="kodmial/kodmai"
TARGET_ISSUE="54"
HISTORICAL_ZEN_REF="dbc43c18945b0f37125a57f7cc6c6206808cc412"
WORKDIR="${RUNNER_TEMP}/kodmai"
BRANCH="runtime-lab/issue54-${GITHUB_RUN_ID}"

report_failure() {
  set +e
  gh issue comment "$TARGET_ISSUE" --repo "$TARGET_REPO" \
    --body "Runtime-lab bridge worker failed before producing a usable private PR. Source run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}. The private Kodmai checkout and OpenCode transcript were not uploaded as runtime-lab artifacts."
}
trap report_failure ERR

echo "Checking private Kodmai access..."
gh api "repos/$TARGET_REPO" --jq '.full_name + " private=" + (.private|tostring)'
gh api "repos/$TARGET_REPO/issues/$TARGET_ISSUE" --jq '.title'

gh auth setup-git
rm -rf "$WORKDIR"
gh repo clone "$TARGET_REPO" "$WORKDIR" -- --quiet
cd "$WORKDIR"
git fetch --all --tags --prune --quiet
test "$(git rev-parse "$HISTORICAL_ZEN_REF^{commit}")" = "$HISTORICAL_ZEN_REF"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git switch -c "$BRANCH"

for attempt in 1 2 3; do
  if curl -fsSL https://opencode.ai/install | bash >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    echo "OpenCode install failed."
    exit 1
  fi
  sleep $((attempt * 5))
done
export PATH="$HOME/.opencode/bin:$PATH"
test -x "$HOME/.opencode/bin/opencode"

ISSUE_BODY="$(gh api "repos/$TARGET_REPO/issues/$TARGET_ISSUE" --jq '.body')"
PROMPT_FILE="${RUNNER_TEMP}/kodmai54-prompt.txt"
cat >"$PROMPT_FILE" <<'EOF'
You are executing private repository kodmial/kodmai issue #54 from an external GitHub-hosted runner.

Critical execution rules:
- Work only in the cloned private repository in the current directory.
- The current branch is a temporary implementation branch based on current main.
- Read and satisfy the complete issue body appended below.
- You MUST create a separate read-only historical worktree at exactly dbc43c18945b0f37125a57f7cc6c6206808cc412.
- Run the complete historical Kodmai application from that historical worktree. Do not replace this with reconstructed request-shape probes.
- Historical application/provider/router source is an oracle and must not be modified.
- Use exactly muse-spark-1.3-contributor-free for the Zen live proof.
- Prove plain curl -> historical Kodmai -> real Zen upstream -> real LLM -> Kodmai.
- Investigate setup/runtime/dependency problems until the live proof succeeds, within the available run time.
- Never print or write secrets/tokens.
- Do not modify .github/workflows/**.
- Commit only reproducibility code/docs or current-main test/support changes genuinely required by issue #54.
- Prefer scripts/reproduce_historical_zen.sh and docs/historical-zen-live-smoke.md as durable deliverables.
- Sanitize all evidence written to repository files.
EOF
printf '\n\n--- ISSUE #54 BODY ---\n%s\n' "$ISSUE_BODY" >>"$PROMPT_FILE"

# Never expose the agent transcript in public runtime-lab logs/artifacts.
opencode run --auto --model "$OPENCODE_MODEL" "$(cat "$PROMPT_FILE")" \
  >"${RUNNER_TEMP}/opencode-kodmai54.log" 2>&1

cd "$WORKDIR"
if [[ "$(git branch --show-current)" != "$BRANCH" ]]; then
  echo "OpenCode changed the main working-tree branch unexpectedly."
  exit 1
fi

if [[ -n "$(git status --porcelain .github/workflows)" ]]; then
  echo "Worker modified workflow files; refusing to publish."
  exit 1
fi

if [[ -z "$(git status --porcelain)" ]]; then
  gh issue comment "$TARGET_ISSUE" --repo "$TARGET_REPO" \
    --body "Runtime-lab bridge worker completed without repository changes, so no private PR was published. Source run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}."
  trap - ERR
  exit 1
fi

git add -A
git commit -m "test: reproduce historical Zen live path"
git push -u origin "HEAD:$BRANCH" >/dev/null

PR_URL="$(gh pr create \
  --repo "$TARGET_REPO" \
  --base main \
  --head "$BRANCH" \
  --title "Prove historical Zen live path from clean runner" \
  --body "External runtime-lab worker for #54. The private Kodmai checkout and OpenCode transcript were not uploaded as runtime-lab artifacts. Sanitized reproducibility evidence is contained in this private PR.\n\nCloses #54")"

gh issue comment "$TARGET_ISSUE" --repo "$TARGET_REPO" \
  --body "Runtime-lab bridge worker produced a private PR: $PR_URL"

trap - ERR
echo "Private Kodmai PR published successfully."
