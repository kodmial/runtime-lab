# Issue #79 — Coding-only low-memory OpenCode variant matrix and fork PR plan

Date: 2026-09-28. Fork examined: `kodmial/opencode` main `9000e7f`
(baseline `automation/opencode-fork.baseline.json`) over upstream
`ad6c72c`; observed `ae343e8` at execution time (drift noted below).
Inventory grounding:
`automation/audits/issue-77-opencode-headless-inventory.md` at
`9000e7f`/`ad6c72c`. Machine contract:
`automation/opencode_coding_variant.py`
(`runtime-lab-opencode-coding-variant/v1`).

Blocked-by status: catalog queries for #78 and #85 return no records, so
no #78 peak exists to diff against yet. Step-0 baseline below reuses the
measured #52 peaks (real agent ~600-615 MB, `--version` ~177-200 MB) as
the surrogate, and every removal group defines the exact #78 rerun
procedure instead of claiming a bulk saving.

Design constraints (authoritative): do not replace OpenCode with a custom
direct provider/API client; keep the upstream/full build usable; the
lightweight variant must remove modules from the dependency graph, not
merely deny them after load; source changes materialize as a PR in
`kodmial/opencode` while tracking stays in this issue (issue execution
itself performs no git pushes or PR creation — the workflow owns Git
state, so this document plus the contract module are the reviewable PR
specification).

## 1. Entrypoint / build target (new files in the fork PR)

- `packages/opencode/src/coding-index.ts` — coding-only yargs entry.
  Registers only `RunCommand` (plus `version`/`help`); statically imports
  only `RunCommand`, `./cli/ui`, `./cli/error`, `./util/error`,
  `./cli/heap`. All other subcommands (`tui`, `web`, `serve`, `acp`,
  `attach`, `generate`, `console`, `providers`, `agent`, `upgrade`,
  `uninstall`, `models`, `debug`, `stats`, `mcp`, `github`, `export`,
  `import`, `session`, `plug`, `db`) are absent from its static graph.
  Verified live at `ae343e8`: current `src/index.ts` statically imports
  every one of these, so the saving is structural, not flag-based.
- `packages/opencode/src/project/bootstrap-coding.ts` — lazy bootstrap.
  Always runs `config.get()` + minimal `plugin.init()` + `project.init()`;
  gates `lsp` / `shareNext` / `format` behind empty-map / disable flags via
  dynamic `import()` (never top-level); `snapshot`/`vcs` run through a
  no-op/file-local backend when `snapshot:false` /
  `OPENCODE_CODING_SNAPSHOT=0` (processor still calls
  `snapshot.track/patch` unconditionally — see §4). Verified coupling at
  `9000e7f`: `project/bootstrap.ts` eagerly materializes all six inits.
- `packages/opencode/src/tool/registry-coding.ts` — pruned registry.
  Statically imports only `read`, `glob`, `grep`, `edit`, `write`,
  `apply_patch`, `shell`, `task`, `todo`, `invalid` (+ conditional
  `skill`/`webfetch` via dynamic import). Excludes `question`,
  `websearch`, `lsp`, `plan`, `code-mode`. Verified live: current
  `src/tool/registry.ts` statically imports `QuestionTool`,
  `WebSearchTool`, `LspTool`, `PlanExitTool` plus codemode.
- `packages/opencode/src/server/routes/instance/httpapi/server-coding.ts`
  — pruned in-process route table for `run` (session/prompt/event/
  permission/config/provider/agent only). The full
  `server/routes/instance/httpapi/server.ts` statically imports the whole
  domain set (Account, ShareNext, MCP, LSP, Format, Plugin, Question, …).
- `packages/opencode/script/build-coding.ts` — `bun build` entry
  `src/coding-index.ts` with `CODING_ONLY=1` define; pruned externals for
  excluded deps (TUI/opentui/solid, MCP SDK, parcel/chokidar, unused
  `@ai-sdk/*`).
- `packages/opencode/package.json` — add `"build:coding":
  "bun run script/build-coding.ts"`; full `build` unchanged so the
  upstream/full binary stays usable.

## 2. Retained / removed capability matrix

Retained (coding loop must still complete the §5 task):

| Capability | Covering tools/services | Status |
|---|---|---|
| repo read/search | `read`, `glob`, `grep`, `Ripgrep` | retained |
| edit/write/patch | `edit`, `write`, `apply_patch` (model swap) | retained |
| shell/build/test | `shell` (bash) | retained |
| provider/model/session/agent loop | `Provider`, `ProviderAuth`, `LLM`, `Agent`, `SessionProcessor`, `SessionPrompt`, `SessionRunState`, `SessionStatus` | retained |
| failure reaction | events + status + `shell`/`read` re-loop | retained |
| planning fallback | `task` (subagent), `todo`, `invalid` router | retained (tiny; drop only with a run proving disuse) |
| conditional skills/web | `skill` (installed skills only), `webfetch` (prompt need only) | dynamic import |

Removed / compile-separated (absent from the coding static graph):

| Subsystem | Mechanism | Group |
|---|---|---|
| TUI + interactive footer (`TuiThreadCommand`, `./run/runtime`, opentui/solid, `@opencode-ai/tui`) | excluded from `coding-index.ts`; dynamic only | A |
| desktop/web UI (`WebCommand`, embedded UI) | excluded from entry | A |
| ACP (`AcpCommand`, `@agentclientprotocol/sdk`) | excluded from entry | A |
| `serve` listeners + mDNS (`Server.listen`, `bonjour-service`) | never linked in coding entry (`run` uses in-process `fetch`) | A |
| sharing sync (`ShareNext` uploader, `SessionShare` routes, `OPENCODE_DISABLE_SHARE`) | bootstrap skip + pruned server table | B/C |
| MCP client (`MCP`, `McpAuth`, `@modelcontextprotocol/sdk`, zero-server) | empty `mcp:{}` + lazy client import | C/D |
| LSP servers/downloaders | empty `lsp:{}` + `OPENCODE_DISABLE_LSP_DOWNLOAD` + lazy import | B |
| formatter table | `formatter:false` + lazy table | B |
| plugin npm-install path (`--pure`) | lazy install path | D |
| 12 internal auth plugins | `OPENCODE_DISABLE_DEFAULT_PLUGINS` + lazy table | D |
| external skills | `OPENCODE_DISABLE_EXTERNAL_SKILLS` + lazy discovery | D |
| `question` tool | excluded from coding registry (already permission-denied in `run.ts`) | C |
| `websearch` tool | excluded from coding registry (default-off gating) | C |
| `lsp`/`plan_exit`/`code-mode execute` tools | excluded (experimental, default off) | C |
| snapshot git-worktree backend | no-op backend behind `snapshot:false`; `track/patch` interface kept | B |
| file-watcher impl (`@parcel/watcher`, `chokidar`) | interface kept; impl lazy behind `OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER` | B |
| update latest-check (`upgrade()`, installation) | excluded; `autoupdate:false` + `OPENCODE_DISABLE_AUTOUPDATE` | D |
| models-catalog fetch (`ModelsDev`, `OPENCODE_DISABLE_MODELS_FETCH`) | excluded from coding path; explicit provider config only | D |
| unused `@ai-sdk/*` providers | lazy provider resolution; pinned `opencode/*` path kept | D |

Explicitly NOT cut (coupling proven in #77): `Snapshot.track/patch`
interface, `Database`/storage sqlite persistence, server in-process `fetch`
mechanism itself, provider transform + auth paths, VCS `Watcher.Event`
subscription interface, session compaction agent wiring.

No custom provider/API rewrite: `provider/provider.ts` interface and the
`session/llm` call path are unchanged; only unused provider imports become
lazy. Any file named `direct-provider-client` / `custom-llm-client` /
`openai-fetch-shim` fails validation.

## 3. Step-0 config-only baseline (measure first, no fork change)

CLI: `opencode run --pure --auto --model <pinned> <prompt>`. Env:
`OPENCODE_PURE=1`, `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`,
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, `OPENCODE_DISABLE_LSP_DOWNLOAD=1`,
`OPENCODE_DISABLE_SHARE=1`, `OPENCODE_DISABLE_AUTOUPDATE=1`,
`OPENCODE_DISABLE_MODELS_FETCH=1`. Config JSON: `mcp:{}` (empty map),
`lsp:{}` (empty map), `formatter:false`, `snapshot:false` (test-only;
processor coupling may veto — fall back to no-op backend in group B),
`share:false`, `autoupdate:false`, `enabled_providers:["opencode"]`.
Surrogate peaks (#52, pinned 1.18.33): `--version` ~177-200 MB tree;
real agent ~600-615 MB tree (host), Docker 512m/no-swap nondeterministic
(one OOM 137, one throttled pass with `max 140`). Reliable envelope
~730 MB. The #78 rerun replaces these numbers when that issue lands.

## 4. Ordered removal groups with benchmark gates (no opaque bulk change)

- Group A — command-tree pruning (low risk): add `coding-index.ts` +
  `build-coding.ts`, `build:coding` script, lazy non-`run` commands in
  `index.ts`. Gate: `--version` peak + startup parse check.
- Group B — bootstrap laziness (medium; snapshot/vcs): add
  `bootstrap-coding.ts`, gate `lsp/shareNext/format/snapshot/vcs` init.
  Gate: config-baseline real-run peak vs step-0.
- Group C — registry/route laziness (medium): add `registry-coding.ts` +
  `server-coding.ts`, conditional tool imports, pruned in-process routes.
  Gate: representative-task peak + correctness pass (§5).
- Group D — dependency pruning (medium-high): drop unused `@ai-sdk/*`,
  TUI/web/desktop, MCP SDK (zero-server), plugin install path. Gate:
  Docker `--memory=512m --memory-swap=512m` real-agent trials + bundle
  check. Rerun `memory_benchmark.py --include-network` and
  `disk_heap_benchmark.py` per group.

## 5. Correctness (representative coding task)

Prompt: "Search the repository for the constant named FOO_LIMIT, read the
surrounding module, change its value from 10 to 20, run the module
build/tests, fix any failure the change causes, and report the files
changed plus the test result." Pass requires: glob/grep/read finds the
constant; edit/write/apply_patch modifies it; shell runs build/tests;
the agent reacts to a failure (re-read/edit/re-run) and returns files
changed plus test result. Coverage: repo-search → read/glob/grep;
file-edit → edit/write/apply_patch; shell-build-test → shell;
provider-agent-loop → Provider/LLM/SessionProcessor/SessionPrompt/Agent;
failure-reaction → status/run-state/events + shell/read.

## 6. Initialization-boundary tests

`automation/test_opencode_coding_variant.py` proves: retained set covers
the representative task; excluded tools absent from the coding registry
spec; each coding file has a static-import boundary and sample pruned
sources pass it while full-build sources fail it; no provider rewrite;
config baseline carries every kill switch; groups A-D ordered with gates.
Bun-side acceptance (fork PR CI): `bun run typecheck`, coding-only test
subset, per-group `memory_benchmark.py` + Docker trials, and a negative
probe asserting excluded routes/tools are unreachable from the coding
binary.

## 7. Fork PR plan (workflow materializes; no git operations here)

Base: `kodmial/opencode` `main` at `9000e7f` (spec base) — observed
`ae343e8` at runtime, so rebase the patch onto current `main` before
opening. Branch: `coding-only-variant-issue-79`. Title: "Add coding-only
low-memory entrypoint and build target (issue #79)". New files: the five
`CODING_*` paths (§1). Modified: `package.json`, `src/index.ts`,
`src/effect/app-runtime.ts`, `src/tool/registry.ts`,
`src/plugin/index.ts`, `src/provider/provider.ts`,
`src/project/bootstrap.ts` (gating/laziness only; full build untouched).
Body must cite: this issue (#79), inventory artifact, exact fork commit
built, per-group benchmark deltas vs #78, retained/removed matrix (§2),
and the no-provider-rewrite attestation. Drift note: fork `main` moved
`9000e7f` → `ae343e8` during this run; the PR must target the fresh
`main` and re-verify `src/index.ts` / `registry.ts` / `bootstrap.ts`
excerpts quoted here.
