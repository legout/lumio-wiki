"""S3 Published Version diff — Location accessor + CLI contract suite.

t_66f162f2. Proves ``S3Location.published_version_content(version)`` loads one
complete immutable version's canonical content through the existing manifest /
digest-validated read path (no re-materialization logic), and that the
``diff-s3`` CLI command reports a deterministic added/removed/changed delta
for two versions with human output by default and one machine-readable JSON
object under ``--json``. The suite uses obstore's in-memory ``MemoryStore`` so
it is fully deterministic with no infrastructure.
"""

from __future__ import annotations

import json
from pathlib import Path

import msgspec
import pytest

obstore = pytest.importorskip("obstore", reason="obstore required for the S3 diff suite")

from lumio_wiki import cli  # noqa: E402
from lumio_wiki.knowledge_base import (  # noqa: E402
    KnowledgeBaseError,
    _fingerprint_sources,
    _InMemoryKbSource,
    _load_and_validate,
)
from lumio_wiki.s3_location import (  # noqa: E402
    MANIFEST_OBJECT,
    S3Location,
    S3Pointer,
    build_published_manifest,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
CATEGORIZED = FIXTURES / "categorized_kb"

ADDED_PAGE = (
    "---\ntitle: \"Added Page\"\nid: \"entity:added\"\nentity_types:\n  - organization\n"
    "summary: \"A page added in v2.\"\nlifecycle: \"approved\"\nvisibility: \"public\"\n"
    "sources:\n  - id: \"added-source\"\nsynthetic: false\n---\n\n# Added Page\n\nNew.\n"
)


# ---------------------------------------------------------------------------
# Test infrastructure: publish real immutable versions into a MemoryStore.
# ---------------------------------------------------------------------------


def _publish(store, prefix: str, version: str, content: dict[str, bytes]) -> None:
    """Materialize canonical content under an immutable version prefix."""
    fingerprint = _fingerprint_sources(_InMemoryKbSource(content, root=ROOT)).digest
    manifest = build_published_manifest(version, fingerprint, content)
    for rel, raw in content.items():
        obstore.put(store, f"{prefix}/{version}/{rel}", raw)
    obstore.put(store, f"{prefix}/{version}/{MANIFEST_OBJECT}", msgspec.json.encode(manifest))


def _v1_content() -> dict[str, bytes]:
    from lumio_wiki.knowledge_base import _FilesystemKbSource, canonical_content

    return canonical_content(_FilesystemKbSource(CATEGORIZED.resolve()))


def _publish_diff_versions(store, prefix: str = "kb") -> None:
    """Publish v1 (fixture) and v2 (v1 + one added page + one modified page)."""
    v1 = _v1_content()
    _publish(store, prefix, "v1", v1)

    v2 = dict(v1)
    v2["entities/added.md"] = ADDED_PAGE.encode()
    v2["concepts/overview.md"] = v2["concepts/overview.md"].replace(
        b"trusted knowledge", b"TRUSTED KNOWLEDGE"
    )
    _publish(store, prefix, "v2", v2)
    obstore.put(store, f"{prefix}/current.json", msgspec.json.encode(S3Pointer(version="v2")))


# ---------------------------------------------------------------------------
# 1. S3Location.published_version_content(version).
# ---------------------------------------------------------------------------


def test_published_version_content_returns_manifest_and_validated_content():
    store = obstore.store.MemoryStore()
    _publish_diff_versions(store)
    location = S3Location(store, "kb", version="v1")
    manifest, content = location.published_version_content("v1")
    assert manifest.version == "v1"
    assert any(rel.endswith(".md") for rel in content)
    # The content is digest-validated canonical content the Core SDK loads.
    kb, _report = _load_and_validate(_InMemoryKbSource(content, root=Path("s3:kb/v1")))
    assert {page.path for page in kb.pages} >= {"entities/acme.md", "concepts/overview.md"}


def test_published_version_content_missing_version_fails_closed():
    store = obstore.store.MemoryStore()
    _publish_diff_versions(store)
    location = S3Location(store, "kb")
    with pytest.raises(KnowledgeBaseError, match="v3"):
        location.published_version_content("v3")


# ---------------------------------------------------------------------------
# 2. The diff-s3 CLI command.
# ---------------------------------------------------------------------------


def test_cli_diff_s3_reports_changes(monkeypatch, capsys):
    store = obstore.store.MemoryStore()
    _publish_diff_versions(store)
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (store, "kb"))

    rc = cli.main(["diff-s3", "s3://bucket/kb", "--from", "v1", "--to", "v2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v1" in out and "v2" in out
    assert "entities/added.md" in out  # stable path identity of the added page


def test_cli_diff_s3_json(monkeypatch, capsys):
    store = obstore.store.MemoryStore()
    _publish_diff_versions(store)
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (store, "kb"))

    rc = cli.main(["diff-s3", "s3://bucket/kb", "--from", "v1", "--to", "v2", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["from_version"] == "v1"
    assert payload["to_version"] == "v2"
    assert "entities/added.md" in payload["added_pages"]
    assert any(change["path"] == "concepts/overview.md" for change in payload["changed"])
    assert payload["added_sources"] == ["added-source"]


def test_cli_diff_s3_no_changes(monkeypatch, capsys):
    store = obstore.store.MemoryStore()
    _publish_diff_versions(store)
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (store, "kb"))

    rc = cli.main(["diff-s3", "s3://bucket/kb", "--from", "v2", "--to", "v2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no changes" in out.lower()


def test_cli_diff_s3_missing_version_errors(monkeypatch, capsys):
    store = obstore.store.MemoryStore()
    _publish_diff_versions(store)
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (store, "kb"))

    rc = cli.main(["diff-s3", "s3://bucket/kb", "--from", "v1", "--to", "v3"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "v3" in captured.err
