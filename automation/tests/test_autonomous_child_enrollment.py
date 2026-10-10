"""Offline contract tests for owner-authorized, variable-only Child enrollment."""
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "enroll_continuum_child.py"
spec = importlib.util.spec_from_file_location("enrollment", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PARENT = "example/parent"
CHILD = "example/private-child"
OTHER = "example/untrusted"


def issue(*, author="example", parent=PARENT, child=CHILD, script="automation/ci.sh"):
    return {
        "title": mod.TITLE,
        "body": mod.MARKER + "\n" + json.dumps({
            "parent": parent,
            "review_provider": "pr-agent",
            "validation_script": script,
        }),
        "repository_url": "https://api.github.com/repos/" + child,
        "user": {"login": author},
        "number": 7,
        "url": "https://api.github.com/repos/" + child + "/issues/7",
    }


class Fake:
    def __init__(self, parent_role="parent", provider="pr-agent"):
        self.vars = {
            PARENT: {
                "CONTINUUM_ROLE": parent_role,
                "CONTINUUM_REVIEW_PROVIDER": provider,
                "CONTINUUM_CHILDREN": '["existing"]',
            },
            CHILD: {},
        }
        self.operations = []
        self.failed = ""

    def __call__(self, *args):
        self.operations.append(args)
        operation, method, path, *rest = args
        if path == self.failed:
            raise mod.EnrollmentError("API unavailable")
        fields = {}
        for index in range(0, len(rest), 2):
            assert rest[index] == "-f"
            k, v = rest[index + 1].split("=", 1)
            fields[k] = v
        if path.endswith("/actions/variables") and method == "GET":
            repo = path[len("repos/"):-len("/actions/variables")]
            if repo not in self.vars:
                raise mod.EnrollmentError("missing variables")
            return {"variables": [
                {"name": k, "value": v} for k, v in self.vars[repo].items()
            ]}
        if path.endswith("/actions/variables") and method == "POST":
            repo = path[len("repos/"):-len("/actions/variables")]
            self.vars[repo][fields["name"]] = fields["value"]
            return {}
        if "/actions/variables/" in path and method == "PATCH":
            prefix, name = path.rsplit("/", 1)
            repo = prefix[len("repos/"):-len("/actions/variables")]
            self.vars[repo][name] = fields["value"]
            return {}
        if path.endswith("/pulls") and method == "GET":
            return []
        if "/contents/" in path and method == "GET":
            return {"type": "file"}
        if "/issues/" in path and method == "PATCH":
            assert fields["state"] == "closed"
            return {}
        raise AssertionError(f"Unexpected API call: {args}")


def test_enrollment_preserves_existing_parent_children_and_configures_child(capsys):
    f = Fake()
    mod.enroll(issue(), PARENT, get=f, new_id=lambda: "new-opaque")
    assert json.loads(f.vars[PARENT]["CONTINUUM_CHILDREN"]) == [
        "existing", "new-opaque"
    ]
    child = f.vars[CHILD]
    assert child == {
        "CONTINUUM_ROLE": "child",
        "CONTINUUM_CHILD_ID": "new-opaque",
        "CONTINUUM_PARENT": PARENT,
        "CONTINUUM_REVIEW_PROVIDER": "pr-agent",
        "CONTINUUM_VALIDATION_SCRIPT": "automation/ci.sh",
    }
    assert capsys.readouterr().out == "::add-mask::" + CHILD + "\n"


def test_replayed_enrollment_keeps_child_id_and_allow_list():
    f = Fake()
    req = issue()
    mod.enroll(req, PARENT, get=f, new_id=lambda: "new-opaque")
    first = list(f.operations)
    mod.enroll(req, PARENT, get=f, new_id=lambda: "different")
    assert f.vars[CHILD]["CONTINUUM_CHILD_ID"] == "new-opaque"
    assert json.loads(f.vars[PARENT]["CONTINUUM_CHILDREN"]) == [
        "existing", "new-opaque"
    ]
    assert not any("different" in repr(x) for x in f.operations[len(first):])


@pytest.mark.parametrize("change", [
    lambda x: x.update(user={"login": "not-owner"}),
    lambda x: x.update(repository_url="https://api.github.com/repos/attacker/r"),
    lambda x: x.update(title="ordinary issue"),
    lambda x: x.update(body="hello"),
    lambda x: x.update(body=mod.MARKER + "\n{}"),
    lambda x: x.update(body=mod.MARKER + "\n" + json.dumps({
        "parent": OTHER, "review_provider": "pr-agent",
        "validation_script": "automation/ci.sh",
    })),
    lambda x: x.update(body=mod.MARKER + "\n" + json.dumps({
        "parent": PARENT, "review_provider": "coderabbit",
        "validation_script": "automation/ci.sh",
    })),
    lambda x: x.update(body=mod.MARKER + "\n" + json.dumps({
        "parent": PARENT, "review_provider": "pr-agent",
        "validation_script": "../hijack.sh",
    })),
])
def test_invalid_requests_fail_without_writing(change):
    f = Fake()
    req = issue()
    change(req)
    with pytest.raises(mod.EnrollmentError):
        mod.enroll(req, PARENT, get=f)
    assert not any(args[1] in ("PATCH", "POST") for args in f.operations)


@pytest.mark.parametrize("key,value", [
    ("CONTINUUM_ROLE", "parent"),
    ("CONTINUUM_PARENT", OTHER),
    ("CONTINUUM_REVIEW_PROVIDER", "unknown"),
    ("CONTINUUM_CHILD_ID", "not valid id"),
])
def test_incompatible_child_relationship_rejected(key, value):
    f = Fake()
    f.vars[CHILD][key] = value
    with pytest.raises(mod.EnrollmentError):
        mod.enroll(issue(), PARENT, get=f)
    assert not any(args[1] in ("PATCH", "POST") for args in f.operations)


def test_no_implicit_parent_or_review_provider():
    for role, provider in [("child", "pr-agent"), ("parent", "none")]:
        f = Fake(parent_role=role, provider=provider)
        with pytest.raises(mod.EnrollmentError):
            mod.enroll(issue(), PARENT, get=f)


def test_unavailable_api_does_not_write_anything():
    f = Fake()
    f.failed = "repos/" + CHILD + "/actions/variables"
    with pytest.raises(mod.EnrollmentError):
        mod.enroll(issue(), PARENT, get=f)
    assert not any(args[1] in ("PATCH", "POST") for args in f.operations)


def test_duplicate_existing_allowed_ids_rejected():
    f = Fake()
    f.vars[PARENT]["CONTINUUM_CHILDREN"] = '["existing","existing"]'
    with pytest.raises(mod.EnrollmentError):
        mod.enroll(issue(), PARENT, get=f)


def test_collision_rejected():
    f = Fake()
    with pytest.raises(mod.EnrollmentError):
        mod.enroll(issue(), PARENT, get=f, new_id=lambda: "existing")


def test_no_tracked_relation_literals():
    source = SOURCE.read_text()
    assert "repo_ids=(" not in source
    assert "kodmial/" not in source
    assert "CHILD_REPOSITORIES=" not in source


def test_owner_approved_legacy_review_provider_transition():
    f = Fake()
    f.vars[CHILD]["CONTINUUM_REVIEW_PROVIDER"] = "coderabbit"
    mod.enroll(issue(), PARENT, get=f, new_id=lambda: "new-opaque")
    assert f.vars[CHILD]["CONTINUUM_REVIEW_PROVIDER"] == "pr-agent"


def test_exact_head_failure_is_replayed_on_child_enrollment():
    class WithPR(Fake):
        def __call__(self, *args):
            if args[1] == "GET" and args[2] == "repos/" + CHILD + "/pulls":
                return [{
                    "number": 27, "draft": False,
                    "head": {"sha": "a" * 40, "repo": {"full_name": CHILD}},
                }]
            if args[1] == "GET" and args[2] == "repos/" + CHILD + "/actions/runs":
                return {"workflow_runs": [{
                    "id": 12345, "name": "CI", "head_sha": "a" * 40,
                    "status": "completed", "conclusion": "failure",
                    "pull_requests": [{"number": 27}],
                }]}
            if args[1] == "POST" and args[2] == "repos/" + CHILD + "/actions/runs/12345/rerun-failed-jobs":
                self.operations.append(args)
                return {}
            return super().__call__(*args)

    f = WithPR()
    mod.enroll(issue(), PARENT, get=f, new_id=lambda: "new-opaque")
    assert any(
        args[1] == "POST" and args[2].endswith("/rerun-failed-jobs")
        for args in f.operations
    )


def test_recheck_never_replays_stale_or_draft_head():
    class Stale(Fake):
        def __call__(self, *args):
            if args[1] == "GET" and args[2] == "repos/" + CHILD + "/pulls":
                return [
                    {"number": 27, "draft": True, "head": {
                        "sha": "a" * 40, "repo": {"full_name": CHILD}}},
                    {"number": 28, "draft": False, "head": {
                        "sha": "b" * 40, "repo": {"full_name": CHILD}}},
                ]
            if args[1] == "GET" and args[2] == "repos/" + CHILD + "/actions/runs":
                return {"workflow_runs": [{
                    "id": 888, "name": "CI", "head_sha": "a" * 40,
                    "status": "completed", "conclusion": "failure",
                    "pull_requests": [{"number": 28}],
                }]}
            return super().__call__(*args)

    f = Stale()
    assert mod.recheck_open_pull_requests(CHILD, get=f) == 0
    assert not any(args[1] == "POST" for args in f.operations)
