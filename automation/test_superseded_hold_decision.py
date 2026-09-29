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
    EXIT_HELD,
    EXIT_NOT_HELD,
    EXIT_USAGE_ERROR,
    decide_from_body,
    decide_from_result,
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
