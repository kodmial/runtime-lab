"""Tests for GitHub App authentication and Render write-back (issue #11).

Covers the Definition of Done without network access or secrets:
- the Render controller obtains a short-lived installation token and
  performs GitHub reads/writes without a workflow GITHUB_TOKEN;
- a worker result is committed and turned into a PR entirely from Render
  (AppWritebackClient + materialize_result reuse from #5);
- no long-lived PAT is required by the final design;
- token expiry/refresh is handled (cache skew + one 401 retry);
- worker cleanup still runs when GitHub write-back fails;
- auth failure, expired token, duplicate PR and no-change cases;
- no OpenCode GitHub Actions job is required on this path.
"""

import base64
import json
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_app import (  # noqa: E402
    OPTIONAL_APP_PERMISSIONS,
    REQUIRED_APP_PERMISSIONS,
    AppWritebackClient,
    GitHubApiClient,
    GitHubAppConfig,
    GitHubAppSnapshotProvider,
    GitHubAuthError,
    InstallationTokenProvider,
    build_app_jwt,
    decode_jwt_payload_unsigned,
    is_app_configured,
    load_app_config_from_env,
    normalize_private_key,
    parse_token_expiry,
    redact_secrets,
    split_repository,
)
from render_controller import (  # noqa: E402
    Controller,
    DeliveryStore,
    StaticSnapshotProvider,
    sign_webhook_body,
)
from render_lifecycle import PUBLIC_REPO_URL  # noqa: E402
from result_materialize import materialize_result  # noqa: E402

SECRET = "test-webhook-secret-11"
BASE_SHA = "abc123def456abc123def456abc123def456abcd"
FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nZmFrZXktbWF0ZXJpYWw=\n-----END PRIVATE KEY-----"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _future_expiry(minutes: int = 55) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _change(path, change_type, content=None):
    entry = {"path": path, "change_type": change_type}
    if change_type != "deleted":
        entry["content_base64"] = _b64(content if content is not None else b"")
    return entry


def _result(**overrides):
    payload = {
        "job_id": "job-1",
        "status": "succeeded",
        "success": True,
        "summary": "done",
        "error": "",
        "metadata": {"issue_number": 5, "model": "m"},
        "issue_number": 5,
        "repository_url": PUBLIC_REPO_URL,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "executed_model": "m",
        "changes": [_change("notes.txt", "added", b"hello")],
    }
    payload.update(overrides)
    return payload


def _config(**overrides):
    values = {
        "app_id": "12345",
        "private_key_pem": FAKE_PEM,
        "installation_id": "67890",
        "repository": "owner/repo",
        "api_base": "https://api.github.com",
    }
    values.update(overrides)
    return GitHubAppConfig(**values)


# ---------------------------------------------------------------------------
# In-memory GitHub transport (no network): implements RawRequestFn.
# ---------------------------------------------------------------------------

class FakeGitHubTransport:
    """Fake GitHub REST backend for App auth/write-back tests."""

    def __init__(self, base_sha=BASE_SHA):
        self.base_sha = base_sha
        self.calls = []
        self.tokens_seen = []
        # Branches: name -> commit sha; commits: sha -> {tree, parents}.
        self.branches = {}
        self.commits = {base_sha: {"tree": "tree-base", "parents": []}}
        self.trees = {"tree-base": []}
        self.blobs = {}
        self._counter = 0
        self.prs = []
        self.pr_counter = 0
        self.issues = {
            5: {"number": 5, "title": "Fix it", "body": "details",
                "state": "open", "labels": [{"name": "priority:p0"}]},
        }
        self.blocked_by = {}  # issue -> [numbers]
        self.native_available = True
        self.fail_next_with_401_paths = set()
        self.force_422_on_pr_create = False

    def _next_sha(self, prefix):
        self._counter += 1
        return "%s-%04d" % (prefix, self._counter)

    def request(self, method, url, token, body):
        self.calls.append({"method": method, "url": url, "body": body})
        self.tokens_seen.append(token)
        path = urllib.parse.urlsplit(url).path
        query = urllib.parse.urlsplit(url).query

        def located(path_key):
            return path_key in self.fail_next_with_401_paths

        # One-shot 401 simulation for the expired-token test.
        for key in list(self.fail_next_with_401_paths):
            if path.endswith(key) or key in path:
                self.fail_next_with_401_paths.discard(key)
                return 401, {"message": "Bad credentials"}

        if path.startswith("/repos/kodmial/runtime-lab/issues/") and path.endswith(
                ("/dependencies/blocked_by", "/blocked_by")):
            if not self.native_available:
                return 404, {"message": "Not Found"}
            parts = path.split("/")
            number = int(parts[5])
            deps = self.blocked_by.get(number, [])
            return 200, [{"number": n} for n in deps]

        if path.startswith("/repos/kodmial/runtime-lab/issues/") and "/labels" in path:
            parts = path.split("/")
            number = int(parts[5])
            issue = self.issues.setdefault(
                number, {"number": number, "title": "", "body": "",
                         "state": "open", "labels": []})
            if method == "POST":
                names = (body or {}).get("labels", []) or []
                existing = {item["name"] for item in issue["labels"]
                            if isinstance(item, dict)}
                for name in names:
                    if name not in existing:
                        issue["labels"].append({"name": name})
                return 200, list(issue["labels"])
            if method == "DELETE":
                name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                issue["labels"] = [item for item in issue["labels"]
                                   if item.get("name") != name]
                return 204, ""

        if path.startswith("/repos/kodmial/runtime-lab/issues/"):
            number = int(path.split("/")[5])
            issue = self.issues.get(number)
            if issue is None:
                return 404, {"message": "Not Found"}
            labels = [item["name"] if isinstance(item, dict) else item
                      for item in issue.get("labels", [])]
            return 200, {"number": number, "title": issue.get("title", ""),
                         "body": issue.get("body", ""),
                         "state": issue.get("state", "open"),
                         "labels": [{"name": name} for name in labels]}

        if path == "/repos/kodmial/runtime-lab/git/ref/heads/main" or path.startswith(
                "/repos/kodmial/runtime-lab/git/ref/heads/"):
            ref = path[len("/repos/kodmial/runtime-lab/git/ref/heads/"):]
            ref = urllib.parse.unquote(ref)
            if ref == "main":
                return 200, {"object": {"sha": self.base_sha}}
            if ref in self.branches:
                return 200, {"object": {"sha": self.branches[ref]}}
            return 404, {"message": "Not Found"}

        if path.startswith("/repos/kodmial/runtime-lab/commits/"):
            return 200, {"sha": self.base_sha}

        if path == "/repos/kodmial/runtime-lab/pulls" and query.startswith("state=open"):
            return 200, [{"number": pr["number"],
                          "head": {"ref": pr["head_ref"]},
                          "state": "open"} for pr in self.prs
                         if pr.get("state") == "open"]

        if path == "/repos/kodmial/runtime-lab/git/blobs" and method == "POST":
            sha = self._next_sha("blob")
            self.blobs[sha] = (body or {}).get("content", "")
            return 201, {"sha": sha}

        if path.startswith("/repos/kodmial/runtime-lab/git/commits/") and method == "GET":
            sha = path.rsplit("/", 1)[-1]
            commit = self.commits.get(sha)
            if commit is None:
                return 404, {"message": "Not Found"}
            return 200, {"sha": sha, "tree": {"sha": commit["tree"]},
                         "parents": [{"sha": p} for p in commit["parents"]]}

        if path == "/repos/kodmial/runtime-lab/git/trees" and method == "POST":
            sha = self._next_sha("tree")
            entries = (body or {}).get("tree", []) or []
            base_tree = (body or {}).get("base_tree", "")
            # Deterministic pseudo-tree sha: identical inputs -> identical
            # output, so no-change detection works in tests.
            fingerprint = json.dumps({"base": base_tree, "entries": entries},
                                     sort_keys=True)
            import hashlib as _hashlib

            digest = _hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
            sha = "tree-%s" % digest
            self.trees[sha] = entries
            return 201, {"sha": sha}

        if path == "/repos/kodmial/runtime-lab/git/commits" and method == "POST":
            sha = self._next_sha("commit")
            self.commits[sha] = {"tree": (body or {}).get("tree", ""),
                                 "parents": (body or {}).get("parents", [])}
            return 201, {"sha": sha}

        if path == "/repos/kodmial/runtime-lab/git/refs" and method == "POST":
            ref = (body or {}).get("ref", "")
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            self.branches[branch] = (body or {}).get("sha", "")
            return 201, {"ref": ref}

        if path.startswith("/repos/kodmial/runtime-lab/git/refs/heads/") and method == "PATCH":
            branch = urllib.parse.unquote(path[len("/repos/kodmial/runtime-lab/git/refs/heads/"):])
            self.branches[branch] = (body or {}).get("sha", "")
            return 200, {"ref": "refs/heads/%s" % branch}

        if path == "/repos/kodmial/runtime-lab/pulls" and method == "POST":
            if self.force_422_on_pr_create:
                return 422, {"message": "A pull request already exists"}
            self.pr_counter += 1
            pr = {"number": self.pr_counter,
                  "head_ref": (body or {}).get("head", ""),
                  "state": "open", "title": (body or {}).get("title", "")}
            self.prs.append(pr)
            return 201, {"number": self.pr_counter}

        raise AssertionError("unexpected fake request %s %s" % (method, url))


def _provider(transport=None, **overrides):
    calls = {"count": 0}

    def exchange(jwt, installation_id, api_base):
        calls["count"] += 1
        assert jwt.count(".") == 2  # a real JWT shape even with fake signer
        return {"token": "ghs-test-token-%d" % calls["count"],
                "expires_at": _future_expiry()}

    provider = InstallationTokenProvider(
        _config(**overrides),
        exchange_fn=exchange,
        jwt_signer=lambda data: b"fake-signature",
    )
    provider.exchange_calls = calls  # type: ignore[attr-defined]
    return provider


def _client(transport, provider=None, **overrides):
    provider = provider or _provider(transport, **overrides)
    api = GitHubApiClient(provider, api_base="https://api.github.com",
                          repository="kodmial/runtime-lab",
                          request_fn=transport.request)
    return api, provider


# ---------------------------------------------------------------------------
# Auth: JWT shape, token cache/expiry, 401 refresh, redaction, no PAT.
# ---------------------------------------------------------------------------

def test_app_jwt_has_expected_claims_with_fake_signer():
    token = build_app_jwt(app_id="12345", private_key_pem=FAKE_PEM,
                          now=1_700_000_000,
                          signer=lambda data: b"sig")
    payload = decode_jwt_payload_unsigned(token)
    assert payload["iss"] == "12345"
    assert payload["exp"] - payload["iat"] == 600 + 60
    assert token.count(".") == 2


def test_installation_token_is_cached_until_expiry():
    provider = _provider()
    first = provider.get_token()
    second = provider.get_token()
    assert first == second
    assert provider.exchange_calls["count"] == 1  # type: ignore[attr-defined]


def test_expired_token_triggers_refresh():
    now = [1_700_000_000.0]

    def exchange(jwt, installation_id, api_base):
        return {"token": "ghs-t-%d" % int(now[0]),
                "expires_at": datetime.fromtimestamp(
                    now[0] + 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    provider = InstallationTokenProvider(
        _config(), exchange_fn=exchange,
        jwt_signer=lambda data: b"s",
        time_fn=lambda: now[0])
    first = provider.get_token()
    assert provider.get_token() == first  # cached
    now[0] += 3700  # past expiry + skew
    assert provider.get_token(force_refresh=False) != first


def test_api_client_retries_once_after_401_expired_token():
    transport = FakeGitHubTransport()
    provider = _provider(transport)
    api, _ = _client(transport, provider)
    transport.fail_next_with_401_paths.add("/issues/5")
    issue = api.get_issue(5)
    assert issue["number"] == 5
    # Initial exchange + one refresh after the 401.
    assert provider.exchange_calls["count"] == 2  # type: ignore[attr-defined]
    assert transport.tokens_seen[0] != transport.tokens_seen[-1]


def test_auth_failures_never_leak_secrets():
    secret_key = FAKE_PEM
    try:
        raise GitHubAuthError("boom %s ghs_abc123 Bearer xyz" % secret_key)
    except GitHubAuthError as exc:
        redacted = redact_secrets(str(exc))
    assert "PRIVATE KEY" not in redacted
    assert "ghs_abc123" not in redacted
    assert "Bearer" not in redacted or "[redacted]" in redacted

    def failing_exchange(jwt, installation_id, api_base):
        raise GitHubAuthError("exchange failed for %s" % FAKE_PEM)

    provider = InstallationTokenProvider(
        _config(), exchange_fn=failing_exchange,
        jwt_signer=lambda data: b"s")
    with pytest.raises(GitHubAuthError) as excinfo:
        provider.get_token()
    assert "PRIVATE KEY" not in str(excinfo.value)


def test_final_design_requires_no_long_lived_pat(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-never-be-used")
    monkeypatch.setenv("GH_TOKEN", "ghp-also-unused")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)
    assert is_app_configured() is False
    with pytest.raises(GitHubAuthError):
        load_app_config_from_env()
    # With App secrets present, the PAT values are still ignored.
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", FAKE_PEM)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    config = load_app_config_from_env()
    assert config.app_id == "1"
    source = Path(__file__).resolve().parents[0] / "github_app.py"
    text = source.read_text(encoding="utf-8")
    assert "GITHUB_TOKEN" in text  # only as a forbidden-name constant
    assert 'os.environ.get("GITHUB_TOKEN"' not in text
    assert 'os.environ.get("GH_TOKEN"' not in text
    assert "os.environ[\"GITHUB_TOKEN\"" not in text


def test_requested_permissions_are_least_privilege():
    assert REQUIRED_APP_PERMISSIONS == {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    assert "actions" not in REQUIRED_APP_PERMISSIONS
    assert OPTIONAL_APP_PERMISSIONS.get("actions") == "read"
    provider = _provider()
    assert provider.requested_permissions == REQUIRED_APP_PERMISSIONS


def test_private_key_normalization_and_repository_split():
    escaped = FAKE_PEM.replace("\n", "\\n")
    assert "PRIVATE KEY" in normalize_private_key(escaped)
    assert split_repository("owner/repo") == ("owner", "repo")
    with pytest.raises(ValueError):
        split_repository("not-a-repo")
    with pytest.raises(ValueError):
        normalize_private_key("not-a-key")


def test_parse_token_expiry_accepts_github_format():
    value = parse_token_expiry("2026-05-13T12:00:00Z")
    assert value > 0
    with pytest.raises(GitHubAuthError):
        parse_token_expiry("")
    with pytest.raises(GitHubAuthError):
        parse_token_expiry("not-a-date")


# ---------------------------------------------------------------------------
# Reads: issue inspection, native blocked-by, labels/reservation.
# ---------------------------------------------------------------------------

def test_issue_inspection_blocked_by_and_labels():
    transport = FakeGitHubTransport()
    transport.blocked_by[5] = [3]
    api, _ = _client(transport)
    issue = api.get_issue(5)
    assert issue["title"] == "Fix it"
    assert issue["state"] == "open"
    assert transport.blocked_by and api.get_blocked_by(5) == [3]

    transport.native_available = False
    assert api.get_blocked_by(5) == []  # unavailable endpoint degrades

    transport.native_available = True
    assert api.add_labels(5, ["automation:in-progress"]) and \
        "automation:in-progress" in api.get_issue(5)["labels"]
    assert api.remove_label(5, "automation:in-progress") is True
    assert "automation:in-progress" not in api.get_issue(5)["labels"]
    # Removing an absent label is idempotent success.
    assert api.remove_label(5, "automation:in-progress") is True


def test_snapshot_provider_uses_live_reads_with_safe_defaults():
    transport = FakeGitHubTransport()
    transport.blocked_by[5] = [3]
    api, _ = _client(transport)
    provider = GitHubAppSnapshotProvider(api)
    assert provider.open_blockers(5) == [3]
    assert provider.has_open_pr(5) is False
    assert provider.lease_valid(5) is False
    api.add_labels(5, ["automation:in-progress"])
    # Label without an open PR is a stale reservation (reclaimable).
    assert provider.lease_valid(5) is False


# ---------------------------------------------------------------------------
# Write-back reuse (#5 orchestration on the App client).
# ---------------------------------------------------------------------------

def test_app_writeback_creates_branch_and_pr_from_render():
    transport = FakeGitHubTransport()
    api, _ = _client(transport)
    client = AppWritebackClient(api, repository="kodmial/runtime-lab")
    outcome = materialize_result(_result(), client=client,
                                 unique_suffix="run-1", issue_title="Fix it")
    assert outcome.action == "created"
    assert outcome.branch == "opencode/issue5-run-1"
    assert outcome.pr_number == 1
    assert outcome.dispatched_ci is True  # recorded, no Actions write
    assert len(transport.prs) == 1
    # No workflow dispatch happened on the fake transport.
    assert all("/actions/workflows" not in call["url"]
               for call in transport.calls)


def test_duplicate_pr_is_reused_not_duplicated():
    transport = FakeGitHubTransport()
    api, _ = _client(transport)
    client = AppWritebackClient(api, repository="kodmial/runtime-lab")
    first = materialize_result(
        _result(changes=[_change("a.txt", "added", b"v1")]),
        client=client, unique_suffix="run-1")
    assert first.action == "created" and first.pr_number == 1
    second = materialize_result(
        _result(changes=[_change("a.txt", "added", b"v2")]),
        client=client, unique_suffix="run-2")
    assert second.action == "updated"
    assert second.pr_number == 1
    assert second.branch == first.branch
    assert len(transport.prs) == 1
    assert client.ci_dispatched == [1, 1]


def test_no_change_produces_no_branch_or_pr():
    transport = FakeGitHubTransport()
    api, _ = _client(transport)
    client = AppWritebackClient(api, repository="kodmial/runtime-lab")
    outcome = materialize_result(_result(changes=[]), client=client,
                                 unique_suffix="run-1")
    assert outcome.action == "no-changes"
    assert outcome.branch == ""
    assert outcome.pr_number is None
    assert transport.branches == {}
    assert transport.prs == []


def test_base_sha_mismatch_fails_safely_without_writes():
    transport = FakeGitHubTransport(base_sha="live-sha")
    api, _ = _client(transport)
    client = AppWritebackClient(api, repository="kodmial/runtime-lab")
    from result_materialize import MaterializeError

    with pytest.raises(MaterializeError, match="base SHA mismatch"):
        materialize_result(_result(), client=client, unique_suffix="run-1")
    assert transport.branches == {}
    assert transport.prs == []


# ---------------------------------------------------------------------------
# Controller integration: dispatch + verified cleanup + write-back.
# ---------------------------------------------------------------------------

class FakeRenderClient:
    def __init__(self):
        self.creations = []
        self.deletes = []
        self.verifies = []
        self.suspends = []
        self._counter = 0
        self._deleted = set()

    def create_service(self, payload):
        self._counter += 1
        service_id = "srv-%d" % self._counter
        self.creations.append(dict(payload))
        return {"service_id": service_id, "deploy_id": "dep-%s" % service_id,
                "plan": "free"}

    def get_service(self, service_id):
        return {"serviceDetails": {"plan": "free",
                                   "url": "https://%s.onrender.com" % service_id}}

    def get_deploy(self, service_id, deploy_id):
        return {"status": "live"}

    def service_url(self, service):
        return "https://worker.onrender.com"

    def delete_service(self, service_id):
        self.deletes.append(service_id)
        self._deleted.add(service_id)
        return 204

    def verify_gone(self, service_id):
        self.verifies.append(service_id)
        return 404 if service_id in self._deleted else 200

    def suspend_service(self, service_id):
        self.suspends.append(service_id)
        return 202


class FakeRunnerWithChanges:
    """Runner fake returning a full materializable result payload."""

    def __init__(self, changes):
        self._changes = changes
        self.submits = []

    def wait_healthy(self, base_url):
        return None

    def submit_job(self, base_url, job_body):
        self.submits.append(dict(job_body))
        return "job-1"

    def get_result(self, base_url, job_id):
        return {"job_id": "job-1", "status": "succeeded", "success": True,
                "summary": "done", "error": "", "metadata": {"issue_number": 5},
                "issue_number": 5, "repository_url": PUBLIC_REPO_URL,
                "base_ref": "main", "base_sha": BASE_SHA,
                "executed_model": "m", "changes": list(self._changes)}


def _controller_with_writeback(tmp_path, transport, runner, **overrides):
    api, _ = _client(transport)

    def factory(issue):
        return AppWritebackClient(api, repository="kodmial/runtime-lab")

    return Controller(
        webhook_secret=SECRET,
        store=DeliveryStore(str(tmp_path / "deliveries.json")),
        provider=StaticSnapshotProvider(),
        render_client=FakeRenderClient(),
        runner_client=runner,
        owner_id="own-123",
        writeback_factory=factory,
        github_api=api,
        **overrides,
    )


def _ingest(controller, delivery_id, number=5, labels=None):
    payload = {"action": "opened",
               "issue": {"number": number, "title": "Fix it", "body": "B",
                         "state": "open",
                         "labels": [{"name": n} for n in (labels or [])]},
               "sender": {"login": "owner"}}
    body = json.dumps(payload).encode()
    status, _ = controller.ingest(
        headers={"X-GitHub-Delivery": delivery_id, "X-GitHub-Event": "issues",
                 "X-Hub-Signature-256": sign_webhook_body(SECRET, body)},
        body=body)
    assert status == 202


def test_controller_commits_worker_result_to_pr_entirely_from_render(tmp_path):
    transport = FakeGitHubTransport()
    runner = FakeRunnerWithChanges([_change("notes.txt", "added", b"hello")])
    controller = _controller_with_writeback(tmp_path, transport, runner)
    render = controller.render_client
    _ingest(controller, "del-wb-1", labels=["priority:p0"])
    outcome = controller.process_delivery("del-wb-1")
    assert outcome["dispatched"] is True
    assert outcome["ok"] is True
    assert outcome["writeback_action"] == "created"
    assert outcome["writeback_branch"] == "opencode/issue5-ctrl-del-wb-1"
    assert outcome["writeback_pr"] == 1
    record = controller.store.get("del-wb-1")
    assert record["status"] == "completed"
    # Mandatory cleanup proof even on the write-back path.
    assert len(render.creations) == 1
    assert len(render.deletes) == 1
    assert render.suspends == []
    assert record["worker_service_id"] == outcome["worker_service_id"]
    assert len(transport.prs) == 1


def test_controller_cleanup_still_runs_when_github_auth_fails(tmp_path):
    transport = FakeGitHubTransport()
    runner = FakeRunnerWithChanges([_change("notes.txt", "added", b"hello")])
    secret_value = "ghs-super-secret-token-value"

    def failing_factory(issue):
        raise GitHubAuthError("installation-token exchange failed 401 "
                              "token=%s" % secret_value)

    controller = Controller(
        webhook_secret=SECRET,
        store=DeliveryStore(str(tmp_path / "deliveries.json")),
        provider=StaticSnapshotProvider(),
        render_client=FakeRenderClient(),
        runner_client=runner,
        owner_id="own-123",
        writeback_factory=failing_factory,
    )
    render = controller.render_client
    _ingest(controller, "del-auth-fail", labels=["priority:p0"])
    outcome = controller.process_delivery("del-auth-fail")
    assert outcome["dispatched"] is True  # the worker still ran
    assert outcome["ok"] is False  # write-back failure marks it failed
    assert "writeback_error" in outcome
    # Secrets never leak into the outcome, the store, or logs.
    assert secret_value not in outcome["writeback_error"]
    assert secret_value not in controller.store.get("del-auth-fail")["reason"]
    # The ephemeral worker was still deleted + verified.
    assert len(render.creations) == 1
    assert len(render.deletes) == 1
    assert len(render.verifies) >= 1
    assert render.suspends == []
    assert controller.store.get("del-auth-fail")["status"] == "failed"


def test_controller_no_change_writes_no_branch_or_pr(tmp_path):
    transport = FakeGitHubTransport()
    runner = FakeRunnerWithChanges([])
    controller = _controller_with_writeback(tmp_path, transport, runner)
    _ingest(controller, "del-nochange", labels=["priority:p1"])
    outcome = controller.process_delivery("del-nochange")
    assert outcome["dispatched"] is True
    assert outcome["writeback_action"] == "no-changes"
    assert outcome["writeback_pr"] is None
    assert transport.prs == []
    assert transport.branches == {}
    assert len(controller.render_client.deletes) == 1


def test_controller_duplicate_pr_reused_across_deliveries(tmp_path):
    transport = FakeGitHubTransport()
    api, _ = _client(transport)

    def factory(issue):
        return AppWritebackClient(api, repository="kodmial/runtime-lab")

    first_runner = FakeRunnerWithChanges([_change("a.txt", "added", b"v1")])
    controller = Controller(
        webhook_secret=SECRET,
        store=DeliveryStore(str(tmp_path / "deliveries.json")),
        provider=StaticSnapshotProvider(),
        render_client=FakeRenderClient(),
        runner_client=first_runner,
        owner_id="own-123",
        writeback_factory=factory,
        github_api=api,
    )
    _ingest(controller, "del-dup-1", labels=["priority:p0"])
    first = controller.process_delivery("del-dup-1")
    assert first["writeback_pr"] == 1

    second_runner = FakeRunnerWithChanges([_change("a.txt", "added", b"v2")])
    controller.runner_client = second_runner
    _ingest(controller, "del-dup-2", labels=["priority:p0"])
    second = controller.process_delivery("del-dup-2")
    assert second["writeback_action"] == "updated"
    assert second["writeback_pr"] == 1
    assert len(transport.prs) == 1  # never a duplicate PR


def test_controller_path_requires_no_actions_job():
    repo_root = Path(__file__).resolve().parents[1]
    app_text = (repo_root / "automation" / "github_app.py").read_text(
        encoding="utf-8")
    core = (repo_root / "automation" / "render_controller.py").read_text(
        encoding="utf-8")
    for text in (app_text, core):
        assert "createWorkflowDispatch" not in text
        assert "opencode run" not in text
        assert "OPENCODE_API_KEY" not in text
    # The App client records CI instead of dispatching a workflow: no
    # actions write permission, no workflow-dispatch call.
    assert '"/actions/workflows"' not in app_text
    assert "workflow run" not in app_text
