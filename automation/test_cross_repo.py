"""Tests for cross-repository task execution/write-back (issue #85).

Covers the Definition of Done without network access:

- normal runtime-lab self-target execution (default, unchanged);
- allow-listed ``kodmial/opencode`` target (marker, job payload,
  worker acceptance, materialization into the target repo);
- unapproved target rejection (fail closed, no worker side effects);
- base SHA mismatch (fail closed, no branch/PR/CI);
- duplicate/retry does not create duplicate target PRs;
- credentials are not present in the OpenCode child environment;
- target PR links back to the Runtime Lab source issue (and never
  uses a bare ``Closes #N`` that would close the wrong issue).
"""

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cross_repo import (  # noqa: E402
    ALLOWED_TARGET_REPOS,
    SOURCE_REPO_FULL,
    SOURCE_REPO_URL,
    TARGET_OPENCODE_URL,
    TARGET_WRITE_CREDENTIAL_ENV_NAMES,
    TargetRepoError,
    TargetSpec,
    assert_no_target_credentials_in_worker_env,
    assert_pr_body_links_source,
    build_execution_evidence,
    build_target_pr_body,
    format_evidence_line,
    is_cross_repo_target,
    normalize_clone_url,
    normalize_target_repo,
    parse_target_repo,
    resolve_bootstrap_pat,
    scrub_target_credentials,
    target_repo_url,
    validate_result_matches_target,
)
from render_lifecycle import (  # noqa: E402
    PUBLIC_REPO_URL,
    ExecutionMetadata,
    JobRequest,
)
from result_materialize import (  # noqa: E402
    MaterializeError,
    WritebackClient,
    apply_changes_to_directory,
    materialize_result,
)

BASE_SHA = "abc123def456abc123def456abc123def456abcd"
OTHER_SHA = "fff123def456abc123def456abc123def456abcd"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _change(path, change_type, content: bytes | None = None):
    entry: dict = {"path": path, "change_type": change_type}
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
        "metadata": {"issue_number": 85, "model": "m"},
        "issue_number": 85,
        "repository_url": SOURCE_REPO_URL,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "executed_model": "m",
        "output": "ok",
        "changes": [_change("notes.txt", "added", b"hello")],
    }
    payload.update(overrides)
    return payload


class FakeTargetClient(WritebackClient):
    """In-memory write-back bound to one target repo (no gh/git)."""

    def __init__(self, repo_dir: str, base_sha: str = BASE_SHA,
                 repository: str = SOURCE_REPO_FULL):
        os.makedirs(repo_dir, exist_ok=True)
        self.repo_dir = repo_dir
        self._base_sha = base_sha
        self.repository = repository
        self.branches: dict[str, dict] = {}
        self.prs: list[dict] = []
        self.ci_dispatched: list[int] = []
        self.bodies: list[str] = []
        self._counter = 0

    def get_base_sha(self, base_ref: str = "main") -> str:
        return self._base_sha

    def find_open_pr_for_issue(self, issue_number: int):
        prefix = "opencode/issue%d-" % issue_number
        for pr in self.prs:
            if pr["state"] == "open" and pr["head_ref"].startswith(prefix):
                return dict(pr)
        return None

    def publish_branch(self, *, branch, base_sha, changes, commit_message):
        assert branch.startswith("opencode/issue"), branch
        assert base_sha == self._base_sha, (base_sha, self._base_sha)
        key = (branch, tuple((c.path, c.change_type, c.content) for c in changes))
        if branch in self.branches and self.branches[branch].get("key") == key:
            return False
        apply_changes_to_directory(self.repo_dir, list(changes))
        self.branches[branch] = {"key": key, "changes": list(changes)}
        return True

    def create_pull_request(self, *, title, body, head, base):
        assert head in self.branches
        self._counter += 1
        pr = {"number": self._counter, "head_ref": head, "state": "open",
              "title": title, "body": body, "base": base}
        self.prs.append(pr)
        self.bodies.append(body)
        return self._counter

    def dispatch_ci(self, pr_number: int) -> None:
        self.ci_dispatched.append(pr_number)


# ---------------------------------------------------------------------------
# 1. Normal runtime-lab self-target execution (default, unchanged).
# ---------------------------------------------------------------------------

def test_self_target_is_default_and_not_cross_repo():
    assert parse_target_repo("") == SOURCE_REPO_FULL
    assert parse_target_repo("plain body, no marker") == SOURCE_REPO_FULL
    assert is_cross_repo_target(SOURCE_REPO_FULL) is False
    assert target_repo_url(SOURCE_REPO_FULL) == SOURCE_REPO_URL
    assert SOURCE_REPO_FULL in ALLOWED_TARGET_REPOS


def test_self_target_job_request_unchanged():
    meta = ExecutionMetadata(issue_number=85)
    req = JobRequest(task_text="do it", issue_number=85, metadata=meta)
    assert req.repository_url == PUBLIC_REPO_URL
    body = req.to_dict()
    assert body["repository_url"] == PUBLIC_REPO_URL
    assert body["issue_number"] == 85


def test_self_target_materializes_with_closes(tmp_path):
    client = FakeTargetClient(str(tmp_path / "repo"))
    outcome = materialize_result(
        _result(), client=client, unique_suffix="run-1", issue_title="T")
    assert outcome.action == "created"
    assert outcome.branch == "opencode/issue85-run-1"
    assert outcome.pr_number == 1
    assert "Closes #85" in client.bodies[0]


# ---------------------------------------------------------------------------
# 2. Allow-listed kodmial/opencode target.
# ---------------------------------------------------------------------------

def test_marker_selects_opencode_target():
    body = "Do the lite build.\n\n<!-- runtime-lab-target: kodmial/opencode -->"
    assert parse_target_repo(body) == "kodmial/opencode"
    assert is_cross_repo_target("kodmial/opencode") is True
    assert target_repo_url("kodmial/opencode") == TARGET_OPENCODE_URL
    # Bare-line spelling is also accepted; last marker wins.
    assert parse_target_repo("runtime-lab-target: kodmial/opencode") == \
        "kodmial/opencode"
    assert parse_target_repo(
        "<!-- runtime-lab-target: kodmial/runtime-lab -->\n"
        "<!-- runtime-lab-target: kodmial/opencode -->") == "kodmial/opencode"


def test_opencode_job_request_carries_target():
    meta = ExecutionMetadata(issue_number=85)
    req = JobRequest(
        repository_url=TARGET_OPENCODE_URL,
        base_ref="main",
        base_sha=BASE_SHA,
        task_text="do it",
        issue_number=85,
        metadata=meta,
        target_repository="kodmial/opencode",
    )
    body = req.to_dict()
    assert body["repository_url"] == TARGET_OPENCODE_URL
    assert body["target_repository"] == "kodmial/opencode"
    assert body["source_repository"] == SOURCE_REPO_URL
    assert body["issue_number"] == 85  # lifecycle stays on the source issue


def test_runner_accepts_opencode_payload(tmp_path):
    from runner_server import JobManager
    manager = JobManager(workspace_root=str(tmp_path / "ws"))
    payload = {
        "task_text": "do it",
        "issue_number": 85,
        "repository_url": TARGET_OPENCODE_URL,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "metadata": {"model": "opencode/muse-spark-1.3-contributor-free",
                     "region": "oregon"},
    }
    task, issue, region, model, timeout, url, ref, sha = \
        manager._validate_submit_payload(payload)
    assert url == TARGET_OPENCODE_URL
    assert issue == 85
    assert ref == "main"


def test_opencode_result_materializes_into_target(tmp_path):
    client = FakeTargetClient(str(tmp_path / "fork"), repository="kodmial/opencode")
    result = _result(repository_url=TARGET_OPENCODE_URL)
    outcome = materialize_result(
        result, client=client, unique_suffix="run-9",
        expected_repository_url=TARGET_OPENCODE_URL,
        source_repo_full=SOURCE_REPO_FULL,
        issue_title="Lite build")
    assert outcome.action == "created"
    assert outcome.branch == "opencode/issue85-run-9"
    assert outcome.pr_number == 1
    assert_pr_body_links_source(client.bodies[0], 85)


def test_target_spec_pins_base_sha():
    spec = TargetSpec(source_issue=85, target_repo="kodmial/opencode",
                      target_base_sha=BASE_SHA)
    assert spec.cross_repo is True
    assert spec.target_url == TARGET_OPENCODE_URL
    validate_result_matches_target(_result(repository_url=TARGET_OPENCODE_URL),
                                   spec)
    from cross_repo import pin_target_base_sha
    pinned = pin_target_base_sha(
        TargetSpec(source_issue=85, target_repo="kodmial/opencode"),
        resolve_fn=lambda ref, repo: BASE_SHA)
    assert pinned.target_base_sha == BASE_SHA


# ---------------------------------------------------------------------------
# 3. Unapproved target rejection (fail closed).
# ---------------------------------------------------------------------------

def test_unapproved_marker_is_rejected():
    with pytest.raises(TargetRepoError):
        parse_target_repo("<!-- runtime-lab-target: evil/fork -->")
    with pytest.raises(TargetRepoError):
        normalize_target_repo("https://github.com/evil/fork")
    with pytest.raises(TargetRepoError):
        normalize_clone_url("https://github.com/evil/fork")
    with pytest.raises(TargetRepoError):
        normalize_target_repo("")


def test_unapproved_job_request_is_rejected():
    meta = ExecutionMetadata(issue_number=85)
    with pytest.raises(ValueError):
        JobRequest(repository_url="https://github.com/evil/fork",
                   task_text="do it", issue_number=85, metadata=meta)


def test_unapproved_runner_payload_is_rejected(tmp_path):
    from runner_server import JobManager
    manager = JobManager(workspace_root=str(tmp_path / "ws2"))
    with pytest.raises(ValueError):
        manager._validate_submit_payload({
            "task_text": "do it",
            "issue_number": 85,
            "repository_url": "https://github.com/evil/fork",
        })


def test_unapproved_dispatch_fails_before_worker_creation():
    from render_controller import execute_issue_attempt

    class _NoCreateClient:
        def create_service(self, payload):
            raise AssertionError("must not create a worker for bad targets")

    class _Runner:
        def wait_healthy(self, base_url):
            raise AssertionError("unreachable")

        def submit_job(self, base_url, job_body):
            raise AssertionError("unreachable")

        def get_result(self, base_url, job_id):
            raise AssertionError("unreachable")

    with pytest.raises((TargetRepoError, ValueError)):
        execute_issue_attempt(
            delivery_id="d-1", issue_number=85, execution_mode="e2e",
            title="t",
            body="<!-- runtime-lab-target: evil/fork -->",
            region="oregon",
            model="opencode/muse-spark-1.3-contributor-free",
            owner_id="owner-1", run_id="run-1",
            render_client=_NoCreateClient(), runner_client=_Runner())


# ---------------------------------------------------------------------------
# 4. Base SHA mismatch (fail closed, no side effects).
# ---------------------------------------------------------------------------

def test_base_mismatch_materialization_creates_nothing(tmp_path):
    client = FakeTargetClient(str(tmp_path / "fork"),
                              repository="kodmial/opencode")
    result = _result(repository_url=TARGET_OPENCODE_URL, base_sha=OTHER_SHA)
    with pytest.raises(MaterializeError):
        materialize_result(
            result, client=client, unique_suffix="run-1",
            expected_repository_url=TARGET_OPENCODE_URL,
            expected_base_sha=BASE_SHA,
            source_repo_full=SOURCE_REPO_FULL)
    assert client.branches == {}
    assert client.prs == []
    assert client.ci_dispatched == []


def test_base_mismatch_against_pinned_spec():
    spec = TargetSpec(source_issue=85, target_repo="kodmial/opencode",
                      target_base_sha=BASE_SHA)
    with pytest.raises(TargetRepoError):
        validate_result_matches_target(
            _result(repository_url=TARGET_OPENCODE_URL, base_sha=OTHER_SHA),
            spec)
    with pytest.raises(TargetRepoError):
        validate_result_matches_target(
            _result(repository_url=SOURCE_REPO_URL), spec)


def test_repo_mismatch_rejected_even_with_right_sha(tmp_path):
    client = FakeTargetClient(str(tmp_path / "repo"))
    with pytest.raises(MaterializeError):
        materialize_result(
            _result(repository_url=TARGET_OPENCODE_URL), client=client,
            unique_suffix="run-1",
            expected_repository_url=SOURCE_REPO_URL)


# ---------------------------------------------------------------------------
# 5. Duplicate/retry does not create duplicate target PRs.
# ---------------------------------------------------------------------------

def test_retry_reuses_open_target_pr(tmp_path):
    client = FakeTargetClient(str(tmp_path / "fork"),
                              repository="kodmial/opencode")
    first = materialize_result(
        _result(repository_url=TARGET_OPENCODE_URL), client=client,
        unique_suffix="run-1",
        expected_repository_url=TARGET_OPENCODE_URL,
        source_repo_full=SOURCE_REPO_FULL)
    assert first.action == "created" and first.pr_number == 1
    second = materialize_result(
        _result(repository_url=TARGET_OPENCODE_URL), client=client,
        unique_suffix="run-2",
        expected_repository_url=TARGET_OPENCODE_URL,
        source_repo_full=SOURCE_REPO_FULL)
    assert second.action in ("updated", "no-changes")
    assert second.pr_number == 1
    assert len([pr for pr in client.prs if pr["state"] == "open"]) == 1


# ---------------------------------------------------------------------------
# 6. Credentials are never present in the OpenCode child environment.
# ---------------------------------------------------------------------------

def test_target_credentials_scrubbed_from_worker_env():
    from opencode_runner import (
        WORKER_SCRUB_ENV_NAMES,
        assert_worker_env_clean,
        scrubbed_env_for_worker,
    )
    from knowledge_store import STORAGE_CREDENTIAL_ENV_NAMES
    assert "TARGET_REPO_PAT" in WORKER_SCRUB_ENV_NAMES
    assert "TARGET_REPO_PAT" in STORAGE_CREDENTIAL_ENV_NAMES
    for name in TARGET_WRITE_CREDENTIAL_ENV_NAMES:
        assert name in WORKER_SCRUB_ENV_NAMES
    dirty = {
        "PATH": "/usr/bin",
        "TARGET_REPO_PAT": "secret-pat",
        "TAP_PAT": "secret-tap",
        "GH_TOKEN": "secret-gh",
        "GITHUB_TOKEN": "secret-gh2",
        "GITHUB_APP_ID": "123",
        "GITHUB_APP_PRIVATE_KEY": "pem",
        "GITHUB_APP_INSTALLATION_ID": "456",
        "OPENCODE_MODEL": "m",
    }
    cleaned = scrubbed_env_for_worker(dirty)
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["OPENCODE_MODEL"] == "m"
    for name in TARGET_WRITE_CREDENTIAL_ENV_NAMES:
        assert name not in cleaned
    assert_no_target_credentials_in_worker_env(cleaned)
    scrubbed_from_cross = scrub_target_credentials(dirty)
    for name in TARGET_WRITE_CREDENTIAL_ENV_NAMES:
        assert name not in scrubbed_from_cross
    with pytest.raises(TargetRepoError):
        assert_no_target_credentials_in_worker_env(dirty)
    with pytest.raises(ValueError):
        assert_worker_env_clean(dirty)


def test_bootstrap_pat_prefers_target_spelling():
    assert resolve_bootstrap_pat({}) == ""
    assert resolve_bootstrap_pat({"TAP_PAT": "tap"}) == "tap"
    assert resolve_bootstrap_pat(
        {"TAP_PAT": "tap", "TARGET_REPO_PAT": "target"}) == "target"


# ---------------------------------------------------------------------------
# 7. Target PR links back to the Runtime Lab source issue.
# ---------------------------------------------------------------------------

def test_target_pr_body_links_source_without_bare_closes():
    body = build_target_pr_body(
        source_issue=85, job_id="job-1", target_repo="kodmial/opencode",
        target_base_sha=BASE_SHA, target_branch="opencode/issue85-run-1",
        summary="done", executed_model="m")
    assert "kodmial/runtime-lab#85" in body
    assert "https://github.com/kodmial/runtime-lab/issues/85" in body
    assert "kodmial/opencode" in body
    assert BASE_SHA in body
    assert_pr_body_links_source(body, 85)
    with pytest.raises(TargetRepoError):
        assert_pr_body_links_source("no link here", 85)
    with pytest.raises(TargetRepoError):
        assert_pr_body_links_source(
            "See kodmial/runtime-lab#85\n\nCloses #85\n", 85)


def test_execution_evidence_records_target_shas():
    spec = TargetSpec(source_issue=85, target_repo="kodmial/opencode",
                      target_base_sha=BASE_SHA)
    evidence = build_execution_evidence(
        spec=spec, job_id="job-1", target_head_sha=OTHER_SHA,
        target_branch="opencode/issue85-run-1", target_pr=7,
        writeback_action="created")
    assert evidence["source_repo"] == SOURCE_REPO_FULL
    assert evidence["source_issue"] == 85
    assert evidence["target_repo"] == "kodmial/opencode"
    assert evidence["target_repo_url"] == TARGET_OPENCODE_URL
    assert evidence["target_base_sha"] == BASE_SHA
    assert evidence["target_head_sha"] == OTHER_SHA
    assert evidence["target_branch"] == "opencode/issue85-run-1"
    assert evidence["target_pr"] == 7
    line = format_evidence_line(evidence)
    assert "kodmial/opencode" in line and "kodmial/runtime-lab" in line
