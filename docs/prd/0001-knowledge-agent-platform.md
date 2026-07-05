# PRD-0001: Lumio Knowledge Agent Platform

_Status: approved. Originates from the 2026-07-03 design spec and the 2026-07-04 stack-agnostic MVP plan. Stack decisions live in ADR-0001 and ADR-0002; ubiquitous language in `CONTEXT.md`._

## Problem Statement

Teams with curated, confidential knowledge need a chat experience that answers from a trusted, compiled knowledge base — citing the pages it used, refusing what it cannot support, and keeping the knowledge base usable outside the chat app. Existing options force a trade-off: multi-tenant SaaS (knowledge leaves your control), chunk-first RAG (loses provenance and citeability), or wiring an agent framework directly to a vector store (the framework owns retrieval and citation policy). Lumio's users also want local coding agents to read the same knowledge the chat app serves, without the browser app becoming the only place that knowledge exists.

## Solution

A single-tenant, deployable modular monolith that serves a compiled Markdown knowledge base over a browser chat and admin UI, backed by a reusable, framework-independent Core SDK. The Agent Runtime retrieves whole-page evidence from the Core SDK before synthesizing a cited answer, refuses unsupported claims, and exposes a retrieval trace. A Chat Gateway enforces auth, roles, and guardrails so no client — including external chat UIs — can bypass retrieval and citation. The compiled wiki stays a portable Markdown artifact in Git or shared storage; every published version can be exported or synced so local agents and humans use it independently.

## User Stories

1. As a **Reader**, I want to ask a factual question and receive an answer with citations to the pages that support it, so that I can trust and verify the response.
2. As a **Reader**, I want to see which pages were used and why they were selected, so that I can judge coverage for myself.
3. As a **Reader**, I want an unsupported question to be answered with "not covered by this knowledge base" rather than a fabricated answer, so that I am not misled.
4. As a **Reader**, I want to ask a multi-hop relationship question and get a traced path through the graph, so that I can understand how concepts connect.
5. As a **Reader**, I want to browse the published wiki directly, so that I can read without the chat intermediary.
6. As a **Reader**, I want to use an external chat UI (Open WebUI, LibreChat) against Lumio and get the same citations and guardrails, so that my preferred client does not weaken correctness.
7. As a **Maintainer**, I want to upload a raw source and receive a staged Markdown proposal, so that I can review before the knowledge base changes.
8. As a **Maintainer**, I want to see a diff of proposed changes, source provenance, and affected pages, so that I can evaluate a proposal's blast radius.
9. As a **Maintainer**, I want validation to run before publish and report failures with file and field, so that broken pages never reach readers.
10. As a **Maintainer**, I want to approve a proposal to publish or discard it, so that only reviewed knowledge becomes canonical.
11. As a **Maintainer**, I want a direct-write mode for trusted demos that is clearly flagged in the UI, so that I can move fast when risk is acceptable without losing the signal.
12. As a **Maintainer**, I want to sync from Git and see the local index refresh, so that the app reflects the canonical wiki.
13. As an **Owner**, I want first-run owner-account creation, so that I can bootstrap a fresh deployment safely.
14. As an **Owner**, I want to configure an OpenAI-compatible model provider without committing secrets, so that credentials stay out of the repo.
15. As an **Owner**, I want to choose Git canonical, shared-storage canonical, or hybrid sync, so that the deployment fits my environment.
16. As an **Owner**, I want to set the write mode and manage users and roles, so that I control who can change knowledge.
17. As an **Owner**, I want an audit log of ingest, publish, auth, sync, and config changes (never secrets), so that I can investigate incidents.
18. As an **Operator**, I want a Docker-first single-app deployment, so that I can run Lumio with minimal infrastructure.
19. As an **Operator**, I want a deployment smoke test (start → create owner → configure provider → load sample wiki → ask one cited question → run one ingest → publish → verify freshness), so that I can prove the MVP loop works.
20. As a **local coding agent**, I want to clone or pull the published wiki and read the same Markdown the web app serves, so that my reasoning is grounded in the same source of truth.
21. As any user, I want prompt injection embedded in a raw source or wiki page to fail to change Lumio's system behavior, so that I am protected from malicious content.
22. As any user, I want raw sources excluded from public compiled-wiki exports, so that unpublished material never leaks.

## Implementation Decisions

- **SDK-centered modular monolith.** One deployable app contains the browser UI, API, Chat Gateway, Agent Runtime, background worker, auth/roles, the Core SDK index, ingest/review workflows, and the storage/sync/publish module. The Core SDK owns domain logic; all clients cross the same SDK seam. (Architectural shape; framework specifics in ADR-0001.)
- **Compiled-wiki-first retrieval, not chunk-first RAG.** The Agent Runtime classifies the question, retrieves whole-page or section Evidence from the Core SDK, synthesizes a cited answer, exposes a Retrieval Trace, and refuses unsupported or out-of-scope claims. MVP retrieval is lexical/frontmatter/graph; no vector database or embedding service is required.
- **Chat Gateway as the single enforced boundary.** Native web chat and a future OpenAI-compatible endpoint both pass through the Gateway, which applies auth, roles, and guardrails. No client reaches the Agent Runtime directly.
- **Storage modes.** Git canonical (preferred), shared-storage canonical (when Git is unavailable), or hybrid (Git canonical, shared storage mirrors bundles). Invariant: the browser app is never the only place knowledge exists; every published version is exportable as Markdown.
- **Write modes.** Proposal-first is the default (stage → validate → review → publish). Direct-write is an Owner-enabled, UI-flagged alternative; validation still runs before publish unless explicitly overridden.
- **Roles.** Reader, Maintainer/Admin, Owner — with the capability split defined in `CONTEXT.md`. Enterprise SSO/OIDC/SAML are future adapters, not MVP dependencies.
- **Future Connector seam.** Retrieval results already allow non-Markdown Evidence, so a future Connector can expose tables, databases, or data lakes without changing the agent or client contracts. Structured query execution is explicitly post-MVP (the DuckDB seam in ADR-0001).
- **Semantic search is post-MVP.** Provider boundaries are defined now so a native hybrid/vector layer can be added without changing agent or client contracts.

## Testing Decisions

- **Test only at pre-agreed seams.** Each module is tested through its public interface, never its internals. The Core SDK's public seam is the same surface every client uses.
- **Agent evals use a fake model provider**, never a live LLM call. Evals cover factual lookup, procedure/how-to, relationship/path, synthesis/comparison, unknown ("not covered"), and prompt-injection attempts in source text.
- **Storage tests** cover Git-only, shared-storage-only, hybrid, failed pull/publish handling, and stale-index detection after sync.
- **Write-workflow tests** cover proposal-first staging, direct-write publish, validation-blocks-publish, raw-source exclusion from exports, and role restrictions.
- **Role tests** prove Reader, Maintainer, and Owner can each do only their capabilities.
- **Prior art:** none — greenfield. Tests establish the baseline.

## Out of Scope

- Multi-tenant SaaS or a microservice deployment architecture.
- A required vector database or embedding service in the MVP.
- A QMD dependency in the MVP.
- Enterprise SSO/oidc/SAML as a first requirement.
- The browser app being the only place knowledge exists.
- Structured dataset query execution (Connector and Dataset are defined as future seams only).

## Further Notes

- Bruno is the reference implementation and demo corpus; the product is generic — bring your own compiled wiki.
- The MVP targets single-tenant deployability and correctness before horizontal scalability.
- Open principles: Markdown is the durable source of truth; indexes are derived; the web app is one client, not the owner of knowledge; local coding-agent use is first-class.
