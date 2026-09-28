# Issue #80 — session-history and tool-output memory trace

Date: 2026-09-28. Fork revision examined: `kodmial/opencode` main
(`9000e7f`, same revision inventoried by #77; #79 has no landed
optimized revision in the catalog, so this trace is pinned to the
#77-inventoried revision). Runner revision: runtime-lab `ceab65e`.
All fork paths below are `packages/opencode/src/**` on fork `main`
unless stated; verified live via the GitHub Contents API (read-only).

## 1. Where tool outputs are retained in the fork (verified)

- `tool/truncate.ts` — the central bound. `MAX_LINES = 2000`,
  `MAX_BYTES = 50 * 1024`, overridable per deployment via
  `tool_output.max_lines` / `tool_output.max_bytes` in opencode
  config (`Truncate.limits()`). Oversize output is written to
  `TRUNCATION_DIR` (`tool/truncation-dir.ts`) and the agent receives a
  preview plus a `Full output saved to: <file>` retrieval path.
  Truncation files are garbage-collected hourly after a 7-day
  retention (`RETENTION = Duration.days(7)`). Direction is
  configurable (`head` default, `tail` supported).
- `tool/shell.ts` — already streams: `trunc.limits()` resolves the
  bound, the live ring keeps `keep = limits.maxBytes * 2` bytes across
  at most a chunk list (`used > keep` drops the oldest chunk while
  `list.length > 1`), overflow spools to a file via `trunc.write()`,
  and the final result is `tail(raw, limits.maxLines, limits.maxBytes)`
  with a `...output truncated... / Full output saved to:` prefix.
  Shell output therefore cannot grow memory without a configured
  bound. Tail (not head) is returned, so trailing errors survive.
- `tool/grep.ts` / `tool/glob.ts` — hard result cap of 100 rows
  (`limit = 100`, `truncated` flag, "Results truncated. Consider using
  a more specific path or pattern."). Bounded by construction.
- `tool/read.ts` — `DEFAULT_READ_LIMIT = 2000` lines,
  `MAX_LINE_LENGTH = 2000` chars per line, `MAX_BYTES = 50 * 1024`
  per read. Bounded by construction.
- `tool/task.ts` — **gap (unbounded)**. Subagent results are injected
  into the parent session via `renderOutput()` with the full
  `result.info.output ?? ""` / `result?.output ?? ""` text and no
  truncation call: no `Truncate` import, no length check on the
  completed/failed/background-task paths. A verbose subagent (large
  build log, recursive search delegated via the Task tool's explore
  agent) grows the parent session by its full transcript on every
  invocation, and repeated Task calls accumulate without a bound.
- `tool/edit.ts` — no output caps found; outputs are match diagnostics
  and unified diffs, small in practice. Verified small, no change
  required (re-check if edit ever returns file contents).
- `session/processor.ts` — session loop calls `snapshot.track()`
  before the LLM stream and `snapshot.patch()` after; failures set
  `ctx.needsCompaction = true`, and the driver returns `"compact"`
  when set, so compaction is the filtering step for obsolete payloads.
  Compaction can be disabled by config (`compaction.auto === false`,
  `OPENCODE_DISABLE_AUTOCOMPACT`), which removes the only filtering
  stage — one-shot jobs must leave it enabled (the runner never sets
  the disable flags; see §3).
- `cli/cmd/run.ts` — fresh session is the default: `--continue`,
  `--session`, `--fork` are opt-in resume flags only
  (`args.fork && !args.continue && !args.session` is rejected; resume
  requires an explicit session id). `opencode run` with no resume
  flags creates a new session every invocation. No fork change needed
  for work item 1; enforcement belongs in the caller (done in
  `automation/opencode_runner.py:assert_fresh_session_command` plus
  per-job `OPENCODE_DB` isolation).

## 2. Where outputs were duplicated in the runner (fixed here)

- `SubprocessCommandRunner.run` used `subprocess.run(PIPE, PIPE)`,
  retaining the full stdout+stderr of every command in memory before
  any truncation. Fixed: `automation/bounded_output.py:run_bounded`
  spools to temp files; only bounded head/tail slices enter memory.
- `_execute_opencode` kept `last_output` (full combined, unbounded
  for custom runners) plus `output`, `detail`, and `first_error`
  copies. Fixed: stream-bound at receipt (`_bound_stream`), terminal
  bound in the record (`_truncate` head+tail), `first_error` stored at
  the terminal bound.
- `JobManager._jobs` grew without eviction: every terminal job's
  output/error/changes and its workspace clone stayed resident.
  Fixed: `max_retained_jobs` (default 20, env
  `RUNNER_MAX_RETAINED_JOBS`) with workspace deletion on eviction.
- `_truncate` was head-only (`text[:limit]`), hiding trailing errors.
  Fixed: head+tail with an explicit omission marker.

## 3. One-shot defaults enforced by the runner (no provider change)

- Fresh process per issue: each job clones into an isolated temp
  workspace and spawns a new `opencode run` subprocess; resume flags
  are rejected fail-closed.
- Fresh session per issue: per-job `OPENCODE_DB` under the workspace
  root (never inside the clone, so change detection stays clean) plus
  `OPENCODE_DISABLE_SHARE=1` (upload side-channel only).
- Compaction left enabled: the runner never sets
  `OPENCODE_DISABLE_AUTOCOMPACT` / `OPENCODE_DISABLE_PRUNE` and never
  passes `snapshot:false`; the fork default (compact when needed)
  applies.
- Exit codes, timeout semantics, stdout/stderr split, and model
  selection are unchanged; only capture size, summary shape, and
  retention are bounded.
