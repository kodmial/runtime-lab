"""Parallel OpenCode experiment artifact pipeline (issue #86).

Stdlib-only, offline, no network or git mutations. This module is the
machine-readable contract shared by the parallel memory experiments: it
builds deterministic standalone Linux OpenCode binaries from arbitrary
supplied ``kodmial/opencode`` experiment refs and lets Runtime Lab/Render
consume those exact artifacts.

Upstream-supported build path: the fork compiles with Bun
(``bun install`` then ``bun run <build script>``, the same
``packages/opencode/script/build.ts`` release path the pinned upstream
release uses) producing the standalone ``opencode-linux-<arch>`` binary
the Render workers execute. Variant tracks add their own build script
(``build-lite.ts`` / ``build-coding.ts`` / ``build-direct.ts``) over the
same Bun compile path; this module allow-lists those additive scripts and
fails closed on anything else.

Immutable-reference rules (enforced fail-closed everywhere below):

- Every build request carries an explicit fork commit/ref; the mutable
  ``latest`` pointer (and bare branch names such as ``main``) is never
  accepted as an artifact identity.
- Every built artifact records fork commit SHA, upstream base revision,
  build toolchain, binary SHA-256 and build identity.
- Every artifact is published/exposed by an immutable GitHub-backed
  reference (a release tag embedding the fork SHA, never ``latest``).
- Runtime selection is explicit: an artifact reference selects that exact
  binary; several experiment artifacts coexist under per-artifact
  directories and never overwrite each other; the upstream pinned binary
  stays the baseline only and is never a silent fallback during a
  qualified experiment.
- Readiness fails when the requested fingerprint/version is not running,
  and no runtime installer/download fallback ever runs for a requested
  experiment artifact.

Source grounding: fork baseline
``automation/opencode-fork.baseline.json`` (fork ``main``
``9000e7fc8d96c845512f7c73122431418a71d4e4`` over upstream
``anomalyco/opencode`` base ``75e1e7ae310dc36c86c920e8997d0e1181e24a88``,
pin ``1.18.33``); deterministic provisioning
(``automation/opencode_runner.py`` deploy artifact
``.opencode-bin/opencode``, ``RUNNER_ALLOW_RUNTIME_INSTALL=0`` strict
mode); qualification fingerprint contract
(``automation/opencode_qualification.py`` fork SHA + artifact SHA-256
selection).
"""

from __future__ import annotations

import hashlib
import json
import os
import re

SCHEMA = "runtime-lab-opencode-artifact/v1"

FORK_REPO = "kodmial/opencode"
UPSTREAM_REPO = "anomalyco/opencode"
UPSTREAM_BASE_COMMIT = "75e1e7ae310dc36c86c920e8997d0e1181e24a88"
UPSTREAM_TAG = "v1.18.33"
PINNED_VERSION = "1.18.33"

# Upstream-supported Bun compile/release path. The default build script is
# the fork's ``packages/opencode/script/build.ts`` (the same release path
# the pinned upstream binary is produced with); variant tracks add their
# own script over the same path, never a different toolchain.
DEFAULT_BUILD_SCRIPT = "packages/opencode/script/build.ts"
ALLOWED_BUILD_SCRIPTS = (
    "packages/opencode/script/build.ts",
    "packages/opencode/script/build-lite.ts",
    "packages/opencode/script/build-coding.ts",
    "packages/opencode/script/build-direct.ts",
)
BUN_INSTALL_COMMAND = "bun install --frozen-lockfile"
BUN_BUILD_COMMAND_TEMPLATE = "bun run {script}"

# Render workers are Linux x86-64; arm64 is built the same way for
# completeness. No other architecture is accepted.
REQUIRED_ARCH = "linux-x64"
SUPPORTED_ARCHS = ("linux-x64", "linux-arm64")

# Per-artifact deploy directory: each experiment artifact lives under its
# own immutable directory so concurrent artifacts never overwrite each
# other. The upstream pinned baseline keeps the legacy single path.
ARTIFACTS_DIRNAME = ".opencode-artifacts"
BASELINE_DEPLOY_SUBPATH = os.path.join(".opencode-bin", "opencode")

# Immutable GitHub-backed publication: one release tag per built artifact
# embedding the fork SHA (never ``latest``), in the fork repository.
ARTIFACT_RELEASES_REPO = "kodmial/opencode"

# Parallel experiment tracks that may each hold an artifact concurrently.
# New tracks are allowed when they match TRACK_RE; this tuple documents the
# tracks known when the pipeline landed.
KNOWN_TRACKS = ("source-stripped", "bounded-output", "direct-headless")

# Refs that are never an artifact identity: mutable pointers whose content
# can move under a running experiment.
MUTABLE_REFS = frozenset(
    {
        "latest",
        "main",
        "master",
        "dev",
        "stable",
        "head",
        "next",
        "current",
        "HEAD",
    }
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Explicit fork refs: branch/path segments plus an optional pinned
# ``@<sha>`` suffix. Anything outside this alphabet is rejected so the
# builder can pass the ref to git without shell interpolation risk.
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+\-]{0,127}(@[0-9a-f]{7,40})?$")
# Immutable GitHub-backed artifact references (``github-release:<repo>@<tag>``)
# carry characters (``:``) that a fork ref never has; they are validated by
# shape below instead of by ``validate_fork_ref``.
ARTIFACT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:\-+@]{0,159}$")
TRACK_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,47}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TAG_RE = re.compile(r"^opencode-[0-9a-f]{12}-linux-(x64|arm64)$")

ENV_ARTIFACT_ID = "OPENCODE_ARTIFACT_ID"
ENV_ARTIFACT_SHA256 = "OPENCODE_ARTIFACT_SHA256"
ENV_ARTIFACT_REF = "OPENCODE_ARTIFACT_REF"

REQUIRED_FINGERPRINT_KEYS = frozenset(
    {
        "schema",
        "fork_repo",
        "fork_ref",
        "fork_commit_sha",
        "upstream_base_commit",
        "upstream_tag",
        "pinned_version",
        "arch",
        "build_script",
        "build_toolchain",
        "binary_sha256",
        "build_identity",
        "artifact_id",
        "artifact_tag",
        "artifact_reference",
    }
)


def is_mutable_ref(ref: object) -> bool:
    """True when ``ref`` is a mutable pointer, never an artifact identity."""
    if not isinstance(ref, str) or not ref.strip():
        return True
    return ref.strip().lower() in {name.lower() for name in MUTABLE_REFS}


def validate_fork_ref(ref: str) -> str:
    """Validate an explicit fork commit/ref; fail closed on mutable input."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("fork ref must be a non-empty string")
    text = ref.strip()
    if is_mutable_ref(text):
        raise ValueError(
            "fork ref %r is a mutable pointer; supply an explicit "
            "experiment ref or commit SHA instead" % ref
        )
    if SHA_RE.match(text) is not None:
        return text
    if REF_RE.match(text) is None:
        raise ValueError("invalid fork ref: %r" % ref)
    return text


def validate_artifact_reference(ref: str) -> str:
    """Validate an immutable GitHub-backed artifact reference.

    Artifact references (``github-release:<repo>@<tag>``) are not fork refs:
    they name a published release, must embed the artifact tag after ``@``,
    and must never use a mutable ``latest`` pointer.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("artifact reference must be a non-empty string")
    text = ref.strip()
    if "latest" in text.lower():
        raise ValueError("artifact reference must never use 'latest': %r" % ref)
    if ARTIFACT_REF_RE.match(text) is None or "@" not in text:
        raise ValueError("invalid artifact reference: %r" % ref)
    return text


def validate_sha(value: str, name: str = "fork_commit_sha") -> str:
    """Validate a 40-char lowercase fork/upstream commit SHA."""
    if not isinstance(value, str) or SHA_RE.match(value) is None:
        raise ValueError("invalid 40-char lowercase SHA for %s: %r" % (name, value))
    return value


def validate_sha256(value: str, name: str = "binary_sha256") -> str:
    """Validate a 64-char lowercase binary SHA-256 digest."""
    if not isinstance(value, str) or SHA256_RE.match(value) is None:
        raise ValueError("invalid 64-char lowercase SHA-256 for %s: %r" % (name, value))
    return value


def validate_track(track: str) -> str:
    """Validate a parallel-experiment track slug."""
    if not isinstance(track, str) or TRACK_RE.match(track) is None:
        raise ValueError("invalid experiment track: %r" % track)
    return track


def sanitize_track(track: str) -> str:
    """Normalize a track slug for artifact ids (fail closed on misuse)."""
    return validate_track(str(track).strip().lower())


def artifact_id_for(fork_commit_sha: str, track: str) -> str:
    """Derive the immutable per-artifact directory id for a commit+track.

    Two different fork commits (or two tracks on the same commit) always
    produce different ids, so concurrent experiment artifacts stay
    distinguishable and never share a path.
    """
    sha = validate_sha(fork_commit_sha)
    slug = sanitize_track(track)
    return "opencode-%s-%s" % (slug, sha[:12])


def artifact_tag_for(fork_commit_sha: str, arch: str) -> str:
    """Derive the immutable release tag for a built artifact."""
    sha = validate_sha(fork_commit_sha)
    if arch not in SUPPORTED_ARCHS:
        raise ValueError("unsupported arch %r; expected one of %s" % (arch, list(SUPPORTED_ARCHS)))
    return "opencode-%s-%s" % (sha[:12], arch)


def github_reference_for(artifact_tag: str, repo: str = ARTIFACT_RELEASES_REPO) -> str:
    """Derive the immutable GitHub-backed reference for a release tag."""
    if not isinstance(artifact_tag, str) or TAG_RE.match(artifact_tag) is None:
        raise ValueError("invalid artifact tag: %r" % artifact_tag)
    if not isinstance(repo, str) or "/" not in repo:
        raise ValueError("invalid releases repo: %r" % repo)
    if "latest" in artifact_tag.lower():
        raise ValueError("artifact reference must never use 'latest'")
    return "github-release:%s@%s" % (repo, artifact_tag)


def validate_build_toolchain(toolchain: object) -> dict:
    """Validate the recorded build toolchain (Bun compile path proof)."""
    if not isinstance(toolchain, dict):
        raise ValueError("build_toolchain must be an object")
    for key in ("bun_version", "build_script", "build_command"):
        value = toolchain.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("build_toolchain.%s must be a non-empty string" % key)
    script = str(toolchain["build_script"]).strip()
    if script not in ALLOWED_BUILD_SCRIPTS:
        raise ValueError(
            "build_toolchain.build_script %r is not an approved Bun build path %s"
            % (script, list(ALLOWED_BUILD_SCRIPTS))
        )
    if "bun" not in str(toolchain["build_command"]):
        raise ValueError("build_toolchain.build_command must use the Bun compile path")
    return dict(toolchain)


def validate_build_identity(identity: object) -> dict:
    """Validate the recorded build identity (who/when produced the binary)."""
    if not isinstance(identity, dict):
        raise ValueError("build_identity must be an object")
    for key in ("builder", "run_id"):
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("build_identity.%s must be a non-empty string" % key)
    return dict(identity)


def make_fingerprint(
    *,
    fork_ref: str,
    fork_commit_sha: str,
    binary_sha256: str,
    track: str,
    arch: str = REQUIRED_ARCH,
    build_script: str = DEFAULT_BUILD_SCRIPT,
    build_toolchain: dict | None = None,
    build_identity: dict | None = None,
    upstream_base_commit: str = UPSTREAM_BASE_COMMIT,
    upstream_tag: str = UPSTREAM_TAG,
    pinned_version: str = PINNED_VERSION,
) -> dict:
    """Build and validate an immutable experiment-artifact fingerprint."""
    ref = validate_fork_ref(fork_ref)
    sha = validate_sha(fork_commit_sha)
    digest = validate_sha256(binary_sha256)
    slug = sanitize_track(track)
    if arch not in SUPPORTED_ARCHS:
        raise ValueError("unsupported arch %r; expected one of %s" % (arch, list(SUPPORTED_ARCHS)))
    if build_script not in ALLOWED_BUILD_SCRIPTS:
        raise ValueError(
            "build_script %r is not an approved Bun build path %s"
            % (build_script, list(ALLOWED_BUILD_SCRIPTS))
        )
    validate_sha(upstream_base_commit, "upstream_base_commit")
    if not isinstance(upstream_tag, str) or not upstream_tag.startswith("v"):
        raise ValueError("upstream_tag must start with 'v': %r" % upstream_tag)
    if not isinstance(pinned_version, str) or VERSION_RE.match(pinned_version) is None:
        raise ValueError("invalid pinned_version: %r" % pinned_version)
    toolchain = validate_build_toolchain(
        build_toolchain
        or {
            "bun_version": "measured-at-build",
            "build_script": build_script,
            "build_command": BUN_BUILD_COMMAND_TEMPLATE.format(script=build_script),
        }
    )
    if toolchain["build_script"] != build_script:
        raise ValueError("build_toolchain.build_script must match build_script")
    identity = validate_build_identity(build_identity or {"builder": "unrecorded", "run_id": "unrecorded"})
    artifact_id = artifact_id_for(sha, slug)
    artifact_tag = artifact_tag_for(sha, arch)
    fingerprint = {
        "schema": SCHEMA,
        "fork_repo": FORK_REPO,
        "fork_ref": ref,
        "fork_commit_sha": sha,
        "upstream_base_commit": upstream_base_commit,
        "upstream_tag": upstream_tag,
        "pinned_version": pinned_version,
        "arch": arch,
        "build_script": build_script,
        "build_toolchain": toolchain,
        "binary_sha256": digest,
        "build_identity": identity,
        "artifact_id": artifact_id,
        "artifact_tag": artifact_tag,
        "artifact_reference": github_reference_for(artifact_tag),
    }
    return validate_fingerprint(fingerprint)


def validate_fingerprint(data: dict) -> dict:
    """Validate a parsed artifact fingerprint; fail closed on any defect."""
    if not isinstance(data, dict):
        raise ValueError("fingerprint must be a JSON object")
    missing = sorted(REQUIRED_FINGERPRINT_KEYS - set(data.keys()))
    extra = sorted(set(data.keys()) - REQUIRED_FINGERPRINT_KEYS)
    if missing or extra:
        raise ValueError("fingerprint keys mismatch: missing=%s extra=%s" % (missing, extra))
    if data.get("schema") != SCHEMA:
        raise ValueError("invalid fingerprint schema: %r" % data.get("schema"))
    if data.get("fork_repo") != FORK_REPO:
        raise ValueError("fork_repo must be %r, got %r" % (FORK_REPO, data.get("fork_repo")))
    validate_fork_ref(data.get("fork_ref", ""))
    sha = validate_sha(data.get("fork_commit_sha", ""))
    validate_sha(data.get("upstream_base_commit", ""), "upstream_base_commit")
    validate_sha256(data.get("binary_sha256", ""))
    arch = data.get("arch")
    if arch not in SUPPORTED_ARCHS:
        raise ValueError("unsupported arch %r" % (arch,))
    if data.get("build_script") not in ALLOWED_BUILD_SCRIPTS:
        raise ValueError("unapproved build_script %r" % (data.get("build_script"),))
    validate_build_toolchain(data.get("build_toolchain"))
    validate_build_identity(data.get("build_identity"))
    track_guess = ""
    artifact_id = data.get("artifact_id", "")
    if not isinstance(artifact_id, str) or not artifact_id.startswith("opencode-"):
        raise ValueError("invalid artifact_id: %r" % (artifact_id,))
    # The artifact id must embed this fingerprint's own commit prefix so a
    # copied id from a sibling experiment cannot masquerade as this one.
    if not artifact_id.endswith(sha[:12]):
        raise ValueError("artifact_id %r does not match fork_commit_sha %r" % (artifact_id, sha))
    try:
        middle = artifact_id[len("opencode-") : -13]
        track_guess = middle
        sanitize_track(track_guess)
    except ValueError:
        raise ValueError("invalid artifact_id track segment: %r" % (artifact_id,))
    expected_tag = artifact_tag_for(sha, arch)
    if data.get("artifact_tag") != expected_tag:
        raise ValueError(
            "artifact_tag %r does not match fork_commit_sha/arch (expected %r)"
            % (data.get("artifact_tag"), expected_tag)
        )
    expected_ref = github_reference_for(expected_tag)
    if data.get("artifact_reference") != expected_ref:
        raise ValueError(
            "artifact_reference %r is not the immutable reference for tag %r"
            % (data.get("artifact_reference"), expected_tag)
        )
    _ = track_guess
    return data


def sha256_of_file(path: str) -> str:
    """Return the lowercase hex SHA-256 of a file (streamed, bounded RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_binary_checksum(path: str, expected_sha256: str) -> str:
    """Verify a built binary against its fingerprint; fail closed on mismatch."""
    expected = validate_sha256(expected_sha256)
    if not isinstance(path, str) or not path:
        raise ValueError("binary path must be a non-empty string")
    if not os.path.isfile(path):
        raise FileNotFoundError("artifact binary not found: %r" % path)
    actual = sha256_of_file(path)
    if actual != expected:
        raise ValueError(
            "artifact checksum mismatch for %r: expected %s, got %s" % (path, expected, actual)
        )
    return actual


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def artifact_binary_path(repo_root: str | None, artifact_id: str) -> str:
    """Per-artifact binary path; concurrent artifacts never share a path."""
    root = repo_root or _repo_root()
    if not isinstance(artifact_id, str) or not artifact_id.startswith("opencode-"):
        raise ValueError("invalid artifact_id: %r" % artifact_id)
    if "/" in artifact_id or artifact_id in (".", ".."):
        raise ValueError("invalid artifact_id: %r" % artifact_id)
    return os.path.join(root, ARTIFACTS_DIRNAME, artifact_id, "opencode")


def artifact_binary_candidates(repo_root: str | None, artifact_id: str) -> list[str]:
    """Ordered candidate paths for one artifact (source tree + cwd)."""
    primary = artifact_binary_path(repo_root, artifact_id)
    candidates = [primary]
    try:
        alt = os.path.join(
            os.path.abspath(os.getcwd()), ARTIFACTS_DIRNAME, artifact_id, "opencode"
        )
        if alt != primary:
            candidates.append(alt)
    except Exception:
        pass
    return candidates


def baseline_binary_path(repo_root: str | None) -> str:
    """Legacy upstream-baseline binary path (unchanged by experiments)."""
    root = repo_root or _repo_root()
    return os.path.join(root, BASELINE_DEPLOY_SUBPATH)


def resolve_requested_artifact(env: object = None) -> dict | None:
    """Resolve the explicitly requested experiment artifact from the env.

    Returns ``None`` in upstream-baseline mode (no artifact requested).
    Fails closed on partial configuration: a requested SHA without an id
    (or vice versa) is a configuration error, never a silent baseline.
    """
    source = os.environ if env is None else env
    if not hasattr(source, "get"):
        raise ValueError("env must be a mapping")
    artifact_id = str(source.get(ENV_ARTIFACT_ID, "") or "").strip()
    artifact_sha = str(source.get(ENV_ARTIFACT_SHA256, "") or "").strip().lower()
    artifact_ref = str(source.get(ENV_ARTIFACT_REF, "") or "").strip()
    if not artifact_id and not artifact_sha and not artifact_ref:
        return None
    if not artifact_id or not artifact_sha:
        raise ValueError(
            "partial artifact selection (%s=%r %s=%r): an experiment run must "
            "set both the artifact id and its SHA-256, or neither (baseline)"
            % (ENV_ARTIFACT_ID, artifact_id, ENV_ARTIFACT_SHA256, artifact_sha)
        )
    if not artifact_id.startswith("opencode-"):
        raise ValueError("invalid %s: %r" % (ENV_ARTIFACT_ID, artifact_id))
    validate_sha256(artifact_sha, ENV_ARTIFACT_SHA256)
    if artifact_ref:
        validate_artifact_reference(artifact_ref)
    return {
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha,
        "artifact_ref": artifact_ref,
    }


def check_artifact_readiness(
    binary_path: str | None, expected_sha256: str
) -> tuple[bool, str, str]:
    """Check that the exact requested artifact binary is present and intact.

    Returns (ready, resolved_path, detail). Never raises: any failure is a
    not-ready verdict with a safe detail string. Missing, non-executable or
    checksum-mismatched binaries are not ready and must never fall back to
    the upstream baseline inside a qualified experiment.
    """
    try:
        expected = validate_sha256(expected_sha256)
    except ValueError as exc:
        return False, binary_path or "", "invalid expected checksum: %s" % exc
    resolved = (binary_path or "").strip()
    if not resolved:
        return False, "", "experiment artifact binary path is empty"
    if not os.path.isfile(resolved):
        return False, resolved, "experiment artifact binary not found: %r" % resolved
    if not os.access(resolved, os.X_OK):
        return False, resolved, "experiment artifact binary is non-executable: %r" % resolved
    try:
        actual = sha256_of_file(resolved)
    except OSError as exc:
        return False, resolved, "cannot read experiment artifact binary: %s" % exc
    if actual != expected:
        return (
            False,
            resolved,
            "experiment artifact checksum mismatch: expected %s, got %s" % (expected, actual),
        )
    return True, resolved, actual[:16]


def build_steps_for(fingerprint: dict, workdir: str = "$BUILD_DIR/opencode-build") -> list[str]:
    """Return the resourced-builder shell steps for one explicit fork ref.

    Every step pins the explicit fork commit SHA; no step consults a
    mutable ``latest`` pointer. The builder clones the fork, checks out the
    exact SHA, proves the upstream base is contained, compiles with the
    upstream-supported Bun path, fingerprints the binary and writes the
    fingerprint JSON next to it.
    """
    data = validate_fingerprint(fingerprint)
    sha = data["fork_commit_sha"]
    script = data["build_script"]
    arch = data["arch"]
    tag = data["artifact_tag"]
    track = data["artifact_id"][len("opencode-") : -13]
    return [
        "git clone https://github.com/%s.git %s/fork" % (FORK_REPO, workdir),
        "cd %s/fork && git fetch origin %s" % (workdir, sha),
        "cd %s/fork && git checkout %s" % (workdir, sha),
        "cd %s/fork && test \"$(git rev-parse HEAD)\" = \"%s\"" % (workdir, sha),
        "cd %s/fork && git merge-base --is-ancestor %s HEAD"
        % (workdir, data["upstream_base_commit"]),
        "cd %s/fork && %s" % (workdir, BUN_INSTALL_COMMAND),
        "cd %s/fork && %s" % (workdir, BUN_BUILD_COMMAND_TEMPLATE.format(script=script)),
        "sha256sum %s/fork/opencode-%s* > %s/%s.sha256" % (workdir, arch, workdir, tag),
        "bash automation/build-opencode-artifact.sh fingerprint"
        " --sha %s --track %s --binary %s/fork/opencode-%s --out %s/%s.json"
        % (sha, track, workdir, arch, workdir, tag),
    ]


def artifact_env_for_payload(fingerprint: dict) -> dict[str, str]:
    """Environment bindings selecting one exact artifact on a Render worker."""
    data = validate_fingerprint(fingerprint)
    return {
        ENV_ARTIFACT_ID: data["artifact_id"],
        ENV_ARTIFACT_SHA256: data["binary_sha256"],
        ENV_ARTIFACT_REF: data["artifact_reference"],
    }


def start_command_with_artifact(base_start_command: str, fingerprint: dict) -> str:
    """Prefix a Render start command with explicit artifact selection env."""
    if not isinstance(base_start_command, str) or not base_start_command.strip():
        raise ValueError("base_start_command must be a non-empty string")
    env = artifact_env_for_payload(fingerprint)
    prefix = "%s=%s %s=%s %s=%s" % (
        ENV_ARTIFACT_ID,
        env[ENV_ARTIFACT_ID],
        ENV_ARTIFACT_SHA256,
        env[ENV_ARTIFACT_SHA256],
        ENV_ARTIFACT_REF,
        env[ENV_ARTIFACT_REF],
    )
    return "%s %s" % (prefix, base_start_command.strip())


def fingerprint_to_json(fingerprint: dict) -> str:
    """Serialize a validated fingerprint deterministically (no wall clock)."""
    data = validate_fingerprint(fingerprint)
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def fingerprint_from_json(text: str) -> dict:
    """Parse and validate a fingerprint JSON document."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("fingerprint JSON is empty")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("fingerprint JSON does not parse: %s" % exc) from exc
    return validate_fingerprint(data)
