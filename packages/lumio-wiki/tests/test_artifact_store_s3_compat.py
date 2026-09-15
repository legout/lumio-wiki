"""S3-compatible integration coverage for private Source Artifacts (issue #164).

Proves the S3 artifact adapter's real-endpoint behavior: content-verified
retention, the private Source Binding Manifest written before activation and
outside the public Published Version prefix, short-lived exact-object signed
GET URLs, and distinct access-denied behavior for unauthorized credentials
(ADR-0020).

Skips the whole module unless ``LUMIO_S3_ENDPOINT`` is configured, mirroring
the other S3-compatible suites. Cross-role policy coverage additionally uses
separately provisioned, test-only credentials and skips unless every role is
configured.
"""

from __future__ import annotations

import json
import os as _os
import shutil
import time
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from pathlib import Path

import msgspec  # type: ignore[import-not-found]
import pytest  # type: ignore[import-not-found]
from lumio_wiki.artifact_store import (
    S3ArtifactStore,
    SourceBindingEntry,
    SourceBindingManifest,
    activation_binding_hook,
    artifact_content_hash,
    retain_artifact,
)
from lumio_wiki.ingest import IngestStore
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.s3_publish import publish_s3_version

obstore = pytest.importorskip("obstore", reason="obstore required for S3-compatible integration")

if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping Source Artifact S3-compatible tests",
        allow_module_level=True,
    )

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"
RAW = b"%PDF-1.4 s3_compat source artifact journey bytes"

_ROLE_CREDENTIAL_ENV = {
    "kb-reader": (
        "LUMIO_S3_TEST_KB_READER_ACCESS_KEY_ID",
        "LUMIO_S3_TEST_KB_READER_SECRET_ACCESS_KEY",
    ),
    "publisher": (
        "LUMIO_S3_TEST_PUBLISHER_ACCESS_KEY_ID",
        "LUMIO_S3_TEST_PUBLISHER_SECRET_ACCESS_KEY",
    ),
    "source-writer": (
        "LUMIO_S3_TEST_SOURCE_WRITER_ACCESS_KEY_ID",
        "LUMIO_S3_TEST_SOURCE_WRITER_SECRET_ACCESS_KEY",
    ),
    "source-inspector": (
        "LUMIO_S3_TEST_SOURCE_INSPECTOR_ACCESS_KEY_ID",
        "LUMIO_S3_TEST_SOURCE_INSPECTOR_SECRET_ACCESS_KEY",
    ),
}
_ROLE_CREDENTIAL_VARIABLES = tuple(
    variable for pair in _ROLE_CREDENTIAL_ENV.values() for variable in pair
)
_MISSING_ROLE_CREDENTIALS = tuple(
    variable for variable in _ROLE_CREDENTIAL_VARIABLES if not _os.environ.get(variable)
)


def _s3_config():
    endpoint = _os.environ["LUMIO_S3_ENDPOINT"]
    config = {
        "aws_region": _os.environ.get("LUMIO_S3_REGION", "us-east-1"),
        "aws_endpoint": endpoint,
        "aws_access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    client_options: dict[str, object] = {}
    if endpoint.startswith("http://"):
        client_options["allow_http"] = True
    return config, client_options


def _bucket() -> str:
    return _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")


def _root_store():
    config, client_options = _s3_config()
    return obstore.store.from_url(f"s3://{_bucket()}", config=config, client_options=client_options)


@pytest.fixture()
def prefixes():
    """A unique KB prefix and artifact prefix per test; best-effort cleanup."""
    store = _root_store()
    run = uuid.uuid4().hex
    kb_prefix = f"art-it/{run}/kb"
    artifact_prefix = f"art-it/{run}/src"
    yield store, kb_prefix, artifact_prefix
    try:  # pragma: no cover - cleanup is best-effort
        for prefix in (kb_prefix, artifact_prefix):
            for batch in obstore.list(store, prefix=prefix):
                for obj in batch:
                    obstore.delete(store, obj["path"])
    except Exception:  # pragma: no cover
        pass


def _artifact_store(store, artifact_prefix: str) -> S3ArtifactStore:
    return S3ArtifactStore(store, artifact_prefix)


def test_s3_compat_artifact_roundtrip_verifies_digest(prefixes):
    store, _, artifact_prefix = prefixes
    artifacts = _artifact_store(store, artifact_prefix)
    digest = artifact_content_hash(RAW)
    artifacts.put_artifact(
        source_id="s3_compat-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    assert artifacts.get_artifact(source_id="s3_compat-report", content_hash=digest) == RAW
    assert artifacts.artifact_exists(source_id="s3_compat-report", content_hash=digest)

    # Corrupt the stored object behind the adapter (delete + different bytes
    # under the same physical key): digest verification must reject it.
    key = f"{artifact_prefix}/artifacts/s3_compat-report/{digest}"
    obstore.delete(store, key)
    obstore.put(store, key, b"tampered replacement bytes")
    assert not artifacts.artifact_exists(source_id="s3_compat-report", content_hash=digest)
    with pytest.raises(Exception, match="digest"):
        artifacts.get_artifact(source_id="s3_compat-report", content_hash=digest)

    assert artifacts.delete_artifact(source_id="s3_compat-report", content_hash=digest)
    assert not artifacts.artifact_exists(source_id="s3_compat-report", content_hash=digest)


def test_s3_compat_signed_url_is_exact_object_short_lived_and_secret_free(prefixes):
    store, _, artifact_prefix = prefixes
    artifacts = _artifact_store(store, artifact_prefix)
    digest = artifact_content_hash(RAW)
    artifacts.put_artifact(
        source_id="s3_compat-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    assert artifacts.supports_signing()
    url = artifacts.signed_get_url(
        source_id="s3_compat-report", content_hash=digest, expires_in=timedelta(seconds=120)
    )
    # No credential material rides the URL: it is a temporary bearer grant.
    secret = _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"]
    assert secret not in url
    assert f"{artifact_prefix}/artifacts/s3_compat-report/{digest}" in url
    # An anonymous fetch returns the exact artifact bytes.
    assert urllib.request.urlopen(url, timeout=10).read() == RAW

    # The signature authorizes ONE exact object, not the prefix.
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url.replace(digest, "0" * 64), timeout=10)

    # And it expires: a one-second grant is refused shortly after.
    brief = artifacts.signed_get_url(
        source_id="s3_compat-report", content_hash=digest, expires_in=timedelta(seconds=1)
    )
    time.sleep(2.5)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(brief, timeout=10)


def _authored_page(source_id: str) -> str:
    return (
        "---\n"
        f'title: "S3 Compat Artifact Page"\n'
        "aliases: []\n"
        'tags:\n  - "artifacts"\n'
        'summary: "Authored for the S3-compatible artifact journey."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} source"\n'
        "synthetic: false\n"
        "---\n\n"
        "# S3 Compat Artifact Page\n\n"
        "Body authored from the original source.\n"
    )


def test_s3_compat_publication_binds_artifacts_outside_public_prefix(tmp_path, prefixes):
    import shutil

    store, kb_prefix, artifact_prefix = prefixes
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)

    ingest = IngestStore(tmp_path / "ingest")
    import lumio_wiki as lw

    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    artifacts = _artifact_store(store, artifact_prefix)
    # Required retention evaluates EVERY referenced non-synthetic source
    # (#164): backfill the fixture pages' source ids too.
    for page in kb.pages:
        for source in page.sources:
            if not source.id:
                continue
            fixture_bytes = f"fixture artifact for {source.id}".encode()
            ingest.source_registry.register_or_reuse(
                source.id, fixture_bytes, filename=None, content_type=None
            )
            retain_artifact(
                artifacts,
                ingest.source_registry,
                source_id=source.id,
                raw_bytes=fixture_bytes,
                content_type="text/plain",
                filename=f"{source.id}.txt",
            )
    pipeline = ProposalPipeline(kb, store=ingest, artifact_store=artifacts)
    proposal = pipeline.managed_ingest(
        RAW, "application/pdf", "report.pdf", "s3_compat-report", _authored_page("s3_compat-report")
    )
    assert pipeline.publish(proposal.id).status == "published"

    hook = activation_binding_hook(
        artifact_store=artifacts,
        registry=ingest.source_registry,
        required=True,
    )
    manifest = publish_s3_version(
        store, kb_prefix, source_root=root, version="v1", before_activation=hook
    )
    assert manifest.version == "v1"

    # The private binding manifest binds the exact Source Version, and lives
    # ONLY under the artifact prefix.
    binding = msgspec.json.decode(artifacts.get_binding_manifest("v1"), type=SourceBindingManifest)
    entry = next(e for e in binding.entries if e.source_id == "s3_compat-report")
    assert entry.content_hash == artifact_content_hash(RAW)
    assert entry.filename == "report.pdf"
    assert artifacts.list_binding_versions() == ["v1"]

    public_keys = sorted(
        obj["path"] for batch in obstore.list(store, prefix=kb_prefix) for obj in batch
    )
    assert public_keys
    assert not any("artifacts/" in key or "bindings/" in key for key in public_keys)
    for key in public_keys:
        blob = bytes(obstore.get(store, key).bytes())
        assert RAW not in blob

    artifact = obstore.get(
        store,
        f"{artifact_prefix}/artifacts/s3_compat-report/{artifact_content_hash(RAW)}",
    )
    assert artifact.attributes["filename"] == "report.pdf"
    assert artifact.attributes["content_type"] == "application/pdf"


# ---------------------------------------------------------------------------
# Cross-role authorization boundaries (issue #164 AC).
# ---------------------------------------------------------------------------


def _role_store(role: str):
    access_key_variable, secret_key_variable = _ROLE_CREDENTIAL_ENV[role]
    config, client_options = _s3_config()
    role_config = dict(config)
    role_config["aws_access_key_id"] = _os.environ[access_key_variable]
    role_config["aws_secret_access_key"] = _os.environ[secret_key_variable]
    return obstore.store.from_url(
        f"s3://{_bucket()}", config=role_config, client_options=client_options
    )


@pytest.mark.skipif(
    bool(_MISSING_ROLE_CREDENTIALS),
    reason=(
        "cross-role S3 policy test requires all separately provisioned test role "
        "credentials: " + ", ".join(_ROLE_CREDENTIAL_VARIABLES)
    ),
)
def test_s3_compat_cross_role_authorization_boundaries(prefixes):
    """Prove the distinct read/write policy matrix with provisioned roles."""
    store, kb_prefix, artifact_prefix = prefixes
    public_key = f"{kb_prefix}/page.md"
    artifact_key = f"{artifact_prefix}/artifacts/report/{artifact_content_hash(RAW)}"

    # Seed through the root integration-test credential. Role credentials are
    # supplied by the endpoint operator; this suite never provisions policy.
    obstore.put(store, public_key, b"published page")
    obstore.put(store, artifact_key, RAW, mode="create")

    reader = _role_store("kb-reader")
    assert bytes(obstore.get(reader, public_key).bytes()) == b"published page"
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.get(reader, artifact_key)
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.put(reader, f"{kb_prefix}/other.md", b"x")

    publisher = _role_store("publisher")
    assert bytes(obstore.get(publisher, public_key).bytes()) == b"published page"
    obstore.put(publisher, f"{kb_prefix}/v2/pages/new-page.md", b"published v2 page")
    obstore.put(publisher, f"{artifact_prefix}/bindings/v2.json", b"{}")
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.get(publisher, artifact_key)

    writer = _role_store("source-writer")
    obstore.put(writer, f"{artifact_prefix}/artifacts/queue/new", b"queued bytes")
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.get(writer, artifact_key)
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.put(writer, f"{kb_prefix}/unpublished.md", b"x")

    inspector = _role_store("source-inspector")
    assert bytes(obstore.get(inspector, artifact_key).bytes()) == RAW
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.put(inspector, f"{artifact_prefix}/artifacts/unauthorized", b"x")
    with pytest.raises(obstore.exceptions.PermissionDeniedError):
        obstore.get(inspector, public_key)


# ---------------------------------------------------------------------------
# Issue #165: authorized inspect/fetch/link through the CLI over a real S3
# Source Artifact Store, and the distinct access-denied outcome for
# unauthorized (KB Reader-only style) credentials.
# ---------------------------------------------------------------------------


def _seed_v165_binding(store, artifact_prefix: str) -> str:
    """Retain the artifact and write the v165 Source Binding Manifest."""
    artifacts = _artifact_store(store, artifact_prefix)
    digest = artifact_content_hash(RAW)
    artifacts.put_artifact(
        source_id="s3_compat-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    manifest = SourceBindingManifest(
        published_version="v165",
        fingerprint="fp",
        created_at="2026-08-21T00:00:00Z",
        entries=[
            SourceBindingEntry(
                page_title="S3 Compat Artifact Page",
                source_id="s3_compat-report",
                content_hash=digest,
                content_type="application/pdf",
                filename="report.pdf",
                size=len(RAW),
            )
        ],
    )
    artifacts.put_binding_manifest("v165", msgspec.json.encode(manifest))
    return digest


def test_s3_compat_cli_source_link_downloads_same_digest_and_fetch_is_byte_exact(
    tmp_path, prefixes, monkeypatch, capsys
):
    from lumio_wiki.cli import main

    store, _, artifact_prefix = prefixes
    digest = _seed_v165_binding(store, artifact_prefix)
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURES, kb)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", f"s3://{_bucket()}/{artifact_prefix}")

    rc = main(
        [
            "source",
            "link",
            str(kb),
            "--source-id",
            "s3_compat-report",
            "--published-version",
            "v165",
            "--expires",
            "30s",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    url = out.splitlines()[0]
    assert url.startswith("http")
    assert "bearer secret" in out
    # No credential material rides the URL (temporary bearer grant only).
    assert _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"] not in url
    # The signed link downloads the same digest as the retained artifact.
    assert urllib.request.urlopen(url, timeout=10).read() == RAW
    # The signature authorizes exactly this object: a URL pointing at any
    # other object under the same prefix is refused.
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url.replace(digest, "0" * 64), timeout=10)

    out_file = tmp_path / "fetched.pdf"
    rc = main(
        [
            "source",
            "fetch",
            str(kb),
            "--source-id",
            "s3_compat-report",
            "--published-version",
            "v165",
            "--output",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.read_bytes() == RAW


def test_s3_compat_cli_unauthorized_credentials_get_distinct_access_denied(
    tmp_path, prefixes, monkeypatch, capsys
):
    from lumio_wiki.cli import main

    store, _, artifact_prefix = prefixes
    _seed_v165_binding(store, artifact_prefix)
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURES, kb)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", f"s3://{_bucket()}/{artifact_prefix}")
    # A credential that may not read the private artifact prefix (the KB
    # Reader situation) surfaces as the distinct access-denied outcome —
    # never absence, never a traceback, never a leaked object key.
    monkeypatch.setenv("LUMIO_S3_ACCESS_KEY_ID", _os.environ["LUMIO_S3_ACCESS_KEY_ID"])
    monkeypatch.setenv("LUMIO_S3_SECRET_ACCESS_KEY", "wrong-secret-on-purpose")

    for command in ("inspect", "fetch", "link"):
        argv = [
            "source",
            command,
            str(kb),
            "--source-id",
            "s3_compat-report",
            "--published-version",
            "v165",
        ]
        if command == "fetch":
            argv += ["--output", str(tmp_path / "out.pdf")]
        rc = main(argv)
        assert rc == 1, command
        err = capsys.readouterr().err
        assert "access denied" in err, command
        assert artifact_prefix not in err


def test_s3_compat_cli_source_resolve_identity_and_denial(tmp_path, prefixes, monkeypatch, capsys):
    """`source resolve` over a real S3 binding manifest (issue #176).

    Authorized: a page-title query resolves to the bound Source identity
    with verified availability, and --json emits an agent-selectable result.
    Unauthorized: the same command surfaces the distinct access-denied
    outcome and discloses neither the private prefix nor artifact existence.
    """
    from lumio_wiki.cli import main

    store, _, artifact_prefix = prefixes
    _seed_v165_binding(store, artifact_prefix)
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURES, kb)
    (kb / "artifact_page.md").write_text(
        "---\n"
        'title: "S3 Compat Artifact Page"\n'
        "aliases: []\n"
        "tags: []\n"
        'summary: "Authored from the s3_compat-report Knowledge Source."\n'
        'lifecycle: "approved"\n'
        'visibility: "internal"\n'
        "sources:\n"
        '  - id: "s3_compat-report"\n'
        '    title: "S3-compatible report source"\n'
        "synthetic: false\n"
        "---\n\n"
        "# S3 Compat Artifact Page\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", f"s3://{_bucket()}/{artifact_prefix}")

    # Authorized: resolve by page title through the v165 binding manifest.
    rc = main(
        [
            "source",
            "resolve",
            str(kb),
            "S3 Compat Artifact Page",
            "--published-version",
            "v165",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "source_id:       s3_compat-report" in out
    assert "matched_by:      canonical-title" in out
    assert "bound_to:        published version v165 (Source Binding Manifest)" in out
    assert "availability:    retained (digest and size verified)" in out
    assert "http" not in out  # a resolution never issues a signed URL

    # Authorized: --json emits one machine-selectable object.
    assert (
        main(
            [
                "source",
                "resolve",
                str(kb),
                "s3_compat-report",
                "--published-version",
                "v165",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "resolved"
    assert payload["source_id"] == "s3_compat-report"

    # Unauthorized (a credential denied the private prefix): distinct
    # access-denied outcome; never absence, never a leaked object key, and
    # never a disclosure of whether a private artifact exists.
    monkeypatch.setenv("LUMIO_S3_ACCESS_KEY_ID", _os.environ["LUMIO_S3_ACCESS_KEY_ID"])
    monkeypatch.setenv("LUMIO_S3_SECRET_ACCESS_KEY", "wrong-secret-on-purpose")
    rc = main(
        [
            "source",
            "resolve",
            str(kb),
            "S3 Compat Artifact Page",
            "--published-version",
            "v165",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "access denied" in err
    assert artifact_prefix not in err
    assert "s3_compat-report" not in err


def test_s3_compat_url_ingest_retains_fetched_bytes_as_artifact(tmp_path, prefixes):
    """issue #178: URL ingestion retains the FETCHED bytes as a private artifact.

    The fetch runs hermetically against a local server through the documented
    ``--allow-http``/``--allow-private-destination`` escape hatch; the
    retained artifact, binding, and provenance all describe the exact fetched
    content hash — never a page-body mutation of it.
    """
    import shutil
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import lumio_wiki as lw
    from lumio_wiki.url_fetch import UrlFetchPolicy, fetch_url

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html><body>s3_compat url source page</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        url = f"http://{host}:{port}/page.html"
        result = fetch_url(
            url,
            UrlFetchPolicy(allow_http=True, allow_private_destinations=True),
        )
    finally:
        server.shutdown()

    store, _, artifact_prefix = prefixes
    artifacts = _artifact_store(store, artifact_prefix)
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report

    ingest = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store=ingest, artifact_store=artifacts)
    proposal = pipeline.managed_ingest(
        result.body,
        result.media_type,
        result.filename,
        "s3_compat-url-report",
        _authored_page("s3_compat-url-report"),
        source_url=result.final_url,
        retrieved_at=result.retrieved_at,
    )
    assert proposal.provenance.source_url == url
    assert proposal.provenance.source_hash == artifact_content_hash(result.body)

    # The exact fetched bytes are privately retained and digest-verified.
    retained = artifacts.get_artifact(
        source_id="s3_compat-url-report", content_hash=artifact_content_hash(result.body)
    )
    assert bytes(retained) == result.body

    # The published page is the AUTHORED markdown, and the public prefix
    # never sees the fetched bytes.
    assert pipeline.publish(proposal.id).status == "published"
    published = (root / "s3_compat_artifact_page.md").read_text(encoding="utf-8")
    assert "Body authored from the original source." in published
    assert "s3_compat url source page" not in published
