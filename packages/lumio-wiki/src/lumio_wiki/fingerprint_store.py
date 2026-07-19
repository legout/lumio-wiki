"""LanceDB-free source-fingerprint persistence.

Persists a Knowledge Base ``SourceFingerprint`` alongside a derived index so
later builds can detect a stale index. Extracted from the LanceDB index module
so zero-index builds can write a fingerprint into a fresh directory without the
optional ``lancedb`` / ``pyarrow`` dependencies.
"""

from __future__ import annotations

from pathlib import Path

import msgspec

from lumio_wiki.records import SourceFingerprint

FINGERPRINT_FILE = "fingerprint.json"


def save_stored_fingerprint(index_dir: Path, fingerprint: SourceFingerprint) -> None:
    """Persist a source fingerprint alongside the index.

    The index directory is created on demand so a zero-index build can persist
    a fingerprint into a fresh directory.
    """
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    path = index_dir / FINGERPRINT_FILE
    path.write_bytes(msgspec.json.encode(fingerprint))


def load_stored_fingerprint(index_dir: Path) -> SourceFingerprint | None:
    """Load the source fingerprint stored alongside the index, if any."""
    path = Path(index_dir) / FINGERPRINT_FILE
    if not path.exists():
        return None
    return msgspec.json.decode(path.read_bytes(), type=SourceFingerprint)
