"""OpenCode execution helpers for the ephemeral Render runner (issue #3).

This module is the reusable core for running OpenCode against a checked-out
public repository inside a per-job isolated workspace. It is deliberately
stdlib-only so the Render free-tier build and the minimal CI image both work.

Known-good OpenCode CLI provisioning (from NanoDictate, via
.github/workflows/opencode.yml, excluding extra integrations and
release-specific behavior):

    curl -fsSL https://opencode.ai/install | bash -s -- --version <pinned>

with bounded retries (3 attempts, backoff), followed by
``test -x "$HOME/.opencode/bin/opencode"``. The explicit ``--version``
pin skips the installer's unauthenticated api.github.com
latest-version lookup, which fails closed with "Failed to fetch version
information" under Render shared-egress rate limiting (run 36421205678).
The shell entrypoint ``automation/install-opencode.sh`` encodes exactly
that pattern for the Render build step; ``ensure_opencode_cli`` below
encodes the same pattern for lazy runtime provisioning.

Non-interactive OpenCode invocation (same order as the known-good
workflow step):

    opencode run --auto --model "$OPENCODE_MODEL" "$PROMPT"

No branch/push/PR commands are ever issued from this module or from the
runner: only ``git clone``, ``git fetch``, ``git checkout``,
``git rev-parse``, ``git status`` and ``git diff`` are used for read-only
checkout and deterministic change detection. OpenCode itself is confined
with ``OPENCODE_CONFIG_CONTENT`` that denies ``git *`` writes while
allowing read-only inspection (status/diff/log/show/rev-parse/ls-files/
grep/blame/branch/remote), mirroring the issue-mode permissions in
``opencode.yml``.

Secrets: provider/model credentials stay entirely environment-driven. This
module never reads ``GITHUB_TOKEN``, ``GH_TOKEN`` or ``OPENCODE_API_KEY``
for execution and never logs secret values; ``sanitize_output`` redacts
any secret-shaped values that might appear in captured command output.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import urllib.parse

try:  # pragma: no cover - import path depends on entrypoint
    from automation.render_lifecycle import (
        ALLOWED_WORKER_REGIONS,
        FALLBACK_MODEL,
        PREFERRED_MODEL,
    )
except ImportError:  # pytest inserts automation/ on sys.path
    from render_lifecycle import (  # type: ignore[no-redef]
        ALLOWED_WORKER_REGIONS,
        FALLBACK_MODEL,
        PREFERRED_MODEL,
    )

# ---------------------------------------------------------------------------
# OpenCode CLI provisioning (known-good NanoDictate pattern).
# ---------------------------------------------------------------------------

OPENCODE_INSTALL_URL = "https://opencode.ai/install"
OPENCODE_INSTALL_COMMAND = "curl -fsSL https://opencode.ai/install | bash"
OPENCODE_BIN_NAME = "opencode"
OPENCODE_HOME_SUBPATH = os.path.join(".opencode", "bin", "opencode")
OPENCODE_INSTALL_MAX_ATTEMPTS = 3
# Deterministic deploy-artifact path for the OpenCode binary (issue #52).
#
# Render build-time $HOME is not guaranteed to equal runtime $HOME, so the
# worker must never rely solely on ``$HOME/.opencode/bin/opencode``. The
# build step (automation/install-opencode.sh) copies the pinned binary into
# this repo-relative directory so it ships inside the deploy artifact; the
# runner resolves it relative to this source file and the process working
# directory before falling back to PATH/HOME.
OPENCODE_DEPLOY_DIRNAME = ".opencode-bin"
OPENCODE_DEPLOY_BIN_SUBPATH = os.path.join(OPENCODE_DEPLOY_DIRNAME, "opencode")
# Explicit binary override (highest precedence). Honored verbatim.
OPENCODE_BIN_ENV_VARS = ("RUNNER_OPENCODE_BIN", "OPENCODE_BIN")
# When false ("0"/"false"/"no"), lazy network installation during a job is
# disabled: a missing binary fails fast instead of running curl|bash.
# Production workers set RUNNER_ALLOW_RUNTIME_INSTALL=0 via the Render start
# command so jobs never pay installer memory/CPU or depend on network.
RUNTIME_INSTALL_ENV_VARS = (
    "RUNNER_ALLOW_RUNTIME_INSTALL",
    "OPENCODE_ALLOW_RUNTIME_INSTALL",
)

# Pinned OpenCode release used for deterministic worker provisioning.
#
# The upstream installer resolves "latest" via an unauthenticated
# ``api.github.com/repos/.../releases/latest`` call and exits 1 with
# "Failed to fetch version information" when that lookup fails (Render
# shared-egress rate limiting or any transient network error). Passing an
# explicit ``--version`` skips that discovery call entirely (verified
# against the live installer script 2026-09-28: the versioned branch only
# HEAD-checks the release tag page, then downloads the pinned asset).
# Pin verified live 2026-09-28: tag v1.18.33 exists with linux
# x64/arm64 tarballs (opencode-linux-x64.tar.gz et al.).
OPENCODE_PINNED_VERSION = "1.18.33"
# Environment override for the pinned version (build-time and lazy
# runtime provisioning both honor it).
OPENCODE_VERSION_ENV_VAR = "OPENCODE_VERSION"
# Seconds to sleep between lazy-provisioning attempts (attempts 1->2, 2->3).
OPENCODE_INSTALL_RETRY_DELAYS = (5.0, 10.0)

# Confine OpenCode to read-only git inspection. Mirrors the issue-mode
# OPENCODE_CONFIG_CONTENT in .github/workflows/opencode.yml: bash is
# allowed, `git *` writes are denied, read-only git inspection is allowed.
# The workflow owns all Git state; the runner never pushes or opens PRs.
OPENCODE_CONFIG_CONTENT = (
    '{"permission":{"bash":{"*":"allow","git *":"deny",'
    '"git status":"allow","git status *":"allow",'
    '"git diff":"allow","git diff *":"allow",'
    '"git log":"allow","git log *":"allow",'
    '"git show":"allow","git show *":"allow",'
    '"git rev-parse *":"allow","git ls-files":"allow",'
    '"git ls-files *":"allow","git grep *":"allow",'
    '"git blame *":"allow","git branch --show-current":"allow",'
    '"git remote -v":"allow"}}}'
)

# Per-job checkout lives in this subdirectory of the isolated workspace so
# workspace-level files (task.txt) never leak into the reported repo diff.
CHECKOUT_SUBDIR = "repo"

# Deterministic result limits: large enough for real tasks, small enough
# that a terminal result always fits in process memory and in the JSON
# body GitHub collects before the ephemeral service is deleted.
MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_CHARS = 8000
MAX_SUMMARY_CHARS = 4000


def find_opencode_binary() -> str | None:
    """Return the OpenCode binary path, or None when not installed.

    Precedence: explicit ``RUNNER_OPENCODE_BIN``/``OPENCODE_BIN`` override,
    repo-relative deterministic deploy artifact (``.opencode-bin/opencode``
    next to this source tree or under the process working directory),
    ``PATH``, then ``$HOME/.opencode/bin/opencode``. The deploy-artifact
    check is what makes build-time provisioning visible at runtime even
    when build-time $HOME differs from runtime $HOME.
    """
    for env_var in OPENCODE_BIN_ENV_VARS:
        raw = os.environ.get(env_var, "")
        if raw and str(raw).strip():
            candidate = str(raw).strip()
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            # An explicit override pointing at a missing binary is a
            # configuration error, not a discovery miss; keep searching so
            # diagnostics can report every candidate deterministically.
    for candidate in deploy_binary_candidates():
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    on_path = shutil.which(OPENCODE_BIN_NAME)
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    candidate = os.path.join(home, OPENCODE_HOME_SUBPATH)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def deploy_binary_candidates() -> list[str]:
    """Repo-relative deterministic binary candidates (deploy artifact)."""
    candidates: list[str] = []
    try:
        source_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(source_dir)
        candidates.append(os.path.join(repo_root, OPENCODE_DEPLOY_BIN_SUBPATH))
    except Exception:
        pass
    try:
        candidates.append(
            os.path.join(os.path.abspath(os.getcwd()), OPENCODE_DEPLOY_BIN_SUBPATH)
        )
    except Exception:
        pass
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def opencode_runtime_install_allowed(raw: object = None) -> bool:
    """True unless runtime network installation was explicitly disabled."""
    if raw is None:
        for env_var in RUNTIME_INSTALL_ENV_VARS:
            value = os.environ.get(env_var)
            if value is not None and str(value).strip() != "":
                raw = value
                break
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled")


def resolve_opencode_bin_override() -> str | None:
    """Return the explicit binary override, or None when not configured."""
    for env_var in OPENCODE_BIN_ENV_VARS:
        raw = os.environ.get(env_var, "")
        if raw and str(raw).strip():
            return str(raw).strip()
    return None


def probe_opencode_readiness(
    binary: str | None = None, timeout: float = 20.0
) -> tuple[bool, str, str]:
    """Check that the expected OpenCode binary exists and reports a version.

    Returns (ready, resolved_path, version_or_detail). Never raises: any
    failure is reported as not-ready with a safe detail string (no secrets).
    """
    resolved = (binary or "").strip() if binary else None
    if not resolved:
        resolved = find_opencode_binary() or ""
    if not resolved:
        return False, "", "opencode binary not found in deploy artifact, PATH, or HOME"
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        return False, resolved, "opencode binary at %r is absent or non-executable" % resolved
    try:
        import subprocess as _subprocess

        completed = _subprocess.run(
            [resolved, "--version"],
            timeout=max(1.0, float(timeout)),
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            text=True,
        )
        output = ((completed.stdout or "") + " " + (completed.stderr or "")).strip()
        version = output.split()[0] if output else ""
        if completed.returncode == 0 and version:
            return True, resolved, version[:64]
        detail = sanitize_output(output or ("exit %d" % completed.returncode))[:200]
        return False, resolved, "opencode --version failed: %s" % detail
    except Exception as exc:
        return False, resolved, "opencode --version probe failed: %s" % str(exc)[:200]


def opencode_install_shell_snippet(version: str | None = None) -> str:
    """Return the version-pinned install snippet (for docs/tests).

    The pinned form skips the installer's unauthenticated
    ``api.github.com`` latest-version lookup, which is the failure mode
    seen live (run 36421205678: "Failed to fetch version information").
    Pass ``version=""`` explicitly for the unpinned base command.
    """
    if version == "":
        return OPENCODE_INSTALL_COMMAND
    return build_opencode_install_command(
        version if version is not None else resolve_opencode_version()
    )


def resolve_opencode_version(raw: str | None = None) -> str:
    """Resolve the OpenCode release version to provision.

    Defaults to ``$OPENCODE_VERSION`` when set, else the pinned release.
    Accepts an optional leading ``v`` and surrounding whitespace; rejects
    anything that is not a numeric ``X.Y.Z`` release.
    """
    if raw is None:
        raw = os.environ.get(OPENCODE_VERSION_ENV_VAR, "")
    text = str(raw or "").strip()
    if not text:
        text = OPENCODE_PINNED_VERSION
    text = text.strip()
    if text.startswith(("v", "V")):
        text = text[1:].strip()
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("invalid OpenCode version: %r" % raw)
    return text


def build_opencode_install_command(version: str | None = None) -> str:
    """Build the installer invocation, pinning ``--version``.

    ``version=None`` resolves via :func:`resolve_opencode_version`
    (env override else pinned release); ``version=""`` returns the
    unpinned base command for callers that explicitly want discovery.
    """
    if version == "":
        return OPENCODE_INSTALL_COMMAND
    resolved = (
        resolve_opencode_version(version)
        if version is not None
        else resolve_opencode_version()
    )
    return "%s -s -- --version %s" % (OPENCODE_INSTALL_COMMAND, resolved)


def build_opencode_command(
    model: str, task_text: str, opencode_bin: str = OPENCODE_BIN_NAME
) -> list[str]:
    """Build the non-interactive OpenCode command.

    Order matches the known-good workflow step:
    ``opencode run --auto --model <model> <task>``.
    """
    if model not in (PREFERRED_MODEL, FALLBACK_MODEL):
        raise ValueError("unknown model: %r" % model)
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("task_text must be a non-empty string")
    if not opencode_bin or not str(opencode_bin).strip():
        raise ValueError("opencode_bin must be a non-empty string")
    return [str(opencode_bin), "run", "--auto", "--model", model, task_text.strip()]


def assert_public_clone_url(repository_url: str) -> str:
    """Validate a clone URL carries no credentials (public https only)."""
    if not isinstance(repository_url, str) or not repository_url.strip():
        raise ValueError("repository_url must be a non-empty string")
    url = repository_url.strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("repository_url must use https, got %r" % url)
    if not parsed.hostname:
        raise ValueError("repository_url has no host: %r" % url)
    if parsed.username or parsed.password or "@" in (parsed.netloc.split(":")[0]):
        raise ValueError("repository_url must not embed credentials")
    return url


def build_clone_command(repository_url: str, dest_dir: str) -> list[str]:
    """Build ``git clone <public-https-url> <dest>`` (no credentials)."""
    url = assert_public_clone_url(repository_url)
    if not dest_dir or not str(dest_dir).strip():
        raise ValueError("dest_dir must be a non-empty string")
    return ["git", "clone", url, str(dest_dir)]


def build_fetch_command(ref: str) -> list[str]:
    """Build ``git fetch origin <ref>`` for pinning the base ref."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty string")
    return ["git", "fetch", "origin", ref.strip()]


def build_checkout_command(ref: str) -> list[str]:
    """Build ``git checkout <ref>`` for the exact base ref/SHA."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty string")
    return ["git", "checkout", ref.strip()]


def build_rev_parse_command() -> list[str]:
    """Build ``git rev-parse HEAD`` to record the checked-out SHA."""
    return ["git", "rev-parse", "HEAD"]


def build_status_command() -> list[str]:
    """Build ``git status --porcelain`` for deterministic change detection."""
    return ["git", "status", "--porcelain"]


# ---------------------------------------------------------------------------
# Model fallback detection (same worker/process, no second Render service).
# ---------------------------------------------------------------------------

_AVAILABILITY_TERMS = (
    "unavailable",
    "not found",
    "not_found",
    "not-found",
    "unknown",
    "no such",
    "does not exist",
    "not available",
    "not supported",
    "timed out",
    "timeout",
)
_MODEL_TERMS = ("model", "provider", "opencode")


def is_model_unavailable_error(text: str | None) -> bool:
    """Detect a model/provider availability failure.

    Mirrors the Actions-side fallback grep (``model|unavailable|not found``)
    but requires a model/provider term alongside generic availability
    wording, except that a bare ``unavailable`` is already a strong signal.
    Case-insensitive; never raises on unexpected input.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    lowered = text.lower()
    if "unavailable" in lowered:
        return True
    has_model_term = any(term in lowered for term in _MODEL_TERMS)
    has_avail_term = any(term in lowered for term in _AVAILABILITY_TERMS)
    if has_model_term and has_avail_term:
        return True
    # Provider 4xx/5xx surfaced without prose (e.g. "provider 404").
    if "provider" in lowered and any(
        code in lowered for code in ("404", "429", "500", "502", "503")
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Secret redaction (never log Render/OpenCode/GitHub secrets).
# ---------------------------------------------------------------------------

_SECRET_KEY_SUBSTRINGS = ("TOKEN", "KEY", "SECRET", "PASSWORD")
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]+"),
    re.compile(r"sk-[A-Za-z0-9\-_]+"),
)


def sanitize_output(text: str) -> str:
    """Redact secret values and secret-shaped tokens from captured output."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(marker in upper for marker in _SECRET_KEY_SUBSTRINGS):
            continue
        if not isinstance(value, str) or len(value) < 4:
            continue
        if value in redacted:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Bound captured output so terminal results stay JSON-safe."""
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


# ---------------------------------------------------------------------------
# Deterministic change detection (additions, edits, deletions).
# ---------------------------------------------------------------------------


def parse_git_status_porcelain(output: str) -> list[dict[str, str]]:
    """Parse ``git status --porcelain`` into structured entries.

    Returns a list of ``{"x":.., "y":.., "path":.., "orig":..}`` where
    ``orig`` is set only for renames (``old -> new``). Handles quoted
    paths conservatively by stripping surrounding quotes.
    """
    entries: list[dict[str, str]] = []
    if not isinstance(output, str) or not output.strip():
        return entries
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if len(line) < 4:
            continue
        x, y = line[0], line[1]
        rest = line[3:].strip()
        if not rest:
            continue
        orig = ""
        path = rest
        if " -> " in rest:
            orig, path = [part.strip() for part in rest.split(" -> ", 1)]
        for key in ("path", "orig"):
            value = path if key == "path" else orig
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                if key == "path":
                    path = value[1:-1]
                else:
                    orig = value[1:-1]
        entries.append({"x": x, "y": y, "path": path, "orig": orig})
    return entries


def _classify_entry(entry: dict[str, str]) -> str:
    x, y = entry.get("x", " "), entry.get("y", " ")
    if x == "?" and y == "?":
        return "added"
    if x == "R" or y == "R":
        return "renamed"
    if "D" in (x, y):
        return "deleted"
    if "A" in (x, y):
        return "added"
    return "modified"


def _read_repo_file(repo_dir: str, rel_path: str) -> bytes | None:
    """Read a repo-relative file; return None when absent (deleted)."""
    if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
        raise ValueError("unsafe repo path: %r" % rel_path)
    full = os.path.join(repo_dir, rel_path)
    try:
        with open(full, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except IsADirectoryError as exc:
        raise ValueError("repo path is a directory: %r" % rel_path) from exc
    except OSError as exc:
        raise ValueError("cannot read repo file %r: %s" % (rel_path, exc)) from exc


def build_changes(status_output: str, repo_dir: str) -> list[dict[str, str]]:
    """Build a deterministic, self-contained change list.

    Each entry is ``{"path":.., "change_type": added|modified|deleted,
    "content_base64":..}`` with ``content_base64`` present for every
    added/modified file (current bytes, base64) and absent for deletions.
    Entries are sorted by path. Renames surface as a delete(old) plus an
    add(new). Size/count limits raise ``ValueError`` deterministically
    instead of producing a truncated patch.
    """
    if not os.path.isdir(repo_dir):
        raise ValueError("repo_dir does not exist: %r" % repo_dir)
    entries = parse_git_status_porcelain(status_output)
    changes: list[dict[str, str]] = []
    total_bytes = 0
    # Expand renames into delete+add before counting against MAX_FILES.
    expanded: list[tuple[str, str]] = []  # (change_type, path)
    for entry in entries:
        kind = _classify_entry(entry)
        if kind == "renamed":
            if entry["orig"]:
                expanded.append(("deleted", entry["orig"]))
            expanded.append(("added", entry["path"]))
        elif kind == "added":
            expanded.append(("added", entry["path"]))
        elif kind == "deleted":
            expanded.append(("deleted", entry["path"]))
        else:
            expanded.append(("modified", entry["path"]))
    # Deterministic order; de-duplicate exact duplicates (defensive).
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for change_type, path in sorted(expanded, key=lambda item: item[1]):
        key = (change_type, path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    if len(ordered) > MAX_FILES:
        raise ValueError(
            "too many changed files (%d > %d)" % (len(ordered), MAX_FILES)
        )
    for change_type, path in ordered:
        if change_type == "deleted":
            changes.append({"path": path, "change_type": "deleted"})
            continue
        content = _read_repo_file(repo_dir, path)
        if content is None:
            # Vanished between status and read: report as deleted.
            changes.append({"path": path, "change_type": "deleted"})
            continue
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(
                "file %r too large (%d > %d bytes)" % (path, len(content), MAX_FILE_BYTES)
            )
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError(
                "total changed bytes exceed %d bytes" % MAX_TOTAL_BYTES
            )
        changes.append(
            {
                "path": path,
                "change_type": change_type,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
    return changes


def decode_change_content(change: dict[str, str]) -> bytes | None:
    """Decode one change entry's content (None for deletions)."""
    if change.get("change_type") == "deleted":
        return None
    encoded = change.get("content_base64", "")
    if not encoded:
        return b""
    return base64.b64decode(encoded.encode("ascii"))


def summarize_changes(changes: list[dict[str, str]]) -> str:
    """One-line deterministic summary (counts + first paths)."""
    if not changes:
        return "no changes"
    added = sum(1 for item in changes if item.get("change_type") == "added")
    modified = sum(1 for item in changes if item.get("change_type") == "modified")
    deleted = sum(1 for item in changes if item.get("change_type") == "deleted")
    paths = ", ".join(item.get("path", "") for item in changes[:5])
    summary = "files=%d added=%d modified=%d deleted=%d" % (
        len(changes),
        added,
        modified,
        deleted,
    )
    if paths:
        summary += " [%s]" % paths
        if len(changes) > 5:
            summary += " (+%d more)" % (len(changes) - 5)
    return summary


def default_opencode_env_overrides() -> dict[str, str]:
    """Safe default env overrides for the OpenCode subprocess.

    Only confinement settings are provided here; provider/model
    credentials stay inherited from the process environment (Render env
    vars) and are never set or logged by this module.
    """
    return {
        "OPENCODE_CONFIG_CONTENT": OPENCODE_CONFIG_CONTENT,
        "GIT_TERMINAL_PROMPT": "0",
    }
