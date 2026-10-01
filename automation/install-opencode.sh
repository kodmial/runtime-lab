#!/usr/bin/env bash
# Install/provision the OpenCode CLI on a Render worker (issue #3).
#
# Known-good invocation pattern from NanoDictate (see
# .github/workflows/continuum-opencode.yml "Install OpenCode CLI" step),
# with only
# the plain installer invocation (no extra integrations and no
# release-specific behavior): retry the upstream installer
# up to 3 times with backoff, then verify the binary is executable.
# The installer is pinned to an explicit --version so its unauthenticated
# api.github.com latest-version lookup ("Failed to fetch version
# information" under Render shared-egress rate limiting, run 36421205678)
# is skipped entirely. Override with OPENCODE_VERSION in the environment.
# No secrets are required or printed here; provider/model credentials
# stay environment-driven at job runtime.
set -euo pipefail

OPENCODE_VERSION="${OPENCODE_VERSION:-1.18.33}"

for attempt in 1 2 3; do
  echo "OpenCode install attempt $attempt/3 (version $OPENCODE_VERSION)"
  if curl -fsSL https://opencode.ai/install | bash -s -- --version "$OPENCODE_VERSION"; then
    break
  fi

  if [[ "$attempt" == "3" ]]; then
    echo "::error::OpenCode installer failed after 3 attempts" >&2
    exit 1
  fi
  sleep $((attempt * 5))
done

test -x "$HOME/.opencode/bin/opencode"
echo "OpenCode CLI is ready."
# Deterministic deploy artifact (issue #52): Render build-time $HOME is not
# guaranteed to equal runtime $HOME, so copy the pinned binary into the
# repo-relative deploy artifact that the runner resolves without HOME.
# The runner checks .opencode-bin/opencode next to the source tree before
# PATH/HOME, and production workers disable runtime installation entirely.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/.opencode-bin"
mkdir -p "$DEPLOY_DIR"
cp -f "$HOME/.opencode/bin/opencode" "$DEPLOY_DIR/opencode"
chmod 755 "$DEPLOY_DIR/opencode"
test -x "$DEPLOY_DIR/opencode"
"$DEPLOY_DIR/opencode" --version
echo "OpenCode deploy artifact is ready at $DEPLOY_DIR/opencode."
