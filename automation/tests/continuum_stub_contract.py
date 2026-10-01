"""Stdlib-only reader for the Continuum caller stubs this repository owns.

Commit ``fbf4b79`` replaced this repository's local Continuum forks with thin
callers: every core automation workflow under ``.github/workflows/`` is now a
single-job ``uses:`` delegation into ``kodmial/continuum``. The invariants
those forks used to carry inline -- concurrency shape, region and model
policy, harness entrypoints -- are Continuum's responsibility now, so a
consumer test can no longer assert them against workflow text.

What this repository *can* still verify is the caller boundary, and this
module exposes exactly that:

- which reusable workflow a caller delegates to, and at which ref;
- which ``workflow_dispatch`` inputs it declares and what it forwards;
- what a caller must never reintroduce locally -- a top-level
  ``concurrency:`` block (which would shadow Continuum's per-issue groups
  with a global mutex), a literal region/model pin (which would override the
  consumer's repository variable for every installed caller), or an inline
  job body at all. "No inline body" is a parse-time invariant rather than a
  reported field: a caller carrying steps is rejected outright, so there is
  no surviving object whose ``has_inline_steps`` could be consulted.

The parser is deliberately strict: a caller whose shape stops matching
raises :class:`ValueError` instead of quietly yielding empty fields, so a
structural refactor surfaces as a failure rather than a vacuous pass.
"""

from __future__ import annotations

import os
import re
from typing import Dict, NamedTuple, Optional, Tuple

CONTINUUM_OWNER = "kodmial/continuum"
CALLER_JOB_NAME = "call"
WORKFLOWS = os.path.join(".github", "workflows")

_GROUP_RE = re.compile(r"^\s*group:\s*(?P<value>.+?)\s*$")


class CallerStub(NamedTuple):
    """The delegation facts of one thin Continuum caller."""

    workflow_name: str
    callee_owner: str
    callee_workflow: str
    callee_ref: str
    job_name: str
    job_count: int
    secrets_inherit: bool
    local_concurrency_group: Optional[str]
    declared_inputs: Tuple[str, ...]
    forwarded: Dict[str, str]

    def delegates_to(self, workflow: str, ref: Optional[str] = None) -> bool:
        """True when the caller invokes ``workflow`` (optionally at ``ref``)."""
        if self.callee_workflow != workflow:
            return False
        return ref is None or self.callee_ref == ref

    def forwards_bare(self, name: str) -> bool:
        """True when ``name`` is forwarded as a bare ``${{ inputs.name }}``.

        A bare passthrough is the contract that keeps the callee's own
        ``vars.`` fallback reachable. A literal here would override the
        repository variable of every consumer that installed this caller.
        """
        return self.forwarded.get(name) == "${{ inputs.%s }}" % name

    def bare_passthrough_inputs(self) -> Tuple[str, ...]:
        """Declared inputs that are forwarded as bare passthroughs."""
        return tuple(
            name for name in self.declared_inputs if self.forwards_bare(name)
        )


def caller_stub_text(repo_root: str, name: str) -> str:
    """Read one caller stub by file name (with or without the .yml suffix)."""
    filename = name if name.endswith(".yml") else name + ".yml"
    with open(
        os.path.join(repo_root, WORKFLOWS, filename), "r", encoding="utf-8"
    ) as handle:
        return handle.read()


def parse_caller_stub(text: str) -> CallerStub:
    """Parse a thin Continuum caller stub.

    Raises :class:`ValueError` when the document is not a single-job caller
    that delegates to Continuum, so a malformed or forked-back stub cannot
    make a downstream assertion pass by accident.
    """
    lines = text.splitlines()

    workflow_name = _top_level_scalar(lines, "name")
    if not workflow_name:
        raise ValueError("caller stub has no top-level `name:`")

    on_index = _index_of_top_level(lines, "on")
    jobs_index = _index_of_top_level(lines, "jobs")
    if on_index is None:
        raise ValueError("caller stub declares no top-level `on:` block")
    if jobs_index is None:
        raise ValueError("caller stub declares no top-level `jobs:` block")

    declared_inputs = _declared_dispatch_inputs(lines[on_index + 1 : jobs_index])
    jobs = _parse_jobs(lines[jobs_index + 1 :])

    delegating = [job for job in jobs if job["uses"]]
    if len(delegating) != 1:
        raise ValueError(
            "a thin caller must contain exactly one delegating job; found %d "
            "(jobs: %s)"
            % (len(delegating), ", ".join(job["name"] for job in jobs))
        )
    job = delegating[0]

    owner, _, target = job["uses"].partition("/.github/workflows/")
    if not target:
        raise ValueError(
            "delegating job %r does not use a Continuum reusable workflow: %r"
            % (job["name"], job["uses"])
        )
    callee_workflow, _, callee_ref = target.partition("@")
    if not owner:
        raise ValueError("continuum caller stub names no owner repository")
    if not callee_ref:
        raise ValueError(
            "continuum caller stub pins no ref: %r" % job["uses"]
        )
    if owner != CONTINUUM_OWNER:
        raise ValueError(
            "continuum caller stub must delegate to %s, got %r"
            % (CONTINUUM_OWNER, owner)
        )

    if any(other["steps"] for other in jobs):
        raise ValueError(
            "a thin caller must not carry inline job steps; found them in: %s"
            % ", ".join(
                other["name"] for other in jobs if other["steps"]
            )
        )

    return CallerStub(
        workflow_name=workflow_name,
        callee_owner=owner,
        callee_workflow=callee_workflow,
        callee_ref=callee_ref,
        job_name=job["name"],
        job_count=len(jobs),
        secrets_inherit=job["secrets_inherit"],
        local_concurrency_group=_top_level_concurrency_group(lines),
        declared_inputs=declared_inputs,
        forwarded=job["forwarded"],
    )


def local_concurrency_groups(text: str) -> Tuple[str, ...]:
    """Return every ``group:`` value declared by the workflow itself.

    Unlike a body-wide regex this reads the workflow's own top-level
    ``concurrency:`` block only, so a group string quoted inside a
    comment or a forwarded input value is never mistaken for one.
    """
    lines = text.splitlines()
    index = _index_of_top_level(lines, "concurrency")
    if index is None:
        return ()
    groups = []
    for line in lines[index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2:
            match = _GROUP_RE.match(line)
            if match:
                groups.append(match.group("value").strip("\"'"))
    return tuple(groups)


# ---------------------------------------------------------------------------
# Line-oriented parsing helpers.
# ---------------------------------------------------------------------------


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _index_of_top_level(lines, key: str) -> Optional[int]:
    for index, line in enumerate(lines):
        if line.rstrip() == key + ":":
            return index
    return None


def _top_level_scalar(lines, key: str) -> str:
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith(key + ":"):
            return stripped[len(key) + 1 :].strip().strip("\"'")
    return ""


def _top_level_concurrency_group(lines) -> Optional[str]:
    groups = []
    index = _index_of_top_level(lines, "concurrency")
    if index is None:
        return None
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        if _indent_of(line) == 0:
            break
        match = _GROUP_RE.match(line)
        if match and _indent_of(line) == 2:
            groups.append(match.group("value").strip("\"'"))
    return groups[0] if groups else None


def _declared_dispatch_inputs(block) -> Tuple[str, ...]:
    names = []
    in_dispatch = False
    in_inputs = False
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent_of(line)
        if indent == 2:
            in_dispatch = stripped == "workflow_dispatch:"
            in_inputs = False
            continue
        if not in_dispatch:
            continue
        if indent == 4:
            in_inputs = stripped == "inputs:"
            continue
        if in_inputs:
            if indent < 6:
                break
            if indent == 6 and stripped.endswith(":"):
                names.append(stripped[:-1].strip())
    return tuple(names)


def _parse_jobs(block):
    jobs = []
    current = None
    in_with = False

    def _close():
        return {
            "name": current["name"],
            "uses": current["uses"],
            "steps": current["steps"],
            "secrets_inherit": current["secrets_inherit"],
            "forwarded": current["forwarded"],
        }

    current = {
        "name": None,
        "uses": None,
        "steps": False,
        "secrets_inherit": False,
        "forwarded": {},
    }
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent_of(line)
        if indent == 2 and stripped.endswith(":"):
            if current["name"] is not None:
                jobs.append(_close())
            current = {
                "name": stripped[:-1].strip(),
                "uses": None,
                "steps": False,
                "secrets_inherit": False,
                "forwarded": {},
            }
            in_with = False
            continue
        if current["name"] is None:
            continue
        if indent <= 2:
            in_with = False
            continue
        if indent == 4:
            in_with = stripped == "with:"
            if stripped.startswith("uses:"):
                current["uses"] = stripped[len("uses:") :].strip()
            elif stripped == "secrets: inherit":
                current["secrets_inherit"] = True
            elif stripped == "steps:":
                current["steps"] = True
            continue
        if indent == 6 and in_with:
            key, _, value = stripped.partition(":")
            # YAML quoting is presentation, not part of the forwarded value.
            current["forwarded"][key.strip()] = value.strip().strip("\"'")
    if current["name"] is not None:
        jobs.append(_close())
    return jobs
