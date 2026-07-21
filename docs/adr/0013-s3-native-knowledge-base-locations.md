---
status: accepted
amends: ADR-0001, ADR-0010, ADR-0011
---

# ADR-0013: S3-native Knowledge Base Locations and remote derived indexes

Lumio must let the web application, `lumio-wiki` CLI, and local coding agents
read the same published Knowledge Base directly from S3 without a durable local
copy. We will make a Knowledge Base Location resolve an immutable Published
Version, with local filesystem and optional S3 implementations. The S3
implementation will use `obstore` behind an opt-in `lumio-wiki[s3]` capability;
it uses object-level reads, listings, and conditional writes rather than a
filesystem abstraction or implicit disk cache.

Each S3 publication writes canonical Markdown, control files, and derived
artifacts under a new immutable version prefix with a manifest containing
relative paths, content digests, and the Knowledge Base fingerprint. After
validation and derived-index health checks, the publisher conditionally updates
a small `current.json` pointer. Readers resolve that pointer once and use only
its version prefix, so they cannot observe a partial publication. The Discovery
Graph remains a rebuildable MessagePack artifact. `lumio-lancedb` connects
directly to an immutable S3 `derived/lance/` prefix using LanceDB storage
options; its metadata belongs in the version's derived manifest. If that index
is unavailable, retrieval falls back to zero-index search over the same
snapshot and says so in its Retrieval Trace.

`lumio.yaml` remains portable Knowledge Base content and never carries S3
credentials. Credentials, region, endpoint, and cache policy belong to the
app/CLI configuration. The default S3 cache policy writes no managed Knowledge
Base or derived-index bytes to local disk; a bounded in-memory cache may be
added later. App-private SQLite/Piccolo state and Conversation Recall stay out
of this decision.

## Considered options

- **Ephemeral filesystem materialization** — rejected as the primary design:
  it is a smaller change but prevents direct native object-store use by the
  standalone CLI and coding agents.
- **`fsspec`/`s3fs` as the Core SDK boundary** — rejected: their file-like and
  cache-oriented model does not express the immutable-version and
  compare-and-swap publication protocol as directly. They may be introduced as
  explicit compatibility adapters later.
- **`boto3` directly** — rejected: it is AWS-specific, while `obstore` supplies
  the needed object-store operations for S3 and compatible backends without
  making an AWS SDK the Core SDK contract.

## Consequences

The current `Path`-only loading, fingerprint, graph, and LanceDB index APIs
must gain typed local-or-remote locations while preserving filesystem behavior.
Tests require an in-memory object-store contract plus MinIO integration coverage
for `obstore`, direct CLI reads, remote LanceDB, publication conflicts, corrupt
manifests, and no managed disk cache. Existing local and zero-index deployments
remain supported.
