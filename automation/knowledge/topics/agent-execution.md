# Agent execution and handoff — current knowledge

## Confirmed
- `automation/knowledge/` is the durable memory boundary between short-lived agents.
- Every automated issue run reads the protocol, relevant topic notes, and prior relevant experiment records before changing code.
- Every run writes one unique experiment record before finishing.
- Unique per-run files avoid conflicts between parallel agents; topic notes are curated current knowledge, not diaries.
- OpenCode issue execution must not modify `.github/workflows/**`; workflow envelopes are maintained separately.
- Git/branch/commit/PR orchestration belongs to the workflow.
- Observations, interpretations, and decisions are recorded separately so old hypotheses are not inherited as facts.
- Discover history with the derived catalog first: `python automation/knowledge_catalog.py query --issue <N> | --topic <slug> | --outcome <succeeded|failed|inconclusive> | --record-id <id>` (filters compose, offline, deterministic). Then read only the matching full records; the catalog never replaces a record as evidence. Evidence: `../experiments/issue-34-run-36410184254.md`.
- Experiment front matter is strict JSON (see `../schema/experiment.json`); filename, `issue`, `run_id`, and `record_id` must agree. Never commit a generated catalog file; `build` writes to stdout or an explicit temp/cache path. Evidence: `../experiments/issue-34-run-36410184254.md`.
- Private long-term knowledge lives in `kodmial/agent-knowledge` (`schema/` + `projects/<project>/experiments|topics|decisions`); ephemeral workers receive only selected knowledge via `automation/knowledge_store.py:build_worker_context` and never receive `TAP_PAT`/App tokens. Normal access uses short-lived App installation tokens scoped to the knowledge repo (`metadata:read` + `contents:write`); writes use bounded optimistic CAS, experiment paths are immutable, and topic conflicts merge explicitly. Absent `TAP_PAT` pauses only live bootstrap with an explicit blocker, never a public fallback. Evidence: `../experiments/issue-37-run-36411929515.md`.
