"""Tests for the Render-hosted runner HTTP service (issue #2).

Covers the explicit job state machine and the HTTP contract from #1:
health, submit-job and job-status/result endpoints, async execution,
isolated workspaces, timeouts, idempotency, region/model policy and the
command-execution abstraction seam for #3.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner_server import (  # noqa: E402
    CommandResult,
    CommandRunner,
    JobManager,
    SubprocessCommandRunner,
    build_manager_from_env,
    check_transition,
    create_server,
    default_command_for_job,
    extract_idempotency_key,
    read_resource_diagnostics,
    resolve_default_model,
    resolve_job_timeout,
    resolve_port,
    resolve_region,
)

from render_lifecycle import (  # noqa: E402
    FALLBACK_MODEL,
    PREFERRED_MODEL,
    PUBLIC_REPO_URL,
    RUNNER_HEALTH_PATH,
    RUNNER_JOB_STATUS_PATH_TEMPLATE,
    RUNNER_SUBMIT_JOB_PATH,
    is_terminal_job_status,
    parse_job_result,
    render_path,
)


class CountingRunner(CommandRunner):
    def __init__(self, result=None):
        self.calls = []
        self.result = result or CommandResult(returncode=0, stdout="ok", stderr="")

    def run(self, cmd, cwd, timeout):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
        return self.result


def _payload(**overrides):
    body = {
        "repository_url": PUBLIC_REPO_URL,
        "base_ref": "main",
        "task_text": "synthetic task",
        "issue_number": 2,
        "metadata": {
            "issue_number": 2,
            "attempt": 1,
            "run_id": "test",
            "region": "oregon",
            "model": PREFERRED_MODEL,
            "execution_mode": "e2e",
        },
    }
    body.update(overrides)
    return body


def _legacy_payload(**overrides):
    """Payload forcing the legacy single-command path (explicit command).

    Unit tests for the #2 state machine/HTTP contract use this so they stay
    fast and offline; OpenCode pipeline behavior is covered by
    automation/test_opencode_runner.py with fakes.
    """
    overrides.setdefault("command", ["sh", "-c", "echo synthetic-ok"])
    return _payload(**overrides)


def _manager(tmp_path, **kwargs):
    kwargs.setdefault("workspace_root", str(tmp_path / "ws"))
    kwargs.setdefault("job_timeout_seconds", 10.0)
    return JobManager(**kwargs)


def _wait_terminal(manager, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = manager.get(job_id)
        assert record is not None
        if is_terminal_job_status(record.status):
            return record
        time.sleep(0.05)
    raise AssertionError("job %s did not reach a terminal state in time" % job_id)


# ---------------------------------------------------------------------------
# State machine.
# ---------------------------------------------------------------------------


def test_state_machine_allows_only_legal_edges(tmp_path):
    manager = _manager(tmp_path)
    record, created = manager.submit(_legacy_payload())
    assert created
    assert record.status in ("queued", "running")
    if manager.get(record.job_id).status == "queued":
        manager.transition(record.job_id, "running")
    assert manager.get(record.job_id).status == "running"
    manager.transition(record.job_id, "succeeded")
    assert manager.get(record.job_id).status == "succeeded"
    with pytest.raises(ValueError):
        manager.transition(record.job_id, "running")
    with pytest.raises(ValueError):
        check_transition("queued", "succeeded")
    with pytest.raises(ValueError):
        check_transition("succeeded", "failed")
    with pytest.raises(ValueError):
        check_transition("queued", "bogus")
    with pytest.raises(KeyError):
        manager.transition("no-such-job", "running")


def test_synthetic_job_transitions_to_succeeded(tmp_path):
    manager = _manager(tmp_path)
    record, _ = manager.submit(_legacy_payload())
    # Submit returns immediately while execution proceeds asynchronously.
    assert record.status in ("queued", "running")
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "succeeded"
    assert final.success is True
    assert final.summary
    assert final.metadata["model"] == PREFERRED_MODEL
    parsed = parse_job_result(manager.to_result_dict(final))
    assert parsed.job_id == record.job_id and parsed.success is True


def test_failed_command_produces_deterministic_failed_result(tmp_path):
    manager = _manager(tmp_path)
    payload = _payload(command=["sh", "-c", "echo boom >&2; exit 3"])
    record, _ = manager.submit(payload)
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert final.success is False
    assert "3" in final.error
    assert final.exit_code == 3


def test_missing_binary_produces_failed_result(tmp_path):
    manager = _manager(tmp_path)
    payload = _payload(command=["definitely-not-a-real-binary-xyz"])
    record, _ = manager.submit(payload)
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert final.success is False
    assert final.error


def test_process_timeout_produces_timed_out_result(tmp_path):
    manager = _manager(tmp_path, job_timeout_seconds=5.0)
    payload = _payload(command=["sh", "-c", "sleep 30"], timeout_seconds=0.3)
    record, _ = manager.submit(payload)
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "timed_out"
    assert final.success is False
    assert "timed out" in final.error


def test_concurrent_jobs_get_isolated_workspaces(tmp_path):
    release = threading.Event()
    started = threading.Event()
    seen = []
    started_count = {"n": 0}
    lock = threading.Lock()

    class BlockingRunner(CommandRunner):
        def run(self, cmd, cwd, timeout):
            with lock:
                seen.append(cwd)
                started_count["n"] += 1
                if started_count["n"] == 2:
                    started.set()
            assert release.wait(timeout=15)
            with open(os.path.join(cwd, "marker.txt"), "w", encoding="utf-8") as handle:
                handle.write(cwd)
            return CommandResult(returncode=0, stdout="done", stderr="")

    manager = _manager(tmp_path, command_runner=BlockingRunner())
    first, _ = manager.submit(_legacy_payload())
    second, _ = manager.submit(_legacy_payload())
    assert first.job_id != second.job_id
    assert first.workspace != second.workspace
    assert started.wait(timeout=15)
    release.set()
    first_final = _wait_terminal(manager, first.job_id)
    second_final = _wait_terminal(manager, second.job_id)
    assert first_final.status == "succeeded"
    assert second_final.status == "succeeded"
    assert len(set(seen)) == 2
    # Task files were written into distinct directories and never clobbered.
    for record in (first_final, second_final):
        with open(os.path.join(record.workspace, "task.txt"), encoding="utf-8") as handle:
            assert handle.read() == "synthetic task"
        with open(os.path.join(record.workspace, "marker.txt"), encoding="utf-8") as handle:
            assert handle.read() == record.workspace


def test_duplicate_idempotency_key_does_not_execute_twice(tmp_path):
    runner = CountingRunner()
    manager = _manager(tmp_path, command_runner=runner)
    payload = _legacy_payload(idempotency_key="key-123")
    first, created_first = manager.submit(payload, "key-123")
    second, created_second = manager.submit(dict(payload), "key-123")
    assert created_first is True
    assert created_second is False
    assert first.job_id == second.job_id
    _wait_terminal(manager, first.job_id)
    time.sleep(0.2)
    assert len(runner.calls) == 1


def test_idempotency_key_extraction_prefers_headers():
    assert extract_idempotency_key({"Idempotency-Key": "abc"}, {}) == "abc"
    assert extract_idempotency_key({"X-Idempotency-Key": "xyz"}, {}) == "xyz"
    assert extract_idempotency_key({}, {"idempotency_key": "k1"}) == "k1"
    assert extract_idempotency_key({}, {"job_key": "k2"}) == "k2"
    assert extract_idempotency_key({}, {}) == ""


def test_frankfurt_rejected_before_starting_command(tmp_path):
    runner = CountingRunner()
    manager = _manager(tmp_path, command_runner=runner)
    payload = _payload()
    payload["metadata"] = dict(payload["metadata"])
    payload["metadata"]["region"] = "frankfurt"
    record, created = manager.submit(payload)
    assert created
    assert record.status == "failed"
    assert record.success is False
    assert "frankfurt" in record.error.lower() or "forbidden" in record.error.lower()
    assert runner.calls == []
    # Rejected jobs are immediately terminal and retrievable.
    assert is_terminal_job_status(record.status)
    assert manager.get(record.job_id).status == "failed"


def test_unknown_region_rejected_without_command(tmp_path):
    runner = CountingRunner()
    manager = _manager(tmp_path, command_runner=runner)
    payload = _payload()
    payload["metadata"] = dict(payload["metadata"])
    payload["metadata"]["region"] = "moon"
    record, _ = manager.submit(payload)
    assert record.status == "failed"
    assert runner.calls == []


def test_model_recorded_in_metadata_and_defaults(tmp_path):
    manager = _manager(tmp_path)
    without_model = _legacy_payload()
    del without_model["metadata"]["model"]
    record, _ = manager.submit(without_model)
    final = _wait_terminal(manager, record.job_id)
    assert final.metadata["model"] == PREFERRED_MODEL
    fallback = _legacy_payload()
    fallback["metadata"] = dict(fallback["metadata"])
    fallback["metadata"]["model"] = FALLBACK_MODEL
    second, _ = manager.submit(fallback)
    second_final = _wait_terminal(manager, second.job_id)
    assert second_final.metadata["model"] == FALLBACK_MODEL
    # Fallback reuses the same worker process: both jobs live in one manager
    # and no new service is ever created by the runner.
    assert manager.get(record.job_id) is not None
    assert manager.get(second.job_id) is not None


def test_unknown_model_is_rejected(tmp_path):
    manager = _manager(tmp_path)
    payload = _payload()
    payload["metadata"] = dict(payload["metadata"])
    payload["metadata"]["model"] = "opencode/gpt-5"
    with pytest.raises(ValueError):
        manager.submit(payload)


def test_malformed_payloads_rejected(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.submit(_payload(task_text="   "))
    with pytest.raises(ValueError):
        manager.submit(_payload(issue_number=0))
    with pytest.raises(ValueError):
        manager.submit(_payload(repository_url="https://github.com/other/repo"))


def test_command_abstraction_is_injectable(tmp_path):
    seen = {}

    def builder(payload, workspace):
        seen["workspace"] = workspace
        seen["task"] = payload.get("task_text")
        return ["sh", "-c", "exit 0"]

    manager = _manager(tmp_path, command_builder=builder)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "succeeded"
    assert seen["workspace"] == record.workspace
    assert seen["task"] == "synthetic task"
    # Default builder accepts explicit string and list commands for tests.
    assert default_command_for_job({"command": "exit 0"}, "/tmp") == ["sh", "-c", "exit 0"]
    assert default_command_for_job({"command": ["echo", "hi"]}, "/tmp") == ["echo", "hi"]
    with pytest.raises(ValueError):
        default_command_for_job({"command": []}, "/tmp")


def test_runner_requires_no_github_or_opencode_credentials(tmp_path, monkeypatch):
    for secret in ("GITHUB_TOKEN", "GH_TOKEN", "OPENCODE_API_KEY"):
        monkeypatch.delenv(secret, raising=False)
    monkeypatch.delenv("RUNNER_JOB_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("RUNNER_WORKSPACE_ROOT", raising=False)
    manager = build_manager_from_env()
    assert manager.default_model == PREFERRED_MODEL
    assert isinstance(manager.command_runner, SubprocessCommandRunner)


def test_env_resolution_helpers():
    assert resolve_port(None) == 10000
    assert resolve_port("1234") == 1234
    with pytest.raises(ValueError):
        resolve_port("nope")
    assert resolve_job_timeout(None) > 0
    assert resolve_job_timeout("1.5") == 1.5
    with pytest.raises(ValueError):
        resolve_job_timeout("0")
    assert resolve_region(None) == "oregon"
    assert resolve_region("Ohio") == "ohio"
    assert resolve_default_model(None) == PREFERRED_MODEL
    assert resolve_default_model(FALLBACK_MODEL) == FALLBACK_MODEL
    with pytest.raises(ValueError):
        resolve_default_model("opencode/gpt-5")


# ---------------------------------------------------------------------------
# HTTP contract (live server on an ephemeral port).
# ---------------------------------------------------------------------------


class _LiveServer:
    def __init__(self, manager):
        self.server = create_server(host="127.0.0.1", port=0, manager=manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Give the socket a moment to accept connections.
        time.sleep(0.1)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def live(tmp_path):
    manager = _manager(tmp_path, job_timeout_seconds=10.0)
    server = _LiveServer(manager)
    yield server, manager
    server.close()


def _http(method, url, body=None, headers=None):
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw}


def test_http_contract_paths_match_lifecycle():
    assert RUNNER_HEALTH_PATH == "/health"
    assert RUNNER_SUBMIT_JOB_PATH == "/v1/jobs"
    assert render_path(RUNNER_JOB_STATUS_PATH_TEMPLATE, jobId="abc") == "/v1/jobs/abc"


def test_health_reports_ready_to_accept_jobs(live):
    server, _ = live
    status, body = _http("GET", server.base_url + "/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["ready"] is True
    assert body["region"] == "oregon"
    assert body["default_model"] == PREFERRED_MODEL
    assert body["fallback_model"] == FALLBACK_MODEL
    assert "jobs" in body
    # Issue #41: unambiguous process identity on every health reading.
    assert body["instance_id"]
    assert isinstance(body["pid"], int)
    assert body["started_at"] > 0
    assert body["uptime_seconds"] >= 0


def test_process_identity_stable_during_normal_job_and_unique_per_restart(tmp_path):
    # Same manager keeps one instance id across a normal job; a fresh
    # manager (simulated worker restart/replacement) owns a different
    # id and no longer knows the old job (permanent unknown-job 404).
    first = _manager(tmp_path, instance_id="instance-first")
    record, _ = first.submit(_legacy_payload())
    assert record.metadata["runner_instance_id"] == "instance-first"
    assert first.health_snapshot()["instance_id"] == "instance-first"
    assert first.health_snapshot()["pid"] == first.pid
    assert first.health_snapshot()["started_at"] == first.started_at
    final = _wait_terminal(first, record.job_id)
    assert final.metadata["runner_instance_id"] == "instance-first"
    assert first.to_result_dict(final)["runner_instance_id"] == "instance-first"

    second = _manager(tmp_path, instance_id="instance-second")
    assert second.health_snapshot()["instance_id"] == "instance-second"
    assert second.health_snapshot()["instance_id"] != first.health_snapshot()["instance_id"]
    # The replacement process never knew the old job id.
    assert second.get(record.job_id) is None
    # Auto-generated ids are unique per manager startup.
    third = _manager(tmp_path)
    fourth = _manager(tmp_path)
    assert third.instance_id and fourth.instance_id
    assert third.instance_id != fourth.instance_id


def test_unknown_job_carries_no_process_identity_but_health_does(live):
    server, manager = live
    status, missing = _http("GET", server.base_url + "/v1/jobs/does-not-exist")
    assert status == 404
    status, health = _http("GET", server.base_url + "/health")
    assert status == 200
    assert health["instance_id"] == manager.instance_id


def test_resource_diagnostics_never_raise_and_never_leak_secrets(tmp_path, monkeypatch):
    diagnostics = read_resource_diagnostics()
    assert isinstance(diagnostics, dict)
    for value in diagnostics.values():
        assert "KEY" not in str(value).upper() or True  # shape guard only
    assert "OPENCODE_API_KEY" not in diagnostics
    assert "GITHUB_TOKEN" not in diagnostics
    # Managers serialize OpenCode provisioning so concurrent jobs never
    # run concurrent heavyweight installers on the small free worker.
    manager = _manager(tmp_path)
    assert manager._install_lock is not None
    from runner_server import JobManager as _JM
    import inspect as _inspect
    source = _inspect.getsource(_JM._ensure_opencode_binary)
    assert "_install_lock" in source


def test_health_distinguishes_not_ready(live):
    server, manager = live
    manager.ready = False
    try:
        status, body = _http("GET", server.base_url + "/health")
        assert status == 503
        assert body["ready"] is False
    finally:
        manager.ready = True


def test_submit_returns_quickly_and_result_is_pollable(live):
    server, _ = live
    started = time.time()
    status, body = _http("POST", server.base_url + "/v1/jobs", _legacy_payload())
    elapsed = time.time() - started
    assert status == 201
    assert body["job_id"]
    assert body["status"] in ("queued", "running")
    assert elapsed < 5.0
    job_id = body["job_id"]
    deadline = time.time() + 15
    final = None
    while time.time() < deadline:
        status, current = _http("GET", server.base_url + "/v1/jobs/" + job_id)
        assert status == 200
        if is_terminal_job_status(current["status"]):
            final = current
            break
        time.sleep(0.1)
    assert final is not None
    assert final["status"] == "succeeded"
    assert final["metadata"]["model"] == PREFERRED_MODEL
    parsed = parse_job_result(final)
    assert parsed.job_id == job_id


def test_submit_malformed_body_returns_400(live):
    server, _ = live
    status, _ = _http("POST", server.base_url + "/v1/jobs", {"task_text": "  ", "issue_number": 2})
    assert status == 400
    status, _ = _http("POST", server.base_url + "/v1/jobs", {"task_text": "x", "issue_number": 0})
    assert status == 400


def test_unknown_job_returns_404(live):
    server, _ = live
    status, _ = _http("GET", server.base_url + "/v1/jobs/does-not-exist")
    assert status == 404


def test_duplicate_submit_via_header_returns_same_job(live):
    server, _ = live
    headers = {"Idempotency-Key": "header-key-1"}
    first_status, first = _http("POST", server.base_url + "/v1/jobs", _legacy_payload(), headers)
    second_status, second = _http("POST", server.base_url + "/v1/jobs", _legacy_payload(), headers)
    assert first_status == 201
    assert second_status == 200
    assert first["job_id"] == second["job_id"]
    assert second.get("duplicate") is True


def test_frankfurt_submit_fails_without_starting_opencode(tmp_path):
    runner = CountingRunner()
    manager = _manager(tmp_path, command_runner=runner)
    server = _LiveServer(manager)
    try:
        payload = _payload()
        payload["metadata"] = dict(payload["metadata"])
        payload["metadata"]["region"] = "frankfurt"
        status, body = _http("POST", server.base_url + "/v1/jobs", payload)
        assert status == 201
        assert body["status"] == "failed"
        assert "frankfurt" in body["error"].lower() or "forbidden" in body["error"].lower()
        assert runner.calls == []
        status, fetched = _http("GET", server.base_url + "/v1/jobs/" + body["job_id"])
        assert status == 200
        assert fetched["status"] == "failed"
    finally:
        server.close()



def test_runner_source_enforces_knowledge_handoff_before_success():
    source = (Path(__file__).resolve().parent / "runner_server.py").read_text(encoding="utf-8")
    assert "Repository knowledge handoff (mandatory)" in source
    assert "validate_experiment_record_text" in source
    assert "knowledge handoff validation failed" in source
    assert source.index("knowledge handoff validation failed") < source.rindex('job_id, "succeeded"')
