---
status: accepted
---

# ADR-0024: Graph Exchange Exports and the CLI/Skill Boundary

## Context

The obsidian-wiki ecosystem (Ar9av, which parts of this project's agent
tooling descend from) ships two skills:
[`wiki-export`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-export/SKILL.md)
and
[`wiki-import`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-import/SKILL.md).
wiki-export emits `graph.json` (NetworkX node_link), `graph.graphml`,
`cypher.txt`, `postgres.sql`, an interactive `graph.html`, and optionally an
OKF markdown bundle. wiki-import loads a `graph.json` as **stub pages** or an
OKF bundle as full pages, with `merge`/`skip`/`overwrite` modes that
direct-write into the vault.

Lumio already exceeds these skills on the OKF axis: Exchange Profiles 1 and 2
(ADR-0007, ADR-0015) provide pinned, diagnostic-carrying OKF import/export with
visibility filtering and proposal-first imports. What Lumio lacks is the
**general graph interop** axis: there is no standard-format export of the
Knowledge Graph / Discovery Graph for external tools (Gephi, yEd, Cytoscape,
Neo4j, notebooks), and no way to bootstrap a Knowledge Base from another
tool's `graph.json`.

The harder question is architectural: the Ar9av skills implement deterministic
transforms (graph extraction, format serialization, stub generation) as
*prose instructions an agent re-executes every time*. That is the right shape
for an Obsidian vault with no tooling, and the wrong shape for Lumio, whose
thesis (ADR-0002, ADR-0003) is a model-free deterministic core that agents
operate, not re-implement.

## Decision

**Deterministic transforms sink into `lumio-wiki`; agent judgment stays in the
skill layer as usage guidance only.**

1. **Core SDK + CLI, new `export-graph` operation.** Deterministic export of
   the Discovery Graph over the **authorized page set** to:
   - `graph.json` — NetworkX node_link format (the de-facto interchange
     standard; what wiki-export/wiki-import and most Python tooling speak);
   - `graph.graphml` — GraphML for Gephi/yEd/Cytoscape.

   Nodes carry page identity, Canonical Page Title, Content Category, tags,
   and summary — **never** body content, Sources, or private registry data.
   Edges are typed canonical Relationships plus untyped Extracted References,
   with the edge kind marked so consumers can distinguish asserted knowledge
   from derived navigation. **Visibility filtering is enforced, not
   disclosed**: `restricted`/`internal` pages are excluded exactly as at the
   OKF export boundary (ADR-0007), so an export can never leak a title or
   summary the caller is not authorized to see.

2. **Core SDK + CLI, new `import-graph` operation.** Loads a `graph.json`
   (ours or wiki-export's) and stages **stub Compiled Pages as an ordinary
   reviewable Ingest Proposal** — frontmatter skeletons (title, category,
   tags) plus link structure, no bodies. The Proposal Pipeline replaces
   wiki-import's `merge`/`skip`/`overwrite` modes entirely: review, validate,
   publish or discard. Imports never direct-write; "merge vs overwrite" is a
   review-time decision the Maintainer makes on a diff, which is strictly
   stronger than the skill's blind modes.

3. **Deferred: `cypher.txt`, `postgres.sql`, `graph.html`.** No known
   consumer. GraphML covers the visual tools; graph.json covers code. Add
   when a real integration asks (YAGNI with a named trigger).

4. **No new implementation skills.** The shipped `lumio-wiki` SKILL.md gains a
   short "Exchange" section teaching the agent *when* to reach for which
   command (`export --profile okf-2` for vault-to-vault/standards exchange,
   `export-graph` for analysis/visualization, `import-graph` for bootstrap,
   `ingest` for content-bearing sources). Choosing formats, presenting
   previews, and advising merge decisions is agent work; producing bytes is
   CLI work.

## Considered Options

- **Port wiki-export/wiki-import as Lumio skills** — rejected: re-implements
  deterministic serialization as per-agent prose; untestable, divergent, and
  contrary to the model-free-core thesis. The skills' *value* is format
  choice and workflow; that value is captured by the CLI + SKILL.md guidance.
- **All five export formats** — rejected: cypher/postgres/html have no
  consumer today; each format is a maintenance contract.
- **Adopt wiki-import's merge/skip/overwrite at the CLI level** — rejected:
  bypasses proposal-first, the platform's core trust invariant. Stub import as
  proposal keeps one rule: nothing enters the Knowledge Base unreviewed.
- **Disclose visibility filtering in export metadata instead of enforcing** —
  rejected: wiki-export's "note it in metadata" approach leaks by default;
  Lumio's export boundary already enforces authorized page sets (ADR-0007).
- **Export body content in graph.json** — rejected: graph export is a
  structure artifact; content-bearing exchange is what OKF profiles are for.

## Consequences

- Two new CLI commands and two new Core SDK public functions, each with
  gold-file tests; no new dependencies (node_link JSON and GraphML are
  writable with stdlib + msgspec).
- `data/skill/SKILL.md` gains an Exchange section; the skill version bumps.
- graph.json becomes a third documented exchange surface alongside OKF
  Profile 1/2, with its own stability note (node fields are additive-only).
- Wiki-to-wiki migration paths: full content = OKF Profile 2 round trip;
  structure only = export-graph → import-graph.
