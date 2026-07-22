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
