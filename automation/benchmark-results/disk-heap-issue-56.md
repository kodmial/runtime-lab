# Transparent disk-backed heap experiment (issue #56)

Pinned OpenCode: 1.18.33 | Run: 36429944096 | Constraint: 512 MB cgroup, swap.max=0, ordinary disk

Machine-readable data: `disk-heap-issue-56-run-36429944096.json`.
Harness: `python3 automation/disk_heap_benchmark.py --include-network [--docker]`
Sampler: `automation/disk_heap_snap.py`. Shim source: `automation/disk_heap_shim/shim.c`.

## Goal

Issue #52 measured the real agent at ~600-615 MB peak: 512 MB/no-swap is
unreliable (one OOM-kill, one throttled pass at the ceiling), while swap
masked ~52 MB. This experiment tests user-space/file-backed substitutes
for swap before declaring Render Free unusable.

## Candidates and availability on the runner

- **libvmmalloc (Candidate 1): no binary package on Ubuntu 24.04**
  (`apt search vmmalloc` empty; PMDK 1.13 dropped it, moved to `pmem/vmem`).
  Built from `pmem/vmem` master source (src version 35c313c3f, nondebug
  `libvmmalloc.so.1`) with the repo's glibc 2.39 toolchain. The upstream
  "no glibc 2.34 adaptation" warning did NOT block basic function here.
- **memkind memtier (Candidate 2 modern): available as libmemkind 1.14.0.**
  `libmemtier.so` interposes malloc/calloc/realloc/free/posix_memalign and
  supports a genuinely transparent mode:
  `LD_PRELOAD=libmemtier.so MEMKIND_MEM_TIERS="KIND:FS_DAX,PATH:<dir>,RATIO:1;KIND:DRAM,RATIO:1;POLICY:STATIC_RATIO"`.
  No source integration needed. `memkind_create_pmem()` file tiers work on
  ordinary non-DAX filesystems.
- **Custom shim (Candidate 3): built for this run**
  (`automation/disk_heap_shim/shim.c`, gcc, `-Wall -Werror` clean).
  Stress-tested (32 MB + reuse + calloc-zero + 64/4096-byte memalign +
  realloc + 4 threads + foreign-pointer forwarding). Benchmark probe only.
- **userfaultfd (Candidate 4): unsuitable.** Unprivileged `userfaultfd()`
  returns EPERM (`vm.unprivileged_userfaultfd=0`), and no maintained
  transparent wrapper exists for an unmodified Bun/JSC binary. Recorded,
  not built.

## Sanity proof (trivial malloc-heavy binary, 32-128 MB)

All three interposers move plain libc malloc traffic to `rw-s` MAP_SHARED
mappings on ordinary disk (unlinked pool files, still file-backed):

| wrapper | pool RSS for 32 MB malloc | mapping |
|---|---|---|
| libvmmalloc 1 GB pool | 32,844 kB | `/pool/#inode (deleted)` |
| memtier FS_DAX 128 MB | 131,076 kB | `/pool/memkind.XXXXXX (deleted)` |
| custom shim 32 MB | 32,772 kB | `/pool/diskheap.XXXXXX (deleted)` |

memtier run: Pss_File 132 MB vs Pss_Anon 3.4 MB. The mechanism works;
the question is only what fraction of *OpenCode* goes through it.

## Host results (no limit, real agent "say hi in one word", free model)

| mode | exit | wall | peak tree | live anon | live pool | pool share |
|---|---|---|---|---|---|---|
| baseline | 0 | 5.3 s | 558-603 MB | 440 MB | 0 | 0% |
| smol (`BUN_OPTIONS=--smol`) | 0 | 4.3 s | 552-562 MB | 440 MB | 0 | 0% |
| vmmalloc | 0 | 5.3 s | 563-610 MB | 441 MB | 2.7 MB | 0.5% |
| vmmalloc+smol | 0 | 4.2 s | 567 MB | 446 MB | 2.7 MB | 0.5% |
| memtier | **-11 SIGSEGV** | 31.7 s | 621-643 MB | 487 MB | 8.9 MB | 1.4% |
| shim | 0 | 4.2-5.3 s | 557-598 MB | 438 MB | 0.1-0.8 MB | <0.2% |
| shim+smol | 0 | 4.2 s | 560 MB | 444 MB | 0.8 MB | <0.2% |

No wrapper reduces peak (all within run-to-run noise of baseline).
memtier segfaults deterministically (3/3 hi-task runs: 2 host + 1 container).

## Docker 512 MB no-swap results (memory.max=536870912, swap.max=0, swap.current=0)

| mode | exit | cgroup peak | oom_kill | note |
|---|---|---|---|---|
| baseline+hi | -9 OOM | ceiling | 1 | |
| baseline+code | -9 OOM | ceiling | 1 | representative coding task also OOMs |
| smol+hi | 0 | ceiling | 0 | throttled through at ceiling (boundary luck) |
| smol+code | -9 OOM | ceiling | 1 | |
| vmmalloc+hi | -9 OOM | ceiling | 1 | pool 1.1 MB |
| vmmalloc+code | -9 OOM | ceiling | 1 | pool 1.2 MB |
| vmmalloc+smol+hi | 0 | 499 MB | 0 | under ceiling by luck, pool 1.1 MB |
| vmmalloc+smol+code | -9 OOM | ceiling | 1 | |
| shim+hi | 0 | 482 MB | 0 | under ceiling by luck, pool 0.1 MB |
| shim+code | -9 OOM | ceiling | 1 | |
| memtier+hi | -11 SEGV | 537 MB | 0 | crash, not OOM |
| memtier+code | -9 OOM | ceiling | 1 | OOMs before reaching crash point |

Scoreboard (repeatable success = the bar): baseline 0/2, smol 1/2,
vmmalloc 0/2, vmmalloc+smol 1/2, shim 1/2, memtier 0/2. The odd passes sit
at/just under the ceiling with `oom_kill 0` and pool residency ~0.1-1 MB:
boundary nondeterminism already documented in issue #52 (one trial OOMs,
one throttles through), not an allocator effect.

## Why LD_PRELOAD cannot fix this (smaps evidence, live agent)

Top anonymous-private mappings during a live run (docker baseline+code):

| RSS | mapping | perms |
|---|---|---|
| 384 MB | `[anon:WKFastMalloc]` | rw-p |
| 39 MB | (unnamed) | rw-p |
| 13 MB | `[anon:JSGigacage]` | rw-p |
| 8 MB | `[anon:JSJITCode]` | rwxp |
| 7 MB | `[anon:JSStructureHeap]` | rw-p |
| 0.04 MB | `[heap]` | rw-p |

The Bun binary exports no malloc/calloc/realloc/free symbols of its own
(verified with `nm -D`), so interposers win lookup and the ~0% result is
genuine: JavaScriptCore's WTF FastMalloc/Gigacage/JIT and Bun's own
segments allocate with direct anonymous `mmap`, bypassing libc
malloc-family paths by design. `[heap]` (brk) is 36-44 kB: the libc heap
is effectively unused. File-backed mappings only ever hold 0.1-8.9 MB.

A MAP_SHARED file page is also not swap: it is reclaimable only via
writeback to its file, and dirty anonymous JSC pages converted to
file-backed dirty pages still consume the same cgroup charge until
written back. With <1% of pages even eligible, reclaim behavior is moot.

## Verdicts

- **libvmmalloc: partially works, not useful.** Loads, intercepts, serves
  malloc from a pool file on ordinary disk with no slowdown (5.3 s vs
  5.3 s), but moves ~0.5% of the footprint. Archived upstream + glibc
  drift make it a production risk even where it helps.
- **memkind memtier FS_DAX: incompatible with this binary.** Transparent
  mode is real and works on trivial programs, but the OpenCode/Bun
  process segfaults deterministically (exit -11, ~32 s stall).
- **Custom shim: partially works, not useful.** Correct and transparent,
  moves <0.2%, which independently confirms the interception ceiling.
  Benchmark probe only; never ship a toy allocator.
- **BUN_OPTIONS=--smol: minor help.** ~40 MB (~7%) off peak; still OOMs.
- **userfaultfd pager: unsuitable.** EPERM unprivileged; no maintained
  transparent wrapper for unmodified Bun/JSC.
- **Overall: no candidate converts 512 MB/no-swap from unreliable to
  repeatable success.** No production wrapper is implemented (deliverable
  4's precondition not met). The irreducible set is the named JSC/Bun
  anonymous mappings above.

## Reproduce

Build libvmmalloc from `pmem/vmem` master (`make -C src`, use
`src/nondebug/libvmmalloc.so.1`; needs glibc >= 2.38 for the prebuilt
artifact used here), build the bench image
(`docker build -f automation/disk_heap_bench.Dockerfile -t diskheap-bench`),
then run the host and docker commands recorded in the JSON `reproduce`
field with `--vmmalloc-so` pointing at the built library.
