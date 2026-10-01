#!/usr/bin/env python3
"""Envelope-consumable hold decision for retired exact-artifact contracts.

Repair issue #171 (failed run 36635914814, source issue #106, smoke):
that run executed at a base already containing every prior hold signal --
the #154 ``superseded_dispatch_guard`` library guard, the #161
``held-superseded`` executor log verdict, and the #165 structured
``dispatch_hold`` refusal-record key -- and all three fired live
(``held-superseded`` in the log plus ``dispatch_hold.held=true`` /
``verdict=held-superseded`` in the uploaded
``render-qualification-106-36635914814`` refusal record, zero Render
calls, cleanup success). The workflow finalize step still minted repair
#171 because its only redispatch dedup (the qualification fingerprint
branch) never fires for smoke issues and it never consults the refusal
record or the retired-contract registry.

The reusable defect fixed here is therefore consumer-side, not
signal-side: no provisioned envelope step can answer "is this refusal
held-superseded?" without embedding registry logic in workflow JS/YAML.
This module is that answer as a stdlib-only executable decision point.
The provisioned ``continuum-render-executor.yml`` finalize step runs on a runner
with a full checkout of ``main``, so it can adopt the hold with one call
and no registry duplication::

    if python3 automation/superseded_hold_decision.py --finalize \
        --result-file "$RENDER_RESULT_FILE" \
        --repair-count "$REPAIR_COUNT" \
        --max-attempts "$MAX_RENDER_REPAIR_ATTEMPTS" \
        --existing-repair "$EXISTING_REPAIR" \
        --source-issue "$ISSUE_NUMBER" \
        --failed-run-id "$GITHUB_RUN_ID" >/tmp/finalize.json; then
      # exit 0 = hold: keep the source paused, post the returned notice,
      # and skip minting another P0 repair for an identical refusal.
    fi
    # exit 1/3/4 = mint/duplicate/exhausted: read .action/.notice and
    # follow the matching existing branch. Exit 2 (usage error, e.g. a
    # missing result file) means "fall through to today's behavior".

The same module answers the scheduler pre-dispatch question from the
issue text (``--title``/``--body``/``--body-file``) wherever a checkout
exists; scheduler/auto-merge adoption itself is provisioned separately
(the Actions token cannot push workflow-file changes).

Contract (stable for provisioned envelopes):
- Without ``--finalize``, stdout is always one JSON object: ``held``
  (bool), ``verdict`` (``"held-superseded"`` or ``""``), ``artifact_id``,
  ``source_run_id``, ``successor`` (registry mapping or ``{}``),
  ``reason`` (human line); exit 0 means held-superseded and exit 1
  means not held.
- With ``--finalize``, stdout is always one JSON object: ``action``
  (``"hold"``/``"mint"``/``"duplicate"``/``"exhausted"``) plus
  ``notice`` (the deterministic successor text for hold, else ``""``);
  exits mirror the action (0 hold, 1 mint, 3 duplicate, 4 exhausted).
- exit 2 means CLI usage error (no input flags, unreadable file).
  Garbage *content* is never a usage error: unparsable bodies/records
  mean "not held" (fail open toward today's behavior), never a crash.
- No network, no GitHub calls, no Render calls, stdlib only. Never
  raises out of the public helpers: garbage in means "not held".
- The ``SUPERSEDED_WORKFLOW_ARTIFACTS`` registry in
  ``automation/render_lifecycle.py`` stays the single source of truth.
  An embedded ``dispatch_hold`` claim in a result file is re-validated
  against the registry (artifact/run pair plus successor mapping), so a
  stale or hand-crafted record can never redirect triage elsewhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from render_lifecycle import (  # noqa: E402
    HELD_SUPERSEDED_VERDICT,
    SUPERSEDED_WORKFLOW_ARTIFACTS,
    superseded_dispatch_guard,
    superseded_hold_verdict,
)

EXIT_HELD = 0
EXIT_NOT_HELD = 1
EXIT_USAGE_ERROR = 2
EXIT_DUPLICATE = 3
EXIT_EXHAUSTED = 4


def _empty_decision(
    artifact: str = "", run: str = "", reason: str = ""
) -> dict[str, Any]:
    return {
        "held": False,
        "verdict": "",
        "artifact_id": artifact,
        "source_run_id": run,
        "successor": {},
        "reason": reason or "no retired exact-artifact contract detected",
    }


def decide_from_body(title: object = "", body: object = "") -> dict[str, Any]:
    """Decide the hold from raw issue text (pure, never raises).

    Returns the held decision with the registry successor mapping when
    the text carries a retired (``SUPERSEDED_WORKFLOW_ARTIFACTS``)
    exact-artifact contract, else an explicit not-held decision.
    """
    try:
        guard = superseded_dispatch_guard(title, body)
    except Exception:
        guard = None
    if not isinstance(guard, dict):
        return _empty_decision()
    try:
        artifact = str(guard.get("artifact_id", "") or "").strip()
        run = str(guard.get("source_run_id", "") or "").strip()
        successor = guard.get("successor", {})
        successor = dict(successor) if isinstance(successor, Mapping) else {}
        entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact, run))
        if not entry:
            return _empty_decision(artifact, run)
        if successor != dict(entry):
            successor = dict(entry)
        return {
            "held": True,
            "verdict": HELD_SUPERSEDED_VERDICT,
            "artifact_id": artifact,
            "source_run_id": run,
            "successor": successor,
            "reason": str(guard.get("reason", "") or ""),
        }
    except Exception:
        return _empty_decision()


def decide_from_result(result: object) -> dict[str, Any]:
    """Decide the hold from a parsed refusal-result mapping (never raises).

    Prefers the embedded ``dispatch_hold`` verdict when it is a
    well-formed held claim, but re-validates the (artifact, run) pair
    and the successor mapping against the registry first: only the
    registry decides what is retired. Falls back to deriving the hold
    from the record's own ``artifact_id``/``source_run_id`` fields so
    pre-hold records (no ``dispatch_hold`` key) classify identically.
    Garbage input means "not held", never an exception.
    """
    try:
        if not isinstance(result, Mapping):
            return _empty_decision()
        hold = result.get("dispatch_hold")
        if isinstance(hold, Mapping) and hold.get("held") is True:
            try:
                artifact = str(hold.get("artifact_id", "") or "").strip()
                run = str(hold.get("source_run_id", "") or "").strip()
                entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact, run))
                if entry:
                    claimed = hold.get("successor", {})
                    claimed = (
                        dict(claimed) if isinstance(claimed, Mapping) else {}
                    )
                    successor = (
                        claimed if claimed == dict(entry) else dict(entry)
                    )
                    return {
                        "held": True,
                        "verdict": HELD_SUPERSEDED_VERDICT,
                        "artifact_id": artifact,
                        "source_run_id": run,
                        "successor": successor,
                        "reason": (
                            "retired exact artifact %s (workflow run %s) "
                            "is held-superseded; successor artifact %s "
                            "(workflow run %s)"
                            % (
                                artifact,
                                run,
                                entry.get("successor_artifact_id", "?"),
                                entry.get("successor_source_run_id", "?"),
                            )
                        ),
                    }
            except Exception:
                pass
        artifact = str(result.get("artifact_id", "") or "").strip()
        run = str(result.get("source_run_id", "") or "").strip()
        try:
            verdict = superseded_hold_verdict(
                {"artifact_id": artifact, "source_run_id": run}
            )
        except Exception:
            return _empty_decision(artifact, run)
        if (
            isinstance(verdict, dict)
            and verdict.get("held") is True
            and verdict.get("verdict") == HELD_SUPERSEDED_VERDICT
        ):
            successor = verdict.get("successor", {})
            successor = dict(successor) if isinstance(successor, Mapping) else {}
            return {
                "held": True,
                "verdict": HELD_SUPERSEDED_VERDICT,
                "artifact_id": artifact,
                "source_run_id": run,
                "successor": successor,
                "reason": (
                    "retired exact artifact %s (workflow run %s) "
                    "is held-superseded; successor artifact %s "
                    "(workflow run %s)"
                    % (
                        artifact,
                        run,
                        successor.get("successor_artifact_id", "?"),
                        successor.get("successor_source_run_id", "?"),
                    )
                ),
            }
        return _empty_decision(artifact, run)
    except Exception:
        return _empty_decision()


def _load_result_file(path: str) -> tuple[Any, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), ""
    except FileNotFoundError:
        return None, "result file not found: %s" % path
    except (OSError, ValueError) as exc:
        return None, "result file unreadable (%s): %s" % (path, exc)


def _load_body_file(path: str) -> tuple[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), ""
    except FileNotFoundError:
        return "", "body file not found: %s" % path
    except OSError as exc:
        return "", "body file unreadable (%s): %s" % (path, exc)


def format_hold_notice(
    decision: object,
    *,
    source_issue: object = 0,
    failed_run_id: object = "",
) -> str:
    """Format the deterministic successor notice for a held refusal.

    Repair issue #177 (failed run 36638450066, source issue #106, smoke):
    that run executed at a base already containing the #171 decision CLI,
    and the uploaded refusal record proves the CLI answers correctly live
    (``dispatch_hold.held=true`` re-validates to exit 0 on the exact
    ``render-qualification-106-36638450066`` bytes). Finalize still minted
    #177 because the CLI never specified what the envelope should do
    next: which branch wins (hold vs mint vs duplicate vs exhausted) and
    which successor text to post while keeping the source paused.

    This helper is that missing branch text: a single deterministic
    human-readable notice naming the retired contract, the failed run,
    and the registry successor, so every provisioned adoption posts
    identical triage instead of divergent free-form messages. Pure and
    never raises: a non-held or garbage decision yields "" (no notice),
    never a crash and never a misleading redirect.
    """
    try:
        if not isinstance(decision, Mapping):
            return ""
        if decision.get("held") is not True:
            return ""
        if decision.get("verdict") != HELD_SUPERSEDED_VERDICT:
            return ""
        artifact = str(decision.get("artifact_id", "") or "").strip()
        run = str(decision.get("source_run_id", "") or "").strip()
        successor = decision.get("successor", {})
        successor = dict(successor) if isinstance(successor, Mapping) else {}
        entry = SUPERSEDED_WORKFLOW_ARTIFACTS.get((artifact, run))
        if not entry:
            return ""
        if successor != dict(entry):
            successor = dict(entry)
        try:
            source = int(source_issue)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            source = 0
        source_label = "#%d" % source if source > 0 else "the source issue"
        failed = str(failed_run_id or "").strip() or "unknown run"
        return (
            "Held-superseded: Render execution for %s refused "
            "pre-creation with zero Render cost in run %s because it "
            "requires retired exact artifact %s (workflow run %s), which "
            "will never execute on Render -- redispatching this exact "
            "contract refuses identically. No new repair was minted. Use "
            "the current immutable successor artifact %s (workflow run "
            "%s, version %s) via %s instead of retrying this body."
            % (
                source_label,
                failed,
                artifact,
                run,
                successor.get("successor_artifact_id", "?"),
                successor.get("successor_source_run_id", "?"),
                successor.get("successor_version", "?"),
                successor.get("owner", "?"),
            )
        )
    except Exception:
        return ""


def finalize_repair_decision(
    decision: object,
    *,
    repair_count: object = 0,
    max_attempts: object = 10,
    existing_repair: object = "",
    source_issue: object = 0,
    failed_run_id: object = "",
) -> dict[str, Any]:
    """Mirror the finalize mint gate with the hold check inserted first.

    The provisioned ``continuum-render-executor.yml`` finalize step nests its
    branches: when ``REPAIR_COUNT < MAX_RENDER_REPAIR_ATTEMPTS`` it mints
    a P0 repair with no open repair carrying the source marker and posts
    a duplicate notice when such a repair is already open; otherwise
    (budget spent) it posts the exhausted notice. Smoke issues always
    reach that gate because the qualification-fingerprint dedup never
    fires for them (``not-chain`` with an empty fingerprint).

    This helper reproduces exactly that nesting with one insertion:
    a held-superseded decision (see ``decide_from_result`` /
    ``decide_from_body``) returns ``action="hold"`` with the
    deterministic ``format_hold_notice`` text, so the envelope keeps
    the source paused and skips minting instead of opening one more P0
    per redispatch. In particular an already-open repair only maps to
    ``duplicate`` while budget remains -- at or over budget the envelope
    reports ``exhausted`` even when a repair is open, and this helper
    does the same. Pure and never raises: garbage inputs fail open
    toward today's behavior (mint/duplicate/exhausted by the count
    inputs), never toward suppressing a legitimate repair; only an
    explicit registry-backed hold suppresses minting.
    """
    try:
        try:
            count = int(repair_count)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            count = 0
        try:
            limit = int(max_attempts)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            limit = 10
        if count < 0:
            count = 0
        if limit <= 0:
            limit = 10
        held = (
            isinstance(decision, Mapping)
            and decision.get("held") is True
            and decision.get("verdict") == HELD_SUPERSEDED_VERDICT
        )
        if held:
            try:
                artifact = str(decision.get("artifact_id", "") or "").strip()  # type: ignore[union-attr]
                run = str(decision.get("source_run_id", "") or "").strip()  # type: ignore[union-attr]
            except Exception:
                artifact, run = "", ""
            if (artifact, run) in SUPERSEDED_WORKFLOW_ARTIFACTS:
                return {
                    "action": "hold",
                    "notice": format_hold_notice(
                        decision,
                        source_issue=source_issue,
                        failed_run_id=failed_run_id,
                    ),
                }
        if count < limit:
            try:
                existing = str(existing_repair or "").strip()
            except Exception:
                existing = ""
            if existing:
                return {"action": "duplicate", "notice": ""}
            return {"action": "mint", "notice": ""}
        return {"action": "exhausted", "notice": ""}
    except Exception:
        return {"action": "mint", "notice": ""}


def finalize_exit_code(action: object) -> int:
    """Map a finalize action to the stable CLI exit code (never raises).

    Repair issue #182 (failed run 36640697494, source issue #106, smoke):
    that run executed at base ``35fb646``, which already contains the #177
    ``finalize_repair_decision`` library -- and the uploaded refusal record
    (``render-qualification-106-36640697494``, ``dispatch_hold.held=true``)
    re-validates to ``action="hold"`` with the successor notice offline.
    Finalize still minted #182 because the helper is library-only: the
    provisioned envelope cannot call library code without hand-rolling the
    repair-count/duplicate/exhausted plumbing in bash, which is exactly the
    registry duplication the hold series exists to avoid. The ``--finalize``
    CLI mode below exposes the same branch over inputs the finalize step
    already holds, keyed by these exit codes. Unknown actions fail open
    toward ``mint`` (exit 1, today's behavior), never toward suppression.
    """
    try:
        if action == "hold":
            return EXIT_HELD
        if action == "duplicate":
            return EXIT_DUPLICATE
        if action == "exhausted":
            return EXIT_EXHAUSTED
        return EXIT_NOT_HELD
    except Exception:
        return EXIT_NOT_HELD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Decide whether a Render exact-artifact refusal is "
            "held-superseded. Prints one JSON decision; exits 0 when "
            "held, 1 when not held, 2 on CLI usage errors."
        )
    )
    parser.add_argument("--title", default="", help="Issue title text.")
    parser.add_argument("--body", default="", help="Issue body text.")
    parser.add_argument(
        "--body-file",
        default="",
        help="Path to a file holding the issue body text.",
    )
    parser.add_argument(
        "--result-file",
        default="",
        help="Path to a refusal-result JSON file (e.g. $RENDER_RESULT_FILE).",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help=(
            "Emit the finalize mint/duplicate/exhausted/hold branch "
            "instead of the hold decision. Prints one JSON object with "
            "action/notice; exits 0 hold, 1 mint, 3 duplicate, "
            "4 exhausted, 2 usage error."
        ),
    )
    parser.add_argument(
        "--repair-count",
        default="0",
        help="Finalize-mode REPAIR_COUNT (repair-attempt comments so far).",
    )
    parser.add_argument(
        "--max-attempts",
        default="10",
        help="Finalize-mode MAX_RENDER_REPAIR_ATTEMPTS budget.",
    )
    parser.add_argument(
        "--existing-repair",
        default="",
        help="Finalize-mode open repair number, if any (else empty).",
    )
    parser.add_argument(
        "--source-issue",
        default="0",
        help="Finalize-mode source issue number for the hold notice.",
    )
    parser.add_argument(
        "--failed-run-id",
        default="",
        help="Finalize-mode failed workflow run id for the hold notice.",
    )
    args = parser.parse_args(argv)

    has_body_input = bool(args.title or args.body or args.body_file)
    has_result_input = bool(args.result_file)
    if not has_body_input and not has_result_input:
        parser.print_usage(sys.stderr)
        print(
            "error: provide --result-file and/or --title/--body/--body-file",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if has_body_input and has_result_input:
        parser.print_usage(sys.stderr)
        print(
            "error: --result-file is mutually exclusive with "
            "--title/--body/--body-file",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    if has_result_input:
        loaded, error = _load_result_file(args.result_file)
        if error:
            print("error: %s" % error, file=sys.stderr)
            return EXIT_USAGE_ERROR
        decision = decide_from_result(loaded)
    else:
        body = args.body
        if args.body_file:
            body, error = _load_body_file(args.body_file)
            if error:
                print("error: %s" % error, file=sys.stderr)
                return EXIT_USAGE_ERROR
        decision = decide_from_body(args.title, body)

    if args.finalize:
        outcome = finalize_repair_decision(
            decision,
            repair_count=args.repair_count,
            max_attempts=args.max_attempts,
            existing_repair=args.existing_repair,
            source_issue=args.source_issue,
            failed_run_id=args.failed_run_id,
        )
        try:
            action = str(outcome.get("action", "mint") or "mint")
        except Exception:
            action = "mint"
        try:
            notice = str(outcome.get("notice", "") or "")
        except Exception:
            notice = ""
        print(json.dumps({"action": action, "notice": notice}, sort_keys=True))
        return finalize_exit_code(action)

    print(json.dumps(decision, sort_keys=True))
    return EXIT_HELD if decision.get("held") is True else EXIT_NOT_HELD


if __name__ == "__main__":
    raise SystemExit(main())
