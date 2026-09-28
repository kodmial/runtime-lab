"""Parallel OpenCode experiment artifact pipeline (issue #86).

Stdlib-only shared infrastructure for building deterministic standalone
Linux OpenCode binaries from arbitrary ``kodmial/opencode`` experiment refs
and making Runtime Lab/Render consume those exact artifacts.

Contract summary (authoritative: issue #86 body plus Definition of Done):

- The builder accepts an explicit fork commit/ref as input and never relies
  on a mutable ``latest`` pointer.
- The build reuses the upstream-supported Bun compile/release path
  (``bun install`` then ``bun run packages/opencode/script/build.ts``,
  release asset ``opencode-linux-x64.tar.gz``).
- At least the Linux x86-64 architecture required by Render workers is
  built and fingerprinted.
- Each artifact records fork commit SHA, upstream base revision, build
  toolchain, binary SHA-256 and build identity.
- Each artifact is published/exposed by an immutable GitHub-backed
  reference (a release download URL keyed by the resolved commit SHA,
  never a ``latest`` pointer).
- Runtime Lab provisioning selects that exact binary from an explicit
  artifact reference; readiness fails when the requested fingerprint or
  version is not running.
- Several experiment artifacts coexist without overwriting each other
  (content-addressed ``.opencode-artifacts/<exp-id>/`` layout keyed by
  the resolved fork SHA).
- The upstream pinned binary stays the baseline only; a qualified
  experiment never silently falls back to it.

This module holds the machine-readable manifest contract, fork-ref
validation, immutable-reference helpers, content-addressed layout helpers,
selection resolution, on-disk verification, and the Bun/release command
shapes shared by the builder script
(``automation/build-opencode-artifact.sh``), the provisioning script
(``automation/install-opencode-artifact.sh``), the runner
(``automation/runner_server.py`` via ``automation/opencode_runner.py``)
and the Render payload (``automation/render_lifecycle.py``).
"""

from __future__ import annotations

import hashlib
import os
import re

ARTIFACT_SCHEMA = "runtime-lab-opencode-artifact/v1"

FORK_REPO = "kodmial/opencode"
UPSTREAM_REPO = "anomalyco/opencode"

# Content-addressed experiment artifact layout (repo-relative). Each
# artifact id owns its own subdirectory, so concurrent experiments never
# overwrite each other; the baseline ``.opencode-bin/opencode`` is left
# untouched for upstream-baseline mode.
ARTIFACT_DIRNAME = ".opencode-artifacts"
ARTIFACT_BIN_NAME = "opencode"

# Render workers require Linux x86-64 (upstream release asset
# ``opencode-linux-x64.tar.gz``). Artifacts for other arches may exist,
# but qualification requires at least this one.
REQUIRED_OS = "linux"
REQUIRED_ARCH = "x64"
REQUIRED_RELEASE_ASSET = "opencode-linux-x64.tar.gz"

# Explicit selection environment (all optional; empty means baseline).
ARTIFACT_REF_ENV_VAR = "OPENCODE_ARTIFACT_REF"
EXPECTED_FORK_SHA_ENV_VAR = "OPENCODE_EXPECTED_FORK_SHA"
EXPECTED_SHA256_ENV_VAR = "OPENCODE_EXPECTED_SHA256"
ARTIFACT_ID_ENV_VAR = "OPENCODE_ARTIFACT_ID"
ARTIFACT_URL_ENV_VAR = "OPENCODE_ARTIFACT_URL"

BASELINE_MODE = "upstream-baseline"
EXPERIMENT_MODE = "experiment"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

REQUIRED_MANIFEST_KEYS = frozenset(
    (
        "schema",
        "fork_repo",
        "fork_ref",
        "fork_commit_sha",
        "upstream_base_revision",
        "pinned_version",
        "build_toolchain",
        "binary_sha256",
        "binary_bytes",
        "build_identity",
        "immutable_ref",
        "artifact_id",
    )
)

REQUIRED_TOOLCHAIN_KEYS = frozenset(("bun", "os", "arch"))
REQUIRED_IMMUTABLE_KEYS = frozenset(("release_tag", "asset_name", "asset_url"))


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def validate_fork_ref(ref: object) -> str:
    """Validate an explicit fork commit/ref; fail closed on mutable input.

    Accepts a full 40-char commit SHA, a short SHA, or a branch/tag ref
    (including ``<branch>/<name>`` forms). Rejects empty input, whitespace,
    ``latest`` in any case, ``HEAD``-style floaters, and shell-unsafe
    characters. Returns the stripped ref unchanged on success.
    """
    if not isinstance(ref, str):
        raise ValueError("fork ref must be a string, got %r" % (ref,))
    text = ref.strip()
    if not text:
        raise ValueError("fork ref must not be empty (pass an explicit ref)")
    if text.lower() in ("latest", "latest-release", "stable", "head"):
        raise ValueError(
            "fork ref %r is a mutable pointer; pass an explicit commit/ref" % ref
        )
    if len(text) > 128:
        raise ValueError("fork ref is too long (max 128 chars): %r" % ref)
    if any(char.isspace() for char in text):
        raise ValueError("fork ref must not contain whitespace: %r" % ref)
    if ".." in text or "//" in text or text.startswith(("/", ".", "-")):
        raise ValueError("fork ref has an unsafe shape: %r" % ref)
    for forbidden in ("@", ":", "~", "^", "?", "*", "[", "\\", "$", "`", "!"):
        if forbidden in text:
            raise ValueError(
                "fork ref contains forbidden character %r: %r" % (forbidden, ref)
            )
    if REF_RE.match(text) is None:
        raise ValueError("fork ref has an unsupported shape: %r" % ref)
    return text


def sanitize_ref_slug(ref: str) -> str:
    """Map a validated ref to a filesystem-safe slug (staging only)."""
    text = validate_fork_ref(ref)
    slug = text.replace("/", "-").strip("-")[:64] or "ref"
    return slug


def artifact_id_from_sha(fork_sha: str) -> str:
    """Return the content-addressed artifact id for a resolved fork SHA."""
    if not isinstance(fork_sha, str) or SHA_RE.match(fork_sha) is None:
        raise ValueError("fork_sha must be a 40-char lowercase hex SHA")
    return "exp-%s" % fork_sha[:12]


def resolve_artifact_id(fork_ref: str, fork_sha: str = "") -> str:
    """Resolve the storage id: SHA-keyed when resolved, ref slug otherwise.

    Final manifests are always SHA-keyed so concurrent experiments never
    collide; the ref slug exists only for pre-resolution staging.
    """
    if fork_sha:
        return artifact_id_from_sha(fork_sha)
    return "ref-%s" % sanitize_ref_slug(fork_ref)


def immutable_release_tag(fork_sha: str) -> str:
    """Return the immutable release tag for a resolved fork SHA."""
    return artifact_id_from_sha(fork_sha).replace("exp-", "opencode-exp-")


def immutable_asset_name(fork_sha: str) -> str:
    """Return the immutable Linux asset name for a resolved fork SHA."""
    short = artifact_id_from_sha(fork_sha).replace("exp-", "")
    return "opencode-linux-x64-%s.tar.gz" % short


def immutable_artifact_url(fork_sha: str) -> str:
    """Return the immutable GitHub-backed download URL for a fork SHA."""
    tag = immutable_release_tag(fork_sha)
    asset = immutable_asset_name(fork_sha)
    return "https://github.com/%s/releases/download/%s/%s" % (
        FORK_REPO,
        tag,
        asset,
    )


def validate_immutable_ref(url: object, fork_sha: str = "") -> str:
    """Validate a GitHub-backed immutable artifact reference.

    The URL must live under ``kodmial/opencode/releases``, must never
    contain a mutable ``latest`` pointer, and must embed the resolved
    commit identity (full SHA or 12-char short SHA) when ``fork_sha`` is
    known. Returns the stripped URL on success.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("immutable artifact reference must be a non-empty string")
    text = url.strip()
    if "latest" in text.lower():
        raise ValueError("immutable reference must never use a 'latest' pointer")
    prefix = "https://github.com/%s/releases/download/" % FORK_REPO
    if not text.startswith(prefix):
        raise ValueError(
            "immutable reference must start with %r, got %r" % (prefix, url)
        )
    if fork_sha:
        if SHA_RE.match(fork_sha) is None:
            raise ValueError("fork_sha must be a 40-char lowercase hex SHA")
        if fork_sha not in text and fork_sha[:12] not in text:
            raise ValueError(
                "immutable reference must embed the resolved fork SHA %r"
                % fork_sha
            )
    return text


def validate_toolchain(toolchain: object) -> dict:
    """Validate the recorded build toolchain; fail closed on any defect."""
    if not isinstance(toolchain, dict):
        raise ValueError("build_toolchain must be an object")
    missing = sorted(REQUIRED_TOOLCHAIN_KEYS - set(toolchain.keys()))
    extra = sorted(set(toolchain.keys()) - REQUIRED_TOOLCHAIN_KEYS)
    if missing or extra:
        raise ValueError(
            "build_toolchain keys mismatch: missing=%s extra=%s" % (missing, extra)
        )
    bun = toolchain.get("bun")
    if not isinstance(bun, str) or not bun.strip():
        raise ValueError("build_toolchain.bun must be a non-empty string")
    if toolchain.get("os") != REQUIRED_OS:
        raise ValueError(
            "build_toolchain.os must be %r, got %r"
            % (REQUIRED_OS, toolchain.get("os"))
        )
    if toolchain.get("arch") != REQUIRED_ARCH:
        raise ValueError(
            "build_toolchain.arch must be %r, got %r"
            % (REQUIRED_ARCH, toolchain.get("arch"))
        )
    return dict(toolchain)


def build_artifact_manifest(
    *,
    fork_ref: str,
    fork_commit_sha: str,
    upstream_base_revision: str,
    binary_sha256: str,
    binary_bytes: int,
    build_toolchain: dict,
    build_identity: dict,
    pinned_version: str = "",
    artifact_id: str = "",
    immutable_ref: dict | None = None,
) -> dict:
    """Build and validate one experiment artifact fingerprint manifest."""
    ref = validate_fork_ref(fork_ref)
    if SHA_RE.match(fork_commit_sha or "") is None:
        raise ValueError("fork_commit_sha must be a 40-char lowercase hex SHA")
    if SHA_RE.match(upstream_base_revision or "") is None:
        raise ValueError("upstream_base_revision must be a 40-char lowercase hex SHA")
    if SHA256_RE.match((binary_sha256 or "").lower()) is None:
        raise ValueError("binary_sha256 must be a 64-char lowercase hex digest")
    if isinstance(binary_bytes, bool) or not isinstance(binary_bytes, int):
        raise ValueError("binary_bytes must be an int")
    if binary_bytes <= 0:
        raise ValueError("binary_bytes must be positive")
    toolchain = validate_toolchain(build_toolchain)
    if not isinstance(build_identity, dict):
        raise ValueError("build_identity must be an object")
    for key in ("builder", "build_id"):
        value = build_identity.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("build_identity.%s must be a non-empty string" % key)
    resolved_id = artifact_id.strip() if isinstance(artifact_id, str) else ""
    if resolved_id and resolved_id != artifact_id_from_sha(fork_commit_sha):
        raise ValueError(
            "artifact_id %r does not match fork SHA %r"
            % (artifact_id, fork_commit_sha)
        )
    resolved_id = resolved_id or artifact_id_from_sha(fork_commit_sha)
    if immutable_ref is None:
        immutable_ref = {
            "release_tag": immutable_release_tag(fork_commit_sha),
            "asset_name": immutable_asset_name(fork_commit_sha),
            "asset_url": immutable_artifact_url(fork_commit_sha),
        }
    if not isinstance(immutable_ref, dict):
        raise ValueError("immutable_ref must be an object")
    missing = sorted(REQUIRED_IMMUTABLE_KEYS - set(immutable_ref.keys()))
    extra = sorted(set(immutable_ref.keys()) - REQUIRED_IMMUTABLE_KEYS)
    if missing or extra:
        raise ValueError(
            "immutable_ref keys mismatch: missing=%s extra=%s" % (missing, extra)
        )
    validate_immutable_ref(
        immutable_ref.get("asset_url"), fork_commit_sha
    )
    if fork_commit_sha[:12] not in str(immutable_ref.get("release_tag", "")):
        raise ValueError("immutable_ref.release_tag must embed the fork short SHA")
    if fork_commit_sha[:12] not in str(immutable_ref.get("asset_name", "")):
        raise ValueError("immutable_ref.asset_name must embed the fork short SHA")
    if pinned_version and not re.match(r"^\d+\.\d+\.\d+$", pinned_version.strip()):
        raise ValueError("pinned_version must look like X.Y.Z, got %r" % pinned_version)
    return validate_artifact_manifest(
        {
            "schema": ARTIFACT_SCHEMA,
            "fork_repo": FORK_REPO,
            "fork_ref": ref,
            "fork_commit_sha": fork_commit_sha,
            "upstream_base_revision": upstream_base_revision,
            "pinned_version": (pinned_version or "").strip(),
            "build_toolchain": toolchain,
            "binary_sha256": binary_sha256.lower(),
            "binary_bytes": binary_bytes,
            "build_identity": dict(build_identity),
            "immutable_ref": dict(immutable_ref),
            "artifact_id": resolved_id,
        }
    )


def validate_artifact_manifest(data: dict) -> dict:
    """Validate a parsed artifact manifest and return it on success."""
    if not isinstance(data, dict):
        raise ValueError("artifact manifest must be a JSON object")
    missing = sorted(REQUIRED_MANIFEST_KEYS - set(data.keys()))
    extra = sorted(set(data.keys()) - REQUIRED_MANIFEST_KEYS)
    if missing or extra:
        raise ValueError(
            "artifact manifest keys mismatch: missing=%s extra=%s"
            % (missing, extra)
        )
    if data.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("invalid artifact schema: %r" % data.get("schema"))
    if data.get("fork_repo") != FORK_REPO:
        raise ValueError("fork_repo must be %r" % FORK_REPO)
    validate_fork_ref(data.get("fork_ref"))
    for key in ("fork_commit_sha", "upstream_base_revision"):
        value = data.get(key)
        if not isinstance(value, str) or SHA_RE.match(value) is None:
            raise ValueError("invalid 40-char lowercase SHA for %s" % key)
    digest = data.get("binary_sha256")
    if not isinstance(digest, str) or SHA256_RE.match(digest.lower()) is None:
        raise ValueError("binary_sha256 must be a 64-char hex digest")
    size = data.get("binary_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("binary_bytes must be a positive int")
    validate_toolchain(data.get("build_toolchain"))
    identity = data.get("build_identity")
    if not isinstance(identity, dict):
        raise ValueError("build_identity must be an object")
    for key in ("builder", "build_id"):
        value = identity.get(key) if isinstance(identity, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("build_identity.%s must be a non-empty string" % key)
    immutable = data.get("immutable_ref")
    if not isinstance(immutable, dict):
        raise ValueError("immutable_ref must be an object")
    missing_imm = sorted(REQUIRED_IMMUTABLE_KEYS - set(immutable.keys()))
    if missing_imm:
        raise ValueError("immutable_ref is missing keys: %s" % missing_imm)
    validate_immutable_ref(
        immutable.get("asset_url"), data.get("fork_commit_sha", "")
    )
    expected_id = artifact_id_from_sha(data["fork_commit_sha"])
    if data.get("artifact_id") != expected_id:
        raise ValueError(
            "artifact_id %r does not match fork SHA (expected %r)"
            % (data.get("artifact_id"), expected_id)
        )
    return data


def fingerprint_string(manifest: dict) -> str:
    """Return the short human fingerprint ``<fork12>:<sha256-16>``."""
    manifest = validate_artifact_manifest(manifest)
    return "%s:%s" % (
        manifest["fork_commit_sha"][:12],
        manifest["binary_sha256"][:16],
    )


def compute_file_sha256(path: str) -> str:
    """Return the lowercase hex SHA-256 of a file; fail closed on errors."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact_file(binary_path: str, expected_sha256: str) -> str | None:
    """Verify one artifact binary; return an error string or None on success."""
    if not isinstance(binary_path, str) or not binary_path:
        return "artifact binary path is empty"
    if SHA256_RE.match((expected_sha256 or "").lower()) is None:
        return "expected SHA-256 %r is not a 64-char hex digest" % expected_sha256
    if not os.path.isfile(binary_path):
        return "artifact binary is absent: %r" % binary_path
    if not os.access(binary_path, os.X_OK):
        return "artifact binary is non-executable: %r" % binary_path
    try:
        actual = compute_file_sha256(binary_path)
    except OSError as exc:
        return "cannot hash artifact binary %r: %s" % (binary_path, exc)
    if actual.lower() != expected_sha256.lower():
        return "artifact SHA-256 mismatch for %r" % binary_path
    return None


def artifact_paths(repo_root: str | None = None, artifact_id: str = "") -> tuple[str, str]:
    """Return ``(binary_path, manifest_path)`` for one artifact id."""
    root = repo_root or _repo_root()
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id must be a non-empty string")
    cleaned = artifact_id.strip()
    if cleaned != os.path.basename(cleaned) or not re.match(
        r"^(exp-[0-9a-f]{12}|ref-[A-Za-z0-9][A-Za-z0-9._\-]{0,63})$", cleaned
    ):
        raise ValueError("invalid artifact_id: %r" % artifact_id)
    directory = os.path.join(root, ARTIFACT_DIRNAME, cleaned)
    return (
        os.path.join(directory, ARTIFACT_BIN_NAME),
        os.path.join(directory, "%s.json" % cleaned),
    )


def artifact_binary_candidates(
    repo_root: str | None = None, artifact_id: str = ""
) -> list[str]:
    """Return candidate binary paths for one artifact (ordered)."""
    binary_path, _ = artifact_paths(repo_root, artifact_id)
    candidates = [binary_path]
    try:
        cwd_candidate = os.path.join(
            os.path.abspath(os.getcwd()), ARTIFACT_DIRNAME, artifact_id.strip()
        )
        cwd_binary = os.path.join(cwd_candidate, ARTIFACT_BIN_NAME)
        if cwd_binary not in candidates:
            candidates.append(cwd_binary)
    except Exception:
        pass
    return candidates


def resolve_artifact_selection(environ: object = None) -> dict[str, str]:
    """Resolve the requested artifact mode from the environment.

    Returns ``mode`` (``upstream-baseline`` when nothing is requested,
    ``experiment`` otherwise) plus the stripped ``artifact_ref``,
    ``expected_fork_sha``, ``expected_sha256``, ``artifact_id`` and
    ``artifact_url``. Any malformed experiment request fails closed with
    ``ValueError`` instead of silently falling back to the baseline.
    """
    source = os.environ if environ is None else environ
    if not hasattr(source, "get"):
        raise ValueError("environ must be a mapping")

    def _get(name: str) -> str:
        value = source.get(name, "")
        return str(value or "").strip()

    ref = _get(ARTIFACT_REF_ENV_VAR)
    fork_sha = _get(EXPECTED_FORK_SHA_ENV_VAR)
    digest = _get(EXPECTED_SHA256_ENV_VAR)
    artifact_id = _get(ARTIFACT_ID_ENV_VAR)
    url = _get(ARTIFACT_URL_ENV_VAR)
    if not ref and not fork_sha and not digest and not artifact_id and not url:
        return {
            "mode": BASELINE_MODE,
            "artifact_ref": "",
            "expected_fork_sha": "",
            "expected_sha256": "",
            "artifact_id": "",
            "artifact_url": "",
        }
    # Any partial experiment request is explicit: validate everything.
    if ref:
        validate_fork_ref(ref)
    if not ref and not fork_sha:
        raise ValueError(
            "experiment artifact requested without %s or %s"
            % (ARTIFACT_REF_ENV_VAR, EXPECTED_FORK_SHA_ENV_VAR)
        )
    if fork_sha and SHA_RE.match(fork_sha) is None:
        raise ValueError(
            "%s must be a 40-char lowercase hex SHA" % EXPECTED_FORK_SHA_ENV_VAR
        )
    if digest and SHA256_RE.match(digest.lower()) is None:
        raise ValueError(
            "%s must be a 64-char hex digest" % EXPECTED_SHA256_ENV_VAR
        )
    if artifact_id:
        artifact_paths(_repo_root(), artifact_id)
    elif fork_sha:
        artifact_id = artifact_id_from_sha(fork_sha)
    elif ref and SHA_RE.match(ref) is not None:
        # A full SHA passed as the ref is already content-addressed.
        artifact_id = artifact_id_from_sha(ref)
    if url:
        validate_immutable_ref(url, fork_sha or (ref if ref and SHA_RE.match(ref) else ""))
    return {
        "mode": EXPERIMENT_MODE,
        "artifact_ref": ref,
        "expected_fork_sha": fork_sha,
        "expected_sha256": digest.lower() if digest else "",
        "artifact_id": artifact_id,
        "artifact_url": url,
    }


def verify_experiment_selection(
    selection: dict, repo_root: str | None = None
) -> list[str]:
    """Fail-closed on-disk verification of an experiment selection.

    Checks the manifest exists and validates, the recorded fork SHA
    matches the requested fingerprint, the binary SHA-256 matches both
    the manifest and the requested digest, and the binary is executable.
    Returns a list of error strings (empty means ready).
    """
    errors: list[str] = []
    if not isinstance(selection, dict):
        return ["artifact selection must be a mapping"]
    if selection.get("mode") != EXPERIMENT_MODE:
        return ["artifact selection is not an experiment request"]
    artifact_id = str(selection.get("artifact_id") or "").strip()
    if not artifact_id:
        return ["experiment request has no artifact_id"]
    try:
        binary_path, manifest_path = artifact_paths(repo_root, artifact_id)
    except ValueError as exc:
        return [str(exc)]
    if not os.path.isfile(manifest_path):
        return ["artifact manifest is absent: %r" % manifest_path]
    try:
        import json as _json

        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = validate_artifact_manifest(_json.load(handle))
    except (OSError, ValueError) as exc:
        return ["artifact manifest is invalid: %s" % exc]
    expected_fork = str(selection.get("expected_fork_sha") or "").strip()
    if expected_fork and manifest["fork_commit_sha"] != expected_fork:
        errors.append(
            "artifact fork SHA %r does not match requested %r"
            % (manifest["fork_commit_sha"], expected_fork)
        )
    expected_digest = str(selection.get("expected_sha256") or "").strip()
    if expected_digest and manifest["binary_sha256"].lower() != expected_digest.lower():
        errors.append("artifact manifest SHA-256 does not match requested digest")
    file_error = verify_artifact_file(binary_path, manifest["binary_sha256"])
    if file_error is not None:
        errors.append(file_error)
    elif expected_digest and manifest["binary_sha256"].lower() != expected_digest.lower():
        errors.append("artifact binary SHA-256 does not match requested digest")
    return errors


# ---------------------------------------------------------------------------
# Upstream-supported Bun compile/release path (shared by builder + tests).
# ---------------------------------------------------------------------------


def bun_build_commands(fork_dir: str = ".") -> list[list[str]]:
    """Return the upstream-supported Bun build steps for a fork checkout.

    The fork builds the same headless CLI artifact validated in issue #76
    (``packages/opencode/script/build.ts`` over ``bun.lock``); the release
    step packages ``opencode-linux-x64.tar.gz``. No mutable ``latest``
    lookup appears anywhere in this path: the checkout is already pinned
    to the explicit fork ref before these commands run.
    """
    _ = fork_dir  # commands run with cwd=fork_dir; argv stays relative.
    return [
        ["bun", "install"],
        ["bun", "run", "packages/opencode/script/build.ts"],
    ]


def release_asset_name() -> str:
    """Return the required Linux release asset name for Render workers."""
    return REQUIRED_RELEASE_ASSET


def clone_commands(fork_ref: str, dest_dir: str) -> list[list[str]]:
    """Return git commands that pin a fork checkout to an explicit ref.

    Shape: clone the source-only fork, fetch the exact ref, checkout the
    exact ref, then resolve the immutable SHA with ``rev-parse``. Callers
    must resolve that SHA and key all later storage by it.
    """
    ref = validate_fork_ref(fork_ref)
    if not isinstance(dest_dir, str) or not dest_dir.strip():
        raise ValueError("dest_dir must be a non-empty string")
    dest = dest_dir.strip()
    remote = "https://github.com/%s.git" % FORK_REPO
    return [
        ["git", "clone", remote, dest],
        ["git", "-C", dest, "fetch", "origin", ref],
        ["git", "-C", dest, "checkout", ref],
        ["git", "-C", dest, "rev-parse", "HEAD"],
    ]


def start_env_prefix(selection: dict) -> str:
    """Render start-command env prefix carrying an explicit artifact choice."""
    if not isinstance(selection, dict) or selection.get("mode") != EXPERIMENT_MODE:
        return ""
    parts: list[str] = []
    for key, env_name in (
        ("artifact_ref", ARTIFACT_REF_ENV_VAR),
        ("expected_fork_sha", EXPECTED_FORK_SHA_ENV_VAR),
        ("expected_sha256", EXPECTED_SHA256_ENV_VAR),
        ("artifact_id", ARTIFACT_ID_ENV_VAR),
        ("artifact_url", ARTIFACT_URL_ENV_VAR),
    ):
        value = str(selection.get(key) or "").strip()
        if value:
            parts.append("%s=%s" % (env_name, value))
    return " ".join(parts)


def build_command_fragment(selection: dict) -> str:
    """Render build-command fragment that provisions one artifact.

    Empty for baseline mode (the pinned installer path is unchanged).
    Experiment mode appends an explicit, SHA-pinned provisioning step that
    fails closed instead of falling back to the baseline binary.
    """
    if not isinstance(selection, dict) or selection.get("mode") != EXPERIMENT_MODE:
        return ""
    env_parts: list[str] = []
    for key, env_name in (
        ("artifact_ref", ARTIFACT_REF_ENV_VAR),
        ("expected_fork_sha", EXPECTED_FORK_SHA_ENV_VAR),
        ("expected_sha256", EXPECTED_SHA256_ENV_VAR),
        ("artifact_id", ARTIFACT_ID_ENV_VAR),
        ("artifact_url", ARTIFACT_URL_ENV_VAR),
    ):
        value = str(selection.get(key) or "").strip()
        if value:
            env_parts.append('%s="%s"' % (env_name, value))
    prefix = " ".join(env_parts)
    step = "bash automation/install-opencode-artifact.sh"
    return ("%s %s" % (prefix, step)).strip()
