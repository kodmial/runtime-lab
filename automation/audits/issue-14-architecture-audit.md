# Issue #14 — Independent architecture and execution audit

Date: 2026-09-28. Branch reviewed: `opencode/issue14-36376219303` (detached from
`origin/main` at `b3bb16e`). No `.github/workflows/**` files were modified by
this audit; workflow envelopes are quoted read-only below.

Target architecture (from #14): GitHub event/webhook → persistent Render
controller → ephemeral Render worker → OpenCode → worker deletion → controller
writes branch/commit/PR to GitHub. Actions remain only as a temporary harness
and ordinary CI.

## What was checked

- Repository tree: only `.github/workflows/` (8 files), `automation/`
  (`control-plane.json` before this PR), `.github/pull_request_template.md`.
  No runner, controller, or harness implementation exists on main yet.
- Workflows read in full: `issue-scheduler.yml`, `opencode.yml`,
  `render-executor.yml`, `ci.yml`, `opencode-repair.yml`, `auto-merge.yml`,
  `pr-agent.yml`, `restart-documented-publishing-temp.yml`.
- `automation/control-plane.json` (`{"execution_backend": "actions"}`).
- Live issues #1–#14 via `gh issue view` and native dependencies via
  `GET /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`.
- Labels via `gh label list` (`priority:p0/p1/p2`, `automation:in-progress`,
  `automation:paused`, `execution:render-smoke`, `execution:render-e2e`).
- Git history (`git log --oneline`), confirming the sequence from global
  Render serialization (`82fd9f6`) to restored per-issue concurrency
  (`73971b2`), region/model pinning (`dc8e81f`), and documented-safe
  publishing (`b3bb16e`, `61a4856`).

## What is correct

- **DAG has no cycles and #14 is independent.** Native `blocked_by`:
  #1=[], #2=[1], #3=[2], #4=[1], #5=[3,4], #9=[3,4], #6=[5,9], #10=[3,4],
  #11=[10,5], #12=[11,6], #13=[12], #7=[13], #14=[]. This matches each
  issue's DoR text. #4 is blocked only by #1 so it can proceed in parallel
  with #2/#3; #10 is blocked only by #3/#4 so it can proceed in parallel
  with #5/#9/#6 as its body allows. No accidental serialization found.
- **Parallelism is real.** `issue-scheduler.yml:27` defaults
  `AUTOMATION_WIP_LIMIT` to `4`; `opencode.yml:35-37` serializes per
  `inputs.issue_number || inputs.pr_number ...` (same issue serialized,
  different issues parallel); `render-executor.yml:22-26` uses
  `group: runtime-lab-render-${{ inputs.issue_number }}` (per-issue, no
  global one-job mutex). Scheduler lease/active-PR tracking
  (`issue-scheduler.yml:165-240`) keeps the same issue from double-dispatch
  while the lease holds.
- **Retry bounds exist on the Actions side.** Scheduler
  `AUTOMATION_MAX_DISPATCH_ATTEMPTS` defaults to `4`
  (`issue-scheduler.yml:29`), with pause after 4 attempts; `opencode.yml`
  releases the reservation and re-triggers the scheduler on failure.
  OpenCode installer retries 3x with backoff (`opencode.yml:70-81`).
- **Cutover switch design is sound.** Scheduler reads
  `automation/control-plane.json` and returns early with a notice when
  `execution_backend == "render-controller"` (`issue-scheduler.yml:62-90`),
  so #12 can cut over without touching workflow files. Current value
  `actions` is correct for the temporary phase.
- **Region/model defaults match policy.** `render-executor.yml:37-38`
  default to `RENDER_REGION=oregon` and
  `OPENCODE_MODEL=opencode/muse-spark-1.3-contributor-free`; `opencode.yml:93`
  uses the same model default. Oregon is in the Muse-allowed set
  (oregon/ohio/virginia/singapore); frankfurt appears nowhere as a default.
  The public-URL/no-provider-connection strategy is specified in #1 and #10
  bodies.
- **Scheduler preserves NanoDictate semantics** (priority ordering, PR
  association via `opencode/issue<N>-` prefix, closed-without-merge pause,
  explicit `ci.yml` dispatch), per `issue-scheduler.yml`, `auto-merge.yml`,
  and `opencode-repair.yml`.

## Concrete defects found

1. **Missing #4 harness entrypoints (blocking for Render path).**
   `render-executor.yml:62` requires `automation/render-job.sh` and
   `:80-88` requires `automation/render-cleanup.sh`, but neither file exists
   on main (only `automation/control-plane.json`). Every render-executor run
   therefore exits 2 before any Render call. Owned by existing issue #4;
   no duplicate created.
2. **No controller / App auth / cutover / proof implementation yet.**
   Expected at this stage: #10 (persistent controller + webhook ingress),
   #11 (GitHub App auth + write-back), #12 (cutover + no-OpenCode-run
   observability check), #13 (no-Actions proof), #7 (hardening) are all still
   open. The `render-controller` scheduler branch is dead code until #10
   lands. All already in backlog; no duplicate created.
3. **Unproven guardrails live only in issue text.** Free-tier abort,
   20/hour Render rate-limit/backoff/429 handling, DELETE-primary with
   suspend-fallback and bounded delete retries, single-creation-per-attempt,
   no-second-worker-on-model-fallback, and delivery-ID idempotency are fully
   specified in #1/#2/#4/#7/#9/#10 but have no executable code to review.
   Owned by those issues; no duplicate created.
4. **Space Bunny fallback has no executable path.** The model pair
   (Muse preferred, `opencode/space-bunny-free` fallback) is specified in
   #1/#2/#3/#9/#10 and defaulted in workflows, but no runner/harness code
   performs the in-worker fallback. Owned by #2/#3/#4.
5. **Temporary publishing-restart workflow still present.**
   `.github/workflows/restart-documented-publishing-temp.yml` force-cancels
   runs and re-dispatches issues 1 and 14 through `opencode.yml` (the Actions
   path). It only triggers on pushes to itself, so blast radius is small, but
   it must be removed before the #13 no-Actions proof; otherwise a stale
   trigger could create an OpenCode Actions run associated with proof work.
   Recommend removal as part of #12/#13; not removed here (workflow files are
   frozen for this issue).

## Fixes made in this PR

- Added `automation/runtime_lab_audit.py`: stdlib-only validators encoding
  the audit invariants (allowed backends `actions`/`render-controller`,
  Muse-allowed regions, preferred/fallback models, per-issue concurrency
  detectors, WIP/attempts extractors, harness-presence probe). No external
  calls, no side effects.
- Added `automation/tests/test_runtime_lab_audit.py` (14 tests): unit tests
  for region/model/backend/concurrency helpers plus read-only live-repo
  checks (control-plane valid, scheduler WIP=4 and attempts=4, per-issue
  Render/OpenCode concurrency, no global Render mutex, harness references
  present). The known #4 gap is recorded as an explicit passing assertion
  (`render-job.sh`/`render-cleanup.sh` absent) rather than an obscure CI
  failure.
- No workflow files modified. No existing roadmap issues closed, reopened,
  or reprioritized.

## Follow-up issues created

None. Every material gap found is already represented by a non-duplicate
backlog issue (#1–#13, see defect list above), so creating more issues would
violate the no-duplicates rule.

## Recommended dependency changes (intentionally NOT applied)

No native dependency-graph change is recommended. The live `blocked_by` graph
already matches the DoR text for every issue, is acyclic, preserves the
intended parallel tracks (#4 with #2/#3; #10 with #5/#9/#6; #14 standalone),
and needs no edge added or removed. Per the issue constraints, nothing was
rewritten automatically; if reviewers later want #10 to also block on #5's
reusable write-back (currently only #11 does), that should be proposed as a
separate reviewed change.

## Validation

- `python3 -m pytest automation/tests/test_runtime_lab_audit.py -q`: 14 passed.
- `git status --porcelain -- .github/workflows`: clean (no workflow changes).
- Definition of Done: roadmap reviewed against the no-Actions architecture;
  safe defect (missing machine-checkable invariants) fixed with tests;
  missing work mapped to existing non-duplicate issues; evidence recorded
  here; main DAG unblocked (#14 has no blockers and adds no blockers).
