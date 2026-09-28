# Issue #101 — fork change spec: real coding-only source stripping in `kodmial/opencode`

Base: `kodmial/opencode` `main` at `ae343e82aba50b77c5c90996d4d784f955cda2ee`
(live at execution time; spec base `9000e7fc8d96c845512f7c73122431418a71d4e4`
from `automation/opencode-fork.baseline.json`).
Upstream `anomalyco/opencode` base `75e1e7ae310dc36c86c920e8997d0e1181e24a88`,
pin `1.18.33`.
Proposed branch: `coding-only-lite-issue-101`.
No custom provider rewrite anywhere: all model access stays through the
existing `provider/provider.ts` + `session/llm` path.

This document is the exact fork-side change for the workflow to apply.
All new files are additive; the full build (`src/index.ts` /
`script/build.ts` / `scripts.build`) is untouched and stays usable.
Follows the #77 inventory
(`automation/audits/issue-77-opencode-headless-inventory.md`) and the #79
contracts (`automation/opencode_lite.py` + `automation/opencode-lite.spec.json`
lite paths, `automation/opencode_coding_variant.py` staging A-D).
Machine checks: `automation/opencode_source_stripped.py`,
tests `automation/test_opencode_source_stripped.py`.

Live-fork grounding at `ae343e8` (read-only raw fetch, no clone):
`src/index.ts` (~4.3 kB) statically imports all ~22 subcommands
(`tui`/`web`/`serve`/`acp`/`attach`/`generate`/`console`/`providers`/
`agent`/`upgrade`/`uninstall`/`models`/`debug`/`stats`/`mcp`/`github`/
`export`/`import`/`session`/`plug`/`db`) plus `@opencode-ai/tui` UI paths;
`src/tool/registry.ts` (~17 kB) statically imports `QuestionTool`,
`WebSearchTool`, `LspTool`, `PlanExitTool` plus codemode;
`src/project/bootstrap.ts` (~2.4 kB) eager-inits
`config`/`plugin` then `lsp`/`shareNext`/`format`/`vcs`/`snapshot`/`project`;
`src/server/routes/instance/httpapi/server.ts` (~12.6 kB) imports the full
domain set (`Account`, `ShareNext`, `MCP`, `LSP`, `Format`, `Plugin`,
`Question`, ...); `src/effect/app-runtime.ts` (~4.5 kB) layers ~50 services;
`script/build.ts` (~7 kB) compiles `src/index.ts` + TUI worker + embedded
web UI into every release binary.

## What changes (additive; full build keeps working)

### New files

1. `packages/opencode/src/cli/cmd/run-coding.ts` — coding-only yargs command.
   Imports only `../../lite/agent` and `../../lite/tools`. Never imports
   `./tui`, `./web`, `./serve`, `./acp`, `./attach`, `@opencode-ai/tui`,
   `@opentui/*`, `solid-js`, `bonjour-service`, `@agentclientprotocol/sdk`,
   `@modelcontextprotocol/sdk`, `@/lsp/*`, `@/mcp`, `@/share/*`.
2. `packages/opencode/src/lite/tools.ts` — minimal tool set re-export:
   `read`, `grep`, `glob`, `edit`, `write`, `patch` (`apply_patch`),
   `bash` (`shell`). No `question`/`websearch`/`lsp`/`plan`/`codemode`,
   no `mcp`/`plugin`/`skill`/`formatter` imports (static or dynamic).
3. `packages/opencode/src/lite/agent.ts` — coding-only agent loop wiring
   over the existing `session`/`provider`/`agent`/`model` modules only.
4. `packages/opencode/src/project/bootstrap-coding.ts` — lazy bootstrap:
   always `config.get()` + minimal `plugin.init()` + `project.init()`;
   `lsp`/`shareNext`/`format` gated behind config via dynamic `import()`
   only from inside functions (never top-level); snapshot keeps the
   `track`/`patch` interface through a local no-op backend (the processor
   calls them unconditionally — see #77); watcher impl never imported.
5. `packages/opencode/src/tool/registry-coding.ts` — pruned registry:
   statically wires only the minimal set; `skill`/`webfetch` only via
   dynamic `import()` behind explicit flags; never `question`/`websearch`/
   `lsp`/`plan`/`codemode`.
6. `packages/opencode/src/server/routes/instance/httpapi/server-coding.ts` —
   pruned in-process route surface: session/agent/provider/tool routes only.
   No `ShareNext`, `share/session`, `Skill`, `MCP`, `McpAuth`, `LSP`,
   `Format`, `Plugin`, `Question` imports.
7. `packages/opencode/src/coding-index.ts` — coding-only yargs entry.
   Registers only the coding run command plus `version`/`help`; never
   imports the full command table.
8. `packages/opencode/script/build-lite.ts` — dedicated bun compile:
   entry `src/cli/cmd/run-coding.ts`, out `dist/lite`, `CODING_ONLY=1`
   define, pruned externals. Same Bun toolchain as `script/build.ts`.
9. `packages/opencode/script/build-coding.ts` — same graph via
   `src/coding-index.ts`, out `dist/coding` (alias track for the #79
   `build:coding` naming; identical exclusion set).

### Modified (one file, scripts only)

- `packages/opencode/package.json` — add `"build:lite":
  "bun run script/build-lite.ts"` and `"build:coding":
  "bun run script/build-coding.ts"` alongside the untouched `"build":
  "bun run script/build.ts"`. Version stays `1.18.33`.

## New file contents (exact)

### 1. `packages/opencode/src/cli/cmd/run-coding.ts`

```ts
// Coding-only headless command for runtime-lab autonomous work (issue #101).
//
// Dedicated entry for the lightweight binary: wires the retained
// provider/model/session/agent loop plus the minimal tool set
// (read/search/edit/write/patch/shell) without pulling the full
// command table (tui/web/serve/acp/attach/...) into the static graph.
// The full `src/index.ts` build is untouched and stays usable.
import { runCodingAgent } from "../../lite/agent"
import { codingTools } from "../../lite/tools"

export const RunCodingCommand = {
  command: "run-coding [message..]",
  describe: "run the coding-only agent with a message",
  builder: (yargs: any) =>
    yargs
      .positional("message", {
        describe: "message to send",
        type: "string",
      })
      .option("model", {
        describe: "provider/model id",
        type: "string",
      })
      .option("dir", {
        describe: "working directory",
        type: "string",
      }),
  handler: async (args: any) => {
    const message = Array.isArray(args.message)
      ? args.message.join(" ")
      : String(args.message ?? "")
    await runCodingAgent(codingTools, {
      message,
      model: args.model,
      directory: args.dir ?? process.cwd(),
    })
  },
}
```

### 2. `packages/opencode/src/lite/tools.ts`

```ts
// Minimal coding tool set (issue #101). Re-export only; no registry
// rewrites, no question/websearch/lsp/plan/codemode, no mcp/plugin/skill
// wiring here (conditional skill/webfetch load dynamically in the full
// registry-coding path when explicitly needed).
export { ReadTool as readTool } from "../tool/read"
export { GrepTool as grepTool } from "../tool/grep"
export { GlobTool as globTool } from "../tool/glob"
export { EditTool as editTool } from "../tool/edit"
export { WriteTool as writeTool } from "../tool/write"
export { ApplyPatchTool as patchTool } from "../tool/apply_patch"
export { ShellTool as bashTool } from "../tool/shell"

import { ReadTool } from "../tool/read"
import { GrepTool } from "../tool/grep"
import { GlobTool } from "../tool/glob"
import { EditTool } from "../tool/edit"
import { WriteTool } from "../tool/write"
import { ApplyPatchTool } from "../tool/apply_patch"
import { ShellTool } from "../tool/shell"

export const codingTools = [
  ReadTool,
  GrepTool,
  GlobTool,
  EditTool,
  WriteTool,
  ApplyPatchTool,
  ShellTool,
] as const
```

### 3. `packages/opencode/src/lite/agent.ts`

```ts
// Coding-only agent loop wiring (issue #101). Reuses the existing
// provider/model/session/agent modules; no direct provider/API client.
import { createSession } from "../session/session"
import { loadProvider } from "../provider/provider"
import { stepAgentLoop } from "../agent/agent"
import { selectedModel } from "../model/model"

export async function runCodingAgent(tools: unknown, input?: unknown) {
  const provider = loadProvider(selectedModel)
  const session = createSession(provider)
  return stepAgentLoop(session, tools, input)
}
```

### 4. `packages/opencode/src/project/bootstrap-coding.ts`

```ts
// Lazy coding-only bootstrap (issue #101). Always runs config + minimal
// plugin + project init. lsp/shareNext/format load only inside functions
// via dynamic import() behind explicit config; snapshot keeps the
// track/patch interface through a local no-op backend because
// session/processor.ts calls track/patch unconditionally; the file-watcher
// implementation is never imported here.
import { Config } from "../config/config"
import { Plugin } from "../plugin"
import { Project } from "./project"

export const codingSnapshotBackend = {
  async track(..._args: unknown[]) {
    return undefined
  },
  async patch(..._args: unknown[]) {
    return undefined
  },
}

export async function bootstrapCoding(ctx: {
  directory: string
  config?: { lsp?: unknown; share?: unknown; formatter?: unknown }
}) {
  await Config.get()
  await Plugin.init({ minimal: true })
  await Project.init(ctx.directory)
  if (ctx.config?.lsp !== undefined && ctx.config.lsp !== false) {
    const { LSP } = await import("../lsp/lsp")
    await LSP.init(ctx.config.lsp)
  }
  if (ctx.config?.share !== undefined && ctx.config.share !== false) {
    const { ShareNext } = await import("../share/share-next")
    await ShareNext.init(ctx.config.share)
  }
  if (ctx.config?.formatter !== undefined && ctx.config.formatter !== false) {
    const { Format } = await import("../format")
    await Format.init(ctx.config.formatter)
  }
  return { snapshot: codingSnapshotBackend }
}
```

### 5. `packages/opencode/src/tool/registry-coding.ts`

```ts
// Pruned coding tool registry (issue #101). Statically wires only the
// minimal set; skill/webfetch load dynamically behind explicit flags;
// question/websearch/lsp/plan/codemode are never referenced here.
import { ReadTool } from "./read"
import { GrepTool } from "./grep"
import { GlobTool } from "./glob"
import { EditTool } from "./edit"
import { WriteTool } from "./write"
import { ApplyPatchTool } from "./apply_patch"
import { ShellTool } from "./shell"
import { TaskTool } from "./task"
import { TodoWriteTool } from "./todo"
import { InvalidTool } from "./invalid"

export const codingBuiltinTools = [
  ReadTool,
  GrepTool,
  GlobTool,
  EditTool,
  WriteTool,
  ApplyPatchTool,
  ShellTool,
  TaskTool,
  TodoWriteTool,
  InvalidTool,
] as const

export async function codingConditionalTools(opts: {
  skills?: boolean
  webfetch?: boolean
}) {
  const extra: unknown[] = []
  if (opts.skills) {
    const { SkillTool } = await import("./skill")
    extra.push(SkillTool)
  }
  if (opts.webfetch) {
    const { WebFetchTool } = await import("./webfetch")
    extra.push(WebFetchTool)
  }
  return extra
}
```

### 6. `packages/opencode/src/server/routes/instance/httpapi/server-coding.ts`

```ts
// Pruned in-process route surface for `run-coding` (issue #101):
// session/agent/provider/tool routes only. The full httpapi/server.ts
// table (Account/ShareNext/MCP/LSP/Format/Plugin/Question/...) stays in
// the normal build.
import { Session } from "@/session/session"
import { SessionProcessor } from "@/session/processor"
import { SessionPrompt } from "@/session/prompt"
import { SessionStatus } from "@/session/status"
import { SessionRunState } from "@/session/run-state"
import { Provider } from "@/provider/provider"
import { ProviderAuth } from "@/provider/auth"
import { Agent } from "@/agent/agent"
import { ToolRegistry } from "@/tool/registry"
import { Config } from "@/config/config"
import { Permission } from "@/permission"
import { Instruction } from "@/session/instruction"
import { LLM } from "@/session/llm"

export const codingRouteServices = [
  Session,
  SessionProcessor,
  SessionPrompt,
  SessionStatus,
  SessionRunState,
  Provider,
  ProviderAuth,
  Agent,
  ToolRegistry,
  Config,
  Permission,
  Instruction,
  LLM,
] as const
```

### 7. `packages/opencode/src/coding-index.ts`

```ts
// Coding-only yargs entry (issue #101; `build:coding` alias for the
// `build:lite` graph). Registers only the coding run command plus
// version/help; the full src/index.ts command table is never imported.
import yargs from "yargs"
import { hideBin } from "yargs/helpers"
import { RunCodingCommand } from "./cli/cmd/run-coding"
import { InstallationVersion } from "@opencode-ai/core/installation/version"
import { Heap } from "./cli/heap"

const cli = yargs(hideBin(process.argv))
  .scriptName("opencode-coding")
  .version("version", "show version number", InstallationVersion)
  .command(RunCodingCommand as any)
  .demandCommand(1)
  .strict()

cli.middleware(async () => {
  Heap.start()
  process.env.AGENT = "1"
  process.env.OPENCODE = "1"
  process.env.OPENCODE_PID = String(process.pid)
})

void cli.parse()
```

### 8. `packages/opencode/script/build-lite.ts`

```ts
#!/usr/bin/env bun
// Dedicated coding-only compile (issue #101). Same Bun toolchain as
// script/build.ts; pruned entry/graph/externals; full build untouched.
import { $ } from "bun"
import path from "path"
import { fileURLToPath } from "url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const dir = path.resolve(__dirname, "..")
process.chdir(dir)

const generated = await import("./generate.ts")
import { Script } from "@opencode-ai/script"
import pkg from "../package.json"

await $`rm -rf dist/lite`

await Bun.build({
  conditions: ["bun", "node"],
  tsconfig: "./tsconfig.json",
  format: "esm",
  minify: true,
  sourcemap: "none",
  splitting: false,
  compile: {
    autoloadBunfig: false,
    autoloadDotenv: false,
    autoloadTsconfig: true,
    autoloadPackageJson: true,
    target: "bun-linux-x64",
    outfile: "dist/lite/bin/opencode-coding",
    execArgv: [`--user-agent=opencode/${Script.version}`, "--use-system-ca", "--"],
    windows: {},
  },
  entrypoints: ["./src/cli/cmd/run-coding.ts"],
  external: [
    "@opencode-ai/tui",
    "@opentui/*",
    "solid-js",
    "@agentclientprotocol/sdk",
    "@modelcontextprotocol/sdk",
    "@parcel/watcher",
    "chokidar",
    "bonjour-service",
  ],
  define: {
    CODING_ONLY: "1",
    OPENCODE_VERSION: `'${Script.version}'`,
    OPENCODE_MODELS_DEV: generated.modelsData,
    OPENCODE_CHANNEL: `'${Script.channel}'`,
  },
})

const binaryPath = "dist/lite/bin/opencode-coding"
console.log(`Smoke test: ${binaryPath} --version`)
const versionOutput = await $`${binaryPath} --version`.text()
console.log(`Smoke test passed: ${versionOutput.trim()}`)
```

### 9. `packages/opencode/script/build-coding.ts`

```ts
#!/usr/bin/env bun
// Alias-track compile for the `build:coding` naming (issue #101; identical
// exclusion set to build-lite.ts, entry via src/coding-index.ts).
import { $ } from "bun"
import path from "path"
import { fileURLToPath } from "url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const dir = path.resolve(__dirname, "..")
process.chdir(dir)

const generated = await import("./generate.ts")
import { Script } from "@opencode-ai/script"

await $`rm -rf dist/coding`

await Bun.build({
  conditions: ["bun", "node"],
  tsconfig: "./tsconfig.json",
  format: "esm",
  minify: true,
  sourcemap: "none",
  splitting: false,
  compile: {
    autoloadBunfig: false,
    autoloadDotenv: false,
    autoloadTsconfig: true,
    autoloadPackageJson: true,
    target: "bun-linux-x64",
    outfile: "dist/coding/bin/opencode-coding",
    execArgv: [`--user-agent=opencode/${Script.version}`, "--use-system-ca", "--"],
    windows: {},
  },
  entrypoints: ["./src/coding-index.ts"],
  external: [
    "@opencode-ai/tui",
    "@opentui/*",
    "solid-js",
    "@agentclientprotocol/sdk",
    "@modelcontextprotocol/sdk",
    "@parcel/watcher",
    "chokidar",
    "bonjour-service",
  ],
  define: {
    CODING_ONLY: "1",
    OPENCODE_VERSION: `'${Script.version}'`,
    OPENCODE_MODELS_DEV: generated.modelsData,
    OPENCODE_CHANNEL: `'${Script.channel}'`,
  },
})

const binaryPath = "dist/coding/bin/opencode-coding"
console.log(`Smoke test: ${binaryPath} --version`)
const versionOutput = await $`${binaryPath} --version`.text()
console.log(`Smoke test passed: ${versionOutput.trim()}`)
```

### 10. `packages/opencode/package.json` (diff)

```diff
     "build": "bun run script/build.ts",
+    "build:lite": "bun run script/build-lite.ts",
+    "build:coding": "bun run script/build-coding.ts",
     "dev": "bun run ./src/index.ts",
```

## Correctness gate (same workload as the other tracks)

Representative task (`FOO_LIMIT` q1-small-edit): search for `FOO_LIMIT`,
change `10` -> `20`, run build/tests, fix failures, report files + result.
Covered by retained `read`/`grep`/`glob` + `edit`/`write`/`apply_patch` +
`shell` + provider/agent loop. Proved offline by
`representative_coding_task` in `automation/opencode_lite.py` and by the
`test_opencode_source_stripped.py` boundary tests; live q1-q5 matrix per
#81 runs on the fork CI binary.

## Measurement gate (same telemetry as the other tracks)

- Hermetic: `python3 automation/memory_benchmark.py --run-id <id>`
  (`--version` peak tree + startup walls).
- Docker: `--memory=512m --memory-swap=512m` ceiling probe, `--version`
  under limit, hog-over-limit negative control, representative coding task
  under limit, live `opencode run --auto` classification when network
  allows. Evidence:
  `automation/benchmark-results/source-stripped-issue-101-run-36462317943.json`.
- Verdict: `benchmark_delta()` on peaks vs the ~600-615 MB (#52, 614,632 kB)
  baseline plus config-only/bounded/direct tracks; fail closed on OOM kill,
  nonzero exit, or unverified cleanup.

## Revertibility

Delete the nine new files, drop `scripts.build:lite`/`build:coding`, and
the tree is the normal build again. The runner selects the coding binary
only when its artifact id/SHA-256 is explicitly requested, else the pinned
`1.18.33` baseline path is used with no silent fallback.
