"""Tests for the envelope-consumable retired-contract hold decision.

Repair issue #171 (failed run 36635914814): that run proved every prior
hold signal fires live yet still loops -- the #161 log verdict plus the
#165 structured ``dispatch_hold`` key were both present (execute=failure,
cleanup=success, zero Render calls) and finalize still minted #171,
because no provisioned envelope step can answer "is this held?" without
embedding registry logic. ``automation/superseded_hold_decision.py`` is
that answer; these tests lock its contract without touching the network
or any file under .github/workflows/**.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = REPO_ROOT / "automation"
sys.path.insert(0, str(AUTOMATION))

from superseded_hold_decision import (  # noqa: E402
    EXIT_DUPLICATE,
    EXIT_EXHAUSTED,
    EXIT_HELD,
    EXIT_NOT_HELD,
    EXIT_USAGE_ERROR,
    decide_from_body,
    decide_from_result,
    finalize_exit_code,
    finalize_repair_decision,
    format_hold_notice,
)

RETIRED_BODY = (
    "Run the exact artifact on Render.\n"
    "- Artifact ID: 11001896223\n"
    "- Workflow run: 36492639568\n"
    "- Artifact archive digest: "
    "sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df\n"
)

RETIRED_BODY_110_PHRASING = (
    "Source workflow run: 36492639568.\n"
    "Artifact ID: 11001896223.\n"
    "Artifact archive digest: "
    "sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df.\n"
    "Artifact name: opencode-coding-linux-x64.\n"
)

SUCCESSOR_BODY = (
    "Run the exact artifact on Render.\n"
    "- Artifact ID: 11004835952\n"
    "- Workflow run: 36498663107\n"
    "- Artifact archive digest: "
    "sha256:0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409e24b040\n"
)

ORDINARY_BODY = "Run the normal smoke workload with the pinned baseline."


def _live_held_result():
    # Minimal shape of the durable refusal record uploaded by the failed
    # run 36635914814 (render-qualification-106-36635914814): the #165
    # structured hold fires live with held=true.
    return {
        "status": "infrastructure-blocked",
        "permanent": True,
        "artifact_id": "11001896223",
        "source_run_id": "36492639568",
        "dispatch_hold": {
            "held": True,
            "verdict": "held-superseded",
            "artifact_id": "11001896223",
            "source_run_id": "36492639568",
            "successor": {
                "successor_artifact_id": "11004835952",
                "successor_source_run_id": "36498663107",
            },
        },
    }


# ---------------------------------------------------------------------------
# Body decisions (scheduler pre-dispatch shape).
# ---------------------------------------------------------------------------


def test_retired_106_body_holds_with_registry_successor():
    decision = decide_from_body("smoke", RETIRED_BODY)
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    assert decision["artifact_id"] == "11001896223"
    assert decision["source_run_id"] == "36492639568"
    assert decision["successor"]["successor_artifact_id"] == "11004835952"
    assert decision["successor"]["successor_source_run_id"] == "36498663107"
    assert decision["successor"]["successor_version"] == "1.18.33"


def test_retired_110_phrasing_holds_identically():
    decision = decide_from_body("smoke", RETIRED_BODY_110_PHRASING)
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    assert decision["successor"]["successor_artifact_id"] == "11004835952"


def test_successor_body_does_not_hold():
    decision = decide_from_body("smoke", SUCCESSOR_BODY)
    assert decision["held"] is False
    assert decision["verdict"] == ""
    assert decision["successor"] == {}


def test_ordinary_body_does_not_hold():
    decision = decide_from_body("smoke", ORDINARY_BODY)
    assert decision["held"] is False
    assert decision["verdict"] == ""
    assert decision["successor"] == {}


def test_garbage_body_never_raises_and_never_holds():
    for title, body in (
        ("", ""),
        (None, None),
        (123, ["not", "text"]),
        ("smoke", "Artifact ID: abc Workflow run: xyz"),
    ):
        decision = decide_from_body(title, body)
        assert decision["held"] is False
        assert decision["verdict"] == ""
        assert decision["successor"] == {}


# ---------------------------------------------------------------------------
# Result decisions (finalize refusal-record shape).
# ---------------------------------------------------------------------------


def test_live_36635914814_refusal_record_holds():
    decision = decide_from_result(_live_held_result())
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    assert decision["artifact_id"] == "11001896223"
    assert decision["source_run_id"] == "36492639568"
    # The registry stays the source of truth: the partial successor in
    # the record is completed from the registry entry.
    assert decision["successor"]["successor_artifact_id"] == "11004835952"
    assert decision["successor"]["successor_version"] == "1.18.33"


def test_pre_hold_record_without_dispatch_hold_derives_identically():
    record = {
        "status": "infrastructure-blocked",
        "artifact_id": "11001896223",
        "source_run_id": "36492639568",
    }
    decision = decide_from_result(record)
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    assert decision["successor"]["successor_artifact_id"] == "11004835952"


def test_tampered_hold_claim_for_unknown_contract_does_not_hold():
    record = {
        "status": "infrastructure-blocked",
        "artifact_id": "99999999999",
        "source_run_id": "36492639568",
        "dispatch_hold": {
            "held": True,
            "verdict": "held-superseded",
            "artifact_id": "99999999999",
            "source_run_id": "36492639568",
            "successor": {"successor_artifact_id": "1"},
        },
    }
    decision = decide_from_result(record)
    assert decision["held"] is False
    assert decision["verdict"] == ""


def test_ordinary_result_does_not_hold():
    decision = decide_from_result({"status": "succeeded", "job_id": "job-1"})
    assert decision["held"] is False
    assert decision["successor"] == {}


def test_garbage_result_never_raises_and_never_holds():
    for value in (None, [], "held", 42, {"dispatch_hold": "held"}):
        decision = decide_from_result(value)
        assert decision["held"] is False
        assert decision["verdict"] == ""


# ---------------------------------------------------------------------------
# CLI contract (what provisioned envelopes key on).
# ---------------------------------------------------------------------------


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(AUTOMATION / "superseded_hold_decision.py"), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_body_held_exit_code_and_json(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(RETIRED_BODY, encoding="utf-8")
    proc = _cli("--body-file", str(body_file))
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    decision = json.loads(proc.stdout)
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    assert decision["successor"]["successor_artifact_id"] == "11004835952"


def test_cli_inline_body_not_held_exit_code(tmp_path):
    proc = _cli("--title", "smoke", "--body", ORDINARY_BODY)
    assert proc.returncode == EXIT_NOT_HELD == 1, proc.stderr
    decision = json.loads(proc.stdout)
    assert decision["held"] is False
    assert decision["successor"] == {}


def test_cli_result_file_reads_live_36635914814_shape(tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(_live_held_result()), encoding="utf-8")
    proc = _cli("--result-file", str(result_file))
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    decision = json.loads(proc.stdout)
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"


def test_cli_result_file_missing_is_usage_error(tmp_path):
    proc = _cli("--result-file", str(tmp_path / "absent.json"))
    assert proc.returncode == EXIT_USAGE_ERROR == 2
    assert proc.stdout == ""


def test_cli_no_input_is_usage_error():
    proc = _cli()
    assert proc.returncode == EXIT_USAGE_ERROR == 2


def test_cli_mixed_inputs_are_usage_error(tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(_live_held_result()), encoding="utf-8")
    proc = _cli("--result-file", str(result_file), "--body", RETIRED_BODY)
    assert proc.returncode == EXIT_USAGE_ERROR == 2


def test_cli_is_deterministic():
    first = _cli("--title", "smoke", "--body", RETIRED_BODY)
    second = _cli("--title", "smoke", "--body", RETIRED_BODY)
    assert first.returncode == second.returncode == EXIT_HELD
    assert first.stdout == second.stdout


# ---------------------------------------------------------------------------
# Finalize gate (repair issue #177, failed run 36638450066).
#
# Run 36638450066 executed at a base already containing this module, and
# its uploaded refusal record (render-qualification-106-36638450066)
# re-validates to held/exit 0 live -- yet finalize still minted #177.
# The missing piece is the mint-vs-hold branch plus the successor notice
# text. These tests lock that finalize contract without touching the
# network or any file under .github/workflows/**.
# ---------------------------------------------------------------------------


def _live_36638450066_held_decision():
    # Exact durable bytes uploaded by run 36638450066
    # (render-qualification-106-36638450066, artifact 11065499378):
    # status=infrastructure-blocked, permanent, superseded, full
    # successor mapping, dispatch_hold.held=true.
    record = {
        "status": "infrastructure-blocked",
        "permanent": True,
        "artifact_id": "11001896223",
        "source_run_id": "36492639568",
        "dispatch_hold": {
            "held": True,
            "verdict": "held-superseded",
            "artifact_id": "11001896223",
            "source_run_id": "36492639568",
            "successor": {
                "successor_artifact_id": "11004835952",
                "successor_source_run_id": "36498663107",
            },
        },
    }
    return decide_from_result(record)


def test_live_36638450066_record_holds_and_formats_notice():
    decision = _live_36638450066_held_decision()
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    notice = format_hold_notice(
        decision, source_issue=106, failed_run_id="36638450066"
    )
    assert "11001896223" in notice
    assert "36492639568" in notice
    assert "11004835952" in notice
    assert "36498663107" in notice
    assert "36638450066" in notice
    assert "No new repair was minted" in notice


def test_hold_suppresses_mint_below_budget_without_existing_repair():
    decision = _live_36638450066_held_decision()
    outcome = finalize_repair_decision(
        decision, repair_count=8, max_attempts=10, existing_repair=""
    )
    assert outcome["action"] == "hold"
    assert "11004835952" in outcome["notice"]


def test_not_held_mints_below_budget_without_existing_repair():
    decision = decide_from_body("smoke", ORDINARY_BODY)
    assert decision["held"] is False
    outcome = finalize_repair_decision(
        decision, repair_count=8, max_attempts=10, existing_repair=""
    )
    assert outcome["action"] == "mint"


def test_not_held_duplicate_when_repair_already_open():
    decision = decide_from_body("smoke", ORDINARY_BODY)
    outcome = finalize_repair_decision(
        decision, repair_count=3, max_attempts=10, existing_repair="177"
    )
    assert outcome["action"] == "duplicate"


def test_not_held_exhausted_at_budget():
    decision = decide_from_body("smoke", ORDINARY_BODY)
    outcome = finalize_repair_decision(
        decision, repair_count=10, max_attempts=10, existing_repair=""
    )
    assert outcome["action"] == "exhausted"


def test_hold_wins_over_duplicate_and_exhausted():
    decision = _live_36638450066_held_decision()
    assert (
        finalize_repair_decision(
            decision, repair_count=3, max_attempts=10, existing_repair="177"
        )["action"]
        == "hold"
    )
    assert (
        finalize_repair_decision(
            decision, repair_count=10, max_attempts=10, existing_repair=""
        )["action"]
        == "hold"
    )


def test_finalize_gate_garbage_fails_open_toward_mint():
    for decision in (None, [], "held", 42, {"held": True}):
        outcome = finalize_repair_decision(
            decision, repair_count=0, max_attempts=10, existing_repair=""
        )
        assert outcome["action"] == "mint"
    assert format_hold_notice(None) == ""
    assert format_hold_notice({"held": True}) == ""


# ---------------------------------------------------------------------------
# Executable finalize branch (repair issue #182, failed run 36640697494).
#
# Run 36640697494 executed at base 35fb646, which already contains the
# #177 finalize_repair_decision library -- and its uploaded refusal record
# (render-qualification-106-36640697494, dispatch_hold.held=true)
# re-validates to action="hold" offline -- yet finalize still minted #182.
# The library has no CLI surface, so the provisioned envelope cannot call
# it without hand-rolling count plumbing in bash. These tests lock the
# --finalize executable branch without touching the network or any file
# under .github/workflows/**.
# ---------------------------------------------------------------------------


def _live_36640697494_refusal_record():
    # Exact durable bytes uploaded by run 36640697494
    # (render-qualification-106-36640697494): status=infrastructure-blocked,
    # permanent, superseded, full successor mapping, dispatch_hold.held=true.
    return {
        "status": "infrastructure-blocked",
        "permanent": True,
        "superseded": True,
        "archive_sha256": (
            "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
        ),
        "artifact_id": "11001896223",
        "artifact_name": "opencode-coding-linux-x64",
        "source_run_id": "36492639568",
        "issue_number": 106,
        "run_id": "36640697494",
        "dispatch_hold": {
            "held": True,
            "verdict": "held-superseded",
            "artifact_id": "11001896223",
            "source_run_id": "36492639568",
            "successor": {
                "successor_artifact_id": "11004835952",
                "successor_source_run_id": "36498663107",
                "successor_archive_sha256": (
                    "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409"
                    "e24b040"
                ),
                "successor_binary_sha256": (
                    "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f"
                    "5485966"
                ),
                "successor_version": "1.18.33",
                "owner": (
                    "automation/issue130_coordinator.py "
                    "(objective-130-state.json, active candidate 11004835952)"
                ),
            },
        },
    }


def _finalize_cli(result_file=None, *extra):
    args = []
    if result_file is not None:
        args += ["--result-file", str(result_file)]
    return _cli("--finalize", *args, *extra)


def test_finalize_exit_code_mapping():
    assert finalize_exit_code("hold") == EXIT_HELD == 0
    assert finalize_exit_code("mint") == EXIT_NOT_HELD == 1
    assert finalize_exit_code("duplicate") == EXIT_DUPLICATE == 3
    assert finalize_exit_code("exhausted") == EXIT_EXHAUSTED == 4
    for garbage in (None, "", "MINT", 42, ["hold"]):
        assert finalize_exit_code(garbage) == EXIT_NOT_HELD == 1


def test_live_36640697494_record_finalizes_to_hold(tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(_live_36640697494_refusal_record()), encoding="utf-8"
    )
    proc = _finalize_cli(
        result_file,
        "--repair-count", "9",
        "--max-attempts", "10",
        "--source-issue", "106",
        "--failed-run-id", "36640697494",
    )
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["action"] == "hold"
    assert "11001896223" in payload["notice"]
    assert "36492639568" in payload["notice"]
    assert "11004835952" in payload["notice"]
    assert "36640697494" in payload["notice"]
    assert "No new repair was minted" in payload["notice"]


def test_finalize_hold_wins_over_duplicate_and_exhausted_cli(tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(_live_36640697494_refusal_record()), encoding="utf-8"
    )
    proc = _finalize_cli(
        result_file,
        "--repair-count", "3",
        "--existing-repair", "177",
        "--source-issue", "106",
        "--failed-run-id", "36640697494",
    )
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    assert json.loads(proc.stdout)["action"] == "hold"
    proc = _finalize_cli(
        result_file,
        "--repair-count", "10",
        "--source-issue", "106",
        "--failed-run-id", "36640697494",
    )
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    assert json.loads(proc.stdout)["action"] == "hold"


def test_finalize_body_inputs_hold_without_result_file():
    proc = _cli(
        "--finalize", "--title", "smoke", "--body", RETIRED_BODY,
        "--source-issue", "106", "--failed-run-id", "36640697494",
    )
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["action"] == "hold"
    assert "11004835952" in payload["notice"]


def test_finalize_mint_duplicate_exhausted_cli():
    proc = _cli("--finalize", "--title", "smoke", "--body", ORDINARY_BODY)
    assert proc.returncode == EXIT_NOT_HELD == 1, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload == {"action": "mint", "notice": ""}

    proc = _cli(
        "--finalize", "--title", "smoke", "--body", ORDINARY_BODY,
        "--existing-repair", "182",
    )
    assert proc.returncode == EXIT_DUPLICATE == 3, proc.stderr
    assert json.loads(proc.stdout)["action"] == "duplicate"

    proc = _cli(
        "--finalize", "--title", "smoke", "--body", ORDINARY_BODY,
        "--repair-count", "10", "--max-attempts", "10",
    )
    assert proc.returncode == EXIT_EXHAUSTED == 4, proc.stderr
    assert json.loads(proc.stdout)["action"] == "exhausted"


def test_finalize_garbage_content_fails_open_to_mint(tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    proc = _finalize_cli(result_file)
    assert proc.returncode == EXIT_NOT_HELD == 1, proc.stderr
    assert json.loads(proc.stdout)["action"] == "mint"

    proc = _cli(
        "--finalize", "--title", "smoke", "--body", ORDINARY_BODY,
        "--repair-count", "not-a-number", "--max-attempts", "also-bad",
    )
    assert proc.returncode == EXIT_NOT_HELD == 1, proc.stderr
    assert json.loads(proc.stdout)["action"] == "mint"


def test_finalize_unreadable_inputs_are_usage_error(tmp_path):
    proc = _finalize_cli(tmp_path / "absent.json")
    assert proc.returncode == EXIT_USAGE_ERROR == 2
    assert proc.stdout == ""

    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not json", encoding="utf-8")
    proc = _finalize_cli(bad_file)
    assert proc.returncode == EXIT_USAGE_ERROR == 2
    assert proc.stdout == ""

    proc = _cli("--finalize")
    assert proc.returncode == EXIT_USAGE_ERROR == 2

    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(_live_36640697494_refusal_record()), encoding="utf-8"
    )
    proc = _finalize_cli(result_file, "--body", RETIRED_BODY)
    assert proc.returncode == EXIT_USAGE_ERROR == 2


# ---------------------------------------------------------------------------
# Envelope parity + live bytes (repair issue #185, failed run 36642218343).
#
# Run 36642218343 (source issue #106, smoke) executed at base f7abb7d,
# which already contains the complete #182 --finalize CLI -- and the
# uploaded refusal record re-validates to action=hold/exit 0 offline --
# yet finalize still minted #185, because the provisioned envelope never
# calls the helper. Comparing the helper against the provisioned
# continuum-render-executor.yml finalize text exposed one mirror infidelity: the
# envelope nests the open-repair check INSIDE the budget check (an open
# repair at or over budget reports exhausted, never duplicate), while
# the helper returned duplicate whenever a repair was open. These tests
# lock the exact nesting plus the full chain on the 36642218343 live
# bytes without touching the network or .github/workflows/**.
# ---------------------------------------------------------------------------


def test_not_held_exhausted_when_repair_open_at_budget():
    # Envelope parity: REPAIR_COUNT(10) < MAX(10) is false, so the
    # exhausted branch wins even with an open repair carrying the
    # source marker.
    decision = decide_from_body("smoke", ORDINARY_BODY)
    assert decision["held"] is False
    outcome = finalize_repair_decision(
        decision, repair_count=10, max_attempts=10, existing_repair="185"
    )
    assert outcome == {"action": "exhausted", "notice": ""}
    outcome = finalize_repair_decision(
        decision, repair_count=11, max_attempts=10, existing_repair="185"
    )
    assert outcome == {"action": "exhausted", "notice": ""}


def test_not_held_duplicate_only_while_budget_remains():
    decision = decide_from_body("smoke", ORDINARY_BODY)
    outcome = finalize_repair_decision(
        decision, repair_count=9, max_attempts=10, existing_repair="185"
    )
    assert outcome == {"action": "duplicate", "notice": ""}


def test_finalize_exhausted_with_open_repair_cli():
    proc = _cli(
        "--finalize", "--title", "smoke", "--body", ORDINARY_BODY,
        "--repair-count", "10", "--max-attempts", "10",
        "--existing-repair", "185",
    )
    assert proc.returncode == EXIT_EXHAUSTED == 4, proc.stderr
    assert json.loads(proc.stdout)["action"] == "exhausted"


def _live_36642218343_refusal_record():
    # Exact durable bytes uploaded by run 36642218343
    # (render-qualification-106-36642218343, artifact 11067161424):
    # status=infrastructure-blocked for retired artifact 11001896223 /
    # run 36492639568, permanent, superseded, full successor mapping,
    # dispatch_hold.held=true. Extra evidence keys (reason/docs/
    # has_known_advisory/source_sha) must not disturb the decision.
    return {
        "archive_sha256": (
            "8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df"
        ),
        "artifact_id": "11001896223",
        "artifact_name": "opencode-coding-linux-x64",
        "dispatch_hold": {
            "artifact_id": "11001896223",
            "held": True,
            "source_run_id": "36492639568",
            "successor": {
                "owner": (
                    "automation/issue130_coordinator.py "
                    "(objective-130-state.json, active candidate 11004835952)"
                ),
                "successor_archive_sha256": (
                    "0f0e3a6e787bcc80cbe7cff90ee0a948448bc7887633a2d5b8df19409"
                    "e24b040"
                ),
                "successor_artifact_id": "11004835952",
                "successor_binary_sha256": (
                    "f09d24273e95c3045e23ee37ecd98f8e31e9aa71ec30f75444df8441f"
                    "5485966"
                ),
                "successor_source_run_id": "36498663107",
                "successor_version": "1.18.33",
            },
            "verdict": "held-superseded",
        },
        "docs": {
            "github_artifacts": "https://docs.github.com/en/rest/actions/artifacts",
            "render_free": "https://render.com/docs/free",
        },
        "has_known_advisory": True,
        "issue_number": 106,
        "permanent": True,
        "reason": "infrastructure-blocked: this issue requires the exact GitHub Actions opencode-coding-linux-x64 11001896223 (workflow run 36492639568) checksum-verified before execution with no rebuild and no binary substitution.",
        "run_id": "36642218343",
        "source_run_id": "36492639568",
        "source_sha": "8ed6c749577d534c55ba9555ba4918ea8be95a97",
        "status": "infrastructure-blocked",
        "successor": {
            "successor_artifact_id": "11004835952",
            "successor_source_run_id": "36498663107",
            "successor_version": "1.18.33",
        },
        "superseded": True,
    }


def test_live_36642218343_record_holds():
    decision = decide_from_result(_live_36642218343_refusal_record())
    assert decision["held"] is True
    assert decision["verdict"] == "held-superseded"
    assert decision["artifact_id"] == "11001896223"
    assert decision["source_run_id"] == "36492639568"
    assert decision["successor"]["successor_artifact_id"] == "11004835952"
    assert decision["successor"]["successor_version"] == "1.18.33"


def test_live_36642218343_record_finalizes_to_hold(tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(_live_36642218343_refusal_record()), encoding="utf-8"
    )
    proc = _finalize_cli(
        result_file,
        "--repair-count", "9",
        "--max-attempts", "10",
        "--source-issue", "106",
        "--failed-run-id", "36642218343",
    )
    assert proc.returncode == EXIT_HELD == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["action"] == "hold"
    assert "11001896223" in payload["notice"]
    assert "36492639568" in payload["notice"]
    assert "11004835952" in payload["notice"]
    assert "36642218343" in payload["notice"]
    assert "No new repair was minted" in payload["notice"]
