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
