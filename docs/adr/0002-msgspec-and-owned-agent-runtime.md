---
status: accepted
amends: ADR-0001 (agent-runtime row and new struct-validation row)
---

# ADR-0002: msgspec for Structs and Validation, and a Lumio-Owned Agent Runtime

## Context

ADR-0001 chose "Pydantic AI as a thin typed orchestration layer," and the early plans assumed a Pydantic-heavy stack more broadly (Pydantic BaseModels for domain models, Pydantic Settings for config, Pydantic request/response at the gateway). Because Pydantic AI transitively requires `pydantic`, the full tree sits in Lumio's footprint.

Three concerns converged on lightening that footprint: startup/CLI snappiness for the validation/indexing tool, per-request throughput on the retrieval hot path (many short-lived records per query), and dependency weight. Benchmark work (JP Hutchins, "The Fastest Python Struct?", Crumpled Paper, 2026-06-21) places `msgspec.Struct` in the fastest tier on both type-definition and instance cost — and unlike the other fast contenders, msgspec is also a validated de/serialization library, so it covers Pydantic's actual trust-boundary jobs without its startup weight. Separately, ADR-0001 already mandated that Lumio own the runtime architecture (classify → retrieve → call model → synthesize cited answer → expose trace → refuse unsupported); the only value Pydantic AI added on top was a tool-call loop, provider abstraction, streaming, and structured-output retries — all of which a small loop over an OpenAI-compatible client covers.

## Decision

Amend ADR-0001:

- **Agent runtime layer**: from "Pydantic AI as a thin typed orchestration layer" to "Lumio-owned minimal agent loop over an OpenAI-compatible provider client."
- **Struct and validation layer** (new row): msgspec.

`msgspec.Struct` is the single library for domain record types (frozen where immutable), boundary decode/validate, and configuration loading. Internal hot-path records that never receive external data skip validation. The agent runtime is a Lumio-owned loop; structured outputs parse `msgspec.Struct` schemas from the provider's structured-output response with a small owned retry loop; streaming uses the provider client's capability.

All other ADR-0001 decisions stand (Stario, Datastar, SQLite, Piccolo, LanceDB, LiteParse, MarkItDown, DuckDB, uv, pytest, ruff, Docker-first).

## Considered Options

- **msgspec for structs but keep Pydantic AI at the runtime** — rejected: Pydantic AI still transitively requires `pydantic`, so it does not remove the tree, and it introduces two struct systems at the runtime seam.
- **Stdlib frozen/slots dataclasses only** — rejected: removes validated JSON decode at the gateway and config boundaries, which is exactly where a validated parser earns its cost.
- **attrs (frozen, slots)** — rejected: mature but slower than msgspec on type-definition cost and adds a dependency for no capability gain.
- **Instructor or Mirascope as a lighter agent wrapper** — deferred: lighter than Pydantic AI and msgspec-compatible, but still a framework boundary over a small, well-understood loop. Reconsider if the owned loop grows complex.

## Consequences

The `pydantic` + `pydantic-ai` + `pydantic-settings` tree is removed. One struct/validation library covers domain models, the API boundary, and configuration. The agent runtime is fully under Lumio's control, with no pre-1.0 framework churn, and the retrieval-before-answer invariant lives in owned code.

Trade-off: Lumio must implement and maintain the loop (classification, provider calls, structured-output parsing, retry, streaming) and loses some Pydantic AI ergonomics. msgspec is stricter than Pydantic on coercion (no implicit `str`→`int`) and has no cross-field model validators, so invariants like `valid_until > valid_from` become explicit post-construction checks. Structured-output retry logic is now owned and must be tested.

> **Note:** Lumio's direct removal of `pydantic-ai` and `pydantic-settings` stands. However, the `openai` SDK transitively reintroduces `pydantic` into the resolved dependency tree. This is accepted because ADR-0002 mandates an OpenAI-compatible provider client, and the `openai` client is the chosen implementation.
