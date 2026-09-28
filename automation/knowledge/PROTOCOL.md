# Runtime Lab Agent Knowledge Handoff Protocol

This directory is the durable, repository-native memory for automated agents and infrastructure experiments.

Agents must not rely on conversation memory, model memory, or a previous process still being alive. The repository is the durable handoff boundary.

## Lifecycle: READ -> PLAN -> EXECUTE -> RECORD -> PROMOTE

### READ
Before changing code:
1. Query the derived experiment catalog first for cheap discovery (offline, deterministic, no network):
   `python automation/knowledge_catalog.py query --issue <N>`,
   `python automation/knowledge_catalog.py query --topic <slug>`,
   `python automation/knowledge_catalog.py query --outcome <succeeded|failed|inconclusive>`,
   `python automation/knowledge_catalog.py query --record-id <id>`.
   Filters compose. `validate` and `build` work the same way.
2. Read relevant files in `automation/knowledge/topics/`.
3. Open only the relevant full experiment records in `automation/knowledge/experiments/` found via the catalog (search by issue, subsystem, failure signature, API/provider, or hypothesis).
4. Read the newest relevant records and any record they supersede.

The catalog is discovery/navigation only. It must never replace the full record as evidence.

### PLAN
State the hypothesis/objective and success evidence. Do not repeat a failed experiment unless a material premise changed. Record the changed premise: code revision, API contract, timeout, credential/configuration, environment, model, region, or other meaningful condition.

### EXECUTE
Prefer the smallest experiment that can falsify the hypothesis. Preserve correlation data: issue number, run ID, base commit, service/job IDs when safe, and durable log links.

Keep these separate:
- **Observation** — directly seen in logs, API responses, tests, or repository state.
- **Interpretation** — what the observations probably mean.
- **Decision** — what the project changes because of the evidence.

### RECORD
Every automated issue execution writes exactly one unique record:
`automation/knowledge/experiments/issue-<ISSUE>-run-<RUN_ID>.md`

Records are immutable history after merge. If later evidence changes the conclusion, create a newer record and mark the old conclusion superseded.

Use:

```markdown
---
{
  "$schema": "automation/knowledge/schema/experiment.json",
  "base_commit": "<40-char-sha>",
  "issue": <number>,
  "outcome": "succeeded|failed|inconclusive",
  "record_id": "issue-<ISSUE>-run-<RUN_ID>",
  "run_id": "<id>",
  "schema": "runtime-lab-experiment/v1",
  "supersedes": [],
  "topic": "<short-topic>"
}
---
```

The front matter is strict JSON: a JSON object between the existing `---` delimiters, parsed with Python `json`. JSON is the canonical machine
representation; the Markdown body remains the human narrative. The filename, `issue`, `run_id`, and `record_id` must agree; validation fails
closed on mismatch. Contracts live in `automation/knowledge/schema/` (JSON Schema Draft 2020-12); runtime validation stays stdlib-only and
enforces the domain invariants directly (see `automation/knowledge_catalog.py`).

```markdown
# <short title>

## Hypothesis / objective
## Prior knowledge consulted
## Preconditions / changed premise
## Procedure
## Observations
## Interpretation
## Decision / result
## Validation
## Reusable knowledge
## Unresolved questions / next experiment
## Evidence
- workflow/log/PR/commit links
## Cleanup proof
- deletion/absence evidence, or "not applicable"
```

Raw logs stay in Actions/artifacts. Knowledge files contain the minimum durable evidence needed to reconstruct the reasoning.

### PROMOTE
Experiment records are history. Topic notes are compact current operating knowledge.

Promote only reusable, evidence-backed findings. If newer evidence invalidates an old rule, keep the old experiment record, update the topic note explicitly, and link the superseding evidence.

Topic notes must stay concise enough for future agents to read before work.

## Concurrency
Never use one shared append-only notes file. Parallel agents write unique per-run files, avoiding hot-file conflicts.

## Catalog (derived, never a source of truth)
Per-run Git records remain canonical. `automation/knowledge_catalog.py` scans the experiment records, enforces domain invariants
(duplicate identity, filename/metadata agreement, outcome/topic/commit shape, known non-self supersedes targets, acyclic supersedence,
no path traversal), and derives a deterministic catalog (stable ordering, stable keys, UTF-8, no wall-clock fields, per-record path
plus SHA-256 of the complete source record). `build` writes to stdout by default and optionally to an explicitly supplied output
path (for example a temp/cache path); never commit a generated shared `index.json` — parallel agents would all rewrite the same
hot file and create unnecessary merge conflicts.

## Security
Never record API keys, tokens, Authorization headers, cookies, private payloads, or secret values. Prefer links to protected logs over copying sensitive data.

## Recovery
If an agent dies before recording, its recovery task reconstructs the minimum experiment record from run metadata/logs while fixing the reusable defect.

## Quality rule
The objective is not more documentation. It is to make the next agent begin at the current frontier of knowledge instead of rediscovering it.
