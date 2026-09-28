"""Tests for temporary result materialization into a branch + PR (issue #5).

Covers the Definition of Done without touching the network or any file
under .github/workflows/**:
- successful results produce exactly one branch + PR with the
  opencode/issue<NUMBER>-... convention and exactly the worker changes;
- ci.yml is explicitly dispatched with the PR number (never via a
  pull_request event from a GITHUB_TOKEN push);
- duplicate attempts reuse the open PR instead of creating a second one;
- base-SHA mismatch fails safely with no side effects;
- added/modified/deleted files round-trip deterministically;
- malformed/out-of-scope results (failed status, wrong repo/issue/job,
  bad base64, absolute/parent/workflow paths) are rejected;
- the no-change case creates no branch and no empty PR;
- GitHub write-back stays behind the reusable GitOps interface so #11
  can execute the same behavior from Render.
"""

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = REPO_ROOT / "automation"
sys.path.insert(0, str(AUTOMATION))
sys.path.insert(0, str(REPO_ROOT))

from render_materialize import (  # noqa: E402
    GitOps,
    MaterializeError,
    SubprocessGitOps,
    apply_changes,
    branch_name_for_issue,
    commit_message_for_issue,
    default_pr_body,
    has_changes,
    is_issue_branch,
    load_result_file,
    materialize,
    outcome_to_dict,
    parse_branch_issue_number,
    select_open_pr_for_branch,
    shas_match,
    validate_result,
)
from render_lifecycle import PREFERRED_MODEL, PUBLIC_REPO_URL  # noqa: E402


def _change(path, change_type, content=b""):
    entry = {"path": path, "change_type": change_type}
    if change_type != "deleted":
        entry["content_base64"] = base64.b64encode(content).decode("ascii")
    return entry


def _result(**overrides):
    payload = {
        "job_id": "job-1",
        "status": "succeeded",
        "success": True,
        "summary": "did the thing",
        "error": "",
        "metadata": {"issue_number": 5},
        "issue_number": 5,
        "repository_url": PUBLIC_REPO_URL,
        "base_ref": "main",
        "base_sha": "abc123def456",
        "changes": [_change("hello.txt", "added", b"hi\n")],
        "executed_model": PREFERRED_MODEL,
    }
    payload.update(overrides)
    return payload


class FakeOps(GitOps):
    """In-memory GitOps recording every write-back call."""

    def __init__(self, main_sha="abc123def456", open_prs=None, commit_sha="c0ffee"):
        self.calls: list[tuple] = []
        self.main_sha = main_sha
        self.open_prs = dict(open_prs or {})
        self.commit_sha = commit_sha
        self.created_prs: list[str] = []
        self.dispatched: list[str] = []

    def current_main_sha(self):
        self.calls.append(("current_main_sha",))
        return self.main_sha

    def ensure_branch_at(self, branch, sha):
        self.calls.append(("ensure_branch_at", branch, sha))
        return "created"

    def checkout_branch(self, branch):
        self.calls.append(("checkout_branch", branch))

    def commit_all(self, message):
        self.calls.append(("commit_all", message))
        return self.commit_sha

    def push_branch(self, branch):
        self.calls.append(("push_branch", branch))

    def branch_diff_empty(self, branch, base_sha):
        self.calls.append(("branch_diff_empty", branch, base_sha))
        return False

    def find_open_pr(self, branch):
        self.calls.append(("find_open_pr", branch))
        return self.open_prs.get(branch, "")

    def create_pr(self, branch, title, body):
        self.calls.append(("create_pr", branch, title, body))
        number = str(100 + len(self.created_prs) + 1)
        self.created_prs.append(branch)
        self.open_prs[branch] = number
        return number

    def dispatch_ci(self, pr_number):
        self.calls.append(("dispatch_ci", pr_number))
        self.dispatched.append(pr_number)


# ---------------------------------------------------------------------------
# Branch convention (scheduler + auto-merge rely on the prefix).
# ---------------------------------------------------------------------------


def test_branch_convention_matches_scheduler_and_merge_prefix():
    branch = branch_name_for_issue(5, "999")
    assert branch == "opencode/issue5-999"
    assert parse_branch_issue_number(branch) == 5
    assert is_issue_branch(branch, 5) is True
    assert is_issue_branch(branch, 6) is False
    assert is_issue_branch("main", 5) is False
    assert is_issue_branch("opencode/issue5", 5) is False
    with pytest.raises(MaterializeError):
        branch_name_for_issue(0, "x")
    with pytest.raises(MaterializeError):
        branch_name_for_issue(5, "")
    with pytest.raises(MaterializeError):
        branch_name_for_issue(5, "has space")
    with pytest.raises(MaterializeError):
        branch_name_for_issue(5, "a/b")
    with pytest.raises(MaterializeError):
        parse_branch_issue_number("feature/x")
    assert commit_message_for_issue(5) == "fix: implement issue #5"
    assert "Closes #5" in default_pr_body(5)


def test_sha_comparison_allows_short_prefix_but_rejects_drift():
    assert shas_match("abc123", "abc123") is True
    assert shas_match("abc123def456", "abc123") is True
    assert shas_match("abc123", "abc123def456") is True
    assert shas_match("abc123", "def456") is False
    assert shas_match("", "abc123") is False
    assert shas_match("abc123", "") is False


# ---------------------------------------------------------------------------
# Result validation.
# ---------------------------------------------------------------------------


def test_validate_accepts_successful_result():
    normalized = validate_result(_result(), expected_issue_number=5)
    assert normalized.job_id == "job-1"
    assert normalized.issue_number == 5
    assert normalized.base_sha == "abc123def456"
    assert has_changes(normalized) is True
    assert normalized.changes[0]["path"] == "hello.txt"


def test_validate_enforces_identity_and_success():
    with pytest.raises(MaterializeError):
        validate_result(_result(status="failed", success=False),
                        expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(_result(status="timed_out", success=False),
                        expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(_result(repository_url="https://github.com/other/repo"),
                        expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(_result(issue_number=6), expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(_result(), expected_issue_number=5,
                        expected_base_sha="deadbeef")
    with pytest.raises(MaterializeError):
        validate_result(_result(), expected_issue_number=5,
                        expected_job_id="job-2")
    with pytest.raises(MaterializeError):
        validate_result(_result(job_id=""), expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(_result(base_sha=""), expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result([], expected_issue_number=5)
    # Expectations that do match are accepted, including short SHAs.
    validate_result(_result(), expected_issue_number=5,
                    expected_base_sha="abc123", expected_job_id="job-1")


def test_validate_rejects_malformed_and_out_of_scope_changes():
    bad_b64 = _change("a.txt", "added", b"x")
    bad_b64["content_base64"] = "!!!not-base64!!!"
    with pytest.raises(MaterializeError):
        validate_result(_result(changes=[bad_b64]), expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(
            _result(changes=[{"path": "a.txt", "change_type": "renamed"}]),
            expected_issue_number=5,
        )
    for bad_path in ("/abs.txt", "../escape.txt", "a/../../b.txt",
                     ".github/workflows/ci.yml", ".github/workflows/x.yml",
                     ".git/config", "", "."):
        with pytest.raises(MaterializeError):
            validate_result(
                _result(changes=[_change(bad_path, "deleted")]),
                expected_issue_number=5,
            )
    conflicting = [_change("a.txt", "added", b"1"), _change("a.txt", "deleted")]
    with pytest.raises(MaterializeError):
        validate_result(_result(changes=conflicting), expected_issue_number=5)
    with pytest.raises(MaterializeError):
        validate_result(_result(changes="nope"), expected_issue_number=5)
    # Exact duplicates are de-duplicated deterministically, not fatal.
    dupes = [_change("a.txt", "added", b"1"), _change("a.txt", "added", b"1")]
    normalized = validate_result(_result(changes=dupes), expected_issue_number=5)
    assert [item["path"] for item in normalized.changes] == ["a.txt"]


def test_validate_enforces_producer_size_limits(monkeypatch):
    import render_materialize as module

    repo_changes = [_change("a.txt", "added", b"x" * 64)]
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 16)
    with pytest.raises(MaterializeError):
        validate_result(_result(changes=repo_changes), expected_issue_number=5)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 512 * 1024)
    monkeypatch.setattr(module, "MAX_FILES", 1)
    with pytest.raises(MaterializeError):
        validate_result(
            _result(changes=[_change("a.txt", "added", b"a"),
                             _change("b.txt", "added", b"b")]),
            expected_issue_number=5,
        )


def test_load_result_file_rejects_missing_and_malformed(tmp_path):
    with pytest.raises(MaterializeError):
        load_result_file(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(MaterializeError):
        load_result_file(str(bad))
    lst = tmp_path / "list.json"
    lst.write_text("[]", encoding="utf-8")
    with pytest.raises(MaterializeError):
        load_result_file(str(lst))


# ---------------------------------------------------------------------------
# Deterministic change application.
# ---------------------------------------------------------------------------


def test_apply_roundtrips_added_modified_deleted(tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "keep.txt").write_text("v1", encoding="utf-8")
    (repo / "gone.txt").write_text("bye", encoding="utf-8")
    changes = [
        _change("keep.txt", "modified", b"v2"),
        _change("sub/new.txt", "added", "unicod\xe9\n".encode("utf-8")),
        _change("gone.txt", "deleted"),
    ]
    applied = apply_changes(changes, str(repo))
    assert [item["path"] for item in applied] == [
        "gone.txt", "keep.txt", "sub/new.txt",
    ]
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "v2"
    assert (repo / "sub/new.txt").read_text(encoding="utf-8") == "unicod\xe9\n"
    assert not (repo / "gone.txt").exists()


def test_apply_is_idempotent_and_tolerates_absent_deletes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    changes = [_change("new.txt", "added", b"n"), _change("ghost.txt", "deleted")]
    first = apply_changes(changes, str(repo))
    second = apply_changes(changes, str(repo))
    assert first == second
    assert (repo / "new.txt").read_bytes() == b"n"


def test_apply_refuses_unsafe_targets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(MaterializeError):
        apply_changes([_change(".git/config", "added", b"x")], str(repo))
    with pytest.raises(MaterializeError):
        apply_changes([_change("../out.txt", "added", b"x")], str(repo))
    with pytest.raises(MaterializeError):
        apply_changes([{"path": "d", "change_type": "added",
                        "content_base64": "!!!"}], str(repo))
    with pytest.raises(MaterializeError):
        apply_changes([_change("a.txt", "added", b"x")], str(tmp_path / "nope"))


# ---------------------------------------------------------------------------
# PR reuse (no duplicate PRs).
# ---------------------------------------------------------------------------


def test_select_open_pr_matches_exact_branch_only():
    prs = [
        {"number": 11, "headRefName": "opencode/issue5-1", "state": "open"},
        {"number": 12, "headRefName": "opencode/issue5-2", "state": "closed"},
        {"number": 13, "head": {"ref": "opencode/issue5-3"}, "state": "open"},
    ]
    assert select_open_pr_for_branch(prs, "opencode/issue5-1") == "11"
    assert select_open_pr_for_branch(prs, "opencode/issue5-2") == ""
    assert select_open_pr_for_branch(prs, "opencode/issue5-3") == "13"
    assert select_open_pr_for_branch(prs, "opencode/issue5-") == ""
    assert select_open_pr_for_branch(prs, "opencode/issue5") == ""
    assert select_open_pr_for_branch([], "opencode/issue5-1") == ""


# ---------------------------------------------------------------------------
# Orchestration with an in-memory GitOps (reusable outside Actions).
# ---------------------------------------------------------------------------


def test_materialize_publishes_exactly_worker_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "gone.txt").write_text("bye", encoding="utf-8")
    changes = [
        _change("keep.txt", "added", b"v1"),
        _change("gone.txt", "deleted"),
    ]
    ops = FakeOps()
    outcome = materialize(
        _result(changes=changes),
        issue_number=5,
        branch="opencode/issue5-999",
        repo_dir=str(repo),
        ops=ops,
    )
    assert outcome.no_change is False
    assert outcome.branch == "opencode/issue5-999"
    assert outcome.pr_number == "101"
    assert outcome.created_pr is True
    assert outcome.dispatched_ci is True
    # Exactly the worker changes are reproduced in the working tree.
    assert (repo / "keep.txt").read_bytes() == b"v1"
    assert not (repo / "gone.txt").exists()
    assert [item["path"] for item in outcome.applied] == ["gone.txt", "keep.txt"]
    # CI is dispatched explicitly for the PR number.
    assert ops.dispatched == ["101"]
    assert ("dispatch_ci", "101") in ops.calls
    assert outcome_to_dict(outcome)["pr_number"] == "101"


def test_materialize_reuses_open_pr_without_duplicates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ops = FakeOps(open_prs={"opencode/issue5-999": "42"})
    first = materialize(
        _result(), issue_number=5, branch="opencode/issue5-999",
        repo_dir=str(repo), ops=ops,
    )
    assert first.pr_number == "42" and first.created_pr is False
    second = materialize(
        _result(), issue_number=5, branch="opencode/issue5-999",
        repo_dir=str(repo), ops=ops,
    )
    assert second.pr_number == "42" and second.created_pr is False
    assert ops.created_prs == []
    assert ops.dispatched == ["42", "42"]


def test_materialize_preserves_no_change_without_empty_pr(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ops = FakeOps()
    outcome = materialize(
        _result(changes=[]), issue_number=5, branch="opencode/issue5-999",
        repo_dir=str(repo), ops=ops,
    )
    assert outcome.no_change is True
    assert outcome.pr_number == ""
    assert outcome.dispatched_ci is False
    assert ops.calls == []  # no branch, no commit, no PR, no CI


def test_materialize_fails_safely_on_base_mismatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ops = FakeOps(main_sha="ffffffffff")
    with pytest.raises(MaterializeError):
        materialize(
            _result(), issue_number=5, branch="opencode/issue5-999",
            repo_dir=str(repo), ops=ops,
        )
    assert ops.calls == [("current_main_sha",)]  # no branch/PR/CI side effects


def test_materialize_rejects_failures_and_bad_branches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(MaterializeError):
        materialize(
            _result(status="failed", success=False), issue_number=5,
            branch="opencode/issue5-999", repo_dir=str(repo), ops=FakeOps(),
        )
    with pytest.raises(MaterializeError):
        materialize(
            _result(), issue_number=5, branch="feature/not-ours",
            repo_dir=str(repo), ops=FakeOps(),
        )
    with pytest.raises(MaterializeError):
        materialize(
            _result(), issue_number=5, branch="opencode/issue6-999",
            repo_dir=str(repo), ops=FakeOps(),
        )


def test_materialize_treats_already_current_content_as_no_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    class AlreadyCurrent(FakeOps):
        def commit_all(self, message):
            self.calls.append(("commit_all", message))
            return ""

        def branch_diff_empty(self, branch, base_sha):
            self.calls.append(("branch_diff_empty", branch, base_sha))
            return True

    ops = AlreadyCurrent()
    outcome = materialize(
        _result(), issue_number=5, branch="opencode/issue5-999",
        repo_dir=str(repo), ops=ops,
    )
    assert outcome.no_change is True
    assert outcome.pr_number == ""
    assert ops.dispatched == []
    assert ops.created_prs == []


# ---------------------------------------------------------------------------
# SubprocessGitOps against a real local git repository (no network).
# ---------------------------------------------------------------------------

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary is required")


def _init_repo(path: Path) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=str(path))
    subprocess.run([GIT, "init", "-b", "main"], cwd=path, check=True,
                   capture_output=True, env=env)
    subprocess.run([GIT, "config", "user.email", "t@example.com"], cwd=path,
                   check=True, capture_output=True, env=env)
    subprocess.run([GIT, "config", "user.name", "t"], cwd=path,
                   check=True, capture_output=True, env=env)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run([GIT, "add", "-A"], cwd=path, check=True,
                   capture_output=True, env=env)
    subprocess.run([GIT, "commit", "-m", "base"], cwd=path, check=True,
                   capture_output=True, env=env)
    head = subprocess.run([GIT, "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True, env=env)
    return head.stdout.strip()


@needs_git
def test_subprocess_ops_manage_local_branches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    ops = SubprocessGitOps(str(repo), repository="o/r")
    assert ops.current_main_sha() == head
    assert ops.ensure_branch_at("opencode/issue5-1", head) == "created"
    assert ops.ensure_branch_at("opencode/issue5-1", head) == "reset"
    (repo / "new.txt").write_text("n", encoding="utf-8")
    commit = ops.commit_all("fix: implement issue #5")
    assert commit and len(commit) == 40
    assert ops.branch_diff_empty("opencode/issue5-1", head) is False
    assert ops.branch_diff_empty("main", head) is True
    assert ops.commit_all("nothing") == ""
    with pytest.raises(MaterializeError):
        ops.ensure_branch_at("feature/x", head)


# ---------------------------------------------------------------------------
# Static harness invariants (temporary wrapper, mutable automation only).
# ---------------------------------------------------------------------------


def _materialize_script():
    return (AUTOMATION / "render-materialize.sh").read_text(encoding="utf-8")


def test_materialize_entrypoint_is_executable_and_syntax_valid():
    path = AUTOMATION / "render-materialize.sh"
    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR, "must be executable"
    proc = subprocess.run(["bash", "-n", str(path)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    module = AUTOMATION / "render_materialize.py"
    assert module.is_file()
    proc = subprocess.run([sys.executable, "-m", "py_compile", str(module)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_harness_is_marked_temporary_not_final_architecture():
    text = " ".join(_materialize_script().lower().split())
    assert "temporary" in text
    assert "harness" in text
    assert "must not require" in text or "not the final" in text
    assert "github app" in text  # final write-back moves to the Render controller


def test_materialize_script_is_independent_of_the_render_worker():
    text = _materialize_script()
    assert "automation/render_materialize.py" in text
    for forbidden in ("api.render.com", "RENDER_API_KEY", "/v1/jobs",
                      "/v1/services", "serviceId", "SERVICE_URL"):
        assert forbidden not in text, forbidden
    assert "RESULT_FILE" in text  # consumes only the collected result file


def test_materialize_script_uses_issue_branch_and_explicit_ci_dispatch():
    text = _materialize_script()
    assert "opencode/issue" in text
    assert "branch-name" in text
    assert "ci.yml" in text
    assert "pr_number" in text
    assert "no_change" in text  # no-change case creates no empty PR
    # Validation happens before any Git mutation.
    assert text.find("validate") < text.find("git fetch")
    # Pushes authenticate via the workflow token helper, never an embedded secret.
    assert "gh auth setup-git" in text
    assert "secrets." not in text
    assert "GITHUB_TOKEN" in text or "GH_TOKEN" in text


def test_no_workflow_files_were_modified():
    proc = subprocess.run(
        [sys.executable, "-c",
         "import subprocess; print(subprocess.run("
         "['git', 'status', '--porcelain', '--', '.github/workflows'], "
         "capture_output=True, text=True, cwd=%r).stdout)" % str(REPO_ROOT)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert ".github/workflows" not in proc.stdout


# ---------------------------------------------------------------------------
# CLI subcommands used by the shell harness.
# ---------------------------------------------------------------------------


def _cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(AUTOMATION / "render_materialize.py"), *args],
        capture_output=True, text=True, timeout=60,
        cwd=cwd or str(REPO_ROOT),
    )


def test_cli_branch_name_and_validate(tmp_path):
    proc = _cli("branch-name", "--issue", "5", "--suffix", "abc123")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "opencode/issue5-abc123"
    proc = _cli("branch-name", "--issue", "5", "--suffix", "bad suffix")
    assert proc.returncode != 0

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(_result()), encoding="utf-8")
    proc = _cli("validate", "--result", str(result_file), "--issue", "5",
                "--base-sha", "abc123", "--job-id", "job-1")
    assert proc.returncode == 0, proc.stderr
    assert "job-1" in proc.stdout
    proc = _cli("validate", "--result", str(result_file), "--issue", "5",
                "--base-sha", "mismatch-sha")
    assert proc.returncode != 0

    repo = tmp_path / "repo"
    repo.mkdir()
    proc = _cli("apply", "--result", str(result_file), "--issue", "5",
                "--repo-dir", str(repo))
    assert proc.returncode == 0, proc.stderr
    assert (repo / "hello.txt").read_bytes() == b"hi\n"


# ---------------------------------------------------------------------------
# End-to-end shell run with a fake gh and a real local git remote.
# ---------------------------------------------------------------------------


def _link_automation(work: Path) -> None:
    """Link the real automation dir into a scratch repo (test-only artifact).

    The link itself must never leak into a materialized branch, so it is
    excluded from git exactly like a local developer tool would be.
    """
    os.symlink(AUTOMATION, work / "automation", target_is_directory=True)
    exclude = work / ".git" / "info" / "exclude"
    with open(exclude, "a", encoding="utf-8") as handle:
        handle.write("automation\n")


def _write_fake_gh(directory: Path) -> Path:
    """Fake gh: PR store + workflow dispatch log; records every call."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state_dir = directory / "gh-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'STATE_DIR="%s"\n' % state_dir +
        'LOG="$STATE_DIR/calls.log"\n'
        'PRS="$STATE_DIR/prs.json"\n'
        '[[ -f "$PRS" ]] || echo "[]" > "$PRS"\n'
        'printf "%s\\n" "gh $*" >> "$LOG"\n'
        'if [[ "$1" == "auth" ]]; then exit 0; fi\n'
        'if [[ "$1" == "issue" && "$2" == "view" ]]; then\n'
        '  printf \'{"title":"Fake title","body":"Fake body"}\'\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "pr" && "$2" == "list" ]]; then\n'
        "  head=''\n"
        "  for ((i=1;i<=$#;i++)); do"
        ' if [[ "${!i}" == "--head" ]]; then j=$((i+1)); head="${!j}"; fi; done\n'
        "  python3 - \"$PRS\" \"$head\" <<'PY'\n"
        "import json,sys\n"
        "prs=json.load(open(sys.argv[1]))\n"
        "head=sys.argv[2]\n"
        "print(json.dumps([p for p in prs if p.get('headRefName')==head and p.get('state')=='open']))\n"
        "PY\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "pr" && "$2" == "create" ]]; then\n'
        "  head=''; title=''\n"
        "  for ((i=1;i<=$#;i++)); do"
        ' if [[ "${!i}" == "--head" ]]; then j=$((i+1)); head="${!j}"; fi;'
        ' if [[ "${!i}" == "--title" ]]; then j=$((i+1)); title="${!j}"; fi; done\n'
        "  python3 - \"$PRS\" \"$head\" \"$title\" <<'PY'\n"
        "import json,sys\n"
        "path,head,title=sys.argv[1],sys.argv[2],sys.argv[3]\n"
        "prs=json.load(open(path))\n"
        "number=max([p['number'] for p in prs]+[40])+1\n"
        "prs.append({'number':number,'headRefName':head,'state':'open','title':title})\n"
        "json.dump(prs,open(path,'w'))\n"
        "print(json.dumps({'number':number}))\n"
        "PY\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "workflow" && "$2" == "run" ]]; then exit 0; fi\n'
        'echo "unexpected gh call: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return bin_dir


def _write_result(directory: Path, payload: dict) -> Path:
    path = directory / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _shell_env(tmp_path: Path, repo: Path, result: Path, state: Path,
               path_prefix: str, run_id: str = "777") -> dict:
    env = dict(os.environ)
    env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    env["ISSUE_NUMBER"] = "5"
    env["GITHUB_REPOSITORY"] = "o/r"
    env["RENDER_RESULT_FILE"] = str(result)
    env["RENDER_STATE_FILE"] = str(state)
    env["GITHUB_RUN_ID"] = run_id
    env["GH_TOKEN"] = "dummy"
    env.pop("PR_SUFFIX", None)
    return env


@needs_git
def test_shell_materializes_branch_pr_and_ci_dispatch(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    head = _init_repo(work)
    (AUTOMATION / "x").exists()  # sanity: automation dir present
    _link_automation(work)
    origin = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "--bare", str(origin)], check=True,
                   capture_output=True)
    subprocess.run([GIT, "remote", "add", "origin", str(origin)], cwd=work,
                   check=True, capture_output=True)
    subprocess.run([GIT, "push", "-u", "origin", "main"], cwd=work, check=True,
                   capture_output=True)

    changes = [_change("added.txt", "added", b"new\n"),
               _change("base.txt", "modified", b"edited\n")]
    result = _write_result(tmp_path, _result(base_sha=head, changes=changes))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"serviceId": "srv-x", "baseSha": head,
                                 "jobId": "job-1"}), encoding="utf-8")
    env = _shell_env(tmp_path, work, result, state,
                     str(_write_fake_gh(tmp_path)))

    first = subprocess.run(["bash", str(AUTOMATION / "render-materialize.sh")],
                           capture_output=True, text=True, timeout=180,
                           env=env, cwd=str(work))
    assert first.returncode == 0, first.stdout + first.stderr
    assert "opencode/issue5-777" in first.stdout

    log = (tmp_path / "gh-state" / "calls.log").read_text(encoding="utf-8")
    assert log.count("pr create") == 1
    assert "workflow run ci.yml" in log
    assert "pr_number=41" in log
    assert "pull_request" not in log.replace("pull-request", "")

    # The published branch contains exactly the worker changes on top of main.
    diff = subprocess.run(
        [GIT, "diff", "--name-status", head + "...opencode/issue5-777"],
        cwd=work, capture_output=True, text=True, check=True)
    assert sorted(diff.stdout.splitlines()) == ["A\tadded.txt", "M\tbase.txt"]
    assert ".github/workflows" not in diff.stdout

    # A duplicate attempt reuses the open PR instead of creating a new one.
    second = subprocess.run(["bash", str(AUTOMATION / "render-materialize.sh")],
                            capture_output=True, text=True, timeout=180,
                            env=env, cwd=str(work))
    assert second.returncode == 0, second.stdout + second.stderr
    log = (tmp_path / "gh-state" / "calls.log").read_text(encoding="utf-8")
    assert log.count("pr create") == 1
    assert log.count("workflow run ci.yml") == 2


@needs_git
def test_shell_preserves_no_change_without_empty_pr(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    head = _init_repo(work)
    _link_automation(work)
    origin = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "--bare", str(origin)], check=True,
                   capture_output=True)
    subprocess.run([GIT, "remote", "add", "origin", str(origin)], cwd=work,
                   check=True, capture_output=True)
    subprocess.run([GIT, "push", "-u", "origin", "main"], cwd=work, check=True,
                   capture_output=True)

    result = _write_result(tmp_path, _result(base_sha=head, changes=[]))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"baseSha": head, "jobId": "job-1"}),
                     encoding="utf-8")
    env = _shell_env(tmp_path, work, result, state,
                     str(_write_fake_gh(tmp_path)))
    proc = subprocess.run(["bash", str(AUTOMATION / "render-materialize.sh")],
                          capture_output=True, text=True, timeout=180,
                          env=env, cwd=str(work))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no changes" in proc.stdout.lower()
    branches = subprocess.run([GIT, "branch", "--list", "opencode/*"],
                              cwd=work, capture_output=True, text=True,
                              check=True)
    assert branches.stdout.strip() == ""
    log = (tmp_path / "gh-state" / "calls.log").read_text(encoding="utf-8")
    assert "pr create" not in log
    assert "workflow run" not in log


@needs_git
def test_shell_fails_safely_on_base_mismatch(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    head = _init_repo(work)
    _link_automation(work)
    origin = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "--bare", str(origin)], check=True,
                   capture_output=True)
    subprocess.run([GIT, "remote", "add", "origin", str(origin)], cwd=work,
                   check=True, capture_output=True)
    subprocess.run([GIT, "push", "-u", "origin", "main"], cwd=work, check=True,
                   capture_output=True)

    stale = _result(base_sha="0" * 40)
    result = _write_result(tmp_path, stale)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"baseSha": "0" * 40, "jobId": "job-1"}),
                     encoding="utf-8")
    env = _shell_env(tmp_path, work, result, state,
                     str(_write_fake_gh(tmp_path)))
    proc = subprocess.run(["bash", str(AUTOMATION / "render-materialize.sh")],
                          capture_output=True, text=True, timeout=180,
                          env=env, cwd=str(work))
    assert proc.returncode != 0
    branches = subprocess.run([GIT, "branch", "--list", "opencode/*"],
                              cwd=work, capture_output=True, text=True,
                              check=True)
    assert branches.stdout.strip() == ""
    log = (tmp_path / "gh-state" / "calls.log").read_text(encoding="utf-8")
    assert "pr create" not in log
    assert "workflow run" not in log
