# Lumio Usage Guide

How to use Lumio three ways: from the **CLI**, as a **Python library**, and
inside a **coding-agent harness** (opencode, Claude Code, Codex, etc.).

> Lumio ships as three progressively enhanced wheels. This guide uses
> `lumio-wiki` (the portable, model-free foundation) unless noted — it works
> everywhere with no model provider, no LanceDB, no web server.

| Distribution | Installs | This guide section |
|---|---|---|
| `lumio-wiki` | Knowledge Base model, CLI, validation, search, traversal, ingest | [CLI](#1-cli) + [Python library](#2-python-library) + [Coding agent](#3-coding-agent-harness) |
| `lumio-lancedb` | `lumio-wiki` + LanceDB BM25/semantic/hybrid retrieval | [Enhanced retrieval](#enhanced-retrieval-optional) |
| `lumio` | `lumio-wiki` + `lumio-lancedb` + the full Stario web app | [Web app](#web-app-deployment-optional) |

## Install

```bash
pip install lumio-wiki                       # foundation
pip install 'lumio-wiki[documents]'          # + PDF/DOCX/image ingestion
pip install 'lumio-wiki[llm]'                # + unattended OpenAI-compatible Distiller
pip install 'lumio-wiki[all]'                # documents + llm (still no LanceDB)
pip install 'lumio-wiki[s3]'                 # + S3-native KB Locations (ADR-0013)
pip install lumio-lancedb                    # + enhanced BM25/semantic/hybrid retrieval
pip install lumio                            # the full deployable web application
```

The base wheel depends only on `msgspec[yaml]` and `msgpack`. Verify your
install:

```bash
lumio-wiki --version         # lumio-wiki 0.1.1
lumio-wiki doctor            # version, detected extras, packaged skill path
```

---

## 1. CLI

Lumio ships two CLIs. `lumio-wiki` is the portable, model-free Knowledge Base
toolkit. `lumio` adds cited answers, storage sync, and the web server.

### One-command project setup

`lumio-wiki setup` wires a project directory so your agent harness knows where
the wiki lives and how to use it. It creates the Knowledge Base (or adopts an
existing one), writes `.env` with `LUMIO_KB_PATH`, writes/updates `AGENTS.md`
with the retrieval-ladder protocol, and optionally installs the packaged Agent
Skill.

```bash
cd my-project

# Existing wiki? Adopt it.
lumio-wiki setup ./wiki

# Brand-new project? Create a wiki and wire it up.
lumio-wiki setup ./wiki --agent claude-code --overwrite

# Skip the AGENTS.md section if you want to write it yourself.
lumio-wiki setup ./wiki --no-agents-md
```

After setup, `lumio-wiki` commands can omit the `<kb>` path because the CLI
reads `LUMIO_KB_PATH` from your environment or `.env`:

```bash
export LUMIO_KB_PATH=./wiki
# or source .env
lumio-wiki search "technology stack"
lumio-wiki page "Technology Stack"
```

### lumio-wiki — portable Knowledge Base CLI

Works from any environment with no model provider and no LanceDB. `<kb>` is
the Knowledge Base root directory (or an `s3://` URI with `[s3]` installed).
If omitted, the CLI falls back to the `LUMIO_KB_PATH` environment variable.

```bash
lumio-wiki setup <kb-path> [--agent <name>] [--no-agents-md]  # one-command project setup
lumio-wiki init <path>                       # scaffold a categorized Knowledge Base
lumio-wiki validate <kb>                     # exit 0 if valid, 1 otherwise
lumio-wiki search <kb> "<query>" [--limit N] # lexical search over titles, aliases, tags, summaries, bodies
lumio-wiki page <kb> "<title>"               # read a Compiled Page by Canonical Title or alias
lumio-wiki related <kb> "<title>"            # list pages related to a title
              [--scope canonical|discovery] [--direction outgoing|incoming|both]
              [--depth N] [--max-edges N] [--max-results N] [--trace]
lumio-wiki paths <kb> "<src>" "<dst>"        # shortest typed path between two titles
              [--scope canonical|discovery] [--max-depth N] [--trace]
lumio-wiki hot <kb>                          # Maintainer-pinned Hot Index (retrieval ladder step 0)
lumio-wiki index <kb> [dir]                  # generated Navigation Index (step 1)
lumio-wiki ingest <kb> <file> [--distiller passthrough|llm]   # stage a Knowledge Source as a proposal
lumio-wiki proposal list <kb>                # list staged proposals
lumio-wiki proposal inspect <kb> <id> [--json]   # review a proposal (metadata + diff)
lumio-wiki proposal validate <kb> <id>       # validate a proposal
lumio-wiki publish <kb> <id>                 # publish a reviewed proposal
lumio-wiki discard <kb> <id>                 # discard a proposal
lumio-wiki publish-s3 <kb> <dest> --version <v>  # publish immutable S3 Published Version
              [--expected-pointer-version <v>]    # (compare-and-swap guard)
lumio-wiki health <kb> [--rebuild]           # page counts, validation, Discovery Graph health
lumio-wiki doctor                            # install shape: version, optionals, skill location
lumio-wiki skill install --agent <name>      # install the Agent Skill into a coding agent
              # agents: pi, hermes, codex, claude-code
```

**Reading from S3.** Any read command (`validate`, `search`, `page`,
`related`, `paths`, `hot`, `index`) accepts an `s3://bucket/path` URI instead
of a local directory. S3 reads resolve one immutable Published Version through
the Location seam with zero-index retrieval. Credentials/region/endpoint come
from standard env vars:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=eu-west-1
# For S3-compatible endpoints (MinIO, etc.):
export LUMIO_S3_ENDPOINT=http://localhost:9000
export LUMIO_S3_ALLOW_HTTP=1

lumio-wiki search s3://my-bucket/kb "technology stack"
```

**Quick example against a local KB:**

```bash
$ lumio-wiki validate tests/fixtures/valid
Knowledge base is valid.

$ lumio-wiki search tests/fixtures/valid "stack" --limit 3
## Technology Stack
summary: The technologies Lumio uses.
path:    technology.md
score:   260.0
matched: title, alias, tag, body

$ lumio-wiki related tests/fixtures/valid "Technology Stack" --scope discovery --trace
Architecture
# trace: scope=discovery direction=outgoing max_depth=1 max_edges=1000 max_results=50 returned=1
```

### lumio — application CLI

Adds the Agent Runtime, storage sync, and the web server on top of
`lumio-wiki`. All commands work offline; `ask` uses the FakeProvider when no
provider is configured.

```bash
lumio validate <kb-path>                          # exit 0 if valid, 1 otherwise
lumio retrieve <kb-path> "<query>" [--limit N]    # lexical/frontmatter/graph retrieval
lumio ask <kb-path> "<question>"                  # cited answer via the Agent Runtime
lumio sync <source> "<query>" --working-dir <path>  # sync a storage source, then retrieve
              [--kb-path <p>] [--ref <branch>] [--mode git|shared|hybrid]
lumio serve [--host HOST] [--port PORT]           # run the web app
```

In a checked-out repository, prefix with `uv run`:

```bash
uv run lumio ask tests/fixtures/valid "What technology does Lumio use?"
```

---

## 2. Python library

The `lumio_wiki` package is the portable, model-free Knowledge Base foundation.
Every CLI command maps one-to-one to a public Python call.

### Load, validate, search

```python
from lumio_wiki import load_knowledge_base, validate

# Load and validate
kb, report = load_knowledge_base("my-kb")
print(f"Pages: {len(kb.pages)}, Valid: {report.is_valid}")

# Validate without loading (returns just the report)
report = validate("my-kb")
if not report.is_valid:
    for issue in report.issues:
        print(f"  {issue.severity}: {issue.message}")
```

### Zero-index retrieval with Evidence + Citation + Trace

```python
from lumio_wiki import load_knowledge_base
from lumio_wiki.retrieval import ZeroIndexRetrieval

kb, report = load_knowledge_base("my-kb")
adapter = ZeroIndexRetrieval()

results = adapter.retrieve(
    kb.pages,                  # the pages to search
    "technology stack",        # query
    limit=3,
)

for r in results:
    ev = r.evidence
    cit = r.citation
    print(f"Title:    {ev.page_title}")
    print(f"Path:     {ev.page_path}")
    print(f"Score:    {r.score}")
    print(f"Snippet:  {r.snippet[:80]}")
    print(f"Trace:    {' -> '.join(s.name for s in r.trace.stages)}")
    print(f"Citation: {cit.page_title} ({cit.relative_path})")
```

Output:
```
Title:    Technology Stack
Path:     technology.md
Score:    4.0
Snippet:  # Technology Stack ...
Trace:    search -> rank
Citation: Technology Stack (technology.md)
```

### Typed Relationship traversal and shortest paths

```python
from lumio_wiki import load_knowledge_base

kb, report = load_knowledge_base("my-kb")

# Related pages: canonical Relationships + Extracted References (--scope discovery)
related = kb.related_pages(
    "Technology Stack",
    scope="discovery",       # or "canonical"
    direction="both",        # outgoing, incoming, or both
)
# -> ['Architecture']

# Shortest directed path between two titles
path = kb.shortest_path(
    "Technology Stack",
    "Architecture",
    scope="discovery",
)
# -> ['Technology Stack', 'Architecture']
```

### Reading a page by Canonical Title or alias

```python
from lumio_wiki import load_knowledge_base

kb, report = load_knowledge_base("my-kb")
pages = kb.lookup_by_title("Technology Stack")  # exact title
aliased = kb.lookup_by_alias("Stack")            # by alias
page = pages[0]

print(page.title)           # "Technology Stack"
print(page.tags)            # ["technology", "stack"]
print(page.summary)         # "The technologies Lumio uses."
print(page.body)            # full Markdown body
print(page.path)            # "technology.md"
```

### Ingest workflow (you are the Distiller)

```python
from lumio_wiki import (
    load_knowledge_base,
    create_proposal_without_provider,
    IngestStore,
)
from lumio_wiki.proposal_pipeline import ProposalPipeline

kb, report = load_knowledge_base("my-kb")
store = IngestStore(kb.root / ".lumio" / "ingest")
pipeline = ProposalPipeline(kb, store=store)

# Stage a proposal (model-free: you author the Compiled Page Markdown)
proposal = create_proposal_without_provider(
    kb,
    source_path="path/to/notes.md",
    page_markdown=authored_markdown,
)

# Validate before publish
validation = pipeline.validate(proposal.id)
if validation.is_valid:
    pipeline.publish(proposal.id)
else:
    pipeline.discard(proposal.id)
```

### Publishing to S3 (immutable Published Versions)

```python
from lumio_wiki.s3_publish import publish_s3_version

manifest = publish_s3_version(
    store,                    # obstore ObjectStore
    prefix="kb",
    source_root="my-kb",
    version="2024-01-15",
    expected_pointer_version="2024-01-10",  # compare-and-swap guard
)
# Writes canonical content + Discovery Graph under kb/2024-01-15/,
# then conditionally advances current.json to the new version.
```

### Reading from an S3 Location

```python
from lumio_wiki.s3_location import open_s3_knowledge_base

# Resolve one immutable Published Version from S3 (requires lumio-wiki[s3])
snapshot = open_s3_knowledge_base(store, prefix="kb")
# snapshot.pages          — all Compiled Pages
# snapshot.fingerprint    — SourceFingerprint of this version
# snapshot.knowledge_base — KnowledgeBase instance for traversal/search

# Or open a specific version:
snapshot = open_s3_knowledge_base(store, prefix="kb", version="2024-01-15")
```

### Key public types

```python
from lumio_wiki import (
    # Core model
    KnowledgeBase, CompiledPage, KnowledgeBaseControlFile,
    # Validation
    ValidationReport, ValidationIssue, validate,
    # Records (immutable msgspec structs)
    RetrievalResult, Evidence, Citation, RetrievalTrace, TraceStage,
    PageSearchResult, Relationship, ExtractedReference,
    SourceFingerprint, GraphState,
    # Location seam
    KnowledgeBaseLocation, KnowledgeBaseSnapshot,
    FilesystemLocation, open_filesystem_knowledge_base,
    # S3 (requires lumio-wiki[s3])
    S3Location, open_s3_knowledge_base,
    # Ingest
    IngestProposal, IngestStore, ProposalPipeline,
    # Retrieval
    ZeroIndexRetrieval, RetrievalAdapter, default_retrieval_adapter,
    # Publish
    publish_s3_version, S3PublicationConflict,
    # Discovery Graph diagnostics
    StructuralGraphReport, GraphHealthReport,
    # Cross-link candidates
    LinkCandidate, RankedLinkCandidate, find_link_candidates,
)
```

---

## 3. Coding agent harness

Lumio works as a **shell-callable CLI** from any coding-agent harness that can
run commands. The packaged **Agent Skill** (`SKILL.md` + `PROTOCOL.md`) gives
the agent a structured protocol for the retrieval ladder, ingest workflow, and
guardrails.

### Install the Agent Skill

```bash
lumio-wiki skill install --agent pi
lumio-wiki skill install --agent hermes
lumio-wiki skill install --agent codex
lumio-wiki skill install --agent claude-code
```

This copies `SKILL.md` and `PROTOCOL.md` into the agent's conventional skill
directory. Pass `--dest <dir>` to override the destination.

### opencode (or any agent that runs shell commands)

opencode doesn't have a native "skill" concept, so you configure it via
**custom instructions** (the project `AGENTS.md` or `.opencode` config). The
fastest path is `lumio-wiki setup`, which writes both `.env` and `AGENTS.md`
for you:

```bash
cd my-project
lumio-wiki setup ./wiki
# or, to also install the skill for a native-skill agent:
# lumio-wiki setup ./wiki --agent claude-code
```

This writes `LUMIO_KB_PATH=./wiki` to `.env` and a `## Lumio Knowledge Base`
section to `AGENTS.md`. In opencode, point the agent at that file (or paste
the relevant section into `.opencode` settings):

**Option A — `AGENTS.md` (project-level, version-controlled, auto-generated):**

```markdown
## Lumio Knowledge Base

This project uses a Lumio Knowledge Base for domain knowledge. The host coding
agent IS the default Distiller (no model provider needed for base ingestion).

**KB path:** `./wiki` (also in `.env` as `LUMIO_KB_PATH`; the CLI reads it
automatically when no `\u003ckb\u003e` argument is given).

### Retrieval ladder (cheapest-first, stop when you have Evidence)

0. `lumio-wiki hot` — Maintainer-pinned entry pages. Read first.
1. `lumio-wiki index [dir]` — generated Navigation Index (all pages by directory).
2. `lumio-wiki search "\u003cquery\u003e"` — zero-index lexical search (no external index).
3. `lumio-wiki page "\u003ctitle\u003e"` — read a page to confirm and cite the exact passage.
4. `lumio-wiki related "\u003ctitle\u003e" --scope discovery` — related pages (canonical + extracted).
5. `lumio-wiki paths "\u003csrc\u003e" "\u003cdst\u003e"` — shortest directed path between two titles.

### Ingest (you are the Distiller)

1. Author a Compiled Page (YAML frontmatter + Markdown body) in a temp file.
2. `lumio-wiki ingest \u003cfile\u003e` — stages a reviewable Ingest Proposal.
3. `lumio-wiki proposal list` → `proposal inspect \u003cid\u003e` → `proposal validate \u003cid\u003e`.
4. `lumio-wiki publish \u003cid\u003e` (or `lumio-wiki discard \u003cid\u003e`).

### Guardrails

- **Cite or refuse.** Every domain claim cites a Compiled Page (title + path +
  passage). Unsupported claims return "not covered by this knowledge base."
- **Connectivity is not support.** Graph reachability selects pages to inspect;
  it never manufactures Evidence.
- **Proposal-first.** Validation always runs before publish. Never write `.md`
  files directly to the KB root.

### Diagnostics

- `lumio-wiki doctor` — version, detected extras, skill location.
- `lumio-wiki health` — page counts, validation, Discovery Graph health.
- `lumio-wiki validate` — exit 0 if valid, 1 otherwise.
```

**Option B — `.opencode` custom instructions:**

In your opencode project settings, paste the same block (or reference the
project `AGENTS.md`). The key is telling the agent that `LUMIO_KB_PATH` is set,
so it can run `lumio-wiki search "authentication"` without hardcoding the
path every time.

**Option C — direct shell calls (no config needed):**

opencode can run shell commands. If `lumio-wiki` is installed (`pip install
lumio-wiki`), the agent simply calls it. With `LUMIO_KB_PATH` exported in
the shell or `.env`, the path is implicit:

```bash
# Find pages about a topic
lumio-wiki search "authentication"

# Read the relevant page
lumio-wiki page "Authentication Architecture"

# Check related pages for broader context
lumio-wiki related "Authentication Architecture" --scope discovery --trace
```

### Claude Code / Codex / Pi / Hermes

These agents have native skill support:

```bash
lumio-wiki skill install --agent claude-code   # installs to ~/.claude/skills/
lumio-wiki skill install --agent codex         # installs to ~/.codex/skills/
lumio-wiki skill install --agent pi            # installs to ~/.pi/skills/
lumio-wiki skill install --agent hermes        # installs to ~/.hermes/skills/
```

After install, the agent automatically knows the retrieval ladder, ingest
workflow, and guardrails from `SKILL.md`.

### FAQ: how does the agent harness know where the wiki is?

**If the wiki already exists:** run `lumio-wiki setup ./wiki`. This writes
`.env` with `LUMIO_KB_PATH=./wiki` and `AGENTS.md` with the retrieval ladder.
The agent reads the path from `.env` (or you paste it into `.opencode`
settings), and the `lumio-wiki` CLI resolves it automatically when no
`\u003ckb\u003e` argument is given.

**If the wiki doesn't exist yet:** run `lumio-wiki setup ./wiki`. It creates
an empty categorized Knowledge Base (`lumio.yaml` + `.lumio/ingest` + `.lumio/index`)
writes `.env` and `AGENTS.md`, and optionally installs the skill for a native-skill
agent (`--agent claude-code`). Add your first `.md` Compiled Pages under `./wiki`,
then `lumio-wiki validate`. The setup is idempotent: re-running it on an
existing KB only updates `.env`, `AGENTS.md`, and the optional skill install.

### Ingest from a coding agent

The host coding agent is the default **Distiller** — no model provider needed.
Author the Compiled Page Markdown, write it to a temp file, then:

```bash
# Stage
lumio-wiki ingest ./kb /tmp/new-page.md
# → proposal abc123 staged (2 pages affected, not blocked)

# Review
lumio-wiki proposal inspect ./kb abc123
lumio-wiki proposal validate ./kb abc123

# Publish or discard
lumio-wiki publish ./kb abc123
```

For unattended distillation (no human-authored Markdown), install
`lumio-wiki[llm]` and pass `--distiller llm`.

### Guardrails (enforced in every harness)

- **Cite or refuse.** Every domain claim cites a Compiled Page (title + path +
  passage). Unsupported claims return "not covered."
- **Connectivity ≠ support.** Graph reachability selects pages to inspect; it
  never manufactures Evidence.
- **Proposal-first.** Validation always runs before publish. Never write `.md`
  files directly to the KB root.
- **Public surface only.** The protocol invokes only the public CLI and Python
  API. It never parses the private MessagePack graph artifact or starts the
  web application.

---

## Enhanced retrieval (optional)

Install `lumio-lancedb` for BM25, semantic, and hybrid ranking over the same
Knowledge Base. The RetrievalResult / Evidence / Citation / Trace contract is
identical to zero-index — only the ranking quality changes.

```bash
pip install lumio-lancedb                        # + LanceDB + PyArrow
pip install 'lumio-lancedb[embeddings]'          # + sentence-transformers for local embeddings
```

```python
from lumio_lancedb import LanceDBRetrievalAdapter

adapter = LanceDBRetrievalAdapter()
results = adapter.retrieve(kb.pages, "how does authentication work?", limit=5)
# Same RetrievalResult contract as ZeroIndexRetrieval
```

The full `lumio` app selects zero-index or LanceDB via `LUMIO_RETRIEVAL_BACKEND`
(default: `lancedb`). Switch without code changes — just install/uninstall the
adapter and flip the env var.

---

## Web app deployment (optional)

The full `lumio` wheel adds the deployable Stario web app:

```bash
pip install lumio

# Configure
export LUMIO_KB_PATH=/data/kb
export LUMIO_STORAGE_MODE=git
export LUMIO_GIT_SOURCE=https://github.com/you/your-compiled-wiki.git
export LUMIO_PROVIDER_BASE_URL=http://localhost:11434/v1   # Ollama
export LUMIO_PROVIDER_MODEL=llama3
export LUMIO_PROVIDER_API_KEY=ignored

# Serve
lumio serve --port 8000
```

Then open `http://localhost:8000/setup` to create the Owner account
(first-run only). See the [README](../README.md) for Docker deployment,
storage modes, and the full HTTP surface.
