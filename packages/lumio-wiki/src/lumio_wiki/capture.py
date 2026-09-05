"""Explicit capture of a coding-agent session or research result (issue #179).

Capture is the opt-in staging seam that turns the CURRENT session (or a
research result) into ONE reviewable Ingest Proposal through the same
managed host-Distiller contract as ordinary ingest (issue #149): an
agent-authored Compiled Page plus a bounded capture manifest, bound to the
original transcript/export bytes under an explicit source identity.

Contract highlights (issue #179 rules):

- Explicit invocation only. Nothing here runs automatically; capture never
  changes normal agent operation.
- Preview first. :func:`capture_session` with ``confirmed=False`` registers
  nothing and stages nothing; it returns the preview (included sections and
  redaction counts) for explicit confirmation. Registration and staging
  happen only on the confirmed call.
- Redaction safety net. Secrets, credentials, signed URLs, private object
  keys, environment dumps, and hidden model reasoning never enter the staged
  Compiled Page, the manifest record, or CLI output: they are replaced with
  ``[REDACTED]`` (or stripped, for reasoning blocks) before anything is
  staged, and transcript digests are computed over redacted bytes.
- Declarative knowledge only. A page that looks like a raw conversational
  transcript is refused with an actionable message; capture targets
  decisions, verified findings, commands/results, and citations.
- Never auto-publish. Capture stages a proposal; review and publish stay
  explicit (``proposal inspect`` / ``validate`` / ``publish``).
- Vendor-neutral core. The manifest contract is client-agnostic; thin
  opt-in adapters (Pi, Codex, Claude Code, Hermes) export a transcript plus
  a manifest through the same seam, and unsupported clients write a manual
  manifest. A missing or unreadable transcript is reported, never
  fabricated: without transcript bytes the manifest itself is the private
  capture record registered under the source identity.

The manifest is private ingest provenance: it is persisted under the private
ingest store (``captures/``), never inside the Knowledge Base root, and only
reviewed Compiled Page knowledge is published.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import msgspec  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from lumio_wiki.ingest import IngestProposal

# Boundaries keeping the capture manifest small and reviewable.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACTS = 32
MAX_REDACTION_LABELS = 32
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024

REDACTED = "[REDACTED]"

# Clients with a thin opt-in export adapter convention (issue #179). Any other
# non-empty lowercase label is accepted — unsupported clients capture through
# a manual manifest over the same contract.
KNOWN_CLIENTS = ("pi", "codex", "claude-code", "hermes")

_CLIENT_LABEL = re.compile(r"^[a-z][a-z0-9-]*$")

# Secret/credential patterns replaced with REDACTED. Each entry is
# (category, pattern); the private-key block is matched first and whole-block.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern))
    for category, pattern in (
        # Private key blocks (PEM), including OpenSSH/EC/RSA variants. DOTALL:
        # the base64 body spans multiple lines.
        (
            "private-key",
            r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----"
            r".*?-----END [A-Z ]*PRIVATE KEY( BLOCK)?-----",
        ),
        # Provider API keys: OpenAI-style sk-..., Anthropic sk-ant-...,
        # GitHub ghp_/gho_, Slack xox?, AWS access keys.
        ("api-key", r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
        ("api-key", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        ("api-key", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        ("api-key", r"\bAKIA[0-9A-Z]{16}\b"),
        # Bearer authorization headers.
        ("bearer-token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}"),
        # Signed / pre-authenticated URLs (S3, GCS, Azure SAS, generic sig).
        (
            "signed-url",
            r"(?i)\bhttps?://\S*[?&](?:X-Amz-Signature|X-Goog-Signature|SharedAccessSignature|sig|signature|token)=\S+",
        ),
        # Private object-store keys / artifact object URIs.
        ("object-key", r"\bs3://\S+"),
        # Credential-shaped assignments (password=, api_key:, "token": ...).
        (
            "credential",
            r"(?i)\b(?:api[_-]?key|passwd|password|secret|token|access[_-]?key)\b\s*[:=]\s*[\"']?[A-Za-z0-9._/~+=-]{8,}",
        ),
        # Environment dumps: whole SECRET/TOKEN/KEY/PASSWORD-valued lines.
        (
            "environment",
            r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]*"
            r"(?:SECRET|TOKEN|KEY|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]*=[^\s]+$",
        ),
    )
)

# Hidden model reasoning / chain-of-thought blocks, stripped entirely (never
# published, never quoted). Unclosed opening tags strip to end-of-text: a
# truncated reasoning block must not leak its remainder.
_REASONING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.DOTALL | re.IGNORECASE))
    for category, pattern in (
        ("hidden-reasoning", r"<think>.*?</think>"),
        ("hidden-reasoning", r"<reasoning>.*?</reasoning>"),
        ("hidden-reasoning", r"<chain-of-thought>.*?</chain-of-thought>"),
        ("hidden-reasoning", r"<think>.*"),
        ("hidden-reasoning", r"<reasoning>.*"),
    )
)

# Conversational turn markers. A page dominated by chat turns is a raw
# transcript, not declarative knowledge; capture refuses it (issue #179:
# capture decisions/findings/commands/citations, not transcripts).
_TRANSCRIPT_TURN = re.compile(
    r"(?im)^[ \t]*(?:<\|(?:im_start|im_end)\|>"
    r"|user|human|assistant|system|claude|codex|gemini|gpt)\s*[:|>]"
)
_RAW_TRANSCRIPT_THRESHOLD = 2

_DIGEST_REFERENCE = re.compile(r"^sha256:([0-9a-f]{64})$")

_HEADING = re.compile(r"(?m)^#{1,3}[ \t]+(.+?)[ \t]*$")

_TRANSCRIPT_CONTENT_TYPES = {
    ".jsonl": "application/json",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class CaptureError(ValueError):
    """A capture could not be previewed or staged.

    Raised for invalid manifests, pages that would publish secrets after
    redaction, raw-transcript pages, missing named transcripts, digest
    mismatches, or manifest-bound manifests. Nothing is registered or staged
    when this is raised.
    """


class CaptureManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Bounded, vendor-neutral capture provenance (issue #179).

    ``client`` names the producing agent (a ``KNOWN_CLIENTS`` label or any
    lowercase label for a manual capture). ``transcript`` is either a path
    (resolved relative to the manifest) or a ``sha256:<hex>`` digest
    reference for an export that is not locally available. ``artifacts``
    names included artifacts; ``redactions`` labels the exclusions the
    distiller already applied. The manifest is private ingest provenance —
    it never publishes; only the reviewed Compiled Page does.
    """

    client: str
    project: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    transcript: str | None = None
    artifacts: list[str] = msgspec.field(default_factory=list)
    redactions: list[str] = msgspec.field(default_factory=list)


class CapturePreview(msgspec.Struct, frozen=True):
    """What confirmation shows: included sections and redaction counts."""

    client: str
    project: str | None
    source_id: str
    sections: list[str]
    redaction_counts: dict[str, int]
    declared_redactions: list[str]
    artifacts: list[str]
    transcript_bound: bool
    transcript_digest: str | None
    warnings: list[str]


class CaptureOutcome(msgspec.Struct, frozen=True):
    """Preview-mode result; ``proposal`` is set only on a confirmed capture."""

    preview: CapturePreview
    proposal: IngestProposal | None = None


def load_capture_manifest(path: str | Path) -> tuple[CaptureManifest, bytes]:
    """Load and validate a bounded capture manifest.

    Returns the manifest and its exact file bytes (the fallback private
    capture record when no transcript is bound). Raises
    :class:`CaptureError` for unreadable, oversized, malformed, or invalid
    manifests — including unknown fields, which keep the contract bounded.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CaptureError(f"capture manifest not readable: {path} ({exc})") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CaptureError(
            f"capture manifest exceeds {MAX_MANIFEST_BYTES} bytes: {path}"
        )
    try:
        manifest = msgspec.yaml.decode(raw, type=CaptureManifest)
    except (msgspec.DecodeError, msgspec.ValidationError, TypeError) as exc:
        raise CaptureError(f"invalid capture manifest {path}: {exc}") from exc
    _validate_manifest(manifest)
    return manifest, raw


def _validate_manifest(manifest: CaptureManifest) -> None:
    if not _CLIENT_LABEL.match(manifest.client):
        raise CaptureError(
            f"capture manifest client must be a lowercase label "
            f"(e.g. {', '.join(KNOWN_CLIENTS)}, or a manual label): "
            f"got {manifest.client!r}"
        )
    for field_name, value in (("project", manifest.project),):
        if value is not None and not value.strip():
            raise CaptureError(f"capture manifest {field_name} must be non-empty")
    if len(manifest.artifacts) > MAX_ARTIFACTS:
        raise CaptureError(
            f"capture manifest lists more than {MAX_ARTIFACTS} artifacts"
        )
    if len(manifest.redactions) > MAX_REDACTION_LABELS:
        raise CaptureError(
            f"capture manifest lists more than {MAX_REDACTION_LABELS} redaction labels"
        )
    if manifest.started_at is not None:
        _parse_timestamp(manifest.started_at, "started_at")
    if manifest.ended_at is not None:
        _parse_timestamp(manifest.ended_at, "ended_at")
    if (
        manifest.started_at is not None
        and manifest.ended_at is not None
        and _parse_timestamp(manifest.started_at, "started_at")
        > _parse_timestamp(manifest.ended_at, "ended_at")
    ):
        raise CaptureError("capture manifest started_at is after ended_at")


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureError(
            f"capture manifest {field_name} is not an ISO-8601 timestamp: {value!r}"
        ) from exc


def redact_capture_text(text: str) -> tuple[str, dict[str, int]]:
    """Apply the capture redaction safety net to ``text``.

    Reasoning blocks are stripped entirely; every secret-shaped match
    (credentials, signed URLs, private object keys, environment dumps) is
    replaced with ``[REDACTED]``. Returns the redacted text and per-category
    counts (categories with zero matches are omitted).
    """
    counts: dict[str, int] = {}
    redacted = text
    for category, pattern in _REASONING_PATTERNS:
        redacted, n = pattern.subn("", redacted)
        if n:
            counts[category] = counts.get(category, 0) + n
    for category, pattern in _CREDENTIAL_PATTERNS:
        redacted, n = pattern.subn(REDACTED, redacted)
        if n:
            counts[category] = counts.get(category, 0) + n
    return redacted, counts


def detect_raw_transcript(markdown: str) -> list[str]:
    """Return the conversational turn markers found in ``markdown``.

    Three or more chat-turn lines (``user:``, ``assistant:``, ``<|im_start|>``,
    ...) mean the page is a raw transcript, not declarative knowledge. Callers
    refuse to stage such pages (issue #179: capture decisions, verified
    findings, commands/results, and citations — not transcripts).
    """
    return _TRANSCRIPT_TURN.findall(markdown)


def build_capture_preview(
    *,
    client: str,
    project: str | None,
    source_id: str,
    redacted_markdown: str,
    redaction_counts: dict[str, int],
    manifest: CaptureManifest,
    transcript_digest: str | None,
    warnings: list[str],
) -> CapturePreview:
    """Assemble the confirmation preview over the REDACTED page."""
    sections = _HEADING.findall(redacted_markdown)
    return CapturePreview(
        client=client,
        project=project,
        source_id=source_id,
        sections=sections,
        redaction_counts=redaction_counts,
        declared_redactions=list(manifest.redactions),
        artifacts=list(manifest.artifacts),
        transcript_bound=manifest.transcript is not None,
        transcript_digest=transcript_digest,
        warnings=warnings,
    )


def _resolve_transcript_bytes(
    manifest: CaptureManifest,
    manifest_path: Path,
    manifest_bytes: bytes,
    transcript_path: Path | None,
) -> tuple[bytes, str | None, str, str | None, list[str]]:
    """Resolve the original capture source bytes.

    Returns ``(raw_bytes, digest, filename, content_type, warnings)``. In
    order: an explicit ``--transcript`` path; the manifest's transcript path;
    a digest-only reference (the export is not local — the manifest itself is
    the private capture record, and the limitation is reported, not
    fabricated). A named transcript that cannot be read is an error.
    """
    warnings: list[str] = []
    explicit = transcript_path
    named = manifest.transcript

    if explicit is not None:
        if not explicit.is_file():
            raise CaptureError(
                f"capture transcript not found: {explicit} — refusing to stage "
                "without the named transcript (report the limitation, do not "
                "fabricate continuity)"
            )
        try:
            raw = explicit.read_bytes()
        except OSError as exc:
            raise CaptureError(f"capture transcript not readable: {explicit} ({exc})") from exc
        return _checked_transcript(raw, explicit.name, named)

    if named is not None and not _DIGEST_REFERENCE.match(named):
        path = (
            Path(named)
            if Path(named).is_absolute()
            else manifest_path.parent / named
        )
        if not path.is_file():
            raise CaptureError(
                f"capture transcript not found at manifest location: {path} — "
                "fix the transcript path or clear it to capture with the "
                "manifest as the private record"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CaptureError(f"capture transcript not readable: {path} ({exc})") from exc
        return _checked_transcript(raw, path.name, None)

    if named is not None:
        # Digest-only reference: the export is not locally available. Register
        # the manifest bytes as the capture record and say so — never
        # fabricate the transcript.
        warnings.append(
            f"transcript referenced by digest only ({named}); the manifest is "
            "registered as the private capture record"
        )
    else:
        warnings.append(
            "no transcript bound; the manifest is registered as the private "
            "capture record"
        )
    return manifest_bytes, None, manifest_path.name, "application/yaml", warnings


def _checked_transcript(
    raw: bytes, filename: str, declared: str | None
) -> tuple[bytes, str | None, str, str | None, list[str]]:
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise CaptureError(
            f"capture transcript exceeds {MAX_TRANSCRIPT_BYTES} bytes: {filename}"
        )
    redacted, transcript_counts = redact_capture_text(raw.decode("utf-8", errors="replace"))
    redacted_bytes = redacted.encode("utf-8")
    digest = hashlib.sha256(redacted_bytes).hexdigest()
    if declared is not None:
        match = _DIGEST_REFERENCE.match(declared)
        if match is not None and match.group(1) != digest:
            raise CaptureError(
                "capture transcript bytes do not match the digest declared in "
                f"the manifest (declared sha256:{match.group(1)[:12]}…, "
                f"computed sha256:{digest[:12]}…) — refusing to stage a "
                "mismatched transcript"
            )
    warnings = [
        f"transcript redactions applied: {count} {category}"
        for category, count in sorted(transcript_counts.items())
    ]
    content_type = _TRANSCRIPT_CONTENT_TYPES.get(Path(filename).suffix.lower())
    return redacted_bytes, digest, filename, content_type, warnings


def _write_capture_record(
    store, source_id: str, manifest: CaptureManifest, transcript_digest: str | None
) -> None:
    """Persist the manifest as private ingest provenance (never in the KB)."""
    captures = Path(store.root) / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    record = {
        "source_id": source_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": msgspec.to_builtins(manifest),
        "transcript_digest": transcript_digest,
    }
    (captures / f"{source_id}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )


def _redact_manifest(
    manifest: CaptureManifest,
) -> tuple[CaptureManifest, dict[str, int]]:
    """Apply the redaction safety net to manifest field VALUES (issue #179).

    Secrets never enter manifests, logs, or output: free-text manifest
    values (project, transcript label, artifact names, redaction labels)
    are redacted before they are persisted to the private capture record or
    rendered in the preview. Structurally validated fields (client label,
    ISO timestamps) need no redaction — they cannot carry secret shapes.
    """

    def red(value: str | None) -> str | None:
        if value is None:
            return None
        return redact_capture_text(value)[0]

    def red_str(value: str) -> str:
        return redact_capture_text(value)[0]

    counts: dict[str, int] = {}
    for value in (manifest.project, manifest.transcript, *manifest.artifacts, *manifest.redactions):
        _merged_counts(counts, redact_capture_text(value or "")[1])
    safe = CaptureManifest(
        client=manifest.client,
        project=red(manifest.project),
        started_at=manifest.started_at,
        ended_at=manifest.ended_at,
        transcript=red(manifest.transcript),
        artifacts=[red_str(a) for a in manifest.artifacts],
        redactions=[red_str(r) for r in manifest.redactions],
    )
    return safe, counts


def _merged_counts(target: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    for category, count in extra.items():
        target[category] = target.get(category, 0) + count
    return target


def capture_session(
    pipeline,
    store,
    *,
    compiled_page_markdown: str,
    manifest: CaptureManifest,
    manifest_bytes: bytes,
    manifest_path: str | Path,
    source_id: str,
    transcript_path: str | Path | None = None,
    confirmed: bool = False,
) -> CaptureOutcome:
    """Preview (and on confirmation stage) ONE capture proposal (issue #179).

    With ``confirmed=False`` nothing is registered and nothing is staged:
    the returned outcome carries only the preview (included sections and
    redaction counts) for explicit confirmation. With ``confirmed=True`` the
    REDACTED capture source bytes (transcript, or the manifest itself when
    no transcript is bound) are registered under ``source_id`` and the
    redacted Compiled Page is staged as a single reviewable proposal through
    the managed host-Distiller contract. Never publishes.
    """
    from lumio_wiki.ingest import _authored_page_declares_source

    manifest_path = Path(manifest_path)
    if transcript_path is not None:
        transcript_path = Path(transcript_path)

    # 1. Redaction safety net over the page and the manifest field values
    #    BEFORE anything is registered or staged: secrets and hidden
    #    reasoning never reach the proposal, the capture record, or output.
    #    ``safe_manifest`` is what gets persisted and printed; the original
    #    manifest is used ONLY to resolve the transcript path (a path is
    #    resolved from where the file actually is, then redacted in display).
    redacted_markdown, redaction_counts = redact_capture_text(compiled_page_markdown)
    safe_manifest, manifest_counts = _redact_manifest(manifest)
    _merged_counts(redaction_counts, manifest_counts)

    # 2. Raw transcripts are refused: capture is declarative knowledge.
    turns = detect_raw_transcript(redacted_markdown)
    if len(turns) >= _RAW_TRANSCRIPT_THRESHOLD:
        raise CaptureError(
            f"authored page looks like a raw conversational transcript "
            f"({len(turns)} chat-turn lines) — capture decisions, verified "
            "findings, commands/results, and citations instead of a "
            "transcript (issue #179)"
        )

    # 3. Resolve the capture source bytes (transcript, or the manifest
    #    itself as the private record). Reports limitations; never fabricates
    #    a transcript. The returned bytes are REDACTED transcript bytes; the
    #    registry hash and provenance cover exactly what is registered.
    capture_source_bytes, transcript_digest, filename, content_type, transcript_warnings = (
        _resolve_transcript_bytes(
            manifest, manifest_path, manifest_bytes, transcript_path
        )
    )

    # 4. The authored page must declare the source identity — checked BEFORE
    #    registration so an inconsistent capture leaves no registry state.
    try:
        _authored_page_declares_source(redacted_markdown, source_id)
    except ValueError as exc:  # ManagedIngestError: one capture contract error
        raise CaptureError(str(exc)) from exc

    # 5. Preview (over the redacted manifest). Without confirmation this is
    #    the end of the flow: no registration, no staging, no capture record.
    preview = build_capture_preview(
        client=safe_manifest.client,
        project=safe_manifest.project,
        source_id=source_id,
        redacted_markdown=redacted_markdown,
        redaction_counts=redaction_counts,
        manifest=safe_manifest,
        transcript_digest=transcript_digest,
        warnings=transcript_warnings,
    )
    if not confirmed:
        return CaptureOutcome(preview=preview, proposal=None)

    # 6. Confirmed: register + stage through the ONE managed contract.
    proposal = pipeline.managed_ingest(
        capture_source_bytes,
        content_type,
        filename,
        source_id,
        redacted_markdown,
    )
    _write_capture_record(store, source_id, safe_manifest, transcript_digest)
    return CaptureOutcome(preview=preview, proposal=proposal)


def format_capture_preview(preview: CapturePreview) -> str:
    """Render the confirmation preview as CLI text (secret-free by design)."""
    lines = [
        f"client:           {preview.client}",
        f"project:          {preview.project or '(none)'}",
        f"source_id:        {preview.source_id}",
        f"transcript:       {'bound' if preview.transcript_bound else 'not bound'}"
        + (
            f" (sha256:{preview.transcript_digest[:12]}…)"
            if preview.transcript_digest
            else ""
        ),
        f"artifacts:        {', '.join(preview.artifacts) or '(none)'}",
        f"declared redactions: {len(preview.declared_redactions)}"
        + (
            f" ({', '.join(preview.declared_redactions)})"
            if preview.declared_redactions
            else ""
        ),
        "sections:",
    ]
    lines += [f"  - {section}" for section in preview.sections] or ["  (none)"]
    if preview.redaction_counts:
        total = sum(preview.redaction_counts.values())
        detail = ", ".join(
            f"{count} {category}"
            for category, count in sorted(preview.redaction_counts.items())
        )
        lines.append(f"redactions applied: {total} ({detail})")
    else:
        lines.append("redactions applied: 0")
    for warning in preview.warnings:
        lines.append(f"note: {warning}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Read-only client session-history adapters (Codex, Pi).
#
# Discovery/export are READ-ONLY: they never register, stage, publish, or
# print transcript content. Session IDs are stable (sha256-16 of client +
# absolute transcript path), ordering is bounded and deterministic. Errors
# name client and session id only — never path fragments from other
# projects' history (secret-safe). Synthetic fixtures only in tests.
# --------------------------------------------------------------------------

ADAPTER_CLIENTS = ("codex", "pi")

#: Default client history roots (relative to the user home), matching each
#: client's documented session layout.
_DEFAULT_ROOTS = {
    "codex": ".codex/sessions",
    "pi": ".pi/agent/sessions",
}

#: Discovery is bounded: at most this many session files are inspected per
#: call so a huge history cannot make discovery unbounded.
MAX_DISCOVERY_FILES = 5000


class ClientSession(msgspec.Struct, frozen=True):
    """One discovered client session (metadata only — never message text).

    ``id`` is the stable adapter session id (sha256-16 of client + absolute
    transcript path) — NOT the client's native header id, which is not
    unique (Codex resume writes multiple rollout files sharing one
    conversation id). ``native_id`` keeps the header id for display.
    """

    client: str
    id: str
    path: str
    project: str | None = None
    started_at: str | None = None
    native_id: str | None = None


class SessionExport(msgspec.Struct, frozen=True):
    """Result of a read-only export: original bytes plus the Capture Manifest."""

    session: ClientSession
    transcript_path: str
    transcript_digest: str
    manifest: CaptureManifest
    manifest_path: str
    warnings: list[str] = msgspec.field(default_factory=list)


def _session_id(client: str, path: Path) -> str:
    """Stable session id: client + absolute transcript path, sha256-16."""
    digest = hashlib.sha256(f"{client}|{path.resolve()}".encode()).hexdigest()
    return digest[:16]


def _jsonl_headers(path: Path, limit: int) -> list[dict]:
    """Parse at most ``limit`` JSONL lines of ``path``; skip bad lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            headers: list[dict] = []
            for _line in handle:
                if len(headers) >= limit:
                    break
                try:
                    decoded = json.loads(_line)
                except ValueError:
                    continue
                if isinstance(decoded, dict):
                    headers.append(decoded)
            return headers
    except OSError as exc:
        raise CaptureError(f"client session file not readable: {path.name}") from exc


def _discovered(
    client: str,
    root: Path,
    files: list[Path],
    project: str | None,
    since: str | None,
    limit: int | None,
) -> list[ClientSession]:
    """Shared bounded, deterministic discovery tail for all adapters."""
    if since is not None:
        since_dt = _parse_timestamp(since, "since")
    else:
        since_dt = None
    if limit is not None and limit <= 0:
        return []
    sessions: list[ClientSession] = []
    for path in sorted(files, key=lambda p: p.name):
        info = _read_session_info(client, path)
        if info is None:
            continue
        if project is not None and info.project != project:
            continue
        if (
            since_dt is not None
            and (
                info.started_at is None
                or _parse_timestamp(info.started_at, "started_at") < since_dt
            )
        ):
            continue
        sessions.append(info)
        if limit is not None and len(sessions) >= limit:
            break
    return sessions


def _read_session_info(client: str, path: Path) -> ClientSession | None:
    """Read one session file's header metadata; None when unusable."""
    if client == "pi":
        header = next(
            (
                line
                for line in _jsonl_headers(path, 4)
                if line.get("type") == "session" and line.get("id")
            ),
            None,
        )
        if header is None:
            return None
    else:  # codex
        meta = next(
            (
                line
                for line in _jsonl_headers(path, 8)
                if line.get("type") == "session_meta"
            ),
            None,
        )
        payload = meta.get("payload") if isinstance(meta, dict) else None
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        header = payload
    project = header.get("cwd")
    started_at = None
    if isinstance(header.get("timestamp"), str) and header["timestamp"]:
        try:
            # Malformed header timestamps skip the file (with --since a hard
            # error would abort the whole listing; metadata-only discovery
            # stays skip-tolerant like the other unusable-file cases).
            started_at = str(header["timestamp"])
            _parse_timestamp(started_at, "session header timestamp")
        except CaptureError:
            return None
    return ClientSession(
        client=client,
        id=_session_id(client, path),
        native_id=str(header["id"]),
        path=str(path),
        project=str(project) if isinstance(project, str) and project.strip() else None,
        started_at=started_at,
    )


def discover_sessions(
    client: str,
    *,
    root: str | Path | None = None,
    project: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[ClientSession]:
    """Discover sessions for ONE client under its history root (read-only).

    ``root`` overrides the client's default history directory (defaults:
    Codex ``~/.codex/sessions``, Pi ``~/.pi/agent/sessions``). Returns
    sessions sorted by filename (chronological for both clients), bounded by
    ``limit`` (0 returns nothing) and ``MAX_DISCOVERY_FILES`` inspected
    files. Filtering is by header metadata only — message content is never
    read here.
    """
    if client not in ADAPTER_CLIENTS:
        raise CaptureError(
            f"unsupported capture client {client!r} — adapters exist for: "
            f"{', '.join(ADAPTER_CLIENTS)}"
        )
    root_path = Path(root) if root is not None else Path.home() / _DEFAULT_ROOTS[client]
    files = [
        path
        for path in sorted(root_path.rglob("*.jsonl"), key=lambda p: str(p))
        if path.is_file()
    ][:MAX_DISCOVERY_FILES]
    return _discovered(client, root_path, files, project, since, limit)


def export_session(
    client: str,
    session_id: str,
    output_dir: str | Path,
    *,
    root: str | Path | None = None,
) -> SessionExport:
    """Export ONE discovered session read-only (issue #179 contract).

    Copies the ORIGINAL transcript bytes unchanged to
    ``<output_dir>/transcript.jsonl`` and writes the existing bounded
    ``CaptureManifest`` as ``<output_dir>/capture.yaml`` referencing the
    sibling transcript file by name (``transcript: transcript.jsonl``). No
    digest is declared here: ``capture session`` redacts transcript bytes
    before hashing, so a digest over the ORIGINAL bytes would make staging
    fail whenever redaction fires — and agent transcripts are exactly where
    secrets live. The digest of the original bytes is returned on
    :class:`SessionExport` for callers that want it. Registers nothing,
    stages nothing, publishes nothing, and never prints transcript content.
    """
    if client not in ADAPTER_CLIENTS:
        raise CaptureError(
            f"unsupported capture client {client!r} — adapters exist for: "
            f"{', '.join(ADAPTER_CLIENTS)}"
        )
    root_path = Path(root) if root is not None else Path.home() / _DEFAULT_ROOTS[client]
    match = next(
        (s for s in discover_sessions(client, root=root_path) if s.id == session_id),
        None,
    )
    if match is None:
        raise CaptureError(f"unknown {client} session id: {session_id}")
    source = Path(match.path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise CaptureError(f"client session file not readable: {source.name}") from exc
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise CaptureError(
            f"client session exceeds {MAX_TRANSCRIPT_BYTES} bytes: {source.name}"
        )
    digest = hashlib.sha256(raw).hexdigest()

    out = Path(output_dir)
    if out.exists() and not out.is_dir():
        raise CaptureError(
            f"output path is a file, not a directory: {out.name} — "
            "choose an output directory path"
        )
    if out.exists() and any(out.iterdir()):
        raise CaptureError(
            f"output directory not empty: {out.name} — choose an empty directory"
        )
    out.mkdir(parents=True, exist_ok=True)
    transcript_path = out / "transcript.jsonl"
    transcript_path.write_bytes(raw)

    manifest = CaptureManifest(
        client=client,
        project=match.project,
        started_at=match.started_at,
        # Sibling file reference, not a declared digest: staging redacts the
        # transcript before hashing, so a digest over the ORIGINAL bytes
        # would make `capture session` refuse the very composition the
        # skill teaches whenever redaction fires.
        transcript="transcript.jsonl",
        artifacts=["transcript.jsonl", "capture.yaml"],
    )
    manifest_bytes = msgspec.yaml.encode(manifest)
    manifest_path = out / "capture.yaml"
    manifest_path.write_bytes(manifest_bytes)
    warnings = [
        "original transcript bytes exported unredacted to a local directory; "
        "redaction happens only at `capture session` staging time"
    ]
    return SessionExport(
        session=match,
        transcript_path=str(transcript_path),
        transcript_digest=digest,
        manifest=manifest,
        manifest_path=str(manifest_path),
        warnings=warnings,
    )


def format_sessions(sessions: list[ClientSession]) -> str:
    """Render discovered sessions as stable text (metadata only, secret-free)."""
    lines = [f"{len(sessions)} session(s):"] if sessions else ["0 session(s):"]
    for s in sessions:
        lines.append(
            f"  - id: {s.id}  client: {s.client}  "
            f"native id: {s.native_id or '(unknown)'}  "
            f"started: {s.started_at or '(unknown)'}  project: {s.project or '(none)'}"
        )
    return "\n".join(lines)


def format_session_export(result: SessionExport) -> str:
    """Render an export result (paths + manifest facts; never transcript text)."""
    lines = [
        f"session:          {result.session.id}",
        f"client:           {result.session.client}",
        f"project:          {result.session.project or '(none)'}",
        f"transcript:       {result.transcript_path}",
        f"  sha256:         {result.transcript_digest[:12]}…",
        f"manifest:         {result.manifest_path}",
        f"  transcript ref: {result.manifest.transcript}",
        "Next: author a Compiled Page over the exported material, then run "
        "`lumio-wiki capture session` (preview first, --yes to stage).",
    ]
    for warning in result.warnings:
        lines.append(f"note: {warning}")
    return "\n".join(lines)
