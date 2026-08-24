"""Remote LanceDB index publication for S3 Published Versions (issue #163).

``lumio-wiki`` publishes one complete immutable S3 Published Version and gates
activation on every requested artifact, but it never imports ``lumio-lancedb``
(ADR-0010). This module is the concrete :data:`lumio_wiki.s3_publish.IndexBuilder`
the publisher is injected with: it builds the lexical (and optionally semantic)
LanceDB tables directly under the version's ``derived/lance/`` prefix, writes
the fingerprint / embedding-model sidecars beside them, health-checks the index
through a **fresh** connection, and returns the completion metadata the
publisher records before activation.

The builder receives the publisher's obstore ``ObjectStore`` (already
authenticated) for its metadata sidecars, and connects LanceDB itself through
its own ``storage_options`` — Lumio never hands its obstore client to LanceDB
(ADR-0013).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lumio_lancedb.graph import (
    ENTITY_TABLE_NAME,
    GRAPH_EDGE_TABLE_NAME,
    build_graph_tables,
)
from lumio_lancedb.index import (
    PAGE_TABLE_NAME,
    TABLE_NAME,
    VECTOR_TABLE_NAME,
    LanceDBRetrievalAdapter,
)
from lumio_lancedb.location import RemoteIndexLocation
from lumio_wiki.s3_publish import IndexBuilder, RemoteIndexCompletion

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from obstore.store import ObjectStore

    from lumio_wiki.embeddings import Embedder

__all__ = ["remote_publication_builder"]


def remote_publication_builder(
    *,
    store_uri: str,
    storage_options: dict[str, str] | None,
    embedder: Embedder | None = None,
) -> IndexBuilder:
    """Build the dependency-neutral index builder ``publish_s3_version`` takes.

    Parameters
    ----------
    store_uri:
        The object-store container URI the publisher's store is rooted at
        (e.g. ``s3://bucket``). The per-version index URI is derived by
        appending the publisher-supplied ``sidecar_prefix``.
    storage_options:
        LanceDB's own S3 connection options (region, endpoint, credentials,
        ``allow_http`` for MinIO) — never the obstore client.
    embedder:
        Optional embedder; when provided a semantic vector table is built and
        its model identity lands in the completion metadata. ``None`` builds
        the lexical BM25 + page tables only.

    The returned closure raises on any build or health failure, which blocks
    activation of the Published Version (issue #163).
    """

    def _build(
        *,
        store: ObjectStore,
        sidecar_prefix: str,
        pages: list,
        fingerprint: Any,
        kb: Any = None,
    ) -> RemoteIndexCompletion:
        index_uri = f"{store_uri.rstrip('/')}/{sidecar_prefix.strip('/')}"
        location = RemoteIndexLocation(
            index_uri,
            storage_options=storage_options,
            store=store,
            sidecar_prefix=sidecar_prefix,
        )
        adapter = LanceDBRetrievalAdapter()
        adapter.build_index(list(pages), location, embedder=embedder, fingerprint=fingerprint)
        # Graph projections (issue #171): materialize entities/graph_edges
        # from the loaded, fingerprinted Knowledge Base snapshot the
        # publisher validated — never from a partially read page list.
        if kb is not None:
            build_graph_tables(kb, location, fingerprint)

        # Health-check through a FRESH connection before reporting completion:
        # every requested table must exist and be row-countable. The graph
        # tables are requested whenever the publisher supplied the Knowledge
        # Base, so a missing/incomplete graph projection blocks activation
        # (issue #171: requested tables complete before activation).
        db = location.connect()
        present = set(db.list_tables().tables)
        required = {TABLE_NAME, PAGE_TABLE_NAME}
        if embedder is not None:
            required.add(VECTOR_TABLE_NAME)
        if kb is not None:
            required |= {ENTITY_TABLE_NAME, GRAPH_EDGE_TABLE_NAME}
        missing = required - present
        if missing:
            raise RuntimeError(
                f"remote LanceDB index at {index_uri} is unhealthy: missing "
                f"table(s) {', '.join(sorted(missing))}"
            )
        try:
            tables = {
                name: int(db.open_table(name).count_rows()) for name in sorted(present)
            }
        except Exception as exc:
            raise RuntimeError(
                f"remote LanceDB index at {index_uri} is unhealthy: could not "
                f"read table rows: {exc}"
            ) from exc

        return RemoteIndexCompletion(
            fingerprint=fingerprint.digest,
            model=embedder.model_info if embedder is not None else None,
            tables=tables,
        )

    return _build
