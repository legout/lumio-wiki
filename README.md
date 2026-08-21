# Lumio

> Deployable chat for trusted knowledge and data. A single-tenant, deployable
> browser agent platform that answers questions over a compiled Markdown
> knowledge base — citing the pages it used, refusing what it cannot support,
> and keeping the wiki portable outside the web app.

**Status:** Lumio is **pre-1.0 MVP software**. Everything listed under
[What works today](#what-works-today) is functional, but it is built for
**single-tenant, trusted-network** deployments and is **not yet hardened for
untrusted-network exposure**. Treat the public-facing chat and the
OpenAI-compatible endpoint as trusted-network surfaces until hardened.

For the *why* and the vision, read the domain docs — this README documents
**what runs today**:

- [`CONTEXT.md`](CONTEXT.md) — ubiquitous language (glossary).
- [`docs/prd/0001-knowledge-agent-platform.md`](docs/prd/0001-knowledge-agent-platform.md) — platform PRD.
- [`docs/prd/0002-core-sdk.md`](docs/prd/0002-core-sdk.md) — Core SDK PRD.
- [`docs/adr/`](docs/adr/) — architectural decisions.
- [`docs/kb-format.md`](docs/kb-format.md) — canonical Compiled Page frontmatter reference.
- [`docs/chat-sources.md`](docs/chat-sources.md) — private document chat (Chat Sources): retention, scope, submission, promotion, limits.

## Contents

- [Quick start](#quick-start)
  - [Operator / Owner — deploy with Docker](#operator--owner--deploy-with-docker)
  - [Reader / Maintainer — use the web app](#reader--maintainer--use-the-web-app)
  - [Developer — run locally](#developer--run-locally)
  - [Coding agent / library user — install the portable foundation](#coding-agent--library-user--install-the-portable-foundation)
  - [Optional capabilities](#optional-capabilities)
  - [Temporary Core SDK compatibility](#temporary-core-sdk-compatibility)
- [Configuration](#configuration)
- [CLI reference](#cli-reference)
  - [`lumio-wiki` — portable Knowledge Base CLI](#lumio-wiki--portable-knowledge-base-cli)
  - [`lumio` — application CLI](#lumio--application-cli)
- [Knowledge Base format](#knowledge-base-format)
- [HTTP surface](#http-surface)
- [Deployment notes](#deployment-notes)
- [Development](#development)
- [Packaging, ownership, and migration](#packaging-ownership-and-migration)
- [What works today](#what-works-today)
- [Roadmap](#roadmap)
- [Usage guide](docs/usage.md)
- [Troubleshooting](#troubleshooting)

## Quick start

Lumio has four audiences. Pick yours.

- **Operator / Owner** — deploy and configure the app (Docker-first).
- **Reader / Maintainer** — ask questions, review ingest, publish (web UI).
- **Developer** — extend the Core SDK, runtime, or app.
- **Coding agent / library user** — install the portable Knowledge Base foundation.

### Operator / Owner — deploy with Docker

Lumio ships as a single Docker image running [Stario](https://stario.dev) on
port `8000`. The image contains the app only — **it does not contain a
knowledge base**, so a bare `docker run` with no configuration will crash on
boot (see [Troubleshooting](#troubleshooting)). Point it at a canonical
Knowledge Base in Git, the PRD-preferred storage mode:

```bash
docker build -t lumio .

docker run -p 8000:8000 \
  -e LUMIO_KB_PATH=/data/kb \
  -e LUMIO_STORAGE_MODE=git \
  -e LUMIO_GIT_SOURCE=https://github.com/you/your-compiled-wiki.git \
  -e LUMIO_PROVIDER_BASE_URL=http://host.docker.internal:11434/v1 \
  -e LUMIO_PROVIDER_MODEL=llama3 \
  -e LUMIO_PROVIDER_API_KEY=ignored \
  lumio
```

On boot, Lumio pulls the wiki from Git, validates it, and builds the retrieval
index. Then open `http://localhost:8000/setup` to create the **Owner** account
(first-run only — setup is disabled once an Owner exists). See
[Deployment notes](#deployment-notes) for storage modes, persistence, and
secrets.

> No model provider configured? Lumio falls back to a deterministic offline
> **FakeProvider** so the app and CLI keep working for development and CI. Real
> answers require an OpenAI-compatible provider (Ollama, vLLM, OpenAI, etc.).

### Reader / Maintainer — use the web app

Once an Operator has deployed Lumio (above) or you are running locally:

1. Browse to `http://localhost:8000/setup` (first run) to create the Owner.
2. `/login` as the Owner, then create users under `/admin/users`
   (Readers, Maintainers, or additional Owners). Readers can also
   self-register at `/register` (each account is created as a Reader).
3. **Reader** → `/chat` (Chat + Citation Workspace): ask a question, get a
   cited answer, inspect the retrieval trace, and open any Citation into the
   Reading Room — a persistent evidence surface beside the chat (or a
   responsive sheet over it on narrower displays).
4. **Reader** → `/kb` (Reading Room): browse and deterministically search
   published Compiled Pages, then read a selected page full-width without chat.
5. **Maintainer** → `/ingest`: upload a raw Knowledge Source, review the staged
   Markdown proposal, then publish or discard. Direct-write is an
   Owner-enabled alternative.
6. Any Reader can export the compiled wiki at `/kb/export` (raw sources are
   excluded).

### Developer — run locally

Requirements: **Python ≥ 3.14** and [uv](https://docs.astral.sh/uv/).
```bash
uv sync                       # install dependencies + the package
uv run lumio validate tests/fixtures/valid   # smoke-test the SDK against the sample KB
uv run lumio serve --port 8000               # serve the web app (Chat, Reading Room, Owner workspace)
```

See [Development](#development) for tests, linting, and codebase layout.

### Coding agent / library user — install the portable foundation

Lumio is published as three progressively enhanced wheels (ADR-0010). Pick
the one that matches your use case; every wheel is independently installable
and the base never pulls in the web app, a vector database, or a model
provider.

| Distribution | Installs | Use when |
|---|---|---|
| `lumio-wiki` | `msgspec[yaml]`, `msgpack`, plus the `lumio-wiki` CLI and packaged Agent Skill | You want a portable, model-free Knowledge Base from any coding agent (Pi, Hermes, Codex, Claude Code, …). |
| `lumio-lancedb` | `lumio-wiki` + LanceDB + PyArrow | You want BM25 / semantic / hybrid retrieval over the same Knowledge Base. |
| `lumio` | `lumio-wiki` + `lumio-lancedb` + the full Stario web app, providers, storage, auth | You want the deployable chat application. |

```bash
pip install lumio-wiki                       # foundation: load, validate, search, traverse, ingest, publish
pip install 'lumio-wiki[documents]'          # + LiteParse/MarkItDown/AnyDoc for PDF/image/office/HTML ingestion
pip install 'lumio-wiki[llm]'                # + an unattended OpenAI-compatible Distiller
pip install 'lumio-wiki[all]'                # documents + llm together (still no LanceDB)
pip install lumio-lancedb                    # + enhanced BM25/semantic/hybrid retrieval
pip install lumio                            # the full deployable web application
```

The base wheel depends only on `msgspec[yaml]` and `msgpack`; it does not
install the web application, LanceDB/PyArrow, Stario/Piccolo, OpenAI,
LiteParse, MarkItDown, or AnyDoc. Optional capabilities are described under
Optional capabilities below; the full packaging, ownership, and migration
contract lives in `docs/packaging.md`.

```bash
python -c "from lumio_wiki import load_knowledge_base; print(load_knowledge_base('tests/fixtures/valid')[1])"
lumio-wiki init ./my-kb                      # scaffold a categorized Knowledge Base
lumio-wiki doctor                            # report the install shape and packaged skill location
```

### Optional capabilities

`lumio-wiki` ships capability **extras** so heavyweight implementations are
pulled in only when needed. Each extra fails with actionable guidance naming
the exact install command when invoked without it.

| Extra | Brings | Capability |
|---|---|---|
| `lumio-wiki[documents]` | LiteParse, MarkItDown, AnyDoc | PDF / scanned-PDF / image (LiteParse, with OCR + page numbers), office formats — Word/PowerPoint/Excel/OpenDocument/RTF/EPUB/CSV (AnyDoc), HTML and broad formats (MarkItDown). The base wheel handles text and Markdown. |
| `lumio-wiki[llm]` | `openai` | Unattended Distiller backed by an OpenAI-compatible provider. The base wheel uses the host coding agent as the Distiller. |
| `lumio-wiki[all]` | both of the above | Document conversion + unattended distillation together. Still LanceDB-free. |
| `lumio-lancedb` | LanceDB, PyArrow | BM25 / vector / semantic / hybrid retrieval. `lumio-wiki` never imports it; install it explicitly when you want enhanced ranking. |
| `lumio-lancedb[embeddings]` | `sentence-transformers` | Local embeddings for the LanceDB adapter. Torch stays out of the base adapter and out of `lumio-wiki`. |
| `lumio[semantic]` | `sentence-transformers` | Local embeddings for the full app's LanceDB backend. |

**Adapter selection.** The full `lumio` application selects zero-index or
LanceDB retrieval through `LUMIO_RETRIEVAL_BACKEND` and dependency injection.
Client modules (`app.py`, `cli.py`) call the single public seam
`build_retrieval_index` and never import adapter types, so removing
`lumio-lancedb` does not require a code change — only flipping the config.

### Temporary Core SDK compatibility

Canonical Knowledge Base behavior now lives under `lumio_wiki`. Existing
`lumio.core` and `lumio.core.<module>` imports are temporary pre-1.0 re-exports
so external consumers continue to work during migration. Internal `lumio`
application modules and the test suite import final package owners
(`lumio_wiki`, `lumio_lancedb`) directly. New consumers must import from
`lumio_wiki` (and `lumio_lancedb` when enhanced retrieval is required). The
compatibility surface owns no duplicate implementation. Removal is
time-bounded to pre-1.0 and will ship only through a separately documented
migration issue that replaces the re-exports with explicit migration guidance.
See `docs/packaging.md` for the full migration and ownership contract.

## Configuration

Lumio is configured entirely through environment variables. Secrets
(provider keys) are read from the environment only — they are never committed
or logged (`.env` is gitignored).

### Provider (model)

| Variable | Required | Purpose |
|---|---|---|
| `LUMIO_PROVIDER_BASE_URL` | no | OpenAI-compatible API base URL (e.g. `http://localhost:11434/v1` for Ollama). |
| `LUMIO_PROVIDER_MODEL` | no | Model name (e.g. `llama3`). |
| `LUMIO_PROVIDER_API_KEY` | no | API key. Leave empty for keyless local servers (a dummy value also works). |

If `LUMIO_PROVIDER_BASE_URL` or `LUMIO_PROVIDER_MODEL` is unset, Lumio uses the
offline FakeProvider.

### Storage, paths, retrieval, and metadata

| Variable | Default | Purpose |
|---|---|---|
| `LUMIO_KB_PATH` | `tests/fixtures/valid` | Path to the compiled Knowledge Base the app loads. |
| `LUMIO_STORAGE_MODE` | `git` | `git`, `shared`, or `hybrid`. |
| `LUMIO_GIT_SOURCE` | _(none)_ | Git URL for the canonical KB (used by `git` and `hybrid`). |
| `LUMIO_SHARED_SOURCE` | _(none)_ | Shared-storage path (used by `shared` and `hybrid`). |
| `LUMIO_CONFIG_PATH` | `data/config` | Runtime config: storage-mode and write-mode state. |
| `LUMIO_INGEST_PATH` | `data/ingest` | Staged ingest proposals and uploaded raw sources. |
| `LUMIO_PUBLISH_PATH` | `data/publish` | Published-version records. |
| `LUMIO_METADATA_DB_PATH` | `lumio.sqlite` | SQLite database for users, roles, sessions, and audit. |
| `LUMIO_RETRIEVAL_BACKEND` | `lancedb` | `lancedb` (enhanced BM25/semantic/hybrid via `lumio-lancedb`) or `zero-index` (deterministic in-memory retrieval owned by `lumio-wiki`). Selected through config/DI; client modules never import adapter types. |

Stario itself respects `STARIO_HOST` (defaults `0.0.0.0` in the image) and
`STARIO_PORT` (defaults `8000`).

## CLI reference

Lumio ships two CLIs. `lumio-wiki` is the portable, model-free Knowledge Base
toolkit (installable on its own); `lumio` is the application CLI that adds
cited answers, sync, and the web server.

### `lumio-wiki` — portable Knowledge Base CLI

`uv run lumio-wiki <command>` (or just `lumio-wiki` once installed). Works
from any coding agent with no model provider and no LanceDB.

```bash
lumio-wiki --version
lumio-wiki init <path>                           # scaffold a categorized Knowledge Base
lumio-wiki validate <kb-path>                    # exit 0 if valid, 1 otherwise
lumio-wiki search <kb-path> "<query>"            # lexical search over titles, aliases, tags, summaries, bodies
lumio-wiki page <kb-path> "<title>"              # read a Compiled Page by Canonical Title or alias
lumio-wiki related <kb-path> "<title>"           # list pages related to a title
lumio-wiki paths <kb-path> "<src>" "<dst>"       # shortest typed path between two titles
lumio-wiki ingest <kb-path> <source> [--distiller passthrough|llm]   # stage a Knowledge Source as a proposal
lumio-wiki proposal list|inspect|validate <kb-path> [id]      # review staged proposals
lumio-wiki publish <kb-path> <id>                # publish a reviewed proposal
lumio-wiki source inspect <kb> --source-id <id> [--published-version <v>]  # secret-free metadata for the exact bound Source Version
lumio-wiki source fetch <kb> --source-id <id> --output <path>  # byte-exact original Source Artifact, digest re-verified
lumio-wiki source link <kb> --source-id <id> [--expires 5m]   # short-lived signed GET URL (max 1h; bearer secret)
lumio-wiki health <kb-path>                      # Knowledge Base health and diagnostics
lumio-wiki doctor                                # install shape: version, optionals, packaged skill location
lumio-wiki skill [--install|--path]              # locate or install the packaged Agent Skill
```

`ingest --distiller llm` requires `lumio-wiki[llm]`; PDF/DOCX/image sources
require `lumio-wiki[documents]`. Both fail with actionable guidance if the
extra is missing.

### `lumio` — application CLI

`uv run lumio <command>` (or just `lumio` once installed). Adds the Agent
Runtime, storage sync, and the web server on top of `lumio-wiki`.

```bash
lumio --version
lumio validate <kb-path>                          # exit 0 if valid, 1 otherwise
lumio retrieve <kb-path> "<query>" [--limit N]    # lexical/frontmatter/graph retrieval
lumio ask <kb-path> "<question>"                  # cited answer via the Agent Runtime
lumio sync <source> "<query>" --working-dir <path>  # sync a storage source, then retrieve
lumio serve [--host HOST] [--port PORT]          # run the web app (Chat /chat, Reading Room /kb, Owner /admin)
```

All commands work offline against any valid KB; `ask` uses the FakeProvider
when no provider is configured.

In a checked-out repository, prefix any command with `uv run`, for example
`uv run lumio sync <source> "<query>" --working-dir <path>`.

## Knowledge Base format

A Knowledge Base is a directory of compiled Markdown pages (YAML frontmatter +
body). Minimal example:

```markdown
---
title: "Technology Stack"
tags: ["technology"]
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "stack-doc"
    title: "Stack decision"
---

# Technology Stack

Lumio is Python-first: Stario, SQLite, LanceDB, msgspec.
```

For the full field table (required vs optional, allowed `lifecycle` /
`visibility` values, the non-synthetic-needs-a-source rule, and cross-page
uniqueness/resolution rules), see **[`docs/kb-format.md`](docs/kb-format.md)**.

A categorized Knowledge Base additionally carries a versioned root Control File
(`lumio.yaml`) declaring its Content Category catalog and Maintainer-pinned Hot
Index titles, and a Published Version carries marked reserved artifacts:
regenerated Navigation Indexes (`index.md`) and Hot Index (`hot.md`), and an
append-only Activity Log (`log.md`). Knowledge Bases without a Control File
load in Legacy Flat Mode with a non-blocking migration warning. See
[`docs/kb-format.md`](docs/kb-format.md) and
[ADR-0008](docs/adr/0008-knowledge-base-control-file-and-published-artifacts.md).

## HTTP surface

All routes except `/health` and first-run `/setup` require authentication.
Role gates are enforced as middleware.

| Route | Role | Purpose |
|---|---|---|
| `GET /health` | _(none)_ | Liveness probe. |
| `GET/POST /setup` | _(first-run only)_ | Create the Owner account; disabled once one exists. |
| `GET/POST /login` · `GET/POST /register` · `POST /logout` | _(auth)_ | Session login / logout; self-service Reader registration (disabled until an Owner exists). |
| `GET/POST /chat` · `POST /chat/ask` · `GET /chat/reading-room` · `GET /chat/threads` · `GET /chat/threads/{id}` | Reader | Chat + Citation Workspace: cited answer with retrieval trace. Selecting a Citation opens the cited Compiled Page in the Reading Room column (or a responsive sheet on narrower displays); the page and cited range are URL-addressable and survive reload, Back/Forward, and shared deep links. |
| `GET /kb` · `GET /kb/page/{title}` · `GET /kb/export` | Reader | Reading Room: browse published Compiled Pages, deterministic lexical search (title, alias, tag, summary, body) with prev/next ranked-result navigation, full-width standalone document reading, and Markdown export (raw sources excluded). No chat composer or generated answer on the standalone surface. |
| `POST /v1/chat/completions` | Reader | OpenAI-compatible endpoint; same retrieval, citation, refusal, and guardrails as `/chat`. Returns a `lumio` extension block with citations, trace, and `covered`. |
| `POST /ingest` · `GET /ingest/proposals` · `GET /ingest/proposals/{id}` · `POST /ingest/proposals/{id}/{publish,discard}` · `POST /ingest/publish` · `GET /ingest/write-mode` | Maintainer | Ingest workflow: stage → review → publish (or direct-write). |
| `GET/POST /admin/users` · `POST /admin/write-mode` · `GET/POST /admin/storage-mode` · `GET /admin/audit` | Owner | User/role management, write-mode, storage-mode, audit log. |

## Deployment notes

**Storage modes** (see `LUMIO_STORAGE_MODE`):

- `git` _(default)_ — Git is the canonical source; Lumio pulls on boot and
  commits/pushes on publish.
- `shared` — a shared-storage path is canonical (when Git is unavailable).
- `hybrid` — Git is canonical; shared storage mirrors bundles.

Invariant: the web app is never the only place knowledge lives — every
published version is exportable as Markdown.

**Persistence in Docker.** The image holds no stateful volumes by default.
Users, sessions, audit log (`LUMIO_METADATA_DB_PATH`), config
(`LUMIO_CONFIG_PATH`), ingest proposals (`LUMIO_INGEST_PATH`), and published
versions (`LUMIO_PUBLISH_PATH`) live in the container filesystem and are lost
on restart unless you mount a volume, e.g. `-v lumio-data:/data` and point the
path variables at `/data/...`.

**Secrets.** Provider keys come from the environment only and are never logged.
Do not bake them into the image — pass them at `docker run` time or via your
orchestrator's secret mechanism.

## Development

```bash
uv sync                              # install (dev extras included)
uv lock --check                      # verify the workspace lockfile is current
uv build --package lumio-wiki --wheel  # build the portable foundation wheel
uv run pytest -q packages/lumio-wiki/tests  # test the portable foundation directly
uv run pytest -q -n 4                # full test suite (four parallel workers)
uv run pytest -q tests/test_chat.py  # focused tests stay serial
uv run ruff check .                  # lint
uv run lumio validate tests/fixtures/valid   # sanity check the sample KB
```

### Browser smoke

The real-browser smoke is a dev-environment check: `playwright` is installed
by `uv sync`. Run it through the project environment:

```bash
uv run playwright install chromium            # once, when no system Chromium is available
uv run python scripts/smoke_browser.py
```

The smoke prefers `LUMIO_SMOKE_CHROMIUM` or a discovered system Chromium; it
otherwise uses Playwright's bundled Chromium.

Testing philosophy (from PRD-0001): tests exercise modules **only through their
public seams**; the Core SDK's public surface is the same one every client
uses; agent/runtime evals use the **FakeProvider**, never a live LLM call.

### Codebase layout

```
packages/lumio-wiki/src/lumio_wiki/
│                    # Canonical portable Knowledge Base owner
packages/lumio-lancedb/src/lumio_lancedb/
│                    # Optional LanceDB enhanced-retrieval adapter
packages/lumio/src/lumio/
├── core/            # Temporary public compatibility re-exports only
├── retrieval.py     # App retrieval backend selection (config/DI)
├── runtime.py       # Agent Runtime: classify → retrieve → synthesize → cite → refuse → trace
├── app.py           # Chat Gateway + web UI (Stario routes, auth, guardrails)
├── providers/       # OpenAI-compatible provider + offline FakeProvider
├── storage/         # git / shared / hybrid sync + publish
├── ingest.py        # Full-app ingestion adapters over lumio-wiki
├── publish.py       # Publish workflow + version records + write mode
├── auth.py · auth_models.py      # roles (Reader/Maintainer/Owner), sessions
├── audit_models.py  # audit log (ingest/publish/auth/sync/config)
├── config.py        # owner provider configuration (env → ProviderConfig)
└── cli.py           # `lumio` CLI: validate / retrieve / ask / sync
```

The canonical `lumio_wiki` package remains framework-independent. The
Stario/Piccolo-specific code stays inside the app boundary. `lumio.core` is a
temporary public compatibility surface only; internal callers use final package
owners (ADR-0010, issue #103).

## Packaging, ownership, and migration

Lumio is a uv workspace with three independently buildable, independently
installable distributions (ADR-0010). The full contract — module ownership,
dependency direction, public compatibility strategy, optional capabilities,
and the migration plan for the temporary `lumio.core` re-exports — lives in
**[`docs/packaging.md`](docs/packaging.md)**.

Quick reference:

- **`lumio-wiki`** owns the canonical Knowledge Base model, loading,
  validation, Navigation/Hot Indexes, deterministic zero-index retrieval,
  typed Relationship traversal, Evidence/citations, ingestion interfaces,
  the proposal pipeline, the `lumio-wiki` CLI, and the packaged Agent Skill.
  It has no downward dependency on `lumio-lancedb` or `lumio`.
- **`lumio-lancedb`** depends only on `lumio-wiki`. It owns LanceDB index
  lifecycle, BM25, semantic/vector retrieval, and hybrid ranking, returning
  the same `RetrievalResult` contract as zero-index retrieval.
- **`lumio`** consumes both upward, selects zero-index or LanceDB retrieval
  through `LUMIO_RETRIEVAL_BACKEND` + dependency injection, and owns the
  Stario web app, the Agent Runtime, auth, storage, providers, and review
  workflows.

No two wheels own the same concrete Python module path
(`lumio_wiki`, `lumio_lancedb`, `lumio`). Published inter-member dependencies
declare bounded version ranges (`lumio-wiki>=0.1.1,<0.2.0`); the shared
lockfile is a development convenience, not a public compatibility contract.

## What works today

- **Core SDK** — load, validate, build lexical/frontmatter/graph index,
  retrieve with citations + retrieval trace, source-fingerprint freshness,
  Markdown export.
- **Agent Runtime** — question classification, evidence retrieval, cited
  synthesis, refusal of unsupported claims, trace exposure.
- **Chat Gateway + web UI** — first-run owner setup, login/logout, Reader chat,
  Maintainer ingest (upload → proposal → review → publish/discard, plus
  direct-write), Owner administration (users/roles, write-mode, storage-mode, audit),
  KB export.
- **Reading Room** — the unified Reader surface for trusted Compiled Pages, in
  three presentations that share one document renderer: a persistent chat-side
  evidence column (opens from a Citation, highlights the cited line range,
  survives later questions, and is URL-addressable so reload, Back/Forward,
  and shared deep links restore the same page and range), a focus-managed
  responsive sheet over chat when the content area cannot fit two readable
  columns, and a standalone chat-free destination at `/kb` for browsing,
  deterministic lexical search (title, alias, tag, summary, body), and
  full-width reading with ranked prev/next navigation. Auth and Visibility are
  enforced at every presentation; temporary Conversation Sources never appear
  there. Existing `/kb` and `/kb/page/{title}` links remain stable.
- **Private document chat** — Readers attach private text/Markdown/PDF/DOCX
  files to a chat session (add button or drag/drop), cite them as "This chat"
  alongside the Knowledge Base, choose retrieval scope, and submit a file for
  Maintainer review. See [`docs/chat-sources.md`](docs/chat-sources.md).
- **OpenAI-compatible endpoint** — `/v1/chat/completions` routed through the
  same `answer()` path (same citations, refusal, and guardrails as `/chat`).
- **Auth & roles** — Reader / Maintainer / Owner with session auth and
  route-level role gates.
- **Storage** — `git`, `shared`, and `hybrid` sync + publish with index
  rebuild on staleness.
- **Audit log** — ingest, publish, auth, sync, and config events (never
  secrets).
- **Provider layer** — OpenAI-compatible provider with offline FakeProvider
  fallback.
- **CLI** — `validate`, `retrieve`, `ask`, `sync`.
- **Guardrails** — domain claims require citation; unsupported questions return
  "not covered"; out-of-scope questions are rejected or narrowed.

## Roadmap

Approved seams, **not yet implemented** (see PRD-0001 "Out of Scope" and
"Future extension"):

- **Semantic / vector / hybrid retrieval** — provider boundary defined; native
  vector layer not built.
- **Connectors** — adapters exposing non-Markdown sources (tables, databases,
  data lakes). Retrieval results already allow non-Markdown evidence.
- **Datasets / structured query execution** — the DuckDB seam; post-MVP.
- **Enterprise SSO / OIDC / SAML** — future auth adapters; not an MVP
  dependency.
- **Multi-tenant** — the MVP is deliberately single-tenant.

## Troubleshooting

- **Container crash-loops on boot (`path does not exist` / invalid KB).** The
  image ships no knowledge base. Set `LUMIO_STORAGE_MODE` + `LUMIO_GIT_SOURCE`
  (or `LUMIO_SHARED_SOURCE`, or mount a valid KB at `LUMIO_KB_PATH`). Verify
  any KB before deploying with `uv run lumio validate <path>`.
- **Answers look deterministic / always refuse.** No provider is configured, so
  Lumio is using the FakeProvider. Set the three `LUMIO_PROVIDER_*` variables.
- **Provider errors / 502 from `/v1`.** The provider URL/model/key is wrong or
  the server is unreachable. Lumio returns an actionable error rather than
  crashing; check `LUMIO_PROVIDER_BASE_URL` and that the server is reachable
  from the container (`host.docker.internal` on Docker Desktop).
- **Lost users/sessions after restart.** State lives in the container
  filesystem. Mount a volume and point the path variables (see
  [Deployment notes](#deployment-notes)).
