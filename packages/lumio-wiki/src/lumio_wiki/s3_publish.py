"""S3 Knowledge Base publication — write immutable Published Versions.

Issue #121, ADR-0013. This is the opt-in ``lumio-wiki[s3]`` capability's write
path: a Maintainer publishes a complete immutable S3 **Published Version**
containing canonical Knowledge Base content and a derived Discovery Graph
artifact. The reader from :mod:`lumio_wiki.s3_location` continues using its old
Snapshot until a **conditional activation pointer** safely exposes the new
version.

Publication protocol (ADR-0013):

1. **Validate** — load and validate canonical Knowledge Base content from a
   filesystem source through the shared Core SDK seam, and materialize + validate
   the derived Discovery Graph state (fingerprint and extractor version match).
2. **Write** — materialize every canonical file, the derived graph artifact, and a
   content-digest manifest under a new immutable version prefix
   (``{prefix}/{version}/...``). The graph lives in the version's ``derived/``
   area and is **never** part of the manifest's canonical file list.
3. **Activate** — conditionally advance ``current.json`` to the new version using
   an expected-version conditional put (compare-and-swap on the pointer's ETag).
   The pointer advances **only after** the version is fully written and
   validated.

Concurrent publication is detected by the compare-and-swap: if another publisher
already advanced the pointer, :class:`S3PublicationConflict` is raised and the
previously active Published Version stays intact (the loser's version prefix is
written but never activated).

Credentials, region, and endpoint live on the obstore ``ObjectStore`` /
client config — never here and never in ``lumio.yaml``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from lumio_wiki.graph_state import (
    GRAPH_ARTIFACT_FILENAME,
    deserialize_graph,
)
from lumio_wiki.knowledge_base import (
    KnowledgeBaseError,
    _FilesystemKbSource,
    _load_and_validate,
    canonical_content,
    fingerprint_sources,
)
from lumio_wiki.records import EXTRACTOR_VERSION, SourceFingerprint
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    DERIVED_DIR,
    MANIFEST_OBJECT,
    S3Manifest,
    S3Pointer,
    _join,
    _require_obstore,
    build_published_manifest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from obstore.store import ObjectStore

__all__ = ["S3PublicationConflict", "publish_s3_version"]


class S3PublicationConflict(KnowledgeBaseError):
    """Raised when a concurrent publication already advanced the active pointer.

    The publisher's version prefix may have been written, but ``current.json``
    was **not** advanced: the previously active Published Version stays intact.
    The caller should re-read the pointer and retry from the now-active version.
    """


def publish_s3_version(
    store: ObjectStore,
    prefix: str,
    *,
    source_root: str | Path,
    version: str,
    expected_pointer_version: str | None = None,
) -> S3Manifest:
    """Publish a filesystem Knowledge Base as an immutable S3 Published Version.

    Loads and validates canonical content and the derived Discovery Graph state,
    writes them under a new immutable version prefix (``{prefix}/{version}/...``)
    alongside a content-digest manifest, then conditionally advances
    ``current.json`` to the new version. The pointer advances **only after** the
    version is fully written and validated.

    Concurrent publication is detected via an expected-version conditional put
    (compare-and-swap on the pointer's ETag): if another publisher already
    advanced the pointer, :class:`S3PublicationConflict` is raised and the
    previously active Published Version stays intact.

    Parameters
    ----------
    store:
        An obstore ``ObjectStore`` (``S3Store``, ``MemoryStore``, ...). The
        publisher uses object-level puts and a conditional pointer put.
    prefix:
        The object-key prefix the Knowledge Base root lives under (relative to
        the store's own root).
    source_root:
        The local filesystem Knowledge Base root to publish. Read-only: the
        publisher never mutates the source tree.
    version:
        The immutable version label for the new Published Version (e.g. a
        timestamp, semver, or content hash). It becomes the version prefix and
        the value ``current.json`` points at.
    expected_pointer_version:
        The version the caller expects to be replacing (the active version it
        observed before publishing). ``None`` means this is the first
        publication (no pointer expected). The compare-and-swap still guards
        against concurrent first-publication races.

    Returns
    -------
    S3Manifest
        The manifest written for the new immutable version.

    Raises
    ------
    KnowledgeBaseError
        If canonical content fails validation or the derived graph state is
        invalid.
    S3PublicationConflict
        If a concurrent publication already advanced the active pointer.
    """
    obstore = _require_obstore()
    clean_prefix = str(prefix).strip("/")
    root = Path(source_root)

    # 1. Validate canonical content and compute the Published Version identity.
    kb, report = _load_and_validate(_FilesystemKbSource(root.resolve()))
    if not report.is_valid:
        blocking = sum(1 for issue in report.issues if issue.severity == "error")
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: canonical Knowledge Base content at "
            f"{root!s} has {blocking} blocking validation error(s)"
        )
    fingerprint = fingerprint_sources(root)
    content = canonical_content(_FilesystemKbSource(root.resolve()))

    # 2. Materialize and validate the derived Discovery Graph state.
    graph_bytes = _materialize_graph_bytes(kb)
    _validate_graph_state(graph_bytes, fingerprint, version)

    # 3. Write the immutable version prefix (content, derived graph, manifest).
    manifest = build_published_manifest(version, fingerprint.digest, content)
    _write_version_prefix(
        obstore, store, clean_prefix, version, content, manifest, graph_bytes
    )

    # 4. Conditionally advance the active pointer.
    _advance_pointer(obstore, store, clean_prefix, version, expected_pointer_version)
    return manifest


# ---------------------------------------------------------------------------
# Graph materialization + validation.
# ---------------------------------------------------------------------------


def _materialize_graph_bytes(kb: Any) -> bytes:
    """Serialize the Knowledge Base's Discovery Graph to MessagePack bytes.

    Reuses the public ``KnowledgeBase.materialize_graph`` serialization path
    (the exact one the filesystem index uses) so the published artifact is
    byte-identical to a locally materialized one. The temp directory is cleaned
    up after reading the bytes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        artifact = kb.materialize_graph(tmp)
        return Path(artifact).read_bytes()


def _validate_graph_state(
    graph_bytes: bytes, fingerprint: SourceFingerprint, version: str
) -> None:
    """Validate the materialized Discovery Graph state before publishing it.

    The graph is derived state; it must decode, match the Knowledge Base
    fingerprint, and target the current extractor version. A failure here is a
    publisher bug, but the gate enforces the ADR-0013 contract that the pointer
    advances only after the derived graph state is valid.
    """
    state = deserialize_graph(graph_bytes)
    if state is None:
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: the derived Discovery Graph artifact "
            f"failed to validate"
        )
    if state.fingerprint_digest != fingerprint.digest:
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: the derived Discovery Graph fingerprint "
            f"{state.fingerprint_digest!r} does not match the Knowledge Base "
            f"fingerprint {fingerprint.digest!r}"
        )
    if state.extractor_version != EXTRACTOR_VERSION:
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: the derived Discovery Graph extractor "
            f"version {state.extractor_version!r} does not match the current "
            f"extractor version {EXTRACTOR_VERSION!r}"
        )


# ---------------------------------------------------------------------------
# Immutable version-prefix writer.
# ---------------------------------------------------------------------------


def _write_version_prefix(
    obstore: Any,
    store: ObjectStore,
    prefix: str,
    version: str,
    content: dict[str, bytes],
    manifest: S3Manifest,
    graph_bytes: bytes,
) -> None:
    """Write every canonical file, the derived graph, and the manifest.

    The manifest is written **last**, after all its referenced content, so an
    interrupted publication can never produce a valid manifest whose content is
    absent. A reader that finds the pointer pointing here before activation never
    observes a partial version: activation is gated by the conditional pointer
    advance, which runs after this returns.
    """
    # Canonical files at their relative paths under the version prefix.
    for rel, raw in content.items():
        obstore.put(store, _join(prefix, version, rel), raw)
    # Derived Discovery Graph artifact: rebuildable state, never canonical.
    graph_key = _join(prefix, version, DERIVED_DIR, GRAPH_ARTIFACT_FILENAME)
    obstore.put(store, graph_key, graph_bytes)
    # The manifest (last).
    manifest_key = _join(prefix, version, MANIFEST_OBJECT)
    obstore.put(store, manifest_key, msgspec.json.encode(manifest))


# ---------------------------------------------------------------------------
# Conditional pointer advance (compare-and-swap).
# ---------------------------------------------------------------------------


def _read_current_pointer(
    obstore: Any, store: ObjectStore, key: str
) -> tuple[str | None, str | None]:
    """Return ``(version, e_tag)`` of the current pointer, or ``(None, None)``.

    The e_tag drives the compare-and-swap on update. ``None`` for both means no
    pointer exists yet (first publication).
    """
    try:
        meta = obstore.head(store, key)
    except FileNotFoundError:
        return None, None
    except Exception as exc:  # pragma: no cover - defensive: unexpected store error
        raise KnowledgeBaseError(
            f"could not read activation pointer {key!r}: {exc}"
        ) from exc
    etag = meta["e_tag"] if isinstance(meta, dict) else getattr(meta, "e_tag", None)
    raw = _get_pointer_bytes(obstore, store, key)
    try:
        pointer = msgspec.json.decode(raw, type=S3Pointer)
    except msgspec.DecodeError as exc:
        raise KnowledgeBaseError(
            f"activation pointer {key!r} is malformed: {exc}"
        ) from exc
    return pointer.version, etag


def _get_pointer_bytes(obstore: Any, store: ObjectStore, key: str) -> bytes:
    """Read the raw pointer bytes (the head already proved it exists)."""
    try:
        result = obstore.get(store, key)
    except Exception as exc:  # pragma: no cover - head proved existence
        raise KnowledgeBaseError(
            f"could not read activation pointer {key!r}: {exc}"
        ) from exc
    return bytes(result.bytes())


def _advance_pointer(
    obstore: Any,
    store: ObjectStore,
    prefix: str,
    version: str,
    expected_pointer_version: str | None,
) -> None:
    """Conditionally advance ``current.json`` via compare-and-swap on its ETag.

    First publication uses a create-if-absent put; subsequent publications use
    an update-if-match-ETag put. Either way, a concurrent publication that
    already touched the pointer is detected and raises
    :class:`S3PublicationConflict`, leaving the previously active Published
    Version intact.
    """
    key = _join(prefix, CURRENT_POINTER_OBJECT)
    pointer_bytes = msgspec.json.encode(S3Pointer(version=version))

    existing_version, existing_etag = _read_current_pointer(obstore, store, key)

    if existing_version is None:
        # First publication: the pointer must not exist yet.
        if expected_pointer_version is not None:
            raise S3PublicationConflict(
                f"cannot publish {version!r}: expected to advance the pointer from "
                f"{expected_pointer_version!r}, but no pointer exists yet"
            )
        _conditional_create(obstore, store, key, pointer_bytes, version)
        return

    # An explicit expectation must match the live pointer.
    if (
        expected_pointer_version is not None
        and expected_pointer_version != existing_version
    ):
        raise S3PublicationConflict(
            f"cannot publish {version!r}: expected the active pointer to be "
            f"{expected_pointer_version!r} but it is {existing_version!r}"
        )

    _conditional_update(
        obstore, store, key, pointer_bytes, existing_etag, version, existing_version
    )


def _conditional_create(
    obstore: Any,
    store: ObjectStore,
    key: str,
    pointer_bytes: bytes,
    version: str,
) -> None:
    """Create the pointer only if it does not already exist."""
    from obstore.exceptions import AlreadyExistsError

    try:
        obstore.put(store, key, pointer_bytes, mode="create")
    except AlreadyExistsError as exc:
        raise S3PublicationConflict(
            f"cannot publish {version!r}: a concurrent publication created the "
            f"activation pointer first"
        ) from exc


def _conditional_update(
    obstore: Any,
    store: ObjectStore,
    key: str,
    pointer_bytes: bytes,
    etag: str | None,
    version: str,
    existing_version: str,
) -> None:
    """Advance the pointer only if its ETag still matches the one we observed."""
    from obstore.exceptions import PreconditionError

    if etag is None:
        # No ETag available: the store cannot support a safe compare-and-swap.
        # Fall back to an unconditional overwrite. The expected-version check
        # above still guards against an explicit mismatch; the ETag-less path is
        # only reached by stores that never return ETags (not the obstore
        # backends this capability targets).
        obstore.put(store, key, pointer_bytes, mode="overwrite")
        return
    try:
        obstore.put(
            store, key, pointer_bytes, mode={"mode": "update", "e_tag": etag}
        )
    except PreconditionError as exc:
        raise S3PublicationConflict(
            f"cannot publish {version!r}: a concurrent publication already "
            f"advanced the activation pointer from {existing_version!r}"
        ) from exc
