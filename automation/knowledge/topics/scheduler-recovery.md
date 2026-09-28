# Scheduler and recovery — current knowledge

## Confirmed
- Runtime Lab is event-driven first; cron is only a safety net.
- Scheduler WIP is 4 and dependency blocking is authoritative.
- Progression after automation-owned merges uses explicit workflow dispatch where needed rather than relying on recursive `GITHUB_TOKEN` event behavior.
- Render failures can create bounded repair issues; after repair merge the source issue is unpaused, retry budget is reset, and scheduler is explicitly dispatched.
- A recovery run must fix reusable implementation and add regression coverage, not blindly repeat the same failed experiment.

## Evidence
- Issue #9 -> repair issue #25 -> PR #26 -> source retry exercised the recovery loop.
