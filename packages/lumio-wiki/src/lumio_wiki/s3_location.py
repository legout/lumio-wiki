"""S3-native Knowledge Base Location — read immutable S3 Snapshots directly.

Issue #120, ADR-0013. This is the opt-in ``lumio-wiki[s3]`` capability: a local
coding agent resolves one immutable S3 **Published Version** and validates,
searches, reads, and traverses it with zero-index retrieval — with **no managed
local Markdown or derived-index copy**.

The capability is a new :class:`lumio_wiki.location.KnowledgeBaseLocation`
implementation built on **`obstore`** (object-level operations: head, list, and
byte/range reads). ``obstore`` is an *optional* dependency declared behind the
``[s3]`` extra; it is imported lazily, only inside this module, so the base
``lumio-wiki`` wheel stays lightweight with zero cloud dependency. When ``[s3]``
is absent, every public entrypoint raises an actionable error naming the exact
install command.

Publication protocol (ADR-0013):

* Each publication writes canonical Markdown, the Control File, and derived
  artifacts under a new immutable version prefix, alongside a **manifest**
  listing every file's relative path, size, and sha-256 digest plus the
  Knowledge Base fingerprint.
* The publisher conditionally updates a small ``current.json`` pointer to the
  new version.
* A **reader** resolves ``current.json`` once, reads the manifest, validates
  every content digest, and uses **only that immutable version prefix** — it can
  never observe a partial publication.

The default S3 cache policy writes **no** managed Knowledge Base or
derived-index bytes to local disk. Content is materialized into a bounded
in-memory cache; there is no implicit on-disk cache. ``lumio.yaml`` never
carries S3 credentials — credentials, region, and endpoint belong to app/CLI
configuration (environment variables / the obstore client config).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from lumio_wiki.graph_state import (
    GRAPH_ARTIFACT_FILENAME,
    GraphState,
    build_graph_state,
    deserialize_graph,
)
from lumio_wiki.knowledge_base import (
    KnowledgeBaseError,
    _fingerprint_sources,
    _InMemoryKbSource,
    _load_and_validate,
)
from lumio_wiki.location import KnowledgeBaseSnapshot
from lumio_wiki.records import EXTRACTOR_VERSION

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from obstore.store import ObjectStore

# Stable identifier for the S3 Location implementation (mirrors
# ``FILESYSTEM_LOCATION_KIND`` so traces/diagnostics name the resolved source
# without coupling to a class name).
S3_LOCATION_KIND = "s3"

# Object keys (relative to the Location prefix) for the version pointer and the
# per-version manifest. These are the publication protocol's fixed names.
CURRENT_POINTER_OBJECT = "current.json"
MANIFEST_OBJECT = "manifest.json"

# The derived-artifact subdirectory under an immutable version prefix
# (``{prefix}/{version}/derived/...``). Holds rebuildable, non-canonical state
# such as the Discovery Graph MessagePack artifact and (later) remote LanceDB
# tables. Never part of the manifest's canonical file list (ADR-0013).
DERIVED_DIR = "derived"

# The remote LanceDB index subdirectory under an immutable version prefix
# (``{prefix}/{version}/derived/lance/``, ADR-0019). Single source of truth for
# the publisher (which builds there) and the Snapshot's dependency-neutral
# remote derived-index descriptor (which points consumers there, issue #162).
LANCE_DERIVED_DIR = f"{DERIVED_DIR}/lance"

# Bounded in-memory cache defaults (the no-managed-disk-cache policy, ADR-0013).
# A single resolved version is cached by default; the cap keeps the resident
# byte footprint bounded. The cache is process-local and never touches disk.
DEFAULT_MAX_CACHED_VERSIONS = 1
DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024  # 256 MiB

__all__ = [
    "CURRENT_POINTER_OBJECT",
    "DEFAULT_MAX_CACHE_BYTES",
    "DEFAULT_MAX_CACHED_VERSIONS",
    "DERIVED_DIR",
    "LANCE_DERIVED_DIR",
    "MANIFEST_OBJECT",
    "S3_LOCATION_KIND",
    "S3Location",
    "S3Manifest",
    "S3ManifestFile",
    "S3Pointer",
    "build_published_manifest",
    "open_s3_knowledge_base",
]


# ---------------------------------------------------------------------------
# Optional-dependency guard.
# ---------------------------------------------------------------------------


def _require_obstore() -> Any:
    """Import and return the ``obstore`` package, or raise an actionable error.

    ``obstore`` lives behind the ``[s3]`` extra. This guard keeps the base wheel
    free of any cloud dependency and gives the caller the exact install command
    instead of a bare ``ModuleNotFoundError``.
    """
    try:
        import obstore  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised by the missing-extra suite
        raise KnowledgeBaseError(
            "the S3 Knowledge Base capability requires the optional '[s3]' extra; "
            "install it with:\n  pip install 'lumio-wiki[s3]'"
        ) from exc
    return obstore


# ---------------------------------------------------------------------------
# Publication-protocol records (manifest + pointer).
#
# These frozen records ARE the wire contract between a publisher (issue #121)
# and a reader (this module). They are intentionally plain JSON-serializable so
# a publisher written in any language can produce a manifest this reader accepts.
# ---------------------------------------------------------------------------


class S3ManifestFile(msgspec.Struct, frozen=True):
    """One canonical file in a Published Version manifest."""

    path: str
    """Relative POSIX path of the file under the immutable version prefix."""

    size: int
    """Byte length of the file (validated on read)."""

    digest: str
    """sha-256 hex digest of the file content (validated on read)."""


class S3Manifest(msgspec.Struct, frozen=True):
    """The manifest of an immutable S3 Published Version.

    Lists every canonical file with its content digest and carries the Knowledge
    Base fingerprint computed at publish time. A reader recomputes the fingerprint
    over the materialized content and rejects a mismatch as corruption.

    Derived artifacts (the Discovery Graph MessagePack, and later remote LanceDB
    tables) are rebuildable state, never canonical content (ADR-0011): they live
    in ``derived_files`` so a reader can integrity-check them without making them
    part of the canonical file tree or the fingerprint.
    """

    version: str
    """The immutable version prefix this manifest describes."""

    fingerprint: str
    """sha-256 hex digest of the canonical Knowledge Base content (Published Version identity)."""

    files: list[S3ManifestFile]
    """Every canonical file: Control File plus all Markdown (including reserved artifacts)."""

    derived_files: list[S3ManifestFile] = msgspec.field(default_factory=list)
    """Derived (non-canonical) artifacts with their digests and sizes."""


class S3Pointer(msgspec.Struct, frozen=True):
    """The ``current.json`` pointer to the active immutable Published Version."""

    version: str


def build_published_manifest(
    version: str,
    fingerprint_digest: str,
    content: dict[str, bytes],
    *,
    derived_content: dict[str, bytes] | None = None,
) -> S3Manifest:
    """Build a manifest for an immutable version from canonical content.

    Pure helper shared by the publisher (issue #121) and tests. ``content`` is the
    canonical Knowledge Base file tree (``{relative_path: bytes}`` — typically
    :func:`lumio_wiki.knowledge_base.canonical_content`). The fingerprint digest
    is the Published Version identity computed at publish time
    (:func:`lumio_wiki.fingerprint_sources`); it is recorded verbatim so a reader
    can validate it without trusting the publisher.

    ``derived_content`` is optional rebuildable state (e.g. the Discovery Graph
    MessagePack artifact) keyed by its relative path under the version's
    ``derived/`` area. It is never part of the canonical file tree or the
    fingerprint — the reader integrity-checks it via ``derived_files`` and falls
    back to in-memory derivation on any mismatch (ADR-0011, ADR-0013).
    """
    files = [
        S3ManifestFile(
            path=path,
            size=len(raw),
            digest=hashlib.sha256(raw).hexdigest(),
        )
        for path, raw in sorted(content.items())
    ]
    derived_files = []
    for path, raw in sorted((derived_content or {}).items()):
        derived_files.append(
            S3ManifestFile(
                path=path,
                size=len(raw),
                digest=hashlib.sha256(raw).hexdigest(),
            )
        )
    return S3Manifest(
        version=version,
        fingerprint=fingerprint_digest,
        files=files,
        derived_files=derived_files,
    )


# ---------------------------------------------------------------------------
# Object-store read helpers.
#
# A thin layer over obstore's object-level API. Kept synchronous and small so
# the read path is straightforward to reason about; obstore also offers async
# variants, but the Core SDK's read surface is synchronous.
# ---------------------------------------------------------------------------


def _join(prefix: str, *parts: str) -> str:
    """Join object-key parts with ``/``, collapsing empties (no leading slash)."""
    segments = [seg for seg in (prefix, *parts) if seg]
    return "/".join(segments)


def _require_confined_relative_path(path: str, version: str) -> None:
    """Reject a manifest path that could escape the immutable version prefix.

    ADR-0013 requires manifest paths to be relative and the reader to use only
    the resolved version prefix. Absolute paths, backslashes, drive letters,
    and any ``..`` segment would let a corrupt or hostile manifest read outside
    the prefix or mix versions, so they are rejected as corruption.
    """
    if not path or path.startswith(("/", "\\")):
        raise KnowledgeBaseError(
            f"S3 manifest path {path!r} for {version!r} is not a relative path"
        )
    if "\\" in path:
        raise KnowledgeBaseError(
            f"S3 manifest path {path!r} for {version!r} must use POSIX '/' separators"
        )
    parts = path.split("/")
    if any(part == ".." for part in parts):
        raise KnowledgeBaseError(
            f"S3 manifest path {path!r} for {version!r} escapes the version prefix "
            f"via a '..' segment"
        )


def _get_bytes(store: Any, key: str) -> bytes:
    """Read an object's full bytes via obstore, raising on a missing key."""
    obstore = _require_obstore()
    try:
        result = obstore.get(store, key)
    except Exception as exc:  # obstore raises its own NotFound/Error types.
        raise KnowledgeBaseError(f"could not read S3 object {key!r}: {exc}") from exc
    return bytes(result.bytes())


def _get_json(store: Any, key: str) -> Any:
    """Read and JSON-decode an object, raising on malformed JSON."""
    raw = _get_bytes(store, key)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise KnowledgeBaseError(f"S3 object {key!r} is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# The S3 Knowledge Base Location.
# ---------------------------------------------------------------------------


class S3Location:
    """A Knowledge Base Location that resolves an immutable S3 Published Version.

    Implements the :class:`lumio_wiki.location.KnowledgeBaseLocation` protocol
    behind the opt-in ``[s3]`` capability. ``resolve()`` reads ``current.json``
    once, validates the manifest and every content digest, then loads only that
    immutable version into an in-memory Snapshot. The default policy writes no
    managed bytes to local disk.

    The ``store`` is an obstore ``ObjectStore`` (``S3Store``, ``MemoryStore``,
    ...). ``prefix`` is the object-key prefix the Knowledge Base root lives under
    (relative to the store's own root). Credentials, region, and endpoint live on
    the store / client config — never on this Location and never in
    ``lumio.yaml``.
    """

    kind = S3_LOCATION_KIND

    def __init__(
        self,
        store: ObjectStore,
        prefix: str,
        *,
        version: str | None = None,
        max_cached_versions: int = DEFAULT_MAX_CACHED_VERSIONS,
        max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        store_uri: str | None = None,
    ) -> None:
        self._store = store
        self._prefix = prefix.strip("/")
        # When ``version`` is set the Location is pinned and never reads the
        # pointer; otherwise ``resolve()`` resolves ``current.json`` each time.
        self._pinned_version = version
        self._max_cached_versions = max(0, max_cached_versions)
        self._max_cache_bytes = max(0, max_cache_bytes)
        # Bounded in-memory cache of materialized versions: {version: {rel: bytes}}.
        self._cache: dict[str, dict[str, bytes]] = {}
        # The object-store container URI (``s3://bucket``) the store is rooted
        # at, when known. A resolved Snapshot uses it to expose its Published
        # Version's remote derived-index descriptor (issue #162, ADR-0019):
        # the descriptor's connect URI is container + key prefix, never
        # reconstructed per caller and never coerced through ``Path``.
        self._store_uri = store_uri.rstrip("/") if store_uri else None

    # -- Location protocol --------------------------------------------------

    @property
    def prefix(self) -> str:
        """The object-key prefix the Knowledge Base root lives under."""
        return self._prefix

    @property
    def store(self) -> ObjectStore:
        """The obstore ObjectStore this Location reads through.

        Exposed so a consumer (e.g. the app binding a remote LanceDB index for
        the same Snapshot, ADR-0013/#124) can read derived sidecars through the
        same authenticated store without rebuilding one. Never carries
        credentials itself — those live on the store.
        """
        return self._store

    def describe(self) -> str:
        """Return a human-readable, secret-free description for diagnostics.

        Never includes credentials. When pinned to a version the version is
        named; otherwise the pointer is resolved on demand.
        """
        where = f"s3:{self._prefix or '/'}"
        if self._pinned_version is not None:
            return f"{where}@{self._pinned_version}"
        return where

    def resolve(self) -> KnowledgeBaseSnapshot:
        """Resolve the active Published Version into an immutable Snapshot.

        Resolves ``current.json`` once (unless a version is pinned), reads and
        validates the manifest, materializes every canonical file while checking
        its size and sha-256 digest, then loads only that immutable version with
        the Core SDK loader and validates the Published Version fingerprint. No
        managed bytes are written to local disk.
        """
        _require_obstore()
        version = self._resolve_version()
        manifest = self._read_manifest(version)
        content = self._materialize(version, manifest)
        return self._load_snapshot(version, manifest, content)

    # -- construction helpers ----------------------------------------------

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        config: dict[str, str] | None = None,
        client_options: dict[str, Any] | None = None,
        prefix: str | None = None,
        version: str | None = None,
        **kwargs: Any,
    ) -> S3Location:
        """Build an :class:`S3Location` from an ``s3://`` (or compatible) URL.

        The store is built from the URL's scheme + authority (the bucket), and
        the URL path becomes the Knowledge Base root prefix. Credentials,
        region, and endpoint are supplied via ``config`` (``aws_*`` keys such as
        ``aws_region``, ``aws_endpoint``, ``aws_access_key_id``) and
        ``client_options`` (e.g. ``{"allow_http": True}`` for a MinIO HTTP
        endpoint). Pass ``prefix`` to override the URL-derived Knowledge Base
        root when it lives elsewhere under the bucket.
        """
        from urllib.parse import urlparse

        obstore = _require_obstore()
        parsed = urlparse(url)
        if not parsed.scheme:
            raise KnowledgeBaseError(f"not an object-store URL: {url!r}")
        # Build the store rooted at the container (scheme://authority) so the
        # URL path is the Knowledge Base root, not baked into the store prefix.
        authority_url = f"{parsed.scheme}://{parsed.netloc}"
        store = obstore.store.from_url(
            authority_url, config=config, client_options=client_options, **kwargs
        )
        url_path = parsed.path.lstrip("/")
        chosen_prefix = prefix if prefix is not None else url_path
        return cls(store, chosen_prefix, version=version, store_uri=authority_url)

    # -- read internals -----------------------------------------------------

    def _resolve_version(self) -> str:
        """Return the pinned version, or read ``current.json`` once."""
        if self._pinned_version is not None:
            return self._pinned_version
        key = _join(self._prefix, CURRENT_POINTER_OBJECT)
        data = _get_json(self._store, key)
        if not isinstance(data, dict) or not isinstance(data.get("version"), str):
            raise KnowledgeBaseError(
                f"S3 pointer {key!r} is malformed: expected a JSON object with a 'version' string"
            )
        return data["version"]

    def _read_manifest(self, version: str) -> S3Manifest:
        key = _join(self._prefix, version, MANIFEST_OBJECT)
        data = _get_json(self._store, key)
        try:
            manifest = msgspec.json.decode(msgspec.json.encode(data), type=S3Manifest)
        except msgspec.DecodeError as exc:
            raise KnowledgeBaseError(f"S3 manifest {key!r} is malformed: {exc}") from exc
        return manifest

    def _materialize(self, version: str, manifest: S3Manifest) -> dict[str, bytes]:
        """Read and digest-validate every canonical file of ``version``.

        Returns the full canonical tree ``{relative_path: bytes}`` for the
        immutable version, rejecting any size or digest mismatch as corruption.
        Cached in memory when within the configured bounds; never written to disk.
        """
        if version in self._cache:
            return self._cache[version]

        if manifest.version != version:
            raise KnowledgeBaseError(
                f"S3 manifest version {manifest.version!r} does not match its prefix {version!r}"
            )

        content: dict[str, bytes] = {}
        seen: set[str] = set()
        for entry in manifest.files:
            if not entry.path or entry.path in seen:
                raise KnowledgeBaseError(
                    f"S3 manifest for {version!r} has a duplicate or empty path"
                )
            # ADR-0013: manifest paths are relative and confined to the
            # immutable version prefix. Reject anything that could escape it
            # (absolute, backslash, or a ``..`` segment) so a corrupt or
            # hostile manifest cannot mix versions or read outside the prefix.
            _require_confined_relative_path(entry.path, version)
            seen.add(entry.path)
            key = _join(self._prefix, version, entry.path)
            raw = _get_bytes(self._store, key)
            if len(raw) != entry.size:
                raise KnowledgeBaseError(
                    f"S3 corruption: {key!r} size {len(raw)} != manifest size {entry.size}"
                )
            actual = hashlib.sha256(raw).hexdigest()
            if actual != entry.digest:
                raise KnowledgeBaseError(
                    f"S3 corruption: {key!r} digest {actual} != manifest digest {entry.digest}"
                )
            content[entry.path] = raw

        self._cache_version(version, content)
        return content

    def _cache_version(self, version: str, content: dict[str, bytes]) -> None:
        """Store a materialized version in the bounded in-memory cache."""
        if self._max_cached_versions == 0:
            return
        # Evict oldest entries (insertion-ordered dict) until within bounds:
        # first by cached-version count, then by resident bytes.
        self._cache[version] = content
        while len(self._cache) > self._max_cached_versions:
            self._cache.pop(next(iter(self._cache)))
        while self._cache and self._cached_bytes() > self._max_cache_bytes:
            self._cache.pop(next(iter(self._cache)))

    def _cached_bytes(self) -> int:
        """Total resident bytes across all cached versions."""
        return sum(len(raw) for files in self._cache.values() for raw in files.values())

    def _load_snapshot(
        self,
        version: str,
        manifest: S3Manifest,
        content: dict[str, bytes],
    ) -> KnowledgeBaseSnapshot:
        """Load validated content into an immutable Snapshot (no disk bytes)."""
        root = Path(f"s3:{self._prefix}/{version}")
        source = _InMemoryKbSource(content, root=root)

        # Load + validate through the shared Core SDK seam so the S3 Snapshot
        # is byte-for-byte identical to the filesystem Location (no duplicated
        # loading/validation logic, no coupling to private loader internals).
        kb, report = _load_and_validate(source)

        # Validate the Published Version fingerprint recorded in the manifest
        # against the content we just materialized: a mismatch is corruption.
        actual_fp = _fingerprint_sources(source)
        if actual_fp.digest != manifest.fingerprint:
            raise KnowledgeBaseError(
                f"S3 corruption: Published Version fingerprint {actual_fp.digest!r} "
                f"!= manifest fingerprint {manifest.fingerprint!r} for {version!r}"
            )

        return KnowledgeBaseSnapshot(
            knowledge_base=kb,
            validation_report=report,
            fingerprint=actual_fp,
            location=self,
            remote_derived_index=self._remote_derived_index_descriptor(version, actual_fp),
        )

    def _remote_derived_index_descriptor(self, version: str, fingerprint: Any) -> Any | None:
        """Describe this Published Version's remote derived index, when known.

        Dependency-neutral (issue #162, ADR-0019): the descriptor carries the
        connect URI, sidecar prefix, immutable version, Published Version
        fingerprint, and this Location's authenticated store. Returned as
        ``None`` when the container URI is unknown (a directly constructed
        Location) so the Snapshot type stays free of any optional-import
        failure; callers fall back to zero-index retrieval.
        """
        if self._store_uri is None:
            return None
        from lumio_wiki.location import RemoteDerivedIndex

        sidecar_prefix = "/".join(
            part for part in (self._prefix, version, LANCE_DERIVED_DIR) if part
        )
        return RemoteDerivedIndex(
            uri=f"{self._store_uri}/{sidecar_prefix}",
            sidecar_prefix=sidecar_prefix,
            version=version,
            fingerprint=fingerprint,
            store=self._store,
        )

    # -- derived graph -----------------------------------------------------

    def _graph_version_for(self, snapshot: KnowledgeBaseSnapshot) -> str:
        """Return the version label for an already-resolved ``snapshot``.

        Never re-reads the activation pointer when the resolution already
        carries the version: pinned Locations use their pin, and a Snapshot's
        remote derived-index descriptor records the exact resolved version
        (issue #175: one immutable resolution serves every status field).
        """
        if self._pinned_version is not None:
            return self._pinned_version
        descriptor = snapshot.remote_derived_index
        if descriptor is not None:
            return descriptor.version
        return self._resolve_version()

    def graph_state_with_source(
        self, snapshot: KnowledgeBaseSnapshot | None = None
    ) -> tuple[GraphState, str, str | None]:
        """Return ``(graph state, source label, note)`` for the resolved version.

        The label is ``"published artifact"`` when the version's published
        MessagePack graph artifact was loaded (present, manifest
        digest-verified, fingerprint- and extractor-current) and ``"memory"``
        when the same adjacency was derived in memory instead; ``note`` is
        ``None`` on the healthy path and otherwise names the truthful cause
        (not published / unreadable / corrupt / stale) plus nothing else. The
        public observability seam for ``lumio-wiki status`` (issue #175): the
        graph is always complete and deterministic either way; the label
        discloses which source served it, never a health verdict.

        ``snapshot`` may be an already-resolved Snapshot from this Location so
        one immutable resolution serves every field (a concurrent pointer
        advance can never mix versions within one status call); when omitted,
        the Location resolves once itself.
        """
        if snapshot is None:
            snapshot = self.resolve()
        version = self._graph_version_for(snapshot)
        fingerprint = snapshot.fingerprint

        # Read the manifest so we can integrity-check the graph artifact
        # against the publisher-recorded digest before trusting it.
        manifest = self._read_manifest(version)
        graph_rel = f"{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}"
        graph_meta = None
        for entry in manifest.derived_files:
            if entry.path == graph_rel:
                graph_meta = entry
                break

        note: str | None = None
        if graph_meta is None:
            note = "published graph artifact not in the version manifest"
        else:
            key = _join(self._prefix, version, graph_rel)
            data: bytes | None
            try:
                data = _get_bytes(self._store, key)
            except KnowledgeBaseError:
                data = None
            if data is None:
                note = "published graph artifact unreadable"
            elif not (
                len(data) == graph_meta.size
                and hashlib.sha256(data).hexdigest() == graph_meta.digest
            ):
                # Integrity-check the artifact bytes against the manifest
                # digest BEFORE trusting its embedded fields (ADR-0013).
                note = "published graph artifact corrupt (manifest digest mismatch)"
            else:
                state = deserialize_graph(data)
                if (
                    state is not None
                    and state.fingerprint_digest == fingerprint.digest
                    and state.extractor_version == EXTRACTOR_VERSION
                ):
                    return state, "published artifact", None
                note = "published graph artifact stale (fingerprint or extractor mismatch)"

        state = build_graph_state(
            snapshot.knowledge_base._knowledge_index(),
            fingerprint,
            EXTRACTOR_VERSION,
        )
        return state, "memory", note

    def load_or_derive_graph(self) -> GraphState:
        """Return the Discovery Graph state for the resolved version.

        Loads the published MessagePack artifact when it is present, fresh
        (fingerprint and extractor version match), and integrity-checked against
        the manifest's ``derived_files`` digest; otherwise derives the same
        adjacency deterministically in memory from the loaded Knowledge Base.
        Never raises on a missing, stale, or corrupt artifact — the Discovery
        Graph is derived state, never canonical content (ADR-0011, ADR-0013).

        A coherently altered graph artifact (valid MessagePack, correct
        fingerprint/extractor fields, but different edges) is rejected by the
        manifest digest check and falls back to in-memory derivation, so a
        reader can never observe a graph that disagrees with what the
        publisher wrote.
        """
        return self.graph_state_with_source()[0]

    def published_version_content(self, version: str) -> tuple[S3Manifest, dict[str, bytes]]:
        """Return one complete immutable version's manifest and canonical content.

        Read-only accessor for version-to-version comparison (t_66f162f2):
        reads the version's manifest, then materializes every canonical file
        through the same digest-validated read path ``resolve()`` uses
        (:meth:`_materialize` — size and sha-256 checked, bounded cache). The
        active pointer is never consulted, so any two complete versions can be
        compared without touching ``current.json``.
        """
        _require_obstore()
        manifest = self._read_manifest(version)
        content = self._materialize(version, manifest)
        return manifest, content


def open_s3_knowledge_base(
    store: ObjectStore,
    prefix: str,
    *,
    version: str | None = None,
) -> KnowledgeBaseSnapshot:
    """Open an immutable S3 Knowledge Base Snapshot through the Location seam.

    Convenience shorthand for ``S3Location(store, prefix, version=version).resolve()``.
    """
    return S3Location(store, prefix, version=version).resolve()
