"""Explicit capture of coding-agent sessions and research results (issue #179).

Covers the acceptance criteria through the same in-process surface a client
adapter uses:

- AC1  one reviewable proposal per capture (managed contract, single staged
  proposal, declarative page bound to the transcript bytes);
- AC2  preview before registration/staging (unconfirmed capture registers
  nothing, stages nothing);
- AC3  published content is declarative knowledge (raw transcripts refused,
  hidden reasoning stripped);
- AC4  secret fixtures are redacted and never reach the page, the manifest
  record, or output;
- AC5  one vendor-neutral manifest contract across client fixtures (pi,
  codex, claude-code, hermes, manual);
- AC6  capture is explicit and side-effect-free on normal ingest (flag
  parity, ordinary ingest unchanged).

Also covers the rules: adapters report missing transcripts instead of
fabricating continuity, digest mismatches refuse staging, and capture never
auto-publishes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.capture import (
    CaptureError,
    CaptureManifest,
    capture_session,
    detect_raw_transcript,
    format_capture_preview,
    load_capture_manifest,
    redact_capture_text,
)
from lumio_wiki.ingest import IngestStore
from lumio_wiki.proposal_pipeline import ProposalPipeline

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
CAPTURE_FIXTURES = FIXTURES / "capture"
CLIENT_MANIFESTS = ["pi", "codex", "claude-code", "hermes", "manual"]

SECRET_FIXTURES = [
    # (label, secret-bearing text, category asserted in redaction_counts)
    ("api-key", "Used key sk-ant-0123456789abcdef0123 in the run.", "api-key"),
    (
        "signed-url",
        "Fetched https://s3.example/bucket/a?X-Amz-Signature=abc123def456ghi789 during research.",
        "signed-url",
    ),
    (
        "env-dump",
        "export LUMIO_PROVIDER_API_KEY=super-secret-value-123\nkept working",
        "environment",
    ),
    (
        "credential-assignment",
        'config had api_key: "hunter2000secret" inside',
        "credential",
    ),
]


def _kb(tmp_path: Path):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def _pipeline(kb, tmp_path: Path) -> tuple[ProposalPipeline, IngestStore]:
    store = IngestStore(tmp_path / "ingest")
    return ProposalPipeline(kb, store=store), store


def _page(source_id: str, *, title: str = "Session Findings", body: str = "") -> str:
    body = body or (
        "# Decisions\n\n- Reader returns `not covered` when Evidence is "
        "insufficient (verified against the guardrail).\n\n"
        "# Commands\n\n- `uv run pytest -q tests/test_capture.py` passed "
        "with 12 focused tests.\n"
    )
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        'tags:\n  - "session-notes"\n'
        'summary: "Verified findings from a captured coding-agent session."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} capture"\n'
        "synthetic: false\n"
        "---\n\n" + body
    )


def _manifest_bytes(tmp_path: Path, manifest: CaptureManifest) -> tuple[Path, bytes]:
    """Materialize a manifest file so capture resolves relative transcripts."""
    import msgspec  # type: ignore[import-not-found]

    raw = msgspec.yaml.encode(manifest)
    path = tmp_path / "capture-manifest.yaml"
    path.write_bytes(raw)
    return path, raw


# ---------------------------------------------------------------------------
# Manifest loading and validation.
# ---------------------------------------------------------------------------


def test_load_manifest_reads_client_manifest_and_keeps_bytes():
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")
    assert manifest.client == "pi"
    assert manifest.project == "lumio-reader"
    assert manifest.transcript == "pi-session.jsonl"
    assert manifest.artifacts == ["lumio-wiki-search-results.md"]
    assert manifest.redactions == ["oracle-a1 host IP"]
    assert raw == (CAPTURE_FIXTURES / "pi.yaml").read_bytes()


def test_load_manifest_rejects_unknown_fields(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text("client: pi\nsurprise: 1\n")
    with pytest.raises(CaptureError, match="invalid capture manifest"):
        load_capture_manifest(path)


def test_load_manifest_rejects_bad_client_label(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text("client: Not A Label\n")
    with pytest.raises(CaptureError, match="client must be a lowercase label"):
        load_capture_manifest(path)


def test_load_manifest_rejects_inverted_time_range(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text(
        'client: pi\nstarted_at: "2026-08-24T10:00:00Z"\nended_at: "2026-08-24T09:00:00Z"\n'
    )
    with pytest.raises(CaptureError, match="started_at is after ended_at"):
        load_capture_manifest(path)


def test_load_manifest_rejects_missing_file(tmp_path: Path):
    with pytest.raises(CaptureError, match="not readable"):
        load_capture_manifest(tmp_path / "absent.yaml")


# ---------------------------------------------------------------------------
# Redaction safety net.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label, secret_text, category", SECRET_FIXTURES)
def test_secret_fixtures_are_redacted_with_counts(label, secret_text, category):
    redacted, counts = redact_capture_text(secret_text)
    assert "sk-ant-" not in redacted
    assert "X-Amz-Signature" not in redacted
    assert "super-secret-value-123" not in redacted
    assert "hunter2000secret" not in redacted
    assert counts.get(category, 0) >= 1, f"{label}: expected a {category} redaction"


def test_hidden_reasoning_blocks_are_stripped_entirely():
    text = "Finding A.\n<think>chain of thought: the user probably wants...</think>\nFinding B."
    redacted, counts = redact_capture_text(text)
    assert "chain of thought" not in redacted
    assert "Finding A." in redacted and "Finding B." in redacted
    assert counts.get("hidden-reasoning", 0) == 1


def test_unclosed_reasoning_tag_strips_to_end_of_text():
    redacted, _ = redact_capture_text("Finding A.\n<think>leaked partial reasoning")
    assert "leaked partial reasoning" not in redacted


def test_private_key_blocks_are_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    )
    redacted, counts = redact_capture_text(f"signing key:\n{pem}\ndone")
    assert "MIIEowIBAAKCAQEA" not in redacted
    assert counts.get("private-key", 0) == 1


def test_plain_prose_is_untouched():
    text = "# Decisions\n\n- `uv run pytest -q` passed with 12 focused tests."
    redacted, counts = redact_capture_text(text)
    assert redacted == text
    assert counts == {}


# ---------------------------------------------------------------------------
# Raw-transcript refusal (AC3).
# ---------------------------------------------------------------------------


def test_detect_raw_transcript_finds_chat_turns():
    turns = detect_raw_transcript("user: hi\nassistant: hello\nuser: bye\n")
    assert len(turns) == 3


def test_session_page_with_chat_turns_is_refused(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")
    manifest_path, manifest_bytes = CAPTURE_FIXTURES / "pi.yaml", raw
    page = _page(
        "session-pi",
        body=(
            "# Transcript\n\n"
            "user: why does it fail?\n"
            "assistant: because of the guardrail.\n"
            "user: ok fix it\n"
            "assistant: done.\n"
        ),
    )
    with pytest.raises(CaptureError, match="raw conversational transcript"):
        capture_session(
            pipeline,
            store,
            compiled_page_markdown=page,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_path=manifest_path,
            source_id="session-pi",
            transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
            confirmed=True,
        )


def test_two_turn_mentions_do_not_trigger_refusal():
    # Below the 3-turn threshold: quoting one exchange inside declarative
    # knowledge is legitimate.
    assert len(detect_raw_transcript("assistant: said it works\nuser: ok")) < 3


# ---------------------------------------------------------------------------
# AC2 — preview before registration/staging.
# ---------------------------------------------------------------------------


def test_unconfirmed_capture_registers_and_stages_nothing(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")

    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-preview"),
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "pi.yaml",
        source_id="session-preview",
        transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
        confirmed=False,
    )

    assert outcome.proposal is None
    assert outcome.preview.client == "pi"
    assert outcome.preview.source_id == "session-preview"
    assert "Decisions" in outcome.preview.sections and "Commands" in outcome.preview.sections
    assert not (Path(store.root) / "captures").exists()
    assert store.source_registry.list() == []
    assert pipeline.list() == []
    # The KB itself is untouched.
    assert not any("Session Findings" in p.title for p in kb.pages)


def test_preview_text_lists_sections_and_redaction_counts(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")
    page = _page("session-preview2").replace(
        "passed with 12 focused tests", "used key sk-ant-0123456789abcdef0123"
    )
    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=page,
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "pi.yaml",
        source_id="session-preview2",
        transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
    )
    text = format_capture_preview(outcome.preview)
    assert "sections:" in text and "Decisions" in text
    assert "redactions applied: 1 (1 api-key)" in text
    assert "sk-ant-" not in text


# ---------------------------------------------------------------------------
# AC1 — one confirmed capture stages exactly one reviewable proposal.
# ---------------------------------------------------------------------------


def test_confirmed_capture_stages_single_reviewable_proposal(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")

    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-2026-pi"),
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "pi.yaml",
        source_id="session-2026-pi",
        transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
        confirmed=True,
    )

    proposal = outcome.proposal
    assert proposal is not None
    assert proposal.status == "staged"
    assert [p.id for p in pipeline.list()] == [proposal.id]
    assert proposal.affected_pages == ["Session Findings"]
    assert proposal.provenance.source_id == "session-2026-pi"
    # Declarative page content, not transcript bytes.
    assert "Verified findings" in proposal.proposed_pages[0].markdown
    assert "Retrieval found no Evidence" not in proposal.proposed_pages[0].markdown
    # Private manifest record under the ingest store only.
    record = json.loads(
        (Path(store.root) / "captures" / "session-2026-pi.json").read_text()
    )
    assert record["manifest"]["client"] == "pi"
    assert record["transcript_digest"] is not None
    # Not published: the KB still has no captured page.
    assert not any(p.title == "Session Findings" for p in kb.pages)


def test_confirmed_capture_binds_transcript_as_source_bytes(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "codex.yaml")

    capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-2026-codex"),
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "codex.yaml",
        source_id="session-2026-codex",
        transcript_path=CAPTURE_FIXTURES / "codex-session.jsonl",
        confirmed=True,
    )

    sources = store.source_registry.list()
    assert [s.source_id for s in sources] == ["session-2026-codex"]
    assert sources[0].status == "active"
    # The registered bytes are the REDACTED transcript bytes.
    version = sources[0].versions[-1]
    transcript = (CAPTURE_FIXTURES / "codex-session.jsonl").read_bytes()
    import hashlib

    from lumio_wiki.capture import redact_capture_text

    expected = redact_capture_text(transcript.decode())[0].encode()
    assert version.content_hash == hashlib.sha256(expected).hexdigest()


# ---------------------------------------------------------------------------
# AC4 — secrets never reach the page, the manifest record, or output.
# ---------------------------------------------------------------------------


def test_secrets_never_reach_proposal_or_capture_record(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")
    page = _page(
        "session-secrets",
        body=(
            "# Findings\n\n"
            "- Signed fetch used "
            "https://s3.example/bucket/r?X-Amz-Signature=deadbeefdeadbeefdead "
            "and key sk-ant-998877665544332211 during verification.\n"
        ),
    )

    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=page,
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "pi.yaml",
        source_id="session-secrets",
        transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
        confirmed=True,
    )

    proposal = outcome.proposal
    markdown = proposal.proposed_pages[0].markdown
    assert "sk-ant-998877665544332211" not in markdown
    assert "X-Amz-Signature" not in markdown
    assert "[REDACTED]" in markdown
    record_text = (Path(store.root) / "captures" / "session-secrets.json").read_text()
    assert "sk-ant-998877665544332211" not in record_text
    assert "deadbeef" not in record_text


def test_secret_bearing_transcript_is_registered_redacted(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    transcript = tmp_path / "secret-session.jsonl"
    transcript.write_text(
        '{"role":"assistant","text":"deploy token: ghp_abc123abc123abc123abc123 done"}\n'
    )
    manifest = CaptureManifest(client="pi", project="p")
    manifest_path, manifest_bytes = _manifest_bytes(tmp_path, manifest)

    capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-secret-transcript"),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_path=manifest_path,
        source_id="session-secret-transcript",
        transcript_path=transcript,
        confirmed=True,
    )

    # The secret never entered the registered source identity either: the
    # registry content hash is over the REDACTED transcript bytes (raw bytes
    # are hash-only in the registry, ADR-0014 — nothing is written out).
    import hashlib

    source = store.source_registry.get("session-secret-transcript")
    redacted_bytes = redact_capture_text(transcript.read_text())[0].encode()
    assert source.versions[-1].content_hash == hashlib.sha256(redacted_bytes).hexdigest()
    assert "ghp_" not in json.dumps(
        json.loads(json.dumps(_registry_state(store))), default=str
    )


def _registry_state(store) -> dict:
    """The registry's persisted private state as plain data (for asserts)."""
    import msgspec  # type: ignore[import-not-found]

    return msgspec.to_builtins(store.source_registry._read())


# ---------------------------------------------------------------------------
# AC5 — one contract across client fixtures, manual fallback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", CLIENT_MANIFESTS)
def test_each_client_fixture_captures_through_one_contract(tmp_path: Path, client: str):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / f"{client}.yaml")
    transcript = (
        CAPTURE_FIXTURES / f"{client}-session.jsonl"
        if (CAPTURE_FIXTURES / f"{client}-session.jsonl").is_file()
        else None
    )

    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page(f"session-{client}"),
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / f"{client}.yaml",
        source_id=f"session-{client}",
        transcript_path=transcript,
        confirmed=True,
    )

    assert outcome.proposal is not None
    assert outcome.preview.client == client
    assert outcome.proposal.provenance.source_id == f"session-{client}"
    # Manual capture (no transcript) registers the manifest as the record and
    # says so instead of fabricating a transcript.
    if client == "manual":
        assert any(
            "manifest is registered as the private capture record" in w
            for w in outcome.preview.warnings
        )


def test_manual_manifest_without_transcript_registers_manifest_record(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "manual.yaml")

    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-manual-only"),
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "manual.yaml",
        source_id="session-manual-only",
        confirmed=True,
    )

    assert outcome.proposal.provenance.original_filename == "manual.yaml"
    assert outcome.proposal.provenance.content_type == "application/yaml"


# ---------------------------------------------------------------------------
# Adapter-limitation rules.
# ---------------------------------------------------------------------------


def test_missing_named_transcript_is_reported_not_fabricated(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest = CaptureManifest(client="codex", transcript="vanished.jsonl")
    manifest_path, manifest_bytes = _manifest_bytes(tmp_path, manifest)

    with pytest.raises(CaptureError, match="transcript not found"):
        capture_session(
            pipeline,
            store,
            compiled_page_markdown=_page("session-gone"),
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_path=manifest_path,
            source_id="session-gone",
            confirmed=True,
        )


def test_digest_only_manifest_reports_limitation_and_registers_manifest(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest = CaptureManifest(client="pi", transcript="sha256:" + "0" * 64)
    manifest_path, manifest_bytes = _manifest_bytes(tmp_path, manifest)

    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-digest-only"),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_path=manifest_path,
        source_id="session-digest-only",
        confirmed=True,
    )

    assert any("digest only" in w for w in outcome.preview.warnings)
    assert outcome.proposal.provenance.original_filename == manifest_path.name


def test_digest_mismatch_refuses_staging(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"role":"user","text":"hello"}\n')
    manifest = CaptureManifest(client="pi", transcript="sha256:" + "f" * 64)
    manifest_path, manifest_bytes = _manifest_bytes(tmp_path, manifest)

    with pytest.raises(CaptureError, match="do not match the digest"):
        capture_session(
            pipeline,
            store,
            compiled_page_markdown=_page("session-mismatch"),
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_path=manifest_path,
            source_id="session-mismatch",
            transcript_path=transcript,
            confirmed=True,
        )
    assert pipeline.list() == []


def test_page_must_declare_source_id_before_any_registration(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")
    page = _page("different-id")

    with pytest.raises(CaptureError, match="must declare source_id"):
        capture_session(
            pipeline,
            store,
            compiled_page_markdown=page,
            manifest=manifest,
            manifest_bytes=raw,
            manifest_path=CAPTURE_FIXTURES / "pi.yaml",
            source_id="session-mismatched",
            transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
            confirmed=True,
        )
    assert store.source_registry.list() == []
    assert pipeline.list() == []


# ---------------------------------------------------------------------------
# AC6 — capture stays optional; ordinary ingest is unchanged.
# ---------------------------------------------------------------------------


def test_capture_never_autopublishes(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    manifest, raw = load_capture_manifest(CAPTURE_FIXTURES / "pi.yaml")
    capture_session(
        pipeline,
        store,
        compiled_page_markdown=_page("session-nopublish"),
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=CAPTURE_FIXTURES / "pi.yaml",
        source_id="session-nopublish",
        transcript_path=CAPTURE_FIXTURES / "pi-session.jsonl",
        confirmed=True,
    )
    # Staged, not published: the page is absent from the live KB.
    assert not any(p.title == "Session Findings" for p in kb.pages)
    assert pipeline.list()[0].status == "staged"


def test_ordinary_managed_ingest_still_works_alongside_capture(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    source = tmp_path / "plain.md"
    source.write_text("# Plain Source\n\nordinary bytes\n")
    page = _page("plain-source", title="Plain Captured Note")
    proposal = pipeline.managed_ingest(
        source.read_bytes(), "text/markdown", "plain.md", "plain-source", page
    )
    assert proposal.status == "staged"
    # Registry holds the ordinary source; no capture record was created.
    assert not (Path(store.root) / "captures").exists()


# ---------------------------------------------------------------------------
# CLI — preview-first contract over the public command surface.
# ---------------------------------------------------------------------------


def _run_cli(tmp_path: Path, *argv: str | Path) -> tuple[int, str]:
    import contextlib
    import io

    from lumio_wiki.cli import main

    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in argv])
        except SystemExit as exc:  # argparse --help / usage errors
            code = exc.code if isinstance(exc.code, int) else 2
    return code, buf.getvalue() + err.getvalue()


def test_cli_preview_mode_stages_nothing(tmp_path: Path, capsys):
    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", kb_root)
    page = tmp_path / "page.md"
    page.write_text(_page("session-cli-preview"))

    code, out = _run_cli(
        tmp_path,
        "capture",
        "session",
        kb_root,
        "--compiled-page",
        page,
        "--manifest",
        CAPTURE_FIXTURES / "pi.yaml",
        "--source-id",
        "session-cli-preview",
        "--transcript",
        CAPTURE_FIXTURES / "pi-session.jsonl",
    )

    assert code == 0
    assert "sections:" in out and "Decisions" in out
    assert "redactions applied:" in out
    assert "Preview only" in out
    assert "Staged proposal" not in out
    kb, report = lw.load_knowledge_base(kb_root)
    assert report.is_valid
    store = IngestStore(kb_root / ".lumio" / "ingest")
    assert store.source_registry.list() == []
    assert not any(p.title == "Session Findings" for p in kb.pages)


def test_cli_yes_stages_reviewable_proposal(tmp_path: Path):
    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", kb_root)
    page = tmp_path / "page.md"
    page.write_text(_page("session-cli-yes"))

    code, out = _run_cli(
        tmp_path,
        "capture",
        "session",
        kb_root,
        "--compiled-page",
        page,
        "--manifest",
        CAPTURE_FIXTURES / "pi.yaml",
        "--source-id",
        "session-cli-yes",
        "--transcript",
        CAPTURE_FIXTURES / "pi-session.jsonl",
        "--yes",
    )

    assert code == 0
    assert "Staged proposal" in out
    assert "affected_pages: Session Findings" in out
    store = IngestStore(kb_root / ".lumio" / "ingest")
    assert [s.source_id for s in store.source_registry.list()] == ["session-cli-yes"]
    assert len(store._cache) == 0  # proposals persist on disk, not in memory


def test_cli_secret_in_page_is_redacted_in_output(tmp_path: Path):
    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", kb_root)
    page = tmp_path / "page.md"
    page.write_text(
        _page("session-cli-secret").replace(
            "passed with 12 focused tests", "used key sk-ant-0123456789abcdef0123"
        )
    )

    code, out = _run_cli(
        tmp_path,
        "capture",
        "session",
        kb_root,
        "--compiled-page",
        page,
        "--manifest",
        CAPTURE_FIXTURES / "pi.yaml",
        "--source-id",
        "session-cli-secret",
        "--transcript",
        CAPTURE_FIXTURES / "pi-session.jsonl",
        "--yes",
    )

    assert code == 0
    assert "sk-ant-" not in out
    assert "redactions applied: 1 (1 api-key)" in out


def test_cli_missing_manifest_errors_cleanly(tmp_path: Path):
    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", kb_root)
    page = tmp_path / "page.md"
    page.write_text(_page("session-cli-missing"))

    code, out = _run_cli(
        tmp_path,
        "capture",
        "session",
        kb_root,
        "--compiled-page",
        page,
        "--manifest",
        tmp_path / "absent.yaml",
        "--source-id",
        "session-cli-missing",
    )
    assert code == 2
    assert "capture manifest not found" in out


def test_cli_capture_help_lists_session_subcommand(tmp_path: Path):
    code, out = _run_cli(tmp_path, "capture", "--help")
    assert code == 0
    assert "session" in out
