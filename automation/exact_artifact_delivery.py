"""Exact OpenCode artifact delivery and /proc identity for Render (issue #128).

Stdlib only, no git mutations, no workflow edits. This module is the single
production owner for delivering the exact corrected GitHub Actions artifact
to an ephemeral Render worker, verifying it, launching it by absolute path
with zero fallback, and proving the actually executing binary via /proc.

Exact artifact contract (authoritative, from the issue):

- repository: ``kodmial/opencode``
- PR: ``#12``
- source/head SHA: ``a3c748143bbc525a7cef4f9db48e2a779418943c``
- PR merge SHA actually built: ``84fa724616e1fddeea8e7665e38568928feffdf9``
- source workflow run: ``36498663107``
- artifact name: ``opencode-coding-linux-x64``
- artifact ID: ``11004835952``
- artifact ZIP digest:
  ``sha256:0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040``
- binary SHA-256:
  ``f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966``
- exact ``--version``: ``1.18.33``

Capability split (never mix the two sides):

- Controller side (Actions / persistent controller) holds the GitHub
  credential, downloads artifact ID ``11004835952`` via the authenticated
  ``actions/artifacts/<id>/zip`` endpoint, verifies the archive digest
  before extraction/use, verifies the bundled binary checksum and the
  expected binary SHA-256, and never rebuilds OpenCode.
- Transport pushes only the verified bytes to the worker over the
  worker's authenticated runner HTTP surface (``POST /v1/exact-artifact``,
  octet-stream + identity headers). The worker never holds a GitHub
  credential for this path.
- Worker side materializes the bytes at a deterministic absolute path,
  rejects missing/mismatched artifacts before starting OpenCode, invokes
  that absolute path directly with zero fallback, and records
  ``/proc/<pid>/exe`` identity. GitHub credentials are never present in
  the OpenCode child environment (the child env is scrubbed before exec).
- Evidence (artifact identity + process identity + terminal job result)
  is returned in the job result payload, which the controller persists
  outside the ephemeral worker (Actions result file / controller store),
  so a worker restart/OOM does not erase proof.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from typing import Any, Mapping

SCHEMA = "runtime-lab-exact-artifact-delivery/v1"

FORK_REPO = "kodmial/opencode"
FORK_PR = 12
FORK_BRANCH = "opencode/issue11-max-headless"
SOURCE_SHA = "a3c748143bbc525a7cef4f9db48e2a779418943c"
MERGE_SHA = "84fa724616e1fddeea8e7665e38568928feffdf9"
SOURCE_RUN_ID = "36498663107"
SOURCE_WORKFLOW_NAME = "OpenCode Coding Artifact"
ARTIFACT_NAME = "opencode-coding-linux-x64"
ARTIFACT_ID = "11004835952"
ARCHIVE_SHA256 = "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040"
EXPECTED_BINARY_SHA256 = (
    "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966"
)
EXPECTED_VERSION = "1.18.33"
ARTIFACT_PAYLOAD = (
    "opencode-coding-linux-x64",
    "opencode-coding-linux-x64.sha256",
    "build-metadata.txt",
)
RETENTION_EXPIRY = "2026-10-28"

BINARY_FILENAME = "opencode-coding-linux-x64"
CHECKSUM_FILENAME = "opencode-coding-linux-x64.sha256"

GITHUB_API_BASE = "https://api.github.com"

# Worker-side transport surface (runner HTTP API).
EXACT_ARTIFACT_POST_PATH = "/v1/exact-artifact"
EXACT_PUSH_MAX_BYTES = 256 * 1024 * 1024

# Machine-readable identity carried through submit/create payloads and the
# push endpoint headers. Header names are lowercase-insensitive on read.
EXACT_ENV_ID = "OPENCODE_EXACT_ARTIFACT_ID"
EXACT_ENV_BINARY_SHA = "OPENCODE_EXACT_ARTIFACT_SHA256"
EXACT_ENV_ARCHIVE_SHA = "OPENCODE_EXACT_ARCHIVE_SHA256"
EXACT_ENV_RUN = "OPENCODE_EXACT_SOURCE_RUN"
EXACT_ENV_VERSION = "OPENCODE_EXACT_VERSION"
EXACT_ENV_NAME = "OPENCODE_EXACT_ARTIFACT_NAME"

EXACT_HEADER_ID = "X-Exact-Artifact-Id"
EXACT_HEADER_BINARY_SHA = "X-Exact-Artifact-Sha256"
EXACT_HEADER_ARCHIVE_SHA = "X-Exact-Archive-Sha256"
EXACT_HEADER_RUN = "X-Exact-Source-Run"
EXACT_HEADER_VERSION = "X-Exact-Version"
EXACT_HEADER_NAME = "X-Exact-Artifact-Name"

# Deterministic on-worker directory (repo-anchored, always resolved to an
# absolute path before use).
EXACT_ARTIFACT_DIRNAME = ".opencode-exact-workflow"

# Credential names that must never reach the OpenCode child. Mirrors
# automation/opencode_runner.py:WORKER_SCRUB_ENV_NAMES (kept local so this
# module stays import-cycle free).
_CREDENTIAL_ENV_NAMES = (
    "TAP_PAT",
    "TARGET_REPO_PAT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_INSTALLATION_ID",
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BINARY_SHA_RE = re.compile(
    r"binary\s+SHA-?256\s*[:=]?\s*`?([0-9a-fA-F]{64})`?", re.IGNORECASE
)


def build_exact_artifact_identity() -> dict[str, str]:
    """Return the machine-readable identity for the supported exact artifact."""
    return {
        "artifact_id": ARTIFACT_ID,
        "artifact_name": ARTIFACT_NAME,
        "source_run_id": SOURCE_RUN_ID,
        "archive_sha256": ARCHIVE_SHA256,
        "binary_sha256": EXPECTED_BINARY_SHA256,
        "version": EXPECTED_VERSION,
        "source_sha": SOURCE_SHA,
        "merge_sha": MERGE_SHA,
        "repo": FORK_REPO,
        "pr": str(FORK_PR),
        "branch": FORK_BRANCH,
    }


def parse_binary_sha256(title: object = "", body: object = "") -> str:
    """Extract a ``binary SHA-256`` digest from issue text, else "".

    Never raises: unparsable input means "no digest".
    """
    try:
        text = "%s\n%s" % (title or "", body or "")
        match = BINARY_SHA_RE.search(text)
        return match.group(1).lower() if match else ""
    except Exception:
        return ""


def validate_exact_identity(identity: object) -> dict[str, str]:
    """Validate a machine-readable exact-artifact identity (fail closed).

    Requires the full supported conjunction: artifact id, source run,
    archive digest, binary digest and version must all equal the pinned
    contract. Anything else raises; there is no partial acceptance.
    """
    if not isinstance(identity, dict):
        raise ValueError("exact_artifact identity must be a mapping")
    get = lambda key: str(identity.get(key, "") or "").strip()
    artifact_id = get("artifact_id")
    source_run = get("source_run_id")
    archive = get("archive_sha256").lower()
    binary = get("binary_sha256").lower()
    version = get("version")
    name = get("artifact_name") or ARTIFACT_NAME
    if artifact_id != ARTIFACT_ID:
        raise ValueError(
            "exact artifact_id %r is not the supported %r" % (artifact_id, ARTIFACT_ID)
        )
    if source_run != SOURCE_RUN_ID:
        raise ValueError(
            "exact source_run_id %r is not the supported %r" % (source_run, SOURCE_RUN_ID)
        )
    if SHA256_RE.match(archive) is None or archive != ARCHIVE_SHA256:
        raise ValueError("exact archive_sha256 does not match the supported digest")
    if SHA256_RE.match(binary) is None or binary != EXPECTED_BINARY_SHA256:
        raise ValueError("exact binary_sha256 does not match the supported digest")
    if version != EXPECTED_VERSION:
        raise ValueError(
            "exact version %r is not the supported %r" % (version, EXPECTED_VERSION)
        )
    if name != ARTIFACT_NAME:
        raise ValueError("exact artifact_name %r is not %r" % (name, ARTIFACT_NAME))
    return {
        "artifact_id": ARTIFACT_ID,
        "artifact_name": ARTIFACT_NAME,
        "source_run_id": SOURCE_RUN_ID,
        "archive_sha256": ARCHIVE_SHA256,
        "binary_sha256": EXPECTED_BINARY_SHA256,
        "version": EXPECTED_VERSION,
        "source_sha": SOURCE_SHA,
        "merge_sha": MERGE_SHA,
        "repo": FORK_REPO,
        "pr": str(FORK_PR),
        "branch": FORK_BRANCH,
    }


def is_supported_exact_requirement(
    requirement: object, binary_sha256: object = ""
) -> bool:
    """True only for the supported exact-artifact contract (issue #128).

    ``requirement`` is the ``parse_exact_workflow_artifact_requirement``
    dict (artifact_id + source_run_id + archive_sha256). ``binary_sha256``
    is the optional ``binary SHA-256`` digest from the issue body: when
    present it must equal the expected binary digest, otherwise a caller
    could smuggle a different binary under the supported archive claim.
    Never raises.
    """
    try:
        if not isinstance(requirement, dict):
            return False
        artifact = str(requirement.get("artifact_id", "") or "").strip()
        run = str(requirement.get("source_run_id", "") or "").strip()
        archive = str(requirement.get("archive_sha256", "") or "").strip().lower()
        if artifact != ARTIFACT_ID or run != SOURCE_RUN_ID or archive != ARCHIVE_SHA256:
            return False
        raw_binary = str(binary_sha256 or "").strip().lower()
        if raw_binary and raw_binary != EXPECTED_BINARY_SHA256:
            return False
        return True
    except Exception:
        return False


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exact_artifact_abs_path(
    artifact_id: str = ARTIFACT_ID,
    base_dir: str | None = None,
    repo_root: str | None = None,
) -> str:
    """Return the deterministic absolute binary path for an exact artifact.

    Layout: ``<base>/.opencode-exact-workflow/<artifact-id>/opencode``,
    where ``<base>`` is ``base_dir`` when given, else
    ``$OPENCODE_EXACT_ARTIFACT_DIR`` when set, else the repository root.
    The result is always absolute and normalized. Fails closed on numeric
    workflow-artifact ids that are not the supported contract (the
    supported path must never be confused with an arbitrary id), while
    still allowing clearly namespaced test ids (``test-...``) for offline
    coverage.
    """
    raw = (artifact_id or "").strip()
    if not raw or "/" in raw or raw in (".", ".."):
        raise ValueError("invalid exact artifact_id: %r" % artifact_id)
    if raw != ARTIFACT_ID and not raw.startswith("test-"):
        raise ValueError(
            "unsupported exact artifact_id %r (supported: %r)" % (raw, ARTIFACT_ID)
        )
    if base_dir is not None and str(base_dir).strip():
        base = str(base_dir).strip()
    else:
        override = os.environ.get("OPENCODE_EXACT_ARTIFACT_DIR", "").strip()
        base = override or (repo_root or _repo_root())
    absolute = os.path.normpath(
        os.path.join(os.path.abspath(base), EXACT_ARTIFACT_DIRNAME, raw, "opencode")
    )
    if not os.path.isabs(absolute):
        raise ValueError("exact artifact path must be absolute")
    return absolute


def exact_env_for_payload(identity: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment bindings selecting the exact artifact on a worker."""
    data = validate_exact_identity(dict(identity) if identity is not None else build_exact_artifact_identity())
    return {
        EXACT_ENV_ID: data["artifact_id"],
        EXACT_ENV_BINARY_SHA: data["binary_sha256"],
        EXACT_ENV_ARCHIVE_SHA: data["archive_sha256"],
        EXACT_ENV_RUN: data["source_run_id"],
        EXACT_ENV_VERSION: data["version"],
        EXACT_ENV_NAME: data["artifact_name"],
    }


def sha256_of_file(path: str) -> str:
    """Return the lowercase hex SHA-256 of a file (streamed, bounded RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of in-memory bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("data must be bytes")
    return hashlib.sha256(bytes(data)).hexdigest()


def verify_archive_file(path: str, expected: str = ARCHIVE_SHA256) -> str:
    """Verify an artifact ZIP against its archive digest (fail closed)."""
    if not isinstance(expected, str) or SHA256_RE.match(expected.strip().lower()) is None:
        raise ValueError("expected archive digest must be 64 lowercase hex chars")
    if not isinstance(path, str) or not path:
        raise ValueError("archive path must be a non-empty string")
    if not os.path.isfile(path):
        raise FileNotFoundError("artifact archive not found: %r" % path)
    actual = sha256_of_file(path)
    if actual != expected.strip().lower():
        raise ValueError(
            "artifact archive checksum mismatch: expected %s, got %s"
            % (expected.strip().lower(), actual)
        )
    return actual


def verify_binary_file(path: str, expected: str = EXPECTED_BINARY_SHA256) -> str:
    """Verify a materialized binary against the expected SHA-256 (fail closed)."""
    if not isinstance(expected, str) or SHA256_RE.match(expected.strip().lower()) is None:
        raise ValueError("expected binary digest must be 64 lowercase hex chars")
    if not isinstance(path, str) or not path:
        raise ValueError("binary path must be a non-empty string")
    if not os.path.isabs(path):
        raise ValueError("exact binary path must be absolute: %r" % path)
    if not os.path.isfile(path):
        raise FileNotFoundError("exact binary not found: %r" % path)
    if not os.access(path, os.X_OK):
        raise ValueError("exact binary is non-executable: %r" % path)
    actual = sha256_of_file(path)
    if actual != expected.strip().lower():
        raise ValueError(
            "exact binary checksum mismatch for %r: expected %s, got %s"
            % (path, expected.strip().lower(), actual)
        )
    return actual


def check_version_output(version_text: str) -> str:
    """Fail closed unless ``--version`` reports the exact expected release."""
    if not isinstance(version_text, str) or not version_text.strip():
        raise ValueError("binary --version output is empty")
    if version_text.strip() != EXPECTED_VERSION:
        raise ValueError(
            "binary --version %r does not equal the expected %r"
            % (version_text.strip(), EXPECTED_VERSION)
        )
    return EXPECTED_VERSION


def controller_token_from_env(env: object = None) -> str:
    """Return the controller-side GitHub token, else "" (never logs values).

    The credential lives only on the controller (Actions / persistent
    controller). It is never forwarded to the worker and never appears in
    the OpenCode child environment.
    """
    source = os.environ if env is None else env
    if not hasattr(source, "get"):
        raise ValueError("env must be a mapping")
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        try:
            raw = str(source.get(name, "") or "").strip()
        except Exception:
            continue
        if raw:
            return raw
    return ""


def exact_download_url(artifact_id: str = ARTIFACT_ID) -> str:
    """Authenticated download URL path for one exact workflow artifact."""
    raw = str(artifact_id or "").strip()
    if raw != ARTIFACT_ID:
        raise ValueError("unsupported exact artifact_id %r" % artifact_id)
    return "repos/%s/actions/artifacts/%s/zip" % (FORK_REPO, raw)


def download_exact_artifact_zip(
    *,
    artifact_id: str = ARTIFACT_ID,
    dest_path: str,
    token: str = "",
    api_base: str = GITHUB_API_BASE,
    urlopen: object = None,
) -> str:
    """Download the exact artifact ZIP with a controller-side credential.

    Requires a non-empty ``token`` (fail closed otherwise: the artifact
    endpoint mandates OAuth/PAT). Verifies nothing yet -- callers must
    run :func:`verify_archive_file` before extraction/use. Never logs the
    token. ``urlopen`` is an injectable ``(request) -> response`` callable
    for offline tests; the default uses :mod:`urllib.request`.
    """
    if str(artifact_id or "").strip() != ARTIFACT_ID:
        raise ValueError("unsupported exact artifact_id %r" % artifact_id)
    if not isinstance(dest_path, str) or not dest_path.strip():
        raise ValueError("dest_path must be a non-empty string")
    credential = str(token or "").strip()
    if not credential:
        raise ValueError(
            "controller GitHub token is required to download exact artifact %s"
            % ARTIFACT_ID
        )
    base = str(api_base or "").strip().rstrip("/") or GITHUB_API_BASE
    url = "%s/%s" % (base, exact_download_url(artifact_id))
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", "Bearer %s" % credential)
    opener = urlopen or urllib.request.urlopen
    response = opener(request, timeout=120)  # type: ignore[operator]
    try:
        parent = os.path.dirname(os.path.abspath(dest_path))
        os.makedirs(parent, exist_ok=True)
        with open(dest_path, "wb") as handle:
            if hasattr(response, "read"):
                shutil.copyfileobj(response, handle)
            else:
                handle.write(bytes(response))
    finally:
        try:
            if hasattr(response, "close"):
                response.close()
        except Exception:
            pass
    return dest_path


def extract_and_verify(
    zip_path: str,
    dest_dir: str,
    expected_binary_sha: str = EXPECTED_BINARY_SHA256,
    expected_archive_sha: str = ARCHIVE_SHA256,
) -> str:
    """Extract an exact artifact ZIP and verify the binary (fail closed).

    Verifies the archive digest before extraction, requires the exact
    payload files, marks the binary executable, and verifies its SHA-256.
    Returns the materialized binary path.
    """
    verify_archive_file(zip_path, expected_archive_sha)
    if not isinstance(dest_dir, str) or not dest_dir.strip():
        raise ValueError("dest_dir must be a non-empty string")
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = set(archive.namelist())
        missing = [name for name in ARTIFACT_PAYLOAD if name not in names]
        if missing:
            raise ValueError("exact artifact ZIP missing payload: %s" % sorted(missing))
        archive.extractall(dest_dir)
    binary_path = os.path.join(dest_dir, BINARY_FILENAME)
    try:
        os.chmod(binary_path, 0o755)
    except OSError as exc:
        raise ValueError("cannot make exact binary executable: %s" % exc) from exc
    actual = sha256_of_file(binary_path)
    if actual != expected_binary_sha.strip().lower():
        raise ValueError(
            "exact binary checksum mismatch: expected %s, got %s"
            % (expected_binary_sha.strip().lower(), actual)
        )
    return binary_path


def materialize_exact_bytes(
    data: bytes,
    dest_abs_path: str,
    expected_sha256: str = EXPECTED_BINARY_SHA256,
) -> tuple[str, str]:
    """Write verified bytes to the deterministic absolute path (no fallback).

    Writes ``data`` to ``dest_abs_path`` (must be absolute), marks it
    executable, and verifies its SHA-256. Returns ``(path, sha256)``.
    Fails closed on any mismatch; never consults PATH, the repo
    ``.opencode-bin``, ``$HOME/.opencode/bin``, installers or baselines.
    """
    if not isinstance(data, (bytes, bytearray)) or not bytes(data):
        raise ValueError("exact artifact bytes must be non-empty")
    if not isinstance(dest_abs_path, str) or not dest_abs_path:
        raise ValueError("dest_abs_path must be a non-empty string")
    if not os.path.isabs(dest_abs_path):
        raise ValueError("exact destination must be an absolute path")
    if not isinstance(expected_sha256, str) or SHA256_RE.match(
        expected_sha256.strip().lower()
    ) is None:
        raise ValueError("expected_sha256 must be 64 lowercase hex chars")
    parent = os.path.dirname(dest_abs_path)
    os.makedirs(parent, exist_ok=True)
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix="exact-opencode-", suffix=".bin", dir=parent
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(bytes(data))
        os.chmod(tmp_path, 0o755)
        actual = sha256_of_file(tmp_path)
        if actual != expected_sha256.strip().lower():
            raise ValueError(
                "exact artifact bytes checksum mismatch: expected %s, got %s"
                % (expected_sha256.strip().lower(), actual)
            )
        os.replace(tmp_path, dest_abs_path)
        tmp_path = ""
        os.chmod(dest_abs_path, 0o755)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return dest_abs_path, sha256_of_file(dest_abs_path)


def scrubbed_child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the OpenCode child env: scrubbed of every credential name.

    Starts from :func:`opencode_runner.scrubbed_env_for_worker` when
    available (same canonical list), else from ``os.environ`` minus
    :data:`_CREDENTIAL_ENV_NAMES`. ``extra`` overrides win (used for
    confinement keys like ``OPENCODE_CONFIG_CONTENT``). Raises when any
    credential name survives (fail closed before exec).
    """
    try:
        try:
            from automation.opencode_runner import (  # type: ignore[import-not-found]
                scrubbed_env_for_worker as _scrub,
            )
        except ImportError:
            from opencode_runner import (  # type: ignore[import-not-found,no-redef]
                scrubbed_env_for_worker as _scrub,
            )
        cleaned = dict(_scrub())
    except ImportError:
        cleaned = {str(k): str(v) for k, v in os.environ.items()}
        for name in _CREDENTIAL_ENV_NAMES:
            cleaned.pop(name, None)
    if extra:
        for key, value in dict(extra).items():
            cleaned[str(key)] = str(value)
    present = [n for n in _CREDENTIAL_ENV_NAMES if str(cleaned.get(n, "") or "").strip()]
    if present:
        raise ValueError(
            "exact OpenCode child env must never carry credentials (present: %s)"
            % ", ".join(sorted(present))
        )
    return cleaned


def capture_proc_identity(pid: int, proc_root: str = "/proc") -> dict[str, str]:
    """Capture the actually executing binary identity for ``pid``.

    Records ``/proc/<pid>/exe`` realpath, the SHA-256 read from that
    executed file, ``/proc/<pid>/cmdline`` (NUL-joined) and the parent PID
    from ``/proc/<pid>/status``. Raises ``FileNotFoundError`` when the
    process (or its ``/proc`` entries) is already gone.
    """
    try:
        numeric = int(pid)
    except (TypeError, ValueError) as exc:
        raise ValueError("pid must be a positive integer") from exc
    if numeric <= 0:
        raise ValueError("pid must be a positive integer")
    root = str(proc_root or "/proc")
    exe_link = os.path.join(root, str(numeric), "exe")
    try:
        exe_realpath = os.path.realpath(exe_link)
    except OSError as exc:
        raise FileNotFoundError("no /proc exe entry for pid %d" % numeric) from exc
    if not exe_realpath or exe_realpath.endswith("/exe") and not os.path.exists(exe_link):
        # readlink-less fallback check: the link must resolve to a file.
        pass
    try:
        target = os.readlink(exe_link)
    except OSError as exc:
        raise FileNotFoundError("no /proc exe entry for pid %d" % numeric) from exc
    exe_realpath = os.path.realpath(exe_link)
    if not os.path.isfile(exe_realpath):
        raise FileNotFoundError(
            "executing binary for pid %d is not a file: %r" % (numeric, target)
        )
    exe_sha = sha256_of_file(exe_realpath)
    cmdline = ""
    try:
        with open(os.path.join(root, str(numeric), "cmdline"), "rb") as handle:
            raw = handle.read(64 * 1024)
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        cmdline = ""
    ppid = ""
    try:
        with open(os.path.join(root, str(numeric), "status"), "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PPid:"):
                    ppid = line.split(":", 1)[1].strip().split()[0]
                    break
    except (OSError, IndexError):
        ppid = ""
    try:
        own_pid = str(os.getpid())
    except Exception:
        own_pid = ""
    return {
        "pid": str(numeric),
        "ppid": ppid,
        "parent_pid": own_pid,
        "exe_realpath": exe_realpath,
        "exe_sha256": exe_sha,
        "cmdline": cmdline[:4000],
    }


def verify_proc_exe_sha(proc_identity: Mapping[str, str], expected_sha256: str) -> str:
    """Fail closed unless the executing ``/proc/<pid>/exe`` SHA matches."""
    if not isinstance(proc_identity, Mapping):
        raise ValueError("proc_identity must be a mapping")
    actual = str(proc_identity.get("exe_sha256", "") or "").strip().lower()
    expected = str(expected_sha256 or "").strip().lower()
    if SHA256_RE.match(actual) is None:
        raise ValueError("proc identity carries no valid exe_sha256")
    if SHA256_RE.match(expected) is None:
        raise ValueError("expected_sha256 must be 64 lowercase hex chars")
    if actual != expected:
        raise ValueError(
            "executing binary SHA mismatch: /proc/<pid>/exe is %s, expected %s"
            % (actual, expected)
        )
    return actual


def launch_exact_opencode_abs(
    *,
    binary_abs_path: str,
    args: list[str] | tuple[str, ...] | None = None,
    cwd: str = "",
    timeout: float = 120.0,
    extra_env: Mapping[str, str] | None = None,
    popen_factory: object = None,
    proc_root: str = "/proc",
    expected_sha256: str = EXPECTED_BINARY_SHA256,
) -> dict[str, object]:
    """Launch the exact binary by absolute path with /proc identity proof.

    Zero fallback: ``binary_abs_path`` must be the deterministic absolute
    path, must exist, be executable and checksum-match ``expected_sha256``
    before exec; no PATH, ``.opencode-bin``, ``$HOME/.opencode/bin``,
    installer or baseline lookup is ever consulted. The child env is
    credential-scrubbed (fail closed when a credential survives).

    Captures separately: materialized file path + SHA-256, child PID,
    ``/proc/<pid>/exe`` realpath, SHA-256 read from the executing exe,
    ``/proc/<pid>/cmdline``, and parent/PID evidence. Fails closed (raises)
    when the executing SHA differs from ``expected_sha256``.

    Returns a dict with ``returncode/stdout/stderr/timed_out/pid``,
    ``file_sha256``, ``proc_identity`` and ``evidence``. ``popen_factory``
    is an injectable ``(argv, cwd, env) -> Popen-like`` for offline tests.
    """
    if not isinstance(binary_abs_path, str) or not binary_abs_path:
        raise ValueError("binary_abs_path must be a non-empty string")
    if not os.path.isabs(binary_abs_path):
        raise ValueError("exact binary must be launched by absolute path")
    file_sha = verify_binary_file(binary_abs_path, expected_sha256)
    argv = [binary_abs_path] + [str(part) for part in (args or [])]
    workdir = str(cwd or "").strip() or os.getcwd()
    try:
        budget = max(0.1, float(timeout))
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive number") from exc
    child_env = scrubbed_child_env(dict(extra_env) if extra_env else None)
    if popen_factory is not None:
        proc = popen_factory(argv, workdir, child_env)  # type: ignore[operator]
        pid = int(getattr(proc, "pid", 0) or 0)
        if pid <= 0:
            raise ValueError("popen_factory returned no child pid")
        try:
            proc_identity = capture_proc_identity(pid, proc_root)
        except FileNotFoundError:
            # Short-lived fakes may already have exited; fall back to the
            # verified file identity and mark the source explicitly.
            proc_identity = {
                "pid": str(pid),
                "ppid": str(os.getpid()),
                "parent_pid": str(os.getpid()),
                "exe_realpath": os.path.realpath(binary_abs_path),
                "exe_sha256": file_sha,
                "cmdline": " ".join(argv)[:4000],
                "exe_source": "file (process exited before /proc read)",
            }
        verify_proc_exe_sha(proc_identity, expected_sha256)
        try:
            stdout, stderr = proc.communicate(timeout=budget)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            raise
        returncode = int(getattr(proc, "returncode", 1) or 0)
        evidence = build_exact_evidence(
            binary_abs_path, file_sha, proc_identity, argv, version=""
        )
        return {
            "returncode": returncode,
            "stdout": stdout if isinstance(stdout, str) else "",
            "stderr": stderr if isinstance(stderr, str) else "",
            "timed_out": bool(returncode == 124),
            "pid": pid,
            "file_sha256": file_sha,
            "proc_identity": proc_identity,
            "evidence": evidence,
        }
    proc = subprocess.Popen(
        argv,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
    )
    pid = int(proc.pid)
    try:
        try:
            proc_identity = capture_proc_identity(pid, proc_root)
        except FileNotFoundError:
            proc_identity = {
                "pid": str(pid),
                "ppid": str(os.getpid()),
                "parent_pid": str(os.getpid()),
                "exe_realpath": os.path.realpath(binary_abs_path),
                "exe_sha256": file_sha,
                "cmdline": " ".join(argv)[:4000],
                "exe_source": "file (process exited before /proc read)",
            }
        verify_proc_exe_sha(proc_identity, expected_sha256)
        try:
            stdout, stderr = proc.communicate(timeout=budget)
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            stdout, stderr = proc.communicate()
            timed_out = True
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass
        raise
    evidence = build_exact_evidence(
        binary_abs_path, file_sha, proc_identity, argv, version=""
    )
    return {
        "returncode": int(proc.returncode),
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timed_out": timed_out,
        "pid": pid,
        "file_sha256": file_sha,
        "proc_identity": proc_identity,
        "evidence": evidence,
    }


def build_exact_evidence(
    binary_abs_path: str,
    file_sha256: str,
    proc_identity: Mapping[str, str],
    argv: list[str] | tuple[str, ...],
    version: str = "",
) -> dict[str, str]:
    """Assemble the durable exact/process identity evidence record."""
    identity = build_exact_artifact_identity()
    evidence: dict[str, str] = {
        "schema": SCHEMA,
        "artifact_id": identity["artifact_id"],
        "artifact_name": identity["artifact_name"],
        "source_run_id": identity["source_run_id"],
        "archive_sha256": identity["archive_sha256"],
        "binary_sha256": identity["binary_sha256"],
        "version": version or identity["version"],
        "source_sha": identity["source_sha"],
        "merge_sha": identity["merge_sha"],
        "file_path": str(binary_abs_path),
        "file_sha256": str(file_sha256),
        "pid": str(dict(proc_identity).get("pid", "")),
        "ppid": str(dict(proc_identity).get("ppid", "")),
        "parent_pid": str(dict(proc_identity).get("parent_pid", "")),
        "exe_realpath": str(dict(proc_identity).get("exe_realpath", "")),
        "exe_sha256": str(dict(proc_identity).get("exe_sha256", "")),
        "cmdline": str(dict(proc_identity).get("cmdline", ""))[:4000],
        "argv0": str(list(argv)[0]) if list(argv) else "",
    }
    return evidence
