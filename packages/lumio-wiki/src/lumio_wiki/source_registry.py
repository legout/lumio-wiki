"""Private Knowledge Source lineage state for proposal-first ingest workflows."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import msgspec

#: Controlled action vocabulary for a staged Source lifecycle transition (#133).
#: Names the primitive strings used by ``PendingSourceTransition.action`` and
#: :func:`safe_lifecycle_trigger_display` so the switch sites share one typed
#: reference instead of repeating bare literals. Kept off the msgspec struct
#: fields (which stay ``str``) so legacy/future persisted values still decode.
SourceLifecycleAction = Literal["retire", "reactivate"]

#: Controlled result vocabulary for managed source registration (#149). Kept
#: off the persisted msgspec structs because it describes the operation result,
#: not registry state.
SourceRegistrationAction = Literal["registered", "reused"]

#: Controlled status vocabulary for a private Knowledge Source identity.
SourceStatus = Literal["active", "retired"]

#: Controlled status vocabulary for a Retirement Candidate review decision.
RetirementCandidateStatus = Literal["pending", "confirmed", "dismissed"]

#: Maximum length of the sanitized filename persisted on a Source Version
#: (#164, ADR-0020). Long enough for real document names, short enough that a
#: pathological name cannot bloat private registry state.
SAFE_FILENAME_MAX_LENGTH = 128


def safe_artifact_filename(filename: str | None) -> str | None:
    """Return a safe, portable basename for private Source Version metadata.

    ADR-0020 (#164): a Source Version records a *safe filename* — the basename
    only, with control characters stripped and the length capped — so a local
    source path (or a crafted ``../../`` traversal) is never persisted in the
    private registry and never reaches inspection output. ``None`` and empty
    names stay ``None``; a name that sanitizes to nothing (``".."``) becomes
    ``None`` rather than an empty string.
    """
    if not filename:
        return None
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in "\r\n\t")
    base = base.strip()
    if base in {"", ".", ".."}:
        return None
    return base[:SAFE_FILENAME_MAX_LENGTH]


class SourceRegistryError(ValueError):
    """A requested private Knowledge Source state transition is invalid."""


#: Controlled vocabulary for Retirement Candidate triggers (#133).
#:
#: A Retirement Candidate must disclose *why* the signal was recorded using one
#: of these Maintainer-chosen, pipeline-safe phrases. Free text is rejected at
#: the registry/pipeline boundary so secret-bearing or arbitrary input can never
#: be persisted or rendered. Every surface (CLI, Workshop) reads this single
#: shared constant rather than inventing its own vocabulary.
RETIREMENT_CANDIDATE_TRIGGERS = (
    "watched file missing",
    "failed read",
    "object store unavailable",
    "incomplete upload",
)


def _validate_retirement_trigger(trigger: str) -> str:
    """Return ``trigger`` if it is in the controlled vocabulary.

    Raises a :class:`SourceRegistryError` with a generic, input-free message
    otherwise: a candidate trigger may carry secret-bearing material, so the
    invalid value is never echoed into an exception or persisted. This is the
    registry/pipeline boundary check; the CLI and Workshop rely on it (defense
    in depth) so a crafted request can never bypass it.
    """
    if trigger not in RETIREMENT_CANDIDATE_TRIGGERS:
        raise SourceRegistryError(
            "retirement candidate trigger must be one of the supported signals"
        )
    return trigger


#: Fixed, content-free label rendered for a persisted Retirement Candidate
#: trigger that is NOT in :data:`RETIREMENT_CANDIDATE_TRIGGERS` (#133).
#:
#: A candidate persisted *before* trigger validation may carry a secret-bearing
#: ``trigger`` value (the registry now rejects such input at the boundary, but
#: it cannot rewrite history). Rendering must never echo an unrecognized
#: persisted value: recognized triggers render verbatim and every other stored
#: value renders this single generic label. This is display-only; it never
#: rejects or destroys the legacy registry state.
RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED = "source unavailable"


def safe_candidate_trigger_display(trigger: str) -> str:
    """Return a display-safe label for a persisted Retirement Candidate trigger.

    Recognized values from :data:`RETIREMENT_CANDIDATE_TRIGGERS` render
    verbatim. Any unrecognized persisted value — including a secret-bearing
    trigger from a legacy candidate recorded before validation — renders the
    fixed :data:`RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED` label and NEVER the
    stored value. This guard is display-only: it neither rejects nor destroys
    the legacy registry, so a legacy candidate stays confirmable/dismissible.
    """
    if trigger in RETIREMENT_CANDIDATE_TRIGGERS:
        return trigger
    return RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED


#: Maximum length of a Knowledge Source id label (#133).
SOURCE_ID_MAX_LENGTH = 48

#: Obvious credential prefixes a leaked secret is likely to start with. A
#: pasted credential (``sk-...``, ``token=...``, ``password=...``) must be
#: rejected at the boundary rather than persisted as a stable identity. The
#: check is case-insensitive on the already-lowercased label.
SOURCE_ID_CREDENTIAL_PREFIXES = (
    "sk-",
    "token",
    "password",
    "secret",
    "api-key",
)

_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9._-]*$")


def _validate_source_id(source_id: str) -> str:
    """Return ``source_id`` if it is a safe, stable Knowledge Source label.

    A Knowledge Source id is a Maintainer-authored lowercase ASCII label that
    starts with a letter, contains only letters/digits/``.``/``_``/``-``, and is
    at most :data:`SOURCE_ID_MAX_LENGTH` characters. This rejects paths
    (``/``), URLs and schemes (``://``/``:``), content hashes (length and a
    non-letter lead), whitespace/control characters, uppercase, and obvious
    credential prefixes (:data:`SOURCE_ID_CREDENTIAL_PREFIXES`). The value may
    carry secret-bearing material, so the raised error is generic and NEVER
    echoes the rejected value. This is the registry/pipeline boundary check;
    the CLI and Workshop rely on it (defense in depth) so a crafted request
    can never bypass it — mirroring :func:`_validate_retirement_trigger`.
    """
    if (
        not isinstance(source_id, str)
        or not (1 <= len(source_id) <= SOURCE_ID_MAX_LENGTH)
        or not _SOURCE_ID_PATTERN.match(source_id)
    ):
        raise SourceRegistryError("source_id must be a lowercase ASCII label")
    lowered = source_id.lower()
    if any(lowered.startswith(prefix) for prefix in SOURCE_ID_CREDENTIAL_PREFIXES):
        raise SourceRegistryError("source_id must be a lowercase ASCII label")
    return source_id


#: Regex for a valid Retirement Candidate id: a lowercase UUID hex (32 chars).
#: Candidate ids are generated by the registry as ``uuid.uuid4().hex``; a
#: caller-supplied id that is not UUID-hex-shaped is an attacker probe and is
#: rejected at the boundary so it can never be interpolated into an error,
#: persisted, or used to confirm/dismiss a candidate it does not name.
_CANDIDATE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _validate_candidate_id(candidate_id: str) -> str:
    """Return ``candidate_id`` if it is UUID-hex-shaped; else raise generically.

    A Retirement Candidate id is a 32-character lowercase hex string generated
    by :func:`uuid.uuid4().hex`. A value that is not UUID-hex-shaped — a
    secret-bearing string, a path, or arbitrary text — is rejected at the
    boundary with a generic, input-free error (the value may carry
    secret-bearing material, so it is NEVER echoed). This is the registry/
    pipeline boundary check for candidate ids; it mirrors
    :func:`_validate_source_id` so every candidate lookup/decision validates
    before any lookup or interpolation (defense in depth).
    """
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID_PATTERN.match(candidate_id):
        raise SourceRegistryError("retirement candidate id must be a valid identifier")
    return candidate_id


class SourceVersion(msgspec.Struct, frozen=True):
    """An immutable content-hashed version recorded under one source identity.

    The optional private inspection metadata (``filename``, ``content_type``,
    ``size``) is recorded at registration from the ingest boundary (#164,
    ADR-0020): the filename is sanitized to a basename by
    :func:`safe_artifact_filename`, so local source paths are never persisted.
    ``artifact_available`` records that a verified artifact binding was
    recorded for this version (a failed upload is NEVER reported as retained);
    activation-time gates re-verify live against the Source Artifact Store
    rather than trusting this flag alone.
    """

    source_id: str
    content_hash: str
    created_at: str
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    artifact_available: bool = False


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


def _source_version(
    source_id: str,
    raw_bytes: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> SourceVersion:
    return SourceVersion(
        source_id=source_id,
        content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        created_at=_now(),
        filename=safe_artifact_filename(filename),
        content_type=content_type,
        size=len(raw_bytes),
    )


class SourceRegistry:
    """Persist private source identities outside portable Knowledge Base content."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if self.root.exists() and not self.root.is_dir():
            raise NotADirectoryError(self.root)
        self._path = self.root / "sources.json"
        self._state = self._read()

    def _read(self) -> _RegistryState:
        if not self._path.exists():
            return _RegistryState()
        return msgspec.json.decode(self._path.read_bytes(), type=_RegistryState)

    def _write(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_bytes(msgspec.json.encode(self._state))
        os.replace(temporary, self._path)

    def _commit(self, state: _RegistryState) -> None:
        """Swap in-memory state and persist; restore the prior state on failure.

        Every mutating method builds the prospective ``_RegistryState`` first and
        routes it through here so a persistence failure can never leave the live
        instance diverged from the durable file: the atomic ``os.replace`` in
        :meth:`_write` keeps the on-disk file at the prior state, and this helper
        restores the in-memory state to match.
        """
        previous = self._state
        self._state = state
        try:
            self._write()
        except Exception:
            self._state = previous
            raise

    def get(self, source_id: str) -> KnowledgeSource:
        # #133 final review: validate the id BEFORE lookup so a secret-bearing
        # value is rejected at the boundary (never interpolated into the
        # unknown-source error). A validated-but-unknown id is a safe label and
        # may be echoed; an invalid id never reaches the lookup or the message.
        _validate_source_id(source_id)
        for source in self._state.sources:
            if source.source_id == source_id:
                return source
        raise SourceRegistryError(f"unknown Knowledge Source {source_id!r}")

    def list(self) -> list[KnowledgeSource]:
        return list(self._state.sources)

    def register_source(
        self,
        source_id: str,
        raw_bytes: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> SourceVersion:
        """Create a NEW Knowledge Source identity under an explicit stable id.

        Register establishes a new identity only: it NEVER appends a version to
        an already-known identity (active or retired). A second registration is
        refused so unreviewed replacement cannot bypass review — reactivation is
        the only reviewed path that appends a new Source Version (#133). The id
        is validated at the boundary (:func:`_validate_source_id`) so a pasted
        path, URL, content hash, or credential is never persisted, and the
        refusal error never echoes the (possibly secret-bearing) id.

        #133 final review: explicit ``source register`` is the managed-lifecycle
        boundary for this iteration. Ordinary ingest (legacy file upload
        compatibility) does NOT flow through here and is left unchanged outside
        #133; registry integration for ordinary ingest is deferred.
        """
        _validate_source_id(source_id)
        if any(source.source_id == source_id for source in self._state.sources):
            raise SourceRegistryError(
                "Knowledge Source already registered; replacement is not "
                "available through register — retire and reactivate to add a "
                "reviewed new version"
            )
        version = _source_version(
            source_id, raw_bytes, filename=filename, content_type=content_type
        )
        sources = list(self._state.sources)
        sources.append(KnowledgeSource(source_id=source_id, status="active", versions=[version]))
        self._commit(msgspec.structs.replace(self._state, sources=sources))
        return version

    def register_or_reuse(
        self,
        source_id: str,
        raw_bytes: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> tuple[SourceVersion, SourceRegistrationAction]:
        """Resolve a managed host-Distiller source identity (issue #149).

        One deep identity rule for the managed ``ingest --compiled-page
        --source-id`` workflow, so callers never reproduce registry/proposal
        coordination:

        * a NEW ``source_id`` registers the original bytes immediately and
          returns ``(version, "registered")``;
        * an ACTIVE identity whose CURRENT Source Version has the SAME content
          hash is reused idempotently — no duplicate Source Version is created —
          and returns ``(current_version, "reused")``;
        * an ACTIVE identity with DIFFERENT bytes is rejected WITHOUT registry
          mutation and names the explicit retirement/reactivation workflow; and
        * a RETIRED identity is rejected until the Maintainer reactivates it.

        Identity is by the explicit ``source_id`` only — filename/path never
        establishes continuing identity (ADR-0014). The id is validated at the
        boundary so a pasted path, URL, content hash, or credential is never
        persisted, and the refusal errors never echo the (possibly
        secret-bearing) id. Unlike :meth:`register_source`, the same-hash case
        is the deliberate idempotent path: a retry of a managed ingest with
        identical bytes is always safe (registration is independent of
        publication, so a discarded proposal leaves an active-but-unsupported
        source that a retry reuses).
        """
        _validate_source_id(source_id)
        new_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing = next(
            (source for source in self._state.sources if source.source_id == source_id),
            None,
        )
        if existing is None:
            version = _source_version(
                source_id, raw_bytes, filename=filename, content_type=content_type
            )
            sources = list(self._state.sources)
            sources.append(
                KnowledgeSource(source_id=source_id, status="active", versions=[version])
            )
            self._commit(msgspec.structs.replace(self._state, sources=sources))
            return version, "registered"
        if existing.status != "active":
            raise SourceRegistryError(
                "Knowledge Source is retired; reactivate it to record a new Source Version"
            )
        current = existing.versions[-1]
        if current.content_hash == new_hash:
            return current, "reused"
        raise SourceRegistryError(
            "Knowledge Source is active with different bytes; replacement is "
            "not available through managed ingest — retire and reactivate to "
            "add a reviewed new version"
        )

    def record_artifact_binding(self, source_id: str, content_hash: str) -> None:
        """Mark one exact Source Version as having a verified artifact (#164).

        The second half of the idempotent ingest saga (ADR-0020): the bytes
        were uploaded to the Source Artifact Store and verified there, and now
        the registry records availability for exactly ``(source_id,
        content_hash)``. Idempotent — recording an already-available binding is
        a no-op — and it raises (never fabricates) when the identity or hash is
        unknown, so a failed upload can never be reported as retained.
        """
        source = self.get(source_id)
        versions = list(source.versions)
        for index, version in enumerate(versions):
            if version.content_hash != content_hash:
                continue
            if not version.artifact_available:
                versions[index] = msgspec.structs.replace(version, artifact_available=True)
                sources = list(self._state.sources)
                source_index = next(
                    i for i, item in enumerate(sources) if item.source_id == source_id
                )
                sources[source_index] = msgspec.structs.replace(
                    sources[source_index], versions=versions
                )
                self._commit(msgspec.structs.replace(self._state, sources=sources))
            return
        raise SourceRegistryError(
            f"Knowledge Source {source_id!r} has no Source Version {content_hash!r}"
        )

    def clear_artifact_binding(self, source_id: str, content_hash: str) -> None:
        """Mark a version's artifact unavailable after explicit deletion (#164).

        Only the explicit deletion path calls this (retirement preserves
        historical artifacts); the binding is cleared so inspection metadata
        never claims an artifact the store no longer holds.
        """
        source = self.get(source_id)
        versions = list(source.versions)
        for index, version in enumerate(versions):
            if version.content_hash != content_hash:
                continue
            if version.artifact_available:
                versions[index] = msgspec.structs.replace(version, artifact_available=False)
                sources = list(self._state.sources)
                source_index = next(
                    i for i, item in enumerate(sources) if item.source_id == source_id
                )
                sources[source_index] = msgspec.structs.replace(
                    sources[source_index], versions=versions
                )
                self._commit(msgspec.structs.replace(self._state, sources=sources))
            return
        raise SourceRegistryError(
            f"Knowledge Source {source_id!r} has no Source Version {content_hash!r}"
        )

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
        self._commit(
            msgspec.structs.replace(
                self._state, pending_transitions=[*self._state.pending_transitions, bound]
            )
        )

    def cancel_transition(self, proposal_id: str) -> None:
        """Remove a pending transition when its proposal is discarded."""
        pending = [
            transition
            for transition in self._state.pending_transitions
            if transition.proposal_id != proposal_id
        ]
        if len(pending) == len(self._state.pending_transitions):
            raise SourceRegistryError(f"proposal {proposal_id!r} has no pending source transition")
        self._commit(msgspec.structs.replace(self._state, pending_transitions=pending))

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
        self._commit(
            msgspec.structs.replace(self._state, sources=sources, pending_transitions=pending)
        )

    def record_retirement_candidate(self, source_id: str, trigger: str) -> RetirementCandidate:
        """Record a missing-source signal without changing source support."""
        source = self.get(source_id)
        if source.status != "active":
            raise SourceRegistryError(
                f"Knowledge Source {source_id!r} must be active for retirement review"
            )
        _validate_retirement_trigger(trigger)
        candidate = RetirementCandidate(
            id=uuid.uuid4().hex,
            source_id=source_id,
            trigger=trigger,
            status="pending",
            created_at=_now(),
        )
        self._commit(
            msgspec.structs.replace(self._state, candidates=[*self._state.candidates, candidate])
        )
        return candidate

    def get_candidate(self, candidate_id: str) -> RetirementCandidate:
        # #133 final review: validate the candidate id BEFORE lookup so a
        # secret-bearing value is rejected generically (never interpolated into
        # the unknown-candidate error). A validated-but-unknown id is a safe
        # UUID hex and may be echoed; an invalid id never reaches the lookup.
        _validate_candidate_id(candidate_id)
        for candidate in self._state.candidates:
            if candidate.id == candidate_id:
                return candidate
        raise SourceRegistryError(f"unknown retirement candidate {candidate_id!r}")

    def list_candidates(self) -> list[RetirementCandidate]:
        """Return all recorded retirement candidates (read query).

        Mirrors :meth:`list`: presentation (which candidates to render) is the
        caller's concern; this returns every recorded candidate so the
        reviewing caller can filter pending vs. decided without reaching past
        the registry's public seam.
        """
        return list(self._state.candidates)

    def _decide_candidate(
        self, candidate_id: str, status: RetirementCandidateStatus
    ) -> RetirementCandidate:
        # #133 final review: validate the candidate id BEFORE lookup or
        # interpolation so a secret-bearing value is rejected generically and
        # never echoed into the pending/unknown error (mirrors get_candidate).
        _validate_candidate_id(candidate_id)
        candidates = list(self._state.candidates)
        for index, candidate in enumerate(candidates):
            if candidate.id != candidate_id:
                continue
            if candidate.status != "pending":
                raise SourceRegistryError(f"retirement candidate {candidate_id!r} is not pending")
            decided = msgspec.structs.replace(candidate, status=status)
            candidates[index] = decided
            self._commit(msgspec.structs.replace(self._state, candidates=candidates))
            return decided
        raise SourceRegistryError(f"unknown retirement candidate {candidate_id!r}")

    def dismiss_retirement_candidate(self, candidate_id: str) -> RetirementCandidate:
        return self._decide_candidate(candidate_id, "dismissed")

    def confirm_retirement_candidate(self, candidate_id: str) -> RetirementCandidate:
        return self._decide_candidate(candidate_id, "confirmed")
