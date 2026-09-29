# mmap file-backing A/B for the OpenCode PR #15 binary (issue #144)

Run `36509899175`, base `befb5ee377c16531870937986e90d410b0879da1`.
Machine-readable data: `mmapfb-issue-144-run-36509899175.json` (this file's
sibling). Reproduce with `python3 automation/mmap_filebacked_bench.py`
(see `--help`); unit/integration coverage in
`automation/test_mmap_filebacked.py` (22 tests).

## SHA chain

- Source: `kodmial/opencode` PR #15, head
  `842157c38db9f8178ed0eee7af32f7536fe2346e` (open, branch `coding-no-mini`;
  tarball 81,533,570 B, byte-identical size to the #134 fetch; all three PR
  file markers verified).
- Build: `OPENCODE_VERSION=1.18.33 bun run --cwd packages/opencode
  script/build.ts --coding --single` (bun 1.4.2, same path as #134).
- Binary: 171,218,400 B, SHA-256
  `d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0`,
  `--version 1.18.33`, `--help` run-only. Two consecutive local rebuilds are
  byte-identical, but the digest differs from the #134 fingerprint
  (`4e310bbd...28ded`, 171,222,496 B) by 4,096 B: the Bun compile is
  locally deterministic yet not reproducible across build environments, so
  exact #134 binary identity is NOT claimed. Both A/B arms below run this
  same binary (no rebuild/substitution between arms); arm-A peaks reproduce
  the #134 marginal band, corroborating functional equivalence.
- Shim: `automation/mmap_filebacked_shim/shim.c`, built
  `gcc -O2 -shared -fPIC -Wall -Werror`, private-file mode
  (`OPENCODE_FILEBACKED_SHARED=0`), min 1 MiB, 2 GiB sparse pool per
  process. MAP_SHARED mode was rejected by the fork-CoW probe (child write
  becomes parent-visible; locked by test).

## Workload and envelope

Frozen FOO_LIMIT task (find constant, 10 -> 20, run pytest, 2 passed),
fresh repo per trial, Docker `--memory=512m --memory-swap=512m` with
fail-closed inner verification (`memory.max=536870912`,
`memory.swap.max=0`), free model `opencode/muse-spark-1.3-contributor-free`.

## Results

| Trial | Shim | Peak | vs 512 MiB | max/oom_kill | Wall | Exit | Correctness |
|---|---|---|---|---|---|---|---|
| noshim-t1 | off | 537,964,544 B (513.0 MiB) | +1.0 MiB | 2534/0 | 62 s | 0 | pass |
| noshim-t2 | off | 537,096,192 B (512.2 MiB) | +0.2 MiB | 730/0 | 36 s | 0 | pass |
| shim-t1 | on | 537,149,440 B (512.3 MiB) | +0.3 MiB | 5803/0 | 30 s | 0 | pass |
| shim-t2 | on | 545,255,424 B (520.0 MiB) | +8.0 MiB | 9652/0 | 60 s | 0 | pass |

Arm verdicts (proven #52/#109/#126 vocabulary): arm A `marginal`, arm B
`marginal`. Comparison: max-peak delta on-vs-off +7,290,880 B (noise),
headroom target (>= 32 MiB below 512 MiB) NOT met, `no_oom_kill` true.

## Shim telemetry (arm B)

- Redirected: t1 1,077,936,128 B (2 mappings, main agent process),
  t2 1,172,307,968 B (46 mappings across the tree) — ~1000x the
  malloc-only/libvmmalloc coverage (~1 MB, issue #56).
- Bypassed: ~13.97 GB per trial, dominated by `pool_exhausted` (the 8 GiB
  JS Gigacage reservation exceeds the 2 GiB pool) and `too_small`
  (hundreds of small libc mmaps from helper processes); `executable`
  (1 GiB JSJITCode), `no_write`, `not_anonymous` correctly skipped;
  `fixed`/`stack`/`shared_anon` never redirected (0 redirect violations).
- Live `/proc/*/maps` proof: backing-file token hits in 14/15 (t1) and
  29/30 (t2) 2-second samples; agent-stderr `[mmapfb]` lines show up to
  ~1.08 GB active file-backed bytes mid-run. (Parser note: the JSON
  `backing_maps_lines` entries are stats-file echoes, not maps lines — a
  fixed post-run parser filter now requires the maps address-range shape;
  the `backed_hits` sampling counters above are the authoritative maps
  evidence.)
- Pool files: 17 (t1) sparse 2 GiB files, 35 GB apparent / 72 KB actual
  disk — no disk pressure. Post-mortem per-pid stats aggregated across
  the tree (Bun raw-exits, skipping destructors, so a periodic flusher +
  throttled synchronous flush carries telemetry; fork children fall back
  to anonymous via `pthread_atfork` to protect zero-init/CoW).

## Verdict

**Technically works but insufficient memory reduction.** ~1.1 GB becomes
file-backed with zero behavioral regression (real coding task succeeds in
all four trials), but cgroup peaks do not move: MAP_PRIVATE file pages,
once dirtied by the JSC GC, carry the same cgroup charge as anonymous
memory, so the workload still rides the ceiling. The shim is NOT wired
into any delivery path (deliverable 6 declined on the evidence). The
remaining ceiling classes are the 8 GiB raw-syscall Gigacage reservation
(invisible to LD_PRELOAD by construction) and dirty private pages that
only MAP_SHARED-style writeback (rejected: breaks fork CoW) or real swap
could reclaim.
