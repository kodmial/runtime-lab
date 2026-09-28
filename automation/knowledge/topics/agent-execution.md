# Agent execution and handoff — current knowledge

## Confirmed
- `automation/knowledge/` is the durable memory boundary between short-lived agents.
- Every automated issue run reads the protocol, relevant topic notes, and prior relevant experiment records before changing code.
- Every run writes one unique experiment record before finishing.
- Unique per-run files avoid conflicts between parallel agents; topic notes are curated current knowledge, not diaries.
- OpenCode issue execution must not modify `.github/workflows/**`; workflow envelopes are maintained separately.
- Git/branch/commit/PR orchestration belongs to the workflow.
- Observations, interpretations, and decisions are recorded separately so old hypotheses are not inherited as facts.
