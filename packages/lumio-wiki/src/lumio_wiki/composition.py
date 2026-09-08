"""Optional capability composition for the CLI and evaluation tools.

The canonical ``lumio-wiki`` SDK stays dependency-neutral.  This small module
is the one explicit boundary where CLI/evaluation code may discover and lazily
construct the separately installed LanceDB adapter.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from typing import Any

from lumio_wiki.retrieval import RetrievalAdapter


class OptionalCapabilityError(RuntimeError):
    """An explicitly requested optional adapter is unavailable."""


def lancedb_available() -> bool:
    """Return whether all modules required by ``lumio-lancedb`` are installed.

    ``find_spec`` performs discovery without importing the optional package.
    The legacy evaluation alias is honored when a caller has explicitly
    monkeypatched it; normal CLI use never imports that module.
    """
    legacy = sys.modules.get("lumio_wiki.retrieval_eval")
    if legacy is not None:
        override = legacy.__dict__.get("lancedb_available")
        if override is not None and getattr(override, "__module__", None) != __name__:
            return bool(override())
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("lumio_lancedb", "lancedb", "pyarrow")
    )


def load_lancedb_module() -> Any | None:
    """Load the optional LanceDB package only after capability discovery."""
    if not lancedb_available():
        return None
    try:
        return importlib.import_module("lumio_lancedb")
    except Exception:
        return None


def load_lancedb_adapter() -> RetrievalAdapter | None:
    """Construct the optional LanceDB retrieval adapter, or return ``None``."""
    module = load_lancedb_module()
    if module is None:
        return None
    try:
        return module.LanceDBRetrievalAdapter()
    except Exception:
        return None


def require_lancedb() -> Any:
    """Return the optional module or raise a stable actionable error."""
    module = load_lancedb_module()
    if module is None:
        raise OptionalCapabilityError(
            "lumio-lancedb is required; install it with: pip install lumio-lancedb"
        )
    return module


__all__ = [
    "OptionalCapabilityError",
    "lancedb_available",
    "load_lancedb_adapter",
    "load_lancedb_module",
    "require_lancedb",
]
