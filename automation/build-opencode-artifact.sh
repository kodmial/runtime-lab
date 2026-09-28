#!/usr/bin/env bash
# Build a deterministic standalone Linux OpenCode experiment artifact (issue #86).
#
# Usage:
#   bash automation/build-opencode-artifact.sh <fork-ref>
#   OPENCODE_ARTIFACT_REF=<fork-ref> bash automation/build-opencode-artifact.sh
#
# Inputs (explicit only; never a mutable `latest`):
#   <fork-ref> or $OPENCODE_ARTIFACT_REF : kodmial/opencode commit SHA or
#       branch/tag ref for the experiment (e.g. the source-stripped
#       candidate, the bounded-output branch, or the direct-headless branch).
#   $UPSTREAM_BASE_REVISION (optional)  : upstream base revision to record;
#       defaults to automation/opencode-fork.baseline.json upstream_base_commit.
#   $BUN_VERSION (optional)             : recorded toolchain label; the build
#       itself uses the `bun` on PATH (upstream-supported release path).
#   $BUILD_ID / $BUILDER (optional)     : build identity labels.
#
# What it does:
#   1. Clones kodmial/opencode, fetches and checks out the exact ref, and
#      resolves the immutable fork commit SHA (never `latest`).
#   2. Reuses the upstream-supported Bun compile/release path:
#        bun install
#        bun run packages/opencode/script/build.ts
#      then packages the Linux x86-64 standalone binary
#      (upstream release asset opencode-linux-x64.tar.gz).
#   3. Records fork commit SHA, upstream base revision, build toolchain,
#      binary SHA-256 and build identity in a validated fingerprint manifest
#      under .opencode-artifacts/<exp-id>/ (content-addressed by fork SHA,
#      so concurrent experiments never overwrite each other).
#   4. Prints the immutable GitHub-backed reference
#      (releases/download/opencode-exp-<short>/opencode-linux-x64-<short>.tar.gz)
#      that Render provisioning must select. Uploading that asset
#      (`gh release create`) is workflow-owned and never done here.
#
# No secrets are required or printed. This script performs no git pushes,
# no branch switches in this repo, and no workflow-file changes.
set -euo pipefail

FORK_REF="${1:-${OPENCODE_ARTIFACT_REF:-}}"
if [[ -z "$FORK_REF" ]]; then
  echo "::error::build-opencode-artifact.sh requires an explicit fork ref (arg or OPENCODE_ARTIFACT_REF)." >&2
  exit 2
fi
LOWER_REF="$(tr '[:upper:]' '[:lower:]' <<<"$FORK_REF")"
if [[ "$LOWER_REF" == "latest" || "$LOWER_REF" == "latest-release" || "$LOWER_REF" == "stable" || "$LOWER_REF" == "head" ]]; then
  echo "::error::refusing mutable fork ref '$FORK_REF'; pass an explicit commit/ref." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="${ARTIFACT_WORK_DIR:-$(mktemp -d)}"
FORK_DIR="$WORK_DIR/opencode-fork"
FORK_REMOTE="https://github.com/kodmial/opencode.git"

UPSTREAM_BASE_REVISION="${UPSTREAM_BASE_REVISION:-$(python3 -c "import json;print(json.load(open('$REPO_ROOT/automation/opencode-fork.baseline.json'))['upstream_base_commit'])")}"
BUN_LABEL="${BUN_VERSION:-$(bun --version 2>/dev/null || echo unknown)}"
BUILD_ID="${BUILD_ID:-local-$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo local)}"
BUILDER="${BUILDER:-local-builder}"

echo "Cloning kodmial/opencode at explicit ref '$FORK_REF'..."
rm -rf "$FORK_DIR"
git clone "$FORK_REMOTE" "$FORK_DIR"
git -C "$FORK_DIR" fetch origin "$FORK_REF"
git -C "$FORK_DIR" checkout "$FORK_REF"
FORK_SHA="$(git -C "$FORK_DIR" rev-parse HEAD)"
echo "Resolved fork commit SHA: $FORK_SHA"

echo "Building with the upstream-supported Bun release path..."
(
  cd "$FORK_DIR"
  bun install
  bun run packages/opencode/script/build.ts
)

# Locate the Linux x86-64 standalone binary produced by the release path.
BIN_CANDIDATE=""
for candidate in \
  "$FORK_DIR/packages/opencode/dist/opencode-linux-x64" \
  "$FORK_DIR/packages/opencode/dist/opencode" \
  "$FORK_DIR/dist/opencode-linux-x64" \
  "$FORK_DIR/opencode-linux-x64" \
  "$FORK_DIR/packages/opencode/bin/opencode"; do
  if [[ -x "$candidate" && -f "$candidate" ]]; then
    BIN_CANDIDATE="$candidate"
    break
  fi
done
if [[ -z "$BIN_CANDIDATE" ]]; then
  echo "::error::Bun build finished but no Linux x86-64 standalone binary was found." >&2
  exit 1
fi
file "$BIN_CANDIDATE" || true
test -x "$BIN_CANDIDATE"

python3 - "$FORK_REF" "$FORK_SHA" "$UPSTREAM_BASE_REVISION" "$BIN_CANDIDATE" "$BUN_LABEL" "$BUILD_ID" "$BUILDER" "$REPO_ROOT" <<'PY'
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(sys.argv[8], "automation"))
from opencode_artifact import (
    artifact_id_from_sha,
    artifact_paths,
    build_artifact_manifest,
    compute_file_sha256,
)

fork_ref, fork_sha, upstream_base, bin_path, bun, build_id, builder, root = sys.argv[1:9]
digest = compute_file_sha256(bin_path)
size = os.path.getsize(bin_path)
artifact_id = artifact_id_from_sha(fork_sha)
binary_dest, manifest_path = artifact_paths(root, artifact_id)
os.makedirs(os.path.dirname(binary_dest), exist_ok=True)
# Content-addressed layout: this id owns only its own directory, so
# concurrent experiments never overwrite each other.
shutil.copyfile(bin_path, binary_dest)
os.chmod(binary_dest, 0o755)
manifest = build_artifact_manifest(
    fork_ref=fork_ref,
    fork_commit_sha=fork_sha,
    upstream_base_revision=upstream_base,
    binary_sha256=digest,
    binary_bytes=size,
    build_toolchain={"bun": bun, "os": "linux", "arch": "x64"},
    build_identity={"builder": builder, "build_id": build_id},
)
with open(manifest_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
print("artifact_id: %s" % artifact_id)
print("binary: %s" % binary_dest)
print("manifest: %s" % manifest_path)
print("fingerprint: %s:%s" % (fork_sha[:12], digest[:16]))
print("immutable_ref: %s" % manifest["immutable_ref"]["asset_url"])
PY

SHORT="$(cut -c1-12 <<<"$FORK_SHA")"
echo "Done. Immutable reference: https://github.com/kodmial/opencode/releases/download/opencode-exp-$SHORT/opencode-linux-x64-$SHORT.tar.gz"
echo "Publish with (workflow-owned): gh release create opencode-exp-$SHORT --repo kodmial/opencode --title <title> --notes <notes> <asset>"
