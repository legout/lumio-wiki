# Onboarding journey (focused example)

This is the executable companion to the canonical quickstart
([`docs/quickstart.md`](../../docs/quickstart.md), issue #180). It contains the
one Knowledge Source the journey ingests and a scriptable smoke journey that
runs the quickstart's exact commands, in order, and fails on any unexpected
outcome — so documentation drift between the quickstart and the CLI is caught
by CI rather than by a reader.

## Contents

| Path | Role |
|---|---|
| `sources/support-runbook.md` | The raw Knowledge Source the journey ingests (managed ingest input; you never commit raw sources into a Knowledge Base). |
| `smoke-journey.sh` | The scriptable journey: Maintainer setup → ontology starter → seed pages → managed ingest → proposal review → publish → retrieval/citation/traversal → Source Artifact inspection → S3 publication with and without LanceDB → read-only Reader project → expected zero-index fallback. |

## Run it

Local steps only (no object store needed):

```bash
./smoke-journey.sh
```

Full journey including MinIO/S3 publication (same environment variables the
CLI and the MinIO test suites read — the values below are the **labelled
local-test defaults** of a throwaway MinIO, never production credentials):

```bash
export LUMIO_S3_ENDPOINT=http://localhost:9000
export AWS_ACCESS_KEY_ID=minioadmin         # local test credential
export AWS_SECRET_ACCESS_KEY=minioadmin     # local test credential
export AWS_REGION=us-east-1
./smoke-journey.sh
```

The bucket (`lumio-quickstart` by default, override with
`LUMIO_S3_TEST_BUCKET`) is created when `mc` is on PATH; otherwise the script
checks it is reachable through `obstore` (the same client the CLI uses) and
tells you to create it otherwise. Each run publishes under a unique
`helpdesk-kb/run-…` prefix because Published Versions are immutable and cannot
be overwritten.

When `lumio-wiki` is not on `PATH` (for example from a Lumio checkout), point
the script at it:

```bash
LUMIO_WIKI_BIN="uv --project /path/to/lumio run lumio-wiki" ./smoke-journey.sh
```

## What a PASS proves

- The quickstart's command sequence works verbatim in a fresh project: setup,
  ontology starter, seeded Entity pages, managed document ingest, proposal
  review and publish, retrieval with citation open actions, canonical graph
  traversal, and truthful Source Artifact unavailability (retention is
  disabled by default and the CLI says so).
- Against MinIO: immutable publication (`publish-s3`) with zero-index and
  LanceDB artifacts, read-only Reader setup (`setup --from`), pathless reads,
  the disclosed zero-index fallback when a LanceDB index is not published,
  health after publishing one, and the truthful embedder requirement for
  `--mode semantic`/`--mode hybrid`.
- Documentation drift: every displayed citation keeps an `open:` action, the
  fallback wording matches, and any CLI change that breaks a documented
  command fails the script (and the CI job that runs it).
