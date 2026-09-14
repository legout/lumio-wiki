---
status: accepted
supersedes: ADR-0003
---

# ADR-0027: Repository Planning Contract and Supervised Orchestration

## Context

ADR-0003 chose an issue-only mattpocock workflow and rejected standing specification and plan documents. The repository now contains durable PRDs, cross-session implementation plans, and work that spans packages and review gates. The old decision no longer describes how changes are shaped, approved, executed, or recovered.

## Decision

Use the repository's `planning-contract` as the single artifact and handoff model. `CONTEXT.md` owns vocabulary, ADRs own durable architectural constraints, `docs/prd/` owns behavior and acceptance, `docs/plans/` owns compact execution maps, and GitHub Issues own canonical task bodies only when tracker coordination is needed.

Plans use the smallest coherent vertical slices. Related tasks may share one validation unit and one obligation (`new-test`, `existing-check`, or `no-new-test`); assurance covers named reachable failures rather than repeating broad test matrices. Orchestrated execution defaults to supervised mode with one configured implementer, proportional independent review, one writer per worktree, and separate approval for candidate assembly, integration, and publication.

## Consequences

ADR-0003 is historical and superseded. Repository plans and specifications are valid standing artifacts, but research, file location, and completed implementation do not manufacture approval. Existing completed plans remain evidence records rather than being rewritten as templates; proposed work must satisfy the current readiness contract before dispatch. No second scheduler, automatic ticket creation, or test-per-task rule is introduced.
