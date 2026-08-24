"""MinIO integration coverage for private Source Artifacts (issue #164).

Proves the S3 artifact adapter's real-endpoint behavior: content-verified
retention, the private Source Binding Manifest written before activation and
outside the public Published Version prefix, short-lived exact-object signed
GET URLs, and cross-role credential denial with separate KB Reader / source
writer / source inspector principals (ADR-0020).

Skips the whole module unless ``LUMIO_S3_ENDPOINT`` is configured, mirroring
the other MinIO suites. The cross-role test additionally needs the MinIO
admin client ``mc`` (from PATH or via ``LUMIO_MINIO_MC``) and skips without
it.
"""

from __future__ import annotations

import json
import os as _os
import shutil
import subprocess
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

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")

if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping Source Artifact MinIO tests",
        allow_module_level=True,
    )

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"
RAW = b"%PDF-1.4 minio source artifact journey bytes"


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


def test_minio_artifact_roundtrip_verifies_digest(prefixes):
    store, _, artifact_prefix = prefixes
    artifacts = _artifact_store(store, artifact_prefix)
    digest = artifact_content_hash(RAW)
    artifacts.put_artifact(
        source_id="minio-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    assert artifacts.get_artifact(source_id="minio-report", content_hash=digest) == RAW
    assert artifacts.artifact_exists(source_id="minio-report", content_hash=digest)

    # Corrupt the stored object behind the adapter (delete + different bytes
    # under the same physical key): digest verification must reject it.
    key = f"{artifact_prefix}/artifacts/minio-report/{digest}"
    obstore.delete(store, key)
    obstore.put(store, key, b"tampered replacement bytes")
    assert not artifacts.artifact_exists(source_id="minio-report", content_hash=digest)
    with pytest.raises(Exception, match="digest"):
        artifacts.get_artifact(source_id="minio-report", content_hash=digest)

    assert artifacts.delete_artifact(source_id="minio-report", content_hash=digest)
    assert not artifacts.artifact_exists(source_id="minio-report", content_hash=digest)


def test_minio_signed_url_is_exact_object_short_lived_and_secret_free(prefixes):
    store, _, artifact_prefix = prefixes
    artifacts = _artifact_store(store, artifact_prefix)
    digest = artifact_content_hash(RAW)
    artifacts.put_artifact(
        source_id="minio-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    assert artifacts.supports_signing()
    url = artifacts.signed_get_url(
        source_id="minio-report", content_hash=digest, expires_in=timedelta(seconds=120)
    )
    # No credential material rides the URL: it is a temporary bearer grant.
    secret = _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"]
    assert secret not in url
    assert f"{artifact_prefix}/artifacts/minio-report/{digest}" in url
    # An anonymous fetch returns the exact artifact bytes.
    assert urllib.request.urlopen(url, timeout=10).read() == RAW

    # The signature authorizes ONE exact object, not the prefix.
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url.replace(digest, "0" * 64), timeout=10)

    # And it expires: a one-second grant is refused shortly after.
    brief = artifacts.signed_get_url(
        source_id="minio-report", content_hash=digest, expires_in=timedelta(seconds=1)
    )
    time.sleep(2.5)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(brief, timeout=10)


def _authored_page(source_id: str) -> str:
    return (
        "---\n"
        f'title: "MinIO Artifact Page"\n'
        "aliases: []\n"
        'tags:\n  - "artifacts"\n'
        'summary: "Authored for the MinIO artifact journey."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} source"\n'
        "synthetic: false\n"
        "---\n\n"
        "# MinIO Artifact Page\n\n"
        "Body authored from the original source.\n"
    )


def test_minio_publication_binds_artifacts_outside_public_prefix(tmp_path, prefixes):
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
        RAW, "application/pdf", "report.pdf", "minio-report", _authored_page("minio-report")
    )
    assert pipeline.publish(proposal.id).status == "published"

    hook = activation_binding_hook(
        artifact_store=artifacts,
        registry=ingest.source_registry,
        source_root=root,
        required=True,
    )
    manifest = publish_s3_version(
        store, kb_prefix, source_root=root, version="v1", before_activation=hook
    )
    assert manifest.version == "v1"

    # The private binding manifest binds the exact Source Version, and lives
    # ONLY under the artifact prefix.
    binding = msgspec.json.decode(artifacts.get_binding_manifest("v1"), type=SourceBindingManifest)
    entry = next(e for e in binding.entries if e.source_id == "minio-report")
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

    # Stored response metadata (ADR-0020): the safe filename and content
    # type ride the artifact as object user metadata so delivery tooling can
    # force an attachment download without guessing. Verified through the
    # admin client when available.
    mc = _mc_command()
    if mc is not None:
        alias = f"lumio-meta-{uuid.uuid4().hex[:8]}"
        _mc(
            "alias",
            "set",
            alias,
            _os.environ["LUMIO_S3_ENDPOINT"],
            _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
            _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
        )
        try:
            stat_key = (
                f"{alias}/{_bucket()}/{artifact_prefix}/artifacts/"
                f"minio-report/{artifact_content_hash(RAW)}"
            )
            stat = subprocess.run(
                [*mc, "stat", "--json", stat_key],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
            assert "X-Amz-Meta-Filename" in stat
            assert "report.pdf" in stat
            # S3 normalizes the attribute key's underscores ("content_type"
            # -> "Content_type"); the VALUE proves the safe media type rides
            # the object.
            assert "application/pdf" in stat
        finally:
            subprocess.run([*mc, "alias", "rm", alias], capture_output=True, timeout=30)


# ---------------------------------------------------------------------------
# Cross-role denial with separate credentials (issue #164 AC).
# ---------------------------------------------------------------------------


def _mc_command() -> list[str] | None:
    """Resolve the MinIO admin client: PATH, then the LUMIO_MINIO_MC template."""
    if shutil.which("mc"):
        return ["mc"]
    template = _os.environ.get("LUMIO_MINIO_MC")
    if template:
        return template.split()
    return None


def _mc(*args: str) -> None:
    command = _mc_command()
    assert command is not None
    subprocess.run([*command, *args], check=True, capture_output=True, text=True, timeout=60)


def _policy_document(bucket: str, actions: list[str], prefix: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": actions,
                "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"],
            }
        ],
    }


def _publisher_policy_document(bucket: str, kb_prefix: str, artifact_prefix: str) -> dict:
    """The Maintainer/publisher role (issue #166): write the public KB
    (immutable versions + CAS pointer) and the private Source Binding
    Manifests written during publication — but never READ private Source
    Artifacts (that is the source inspector's distinct role)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/{kb_prefix}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/{artifact_prefix}/*"],
            },
        ],
    }


def test_minio_cross_role_denial(prefixes):
    mc = _mc_command()
    if mc is None:  # pragma: no cover - environment-dependent skip
        pytest.skip("mc not available (set LUMIO_MINIO_MC or install mc)")

    assert mc is not None  # narrowed here; pytest.skip above is NoReturn
    store, kb_prefix, artifact_prefix = prefixes
    bucket = _bucket()
    run = uuid.uuid4().hex[:8]
    alias = f"lumio-it-{run}"
    endpoint = _os.environ["LUMIO_S3_ENDPOINT"]

    users: dict[str, tuple[str, str]] = {
        "kb-reader": (f"kb-reader-{run}", uuid.uuid4().hex),
        "publisher": (f"publisher-{run}", uuid.uuid4().hex),
        "src-writer": (f"src-writer-{run}", uuid.uuid4().hex),
        "src-inspector": (f"src-inspector-{run}", uuid.uuid4().hex),
    }
    policies = {
        "kb-reader": _policy_document(bucket, ["s3:GetObject"], kb_prefix),
        "publisher": _publisher_policy_document(bucket, kb_prefix, artifact_prefix),
        "src-writer": _policy_document(bucket, ["s3:PutObject"], artifact_prefix),
        "src-inspector": _policy_document(bucket, ["s3:GetObject"], artifact_prefix),
    }
    try:
        _mc(
            "alias",
            "set",
            alias,
            endpoint,
            _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
            _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
        )
        for role, (user, secret) in users.items():
            policy_name = f"lumio-art-it-{role}-{run}"
            document = json.dumps(policies[role])
            subprocess.run(
                [*mc, "admin", "policy", "create", alias, policy_name, "/dev/stdin"],
                input=document,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            _mc("admin", "user", "add", alias, user, secret)
            _mc("admin", "policy", "attach", alias, policy_name, "--user", user)

        # Seed content: a public KB object and a retained artifact.
        obstore.put(store, f"{kb_prefix}/page.md", b"published page")
        artifact_key = f"{artifact_prefix}/artifacts/report/{artifact_content_hash(RAW)}"
        obstore.put(store, artifact_key, RAW, mode="create")

        def _client(role: str):
            _, secret = users[role]
            config, client_options = _s3_config()
            config = dict(config)
            config["aws_access_key_id"] = users[role][0]
            config["aws_secret_access_key"] = secret
            return obstore.store.from_url(
                f"s3://{bucket}", config=config, client_options=client_options
            )

        # KB Reader reads the public Knowledge Base but is denied artifacts.
        reader = _client("kb-reader")
        assert bytes(obstore.get(reader, f"{kb_prefix}/page.md").bytes()) == b"published page"
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.get(reader, artifact_key)
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.put(reader, f"{kb_prefix}/other.md", b"x")

        # Source writer uploads artifacts but cannot read anything back.
        writer = _client("src-writer")
        obstore.put(writer, f"{artifact_prefix}/artifacts/queue/new", b"queued bytes")
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.get(writer, artifact_key)
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.put(writer, f"{kb_prefix}/evil.md", b"x")

        # Source inspector reads artifacts but cannot write anywhere.
        inspector = _client("src-inspector")
        assert bytes(obstore.get(inspector, artifact_key).bytes()) == RAW
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.put(inspector, f"{artifact_prefix}/artifacts/evil", b"x")
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.get(inspector, f"{kb_prefix}/page.md")

        # Publisher (issue #166): writes new immutable versions and the CAS
        # pointer under the public prefix plus the private binding manifests
        # written during publication, and reads the public KB — but is DENIED
        # reading private Source Artifacts (inspection is a distinct role).
        publisher = _client("publisher")
        assert bytes(obstore.get(publisher, f"{kb_prefix}/page.md").bytes()) == b"published page"
        obstore.put(publisher, f"{kb_prefix}/v2/pages/new-page.md", b"published v2 page")
        obstore.put(publisher, f"{artifact_prefix}/bindings/v2.json", b"{}")
        with pytest.raises(obstore.exceptions.PermissionDeniedError):
            obstore.get(publisher, artifact_key)
    finally:
        try:  # pragma: no cover - cleanup is best-effort
            for role, (user, _secret) in users.items():
                _mc("admin", "user", "remove", alias, user)
                _mc("admin", "policy", "remove", alias, f"lumio-art-it-{role}-{run}")
        except Exception:
            pass


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
        source_id="minio-report",
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
                page_title="MinIO Artifact Page",
                source_id="minio-report",
                content_hash=digest,
                content_type="application/pdf",
                filename="report.pdf",
                size=len(RAW),
            )
        ],
    )
    artifacts.put_binding_manifest("v165", msgspec.json.encode(manifest))
    return digest


def test_minio_cli_source_link_downloads_same_digest_and_fetch_is_byte_exact(
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
            "source", "link", str(kb),
            "--source-id", "minio-report",
            "--published-version", "v165",
            "--expires", "30s",
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
            "source", "fetch", str(kb),
            "--source-id", "minio-report",
            "--published-version", "v165",
            "--output", str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.read_bytes() == RAW


def test_minio_cli_unauthorized_credentials_get_distinct_access_denied(
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
            "source", command, str(kb),
            "--source-id", "minio-report",
            "--published-version", "v165",
        ]
        if command == "fetch":
            argv += ["--output", str(tmp_path / "out.pdf")]
        rc = main(argv)
        assert rc == 1, command
        err = capsys.readouterr().err
        assert "access denied" in err, command
        assert artifact_prefix not in err


def test_minio_cli_source_resolve_identity_and_denial(
    tmp_path, prefixes, monkeypatch, capsys
):
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
        'title: "MinIO Artifact Page"\n'
        "aliases: []\n"
        'tags: []\n'
        'summary: "Authored from the minio-report Knowledge Source."\n'
        'lifecycle: "approved"\n'
        'visibility: "internal"\n'
        "sources:\n"
        '  - id: "minio-report"\n'
        '    title: "MinIO report source"\n'
        "synthetic: false\n"
        "---\n\n"
        "# MinIO Artifact Page\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", f"s3://{_bucket()}/{artifact_prefix}")

    # Authorized: resolve by page title through the v165 binding manifest.
    rc = main(
        [
            "source", "resolve", str(kb),
            "MinIO Artifact Page",
            "--published-version", "v165",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "source_id:       minio-report" in out
    assert "matched_by:      canonical-title" in out
    assert "bound_to:        published version v165 (Source Binding Manifest)" in out
    assert "availability:    retained (digest and size verified)" in out
    assert "http" not in out  # a resolution never issues a signed URL

    # Authorized: --json emits one machine-selectable object.
    assert (
        main(
            [
                "source", "resolve", str(kb),
                "minio-report",
                "--published-version", "v165",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "resolved"
    assert payload["source_id"] == "minio-report"

    # Unauthorized (a credential denied the private prefix): distinct
    # access-denied outcome; never absence, never a leaked object key, and
    # never a disclosure of whether a private artifact exists.
    monkeypatch.setenv("LUMIO_S3_ACCESS_KEY_ID", _os.environ["LUMIO_S3_ACCESS_KEY_ID"])
    monkeypatch.setenv("LUMIO_S3_SECRET_ACCESS_KEY", "wrong-secret-on-purpose")
    rc = main(
        [
            "source", "resolve", str(kb),
            "MinIO Artifact Page",
            "--published-version", "v165",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "access denied" in err
    assert artifact_prefix not in err
    assert "minio-report" not in err


def test_minio_url_ingest_retains_fetched_bytes_as_artifact(tmp_path, prefixes):
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
            body = b"<html><body>minio url source page</body></html>"
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
        "minio-url-report",
        _authored_page("minio-url-report"),
        source_url=result.final_url,
        retrieved_at=result.retrieved_at,
    )
    assert proposal.provenance.source_url == url
    assert proposal.provenance.source_hash == artifact_content_hash(result.body)

    # The exact fetched bytes are privately retained and digest-verified.
    retained = artifacts.get_artifact(
        source_id="minio-url-report", content_hash=artifact_content_hash(result.body)
    )
    assert bytes(retained) == result.body

    # The published page is the AUTHORED markdown, and the public prefix
    # never sees the fetched bytes.
    assert pipeline.publish(proposal.id).status == "published"
    published = (root / "minio_artifact_page.md").read_text(encoding="utf-8")
    assert "Body authored from the original source." in published
    assert "minio url source page" not in published
