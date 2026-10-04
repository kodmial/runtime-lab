"""Maintainable low-memory OpenCode fork against upstream changes (issue #87).

Stdlib only. Offline, no network or git mutations. This module is the
machine-readable maintenance contract for ``kodmial/opencode`` after the
optimized fork is proven (blocked by #81, which is closed):

- records the upstream remote/base revision and the small set of
  intentional fork deltas (``automation/opencode-fork-maintenance.json``);
- provides a deterministic upstream-sync/rebase procedure
  (``sync_steps`` / ``sync_plan``) so a future upstream revision can be
  evaluated and rebased without reconstructing the fork strategy;
- rebuilds the lightweight artifacts after an upstream refresh
  (``build_commands``);
- runs the focused coding-agent correctness gate plus the memory smoke
  benchmark after sync (``correctness_plan`` / ``memory_smoke_plan`` /
  ``evaluate_sync_budget``);
- detects when upstream has independently removed/changed a forked
  subsystem so obsolete patches can be dropped
  (``detect_obsolete_deltas``);
- produces a concise patch/delta inventory reviewable by purpose rather
  than raw diff size (``render_delta_inventory``);
- never auto-merges upstream changes that break the low-memory
  acceptance budget (``assert_merge_allowed``);
- turns refresh failures into an actionable Runtime Lab task instead of
  silently leaving the deployed fork stale
  (``render_refresh_failure_task``).

Source grounding: fork baseline
``automation/opencode-fork.baseline.json`` (fork ``main`` over upstream
``anomalyco/opencode`` base ``75e1e7a``, pin ``1.18.33``); variant specs
``automation/opencode-lite.spec.json``,
``automation/opencode-bounds.spec.json``,
``automation/opencode-artifacts.spec.json``; patch docs in
``automation/patches/``; budgets reuse the ``#81`` vocabulary from
``automation/opencode_qualification.py`` (450 MiB preferred target,
512 MiB hard limit).
"""

from __future__ import annotations

import json
import os
import re

SCHEMA = "runtime-lab-opencode-fork-maintenance/v1"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

REQUIRED_KEYS = frozenset(
    (
        "schema",
        "fork_repo",
        "fork_branch",
        "upstream_repo",
        "upstream_remote",
        "upstream_default_branch",
        "upstream_base_commit",
        "upstream_tag",
        "pinned_opencode_version",
        "baseline_file",
        "deltas",
        "sync_order",
        "sync_procedure",
        "rebuild",
        "correctness",
        "memory_smoke",
        "budgets",
        "refresh_failure",
        "invariants",
        "archs",
    )
)

REQUIRED_DELTA_KEYS = frozenset(
    (
        "id",
        "purpose",
        "patch_doc",
        "runtime_lab_issue",
        "status",
        "group",
        "touches",
        "upstream_probes",
        "obsolete_when",
        "validator",
    )
)

# Optional delta keys that carry extra provenance without changing the
# maintenance contract (e.g. the fork branch carrying a specified patch).
OPTIONAL_DELTA_KEYS = frozenset(("branch",))

# Refresh stages in execution order. The failure-task renderer accepts
# only these stages so a stale/unknown stage can never file a vague task.
SYNC_STAGES = (
    "record-revisions",
    "fetch-upstream",
    "verify-ancestry",
    "rebase-deltas",
    "rebuild-artifacts",
    "correctness-gate",
    "memory-smoke",
    "obsolete-scan",
    "budget-gate",
    "publish-inventory",
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def maintenance_path() -> str:
    """Return the absolute path of the fork-maintenance inventory."""
    return os.path.join(
        _repo_root(), "automation", "opencode-fork-maintenance.json"
    )


def load_maintenance(path: str | None = None) -> dict:
    """Load and validate the maintenance inventory; fail closed."""
    target = path or maintenance_path()
    with open(target, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return validate_maintenance(data)


def validate_maintenance(data: dict) -> dict:
    """Validate a parsed maintenance object and return it on success."""
    if not isinstance(data, dict):
        raise ValueError("maintenance must be a JSON object")
    missing = sorted(REQUIRED_KEYS - set(data.keys()))
    extra = sorted(set(data.keys()) - REQUIRED_KEYS)
    if missing or extra:
        raise ValueError(
            "maintenance keys mismatch: missing=%s extra=%s" % (missing, extra)
        )
    if data.get("schema") != SCHEMA:
        raise ValueError("invalid maintenance schema: %r" % data.get("schema"))
    for key in ("fork_repo", "upstream_repo"):
        value = data.get(key)
        if not isinstance(value, str) or REPO_RE.match(value) is None:
            raise ValueError("invalid repository name for %s: %r" % (key, value))
    if data.get("fork_repo") != "kodmial/opencode":
        raise ValueError("fork_repo must be kodmial/opencode")
    if data.get("upstream_repo") != "anomalyco/opencode":
        raise ValueError("upstream_repo must be anomalyco/opencode")
    branch = data.get("fork_branch")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("fork_branch must be a non-empty string")
    remote = data.get("upstream_remote")
    if not isinstance(remote, str) or not remote.strip():
        raise ValueError("upstream_remote must be a non-empty string")
    if "anomalyco/opencode" not in remote:
        raise ValueError(
            "upstream_remote must point at anomalyco/opencode: %r" % remote
        )
    base = data.get("upstream_base_commit")
    if not isinstance(base, str) or SHA_RE.match(base) is None:
        raise ValueError("invalid 40-char upstream_base_commit: %r" % base)
    tag = data.get("upstream_tag")
    version = data.get("pinned_opencode_version")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("upstream_tag must start with 'v': %r" % tag)
    if not isinstance(version, str) or VERSION_RE.match(version) is None:
        raise ValueError("invalid pinned_opencode_version: %r" % version)
    if tag != "v%s" % version:
        raise ValueError(
            "upstream_tag %r does not match pinned version %r" % (tag, version)
        )
    deltas = data.get("deltas")
    if not isinstance(deltas, list) or not deltas:
        raise ValueError("deltas must be a non-empty list")
    seen: set[str] = set()
    for delta in deltas:
        validate_delta(delta)
        if delta["id"] in seen:
            raise ValueError("duplicate delta id: %r" % delta["id"])
        seen.add(delta["id"])
    order = data.get("sync_order")
    if not isinstance(order, list) or sorted(order) != sorted(seen):
        raise ValueError(
            "sync_order must list every delta id exactly once: %r" % order
        )
    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        raise ValueError("budgets must be an object")
    target_bytes = budgets.get("target_bytes")
    limit_bytes = budgets.get("hard_limit_bytes")
    for key, value in (
        ("target_bytes", target_bytes),
        ("hard_limit_bytes", limit_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("budgets.%s must be a positive int" % key)
    if not target_bytes < limit_bytes:
        raise ValueError("budgets.target_bytes must be below hard_limit_bytes")
    rebuild = data.get("rebuild")
    if not isinstance(rebuild, dict) or not rebuild.get("commands"):
        raise ValueError("rebuild.commands must be non-empty")
    if not rebuild.get("build_scripts"):
        raise ValueError("rebuild.build_scripts must be non-empty")
    correctness = data.get("correctness")
    if not isinstance(correctness, dict) or not correctness.get("pytest_subset"):
        raise ValueError("correctness.pytest_subset must be non-empty")
    failure = data.get("refresh_failure")
    if not isinstance(failure, dict) or not failure.get("title_template"):
        raise ValueError("refresh_failure.title_template must be set")
    return data


def validate_delta(delta: dict) -> dict:
    """Validate one fork-delta entry; fail closed on any defect."""
    if not isinstance(delta, dict):
        raise ValueError("delta must be an object")
    missing = sorted(REQUIRED_DELTA_KEYS - set(delta.keys()))
    extra = sorted(set(delta.keys()) - REQUIRED_DELTA_KEYS - OPTIONAL_DELTA_KEYS)
    if missing or extra:
        raise ValueError(
            "delta keys mismatch: missing=%s extra=%s" % (missing, extra)
        )
    delta_id = delta.get("id")
    if not isinstance(delta_id, str) or not delta_id.strip():
        raise ValueError("delta.id must be a non-empty string")
    if not str(delta.get("purpose", "")).strip():
        raise ValueError("delta %r needs a purpose" % delta_id)
    if not str(delta.get("patch_doc", "")).strip():
        raise ValueError("delta %r needs a patch_doc" % delta_id)
    if not isinstance(delta.get("runtime_lab_issue"), int):
        raise ValueError("delta %r needs an int runtime_lab_issue" % delta_id)
    touches = delta.get("touches")
    if not isinstance(touches, dict):
        raise ValueError("delta %r touches must be an object" % delta_id)
    for key in ("added", "modified"):
        value = touches.get(key)
        if not isinstance(value, list):
            raise ValueError("delta %r touches.%s must be a list" % (delta_id, key))
    if not touches["added"] and not touches["modified"]:
        raise ValueError("delta %r touches no files" % delta_id)
    probes = delta.get("upstream_probes")
    if not isinstance(probes, list) or not probes:
        raise ValueError("delta %r needs non-empty upstream_probes" % delta_id)
    if not str(delta.get("obsolete_when", "")).strip():
        raise ValueError("delta %r needs an obsolete_when rule" % delta_id)
    if not str(delta.get("validator", "")).strip():
        raise ValueError("delta %r needs a validator" % delta_id)
    return delta


def delta_ids(maintenance: dict) -> list[str]:
    """Delta ids in inventory order."""
    return [delta["id"] for delta in maintenance.get("deltas", [])]


def delta_by_id(maintenance: dict, delta_id: str) -> dict:
    """Return one delta entry by id; fail closed on unknown ids."""
    for delta in maintenance.get("deltas", []):
        if delta.get("id") == delta_id:
            return delta
    raise ValueError("unknown fork delta: %r" % delta_id)


def check_consistent_with_baseline(maintenance: dict, baseline: dict) -> None:
    """Fail closed when the inventory drifts from the fork baseline."""
    for key in (
        "fork_repo",
        "fork_branch",
        "upstream_repo",
        "upstream_base_commit",
        "upstream_tag",
        "pinned_opencode_version",
    ):
        expected = baseline.get(key)
        actual = maintenance.get(key)
        if actual != expected:
            raise ValueError(
                "maintenance %s %r does not match baseline %r"
                % (key, actual, expected)
            )
    if maintenance.get("upstream_remote") != (
        baseline.get("sync") or {}
    ).get("upstream_remote"):
        raise ValueError("maintenance upstream_remote does not match baseline")


def check_consistent_with_specs(
    maintenance: dict,
    lite_spec: dict,
    bounds_spec: dict,
    artifacts_spec: dict,
) -> None:
    """Fail closed when variant specs drift from the maintenance pin."""
    fork_repo = maintenance["fork_repo"]
    version = maintenance["pinned_opencode_version"]
    fork_sha = maintenance["upstream_base_commit"]
    for name, spec in (
        ("lite", lite_spec),
        ("bounds", bounds_spec),
        ("artifacts", artifacts_spec),
    ):
        if spec.get("fork_repo") != fork_repo:
            raise ValueError("%s spec fork_repo drifted" % name)
        if spec.get("pinned_opencode_version", spec.get("pinned_version")) not in (
            version,
            None,
        ) and spec.get("pinned_opencode_version") != version:
            # artifacts spec uses pinned_opencode_version; be explicit.
            raise ValueError("%s spec pinned version drifted" % name)
    # The lite/bounds/artifacts specs pin the fork base SHA they were
    # authored against; the maintenance upstream base must equal the
    # artifacts upstream base (the build-ancestor proof).
    if artifacts_spec.get("upstream_base_commit") != fork_sha:
        raise ValueError("artifacts spec upstream_base_commit drifted")
    if artifacts_spec.get("upstream_repo") != maintenance["upstream_repo"]:
        raise ValueError("artifacts spec upstream_repo drifted")


def _short(sha: str) -> str:
    return sha[:12]


def sync_steps(
    maintenance: dict,
    upstream_candidate_sha: str,
    fork_sha: str | None = None,
) -> list[str]:
    """Return the deterministic upstream-sync/rebase shell steps.

    The steps never auto-merge over a broken budget: the budget gate is
    an explicit step whose failure stops the sync and files a Runtime
    Lab task via ``render_refresh_failure_task``. No git mutation happens
    here; this only renders the procedure.
    """
    if not isinstance(upstream_candidate_sha, str) or (
        SHA_RE.match(upstream_candidate_sha.strip()) is None
    ):
        raise ValueError("upstream_candidate_sha must be a 40-char SHA")
    candidate = upstream_candidate_sha.strip()
    if candidate == maintenance.get("upstream_base_commit"):
        raise ValueError("candidate equals the recorded base: nothing to sync")
    current_fork = (fork_sha or "").strip() or "<fork-head-sha>"
    if fork_sha is not None and SHA_RE.match(current_fork) is None:
        raise ValueError("fork_sha must be a 40-char SHA when supplied")
    upstream = maintenance["upstream_repo"]
    remote = maintenance["upstream_remote"]
    branch = maintenance["fork_branch"]
    base = maintenance["upstream_base_commit"]
    eval_branch = "fork-sync/%s" % _short(candidate)
    order = list(maintenance.get("sync_order", []))
    steps = [
        "# 1. record revisions (fail closed on mutable refs)",
        "test \"$(git rev-parse HEAD)\" = \"%s\" # fork HEAD under test"
        % current_fork,
        "git remote get-url upstream || git remote add upstream %s" % remote,
        "git fetch upstream",
        "git cat-file -t %s # candidate exists" % candidate,
        "# 2. prove the recorded base is history (never rebase from scratch)",
        "git merge-base --is-ancestor %s HEAD" % base,
        "git merge-base --is-ancestor %s %s # upstream %s contains %s/%s"
        % (base, candidate, upstream, upstream, candidate),
        "# 3. evaluate on a throwaway branch (never mutate %s directly)"
        % branch,
        "git checkout -b %s" % eval_branch,
        "git checkout %s # exact upstream candidate, never a mutable pointer" % candidate,
    ]
    for delta_id in order:
        delta = delta_by_id(maintenance, delta_id)
        steps.append(
            "# 4. reapply %s (%s; %s)"
            % (delta_id, delta["patch_doc"], delta["validator"])
        )
        steps.append(
            "git cherry-pick --strategy=recursive -X patience <commits-for-%s>"
            % delta_id
            + " # stop on conflict; never -X theirs across the budget gate"
        )
    steps += [
        "# 5. rebuild the lightweight artifacts (upstream-supported Bun path)",
    ]
    steps += ["%s" % cmd for cmd in build_commands(maintenance)]
    steps += [
        "# 6. focused coding-agent correctness gate (offline)",
        "python3 -m pytest %s -q"
        % " ".join(maintenance["correctness"]["pytest_subset"]),
        "# 7. memory smoke benchmark (hermetic probe; resourced builders add --include-network)",
        "python3 automation/memory_benchmark.py --run-id fork-sync-%s"
        % _short(candidate),
        "# 8. obsolete-delta scan (drop patches upstream made redundant)",
        "python3 -c 'from automation import opencode_fork_maintenance as m; print(m.render_delta_inventory(m.load_maintenance()))'",
        "# 9. budget gate (blocks auto-merge over the hard limit)",
        "PEAK_BYTES=${PEAK_BYTES:?set measured peak bytes}; python3 -c 'import os; from automation import opencode_fork_maintenance as m; m.assert_merge_allowed(int(os.environ[\"PEAK_BYTES\"]), m.load_maintenance())'",
        "# 10. publish the concise delta inventory with the sync result",
        "git status --porcelain # reviewer reads deltas by purpose, not raw diff size",
    ]
    return steps


def sync_plan(
    maintenance: dict, upstream_candidate_sha: str, fork_sha: str | None = None
) -> dict:
    """Structured sync plan (stages + steps + gates, no wall clock)."""
    steps = sync_steps(maintenance, upstream_candidate_sha, fork_sha)
    return {
        "schema": SCHEMA,
        "fork_repo": maintenance["fork_repo"],
        "fork_branch": maintenance["fork_branch"],
        "upstream_repo": maintenance["upstream_repo"],
        "upstream_base_commit": maintenance["upstream_base_commit"],
        "upstream_candidate_sha": upstream_candidate_sha.strip(),
        "eval_branch": "fork-sync/%s" % _short(upstream_candidate_sha.strip()),
        "delta_order": list(maintenance.get("sync_order", [])),
        "stages": list(SYNC_STAGES),
        "steps": steps,
        "budget_gate": {
            "target_bytes": maintenance["budgets"]["target_bytes"],
            "hard_limit_bytes": maintenance["budgets"]["hard_limit_bytes"],
        },
    }


def build_commands(maintenance: dict) -> list[str]:
    """Rebuild commands for the lightweight artifacts after a refresh."""
    commands = list((maintenance.get("rebuild") or {}).get("commands", []))
    if not commands:
        raise ValueError("rebuild.commands must be non-empty")
    return commands


def correctness_plan(maintenance: dict) -> dict:
    """Focused correctness gate: pytest subset + representative task."""
    correctness = maintenance.get("correctness", {})
    subset = list(correctness.get("pytest_subset", []))
    if not subset:
        raise ValueError("correctness.pytest_subset must be non-empty")
    return {
        "pytest_subset": subset,
        "representative_task": correctness.get("representative_task", ""),
        "command": "python3 -m pytest %s -q" % " ".join(subset),
    }


def memory_smoke_plan(maintenance: dict, run_id: str) -> dict:
    """Memory smoke plan reusing the shared harness (no second stack)."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    return {
        "method": (maintenance.get("memory_smoke") or {}).get("method", ""),
        "hermetic_command": "python3 automation/memory_benchmark.py --run-id %s"
        % run_id.strip(),
        "constrained_trial": "docker run --rm --memory=512m --memory-swap=512m ...",
        "budget_gate": (maintenance.get("memory_smoke") or {}).get(
            "budget_gate", ""
        ),
    }


def evaluate_sync_budget(peak_bytes: int, maintenance: dict) -> dict:
    """Evaluate one post-sync peak against the acceptance budget.

    Mirrors the ``#81`` vocabulary (450 MiB target, 512 MiB limit). A peak
    above the hard limit blocks the merge; a peak above the target but
    within the limit is reviewable but never silently merged as progress.
    """
    if isinstance(peak_bytes, bool) or not isinstance(peak_bytes, int):
        raise ValueError("peak_bytes must be an int")
    if peak_bytes <= 0:
        raise ValueError("peak_bytes must be positive")
    budgets = maintenance.get("budgets", {})
    target = budgets.get("target_bytes")
    limit = budgets.get("hard_limit_bytes")
    if not isinstance(target, int) or not isinstance(limit, int):
        raise ValueError("budgets must carry target_bytes/hard_limit_bytes")
    return {
        "peak_bytes": peak_bytes,
        "peak_mib": round(peak_bytes / (1024 * 1024), 1),
        "target_bytes": target,
        "passes_target": peak_bytes <= target,
        "gap_to_target_bytes": peak_bytes - target,
        "hard_limit_bytes": limit,
        "passes_limit": peak_bytes <= limit,
        "gap_to_limit_bytes": peak_bytes - limit,
        "merge_blocked": peak_bytes > limit,
    }


def assert_merge_allowed(peak_bytes: int, maintenance: dict) -> dict:
    """Fail closed when a synced peak breaks the acceptance budget."""
    verdict = evaluate_sync_budget(peak_bytes, maintenance)
    if verdict["merge_blocked"]:
        raise ValueError(
            "refusing to auto-merge: post-sync peak %d bytes exceeds the "
            "hard limit %d bytes (over by %d bytes); file a Runtime Lab "
            "refresh task instead"
            % (
                verdict["peak_bytes"],
                verdict["hard_limit_bytes"],
                verdict["gap_to_limit_bytes"],
            )
        )
    return verdict


def _upstream_in_place_implements(delta: dict, contents: dict[str, str]) -> bool:
    """Return True when upstream file contents already carry the fork behavior.

    Path presence alone misses the case where upstream keeps the same
    files but independently implements the fork patch in place (e.g.
    D2 bounds in ``tool/task.ts``). Only the delta's own
    ``touches.modified`` files are inspected so the pre-existing
    ``Truncate`` service (``tool/truncate.ts``) or the already-bounded
    ``tool/shell.ts`` cannot cause a false positive.
    """
    if not contents:
        return False
    touches = delta.get("touches", {}) or {}
    modified = [p for p in touches.get("modified", []) if isinstance(p, str)]
    combined = "\n".join(contents.get(p, "") for p in modified if p in contents)
    if not combined:
        return False
    delta_id = delta.get("id", "")
    if delta_id == "D2-bounded-output":
        # Fork bound: task.ts routes renderOutput through Truncate.output()
        # with tail direction plus a file-backed retrieval hint, exactly as
        # shell.ts does. Require all three signals together.
        has_truncate = "Truncate" in combined
        has_tail = "tail" in combined
        has_retrieval = (
            "outputPath" in combined or "Full output saved to" in combined
        )
        return bool(has_truncate and has_tail and has_retrieval)
    return False


def detect_obsolete_deltas(
    upstream_files: list[str] | dict[str, str], maintenance: dict
) -> list[dict[str, str]]:
    """Detect deltas upstream made redundant; fail closed on misuse.

    ``upstream_files`` is either a list of upstream repo-relative paths
    or a mapping of path to file content from the candidate revision.
    Each delta is reported as ``active`` (all probes still present and
    no in-place upstream implementation detected), ``needs-review``
    (some probes moved/renamed), or ``obsolete`` (no probe present, or
    upstream already ships the fork behavior: it carries the fork-added
    files or its in-place file contents implement the patch, so the
    patch can be dropped).
    """
    if isinstance(upstream_files, dict):
        paths = set(upstream_files.keys())
        contents = dict(upstream_files)
        for key, value in contents.items():
            if not isinstance(value, str):
                raise ValueError("upstream file contents must be strings")
    elif isinstance(upstream_files, list):
        paths = set(upstream_files)
        contents = {}
    else:
        raise ValueError("upstream_files must be a path list or path->content map")
    if any(not isinstance(p, str) or not p for p in paths):
        raise ValueError("upstream file paths must be non-empty strings")
    results: list[dict[str, str]] = []
    for delta in maintenance.get("deltas", []):
        probes = list(delta.get("upstream_probes", []))
        present = [p for p in probes if p in paths]
        touches = delta.get("touches", {}) or {}
        added = [p for p in touches.get("added", []) if isinstance(p, str)]
        added_overlap = sorted(p for p in added if p in paths)
        if added_overlap:
            status = "obsolete"
            reason = (
                "upstream already ships %d fork-added file(s) (%s) so %s "
                "adds no delta and the patch can be dropped"
                % (len(added_overlap), ", ".join(added_overlap), delta["id"])
            )
        elif len(present) == len(probes) and _upstream_in_place_implements(
            delta, contents
        ):
            status = "obsolete"
            reason = (
                "all %d upstream probes still present but upstream file "
                "contents already implement %s in place so the patch can "
                "be dropped" % (len(probes), delta["id"])
            )
        elif len(present) == len(probes):
            status = "active"
            reason = "all %d upstream probes still present" % len(probes)
        elif not present:
            status = "obsolete"
            reason = (
                "none of the %d upstream probes present; upstream removed or "
                "relocated the forked subsystem (%s) so the patch can be dropped"
                % (len(probes), delta["id"])
            )
        else:
            status = "needs-review"
            reason = "only %d/%d upstream probes present; subsystem moved: missing=%s" % (
                len(present),
                len(probes),
                sorted(set(probes) - set(present)),
            )
        results.append(
            {
                "delta_id": delta["id"],
                "status": status,
                "reason": reason,
                "obsolete_when": delta.get("obsolete_when", ""),
            }
        )
    return results


def render_delta_inventory(maintenance: dict) -> str:
    """Render the concise patch/delta inventory (by purpose, not diff size)."""
    lines = [
        "# OpenCode fork delta inventory (issue #87)",
        "",
        "Fork `%s` `%s` over upstream `%s` base `%s` (%s, pin `%s`)."
        % (
            maintenance.get("fork_repo"),
            maintenance.get("fork_branch"),
            maintenance.get("upstream_repo"),
            maintenance.get("upstream_base_commit"),
            maintenance.get("upstream_tag"),
            maintenance.get("pinned_opencode_version"),
        ),
        "",
        "| Delta | Purpose | Fork touch points | Upstream probes | Status |",
        "|---|---|---|---|---|",
    ]
    for delta in maintenance.get("deltas", []):
        touches = delta.get("touches", {})
        touch_points = len(touches.get("added", [])) + len(
            touches.get("modified", [])
        )
        lines.append(
            "| %s | %s | %d file(s): %s | %d probe(s) | %s |"
            % (
                delta.get("id"),
                delta.get("purpose"),
                touch_points,
                delta.get("patch_doc"),
                len(delta.get("upstream_probes", [])),
                delta.get("status"),
            )
        )
    lines.append("")
    lines.append(
        "Sync order: %s." % ", ".join(maintenance.get("sync_order", []))
    )
    lines.append(
        "Full build stays usable; no custom provider rewrite; "
        "tracking stays in kodmial/runtime-lab."
    )
    return "\n".join(lines) + "\n"


def render_refresh_failure_task(
    maintenance: dict,
    stage: str,
    fork_sha: str,
    upstream_candidate_sha: str,
    evidence: str,
) -> dict[str, object]:
    """Render an actionable Runtime Lab task for a refresh failure.

    Every failure (rebase conflict, rebuild failure, correctness
    failure, smoke over budget, obsolete-scan ambiguity) produces this
    task instead of silently leaving the deployed fork stale.
    """
    if stage not in SYNC_STAGES:
        raise ValueError("unknown sync stage: %r" % stage)
    for name, value in (
        ("fork_sha", fork_sha),
        ("upstream_candidate_sha", upstream_candidate_sha),
    ):
        if not isinstance(value, str) or SHA_RE.match(value.strip()) is None:
            raise ValueError("%s must be a 40-char SHA" % name)
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence must be a non-empty string")
    fork_short = _short(fork_sha.strip())
    upstream_short = _short(upstream_candidate_sha.strip())
    title = "P1: OpenCode fork refresh blocked at %s (fork %s vs upstream %s)" % (
        stage,
        fork_short,
        upstream_short,
    )
    body_lines = [
        "## Priority",
        "",
        "P1 — fork maintenance follow-up for kodmial/runtime-lab#87.",
        "",
        "## Context",
        "",
        "- Fork `%s` `%s` at `%s`."
        % (
            maintenance.get("fork_repo"),
            maintenance.get("fork_branch"),
            fork_sha.strip(),
        ),
        "- Upstream `%s` candidate `%s` (recorded base `%s`)."
        % (
            maintenance.get("upstream_repo"),
            upstream_candidate_sha.strip(),
            maintenance.get("upstream_base_commit"),
        ),
        "- Failed stage: `%s` (stages: %s)." % (stage, ", ".join(SYNC_STAGES)),
        "- Sync order: %s." % ", ".join(maintenance.get("sync_order", [])),
        "",
        "## Evidence",
        "",
        evidence.strip(),
        "",
        "## Next step",
        "",
        "Re-run the deterministic procedure from "
        "`automation/opencode_fork_maintenance.py:sync_steps` for this "
        "candidate on a throwaway `fork-sync/%s` branch: resolve the `%s` "
        "failure, rebuild the lightweight artifacts, re-run the focused "
        "correctness gate plus the memory smoke benchmark, re-check the "
        "budget gate, and update the delta inventory. Do not mutate fork "
        "`%s` directly and do not auto-merge over the hard limit "
        "(%d bytes)."
        % (
            upstream_short,
            stage,
            maintenance.get("fork_branch"),
            maintenance["budgets"]["hard_limit_bytes"],
        ),
        "",
        "## Acceptance",
        "",
        "- The refresh either lands with all gates green or this task "
        "stays open; the deployed fork is never silently left stale.",
        "",
        "<!-- runtime-lab-fork-refresh-failure -->",
    ]
    return {
        "title": title,
        "body": "\n".join(body_lines) + "\n",
        "labels": list((maintenance.get("refresh_failure") or {}).get("labels", [])),
    }
