"""Registry and freshness view for the Knowledge Base."""

from pathlib import Path
from shutil import copytree

from lumio.core import KnowledgeBase, load_knowledge_base
from lumio.core.records import RegistryEntry

FIXTURES = Path(__file__).parent / "fixtures" / "valid"


def test_registry_contains_every_page():
    kb, _ = load_knowledge_base(FIXTURES)
    registry = kb.registry()
    assert isinstance(registry, list)
    assert len(registry) == len(kb.pages) == 3
    assert all(isinstance(entry, RegistryEntry) for entry in registry)
    titles = {entry.title for entry in registry}
    assert titles == {"Lumio Overview", "Architecture", "Technology Stack"}


def test_registry_entry_has_expected_fields():
    kb, _ = load_knowledge_base(FIXTURES)
    entry = next(entry for entry in kb.registry() if entry.title == "Lumio Overview")

    assert entry.title == "Lumio Overview"
    assert entry.aliases == ["Lumio"]
    assert entry.tags == ["lumio", "overview"]
    assert entry.summary == "A high-level introduction to Lumio."
    assert entry.lifecycle == "approved"
    assert entry.visibility == "public"
    assert entry.path == "overview.md"
    assert entry.source_count == 1
    assert entry.relationship_count == 1

    architecture = next(entry for entry in kb.registry() if entry.title == "Architecture")
    assert architecture.visibility == "internal"
    assert architecture.source_count == 1
    assert architecture.relationship_count == 2


def test_registry_is_rebuildable():
    kb, _ = load_knowledge_base(FIXTURES)
    first = kb.registry()
    second = kb.registry()
    assert first == second


def test_is_fresh_reports_fresh_after_build(tmp_path: Path) -> None:
    kb, _ = load_knowledge_base(FIXTURES)
    assert not kb.is_fresh()

    indexed = kb.build_index(tmp_path)
    assert indexed.is_fresh()


def test_is_fresh_reports_stale_after_source_change(tmp_path: Path) -> None:
    tmp_root = tmp_path / "kb"
    copytree(FIXTURES, tmp_root)
    index_dir = tmp_path / "index"

    kb, _ = load_knowledge_base(tmp_root)
    indexed = kb.build_index(index_dir)
    assert indexed.is_fresh()

    page = tmp_root / "overview.md"
    page.write_text(page.read_text() + "\n<!-- changed -->\n")

    reloaded, _ = load_knowledge_base(tmp_root)
    stale_kb = KnowledgeBase(root=reloaded.root, pages=reloaded.pages, index_dir=index_dir)
    assert not stale_kb.is_fresh()
    assert stale_kb.fingerprint().digest != stale_kb.stored_fingerprint().digest


def test_is_fresh_restored_after_rebuild(tmp_path: Path) -> None:
    tmp_root = tmp_path / "kb"
    copytree(FIXTURES, tmp_root)
    index_dir = tmp_path / "index"

    kb, _ = load_knowledge_base(tmp_root)
    indexed = kb.build_index(index_dir)
    assert indexed.is_fresh()

    page = tmp_root / "overview.md"
    page.write_text(page.read_text() + "\n<!-- changed -->\n")

    reloaded, _ = load_knowledge_base(tmp_root)
    stale_kb = KnowledgeBase(root=reloaded.root, pages=reloaded.pages, index_dir=index_dir)
    assert not stale_kb.is_fresh()

    rebuilt = stale_kb.build_index(index_dir)
    assert rebuilt.is_fresh()
