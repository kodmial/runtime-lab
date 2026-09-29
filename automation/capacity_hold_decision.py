#!/usr/bin/env python3
"""Envelope-consumable hold decision for proven capacity mismatches.

Repair issue #176 (failed run 36637940250, source issue #58, smoke):
that run executed at base ``bc76158``, which already contains the
complete validated config-only profile from the #75/#159/#164/#170
repairs (production ``OPENCODE_CONFIG_CONTENT`` equals
``lowmem_config()`` exactly, every qualified env kill-switch wired),
and still storm-aborted with the identical signature as the four
prior storms (4 consecutive proven worker restarts, cgroup pinned at
the 512 MB limit with usage ratio 1.0, PRESSURE via the replacements
branch, Python RSS ~33 MB). The storm breaker fired exactly as
designed, so the harness is not the defect; the pinned baseline
agent peak (~600-615 MB, issue #52) physically exceeds the Free
worker (0.1 CPU / 512 MB, ``https://render.com/docs/free``,
re-verified 2026-09-29), and no further config-only switch exists to
wire. Redispatching the identical ordinary-smoke payload therefore
storms identically while the finalize path mints another P0 repair
(smoke issues carry no qualification fingerprint, so the
fingerprint-dedup branch never fires).

The reusable defect fixed here is therefore dispatch-side, not
signal-side: no provisioned envelope step can answer "is this issue
held for a proven capacity mismatch?" without embedding registry
logic in workflow JS/YAML. This module is that answer as a
stdlib-only executable decision point. The provisioned
``render-executor.yml`` finalize step runs on a runner with a full
checkout of ``main``, so it can adopt the hold with three lines and
no registry duplication::

    if python3 automation/capacity_hold_decision.py \
        --issue "$ISSUE_NUMBER" --title "$ISSUE_TITLE" \
        --body-file /tmp/issue-body.txt >/tmp/capacity-hold.json; then
      # held-capacity-mismatch: keep the source paused, note the
      # successor premise, and skip minting another P0 repair for an
      # identical storm.
    fi

The same module answers the scheduler pre-dispatch question wherever
a checkout exists; scheduler/auto-merge adoption itself is
provisioned separately (the Actions token cannot push workflow-file
changes). The live executor (``automation/render-job.sh``) already
consults the same registry before any Render service is created and
exits with the ``held-capacity-mismatch`` verdict at zero Render
cost; this CLI lets envelopes key on the identical decision without
re-implementing it.

Contract (stable for provisioned envelopes):
- stdout is always one JSON object: ``held`` (bool), ``verdict``
  (``"held-capacity-mismatch"`` or ``""``), ``issue_number`` (int),
  ``successor`` (registry mapping or ``{}``), ``reason`` (human line).
- exit 0 means held: the issue proved an ordinary-smoke capacity
  mismatch at full profile and redispatch holds identically; the
  envelope should hold/skip instead of minting.
- exit 1 means not held: existing behavior continues unchanged.
- exit 2 means CLI usage error (no input flags, unreadable file,
  non-numeric issue). Garbage *content* is never a usage error:
  unparsable bodies/records mean "not held" (fail open toward
  today's behavior), never a crash.
- No network, no GitHub calls, no Render calls, stdlib only. Never
  raises out of the public helpers: garbage in means "not held".
- The ``CAPACITY_MISMATCH_HOLDS`` registry in
  ``automation/render_lifecycle.py`` stays the single source of
  truth. An embedded ``capacity_hold`` claim in a result file is
  re-validated against the registry (issue number plus successor
  mapping), so a stale or hand-crafted record can never redirect
  triage elsewhere. Lifting a hold removes its registry entry when
  the successor premise lands; this module follows without changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from render_lifecycle import (  # noqa: E402
    CAPACITY_MISMATCH_HOLDS,
    HELD_CAPACITY_VERDICT,
    capacity_dispatch_hold,
    capacity_hold_verdict,
)

EXIT_HELD = 0
EXIT_NOT_HELD = 1
EXIT_USAGE_ERROR = 2


def _empty_decision(
    issue: int = 0, reason: str = ""
) -> dict[str, Any]:
    return {
        "held": False,
        "verdict": "",
        "issue_number": issue,
        "successor": {},
        "reason": reason or "no proven ordinary-smoke capacity mismatch detected",
    }


def decide_from_dispatch(
    issue_number: object = 0, title: object = "", body: object = ""
) -> dict[str, Any]:
    """Decide the hold from an issue number plus raw text (pure, never raises).

    Returns the held decision with the registry successor mapping when
    the issue number names a ``CAPACITY_MISMATCH_HOLDS`` entry and the
    text is the ordinary (non-exact-artifact) shape, else an explicit
    not-held decision.
    """
    try:
        try:
            issue = int(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _empty_decision()
        guard = capacity_dispatch_hold(issue, title, body)
    except Exception:
        return _empty_decision()
    if not isinstance(guard, dict):
        try:
            issue = int(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            issue = 0
        return _empty_decision(issue)
    try:
        entry = CAPACITY_MISMATCH_HOLDS.get(issue)
        if not entry:
            return _empty_decision(issue)
        successor = guard.get("successor", {})
        successor = dict(successor) if isinstance(successor, Mapping) else {}
        if successor != dict(entry.get("successor", {})):
            successor = dict(entry.get("successor", {}))
        return {
            "held": True,
            "verdict": HELD_CAPACITY_VERDICT,
            "issue_number": issue,
            "successor": successor,
            "reason": str(guard.get("reason", "") or ""),
        }
    except Exception:
        try:
            issue = int(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            issue = 0
        return _empty_decision(issue)


def decide_from_result(result: object) -> dict[str, Any]:
    """Decide the hold from a parsed held-result mapping (never raises).

    Prefers the embedded ``capacity_hold`` verdict when it is a
    well-formed held claim, but re-validates the issue number and the
    successor mapping against the registry first: only the registry
    decides what is held. Falls back to deriving the hold from the
    record's own ``issue_number`` field so pre-hold records (no
    ``capacity_hold`` key) classify identically. Garbage input means
    "not held", never an exception.
    """
    try:
        if not isinstance(result, Mapping):
            return _empty_decision()
        hold = result.get("capacity_hold")
        if isinstance(hold, Mapping) and hold.get("held") is True:
            try:
                raw_issue = hold.get("issue_number", 0)
                issue = int(raw_issue)  # type: ignore[arg-type]
                entry = CAPACITY_MISMATCH_HOLDS.get(issue)
                if entry:
                    claimed = hold.get("successor", {})
                    claimed = (
                        dict(claimed) if isinstance(claimed, Mapping) else {}
                    )
                    successor = (
                        claimed
                        if claimed == dict(entry.get("successor", {}))
                        else dict(entry.get("successor", {}))
                    )
                    return {
                        "held": True,
                        "verdict": HELD_CAPACITY_VERDICT,
                        "issue_number": issue,
                        "successor": successor,
                        "reason": (
                            "ordinary-smoke issue #%d is "
                            "held-capacity-mismatch; successor premise "
                            "via %s"
                            % (issue, successor.get("owner", "?"))
                        ),
                    }
            except Exception:
                pass
        try:
            raw_issue = result.get("issue_number", 0)
            issue = int(raw_issue)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _empty_decision()
        try:
            verdict = capacity_hold_verdict(issue)
        except Exception:
            return _empty_decision(issue)
        if (
            isinstance(verdict, dict)
            and verdict.get("held") is True
            and verdict.get("verdict") == HELD_CAPACITY_VERDICT
        ):
            successor = verdict.get("successor", {})
            successor = dict(successor) if isinstance(successor, Mapping) else {}
            return {
                "held": True,
                "verdict": HELD_CAPACITY_VERDICT,
                "issue_number": issue,
                "successor": successor,
                "reason": (
                    "ordinary-smoke issue #%d is held-capacity-mismatch; "
                    "successor premise via %s"
                    % (issue, successor.get("owner", "?"))
                ),
            }
        return _empty_decision(issue)
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
            "Decide whether an ordinary-smoke issue is held for a "
            "proven capacity mismatch. Prints one JSON decision; exits "
            "0 when held, 1 when not held, 2 on CLI usage errors."
        )
    )
    parser.add_argument(
        "--issue",
        default="",
        help="Issue number the dispatch/result belongs to.",
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
        help="Path to a held-result JSON file (e.g. $RENDER_RESULT_FILE).",
    )
    args = parser.parse_args(argv)

    has_dispatch_input = bool(args.issue or args.title or args.body or args.body_file)
    has_result_input = bool(args.result_file)
    if not has_dispatch_input and not has_result_input:
        parser.print_usage(sys.stderr)
        print(
            "error: provide --result-file and/or "
            "--issue/--title/--body/--body-file",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if has_dispatch_input and has_result_input:
        parser.print_usage(sys.stderr)
        print(
            "error: --result-file is mutually exclusive with "
            "--issue/--title/--body/--body-file",
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
        if not str(args.issue or "").strip():
            parser.print_usage(sys.stderr)
            print(
                "error: --issue is required with "
                "--title/--body/--body-file",
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        try:
            issue = int(str(args.issue).strip())
        except (TypeError, ValueError):
            print(
                "error: --issue must be a positive integer, got %r"
                % (args.issue,),
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        body = args.body
        if args.body_file:
            body, error = _load_body_file(args.body_file)
            if error:
                print("error: %s" % error, file=sys.stderr)
                return EXIT_USAGE_ERROR
        decision = decide_from_dispatch(issue, args.title, body)

    print(json.dumps(decision, sort_keys=True))
    return EXIT_HELD if decision.get("held") is True else EXIT_NOT_HELD


if __name__ == "__main__":
    raise SystemExit(main())
