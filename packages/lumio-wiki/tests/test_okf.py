from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from lumio_wiki.knowledge_base import load_knowledge_base

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


def test_okf_export_and_import_use_only_portable_foundation(tmp_path: Path):
    from lumio_wiki.okf import (
        export_okf_profile1,
        export_okf_profile2,
        import_okf_profile1,
        import_okf_profile2,
    )

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

    exported_v2 = export_okf_profile2(kb.public_pages())
    assert exported_v2.profile_version == 2
    assert "sources:" in exported_v2.files["overview.md"]
    bundle_v2 = tmp_path / "bundle-v2"
    for relative, content in exported_v2.files.items():
        target = bundle_v2 / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    imported_v2 = import_okf_profile2(bundle_v2)
    assert imported_v2.profile_version == 2
    assert {page.title for page in imported_v2.proposed_pages} == {
        page.title for page in kb.public_pages()
    }


def _lumio_core_compat_available() -> bool:
    """ADR-0025: the temporary ``lumio.core`` compatibility surface lives in
    the private application repository; skip these alias checks when it (and
    therefore the application) is not installed."""
    return importlib.util.find_spec("lumio") is not None

@pytest.mark.skipif(
    not _lumio_core_compat_available(),
    reason="lumio.core lives in the private app repo (ADR-0025)",
)
def test_legacy_okf_module_aliases_the_new_owner():
    assert importlib.import_module("lumio.core.okf") is importlib.import_module("lumio_wiki.okf")
