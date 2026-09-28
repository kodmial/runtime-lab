# Architecture and execution audit — issue #14

Date: 2026-09-28 (UTC).
Scope: review `main` against the target architecture
`GitHub event/webhook → persistent Render controller → ephemeral Render
worker → OpenCode → worker deletion → controller writes branch/commit/PR`,
with GitHub Actions allowed only as temporary harness plus ordinary CI.
This issue has no blockers and was executed independently of #1–#13.

## What was checked (with evidence)

- Repository files on `main`: `automation/control-plane.json`
  (`{"execution_backend": "actions"}` — correct pre-cutover value),
  `.github/workflows/issue-scheduler.yml`,
  `.github/workflows/opencode.yml`, `.github/workflows/render-executor.yml`,
  `.github/workflows/ci.yml`, `.github/workflows/auto-merge.yml`,
  `.github/workflows/opencode-repair.yml`, `.github/workflows/pr-agent.yml`.
- Live native dependency graph via GraphQL `blockedBy` (12 Sep 2026 state):
  `1:[]`, `2:[1]`, `3:[2]`, `4:[1]`, `5:[4,3]`, `9:[4,3]`, `10:[4,3]`,
  `11:[5,10]`, `6:[9,5]`, `12:[6,11]`, `13:[12]`, `7:[13]`, `14:[]`.
- Labels via `gh label list` / `gh issue list`: `priority:p0/p1/p2`,
  `automation:in-progress`, `automation:paused`,
  `execution:render-smoke`, `execution:render-e2e` all exist.
- Bodies of issues #1–#13 against the 15 review bullets in #14.
- Scheduler dispatch logic, concurrency groups, retry bounds, branch
  convention, and the `render-controller` cutover switch. Workflow files
  were read only; none were modified per #14 required action 7.

## What is correct

- DAG has no cycles. Chain `1→2→3→{5,9,10}→11→12→13→7` plus `1→4`
  rejoining at `5/9/10` is acyclic; `14` is unblocked as required.
- Independent work can run in parallel: #2 and #4 both wait only on #1;
  #5, #9, #10 all wait on {#3,#4} and can proceed concurrently once those
  land. The scheduler additionally falls back to DoR text (`#N is
  completed`, `issue-scheduler.yml:294-318`) when native dependencies are
  absent, so parallel WIP cannot start dependent work early.
- Up to 4 different issues can execute: `WIP_LIMIT` defaults to `4`
  (`issue-scheduler.yml:27`), `MAX_DISPATCH_ATTEMPTS` defaults to `4`
  (`issue-scheduler.yml:29`), matching the #1 low-burn policy.
- Same issue cannot execute twice concurrently: `opencode.yml:35-37`
  groups by `opencode-<issue|pr|run>`, `render-executor.yml:22-26` groups
  by `runtime-lab-render-<issue_number>`, both with
  `cancel-in-progress: false`; the scheduler lease/`active` set
  (`issue-scheduler.yml:165-244`) prevents re-dispatch while active.
- No global one-job mutex in enforcement: both execution workflows use
  per-issue concurrency groups, so different issues may run concurrently.
- Cutover path exists and matches #12: the scheduler reads
  `automation/control-plane.json` from `main`, accepts only
  `actions|render-controller` (`issue-scheduler.yml:78-90`), and returns
  early without dispatching `opencode.yml` when `render-controller` is set.
- Model defaults are correct: `opencode/muse-spark-1.3-contributor-free`
  is the default in `opencode.yml:93,196,258` and
  `render-executor.yml:38`; `RENDER_REGION` defaults to `oregon`
  (`render-executor.yml:37`). Issues #1–#4/#9–#13 consistently pin the
  Muse Spark preferred / Space Bunny fallback policy and the allowed
  regions `oregon|ohio|virginia|singapore` (`frankfurt` forbidden).
- Cleanup is mandatory in the envelope: `render-executor.yml:68-90` runs
  deletion `if: always()`, and `Finalize execution` (`:92-139`) fails and
  pauses the issue unless both execute and cleanup succeed; `e2e` mode
  additionally requires an `opencode/issue<N>-...` PR.
- Branch convention `opencode/issue<NUMBER>-...` is enforced consistently
  by `opencode.yml:111`, `render-executor.yml:122-124`, and
  `auto-merge.yml:188-194`.
- Public-URL source strategy (no Render↔GitHub provider connection for
  the public phase, `autoDeploy=no`) is stated identically in #1, #4, #9,
  #10. No repository code contradicts it.

## Concrete defects found

1. Stale global-mutex comment (not fixed here — workflow file protected).
   `render-executor.yml:22-24` comments that "only one automation-owned
   ephemeral Render worker may exist at a time", but the concurrency group
   directly below is per-issue
   (`runtime-lab-render-${{ inputs.issue_number }}`). History shows
   `82fd9f6` serialized provisioning globally and `73971b2` restored
   per-issue concurrency without updating the comment. Enforcement is
   correct; the comment contradicts the target architecture and should be
   corrected by the workflow owners.
2. Unguarded cutover contract (fixed in this PR — see below). Only the
   scheduler validates `automation/control-plane.json` at runtime; a typo
   such as `render_controller` would fail after merge. There was no local
   test for the `actions|render-controller` contract backing #12/#13.
3. Missing `automation/render-job.sh` / `automation/render-cleanup.sh`.
   `render-executor.yml:62-88` references both, but `automation/` contains
   only `control-plane.json`. Expected at this roadmap stage: both scripts
   are explicitly owned by #4 (harness) and gated live by #9. Not a new
   defect; no duplicate issue created. Until they land, single-creation,
   DELETE-primary/suspend-fallback, free-tier, region-reject, and
   bounded-retry behavior are specified correctly in #1/#4/#9 but not yet
   verifiable in code.
4. Label color/description drift (cosmetic, no action). Live
   `execution:render-smoke` / `execution:render-e2e` are `#ededed` with
   empty descriptions, while `issue-scheduler.yml:92-100` tries to create
   them as `5319E7`/`0052CC` with descriptions and silently keeps the old
   colors on HTTP 422. Harmless for dispatch (matching is by name).
5. Minor doc/envelope drift (flag only). #4 scope says the workflow
   provides `OPENCODE_API_KEY`, but `render-executor.yml:32-38` exposes
   only `RENDER_API_KEY` (plus `OPENCODE_MODEL`/`RENDER_REGION` vars). The
   anonymous/free-model policy itself is consistent everywhere; the #4
   implementer and workflow owners should align the wording without
   changing the envelope here.

## Fixes made in this PR

- Added `automation/tests/test_control_plane.py`: validates that
  `automation/control-plane.json` exists, parses as a JSON object,
  contains only `execution_backend`, and that the value is in
  `{actions, render-controller}` — the exact contract enforced at runtime
  by `issue-scheduler.yml:62-90`. This fails fast locally and in ordinary
  CI (`ci.yml` auto-runs pytest when Python tests exist) instead of after
  merge. No workflow files were touched.

## Follow-up issues created

- None. Every substantive gap found (controller in #10, App auth in #11,
  cutover observability in #12, no-Actions proof in #13, lifecycle
  hardening in #7, harness scripts in #4, smoke/e2e gates in #9/#6) is
  already represented in the backlog. Creating more would violate the
  no-duplicates rule in #14.

## Recommended dependency changes (intentionally NOT applied)

- Consider dropping #3 from #10's native `blockedBy` (currently `[4,3]`).
  The controller never executes OpenCode itself (#10 DoD: "Do not execute
  OpenCode inside the controller process"); it only reuses the Render
  lifecycle client from #4. #10 could therefore proceed in parallel with
  #3 once #4 lands. Left unchanged because #10's own DoR explicitly says
  "#3 and #4 are completed", and rewriting the native graph automatically
  is forbidden by #14 required action 6. Needs human review.
- No other dependency change is recommended: #5/#9 both on {#3,#4}, #11
  on {5,10}, #6 on {9,5}, #12 on {6,11}, #13 on {12}, #7 on {13} all match
  their DoR text; no cycles, no accidental serialization, and #14
  correctly remains unblocked.
