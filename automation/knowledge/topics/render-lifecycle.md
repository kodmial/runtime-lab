# Render lifecycle — current knowledge

## Confirmed
- Ephemeral workers are named `runtime-lab-issue<ISSUE>-<RUN_OR_ATTEMPT>`. `runtime-lab-controller` is persistent and must never be treated as an ephemeral worker.
- One issue attempt creates at most one worker. Model fallback reuses the same worker.
- Once a service ID exists, cleanup is mandatory on every terminal path. Deletion is primary; suspension is only an emergency fallback while deletion retries continue.
- Success requires authoritative absence verification (404/410 or equivalent). Unverified cleanup blocks GitHub write-back/materialization.
- Render workspace/list-owner responses expose the workspace identifier under `owner.id`; top-level `id` must not be assumed. Evidence: `../experiments/issue-9-run-36379129913.md`.
- Runner jobs may legitimately take up to 45 minutes; the controller polling envelope must exceed that bound plus margin. A 20-minute envelope caused a false timeout. Evidence: `../experiments/issue-9-run-36399649036.md`.

- A stale-worker watchdog now exists as a second cleanup line for orphaned automation-owned workers. It fail-closes on ambiguous names/timestamps, excludes the persistent controller, supports active-lease protection and dry-run planning, and never provisions a replacement worker. Evidence: `../experiments/issue-27-run-36405661952.md`.
- An offline lifecycle fault-injection matrix now covers failures across provisioning, deploy, health, job execution, fallback, cleanup and GitHub write-back. Evidence: `../experiments/issue-28-run-36405666089.md`.

## Do not repeat
- Do not parse the first workspace as `.[0].id`.
- Do not use an outer poll timeout shorter than the runner timeout.
- Do not create a second service for model fallback or polling retry.

## Open
- Hard process termination can bypass in-process cleanup; stale-worker reconciliation is being developed separately.
