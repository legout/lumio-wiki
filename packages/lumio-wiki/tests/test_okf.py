from __future__ import annotations

import importlib
from pathlib import Path

from lumio_wiki.knowledge_base import load_knowledge_base

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


def test_okf_export_and_import_use_only_portable_foundation(tmp_path: Path):
    from lumio_wiki.okf import export_okf_profile1, import_okf_profile1

    kb, report = load_knowledge_base(FIXTURES / "valid")
    assert report.is_valid
    exported = export_okf_profile1(kb.public_pages())
    assert exported.public_page_count == len(kb.public_pages())
    assert "index.md" in exported.files

    bundle = tmp_path / "bundle"
    for relative, content in exported.files.items():
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    imported = import_okf_profile1(bundle)
    assert imported.proposed_pages
    assert imported.navigation_index_count >= 1
    assert {page.title for page in imported.proposed_pages} == {
        page.title for page in kb.public_pages()
    }


def test_legacy_okf_module_aliases_the_new_owner():
    assert importlib.import_module("lumio.core.okf") is importlib.import_module(
        "lumio_wiki.okf"
    )
