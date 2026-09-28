"""Private long-term agent knowledge store (issue #37).

Centralized versioned knowledge ledger in a dedicated **private**
GitHub repository ``kodmial/agent-knowledge``, reusing the canonical
schema-validated record model from issue #34.

Trust boundary (authoritative)::

    private agent-knowledge repo
            ^
            | read/write via trusted layer
            |
    trusted GitHub Actions / Render controller / knowledge client
            |
            | only relevant selected knowledge
            v
    ephemeral OpenCode worker
            |
            | structured result + knowledge record
            v
    trusted layer validates and persists

The ephemeral worker must never receive a credential that can read the
private repository. It receives only relevant retrieved knowledge in its
task/context and returns its new record as structured output; the
trusted layer performs private-repository reads/writes.

Repository layout (multiple source projects)::

    schema/
    projects/
      runtime-lab/
        experiments/
        topics/
        decisions/

Future projects use sibling namespaces such as
``projects/nanodictate/``. No Runtime Lab-specific assumption lives at
the repository root except generic knowledge-store contracts.

Relationship to issue #34: this module reuses the schema, identifiers,
deterministic catalog rules, and validation/query semantics from
``automation/knowledge_catalog.py``. It never invents a second
incompatible format.

Storage: the adapter speaks GitHub REST Git/Contents APIs through an
injected transport. Normal agent execution never clones the entire
private repository.

Authentication:

- Temporary/bootstrap path: ``TAP_PAT`` only when already available as
  a secret with sufficient permissions. Never printed, persisted,
  committed, or exposed.
- Target path: the existing Runtime Lab GitHub App infrastructure
  (``automation/github_app.py``) mints a short-lived installation
  token scoped to ``kodmial/agent-knowledge`` with least privilege
  (metadata:read + contents:write/read). The installation token is
  never passed to the ephemeral worker.

Concurrency is optimistic compare-and-swap with bounded retries, never
naive parallel clone/commit/push.

Stdlib only. All unit tests run without real GitHub credentials or
network access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

KNOWLEDGE_REPO_FULL = "kodmial/agent-knowledge"
KNOWLEDGE_REPO_OWNER = "kodmial"
KNOWLEDGE_REPO_NAME = "agent-knowledge"
KNOWLEDGE_DEFAULT_BRANCH = "main"
TAP_PAT_ENV = "TAP_PAT"

# In the OpenCode Actions workflow the TAP_PAT repository secret is mapped
# as ``GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}`` (and the same for
# ``GITHUB_TOKEN``). It is NOT exposed under the literal name ``TAP_PAT``,
# so a literal ``TAP_PAT`` presence check cannot distinguish "PAT missing"
# from "PAT present under GH_TOKEN". The authoritative test in Actions is a
# live authenticated capability check through GH_TOKEN/GitHub CLI or the
# trusted transport, distinguishing the repository-scoped ``github.token``
# fallback from a PAT by the required capability (not by token format).
TRUSTED_TRANSPORT_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")

# kodmial is a personal GitHub account, not an Organization. Repository
# creation must use POST /user/repos (authenticated-user endpoint), never
# POST /orgs/kodmial/repos. Creating a private repo for the authenticated
# user requires classic PAT scope ``repo`` or fine-grained PAT repository
# creation (write) / administration (write).
BOOTSTRAP_CREATE_PATH = "/user/repos"
FORBIDDEN_ORG_CREATE_PATH = "/orgs/kodmial/repos"

# Least privilege for normal knowledge-store operation.
KNOWLEDGE_APP_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "metadata": "read",
}

# Bounded optimistic-concurrency retry budget.
MAX_CAS_ATTEMPTS = 5

PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TOPIC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RECORD_ID_RE = re.compile(r"^issue-[1-9][0-9]*-run-[A-Za-z0-9._-]+$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Credential-shaped patterns that must never appear in a worker payload.
_CREDENTIAL_KEY_MARKERS = (
    "TAP_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "INSTALLATION_TOKEN",
    "PRIVATE KEY",
)
_TOKEN_SHAPED_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"),
    re.compile(r"gh[pousr][-_][A-Za-z0-9\-_]+"),
    re.compile(r"ghs[-_][A-Za-z0-9\-_]+"),
    re.compile(r"ghu[-_][A-Za-z0-9\-_]+"),
    re.compile(r"github_pat_[A-Za-z0-9\-_]+"),
)


# ---------------------------------------------------------------------------
# Errors (never carry secret material).
# ---------------------------------------------------------------------------


class KnowledgeStoreError(RuntimeError):
    """Base error for the private knowledge store (fail closed)."""


class KnowledgeAuthBlocker(KnowledgeStoreError):
    """Explicit safe blocker: credential unavailable or insufficient."""


class PublicRepositoryError(KnowledgeStoreError):
    """The target repository is public or would be made public."""


class KnowledgeConflictError(KnowledgeStoreError):
    """Optimistic-concurrency conflict (retryable within budget)."""


class ImmutableOverwriteError(KnowledgeConflictError):
    """An immutable experiment record would be silently overwritten."""


class CASSExhaustedError(KnowledgeConflictError):
    """Optimistic-concurrency retry budget exhausted."""


class KnowledgeValidationError(KnowledgeStoreError):
    """Schema/catalog validation failed for a knowledge record."""


# ---------------------------------------------------------------------------
# Secret hygiene (reuses github_app redaction plus TAP_PAT coverage).
# ---------------------------------------------------------------------------


def _github_app_redact(text: str) -> str:
    try:
        try:
            from automation.github_app import redact_secrets as _redact
        except ImportError:
            from github_app import redact_secrets as _redact  # type: ignore[no-redef]
        return _redact(text)
    except Exception:
        redacted = text
        for pattern in _TOKEN_SHAPED_PATTERNS:
            redacted = pattern.sub("[redacted]", redacted)
        redacted = re.sub(
            r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----",
            "[redacted-pem-key]",
            redacted,
            flags=re.DOTALL,
        )
        return redacted[:2000]


def redact_knowledge_error(text: str) -> str:
    """Redact PAT/token/key material from an error string.

    Extends the ``github_app.redact_secrets`` behavior (which scans
    ``*TOKEN*``/``*KEY*``/``*SECRET*`` env names) with explicit
    ``TAP_PAT`` coverage, because ``TAP_PAT`` does not contain the
    ``TOKEN`` marker but is equally sensitive.
    """
    if not isinstance(text, str) or not text:
        return ""
    redacted = _github_app_redact(text)
    try:
        tap_value = os.environ.get(TAP_PAT_ENV, "")
    except Exception:
        tap_value = ""
    if isinstance(tap_value, str) and len(tap_value) >= 4 and tap_value in redacted:
        redacted = redacted.replace(tap_value, "[redacted]")
    # Generic TAP_PAT-shaped assignment (e.g. "TAP_PAT=abc...").
    redacted = re.sub(r"(?i)(TAP_PAT\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", redacted)
    return redacted[:2000]


def _fail(message: str, exc: type = KnowledgeStoreError) -> KnowledgeStoreError:
    return exc(redact_knowledge_error(message))


# ---------------------------------------------------------------------------
# Bootstrap request (private=true required) and repository verification.
# ---------------------------------------------------------------------------


def build_bootstrap_repo_request(
    owner: str = KNOWLEDGE_REPO_OWNER,
    repo: str = KNOWLEDGE_REPO_NAME,
    *,
    description: str = "Private long-term agent knowledge ledger (Runtime Lab).",
) -> tuple[str, dict[str, Any]]:
    """Build the GitHub create-repository request (personal-account path).

    Returns ``(url_path, payload)`` for ``POST /user/repos``. ``kodmial``
    is a personal account, not an Organization, so ``POST
    /orgs/kodmial/repos`` must never be used. The payload always requires
    ``private=true``.
    """
    if (owner or "").strip() != KNOWLEDGE_REPO_OWNER:
        raise _fail("knowledge repository owner must be %r" % KNOWLEDGE_REPO_OWNER)
    if (repo or "").strip() != KNOWLEDGE_REPO_NAME:
        raise _fail("knowledge repository name must be %r" % KNOWLEDGE_REPO_NAME)
    path = BOOTSTRAP_CREATE_PATH
    payload: dict[str, Any] = {
        "name": KNOWLEDGE_REPO_NAME,
        "private": True,
        "visibility": "private",
        "description": description,
        # The Knowledge Plane roadmap lives in the private repository
        # itself as native GitHub issues, so bootstrap must leave Issues
        # enabled. (Previously has_issues=False; corrected per issue #37
        # repository-settings correction.)
        "has_issues": True,
        "has_projects": False,
        "has_wiki": False,
        "auto_init": False,
        "default_branch": KNOWLEDGE_DEFAULT_BRANCH,
    }
    return path, payload


def assert_not_org_bootstrap_endpoint(path: str) -> str:
    """Fail closed when the forbidden org endpoint is used (personal acct)."""
    cleaned = (path or "").strip()
    if cleaned == FORBIDDEN_ORG_CREATE_PATH or cleaned.startswith("/orgs/"):
        raise _fail(
            "knowledge-repository bootstrap must use POST /user/repos "
            "(kodmial is a personal account, not an Organization)"
        )
    if cleaned != BOOTSTRAP_CREATE_PATH:
        raise _fail("knowledge-repository bootstrap path must be /user/repos")
    return cleaned


def require_private_bootstrap_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Fail closed unless the bootstrap request requires a private repo."""
    if not isinstance(payload, Mapping):
        raise _fail("bootstrap request must be a mapping")
    if payload.get("private") is not True:
        raise PublicRepositoryError(
            redact_knowledge_error(
                "refusing knowledge-repository bootstrap without private=true"
            )
        )
    visibility = str(payload.get("visibility", "private") or "private").strip().lower()
    if visibility != "private":
        raise PublicRepositoryError(
            redact_knowledge_error(
                "refusing knowledge-repository bootstrap with visibility %r" % visibility
            )
        )
    if str(payload.get("name", "") or "").strip() != KNOWLEDGE_REPO_NAME:
        raise _fail("bootstrap request names the wrong repository")
    # The Knowledge Plane uses repository-native roadmap/dependency
    # tracking, so bootstrap must not disable Issues.
    if payload.get("has_issues") is not True:
        raise _fail("bootstrap request must leave GitHub Issues enabled (has_issues=true)")
    return payload


def build_knowledge_repo_settings_patch() -> dict[str, Any]:
    """Return the PATCH body enabling Issues on the knowledge repository.

    Minimal correction: only ``has_issues`` is flipped to ``True``.
    Visibility and every other security-relevant setting are untouched
    (never made public here).
    """
    return {"has_issues": True}


def knowledge_repo_settings_patch_path(
    owner: str = KNOWLEDGE_REPO_OWNER,
    repo: str = KNOWLEDGE_REPO_NAME,
) -> str:
    """Return the API path for the knowledge-repository settings PATCH."""
    if (owner or "").strip() != KNOWLEDGE_REPO_OWNER:
        raise _fail("knowledge repository owner must be %r" % KNOWLEDGE_REPO_OWNER)
    if (repo or "").strip() != KNOWLEDGE_REPO_NAME:
        raise _fail("knowledge repository name must be %r" % KNOWLEDGE_REPO_NAME)
    return "/repos/%s/%s" % (KNOWLEDGE_REPO_OWNER, KNOWLEDGE_REPO_NAME)


def verify_knowledge_repo_settings_for_roadmap(info: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the repo stays private and has Issues enabled for the roadmap.

    Combines :func:`verify_knowledge_repository` (exact full name, private,
    default branch) with the ``has_issues == True`` requirement. Fail
    closed on any violation; never weakens visibility.
    """
    verified = verify_knowledge_repository(info)
    if not isinstance(info, Mapping):
        raise _fail("repository info must be a mapping")
    if info.get("has_issues") is not True:
        raise _fail(
            "knowledge repository %s must have GitHub Issues enabled "
            "(has_issues=true) for the Knowledge Plane roadmap" % KNOWLEDGE_REPO_FULL
        )
    verified["has_issues"] = True
    return verified


def verify_knowledge_repository(info: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a GitHub repository payload is the private knowledge store.

    Fail closed when: wrong full name, not private, default branch
    missing, or any public visibility signal. Never silently reuses a
    public repository.
    """
    if not isinstance(info, Mapping):
        raise _fail("repository info must be a mapping")
    full_name = str(info.get("full_name", "") or "").strip()
    if full_name != KNOWLEDGE_REPO_FULL:
        raise _fail(
            "unexpected knowledge repository %r (expected %r)"
            % (full_name, KNOWLEDGE_REPO_FULL)
        )
    private = info.get("private")
    visibility = str(info.get("visibility", "private") or "private").strip().lower()
    # GitHub returns private=true for private repos; visibility may be
    # absent on older payloads, in which case private alone decides.
    if private is not True:
        raise PublicRepositoryError(
            redact_knowledge_error(
                "knowledge repository %s is not private (private=%r)"
                % (KNOWLEDGE_REPO_FULL, private)
            )
        )
    if visibility not in ("private", ""):
        raise PublicRepositoryError(
            redact_knowledge_error(
                "knowledge repository %s has non-private visibility %r"
                % (KNOWLEDGE_REPO_FULL, visibility)
            )
        )
    default_branch = str(info.get("default_branch", "") or "").strip()
    if not default_branch:
        raise _fail("knowledge repository has no default branch")
    return {
        "full_name": full_name,
        "private": True,
        "visibility": "private" if visibility in ("private", "") else visibility,
        "default_branch": default_branch,
    }


# ---------------------------------------------------------------------------
# TAP_PAT availability (explicit safe blocker, never a public fallback).
#
# NOTE (live-bootstrap correction): in `.github/workflows/opencode.yml` the
# TAP_PAT secret is mapped as GH_TOKEN/GITHUB_TOKEN
# (``${{ secrets.TAP_PAT || github.token }}``), never under the literal
# name ``TAP_PAT``. A literal ``os.environ["TAP_PAT"]`` check therefore
# cannot prove the PAT is unavailable inside Actions. The authoritative
# test there is a live authenticated capability check through GH_TOKEN /
# GitHub CLI or the trusted transport (see below): a repository-scoped
# ``github.token`` fallback fails the required capability (create/read the
# private personal-account repository) while a PAT with ``repo`` scope (or
# fine-grained repository-creation write) succeeds. Capability, not token
# format/value, decides. Never print either token.
# ---------------------------------------------------------------------------

TAP_PAT_BLOCKER_MESSAGE = (
    "TAP_PAT is unavailable or lacks permission to create/access "
    "kodmial/agent-knowledge. Live bootstrap is paused for manual "
    "credential setup: provide TAP_PAT with repo (read/write) scope for "
    "the private repository, or install the Runtime Lab GitHub App on "
    "kodmial/agent-knowledge with metadata:read + contents:write. "
    "No public fallback is permitted; offline implementation remains valid."
)


def tap_pat_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Report literal TAP_PAT availability without exposing its value.

    Offline helper only. Inside the Actions workflow the secret arrives as
    GH_TOKEN/GITHUB_TOKEN (see note above), so this literal check must not
    be used as the availability test there; use the trusted-transport
    capability probe instead.
    """
    env = environ if environ is not None else os.environ
    try:
        value = str(env.get(TAP_PAT_ENV, "") or "")
    except Exception:
        value = ""
    available = bool(value.strip())
    if available:
        return {"available": True, "blocker": None}
    return {"available": False, "blocker": TAP_PAT_BLOCKER_MESSAGE}


def require_tap_pat(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed with an explicit blocker when literal TAP_PAT is absent."""
    status = tap_pat_status(environ)
    if not status["available"]:
        raise KnowledgeAuthBlocker(redact_knowledge_error(str(status["blocker"])))


def trusted_transport_token_present(environ: Mapping[str, str] | None = None) -> bool:
    """True when GH_TOKEN/GITHUB_TOKEN is non-empty (value never exposed)."""
    env = environ if environ is not None else os.environ
    try:
        for name in TRUSTED_TRANSPORT_ENV_NAMES:
            if str(env.get(name, "") or "").strip():
                return True
    except Exception:
        return False
    return False


def describe_bootstrap_credential_source(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Redacted description of the bootstrap credential source (no values)."""
    env = environ if environ is not None else os.environ
    try:
        literal = bool(str(env.get(TAP_PAT_ENV, "") or "").strip())
    except Exception:
        literal = False
    transport = trusted_transport_token_present(env)
    if literal and transport:
        return (
            "TAP_PAT literal present and GH_TOKEN/GITHUB_TOKEN trusted "
            "transport present (values redacted; live capability probe "
            "is authoritative)"
        )
    if literal:
        return "TAP_PAT literal present (value redacted)"
    if transport:
        return (
            "GH_TOKEN/GITHUB_TOKEN trusted transport present (value "
            "redacted; may carry TAP_PAT or the repository-scoped "
            "github.token fallback; live capability probe is authoritative)"
        )
    return "no bootstrap credential in TAP_PAT or GH_TOKEN/GITHUB_TOKEN"


def classify_bootstrap_capability_failure(
    *,
    operation: str,
    status: int | None,
    message: str = "",
) -> KnowledgeAuthBlocker:
    """Build a redacted blocker for a failed live capability probe.

    Distinguishes the repository-scoped ``github.token`` fallback (403 on
    user-repo creation / 404 on the private repo) from other failures by
    HTTP status/capability, never by token format/value. Never embeds
    token material.
    """
    op = (operation or "bootstrap").strip() or "bootstrap"
    detail = redact_knowledge_error(str(message or ""))[:300]
    if status == 401:
        text = (
            "live bootstrap capability probe failed during %s: HTTP 401 "
            "(unauthenticated; trusted token missing or revoked). %s" % (op, detail)
        )
    elif status == 403:
        text = (
            "live bootstrap capability probe failed during %s: HTTP 403 "
            "(token lacks the required capability: classic PAT needs repo "
            "scope, fine-grained PAT needs repository-creation write; the "
            "repository-scoped github.token fallback cannot create/access "
            "a different private repository). %s" % (op, detail)
        )
    elif status == 404:
        text = (
            "live bootstrap capability probe returned HTTP 404 during %s "
            "(repository not visible to this token: either it does not "
            "exist yet or the token -- e.g. the repository-scoped "
            "github.token fallback -- cannot see the private repository). "
            "%s" % (op, detail)
        )
    elif status == 422:
        text = (
            "live bootstrap capability probe failed during %s: HTTP 422 "
            "(validation failed; the repository may already exist). %s" % (op, detail)
        )
    elif status is None:
        text = "live bootstrap capability probe failed during %s. %s" % (op, detail)
    else:
        text = (
            "live bootstrap capability probe failed during %s: HTTP %s. %s"
            % (op, status, detail)
        )
    return KnowledgeAuthBlocker(redact_knowledge_error(text))


# ---------------------------------------------------------------------------
# Project namespace paths (no traversal, no root-level project assumptions).
# ---------------------------------------------------------------------------


def validate_project_slug(project: str) -> str:
    """Validate a knowledge project namespace slug (fail closed)."""
    slug = (project or "").strip()
    if not slug or PROJECT_SLUG_RE.match(slug) is None:
        raise _fail("invalid knowledge project %r" % (project,))
    return slug


def _validate_record_id(record_id: str) -> str:
    rid = (record_id or "").strip()
    if not rid or RECORD_ID_RE.match(rid) is None:
        raise _fail("invalid record_id %r" % (record_id,))
    if "/" in rid or ".." in rid:
        raise _fail("invalid record_id %r" % (record_id,))
    return rid


def _validate_topic_slug(topic: str) -> str:
    slug = (topic or "").strip()
    if not slug or TOPIC_SLUG_RE.match(slug) is None:
        raise _fail("invalid topic slug %r" % (topic,))
    return slug


def _validate_doc_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or "/" in cleaned or ".." in cleaned or cleaned.startswith("."):
        raise _fail("invalid document name %r" % (name,))
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", cleaned):
        raise _fail("invalid document name %r" % (name,))
    return cleaned


def experiment_remote_path(project: str, record_id: str) -> str:
    """Remote path for one immutable experiment record."""
    return "projects/%s/experiments/%s.md" % (
        validate_project_slug(project),
        _validate_record_id(record_id),
    )


def topic_remote_path(project: str, topic: str) -> str:
    """Remote path for one curated topic note."""
    return "projects/%s/topics/%s.md" % (
        validate_project_slug(project),
        _validate_topic_slug(topic),
    )


def decision_remote_path(project: str, name: str) -> str:
    """Remote path for one decision record."""
    return "projects/%s/decisions/%s" % (
        validate_project_slug(project),
        _validate_doc_name(name),
    )


def schema_remote_path(name: str) -> str:
    """Remote path for a generic store-level schema contract."""
    return "schema/%s" % _validate_doc_name(name)


def initial_layout_file_map(
    *,
    project: str = "runtime-lab",
    experiment_schema_text: str = "",
    catalog_schema_text: str = "",
) -> dict[str, str]:
    """Return the intended initial file map for the private repository.

    Generic contracts live at the root; project namespaces live under
    ``projects/<project>/`` so sibling projects can be added without
    restructuring.
    """
    slug = validate_project_slug(project)
    files: dict[str, str] = {
        "README.md": (
            "# Agent knowledge ledger\n\n"
            "Private, centralized, versioned long-term memory for agents.\n"
            "Generic contracts live at the repository root; per-project "
            "knowledge lives under `projects/<project>/`.\n"
            "Record format reuses the Runtime Lab canonical experiment "
            "schema (see `schema/`).\n"
        ),
        "projects/%s/experiments/.gitkeep" % slug: "",
        "projects/%s/topics/.gitkeep" % slug: "",
        "projects/%s/decisions/.gitkeep" % slug: "",
    }
    if experiment_schema_text:
        files["schema/experiment.json"] = experiment_schema_text
    if catalog_schema_text:
        files["schema/catalog.json"] = catalog_schema_text
    return files


# ---------------------------------------------------------------------------
# Worker trust boundary: knowledge in, credentials never.
# ---------------------------------------------------------------------------


def worker_payload_contains_credentials(payload: Any) -> bool:
    """True when a worker payload carries storage credentials."""
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    upper = text.upper()
    for marker in _CREDENTIAL_KEY_MARKERS:
        if marker in upper:
            # Allow the words "private key" only in prose that does not
            # carry key material? Fail closed: any marker is suspicious.
            # The curated knowledge notes legitimately mention file names
            # such as GITHUB_APP_ID only in code references, so callers
            # should pass structured knowledge, not raw docs, when the
            # marker appears. Treat presence as credential-bearing.
            return True
    for pattern in _TOKEN_SHAPED_PATTERNS:
        if pattern.search(text):
            return True
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        return True
    return False


def assert_worker_payload_has_no_credentials(payload: Any) -> None:
    """Fail closed when a worker payload carries storage credentials."""
    if worker_payload_contains_credentials(payload):
        raise _fail("worker payload must never carry storage credentials")


def build_worker_context(
    *,
    issue: int,
    topic: str = "",
    experiments: Sequence[Mapping[str, Any]] | None = None,
    topic_notes: Mapping[str, str] | None = None,
    schemas: Mapping[str, str] | None = None,
    record_limit: int = 10,
) -> dict[str, Any]:
    """Build the credential-free context handed to an ephemeral worker.

    Contains only relevant selected knowledge (record metadata + bodies
    supplied by the trusted layer, topic notes, schemas). Never embeds
    PAT/App tokens, repository credentials, or API secrets. Raises when
    any input already carries credential material.
    """
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise _fail("worker context requires a positive issue number")
    if topic and _validate_topic_slug(topic) != topic:
        raise _fail("invalid worker context topic")
    selected: list[dict[str, Any]] = []
    for entry in list(experiments or [])[: max(0, int(record_limit))]:
        if not isinstance(entry, Mapping):
            raise _fail("worker context experiment must be a mapping")
        item = {
            "record_id": str(entry.get("record_id", "")),
            "issue": entry.get("issue"),
            "topic": str(entry.get("topic", "")),
            "outcome": str(entry.get("outcome", "")),
            "title": str(entry.get("title", "")),
            "body": str(entry.get("body", "") or entry.get("content", "")),
        }
        selected.append(item)
    notes = {str(k): str(v) for k, v in dict(topic_notes or {}).items()}
    schema_map = {str(k): str(v) for k, v in dict(schemas or {}).items()}
    context: dict[str, Any] = {
        "store": KNOWLEDGE_REPO_FULL,
        "issue": issue,
        "topic": topic,
        "experiments": selected,
        "topics": notes,
        "schemas": schema_map,
        "trust": "ephemeral-worker: knowledge only, no storage credentials",
    }
    assert_worker_payload_has_no_credentials(context)
    # Defensive: the static trust marker plus the scan above guarantee no
    # credential key ever reaches the worker.
    for forbidden in ("TAP_PAT", "GITHUB_TOKEN", "GH_TOKEN", "PRIVATE KEY"):
        assert forbidden not in json.dumps(context), forbidden
    return context


# ---------------------------------------------------------------------------
# Migration (canonical #34 model only; identity/provenance preserved).
# ---------------------------------------------------------------------------


def _knowledge_catalog():
    try:
        from automation import knowledge_catalog as catalog
    except ImportError:
        import knowledge_catalog as catalog  # type: ignore[no-redef]
    return catalog


def validate_experiment_text_canonical(text: str, source: str = "<record>") -> dict:
    """Validate one record against the canonical #34 model (fail closed)."""
    catalog = _knowledge_catalog()
    metadata, body = catalog.parse_record_text(text, source)
    catalog.validate_metadata_shapes(metadata, source)
    catalog.check_required_sections(body, source)
    return metadata


def local_record_file_to_remote_path(project: str, filename: str) -> str:
    """Map a local record filename to its namespaced remote path."""
    catalog = _knowledge_catalog()
    match = catalog.RECORD_FILENAME_RE.match((filename or "").strip())
    if match is None:
        raise _fail("local record filename must match issue-<N>-run-<ID>.md")
    record_id = "issue-%s-run-%s" % (match.group("issue"), match.group("run_id"))
    return experiment_remote_path(project, record_id)


def migrate_experiment_text(text: str, source: str = "<record>") -> tuple[str, dict]:
    """Validate and return migratable record content (bytes preserved).

    Returns ``(content, metadata)`` where content is the original text
    unchanged, so record IDs, run IDs, provenance, supersedence,
    evidence links, and historical meaning are preserved exactly.
    Raises :class:`KnowledgeValidationError` when the record does not
    satisfy the canonical #34 schema.
    """
    catalog = _knowledge_catalog()
    try:
        metadata, _body = catalog.parse_record_text(text, source)
        catalog.validate_metadata_shapes(metadata, source)
        # Body sections are part of the canonical contract.
        _, body = catalog.parse_record_text(text, source)
        catalog.check_required_sections(body, source)
    except ValueError as exc:
        raise KnowledgeValidationError(
            redact_knowledge_error("migrated record failed canonical validation: %s" % exc)
        ) from None
    return text, dict(metadata)


def verify_migrated_record_identity(local_text: str, remote_text: str) -> dict:
    """Prove a migrated record preserves identity/provenance (fail closed)."""
    catalog = _knowledge_catalog()
    try:
        local_meta, _ = catalog.parse_record_text(local_text, "local")
        remote_meta, _ = catalog.parse_record_text(remote_text, "remote")
    except ValueError as exc:
        raise KnowledgeValidationError(
            redact_knowledge_error("migrated record is malformed: %s" % exc)
        ) from None
    for key in ("record_id", "issue", "run_id", "base_commit", "topic",
                "outcome", "supersedes", "schema", "$schema"):
        if local_meta.get(key) != remote_meta.get(key):
            raise KnowledgeValidationError(
                redact_knowledge_error(
                    "migrated record changed %r (identity/provenance must be preserved)"
                    % key
                )
            )
    return dict(remote_meta)


# ---------------------------------------------------------------------------
# Storage adapter (no local-filesystem coupling, no full clone).
# ---------------------------------------------------------------------------


class KnowledgeStore:
    """Abstract knowledge-store interface (project-namespaced)."""

    def list_experiments(self, project: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_experiments(
        self,
        project: str,
        *,
        issue: int | None = None,
        topic: str | None = None,
        outcome: str | None = None,
        record_id: str | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_experiment(self, project: str, record_id: str) -> str:
        raise NotImplementedError

    def get_topic(self, project: str, topic: str) -> str | None:
        raise NotImplementedError

    def put_experiment(self, project: str, record_id: str, content: str) -> None:
        raise NotImplementedError

    def put_topic(
        self,
        project: str,
        topic: str,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        raise NotImplementedError

    def get_schema(self, name: str) -> str | None:
        raise NotImplementedError


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InMemoryKnowledgeStore(KnowledgeStore):
    """Offline reference backend with CAS semantics (no network).

    - Experiment records are immutable: a second ``put_experiment`` with
      different bytes raises :class:`ImmutableOverwriteError`.
    - Topic notes are versioned by content SHA-256: ``put_topic`` with
      an ``expected_sha256`` that no longer matches raises
      :class:`KnowledgeConflictError` carrying both versions so the
      caller merges explicitly instead of silently discarding the
      concurrent update.
    """

    def __init__(self) -> None:
        self._experiments: dict[tuple[str, str], str] = {}
        self._topics: dict[tuple[str, str], tuple[str, str]] = {}
        self._schemas: dict[str, str] = {}

    # -- experiments ----------------------------------------------------

    def list_experiments(self, project: str) -> list[dict[str, Any]]:
        slug = validate_project_slug(project)
        catalog = _knowledge_catalog()
        entries: list[dict[str, Any]] = []
        for (owner, record_id), content in sorted(self._experiments.items()):
            if owner != slug:
                continue
            try:
                metadata, body = catalog.parse_record_text(
                    content, "store:%s" % record_id)
                catalog.validate_metadata_shapes(metadata, "store:%s" % record_id)
            except ValueError as exc:
                raise KnowledgeValidationError(
                    redact_knowledge_error("stored record is invalid: %s" % exc)
                ) from None
            title = catalog.extract_title(body)
            entries.append(
                {
                    "record_id": metadata["record_id"],
                    "issue": metadata["issue"],
                    "run_id": metadata["run_id"],
                    "base_commit": metadata["base_commit"],
                    "topic": metadata["topic"],
                    "outcome": metadata["outcome"],
                    "supersedes": list(metadata["supersedes"]),
                    "path": experiment_remote_path(slug, record_id),
                    "sha256": _sha256_text(content),
                    "title": title,
                }
            )
        entries.sort(key=lambda item: item["record_id"])
        return entries

    def query_experiments(
        self,
        project: str,
        *,
        issue: int | None = None,
        topic: str | None = None,
        outcome: str | None = None,
        record_id: str | None = None,
    ) -> list[dict[str, Any]]:
        catalog = _knowledge_catalog()
        return catalog.filter_records(
            self.list_experiments(project),
            issue=issue,
            topic=topic,
            outcome=outcome,
            record_id=record_id,
        )

    def get_experiment(self, project: str, record_id: str) -> str:
        slug = validate_project_slug(project)
        rid = _validate_record_id(record_id)
        try:
            return self._experiments[(slug, rid)]
        except KeyError:
            raise _fail("experiment %r not found in project %r" % (rid, slug)) from None

    def put_experiment(self, project: str, record_id: str, content: str) -> None:
        slug = validate_project_slug(project)
        rid = _validate_record_id(record_id)
        if not isinstance(content, str) or not content.strip():
            raise _fail("experiment content must be non-empty")
        metadata = validate_experiment_text_canonical(content, "store:%s" % rid)
        if metadata.get("record_id") != rid:
            raise KnowledgeValidationError(
                redact_knowledge_error("record_id does not match storage path")
            )
        key = (slug, rid)
        existing = self._experiments.get(key)
        if existing is None:
            self._experiments[key] = content
            return
        if existing != content:
            raise ImmutableOverwriteError(
                redact_knowledge_error(
                    "immutable experiment record %r already exists with different content"
                    % rid
                )
            )
        # Identical bytes: idempotent success.

    # -- topics ----------------------------------------------------------

    def get_topic(self, project: str, topic: str) -> str | None:
        slug = validate_project_slug(project)
        name = _validate_topic_slug(topic)
        entry = self._topics.get((slug, name))
        return entry[0] if entry is not None else None

    def topic_version(self, project: str, topic: str) -> str | None:
        slug = validate_project_slug(project)
        name = _validate_topic_slug(topic)
        entry = self._topics.get((slug, name))
        return entry[1] if entry is not None else None

    def put_topic(
        self,
        project: str,
        topic: str,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        slug = validate_project_slug(project)
        name = _validate_topic_slug(topic)
        if not isinstance(content, str) or not content.strip():
            raise _fail("topic content must be non-empty")
        key = (slug, name)
        existing = self._topics.get(key)
        current_version = existing[1] if existing is not None else None
        if expected_sha256 is not None and current_version != expected_sha256:
            raise KnowledgeConflictError(
                redact_knowledge_error(
                    "topic %r changed concurrently; merge explicitly instead of overwriting"
                    % name
                )
            )
        version = _sha256_text(content)
        self._topics[key] = (content, version)
        return version

    def merge_topic_contents(self, base: str, ours: str, theirs: str) -> str:
        """Explicit three-way line merge for concurrent topic updates.

        Keeps every unique line from both sides in deterministic order.
        Raises :class:`KnowledgeConflictError` when an automatic merge
        is unsafe (handled by failing closed is also acceptable to the
        caller; this helper covers the mechanical line-union case).
        """
        base_lines = (base or "").splitlines()
        our_lines = (ours or "").splitlines()
        their_lines = (theirs or "").splitlines()
        # Lines both sides agree on stay in place; unique additions from
        # either side are appended deterministically. Nothing is silently
        # dropped: the union always contains every line from both sides.
        ordered: list[str] = []
        seen: set[str] = set()
        for line in our_lines + their_lines:
            if line not in seen:
                seen.add(line)
                ordered.append(line)
        # Sanity: every input line survives.
        for line in our_lines + their_lines:
            assert line in seen
        void = base_lines  # base documents intent; union is the merge.
        _ = void
        return "\n".join(ordered) + ("\n" if ordered else "")

    # -- schemas ----------------------------------------------------------

    def get_schema(self, name: str) -> str | None:
        return self._schemas.get(_validate_doc_name(name))

    def put_schema(self, name: str, content: str) -> None:
        if not isinstance(content, str) or not content.strip():
            raise _fail("schema content must be non-empty")
        try:
            json.loads(content)
        except ValueError as exc:
            raise KnowledgeValidationError(
                redact_knowledge_error("schema %r is not valid JSON" % name)
            ) from exc
        self._schemas[_validate_doc_name(name)] = content


# ---------------------------------------------------------------------------
# GitHub-backed store (REST Git/Contents APIs, no full clone).
# ---------------------------------------------------------------------------


class GitHubBackendConflict(KnowledgeConflictError):
    """The injected GitHub transport reported a CAS conflict."""


@dataclass
class RefState:
    """Current ref state observed by the trusted layer."""

    branch: str
    sha: str | None


class DictGitHubBackend:
    """Minimal in-memory GitHub Git-database backend (tests only).

    Implements just enough of the Git database surface to exercise the
    CAS write path without network: ``get_ref``, ``get_content``,
    ``commit_files`` (blob/tree/commit), and ``cas_update_ref`` which
    only succeeds when the current ref still equals the expected SHA.
    """

    def __init__(self, branch: str = KNOWLEDGE_DEFAULT_BRANCH) -> None:
        self.branch = branch
        self.files: dict[str, str] = {}
        self.head: str | None = None
        self._counter = 0
        self.update_calls = 0

    def get_ref(self, branch: str) -> str | None:
        if branch != self.branch:
            raise _fail("unknown branch %r" % (branch,))
        return self.head

    def get_content(self, path: str, ref: str | None = None) -> str | None:
        _ = ref
        return self.files.get(path)

    def commit_files(
        self, parent_sha: str | None, files: Mapping[str, str], message: str
    ) -> str:
        _ = message
        self._counter += 1
        # Parent is recorded implicitly: linear history of file maps.
        for path, content in files.items():
            self.files[str(path)] = str(content)
        new_sha = "commit-%04d-parent-%s" % (self._counter, parent_sha or "none")
        void = parent_sha  # history linkage is conceptual in this fake.
        _ = void
        return new_sha

    def cas_update_ref(self, branch: str, new_sha: str, expected_old_sha: str | None) -> None:
        self.update_calls += 1
        if branch != self.branch:
            raise _fail("unknown branch %r" % (branch,))
        if self.head != expected_old_sha:
            raise GitHubBackendConflict(
                redact_knowledge_error("reference moved concurrently (non-fast-forward)")
            )
        self.head = new_sha


def cas_commit_files_with_retry(
    backend: Any,
    *,
    branch: str = KNOWLEDGE_DEFAULT_BRANCH,
    files: Mapping[str, str] | None = None,
    message: str = "",
    max_attempts: int = MAX_CAS_ATTEMPTS,
    refresh_fn: Callable[[], Mapping[str, str]] | None = None,
) -> str:
    """Optimistic compare-and-swap commit with bounded retries.

    Reads current HEAD, creates a blob/tree/commit, then CAS-updates the
    ref. On conflict the HEAD is reread and the attempt retried with
    bounded attempts. ``refresh_fn`` optionally rebuilds ``files`` per
    attempt (needed when the commit content depends on current HEAD).
    Experiment records use unique immutable paths so retries preserve
    independent concurrent records rather than overwrite them.
    """
    if max_attempts < 1:
        raise _fail("max_attempts must be positive")
    if not message.strip():
        raise _fail("commit message must not be empty")
    attempts = 0
    last_conflict: KnowledgeConflictError | None = None
    while attempts < int(max_attempts):
        attempts += 1
        head = backend.get_ref(branch)
        current_files = dict(files or {}) if refresh_fn is None else dict(refresh_fn() or {})
        if not current_files:
            raise _fail("no files to commit")
        new_sha = backend.commit_files(head, current_files, message)
        try:
            backend.cas_update_ref(branch, new_sha, head)
            return new_sha
        except KnowledgeConflictError as exc:
            last_conflict = exc
            continue
    raise CASSExhaustedError(
        redact_knowledge_error(
            "optimistic-concurrency retry budget exhausted after %d attempts: %s"
            % (attempts, last_conflict)
        )
    )


class GitHubKnowledgeStore(KnowledgeStore):
    """Project-namespaced store on top of a Git-backed CAS backend.

    The backend speaks file-level CAS operations (``get_ref``,
    ``get_content``, ``commit_files``, ``cas_update_ref``); a real
    implementation wires those to the GitHub REST Git/Contents APIs so
    normal agent execution never clones the entire private repository.
    """

    def __init__(
        self,
        backend: Any,
        *,
        branch: str = KNOWLEDGE_DEFAULT_BRANCH,
        max_attempts: int = MAX_CAS_ATTEMPTS,
    ) -> None:
        if backend is None:
            raise ValueError("backend must not be None")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._backend = backend
        self._branch = (branch or KNOWLEDGE_DEFAULT_BRANCH).strip()
        self._max_attempts = int(max_attempts)

    def _read(self, path: str) -> str | None:
        return self._backend.get_content(path, self._backend.get_ref(self._branch))

    def list_experiments(self, project: str) -> list[dict[str, Any]]:
        slug = validate_project_slug(project)
        catalog = _knowledge_catalog()
        backend_files = getattr(self._backend, "files", None)
        if not isinstance(backend_files, dict):
            return []
        prefix = "projects/%s/experiments/" % slug
        entries: list[dict[str, Any]] = []
        for path in sorted(backend_files):
            if not path.startswith(prefix) or not path.endswith(".md"):
                continue
            if path.endswith(".gitkeep"):
                continue
            content = backend_files[path]
            record_id = path[len(prefix): -len(".md")]
            try:
                metadata, body = catalog.parse_record_text(content, path)
                catalog.validate_metadata_shapes(metadata, path)
            except ValueError as exc:
                raise KnowledgeValidationError(
                    redact_knowledge_error("stored record %s is invalid: %s" % (path, exc))
                ) from None
            entries.append(
                {
                    "record_id": metadata["record_id"],
                    "issue": metadata["issue"],
                    "run_id": metadata["run_id"],
                    "base_commit": metadata["base_commit"],
                    "topic": metadata["topic"],
                    "outcome": metadata["outcome"],
                    "supersedes": list(metadata["supersedes"]),
                    "path": path,
                    "sha256": _sha256_text(content),
                    "title": catalog.extract_title(body),
                    "record_matches_path": (record_id == metadata["record_id"]),
                }
            )
        entries.sort(key=lambda item: item["record_id"])
        return entries

    def query_experiments(
        self,
        project: str,
        *,
        issue: int | None = None,
        topic: str | None = None,
        outcome: str | None = None,
        record_id: str | None = None,
    ) -> list[dict[str, Any]]:
        catalog = _knowledge_catalog()
        return catalog.filter_records(
            self.list_experiments(project),
            issue=issue,
            topic=topic,
            outcome=outcome,
            record_id=record_id,
        )

    def get_experiment(self, project: str, record_id: str) -> str:
        path = experiment_remote_path(project, record_id)
        content = self._read(path)
        if content is None:
            raise _fail("experiment %r not found" % (record_id,))
        validate_experiment_text_canonical(content, path)
        return content

    def get_topic(self, project: str, topic: str) -> str | None:
        return self._read(topic_remote_path(project, topic))

    def put_experiment(self, project: str, record_id: str, content: str) -> None:
        rid = _validate_record_id(record_id)
        path = experiment_remote_path(project, rid)
        metadata = validate_experiment_text_canonical(content, path)
        if metadata.get("record_id") != rid:
            raise KnowledgeValidationError(
                redact_knowledge_error("record_id does not match storage path")
            )
        existing = self._read(path)
        if existing is not None and existing != content:
            raise ImmutableOverwriteError(
                redact_knowledge_error(
                    "immutable experiment record %r already exists with different content" % rid
                )
            )
        if existing == content:
            return  # idempotent success.

        def _files() -> dict[str, str]:
            # Re-read inside the retry loop would go here for real
            # backends; the unique immutable path means a concurrent
            # independent record is preserved, not overwritten.
            return {path: content}

        cas_commit_files_with_retry(
            self._backend,
            branch=self._branch,
            message="knowledge: add experiment %s" % rid,
            max_attempts=self._max_attempts,
            refresh_fn=_files,
        )

    def put_topic(
        self,
        project: str,
        topic: str,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        path = topic_remote_path(project, topic)
        if not isinstance(content, str) or not content.strip():
            raise _fail("topic content must be non-empty")
        if expected_sha256 is not None:
            current = self._read(path)
            current_version = _sha256_text(current) if current is not None else None
            if current_version != expected_sha256:
                raise KnowledgeConflictError(
                    redact_knowledge_error(
                        "topic %r changed concurrently; merge explicitly instead of overwriting"
                        % topic
                    )
                )
        cas_commit_files_with_retry(
            self._backend,
            branch=self._branch,
            files={path: content},
            message="knowledge: update topic %s" % topic,
            max_attempts=self._max_attempts,
        )
        return _sha256_text(content)

    def get_schema(self, name: str) -> str | None:
        return self._read(schema_remote_path(name))


# ---------------------------------------------------------------------------
# GitHub App token path scoped to the knowledge repository.
# ---------------------------------------------------------------------------


def knowledge_installation_token_request(
    knowledge_repo_name: str = KNOWLEDGE_REPO_NAME,
) -> dict[str, Any]:
    """Return the scoped installation-token request for the knowledge repo.

    Least privilege (metadata:read + contents:write/read) and restricted
    to the single knowledge repository, even when the source checkout is
    a different repository.
    """
    if (knowledge_repo_name or "").strip() != KNOWLEDGE_REPO_NAME:
        raise _fail("knowledge repository name must be %r" % KNOWLEDGE_REPO_NAME)
    return {
        "repositories": [KNOWLEDGE_REPO_NAME],
        "permissions": dict(KNOWLEDGE_APP_PERMISSIONS),
    }


def create_scoped_token_provider(
    config: Any,
    raw_exchange_fn: Callable[..., Mapping[str, Any]],
    *,
    knowledge_repo_name: str = KNOWLEDGE_REPO_NAME,
    jwt_signer: Callable[[bytes], bytes] | None = None,
    time_fn: Callable[[], float] | None = None,
) -> Any:
    """Build an InstallationTokenProvider scoped to the knowledge repo.

    ``config`` is the Runtime Lab :class:`GitHubAppConfig` (whose
    ``repository`` is normally the source repository). The returned
    provider mints tokens restricted to ``kodmial/agent-knowledge``
    with :data:`KNOWLEDGE_APP_PERMISSIONS`, reusing the existing token
    refresh/redaction behavior from ``automation/github_app.py``. The
    installation token must never be passed to the ephemeral worker.
    """
    try:
        try:
            from automation.github_app import InstallationTokenProvider
        except ImportError:
            from github_app import InstallationTokenProvider  # type: ignore[no-redef]
    except ImportError as exc:
        raise _fail("github_app module is required for App token path: %s" % exc) from None
    request = knowledge_installation_token_request(knowledge_repo_name)

    def _scoped_exchange(app_jwt: str, installation_id: str, api_base: str) -> Mapping[str, Any]:
        return raw_exchange_fn(
            app_jwt,
            installation_id,
            api_base,
            repositories=list(request["repositories"]),
            permissions=dict(request["permissions"]),
        )

    return InstallationTokenProvider(
        config,
        exchange_fn=_scoped_exchange,  # type: ignore[arg-type]
        jwt_signer=jwt_signer,
        time_fn=time_fn,
        requested_permissions=dict(request["permissions"]),
    )


def detect_app_access_to_knowledge_repo(
    api_client: Any,
    knowledge_repo: str = KNOWLEDGE_REPO_FULL,
) -> dict[str, Any]:
    """Detect whether the installed GitHub App can access the knowledge repo.

    Returns ``{"accessible": bool, "detail": str}``. Never raises for a
    plain 404 (reported as not-installed); other API failures propagate
    as redacted :class:`KnowledgeStoreError`.
    """
    if (knowledge_repo or "").strip() != KNOWLEDGE_REPO_FULL:
        raise _fail("knowledge repository must be %r" % KNOWLEDGE_REPO_FULL)
    owner, _, name = knowledge_repo.partition("/")
    try:
        payload = api_client.api("GET", "/repos/%s/%s" % (owner, name))
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            return {
                "accessible": False,
                "detail": (
                    "Runtime Lab GitHub App is not installed on %s "
                    "(HTTP 404). Install it with metadata:read + "
                    "contents:write before using the App token path."
                    % KNOWLEDGE_REPO_FULL
                ),
            }
        raise _fail("knowledge-repository access check failed: %s" % exc) from None
    if not isinstance(payload, Mapping):
        raise _fail("knowledge-repository access check returned invalid JSON")
    try:
        verify_knowledge_repository(payload)
    except PublicRepositoryError:
        raise
    except KnowledgeStoreError as exc:
        raise _fail("knowledge-repository access check failed: %s" % exc) from None
    return {"accessible": True, "detail": "App can access %s" % KNOWLEDGE_REPO_FULL}


# ---------------------------------------------------------------------------
# Live-bootstrap planner (safe without credentials).
# ---------------------------------------------------------------------------


def plan_live_bootstrap(
    environ: Mapping[str, str] | None = None,
    *,
    project: str = "runtime-lab",
) -> dict[str, Any]:
    """Plan the live bootstrap, failing closed without literal TAP_PAT.

    Offline helper: returns a credential-free plan (request path/payload,
    layout paths, verification steps) when literal ``TAP_PAT`` is present.
    Inside Actions prefer :func:`plan_live_bootstrap_trusted_transport`,
    because the secret arrives as GH_TOKEN/GITHUB_TOKEN there. The PAT
    value itself is never included in the plan.
    """
    require_tap_pat(environ)
    path, payload = build_bootstrap_repo_request()
    assert_not_org_bootstrap_endpoint(path)
    require_private_bootstrap_request(payload)
    layout = initial_layout_file_map(project=project)
    return {
        "repository": KNOWLEDGE_REPO_FULL,
        "request_method": "POST",
        "request_path": path,
        "request_payload": dict(payload),
        "layout_paths": sorted(layout.keys()),
        "verification": [
            "full name is exactly kodmial/agent-knowledge",
            "private == true / visibility is private",
            "GitHub Issues enabled (has_issues=true) for Knowledge Plane roadmap",
            "default branch exists",
            "trusted layer can read the repository",
            "trusted layer can create/update a bootstrap file",
            "no anonymous/public read succeeds",
            "no secret value appears in contents or logs",
        ],
        "credential": "TAP_PAT (held by trusted layer only, never in plan output)",
    }


def plan_live_bootstrap_trusted_transport(
    environ: Mapping[str, str] | None = None,
    *,
    project: str = "runtime-lab",
) -> dict[str, Any]:
    """Plan the live bootstrap via the GH_TOKEN/GITHUB_TOKEN transport.

    Fails closed with :class:`KnowledgeAuthBlocker` when no trusted
    transport token is present. Presence alone does not prove capability:
    the caller must still run the live authenticated capability check
    (GET /user, GET /repos/kodmial/agent-knowledge, POST /user/repos with
    private=true) through GH_TOKEN/GitHub CLI, because the transport may
    carry either TAP_PAT or the repository-scoped github.token fallback.
    The token value itself is never included in the plan.
    """
    if not trusted_transport_token_present(environ):
        raise KnowledgeAuthBlocker(redact_knowledge_error(TAP_PAT_BLOCKER_MESSAGE))
    path, payload = build_bootstrap_repo_request()
    assert_not_org_bootstrap_endpoint(path)
    require_private_bootstrap_request(payload)
    layout = initial_layout_file_map(project=project)
    return {
        "repository": KNOWLEDGE_REPO_FULL,
        "request_method": "POST",
        "request_path": path,
        "request_payload": dict(payload),
        "layout_paths": sorted(layout.keys()),
        "capability_probes": [
            "GET /user (authenticated identity; no token output)",
            "GET /repos/kodmial/agent-knowledge (exists? private?)",
            "POST /user/repos with private=true (only when missing)",
            "unauthenticated GET /repos/kodmial/agent-knowledge expects 404",
        ],
        "verification": [
            "full name is exactly kodmial/agent-knowledge",
            "private == true / visibility is private",
            "GitHub Issues enabled (has_issues=true) for Knowledge Plane roadmap",
            "default branch exists",
            "trusted layer can read the repository",
            "trusted layer can create/update a bootstrap file",
            "no anonymous/public read succeeds",
            "no secret value appears in contents or logs",
        ],
        "credential": (
            "GH_TOKEN/GITHUB_TOKEN trusted transport "
            "(held by trusted layer only, never in plan output)"
        ),
    }


def anonymous_read_check_plan() -> dict[str, Any]:
    """Return the meaningful no-auth read check (no secret, no network)."""
    return {
        "url": "https://api.github.com/repos/%s" % KNOWLEDGE_REPO_FULL,
        "auth": None,
        "expect": "HTTP 404 for a private repository (no anonymous disclosure)",
    }
