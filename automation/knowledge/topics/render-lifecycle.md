# Render lifecycle — current knowledge

## Confirmed
- Ephemeral workers are named `runtime-lab-issue<ISSUE>-<RUN_OR_ATTEMPT>`. `runtime-lab-controller` is persistent and must never be treated as an ephemeral worker.
- One issue attempt creates at most one worker. Model fallback reuses the same worker.
- Once a service ID exists, cleanup is mandatory on every terminal path. Deletion is primary; suspension is only an emergency fallback while deletion retries continue.
- Success requires authoritative absence verification (404/410 or equivalent). Unverified cleanup blocks GitHub write-back/materialization.
- Render workspace/list-owner responses expose the workspace identifier under `owner.id`; top-level `id` must not be assumed. Evidence: `../experiments/issue-9-run-36379129913.md`.
- Runner jobs may legitimately take up to 45 minutes; the controller polling envelope must exceed that bound plus margin. A 20-minute envelope caused a false timeout. Evidence: `../experiments/issue-9-run-36399649036.md`.

- Runner jobs live in worker process memory, so a worker restart turns a submitted job id into a permanent unknown-job 404. The controller poll loop must classify each poll from (HTTP status + parsed status): only queued/running on HTTP 2xx is pending; persistent 404/410 fails fast as job loss; transport failures retry within budget with periodic /health re-probes and diagnostic errors. Empty poll responses must never be treated as proof the job is still working. Evidence: `../experiments/issue-31-run-36408413138.md`.

- Render may restart a Free web service at any time (vendor contract: `https://render.com/docs/free`), so transient restarts must not fail the whole attempt: on proven job loss (persistent unknown-job with a healthy runner) the controller resubmits the identical payload up to three times on the SAME worker (never a second service) within the unchanged poll budget; a fourth consecutive loss still fails fast. Restart evidence prefers the runner process instance id, then wall-clock-vs-uptime drift; uptime order alone is never proof of identity. Evidence: `../experiments/issue-33-run-36409578168.md`, `../experiments/issue-41-run-36416327622.md`, `../experiments/issue-47-run-36419944182.md`, `../experiments/issue-53-run-36424221297.md`.

- Every runner process owns a unique `instance_id` (uuid at manager startup) exposed in `GET /health` alongside `pid`, `started_at`, `uptime_seconds`, and lightweight resource signals, and stamped into each submitted job's metadata/status. A changed instance id across a job loss proves a restart/replacement directly, even when the new uptime is numerically greater than the old snapshot (run `36410676408` advanced minutes of wall-clock with only `25.9 -> 48.9 -> 60.3` uptime deltas, which the old `current < prior` check misread as "same worker process lifetime"). OpenCode CLI provisioning is serialized behind an install lock so concurrent jobs never run concurrent heavyweight installers on the small free worker. Evidence: `../experiments/issue-41-run-36416327622.md`.

- A stale-worker watchdog now exists as a second cleanup line for orphaned automation-owned workers. It fail-closes on ambiguous names/timestamps, excludes the persistent controller, supports active-lease protection and dry-run planning, and never provisions a replacement worker. Evidence: `../experiments/issue-27-run-36405661952.md`.
- An offline lifecycle fault-injection matrix now covers failures across provisioning, deploy, health, job execution, fallback, cleanup and GitHub write-back. Evidence: `../experiments/issue-28-run-36405666089.md`.
- OpenCode CLI provisioning is pinned to an explicit release (`--version <pinned>`, `$OPENCODE_VERSION` override) in both `automation/install-opencode.sh` and lazy runtime provisioning: the unpinned installer depends on an unauthenticated `api.github.com` latest-release lookup that fails closed with "Failed to fetch version information" under shared-egress rate limiting. Lazy provisioning retries with bounded backoff capped by the job budget. Evidence: `../experiments/issue-50-run-36421399199.md`.

## Do not repeat
- Do not parse the first workspace as `.[0].id`.
- Do not use an outer poll timeout shorter than the runner timeout.
- Do not treat an empty/unparsable job poll as queued/running.
- Do not create a second service for model fallback or polling retry (same-worker resubmission reuses the existing worker, up to three resubmissions).
- Do not infer worker process identity from `current_uptime < prior_uptime` alone; a replacement process can report a larger uptime than the old snapshot. Compare instance ids first, then wall-clock elapsed vs uptime delta.
- Do not provision workers with unpinned `curl -fsSL https://opencode.ai/install | bash`; always pin `--version` (or `$OPENCODE_VERSION`).

## Open
- Hard process termination can bypass in-process cleanup; stale-worker reconciliation is being developed separately.
