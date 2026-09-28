# Scheduler and recovery — current knowledge

## Confirmed
- Runtime Lab is event-driven first; cron is only a safety net.
- Scheduler WIP is 4 and dependency blocking is authoritative.
- Progression after automation-owned merges uses explicit workflow dispatch where needed rather than relying on recursive `GITHUB_TOKEN` event behavior.
- Render failures can create bounded repair issues; after repair merge the source issue is unpaused, retry budget is reset, and scheduler is explicitly dispatched.
- A recovery run must fix reusable implementation and add regression coverage, not blindly repeat the same failed experiment.

## Evidence
- Issue #9 -> repair issue #25 -> PR #26 -> source retry exercised the recovery loop.

## Enforced OpenCode optimization DAG (issue #88)

- Only native `blockedBy` and the DoR `#N is completed` phrasing gate
  dispatch; "Blocked by #N" prose is inert and previously let #77 reserve
  before #76 completed.
- Enforced graph (`automation/opencode_dependency_dag.py`, mirrored live
  as native edges): #77<-[76,85], #78<-[77], #79<-[77,78], #80<-[79],
  #86<-[79,80], #81<-[58,86], #87<-[81]; #89 stays paused (manual enable
  only if #81 misses its target); #76/#85 parallel, #88 independent.
- Controller merges enforced open blockers before the reservation gate
  and fails closed on unknown prerequisite state, so a blocked issue
  never receives `automation:in-progress`; gating is per-issue, never a
  global mutex. Pause labels are removed only when the enforced set is
  satisfied (#89 never auto-unpauses). Evidence:
  `../experiments/issue-88-run-36453015465.md`.
