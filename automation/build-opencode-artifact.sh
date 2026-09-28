#!/usr/bin/env bash
# Build a deterministic standalone Linux OpenCode experiment artifact (issue #86).
#
# The builder compiles ONE explicit kodmial/opencode fork ref with the
# upstream-supported Bun compile/release path and fingerprints the result,
# so parallel memory experiments can each hand this pipeline a commit/ref
# and receive an immutable runnable Linux artifact.
#
# Usage:
#   bash automation/build-opencode-artifact.sh build --ref <fork-ref>
#       --sha <40-char fork commit SHA> --track <experiment-track>
#       [--arch linux-x64|linux-arm64] [--build-script <script>]
#       [--bun-version <v>] [--builder <name>] [--run-id <id>]
#       [--workdir <dir>] [--out <dir>]
#   bash automation/build-opencode-artifact.sh verify --artifact-dir <dir>
#   bash automation/build-opencode-artifact.sh fingerprint --sha <sha>
#       --track <track> --binary <path> [same build opts] [--out <path>]
#
# Rules (fail closed):
# - The fork ref must be explicit; mutable pointers (latest/main/stable)
#   are rejected before any network or build step runs.
# - The checked-out HEAD must equal the supplied --sha exactly.
# - The recorded upstream base commit must be an ancestor of HEAD.
# - At least the Linux architecture required by Render workers is built
#   (default linux-x64); every artifact records arch + toolchain + SHA-256.
# - Each artifact lands under .opencode-artifacts/<artifact-id>/ so
#   concurrent experiment artifacts coexist without overwriting each other.
# - No secrets are required or printed; the fingerprint JSON is the only
#   machine-readable output besides the binary itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FORK_REPO="kodmial/opencode"
UPSTREAM_BASE="75e1e7ae310dc36c86c920e8997d0e1181e24a88"
DEFAULT_ARCH="linux-x64"
DEFAULT_BUILD_SCRIPT="packages/opencode/script/build.ts"

die() { echo "::error::$*" >&2; exit 1; }

is_mutable_ref() {
  case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
    latest|main|master|dev|stable|head|next|current) return 0 ;;
    *) return 1 ;;
  esac
}

require_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]] || die "expected a 40-char lowercase commit SHA for $2, got '$1'"
}

cmd_build() {
  local ref="" sha="" track="" arch="$DEFAULT_ARCH" build_script="$DEFAULT_BUILD_SCRIPT"
  local bun_version="measured-at-build" builder="local-builder" run_id="unrecorded"
  local workdir="" out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ref) ref="${2:-}"; shift 2 ;;
      --sha) sha="${2:-}"; shift 2 ;;
      --track) track="${2:-}"; shift 2 ;;
      --arch) arch="${2:-}"; shift 2 ;;
      --build-script) build_script="${2:-}"; shift 2 ;;
      --bun-version) bun_version="${2:-}"; shift 2 ;;
      --builder) builder="${2:-}"; shift 2 ;;
      --run-id) run_id="${2:-}"; shift 2 ;;
      --workdir) workdir="${2:-}"; shift 2 ;;
      --out) out="${2:-}"; shift 2 ;;
      *) die "unknown build flag: $1" ;;
    esac
  done
  [[ -n "$ref" ]] || die "--ref is required (explicit fork commit/ref; never 'latest')"
  [[ -n "$sha" ]] || die "--sha is required (explicit 40-char fork commit SHA)"
  [[ -n "$track" ]] || die "--track is required (parallel-experiment track slug)"
  is_mutable_ref "$ref" && die "ref '$ref' is a mutable pointer; supply an explicit experiment ref"
  require_sha "$sha" "--sha"
  [[ "$track" =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]] || die "invalid track slug: '$track'"
  [[ "$arch" == "linux-x64" || "$arch" == "linux-arm64" ]] || die "unsupported arch: '$arch'"
  case "$build_script" in
    packages/opencode/script/build.ts|packages/opencode/script/build-lite.ts|packages/opencode/script/build-coding.ts|packages/opencode/script/build-direct.ts) ;;
    *) die "unapproved build script: '$build_script'" ;;
  esac
  workdir="${workdir:-$RUNNER_TEMP/opencode-build}"
  [[ -n "${RUNNER_TEMP:-}" ]] || workdir="${workdir:-/tmp/opencode-build}"
  out="${out:-$REPO_ROOT/.opencode-artifacts}"
  command -v git >/dev/null || die "git is required"
  command -v bun >/dev/null || die "bun is required (upstream-supported compile path)"
  command -v python3 >/dev/null || die "python3 is required (fingerprinting)"

  local clone_dir="$workdir/fork"
  rm -rf "$clone_dir"
  mkdir -p "$workdir"
  echo "Cloning https://github.com/$FORK_REPO.git ..."
  git clone "https://github.com/$FORK_REPO.git" "$clone_dir"
  (
    cd "$clone_dir"
    git fetch origin "$sha"
    git checkout "$sha"
    test "$(git rev-parse HEAD)" = "$sha" || die "HEAD does not equal requested $sha"
    git merge-base --is-ancestor "$UPSTREAM_BASE" HEAD \
      || die "upstream base $UPSTREAM_BASE is not an ancestor of $sha"
    echo "Upstream-supported Bun compile path: bun install + bun run $build_script"
    bun install --frozen-lockfile
    bun run "$build_script"
  )
  local binary
  binary="$(ls -1 "$clone_dir"/opencode-"$arch"* 2>/dev/null | head -n 1 || true)"
  [[ -n "$binary" && -x "$binary" ]] || die "build produced no executable opencode-$arch binary"
  local digest
  digest="$(sha256sum "$binary" | awk '{print $1}')"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die "cannot fingerprint binary checksum"

  local short="${sha:0:12}"
  local artifact_id="opencode-$track-$short"
  local artifact_tag="opencode-$short-$arch"
  local dest="$out/$artifact_id"
  mkdir -p "$dest"
  cp -f "$binary" "$dest/opencode"
  chmod 755 "$dest/opencode"
  # Authoritative fingerprint write (env-populated) plus checksum proof.
  BUILD_FORK_REF="$ref" BUILD_FORK_SHA="$sha" BUILD_TRACK="$track" \
    BUILD_ARCH="$arch" BUILD_SCRIPT="$build_script" \
    BUILD_BUN_VERSION="$bun_version" BUILD_BUILDER="$builder" BUILD_RUN_ID="$run_id" \
    python3 - > "$dest/fingerprint.json" <<'PYEOF'
import json, os, sys
sys.path.insert(0, "automation")
from opencode_artifacts import fingerprint_to_json, make_fingerprint
fp = make_fingerprint(
    fork_ref=os.environ["BUILD_FORK_REF"],
    fork_commit_sha=os.environ["BUILD_FORK_SHA"],
    binary_sha256=sys.argv[1],
    track=os.environ["BUILD_TRACK"],
    arch=os.environ["BUILD_ARCH"],
    build_script=os.environ["BUILD_SCRIPT"],
    build_toolchain={
        "bun_version": os.environ["BUILD_BUN_VERSION"],
        "build_script": os.environ["BUILD_SCRIPT"],
        "build_command": "bun run %s" % os.environ["BUILD_SCRIPT"],
    },
    build_identity={
        "builder": os.environ["BUILD_BUILDER"],
        "run_id": os.environ["BUILD_RUN_ID"],
    },
)
sys.stdout.write(fingerprint_to_json(fp))
PYEOF
  # NOTE: the write above is authoritative. Verify the binary against
  # the recorded fingerprint.
  BUILD_FORK_REF="$ref" BUILD_FORK_SHA="$sha" python3 - "$dest" <<'PYEOF'
import json, os, sys
sys.path.insert(0, "automation")
from opencode_artifacts import artifact_binary_path, fingerprint_from_json, verify_binary_checksum
dest = sys.argv[1]
with open(os.path.join(dest, "fingerprint.json"), encoding="utf-8") as handle:
    fp = fingerprint_from_json(handle.read())
verify_binary_checksum(os.path.join(dest, "opencode"), fp["binary_sha256"])
print("artifact ready: %s (%s)" % (fp["artifact_id"], fp["artifact_reference"]))
PYEOF
  echo "Published $dest/opencode ($artifact_tag)"
}

cmd_verify() {
  local dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --artifact-dir) dir="${2:-}"; shift 2 ;;
      *) die "unknown verify flag: $1" ;;
    esac
  done
  [[ -n "$dir" ]] || die "--artifact-dir is required"
  python3 - "$dir" <<'PYEOF'
import os, sys
sys.path.insert(0, "automation")
from opencode_artifacts import fingerprint_from_json, verify_binary_checksum
dest = sys.argv[1]
with open(os.path.join(dest, "fingerprint.json"), encoding="utf-8") as handle:
    fp = fingerprint_from_json(handle.read())
verify_binary_checksum(os.path.join(dest, "opencode"), fp["binary_sha256"])
print("verified %s (%s)" % (fp["artifact_id"], fp["artifact_reference"]))
PYEOF
}

cmd_fingerprint() {
  local sha="" track="" binary="" arch="$DEFAULT_ARCH" build_script="$DEFAULT_BUILD_SCRIPT"
  local bun_version="measured-at-build" builder="local-builder" run_id="unrecorded" out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --sha) sha="${2:-}"; shift 2 ;;
      --track) track="${2:-}"; shift 2 ;;
      --binary) binary="${2:-}"; shift 2 ;;
      --arch) arch="${2:-}"; shift 2 ;;
      --build-script) build_script="${2:-}"; shift 2 ;;
      --bun-version) bun_version="${2:-}"; shift 2 ;;
      --builder) builder="${2:-}"; shift 2 ;;
      --run-id) run_id="${2:-}"; shift 2 ;;
      --ref) ref_in="${2:-}"; shift 2 ;;
      --out) out="${2:-}"; shift 2 ;;
      *) die "unknown fingerprint flag: $1" ;;
    esac
  done
  # --ref is optional here (defaults to the explicit SHA identity).
  ref_in="${ref_in:-$sha}"
  BUILD_FORK_REF="$ref_in" BUILD_FORK_SHA="$sha" BUILD_TRACK="$track" \
    BUILD_ARCH="$arch" BUILD_SCRIPT="$build_script" \
    BUILD_BUN_VERSION="$bun_version" BUILD_BUILDER="$builder" BUILD_RUN_ID="$run_id" \
    python3 - "$binary" "$out" <<'PYEOF'
import json, os, sys
sys.path.insert(0, "automation")
from opencode_artifacts import fingerprint_to_json, make_fingerprint, sha256_of_file
binary, out = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
fp = make_fingerprint(
    fork_ref=os.environ["BUILD_FORK_REF"],
    fork_commit_sha=os.environ["BUILD_FORK_SHA"],
    binary_sha256=sha256_of_file(binary),
    track=os.environ["BUILD_TRACK"],
    arch=os.environ["BUILD_ARCH"],
    build_script=os.environ["BUILD_SCRIPT"],
    build_toolchain={
        "bun_version": os.environ["BUILD_BUN_VERSION"],
        "build_script": os.environ["BUILD_SCRIPT"],
        "build_command": "bun run %s" % os.environ["BUILD_SCRIPT"],
    },
    build_identity={
        "builder": os.environ["BUILD_BUILDER"],
        "run_id": os.environ["BUILD_RUN_ID"],
    },
)
if out:
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(fingerprint_to_json(fp))
else:
    sys.stdout.write(fingerprint_to_json(fp))
PYEOF
}

sub="${1:-}"; shift || true
case "$sub" in
  build) cmd_build "$@" ;;
  verify) cmd_verify "$@" ;;
  fingerprint) cmd_fingerprint "$@" ;;
  *) die "usage: build-opencode-artifact.sh {build|verify|fingerprint} ..." ;;
esac
