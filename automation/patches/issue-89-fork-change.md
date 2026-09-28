# Issue #89 — fork change spec: direct headless entrypoint

Base: `kodmial/opencode` `main` at `9000e7fc8d96c845512f7c73122431418a71d4e4`
(spec base; rebase onto fresh `main` before opening — `ae343e8` drift was
observed during #79). Upstream `anomalyco/opencode`, pin `1.18.33`.
Branch: `direct-headless-issue-89`. No custom provider rewrite anywhere.

## What changes (additive; full build keeps working)

New files (see `automation/opencode_direct_headless.py:direct_files()`):

1. `packages/opencode/src/cli/cmd/run-direct.ts` — headless CLI entry that
   wires `RunCommand`-equivalent flags (`--auto`, `--model`, prompt) to the
   direct runner. Must not statically import `cli/cmd/tui|web|serve|acp|
   attach`, `@opencode-ai/tui`, `bonjour-service`, or
   `@agentclientprotocol/sdk` (boundary in `STATIC_IMPORT_BOUNDARIES`).
2. `packages/opencode/src/direct/direct-runner.ts` — direct dispatch: minimal
   bootstrap, then calls to `SessionProcessor` / `SessionPrompt` /
   `ToolRegistry` / `LLM` functions directly. Must not statically import
   `server/server`, `httpapi/server`, `share/share-next`, `@/mcp`,
   `@modelcontextprotocol/sdk`, or `@/lsp/lsp`. No `app.fetch()` hop; no
   `Request`/`Response` serialization between the CLI and the session
   services. Provider invocation stays `session/llm` -> `provider/
   provider.ts` unchanged.
3. `packages/opencode/src/project/bootstrap-direct.ts` — minimal instance
   bootstrap: `config.get()` + minimal `plugin.init()` + `project.init()`
   always; `lsp` / `shareNext` / `format` gated behind config; snapshot keeps
   the `track`/`patch` interface with a no-op backend option (the processor
   calls them unconditionally — see #77 inventory).
4. `packages/opencode/src/server/routes/instance/httpapi/server-direct.ts` —
   pruned in-process route surface: session/agent/provider/tool routes only.
   No `ShareNext`, `share/session`, `Skill`, `MCP`, `McpAuth`, `LSP`,
   `Format`, `Plugin`, or `Question` imports.
5. `packages/opencode/script/build-direct.ts` — dedicated bun build target
   emitting to `dist/direct`.

Modified (gating only):

- `packages/opencode/package.json` — add `scripts.build:direct` alongside
  the untouched `scripts.build` (version stays `1.18.33`).
- `packages/opencode/src/index.ts` — register the direct command lazily
  (dynamic `import()`, no new static edges).
- `packages/opencode/src/effect/app-runtime.ts`,
  `packages/opencode/src/server/routes/instance/httpapi/server.ts`,
  `packages/opencode/src/tool/registry.ts`,
  `packages/opencode/src/provider/provider.ts` — lazify only (unused
  `@ai-sdk/*` providers via dynamic `import()`; pinned provider path kept).

## Normal-path refs replaced (all at the spec base)

- `packages/opencode/src/index.ts` (yargs table, ~22 commands)
- `packages/opencode/src/cli/effect-cmd.ts` (`instance: !args.attach`)
- `packages/opencode/src/effect/app-runtime.ts` (~50-service layer graph)
- `packages/opencode/src/project/bootstrap.ts` (eager six-init)
- `packages/opencode/src/cli/cmd/run.ts`
  (`Server.Default().app.fetch`, `baseUrl: "http://opencode.internal"`)
- `packages/opencode/src/server/server.ts` + `httpapi/server.ts`
  (full domain route set; `Server.listen()` TCP + mDNS unused by `run`)

## Correctness gate (same workload as the other tracks)

Representative task (`REPRESENTATIVE_TASK`, `qualification_workload:
q1-small-edit`): search for `FOO_LIMIT`, change `10` -> `20`, run
build/tests, fix failures, report files + result. Then the #81 matrix
q1-q5 on a real Free worker per profile (normal vs direct binary).

## Measurement gate (same telemetry as the other tracks)

- Hermetic: `python3 automation/memory_benchmark.py --run-id <id>`
  (`--version` peak tree + startup walls, step-0 config on/off).
- Live: `python3 automation/memory_benchmark.py --include-network` for both
  binaries (normal `opencode run` vs direct binary `run`), Docker
  `--memory=512m --memory-swap=512m` real-agent trials, ~1s external
  sampler merged as `memory_telemetry`, all 22 #81 fields per run.
- Verdict: `benchmark_delta()` on the two peaks + correctness delta from
  the representative task; fail closed on OOM kill, worker replacement,
  nonzero exit, or unverified cleanup.

## Revertibility

Delete the five new files, drop `scripts.build:direct`, and the tree is
the normal build again. The runner selects the direct binary only when
`RUNNER_OPENCODE_DIRECT_BIN`/`OPENCODE_DIRECT_BIN` names an existing
executable, else it falls back to the normal path
(`build_direct_command`).
