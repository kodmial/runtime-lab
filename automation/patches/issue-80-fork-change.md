# Issue #80 — follow-up change spec for `kodmial/opencode`

Owner: `kodmial/runtime-lab#80` (this Runtime Lab issue remains the
owner; no fork backlog is created). Target: the #79 optimized
revision of `kodmial/opencode`; #79 has no landed revision in the
knowledge catalog as of 2026-09-28, so this spec is pinned to the
#77-inventoried revision (`main 9000e7f`, upstream baseline
`ad6c72c`) and rebases cleanly onto the #79 revision when it lands
(all touch points are post-#77 paths listed below). PR creation and
branch orchestration belong to the workflow; this file is the
ready-to-apply change definition.

## Goal (from #80 Definition of Done)

A single tool call or accumulated output in a one-shot coding job
cannot grow memory without a configured bound, while required coding
behavior stays correct and no critical error information is hidden.

## Verified already-bounded (no change; regression-guard only)

- `tool/shell.ts`: streaming ring (`maxBytes * 2`) + file spool +
  tail result. Keep as is.
- `tool/grep.ts`, `tool/glob.ts`: 100-row cap. Keep as is.
- `tool/read.ts`: 2000-line / 2000-char / 50 KB caps. Keep as is.
- `tool/truncate.ts`: `tool_output.max_lines` / `max_bytes`
  config (defaults 2000 / 50 KB), file-backed retrieval, 7-day GC.
  Keep as is.
- `cli/cmd/run.ts`: fresh session by default. Keep as is.

## Change 1 (required): bound Task-tool subagent output injection

File: `packages/opencode/src/tool/task.ts`.

Problem: `renderOutput()` injects the full subagent transcript
(`result.info.output`, `result?.output`) into the parent session with
no truncation, so one verbose subagent (or repeated Task calls in a
fail -> inspect -> fix -> rerun loop) grows the parent session
without a bound.

Fix: after the subagent completes and before `inject()` /
`renderOutput()`, pass the subagent text through the existing
`Truncate.output()` service with `direction: "tail"` (trailing build
errors and test failures live at the end; head-only truncation would
hide them), and append the returned `outputPath` retrieval hint to
the injected message exactly as `tool/shell.ts` does
(`...output truncated... / Full output saved to: <file>`). Reuse the
resolved `tool_output.max_lines / max_bytes` limits (no new config
surface, no provider-semantics change). Suggested bound site: the
three `renderOutput({ ... text: <full> ... })` call sites
(completed, failed, background-task update paths near the current
`result.info.output ?? ""` / `result?.output ?? ""` expressions).

Preservation rule: exit status (`completed` vs failure message) and
the `<summary>` element are never truncated; only the free-text
transcript is bounded.

## Change 2 (required): route every remaining tool result through Truncate

Files: any `tool/*.ts` result path not listed as already-bounded
(`edit.ts` diagnostics today are small; audit again at implementation
time with a `grep -n "Truncate" packages/opencode/src/tool/*.ts`
pass and route stragglers through `Truncate.output()` with the same
tail-direction + retrieval-hint pattern).

## Change 3 (required): regression tests in the fork

- Subagent output larger than `tool_output.max_bytes` is injected as
  tail + retrieval hint; status/summary intact (small, exact, and
  over-limit cases).
- Repeated Task calls (e.g. 20 sequential verbose subagents)
  accumulate bounded session growth: assert total injected
  transcript bytes stay O(calls * bound), not O(total output).
- Config override (`tool_output.max_bytes`) is honored by the Task
  path identically to the shell path.

## Change 4 (required): memory measurement

Rerun the representative workload from the #77 plan
(`memory_benchmark.py` + Docker `--memory=512m --memory-swap=512m`
real-agent trial with a deliberately verbose subagent task: large
test log + recursive search + fail -> inspect -> fix -> rerun) before
and after Changes 1-2, and record peak family RSS. The effect must be
reported separately from any #79 dependency-stripping numbers.

## Non-goals (explicitly out of scope per #80 constraints)

- No provider/model/catalog semantics change.
- No head-only truncation of error text; tail or file-backed
  retrieval is mandatory.
- No session-history reuse across issues (caller-enforced fresh
  `opencode run`; fork default already fresh).
