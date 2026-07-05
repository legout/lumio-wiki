---
status: accepted
---

# ADR-0003: mattpocock/skills as the Sole Agent-Skill Backbone

## Context

Lumio's early docs were produced with the superpowers plugin (obra/superpowers) for planning and execution, plus mattpocock-skills (mattpocock/skills) for domain vocabulary (`codebase-design`, `domain-modeling`, `grilling`). The two ecosystems overlap on `tdd`, `research`, `prototype`, and `code-review`, and — more fundamentally — prescribe conflicting orchestration models: superpowers is doc-in-repo (`docs/superpowers/specs/`, `docs/superpowers/plans/`) executed by `subagent-driven-development`; mattpocock is issue-tracker-driven (`grill-with-docs` → `to-prd` → `to-issues` → `implement`), with `CONTEXT.md` + `docs/adr/` as the only standing repo docs. Running both installed meant a parallel orchestration layer that was never used, routing ambiguity on overlapping skills, and composition smells in the artifacts (e.g. ADRs far heavier than mattpocock's own ADR format because the superpowers plan mandated nine sections).

## Decision

Adopt mattpocock/skills as Lumio's sole agent-skill backbone. Plans and specs no longer live as repo docs; "what to build" is captured in PRDs under `docs/prd/` and broken into issues via `to-issues`. The only standing repo documentation mattpocock skills consume is `CONTEXT.md` (ubiquitous language) and `docs/adr/` (decisions). The setup scaffold lives in `docs/agents/` (issue tracker, triage labels, domain-doc consumer rules).

## Considered Options

- **Keep superpowers as backbone, mattpocock vocabulary only** — rejected: preserves the existing plan artifacts with least churn, but leaves the parallel mattpocock orchestration layer installed as dead weight and keeps the two orchestration philosophies in conflict.
- **Run both fully** — rejected: the overlap creates routing ambiguity and the models disagree on where "the plan" lives (repo doc vs issue tracker).

## Consequences

One orchestration model (interview-driven, issue-tracker-backed). `CONTEXT.md` is consumed by every mattpocock skill, so domain language stays consistent across `tdd`, `diagnosing-bugs`, `to-prd`, and `improve-codebase-architecture`. ADRs follow mattpocock's minimal format. `/to-prd`, `/to-issues`, `/triage`, and `/wayfinder` are usable now that the GitHub tracker is configured.

Trade-off: superpowers' strengths — `executing-plans`, `subagent-driven-development`, `dispatching-parallel-agents`, `verification-before-completion`, `systematic-debugging` — are no longer available. If Lumio later wants doc-in-repo plan execution or parallel sub-agent dispatch, this decision should be revisited.
