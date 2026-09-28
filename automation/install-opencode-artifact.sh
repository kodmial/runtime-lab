#!/usr/bin/env bash
# Provision one OpenCode experiment artifact on a Render worker (issue #86).
#
# Selection is explicit via the environment (never a mutable `latest`):
#   OPENCODE_ARTIFACT_REF      : fork ref that was built (informational pin)
#   OPENCODE_EXPECTED_FORK_SHA : resolved 40-char fork commit SHA (fingerprint)
#   OPENCODE_EXPECTED_SHA256   : expected binary SHA-256 (fingerprint)
#   OPENCODE_ARTIFACT_ID       : content-addressed id (exp-<12>); derived
#       from the fork SHA when absent
#   OPENCODE_ARTIFACT_URL      : immutable GitHub-backed release download URL
#       (releases/download/opencode-exp-<short>/...); used only when the
#       artifact directory is not already present in the deploy.
#
# Behavior:
#   - Baseline mode (none of the above set): do nothing successfully; the
#     pinned upstream binary at .opencode-bin/opencode (installed by
#     automation/install-opencode.sh) remains the only binary.
#   - Experiment mode (any set): resolve the content-addressed directory
#     .opencode-artifacts/<exp-id>/, fetch the immutable asset only when
#     the binary is absent, then verify the SHA-256 fingerprint and
#     executability. Any mismatch or absence fails closed (nonzero exit)
#     and never falls back to the baseline binary.
#   - Concurrent experiments coexist: this script touches only its own
#     <exp-id> directory and never deletes or overwrites sibling ids.
#   - No runtime installer/download fallback: this script never invokes
#     the upstream install pipeline in experiment mode; the runner likewise
#     refuses network installation for qualified experiments.
#
# No secrets are required or printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REF="${OPENCODE_ARTIFACT_REF:-}"
FORK_SHA="${OPENCODE_EXPECTED_FORK_SHA:-}"
DIGEST="${OPENCODE_EXPECTED_SHA256:-}"
ARTIFACT_ID="${OPENCODE_ARTIFACT_ID:-}"
ARTIFACT_URL="${OPENCODE_ARTIFACT_URL:-}"

if [[ -z "$REF" && -z "$FORK_SHA" && -z "$DIGEST" && -z "$ARTIFACT_ID" && -z "$ARTIFACT_URL" ]]; then
  echo "No experiment artifact requested; keeping upstream baseline (.opencode-bin/opencode)."
  exit 0
fi

# Fail closed on mutable pointers even before Python validation.
for value in "$REF" "$ARTIFACT_URL"; do
  lower="$(tr '[:upper:]' '[:lower:]' <<<"$value")"
  if [[ "$lower" == *"latest"* ]]; then
    echo "::error::refusing mutable 'latest' artifact reference." >&2
    exit 1
  fi
done

if [[ -z "$ARTIFACT_ID" && -n "$FORK_SHA" ]]; then
  ARTIFACT_ID="exp-$(cut -c1-12 <<<"$FORK_SHA")"
fi
if [[ -z "$ARTIFACT_ID" && "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  ARTIFACT_ID="exp-$(cut -c1-12 <<<"$REF")"
fi
if [[ -z "$ARTIFACT_ID" ]]; then
  echo "::error::experiment artifact requested but no OPENCODE_ARTIFACT_ID (or fork SHA to derive it) is set." >&2
  exit 1
fi

ARTIFACT_DIR="$REPO_ROOT/.opencode-artifacts/$ARTIFACT_ID"
BIN_PATH="$ARTIFACT_DIR/opencode"
MANIFEST_PATH="$ARTIFACT_DIR/$ARTIFACT_ID.json"
mkdir -p "$ARTIFACT_DIR"

# Fetch the immutable asset only when the binary is absent. The URL is a
# pinned releases/download reference keyed by commit SHA, never `latest`.
if [[ ! -x "$BIN_PATH" && -n "$ARTIFACT_URL" ]]; then
  echo "Fetching experiment artifact $ARTIFACT_ID from immutable reference..."
  TMP_FILE="$(mktemp)"
  if ! curl -fsSL --max-time 300 "$ARTIFACT_URL" -o "$TMP_FILE"; then
    echo "::error::could not download immutable artifact reference." >&2
    rm -f "$TMP_FILE"
    exit 1
  fi
  # The published asset is a tarball holding the standalone binary, or the
  # raw binary itself; accept either without touching sibling artifacts.
  if tar -tzf "$TMP_FILE" >/dev/null 2>&1; then
    tar -xzf "$TMP_FILE" -C "$ARTIFACT_DIR"
    if [[ ! -x "$BIN_PATH" ]]; then
      FOUND="$(find "$ARTIFACT_DIR" -maxdepth 2 -type f -name 'opencode*' | head -n 1 || true)"
      if [[ -n "$FOUND" && "$FOUND" != "$BIN_PATH" ]]; then
        mv -f "$FOUND" "$BIN_PATH"
      fi
    fi
  else
    mv -f "$TMP_FILE" "$BIN_PATH"
  fi
  rm -f "$TMP_FILE"
  chmod 755 "$BIN_PATH" 2>/dev/null || true
fi

# Fingerprint verification (Python, stdlib only): manifest shape, fork SHA,
# binary SHA-256, and executability. Fails closed; never falls back.
OPENCODE_ARTIFACT_REF="$REF" \
OPENCODE_EXPECTED_FORK_SHA="$FORK_SHA" \
OPENCODE_EXPECTED_SHA256="$DIGEST" \
OPENCODE_ARTIFACT_ID="$ARTIFACT_ID" \
OPENCODE_ARTIFACT_URL="$ARTIFACT_URL" \
python3 - <<'PY'
import os
import sys

sys.path.insert(0, "automation")
from opencode_artifact import (
    resolve_artifact_selection,
    verify_experiment_selection,
)

try:
    selection = resolve_artifact_selection(os.environ)
except ValueError as exc:
    print("::error::invalid experiment artifact selection: %s" % exc)
    raise SystemExit(1)
errors = verify_experiment_selection(selection)
if errors:
    for error in errors:
        print("::error::experiment artifact not ready: %s" % error)
    raise SystemExit(1)
print(
    "Experiment artifact %s is ready (fingerprint %s:%s)."
    % (
        selection["artifact_id"],
        (selection["expected_fork_sha"] or "?")[:12],
        (selection["expected_sha256"] or "?")[:16],
    )
)
PY

test -x "$BIN_PATH"
"$BIN_PATH" --version
echo "Experiment artifact is ready at $BIN_PATH (id $ARTIFACT_ID; siblings untouched)."
