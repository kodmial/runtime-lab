#!/usr/bin/env bash
# Install/provision the OpenCode CLI on a Render worker (issue #3).
#
# Known-good invocation pattern from NanoDictate (see
# .github/workflows/opencode.yml "Install OpenCode CLI" step), with only
# the plain installer invocation (no extra integrations and no
# release-specific behavior): retry the upstream installer
# up to 3 times with backoff, then verify the binary is executable.
# No secrets are required or printed here; provider/model credentials
# stay environment-driven at job runtime.
set -euo pipefail

for attempt in 1 2 3; do
  echo "OpenCode install attempt $attempt/3"
  if curl -fsSL https://opencode.ai/install | bash; then
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
