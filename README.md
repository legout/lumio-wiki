# Lumio

Deployable chat for trusted knowledge and data. A single-tenant, deployable browser agent platform for compiled Markdown knowledge bases.

Start with the domain docs:

- [`CONTEXT.md`](CONTEXT.md) — ubiquitous language.
- [`docs/prd/0001-knowledge-agent-platform.md`](docs/prd/0001-knowledge-agent-platform.md) — the platform PRD.
- [`docs/adr/`](docs/adr/) — architectural decisions.

## Provider configuration

Lumio answers using an OpenAI-compatible model provider when one is configured,
and falls back to a deterministic offline provider otherwise.

Set these environment variables to enable a real provider:

- `LUMIO_PROVIDER_BASE_URL` — OpenAI-compatible API base URL (e.g. `http://localhost:11434/v1` for Ollama)
- `LUMIO_PROVIDER_MODEL` — model name (e.g. `llama3`)
- `LUMIO_PROVIDER_API_KEY` — API key (leave empty for local servers that do not require one; a dummy value also works)

Secrets are loaded from the environment only and are never committed or logged.

### Verifying a real provider locally

1. Start an OpenAI-compatible server such as Ollama with a model pulled:
   ```bash
   ollama run llama3
   ```
2. Run the CLI with the provider variables set:
   ```bash
   LUMIO_PROVIDER_BASE_URL=http://localhost:11434/v1 \
   LUMIO_PROVIDER_MODEL=llama3 \
   LUMIO_PROVIDER_API_KEY=ignored \
   uv run lumio ask tests/fixtures/valid "What technology does Lumio use?"
   ```

If the provider is unreachable or misconfigured, the app returns an actionable
error instead of crashing.

