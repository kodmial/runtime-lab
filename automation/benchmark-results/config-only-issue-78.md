# Config-only low-memory profile benchmark — issue #78

Rerunnable: `python3 automation/memory_benchmark.py --include-network` for the
full-binary signals; per-switch/cumulative arms via
`automation/opencode_lowmem_profile.py:measure_probe` (hermetic bootstrap
probe) with the profile from `automation/opencode_lowmem_profile.py`.

Machine-readable artifact:
`automation/benchmark-results/config-only-issue-78-run-36455233181.json`
(run 36455233181, base `e6c8918538c4cffd7d97aef68982e68ff8da27ae`,
pinned OpenCode 1.18.33, fork `kodmial/opencode` base `9000e7f`).

Profile artifact: `automation/opencode_lowmem_profile.py`
(schema `runtime-lab-opencode-lowmem-profile/v1`) plus
`write_profile_files` output (`opencode.json` + `opencode.env`) — the exact
files a Render worker consumes, ready for the #81 matrix.

Constraint: Render Free Web Service, 0.1 CPU, 512 MB RAM, no swap assumed
(same envelope as issue #52).

## Method (same workload and baseline as the parallel variants)

- Shared baseline: issue-52 real-agent host peak **614,632 kB (~600 MiB)**
  on the same pinned binary — the number every variant (#78 config-only,
  #79 source-stripped, #80 bounded, direct-headless) must beat.
- Hermetic bootstrap probe (this run, no credentials/network): `opencode run
  --auto --model nope/nonexistent "say hi"` fails fast (exit 1) on provider
  resolution AFTER paying `InstanceBootstrap` (config + plugin + lsp /
  shareNext / format / vcs / snapshot / project init) — exactly the path the
  config-only switches act on. Full process-tree polling at 50 ms
  (`memory_benchmark.run_with_peak`), N=3 per arm, scrubbed env + isolated
  HOME per arm. Peak reported as max-of-3 (conservative: peaks OOM-kill).
- Correctness gate: deterministic offline representative task (inspect ->
  search -> small edit -> focused test, #81 q1 shape) under the profile, plus
  identical failure-mode comparison (bare vs config-file vs cumulative reach
  the same in-process-server bootstrap error, exit 1).
- Live Render deltas (real LLM stream, GC pressure, downloader/upload
  avoidance) are collected by the #81 matrix, not inferred here.

## A/B table (hermetic bootstrap probe, kB, max of 3)

| Arm | Peak tree (kB) | Peak (MiB) | Delta vs shared baseline (614632 kB) | Delta vs bare arm | Wall (s) | Exit | Provenance |
|---|---|---|---|---|---|---|---|
| bare-baseline | 514620 | 502.6 | -100012 kB (-97.7 MiB) | n/a (baseline) | 1.59 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-bun_options | 515596 | 503.5 | -99036 kB (-96.7 MiB) | +976 kB (+1.0 MiB) | 1.60 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_pure | 517084 | 505.0 | -97548 kB (-95.3 MiB) | +2464 kB (+2.4 MiB) | 1.57 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_disable_default_plugins | 518528 | 506.4 | -96104 kB (-93.9 MiB) | +3908 kB (+3.8 MiB) | 1.57 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_disable_external_skills | 578308 | 564.8 | -36324 kB (-35.5 MiB) | +63688 kB (+62.2 MiB) | 1.59 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_disable_lsp_download | 512728 | 500.7 | -101904 kB (-99.5 MiB) | -1892 kB (-1.8 MiB) | 1.57 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_disable_share | 516096 | 504.0 | -98536 kB (-96.2 MiB) | +1476 kB (+1.4 MiB) | 1.58 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_disable_autoupdate | 517632 | 505.5 | -97000 kB (-94.7 MiB) | +3012 kB (+2.9 MiB) | 1.57 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-opencode_disable_models_fetch | 511884 | 499.9 | -102748 kB (-100.3 MiB) | -2736 kB (-2.7 MiB) | 1.59 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-config-file | 526508 | 514.2 | -88124 kB (-86.1 MiB) | +11888 kB (+11.6 MiB) | 1.59 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| single-pure-flag | 517136 | 505.0 | -97496 kB (-95.2 MiB) | +2516 kB (+2.5 MiB) | 1.57 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |
| cumulative | 521056 | 508.8 | -93576 kB (-91.4 MiB) | +6436 kB (+6.3 MiB) | 1.61 | 1 | hermetic bootstrap probe N=3 (max of 3), pinned 1.18.33 |

Leave-one-out band (cumulative minus one switch, max of 3, full data in
JSON): 519,448–553,268 kB, walls 1.58–1.66 s — the same band. No single
switch dominates; removing any one switch changes nothing measurable.

## Reading the table honestly

- The probe floor (~503–521 kB min-to-max across arms, family 1) sits
  ~90–110 MB under the shared real-agent baseline because the probe never
  runs an LLM stream; it measures startup + bootstrap only.
- **No config-only switch moves the startup peak outside measurement noise**
  (±~15 MB at 50 ms polling; one 578 MB single-poll outlier in the
  external-skills arm whose min, 508 MB, sits inside the band). Startup time
  is likewise flat (1.56–1.66 s every arm).
- Interpretation (consistent with #56/#77): the disabled subsystems are
  already empty/lazy on this path (zero MCP/LSP servers by default), while
  the floor is the Bun runtime + static module-graph parse — exactly what
  #77 says configuration cannot shrink. `BUN_OPTIONS=--smol` shows no
  startup effect here either (its #56 ~40 MB win came from GC pressure under
  a sustained real-agent stream, which this probe never builds).
- Discarded pass-1 artifact: the first cumulative measurement (~219 MB)
  measured the `OPENCODE_DB` fast-fail (missing parent dir), not bootstrap.
  Every arm above creates the DB dir first, as the runner now does; all arms
  reach the identical bootstrap failure shape (exit 1, same UnknownError).

## Accepted / rejected settings (verified against fork/source + live binary)

Accepted (14, all in the qualified profile): `BUN_OPTIONS=--smol`,
`--pure`/`OPENCODE_PURE=1`, `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`,
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, `mcp:{}`, `lsp:{}` +
`OPENCODE_DISABLE_LSP_DOWNLOAD=1`, `formatter:false`, `share:disabled` +
`OPENCODE_DISABLE_SHARE=1`, `autoupdate:false` +
`OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_MODELS_FETCH=1`,
`enabled_providers:["opencode"]`, `plugin:[]`,
`OPENCODE_DISABLE_EMBEDDED_WEB_UI=1`, per-job `OPENCODE_DB`, read-only-git
`OPENCODE_CONFIG_CONTENT` + matching config `permission` block.
Full rationale + fork paths: `ACCEPTED_SETTINGS` in
`automation/opencode_lowmem_profile.py`.

Rejected (5, fail-closed with overturn trials): `snapshot:false`
(unconditional `track`/`patch` in `session/processor.ts`),
`OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER` (run-path effect unverified),
`OPENCODE_DISABLE_AUTOCOMPACT` (#80 keeps compaction as the history bound),
`OPENCODE_DISABLE_PROJECT_CONFIG` (breaks repo-specific behavior),
`OPENCODE_MODELS_URL/PATH` stubs (new failure mode, no win over the disable
flag). See `REJECTED_SETTINGS` in the module.

Two live-binary corrections found while verifying (both promoted to the
topic note): `share:false` is schema-invalid (must be `"disabled"`);
`OPENCODE_DB` requires a pre-created parent dir (runner fixed to `mkdir -p`
it — without this, every worker job fails before bootstrap).

## Correctness

- Offline representative task (q1 shape) succeeds under the profile:
  `{"result": "42", "matches": "1"}`.
- Profile validation accepts the triple and rejects every rejected switch,
  session-reuse flag, missing `--pure`, and broken confinement (12 tests).
- Bare / config-file / cumulative probes reach the byte-identical bootstrap
  failure (modulo error ref), exit 1: the profile neither breaks nor
  shortcuts startup.

## What remains for #81 (not inferred)

Whether the profile moves the **live** peak (GC heap via `--smol` under a
sustained stream, avoided catalog/downloader/share work) is a live
measurement on a real Free worker with the ~1 s sampler — the profile files
and fingerprint in the JSON artifact are exactly what that matrix consumes.
Against the 450 MiB target / 512 MiB limit, config-only shows no
startup-peak path to the ~88–150 MiB gap; the gap must come from the
source-level variants if at all.
