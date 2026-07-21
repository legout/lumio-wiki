"""Package contraction invariants for issue #103 / ADR-0010.

After the workspace migration, internal callers must use final package owners.
Documented pre-1.0 ``lumio.core`` re-exports remain for external consumers only.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_SRC = ROOT / "packages" / "lumio" / "src" / "lumio"
WIKI_SRC = ROOT / "packages" / "lumio-wiki" / "src" / "lumio_wiki"
LANCEDB_SRC = ROOT / "packages" / "lumio-lancedb" / "src" / "lumio_lancedb"

# Documented temporary public compatibility surface (README + lumio.core docs).
COMPATIBILITY_MODULES = {
    APP_SRC / "core" / "__init__.py",
    APP_SRC / "core" / "embeddings.py",
    APP_SRC / "core" / "evidence.py",
    APP_SRC / "core" / "fingerprint_store.py",
    APP_SRC / "core" / "index.py",
    APP_SRC / "core" / "knowledge_base.py",
    APP_SRC / "core" / "okf.py",
    APP_SRC / "core" / "page_search.py",
    APP_SRC / "core" / "records.py",
    APP_SRC / "core" / "retrieval.py",
    # Thin public re-exports kept for external importers; production code must
    # not use them as the preferred internal path.
    APP_SRC / "proposal_pipeline.py",
    APP_SRC / "source_processor.py",
}


def _iter_app_python_files() -> list[Path]:
    return sorted(p for p in APP_SRC.rglob("*.py") if p.is_file())


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_production_modules_do_not_import_compatibility_surfaces():
    """Internal callers import final owners, not lumio.core / pure re-exports."""
    forbidden_prefixes = (
        "lumio.core",
        "lumio.proposal_pipeline",
        "lumio.source_processor",
    )
    offenders: list[str] = []
    for path in _iter_app_python_files():
        if path in COMPATIBILITY_MODULES:
            continue
        for module in sorted(_imported_modules(path)):
            if module == "lumio.core" or module.startswith("lumio.core."):
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
            elif module in {"lumio.proposal_pipeline", "lumio.source_processor"}:
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
            elif any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in forbidden_prefixes
            ):
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert offenders == [], "internal callers still use compatibility surfaces:\n" + "\n".join(
        offenders
    )


def test_app_and_cli_select_retrieval_through_factory_not_adapter_types():
    """Client modules must not hard-import LanceDB adapter helpers directly."""
    client_files = [
        APP_SRC / "app.py",
        APP_SRC / "cli.py",
    ]
    forbidden = {
        "lumio.core.index",
        "lumio_lancedb",
        "lumio_lancedb.index",
    }
    offenders: list[str] = []
    for path in client_files:
        imported = _imported_modules(path)
        for module in sorted(imported & forbidden):
            offenders.append(f"{path.name} imports {module}")
        text = path.read_text(encoding="utf-8")
        if "build_lancedb_index" in text:
            offenders.append(f"{path.name} references build_lancedb_index directly")
        if "build_retrieval_index" not in text and path.name in {"app.py", "cli.py"}:
            offenders.append(f"{path.name} does not use build_retrieval_index")
    assert offenders == [], "retrieval selection is not contracted:\n" + "\n".join(offenders)


def test_compatibility_core_modules_are_thin_reexports_only():
    """Documented lumio.core.* modules must not own duplicate implementations."""
    for path in sorted(COMPATIBILITY_MODULES):
        if path.name in {"proposal_pipeline.py", "source_processor.py"}:
            # Pure re-export modules: only imports + __all__.
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            definitions = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            assert definitions == [], f"{path.name} must not define concrete behavior"
            continue
        if path.parent.name != "core":
            continue
        text = path.read_text(encoding="utf-8")
        assert "lumio_wiki" in text or "lumio_lancedb" in text, path
        # Alias modules rebind sys.modules; the package facade and index shim
        # are star-import re-exports. None may grow a second implementation.
        tree = ast.parse(text, filename=str(path))
        definitions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert definitions == [], f"{path.relative_to(ROOT)} owns concrete definitions"


def test_dependency_direction_remains_one_way():
    """lumio-wiki never depends downward; lancedb depends only on wiki."""
    forbidden_in_wiki = {"lumio", "lumio_lancedb", "lancedb", "pyarrow", "stario", "piccolo"}
    seen: set[str] = set()
    for path in WIKI_SRC.rglob("*.py"):
        for module in _imported_modules(path):
            seen.add(module.split(".", 1)[0])
    assert seen.isdisjoint(forbidden_in_wiki), sorted(seen & forbidden_in_wiki)

    for path in LANCEDB_SRC.rglob("*.py"):
        modules = _imported_modules(path)
        roots = {module.split(".", 1)[0] for module in modules}
        assert "lumio" not in roots or all(
            not module.startswith("lumio.") or module.startswith("lumio_wiki")
            for module in modules
        )
        assert "lumio" not in roots


def test_no_two_wheels_own_the_same_python_module_path():
    wiki_names = {p.relative_to(WIKI_SRC) for p in WIKI_SRC.rglob("*.py")}
    lance_names = {p.relative_to(LANCEDB_SRC) for p in LANCEDB_SRC.rglob("*.py")}
    app_names = {p.relative_to(APP_SRC) for p in APP_SRC.rglob("*.py")}
    # Concrete ownership is by import root, not relative path coincidence.
    assert not (ROOT / "packages" / "lumio-wiki" / "src" / "lumio").exists()
    assert not (ROOT / "packages" / "lumio-lancedb" / "src" / "lumio").exists()
    assert not (ROOT / "packages" / "lumio" / "src" / "lumio_wiki").exists()
    assert not (ROOT / "packages" / "lumio" / "src" / "lumio_lancedb").exists()
    assert wiki_names is not None and lance_names is not None and app_names is not None
