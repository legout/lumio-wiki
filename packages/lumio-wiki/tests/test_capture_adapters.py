"""Read-only Codex and Pi session-history discovery/export adapters.

Synthetic fixtures only — no real user histories are inspected, read, or
printed. The adapters are read-only: discovery never leaves the declared
session root, export copies the ORIGINAL bytes (never transcript content) to
a caller-provided output directory, produces the existing Capture Manifest,
and never registers, stages, publishes, or prints transcript content.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lumio_wiki.capture import (
    CaptureError,
    discover_sessions,
    export_session,
    format_session_export,
    format_sessions,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "capture"

CODEX_SESSION_ID = "9f0f8f0e-1111-4222-8333-444455556666"
CODEX_TS = "2026-08-24T11:00:00.000Z"
PI_SESSION_ID = "11111111-2222-4333-8444-555566667777"
PI_TS = "2026-08-24T09:30:00.000Z"


def _codex_root(tmp_path: Path) -> Path:
    root = tmp_path / "codex" / "sessions" / "2026" / "08" / "24"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_codex(
    tmp_path: Path,
    *,
    cwd: str = "/repo",
    session_id: str = CODEX_SESSION_ID,
    filename_ts: str = "2026-08-24T11-00-00",
    with_secret: bool = False,
) -> Path:
    """Write one synthetic Codex rollout fixture under a temp sessions root."""
    path = _codex_root(tmp_path) / f"rollout-{filename_ts}-{session_id}.jsonl"
    lines = [
        {
            "timestamp": CODEX_TS,
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": CODEX_TS,
                "cwd": cwd,
                "originator": "codex",
                "cli_version": "0.121.0",
            },
        },
        {
            "timestamp": CODEX_TS,
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Run the focused suite."
                # api-key-shaped secret: trips the capture redaction net
                + (" used sk-abcdef1234567890abcdef" if with_secret else ""),
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _pi_root(tmp_path: Path, cwd: str = "/repo") -> Path:
    root = tmp_path / "pi" / "agent" / "sessions" / f"--{cwd.replace('/', '-')}--"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_pi(
    tmp_path: Path,
    *,
    cwd: str = "/repo",
    session_id: str = PI_SESSION_ID,
    filename_ts: str = "2026-08-24T09-30-00",
) -> Path:
    """Write one synthetic Pi session fixture under a temp sessions root."""
    path = _pi_root(tmp_path, cwd) / f"{filename_ts}_{session_id}.jsonl"
    lines = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": PI_TS,
            "cwd": cwd,
        },
        {
            "type": "label",
            "id": "cafebabe",
            "label": "night run",
            "targetId": "a1b2c3d4",
        },
        {
            "type": "message",
            "id": "a1b2c3d4",
            "parentId": None,
            "timestamp": "2026-08-24T09:30:01.000Z",
            "message": {"role": "user", "content": "Run the focused suite."},
        },
        # deliberately malformed middle line: skipped, never a hard failure
        "{not json",
    ]
    path.write_text(
        "\n".join(json.dumps(line) for line in lines[:3]) + "\n" + lines[3] + "\n", encoding="utf-8"
    )
    return path


def test_codex_discovery_orders_and_projects(tmp_path: Path) -> None:
    _make_codex(tmp_path, filename_ts="2026-08-24T09-00-00")
    _make_codex(
        tmp_path,
        cwd="/other",
        session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeffff0000",
        filename_ts="2026-08-24T10-00-00",
    )
    root = _codex_root(tmp_path)  # single shared sessions root
    sessions = discover_sessions("codex", root=root.parent.parent.parent)
    # deterministic chronological order (filename sort == time for both clients)
    assert [s.native_id for s in sessions] == [
        CODEX_SESSION_ID,
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeffff0000",
    ]
    assert sessions[0].client == "codex"
    assert sessions[1].project == "/other"
    assert sessions[0].started_at == CODEX_TS
    # ids are the stable sha256-16 path-derived ids, NOT the header ids
    for session in sessions:
        assert len(session.id) == 16
        int(session.id, 16)  # hex
        assert session.id != session.native_id
    assert sessions[0].id == _rediscovered_id(sessions[0])


def _rediscovered_id(session) -> str:
    """Recompute the stable id from client + path (determinism check)."""
    import hashlib

    digest = hashlib.sha256(f"{session.client}|{Path(session.path).resolve()}".encode()).hexdigest()
    return digest[:16]


def test_pi_discovery_reads_header_and_skips_malformed(tmp_path: Path) -> None:
    _make_pi(tmp_path)
    sessions = discover_sessions("pi", root=tmp_path / "pi" / "agent" / "sessions")
    assert len(sessions) == 1
    assert sessions[0].client == "pi"
    assert sessions[0].native_id == PI_SESSION_ID
    assert len(sessions[0].id) == 16
    assert sessions[0].project == "/repo"
    assert sessions[0].started_at == PI_TS


def test_discovery_filters_and_limit(tmp_path: Path) -> None:
    _make_codex(tmp_path)
    _make_pi(tmp_path)
    root = tmp_path
    assert discover_sessions("codex", root=root) == discover_sessions("codex", root=root)
    only_codex = discover_sessions("codex", root=root)
    assert {s.client for s in only_codex} == {"codex"}
    limited = discover_sessions("pi", root=root, limit=0)
    assert limited == []
    # since filter
    future = discover_sessions("codex", root=root, since="2099-01-01T00:00:00Z")
    assert future == []
    past = discover_sessions("codex", root=root, since="2000-01-01T00:00:00Z")
    assert len(past) == 1


def test_project_filter_matches_cwd(tmp_path: Path) -> None:
    _make_pi(tmp_path, cwd="/alpha")
    _make_pi(tmp_path, cwd="/beta", session_id="22222222-3333-4444-8555-666677778888")
    sessions = discover_sessions("pi", root=tmp_path / "pi" / "agent" / "sessions", project="/beta")
    assert [s.native_id for s in sessions] == ["22222222-3333-4444-8555-666677778888"]


def test_unknown_client_refused(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="unsupported capture client"):
        discover_sessions("claude", root=tmp_path)
    with pytest.raises(CaptureError, match="unsupported capture client"):
        export_session("claude", "whatever", Path(tmp_path) / "out", root=tmp_path)


def test_export_codex_writes_manifest_and_original_bytes(tmp_path: Path) -> None:
    transcript = _make_codex(tmp_path, with_secret=True)
    (session,) = discover_sessions("codex", root=tmp_path)
    out = tmp_path / "export"
    result = export_session("codex", session.id, out, root=tmp_path)
    assert result.session.native_id == CODEX_SESSION_ID
    assert result.transcript_path == str(out / "transcript.jsonl")
    assert Path(result.transcript_path).read_bytes() == transcript.read_bytes()
    manifest = result.manifest
    assert manifest.client == "codex"
    assert manifest.project == "/repo"
    assert manifest.started_at == CODEX_TS
    # Sibling file reference, NOT a declared digest: staging redacts the
    # transcript bytes before hashing, so a digest over the ORIGINAL bytes
    # would make `capture session` refuse whenever redaction fires.
    assert manifest.transcript == "transcript.jsonl"
    manifest_path = out / "capture.yaml"
    assert manifest_path.is_file()
    # the export result still reports the ORIGINAL-bytes digest
    import hashlib

    assert (
        result.transcript_digest
        == hashlib.sha256((out / "transcript.jsonl").read_bytes()).hexdigest()
    )


def test_export_pi_writes_manifest(tmp_path: Path) -> None:
    _make_pi(tmp_path)
    (session,) = discover_sessions("pi", root=tmp_path / "pi" / "agent" / "sessions")
    out = tmp_path / "export-pi"
    result = export_session("pi", session.id, out, root=tmp_path / "pi" / "agent" / "sessions")
    assert result.session.client == "pi"
    assert result.transcript_path == str(out / "transcript.jsonl")
    manifest = result.manifest
    assert manifest.client == "pi"
    assert manifest.transcript == "transcript.jsonl"
    assert (out / "capture.yaml").is_file()
    assert result.warnings  # malformed line reported, never fabricated


def test_export_unknown_session_is_secret_safe(tmp_path: Path) -> None:
    _make_pi(tmp_path, cwd="/hidden/project-x")
    with pytest.raises(CaptureError, match="unknown pi session id"):
        export_session("pi", "nope", tmp_path / "out", root=tmp_path / "pi" / "agent" / "sessions")
    assert not (tmp_path / "out").exists()


def test_export_refuses_to_overwrite_nonempty_output(tmp_path: Path) -> None:
    _make_codex(tmp_path)
    (session,) = discover_sessions("codex", root=tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(CaptureError, match="output directory not empty"):
        export_session("codex", session.id, out, root=tmp_path)


def test_export_refuses_output_path_that_is_a_file(tmp_path: Path) -> None:
    _make_codex(tmp_path)
    (session,) = discover_sessions("codex", root=tmp_path)
    out = tmp_path / "not-a-dir"
    out.write_text("occupied", encoding="utf-8")
    with pytest.raises(CaptureError, match="not a directory"):
        export_session("codex", session.id, out, root=tmp_path)
    assert out.read_text() == "occupied"  # untouched


def test_discovery_skips_malformed_header_timestamp(tmp_path: Path) -> None:
    _make_pi(tmp_path)
    # a second, otherwise-valid session file with a garbage header timestamp
    bad = _pi_root(tmp_path, cwd="/repo") / "2026-08-24T08-00-00_x.jsonl"
    bad.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "33333333-4444-4555-8666-777788889999",
                "timestamp": "not-a-timestamp",
                "cwd": "/repo",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # no hard failure: the malformed-header file is skipped, the rest listed
    sessions = discover_sessions("pi", root=tmp_path / "pi" / "agent" / "sessions")
    assert len(sessions) == 1
    assert sessions[0].native_id == PI_SESSION_ID
    # and --since does not abort on it either
    assert (
        discover_sessions(
            "pi",
            root=tmp_path / "pi" / "agent" / "sessions",
            since="2000-01-01T00:00:00Z",
        )
        == sessions
    )


def test_duplicate_native_header_ids_get_distinct_stable_ids(tmp_path: Path) -> None:
    """Codex resume writes multiple rollouts sharing one conversation id."""
    first = _make_codex(tmp_path, filename_ts="2026-08-24T09-00-00")
    second = _make_codex(
        tmp_path,
        filename_ts="2026-08-24T10-00-00",
        with_secret=True,  # different content, same native header id
    )
    assert first != second
    sessions = discover_sessions("codex", root=tmp_path)
    assert [s.native_id for s in sessions] == [CODEX_SESSION_ID, CODEX_SESSION_ID]
    # stable ids are file-derived: distinct, deterministic
    assert sessions[0].id != sessions[1].id
    assert len({s.id for s in sessions}) == 2
    # export picks exactly the requested file, not "whichever sorts last"
    picked = export_session("codex", sessions[0].id, tmp_path / "out", root=tmp_path)
    assert Path(picked.session.path) == first
    assert Path(picked.transcript_path).read_bytes() == first.read_bytes()


def test_export_then_preview_composes_through_capture_contract(
    tmp_path: Path,
) -> None:
    """The taught flow: export → distill → capture session preview.

    The transcript trips redaction (secret-shaped env dump), so the manifest
    must NOT declare an original-bytes digest — staging hashes REDACTED
    bytes and would otherwise refuse this exact composition.
    """
    import shutil

    import lumio_wiki as lw
    from lumio_wiki.capture import capture_session, load_capture_manifest
    from lumio_wiki.ingest import IngestStore
    from lumio_wiki.proposal_pipeline import ProposalPipeline

    transcript = _make_codex(tmp_path, with_secret=True)
    (session,) = discover_sessions("codex", root=tmp_path)
    export = export_session("codex", session.id, tmp_path / "export", root=tmp_path)
    assert Path(export.transcript_path).read_bytes() == transcript.read_bytes()

    manifest, raw = load_capture_manifest(export.manifest_path)
    assert manifest.transcript == "transcript.jsonl"

    kb_root = tmp_path / "kb"
    shutil.copytree(ROOT / "tests" / "fixtures" / "valid", kb_root)
    kb, report = lw.load_knowledge_base(kb_root)
    assert report.is_valid, report
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store=store)

    page = (
        "---\n"
        f'title: "Session Findings"\n'
        "aliases: []\n"
        'tags:\n  - "session-notes"\n'
        'summary: "Verified findings from a captured coding-agent session."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{export.session.id}"\n'
        f'    title: "{export.session.id} capture"\n'
        "synthetic: false\n"
        "---\n\n"
        "# Decisions\n\n- Focused suite runs green after the fix.\n\n"
        "# Commands\n\n- `uv run pytest -q` passed with 12 focused tests.\n"
    )
    outcome = capture_session(
        pipeline,
        store,
        compiled_page_markdown=page,
        manifest=manifest,
        manifest_bytes=raw,
        manifest_path=export.manifest_path,
        source_id=export.session.id,
        transcript_path=export.transcript_path,
        confirmed=False,
    )
    assert outcome.proposal is None  # preview path: nothing registered/staged
    assert outcome.preview.transcript_bound is True
    assert any("api-key" in w or "environment" in w for w in outcome.preview.warnings)
    assert store.source_registry.list() == []
    assert pipeline.list() == []


def test_formatting_never_contains_transcript_text(tmp_path: Path) -> None:
    _make_codex(tmp_path)
    sessions = discover_sessions("codex", root=tmp_path)
    table = format_sessions(sessions)
    assert sessions[0].native_id == CODEX_SESSION_ID
    assert CODEX_SESSION_ID in table
    assert "Run the focused suite" not in table
    result = export_session("codex", sessions[0].id, tmp_path / "out", root=tmp_path)
    rendered = format_session_export(result)
    assert "Run the focused suite" not in rendered
    assert "capture.yaml" in rendered


def test_manifest_fixture_round_trips_through_capture_contract() -> None:
    """The exported manifest shape matches the existing CaptureManifest type."""
    from lumio_wiki.capture import CaptureManifest, load_capture_manifest

    manifest, _raw = load_capture_manifest(FIXTURES / "codex.yaml")
    assert isinstance(manifest, CaptureManifest)
    manifest, _raw = load_capture_manifest(FIXTURES / "pi.yaml")
    assert manifest.client == "pi"


def test_discovery_does_not_leave_the_declared_root(tmp_path: Path) -> None:
    _make_pi(tmp_path / "nested", cwd="/repo")
    sessions = discover_sessions("pi", root=tmp_path / "pi" / "agent" / "sessions")
    assert sessions == []
    shutil.rmtree(tmp_path / "nested")
    assert discover_sessions("pi", root=tmp_path / "pi" / "agent" / "sessions") == []


# ---------------------------- CLI surface ----------------------------------


def _run_cli(*cli_args: object) -> tuple[int, str]:
    import contextlib
    import io

    from lumio_wiki.cli import main

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = main([str(a) for a in cli_args])
    except SystemExit as exc:  # argparse errors
        return int(exc.code or 0), buffer.getvalue()
    return code, buffer.getvalue()


def test_cli_sessions_json_and_export(tmp_path: Path) -> None:
    _make_codex(tmp_path)
    _make_pi(tmp_path)
    code, out = _run_cli(
        "capture",
        "sessions",
        "--client",
        "codex",
        "--root",
        tmp_path,
        "--json",
    )
    assert code == 0
    payload = json.loads(out)
    assert [s["native_id"] for s in payload] == [CODEX_SESSION_ID]
    assert payload[0]["client"] == "codex"
    assert len(payload[0]["id"]) == 16  # stable adapter id, not the header id

    out_dir = tmp_path / "cli-export"
    code, out = _run_cli(
        "capture",
        "export",
        "--client",
        "codex",
        "--session-id",
        payload[0]["id"],  # the stable adapter id from discovery
        "--output",
        out_dir,
        "--root",
        tmp_path,
    )
    assert code == 0
    assert "capture.yaml" in out
    assert "Run the focused suite" not in out  # transcript content never printed
    assert (out_dir / "transcript.jsonl").is_file()
    assert (out_dir / "capture.yaml").is_file()


def test_cli_sessions_unknown_client_exits_nonzero(tmp_path: Path) -> None:
    code, _out = _run_cli("capture", "sessions", "--client", "claude")
    assert code != 0
