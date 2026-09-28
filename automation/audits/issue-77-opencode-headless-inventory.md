# Issue #77 — OpenCode fork headless inventory for low-memory builds

Date: 2026-09-28. Examined fork revision: `kodmial/opencode` main
`9000e7fc8d96c845512f7c73122431418a71d4e4`
(`{"msg":"chore: wire Continuum control plane"}`), whose parent
`ad6c72c7068812d43b31f3cfb9e413356a19d850` (`"chore: generate"`) is also
present verbatim in upstream `sst/opencode` and is the effective upstream
baseline for everything below. Upstream default branch is `dev`; the fork adds
exactly one commit on top (`9000e7f`, Continuum control-plane wiring), so all
`packages/opencode/src/**` paths below are upstream code unless stated.

Blocking note: issue #76 (fork bootstrap) is still OPEN. The fork is populated
and preserves upstream history, so source tracing is valid now; rebase/sync
risk from #76 remains (fork `main` may move when #76 lands).

Our workflow path (the only one inventoried): non-interactive, non-attach
`opencode run --auto --model <provider/model> <prompt>` as built by
`automation/opencode_runner.py:build_opencode_command`. Interactive
(`--mini`), `--attach`, `serve`, `web`, `tui`, `acp` paths are noted only as
exclusion candidates.

## 1. Startup / dependency graph for `opencode run` (non-interactive)

1. `packages/opencode/src/index.ts` — yargs entry. Statically imports ~22
   command modules (`RunCommand`, `GenerateCommand`, `ConsoleCommand`,
   `ProvidersCommand`, `AgentCommand`, `UpgradeCommand`, `UninstallCommand`,
   `ModelsCommand`, `ServeCommand`, `DebugCommand`, `StatsCommand`,
   `McpCommand`, `GithubCommand`, `ExportCommand`, `ImportCommand`,
   `AttachCommand`, `TuiThreadCommand`, `AcpCommand`, `WebCommand`,
   `PrCommand`, `SessionCommand`, `PluginCommand`, `DbCommand`) plus
   `UI`, `InstallationVersion`, `FormatError`, `Heap`. Global middleware maps
   `--print-logs` to `OPENCODE_PRINT_LOGS=1`, `--log-level` to
   `OPENCODE_LOG_LEVEL`, `--pure` to `OPENCODE_PURE=1`, then calls
   `Heap.start()` and sets `AGENT=1`, `OPENCODE=1`, `OPENCODE_PID`.
2. `packages/opencode/src/cli/effect-cmd.ts` (`effectCmd`) — `RunCommand`
   declares `instance: (args) => !args.attach`, so the default run path sets
   `useInstance=true` and executes
   `InstanceStore.Service.use((store) => store.load({ directory }))`, then runs
   the handler with `InstanceRef` provided and `store.dispose(ctx)` in a
   `finally`. `instance:false` commands skip all of step 3 (`models`, `serve`,
   `web`, `account`, `db`, `upgrade` per the doc comment); `run` is NOT one.
3. `packages/opencode/src/effect/app-runtime.ts` (`AppLayer`) — builds a
   `ManagedRuntime` over ~50 `LayerNode.group` services: `Npm`, `FSUtil`,
   `Database`, `Auth`, `Account`, `Config`, `Git`, `Storage`, `Snapshot`,
   `Plugin`, `ModelsDev`, `Provider`, `ProviderAuth`, `Agent`, `Skill`,
   `Discovery`, `Question`, `Permission`, `Todo`, `Session`, `SessionProjector`,
   `SessionStatus`, `BackgroundJob`, `RuntimeFlags`, `EventV2Bridge`,
   `SessionRunState`, `SessionProcessor`, `SessionCompaction`, `SessionRevert`,
   `SessionSummary`, `SessionPrompt`, `Instruction`, `LLM`, `LSP`, `MCP`,
   `McpAuth`, `Command`, `Truncate`, `ToolRegistry`, `Format`,
   `InstanceStore`, `Project`, `Vcs`, `Workspace`, `Worktree`, `Installation`,
   `ShareNext`, `SessionShare`, plus `Ripgrep` and the Observability layer.
   All are statically imported at the top of the file.
4. `packages/opencode/src/project/instance-store.ts` (`InstanceStore.load`) —
   resolves the directory, runs `project.fromDirectory`, then
   `bootstrap.run` with `InstanceRef` provided.
5. `packages/opencode/src/project/bootstrap.ts` (`InstanceBootstrap.run`) —
   the eager per-instance init that every `run` pays for: `config.get()`,
   then `plugin.init()` (plugins can mutate config, so first), then
   concurrently `lsp.init()`, `shareNext.init()`, `format.init()`,
   `vcs.init()`, `snapshot.init()`, `project.init()` (each `init` only
   materializes per-instance `InstanceState`; slow work is forked to the
   instance scope, but construction + state scaffolding still runs).
6. `packages/opencode/src/cli/cmd/run.ts` (handler, `Effect.fn("Cli.run")`) —
   dynamically imports `@/agent/agent`, `@/effect/runtime-flags`,
   `@/effect/instance-ref`, `@/server/auth`. Non-interactive behavior:
   denies `question`/`plan_enter`/`plan_exit` via session permission ruleset;
   resolves `--dir`, `--file` attachments, piped stdin; creates the session
   over an **in-process** server (`Server.Default().app.fetch`, no TCP/MDNS)
   via `createOpencodeClient({ baseUrl: "http://opencode.internal", ... })`;
   subscribes to events and loops until `session.status idle`; auto-replies
   `permission.reply "once"` when `--auto/--yolo/--dangerously-skip-permissions`
   is set, else auto-rejects. `share()` runs when config `share === "auto"`,
   `flags.autoShare`, or `--share`. The `./run/runtime` interactive footer is
   dynamically imported ONLY on the interactive branch; the default
   non-interactive path never executes it (but see static-import findings).
7. Server side of the same process:
   `packages/opencode/src/server/server.ts` (`Server.Default()`) wraps
   `HttpApiApp.webHandler()`; `server/routes/instance/httpapi/server.ts`
   statically imports the full domain service set (Account, Agent, Auth,
   BackgroundJob, Command, Config, Env, Format, Git, Installation, LSP, MCP,
   McpAuth, Permission, Plugin, Project, Vcs, Provider, Question, session
   services, ShareNext, SessionShare, Skill, Snapshot, Storage, ToolRegistry,
   …). `Server.listen()` (TCP + mDNS) is NOT used by `run`.
8. Agent loop: `session/processor.ts` calls `Snapshot.track()` before the LLM
   stream and `snapshot.patch()` after; `ToolRegistry.tools()` filters per
   model/provider/agent/permission; `session/llm` invokes the provider;
   persistence goes through `Database` (sqlite) + `storage/storage.ts`
   (`Global.Path.data/storage`, overridable via `OPENCODE_DB`).

## 2. Disable switches verified against fork source

CLI flags (`run.ts` builder + global `index.ts` options): `--auto`
(`--yolo`, `--dangerously-skip-permissions` aliases in effect only),
`--model`, `--agent`, `--command`, `--format default|json`, `--file`,
`--title`, `--continue/--session/--fork`, `--share`, `--dir`, `--port`,
`--variant`, `--thinking`, `--attach/--username/--password`,
`--interactive/-i`, hidden `--mini/--replay/--replay-limit/--demo`;
global `--pure` (= `OPENCODE_PURE=1`), `--print-logs`, `--log-level`.

`OPENCODE_*` env (all confirmed by source grep at the examined commit;
primary definitions in `packages/core/src/flag/flag.ts` and
`packages/opencode/src/effect/runtime-flags.ts`):
`OPENCODE_PURE` (skip external plugins; `packages/opencode/src/plugin/index.ts`: `flags.pure ? []`
for `cfg.plugin_origins`),
`OPENCODE_DISABLE_DEFAULT_PLUGINS` (skip 12 internal auth plugins in
`internalPlugins()`),
`OPENCODE_DISABLE_EXTERNAL_SKILLS`, `OPENCODE_DISABLE_LSP_DOWNLOAD`,
`OPENCODE_DISABLE_CLAUDE_CODE[/_PROMPT/_SKILLS]`,
`OPENCODE_ENABLE_EXA` (+ legacy `OPENCODE_EXPERIMENTAL_EXA`),
`OPENCODE_ENABLE_PARALLEL` (+ legacy), `OPENCODE_ENABLE_EXPERIMENTAL_MODELS`,
`OPENCODE_ENABLE_QUESTION_TOOL`, `OPENCODE_EXPERIMENTAL[_REFERENCES,
_BACKGROUND_SUBAGENTS,_LSP_TY,_LSP_TOOL,_OXFMT,_PLAN_MODE,_CODE_MODE,
_EVENT_SYSTEM,_WORKSPACES,_ICON_DISCOVERY,_OUTPUT_TOKEN_MAX,
_BASH_DEFAULT_TIMEOUT_MS,_NATIVE_LLM,_WEBSOCKETS]`, `OPENCODE_AUTO_SHARE`,
`OPENCODE_DISABLE_EMBEDDED_WEB_UI`, `OPENCODE_CLIENT` (default `"cli"`),
`OPENCODE_DISABLE_SHARE=1/true` (`packages/opencode/src/share/share-next.ts` module-level kill
switch), `OPENCODE_CONFIG_CONTENT` / `OPENCODE_CONFIG` / `OPENCODE_CONFIG_DIR`
/ `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_PERMISSION`,
`OPENCODE_DISABLE_AUTOCOMPACT`, `OPENCODE_DISABLE_PRUNE`,
`OPENCODE_DISABLE_AUTOUPDATE` (+ `OPENCODE_ALWAYS_NOTIFY_UPDATE`,
honored in `packages/opencode/src/cli/upgrade.ts:upgrade()`), `OPENCODE_DISABLE_MODELS_FETCH`,
`OPENCODE_MODELS_URL` / `OPENCODE_MODELS_PATH`, `OPENCODE_DB`,
`OPENCODE_SERVER_PASSWORD/USERNAME`, `OPENCODE_GIT_BASH_PATH`,
`OPENCODE_FAKE_VCS`, `OPENCODE_PRINT_LOGS`, `OPENCODE_LOG_LEVEL`,
`OPENCODE_AUTO_HEAP_SNAPSHOT`, `OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER`,
`OPENCODE_PID`, `OPENCODE_WORKSPACE_ID`.

Config-file switches (`opencode.json[c]` schema, `config/config.ts` +
`config/v2-compat.ts` + domain services): `mcp.<name>.enabled=false` (server
never spawned; status `disabled`), `lsp.<name>.disabled=true` (server dropped;
empty/absent `lsp` map logs "all LSPs are disabled"),
`formatter=false` (logs "all formatters are disabled") or
`formatter.<name>.disabled=true` (ruff/uv linked), `snapshot=false`
(`packages/opencode/src/snapshot/index.ts` enablement + `packages/opencode/src/session/summary.ts` diff skip),
`share:"auto"|false`, `autoupdate:false|"notify"`, `disabled_providers` /
`enabled_providers` allowlists (`packages/opencode/src/provider/provider.ts:isProviderAllowed`),
`permission` rulesets (per-tool `allow|deny|ask`, what our
`OPENCODE_CONFIG_CONTENT` uses), `plugin` list (empty = no external loads),
`instructions`/agents/commands overrides.

## 3. Static-import findings (config cannot fix these)

- `src/index.ts` statically imports every subcommand, including the full TUI
  thread, web/serve, ACP, and debug trees. Bun compiles one binary, so
  headless `run` ships (and parses at startup) all interactive code.
- `src/effect/app-runtime.ts` statically imports all ~50 services; merely
  existing in the layer graph keeps their module initializers in the bundle.
- `server/routes/instance/httpapi/server.ts` statically imports the full
  domain service set even though `run` only exercises session/prompt/event
  routes through the in-process fetch.
- `packages/opencode/src/tool/registry.ts` statically imports all 16 builtin tool implementations
  plus `code-mode`; `packages/opencode/src/plugin/index.ts` statically imports 12 internal auth
  plugins; `packages/opencode/src/format/formatter.ts` holds every formatter definition;
  `provider/provider.ts` imports all `@ai-sdk/*` providers plus `ModelsDev`.
- `run.ts` itself only statically imports UI/error/filesystem/SDK scaffolding;
  the heavy footer UI (`./run/runtime`, `./run/footer*.tsx`) and
  `@/server/server` are dynamic `import()`s on their respective branches —
  non-interactive runs never execute them, but they remain in the binary.

## 4. Minimal required tool set (for repo inspection/edit/shell/agent loop)

Keep: `read`, `glob`, `grep`, `edit`, `write` (or `apply_patch` for
`gpt-*`-class models — `registry.ts:tools()` swaps them by model id),
`shell` (bash/build/test), `task` (subagent loop; keep pending a run proving
our prompts never invoke it), `todo` (planning state; tiny), `invalid`
(fallback router; tiny). Conditionally keep: `skill` (only with installed
skills), `webfetch` (only if prompts need it). Drop for our workflow:
`websearch` (gated already unless opencode-provider/EXA/PARALLEL),
`question` (already permission-denied in non-interactive `run`),
`lsp`/`plan_exit` (experimental, default off), `code-mode execute`
(experimental, default off).

## 5. Classification

1. Required: agent loop (`session/processor|prompt|llm|run-state|status`),
   provider load + auth for exactly one pinned provider, the Keep tool set
   above, `config.get`, permission evaluation, `ToolRegistry.tools`
   filtering, `Database`/storage session persistence for one execution,
   `Snapshot.track/patch` (processor + revert depend on it unconditionally),
   `Truncate`, `Instruction`, `InstanceStore`/`Project`, event bridge.
2. Safe to disable by configuration (no source change, measure first):
   external plugins (`--pure`), internal auth plugins
   (`OPENCODE_DISABLE_DEFAULT_PLUGINS`), external skills, all MCP servers
   (empty `mcp` map), LSP (`{}` map + `OPENCODE_DISABLE_LSP_DOWNLOAD`),
   formatters (`formatter:false`), share (`OPENCODE_DISABLE_SHARE=1`, no
   `--share`), snapshot compaction/prune/autocompact flags, models fetch,
   provider allowlist to one provider, update checks (`autoupdate:false`),
   question/websearch (permission deny + default-off gating),
   project-config loading if the worker needs it.
3. Candidates for physical exclusion (source change, ordered in §6):
   non-`run` subcommands; TUI/footer/interactive runtime; desktop/web/embedded
   UI; ACP; `serve`/`web` listeners + mDNS; upgrade/installation
   latest-check; models-dev catalog; ShareNext sync uploader; snapshot git
   worktree backend (only with a safe no-op behind `snapshot:false`);
   LSP servers/downloaders; formatter table; skill discovery; question/todo
   (if verified unused); MCP client stack under zero-server config; plugin
   npm-install path under `--pure`; unused `@ai-sdk/*` providers.
4. Risky due to coupling (do NOT cut without a harness run proving idle
   behavior): `Snapshot` (unconditional `track/patch` in processor/revert),
   `Database`/storage (projector + message history assume sqlite), server
   route table (in-process `run` constructs all routes), internal plugin auth
   (provider auth paths), provider transform + model catalog (many call
   sites), VCS file watcher (`Watcher.Event` HEAD subscription; disable flag
   exists but run-path effect unverified), session compaction agent.

## 6. Prioritized source-removal plan + benchmark order

- Step 0 — config-only baseline (no fork change): `--pure`,
  `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`,
  `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, empty `mcp`/`lsp` maps,
  `formatter:false`, `snapshot:false` (test-only; processor coupling may
  veto), `OPENCODE_DISABLE_SHARE=1`, `autoupdate:false`,
  `enabled_providers:[single]`, `OPENCODE_DISABLE_MODELS_FETCH=1`.
  Benchmark with `python3 automation/memory_benchmark.py --include-network`
  and `disk_heap_benchmark.py` reruns; record `--version` vs real-run peaks.
- Step 1 — command-tree pruning: lazy-load non-`run` commands in `index.ts`
  (dynamic `import()` per subcommand). Risk: low. Expected: binary/parse-time
  win, small RSS win.
- Step 2 — bootstrap laziness: gate `lsp/shareNext/format/snapshot/vcs`
  `init()` in `project/bootstrap.ts` behind the §2 flags (skip
  materialization, not just disable output). Risk: medium (snapshot/vcs).
- Step 3 — registry/route laziness: conditional tool imports in
  `packages/opencode/src/tool/registry.ts`; prune unused `httpapi` groups from the in-process
  server used by `run`. Risk: medium.
- Step 4 — dependency pruning: drop unused `@ai-sdk/*`, TUI/web/desktop
  packages, `@modelcontextprotocol/sdk` (zero-server runs), plugin install
  path. Risk: medium-high (bundle graph). Re-run Docker
  `--memory=512m --memory-swap=512m` real-agent trials per step.
- Explicitly out of scope: custom provider/API reimplementation.

## 7. Risks

Fork drift (#76 open; upstream `dev` moves fast), snapshot coupling vetoing
`snapshot:false` savings, sqlite persistence floor, Bun single-binary
dead-code limits (lazy `import()` helps startup/RSS more than bundle bytes),
and model-catalog network fetch under shared-egress rate limiting (already
seen with the installer lookup; pin + `OPENCODE_DISABLE_MODELS_FETCH`).
