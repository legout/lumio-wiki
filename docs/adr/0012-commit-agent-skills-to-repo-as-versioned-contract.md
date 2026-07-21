---
status: accepted
---

# ADR-0012: Commit Agent Skill Markdown to the Repository as a Versioned Contract

## Context

Issue #91 ships two mattpocock Agent Skills — `lint` and `cross-linker` — as
thin, role-gated wrappers over public `lumio_wiki` Core SDK operations. Each
skill has a durable contract: its inputs, the public SDK operations it calls,
its role gate (Maintainer/Owner only), its no-direct-write policy, and (for
`lint`) its canonical-vs-Discovery graph-scope disclosure.

ADR-0003 adopted mattpocock/skills as Lumio's sole agent-skill backbone and
established that the only standing repo documentation mattpocock skills
consume is `CONTEXT.md` (ubiquitous language) and `docs/adr/` (decisions).
"Plans and specs no longer live as repo docs." The question for #91 is whether
the skill *contracts themselves* (the Markdown a coding agent consumes) live
in the repository or are kept external.

The precedent is the `lumio-wiki` CLI skill: its `SKILL.md` and `PROTOCOL.md`
ship as versioned wheel data under `src/lumio_wiki/data/` (issue #98,
ADR-0010) so a coding agent can resolve them deterministically via
`importlib.resources` from a built wheel without cloning the Lumio repository.

## Decision

**Commit the `lint` and `cross-linker` skill Markdown to the repository as
versioned package data, colocated with the enforcing code.**

The two contracts live at `packages/lumio/src/lumio/skills/lint.md` and
`packages/lumio/src/lumio/skills/cross-linker.md`, alongside the Python that
enforces them (`lumio.skills`). They are declared as wheel data via
`[tool.hatch.build.targets.wheel.force-include]` in `packages/lumio/pyproject.toml`
and resolved at runtime by `lumio.skills.resolve_skill_markdown_path`.

## Rationale

- **Skills are durable contracts, not orchestration artifacts.** ADR-0003's
  "no plan/spec docs in repo" rule targets transient orchestration output
  (plans, specs executed once). A skill contract is a stable interface that
  must travel with the code it documents and be versioned with it.
- **Deterministic location.** A coding agent (or the Lumio runtime) must
  resolve a skill's contract without cloning the repo. In-repo package data
  is locatable via `importlib.resources` from the built wheel, exactly as the
  `lumio-wiki` CLI skill already is (issue #98).
- **No drift.** Colocating the Markdown with the enforcing Python keeps the
  declared role gate, SDK surface, and write policy in lockstep with the code.
  The `SkillContract` registry (`lumio.skills.SKILLS`) is the single source of
  truth the runtime enforces; the Markdown restates it for agents.
- **Versioning.** Committed Markdown is reviewed, diffed, and released with
  the code. An external store has no such guarantee and can silently diverge.

## Considered Options

- **Keep skill Markdown external** (e.g. a separate skill registry or a
  coding-agent vendor store) — rejected: breaks deterministic location from
  the built wheel, loses version pinning with the enforcing code, and invites
  drift between the declared contract and the enforced behavior.
- **Markdown-only, no enforcing code** — rejected: the role gate (Readers
  cannot run the authoritative checks) requires an enforcement point.
  Declaration alone is insufficient; the runtime must enforce it.

## In-repo SDK contract

Each skill Markdown declares, in YAML frontmatter and prose:

- `name`, `version`, `user-invocable`, `license`;
- `required-role: maintainer` (the role gate; enforced by
  `lumio.skills.authorize_skill`);
- `writes-directly: false` (every write stages a proposal through the
  Proposal Pipeline: `stage -> validate -> review -> publish`);
- `discloses-graph-scope` (true for `lint`);
- the public `lumio_wiki` operations called (the skill is thin — it delegates
  and adds no parallel logic).

`lumio_wiki` operations consumed:

- `lint`: `load_knowledge_base` (yields the authoritative cross-page QA
  `ValidationReport` from issue #86 AND the typed `KnowledgeBase`),
  `extract_references`, `KnowledgeBase.graph_health`,
  `GRAPH_SCOPE_CANONICAL`, `GRAPH_SCOPE_DISCOVERY`.
- `cross-linker`: `load_knowledge_base`, `find_link_candidates`,
  `LinkCandidate`, `ProposalPipeline`, `IngestStore`.

## Consequences

The skill Markdown is reviewable in diffs and ships in the wheel. Adding a
third skill means adding a Markdown file plus a `SkillContract` entry in
`lumio.skills.SKILLS` (and a `force-include` line). The role gate, SDK
surface, and write policy remain enforceable in tests without the web stack
by constructing `AuthContext` directly with each `Role`.
