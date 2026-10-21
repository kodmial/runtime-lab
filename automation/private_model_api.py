#!/usr/bin/env python3
"""Bounded, privacy-safe GitHub REST access for private model rollout.

Only the Parent workflow consumes this module. Private repository names are
never included in errors or diagnostics; callers receive an HTTP status (or a
generic transport failure) instead. Transient 429/5xx responses are retried a
bounded number of times, honoring Retry-After / X-RateLimit-Reset before the
retry. Permission failures such as 401/403/404 are never retried.
"""
from __future__ import annotations

import argparse
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
TRANSIENT = frozenset({429, 500, 502, 503, 504})
DEFAULT_BACKOFF = (5, 15, 45)
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 180
DEFAULT_TIMEOUT = 20
DEFAULT_RATE_THRESHOLD = 80
DEFAULT_RATE_WAIT = 1500


class GitHubApiError(RuntimeError):
    """Safe API failure carrying only an optional HTTP status."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _header(headers, name: str) -> str:
    if not headers:
        return ""
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def retry_delay(headers, attempt: int, *, now: int | None = None) -> int:
    """Return a bounded retry delay from headers or exponential backoff."""
    current = int(time.time()) if now is None else int(now)
    delay: int | None = None
    retry_after = _header(headers, "Retry-After").strip()
    if retry_after.isdigit():
        delay = int(retry_after)
    elif retry_after:
        try:
            parsed = parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = int(parsed.timestamp()) - current
        except (TypeError, ValueError, OverflowError):
            delay = None
    if delay is None:
        reset = _header(headers, "X-RateLimit-Reset").strip()
        if reset.isdigit():
            delay = int(reset) - current + 1
    if delay is None:
        delay = DEFAULT_BACKOFF[min(attempt, len(DEFAULT_BACKOFF) - 1)]
    delay = max(0, delay)
    if delay > MAX_RETRY_WAIT:
        raise GitHubApiError(
            "GitHub API retry window exceeds the bounded wait", status=429
        )
    return delay


def client_from_env(**kwargs) -> "GitHubClient":
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise GitHubApiError("Trusted GitHub token is unavailable", status=401)
    return GitHubClient(token, **kwargs)


class GitHubClient:
    """Small REST client with bounded, status-aware retries."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = API_ROOT,
        request_fn=None,
        sleep_fn=time.sleep,
        clock_fn=time.time,
        timeout: int = DEFAULT_TIMEOUT,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not token:
            raise GitHubApiError("Trusted GitHub token is unavailable", status=401)
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._request_fn = request_fn or self._urlopen
        self._sleep = sleep_fn
        self._clock = clock_fn
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))

    def _urlopen(self, method, url, headers, data, timeout):
        request = urllib.request.Request(
            url, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, dict(reply.headers.items()), reply.read()
        except urllib.error.HTTPError as error:
            headers_out = dict(error.headers.items()) if error.headers else {}
            return error.code, headers_out, error.read()

    def request(
        self,
        method: str,
        path: str,
        body=None,
        *,
        allowed=(200,),
        allow_missing: bool = False,
    ):
        data = None if body is None else json.dumps(body).encode()
        url = self.api_base + "/" + path.lstrip("/")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Content-Type": "application/json",
        }
        for attempt in range(self.max_attempts):
            try:
                status, response_headers, raw = self._request_fn(
                    method, url, headers, data, self.timeout
                )
            except (OSError, TimeoutError):
                if attempt + 1 >= self.max_attempts:
                    raise GitHubApiError(
                        "GitHub API temporarily unavailable"
                    ) from None
                self._sleep(retry_delay({}, attempt))
                continue
            if status in TRANSIENT:
                if attempt + 1 >= self.max_attempts:
                    raise GitHubApiError(
                        "GitHub API transient failure after bounded retries",
                        status=status,
                    )
                self._sleep(retry_delay(response_headers, attempt))
                continue
            if allow_missing and status == 404:
                return None
            if status not in allowed:
                # Deliberately omit the request path: it may name a private repo.
                raise GitHubApiError(
                    "GitHub API request failed with status %d" % status,
                    status=status,
                )
            if not raw:
                return None
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return json.loads(raw)
            except (UnicodeDecodeError, ValueError):
                raise GitHubApiError(
                    "GitHub API returned invalid JSON", status=status
                ) from None
        raise GitHubApiError("GitHub API retry budget exhausted", status=429)

    def budget(self) -> tuple[int, int, int]:
        data = self.request("GET", "rate_limit")
        core = (data or {}).get("resources", {}).get("core", {})
        try:
            remaining = int(core.get("remaining"))
            limit = int(core.get("limit"))
            reset = int(core.get("reset"))
        except (TypeError, ValueError):
            raise GitHubApiError("GitHub API rate status unavailable") from None
        return remaining, limit, reset

    def rate_budget(
        self,
        *,
        threshold: int = DEFAULT_RATE_THRESHOLD,
        max_wait: int = DEFAULT_RATE_WAIT,
    ) -> tuple[int, int]:
        remaining, limit, reset = self.budget()
        if remaining < threshold:
            seconds = max(0, reset - int(self._clock()) + 10)
            if seconds > max_wait:
                raise GitHubApiError(
                    "GitHub API reset exceeds the bounded wait", status=429
                )
            if seconds:
                self._sleep(seconds)
        return remaining, limit

    def wait_for_reset(self, *, max_wait: int = MAX_RETRY_WAIT) -> int:
        _, _, reset = self.budget()
        seconds = max(0, reset - int(self._clock()) + 1)
        if seconds > max_wait:
            raise GitHubApiError(
                "GitHub API reset exceeds the bounded wait", status=429
            )
        if seconds:
            self._sleep(seconds)
        return seconds


def _repository_argument(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value or ""):
        raise GitHubApiError("Invalid repository identity", status=400)
    return value


def metadata(client: GitHubClient, repository: str) -> bool:
    data = client.request("GET", "repos/" + _repository_argument(repository))
    return bool((data or {}).get("private"))


def has_marker(client: GitHubClient, repository: str) -> bool:
    path = (
        "repos/"
        + _repository_argument(repository)
        + "/contents/.github/workflows/continuum-model-rollout.yml"
    )
    return client.request("GET", path, allow_missing=True) is not None


def ensure_variable(client: GitHubClient, repository: str, pool: str) -> None:
    repo = _repository_argument(repository)
    endpoint = "repos/" + repo + "/actions/variables/OPENCODE_MODELS"
    current = client.request("GET", endpoint, allow_missing=True)
    if isinstance(current, dict) and current.get("value") == pool:
        return
    if current is None:
        client.request(
            "POST",
            "repos/" + repo + "/actions/variables",
            {"name": "OPENCODE_MODELS", "value": pool},
            allowed=(201, 204),
        )
    else:
        client.request(
            "PATCH",
            endpoint,
            {"name": "OPENCODE_MODELS", "value": pool},
            allowed=(200, 204),
        )
    actual = client.request("GET", endpoint)
    if not isinstance(actual, dict) or actual.get("value") != pool:
        raise GitHubApiError("Child model policy mismatch")


def repository_variables(client: GitHubClient, repository: str) -> dict[str, str]:
    repo = _repository_argument(repository)
    values: dict[str, str] = {}
    page = 1
    while True:
        data = client.request(
            "GET",
            "repos/%s/actions/variables?per_page=100&page=%d" % (repo, page),
        )
        entries = (data or {}).get("variables")
        if not isinstance(entries, list):
            raise GitHubApiError("Repository variables payload is invalid")
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                values[entry["name"]] = str(entry.get("value", ""))
        total_count = (data or {}).get("total_count")
        if total_count is None:
            raise GitHubApiError("Repository variables payload missing total_count")
        try:
            total = int(total_count)
        except (TypeError, ValueError):
            raise GitHubApiError("Repository variables payload is invalid") from None
        if len(entries) < 100 or page * 100 >= total:
            return values
        page += 1


def _owner_repositories(client: GitHubClient):
    page = 1
    while True:
        data = client.request(
            "GET", "user/repos?affiliation=owner&per_page=100&page=%d" % page
        )
        if not isinstance(data, list):
            raise GitHubApiError("Repository inventory payload is invalid")
        for item in data:
            if isinstance(item, dict):
                yield item
        if len(data) < 100:
            return
        page += 1


def _plan_repositories(path: str) -> list[str]:
    """Read repository names from the canonical resolver plan, never logging them."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise GitHubApiError("Canonical resolver plan is invalid") from None
    if not isinstance(data, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("repository"), str)
        for item in data
    ):
        raise GitHubApiError("Canonical resolver plan is invalid")
    return [item["repository"] for item in data]


def child_repository_map(
    client: GitHubClient,
    parent: str,
    *,
    plan: list[str] | None = None,
) -> dict[str, str] | None:
    """Map authorized child IDs to private repositories without logging names.

    ``plan`` optionally names the authorized repositories already proven by the
    canonical Continuum resolver (``delegation_repository.sh plan``). When it
    is given, the owner-wide repository scan is skipped entirely and only
    resolver-named repositories consume Variables reads, so the admission proof
    costs one variables read per authorized candidate instead of one per
    private repository owned by the same account.

    Returns None when the authorized inventory cannot be proven from installed
    repository variables alone; the caller must fail closed rather than
    guessing or weakening admission.
    """
    parent = _repository_argument(parent)
    parent_values = repository_variables(client, parent)
    if parent_values.get("CONTINUUM_ROLE") != "parent":
        raise GitHubApiError("Parent role is not reconciled")
    try:
        allowed = json.loads(parent_values.get("CONTINUUM_CHILDREN", ""))
    except (TypeError, ValueError):
        raise GitHubApiError("Parent child allowlist is invalid") from None
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(item, str) or not item for item in allowed)
        or len(set(allowed)) != len(allowed)
    ):
        raise GitHubApiError("Parent child allowlist is invalid")
    owner = parent.split("/")[0].lower()
    allowed_ids = set(allowed)

    def candidates():
        if plan is None:
            for item in _owner_repositories(client):
                repository = item.get("full_name")
                if (
                    not isinstance(repository, str)
                    or item.get("private") is not True
                    or repository.lower() == parent.lower()
                    or repository.split("/")[0].lower() != owner
                ):
                    continue
                yield repository
            return
        emitted: set[str] = set()
        for name in plan:
            if not isinstance(name, str):
                continue
            key = name.lower()
            if key in emitted:
                continue
            emitted.add(key)
            if name.lower() == parent.lower() or name.split("/")[0].lower() != owner:
                continue
            yield name

    mapping: dict[str, str] = {}
    seen: set[str] = set()
    for repository in candidates():
        values = repository_variables(client, repository)
        if (
            values.get("CONTINUUM_ROLE") != "child"
            or values.get("CONTINUUM_PARENT", "").lower() != parent.lower()
        ):
            continue
        child_id = values.get("CONTINUUM_CHILD_ID", "")
        if child_id not in allowed:
            continue
        if child_id in mapping or repository.lower() in seen:
            raise GitHubApiError("Child relationship is ambiguous")
        mapping[child_id] = repository
        seen.add(repository.lower())
        if set(mapping) == allowed_ids:
            # The parent allowlist is authoritative; stop spending Variables
            # API reads once every authorized Child has a unique repository.
            break
    if set(mapping) != allowed_ids:
        return None
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    budget = subparsers.add_parser("rate-budget")
    budget.add_argument("--threshold", type=int, default=DEFAULT_RATE_THRESHOLD)
    budget.add_argument("--max-wait", type=int, default=DEFAULT_RATE_WAIT)

    metadata_parser = subparsers.add_parser("metadata")
    metadata_parser.add_argument("repository")

    marker_parser = subparsers.add_parser("marker")
    marker_parser.add_argument("repository")

    variable_parser = subparsers.add_parser("ensure-variable")
    variable_parser.add_argument("repository")
    variable_parser.add_argument("pool")

    child_map_parser = subparsers.add_parser("child-map")
    child_map_parser.add_argument(
        "--plan",
        metavar="PATH",
        help=(
            "Canonical Continuum resolver plan JSON; only its repositories "
            "are checked instead of scanning the owner-wide inventory."
        ),
    )

    args = parser.parse_args(argv)
    try:
        client = client_from_env()
        if args.operation == "rate-budget":
            remaining, _ = client.rate_budget(
                threshold=args.threshold, max_wait=args.max_wait
            )
            print("GitHub API core request budget remaining: %d." % remaining)
            return 0
        if args.operation == "metadata":
            print("true" if metadata(client, args.repository) else "false")
            return 0
        if args.operation == "marker":
            return 0 if has_marker(client, args.repository) else 1
        if args.operation == "ensure-variable":
            ensure_variable(client, args.repository, args.pool)
            return 0
        if args.operation == "child-map":
            parent = os.environ.get("GITHUB_REPOSITORY", "")
            plan = _plan_repositories(args.plan) if args.plan else None
            mapping = child_repository_map(client, parent, plan=plan)
            if mapping is None:
                print(
                    "Authorized child map is incomplete; failing closed.",
                    file=sys.stderr,
                )
                return 3
            print(json.dumps(mapping, sort_keys=True, separators=(",", ":")))
            return 0
    except GitHubApiError as error:
        status = "unavailable" if error.status is None else str(error.status)
        print(
            "::error::GitHub API operation failed (status %s)." % status,
            file=sys.stderr,
        )
        return 2
    parser.error("Invalid operation")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
