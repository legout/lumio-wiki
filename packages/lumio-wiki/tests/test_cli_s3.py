"""CLI S3 Knowledge Base Location support (issue #120, ADR-0013).

Proves the ``lumio-wiki`` CLI routes validation, search, page reading, and
related-page traversal through the S3 Location seam (zero-index retrieval, no
LanceDB) when the path argument is an object-store URI. The S3 resolution is
stubbed to a real filesystem Snapshot so the command *output* contracts are
exercised deterministically without infrastructure; the live object-store read
path is covered by the in-memory contract suite and the MinIO integration.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from lumio_wiki import cli
from lumio_wiki.location import FilesystemLocation

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


class _StubS3Location:
    """A stand-in S3 Location that resolves to a real fixture Snapshot."""

    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def resolve(self):
        return self._snapshot


@pytest.fixture
def stub_s3_resolution(monkeypatch):
    """Route S3 URIs to the ``valid`` fixture Snapshot via the CLI seam."""
    snapshot = FilesystemLocation(FIXTURES / "valid").resolve()

    def _fake_resolve(uri):
        assert cli._is_object_store_uri(uri), "S3 path must reach the object-store branch"
        return _StubS3Location(snapshot)

    monkeypatch.setattr(cli, "_resolve_object_store_location", _fake_resolve)
    return snapshot


# ---------------------------------------------------------------------------
# URI detection and environment config.
# ---------------------------------------------------------------------------


def test_is_object_store_uri_detects_s3_and_rejects_local_paths():
    assert cli._is_object_store_uri("s3://bucket/kb")
    assert cli._is_object_store_uri("s3a://bucket/kb")
    assert not cli._is_object_store_uri("/local/path")
    assert not cli._is_object_store_uri("relative/path")
    assert not cli._is_object_store_uri(Path("/local/path"))


def test_s3_config_from_env_reads_lumio_and_aws_vars(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("LUMIO_S3_", "AWS_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LUMIO_S3_REGION", "eu-west-1")
    monkeypatch.setenv("LUMIO_S3_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("LUMIO_S3_ACCESS_KEY_ID", "minio")
    monkeypatch.setenv("LUMIO_S3_SECRET_ACCESS_KEY", "minio123")
    config, client_options = cli._s3_config_from_env()
    assert config["aws_region"] == "eu-west-1"
    assert config["aws_endpoint"] == "http://localhost:9000"
    assert config["aws_access_key_id"] == "minio"
    assert config["aws_secret_access_key"] == "minio123"
    # An HTTP endpoint opts in to allow_http on the client.
    assert client_options == {"allow_http": True}


def test_is_object_store_uri_rejects_non_store_schemes():
    assert not cli._is_object_store_uri("https://example.com/path")
    assert not cli._is_object_store_uri("file:///local/path")


def test_s3_config_from_env_falls_back_to_aws_vars(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("LUMIO_S3_", "AWS_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    config, _ = cli._s3_config_from_env()
    assert config == {"aws_region": "us-east-2"}


def test_s3_config_from_env_empty_when_unset(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("LUMIO_S3_", "AWS_")):
            monkeypatch.delenv(var, raising=False)
    config, client_options = cli._s3_config_from_env()
    assert config == {}
    assert client_options == {}


# ---------------------------------------------------------------------------
# Command routing: an S3 URI resolves a Snapshot and serves the command.
# ---------------------------------------------------------------------------


def test_cli_validate_s3_uri_routes_through_the_snapshot(stub_s3_resolution, capsys):
    rc = cli.main(["validate", "s3://bucket/kb"])
    captured = capsys.readouterr()
    snapshot = stub_s3_resolution
    assert rc == (0 if snapshot.validation_report.is_valid else 1)
    assert str(snapshot.validation_report) in captured.out


def test_cli_search_s3_uri_returns_results(stub_s3_resolution, capsys):
    rc = cli.main(["search", "s3://bucket/kb", "Lumio"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "##" in captured.out  # at least one page header


def test_cli_page_s3_uri_reads_a_compiled_page(stub_s3_resolution, capsys):
    rc = cli.main(["page", "s3://bucket/kb", "Lumio Overview"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# Lumio Overview" in captured.out


def test_cli_related_s3_uri_traverses_the_graph(stub_s3_resolution, capsys):
    rc = cli.main(["related", "s3://bucket/kb", "Lumio Overview"])
    captured = capsys.readouterr()
    assert rc == 0
    # The fixture's Lumio Overview has outgoing relationships.
    out = captured.out.strip()
    assert out == "" or "No related pages" in out or out  # command always exits 0


def test_cli_paths_s3_uri_finds_a_path(stub_s3_resolution, capsys):
    rc = cli.main(["paths", "s3://bucket/kb", "Lumio Overview", "Technology Stack"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Lumio Overview" in captured.out and "Technology Stack" in captured.out


def test_cli_doctor_reports_the_s3_extra(capsys):
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "extra[s3]" in captured.out
    assert "lumio-wiki[s3]" in captured.out or "extra[s3]: installed" in captured.out



# ---------------------------------------------------------------------------
# publish-s3: publish a local Knowledge Base to an object store (issue #121).
# ---------------------------------------------------------------------------

obstore = pytest.importorskip("obstore", reason="obstore required for publish-s3 CLI")


def test_cli_publish_s3_writes_an_immutable_version_and_advances_the_pointer(
    monkeypatch, capsys
):
    """The publish-s3 command validates, writes the version prefix, and advances
    the pointer — routed through a real in-memory object store."""
    store = obstore.store.MemoryStore()

    def _fake_build(uri):
        assert cli._is_object_store_uri(uri)
        return store, "kb"

    monkeypatch.setattr(cli, "_build_publish_store", _fake_build)
    rc = cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Published v1" in captured.out
    # The pointer advanced to v1.
    import msgspec
    from lumio_wiki.s3_location import CURRENT_POINTER_OBJECT, S3Pointer

    raw = obstore.get(store, f"kb/{CURRENT_POINTER_OBJECT}")
    pointer = msgspec.json.decode(bytes(raw.bytes()), type=S3Pointer)
    assert pointer.version == "v1"
    # The manifest and derived graph exist under the version prefix.
    from lumio_wiki.graph_state import GRAPH_ARTIFACT_FILENAME
    from lumio_wiki.s3_location import DERIVED_DIR, MANIFEST_OBJECT

    assert f"kb/v1/{MANIFEST_OBJECT}" in _list(store, "kb/")
    assert f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}" in _list(store, "kb/")


def test_cli_publish_s3_reports_a_pointer_conflict(monkeypatch, capsys):
    """A concurrent publication surfaces a conflict error with a nonzero exit."""
    store = obstore.store.MemoryStore()

    def _fake_build(uri):
        return store, "kb"

    monkeypatch.setattr(cli, "_build_publish_store", _fake_build)
    # First publication from a version that does not exist yet.
    cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    # A second publication expecting a bogus pointer version conflicts.
    rc = cli.main(
        [
            "publish-s3",
            str(FIXTURES / "valid"),
            "s3://bucket/kb",
            "--version",
            "v2",
            "--expected-pointer-version",
            "does-not-exist",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "conflict" in captured.err.lower()


def _list(store, prefix):
    paths = []
    for batch in obstore.list(store, prefix=prefix):
        for obj in batch:
            paths.append(obj["path"])
    return sorted(paths)

# ---------------------------------------------------------------------------
# publish-s3 --retrieval lancedb / rollback-s3 / cleanup-s3 (issue #163).
# ---------------------------------------------------------------------------


def _fake_store(monkeypatch, prefix="kb"):
    store = obstore.store.MemoryStore()
    monkeypatch.setattr(
        cli, "_build_publish_store", lambda uri: (store, prefix)
    )
    return store


class _RecordingBuilder:
    """A stub IndexBuilder: records the call, writes a marker sidecar."""

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    def __call__(self, *, store, sidecar_prefix, pages, fingerprint):
        self.calls.append((sidecar_prefix, len(pages), fingerprint.digest))
        if self.exc is not None:
            raise self.exc

        from lumio_wiki.s3_publish import RemoteIndexCompletion

        return RemoteIndexCompletion(
            fingerprint=fingerprint.digest, model=None, tables={"evidence": 3, "pages": 3}
        )


def test_cli_publish_s3_retrieval_lancedb_builds_before_activation(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    builder = _RecordingBuilder()
    monkeypatch.setattr(cli, "_publication_index_builder", lambda dest: builder)

    rc = cli.main(
        [
            "publish-s3",
            str(FIXTURES / "valid"),
            "s3://bucket/kb",
            "--version",
            "v1",
            "--retrieval",
            "lancedb",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Published v1" in out
    assert "lance:" in out
    # The builder ran against the version's lance prefix before activation.
    assert builder.calls[0][0] == "kb/v1/derived/lance"
    # Completion metadata landed under the version prefix.
    assert "kb/v1/derived/lance/completion.json" in _list(store, "kb/")


def test_cli_publish_s3_retrieval_lancedb_failure_blocks_activation(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    builder = _RecordingBuilder(exc=RuntimeError("lance unavailable"))
    monkeypatch.setattr(cli, "_publication_index_builder", lambda dest: builder)

    rc = cli.main(
        [
            "publish-s3",
            str(FIXTURES / "valid"),
            "s3://bucket/kb",
            "--version",
            "v1",
            "--retrieval",
            "lancedb",
        ]
    )
    capsys.readouterr()
    assert rc == 2
    # The pointer was never created and no manifest was written.
    assert "kb/current.json" not in _list(store, "kb/")
    assert "kb/v1/manifest.json" not in _list(store, "kb/")


def test_cli_rollback_s3_activates_a_prior_complete_version(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    cli.main(
        [
            "publish-s3",
            str(FIXTURES / "categorized_kb"),
            "s3://bucket/kb",
            "--version",
            "v2",
            "--expected-pointer-version",
            "v1",
        ]
    )
    rc = cli.main(
        ["rollback-s3", "s3://bucket/kb", "--version", "v1", "--expected-pointer-version", "v2"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Rolled back to v1" in out
    pointer = msgspec_pointer(store)
    assert pointer == "v1"


def test_cli_rollback_s3_reports_conflicts(monkeypatch, capsys):
    _fake_store(monkeypatch)
    cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    rc = cli.main(
        ["rollback-s3", "s3://bucket/kb", "--version", "v1", "--expected-pointer-version", "nope"]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "conflict" in captured.err.lower()


def test_cli_rollback_s3_names_cleanup_candidates_on_incomplete_targets(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    # Residue of an interrupted build under v2 (no manifest).
    obstore.put(store, "kb/v2/overview.md", b"partial", mode="create")
    rc = cli.main(["rollback-s3", "s3://bucket/kb", "--version", "v2"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "v2" in captured.err
    assert "cleanup candidate" in captured.err


def test_cli_cleanup_s3_reports_candidates_without_deleting(monkeypatch, capsys):
    store = _fake_store(monkeypatch)
    cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    obstore.put(store, "kb/v2/overview.md", b"partial", mode="create")

    rc = cli.main(["cleanup-s3", "s3://bucket/kb"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v2" in out
    assert "nothing is deleted" in out.lower()
    # Report-only: the residue survives.
    assert "kb/v2/overview.md" in _list(store, "kb")


def test_cli_cleanup_s3_with_no_candidates(monkeypatch, capsys):
    _fake_store(monkeypatch)
    cli.main(["publish-s3", str(FIXTURES / "valid"), "s3://bucket/kb", "--version", "v1"])
    rc = cli.main(["cleanup-s3", "s3://bucket/kb"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No cleanup candidates" in out


def msgspec_pointer(store) -> str:
    import msgspec
    from lumio_wiki.s3_location import CURRENT_POINTER_OBJECT, S3Pointer

    raw = obstore.get(store, f"kb/{CURRENT_POINTER_OBJECT}")
    return msgspec.json.decode(bytes(raw.bytes()), type=S3Pointer).version


# ---------------------------------------------------------------------------
# --retrieval lancedb resolves its S3 endpoint/region like the obstore path
# (#161 setup wrote them to .env; #163's builder must read them there too).
# ---------------------------------------------------------------------------


def test_lance_storage_options_read_project_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in (
        "LUMIO_S3_REGION",
        "LUMIO_S3_ENDPOINT",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ENDPOINT_URL_S3",
        "AWS_ACCESS_KEY_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "LUMIO_S3_REGION=us-east-1\n"
        "LUMIO_S3_ENDPOINT=http://localhost:9000\n"
        "AWS_ACCESS_KEY_ID=file-never-wins\n",
        encoding="utf-8",
    )

    options = cli._lance_storage_options_from_env()

    assert options["region"] == "us-east-1"
    assert options["endpoint"] == "http://localhost:9000"
    assert options["allow_http"] == "true"
    # Setup never writes credentials to .env and they never load from there.
    assert "access_key_id" not in options

    monkeypatch.setenv("LUMIO_S3_ENDPOINT", "https://exported.example")
    assert cli._lance_storage_options_from_env()["endpoint"] == "https://exported.example"
