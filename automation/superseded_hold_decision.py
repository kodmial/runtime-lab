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
The provisioned ``render-executor.yml`` finalize step runs on a runner
with a full checkout of ``main``, so it can adopt the hold with three
lines and no registry duplication::

    if python3 automation/superseded_hold_decision.py \
        --result-file "$RENDER_RESULT_FILE" >/tmp/hold.json; then
      # held-superseded: keep the source paused, note the successor,
      # and skip minting another P0 repair for an identical refusal.
    fi

The same module answers the scheduler pre-dispatch question from the
issue text (``--title``/``--body``/``--body-file``) wherever a checkout
exists; scheduler/auto-merge adoption itself is provisioned separately
(the Actions token cannot push workflow-file changes).

Contract (stable for provisioned envelopes):
- stdout is always one JSON object: ``held`` (bool), ``verdict``
  (``"held-superseded"`` or ``""``), ``artifact_id``, ``source_run_id``,
  ``successor`` (registry mapping or ``{}``), ``reason`` (human line).
- exit 0 means held-superseded: the contract is retired and redispatch
  refuses identically; the envelope should hold/skip instead of minting.
- exit 1 means not held: existing behavior continues unchanged.
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

    print(json.dumps(decision, sort_keys=True))
    return EXIT_HELD if decision.get("held") is True else EXIT_NOT_HELD


if __name__ == "__main__":
    raise SystemExit(main())
