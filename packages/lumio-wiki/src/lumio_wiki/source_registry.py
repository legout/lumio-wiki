"""Private Knowledge Source lineage state for proposal-first ingest workflows."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import msgspec


class SourceRegistryError(ValueError):
    """A requested private Knowledge Source state transition is invalid."""


class SourceVersion(msgspec.Struct, frozen=True):
    """An immutable content-hashed version recorded under one source identity."""

    source_id: str
    content_hash: str
    created_at: str


class KnowledgeSource(msgspec.Struct, frozen=True):
    """Private stable source identity and its immutable versions."""

    source_id: str
    status: str
    versions: list[SourceVersion]


class RetirementCandidate(msgspec.Struct, frozen=True):
    """A non-mutating signal that a Maintainer may explicitly review."""

    id: str
    source_id: str
    trigger: str
    status: str
    created_at: str


class PendingSourceTransition(msgspec.Struct, frozen=True):
    """A lifecycle change held until its Ingest Proposal publishes."""

    proposal_id: str
    action: str
    source_id: str
    version: SourceVersion | None = None


class _RegistryState(msgspec.Struct, frozen=True):
    sources: list[KnowledgeSource] = msgspec.field(default_factory=list)
    candidates: list[RetirementCandidate] = msgspec.field(default_factory=list)
    pending_transitions: list[PendingSourceTransition] = msgspec.field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SourceRegistry:
    """Persist private source identities outside portable Knowledge Base content."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / "sources.json"
        self._state = self._read()

    def _read(self) -> _RegistryState:
        if not self._path.exists():
            return _RegistryState()
        return msgspec.json.decode(self._path.read_bytes(), type=_RegistryState)

    def _write(self) -> None:
        temporary = self._path.with_suffix(".tmp")
        temporary.write_bytes(msgspec.json.encode(self._state))
        os.replace(temporary, self._path)

    def get(self, source_id: str) -> KnowledgeSource:
        for source in self._state.sources:
            if source.source_id == source_id:
                return source
        raise SourceRegistryError(f"unknown Knowledge Source {source_id!r}")

    def list(self) -> list[KnowledgeSource]:
        return list(self._state.sources)

    def register_source(self, source_id: str, raw_bytes: bytes) -> SourceVersion:
        """Append a version to an explicitly chosen active source identity."""
        if not source_id.strip():
            raise SourceRegistryError("source_id is required")
        version = SourceVersion(
            source_id=source_id,
            content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            created_at=_now(),
        )
        sources = list(self._state.sources)
        for index, source in enumerate(sources):
            if source.source_id != source_id:
                continue
            if source.status != "active":
                raise SourceRegistryError(
                    f"retired Knowledge Source {source_id!r} must be reactivated explicitly"
                )
            sources[index] = msgspec.structs.replace(
                source,
                versions=[*source.versions, version],
            )
            self._state = msgspec.structs.replace(self._state, sources=sources)
            self._write()
            return version
        sources.append(KnowledgeSource(source_id=source_id, status="active", versions=[version]))
        self._state = msgspec.structs.replace(self._state, sources=sources)
        self._write()
        return version
