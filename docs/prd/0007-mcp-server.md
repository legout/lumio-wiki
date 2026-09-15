# PRD-0007: Read-Only MCP Server

_Status: behavior approved. Owner decisions captured 2026-09-15: read-only
first, `[mcp]` extra, `lumio-wiki mcp <kb-path-or-s3-uri>` over stdio, one
server bound to one Knowledge Base Location; the ADR and this specification
text were reviewed and approved the same day after independent review.
Governing decision: ADR-0028.
Capture checkpoint: no new domain vocabulary beyond "MCP server"; no further
ADR warranted. Implementation planning and execution remain separately gated.
Contract version 1; local project skill provenance `unknown`._

## Goal

Let any MCP-capable agent host retrieve from a Lumio Knowledge Base — with the
same answers the CLI gives — without installing the Agent Skill or shelling
out to the CLI.

## Behavioral constraints

- Read-only. The server never creates, modifies, stages, or publishes
  Knowledge Base content. There are no write tools in this version.
- One server process is bound to exactly one Knowledge Base Location given at
  startup: a filesystem path or an `s3://` URI. The server constructs the
  matching `FilesystemLocation` or `S3Location` and resolves Snapshots through
  the existing `open_knowledge_base` seam. Snapshot content, retrieval
  behavior, and authorization scope are exactly the Snapshot façade's.
- Every tool call resolves a fresh Snapshot; a long-running server never
  serves stale state.
- Pure reads only: no tool call performs a filesystem or S3 write. `health` is
  a non-mutating report — unlike the rebuild-capable CLI command, the server
  never creates derived directories or materializes graph artifacts.
- Errors are bounded and safe: startup failures exit non-zero with a clear
  message; tool failures return bounded tool errors. Credentials never appear
  in output — existing S3 credential and redaction rules apply unchanged
  (ADR-0020, object-store URI hardening).
- No new mandatory dependencies. The server ships as the optional `[mcp]`
  extra; the base wheel's dependency set is unchanged.

## Acceptance criteria

### AC1: Startup and binding

`lumio-wiki mcp <location>` starts a stdio MCP server bound to that Location.
A filesystem KB and an `s3://` KB both bind successfully (S3 requires `[s3]`
as today). An invalid path, an invalid KB, or missing S3 credentials exits
non-zero with a bounded, credential-free message. Without `[mcp]` installed,
the subcommand prints the exact install command and exits non-zero.

### AC2: Retrieval parity

`search`, `page`, `related`, and `paths` return results equivalent to the same
inputs through the CLI against the same Knowledge Base Location — filesystem
and `s3://` alike — including bounded result shapes and the honest
empty/negative cases. `hot` and `index` are defined over the bound Snapshot
uniformly for both Location kinds and match CLI output on filesystem KBs
(their CLI commands are filesystem-only today). Tool output carries the
citation data (canonical title, path, passage) the guardrails require.

### AC3: Diagnostics

`health` and `status` report the same validation, page-count, and retrieval
state as their CLI counterparts, including Discovery Graph health disclosure.
Both are non-mutating: no derived-index directory creation, no graph
materialization, no rebuild path exists in the server.

### AC4: Packaging isolation

The isolated-wheel verification passes with and without `[mcp]` installed:
base-wheel dependency set and behavior are unchanged; the `mcp` dependency
appears only under the extra. The `lumio-wiki mcp` subcommand is the only new
entry point.

## Non-goals

- Write, ingest, proposal, review, or publish tools.
- Serving multiple Knowledge Bases from one server process.
- HTTP/SSE transport, authentication, or multi-user concerns.
- MCP resources, prompts, subscriptions, or notifications.
- Any change to Snapshot, traversal, or storage behavior (Plan 05 owns those).

## Evidence

Owner decisions and the distribution-surface rationale: ADR-0028. The server
is a thin pass-through over the retained Snapshot façade
(`location.py`); no new retrieval or storage path is introduced. CLI tool
semantics and guardrails: `docs/kb-format.md`, the packaged Agent Skill.
