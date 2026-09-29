"""Tests for the proven capacity-mismatch hold decision.

Repair issue #176 (failed run 36637940250, source issue #58, smoke):
that run executed at base ``bc76158``, which already contains the
complete validated config-only profile from the #75/#159/#164/#170
repairs, and still storm-aborted with the identical signature as the
four prior storms (4 consecutive proven worker restarts, cgroup
pinned at the 512 MB limit, PRESSURE via replacements). No further
config-only switch exists to wire, so redispatching the identical
ordinary-smoke payload storms identically while finalize mints
another P0 repair. ``automation/render_lifecycle.py`` now holds such
proven issues at dispatch time
(``CAPACITY_MISMATCH_HOLDS``/``capacity_dispatch_hold``), the live
executor names the hold with zero Render cost, and
``automation/capacity_hold_decision.py`` is the envelope-consumable
answer. These tests lock that contract without touching the network
or any file under .github/workflows/**.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = REPO_ROOT / "automation"
sys.path.insert(0, str(AUTOMATION))

from capacity_hold_decision import (  # noqa: E402
    EXIT_HELD,
    EXIT_NOT_HELD,
    EXIT_USAGE_ERROR,
    decide_from_dispatch,
    decide_from_result,
    main,
)
from render_controller import (  # noqa: E402
    EligibilitySnapshot,
    decide_eligible,
)
from render_lifecycle import (  # noqa: E402
    CAPACITY_MISMATCH_HOLDS,
    HELD_CAPACITY_VERDICT,
    PREFERRED_MODEL,
    build_capacity_hold_result,
    capacity_dispatch_hold,
    capacity_hold_verdict,
)

ORDINARY_TITLE = "P0: Measure a real OpenCode coding run on Render Free"
ORDINARY_BODY = (
    "Measure the real memory footprint of a genuine OpenCode "
    "coding-agent request on an actual Render Free worker."
)

RETIRED_BODY = (
    "Do **not rebuild OpenCode** for this task. Consume exactly:\n"
    "- Artifact ID: `11001896223`\n"
    "- Workflow run: `36492639568`\n"
    "- Artifact archive digest: "
    "`sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df`\n"
    "Never silently fall back to another OpenCode binary.\n"
)


def _snapshot(**overrides):
    base = {
        "issue_number": 7,
        "state": "open",
        "labels": frozenset({"priority:p1"}),
    }
    base.update(overrides)
    return EligibilitySnapshot(**base)


# ---------------------------------------------------------------------------
# Registry (single source of truth).
# ---------------------------------------------------------------------------


def test_registry_holds_issue_58_with_successor_premise():
    entry = CAPACITY_MISMATCH_HOLDS.get(58)
    assert isinstance(entry, dict)
    assert entry["mode"] == "smoke"
    assert entry["contract"] == "ordinary"
    assert entry["proving_run_id"] == "36637940250"
    assert entry["proving_base_sha"] == (
        "bc761583a13c875dbeb2b97196985ee9a73e0289"
    )
    successor = entry["successor"]
    assert successor["owner"] == "qualification-chain"
    assert "condition" in successor and successor["condition"].strip()
    assert HELD_CAPACITY_VERDICT == "held-capacity-mismatch"


# ---------------------------------------------------------------------------
# Guard (dispatch shape).
# ---------------------------------------------------------------------------


def test_guard_holds_ordinary_body_for_issue_58():
    hold = capacity_dispatch_hold(58, ORDINARY_TITLE, ORDINARY_BODY)
    assert isinstance(hold, dict)
    assert hold["issue_number"] == 58
    assert hold["successor"]["owner"] == "qualification-chain"
    assert "held-capacity-mismatch" in hold["reason"]
    assert "36637940250" in hold["reason"]


def test_guard_accepts_string_issue_number():
    hold = capacity_dispatch_hold("58", ORDINARY_TITLE, ORDINARY_BODY)
    assert isinstance(hold, dict)
    assert hold["issue_number"] == 58


def test_guard_ignores_unlisted_issues():
    for issue in (4, 7, 106, 0, -1, "", None):
        assert capacity_dispatch_hold(issue, ORDINARY_TITLE, ORDINARY_BODY) is None


def test_guard_ignores_exact_artifact_body_for_issue_58():
    # Exact-artifact bodies ride the exact gate and the superseded
    # path, never the capacity hold -- even on a held issue number.
    assert capacity_dispatch_hold(58, "retired", RETIRED_BODY) is None


def test_guard_never_raises_on_garbage():
    assert capacity_dispatch_hold(None, None, None) is None
    assert capacity_dispatch_hold(object(), object(), object()) is None
    assert capacity_dispatch_hold(["58"], {"t": 1}, ["b"]) is None


# ---------------------------------------------------------------------------
# Verdict + held record.
# ---------------------------------------------------------------------------


def test_verdict_held_shape_for_issue_58():
    verdict = capacity_hold_verdict(58)
    assert verdict == {
        "held": True,
        "verdict": "held-capacity-mismatch",
        "issue_number": 58,
        "successor": dict(CAPACITY_MISMATCH_HOLDS[58]["successor"]),
    }


def test_verdict_not_held_shape_otherwise():
    for issue in (4, 106, 0, "abc", None):
        verdict = capacity_hold_verdict(issue)
        assert verdict["held"] is False
        assert verdict["verdict"] == ""
        assert verdict["successor"] == {}


def test_build_capacity_hold_result_carries_verdict():
    record = build_capacity_hold_result(issue_number=58, run_id="36639274715")
    assert record["status"] == "infrastructure-blocked"
    assert record["permanent"] is True
    assert record["capacity_mismatch"] is True
    assert record["reason"].startswith("held-capacity-mismatch:")
    hold = record["capacity_hold"]
    assert hold["held"] is True
    assert hold["verdict"] == "held-capacity-mismatch"
    assert hold["issue_number"] == 58
    assert hold["successor"]["owner"] == "qualification-chain"
    assert record["successor"] == hold["successor"]
    assert record["issue_number"] == 58
    assert record["run_id"] == "36639274715"
    assert record["docs"]["render_free"].startswith("https://")


def test_build_capacity_hold_result_never_raises_on_garbage():
    record = build_capacity_hold_result(issue_number=None, run_id=None)
    assert record["status"] == "infrastructure-blocked"
    assert record["capacity_mismatch"] is False
    assert record["capacity_hold"]["held"] is False


# ---------------------------------------------------------------------------
# CLI decisions.
# ---------------------------------------------------------------------------


def test_cli_dispatch_held_exit_0(capsys):
    code = main(
        ["--issue", "58", "--title", ORDINARY_TITLE, "--body", ORDINARY_BODY]
    )
    assert code == EXIT_HELD
    decision = json.loads(capsys.readouterr().out)
    assert decision["held"] is True
    assert decision["verdict"] == "held-capacity-mismatch"
    assert decision["issue_number"] == 58
    assert decision["successor"]["owner"] == "qualification-chain"


def test_cli_dispatch_not_held_exit_1(capsys):
    code = main(
        ["--issue", "4", "--title", ORDINARY_TITLE, "--body", ORDINARY_BODY]
    )
    assert code == EXIT_NOT_HELD
    decision = json.loads(capsys.readouterr().out)
    assert decision["held"] is False
    assert decision["verdict"] == ""


def test_cli_body_file_held(capsys, tmp_path):
    body_file = tmp_path / "body.txt"
    body_file.write_text(ORDINARY_BODY, encoding="utf-8")
    code = main(["--issue", "58", "--body-file", str(body_file)])
    assert code == EXIT_HELD
    assert json.loads(capsys.readouterr().out)["held"] is True


def test_cli_result_file_held_exit_0(capsys, tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(build_capacity_hold_result(issue_number=58, run_id="1")),
        encoding="utf-8",
    )
    code = main(["--result-file", str(result_file)])
    assert code == EXIT_HELD
    decision = json.loads(capsys.readouterr().out)
    assert decision["issue_number"] == 58
    assert decision["successor"]["owner"] == "qualification-chain"


def test_cli_tampered_result_is_not_held(capsys, tmp_path):
    # A hand-crafted held claim for an unlisted issue must never
    # redirect triage: only the registry decides what is held.
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(
            {
                "status": "infrastructure-blocked",
                "issue_number": 4,
                "capacity_hold": {
                    "held": True,
                    "verdict": "held-capacity-mismatch",
                    "issue_number": 4,
                    "successor": {"owner": "attacker"},
                },
            }
        ),
        encoding="utf-8",
    )
    code = main(["--result-file", str(forged)])
    assert code == EXIT_NOT_HELD
    assert json.loads(capsys.readouterr().out)["held"] is False


def test_cli_wrong_successor_is_corrected_to_registry(capsys, tmp_path):
    tampered = tmp_path / "tampered.json"
    tampered.write_text(
        json.dumps(
            {
                "status": "infrastructure-blocked",
                "issue_number": 58,
                "capacity_hold": {
                    "held": True,
                    "verdict": "held-capacity-mismatch",
                    "issue_number": 58,
                    "successor": {"owner": "attacker"},
                },
            }
        ),
        encoding="utf-8",
    )
    code = main(["--result-file", str(tampered)])
    assert code == EXIT_HELD
    decision = json.loads(capsys.readouterr().out)
    assert decision["successor"] == dict(
        CAPACITY_MISMATCH_HOLDS[58]["successor"]
    )


def test_cli_garbage_record_shape_is_not_held(capsys, tmp_path):
    shapeless = tmp_path / "shapeless.json"
    shapeless.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    code = main(["--result-file", str(shapeless)])
    assert code == EXIT_NOT_HELD
    assert json.loads(capsys.readouterr().out)["held"] is False


def test_cli_usage_errors(capsys):
    assert main([]) == EXIT_USAGE_ERROR
    assert main(["--result-file", "/nonexistent.json"]) == EXIT_USAGE_ERROR
    assert (
        main(["--result-file", "/nonexistent.json", "--body", "x"])
        == EXIT_USAGE_ERROR
    )
    assert main(["--title", "only-a-title"]) == EXIT_USAGE_ERROR
    assert (
        main(["--issue", "not-a-number", "--body", ORDINARY_BODY])
        == EXIT_USAGE_ERROR
    )
    capsys.readouterr()


def test_cli_decisions_are_deterministic(capsys):
    first = main(
        ["--issue", "58", "--title", ORDINARY_TITLE, "--body", ORDINARY_BODY]
    )
    out_first = capsys.readouterr().out
    second = main(
        ["--issue", "58", "--title", ORDINARY_TITLE, "--body", ORDINARY_BODY]
    )
    out_second = capsys.readouterr().out
    assert (first, out_first) == (second, out_second) == (EXIT_HELD, out_first)
    assert json.loads(out_first)["held"] is True


# ---------------------------------------------------------------------------
# Pure dispatch helpers never raise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 0, "", [], {}, object()])
def test_dispatch_helpers_fail_open_on_garbage(bad):
    assert decide_from_dispatch(bad, bad, bad)["held"] is False
    assert decide_from_result(bad)["held"] is False


def test_decide_from_result_derives_pre_hold_records():
    # Records written before the structured verdict existed classify
    # identically from their own issue_number field.
    decision = decide_from_result({"issue_number": 58})
    assert decision["held"] is True
    assert decision["verdict"] == "held-capacity-mismatch"
    assert decide_from_result({"issue_number": 4})["held"] is False


# ---------------------------------------------------------------------------
# Scheduler mirror (owned controller path).
# ---------------------------------------------------------------------------


def test_decide_eligible_holds_issue_58_with_successor():
    held = _snapshot(
        issue_number=58,
        labels=frozenset({"priority:p0", "execution:render-smoke"}),
        title=ORDINARY_TITLE,
        body=ORDINARY_BODY,
    )
    decision = decide_eligible(held)
    assert decision.eligible is False
    assert "held-capacity-mismatch" in decision.reason
    assert "qualification-chain" in decision.reason
    # Ordinary work still dispatches: the hold must not swallow
    # legitimate issues.
    assert decide_eligible(_snapshot(body=ORDINARY_BODY)).eligible is True
    # Paused still short-circuits first.
    paused = _snapshot(
        issue_number=58,
        labels=frozenset({"priority:p0", "automation:paused"}),
        body=ORDINARY_BODY,
    )
    assert "paused" in decide_eligible(paused).reason


def test_decide_eligible_prefers_superseded_for_exact_bodies():
    # The retired-contract guard owns exact-artifact bodies even on a
    # capacity-held issue number: the two holds are mutually exclusive.
    retired = _snapshot(
        issue_number=58,
        labels=frozenset({"priority:p0", "execution:render-smoke"}),
        title="retired",
        body=RETIRED_BODY,
    )
    decision = decide_eligible(retired)
    assert decision.eligible is False
    assert "11001896223" in decision.reason


# ---------------------------------------------------------------------------
# Executor harness (live shell path, zero Render cost).
# ---------------------------------------------------------------------------


def _write_capacity_bin(directory, curl_log):
    bin_dir = Path(directory) / "cap-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'echo "curl called: $*" >> "$FAKE_CURL_LOG"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "issue" && "$2" == "view" ]]; then\n'
        '  jq -Rn --arg t "${FAKE_GH_TITLE:-Harness title}" '
        '--arg b "${FAKE_GH_BODY:-Harness body}" '
        "'{title:$t,body:$b}'\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return str(bin_dir)


def _capacity_env(tmp_path):
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    env = dict(os.environ)
    env["ISSUE_NUMBER"] = "58"
    env["EXECUTION_MODE"] = "smoke"
    env["RENDER_API_KEY"] = "dummy-key"
    env["RENDER_REGION"] = "oregon"
    env["OPENCODE_MODEL"] = PREFERRED_MODEL
    env["RENDER_STATE_FILE"] = str(state)
    env["RENDER_RESULT_FILE"] = str(result)
    env["GITHUB_SHA"] = "abc123def456"
    env["GITHUB_RUN_ID"] = "999"
    env["GH_TOKEN"] = "dummy"
    return env, state, result


def test_job_names_capacity_hold_before_service_creation():
    # Static placement for repair issue #176 (run 36637940250): the
    # scheduler envelope and the repair-reset unpause never consult
    # decide_eligible (see repair issue #161), so the executor -- the
    # one production chokepoint this repository owns -- must name the
    # capacity hold BEFORE any Render service can be created, while
    # the retired-contract hold and the exact-artifact gate keep
    # their own positions for exact bodies.
    job = (AUTOMATION / "render-job.sh").read_text(encoding="utf-8")
    assert "capacity_dispatch_hold" in job
    assert "held-capacity-mismatch" in job
    assert "CAPACITY_HOLD_JSON" in job
    assert job.index("capacity_dispatch_hold") < job.index(
        "One service creation per attempt"
    )
    # The hold is read-only: no GitHub writes are added.
    assert "gh issue edit" not in job
    assert "gh issue comment" not in job
    assert "gh issue close" not in job


def test_job_holds_capacity_mismatch_with_zero_render_cost(tmp_path):
    # Live regression for run 36637940250 (repair issue #176): the
    # ordinary-smoke #58 body must hold pre-creation with the stable
    # verdict, a structured held record, and zero Render calls.
    env, state, result = _capacity_env(tmp_path)
    log = tmp_path / "curl-cap.log"
    env["FAKE_CURL_LOG"] = str(log)
    env["PATH"] = _write_capacity_bin(tmp_path, log) + os.pathsep + env.get(
        "PATH", ""
    )
    proc = subprocess.run(
        ["bash", str(AUTOMATION / "render-job.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "held-capacity-mismatch" in combined
    assert "qualification-chain" in combined
    assert not log.exists() or "api.render.com" not in log.read_text()
    assert not state.exists() or "srv-" not in state.read_text()
    payload = json.loads(result.read_text())
    assert payload["status"] == "infrastructure-blocked"
    assert payload["permanent"] is True
    assert payload["capacity_mismatch"] is True
    hold = payload["capacity_hold"]
    assert hold["held"] is True
    assert hold["verdict"] == "held-capacity-mismatch"
    assert hold["issue_number"] == 58
    assert hold["successor"]["owner"] == "qualification-chain"
    assert payload["successor"] == hold["successor"]
    assert payload["issue_number"] == 58


def test_job_prefers_superseded_for_exact_body_on_held_issue(tmp_path):
    # Mutual exclusivity live: a retired exact-artifact body on the
    # held issue number trips the superseded hold, never the capacity
    # hold, and still refuses with zero Render cost.
    env, state, result = _capacity_env(tmp_path)
    log = tmp_path / "curl-cap-exact.log"
    env["FAKE_CURL_LOG"] = str(log)
    env["FAKE_GH_BODY"] = RETIRED_BODY
    env["PATH"] = _write_capacity_bin(tmp_path, log) + os.pathsep + env.get(
        "PATH", ""
    )
    proc = subprocess.run(
        ["bash", str(AUTOMATION / "render-job.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "held-superseded" in combined
    assert "held-capacity-mismatch" not in combined
    assert not log.exists() or "api.render.com" not in log.read_text()
    payload = json.loads(result.read_text())
    assert payload["superseded"] is True
