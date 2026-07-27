"""Private Knowledge Source lineage state for proposal-first ingest workflows."""

from __future__ import annotations

import hashlib
import os
import uuid
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


def _source_version(source_id: str, raw_bytes: bytes) -> SourceVersion:
    return SourceVersion(
        source_id=source_id,
        content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        created_at=_now(),
    )


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
        version = _source_version(source_id, raw_bytes)
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

    def stage_retirement(self, source_id: str) -> PendingSourceTransition:
        """Validate and prepare a retirement without changing source status."""
        source = self.get(source_id)
        if source.status != "active":
            raise SourceRegistryError(f"Knowledge Source {source_id!r} must be active to retire")
        self._ensure_no_pending_transition(source_id)
        return PendingSourceTransition("", "retire", source_id)

    def stage_reactivation(self, source_id: str, raw_bytes: bytes) -> PendingSourceTransition:
        """Prepare a fresh version for a retired source without activating it."""
        source = self.get(source_id)
        if source.status != "retired":
            raise SourceRegistryError(
                f"Knowledge Source {source_id!r} must be retired to reactivate"
            )
        self._ensure_no_pending_transition(source_id)
        return PendingSourceTransition(
            "", "reactivate", source_id, _source_version(source_id, raw_bytes)
        )

    def _ensure_no_pending_transition(self, source_id: str) -> None:
        if any(transition.source_id == source_id for transition in self._state.pending_transitions):
            raise SourceRegistryError(
                f"Knowledge Source {source_id!r} already has a pending transition"
            )

    def bind_pending(self, transition: PendingSourceTransition, proposal_id: str) -> None:
        """Persist a prepared transition under its staged proposal identity."""
        bound = msgspec.structs.replace(transition, proposal_id=proposal_id)
        self._state = msgspec.structs.replace(
            self._state,
            pending_transitions=[*self._state.pending_transitions, bound],
        )
        self._write()

    def cancel_transition(self, proposal_id: str) -> None:
        """Remove a pending transition when its proposal is discarded."""
        pending = [
            transition
            for transition in self._state.pending_transitions
            if transition.proposal_id != proposal_id
        ]
        if len(pending) == len(self._state.pending_transitions):
            raise SourceRegistryError(f"proposal {proposal_id!r} has no pending source transition")
        self._state = msgspec.structs.replace(self._state, pending_transitions=pending)
        self._write()

    def apply_transition(self, proposal_id: str) -> None:
        """Apply and remove the source transition bound to a published proposal."""
        transition = next(
            (
                pending
                for pending in self._state.pending_transitions
                if pending.proposal_id == proposal_id
            ),
            None,
        )
        if transition is None:
            raise SourceRegistryError(f"proposal {proposal_id!r} has no pending source transition")
        sources = list(self._state.sources)
        source_index = next(
            index
            for index, source in enumerate(sources)
            if source.source_id == transition.source_id
        )
        source = sources[source_index]
        if transition.action == "retire":
            if source.status != "active":
                raise SourceRegistryError(
                    f"Knowledge Source {source.source_id!r} must be active to retire"
                )
            sources[source_index] = msgspec.structs.replace(source, status="retired")
        elif transition.action == "reactivate":
            if source.status != "retired" or transition.version is None:
                raise SourceRegistryError(
                    f"Knowledge Source {source.source_id!r} cannot be reactivated"
                )
            sources[source_index] = msgspec.structs.replace(
                source,
                status="active",
                versions=[*source.versions, transition.version],
            )
        else:
            raise SourceRegistryError(f"unknown source transition action {transition.action!r}")
        pending = [
            item for item in self._state.pending_transitions if item.proposal_id != proposal_id
        ]
        previous_state = self._state
        self._state = msgspec.structs.replace(
            self._state, sources=sources, pending_transitions=pending
        )
        try:
            self._write()
        except Exception:
            self._state = previous_state
            raise

    def record_retirement_candidate(self, source_id: str, trigger: str) -> RetirementCandidate:
        """Record a missing-source signal without changing source support."""
        source = self.get(source_id)
        if source.status != "active":
            raise SourceRegistryError(
                f"Knowledge Source {source_id!r} must be active for retirement review"
            )
        if not trigger.strip():
            raise SourceRegistryError("retirement candidate trigger is required")
        candidate = RetirementCandidate(
            id=uuid.uuid4().hex,
            source_id=source_id,
            trigger=trigger,
            status="pending",
            created_at=_now(),
        )
        self._state = msgspec.structs.replace(
            self._state, candidates=[*self._state.candidates, candidate]
        )
        self._write()
        return candidate

    def get_candidate(self, candidate_id: str) -> RetirementCandidate:
        for candidate in self._state.candidates:
            if candidate.id == candidate_id:
                return candidate
        raise SourceRegistryError(f"unknown retirement candidate {candidate_id!r}")

    def _decide_candidate(self, candidate_id: str, status: str) -> RetirementCandidate:
        candidates = list(self._state.candidates)
        for index, candidate in enumerate(candidates):
            if candidate.id != candidate_id:
                continue
            if candidate.status != "pending":
                raise SourceRegistryError(f"retirement candidate {candidate_id!r} is not pending")
            decided = msgspec.structs.replace(candidate, status=status)
            candidates[index] = decided
            self._state = msgspec.structs.replace(self._state, candidates=candidates)
            self._write()
            return decided
        raise SourceRegistryError(f"unknown retirement candidate {candidate_id!r}")

    def dismiss_retirement_candidate(self, candidate_id: str) -> RetirementCandidate:
        return self._decide_candidate(candidate_id, "dismissed")

    def confirm_retirement_candidate(self, candidate_id: str) -> RetirementCandidate:
        return self._decide_candidate(candidate_id, "confirmed")
