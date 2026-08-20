"""S3 Knowledge Base publication — publish complete immutable Published Versions.

Issues #121 and #163, ADR-0013/ADR-0019. This is the opt-in
``lumio-wiki[s3]`` capability's write path: a Maintainer publishes one complete
immutable S3 **Published Version** containing canonical Knowledge Base content,
a derived Discovery Graph artifact, and — when requested — a remote LanceDB
index, with atomic compare-and-swap activation and explicit rollback.

Publication protocol (ADR-0013, ADR-0019):

1. **Observe** — read and capture the active pointer version plus its ETag
   *before* any expensive preparation. The captured observation is the
   compare-and-swap intent used at activation; the pointer is never re-read,
   so a publisher that raced with a concurrent activation fails closed instead
   of silently overwriting it.
2. **Preflight** — require a previously absent version prefix. Interrupted or
   retried builds never mutate an existing immutable version (create-only puts
   remain the hard guarantee; the preflight gives an early, actionable error).
3. **Prepare** — load and validate canonical content through the shared Core
   SDK seam and serialize + validate the derived Discovery Graph state.
4. **Build** — write every canonical file and the graph artifact under the new
   version prefix (create-only), then build the requested remote LanceDB index
   directly under ``{prefix}/{version}/derived/lance/`` through an injected,
   dependency-neutral builder. The builder writes its tables and fingerprint /
   embedding-model sidecars, health-checks the index through a fresh
   connection, and returns completion metadata (fingerprint, optional model
   identity, table row counts). The publisher records that metadata as
   ``derived/lance/completion.json`` and digest-protects it in the manifest.
   Any build or health failure blocks activation; LanceDB's absence remains
   valid when no builder was requested.
5. **Activate** — run the optional pre-activation hook (the extension point for
   the private Source Binding Manifest of the Source Artifact work, #164 /
   ADR-0020: no source bytes ever enter the public manifest), then advance
   ``current.json`` with **one** conditional put against the originally
   observed pointer. A backend that cannot supply an ETag for the existing
   pointer fails closed — publication is refused rather than falling back to
   last-write-wins.

Rollback is an explicit compare-and-swap activation of an already complete,
validated immutable version: it never rebuilds or overwrites the target
version, and a stale rollback (the pointer moved since it was observed) fails
closed. Inactive version prefixes without a manifest are reported as cleanup
candidates by :func:`list_cleanup_candidates`; this module never deletes them.

Credentials, region, and endpoint live on the obstore ``ObjectStore`` /
client config — never here and never in ``lumio.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import msgspec

from lumio_wiki.graph_state import (
    GRAPH_ARTIFACT_FILENAME,
    deserialize_graph,
    serialize_graph,
)
from lumio_wiki.knowledge_base import (
    KnowledgeBaseError,
    _FilesystemKbSource,
    _load_and_validate,
    canonical_content,
    fingerprint_sources,
)
from lumio_wiki.records import EXTRACTOR_VERSION, EmbeddingModelInfo, SourceFingerprint
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    DERIVED_DIR,
    MANIFEST_OBJECT,
    S3Location,
    S3Manifest,
    S3Pointer,
    _join,
    _require_obstore,
    build_published_manifest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from obstore.store import ObjectStore

    from lumio_wiki.records import CompiledPage, EmbeddingModelInfo

__all__ = [
    "ActivationHook",
    "CleanupCandidate",
    "IndexBuilder",
    "PointerObservation",
    "PreparedVersion",
    "RemoteIndexCompletion",
    "S3PublicationConflict",
    "list_cleanup_candidates",
    "observe_current_pointer",
    "publish_s3_version",
    "rollback_s3_version",
]

# The relative path of the Discovery Graph artifact under a version prefix.
_GRAPH_REL = f"{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}"

# The remote LanceDB index lives directly under the version's derived area
# (ADR-0019: "build requested LanceDB directly under <version>/derived/lance/").
LANCE_DERIVED_DIR = f"{DERIVED_DIR}/lance"

# The completion metadata the publisher records beside the built index: the
# canonical fingerprint, the optional embedding-model identity, and the table
# row counts observed by the builder's post-build health check. Written by the
# publisher (not the builder) so the activation gate owns it, and recorded in
# the manifest's ``derived_files`` so Readers can integrity-check it.
LANCE_COMPLETION_OBJECT = "completion.json"

# Type aliases for the dependency-neutral publication seams. ``lumio-wiki``
# never imports ``lumio-lancedb`` (ADR-0010): callers inject a builder closure
# (constructed e.g. from ``lumio_lancedb.remote_publication_builder``) and the
# publisher calls it duck-typed.
IndexBuilder = Callable[..., "RemoteIndexCompletion"]
"""``index_builder(store=..., sidecar_prefix=..., pages=..., fingerprint=...)``.

Builds a remote LanceDB index under ``{prefix}/{version}/derived/lance/``,
health-checks it through a fresh connection, and returns completion metadata.
Any exception blocks activation."""

ActivationHook = Callable[["PreparedVersion"], None]
"""Runs after the version is complete but before the pointer advances.

The extension point for the private Source Binding Manifest of the Source
Artifact work (#164, ADR-0020): the hook writes private binding state to the
separate Source Artifact Store. Raising blocks activation. No source bytes
enter the public manifest."""


class S3PublicationConflict(KnowledgeBaseError):
    """Raised when a concurrent publication already advanced the active pointer.

    The publisher's version prefix may have been written, but ``current.json``
    was **not** advanced: the previously active Published Version stays intact.
    The caller should re-read the pointer and retry from the now-active version.
    """


class PointerObservation(msgspec.Struct, frozen=True):
    """The active pointer as observed *before* expensive preparation.

    ``version`` is ``None`` when no pointer exists yet (first publication);
    ``e_tag`` drives the compare-and-swap at activation and is fail-closed when
    a pointer exists but the store cannot supply one.
    """

    version: str | None
    e_tag: str | None


class RemoteIndexCompletion(msgspec.Struct, frozen=True):
    """Completion metadata for a requested remote LanceDB index.

    Returned by the injected index builder after it built the tables and
    health-checked them through a fresh connection. The publisher verifies the
    fingerprint against the canonical Published Version fingerprint, then
    records this verbatim as ``derived/lance/completion.json``.
    """

    fingerprint: str
    """The canonical Knowledge Base fingerprint the index was built from."""

    model: EmbeddingModelInfo | None = None
    """Identity of the embedding model behind the vector table, when built."""

    tables: dict[str, int] = msgspec.field(default_factory=dict)
    """Table name -> row count observed by the post-build health check."""


class PreparedVersion(msgspec.Struct, frozen=True):
    """A fully built, validated, not-yet-activated Published Version.

    Passed to the :data:`ActivationHook` so the Source Artifact work (#164)
    can bind the exact Published Version identity before Readers see it.
    """

    prefix: str
    """The Knowledge Base root prefix the version lives under."""

    version: str
    """The immutable version label."""

    fingerprint: str
    """The canonical Published Version fingerprint."""

    manifest: S3Manifest
    """The manifest written for the version (activation is next)."""


class CleanupCandidate(msgspec.Struct, frozen=True):
    """An inactive, incomplete version prefix reported for cleanup.

    A version prefix without a manifest can never be activated (the manifest is
    written last): it is the residue of an interrupted or failed publication.
    Reporting never deletes anything (ADR-0019: a cleanup operation *may* later
    remove them; this module only reports).
    """

    version: str
    object_count: int


# ---------------------------------------------------------------------------
# Publication: observe -> preflight -> prepare -> build -> validate -> activate.
# ---------------------------------------------------------------------------


def publish_s3_version(
    store: ObjectStore,
    prefix: str,
    *,
    source_root: str | Path,
    version: str,
    expected_pointer_version: str | None = None,
    index_builder: IndexBuilder | None = None,
    before_activation: ActivationHook | None = None,
) -> S3Manifest:
    """Publish a filesystem Knowledge Base as one complete immutable S3 version.

    Deep prepare/build/validate/activate operation (issue #163, ADR-0019):

    1. Observe the active pointer (version + ETag) and check the caller's
       expectation **before** any expensive work.
    2. Require a previously absent version prefix.
    3. Load and validate canonical content and the derived Discovery Graph.
    4. Write canonical files and the graph (create-only). When ``index_builder``
       is provided, build the remote LanceDB index under
       ``{prefix}/{version}/derived/lance/``, record its completion metadata,
       and gate activation on its success (a missing index stays valid when no
       builder was requested).
    5. Write the manifest last, run the optional ``before_activation`` hook,
       then advance ``current.json`` with one conditional put against the
       *originally observed* pointer. Concurrent or stale publishers fail
       closed via :class:`S3PublicationConflict`.

    Parameters
    ----------
    store:
        An obstore ``ObjectStore`` (``S3Store``, ``MemoryStore``, ...).
    prefix:
        The object-key prefix the Knowledge Base root lives under.
    source_root:
        The local filesystem Knowledge Base root to publish (read-only).
    version:
        The immutable version label for the new Published Version.
    expected_pointer_version:
        The version the caller expects to replace. ``None`` means the pointer
        may exist or not; the compare-and-swap still guards every race.
    index_builder:
        Requested remote LanceDB index builder (see :data:`IndexBuilder`).
        ``None`` (the default) publishes without a LanceDB index — existing
        S3-without-LanceDB publication stays compatible.
    before_activation:
        Optional pre-activation hook (see :data:`ActivationHook`).

    Returns
    -------
    S3Manifest
        The manifest written for the new immutable version.

    Raises
    ------
    KnowledgeBaseError
        If validation fails, the version prefix already exists, LanceDB was
        requested and its build/health check failed, or the store cannot
        support conditional pointer writes.
    S3PublicationConflict
        If the pointer does not match the expectation, or a concurrent
        publication advanced it first.
    """
    obstore = _require_obstore()
    clean_prefix = str(prefix).strip("/")
    root = Path(source_root)

    # 1. Observe the pointer before any expensive preparation (issue #163):
    #    the captured ETag is the compare-and-swap intent used at activation.
    observed = observe_current_pointer(store, clean_prefix)
    _check_expectation(observed, expected_pointer_version, version)

    # 2. Require a previously absent version prefix — interrupted or retried
    #    builds never mutate an existing immutable version.
    _require_absent_version_prefix(obstore, store, clean_prefix, version)

    # 3. Prepare: validate canonical content + compute the version identity.
    kb, report = _load_and_validate(_FilesystemKbSource(root.resolve()))
    if not report.is_valid:
        blocking = sum(1 for issue in report.issues if issue.severity == "error")
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: canonical Knowledge Base content at "
            f"{root!s} has {blocking} blocking validation error(s)"
        )
    fingerprint = fingerprint_sources(root)
    content = canonical_content(_FilesystemKbSource(root.resolve()))

    # Serialize and validate the derived Discovery Graph state (in memory,
    # no managed local bytes — ADR-0013).
    graph_bytes = _serialize_graph_bytes(kb, fingerprint)
    _validate_graph_state(graph_bytes, fingerprint, version)

    # 4. Build: canonical files + graph artifact under the immutable prefix.
    _write_canonical_and_graph(obstore, store, clean_prefix, version, content, graph_bytes)
    derived: dict[str, bytes] = {_GRAPH_REL: graph_bytes}

    # Requested remote LanceDB: build directly under <version>/derived/lance/,
    # record completion metadata, and gate activation on its success. When no
    # builder was requested, the index's absence remains valid (ADR-0019).
    if index_builder is not None:
        completion = index_builder(
            store=store,
            sidecar_prefix=_join(clean_prefix, version, LANCE_DERIVED_DIR),
            pages=list(kb.pages),
            fingerprint=fingerprint,
        )
        completion_bytes = _verify_and_encode_completion(completion, fingerprint, version)
        completion_rel = f"{LANCE_DERIVED_DIR}/{LANCE_COMPLETION_OBJECT}"
        _put_create_only(
            obstore,
            store,
            _join(clean_prefix, version, completion_rel),
            completion_bytes,
            version,
        )
        derived[completion_rel] = completion_bytes

    # 5. The manifest is written last: its presence marks a complete version
    #    (this is what rollback validates and cleanup candidates lack).
    manifest = build_published_manifest(
        version,
        fingerprint.digest,
        content,
        derived_content=derived,
    )
    _write_manifest(obstore, store, clean_prefix, version, manifest)

    # Extension point for the private Source Binding Manifest (#164,
    # ADR-0020): private state, written before activation, never in the
    # public manifest. A raising hook blocks activation.
    if before_activation is not None:
        before_activation(
            PreparedVersion(
                prefix=clean_prefix,
                version=version,
                fingerprint=fingerprint.digest,
                manifest=manifest,
            )
        )

    # 6. Activate: ONE conditional put against the originally observed pointer.
    _activate_pointer(obstore, store, clean_prefix, version, observed, expected_pointer_version)
    return manifest


def rollback_s3_version(
    store: ObjectStore,
    prefix: str,
    *,
    version: str,
    expected_pointer_version: str | None = None,
) -> S3Manifest:
    """CAS-activate an already complete immutable version; never rebuild it.

    Rollback (issue #163, ADR-0019) validates that the target version prefix
    carries a manifest whose entries are all present (a complete version), then
    conditionally advances ``current.json`` to it with one compare-and-swap
    against the currently observed pointer. No object under the target version
    is rewritten or overwritten; a stale rollback (the pointer moved since the
    caller observed it, or the expectation mismatches) fails closed with
    :class:`S3PublicationConflict` and leaves the active version unchanged.

    Returns the manifest of the activated version.

    Raises
    ------
    KnowledgeBaseError
        If the target version is absent or incomplete (no manifest, or manifest
        entries missing) — an incomplete inactive prefix is a cleanup candidate,
        not a rollback target.
    S3PublicationConflict
        On a pointer expectation mismatch or a concurrent activation.
    """
    obstore = _require_obstore()
    clean_prefix = str(prefix).strip("/")

    observed = observe_current_pointer(store, clean_prefix)
    _check_expectation(observed, expected_pointer_version, version)

    manifest = _load_complete_version(obstore, store, clean_prefix, version)
    _activate_pointer(obstore, store, clean_prefix, version, observed, expected_pointer_version)
    return manifest


def list_cleanup_candidates(
    store: ObjectStore, prefix: str
) -> list[CleanupCandidate]:
    """Report inactive, incomplete version prefixes as cleanup candidates.

    A version prefix is incomplete when it has no manifest (the manifest is
    written last, so its absence means an interrupted or failed publication).
    The active version is never a candidate; complete inactive versions are
    not candidates (they are valid rollback targets). Nothing is deleted —
    reporting only (ADR-0019).
    """
    obstore = _require_obstore()
    clean_prefix = str(prefix).strip("/")
    try:
        active = observe_current_pointer(store, clean_prefix).version
    except KnowledgeBaseError:
        active = None

    versions: dict[str, int] = {}
    complete: set[str] = set()
    for key in _list_keys(obstore, store, _join(clean_prefix, "")):
        rel = key[len(clean_prefix) + 1 :] if clean_prefix else key
        segments = rel.split("/")
        # Version prefixes are directories: only keys nested under a first
        # segment count (this skips the pointer object living at the root).
        if len(segments) < 2 or not segments[0]:
            continue
        version = segments[0]
        versions[version] = versions.get(version, 0) + 1
        if segments[1] == MANIFEST_OBJECT:
            complete.add(version)

    return sorted(
        (
            CleanupCandidate(version=version, object_count=count)
            for version, count in versions.items()
            if version not in complete and version != active
        ),
        key=lambda candidate: candidate.version,
    )


def observe_current_pointer(store: ObjectStore, prefix: str) -> PointerObservation:
    """Read and capture the active pointer version plus its ETag.

    The observation must happen *before* expensive preparation (issue #163):
    the returned ETag is the compare-and-swap intent for activation, so a
    publisher that raced with a concurrent activation fails closed instead of
    overwriting it. ``(None, None)`` means no pointer exists yet.
    """
    obstore = _require_obstore()
    key = _join(str(prefix).strip("/"), CURRENT_POINTER_OBJECT)
    try:
        meta = obstore.head(store, key)
    except FileNotFoundError:
        return PointerObservation(version=None, e_tag=None)
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
    return PointerObservation(version=pointer.version, e_tag=etag)


# ---------------------------------------------------------------------------
# Expectation + preflight helpers.
# ---------------------------------------------------------------------------


def _check_expectation(
    observed: PointerObservation, expected: str | None, version: str
) -> None:
    """Fail fast when activation can never succeed against this observation.

    An explicit expectation that already mismatches the live pointer is a
    conflict, and a pointer the store cannot give an ETag for can never be
    compare-and-swapped — both are raised *before* expensive preparation
    (issue #163).
    """
    if expected is not None:
        if observed.version is None:
            raise S3PublicationConflict(
                f"cannot activate {version!r}: expected to advance the pointer from "
                f"{expected!r}, but no pointer exists yet"
            )
        if expected != observed.version:
            raise S3PublicationConflict(
                f"cannot activate {version!r}: expected the active pointer to be "
                f"{expected!r} but it is {observed.version!r}"
            )
    if observed.version is not None and observed.e_tag is None:
        # Fail closed when the store cannot supply an ETag for compare-and-swap
        # (ADR-0013 scopes out multi-writer last-write-wins publishing).
        raise KnowledgeBaseError(
            f"cannot activate {version!r}: the object store does not provide an "
            f"ETag for the activation pointer, so a safe conditional write is "
            f"impossible"
        )


def _require_absent_version_prefix(
    obstore: Any, store: ObjectStore, prefix: str, version: str
) -> None:
    """Require that the version prefix holds no objects at all.

    The manifest being written last means an interrupted earlier build leaves
    objects without a manifest; a retried build must never mutate them (issue
    #163). Create-only puts remain the hard immutability guarantee; this
    preflight turns a would-be mid-write failure into an early, actionable
    error that names the cleanup path.
    """
    if _list_keys(obstore, store, _join(prefix, version, "")):
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: version prefix already exists at "
            f"{_join(prefix, version, '')!r} — immutable versions cannot be "
            f"overwritten; an interrupted build's residue is reported by "
            f"list_cleanup_candidates()"
        )


# ---------------------------------------------------------------------------
# Graph serialization + validation (pure in-memory, no disk I/O).
# ---------------------------------------------------------------------------


def _serialize_graph_bytes(kb: Any, fingerprint: SourceFingerprint) -> bytes:
    """Serialize the Knowledge Base's Discovery Graph to MessagePack bytes.

    Uses the public ``serialize_graph`` function (the same deterministic
    serialization ``KnowledgeBase.materialize_graph`` writes to disk), so the
    published artifact is byte-identical to a locally materialized one — but
    without writing any derived bytes to local disk (ADR-0013).
    """
    return serialize_graph(
        kb._knowledge_index(),
        fingerprint,
        EXTRACTOR_VERSION,
    )


def _validate_graph_state(
    graph_bytes: bytes, fingerprint: SourceFingerprint, version: str
) -> None:
    """Validate the serialized Discovery Graph state before publishing it.

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
# Immutable version-prefix writers.
# ---------------------------------------------------------------------------


def _put_create_only(
    obstore: Any, store: ObjectStore, key: str, raw: bytes, version: str
) -> None:
    """Create-only put: a reused version label fails, never mutates."""
    from obstore.exceptions import AlreadyExistsError  # type: ignore[import-not-found]

    try:
        obstore.put(store, key, raw, mode="create")
    except AlreadyExistsError as exc:
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: version prefix already exists at "
            f"{key!r} — immutable versions cannot be overwritten"
        ) from exc


def _write_canonical_and_graph(
    obstore: Any,
    store: ObjectStore,
    prefix: str,
    version: str,
    content: dict[str, bytes],
    graph_bytes: bytes,
) -> None:
    """Write every canonical file and the derived graph (create-only puts).

    All writes use ``mode="create"``: a reused version label (or a collision
    with a concurrent publisher) raises so an immutable Published Version can
    never be silently mutated.
    """
    for rel, raw in content.items():
        _put_create_only(obstore, store, _join(prefix, version, rel), raw, version)
    # Derived Discovery Graph artifact: rebuildable state, never canonical.
    _put_create_only(
        obstore, store, _join(prefix, version, _GRAPH_REL), graph_bytes, version
    )


def _write_manifest(
    obstore: Any, store: ObjectStore, prefix: str, version: str, manifest: S3Manifest
) -> None:
    """Write the manifest last: its presence marks a complete version."""
    _put_create_only(
        obstore,
        store,
        _join(prefix, version, MANIFEST_OBJECT),
        msgspec.json.encode(manifest),
        version,
    )


def _verify_and_encode_completion(
    completion: RemoteIndexCompletion, fingerprint: SourceFingerprint, version: str
) -> bytes:
    """Verify the builder's completion report and encode the metadata.

    The index must have been built from exactly this Published Version's
    canonical fingerprint — a mismatch means the builder indexed different
    content, and activation is blocked (issue #163).
    """
    if completion.fingerprint != fingerprint.digest:
        raise KnowledgeBaseError(
            f"cannot publish {version!r}: the remote LanceDB index completion "
            f"metadata fingerprint {completion.fingerprint!r} does not match the "
            f"Knowledge Base fingerprint {fingerprint.digest!r}"
        )
    return msgspec.json.encode(completion)


# ---------------------------------------------------------------------------
# Rollback target validation.
# ---------------------------------------------------------------------------


def _load_complete_version(
    obstore: Any, store: ObjectStore, prefix: str, version: str
) -> S3Manifest:
    """Load and validate an already complete immutable version for rollback.

    Complete + validated (ADR-0019) means: a manifest exists, decodes, names
    this version, every canonical and derived object it lists is present, and
    the whole version still resolves through the Reader seam — every content
    digest checks out and the Published Version fingerprint matches the
    materialized content. Rollback never rebuilds or rewrites the target
    (issue #163); validation is read-only, so a corrupted version is rejected
    BEFORE the pointer advances instead of leaving Readers on a broken
    Snapshot after activation.
    """
    manifest_key = _join(prefix, version, MANIFEST_OBJECT)
    try:
        raw = bytes(obstore.get(store, manifest_key).bytes())
    except FileNotFoundError as exc:
        raise KnowledgeBaseError(
            f"cannot roll back to {version!r}: no manifest at {manifest_key!r} — "
            f"the version is absent or incomplete (an inactive incomplete prefix "
            f"is a cleanup candidate, not a rollback target)"
        ) from exc
    try:
        manifest = msgspec.json.decode(raw, type=S3Manifest)
    except msgspec.DecodeError as exc:
        raise KnowledgeBaseError(
            f"cannot roll back to {version!r}: manifest {manifest_key!r} is "
            f"malformed: {exc}"
        ) from exc
    if manifest.version != version:
        raise KnowledgeBaseError(
            f"cannot roll back to {version!r}: manifest {manifest_key!r} names "
            f"version {manifest.version!r}"
        )
    present = set(_list_keys(obstore, store, _join(prefix, version, "")))
    expected = {
        _join(prefix, version, entry.path) for entry in [*manifest.files, *manifest.derived_files]
    }
    missing = sorted(expected - present)
    if missing:
        raise KnowledgeBaseError(
            f"cannot roll back to {version!r}: manifest lists objects missing "
            f"from the store: {', '.join(missing[:5])}"
            + (" …" if len(missing) > 5 else "")
        )
    # Full read-only validation through the Reader seam: every digest, the
    # fingerprint, and canonical loading. A version Readers cannot resolve is
    # not a rollback target.
    try:
        S3Location(store, prefix, version=version).resolve()
    except KnowledgeBaseError as exc:
        raise KnowledgeBaseError(
            f"cannot roll back to {version!r}: the version does not validate "
            f"({exc})"
        ) from exc
    return manifest


def _list_keys(obstore: Any, store: ObjectStore, prefix: str) -> list[str]:
    """Return every object key under ``prefix`` (may be empty)."""
    keys: list[str] = []
    for batch in obstore.list(store, prefix=prefix):
        for obj in batch:
            keys.append(obj["path"] if isinstance(obj, dict) else obj["path"])
    return keys


# ---------------------------------------------------------------------------
# Conditional pointer activation (compare-and-swap, fail-closed).
# ---------------------------------------------------------------------------


def _get_pointer_bytes(obstore: Any, store: ObjectStore, key: str) -> bytes:
    """Read the raw pointer bytes (the head already proved it exists)."""
    try:
        result = obstore.get(store, key)
    except Exception as exc:  # pragma: no cover - head proved existence
        raise KnowledgeBaseError(
            f"could not read activation pointer {key!r}: {exc}"
        ) from exc
    return bytes(result.bytes())


def _activate_pointer(
    obstore: Any,
    store: ObjectStore,
    prefix: str,
    version: str,
    observed: PointerObservation,
    expected_pointer_version: str | None,
) -> None:
    """Conditionally advance ``current.json`` against the observed pointer.

    The compare-and-swap uses the pointer observation captured **before**
    preparation (issue #163): first publication uses a create-if-absent put;
    subsequent activations use an update-if-match-ETag put against the
    originally observed ETag. A concurrent publication that touched the
    pointer in the meantime is detected and raises
    :class:`S3PublicationConflict`, leaving the active version intact.

    A store that cannot supply an ETag for the existing pointer fails closed:
    activation is refused rather than silently falling back to
    last-write-wins (ADR-0013 scopes out multi-writer LWW publishing).
    """
    key = _join(prefix, CURRENT_POINTER_OBJECT)
    pointer_bytes = msgspec.json.encode(S3Pointer(version=version))

    if observed.version is None:
        # First publication: the pointer must not exist yet (an explicit
        # expectation was already rejected by _check_expectation).
        _conditional_create(obstore, store, key, pointer_bytes, version)
        return

    # Fail closed when the store cannot supply an ETag for compare-and-swap.
    if observed.e_tag is None:
        raise KnowledgeBaseError(
            f"cannot activate {version!r}: the object store does not provide an "
            f"ETag for the activation pointer, so a safe conditional write is "
            f"impossible"
        )

    _conditional_update(
        obstore, store, key, pointer_bytes, observed.e_tag, version, observed.version
    )


def _conditional_create(
    obstore: Any, store: ObjectStore, key: str, pointer_bytes: bytes, version: str
) -> None:
    """Create the pointer only if it does not already exist."""
    from obstore.exceptions import AlreadyExistsError  # type: ignore[import-not-found]

    try:
        obstore.put(store, key, pointer_bytes, mode="create")
    except AlreadyExistsError as exc:
        raise S3PublicationConflict(
            f"cannot activate {version!r}: a concurrent publication created the "
            f"activation pointer first"
        ) from exc


def _conditional_update(
    obstore: Any,
    store: ObjectStore,
    key: str,
    pointer_bytes: bytes,
    etag: str,
    version: str,
    existing_version: str,
) -> None:
    """Advance the pointer only if its ETag still matches the one we observed."""
    from obstore.exceptions import PreconditionError  # type: ignore[import-not-found]

    try:
        obstore.put(store, key, pointer_bytes, mode={"mode": "update", "e_tag": etag})
    except PreconditionError as exc:
        raise S3PublicationConflict(
            f"cannot activate {version!r}: a concurrent publication already "
            f"advanced the activation pointer from {existing_version!r}"
        ) from exc
