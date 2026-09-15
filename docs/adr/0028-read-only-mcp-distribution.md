---
status: accepted
---

# ADR-0028: Read-Only MCP Distribution Surface

## Context

The Knowledge Base SDK reaches coding agents through two surfaces: the
`lumio-wiki` CLI and the packaged Agent Skill (`lumio-wiki skill install`),
which teaches skill-capable hosts the retrieval ladder and guardrails. Hosts
that speak the Model Context Protocol (MCP) but do not read installed skills
cannot use either surface without shelling out ad hoc.

MCP is a supported, versioned protocol with many hosts. Publishing a server is
hard to reverse: once agents configure `lumio-wiki` as an MCP server, its tool
names, arguments, and result shapes become a compatibility contract on par with
the CLI. The choice of transport, write scope, and packaging all create
durable surface.

Owner decisions from 2026-09-15: read-only tools first; `[mcp]` extra with a
`lumio-wiki mcp <kb-path-or-s3-uri>` stdio entry point; one server bound to one
Knowledge Base Location.

## Decision

Ship an optional, read-only MCP server inside `lumio-wiki`:

- **Packaging:** optional `[mcp]` extra carrying the official Python `mcp`
  dependency; the base wheel stays `msgpack` + `msgspec[yaml]` only
  (ADR-0010's lightweight-core constraint is unchanged).
- **Entry point:** `lumio-wiki mcp <location>` subcommand speaking MCP over
  stdio. One server process is bound to exactly one Knowledge Base Location
  (filesystem path or `s3://` URI): it constructs the matching
  `FilesystemLocation` or `S3Location` from the startup argument and resolves
  Snapshots through the existing `open_knowledge_base` seam.
- **Tool surface (v1):** read-only retrieval and diagnostics that map onto
  existing Snapshot/SDK behavior: `search`, `page`, `related`, `paths`, `hot`,
  `index`, `health`, `status`. Each tool call resolves a fresh Snapshot, so a
  long-running server never serves stale state. Every tool is a pure read —
  notably `health`, which is a non-mutating report and never creates derived
  directories or materializes graph artifacts.
- **No write tools:** ingest, proposal staging, review, and publish stay out of
  v1. They require their own authorization and safety design and are a
  separately approved follow-up.

Behavioral acceptance lives in PRD-0007.

## Considered Options

- **Separate `lumio-mcp` wheel** — rejected: a second package, version line,
  and release train for a thin wrapper; an optional extra keeps the core light
  without forking the distribution.
- **HTTP/SSE transport first** — rejected: stdio is the standard local-agent
  MCP binding and needs no listener, port, or auth story. HTTP can be added
  later without changing tool semantics.
- **Read + write tools now** — rejected: writes mean proposal-first semantics,
  identity, and failure modes that deserve their own design; shipping them
  half-designed would create exactly the unsupported surface Plan 05 is
  removing elsewhere.
- **Do nothing; skill is enough** — rejected: the skill only helps hosts that
  read skills. MCP is the host-agnostic door and is cheap because the server
  is a pass-through over retained APIs.

## Consequences

- A new supported surface exists: tool names/arguments/results are
  compatibility contracts and fall under the same consumer-evidence rules as
  any public API (PRD-0006).
- Plan 05 is unaffected: the server consumes the retained Snapshot façade and
  bounded traversal owner; it introduces no new traversal or storage path.
- S3 Locations inherit the existing credential and redaction rules; the server
  adds no new secret handling.
- Adding write tools later is additive and does not break v1 consumers.
