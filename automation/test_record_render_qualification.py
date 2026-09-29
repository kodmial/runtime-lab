"""Tests for the Render qualification classifier (issue #138).

Locks the retirement-signal propagation through
``record_render_qualification.classify`` without touching the network:

- a retired exact-artifact refusal (the live #110 contract) keeps its
  infrastructure/identity classification while carrying permanent /
  superseded / successor machine-readably in the durable payload;
- ordinary and garbage results stay fail-closed with no retirement
  signal;
- the fingerprint inputs stay byte-identical to the pre-#138 shape so
  the already-posted live marker keeps deduping identical redispatches.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from record_render_qualification import classify, refusal_retirement  # noqa: E402
from render_lifecycle import (  # noqa: E402
    build_exact_artifact_refusal_result,
    parse_exact_workflow_artifact_requirement,
)

LIVE_ISSUE110_BODY = """- Source workflow run: `36492639568` (`OpenCode Coding Artifact`)
- Artifact ID: `11001896223`
- Artifact archive digest: `sha256:8d5c5c3e98844c4800621031d0bbcb7c15f1039ec82ef1831928b8caeaa932df`
"""

# Fingerprint posted live on #110 by run 36505913972 for the retired
# contract (infrastructure/identity, unknown binary, empty telemetry).
LIVE_ISSUE110_FINGERPRINT = (
    "17aee62ac5b68966042acecc14b0f68000374241b7943d1041afad37d3d0c32f"
)


def retired_issue110_refusal():
    requirement = parse_exact_workflow_artifact_requirement(
        "P0 qualification", LIVE_ISSUE110_BODY
    )
    assert requirement is not None
    return build_exact_artifact_refusal_result(
        requirement, issue_number=110, run_id="36505913972"
    )


def classify_retired_issue110():
    return classify(
        retired_issue110_refusal(),
        {},
        artifact_id=11001896223,
        expected_sha="unknown",
        execute_outcome="failure",
        cleanup_outcome="success",
    )


def test_retired_refusal_keeps_classification_and_carries_retirement():
    payload = classify_retired_issue110()
    assert payload["classification"] == "infrastructure"
    assert payload["subtype"] == "identity"
    assert payload["permanent"] is True
    assert payload["superseded"] is True
    assert payload["successor"]["successor_artifact_id"] == "11004835952"
    assert payload["successor"]["successor_source_run_id"] == "36498663107"
    assert payload["successor"]["successor_version"] == "1.18.33"


def test_retired_refusal_fingerprint_matches_live_posted_marker():
    payload = classify_retired_issue110()
    assert payload["fingerprint"] == LIVE_ISSUE110_FINGERPRINT


def test_retirement_fields_do_not_change_fingerprint_inputs():
    refusal = retired_issue110_refusal()
    stripped = {
        key: value
        for key, value in refusal.items()
        if key not in ("permanent", "superseded", "successor")
    }
    kwargs = {
        "artifact_id": 11001896223,
        "expected_sha": "unknown",
        "execute_outcome": "failure",
        "cleanup_outcome": "success",
    }
    assert classify(refusal, {}, **kwargs)["fingerprint"] == classify(
        stripped, {}, **kwargs
    )["fingerprint"]


def test_ordinary_empty_result_carries_no_retirement_signal():
    payload = classify(
        {},
        {},
        artifact_id=0,
        expected_sha="unknown",
        execute_outcome="failure",
        cleanup_outcome="success",
    )
    assert payload["permanent"] is False
    assert payload["superseded"] is False
    assert payload["successor"] == {}


def test_non_superseded_refusal_carries_no_successor():
    requirement = parse_exact_workflow_artifact_requirement(
        "ordinary smoke", "no artifact contract here"
    )
    assert requirement is None
    record = build_exact_artifact_refusal_result(
        {"artifact_id": "99999999", "source_run_id": "123"},
        issue_number=110,
        run_id="x",
    )
    assert record["superseded"] is False
    payload = classify(
        record,
        {},
        artifact_id=99999999,
        expected_sha="unknown",
        execute_outcome="failure",
        cleanup_outcome="success",
    )
    assert payload["superseded"] is False
    assert payload["successor"] == {}


def test_refusal_retirement_is_garbage_safe():
    assert refusal_retirement(None) == (False, False, {})
    assert refusal_retirement("garbage") == (False, False, {})
    assert refusal_retirement([]) == (False, False, {})
    assert refusal_retirement({}) == (False, False, {})
    permanent, superseded, successor = refusal_retirement(
        {"permanent": True, "superseded": True, "successor": "garbage"}
    )
    assert (permanent, superseded, successor) == (True, True, {})
