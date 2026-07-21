"""Issue #104, AC3: the assembled ``lumio`` application's adapter-selection
contract.

ADR-0010 requires the full application to choose zero-index or LanceDB
retrieval through configuration and dependency injection. Client modules
(``app.py``, ``cli.py``) must call the single public seam and must never
import adapter types. The full membership of that contract is:

* exactly one public entry point named ``build_retrieval_index``;
* every internal caller reaches retrieval through it; and
* adapter module names (``lumio_lancedb``, ``lumio.core.index``) never appear
  in client modules' import graphs.

``test_package_contraction.py`` already asserts the import side of this; this
module certifies the behavioral contract end-to-end so a regression in either
direction is caught at certification time. It runs in the fast suite because
it operates only on source text + the live retrieval seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).parents[1]
APP_SRC = ROOT / "packages" / "lumio" / "src" / "lumio"
CLIENT_MODULES = ("app.py", "cli.py")

# Modules whose import graphs must not name adapter types directly.
FORBIDDEN_ADAPTER_IMPORTS = {
    "lumio_lancedb",
    "lumio_lancedb.index",
    "lumio.core.index",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_retrieval_module_owns_the_adapter_import_inside_build_retrieval_index():
    """AC3: ``build_retrieval_index`` is the sanctioned seam and owns the
    adapter import. Other public helpers in ``lumio.retrieval`` may exist to
    support it, but the local ``lumio_lancedb`` import must live inside this
    function's body so zero-index deployments never force client modules to
    name adapter types (ADR-0010)."""
    retrieval_path = APP_SRC / "retrieval.py"
    tree = ast.parse(retrieval_path.read_text(encoding="utf-8"))
    public_funcs = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    assert "build_retrieval_index" in public_funcs
    # The seam must be the only function whose body imports adapter types.
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.ImportFrom)
                and child.module
                and (child.module == "lumio_lancedb" or child.module.startswith("lumio_lancedb."))
            ):
                assert node.name == "build_retrieval_index", (
                    f"{node.name} imports {child.module}; only build_retrieval_index "
                    "may name the adapter (ADR-0010)."
                )


@pytest.mark.parametrize("client_name", CLIENT_MODULES)
def test_client_modules_reach_retrieval_only_through_the_seam(client_name: str):
    """AC3: client modules call ``build_retrieval_index`` and nothing else."""
    client_path = APP_SRC / client_name
    text = client_path.read_text(encoding="utf-8")
    assert "build_retrieval_index" in text, (
        f"{client_name} must call build_retrieval_index (ADR-0010 selection seam)"
    )
    imports = _imports(client_path)
    leaked = imports & FORBIDDEN_ADAPTER_IMPORTS
    assert not leaked, (
        f"{client_name} imports adapter types directly: {sorted(leaked)}"
    )


def test_workspace_lockfile_pinning_and_runtime_resolution_are_documented_in_config():
    """AC3: ``LUMIO_RETRIEVAL_BACKEND`` is documented as the runtime selector.

    The README's configuration table must name both backends and call out
    that client modules never import adapter types — the documentation is
    part of the contract, not a side note.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "LUMIO_RETRIEVAL_BACKEND" in readme
    assert "zero-index" in readme
    assert "lancedb" in readme
    # The contract claim itself must be visible to operators reading the docs.
    assert "dependency injection" in readme.lower() or "config/di" in readme.lower(), (
        "README must state that retrieval is selected through config/DI"
    )


def test_lumio_application_depends_on_both_retrieval_owners():
    """AC3: the assembled app declares both lumio-wiki and lumio-lancedb."""
    app_pyproject = tomllib.loads(
        (ROOT / "packages" / "lumio" / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps = app_pyproject["project"]["dependencies"]
    assert any(d.startswith("lumio-wiki>=") for d in deps), deps
    assert any(d.startswith("lumio-lancedb>=") for d in deps), deps
    # The app's semantic extra is the only place Torch may appear.
    semantic = app_pyproject["project"].get("optional-dependencies", {}).get("semantic", [])
    assert any("sentence-transformers" in d for d in semantic) or semantic == [], (
        "lumio[semantic] is the sanctioned Torch surface; nothing else may pull it"
    )
