"""Issue #177: CLI rendering exposes labelled, openable citation actions.

The ``search``, evidence-mode ``search``, and ``page`` outputs gain additive,
labelled open-action lines:

- ``open:``  the copyable ``lumio-wiki page "<title>"`` command;
- ``web:``   the optional Reader browser URL — present ONLY when a valid
  ``LUMIO_READER_BASE_URL`` is configured;
- ``source-url:`` / ``source-artifact:`` clearly distinct labels for the
  authored external Source URL and the explicit private-Source inspect
  command (never an implicit signed/public artifact URL, ADR-0020).

Installed-wheel journeys cover successful reads and browser/open actions;
this file retains invalid-base and private-source separation checks.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from lumio_wiki import cli
from lumio_wiki.citation_actions import (
    citation_open_actions,
    render_open_actions,
)
from lumio_wiki.cli import main

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# S3 Published Version pinning (A4 remediation)
# ---------------------------------------------------------------------------


obstore = pytest.importorskip("obstore", reason="obstore required for S3 journeys")


class _PublishedS3Kb:
    """The ``valid`` fixture published to an in-memory store under versions.

    Real publication protocol (manifest + pointer) through the CLI's own
    publish-s3 route, so the reader journeys exercise the exact production
    resolution path — fully offline via ``obstore.store.MemoryStore``.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = obstore.store.MemoryStore()
        self.calls: list[tuple[str, str | None]] = []

        def _fake_build(uri: str):
            assert cli._is_object_store_uri(uri)
            return self.store, "kb"

        monkeypatch.setattr(cli, "_build_publish_store", _fake_build)

        def _fake_resolve(uri: str, version: str | None = None):
            from lumio_wiki.s3_location import S3Location

            self.calls.append((uri, version))
            # The container is a MemoryStore, so the whole journey is offline;
            # version threading (pin vs. pointer) stays production-real.
            return S3Location(self.store, "kb", version=version)

        monkeypatch.setattr(cli, "_resolve_object_store_location", _fake_resolve)

        monkeypatch.chdir(tmp_path)
        for var in ("LUMIO_KB_PATH", "LUMIO_READER_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        self.source_root = tmp_path / "source-kb"
        shutil.copytree(FIXTURES / "valid", self.source_root)
        self._body = "Lumio is a deployable chat platform for trusted knowledge and data."
        self.publish("v1")

    def publish(self, version: str) -> None:
        assert (
            main(["publish-s3", str(self.source_root), "s3://bucket/kb", "--version", version]) == 0
        )

    def set_body(self, text: str) -> None:
        page = self.source_root / "overview.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(self._body, text), encoding="utf-8"
        )
        self._body = text


def test_page_actions_pin_the_exact_resolved_published_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Copyable page/source actions over an S3 KB pin the resolved version."""
    import shlex

    kb = _PublishedS3Kb(tmp_path, monkeypatch)
    kb.set_body("v2 body sentinel")
    kb.publish("v2")  # pointer now at v2; a reader resolves exactly v2

    assert main(["page", "s3://bucket/kb", "Lumio Overview"]) == 0
    out = capsys.readouterr().out
    open_line = next(line for line in out.splitlines() if line.startswith("open:"))
    source_line = next(line for line in out.splitlines() if line.startswith("source-artifact:"))

    # The exact resolved Published Version is pinned — never the bare pointer.
    assert open_line == (
        'open:            lumio-wiki page "s3://bucket/kb" "Lumio Overview" --published-version v2'
    )
    assert source_line == (
        'source-artifact: lumio-wiki source inspect "s3://bucket/kb" '
        "--source-id lumio-overview --published-version v2"
    )
    # Never a local materialization path and never a private artifact URL.
    assert str(tmp_path) not in out
    assert "signed" not in out.lower()

    # Replay reads the SAME version even after the pointer advances again.
    kb.set_body("v3 body sentinel")
    kb.publish("v3")
    assert main(shlex.split(open_line.split(":", 1)[1].strip())[1:]) == 0
    assert "v2 body sentinel" in capsys.readouterr().out
    # ...and the replay pinned v2 (never v3) through the real Location seam.
    assert kb.calls[-1] == ("s3://bucket/kb", "v2")


def test_search_json_pins_the_resolved_published_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Search JSON carries the exact resolved version on the payload and on
    every copyable command; ``kb_location`` stays the original public S3 URI."""
    kb = _PublishedS3Kb(tmp_path, monkeypatch)
    kb.publish("v2")
    capsys.readouterr()

    assert main(["search", "s3://bucket/kb", "Lumio", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["published_version"] == "v2"
    assert data["results"], "fixture query must match"
    for entry in data["results"]:
        assert entry["kb_location"] == "s3://bucket/kb"
        assert "--published-version v2" in entry["open_command"]
    assert str(tmp_path) not in json.dumps(data)


def test_page_json_pins_the_resolved_published_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    kb = _PublishedS3Kb(tmp_path, monkeypatch)
    kb.publish("v2")
    capsys.readouterr()

    assert main(["page", "s3://bucket/kb", "Lumio Overview", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["published_version"] == "v2"
    assert data["kb_location"] == "s3://bucket/kb"
    assert str(tmp_path) not in json.dumps(data)


def test_explicit_published_version_replays_a_historical_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """``--published-version`` reads that exact version, pointer notwithstanding."""
    kb = _PublishedS3Kb(tmp_path, monkeypatch)

    assert main(["page", "s3://bucket/kb", "Lumio Overview", "--published-version", "v1"]) == 0
    out = capsys.readouterr().out
    assert kb.calls[-1] == ("s3://bucket/kb", "v1")
    assert "v1 body sentinel" not in out


def test_published_version_flag_on_a_local_kb_is_a_usage_error(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    # A local Knowledge Base has no Published Versions to pin: refuse instead
    # of silently ignoring the flag.
    assert main(["page", str(kb_root), "Lumio Overview", "--published-version", "v1"]) == 2
    assert "--published-version" in capsys.readouterr().err
    assert main(["search", str(kb_root), "Lumio", "--published-version", "v1"]) == 2
    assert "--published-version" in capsys.readouterr().err


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the valid fixture into a writable Knowledge Base root."""
    import shutil

    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    return root


# ---------------------------------------------------------------------------
# search (page results)
# ---------------------------------------------------------------------------


def test_search_rejects_an_invalid_reader_base_url(
    kb_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # An object-store URI is private storage, never a public browser URL.
    monkeypatch.setenv("LUMIO_READER_BASE_URL", "s3://bucket/kb")
    rc = main(["search", str(kb_root), "Lumio", "--limit", "3"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_READER_BASE_URL" in err
    assert "http" in err


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def test_page_labels_authored_source_url_and_private_source_action(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("LUMIO_READER_BASE_URL", raising=False)
    rc = main(["page", str(kb_root), "Lumio Overview"])
    assert rc == 0
    out = capsys.readouterr().out
    # Authored external Source URL is a distinct, labelled line.
    assert "source-url:      https://example.com/lumio" in out
    # The private Source action is the explicit inspect command, never a URL.
    assert (
        f'source-artifact: lumio-wiki source inspect "{kb_root.resolve()}" '
        "--source-id lumio-overview" in out
    )
    assert "--published-version" not in out
    assert "signed" not in out.lower()


def test_s3_page_actions_and_json_pin_the_resolved_version(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page read resolves once; its actions must not follow a later pointer."""
    import lumio_wiki as lw

    kb, report = lw.load_knowledge_base(kb_root)
    assert report.is_valid
    requested_versions: list[str | None] = []
    active_version = "v1"

    def fake_location(_uri: str, *, version: str | None = None):
        requested_versions.append(version)
        return SimpleNamespace(
            resolve=lambda: SimpleNamespace(
                knowledge_base=kb,
                published_version=version if version is not None else active_version,
            )
        )

    monkeypatch.setattr("lumio_wiki.cli._resolve_object_store_location", fake_location)
    uri = "s3://public-bucket/team-kb"

    assert main(["page", uri, "Lumio Overview"]) == 0
    out = capsys.readouterr().out
    assert (
        'open:            lumio-wiki page "s3://public-bucket/team-kb" "Lumio Overview" '
        "--published-version v1" in out
    )
    assert (
        'source-artifact: lumio-wiki source inspect "s3://public-bucket/team-kb" '
        "--source-id lumio-overview --published-version v1" in out
    )
    assert "X-Amz" not in out

    assert main(["page", uri, "Lumio Overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["published_version"] == "v1"
    assert payload["open_command"].endswith("--published-version v1")

    # A later active pointer does not alter an already copied action: replay
    # asks the S3 Location for the explicit resolved version.
    active_version = "v2"
    assert main(["page", uri, "Lumio Overview", "--published-version", "v1"]) == 0
    capsys.readouterr()
    assert requested_versions[-1] == "v1"


# ---------------------------------------------------------------------------
# rendered actions never leak object-store URLs (issue #177 AC)
# ---------------------------------------------------------------------------


def test_rendered_open_actions_never_contain_object_store_urls():
    actions = citation_open_actions(
        page_title="T",
        page_path="t.md",
        source_id="s",
        source_url="https://example.com/doc.pdf",
    )
    lines = render_open_actions(actions)
    assert not any(scheme in line for line in lines for scheme in ("s3://", "gs://", "az://"))
