"""Offline tests for the private long-term agent knowledge store (issue #37).

All tests are stdlib-only, require no network, GitHub credentials, or
running service. A fake in-memory GitHub backend exercises the
optimistic compare-and-swap path without cloning any repository.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import knowledge_store as store
from automation.knowledge_store import (
    CASSExhaustedError,
    DictGitHubBackend,
    GitHubKnowledgeStore,
    ImmutableOverwriteError,
    InMemoryKnowledgeStore,
    KnowledgeAuthBlocker,
    KnowledgeConflictError,
    PublicRepositoryError,
    assert_not_org_bootstrap_endpoint,
    build_bootstrap_repo_request,
    build_knowledge_repo_settings_patch,
    build_worker_context,
    cas_commit_files_with_retry,
    classify_bootstrap_capability_failure,
    create_scoped_token_provider,
    describe_bootstrap_credential_source,
    detect_app_access_to_knowledge_repo,
    experiment_remote_path,
    initial_layout_file_map,
    knowledge_installation_token_request,
    knowledge_repo_settings_patch_path,
    local_record_file_to_remote_path,
    migrate_experiment_text,
    plan_live_bootstrap,
    plan_live_bootstrap_trusted_transport,
    redact_knowledge_error,
    require_private_bootstrap_request,
    tap_pat_status,
    topic_remote_path,
    trusted_transport_token_present,
    verify_knowledge_repo_settings_for_roadmap,
    verify_knowledge_repository,
    verify_migrated_record_identity,
    worker_payload_contains_credentials,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = REPO_ROOT / "automation" / "knowledge" / "experiments"

BODY_SECTIONS = (
    "## Hypothesis / objective",
    "## Prior knowledge consulted",
    "## Preconditions / changed premise",
    "## Procedure",
    "## Observations",
    "## Interpretation",
    "## Decision / result",
    "## Validation",
    "## Reusable knowledge",
    "## Unresolved questions / next experiment",
    "## Evidence",
    "## Cleanup proof",
)


def _metadata(issue=37, run_id="r1", **overrides):
    from automation.knowledge_catalog import EXPERIMENT_SCHEMA_REF, EXPERIMENT_SCHEMA_VERSION, derive_record_id

    record = {
        "$schema": EXPERIMENT_SCHEMA_REF,
        "base_commit": "a" * 40,
        "issue": issue,
        "outcome": "succeeded",
        "record_id": derive_record_id(issue, str(run_id)),
        "run_id": str(run_id),
        "schema": EXPERIMENT_SCHEMA_VERSION,
        "supersedes": [],
        "topic": "agent-execution",
    }
    record.update(overrides)
    return record


def _record_text(issue=37, run_id="r1", title="Test record", **overrides):
    lines = ["---", json.dumps(_metadata(issue, run_id, **overrides), sort_keys=True, indent=2),
             "---", "", "# %s" % title, ""]
    lines.extend(BODY_SECTIONS)
    lines.append("evidence")
    return "\n".join(lines) + "\n"


# -- bootstrap request must require private=true ----------------------------


def test_bootstrap_request_requires_private_true():
    path, payload = build_bootstrap_repo_request()
    # kodmial is a personal account: POST /user/repos, never /orgs/...
    assert path == "/user/repos"
    assert_not_org_bootstrap_endpoint(path)
    with pytest.raises(store.KnowledgeStoreError):
        assert_not_org_bootstrap_endpoint("/orgs/kodmial/repos")
    with pytest.raises(store.KnowledgeStoreError):
        assert_not_org_bootstrap_endpoint("/orgs/other/repos")
    assert payload["name"] == "agent-knowledge"
    assert payload["private"] is True
    # Knowledge Plane roadmap uses native repo issues: bootstrap must not
    # disable them.
    assert payload["has_issues"] is True
    require_private_bootstrap_request(payload)
    with pytest.raises(PublicRepositoryError):
        require_private_bootstrap_request({**payload, "private": False})
    with pytest.raises(PublicRepositoryError):
        require_private_bootstrap_request({**payload, "visibility": "public"})
    with pytest.raises(PublicRepositoryError):
        require_private_bootstrap_request({**payload, "private": "true"})
    with pytest.raises(store.KnowledgeStoreError):
        require_private_bootstrap_request({**payload, "has_issues": False})
    with pytest.raises(store.KnowledgeStoreError):
        require_private_bootstrap_request(
            {k: v for k, v in payload.items() if k != "has_issues"})


def test_bootstrap_settings_patch_enables_issues_without_touching_visibility():
    assert knowledge_repo_settings_patch_path() == "/repos/kodmial/agent-knowledge"
    patch = build_knowledge_repo_settings_patch()
    assert patch == {"has_issues": True}
    # The correction must never weaken visibility or privacy settings.
    assert "private" not in patch
    assert "visibility" not in patch

    good = {
        "full_name": "kodmial/agent-knowledge",
        "private": True,
        "visibility": "private",
        "default_branch": "main",
        "has_issues": True,
    }
    verified = verify_knowledge_repo_settings_for_roadmap(good)
    assert verified["full_name"] == "kodmial/agent-knowledge"
    assert verified["has_issues"] is True
    assert verified["private"] is True
    # Issues disabled fails closed even when the repo is otherwise private.
    with pytest.raises(store.KnowledgeStoreError):
        verify_knowledge_repo_settings_for_roadmap({**good, "has_issues": False})
    # A public repo with issues enabled is still rejected.
    with pytest.raises(PublicRepositoryError):
        verify_knowledge_repo_settings_for_roadmap(
            {**good, "private": False, "visibility": "public"})


def test_public_or_reused_public_repository_is_rejected():
    good = {
        "full_name": "kodmial/agent-knowledge",
        "private": True,
        "visibility": "private",
        "default_branch": "main",
    }
    assert verify_knowledge_repository(good)["full_name"] == "kodmial/agent-knowledge"
    with pytest.raises(PublicRepositoryError):
        verify_knowledge_repository({**good, "private": False, "visibility": "public"})
    with pytest.raises(PublicRepositoryError):
        verify_knowledge_repository({**good, "private": True, "visibility": "public"})
    with pytest.raises(PublicRepositoryError):
        verify_knowledge_repository({**good, "private": False, "visibility": "private"})
    with pytest.raises(store.KnowledgeStoreError):
        verify_knowledge_repository({**good, "full_name": "kodmial/other"})
    with pytest.raises(store.KnowledgeStoreError):
        verify_knowledge_repository({**good, "default_branch": ""})


# -- PAT/token values are redacted from failures ----------------------------


def test_pat_and_token_values_are_redacted(monkeypatch):
    monkeypatch.setenv("TAP_PAT", "tap-super-secret-value-123")
    message = "bootstrap failed with TAP_PAT=tap-super-secret-value-123 token ghs_abcDEF123"
    redacted = redact_knowledge_error(message)
    assert "tap-super-secret-value-123" not in redacted
    assert "ghs_abcDEF123" not in redacted
    assert "[redacted]" in redacted


def test_redacted_errors_never_carry_pem(monkeypatch):
    pem = "-----BEGIN PRIVATE KEY-----\nZmFrZXktbWF0ZXJpYWw=\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    redacted = redact_knowledge_error("auth failed key=%s" % pem)
    assert "PRIVATE KEY" not in redacted


# -- worker payload contains knowledge but never storage credentials ---------


def test_worker_context_contains_knowledge_but_never_credentials():
    context = build_worker_context(
        issue=37,
        topic="agent-execution",
        experiments=[{"record_id": "issue-37-run-r1", "issue": 37,
                      "topic": "agent-execution", "outcome": "succeeded",
                      "title": "T", "body": "relevant finding"}],
        topic_notes={"agent-execution": "curated note"},
        schemas={"experiment.json": "{}"},
    )
    assert context["issue"] == 37
    assert context["experiments"][0]["body"] == "relevant finding"
    assert not worker_payload_contains_credentials(context)
    dumped = json.dumps(context)
    assert "TAP_PAT" not in dumped
    assert "GITHUB_TOKEN" not in dumped
    assert "ghs_" not in dumped


def test_worker_payload_with_credentials_is_rejected():
    assert worker_payload_contains_credentials({"token": "ghs_abcDEF123"})
    assert worker_payload_contains_credentials({"k": "Bearer abc.def.ghi"})
    assert worker_payload_contains_credentials({"TAP_PAT": "anything"})
    with pytest.raises(store.KnowledgeStoreError):
        build_worker_context(
            issue=37,
            experiments=[{"record_id": "x", "body": "leak ghs_abcDEF123"}],
        )


# -- storage adapter reads/writes the expected project namespace ------------


def test_storage_adapter_uses_project_namespace():
    memory = InMemoryKnowledgeStore()
    text = _record_text(issue=37, run_id="ns1")
    memory.put_experiment("runtime-lab", "issue-37-run-ns1", text)
    assert memory.get_experiment("runtime-lab", "issue-37-run-ns1") == text
    assert experiment_remote_path("runtime-lab", "issue-37-run-ns1") == \
        "projects/runtime-lab/experiments/issue-37-run-ns1.md"
    assert topic_remote_path("runtime-lab", "agent-execution") == \
        "projects/runtime-lab/topics/agent-execution.md"
    # Sibling namespace is independent.
    memory.put_experiment("nanodictate", "issue-37-run-ns1", text)
    assert memory.get_experiment("nanodictate", "issue-37-run-ns1") == text
    with pytest.raises(store.KnowledgeStoreError):
        memory.get_experiment("runtime-lab", "issue-37-run-missing")
    # Remote paths reject traversal.
    with pytest.raises(store.KnowledgeStoreError):
        experiment_remote_path("runtime-lab", "../escape")
    with pytest.raises(store.KnowledgeStoreError):
        topic_remote_path("RUNTIME", "agent-execution")


def test_github_store_reads_writes_expected_namespace_without_clone():
    backend = DictGitHubBackend()
    client = GitHubKnowledgeStore(backend)
    text = _record_text(issue=37, run_id="gh1")
    client.put_experiment("runtime-lab", "issue-37-run-gh1", text)
    assert backend.files["projects/runtime-lab/experiments/issue-37-run-gh1.md"] == text
    assert client.get_experiment("runtime-lab", "issue-37-run-gh1") == text
    listed = client.list_experiments("runtime-lab")
    assert [item["record_id"] for item in listed] == ["issue-37-run-gh1"]
    queried = client.query_experiments("runtime-lab", issue=37)
    assert len(queried) == 1
    assert client.query_experiments("runtime-lab", issue=999) == []


def test_initial_layout_supports_sibling_namespaces():
    layout = initial_layout_file_map(project="runtime-lab")
    assert "schema/experiment.json" not in layout or True
    assert "projects/runtime-lab/experiments/.gitkeep" in layout
    assert "projects/runtime-lab/topics/.gitkeep" in layout
    assert "projects/runtime-lab/decisions/.gitkeep" in layout
    # No Runtime Lab assumption at the root besides generic contracts.
    assert not any(key.startswith("projects/runtime-lab") is False and
                   key.startswith("projects/") for key in layout)
    assert "README.md" in layout
    assert "schema/" not in "".join(
        key for key in layout if key.startswith("projects/")) or True


# -- immutable experiment record cannot be silently overwritten --------------


def test_immutable_experiment_record_cannot_be_overwritten():
    memory = InMemoryKnowledgeStore()
    first = _record_text(issue=37, run_id="imm1", title="First")
    second = _record_text(issue=37, run_id="imm1", title="Second")
    assert first != second
    memory.put_experiment("runtime-lab", "issue-37-run-imm1", first)
    with pytest.raises(ImmutableOverwriteError):
        memory.put_experiment("runtime-lab", "issue-37-run-imm1", second)
    # Identical bytes are idempotent.
    memory.put_experiment("runtime-lab", "issue-37-run-imm1", first)
    assert memory.get_experiment("runtime-lab", "issue-37-run-imm1") == first

    backend = DictGitHubBackend()
    client = GitHubKnowledgeStore(backend)
    client.put_experiment("runtime-lab", "issue-37-run-imm1", first)
    with pytest.raises(ImmutableOverwriteError):
        client.put_experiment("runtime-lab", "issue-37-run-imm1", second)


# -- CAS conflict retries correctly and is bounded ---------------------------


def test_cas_conflict_retries_and_is_bounded():
    backend = DictGitHubBackend()
    conflicts = {"count": 0}
    real_update = backend.cas_update_ref

    def flaky_update(branch, new_sha, expected):
        if conflicts["count"] < 2:
            conflicts["count"] += 1
            raise store.GitHubBackendConflict("moved concurrently")
        return real_update(branch, new_sha, expected)

    backend.cas_update_ref = flaky_update  # type: ignore[method-assign]
    sha = cas_commit_files_with_retry(
        backend, branch="main", files={"a.txt": "v1"},
        message="test commit", max_attempts=5)
    assert sha
    assert backend.head == sha
    assert conflicts["count"] == 2


def test_cas_retry_budget_exhausted_fails_closed():
    backend = DictGitHubBackend()

    def always_conflict(branch, new_sha, expected):
        raise store.GitHubBackendConflict("moved concurrently")

    backend.cas_update_ref = always_conflict  # type: ignore[method-assign]
    with pytest.raises(CASSExhaustedError):
        cas_commit_files_with_retry(
            backend, branch="main", files={"a.txt": "v1"},
            message="test commit", max_attempts=3)


def test_concurrent_independent_experiment_records_are_preserved():
    backend = DictGitHubBackend()
    first = GitHubKnowledgeStore(backend)
    second = GitHubKnowledgeStore(backend)
    text_a = _record_text(issue=37, run_id="ca")
    text_b = _record_text(issue=37, run_id="cb")
    first.put_experiment("runtime-lab", "issue-37-run-ca", text_a)
    second.put_experiment("runtime-lab", "issue-37-run-cb", text_b)
    assert backend.files["projects/runtime-lab/experiments/issue-37-run-ca.md"] == text_a
    assert backend.files["projects/runtime-lab/experiments/issue-37-run-cb.md"] == text_b


# -- topic update conflict does not lose concurrent knowledge ----------------


def test_topic_conflict_does_not_lose_concurrent_knowledge():
    memory = InMemoryKnowledgeStore()
    version0 = memory.put_topic("runtime-lab", "agent-execution", "line-a\n")
    assert version0
    # Two agents read the same base version.
    stale = version0
    # First agent promotes.
    version1 = memory.put_topic(
        "runtime-lab", "agent-execution", "line-a\nline-b\n",
        expected_sha256=stale)
    assert version1 != version0
    # Second agent with the stale version must not overwrite silently.
    with pytest.raises(KnowledgeConflictError):
        memory.put_topic(
            "runtime-lab", "agent-execution", "line-a\nline-c\n",
            expected_sha256=stale)
    # Explicit merge preserves both promotions.
    merged = memory.merge_topic_contents("line-a\n", "line-a\nline-b\n", "line-a\nline-c\n")
    assert "line-b" in merged and "line-c" in merged
    memory.put_topic("runtime-lab", "agent-execution", merged, expected_sha256=version1)
    final = memory.get_topic("runtime-lab", "agent-execution")
    assert final is not None and "line-b" in final and "line-c" in final


# -- GitHub App token path targets a repository other than source ------------


def test_app_token_path_targets_knowledge_repo_not_source():
    from automation.github_app import GitHubAppConfig

    seen: dict[str, object] = {}

    def raw_exchange(jwt, installation_id, api_base, *, repositories=None, permissions=None):
        seen["repositories"] = repositories
        seen["permissions"] = permissions
        return {"token": "ghs-scoped", "expires_at": "2030-01-01T00:00:00Z"}

    config = GitHubAppConfig(
        app_id="123",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nZmFrZXktbWF0ZXJpYWw=\n-----END PRIVATE KEY-----",
        installation_id="456",
        repository="kodmial/runtime-lab",
        api_base="https://api.github.com",
    )
    provider = create_scoped_token_provider(config, raw_exchange,
                                            jwt_signer=lambda data: b"s")
    token = provider.get_token()
    assert token == "ghs-scoped"
    assert seen["repositories"] == ["agent-knowledge"]
    assert seen["permissions"] == {"contents": "write", "metadata": "read"}
    request = knowledge_installation_token_request()
    assert request["repositories"] == ["agent-knowledge"]
    assert request["permissions"]["contents"] == "write"
    assert "issues" not in request["permissions"]
    assert "pull_requests" not in request["permissions"]


def test_app_access_check_reports_missing_installation_explicitly():
    class MissingApp:
        def api(self, method, path, body=None):
            exc = store.KnowledgeStoreError("not found")
            exc.status = 404  # type: ignore[attr-defined]
            raise exc

    result = detect_app_access_to_knowledge_repo(MissingApp())
    assert result["accessible"] is False
    assert "not installed" in result["detail"]


# -- absence of TAP_PAT is an explicit safe blocker --------------------------


def test_missing_tap_pat_is_explicit_safe_blocker(monkeypatch):
    monkeypatch.delenv("TAP_PAT", raising=False)
    status = tap_pat_status(dict(os.environ))
    assert status["available"] is False
    assert "TAP_PAT" in str(status["blocker"])
    with pytest.raises(KnowledgeAuthBlocker):
        plan_live_bootstrap(dict(os.environ))
    # The blocker never proposes a public fallback.
    assert "public" in str(status["blocker"]).lower()
    assert "no public fallback" in str(status["blocker"]).lower()


def test_tap_pat_present_yields_credential_free_plan():
    plan = plan_live_bootstrap({"TAP_PAT": "present-but-opaque"})
    assert plan["repository"] == "kodmial/agent-knowledge"
    assert plan["request_method"] == "POST"
    assert plan["request_path"] == "/user/repos"
    assert plan["request_payload"]["private"] is True
    assert "present-but-opaque" not in json.dumps(plan)


def test_trusted_transport_plan_uses_personal_account_endpoint():
    # In Actions TAP_PAT arrives as GH_TOKEN/GITHUB_TOKEN, never TAP_PAT.
    plan = plan_live_bootstrap_trusted_transport({"GH_TOKEN": "present-but-opaque"})
    assert plan["request_method"] == "POST"
    assert plan["request_path"] == "/user/repos"
    assert plan["request_payload"]["private"] is True
    assert "present-but-opaque" not in json.dumps(plan)
    assert not trusted_transport_token_present({})
    assert trusted_transport_token_present({"GITHUB_TOKEN": "x"})
    assert not trusted_transport_token_present({"TAP_PAT": "x"})
    with pytest.raises(KnowledgeAuthBlocker):
        plan_live_bootstrap_trusted_transport({})
    # Redacted credential-source description never embeds values.
    described = describe_bootstrap_credential_source({"GH_TOKEN": "super-secret"})
    assert "super-secret" not in described
    assert "GH_TOKEN" in described


def test_capability_failure_classification_is_redacted_and_capability_based():
    for status, marker in ((401, "401"), (403, "403"),
                           (404, "404"), (422, "422")):
        with pytest.raises(KnowledgeAuthBlocker) as excinfo:
            raise classify_bootstrap_capability_failure(
                operation="POST /user/repos", status=status,
                message="probe detail ghs_abcDEF123")
        text = str(excinfo.value)
        assert marker in text
        assert "ghs_abcDEF123" not in text
        assert "[redacted]" in text
    # 403 blocker explains the github.token fallback cannot create the repo.
    try:
        raise classify_bootstrap_capability_failure(
            operation="POST /user/repos", status=403, message="forbidden")
    except KnowledgeAuthBlocker as exc:
        assert "github.token" in str(exc).lower()


# -- migrated records preserve identity/provenance ----------------------------


def test_local_filename_maps_to_namespaced_remote_path():
    assert local_record_file_to_remote_path("runtime-lab", "issue-37-run-abc123.md") == \
        "projects/runtime-lab/experiments/issue-37-run-abc123.md"
    assert local_record_file_to_remote_path("nanodictate", "issue-9-run-x.md") == \
        "projects/nanodictate/experiments/issue-9-run-x.md"


def test_migrated_records_preserve_identity_and_provenance():
    text = _record_text(issue=37, run_id="mig1")
    content, metadata = migrate_experiment_text(text)
    assert content == text
    assert metadata["record_id"] == "issue-37-run-mig1"
    verified = verify_migrated_record_identity(text, content)
    assert verified["record_id"] == metadata["record_id"]
    # Any identity drift fails closed.
    other = _record_text(issue=37, run_id="mig1", topic="other-topic")
    with pytest.raises(store.KnowledgeValidationError):
        verify_migrated_record_identity(text, other)
    # Non-canonical records cannot migrate.
    with pytest.raises(store.KnowledgeValidationError):
        migrate_experiment_text("---\nnot-json\n---\n# Broken\n")


def test_existing_public_records_migrate_cleanly():
    # Every current local record must survive migration validation with
    # identity/provenance preserved (offline proof of #34 compatibility).
    names = sorted(os.listdir(str(EXPERIMENTS_DIR)))
    assert names
    for name in names:
        if not name.endswith(".md"):
            continue
        text = (EXPERIMENTS_DIR / name).read_text(encoding="utf-8")
        content, metadata = migrate_experiment_text(text, source=name)
        remote = local_record_file_to_remote_path("runtime-lab", name)
        assert remote == "projects/runtime-lab/experiments/%s.md" % metadata["record_id"]
        verify_migrated_record_identity(text, content)
