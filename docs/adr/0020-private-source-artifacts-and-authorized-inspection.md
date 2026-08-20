---
status: proposed
amends: ADR-0014
---

# ADR-0020: Private Source Artifacts and authorized inspection

## Context

A Compiled Page records which Knowledge Sources support it, but managed
host-agent ingest currently retains only private source identity and a content
hash. The original PDF, Office document, image, HTML, or text bytes are not
retained. An authorized user can inspect the Compiled Page and its provenance
identifier but cannot retrieve the exact original Source Version to judge the
underlying material.

Source inspection is important for trust, but raw files may be confidential,
licensed, malicious, or unsafe to render. They must not become public Knowledge
Base content merely because they are retained. A source identifier alone is
also insufficient: the active Source Version can change while a Reader remains
bound to an older immutable Published Version.

## Decision

### Introduce an optional private Source Artifact

A **Source Artifact** is the immutable original byte content retained for one
Source Version. It is addressed and integrity-checked by SHA-256, but the
authoritative identity is `(source_id, content_hash)`. Equal bytes may be
physically deduplicated without merging distinct Knowledge Source identities.

Source Artifact retention is optional and disabled when no Source Artifact
Store is configured. When configured, managed ingest uploads original bytes
create-only, verifies stored size and digest, and records the artifact binding
in the private Knowledge Source Registry. Upload and registry mutation form an
idempotent saga: a failed upload never fabricates a successful binding, and an
uploaded object without a binding is a recoverable orphan.

The Source Version remains valid identity/provenance state even if its artifact
is temporarily unavailable. When artifact retention is required for a
publication, activation blocks until every referenced non-synthetic source has
a verified artifact. Existing deployments may leave retention disabled until
historical sources are backfilled.

### Keep Source Artifacts separate from Published Knowledge Base content

The Source Artifact Store is a separate private storage location, configured
independently from the Knowledge Base Location. A separate bucket and KMS key
are recommended in production so IAM can deny source access even to principals
that may read compiled knowledge. A private prefix in the same bucket is a
development convenience, not the recommended trust boundary.

Source Artifacts and their registry/binding metadata are excluded from:

- canonical Compiled Pages and canonical fingerprints;
- public Knowledge Base exports and OKF bundles;
- zero-index, BM25, semantic, hybrid, and graph retrieval;
- Navigation and Hot Indexes; and
- ordinary Reader credentials and diagnostics.

Credentials and encryption configuration remain deployment state and never
enter `lumio.yaml`.

### Bind each Published Version to exact Source Versions privately

Publication writes a private Source Binding Manifest in the Source Artifact
Store before activating the public Published Version. The manifest binds the
Published Version identity and each referenced `(page, source_id)` pair to the
exact Source Version content hash and safe metadata needed for inspection. It
contains no raw bytes and is not part of the portable Knowledge Base.

This binding ensures that inspecting a source from an older Published Version
returns the original bytes used for that version, not the Knowledge Source's
newest bytes. The public Compiled Page may continue to carry portable source
identifiers without exposing private object keys or registry state.

### Source inspection is authorized retrieval, not Evidence

`lumio-wiki source inspect` reports secret-free metadata and availability for a
source bound to the selected Published Version. `lumio-wiki source fetch`
retrieves the byte-exact artifact through registry and binding lookup, verifies
its digest again, and writes it to an explicit local destination for the coding
harness or user to open with an appropriate tool.

`lumio-wiki source link` may explicitly issue a short-lived, signed GET URL for
that same exact binding when the Source Artifact Store supports signing. The
link is a temporary bearer credential: it defaults to five minutes, is capped
at one hour, is never persisted or included in ordinary logs, and is redacted
as a secret-bearing URL. Its signer credentials must already authorize the
object read. Stored response metadata should force download with a safe
filename rather than inline rendering. Public, permanent, unsigned, wildcard,
or prefix-level source URLs are never issued.

For coding agents, byte-exact fetch is the default because a signed URL can leak
through conversation history. After fetch, the harness may read bounded text
from text/CSV/JSON/XML artifacts or pass PDF/Office/image content to an
appropriate sandboxed document tool. The fetched file is the exact original;
any decoded, OCR, or converted representation is derived output and must be
identified as such. An agent never executes macros, scripts, or active HTML and
never dumps a large private source into model context without an explicit user
request.

Safe filenames, content disposition, size limits, encryption in transit/at
rest, and object-store authorization apply at the trust boundary. Ordinary
output never prints private object keys or credentials.

A Source Artifact is not Evidence, and source inspection does not claim that a
specific raw-file passage supports a specific answer sentence. Citations remain
grounded in Compiled Page Evidence. An agent may return an exact quotation when
it can identify the original text and stable source coordinates; otherwise it
must label converted text as derived. General exact raw passage highlighting
requires separately reviewed claim- or span-level lineage and remains out of
scope.

Retiring a Knowledge Source does not delete historical Source Artifacts:
existing Published Versions must remain inspectable. Deletion is explicit,
policy-controlled, and must disclose which Published Versions lose inspection
coverage.

## Considered Options

- **Store raw files inside each public Published Version** — rejected. It mixes
  private ingest material with portable Reader content, duplicates large files,
  and makes accidental export likely.
- **Store only a URL in `sources[].url`** — rejected as the retention model.
  External URLs can expire, change content, require unrelated credentials, or
  reveal private locations. They remain useful optional provenance metadata.
- **Issue permanent or public Source Artifact URLs** — rejected. A short-lived
  signed URL is an explicit delivery mechanism, not stored provenance; its
  bearer authority expires and is limited to one exact object and GET method.
- **Resolve inspection to the registry's latest Source Version** — rejected.
  It can show different bytes from those behind the selected Published Version.
- **Expose fetch-by-hash** — rejected. Callers resolve through source identity
  and publication binding; a bare digest is not an authorization or provenance
  boundary.
- **Render uploaded HTML/PDF directly in the Core SDK** — rejected. The portable
  SDK returns verified bytes; presentation clients own sandboxed rendering.
- **Treat raw files as citation-ready Evidence** — rejected until exact
  claim/span lineage exists and is reviewed.

## Consequences

Authorized users and coding agents can inspect the exact original Source
Version behind a Compiled Page without weakening public export, retrieval, or
citation guardrails. Deployments that do not want raw retention keep today's
hash-only behavior.

The feature adds private storage lifecycle, IAM, encryption, orphan recovery,
and historical-retention obligations. It also exposes an existing limitation
honestly: source-level provenance enables document inspection, while exact
claim-to-source-passage lineage remains future work.
