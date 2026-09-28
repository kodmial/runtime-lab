"""GitHub App authentication and API write-back for the Render controller.

Issue #11 (P0): move all GitHub-side task orchestration and write-back
needed for OpenCode execution into the Render controller using GitHub App
installation authentication, removing GitHub Actions from the
execution/write-back path.

Design notes:

- Short-lived installation access tokens only. No long-lived PAT is read
  or required anywhere in this module (it never consults ``GITHUB_TOKEN``
  or ``GH_TOKEN``). App ID, private key, installation ID, target
  repository and the webhook secret all come from Render environment
  secrets and are never logged.
- Requested permissions (least privilege)::

      contents: write        (issue branches, commits, pushes)
      issues: write          (inspect issues, apply/remove automation labels)
      pull-requests: write   (create/update PRs, avoid duplicates)
      metadata: read         (mandatory for every GitHub App)
      actions: read          (optional, read-only: only to prove no OpenCode
                              Actions job ran during final verification)

  The App never requests ``actions: write``: PR pushes made with an
  installation token trigger CI natively, so no explicit workflow dispatch
  is needed (see :meth:`AppWritebackClient.dispatch_ci`).
- Reuses the write-back orchestration factored in issue #5
  (``automation/result_materialize.py``): this module implements the same
  :class:`WritebackClient` interface with installation-token REST calls,
  so :func:`materialize_result` behaves identically whether it runs from
  the temporary Actions harness or from Render.
- Token expiry/refresh is handled by :class:`InstallationTokenProvider`
  (cached token with clock-skew refresh plus one retry after a 401), and
  every authentication failure raises a redacted error that never embeds
  key/token/JWT material. Worker cleanup always runs independently of
  write-back: the controller deletes the ephemeral worker inside
  ``execute_issue_attempt`` before this module is ever called.

Stdlib only at runtime (like the rest of automation/). RS256 signing of
the App JWT needs an RSA implementation: :func:`default_rs256_signer`
prefers the ``cryptography`` package when installed, otherwise falls back
to the ``openssl`` CLI. Tests inject a fake signer and a fake HTTP layer,
so neither is required for unit tests.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Constants: API base, env names, permissions.
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

ENV_APP_ID = "GITHUB_APP_ID"
ENV_APP_PRIVATE_KEY = "GITHUB_APP_PRIVATE_KEY"
ENV_APP_INSTALLATION_ID = "GITHUB_APP_INSTALLATION_ID"
ENV_REPOSITORY = "GITHUB_REPOSITORY"
ENV_API_BASE = "GITHUB_API_BASE"

# Least-privilege permission set for the GitHub App (see module docstring).
# ``actions: read`` is optional and read-only (final verification only).
REQUIRED_APP_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "metadata": "read",
}
OPTIONAL_APP_PERMISSIONS: dict[str, str] = {
    "actions": "read",
}

# App JWTs live at most 10 minutes (GitHub-enforced). Tokens returned by
# the installation exchange live ~60 minutes; the provider refreshes early.
APP_JWT_EXPIRY_SECONDS = 600
APP_JWT_CLOCK_SKEW_SECONDS = 60
TOKEN_REFRESH_SKEW_SECONDS = 120

# Long-lived PAT env names that the final architecture must NOT use.
# This module never reads them; the constant exists so tests can assert
# the final path is PAT-free.
FORBIDDEN_PAT_ENV_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")


# ---------------------------------------------------------------------------
# Errors (never carry secret material).
# ---------------------------------------------------------------------------

class GitHubAppError(RuntimeError):
    """Base error for GitHub App authentication and API failures."""


class GitHubAuthError(GitHubAppError):
    """Authentication failure (bad config, JWT, or token exchange)."""


class GitHubApiError(GitHubAppError):
    """GitHub REST call failure; carries the HTTP status, never secrets."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Secret hygiene.
# ---------------------------------------------------------------------------

_TOKEN_SHAPED = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"),
    re.compile(r"gh[pousr][-_][A-Za-z0-9\-_]+"),
    re.compile(r"ghs[-_][A-Za-z0-9\-_]+"),
    re.compile(r"ghu[-_][A-Za-z0-9\-_]+"),
    re.compile(r"github_pat_[A-Za-z0-9\-_]+"),
    re.compile(r"sk-[A-Za-z0-9\-_]+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)

_SECRET_ENV_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def redact_secrets(text: str) -> str:
    """Redact secret values and secret-shaped tokens from an error string."""
    if not isinstance(text, str) or not text:
        return ""
    redacted = text
    # PEM blocks first (DOTALL so escaped and real newlines both match).
    redacted = re.sub(
        r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----",
        "[redacted-pem-key]", redacted, flags=re.DOTALL)
    for pattern in _TOKEN_SHAPED:
        redacted = pattern.sub("[redacted]", redacted)
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(marker in upper for marker in _SECRET_ENV_MARKERS):
            continue
        if not isinstance(value, str) or len(value) < 4:
            continue
        if value in redacted:
            redacted = redacted.replace(value, "[redacted]")
    # Defensive: long base64url/JWT-looking blobs that could be a JWT or
    # token are collapsed even when they match no known prefix.
    redacted = re.sub(r"eyJ[A-Za-z0-9_\-]{16,}", "[redacted-jwt]", redacted)
    return redacted[:2000]


def _auth_failure(message: str) -> GitHubAuthError:
    return GitHubAuthError(redact_secrets(message))


def _api_failure(message: str, status: int | None = None) -> GitHubApiError:
    return GitHubApiError(redact_secrets(message), status=status)


# ---------------------------------------------------------------------------
# App configuration (Render environment secrets only).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GitHubAppConfig:
    """GitHub App credentials for the Render controller.

    All fields come from Render environment secrets; nothing is ever read
    from the repository. A long-lived PAT is never part of this config.
    """

    app_id: str
    private_key_pem: str
    installation_id: str
    repository: str = ""  # "owner/repo"; empty means "all installation repos"
    api_base: str = GITHUB_API_BASE

    def __post_init__(self) -> None:
        if not self.app_id.strip():
            raise ValueError("GitHub App ID must not be empty")
        if "PRIVATE KEY" not in self.private_key_pem:
            raise ValueError("GitHub App private key must be a PEM private key")
        if not self.installation_id.strip():
            raise ValueError("GitHub App installation ID must not be empty")


def normalize_private_key(raw: str) -> str:
    """Normalize a PEM private key from an environment secret.

    Render secrets often store the PEM with literal ``\\n`` escapes; those
    are converted back to real newlines. Leading/trailing whitespace is
    stripped. The key material itself is never logged.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("GitHub App private key must not be empty")
    text = raw.strip()
    if "\\n" in text and "-----BEGIN" in text:
        text = text.replace("\\n", "\n")
    # Strip surrounding quotes sometimes added by secret managers.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip().replace("\\n", "\n")
    if "PRIVATE KEY" not in text:
        raise ValueError("GitHub App private key must be a PEM private key")
    return text


def split_repository(full: str) -> tuple[str, str]:
    """Split ``owner/repo`` into ``(owner, repo)`` (fail closed)."""
    if not isinstance(full, str) or "/" not in full:
        raise ValueError("repository must look like 'owner/repo', got %r" % (full,))
    owner, _, repo = full.strip().partition("/")
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo or "/" in repo:
        raise ValueError("repository must look like 'owner/repo', got %r" % (full,))
    return owner, repo


def load_app_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> GitHubAppConfig:
    """Load the App config from Render environment secrets.

    Reads only ``GITHUB_APP_ID``, ``GITHUB_APP_PRIVATE_KEY``,
    ``GITHUB_APP_INSTALLATION_ID``, ``GITHUB_REPOSITORY`` and the optional
    ``GITHUB_API_BASE`` override. Never reads ``GITHUB_TOKEN``/``GH_TOKEN``
    and never logs secret values.
    """
    env = environ if environ is not None else os.environ
    app_id = str(env.get(ENV_APP_ID, "") or "").strip()
    raw_key = str(env.get(ENV_APP_PRIVATE_KEY, "") or "")
    installation_id = str(env.get(ENV_APP_INSTALLATION_ID, "") or "").strip()
    repository = str(env.get(ENV_REPOSITORY, "") or "").strip()
    api_base = str(env.get(ENV_API_BASE, "") or "").strip() or GITHUB_API_BASE
    if not app_id:
        raise _auth_failure("GitHub App authentication is not configured "
                            "(missing %s)" % ENV_APP_ID)
    if not raw_key or not raw_key.strip():
        raise _auth_failure("GitHub App authentication is not configured "
                            "(missing %s)" % ENV_APP_PRIVATE_KEY)
    if not installation_id:
        raise _auth_failure("GitHub App authentication is not configured "
                            "(missing %s)" % ENV_APP_INSTALLATION_ID)
    try:
        private_key_pem = normalize_private_key(raw_key)
    except ValueError as exc:
        raise _auth_failure("GitHub App private key is invalid: %s" % exc) from None
    if repository:
        split_repository(repository)  # validate eagerly
    return GitHubAppConfig(
        app_id=app_id,
        private_key_pem=private_key_pem,
        installation_id=installation_id,
        repository=repository,
        api_base=api_base.rstrip("/"),
    )


def is_app_configured(environ: Mapping[str, str] | None = None) -> bool:
    """True when the three mandatory App secrets are all present."""
    env = environ if environ is not None else os.environ
    return bool(
        str(env.get(ENV_APP_ID, "") or "").strip()
        and str(env.get(ENV_APP_PRIVATE_KEY, "") or "").strip()
        and str(env.get(ENV_APP_INSTALLATION_ID, "") or "").strip()
    )


# ---------------------------------------------------------------------------
# App JWT (RS256): header/payload are stdlib; signing is injectable.
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(raw)


def default_rs256_signer(private_key_pem: str) -> Callable[[bytes], bytes]:
    """Build an RS256 signer for App JWTs (never logs the key).

    Prefers the ``cryptography`` package when installed, otherwise falls
    back to the ``openssl`` CLI (``openssl dgst -sha256 -sign``). Raises
    :class:`GitHubAuthError` (redacted) when neither backend is available.
    """
    normalized = normalize_private_key(private_key_pem)

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_private_key(
            normalized.encode("utf-8"), password=None
        )

        def _sign_cryptography(signing_input: bytes) -> bytes:
            return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

        return _sign_cryptography
    except ImportError:
        pass
    except Exception as exc:
        raise _auth_failure("could not load GitHub App private key: %s"
                            % type(exc).__name__) from None

    if _openssl_available():
        def _sign_openssl(signing_input: bytes) -> bytes:
            tmp_key = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".pem", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(normalized)
                    tmp_key = handle.name
                proc = subprocess.run(
                    ["openssl", "dgst", "-sha256", "-sign", tmp_key],
                    input=signing_input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except (OSError, FileNotFoundError) as exc:
                raise _auth_failure("openssl signing failed: %s"
                                    % type(exc).__name__) from None
            finally:
                if tmp_key:
                    try:
                        os.unlink(tmp_key)
                    except OSError:
                        pass
            if proc.returncode != 0:
                raise _auth_failure("openssl signing failed with status %d"
                                    % proc.returncode)
            return bytes(proc.stdout)

        return _sign_openssl
    raise _auth_failure(
        "no RS256 backend available for GitHub App JWTs "
        "(install 'cryptography' or provide openssl)")


def _openssl_available() -> bool:
    try:
        proc = subprocess.run(
            ["openssl", "version"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
    except (OSError, FileNotFoundError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def build_app_jwt(
    *,
    app_id: str,
    private_key_pem: str,
    now: float | None = None,
    expires_in_seconds: int = APP_JWT_EXPIRY_SECONDS,
    signer: Callable[[bytes], bytes] | None = None,
) -> str:
    """Build a short-lived GitHub App JWT (RS256, max 10 minutes).

    ``signer`` maps the ``header.payload`` bytes to the raw RSA signature;
    tests inject a fake (e.g. HMAC or constant) signer so no RSA key is
    needed. The default signer uses ``cryptography`` or ``openssl``.
    Failures raise redacted :class:`GitHubAuthError` (no key/JWT leakage).
    """
    if not str(app_id or "").strip():
        raise _auth_failure("GitHub App ID must not be empty")
    try:
        normalized = normalize_private_key(private_key_pem)
    except ValueError as exc:
        raise _auth_failure("GitHub App private key is invalid: %s" % exc) from None
    if expires_in_seconds <= 0 or expires_in_seconds > APP_JWT_EXPIRY_SECONDS:
        raise ValueError("expires_in_seconds must be within (0, %d]"
                         % APP_JWT_EXPIRY_SECONDS)
    moment = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": moment - APP_JWT_CLOCK_SKEW_SECONDS,
        "exp": moment + int(expires_in_seconds),
        "iss": str(app_id).strip(),
    }
    signing_input = ("%s.%s" % (_b64url_json(header), _b64url_json(payload))).encode("ascii")
    sign = signer or default_rs256_signer(normalized)
    try:
        signature = sign(signing_input)
    except GitHubAppError:
        raise
    except Exception as exc:
        raise _auth_failure("could not sign GitHub App JWT: %s"
                            % type(exc).__name__) from None
    if not isinstance(signature, (bytes, bytearray)) or not signature:
        raise _auth_failure("GitHub App JWT signer returned no signature")
    return "%s.%s" % (signing_input.decode("ascii"), _b64url(bytes(signature)))


def decode_jwt_payload_unsigned(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying (tests/diagnostics only)."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("invalid JWT payload: %s" % exc) from exc


# ---------------------------------------------------------------------------
# Installation-token exchange (short-lived, cached, refreshable).
# ---------------------------------------------------------------------------

@dataclass
class InstallationToken:
    """A cached installation access token with its expiry."""

    token: str
    expires_at: float  # epoch seconds
    installation_id: str = ""

    def expired(self, now: float | None = None,
                skew_seconds: int = TOKEN_REFRESH_SKEW_SECONDS) -> bool:
        moment = now if now is not None else time.time()
        return moment >= (self.expires_at - max(0, skew_seconds))


def parse_token_expiry(expires_at_raw: str, now: float | None = None) -> float:
    """Parse a GitHub ``expires_at`` timestamp into epoch seconds.

    Accepts ``YYYY-MM-DDTHH:MM:SSZ`` (and with fractional seconds /
    explicit offsets). Raises :class:`GitHubAuthError` (redacted) when
    the value is missing or unparsable.
    """
    raw = (expires_at_raw or "").strip()
    if not raw:
        raise _auth_failure("GitHub token exchange returned no expiry")
    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    # Fallback for strict Zulu form without fromisoformat support.
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    raise _auth_failure("GitHub token exchange returned an invalid expiry")


# Injectable HTTP layer type: (jwt, installation_id, api_base) -> payload.
TokenExchangeFn = Callable[[str, str, str], Mapping[str, Any]]


def urllib_exchange_installation_token(
    app_jwt: str,
    installation_id: str,
    api_base: str = GITHUB_API_BASE,
    *,
    repositories: Sequence[str] | None = None,
    permissions: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> Mapping[str, Any]:
    """Exchange an App JWT for an installation token via urllib (stdlib).

    Never logs or surfaces the JWT/token. HTTP failures raise redacted
    :class:`GitHubAuthError` (401/403/404 included); other transport
    errors raise :class:`GitHubAppError`.
    """
    if not installation_id or not installation_id.strip():
        raise _auth_failure("GitHub App installation ID must not be empty")
    url = "%s/app/installations/%s/access_tokens" % (
        (api_base or GITHUB_API_BASE).rstrip("/"), installation_id.strip())
    body: dict[str, Any] = {}
    if repositories:
        body["repositories"] = list(repositories)
    if permissions:
        body["permissions"] = dict(permissions)
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": "Bearer REDACTED",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Content-Type": "application/json",
        },
    )
    # Set the real Authorization header after building the redacted copy
    # used in every diagnostic path below.
    request.add_unredirected_header("Authorization", "Bearer %s" % app_jwt)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = getattr(response, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise _auth_failure(
            "GitHub installation-token exchange failed with status %d %s"
            % (exc.code, redact_secrets(detail))) from None
    except urllib.error.URLError as exc:
        raise GitHubAppError(redact_secrets(
            "GitHub installation-token exchange failed: %s"
            % getattr(exc, "reason", exc))) from None
    except (OSError, ValueError) as exc:
        raise GitHubAppError(redact_secrets(
            "GitHub installation-token exchange failed: %s" % exc)) from None
    try:
        payload = json.loads(raw)
    except ValueError:
        raise _auth_failure(
            "GitHub installation-token exchange returned invalid JSON") from None
    if not isinstance(payload, Mapping) or not payload.get("token"):
        raise _auth_failure(
            "GitHub installation-token exchange returned no token (status %d)"
            % status)
    return payload


class InstallationTokenProvider:
    """Caches a short-lived installation token with expiry-aware refresh.

    Thread-safe. ``exchange_fn`` and ``jwt_signer`` are injectable so tests
    never touch the network or RSA keys. Authentication failures raise
    redacted errors and never expose key/JWT/token material.
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        exchange_fn: TokenExchangeFn | None = None,
        jwt_signer: Callable[[bytes], bytes] | None = None,
        time_fn: Callable[[], float] | None = None,
        requested_permissions: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._exchange_fn = exchange_fn or (
            lambda jwt, installation_id, api_base: urllib_exchange_installation_token(
                jwt, installation_id, api_base,
                permissions=dict(requested_permissions or REQUIRED_APP_PERMISSIONS),
            )
        )
        self._jwt_signer = jwt_signer
        self._time_fn = time_fn or time.time
        self._requested_permissions = dict(
            requested_permissions or REQUIRED_APP_PERMISSIONS)
        self._lock = threading.Lock()
        self._cached: InstallationToken | None = None

    @property
    def requested_permissions(self) -> dict[str, str]:
        return dict(self._requested_permissions)

    def cached(self) -> InstallationToken | None:
        with self._lock:
            return self._cached

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-exchanges (401 path)."""
        with self._lock:
            self._cached = None

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a live installation token, refreshing when missing/expired."""
        now = self._time_fn()
        with self._lock:
            cached = self._cached
            if (not force_refresh and cached is not None
                    and not cached.expired(now)):
                return cached.token
        return self._exchange_locked()

    def _exchange_locked(self) -> str:
        now = self._time_fn()
        try:
            app_jwt = build_app_jwt(
                app_id=self.config.app_id,
                private_key_pem=self.config.private_key_pem,
                now=now,
                signer=self._jwt_signer,
            )
        except GitHubAppError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise _auth_failure("could not build GitHub App JWT: %s"
                                % type(exc).__name__) from None
        try:
            payload = self._exchange_fn(
                app_jwt, self.config.installation_id, self.config.api_base)
        except GitHubAppError as exc:
            # Custom exchange functions may raise unredacted errors; scrub
            # again so key/JWT/token material can never propagate.
            raise _auth_failure("GitHub installation-token exchange failed: %s"
                                % exc) from None
        except Exception as exc:
            raise _auth_failure("GitHub installation-token exchange failed: %s"
                                % type(exc).__name__) from None
        if not isinstance(payload, Mapping) or not payload.get("token"):
            raise _auth_failure("GitHub installation-token exchange returned no token")
        raw_token = str(payload.get("token") or "")
        if not raw_token.strip():
            raise _auth_failure("GitHub installation-token exchange returned no token")
        try:
            expires_at = parse_token_expiry(str(payload.get("expires_at", "") or ""))
        except GitHubAppError:
            # Tokens without a parseable expiry are treated as
            # immediately stale so the next call refreshes; the current
            # token is still usable once.
            expires_at = now + 60.0
        token = InstallationToken(
            token=raw_token.strip(),
            expires_at=expires_at,
            installation_id=self.config.installation_id,
        )
        with self._lock:
            self._cached = token
        return token.token


# ---------------------------------------------------------------------------
# GitHub REST client on top of an installation-token provider (stdlib).
# ---------------------------------------------------------------------------

# Injectable low-level request fn:
# (method, url, token, body_json) -> (status, response_json_or_text).
RawRequestFn = Callable[[str, str, str, Any], tuple[int, Any]]


def urllib_raw_request(
    method: str, url: str, token: str, body: Any = None,
    *, timeout: float = 30.0,
) -> tuple[int, Any]:
    """Perform one GitHub REST call via urllib (stdlib).

    ``token`` is sent as ``Bearer`` but never appears in any exception.
    Returns ``(status, parsed_json_or_text)``.
    """
    data: bytes | None = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method.upper(),
                                     headers=headers)
    request.add_unredirected_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200) or 200
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            parsed: Any = json.loads(raw) if raw else ""
        except ValueError:
            parsed = raw[:2000]
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise GitHubAppError(redact_secrets(
            "GitHub API request failed: %s" % getattr(exc, "reason", exc))) from None
    except (OSError, ValueError) as exc:
        raise GitHubAppError(redact_secrets("GitHub API request failed: %s" % exc)) from None
    if not raw:
        return status, ""
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:2000]


class GitHubApiClient:
    """Minimal GitHub REST client authenticated with installation tokens.

    Covers exactly the write-back surface from issue #5 plus the label and
    dependency reads needed for orchestration:

    - inspect issue/task (title, body, state, labels);
    - inspect native blocked-by dependencies (best-effort; ``[]`` when the
      endpoint is unavailable);
    - apply/remove automation labels;
    - resolve the base SHA; find/create/update issue branches via the git
      database API; create PRs; avoid duplicates; report no-change.

    ``request_fn`` is injectable ``(method, url, token, body)`` so tests
    run without network. Expired tokens (401) trigger exactly one
    invalidate + refresh + retry; the retry failure surfaces as a redacted
    :class:`GitHubApiError`.
    """

    def __init__(
        self,
        token_provider: InstallationTokenProvider,
        *,
        api_base: str = GITHUB_API_BASE,
        repository: str = "",
        request_fn: RawRequestFn | None = None,
    ) -> None:
        self.provider = token_provider
        self.api_base = (api_base or token_provider.config.api_base
                         or GITHUB_API_BASE).rstrip("/")
        configured_repo = (repository or token_provider.config.repository or "").strip()
        self.repository = configured_repo
        self._request_fn = request_fn or urllib_raw_request

    # -- low level ------------------------------------------------------

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("GitHub API path must start with '/': %r" % path)
        return self.api_base + path

    def _repo_parts(self, repository: str = "") -> tuple[str, str]:
        full = (repository or self.repository or "").strip()
        if not full:
            raise ValueError("repository (owner/repo) is not configured")
        return split_repository(full)

    def api(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        _retried: bool = False,
    ) -> Any:
        """Perform one authenticated REST call with one 401-refresh retry."""
        token = self.provider.get_token()
        try:
            status, parsed = self._request_fn(method.upper(), self._url(path), token, body)
        except GitHubAppError as exc:
            # Injectable transports may raise unredacted errors; scrub so
            # tokens never propagate in diagnostics.
            raise _api_failure("GitHub API request failed: %s" % exc,
                               status=getattr(exc, "status", None)) from None
        except Exception as exc:
            raise _api_failure("GitHub API request failed: %s"
                               % type(exc).__name__) from None
        if status == 401 and not _retried:
            # Probably an expired token: drop the cache, refresh once, retry.
            self.provider.invalidate()
            token = self.provider.get_token(force_refresh=True)
            try:
                status, parsed = self._request_fn(
                    method.upper(), self._url(path), token, body)
            except GitHubAppError as exc:
                raise _api_failure("GitHub API request failed: %s" % exc,
                                   status=getattr(exc, "status", None)) from None
            except Exception as exc:
                raise _api_failure("GitHub API request failed: %s"
                                   % type(exc).__name__) from None
        if status in (401, 403):
            raise _api_failure(
                "GitHub API authentication failed with status %d" % status,
                status=status)
        if status is None or not (200 <= int(status) < 300):
            detail = ""
            if isinstance(parsed, Mapping):
                detail = str(parsed.get("message", "") or "")[:300]
            elif isinstance(parsed, str):
                detail = parsed[:300]
            raise _api_failure(
                "GitHub API %s %s failed with status %s %s"
                % (method.upper(), redact_secrets(path), status,
                   redact_secrets(detail)),
                status=int(status) if isinstance(status, int) else None)
        return parsed

    # -- issue inspection -------------------------------------------------

    def get_issue(self, issue_number: int, repository: str = "") -> dict[str, Any]:
        """Fetch one issue (title, body, state, labels)."""
        if not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        owner, repo = self._repo_parts(repository)
        payload = self.api("GET", "/repos/%s/%s/issues/%d" % (owner, repo, issue_number))
        if not isinstance(payload, Mapping):
            raise _api_failure("GitHub issue lookup returned invalid JSON")
        labels: list[str] = []
        for item in payload.get("labels", []) or []:
            if isinstance(item, str):
                labels.append(item)
            elif isinstance(item, Mapping) and item.get("name"):
                labels.append(str(item["name"]))
        return {
            "number": int(payload.get("number", issue_number) or issue_number),
            "title": str(payload.get("title", "") or ""),
            "body": str(payload.get("body", "") or ""),
            "state": str(payload.get("state", "") or ""),
            "labels": labels,
        }

    def get_blocked_by(self, issue_number: int, repository: str = "") -> list[int]:
        """Return native blocked-by dependency issue numbers (best-effort).

        Uses the native issue-dependencies endpoint when available; any
        404/410/403/422 (endpoint unavailable or not permitted) yields
        ``[]`` so the caller falls back to the DoR ``#N is completed``
        text scan. Never raises for unavailable-dependency signals.
        """
        if not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        owner, repo = self._repo_parts(repository)
        candidates = (
            "/repos/%s/%s/issues/%d/dependencies/blocked_by" % (owner, repo, issue_number),
            "/repos/%s/%s/issues/%d/blocked_by" % (owner, repo, issue_number),
        )
        for path in candidates:
            try:
                payload = self.api("GET", path)
            except GitHubApiError as exc:
                if exc.status in (400, 403, 404, 410, 422):
                    continue
                raise
            numbers = _extract_dependency_numbers(payload, own_number=issue_number)
            if numbers or path == candidates[-1]:
                return numbers
            # Empty first page with an available endpoint: still try alias.
            continue
        return []

    # -- labels (reservation) ----------------------------------------------

    def add_labels(self, issue_number: int, labels: Sequence[str],
                   repository: str = "") -> list[str]:
        """Add automation labels to an issue; returns the resulting labels."""
        if not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        names = [str(name).strip() for name in labels if str(name).strip()]
        if not names:
            raise ValueError("labels must not be empty")
        owner, repo = self._repo_parts(repository)
        payload = self.api(
            "POST", "/repos/%s/%s/issues/%d/labels" % (owner, repo, issue_number),
            {"labels": names},
        )
        result: list[str] = []
        items = payload if isinstance(payload, list) else []
        for item in items:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, Mapping) and item.get("name"):
                result.append(str(item["name"]))
        return result

    def remove_label(self, issue_number: int, label: str, repository: str = "") -> bool:
        """Remove one label; True when removed (or already absent via 404)."""
        if not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        name = (label or "").strip()
        if not name:
            raise ValueError("label must not be empty")
        owner, repo = self._repo_parts(repository)
        token = self.provider.get_token()
        try:
            status, _ = self._request_fn(
                "DELETE",
                self._url("/repos/%s/%s/issues/%d/labels/%s"
                          % (owner, repo, issue_number,
                             _quote_path_segment(name))),
                token, None)
        except GitHubAppError as exc:
            raise _api_failure("GitHub API request failed: %s" % exc,
                               status=getattr(exc, "status", None)) from None
        except Exception as exc:
            raise _api_failure("GitHub API request failed: %s"
                               % type(exc).__name__) from None
        if status == 404:
            return True  # already absent: idempotent success
        if status == 401:
            self.provider.invalidate()
            token = self.provider.get_token(force_refresh=True)
            try:
                status, _ = self._request_fn(
                    "DELETE",
                    self._url("/repos/%s/%s/issues/%d/labels/%s"
                              % (owner, repo, issue_number,
                                 _quote_path_segment(name))),
                    token, None)
            except GitHubAppError as exc:
                raise _api_failure("GitHub API request failed: %s" % exc,
                                   status=getattr(exc, "status", None)) from None
            except Exception as exc:
                raise _api_failure("GitHub API request failed: %s"
                                   % type(exc).__name__) from None
            if status == 404:
                return True
        if status is None or not (200 <= int(status) < 300):
            raise _api_failure(
                "GitHub API DELETE labels failed with status %s" % status,
                status=int(status) if isinstance(status, int) else None)
        return True

    # -- branches / commits via the git database API ------------------------

    def get_base_sha(self, base_ref: str = "main", repository: str = "") -> str:
        """Resolve the current base SHA for ``base_ref`` (fail closed)."""
        ref = (base_ref or "main").strip() or "main"
        owner, repo = self._repo_parts(repository)
        try:
            payload = self.api(
                "GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, repo, ref))
            if isinstance(payload, Mapping):
                obj = payload.get("object", {})
                sha = obj.get("sha", "") if isinstance(obj, Mapping) else ""
                if isinstance(sha, str) and sha.strip():
                    return sha.strip()
        except GitHubApiError as exc:
            if exc.status not in (403, 404):
                raise
        # Fallback: resolve the commit SHA directly.
        payload = self.api("GET", "/repos/%s/%s/commits/%s" % (owner, repo, ref))
        if isinstance(payload, Mapping) and payload.get("sha"):
            return str(payload["sha"]).strip()
        raise _api_failure("could not resolve current base SHA for %r" % ref)

    def get_branch_sha(self, branch: str, repository: str = "") -> str | None:
        """Return the SHA a branch points at, or None when it is absent."""
        name = (branch or "").strip()
        if not name:
            raise ValueError("branch must not be empty")
        owner, repo = self._repo_parts(repository)
        token = self.provider.get_token()
        try:
            status, parsed = self._request_fn(
                "GET",
                self._url("/repos/%s/%s/git/ref/heads/%s"
                          % (owner, repo, _quote_path_segment(name))),
                token, None)
        except GitHubAppError as exc:
            raise _api_failure("GitHub API request failed: %s" % exc,
                               status=getattr(exc, "status", None)) from None
        except Exception as exc:
            raise _api_failure("GitHub API request failed: %s"
                               % type(exc).__name__) from None
        if status == 404:
            return None
        if status == 401:
            self.provider.invalidate()
            token = self.provider.get_token(force_refresh=True)
            try:
                status, parsed = self._request_fn(
                    "GET",
                    self._url("/repos/%s/%s/git/ref/heads/%s"
                              % (owner, repo, _quote_path_segment(name))),
                    token, None)
            except GitHubAppError as exc:
                raise _api_failure("GitHub API request failed: %s" % exc,
                                   status=getattr(exc, "status", None)) from None
            except Exception as exc:
                raise _api_failure("GitHub API request failed: %s"
                                   % type(exc).__name__) from None
            if status == 404:
                return None
        if status is None or not (200 <= int(status) < 300):
            raise _api_failure(
                "GitHub branch lookup failed with status %s" % status,
                status=int(status) if isinstance(status, int) else None)
        if isinstance(parsed, Mapping):
            obj = parsed.get("object", {})
            sha = obj.get("sha", "") if isinstance(obj, Mapping) else ""
            if isinstance(sha, str) and sha.strip():
                return sha.strip()
        return None

    def list_open_prs(self, repository: str = "") -> list[dict[str, Any]]:
        """List open PRs (number + head ref) for duplicate avoidance."""
        owner, repo = self._repo_parts(repository)
        payload = self.api(
            "GET",
            "/repos/%s/%s/pulls?state=open&per_page=100" % (owner, repo))
        if not isinstance(payload, list):
            raise _api_failure("GitHub pull listing returned invalid JSON")
        result: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            try:
                number = int(item.get("number", 0) or 0)
            except (TypeError, ValueError):
                continue
            head = item.get("head", {})
            head_ref = head.get("ref", "") if isinstance(head, Mapping) else ""
            if number > 0 and head_ref:
                result.append({"number": number, "head_ref": str(head_ref),
                               "state": "open"})
        return result

    def find_open_pr_for_issue(self, issue_number: int,
                               repository: str = "") -> Mapping[str, Any] | None:
        """Return the open PR for an issue branch prefix, if any."""
        try:
            from automation.result_materialize import branch_prefix_for_issue
        except ImportError:  # pytest inserts automation/ on sys.path
            from result_materialize import branch_prefix_for_issue  # type: ignore[no-redef]
        prefix = branch_prefix_for_issue(issue_number)
        for pr in self.list_open_prs(repository):
            if str(pr.get("head_ref", "")).startswith(prefix):
                return dict(pr)
        return None

    # -- git-database write primitives --------------------------------------

    def create_blob(self, content: bytes, repository: str = "") -> str:
        """Store file bytes as a blob; return its SHA."""
        owner, repo = self._repo_parts(repository)
        payload = self.api(
            "POST", "/repos/%s/%s/git/blobs" % (owner, repo),
            {"content": base64.b64encode(content).decode("ascii"),
             "encoding": "base64"},
        )
        if not isinstance(payload, Mapping) or not payload.get("sha"):
            raise _api_failure("GitHub blob creation returned no SHA")
        return str(payload["sha"])

    def get_commit(self, sha: str, repository: str = "") -> Mapping[str, Any]:
        owner, repo = self._repo_parts(repository)
        payload = self.api("GET", "/repos/%s/%s/git/commits/%s" % (owner, repo, sha))
        if not isinstance(payload, Mapping):
            raise _api_failure("GitHub commit lookup returned invalid JSON")
        return payload

    def create_tree(self, base_tree_sha: str,
                    entries: Sequence[Mapping[str, Any]],
                    repository: str = "") -> str:
        owner, repo = self._repo_parts(repository)
        payload = self.api(
            "POST", "/repos/%s/%s/git/trees" % (owner, repo),
            {"base_tree": base_tree_sha, "tree": list(entries)},
        )
        if not isinstance(payload, Mapping) or not payload.get("sha"):
            raise _api_failure("GitHub tree creation returned no SHA")
        return str(payload["sha"])

    def create_commit(self, message: str, tree_sha: str,
                      parents: Sequence[str], repository: str = "") -> str:
        owner, repo = self._repo_parts(repository)
        payload = self.api(
            "POST", "/repos/%s/%s/git/commits" % (owner, repo),
            {"message": message, "tree": tree_sha, "parents": list(parents)},
        )
        if not isinstance(payload, Mapping) or not payload.get("sha"):
            raise _api_failure("GitHub commit creation returned no SHA")
        return str(payload["sha"])

    def create_or_update_ref(self, branch: str, sha: str,
                             repository: str = "") -> None:
        """Create the branch ref, or fast-forward-update it when present."""
        owner, repo = self._repo_parts(repository)
        existing = self.get_branch_sha(branch, repository)
        if existing is None:
            self.api("POST", "/repos/%s/%s/git/refs" % (owner, repo),
                     {"ref": "refs/heads/%s" % branch, "sha": sha})
            return
        if existing == sha:
            return
        self.api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (owner, repo, branch),
                 {"sha": sha, "force": False})

    def create_pull_request(self, *, title: str, body: str, head: str,
                            base: str, repository: str = "") -> int:
        owner, repo = self._repo_parts(repository)
        if not (title or "").strip():
            raise ValueError("PR title must not be empty")
        try:
            payload = self.api(
                "POST", "/repos/%s/%s/pulls" % (owner, repo),
                {"title": title.strip(), "body": body or "",
                 "head": head.strip(), "base": (base or "main").strip()},
            )
        except GitHubApiError as exc:
            # A concurrent attempt may have created the PR first: reuse the
            # now-existing open PR instead of failing duplicates.
            if exc.status in (422,):
                existing = self.find_open_pr_for_issue_from_head(
                    head, repository=repository)
                if existing:
                    return int(existing["number"])
            raise
        if not isinstance(payload, Mapping) or not payload.get("number"):
            raise _api_failure("GitHub PR creation returned no number")
        try:
            number = int(payload["number"])
        except (TypeError, ValueError) as exc:
            raise _api_failure("GitHub PR creation returned an invalid number") from exc
        if number <= 0:
            raise _api_failure("GitHub PR creation returned an invalid number")
        return number

    def find_open_pr_for_issue_from_head(
        self, head: str, repository: str = ""
    ) -> Mapping[str, Any] | None:
        """Reuse the open PR for a head branch after a 422 race."""
        try:
            from automation.result_materialize import _BRANCH_PATTERN
        except ImportError:
            from result_materialize import _BRANCH_PATTERN  # type: ignore[no-redef]
        match = _BRANCH_PATTERN.match((head or "").strip())
        if not match:
            return None
        try:
            issue_number = int(match.group(1))
        except ValueError:
            return None
        return self.find_open_pr_for_issue(issue_number, repository)


def _extract_dependency_numbers(payload: Any, own_number: int) -> list[int]:
    """Extract issue numbers from a native dependencies payload."""
    numbers: list[int] = []
    seen: set[int] = set()
    items = payload if isinstance(payload, list) else (
        payload.get("data", []) if isinstance(payload, Mapping) else [])
    if not isinstance(items, list):
        return []
    for item in items:
        number: Any = None
        if isinstance(item, Mapping):
            for key in ("number", "issue_number"):
                if item.get(key) is not None:
                    number = item.get(key)
                    break
            if number is None:
                issue = item.get("issue")
                if isinstance(issue, Mapping):
                    number = issue.get("number")
        elif isinstance(item, (int, float)):
            number = item
        try:
            value = int(number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value <= 0 or value == own_number or value in seen:
            continue
        seen.add(value)
        numbers.append(value)
    return sorted(numbers)


def _quote_path_segment(value: str) -> str:
    import urllib.parse as _parse

    return _parse.quote(value, safe="")


# ---------------------------------------------------------------------------
# WritebackClient implementation on installation-token REST (issue #5 reuse).
# ---------------------------------------------------------------------------

class AppWritebackClient:
    """Render-controller write-back using GitHub App installation tokens.

    Implements the :class:`WritebackClient` interface from
    ``automation/result_materialize.py`` (issue #5) so
    :func:`materialize_result` runs unchanged from Render: exactly one
    branch and at most one PR per call, no-change detection, duplicate-PR
    reuse, and base-SHA safety. Branch content is published through the
    git-database REST API (blobs/trees/commits/refs), so no local checkout
    or ``GITHUB_TOKEN`` is needed.

    ``dispatch_ci`` is intentionally a recorded no-op: pushes made with an
    installation token trigger CI natively via ``pull_request`` events, and
    the App deliberately does not request ``actions: write``.
    """

    def __init__(
        self,
        api: GitHubApiClient,
        *,
        repository: str = "",
        base_ref: str = "main",
    ) -> None:
        if api is None:
            raise ValueError("api (GitHubApiClient) must not be None")
        self.api = api
        configured = (repository or api.repository or "").strip()
        if not configured:
            raise ValueError("repository (owner/repo) must be configured")
        split_repository(configured)
        self.repository = configured
        self.base_ref = (base_ref or "main").strip() or "main"
        self.ci_dispatched: list[int] = []
        self.publish_calls: list[dict[str, Any]] = []

    # -- WritebackClient interface ----------------------------------------

    def get_base_sha(self, base_ref: str = "main") -> str:
        ref = (base_ref or self.base_ref).strip() or self.base_ref
        return self.api.get_base_sha(ref, self.repository)

    def find_open_pr_for_issue(self, issue_number: int) -> Mapping[str, Any] | None:
        return self.api.find_open_pr_for_issue(issue_number, self.repository)

    def publish_branch(
        self,
        *,
        branch: str,
        base_sha: str,
        changes: Sequence[Any],
        commit_message: str,
    ) -> bool:
        """Publish ``branch`` from ``base_sha`` with ``changes`` via REST.

        Returns True when a new commit was pushed, False when the branch
        already points at a commit with exactly this content (nothing to
        commit). Raises :class:`MaterializeError` for foreign branches or
        workflow-file writes (same guard as the Actions client).
        """
        try:
            from automation.result_materialize import (
                WORKFLOW_DIR_PREFIX,
                MaterializeError,
                branch_matches_issue,
                parse_issue_number_from_branch,
            )
        except ImportError:
            from result_materialize import (  # type: ignore[no-redef]
                WORKFLOW_DIR_PREFIX,
                MaterializeError,
                branch_matches_issue,
                parse_issue_number_from_branch,
            )
        parsed_issue = parse_issue_number_from_branch(branch)
        if not parsed_issue or not branch_matches_issue(branch, parsed_issue):
            raise MaterializeError("refusing to publish foreign branch %r" % branch)
        if not (base_sha or "").strip():
            raise MaterializeError("base_sha must not be empty")
        if not (commit_message or "").strip():
            raise MaterializeError("commit message must not be empty")
        items = list(changes or [])
        if not items:
            return False
        # Normalize entries to (path, change_type, content bytes|None).
        normalized: list[tuple[str, str, bytes | None]] = []
        for entry in items:
            if isinstance(entry, Mapping):
                path = str(entry.get("path", "") or "")
                change_type = str(entry.get("change_type", "") or "")
                content: bytes | None = None
                if change_type != "deleted":
                    encoded = entry.get("content_base64", "")
                    if isinstance(encoded, str) and encoded:
                        try:
                            content = base64.b64decode(
                                encoded.encode("ascii"), validate=True)
                        except (ValueError, binascii.Error) as exc:
                            raise MaterializeError(
                                "change %r has invalid base64 content" % path
                            ) from exc
                    else:
                        content = b""
                normalized.append((path, change_type, content))
            else:
                path = str(getattr(entry, "path", "") or "")
                change_type = str(getattr(entry, "change_type", "") or "")
                content = getattr(entry, "content", None)
                if content is not None and not isinstance(content, (bytes, bytearray)):
                    raise MaterializeError(
                        "change %r has invalid content" % path)
                normalized.append((path, change_type,
                                   bytes(content) if content is not None else None))
        for path, _, _ in normalized:
            lowered = path.strip()
            if lowered.startswith(WORKFLOW_DIR_PREFIX):
                raise MaterializeError(
                    "refusing to commit .github/workflows/** changes")
        self.publish_calls.append({"branch": branch,
                                   "changes": list(items)})
        base = base_sha.strip()
        owner_repo = self.repository
        # Resolve the parent commit: existing branch head wins (update path),
        # otherwise the exact base SHA (create path).
        head_sha = self.api.get_branch_sha(branch, owner_repo)
        parent_sha = head_sha or base
        try:
            parent = self.api.get_commit(parent_sha, owner_repo)
        except GitHubAppError as exc:
            raise MaterializeError("could not resolve parent commit: %s"
                                   % redact_secrets(str(exc))[:300]) from exc
        base_tree = (((parent.get("tree") or {}) if isinstance(parent, Mapping)
                      else {}).get("sha", "") if isinstance(parent, Mapping) else "")
        if not base_tree:
            raise MaterializeError("parent commit has no tree")
        tree_entries: list[dict[str, Any]] = []
        for path, change_type, content in sorted(normalized, key=lambda t: t[0]):
            if change_type == "deleted":
                tree_entries.append({"path": path, "mode": "100644",
                                     "type": "blob", "sha": None})
            else:
                blob_sha = self.api.create_blob(bytes(content or b""), owner_repo)
                tree_entries.append({"path": path, "mode": "100644",
                                     "type": "blob", "sha": blob_sha})
        try:
            tree_sha = self.api.create_tree(str(base_tree), tree_entries, owner_repo)
        except GitHubAppError as exc:
            raise MaterializeError("could not create tree: %s"
                                   % redact_secrets(str(exc))[:300]) from exc
        # No-change detection: an identical tree means the branch already
        # contains exactly these changes.
        parent_tree = str(base_tree)
        if tree_sha == parent_tree:
            return False
        try:
            commit_sha = self.api.create_commit(
                commit_message.strip(), tree_sha, [parent_sha], owner_repo)
        except GitHubAppError as exc:
            raise MaterializeError("could not create commit: %s"
                                   % redact_secrets(str(exc))[:300]) from exc
        if head_sha == commit_sha:
            return False
        try:
            self.api.create_or_update_ref(branch, commit_sha, owner_repo)
        except GitHubAppError as exc:
            raise MaterializeError("could not update branch %r: %s"
                                   % (branch, redact_secrets(str(exc))[:300])) from exc
        return True

    def create_pull_request(
        self, *, title: str, body: str, head: str, base: str
    ) -> int:
        return self.api.create_pull_request(
            title=title, body=body, head=head,
            base=(base or self.base_ref).strip(), repository=self.repository)

    def dispatch_ci(self, pr_number: int) -> None:
        """Record CI coverage without an Actions write.

        Installation-token pushes fire ``pull_request`` events natively, so
        CI runs without an explicit ``ci.yml`` dispatch (which would need
        ``actions: write`` that this App deliberately does not request).
        The PR number is recorded so tests and logs can prove coverage.
        """
        try:
            number = int(pr_number)
        except (TypeError, ValueError) as exc:
            try:
                from automation.result_materialize import MaterializeError
            except ImportError:
                from result_materialize import MaterializeError  # type: ignore[no-redef]
            raise MaterializeError("invalid pr_number") from exc
        if number <= 0:
            try:
                from automation.result_materialize import MaterializeError
            except ImportError:
                from result_materialize import MaterializeError  # type: ignore[no-redef]
            raise MaterializeError("invalid pr_number")
        self.ci_dispatched.append(number)


# ---------------------------------------------------------------------------
# Issue reads for scheduling (GitHub-backed snapshot provider).
# ---------------------------------------------------------------------------

class GitHubAppSnapshotProvider:
    """Enrich webhook payloads with live GitHub reads (issue #11).

    Duck-type compatible with ``IssueSnapshotProvider`` from
    ``automation/render_controller.py`` (same method names) without a hard
    import, so either module can be loaded first. All reads use the
    installation token; failures degrade to safe defaults (no blockers, no
    PR, no lease) and never leak secrets.
    """

    def __init__(self, api: GitHubApiClient,
                 *,
                 attempts: Mapping[int, int] | None = None,
                 active: int = 0) -> None:
        self.api = api
        self._attempts = {int(k): int(v) for k, v in dict(attempts or {}).items()}
        self._active = active

    def open_blockers(self, issue_number: int) -> list[int]:
        try:
            return self.api.get_blocked_by(issue_number)
        except (GitHubAppError, ValueError):
            return []

    def open_issue_states(self, numbers: Sequence[int]) -> dict[int, str]:
        states: dict[int, str] = {}
        for number in numbers:
            try:
                info = self.api.get_issue(int(number))
            except (GitHubAppError, ValueError):
                continue
            states[int(number)] = str(info.get("state", "") or "")
        return states

    def has_open_pr(self, issue_number: int) -> bool:
        try:
            return self.api.find_open_pr_for_issue(issue_number) is not None
        except (GitHubAppError, ValueError):
            return False

    def lease_valid(self, issue_number: int) -> bool:
        try:
            info = self.api.get_issue(int(issue_number))
        except (GitHubAppError, ValueError):
            return False
        labels = set(info.get("labels", []) or [])
        if "automation:in-progress" not in labels:
            return False
        # A live reservation is an in-progress label plus an open PR; a
        # stale label without a PR is reclaimable (mirrors the scheduler).
        return self.has_open_pr(issue_number)

    def dispatch_attempts(self, issue_number: int) -> int:
        return self._attempts.get(int(issue_number), 0)

    def active_count(self) -> int:
        return self._active


# ---------------------------------------------------------------------------
# Reservation labels (best-effort; failures never block dispatch/cleanup).
# ---------------------------------------------------------------------------

IN_PROGRESS_LABEL = "automation:in-progress"


def reserve_issue(api: GitHubApiClient, issue_number: int,
                  repository: str = "") -> bool:
    """Apply the ``automation:in-progress`` reservation label (best-effort)."""
    try:
        api.add_labels(issue_number, [IN_PROGRESS_LABEL],
                       repository or api.repository)
        return True
    except (GitHubAppError, ValueError):
        return False


def release_reservation(api: GitHubApiClient, issue_number: int,
                        repository: str = "") -> bool:
    """Remove the reservation label after completion (best-effort)."""
    try:
        return api.remove_label(issue_number, IN_PROGRESS_LABEL,
                                repository or api.repository)
    except (GitHubAppError, ValueError):
        return False


def build_provider_and_client(
    config: GitHubAppConfig,
    *,
    exchange_fn: TokenExchangeFn | None = None,
    jwt_signer: Callable[[bytes], bytes] | None = None,
    request_fn: RawRequestFn | None = None,
    requested_permissions: Mapping[str, str] | None = None,
) -> tuple[GitHubApiClient, GitHubAppSnapshotProvider]:
    """Build the API client + snapshot provider for a config (one helper)."""
    provider = InstallationTokenProvider(
        config, exchange_fn=exchange_fn, jwt_signer=jwt_signer,
        requested_permissions=requested_permissions)
    api = GitHubApiClient(provider, api_base=config.api_base,
                          repository=config.repository, request_fn=request_fn)
    return api, GitHubAppSnapshotProvider(api)


def writeback_client_for(
    api: GitHubApiClient, *, base_ref: str = "main",
    repository: str = "",
) -> AppWritebackClient:
    """Build the #5-compatible write-back client for one API client.

    ``repository`` optionally rebinds the client to an allow-listed
    cross-repository target (issue #85, e.g. ``kodmial/opencode``);
    otherwise the API client's configured repository is used.
    """
    target = (repository or "").strip() or api.repository
    try:
        try:
            from automation.cross_repo import normalize_target_repo
        except ImportError:
            from cross_repo import normalize_target_repo  # type: ignore[no-redef]
        if target:
            target = normalize_target_repo(target)
    except ImportError:
        pass
    return AppWritebackClient(api, repository=target, base_ref=base_ref)
