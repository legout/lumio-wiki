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

## Contents

- [Quick start](#quick-start)
  - [Operator / Owner — deploy with Docker](#operator--owner--deploy-with-docker)
  - [Reader / Maintainer — use the web app](#reader--maintainer--use-the-web-app)
  - [Developer — run locally](#developer--run-locally)
- [Configuration](#configuration)
- [CLI reference](#cli-reference)
- [Knowledge Base format](#knowledge-base-format)
- [HTTP surface](#http-surface)
- [Deployment notes](#deployment-notes)
- [Development](#development)
- [What works today](#what-works-today)
- [Roadmap](#roadmap)
- [Troubleshooting](#troubleshooting)

## Quick start

Lumio has three audiences. Pick yours.

- **Operator / Owner** — deploy and configure the app (Docker-first).
- **Reader / Maintainer** — ask questions, review ingest, publish (web UI).
- **Developer** — extend the Core SDK, runtime, or app.

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
   (Readers, Maintainers, or additional Owners).
3. **Reader** → `/chat`: ask a question, get a cited answer, and inspect the
   retrieval trace.
4. **Maintainer** → `/ingest`: upload a raw Knowledge Source, review the staged
   Markdown proposal, then publish or discard. Direct-write is an
   Owner-enabled alternative.
5. Any Reader can export the compiled wiki at `/kb/export` (raw sources are
   excluded).

### Developer — run locally

Requirements: **Python ≥ 3.14** and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # install dependencies + the package
uv run lumio validate tests/fixtures/valid   # smoke-test the SDK against the sample KB
uv run stario serve lumio.app:bootstrap      # serve the web app on :8000
```

See [Development](#development) for tests, linting, and codebase layout.

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

### Storage, paths, and metadata

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

Stario itself respects `STARIO_HOST` (defaults `0.0.0.0` in the image) and
`STARIO_PORT` (defaults `8000`).

## CLI reference

`uv run lumio <command>` (or just `lumio` once installed).

```bash
lumio --version
lumio validate <kb-path>                          # exit 0 if valid, 1 otherwise
lumio retrieve <kb-path> "<query>" [--limit N]    # lexical/frontmatter/graph retrieval
lumio ask <kb-path> "<question>"                  # cited answer via the Agent Runtime
lumio sync <git-source> "<query>" --working-dir <dir> [--mode git|shared|hybrid] [--ref REF]
```

All commands work offline against any valid KB; `ask` uses the FakeProvider
when no provider is configured.

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

## HTTP surface

All routes except `/health` and first-run `/setup` require authentication.
Role gates are enforced as middleware.

| Route | Role | Purpose |
|---|---|---|
| `GET /health` | _(none)_ | Liveness probe. |
| `GET/POST /setup` | _(first-run only)_ | Create the Owner account; disabled once one exists. |
| `GET/POST /login` · `POST /logout` | _(auth)_ | Session login / logout. |
| `GET/POST /chat` · `POST /chat/ask` | Reader | Chat UI + cited answer with retrieval trace. |
| `GET /kb/export` | Reader | Export the compiled wiki as a Markdown bundle (raw sources excluded). |
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
uv run pytest -q                     # test suite
uv run ruff check .                  # lint
uv run lumio validate tests/fixtures/valid   # sanity check the sample KB
```

Testing philosophy (from PRD-0001): tests exercise modules **only through their
public seams**; the Core SDK's public surface is the same one every client
uses; agent/runtime evals use the **FakeProvider**, never a live LLM call.

### Codebase layout

```
src/lumio/
├── core/            # Core SDK (framework-independent): records, knowledge_base, index
├── runtime.py       # Agent Runtime: classify → retrieve → synthesize → cite → refuse → trace
├── app.py           # Chat Gateway + web UI (Stario routes, auth, guardrails)
├── guardrails.py    # Citation-required / not-covered / out-of-scope enforcement
├── providers/       # OpenAI-compatible provider + offline FakeProvider
├── storage/         # git / shared / hybrid sync + publish
├── ingest.py        # Knowledge Source → staged Markdown proposal
├── publish.py       # Publish workflow + version records + freshness rebuild
├── auth.py · auth_models.py      # roles (Reader/Maintainer/Owner), sessions
├── audit.py · audit_models.py    # audit log (ingest/publish/auth/sync/config)
├── config.py        # owner provider configuration (env → ProviderConfig)
└── cli.py           # `lumio` CLI: validate / retrieve / ask / sync
```

The Stario/Piccolo-specific code stays inside the app boundary; the Core SDK
remains framework-independent (ADR-0001, ADR-0002).

## What works today

- **Core SDK** — load, validate, build lexical/frontmatter/graph index,
  retrieve with citations + retrieval trace, source-fingerprint freshness,
  Markdown export.
- **Agent Runtime** — question classification, evidence retrieval, cited
  synthesis, refusal of unsupported claims, trace exposure.
- **Chat Gateway + web UI** — first-run owner setup, login/logout, Reader chat,
  Maintainer ingest (upload → proposal → review → publish/discard, plus
  direct-write), Owner admin (users/roles, write-mode, storage-mode, audit),
  KB export.
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
