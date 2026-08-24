# Lumio agent onboarding quickstart (canonical)

This is the **one canonical onboarding journey** (issue #180): a single linear
path from prerequisites to a grounded, citation-backed agent answer. It runs
against a local [MinIO](https://min.io) so every S3 step is real, and closes
with the AWS/S3 production variant. You do not need to read any ADR, the
repository tests, or the rest of the docs to finish it.

Time: ~20 minutes. You end with a Maintainer project that publishes immutable
Knowledge Base versions to object storage, and a read-only Reader project that
answers questions from them with citations.

> The journey is scriptable: [`examples/onboarding-journey/smoke-journey.sh`](../examples/onboarding-journey/smoke-journey.sh)
> executes exactly the commands below and fails on drift. CI runs it against
> MinIO on every push.

## What you are building

Lumio compiles trusted knowledge into a **Knowledge Base**: a directory of
Compiled Markdown pages plus a root `lumio.yaml` Control File. A **Maintainer**
authors and reviews locally; `publish-s3` writes an **immutable Published
Version** to object storage; a **Reader** project reads that version
read-only. One loop, two roles:

```text
setup → status → ingest → proposal review → publish
      → publish-s3 → reader setup → search/page/related → open citation/source
```

## 0. Prerequisites

- Python **≥ 3.14** and a shell.
- Docker (only for the local MinIO in Part 1).
- `mc` (the MinIO client) or the AWS CLI for the one-time bucket creation in
  Part 1.

Install the portable foundation with the S3 extra, and — when you want the
enhanced retrieval backend — the LanceDB adapter with its S3 extra:

```bash
pip install 'lumio-wiki[s3]'        # Knowledge Base CLI + S3 Locations
pip install 'lumio-lancedb[s3]'     # optional: LanceDB backend over S3
```

Verify:

```bash
lumio-wiki --version                # e.g. lumio-wiki 0.1.1
lumio-wiki doctor                   # version, detected extras, skill location
```

## 1. Local MinIO with a dedicated bucket

Start a throwaway MinIO and create a bucket dedicated to this journey. The
`minioadmin`/`minioadmin` values below are MinIO's **labelled local-test
defaults** — fine on localhost, never production credentials.

```bash
docker run -d --name lumio-quickstart-minio -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data

mc alias set local http://localhost:9000 minioadmin minioadmin
mc mb --ignore-existing local/lumio-quickstart
```

(No `mc`? `aws s3api create-bucket --bucket lumio-quickstart --endpoint-url
http://localhost:9000` with the same credentials works too.)

Point Lumio at it — standard AWS credential variables plus the endpoint
override; exported values win, nothing is written to any file:

```bash
export AWS_ACCESS_KEY_ID=minioadmin        # local test credential
export AWS_SECRET_ACCESS_KEY=minioadmin    # local test credential
export AWS_REGION=us-east-1
export LUMIO_S3_ENDPOINT=http://localhost:9000
export LUMIO_S3_ALLOW_HTTP=1               # plain HTTP is localhost-only
```

## 2. Maintainer: local project setup

Create a fresh project directory and run the canonical bootstrap. It creates
the Knowledge Base (seeded `lumio.yaml` + ingest store), writes project
`.env`, writes the `AGENTS.md` protocol section, and records the S3
publication destination:

```bash
mkdir helpdesk-project && cd helpdesk-project
lumio-wiki setup ./kb --publish-to s3://lumio-quickstart/helpdesk-kb
```

`setup` ends with a status summary — role, location, retrieval backend/mode,
graph state, artifact retention, validation, and one next action. Re-run it
any time with `lumio-wiki status`.

## 3. Ontology starter

A fresh Knowledge Base seeds a **valid-empty ontology**: `lumio.yaml` carries
the version-2 Control File, but `ontology.entity_types` and
`ontology.predicates` are empty. That is sufficient until the first page
declares `entity_types` or `claims` — validation then fails with "declare it
under `ontology.entity_types`/`ontology.predicates` in `lumio.yaml`", because
the vocabulary is KB-local content you declare deliberately.

Open `kb/lumio.yaml` and replace the empty ontology block

```yaml
ontology:
  entity_types:
  predicates:
  redirects:
```

with this starter (small but real: two Entity Types for the graph, a literal
predicate, and an inverse pair — an inverse must itself be a declared
predicate):

```yaml
ontology:
  entity_types:
    software-system:
      description: "A deployed software product or platform."
    database:
      description: "A persistent storage service backing an application."
    procedure:
      description: "A reviewed runbook or operating procedure."
  predicates:
    uses:
      subject_types: [software-system]
      object_types: [database, software-system]
      inverse: used-by
    used-by:
      subject_types: [database, software-system]
      object_types: [software-system]
      inverse: uses
    described-as:
      literal_kind: string
  redirects: {}
```

## 4. Seed pages with one accepted Claim

In a version-2 Knowledge Base every page declares a stable Entity ID and at
least one entity type, and lives under a configured Content Category. Create
two pages — the second page is the object of the first page's accepted Claim,
which is what makes graph traversal real:

```bash
mkdir -p kb/entities
cat > kb/entities/aurora-helpdesk.md <<'EOF'
---
title: "Aurora Helpdesk"
id: "entity:aurora-helpdesk"
entity_types: ["software-system"]
aliases: ["Aurora"]
tags: ["support", "product"]
summary: "The Aurora helpdesk product this Knowledge Base documents."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "aurora-product-notes"
    title: "Aurora product notes"
claims:
  - id: "claim:aurora-uses-starlight"
    predicate: "uses"
    object: "entity:starlight-db"
    status: "accepted"
    evidence:
      - lines: [6, 6]
---

# Aurora Helpdesk

Aurora is the support helpdesk this Knowledge Base documents. It stores
tickets and attachments in [Starlight DB](starlight-db.md).
EOF

cat > kb/entities/starlight-db.md <<'EOF'
---
title: "Starlight DB"
id: "entity:starlight-db"
entity_types: ["database"]
tags: ["storage"]
summary: "The persistent database behind Aurora Helpdesk."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "aurora-product-notes"
    title: "Aurora product notes"
claims:
  - id: "claim:starlight-described-as"
    predicate: "described-as"
    value: "managed PostgreSQL, region eu-west-1"
    value_type: "string"
    status: "accepted"
    evidence:
      - lines: [3, 4]
---

# Starlight DB

Starlight DB is the managed PostgreSQL (region eu-west-1) that stores Aurora
tickets and attachments.
EOF

lumio-wiki validate ./kb
```

A Claim needs `id`, `predicate`, exactly one object (`object` **or**
`value`+`value_type`), `status: accepted`, and at least one `evidence` anchor
(`lines` and/or `section`) into the page body — a Claim is a graph edge, never
answer Evidence by itself.

## 5. Managed document ingest (you are the Distiller)

Take a raw document — any file your team actually owns, here a support runbook
— and author its Compiled Page yourself (the host coding agent is the default
Distiller; no model provider, no `[documents]` extra). The managed mode binds
the ORIGINAL bytes and your authored page under one stable source identity, so
provenance records the real file:

```bash
cat > support-runbook.md <<'EOF'
# Support runbook: password reset (internal)

Last reviewed: 2026-07-10. Owner: Support Ops.

1. Verify the requester through the secondary email on file.
2. In Aurora Helpdesk, open the requester's profile and click
   "Force password reset".
3. The reset link expires after 30 minutes; tell the requester.
4. Escalate to Support Ops on-call if the profile has no secondary email.

Reset volume is tracked in Starlight DB table `reset_audit`.
EOF

cat > password-reset-page.md <<'EOF'
---
title: "Password Reset Runbook"
category: procedures
type: runbook
durability_rationale: "Owned runbook; reviewed yearly by Support Ops."
id: "entity:password-reset-runbook"
entity_types: ["procedure"]
tags: ["support", "runbook"]
summary: "How Aurora support verifies a requester and forces a password reset."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "support-runbook-2026"
    title: "Support runbook: password reset"
---

# Password Reset Runbook

Verified steps from the 2026 support runbook. Aurora Helpdesk support
verifies the requester through the secondary email on file, then forces the
reset from the requester's profile. The reset link expires after 30 minutes.
Requests without a secondary email escalate to Support Ops on-call; reset
volume is tracked in [Starlight DB](../entities/starlight-db.md).
EOF

lumio-wiki ingest ./kb support-runbook.md \
  --compiled-page password-reset-page.md --source-id support-runbook-2026
```

The authored page declares `category`, a free-form `type`, and a
`durability_rationale` (categorized pages need all three), plus the same
`support-runbook-2026` id in `sources[].id` — that is the binding. Raw bytes
never enter the Knowledge Base; they are registered privately.

## 6. Proposal review: inspect → validate → publish

Proposal-first is the write mode — validation always runs before publish:

```bash
lumio-wiki proposal list ./kb
lumio-wiki proposal inspect ./kb <id>      # provenance, blast radius, diff
lumio-wiki proposal validate ./kb <id>     # must end "Knowledge base is valid."
lumio-wiki publish ./kb <id>
```

## 7. Retrieval, citations with open actions, traversal

Every retrieval result and page read labels how to **open** what it cites —
reuse the labels verbatim when you answer:

```bash
lumio-wiki search "password reset" --limit 2
```

```text
## Password Reset Runbook
summary: How Aurora support verifies a requester and forces a password reset.
path:    procedures/password_reset_runbook.md
...
open:            lumio-wiki page "Password Reset Runbook"
```

Read the page (ladder step 3) to copy the exact passage before citing it:

```bash
lumio-wiki page "Password Reset Runbook"
```

The page read adds the private-Source action — an explicit, authorized
inspection command, never an implicit URL:

```text
source-artifact: lumio-wiki source inspect --source-id support-runbook-2026
```

Traverse the Discovery Graph over the accepted Claim you seeded (add
`--scope discovery` to include deterministic body-link Extracted References —
topology only, never Evidence):

```bash
lumio-wiki related "Aurora Helpdesk" --scope canonical --trace
lumio-wiki paths "Aurora Helpdesk" "Starlight DB" --scope canonical --trace
```

```text
Aurora Helpdesk (entity:aurora-helpdesk) -> Starlight DB (entity:starlight-db)
# trace: scope=canonical direction=outgoing max_depth=5 max_edges=1000 found=true hops=1
```

## 8. Source Artifact inspection — truthful unavailability

Retention of raw Source Artifacts is **disabled by default**; nothing was
configured in `setup`, so inspection says exactly that (secret-free metadata,
no raw bytes):

```bash
lumio-wiki source inspect ./kb --source-id support-runbook-2026
```

```text
source_id:       support-runbook-2026
...
bound_to:        current registry version (local worktree)
availability:    not retained (no Source Artifact Store configured)
authorization:   granted (private registry view)
```

`lumio-wiki source fetch ./kb --source-id support-runbook-2026` fails with an
equally actionable message (naming `--source-store`) rather than substituting
something else. Retention is **not retroactive**: raw bytes are retained at
managed-ingest time, only when a store is already configured. To retain
originals, configure the store when you set the project up — `lumio-wiki
setup ./kb --publish-to s3://lumio-quickstart/helpdesk-kb --source-store
s3://lumio-private-sources/helpdesk` (a **separate, non-public** bucket;
retention is opt-in) — and re-ingest; `source fetch --source-id
support-runbook-2026 --output out.md` then returns the byte-exact original.

## 9. Publish an immutable S3 version (zero-index)

Publication validates, writes canonical content plus the Discovery Graph
under a version prefix, then advances the active pointer
(`current.json`) — compare-and-swap guarded:

```bash
lumio-wiki publish-s3 --version v1
```

```text
Published v1: 7 canonical file(s)
  fingerprint: 3d9475a1…
  location:    s3://lumio-quickstart/helpdesk-kb@v1
```

The destination came from `.env` (`LUMIO_PUBLISH_TO`, recorded by `setup`);
pass one explicitly to override. Published Versions are immutable — publishing
the same version again fails; publish a new version instead.

## 10. Reader: read-only project against S3

In a second directory, bind a read-only project to the Location. Pathless
commands resolve the active Published Version — no local copy of the KB:

```bash
mkdir ../helpdesk-reader && cd ../helpdesk-reader
lumio-wiki setup --from s3://lumio-quickstart/helpdesk-kb
lumio-wiki search "password reset" --limit 2     # pathless, reads v1
lumio-wiki related "Aurora Helpdesk" --scope canonical
lumio-wiki source inspect --source-id support-runbook-2026
```

The final command fails truthfully: a Reader of a Location with no private
Source Artifact Store gets an actionable error naming
`lumio-wiki setup --source-store`, never a fallback or a silent success.

## 11. LanceDB: expected zero-index fallback, then a healthy index

The **retrieval backend** (`zero-index` | `lancedb`) and the **retrieval
mode** (`lexical` | `semantic` | `hybrid`) are separate settings. Lexical
works on every backend; semantic/hybrid additionally need an embedder.

Request the LanceDB backend while the active version has no remote index
(v1 was published zero-index) — `status` discloses the **expected zero-index
fallback** instead of hiding it:

```bash
lumio-wiki setup --from s3://lumio-quickstart/helpdesk-kb --retrieval lancedb
lumio-wiki search "password reset" --limit 1    # still answers — zero-index
```

```text
lancedb_requested:           true
lancedb_healthy:             false
lancedb_fallback:            LanceDB index missing at s3://lumio-quickstart/helpdesk-kb/v1/derived/lance; zero-index page search over the same Published Version — publish a fresh version with 'lumio-wiki publish-s3 --retrieval lancedb'
```

Do exactly what the fallback says — publish `v2` with the remote index built
and health-checked **before** the pointer advances (needs `lumio-lancedb`),
using the CAS guard because `v1` is active:

```bash
cd ../helpdesk-project
lumio-wiki publish-s3 --version v2 --expected-pointer-version v1 --retrieval lancedb
cd ../helpdesk-reader
lumio-wiki setup --from s3://lumio-quickstart/helpdesk-kb --retrieval lancedb
```

```text
lancedb_requested:           true
lancedb_healthy:             true
lancedb_fingerprint_matches: true
```

The Reader resolved the new pointer automatically. Lexical mode now ranks
through LanceDB (BM25); the contract — results, `open:` actions — is
identical, only ranking quality changes. Semantic and hybrid stay separate
rungs and demand an embedder; without one the CLI refuses truthfully:

```bash
lumio-wiki search "reset link expiry" --mode semantic
```

```text
error: semantic/hybrid search needs an embedder: install 'lumio-lancedb[embeddings]' (local) or set LUMIO_PROVIDER_BASE_URL + LUMIO_PROVIDER_API_KEY + (LUMIO_EMBEDDING_MODEL or --model) for a remote endpoint.
```

Install `'lumio-lancedb[embeddings]'` (local sentence-transformers) or point
`LUMIO_PROVIDER_*` at an OpenAI-compatible `/embeddings` endpoint to enable
them — `--mode semantic` and `--mode hybrid` are then two separately
demonstrable rungs (the error text is identical until an embedder exists):

```bash
lumio-wiki search "reset link expiry" --mode semantic
lumio-wiki search "reset link expiry" --mode hybrid
```

To revert a bad activation, `lumio-wiki rollback-s3
s3://lumio-quickstart/helpdesk-kb --version v1 --expected-pointer-version v2`
re-points to a prior complete version (never rebuilds it); `lumio-wiki
cleanup-s3 s3://lumio-quickstart/helpdesk-kb` reports interrupted-build
residue and deletes nothing.

## 12. A grounded agent answer

This is what the journey was for. A coding agent in the Reader project (or a
Maintainer with `LUMIO_KB_PATH` exported) follows the retrieval ladder — hot
index, navigation index, search, page read — and answers **citing pages, not
memory**:

> **Q: How long is an Aurora password reset link valid?**
>
> The reset link expires after **30 minutes**.
>
> — Password Reset Runbook (`procedures/password_reset_runbook.md`):
> "The reset link expires after 30 minutes."
> Open: `lumio-wiki page "Password Reset Runbook"`
> Source artifact: `lumio-wiki source inspect --source-id support-runbook-2026`

Every domain claim carries a citation with an open action; a question the
Knowledge Base cannot support returns **"not covered by this knowledge
base"** — never a fabricated answer.

## AWS/S3 variant (production)

The same journey against real S3 drops only Part 1's local-endpoint
variables — credentials resolve through the standard AWS chain (environment,
`~/.aws`, instance role), so **no secrets are committed** and none are needed
in `.env` (setup writes locations, never credentials):

```bash
export AWS_REGION=eu-west-1                    # standard resolution; no keys exported
lumio-wiki setup ./kb --publish-to s3://example-public-kb/team-kb
lumio-wiki publish-s3 --version v1
lumio-wiki setup --from s3://example-public-kb/team-kb --retrieval lancedb
```

`example-public-kb` is a non-secret placeholder — substitute your bucket.
Keep the private Source Artifact Store (`--source-store`) in a **separate,
non-public** bucket/prefix if you use retention.

## Operating model notes

**Three states of `lumio.yaml`.** *Missing* → the Knowledge Base loads in
Legacy Flat Mode (Entity contract optional, no ontology validation, warning
emitted). *Empty file or empty mapping* → treated the same as missing for
content; establish the Control File deliberately. *Valid-empty* (what `setup`
seeds) → version 2 with the category catalog and an **empty ontology**: fully
valid, and sufficient until the first `entity_types`/`claims` declaration —
from then on the vocabulary must be declared (Part 3).

**Backend vs mode.** `LUMIO_RETRIEVAL_BACKEND` = `zero-index` (deterministic,
in-memory, always available) or `lancedb` (BM25/vector/hybrid ranking; needs
the adapter and, for remote indexes, a published `derived/lance/`). The
retrieval *mode* (`--mode lexical|semantic|hybrid`) picks how a query runs;
`semantic`/`hybrid` require the LanceDB backend **and** an embedder. A missing
or unhealthy LanceDB index never breaks reads — `status` discloses the
zero-index fallback and the exact repair command.

**Local authoring vs immutable S3 reading.** Maintainers author, ingest, and
publish **in a local worktree**; `publish-s3` writes one immutable version
prefix and advances a tiny pointer. Readers never write: `setup --from` binds
read-only and every pathless read resolves the active Published Version once.
Never point a Maintainer worktree at the S3 prefix as if it were a filesystem.

## Where to go next

- [`docs/usage.md`](usage.md) — full CLI, Python library, and coding-agent
  reference.
- [`docs/kb-format.md`](kb-format.md) — every frontmatter field and validation
  rule.
- [`docs/chat-sources.md`](chat-sources.md) — private document chat in the web
  app.
- [`examples/onboarding-journey/`](../examples/onboarding-journey/) — the
  scriptable journey CI runs.
journey/) — the
  scriptable journey CI runs.
