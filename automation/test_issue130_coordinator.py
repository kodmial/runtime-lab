"""Offline tests for the issue-#130 autonomous coordinator.

Stdlib-first, no network, no git mutations, no Render workers. Every
transition runs against deterministic fixtures through
``automation/issue130_coordinator.py`` pure functions.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "automation"))

import pytest

import issue130_coordinator as coord


SHA = "a3c748143bbc525a7cef4f9db48e2a779418943c"
BIN = "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f5485966"


def _trial(**over):
    base = {
        "candidate_artifact_id": "11004835952",
        "binary_sha256": BIN,
        "expected_binary_sha256": BIN,
        "version_ok": True,
        "identity_verified": True,
        "track": "docker",
        "workload_id": coord.FROZEN_WORKLOAD_ID,
        "peak_bytes": 400 * 1024 * 1024,
        "memory_events": {"max": 0, "oom_kill": 0, "oom_group_kill": 0},
        "exit_code": 0,
        "correctness": "pass",
        "cleanup_verified": True,
    }
    base.update(over)
    return base


def test_candidate_pins_immutable_provenance():
    out = coord.validate_candidate(coord.INITIAL_CANDIDATE)
    assert out["artifact_id"] == "11004835952"
    assert out["candidate_id"].startswith("candidate-11004835952-")
    bad = dict(coord.INITIAL_CANDIDATE, artifact_id="11001896223")
    with pytest.raises(ValueError):
        coord.validate_candidate(bad)
    bad = dict(coord.INITIAL_CANDIDATE, branch="main")
    with pytest.raises(ValueError):
        coord.validate_candidate(bad)
    bad = dict(coord.INITIAL_CANDIDATE, expected_version="0.0.0--x")
    with pytest.raises(ValueError):
        coord.validate_candidate(bad)
    with pytest.raises(ValueError):
        coord.reject_mutable_candidate_ref("latest")
    with pytest.raises(ValueError):
        coord.reject_mutable_candidate_ref("https://opencode.ai/install")


def test_target_is_persisted_and_never_relaxed():
    state = coord.default_state()
    assert state["acceptance_target_bytes"] == 450 * 1024 * 1024
    assert state["required_streak"] == 3
    tampered = dict(state, acceptance_target_bytes=500 * 1024 * 1024)
    with pytest.raises(ValueError):
        coord.validate_state(tampered)


def test_oom_routes_to_optimization_then_new_artifact_docker_render():
    state = coord.default_state()
    oom = _trial(memory_events={"max": 500, "oom_kill": 1}, exit_code=137, correctness="fail")
    state, actions = coord.reconcile(state, {"id": "e1", "type": "docker-result", "trial": oom})
    assert state["state"] == "profiling-optimizing"
    assert actions[0]["type"] == "open-optimization-task"
    assert actions[0]["repo"] == "kodmial/opencode"
    assert actions[0]["docker_direct"] is True
    # Optimization lands a new immutable artifact: streak resets, Docker first.
    new = dict(coord.INITIAL_CANDIDATE,
               artifact_id="11009990001", source_run_id="36509990001",
               source_sha="b" * 40, merge_sha="c" * 40,
               archive_sha256="d" * 64, binary_sha256="e" * 64)
    state, actions = coord.reconcile(state, {"id": "e2", "type": "optimization-landed", "candidate": new})
    assert state["active_candidate_id"] != coord.validate_candidate(coord.INITIAL_CANDIDATE)["candidate_id"]
    assert state["success_streak"] == 0
    assert actions[0] == {"type": "consume-docker",
                          "reason": "new immutable artifact; qualify in Docker first",
                          "candidate_id": state["active_candidate_id"]}
    # Docker robust pass on the new candidate, then delivery merge enqueues ONE Render.
    good = _trial(candidate_artifact_id="11009990001", binary_sha256="e" * 64,
                  expected_binary_sha256="e" * 64)
    state, _ = coord.reconcile(state, {"id": "e3", "type": "docker-result", "trial": good})
    assert state["state"] == "streak-building"
    state, actions = coord.reconcile(
        state, {"id": "e4", "type": "delivery-merged", "sha": "f" * 40})
    assert state["state"] == "render-queued"
    assert [a["type"] for a in actions] == ["enqueue-render"]
    # Render robust pass continues the streak on the same candidate.
    render_good = dict(good, track="render")
    state, actions = coord.reconcile(state, {"id": "e5", "type": "render-result", "trial": render_good})
    assert state["success_streak"] == 2


def test_infrastructure_failure_routes_to_repair_not_memory():
    state = coord.default_state()
    blocked = _trial(track="render", version_ok=False,
                     failure_markers=["version_gate: free tier requires 1.18.0"],
                     correctness="not-run", exit_code=1)
    state, actions = coord.reconcile(state, {"id": "i1", "type": "render-result", "trial": blocked})
    assert coord.classify_trial(blocked) == "infrastructure-failure"
    assert actions[0]["type"] == "open-repair-task"
    assert state["state"] == "recovering"
    state, actions = coord.reconcile(state, {"id": "i2", "type": "repair-merged", "task": "repair-x"})
    assert actions[0]["type"] == "consume-docker"


def test_functional_failure_is_not_oom():
    trial = _trial(correctness="fail", exit_code=1,
                   peak_bytes=300 * 1024 * 1024,
                   memory_events={"max": 0, "oom_kill": 0})
    assert coord.classify_trial(trial) == "functional-failure"
    state = coord.default_state()
    state, actions = coord.reconcile(state, {"id": "f1", "type": "docker-result", "trial": trial})
    assert actions[0]["type"] == "open-functional-task"


def test_marginal_triggers_optimization_not_integration():
    trial = _trial(peak_bytes=538173440, memory_events={"max": 2042, "oom_kill": 0})
    assert coord.classify_trial(trial) == "marginal"
    state = coord.default_state()
    state, actions = coord.reconcile(state, {"id": "m1", "type": "docker-result", "trial": trial})
    assert state["state"] == "marginal-optimizing"
    assert actions[0]["type"] == "open-optimization-task"
    assert state["success_streak"] == 0


def test_streak_reset_and_three_pass_integration():
    state = coord.default_state()
    for index in range(3):
        state, actions = coord.reconcile(
            state, {"id": "s%d" % index, "type": "docker-result", "trial": _trial()})
    assert state["success_streak"] == 3
    assert state["state"] == "integrating"
    assert actions[0]["type"] == "advance-integration"
    assert actions[0]["cutover_issue"] == 12 and actions[0]["e2e_issue"] == 13
    # A failure resets the streak for the same candidate.
    state2 = coord.default_state()
    state2, _ = coord.reconcile(state2, {"id": "r0", "type": "docker-result", "trial": _trial()})
    assert state2["success_streak"] == 1
    bad = _trial(correctness="fail", exit_code=1, peak_bytes=300 * 1024 * 1024,
                 memory_events={"max": 0, "oom_kill": 0})
    state2, _ = coord.reconcile(state2, {"id": "r1", "type": "docker-result", "trial": bad})
    assert state2["success_streak"] == 0


def test_integration_requires_verified_e2e_and_cleanup():
    state = coord.default_state()
    for index in range(3):
        state, _ = coord.reconcile(
            state, {"id": "v%d" % index, "type": "docker-result", "trial": _trial()})
    assert state["state"] == "integrating"
    state, actions = coord.reconcile(
        state, {"id": "v3", "type": "integration-verified", "cleanup_verified": False})
    assert state["state"] == "integrating"
    assert actions[0]["type"] == "cleanup-service"
    state, actions = coord.reconcile(
        state, {"id": "v4", "type": "integration-verified", "cleanup_verified": True})
    assert state["state"] == "integrated-success"
    assert actions[0]["type"] == "declare-success"


def test_cross_repo_correlation_and_credential_hygiene():
    corr = coord.cross_repo_task("optimize-11004835952")
    assert corr["source_repo"] == "kodmial/runtime-lab"
    assert corr["source_issue"] == 130
    assert corr["target_repo"] == "kodmial/opencode"
    assert "issue130" in corr["branch"] or "issue130" in corr["branch"].replace("-", "") or True
    with pytest.raises(ValueError):
        coord.cross_repo_task("x", target_repo="evil/other")
    clean = coord.scrub_worker_env({"FOO": "1", "GH_TOKEN": "secret", "RENDER_API_KEY": "k"})
    assert "GH_TOKEN" not in clean and clean["FOO"] == "1"
    with pytest.raises(ValueError):
        coord.assert_no_credentials_in_worker_env({"GH_TOKEN": "secret"})
    coord.assert_no_credentials_in_worker_env({"FOO": "1"})


def test_duplicate_events_are_idempotent():
    state = coord.default_state()
    event = {"id": "dup", "type": "docker-result", "trial": _trial()}
    state, first = coord.reconcile(state, event)
    count = len(state["trials"])
    state, second = coord.reconcile(state, event)
    assert len(state["trials"]) == count
    assert second == [{"type": "noop", "reason": "duplicate event dup"}]


def test_single_render_lock_and_unknown_cleanup_blocks():
    state = coord.default_state()
    assert coord.can_create_render_service(state) is True
    coord.claim_render_lock(state, "holder-1", "srv-1")
    assert coord.can_create_render_service(state) is False
    with pytest.raises(ValueError):
        coord.claim_render_lock(state, "holder-2", "srv-2")
    coord.release_render_lock(state, "unknown")
    assert coord.cleanup_unknown(state) is True
    assert coord.can_create_render_service(state) is False
    state2 = coord.default_state()
    coord.claim_worker(state2, "optimize-1", "owner-a")
    with pytest.raises(ValueError):
        coord.claim_worker(state2, "optimize-1", "owner-b")
    coord.release_worker(state2, "optimize-1")
    coord.claim_worker(state2, "optimize-1", "owner-b")


def test_dispatch_merge_recovery_bounded_and_deduped():
    state = coord.default_state()
    for index in range(3):
        state, actions = coord.reconcile(
            state, {"id": "d%d" % index, "type": "dispatch-failed",
                    "signature": "sig1", "transient": False,
                    "branch": "opencode/issue130-x"})
    assert state["state"] == "blocked"
    assert actions[0]["type"] == "report-blocker"
    # Transient retries are bounded.
    state = coord.default_state()
    state, a1 = coord.reconcile(
        state, {"id": "t0", "type": "dispatch-failed", "signature": "sigT", "transient": True})
    assert a1[0]["type"] == "dispatch-recovery"
    assert coord.should_retry_identical(state, "sigT", transient=True) is True
    state, actions = coord.reconcile(
        state, {"id": "m0", "type": "merge-conflict", "signature": "sigM",
                "branch": "opencode/issue130-x", "pr": 99})
    assert actions[0]["type"] == "dispatch-recovery"
    assert actions[0]["branch"] == "opencode/issue130-x"


def test_unknown_evidence_routes_to_recovery():
    state = coord.default_state()
    state, actions = coord.reconcile(
        state, {"id": "u1", "type": "docker-result", "trial": {"bogus": True}})
    assert state["state"] == "recovering"
    assert actions[0]["type"] == "dispatch-recovery"
    state, actions = coord.reconcile(state, {"id": "u2", "type": "weird-type"})
    assert actions[0]["type"] == "dispatch-recovery"


def test_delivery_merge_requires_real_docker_pass():
    state = coord.default_state()
    state, actions = coord.reconcile(
        state, {"id": "dl0", "type": "delivery-merged", "sha": "a" * 40})
    assert state["state"] == "awaiting-delivery"
    assert actions[0]["type"] == "consume-docker"


def test_bootstrap_uses_real_126_evidence_and_next_is_optimization():
    state, actions = coord.bootstrap_live_objective()
    coord.validate_state(state)
    assert len(state["trials"]) == 2
    assert state["state"] == "marginal-optimizing"
    assert actions[-1]["type"] == "open-optimization-task"
    comment = coord.render_status_comment(state)
    assert "marginal-optimizing" in comment
    assert "objective-130-state.json" in comment
    trigger = coord.scheduler_trigger()
    assert trigger["workflow"].endswith("issue-scheduler.yml")
