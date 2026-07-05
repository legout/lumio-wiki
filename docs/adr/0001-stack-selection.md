---
status: accepted
amends: ADR-0002 amends the agent-runtime and struct-validation rows
---

# ADR-0001: Lumio MVP Technology Stack

## Context

Lumio is a deployable, single-tenant chat platform for trusted knowledge and data. The MVP starts with compiled Markdown knowledge bases, browser chat, admin/review workflows, storage/sync/publish, and a reusable Core SDK, while leaving room for future structured-data chat. The stack must be Python-first and hypermedia-style (no separate SPA), keep the compiled wiki portable, support lexical retrieval first with a clean path to semantic, and keep any agent framework from owning Lumio's retrieval, citation, or guardrail architecture.

## Decision

Use a Python-first hypermedia modular monolith:

| Concern | Choice |
|---|---|
| App framework | Stario |
| Frontend interactivity | Datastar |
| App metadata database | SQLite |
| App metadata ORM and migrations | Piccolo |
| Retrieval index | LanceDB |
| PDF / scanned-PDF / image OCR ingestion | LiteParse |
| Broad document conversion | MarkItDown |
| Future structured analytics | DuckDB |
| Agent runtime layer | _amended by ADR-0002_ |
| Struct and validation layer | _added by ADR-0002_ |
| Packaging and dependency management | uv |
| Testing | pytest |
| Linting and formatting | ruff |
| Deployment packaging | Docker-first single app |

Stario + Datastar give one Python codebase with server-rendered HTML and the SSE/reactive model chat, ingest progress, diff review, and sync status need — no SPA. SQLite + Piccolo keep single-tenant metadata low-ceremony. LanceDB covers BM25/full-text now and vector/hybrid later, avoiding a SQLite-FTS-then-migrate path. LiteParse and MarkItDown split specialized PDF/OCR from broad conversion. DuckDB is an explicit future seam, not an MVP dependency.

Stario/Piccolo-specific code stays inside the app/web boundary; the Core SDK remains framework-independent. LanceDB stays behind a retrieval-index seam. See ADR-0002 for the runtime and struct layer.

## Considered Options

- **FastAPI / Starlette directly** — mature ASGI, works with Datastar, but Stario better matches the Python-first hypermedia style and ships higher-level HTML/Datastar ergonomics.
- **Django** — mature auth/admin/ORM/migrations, but heavier than the hypermedia-native modular monolith the MVP wants.
- **SQLite FTS for lexical retrieval first** — fewer dependencies, but LanceDB covers BM25 now and hybrid/vector later, so a FTS-first choice pays a migration cost later.
- **SQLModel + Alembic** — aligns with Pydantic and SQLAlchemy, but more moving parts than Piccolo for single-tenant SQLite metadata.
- **SQLAlchemy directly** — broadest and most flexible, but Lumio doesn't need its advanced patterns for the metadata layer.

## Consequences

One Python codebase delivers app, UI, API, runtime, and admin. Hypermedia UI avoids SPA complexity. Retrieval is future-proofed for lexical, vector, and hybrid. SQLite keeps deployment simple.

Trade-off: Stario and Piccolo are less widely adopted than FastAPI/SQLAlchemy, and LanceDB enters the MVP dependency footprint before semantic search is strictly needed. LiteParse and MarkItDown need deterministic routing rules. These are accepted; the seam discipline above contains the blast radius of any later swap.
