# 06 — Read-only MCP server

**Approved: owner sign-off 2026-09-15 after five independent review rounds;
execution proceeds supervised per ADR-0027 — immediate review after M1, one
final candidate review, integration and publication gates retained.** Goal: deliver
[PRD-0007](../prd/0007-mcp-server.md) — a read-only MCP server that lets any
MCP-capable host retrieve from a Knowledge Base with CLI-equivalent answers.
Source specification: `docs/prd/0007-mcp-server.md` and
[ADR-0028](../adr/0028-read-only-mcp-distribution.md), owner-approved
2026-09-15 (spec commit `661df78`, independently reviewed clean).
Capture checkpoint: no new domain vocabulary; ADR-0028 is the only new
decision record; no unresolved material decisions. Contract version 1; local
project skill provenance `unknown`.

Architecture constraints needed for sequencing: read-only (no filesystem or
S3 writes under any tool call); one server bound to one Knowledge Base
Location constructed via `FilesystemLocation` / `S3Location.from_url` and
resolved through `open_knowledge_base`; fresh Snapshot per tool call; no new
mandatory dependencies — the base wheel stays `msgpack` + `msgspec[yaml]`
(ADR-0010). Runtime: Python 3.14, uv workspace, official `mcp` Python SDK
(FastMCP) over stdio.

Requirement map: AC1 → M1, AC2 → M2, AC3 → M3, AC4 → M4. The git transport
convenience is a separate approved bounded change and is deliberately not in
this plan. Plan 05 is unaffected: this plan consumes the retained Snapshot
façade only.

## Validation units

| Unit | Tasks | Risk / obligation | Distinct failure protected |
| --- | --- | --- | --- |
| MV1 | M1 | high / `new-test` | server binds the wrong KB, starts without the extra, echoes credentials, or its first tool returns wrong data |
| MV2 | M2 | normal / `new-test` | a retrieval tool diverges from CLI/Snapshot behavior |
| MV3 | M3 | normal / `new-test` | diagnostics misreport state or the server writes to the KB |
| MV4 | M4 | normal / `existing-check` | the base wheel gains a dependency or the entry point breaks from an installed wheel |

M1 is high risk: it adds a dependency and defines the public tool contract
that M2–M4 consume; it receives immediate review at that boundary plus one
final candidate review. Security scope: the server handles S3 credentials and
a new dependency; realistic threats are credential echo in error output
(asserted in MV1) and hostile KB content or tool arguments (bounded by the
existing loader redaction and by title-based lookups — no path traversal
exists because tool arguments are titles/queries, never filesystem paths).
A local stdio server the user configures against their own KB is the real
deployment boundary; no network listener exists.

## Produced interface: v1 tool contract

Common rules: tool arguments are titles or queries — never filesystem paths;
every tool resolves a fresh Snapshot before answering; failures surface as
bounded, credential-free tool errors (FastMCP error path), never tracebacks
with environment content. Schemas below are the durable compatibility contract
(ADR-0028); parity tests assert these concrete shapes, not rendered CLI text.

| Tool | Arguments (defaults match the CLI) | Result (JSON) | Empty/negative |
| --- | --- | --- | --- |
| `search` | `query: str` (required), `limit: int = 20` (forwarded unchanged, as the CLI accepts) | `{"results": [{"title", "path", "passage"}]}` | `{"results": []}` |
| `page` | `title: str` — canonical lookup, then alias fallback as `_cmd_page` (`cli.py:1828`) | `{"found": true, "title", "path", "summary", "markdown"}` | `{"found": false}` |
| `related` | `title: str`, `relationship_type: str \| None = None`, `depth: int = 1`, `scope: "canonical" \| "discovery" = "canonical"`, `direction: "outgoing" \| "incoming" \| "both" = "outgoing"`, `max_edges`/`max_results` = `DEFAULT_GRAPH_MAX_EDGES`/`DEFAULT_GRAPH_MAX_RESULTS` | `{"results": [str], "scope": str, "direction": str}` — sorted titles (Snapshot semantics; the CLI's entity-ID suffixes are display, not data) | `{"results": [], "scope": str, "direction": str}` |
| `paths` | `source: str`, `target: str`, `scope = "canonical"`, `direction = "outgoing"`, `max_depth = DEFAULT_GRAPH_MAX_DEPTH`, `max_edges = DEFAULT_GRAPH_MAX_EDGES` | `{"found": true, "path": [str], "scope": str, "direction": str}` | `{"found": false, "scope": str, "direction": str}` |
| `hot` | — | `{"markdown": str}` generated in memory, byte-identical to `hot.md` | `{"markdown": null}` when no pins |
| `index` | — | `{"markdown": str}` — the generated **root** Navigation Index only (the `lumio-wiki index` default view), not the per-directory set | always non-empty |
| `health` | — | `{"valid": bool, "pages": int, "issues": int, "broken_relationships": [str], "unknown_relationship_types": [str], "invalid_fields": [str], "duplicate_aliases": [str], "missing_summaries": [str], "graph": {"materialized": bool, "fresh": bool \| null, "edges": int \| null, "source": "derived" \| "zero-index", "disclosure": str}, "derived_index": {"available": bool, "kind": "filesystem" \| "remote-lancedb" \| null}}` | n/a |
| `status` | — | `{"location": {"kind": "filesystem" \| "s3"}, "fingerprint": str, "pages": int, "valid": bool, "retrieval": "zero-index", "remote_derived_index": {"available": bool}, "extras": [str]}` | n/a |

## M1

- [ ] **Server skeleton, binding, and the first tool end-to-end (AC1).**
  **Files:** `packages/lumio-wiki/pyproject.toml` (new `mcp` extra; add it to
  `all`), `packages/lumio-wiki/src/lumio_wiki/mcp_server.py` (new),
  `packages/lumio-wiki/src/lumio_wiki/cli.py` (`mcp` subcommand +
  `_cmd_mcp`), `uv.lock`, `packages/lumio-wiki/tests/test_mcp_server.py`
  (new).
  **Consumes → produces:** a startup argument (filesystem path or `s3://`
  URI) → a stdio FastMCP server whose `search` tool answers from a fresh
  Snapshot.
  **Interfaces:** filesystem arguments construct `FilesystemLocation(path)`;
  `s3://` arguments reuse the CLI's existing resolver
  `_resolve_object_store_location` (`cli.py:286-295`), which applies
  `_s3_config_from_env()` (endpoint/region/credential env handling) and the
  redacting `S3Location.from_url` parser — the server imports this resolver
  rather than duplicating URI or credential handling. Resolution then goes
  through the module-level `open_knowledge_base(location)` (`location.py:383`).
  The `mcp` dependency uses the official SDK's high-level decorator API
  (named `MCPServer` at `mcp.server.mcpserver` in mcp 2.x — the FastMCP
  rename; execution-time owner disposition 2026-09-16) with a `>=` floor at
  the current stable version at implementation time, recorded in `uv.lock`.
  Missing `[mcp]` follows the existing lazy-extra pattern: bounded message
  naming the exact install command, non-zero exit.
  **Behavior and edge cases:** invalid path, invalid KB, missing S3
  credentials, and unsafe URIs (userinfo/query/fragment) all fail before the
  server starts with a bounded, credential-free message; the error never
  echoes the supplied URI. The `search` tool takes a query and optional limit
  and returns title/path/passage rows from `snapshot.search_pages(...)`.
  **Validation unit:** MV1. Red → minimal green → verify: tests call the tool
  functions in-process (no stdio subprocess needed) and drive `_cmd_mcp`
  argument/startup failures directly. Assert: fixture KB binds and `search`
  returns the expected hit; each startup failure class is bounded and
  credential-free; missing-extra message names `pip install
  'lumio-wiki[mcp]'`. A successful `s3://` binding test uses the existing
  in-memory-store pattern and asserts `search` returns the expected hit.
  **Verify:** `uv run pytest -q packages/lumio-wiki/tests/test_mcp_server.py`
  **Done:** `lumio-wiki mcp <fixture-kb>` starts; `search` answers; all
  failure classes bounded; immediate review of the dependency change and tool
  contract recorded.

## M2

- [ ] **Remaining retrieval tools with parity (AC2).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/mcp_server.py`,
  `packages/lumio-wiki/tests/test_mcp_server.py`.
  **Consumes → produces:** the reviewed M1 server → `page`, `related`,
  `paths`, `hot`, `index` tools over the same fresh-Snapshot rule.
  **Behavior and edge cases:** all tools implement the v1 tool contract table
  above. `page` does canonical lookup with the CLI's alias fallback; not-found
  is `{"found": false}`, not an error. `related` and `paths` expose the CLI's
  depth/scope/direction/bounds arguments with their defaults (`cli.py:5064-5078`)
  and report the effective scope/direction; `related` returns sorted title rows
  as `related_pages()` produces them (the CLI's entity-ID display suffixes are
  presentation, not data). `paths` handles same/missing endpoints exactly as
  the Snapshot delegate does. `hot` and `index` are
  generated in memory from the Snapshot (`generate_hot_index`,
  `generate_navigation_indexes`) — deterministic, byte-identical to the
  reserved artifacts on filesystem KBs, and uniform across filesystem and S3
  Locations; `index` returns the root index (`index.md`) entry of the
  generated mapping. Parity tests compare tool output against
  Snapshot/CLI behavior on `tests/fixtures/valid` and
  `tests/fixtures/categorized_kb`; S3-Location coverage uses the existing
  in-memory-store pattern (`S3Location(store, prefix)` as in `cli.py:3132-3136`).
  The categorized fixture covers typed relationships and aliases.
  **Validation unit:** MV2. `new-test`: one parity test per tool at the
  in-process tool seam; failure covered: a tool diverges from CLI/Snapshot
  behavior or mis-shapes citation data (title, path, passage).
  **Verify:** `uv run pytest -q packages/lumio-wiki/tests/test_mcp_server.py`
  **Done:** all six retrieval tools match CLI behavior on fixtures for both
  Location kinds; not-found and empty cases are honest.

## M3

- [ ] **Diagnostics tools and the no-write proof (AC3).**
  **Files:** `packages/lumio-wiki/src/lumio_wiki/mcp_server.py`,
  `packages/lumio-wiki/tests/test_mcp_server.py`.
  **Consumes → produces:** Snapshot validation/fingerprint state → `health`
  and `status` tools reporting page counts, validation, Discovery Graph
  health, and retrieval state per the contract table.
  **Behavior and edge cases:** validation and page fields are computed only
  from in-memory Snapshot data — `snapshot.validation_report` (its issues
  drive the broken/unknown/invalid sets exactly as `health_report()` derives
  them) and `snapshot.knowledge_base.pages` (missing summaries, duplicate
  aliases). The projection never calls `KnowledgeBase.health_report()` (it
  revalidates via the filesystem root, which is invalid for S3 snapshots) and
  never creates the derived-index directory or materializes a graph artifact.
  The single permitted filesystem read is graph disclosure on filesystem
  Locations: when the derived index directory exists, call
  `graph_health(<derived-dir>)` (a pure read — MV3 proves it), map its
  `graph_fresh` to `fresh` and `edge_count` to `edges`, and set
  `source: "derived"` only when the report shows a materialized, fresh
  artifact. When the artifact is missing, stale, or corrupt (`graph_health`
  itself falls back to an in-memory graph, `knowledge_base.py:1148-1159`),
  report `source: "zero-index"` with a disclosure string naming the actual
  state. S3 Locations use the read-only `graph_state_with_source()` seam
  (`s3_location.py:589-601`): a `"published artifact"` label (digest-verified,
  fingerprint/extractor-current) reports `source: "derived"` with the loaded
  graph's edge count; a `"memory"` label reports `source: "zero-index"` with
  the returned `note` as the disclosure (not published / unreadable / corrupt
  / stale). No path writes or materializes anything. Derived-index
  availability is a separate read-only check: directory-existence on
  filesystem Locations, the `remote_derived_index` descriptor on S3
  Snapshots. On filesystem fixtures with a materialized fresh derived graph
  the projection equals `_cmd_health`'s reported state; elsewhere it reports
  the same validation fields plus the honest disclosure. `status` reports
  retrieval as `"zero-index"` unconditionally (the server binds no remote
  adapter) and exposes remote-index descriptor availability separately.
  **Validation unit:** MV3. `new-test`: content equivalence for both tools,
  plus a tree-hash assertion that constructing the server and calling every
  tool leaves the fixture directory byte-identical (no new files, no
  modifications) — run once on a plain fixture and once on a copy with a
  pre-materialized derived graph, proving the `graph_health` read path also
  never writes. For the S3 Location, assert the in-memory store's keys and
  bytes are unchanged after invoking the full tool surface. Failure covered:
  diagnostics misreport, or any tool silently writes.
  **Verify:** `uv run pytest -q packages/lumio-wiki/tests/test_mcp_server.py`
  **Done:** health/status match CLI state disclosure; the no-write assertion
  passes over the full tool surface.

## M4

- [ ] **Packaging isolation, CI smoke, and documentation (AC4).**
  **Files:** `.github/workflows/ci.yml` (isolated-wheel job), `README.md`,
  `docs/usage.md`, `docs/packaging.md` (extras list).
  **Consumes → produces:** the complete server → wheel-level proof that the
  base install is unchanged and `lumio-wiki[mcp]` works from the built wheel,
  plus user-facing documentation.
  **Behavior and edge cases:** the isolated-wheel job gains a second venv
  step that installs `lumio-wiki[mcp]` from the built wheel and runs a
  startup + `search` smoke against `tests/fixtures/valid`;
  `scripts/verify_lumio_wiki_wheel.py` is unchanged and must keep passing
  (its exact core-requirements assertion proves the base wheel did not gain
  `mcp`). Docs: README quick-start line, a usage section (configure the
  server in an MCP host, tool list, read-only scope), and the `mcp` extra in
  the packaging extras list. Do not touch `AGENTS.md` (unrelated local
  changes present).
  **Validation unit:** MV4. `existing-check`: the wheel verifier and CI
  workflow are the evidence; the added venv smoke is part of the required CI
  definition, not a new test suite. Docs edits are `no-new-test`.
  **Verify:** CI green, including the new `[mcp]` venv smoke and the
  unchanged base-wheel verification.
  **Done:** AC4 demonstrated from built wheels; docs published; final
  candidate review recorded.

## Global validation

```sh
uv lock --check
uv run pytest -q -n 4
uv run python -m ruff check packages tests eval scripts
uv run python -m ruff format --check packages tests eval scripts
git diff --check
```

## Residual risks and manual checks

- FastMCP API drift: mitigated by the locked floor; no live check beyond CI.
- Host interop: one manual check — register the server in a real MCP host,
  call `search` and `page`, confirm stdio lifecycle (startup, call, shutdown)
  behaves. Not automatable in repo CI.
- Live S3 binding: logic is covered by in-memory-store tests and the existing
  `*_s3_compat.py` suites cover the Location itself; a live-SeaweedFS MCP
  smoke is optional, not required.
