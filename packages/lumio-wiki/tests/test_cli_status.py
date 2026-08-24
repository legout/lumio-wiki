"""``lumio-wiki status``: effective configuration + runtime state (issue #175).

Covers the one-command explanation of the project's effective Knowledge Base
configuration and retrieval state: role and configuration-source attribution,
backend/mode as separate fields in rendered and JSON output, the S3 reader
journey (active Published Version + canonical fingerprint), truthful LanceDB
degradation with recovery guidance, Source Artifact retention without
credentials or object keys, and the shared post-``setup`` summary.

Stub-based orchestration tests run everywhere (no S3 / LanceDB required); the
live reader journey is covered by the MinIO suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import msgspec
import pytest  # type: ignore[import-not-found]
from lumio_wiki import cli
from lumio_wiki.cli import CliError
from lumio_wiki.location import FilesystemLocation, KnowledgeBaseSnapshot, RemoteDerivedIndex
from lumio_wiki.records import SourceFingerprint

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


def _flat(text: str) -> str:
    """Collapse the renderer's alignment padding: ``key:<value>`` per line."""
    import re

    return "\n".join(re.sub(r": +", ":", line) for line in text.splitlines())


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_LUMIO_ENV_KEYS = (
    "LUMIO_KB_PATH",
    "LUMIO_PUBLISH_TO",
    "LUMIO_SOURCE_STORE",
    "LUMIO_ARTIFACT_RETENTION",
    "LUMIO_RETRIEVAL_BACKEND",
    "LUMIO_RETRIEVAL_MODE",
    "LUMIO_S3_REGION",
    "LUMIO_S3_ENDPOINT",
)


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Isolate env-derived config: clean cwd, no exported LUMIO keys."""
    monkeypatch.chdir(tmp_path)
    for key in _LUMIO_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _StubS3Location:
    """A stand-in S3 Location resolving to a prepared Snapshot."""

    def __init__(self, snapshot, graph_source="published artifact", edges=2, note=None) -> None:
        self._snapshot = snapshot
        self._graph_source = graph_source
        self._edges = edges
        self._note = note

    def resolve(self):
        return self._snapshot

    def graph_state_with_source(self, snapshot=None):
        return SimpleNamespace(edge_count=self._edges), self._graph_source, self._note


def _reader_snapshot() -> KnowledgeBaseSnapshot:
    return FilesystemLocation(FIXTURES / "valid").resolve()


def _snapshot_with_descriptor(snapshot, *, version="v1"):
    return msgspec.structs.replace(
        snapshot,
        remote_derived_index=RemoteDerivedIndex(
            uri="s3://bucket/kb/v1/derived/lance",
            sidecar_prefix="kb/v1/derived/lance",
            version=version,
            fingerprint=snapshot.fingerprint,
            store=None,
        ),
    )


class _StubLanceLocation:
    """Stand-in for a lumio_lancedb IndexLocation with scripted behavior."""

    def __init__(self, *, has_index=True, describe="s3://bucket/kb/v1/derived/lance",
                 fingerprint=None, tables=("evidence", "pages"), error=None):
        self._has_index = has_index
        self._describe = describe
        self._fingerprint = fingerprint
        self._tables = tables
        self._error = error

    @property
    def describe(self):
        return self._describe

    def has_index(self):
        if self._error is not None:
            raise self._error
        return self._has_index

    def read_sidecar(self, name):
        from lumio_wiki.fingerprint_store import FINGERPRINT_FILE

        if name == FINGERPRINT_FILE and self._fingerprint is not None:
            return msgspec.json.encode(self._fingerprint)
        return None


def _route_reader(monkeypatch, snapshot, graph_source="published artifact") -> None:
    """Route S3 resolution to a stub Location wrapping ``snapshot``.

    The stub also replaces the snapshot's ``location`` so the collector's
    graph-source probe (``location.graph_state_with_source``) observes it.
    """
    stub = _StubS3Location(snapshot, graph_source=graph_source)
    routed = msgspec.structs.replace(snapshot, location=stub)

    def _fake_resolve(uri):
        assert cli._is_object_store_uri(uri), "S3 path must reach the object-store branch"
        return _StubS3Location(routed, graph_source=graph_source)

    monkeypatch.setattr(cli, "_resolve_object_store_location", _fake_resolve)


def _route_lance(monkeypatch, location) -> None:
    """Route the dynamic lumio_lancedb binding to a stub module + location."""

    class _StubModule:
        TABLE_NAME = "evidence"
        PAGE_TABLE_NAME = "pages"

        RemoteIndexLocation = lambda *a, **k: location  # noqa: E731
        LocalIndexLocation = lambda *a, **k: location  # noqa: E731

    import importlib

    monkeypatch.setattr(importlib, "import_module", lambda name: _StubModule())


def _stale_fingerprint() -> SourceFingerprint:
    return SourceFingerprint(digest="0" * 64)


# ---------------------------------------------------------------------------
# Local Maintainer status.
# ---------------------------------------------------------------------------


def test_status_maintainer_role_and_argument_source(capsys):
    assert cli.main(["status", str(FIXTURES / "valid")]) == 0
    out = _flat(capsys.readouterr().out)
    assert "role:maintainer (local worktree)" in out
    assert "config_source:argument" in out
    assert f"kb_location:{FIXTURES / 'valid'}" in out


def test_status_config_source_exported_env(monkeypatch, capsys):
    monkeypatch.setenv("LUMIO_KB_PATH", str(FIXTURES / "valid"))
    assert cli.main(["status"]) == 0
    assert "config_source:exported env (LUMIO_KB_PATH)" in _flat(capsys.readouterr().out)


def test_status_config_source_project_env_file(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(f"LUMIO_KB_PATH={FIXTURES / 'valid'}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == 0
    out = _flat(capsys.readouterr().out)
    assert f"config_source:project .env ({tmp_path / '.env'})" in out


def test_status_without_any_location_is_actionable():
    with pytest.raises(CliError, match="no Knowledge Base location configured"):
        cli._collect_status(None)


def test_status_backend_and_mode_are_separate_fields(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"LUMIO_KB_PATH={FIXTURES / 'valid'}",
                "LUMIO_RETRIEVAL_BACKEND=lancedb",
                "LUMIO_RETRIEVAL_MODE=semantic",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    status = cli._collect_status()
    assert status["retrieval_backend"] == "lancedb"
    assert status["retrieval_mode"] == "semantic"
    assert status["retrieval_backend"] != status["retrieval_mode"]


def test_status_rendered_output_distinguishes_backend_and_mode(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"LUMIO_KB_PATH={FIXTURES / 'valid'}",
                "LUMIO_RETRIEVAL_BACKEND=lancedb",
                "LUMIO_RETRIEVAL_MODE=hybrid",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == 0
    out = _flat(capsys.readouterr().out)
    assert "retrieval_backend:lancedb" in out
    assert "retrieval_mode:hybrid" in out


def test_status_graph_memory_then_artifact_after_materialization(tmp_path):
    import shutil

    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", kb_root)
    first = cli._collect_status(str(kb_root))
    assert first["graph_source"] == "memory"
    assert first["graph_fresh"] is False
    assert "health" in first["next_action"] and "--rebuild" in first["next_action"]

    from lumio_wiki.knowledge_base import load_knowledge_base

    kb, _report = load_knowledge_base(kb_root)
    kb.materialize_graph(cli.default_index_dir(kb.root))
    second = cli._collect_status(str(kb_root))
    assert second["graph_source"] == "artifact"
    assert second["graph_fresh"] is True
    assert second["next_action"] == "none required"


def test_status_reports_validation_state_and_next_action():
    status = cli._collect_status(str(FIXTURES / "invalid"))
    assert status["validation_valid"] is False
    assert status["validation_errors"] >= 1
    assert "validate" in status["next_action"]


def test_status_retention_and_store_kind_without_secrets(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"LUMIO_KB_PATH={FIXTURES / 'valid'}",
                "LUMIO_SOURCE_STORE=s3://private-bucket/artifacts",
                "LUMIO_ARTIFACT_RETENTION=required",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rendered = _flat(cli._render_status(cli._collect_status()))
    assert "artifact_retention:required" in rendered
    assert "artifact_store:s3" in rendered
    # The private store URI never appears: kind only, no keys, no credentials.
    assert "private-bucket" not in rendered


def test_status_lancedb_requested_but_not_installed(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(
            [f"LUMIO_KB_PATH={FIXTURES / 'valid'}", "LUMIO_RETRIEVAL_BACKEND=lancedb"]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from lumio_wiki import retrieval_eval

    monkeypatch.setattr(retrieval_eval, "lancedb_available", lambda: False)
    status = cli._collect_status()
    assert status["lancedb_requested"] is True
    assert status["lancedb_available"] is False
    assert "lumio-lancedb" in status["lancedb_fallback"]
    assert "pip install 'lumio-lancedb[s3]'" in status["next_action"]


def test_status_local_lancedb_index_not_built_yet(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(
            [f"LUMIO_KB_PATH={FIXTURES / 'valid'}", "LUMIO_RETRIEVAL_BACKEND=lancedb"]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from lumio_wiki import retrieval_eval

    monkeypatch.setattr(retrieval_eval, "lancedb_available", lambda: True)
    status = cli._collect_status()
    assert status["lancedb_healthy"] is None
    assert "no local derived index yet" in status["lancedb_fallback"]
    assert ".lumio" in status["lancedb_fallback"]


def test_status_zero_index_backend_never_probes_lance():
    status = cli._collect_status(str(FIXTURES / "valid"))
    assert status["lancedb_requested"] is False
    assert status["lancedb_healthy"] is None
    assert status["lancedb_fallback"] is None


def test_status_json_matches_rendered_keys(capsys):
    assert cli.main(["status", str(FIXTURES / "valid"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = cli._render_status(payload)
    for key in payload:
        assert f"{key}:" in rendered
    assert payload["role"] == "maintainer"
    assert payload["retrieval_backend"] == "zero-index"
    assert payload["retrieval_mode"] == "lexical"


# ---------------------------------------------------------------------------
# Read-only S3 Reader status (stubbed Location).
# ---------------------------------------------------------------------------


def test_status_reader_reports_published_version_and_fingerprint(monkeypatch):
    snapshot = _snapshot_with_descriptor(_reader_snapshot(), version="v1")
    _route_reader(monkeypatch, snapshot)
    status = cli._collect_status("s3://bucket/kb")
    assert status["role"] == "reader"
    assert status["published_version"] == "v1"
    assert status["fingerprint"] == snapshot.fingerprint.digest
    assert status["graph_source"] == "published artifact"
    assert status["graph_fresh"] is True
    assert status["graph_edges"] == 2
    assert status["validation_valid"] is True


def test_status_reader_graph_derived_in_memory(monkeypatch):
    _route_reader(monkeypatch, _reader_snapshot(), graph_source="memory")
    status = cli._collect_status("s3://bucket/kb")
    assert status["graph_source"] == "memory"
    assert status["graph_fresh"] is False
    # Derived-in-memory is complete, not broken: no recovery action demanded.
    assert status["next_action"] == "none required"


def test_status_reader_lance_healthy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    snapshot = _snapshot_with_descriptor(_reader_snapshot())
    _route_reader(monkeypatch, snapshot)
    _route_lance(
        monkeypatch,
        _StubLanceLocation(fingerprint=snapshot.fingerprint, tables=("evidence", "pages")),
    )
    _reader_with_backend(monkeypatch, _StubLanceLocation(fingerprint=snapshot.fingerprint))
    status = cli._collect_status()
    assert status["lancedb_healthy"] is True
    assert status["lancedb_fingerprint_matches"] is True
    assert status["lancedb_fallback"] is None
    assert status["lancedb_index"] == "s3://bucket/kb/v1/derived/lance"


def _reader_with_backend(monkeypatch, lance, *, backend="lancedb"):
    snapshot = _snapshot_with_descriptor(_reader_snapshot())
    _route_reader(monkeypatch, snapshot)
    _route_lance(monkeypatch, lance)
    (Path.cwd() / ".env").write_text(
        "\n".join(["LUMIO_KB_PATH=s3://bucket/kb", f"LUMIO_RETRIEVAL_BACKEND={backend}"]) + "\n",
        encoding="utf-8",
    )


def test_status_reader_lance_missing_shows_fallback_and_recovery(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reader_with_backend(monkeypatch, _StubLanceLocation(has_index=False))
    status = cli._collect_status()
    assert status["lancedb_healthy"] is False
    assert "missing" in status["lancedb_fallback"]
    assert "zero-index" in status["lancedb_fallback"]
    assert "publish-s3 --retrieval lancedb" in status["lancedb_fallback"]
    assert status["next_action"] == status["lancedb_fallback"]


def test_status_reader_lance_stale_fingerprint_mismatch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reader_with_backend(monkeypatch, _StubLanceLocation(fingerprint=_stale_fingerprint()))
    status = cli._collect_status()
    assert status["lancedb_healthy"] is False
    assert status["lancedb_fingerprint_matches"] is False
    fallback = status["lancedb_fallback"]
    assert "stale" in fallback and "fingerprint mismatch" in fallback


def test_status_reader_lance_unavailable_degrades_truthfully(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reader_with_backend(
        monkeypatch, _StubLanceLocation(error=RuntimeError("connection reset"))
    )
    status = cli._collect_status()
    assert status["lancedb_healthy"] is False
    assert "unavailable" in status["lancedb_fallback"]
    assert "RuntimeError" in status["lancedb_fallback"]
    assert "zero-index" in status["lancedb_fallback"]


def test_status_reader_no_remote_derived_index_descriptor(monkeypatch, tmp_path):
    _route_reader(monkeypatch, _reader_snapshot())  # snapshot without descriptor
    monkeypatch.chdir(tmp_path)
    (Path.cwd() / ".env").write_text(
        "\n".join(["LUMIO_KB_PATH=s3://bucket/kb", "LUMIO_RETRIEVAL_BACKEND=lancedb"]) + "\n",
        encoding="utf-8",
    )
    status = cli._collect_status()
    assert status["published_version"] is None
    assert status["lancedb_healthy"] is None
    assert "no remote derived index" in status["lancedb_fallback"]
    assert "publish-s3 --retrieval lancedb" in status["lancedb_fallback"]


def test_status_output_never_contains_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("LUMIO_S3_ACCESS_KEY_ID", "AKIASECRETKEYVALUE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "topSecretAccessKeyValue")
    snapshot = _snapshot_with_descriptor(_reader_snapshot())
    _route_reader(monkeypatch, snapshot)
    rendered = cli._render_status(cli._collect_status("s3://bucket/kb"))
    assert "AKIASECRETKEYVALUE" not in rendered
    assert "topSecretAccessKeyValue" not in rendered


# ---------------------------------------------------------------------------
# Shared post-setup summary.
# ---------------------------------------------------------------------------


def test_setup_prints_shared_status_summary_maintainer(tmp_path, capsys):
    assert cli.main(["setup", "kb"]) == 0
    out = _flat(capsys.readouterr().out)
    assert "role:maintainer (local worktree)" in out
    assert "retrieval_backend:zero-index" in out
    assert "next_action:" in out


def test_setup_prints_shared_status_summary_reader(monkeypatch, tmp_path, capsys):
    snapshot = _snapshot_with_descriptor(_reader_snapshot())
    _route_reader(monkeypatch, snapshot)
    assert cli.main(["setup", "--from", "s3://bucket/kb"]) == 0
    out = _flat(capsys.readouterr().out)
    assert "role:reader (read-only S3)" in out
    assert "published_version:v1" in out


def test_setup_summary_degrades_when_location_not_yet_resolvable(
    monkeypatch, tmp_path, capsys
):
    def _unreachable(uri):
        raise cli.CliError("could not resolve S3 Knowledge Base")

    monkeypatch.setattr(cli, "_resolve_object_store_location", _unreachable)
    assert cli.main(["setup", "--from", "s3://bucket/kb"]) == 0
    out = capsys.readouterr().out
    assert "not resolvable yet" in out
    assert "could not resolve S3 Knowledge Base" in out
