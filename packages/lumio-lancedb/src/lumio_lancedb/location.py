"""Typed local-or-remote LanceDB index locations (ADR-0013).

A LanceDB derived index lives either on the local filesystem (the original
behavior) or under an immutable S3 Published Version's ``derived/lance/``
prefix. This module normalizes both into one ``IndexLocation`` so build and
search work uniformly:

* LanceDB connects to its own ``uri`` with the ``storage_options`` recorded
  here — Lumio never hands its obstore client to LanceDB (ADR-0013). A remote
  connection uses ``read_consistency_interval=timedelta(0)`` for the strongest
  documented cross-process visibility (research:
  ``docs/research/lancedb-s3-remote-storage.md``).
* The small metadata sidecars beside the tables (source fingerprint,
  embedding-model identity) are read/written through the same object store, so
  a remote index carries its freshness and model identity with no managed local
  bytes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from lumio_wiki.fingerprint_store import FINGERPRINT_FILE

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from obstore.store import ObjectStore

__all__ = [
    "IndexLocation",
    "LocalIndexLocation",
    "RemoteIndexLocation",
    "as_location",
]


@runtime_checkable
class IndexLocation(Protocol):
    """Where a LanceDB index lives and how its metadata sidecars are reached.

    Build and search consume this uniformly: ``connect()`` returns a LanceDB
    connection, ``has_index()`` is a cheap presence probe (missing/unhealthy
    indexes trigger zero-index fallback), and ``read_sidecar`` /
    ``write_sidecar`` move the freshness + model-identity JSON beside the
    tables (local filesystem or object store).
    """

    @property
    def describe(self) -> str:
        """A human-readable, secret-free description for diagnostics."""
        ...

    @property
    def is_remote(self) -> bool:
        """True when the index lives on an object store rather than local disk."""
        ...

    def connect(self) -> Any:
        """Open and return a LanceDB connection to this index location."""
        ...

    def has_index(self) -> bool:
        """Return True when an index appears to have been published here.

        Cheap and deterministic — never opens a LanceDB connection — so it can
        gate the zero-index fallback without a network round-trip.
        """
        ...

    def prepare(self) -> None:
        """Ensure the location is writable (create a local dir; no-op remote)."""
        ...

    def read_sidecar(self, name: str) -> bytes | None:
        """Read a metadata sidecar, or ``None`` when it is absent."""
        ...

    def write_sidecar(self, name: str, data: bytes) -> None:
        """Write a metadata sidecar beside the index."""
        ...


class LocalIndexLocation:
    """A LanceDB index on the local filesystem (the original behavior)."""

    is_remote = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def describe(self) -> str:
        return str(self.path)

    def connect(self) -> Any:
        import lancedb  # local build/search already require the dependency

        return lancedb.connect(self.path)

    def has_index(self) -> bool:
        return self.path.exists()

    def prepare(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)

    def read_sidecar(self, name: str) -> bytes | None:
        sidecar = self.path / name
        return sidecar.read_bytes() if sidecar.exists() else None

    def write_sidecar(self, name: str, data: bytes) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / name).write_bytes(data)


class RemoteIndexLocation:
    """A LanceDB index under an immutable S3 Published Version's derived prefix.

    LanceDB connects to its own ``uri`` with ``storage_options`` (ADR-0013):
    Lumio never passes its obstore client to LanceDB. The metadata sidecars are
    read/written through ``store`` at ``sidecar_prefix`` so the index carries
    its source fingerprint and embedding-model identity beside the tables.

    ``has_index()`` probes the sidecar store only (no LanceDB connection), so a
    missing index is detected deterministically and the adapter can select
    zero-index retrieval without a doomed network attempt.
    """

    is_remote = True

    def __init__(
        self,
        uri: str,
        *,
        storage_options: dict[str, str] | None = None,
        store: ObjectStore | None = None,
        sidecar_prefix: str = "",
    ) -> None:
        self.uri = uri
        self.storage_options = dict(storage_options) if storage_options else None
        self.store = store
        self.sidecar_prefix = sidecar_prefix.strip("/")

    @property
    def describe(self) -> str:
        return self.uri

    def _require_store(self) -> Any:
        if self.store is None:
            raise ValueError(
                "a RemoteIndexLocation needs an obstore ObjectStore for its "
                "metadata sidecars; pass store=... (the '[s3]' extra)"
            )
        return self.store

    def connect(self) -> Any:
        import lancedb  # remote build/search already require the dependency

        return lancedb.connect(
            self.uri,
            storage_options=self.storage_options,
            read_consistency_interval=timedelta(0),
        )

    def has_index(self) -> bool:
        # Cheap, deterministic presence probe via the freshness sidecar: the
        # publisher writes it when building, so its absence means no index was
        # published here (-> zero-index fallback) without a LanceDB connection.
        return self.read_sidecar(FINGERPRINT_FILE) is not None

    def prepare(self) -> None:
        # Object stores create keys on write; nothing to create up front.
        return None

    def _sidecar_key(self, name: str) -> str:
        return f"{self.sidecar_prefix}/{name}" if self.sidecar_prefix else name

    def read_sidecar(self, name: str) -> bytes | None:
        store = self._require_store()
        import obstore  # behind the '[s3]' extra; only remote locations reach here

        try:
            result = obstore.get(store, self._sidecar_key(name))
            return bytes(result.bytes())
        except Exception:
            # A missing or unreachable sidecar means "not published here"; the
            # caller treats None as absence and selects zero-index fallback.
            return None

    def write_sidecar(self, name: str, data: bytes) -> None:
        store = self._require_store()
        import obstore  # behind the '[s3]' extra

        obstore.put(store, self._sidecar_key(name), data)


def as_location(value: str | Path | IndexLocation | None) -> IndexLocation | None:
    """Normalize a raw path/URI or ``IndexLocation`` into an ``IndexLocation``.

    ``None`` passes through (callers that accept an optional index use it to
    signal "no index configured"). A bare ``str`` / ``Path`` becomes a local
    location; an existing ``IndexLocation`` is returned unchanged so callers can
    inject a typed remote location directly.
    """
    if value is None:
        return None
    if isinstance(value, (LocalIndexLocation, RemoteIndexLocation)):
        return value
    if isinstance(value, (str, Path)):
        return LocalIndexLocation(value)
    raise TypeError(
        f"expected a Path/str or IndexLocation, got {type(value).__name__}"
    )
