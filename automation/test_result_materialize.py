"""Tests for temporary runner result materialization (issue #5).

Covers the Definition of Done without network access or workflow edits:
- exactly one branch/PR from a successful result with the required
  opencode/issue<N>-... prefix and exactly the worker changes;
- ci.yml explicitly dispatched with the PR number (never via
  pull_request events);
- duplicate attempts reuse the open PR instead of duplicating it;
- base-SHA mismatch fails safely (no branch/PR/CI);
- added/modified/deleted files round-trip deterministically;
- malformed/out-of-scope results are rejected;
- the no-change case produces no branch, PR or CI dispatch;
- write-back is reusable outside Actions (injectable WritebackClient;
  core never touches GITHUB_TOKEN, Render workers or workflow files).
"""

import base64
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render_lifecycle import PUBLIC_REPO_URL  # noqa: E402
from result_materialize import (  # noqa: E402
    CI_WORKFLOW_ID,
    GhCliWritebackClient,
    MaterializeError,
    ValidatedChange,
    WritebackClient,
    apply_changes_to_directory,
    branch_matches_issue,
    branch_name_for_issue,
    build_pr_body,
    build_pr_title,
    load_result_file,
    materialize_from_file,
    materialize_result,
    normalize_changes,
    parse_issue_number_from_branch,
    sanitize_suffix,
    validate_change_entry,
    validate_result_payload,
)

BASE_SHA = "abc123def456abc123def456abc123def456abcd"


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
        "metadata": {"issue_number": 5, "model": "m"},
        "issue_number": 5,
        "repository_url": PUBLIC_REPO_URL,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "executed_model": "m",
        "output": "ok",
        "changes": [_change("notes.txt", "added", b"hello")],
    }
    payload.update(overrides)
    return payload


class FakeWritebackClient(WritebackClient):
    """In-memory write-back; proves reuse outside Actions (no gh/git)."""

    def __init__(self, repo_dir: str, base_sha: str = BASE_SHA):
        os.makedirs(repo_dir, exist_ok=True)
        self.repo_dir = repo_dir
        self._base_sha = base_sha
        self.branches: dict[str, dict] = {}
        self.prs: list[dict] = []
        self.ci_dispatched: list[int] = []
        self.publish_calls: list[dict] = []
        self.created_prs: list[dict] = []
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
        assert commit_message.strip()
        key = (branch, tuple((c.path, c.change_type, c.content) for c in changes))
        self.publish_calls.append({"branch": branch, "changes": list(changes)})
        if branch in self.branches and self.branches[branch].get("key") == key:
            return False
        apply_changes_to_directory(self.repo_dir, list(changes))
        self.branches[branch] = {"key": key, "changes": list(changes)}
        return True

    def create_pull_request(self, *, title, body, head, base):
        assert head in self.branches, "PR head must be a published branch"
        assert "Closes #%d" % 5 in body or "Closes #" in body
        self._counter += 1
        pr = {"number": self._counter, "head_ref": head, "state": "open",
              "title": title, "base": base}
        self.prs.append(pr)
        self.created_prs.append(dict(pr))
        return self._counter

    def dispatch_ci(self, pr_number: int) -> None:
        assert any(pr["number"] == pr_number for pr in self.prs)
        self.ci_dispatched.append(pr_number)


# ---------------------------------------------------------------------------
# Branch convention (scheduler / auto-merge association).
# ---------------------------------------------------------------------------

def test_branch_name_uses_exact_issue_prefix():
    branch = branch_name_for_issue(5, "999")
    assert branch == "opencode/issue5-999"
    assert branch_matches_issue(branch, 5) is True
    assert branch_matches_issue(branch, 6) is False
    assert parse_issue_number_from_branch(branch) == 5
    assert parse_issue_number_from_branch("main") is None
    assert parse_issue_number_from_branch("opencode/issueX-1") is None


def test_branch_suffix_rejects_empty_and_separators():
    with pytest.raises(ValueError):
        branch_name_for_issue(5, "")
    with pytest.raises(ValueError):
        branch_name_for_issue(5, "a/b")
    with pytest.raises(ValueError):
        branch_name_for_issue(0, "123")
    with pytest.raises(ValueError):
        sanitize_suffix("../evil")


def test_pr_title_and_body_link_source_issue():
    assert build_pr_title("Fix it", 5) == "Fix it"
    assert "Automated implementation" in build_pr_title("", 5)
    body = build_pr_body(issue_number=5, job_id="job-1", base_sha=BASE_SHA,
                         summary="done", executed_model="m")
    assert "Closes #5" in body
    assert "job-1" in body and BASE_SHA in body


# ---------------------------------------------------------------------------
# Happy path: exactly one branch/PR with exactly the worker changes + CI.
# ---------------------------------------------------------------------------

def test_successful_result_creates_one_branch_pr_and_ci(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    result = _result(changes=[
        _change("new.txt", "added", b"new content"),
        _change("docs/guide.md", "added", b"guide"),
    ])
    outcome = materialize_result(result, client=client, unique_suffix="run-1",
                                 issue_title="Fix it")
    assert outcome.action == "created"
    assert outcome.branch == "opencode/issue5-run-1"
    assert outcome.pr_number == 1
    assert outcome.dispatched_ci is True
    assert client.ci_dispatched == [1]
    assert len(client.branches) == 1
    assert len(client.created_prs) == 1
    assert Path(repo, "new.txt").read_bytes() == b"new content"
    assert Path(repo, "docs/guide.md").read_bytes() == b"guide"
    assert outcome.files_added == 2


def test_added_modified_deleted_files_work(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    Path(repo, "keep.txt").write_bytes(b"old")
    Path(repo, "remove.txt").write_bytes(b"bye")
    client = FakeWritebackClient(repo)
    result = _result(changes=[
        _change("added.txt", "added", b"new"),
        _change("keep.txt", "modified", b"updated"),
        _change("remove.txt", "deleted"),
    ])
    outcome = materialize_result(result, client=client, unique_suffix="run-2")
    assert outcome.action == "created"
    assert (outcome.files_added, outcome.files_modified,
            outcome.files_deleted) == (1, 1, 1)
    assert Path(repo, "added.txt").read_bytes() == b"new"
    assert Path(repo, "keep.txt").read_bytes() == b"updated"
    assert not Path(repo, "remove.txt").exists()


def test_apply_changes_is_deterministic_and_sorted(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    counts = apply_changes_to_directory(repo, [
        ValidatedChange(path="b.txt", change_type="added", content=b"B"),
        ValidatedChange(path="a.txt", change_type="added", content=b"A"),
    ])
    assert counts == {"added": 2, "modified": 0, "deleted": 0}
    # Reapplying as modifications still writes both files.
    counts = apply_changes_to_directory(repo, [
        {"path": "a.txt", "change_type": "modified",
         "content_base64": _b64(b"A2")},
    ])
    assert Path(repo, "a.txt").read_bytes() == b"A2"


# ---------------------------------------------------------------------------
# No-change, failed, and duplicate handling.
# ---------------------------------------------------------------------------

def test_no_changes_produces_no_branch_pr_or_ci(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    outcome = materialize_result(_result(changes=[]), client=client,
                                 unique_suffix="run-1")
    assert outcome.action == "no-changes"
    assert outcome.branch == ""
    assert outcome.pr_number is None
    assert outcome.dispatched_ci is False
    assert client.branches == {}
    assert client.ci_dispatched == []


def test_failed_result_is_skipped_without_writes(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    result = _result(status="failed", success=False, error="boom")
    outcome = materialize_result(result, client=client, unique_suffix="run-1")
    assert outcome.action == "skipped"
    assert client.publish_calls == []
    assert client.ci_dispatched == []


def test_duplicate_attempts_reuse_open_pr(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    first = _result(changes=[_change("a.txt", "added", b"v1")])
    outcome1 = materialize_result(first, client=client, unique_suffix="run-1")
    assert outcome1.action == "created" and outcome1.pr_number == 1
    second = _result(changes=[_change("a.txt", "added", b"v2")])
    outcome2 = materialize_result(second, client=client, unique_suffix="run-2")
    assert outcome2.action == "updated"
    assert outcome2.pr_number == 1  # no duplicate PR
    assert outcome2.branch == outcome1.branch  # same branch reused
    assert len(client.created_prs) == 1
    assert client.ci_dispatched == [1, 1]  # CI dispatched for the update too
    assert Path(repo, "a.txt").read_bytes() == b"v2"


def test_identical_duplicate_does_not_repush_or_duplicate(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    payload = _result(changes=[_change("a.txt", "added", b"same")])
    outcome1 = materialize_result(payload, client=client, unique_suffix="run-1")
    assert outcome1.action == "created"
    outcome2 = materialize_result(payload, client=client, unique_suffix="run-9")
    # Same content on the same reused branch: nothing to commit, no new PR.
    assert outcome2.action == "no-changes"
    assert outcome2.pr_number == 1
    assert len(client.created_prs) == 1
    assert client.ci_dispatched == [1]


# ---------------------------------------------------------------------------
# Verification: repository / base SHA / job ID; malformed / out-of-scope.
# ---------------------------------------------------------------------------

def test_base_sha_mismatch_fails_safely_without_writes(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo, base_sha="live-sha-1")
    result = _result()
    result["base_sha"] = "stale-sha-2"
    with pytest.raises(MaterializeError, match="base SHA mismatch"):
        materialize_result(result, client=client, unique_suffix="run-1")
    assert client.publish_calls == []
    assert client.created_prs == []
    assert client.ci_dispatched == []


def test_expected_base_sha_mismatch_fails_before_client(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    with pytest.raises(MaterializeError, match="base SHA mismatch"):
        materialize_result(_result(), client=client, unique_suffix="run-1",
                           expected_base_sha="different-sha")
    assert client.publish_calls == []


def test_repository_and_job_id_verification(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    bad_repo = _result(repository_url="https://github.com/other/repo")
    with pytest.raises(MaterializeError, match="repository mismatch"):
        materialize_result(bad_repo, client=client, unique_suffix="r1")
    bad_job = _result()
    with pytest.raises(MaterializeError, match="job id mismatch"):
        materialize_result(bad_job, client=client, unique_suffix="r1",
                           expected_job_id="job-other")
    bad_issue = _result()
    with pytest.raises(MaterializeError, match="issue mismatch"):
        materialize_result(bad_issue, client=client, issue_number=6,
                           unique_suffix="r1")
    assert client.publish_calls == []


def test_out_of_scope_paths_rejected(tmp_path):
    for path in ("/abs.txt", "../escape.txt", "a/../../b.txt",
                 ".git/config", ".github/workflows/ci.yml"):
        with pytest.raises(MaterializeError):
            validate_change_entry(_change(path, "added", b"x"))
    with pytest.raises(MaterializeError):
        validate_change_entry({"path": "a.txt", "change_type": "weird",
                               "content_base64": _b64(b"x")})
    with pytest.raises(MaterializeError):
        validate_change_entry({"path": "a.txt", "change_type": "added",
                               "content_base64": "!!!not-base64!!!"})
    with pytest.raises(MaterializeError):
        validate_change_entry({"path": "a.txt", "change_type": "added"})
    with pytest.raises(MaterializeError):
        normalize_changes([_change("dup.txt", "added", b"1"),
                           _change("dup.txt", "added", b"1")])


def test_malformed_payloads_rejected():
    with pytest.raises(MaterializeError):
        validate_result_payload("not-a-mapping")  # type: ignore[arg-type]
    with pytest.raises(MaterializeError):
        validate_result_payload({})
    with pytest.raises(MaterializeError):
        validate_result_payload(_result(changes="not-a-list"))
    with pytest.raises(MaterializeError):
        load_result_file("/nonexistent/result.json")


def test_result_file_roundtrip_and_materialize_from_file(tmp_path):
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result()), encoding="utf-8")
    loaded = load_result_file(str(result_path))
    assert loaded["job_id"] == "job-1"
    outcome = materialize_from_file(str(result_path), client=client,
                                    unique_suffix="run-7")
    assert outcome.action == "created"
    assert outcome.branch == "opencode/issue5-run-7"


# ---------------------------------------------------------------------------
# Reusability: core is stdlib-only and independent of Actions/Render.
# ---------------------------------------------------------------------------

def test_core_does_not_require_actions_env_or_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    repo = str(tmp_path / "repo")
    client = FakeWritebackClient(repo)
    outcome = materialize_result(_result(), client=client, unique_suffix="r1")
    assert outcome.action == "created"
    # The WritebackClient interface is what #11 re-implements with an App
    # token; the orchestrator itself never imports gh, Render or Actions.
    assert issubclass(FakeWritebackClient, WritebackClient)


def test_gh_client_construction_validates_repo_dir(tmp_path):
    with pytest.raises(ValueError):
        GhCliWritebackClient(repo_dir=str(tmp_path / "missing"))
    client = GhCliWritebackClient(repo_dir=str(tmp_path))
    assert client.base_ref == "main"
    assert CI_WORKFLOW_ID == "ci.yml"
