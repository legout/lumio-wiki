# LanceDB S3-backed storage research

_Date: 2026-07-21. Scope: LanceDB 0.34.0 as installed by Lumio._

## Findings

- LanceDB supports an OSS database rooted at an object-store URI such as
  `s3://bucket/prefix`; its Python `connect` API accepts `storage_options` for
  credential and endpoint configuration. This can keep Lance tables in S3
  rather than in a local table directory.
- Readers against object storage do not automatically observe changes written
  by another process unless a read-consistency interval is configured. Setting
  `read_consistency_interval=timedelta(0)` provides the strongest documented
  cross-process visibility at the cost of additional object-store checks,
  latency, and request cost.
- Object storage does not make a publication protocol unnecessary. Lumio should
  serialize publishers or use optimistic version/pointer validation; it should
  not rely on filesystem rename semantics or raw concurrent writes to a shared
  prefix.

## Implications for Lumio today

Remote LanceDB is technically viable, but Lumio does not currently support it:

- `KnowledgeBase.build_index()` normalizes its index location as a local
  `Path`.
- `LanceDBRetrievalAdapter` likewise coerces its input to `Path` before calling
  `lancedb.connect`, which destroys an `s3://` URI.
- `SharedStorageBackend` is intentionally a local-directory adapter and makes
  copies plus atomic directory replacements.
- The Knowledge Base loader, app sync flow, and Conversation Recall index also
  assume local filesystem paths.

Therefore, changing only the LanceDB connection would enable a remote derived
index but would not make Lumio remote-only. A remote Knowledge Base source and
an object-store-safe versioned publication protocol are separate work.

## S3 client evaluation

**Recommended: `obstore` as an opt-in `lumio-wiki[s3]` dependency.** It is an
object-store-native API for S3 and compatible backends: `head`, list, byte and
range reads, and conditional puts (`create` and version-checked update) match
Lumio's versioned bundle and compare-and-swap pointer protocol. Its async APIs
also avoid requiring the Core SDK to force a synchronous file-like interface.

`fsspec`/`s3fs` are not the preferred Core SDK boundary. They are valuable
compatibility adapters and offer file/block caching, but their filesystem-like
surface and cache wrappers are a poor default for a remote-only source. Lumio
may add an explicit, opt-in cache policy later; it should never be implicit.

LanceDB continues to connect directly to its own S3 URI with LanceDB
`storage_options`; Lumio does not need to pass it an `obstore` client.

## Sources

- LanceDB Python `connect` API and storage-options documentation, installed
  package source: `.venv/lib/python3.14/site-packages/lancedb/__init__.py`
  (version 0.34.0, lines 69–238).
- [LanceDB storage configuration](https://docs.lancedb.com/storage/configuration)
- [LanceDB storage overview](https://docs.lancedb.com/storage)
- [LanceDB OSS FAQ](https://docs.lancedb.com/faq/faq-oss)
- [obstore reads and ranges](https://developmentseed.org/obstore/latest/api/get/)
- [obstore conditional puts](https://developmentseed.org/obstore/latest/api/put/)
- [fsspec caching and transactions](https://filesystem-spec.readthedocs.io/en/latest/features.html)
- [s3fs](https://s3fs.readthedocs.io/en/stable/)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
