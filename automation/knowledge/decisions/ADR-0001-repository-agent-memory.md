# ADR-0001: Use repository-native experiment records as agent memory

- Status: Accepted
- Date: 2026-09-28

## Context
Runtime Lab uses short-lived agents and ephemeral workers. A later agent does not share reliable process memory with an earlier one. Findings were scattered across Actions logs, issue comments, code, and conversation context, causing rediscovery and repeated experiments.

## Decision
Use Git as the durable handoff boundary with two layers:
1. immutable per-run records under `automation/knowledge/experiments/`;
2. compact curated current knowledge under `automation/knowledge/topics/`.

Every automated issue run reads prior knowledge before changing code and writes one unique run record before finishing. Reusable validated findings are promoted into topic notes. Historical records are not rewritten.

## Rationale
- Git provides provenance, review, history, and source-version coupling.
- Per-run files avoid concurrent append conflicts.
- Topic notes bound context cost for future agents.
- Separating observation, interpretation, and decision reduces propagation of unproven assumptions.
- Run links keep detailed evidence out of the prompt while preserving traceability.

## Alternatives rejected
- Conversation/model memory only: not a durable execution boundary.
- One shared NOTES.md/JSONL: hot-file conflicts and unbounded growth.
- Issue comments only: harder for runtime agents to consume systematically and weakly coupled to source revisions.
- Raw Actions logs: evidence, not curated knowledge.
