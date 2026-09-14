# Library stabilization and simplification

## Status and authority

Plan 01, Plan 02 (P1–P5), Plan 03 (R1–R6), and Plan 04 (A1–A7) are implemented. Plan 05 is proposed and requires separate approval. The Plan 03 integration is recorded in `42fdc03`; no remaining proposed production change is authorized here.

- **Behavioral source:** [PRD-0006](../prd/0006-library-stabilization.md), behavior approved from owner decisions captured at audit revision `9712f6fd51ac4381a6ca4fc513e2546355fba0d0`.
- **Evidence:** [Library stabilization audit](../research/library-stabilization-audit.md). Findings select work to inspect; they do not define behavior or prove a current defect without reproduction.
- **Architecture:** the ADRs linked by each plan. Proposed ADR-0019 and ADR-0020 are evidence only until accepted.
- **Execution authority:** completed integrations had separate approval. This completion record authorizes no further implementation, candidate assembly, push, or release.
- **Planning contract:** Contract version 1; local project skill provenance `unknown`.

Capture checkpoint for this plan set: no new product vocabulary; ADR-0027 records the workflow decision; PRD-0006 owns the relocated behavior and non-goals; Plan 03 is integrated, and Plan 05 remains blocked on real external-consumer evidence before public API removal. Material source changes require reconciliation and approval before affected work proceeds.

## Plan map

| Plan | State | Behavioral criteria | Purpose |
| --- | --- | --- | --- |
| [01 — Test retention](01-test-retention.md) | Implemented | PRD-0006 constraints | Preserve essential journeys while deleting approved redundant coverage |
| [02 — Publication integrity](02-publication-integrity.md) | Implemented | [AC1](../prd/0006-library-stabilization.md#ac1-publication-integrity) | Prevent silent canonical loss and stale mutation |
| [03 — Snapshot/retrieval](03-snapshot-retrieval.md) | Implemented | [AC2](../prd/0006-library-stabilization.md#ac2-captured-snapshots-and-retrieval) | Bind derivatives to captured bytes and improve passage retrieval |
| [04 — Agent workflows](04-agent-workflows.md) | Implemented | [AC3](../prd/0006-library-stabilization.md#ac3-safe-agent-workflows) | Repair safety and machine-facing workflow defects |
| [05 — Simplification/release](05-simplification-release.md) | Proposed / partly blocked | [AC4](../prd/0006-library-stabilization.md#ac4-simplification-and-coordinated-release) | Remove obsolete ownership only after consumer evidence; release separately |

Completed plans are evidence records, not templates for future assurance. Their historical per-task test wording and commands are retained because they describe what ran. Proposed plans use current validation units and lean review policy.

## Order and ownership

Plan 03 integrated R1 → R2/R3 → R4/R5 → R6. Consumer inventory precedes S1/S2 public removals; all accepted fixes precede separately approved S4 release work. Shared `knowledge_base.py`, `ingest.py`, `proposal_pipeline.py`, `cli.py`, `__init__.py`, and fixtures require serial integration even when independent work is developed separately. Use one writer per worktree; no automatic merge or release.

Plan 04's A6 shipped against the existing snapshot and Evidence seams. R1/R5 are later hardening work, not prerequisites for the already integrated bounded-read behavior.

## Requirement coverage

| PRD-0006 criterion | Tasks |
| --- | --- |
| AC1 | P1–P5 |
| AC2 | R1–R6 |
| AC3 | A1–A7 |
| AC4 | S1–S4 |

Detailed B/I/C identifiers remain evidence labels in the audit note; task completion is judged against PRD acceptance, not against a finding table stored in a plan.

## Lean validation policy

A validation unit may cover several tightly related tasks. Give each unit exactly one obligation:

- `new-test` only when changed behavior lacks meaningful existing coverage and a named reachable failure would otherwise be unprotected;
- `existing-check` when a focused existing journey already exercises the behavior; or
- `no-new-test` when a new test would prove little, including documentation and mechanical work.

For `new-test`, add the smallest public-seam regression, observe the intended failure, make the minimal change, and rerun the focused check. Do not duplicate one behavior across layers or adapters without a distinct material failure mode. Low-risk work gets parent diff inspection, normal-risk work one candidate review, and high-risk or dependency-defining work immediate plus candidate review.

Run focused tests serially while developing. Run the full suite once at the candidate boundary when the plan or repository policy requires it, not at every task boundary:

```sh
uv run pytest -q <focused paths or node IDs>
uv run pytest -q -n 4
uv run ruff check packages tests eval scripts
uv lock --check
git diff --check
```

Only run commands whose covered failure mode is named. Installed-wheel and live MinIO checks remain release evidence where packaging or remote behavior is in scope; a skip is not live certification. Plan/documentation changes are `no-new-test`: check links, paths, task IDs, placeholders, and `git diff --check`, plus an existing Markdown linter only if one is already configured.
