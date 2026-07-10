---
status: accepted
---

# ADR-0005: Role-based UI synthesis from design studies

## Context

Lumio's first web UI pass established a working Stario + Datastar app shell
with chat, citation rendering, Compiled Page viewing, Knowledge Base browsing,
and Owner administration. A follow-up design study explored three more radical
interaction models:

1. **Constellation** — a graph-first explorer where Relationships between
   Compiled Pages are the primary navigation surface.
2. **Reading Room** — a document-first editorial reader where cited Q&A appears
   as marginalia beside the relevant source text.
3. **Console** — a keyboard-driven multi-pane operator workspace for ingest,
   sync, proposal review, and diagnostics.

Each study exposed a useful part of Lumio's product model, but none should
become the whole application. Constellation is powerful for explaining
relationships, but graph-first navigation conflicts with the Reader role's need
to ask a question immediately. Reading Room makes evidence inspection feel
trustworthy and humane, but document-first navigation makes chat feel secondary.
Console fits Maintainer and Owner workflows, but would overcomplicate the Reader
experience if used as the default shell.

Lumio therefore needs a unified role-based interaction model that preserves the
best parts of all three studies without turning the web app into three separate
products.

## Decision

1. **Use one unified product shell with differentiated content models.** Lumio's
   web UI keeps a shared product spine: common navigation, role awareness, and a
   unified design system. Reader, Maintainer, and Owner workflows may use
   different content layouts inside that shell, but role changes must not feel
   like switching products.

2. **Make the Reader entrypoint the Chat + Citation Workspace.** The Reader's
   default surface is a classic chat interface paired with first-class evidence
   context. Chat is the main entrypoint, but not a generic chatbot: answers must
   keep Citations, Compiled Pages, and Retrieval Trace access close to the
   conversation.

3. **Open Citations into inline Reading Room states.** Selecting a Citation
   promotes the relevant Compiled Page into the workspace instead of treating the
   Citation as a detached link. The Reading Room state keeps the chat thread
   available, highlights cited line ranges, and supports page- or
   passage-grounded follow-up questions.

4. **Use Constellation as a contextual relationship lens.** Constellation is not
   the Reader home. Reader graph views are seeded from an answer, Citation, or
   Compiled Page. Maintainer and Owner graph views may add lifecycle,
   visibility, Relationship, Index Freshness, and Ingest Proposal impact
   overlays.

5. **Use a Progressive Console model for Maintainer workflows.** The Workshop is
   approachable by default: proposal review, validation results, and publish
   decisions should remain readable without requiring command syntax. Console
   affordances such as panes, operation logs, status lines, and a command palette
   appear when they help with ingest, validation, sync, proposal review, or power
   navigation.

6. **Use calm administration with Operational Disclosure for Owner workflows.**
   Owner surfaces default to clear settings and administration workflows for
   users, storage, auth, model provider, Write Mode, secrets, and health.
   Deeper operational affordances appear only for audit, diagnostics, risky
   changes, or troubleshooting.

7. **Keep one visual system with contextual accents.** Lumio's base visual
   identity remains unified. Reading Room contributes editorial calm and
   marginalia patterns; Console contributes dense operational affordances and
   status treatments; Constellation contributes spatial relationship and graph
   glow treatments. These are contextual accents, not separate themes.

This ADR is binding for Lumio's role-level interaction model and design
direction. It is not binding for exact component geometry, pane widths,
breakpoints, route names, or whether a graph/detail state appears as an overlay,
split pane, or full route. Future UI work may adapt implementation details, but
must preserve the chat-first Reader entry, inline evidence inspection,
contextual relationship lens, Progressive Console Maintainer model, calm Owner
administration, and unified visual system unless this ADR is superseded.

## Considered Options

- **Choose Constellation as the primary app model** — rejected: graph-first
  navigation is memorable and useful for exploration, but it asks Readers to
  understand the Knowledge Base before asking their first question.
- **Choose Reading Room as the primary app model** — rejected: document-first
  reading is the right evidence-inspection model, but Reader entry must remain
  chat-first.
- **Choose Console as the primary app model** — rejected: the operator workspace
  fits Maintainer and Owner workflows, but it would make the Reader experience
  unnecessarily technical.
- **Keep one generic app shell for every role** — rejected: a single flat layout
  would be simpler to implement, but it would ignore the different jobs of
  Readers, Maintainers, and Owners.
- **Create three completely separate role shells** — rejected: separate shells
  would preserve each concept's character, but would fragment Lumio into
  multiple products and increase implementation and maintenance cost.

## Consequences

Future UI work should treat chat, evidence inspection, relationship exploration,
proposal review, and administration as distinct but connected states of one
product. The Reader path should be optimized first around asking, reading cited
answers, inspecting Citations, and following Compiled Pages. Maintainer work can
then deepen around Workshop and Progressive Console states. Owner work should
remain calm by default and reveal operational depth only when the task requires
it.

The terms **Chat + Citation Workspace**, **Reading Room**, **Constellation**,
**Workshop**, **Progressive Console**, and **Operational Disclosure** are part of
Lumio's canonical UI vocabulary and are recorded in `CONTEXT.md`.

The cost of this decision is additional interaction design discipline. The app
must stay unified while still giving each role the right surface. Graph effects,
console density, and editorial reading treatments must remain contextual and
purposeful rather than becoming competing themes.
