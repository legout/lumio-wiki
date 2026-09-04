"""Private Source Artifact Store seam (issue #164, ADR-0020).

Retains the optional, private ORIGINAL bytes of a Knowledge Source as
immutable **Source Artifacts** bound to exact Source Versions.

The external interface is identity-oriented — callers address an artifact by
``(source_id, content_hash)`` and never see or spell a physical object key.
Adapters choose the physical layout; content-addressed physical deduplication
never merges two distinct source identities (each artifact is stored under a
key derived from BOTH the source id and the content hash).

Separation contract (ADR-0020): artifacts, binding manifests, and this store
live OUTSIDE public Published Version prefixes, canonical fingerprints,
exports, retrieval, and indexes. Nothing here is Compiled Page content.

This module never touches credentials: store construction and signing are the
caller's (deployment) concern.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import msgspec

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from datetime import timedelta


class ArtifactStoreError(Exception):
    """Base error for private Source Artifact Store operations (#164).

    Messages never disclose object keys, credentials, or secret-bearing
    URLs (ADR-0020 ordinary-output rule).
    """


class ArtifactAccessDenied(ArtifactStoreError):
    """The configured credentials cannot read the private store (#165).

    Raised for object-store permission/authentication denials and local
    filesystem permission errors so inspection can report access denial as
    a distinct actionable outcome (issue #165, ADR-0020).
    """


class ArtifactUnavailable(ArtifactStoreError):
    """No artifact is retained for the requested identity (#165)."""


class ArtifactCorruption(ArtifactStoreError):
    """Stored bytes failed digest or size verification (#165)."""
    """A Source Artifact Store operation failed (never reports success)."""


class SigningUnavailable(ArtifactStoreError):
    """The adapter cannot issue signed URLs for one exact object."""


#: Managed ingest computes the SHA-256 itself (it also feeds the Source
#: Version identity); the store re-verifies what it actually holds.
def artifact_content_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


#: Maximum retained Source Artifact size at the trust boundary (ADR-0020:
#: "size limits ... apply at the trust boundary"). Larger sources stay
#: ordinary hash-only Source Versions; retention refuses them with an
#: actionable error rather than silently streaming gigabytes into the
#: private store. Module-level so deployments/tests can tighten it.
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class SourceArtifactStore(Protocol):
    """Identity-oriented seam for private Source Artifact retention (#164).

    ``source_id``/``content_hash`` address one exact artifact (the
    authoritative identity from ADR-0020); ``content_type`` and ``filename``
    (already sanitized to a safe basename) are recorded for authorized
    inspection. Implementations verify stored size and digest on upload and
    fetch, upload create-only, and never expose object keys.
    """

    def put_artifact(
        self,
        *,
        source_id: str,
        content_hash: str,
        raw_bytes: bytes,
        content_type: str | None,
        filename: str | None,
    ) -> None:
        """Create-only upload of one artifact, verified after write."""

    def get_artifact(self, *, source_id: str, content_hash: str) -> bytes:
        """Fetch one artifact, verifying size and digest before returning."""

    def artifact_exists(self, *, source_id: str, content_hash: str) -> bool:
        """Report whether one exact artifact is present (digest-verified)."""

    def delete_artifact(self, *, source_id: str, content_hash: str) -> bool:
        """Explicitly delete one exact artifact; returns whether it existed."""

    def supports_signing(self) -> bool:
        """Whether :meth:`signed_get_url` can issue exact-object URLs here."""

    def signed_get_url(self, *, source_id: str, content_hash: str, expires_in: timedelta) -> str:
        """Short-lived signed GET URL for ONE exact artifact (or raise)."""

    def put_binding_manifest(self, version: str, manifest: bytes) -> None:
        """Store the private Source Binding Manifest for a Published Version."""

    def get_binding_manifest(self, version: str) -> bytes:
        """Fetch the private binding manifest bytes for one version."""

    def list_binding_versions(self) -> list[str]:
        """List Published Versions that have a stored binding manifest."""
        ...


class InMemoryArtifactStore:
    """Deterministic in-memory adapter: tests and no-storage local runs.

    Content-addressed by ``(source_id, content_hash)`` — identical bytes for
    distinct source ids are stored separately, so physical deduplication never
    merges source identities. Signing is genuinely unavailable (mirrors any
    adapter without a signing capability).
    """

    def __init__(self) -> None:
        self._artifacts: dict[tuple[str, str], tuple[bytes, str | None, str | None]] = {}
        self._manifests: dict[str, bytes] = {}

    def put_artifact(
        self,
        *,
        source_id: str,
        content_hash: str,
        raw_bytes: bytes,
        content_type: str | None,
        filename: str | None,
    ) -> None:
        if artifact_content_hash(raw_bytes) != content_hash:
            raise ArtifactStoreError("artifact bytes do not hash to the declared content hash")
        self._artifacts[(source_id, content_hash)] = (raw_bytes, content_type, filename)

    def get_artifact(self, *, source_id: str, content_hash: str) -> bytes:
        entry = self._artifacts.get((source_id, content_hash))
        if entry is None:
            raise ArtifactUnavailable("artifact not retained")
        raw = entry[0]
        if artifact_content_hash(raw) != content_hash:
            raise ArtifactCorruption("stored artifact failed digest verification")
        return raw

    def artifact_exists(self, *, source_id: str, content_hash: str) -> bool:
        try:
            self.get_artifact(source_id=source_id, content_hash=content_hash)
        except ArtifactAccessDenied:
            # Denial is unknown-presence, not absence (#165): fail closed so
            # gates and inspection never misreport denied reads as missing.
            raise
        except ArtifactStoreError:
            return False
        return True

    def delete_artifact(self, *, source_id: str, content_hash: str) -> bool:
        # Presence by identity (not digest): a corrupted artifact must still
        # be explicitly deletable — that IS the recovery path.
        existed = (source_id, content_hash) in self._artifacts
        self._artifacts.pop((source_id, content_hash), None)
        return existed

    def supports_signing(self) -> bool:
        return False

    def signed_get_url(self, *, source_id: str, content_hash: str, expires_in: timedelta) -> str:
        raise SigningUnavailable("this Source Artifact Store adapter cannot issue signed URLs")

    def put_binding_manifest(self, version: str, manifest: bytes) -> None:
        self._manifests[version] = manifest

    def get_binding_manifest(self, version: str) -> bytes:
        raw = self._manifests.get(version)
        if raw is None:
            raise ArtifactUnavailable(f"no Source Binding Manifest for version {version!r}")
        return raw

    def list_binding_versions(self) -> list[str]:
        return sorted(self._manifests)


class LocalDirectoryArtifactStore:
    """Local-filesystem adapter for single-maintainer development (#164).

    Layout (private state, deliberately outside any Knowledge Base root the
    loader scans; callers choose the directory)::

        <root>/artifacts/<source_id>/<content_hash>       original bytes
        <root>/artifacts/<source_id>/<content_hash>.meta  safe metadata
        <root>/bindings/<version>.json                    binding manifests

    The physical path is private adapter state: it never appears in public
    manifests, exports, or diagnostics. Signing is unavailable (local paths
    are not URLs).
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if self.root.exists() and not self.root.is_dir():
            raise NotADirectoryError(self.root)

    def _artifact_path(self, source_id: str, content_hash: str) -> Path:
        # source_id is a validated registry label (lowercase, no "/") and
        # content_hash is hex; confine defensively anyway.
        if "/" in source_id or source_id in {"", ".", ".."}:
            raise ArtifactStoreError("invalid source identity")
        return self.root / "artifacts" / source_id / content_hash

    def put_artifact(
        self,
        *,
        source_id: str,
        content_hash: str,
        raw_bytes: bytes,
        content_type: str | None,
        filename: str | None,
    ) -> None:
        if artifact_content_hash(raw_bytes) != content_hash:
            raise ArtifactStoreError("artifact bytes do not hash to the declared content hash")
        path = self._artifact_path(source_id, content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # Create-only: never rewrite a retained artifact.
            if self._verify(path, content_hash):
                return
            raise ArtifactStoreError("an artifact already exists at this identity")
        meta = msgspec.json.encode(
            {
                "content_type": content_type,
                "filename": filename,
                "size": len(raw_bytes),
                # Attachment-preferring disposition for delivery tooling
                # (ADR-0020 #165): same stored metadata contract as the S3
                # adapter's user metadata.
                "content_disposition": f'attachment; filename="{filename or f"{source_id}.bin"}"',
            }
        )
        path.write_bytes(raw_bytes)
        path.with_suffix(".meta").write_bytes(meta)
        if not self._verify(path, content_hash):
            path.unlink(missing_ok=True)
            raise ArtifactStoreError("stored artifact failed digest verification")

    def _verify(self, path: Path, content_hash: str) -> bool:
        raw = path.read_bytes()
        return artifact_content_hash(raw) == content_hash

    def get_artifact(self, *, source_id: str, content_hash: str) -> bytes:
        path = self._artifact_path(source_id, content_hash)
        # Read-first (not ``exists``-first): a permission failure must surface
        # as access denial, never masquerade as a missing artifact (#165).
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise ArtifactUnavailable("artifact not retained") from None
        except PermissionError as exc:
            raise ArtifactAccessDenied(
                "access denied: cannot read the private Source Artifact Store"
            ) from exc
        if artifact_content_hash(raw) != content_hash:
            raise ArtifactCorruption("stored artifact failed digest verification")
        return raw

    def artifact_exists(self, *, source_id: str, content_hash: str) -> bool:
        try:
            self.get_artifact(source_id=source_id, content_hash=content_hash)
        except ArtifactAccessDenied:
            # Denial is unknown-presence, not absence (#165): fail closed so
            # gates and inspection never misreport denied reads as missing.
            raise
        except ArtifactStoreError:
            return False
        return True

    def delete_artifact(self, *, source_id: str, content_hash: str) -> bool:
        # Path presence (not digest): corrupted residue stays deletable.
        path = self._artifact_path(source_id, content_hash)
        if not path.exists():
            return False
        path.unlink()
        path.with_suffix(".meta").unlink(missing_ok=True)
        return True

    def supports_signing(self) -> bool:
        return False

    def signed_get_url(self, *, source_id: str, content_hash: str, expires_in: timedelta) -> str:
        raise SigningUnavailable("this Source Artifact Store adapter cannot issue signed URLs")

    def put_binding_manifest(self, version: str, manifest: bytes) -> None:
        if "/" in version or version in {"", ".", ".."}:
            raise ArtifactStoreError("invalid version label")
        bindings = self.root / "bindings"
        bindings.mkdir(parents=True, exist_ok=True)
        (bindings / f"{version}.json").write_bytes(manifest)

    def get_binding_manifest(self, version: str) -> bytes:
        # Same label constraint as put_binding_manifest (defense in depth):
        # a read request can never traverse the private bindings directory.
        if "/" in version or "\\" in version or version in {"", ".", ".."}:
            raise ArtifactStoreError("invalid version label")
        path = self.root / "bindings" / f"{version}.json"
        # Read-first: a permission failure is access denial, not a missing
        # manifest (#165).
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise ArtifactUnavailable(
                f"no Source Binding Manifest for version {version!r}"
            ) from None
        except PermissionError as exc:
            raise ArtifactAccessDenied(
                "access denied: cannot read the private Source Artifact Store"
            ) from exc

    def list_binding_versions(self) -> list[str]:
        return sorted(p.name.removesuffix(".json") for p in (self.root / "bindings").glob("*.json"))


class S3ArtifactStore:
    """S3/object-store adapter over an injected obstore client (#164).

    The caller (deployment configuration) builds the obstore ``ObjectStore``
    with its own credentials; this adapter never sees them. Physical layout
    (private, outside public Published Version prefixes)::

        {prefix}/artifacts/{source_id}/{content_hash}
        {prefix}/bindings/{version}.json

    Uploads are create-only; an identical retry is digest-verified and kept.
    Signing issues a presigned GET for ONE exact object and one HTTP method
    through obstore's signer — never prefix or wildcard access.
    """

    def __init__(self, store: Any, prefix: str) -> None:
        from lumio_wiki.s3_location import _require_obstore

        self._obstore = _require_obstore()
        self._store = store
        self._prefix = str(prefix).strip("/")

    def _key(self, *parts: str) -> str:
        from lumio_wiki.s3_location import _join

        return _join(self._prefix, *parts)

    def put_artifact(
        self,
        *,
        source_id: str,
        content_hash: str,
        raw_bytes: bytes,
        content_type: str | None,
        filename: str | None,
    ) -> None:
        if artifact_content_hash(raw_bytes) != content_hash:
            raise ArtifactStoreError("artifact bytes do not hash to the declared content hash")
        from obstore.exceptions import AlreadyExistsError  # type: ignore[import-not-found]

        key = self._key("artifacts", source_id, content_hash)
        # Stored response metadata for authorized inspection (ADR-0020): the
        # safe filename, content type, and an attachment-preferring content
        # disposition ride the object as user metadata so delivery tooling
        # can force a download with a safe name instead of inline rendering.
        # ponytail: obstore 0.11 cannot set the real Content-Disposition
        # response header on GET/sign — add a response-content-disposition
        # override to signed_get_url when obstore exposes one.
        safe_name = filename or f"{source_id}.bin"
        attributes = {
            "filename": safe_name,
            "content_type": content_type or "application/octet-stream",
            "content_disposition": f'attachment; filename="{safe_name}"',
        }
        try:
            self._obstore.put(
                self._store, key, raw_bytes, mode="create", attributes=attributes
            )
        except AlreadyExistsError:
            # Create-only for a fresh identity; an identical retry must
            # verify what is already there, never overwrite it.
            pass
        if not self.artifact_exists(source_id=source_id, content_hash=content_hash):
            raise ArtifactStoreError("stored artifact failed digest verification")

    def get_artifact(self, *, source_id: str, content_hash: str) -> bytes:
        from obstore.exceptions import (  # type: ignore[import-not-found]
            PermissionDeniedError,
            UnauthenticatedError,
        )

        key = self._key("artifacts", source_id, content_hash)
        try:
            raw = bytes(self._obstore.get(self._store, key).bytes())
        except FileNotFoundError:
            raise ArtifactUnavailable("artifact not retained") from None
        except (PermissionDeniedError, UnauthenticatedError) as exc:
            raise ArtifactAccessDenied(
                "access denied: credentials cannot read the private Source "
                "Artifact Store"
            ) from exc
        if artifact_content_hash(raw) != content_hash:
            raise ArtifactCorruption("stored artifact failed digest verification")
        return raw

    def artifact_exists(self, *, source_id: str, content_hash: str) -> bool:
        try:
            self.get_artifact(source_id=source_id, content_hash=content_hash)
        except ArtifactAccessDenied:
            # Denial is unknown-presence, not absence (#165): fail closed so
            # gates and inspection never misreport denied reads as missing.
            raise
        except ArtifactStoreError:
            return False
        return True

    def delete_artifact(self, *, source_id: str, content_hash: str) -> bool:
        # HEAD presence (not digest verification): corrupted residue stays
        # explicitly deletable — that is the recovery path.
        key = self._key("artifacts", source_id, content_hash)
        try:
            self._obstore.head(self._store, key)
        except FileNotFoundError:
            return False
        self._obstore.delete(self._store, key)
        return True

    def supports_signing(self) -> bool:
        return True

    def signed_get_url(self, *, source_id: str, content_hash: str, expires_in: timedelta) -> str:
        from obstore.exceptions import (  # type: ignore[import-not-found]
            PermissionDeniedError,
            UnauthenticatedError,
        )

        key = self._key("artifacts", source_id, content_hash)
        try:
            return str(self._obstore.sign(self._store, "GET", key, expires_in=expires_in))
        except SigningUnavailable:
            raise
        except (PermissionDeniedError, UnauthenticatedError) as exc:
            # A signing credential without object-read authority is access
            # denial, not a signing-capability gap (#165).
            raise ArtifactAccessDenied(
                "access denied: credentials cannot sign for this artifact"
            ) from exc
        except Exception as exc:  # transport/signer failure — never leak keys
            raise SigningUnavailable(
                "could not issue a signed URL for this exact artifact"
            ) from exc

    def put_binding_manifest(self, version: str, manifest: bytes) -> None:
        key = self._key("bindings", f"{version}.json")
        self._obstore.put(self._store, key, manifest)

    def get_binding_manifest(self, version: str) -> bytes:
        from obstore.exceptions import (  # type: ignore[import-not-found]
            PermissionDeniedError,
            UnauthenticatedError,
        )

        key = self._key("bindings", f"{version}.json")
        try:
            raw = bytes(self._obstore.get(self._store, key).bytes())
        except FileNotFoundError:
            raise ArtifactUnavailable(
                f"no Source Binding Manifest for version {version!r}"
            ) from None
        except (PermissionDeniedError, UnauthenticatedError) as exc:
            raise ArtifactAccessDenied(
                "access denied: credentials cannot read the private Source "
                "Artifact Store"
            ) from exc
        return raw

    def list_binding_versions(self) -> list[str]:
        prefix = self._key("bindings", "")
        versions = set()
        for batch in self._obstore.list(self._store, prefix=prefix):
            for obj in batch:
                name = (obj["path"] if isinstance(obj, dict) else obj["path"]).rsplit("/", 1)[-1]
                versions.add(name.removesuffix(".json"))
        return sorted(versions)


def sweep_orphaned_uploads(store: SourceArtifactStore, registry: Any) -> list[tuple[str, str]]:
    """Report uploaded artifacts the registry has no binding for (#164).

    An upload that succeeded but whose registry binding failed is a
    *sweepable orphan* (ADR-0020): recoverable, reported here with the exact
    identity, and safe to re-run — the idempotent saga re-uploads create-only
    and then records the binding. Physical keys are never exposed.

    The registry exposes ``list()`` → ``KnowledgeSource`` records; each
    retained-but-unbound artifact is reported as ``(source_id, content_hash)``
    so a Maintainer can re-bind (or explicitly delete) it.
    """
    orphans: list[tuple[str, str]] = []
    for source in registry.list():
        available = {
            version.content_hash for version in source.versions if version.artifact_available
        }
        # Candidates: the store holds an artifact the registry never bound —
        # exactly the recoverable saga failure (upload succeeded, binding
        # failed). Reported by identity so a Maintainer can re-bind (rerun
        # managed ingest, which re-verifies then records) or delete it.
        for version in source.versions:
            if version.content_hash in available:
                continue
            if store.artifact_exists(source_id=source.source_id, content_hash=version.content_hash):
                orphans.append((source.source_id, version.content_hash))
    return orphans


# ---------------------------------------------------------------------------
# The private Source Binding Manifest (issue #164, ADR-0020).
# ---------------------------------------------------------------------------


class SourceBindingEntry(msgspec.Struct, frozen=True):
    """One ``(page, source_id)`` reference bound to an exact Source Version.

    Carries the safe inspection metadata recorded for that version. Contains
    no raw bytes, no object keys, and no local paths.
    """

    page_title: str
    source_id: str
    content_hash: str
    content_type: str | None = None
    filename: str | None = None
    size: int | None = None
    synthetic_page: bool = False


class SourceBindingManifest(msgspec.Struct, frozen=True):
    """Private binding of one Published Version to exact Source Versions.

    Written to the Source Artifact Store BEFORE public activation so
    inspection of an older Published Version always resolves the exact bytes
    used for it, never the Knowledge Source's newest version. Never part of
    the portable Knowledge Base, exports, or fingerprints.
    """

    published_version: str
    fingerprint: str
    created_at: str
    entries: list[SourceBindingEntry] = msgspec.field(default_factory=list)


def build_binding_manifest(
    *,
    published_version: str,
    fingerprint: str,
    registry: Any,
    pages: list[Any],
    now: str,
) -> SourceBindingManifest:
    """Build the manifest for the pages being published (#164, ADR-0020).

    Every non-synthetic page's declared ``sources[].id`` is resolved through
    the private registry to that source's CURRENT version hash. Synthetic
    pages may omit provenance (ADR-0014) and are recorded with
    ``synthetic_page=True`` only when they declare a source id anyway.
    ``pages`` are loaded Compiled Page records (``title``/``sources``/
    ``synthetic`` attributes). A declared source id the registry does not
    know is public provenance only — the manifest binds privately registered
    identities exclusively.
    """
    from lumio_wiki.source_registry import SourceRegistryError

    entries: list[SourceBindingEntry] = []
    for page in pages:
        for source in page.sources:
            if not source.id:
                continue
            try:
                source_record = registry.get(source.id)
            except SourceRegistryError:
                continue
            current = source_record.versions[-1]
            entries.append(
                SourceBindingEntry(
                    page_title=page.title,
                    source_id=source.id,
                    content_hash=current.content_hash,
                    content_type=current.content_type,
                    filename=current.filename,
                    size=current.size,
                    synthetic_page=bool(getattr(page, "synthetic", False)),
                )
            )
    return SourceBindingManifest(
        published_version=published_version,
        fingerprint=fingerprint,
        created_at=now,
        entries=entries,
    )


def write_binding_manifest(store: SourceArtifactStore, manifest: SourceBindingManifest) -> bytes:
    """Serialize and store the manifest in the private store (pre-activation)."""
    raw = msgspec.json.encode(manifest)
    store.put_binding_manifest(manifest.published_version, raw)
    return raw


def required_coverage_missing(
    store: SourceArtifactStore,
    registry: Any,
    pages: list[Any],
) -> list[str]:
    """Unmet artifact requirements under REQUIRED retention (#164, ADR-0020).

    "Publication blocks if a referenced non-synthetic source lacks a verified
    artifact" — evaluated against the pages being activated, not just the
    manifest: a source id the private registry does not know can never have a
    verified artifact, so it blocks (the manifest only binds registered
    identities; an unregistered reference is a coverage hole, not a pass).
    The registry availability flag alone is not trust either: the store is
    re-verified live for every referenced identity.
    """
    from lumio_wiki.source_registry import SourceRegistryError, _validate_source_id

    missing: list[str] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        if bool(getattr(page, "synthetic", False)):
            continue
        for source in page.sources:
            if not source.id or (page.title, source.id) in seen:
                continue
            seen.add((page.title, source.id))
            # Never echo an invalid, possibly secret-bearing source id (the
            # registry's own boundary convention): validate the shape first
            # and report a generic label for anything malformed.
            try:
                _validate_source_id(source.id)
            except SourceRegistryError:
                missing.append(f"{page.title}:<invalid source id> (unregistered source)")
                continue
            try:
                current = registry.get(source.id).versions[-1]
            except SourceRegistryError:
                # A validated-but-unknown id is a safe label and may be echoed.
                missing.append(f"{page.title}:{source.id} (unregistered source)")
                continue
            if not store.artifact_exists(source_id=source.id, content_hash=current.content_hash):
                missing.append(f"{page.title}:{source.id}")
    return missing


# ---------------------------------------------------------------------------
# The idempotent managed-ingest artifact saga (issue #164, ADR-0020).
# ---------------------------------------------------------------------------


def retain_artifact(
    store: SourceArtifactStore,
    registry: Any,
    *,
    source_id: str,
    raw_bytes: bytes,
    content_type: str | None,
    filename: str | None,
) -> str:
    """Upload + bind one artifact: an idempotent, recoverable saga (#164).

    Order (ADR-0020): SHA-256 → create-only upload → verify size/digest →
    record the registry binding. Any failure raises and is recoverable by
    re-running: the retry re-uploads create-only (a previous partial upload is
    digest-verified and kept) and then records the binding. A failed upload is
    NEVER reported as retained; an upload without a binding is a sweepable
    orphan (:func:`sweep_orphaned_uploads`).

    Returns the content hash. The registry must already hold the Source
    Version (managed ingest registers identity first, then retains).
    """
    if len(raw_bytes) > MAX_ARTIFACT_BYTES:
        raise ArtifactStoreError(
            f"source exceeds the maximum retained artifact size "
            f"({len(raw_bytes)} > {MAX_ARTIFACT_BYTES} bytes); the Source "
            "Version is registered hash-only — retention refused"
        )
    content_hash = artifact_content_hash(raw_bytes)
    store.put_artifact(
        source_id=source_id,
        content_hash=content_hash,
        raw_bytes=raw_bytes,
        content_type=content_type,
        filename=filename,
    )
    registry.record_artifact_binding(source_id, content_hash)
    return content_hash


# ---------------------------------------------------------------------------
# Required-retention activation gate (issue #164, ADR-0020).
# ---------------------------------------------------------------------------


class RetentionRequiredError(ArtifactStoreError):
    """Activation blocked: a referenced source lacks a verified artifact."""


def activation_binding_hook(
    *,
    artifact_store: SourceArtifactStore,
    registry: Any,
    source_root: str | Path,
    required: bool,
):
    """Build the ``before_activation`` hook for S3 publication (#164).

    The returned closure builds the private Source Binding Manifest from the
    Knowledge Base root the publisher is publishing (``source_root`` — the
    same root passed to ``publish_s3_version``), checks artifact coverage
    when retention is REQUIRED (blocking activation by raising
    :class:`RetentionRequiredError`; disabled retention preserves today's
    hash-only behavior and never blocks), stores the manifest in the private
    Source Artifact Store, and only then lets activation proceed. No source
    bytes or binding state ever enter the public manifest.
    """

    def hook(prepared: Any) -> None:
        from lumio_wiki.knowledge_base import load_knowledge_base

        kb, report = load_knowledge_base(source_root)
        if not report.is_valid:
            raise ArtifactStoreError(
                "cannot bind sources: the local Knowledge Base no longer validates"
            )
        manifest = build_binding_manifest(
            published_version=prepared.version,
            fingerprint=prepared.fingerprint,
            registry=registry,
            pages=list(kb.pages),
            now=_utc_now_iso(),
        )
        if required:
            missing = required_coverage_missing(artifact_store, registry, list(kb.pages))
            if missing:
                raise RetentionRequiredError(
                    "artifact retention is required but these referenced sources "
                    "lack a verified artifact: " + ", ".join(missing)
                )
        write_binding_manifest(artifact_store, manifest)

    return hook


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def verify_rollback_coverage(
    artifact_store: SourceArtifactStore, version: str, *, required: bool
) -> None:
    """Gate ROLLBACK activation under required retention (#164, ADR-0020).

    Activation blocks until every referenced non-synthetic source has a
    verified artifact — rollback is an activation of an already complete
    Published Version, so its PRIVATE binding manifest (not the current
    registry state) defines coverage for that historical version. A version
    with no stored manifest cannot prove coverage and fails closed.
    No-op when retention is not required.
    """
    if not required:
        return
    try:
        raw = artifact_store.get_binding_manifest(version)
    except ArtifactStoreError as exc:
        raise RetentionRequiredError(
            f"artifact retention is required but Published Version {version!r} "
            "has no private Source Binding Manifest to verify coverage"
        ) from exc
    manifest = msgspec.json.decode(raw, type=SourceBindingManifest)
    missing = [
        f"{entry.page_title}:{entry.source_id}"
        for entry in manifest.entries
        if not entry.synthetic_page
        and not artifact_store.artifact_exists(
            source_id=entry.source_id, content_hash=entry.content_hash
        )
    ]
    if missing:
        raise RetentionRequiredError(
            "artifact retention is required but these sources bound by "
            f"{version!r} lack a verified artifact: " + ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# Explicit artifact deletion with Published Version disclosure (#164).
# ---------------------------------------------------------------------------


def affected_published_versions(
    artifact_store: SourceArtifactStore, *, source_id: str, content_hash: str
) -> list[str]:
    """Report the Published Versions whose binding covers this exact artifact.

    Deletion is explicit and must disclose which Published Versions lose
    inspection coverage (ADR-0020). This lists the versions whose stored
    binding manifest binds ``(source_id, content_hash)``.
    """
    affected: list[str] = []
    for version in _bound_versions(artifact_store):
        try:
            raw = artifact_store.get_binding_manifest(version)
            manifest = msgspec.json.decode(raw, type=SourceBindingManifest)
        except ArtifactStoreError:
            continue
        if any(
            entry.source_id == source_id and entry.content_hash == content_hash
            for entry in manifest.entries
        ):
            affected.append(version)
    return affected


def delete_artifact_with_disclosure(
    artifact_store: SourceArtifactStore,
    registry: Any,
    *,
    source_id: str,
    content_hash: str,
) -> list[str]:
    """Explicitly delete one artifact and disclose affected versions (#164).

    Retirement NEVER deletes historical artifacts; this is the only deletion
    path. Returns the Published Versions that lost inspection coverage, and
    clears the registry binding so metadata never claims a retained artifact
    the store no longer holds.
    """
    affected = affected_published_versions(
        artifact_store, source_id=source_id, content_hash=content_hash
    )
    artifact_store.delete_artifact(source_id=source_id, content_hash=content_hash)
    registry.clear_artifact_binding(source_id, content_hash)
    return affected


def _bound_versions(artifact_store: SourceArtifactStore) -> list[str]:
    """Every Published Version label with a stored binding manifest.

    Identity-oriented seam: each adapter enumerates its own private binding
    manifests; no physical keys cross this boundary.
    """
    return list(artifact_store.list_binding_versions())
