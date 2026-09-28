# Render worker memory benchmark — issue #52

Rerunnable harness: `python3 automation/memory_benchmark.py --include-network
--output <json> --report <md>`
Machine-readable artifact:
`automation/benchmark-results/memory-benchmark-issue-52-run-36423884312.json`
(run 36423884312, base `6e3cba01307e7bd4acba4548219ffc3521e62026`,
pinned OpenCode 1.18.33).

Constraint: Render Free Web Service, 0.1 CPU, 512 MB RAM, no swap assumed
([Render free docs](https://render.com/docs/free): `free` = 0.1 CPU / 512 MB;
"Render might restart a Free web service at any time"; ephemeral filesystem).

## Upstream / vendor facts (verified live 2026-09-28)

- Installer (`https://opencode.ai/install`) defaults to `$HOME/.opencode/bin`,
  supports `--version <v>` (skips the unauthenticated `api.github.com`
  latest-release lookup that fails with "Failed to fetch version information"),
  `--binary <path>`, `--no-modify-path`.
- Docs (`https://opencode.ai/docs/`) list the install script, `npm/pnpm/yarn/bun
  install [-g] opencode-ai`, Homebrew, and release binaries. The npm tarball
  (`opencode-ai-1.18.33`, 3 kB) is a downloader shim around the same release
  binary, so package launch adds a Node/Bun runtime process instead of
  replacing the binary.
- Standalone release `1.18.33` is an ELF x86-64 binary (~177 MB on disk),
  dynamically linked against libc only — no Node.js runtime required.
  `file`/`ldd` verified; `bun` is not installed on this runner.

## Benchmark matrix (Linux GitHub Actions, kB)

| Variant | What ran | Peak tree | Peak main | Family | Result |
|---|---|---|---|---|---|
| Idle controller | `runner_server` process (live HTTP stack) | 22,540 | 22,540 | 1 | baseline |
| Bare Python | `python3 -c sleep` | 10,236 | — | — | interpreter floor |
| Git phase | `git clone --depth 1 file://` proxy | 5,220 | 5,220 | 1 | negligible |
| A. Python + subprocess | controller + fake agent script | 17,048 | 11,588 | 3 | overhead isolated |
| B. Standalone `--version` | pinned binary, no controller | 184,180 | 184,180 | 1 | deterministic |
| B. Standalone `run` (live free model `muse-spark-1.3-contributor-free`) | real agent, host | 614,632 | 614,632 | 1–2 | exit 0 |
| C. npm/Node wrapper | `node --version` proxy (`/usr/bin/time`) | 19,208 | — | — | shim adds ~19 MB transient |
| C. Bun package | not installed | — | — | — | not measurable; same-binary accounting applies |
| D. Go controller prototype | skipped (see below) | — | — | — | unjustified |

Method: full descendant-family VmRSS polling (never parent RSS alone),
cgroup v2 signals when present, `/usr/bin/time -v` as secondary
(`--version` max RSS ≈ 200 MB host; `run` max RSS ≈ 595–600 MB host).
No hidden helper processes: max family size 1–2; the Bun-compiled binary is
a single process.

## 512 MB no-swap stress (Docker `--memory=512m --memory-swap=512m`, verified ceiling `memory.max=536870912`, `memory.swap.max=0`)

- `opencode --version`: PASS, exit 0, cgroup peak 130,068,480 B (~124 MB), `oom 0`.
- Synthetic 700 MB perl hog: OOM-KILLED, exit 137, `oom 1 oom_kill 1` (harness validated).
- Real agent `opencode run` under the ceiling, two trials, same command:
  - trial 1: OOM-KILLED, `agent_exit=137`, `oom 1 oom_kill 1`;
  - trial 2 (in-artifact): throttled success, `agent_exit=0`, peak 536,920,064 B,
    `max 140` throttling events, `oom 0`.
- With swap allowed (`--memory-swap=1g`): success, `swap.peak` ≈ 52 MB — swap
  would mask ~52 MB, but Render Free guarantees no swap, so the design must
  not rely on it.

## Recommendation (evidence-backed)

1. **Keep the Python controller.** Idle Python is ~22 MB (~10 MB interpreter +
   ~12 MB app); the real agent peaks at ~600–615 MB. Controller overhead is
   ≈ 4% of peak — negligible. A Go rewrite would save at most ~15 MB while
   adding a toolchain and a second language to a stdlib-only worker, so
   variant D was not prototyped: the measured saving cannot justify the
   complexity against a ~600 MB agent footprint.
2. **Prefer the standalone binary over package wrappers.** npm/pnpm/yarn/bun
   forms are install shims for the same binary and add a runtime process;
   they cannot beat variant B on memory.
3. **Ship deterministic provisioning (implemented in this change):** pinned
   `1.18.33`, build-time copy to repo-relative `.opencode-bin/opencode`
   (independent of build-time vs runtime `$HOME`), `RUNNER_ALLOW_RUNTIME_INSTALL=0`
   in the Render start command, `/health` 503 + fail-fast job error when the
   binary is absent, safe path/version startup log. This removes the lazy
   `curl|bash` memory/CPU spike and the run-36421205678 failure class, but it
   does not shrink the ~600 MB agent peak itself.
4. **State the 512 MB verdict honestly: no architecture fits.** The standalone
   binary alone peaks ~90 MB over the ceiling on a real agent task, and the
   stress result at the boundary is nondeterministic (OOM kill vs throttled
   pass). The worker is therefore not reliable on the Free plan for real
   agent workloads.

## Safe memory budget for a 512 MB worker

- Measured peak (real agent, host): **614,632 kB (~600 MB)**.
- Recommended headroom: **128 MB** → reliable envelope ≈ **730 MB**.
- Pass/fail threshold: **524,288 kB (512 MB)** — the harness `budget.fits_512m`
  flag is `false` for every real-agent measurement.
- Operational consequence: expect OOM kills/throttling on Free; do not
  schedule real agent work on 512 MB without a larger plan, and keep the
  mandatory cleanup + one-service-per-attempt invariants (they are unaffected
  by this change).

## Unresolved (not guessed)

- Exact Render kernel/cgroup accounting may differ slightly from GitHub
  Actions runners; the host-measured ~600 MB peak is the portable signal.
- Future OpenCode releases (including v2) may change the footprint; re-run
  the harness after any re-pin.
- Whether Render would ever offer swap on Free is undocumented; the solution
  assumes none.
