"""Safe URL and research-bundle ingestion (issue #178).

Covers the network-safety fetch boundary (HTTPS-only by default, SSRF
destination rejection, bounded bytes/time/redirects, truthful final
provenance), the ``ingest-url`` managed host-Distiller command, the bounded
research bundle (``ingest-research``), and the acceptance criteria:

* one public command stages an HTTPS Knowledge Source under a stable Source ID
  through the ORDINARY proposal pipeline (inspect/validate/publish/discard);
* network and payload bounds fail CLOSED with actionable errors — nothing is
  staged or registered when a bound trips;
* redirects re-validate every hop and record the FINAL URL;
* no fetched URL content becomes a Claim/Citation/Evidence without the
  authored Compiled Page + review;
* base local-file text/Markdown ingestion stays unchanged and model-free.

Hermetic strategy: the fetcher's address policy is unit-tested by injecting
resolved addresses; the wire path is exercised against a real local
``http.server`` with the explicit ``--allow-http``/``--allow-private-
destination`` escape hatches (the same hatches an intranet deployment uses).
No external network is ever touched.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.cli import main
from lumio_wiki.url_fetch import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_TIMEOUT_SECONDS,
    UrlFetchError,
    UrlFetchPolicy,
    fetch_url,
)

# ---------------------------------------------------------------------------
# Static URL policy (no network)
# ---------------------------------------------------------------------------


def test_fetch_url_rejects_plain_http_by_default_with_actionable_error():
    with pytest.raises(UrlFetchError, match="--allow-http"):
        fetch_url("http://example.com/page", UrlFetchPolicy())


def test_fetch_url_rejects_non_http_s_schemes():
    for scheme in ("ftp", "file", "gopher", "javascript", "data"):
        with pytest.raises(UrlFetchError, match="scheme"):
            fetch_url(f"{scheme}://example.com/x", UrlFetchPolicy())


def test_fetch_url_rejects_credential_bearing_urls():
    for url in (
        "https://user:pass@example.com/page",
        "https://token@example.com/page",
    ):
        with pytest.raises(UrlFetchError, match="credential"):
            fetch_url(url, UrlFetchPolicy())


def test_fetch_url_rejects_urls_without_host():
    with pytest.raises(UrlFetchError):
        fetch_url("https:///no-host", UrlFetchPolicy())


# ---------------------------------------------------------------------------
# Resolved-address policy (SSRF boundary) — resolution injected, no network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.8.8.8",  # loopback is the whole 127/8, not just 127.0.0.1
        "::1",
        "10.1.2.3",
        "192.168.1.10",
        "172.16.0.5",
        "169.254.169.254",  # cloud metadata endpoint
        "0.0.0.0",
        "fe80::1",
        "fd00::1",
        # Standards-review additions: hand-rolled CIDR checks missed these
        # non-global / multicast / site-local ranges (Python ipaddress
        # classifies them all as not safe to dial).
        "192.0.2.1",  # TEST-NET-1 (documentation)
        "198.18.0.1",  # benchmarking
        "2001:db8::1",  # IPv6 documentation
        "240.0.0.1",  # reserved
        "224.0.0.1",  # multicast (is_global alone would permit it)
        "ff02::1",  # IPv6 multicast
        "fec0::1",  # deprecated IPv6 site-local
        "::ffff:10.0.0.1",  # IPv4-mapped private
    ],
)
def test_fetch_url_rejects_private_and_loopback_destinations(address, monkeypatch):
    monkeypatch.setattr("lumio_wiki.url_fetch._resolve_addresses", lambda host, port: [address])
    with pytest.raises(UrlFetchError, match="private"):
        fetch_url("https://example.com/page", UrlFetchPolicy())


def test_fetch_url_rejects_when_any_resolved_address_is_private(monkeypatch):
    # A DNS name resolving to one public AND one private address must fail
    # closed (the connection could land on the private one).
    monkeypatch.setattr(
        "lumio_wiki.url_fetch._resolve_addresses",
        lambda host, port: ["93.184.216.34", "10.0.0.1"],
    )
    with pytest.raises(UrlFetchError, match="private"):
        fetch_url("https://example.com/page", UrlFetchPolicy())


def test_fetch_url_allows_private_destinations_only_with_explicit_policy(monkeypatch):
    monkeypatch.setattr("lumio_wiki.url_fetch._resolve_addresses", lambda host, port: ["127.0.0.1"])
    policy = UrlFetchPolicy(allow_private_destinations=True, allow_http=True)
    # A connection attempted against a non-listening port still proves the
    # policy gate passed: the failure is a connection failure, not a policy
    # rejection of the private destination.
    with pytest.raises(UrlFetchError, match="failed connecting"):
        fetch_url("http://127.0.0.1:9/page", policy)


# ---------------------------------------------------------------------------
# Hermetic wire path against a real local server
# ---------------------------------------------------------------------------


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


_LOCAL_POLICY = UrlFetchPolicy(allow_http=True, allow_private_destinations=True)


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/moved":
            self.send_response(301)
            self.send_header("Location", "/final.html")
            self.end_headers()
        elif self.path == "/final.html":
            body = b"<html><body>Example page</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/":
            body = b"root index"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/huge":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(DEFAULT_MAX_BYTES + 1))
            self.end_headers()
        elif self.path == "/no-length":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"x" * (DEFAULT_MAX_BYTES + 1))
        elif self.path == "/denied":
            self.send_response(403)
            self.end_headers()
        elif self.path == "/away":
            self.send_response(302)
            self.send_header("Location", "ftp://elsewhere.example/file")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib API; silence test server
        return


@pytest.fixture(scope="module")
def local_server():
    server, base = _serve(_PageHandler)
    yield base
    server.shutdown()


def test_fetch_url_follows_redirects_and_records_truthful_provenance(local_server):
    result = fetch_url(f"{local_server}/moved", _LOCAL_POLICY)
    assert result.final_url == f"{local_server}/final.html"
    assert result.body == b"<html><body>Example page</body></html>"
    assert result.media_type == "text/html"
    assert result.filename == "final.html"
    assert result.content_hash == hashlib.sha256(result.body).hexdigest()
    assert result.retrieved_at  # ISO-8601 recorded
    assert result.redirects == 1


def test_fetch_url_derives_filename_when_url_path_has_none(local_server):
    result = fetch_url(local_server + "/", _LOCAL_POLICY)
    assert result.filename
    assert result.filename.endswith(".txt")


def test_fetch_url_fails_closed_on_declared_oversize(local_server):
    with pytest.raises(UrlFetchError, match="exceeds"):
        fetch_url(f"{local_server}/huge", _LOCAL_POLICY)


def test_fetch_url_fails_closed_on_streamed_oversize(local_server):
    # No Content-Length: the reader must still stop at max_bytes.
    with pytest.raises(UrlFetchError, match="exceeds"):
        fetch_url(f"{local_server}/no-length", _LOCAL_POLICY)


def test_fetch_url_fails_closed_on_http_error_status(local_server):
    with pytest.raises(UrlFetchError, match="403"):
        fetch_url(f"{local_server}/denied", _LOCAL_POLICY)


def test_fetch_url_rejects_redirect_to_disallowed_scheme(local_server):
    with pytest.raises(UrlFetchError, match="scheme"):
        fetch_url(f"{local_server}/away", _LOCAL_POLICY)


def test_fetch_url_enforces_redirect_budget(local_server, monkeypatch):
    monkeypatch.setattr("lumio_wiki.url_fetch.DEFAULT_MAX_REDIRECTS", DEFAULT_MAX_REDIRECTS)
    policy = UrlFetchPolicy(allow_http=True, allow_private_destinations=True, max_redirects=0)
    with pytest.raises(UrlFetchError, match="redirect"):
        fetch_url(f"{local_server}/moved", policy)


def test_url_fetch_policy_defaults_are_bounded():
    policy = UrlFetchPolicy()
    assert policy.max_bytes == DEFAULT_MAX_BYTES
    assert policy.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert policy.max_redirects == DEFAULT_MAX_REDIRECTS
    assert policy.allow_http is False
    assert policy.allow_private_destinations is False
    assert DEFAULT_MAX_BYTES <= 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# ingest-url — managed host-Distiller URL ingestion through the ORDINARY
# proposal pipeline (issue #178 interface + ACs).
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    return root


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    return root


def _url_page(source_id: str, *, title: str = "Example Page Notes") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        'tags:\n  - "notes"\n'
        'summary: "Authored from the fetched page."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} source"\n'
        "synthetic: false\n"
        "---\n\n"
        f"# {title}\n\n"
        "Agent-authored synthesis; the fetched bytes are provenance only.\n"
    )


def _write_page(tmp_path: Path, source_id: str) -> Path:
    page = tmp_path / "page.md"
    page.write_text(_url_page(source_id), encoding="utf-8")
    return page


def test_ingest_url_stages_https_source_under_stable_id(
    kb_root: Path, tmp_path: Path, local_server, capsys
):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/final.html",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
            "--allow-http",
            "--allow-private-destination",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "source_id:      example-page" in out
    assert f"source_url:     {local_server}/final.html" in out
    assert "retrieved_at:" in out
    # The ordinary review pipeline is the ONLY path to publication.
    assert "lumio-wiki proposal inspect" in out
    # Private Source Registry: stable identity, immutable content-hashed version.
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    registered = store.source_registry.get("example-page")
    assert registered.status == "active"
    assert registered.versions[0].filename == "final.html"
    assert registered.versions[0].content_type == "text/html"


def test_ingest_url_rejects_plain_http_by_default(kb_root: Path, tmp_path: Path):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            "http://example.com/page",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_ingest_url_rejects_private_destination_by_default(
    kb_root: Path, tmp_path: Path, local_server
):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/final.html",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
            "--allow-http",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_ingest_url_requires_compiled_page_and_source_id_together(
    kb_root: Path, tmp_path: Path, local_server
):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/final.html",
            "--compiled-page",
            str(page),
        ]
    )
    assert rc != 0
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/final.html",
            "--source-id",
            "example-page",
            "--allow-http",
            "--allow-private-destination",
        ]
    )
    assert rc != 0


def test_ingest_url_failed_fetch_stages_and_registers_nothing(
    kb_root: Path, tmp_path: Path, local_server
):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/denied",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
            "--allow-http",
            "--allow-private-destination",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []
    with pytest.raises(lw.SourceRegistryError):
        store.source_registry.get("example-page")


def test_ingest_url_identical_retry_reuses_version_changed_bytes_rejected(
    kb_root: Path, tmp_path: Path, local_server
):
    page = _write_page(tmp_path, "example-page")
    argv = [
        "ingest-url",
        str(kb_root),
        f"{local_server}/final.html",
        "--compiled-page",
        str(page),
        "--source-id",
        "example-page",
        "--allow-http",
        "--allow-private-destination",
    ]
    assert main(argv) == 0
    assert main(argv) == 0  # idempotent retry: same bytes, same version
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert len(store.source_registry.get("example-page").versions) == 1
    # A DIFFERENT page at the same identity must fail closed under the
    # existing lifecycle rules (retire/reactivate adds reviewed versions).
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
            "--allow-http",
            "--allow-private-destination",
        ]
    )
    assert rc != 0


def test_ingest_url_publishes_through_ordinary_pipeline(
    kb_root: Path, tmp_path: Path, local_server, capsys
):
    page = _write_page(tmp_path, "example-page")
    assert (
        main(
            [
                "ingest-url",
                str(kb_root),
                f"{local_server}/final.html",
                "--compiled-page",
                str(page),
                "--source-id",
                "example-page",
                "--allow-http",
                "--allow-private-destination",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    assert main(["publish", str(kb_root), pid]) == 0
    assert lw.validate(kb_root).is_valid
    # The published page is the AUTHORED Markdown, not fetched content.
    assert any(
        "Agent-authored synthesis" in p.read_text(encoding="utf-8") for p in kb_root.rglob("*.md")
    )


def test_ingest_url_respects_max_bytes_flag(kb_root: Path, tmp_path: Path, local_server):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            f"{local_server}/final.html",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
            "--allow-http",
            "--allow-private-destination",
            "--max-bytes",
            "4",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_proposal_inspect_shows_url_provenance(kb_root: Path, tmp_path: Path, local_server, capsys):
    page = _write_page(tmp_path, "example-page")
    assert (
        main(
            [
                "ingest-url",
                str(kb_root),
                f"{local_server}/moved",
                "--compiled-page",
                str(page),
                "--source-id",
                "example-page",
                "--allow-http",
                "--allow-private-destination",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    assert main(["proposal", "inspect", str(kb_root), pid]) == 0
    out = capsys.readouterr().out
    # Truthful FINAL provenance after the redirect (inspect column alignment).
    assert f"source_url:      {local_server}/final.html" in out


# ---------------------------------------------------------------------------
# ingest-research — bounded research bundle (report + consulted-URL manifest).
# ---------------------------------------------------------------------------

_MANIFEST = """\
- url: https://example.com/a
  title: "Source A"
  accessed_at: 2026-07-01T10:00:00+00:00
- url: https://example.com/b
  title: "Source B"
  accessed_at: 2026-07-01T11:30:00+00:00
"""

_RESEARCH_PAGE = (
    "---\n"
    'title: "Research Notes Q3"\n'
    "aliases: []\n"
    'tags:\n  - "research"\n'
    'summary: "Agent research with consulted-source manifest."\n'
    'lifecycle: "draft"\n'
    'visibility: "internal"\n'
    "sources:\n"
    '  - id: "research-q3"\n'
    '    title: "Q3 research bundle"\n'
    "synthetic: false\n"
    "---\n\n"
    "# Research Notes Q3\n\n"
    '> Quoted passage from Source A ("quoted text").\n\n'
    "Agent synthesis clearly separated from quotations above.\n"
)


def _write_research(tmp_path: Path) -> tuple[Path, Path]:
    report = tmp_path / "report.md"
    report.write_text(_RESEARCH_PAGE, encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(_MANIFEST, encoding="utf-8")
    return report, manifest


def test_ingest_research_stages_report_with_consulted_provenance(
    kb_root: Path, tmp_path: Path, capsys
):
    report, manifest = _write_research(tmp_path)
    rc = main(
        [
            "ingest-research",
            str(kb_root),
            str(report),
            "--manifest",
            str(manifest),
            "--source-id",
            "research-q3",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "source_id:      research-q3" in out
    assert "consulted:      2 sources" in out
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    proposal = store.list()[0]
    assert [c.url for c in proposal.provenance.consulted_sources] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    # The report — not any fetched URL — is the proposed page content.
    assert "Research Notes Q3" in proposal.proposed_pages[0].markdown


def test_ingest_research_publishes_through_ordinary_pipeline(kb_root: Path, tmp_path: Path, capsys):
    report, manifest = _write_research(tmp_path)
    assert (
        main(
            [
                "ingest-research",
                str(kb_root),
                str(report),
                "--manifest",
                str(manifest),
                "--source-id",
                "research-q3",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    assert main(["publish", str(kb_root), pid]) == 0
    assert lw.validate(kb_root).is_valid


@pytest.mark.parametrize(
    "manifest_text",
    [
        "[]",  # no consulted sources recorded
        "- url: ftp://example.com/a\n  accessed_at: 2026-07-01T10:00:00+00:00\n",
        "- url: https://user:pass@example.com/a\n  accessed_at: 2026-07-01T10:00:00+00:00\n",
        '- url: https://example.com/a\n  title: "x"\n  accessed_at: not-a-date\n',
        "not: a: list\n",
    ],
)
def test_ingest_research_rejects_invalid_manifests(kb_root: Path, tmp_path: Path, manifest_text):
    report, manifest = _write_research(tmp_path)
    manifest.write_text(manifest_text, encoding="utf-8")
    rc = main(
        [
            "ingest-research",
            str(kb_root),
            str(report),
            "--manifest",
            str(manifest),
            "--source-id",
            "research-q3",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_ingest_research_requires_page_to_declare_source_id(kb_root: Path, tmp_path: Path):
    report, manifest = _write_research(tmp_path)
    report.write_text(_RESEARCH_PAGE.replace("research-q3", "other-id"), encoding="utf-8")
    rc = main(
        [
            "ingest-research",
            str(kb_root),
            str(report),
            "--manifest",
            str(manifest),
            "--source-id",
            "research-q3",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_proposal_inspect_lists_consulted_sources(kb_root: Path, tmp_path: Path, capsys):
    report, manifest = _write_research(tmp_path)
    assert (
        main(
            [
                "ingest-research",
                str(kb_root),
                str(report),
                "--manifest",
                str(manifest),
                "--source-id",
                "research-q3",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    pid = store.list()[0].id
    assert main(["proposal", "inspect", str(kb_root), pid]) == 0
    out = capsys.readouterr().out
    assert "https://example.com/a" in out
    assert "2026-07-01T10:00:00+00:00" in out


# ---------------------------------------------------------------------------
# Spec-review regression tests (issue #178): manifest completeness, malformed
# URLs, and both-missing flags must fail closed with actionable errors.
# ---------------------------------------------------------------------------


def test_ingest_url_malformed_port_fails_with_actionable_error(
    kb_root: Path, tmp_path: Path, capsys
):
    page = _write_page(tmp_path, "example-page")
    rc = main(
        [
            "ingest-url",
            str(kb_root),
            "https://example.com:not-a-port/page",
            "--compiled-page",
            str(page),
            "--source-id",
            "example-page",
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "error:" in err  # actionable CliError, not a raw traceback
    assert "Traceback" not in err


@pytest.mark.parametrize(
    "manifest_text",
    [
        # title missing
        "- url: https://example.com/a\n  accessed_at: 2026-07-01T10:00:00+00:00\n",
        # accessed_at missing
        '- url: https://example.com/a\n  title: "A"\n',
        # no host
        '- url: https:///path\n  title: "A"\n  accessed_at: 2026-07-01T10:00:00+00:00\n',
    ],
)
def test_ingest_research_requires_complete_manifest_entries(
    kb_root: Path, tmp_path: Path, manifest_text
):
    report, manifest = _write_research(tmp_path)
    manifest.write_text(manifest_text, encoding="utf-8")
    rc = main(
        [
            "ingest-research",
            str(kb_root),
            str(report),
            "--manifest",
            str(manifest),
            "--source-id",
            "research-q3",
        ]
    )
    assert rc != 0
    store = lw.IngestStore(kb_root / ".lumio" / "ingest")
    assert store.list() == []


def test_ingest_url_with_neither_flag_fails_with_actionable_error(
    kb_root: Path, tmp_path: Path, capsys
):
    rc = main(["ingest-url", str(kb_root), "https://example.com/page"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "--compiled-page and --source-id are required together" in err
    assert "Traceback" not in err
