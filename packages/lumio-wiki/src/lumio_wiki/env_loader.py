"""Project ``.env`` discovery for the ``LUMIO_KB_PATH`` configuration key.

A fresh ``lumio-wiki`` subprocess resolves the Knowledge Base path through a
deterministic precedence (issue #152, ADR-0017):

1. an explicit positional ``<kb>`` argument (handled by argparse);
2. an exported process ``LUMIO_KB_PATH`` environment variable;
3. ``LUMIO_KB_PATH`` read from the nearest project ``.env`` from the current
   working directory up to the trusted project root; and
4. an actionable missing-path error.

This module owns step 3 only. The CLI combines it with the exported-variable
check (step 2) so an exported value is never overwritten by a ``.env`` file.

Design constraints (ADR-0017):

- Reads a *single* configuration key (``LUMIO_KB_PATH``). It never loads
  arbitrary keys into ``os.environ``.
- Has no third-party dependencies, so the lightweight base wheel resolves a
  project Knowledge Base without importing the full Lumio application.
- Relative values resolve against the directory that contains the ``.env``
  file, never against the current working directory.
- Discovery skips directories without a ``.env`` file. The nearest existing
  ``.env`` is authoritative: a missing key, malformed content, or empty value
  yields ``None`` (an actionable error) rather than silently falling through
  to an ancestor value.
- Discovery never searches above the trusted project root. The root is the
  nearest ancestor (inclusive) containing a version-control marker; when none
  exists, the invocation directory itself is the trusted root, so its ``.env``
  is read but ancestors are never walked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: The single configuration key this loader reads.
KB_PATH_ENV_VAR: Final[str] = "LUMIO_KB_PATH"

#: Version-control directories that mark a trusted project root (ADR-0017).
#: The nearest ancestor containing one bounds ``.env`` discovery from above.
PROJECT_ROOT_MARKERS: tuple[str, ...] = (".git", ".hg")

#: Characters whose presence around a ``.env`` value mark a quoted string that
#: should be unquoted.
_QUOTE_CHARS = ("'", '"')


def read_kb_path_from_env_file(env_path: Path) -> str | None:
    """Return the non-empty ``LUMIO_KB_PATH`` value in ``env_path``, or ``None``.

    Reads only ``LUMIO_KB_PATH`` and never mutates ``os.environ``. Returns
    ``None`` for a missing/unreadable file, a file without the key, malformed
    content, or an empty value. Discovery treats an existing file as
    authoritative, so these invalid states become an actionable CLI error
    rather than falling through to an ancestor file.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    prefix = f"{KB_PATH_ENV_VAR}="
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] in _QUOTE_CHARS and value[-1] == value[0]:
            value = value[1:-1].strip()
        return value or None
    return None


def resolve_env_value(value: str, env_dir: str | Path) -> str:
    """Resolve a ``.env`` value relative to the ``.env`` file's directory.

    Absolute values pass through unchanged. Relative values resolve against
    ``env_dir`` (the directory containing the ``.env`` file), never against the
    current working directory.
    """
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((Path(env_dir) / path).resolve())


def _is_project_root(directory: Path) -> bool:
    """Return whether ``directory`` is a trusted project-root boundary."""
    return any((directory / marker).exists() for marker in PROJECT_ROOT_MARKERS)


def _trusted_project_root(start: Path) -> Path:
    """Return the trusted project root bounding discovery from ``start``.

    Walks from ``start`` upward and returns the nearest directory containing a
    version-control marker (ADR-0017). When no marker is found, ``start``
    itself is the trusted root: the invocation directory's ``.env`` is read,
    but ancestors are never walked into untrusted territory.
    """
    current = start
    while True:
        if _is_project_root(current):
            return current
        if current == current.parent:
            return start
        current = current.parent


def discover_kb_path_from_project_env(start_dir: str | Path | None = None) -> str | None:
    """Discover ``LUMIO_KB_PATH`` from the nearest trusted project ``.env``.

    Walks from ``start_dir`` (default: the current working directory) up to the
    trusted project root. At each directory it reads ``LUMIO_KB_PATH`` from a
    ``.env`` file. The nearest existing ``.env`` is authoritative: a
    non-empty value is resolved relative to that ``.env`` file's directory
    and returned; a missing key, malformed content, or empty value returns
    ``None`` (an actionable error) rather than silently falling through to an
    ancestor.

    The walk never crosses above the trusted project root; when no
    version-control marker is found, the invocation directory is the root.
    Returns ``None`` when no authoritative value is found.
    """
    current = Path(start_dir).resolve() if start_dir is not None else Path.cwd()
    boundary = _trusted_project_root(current)
    while True:
        env_path = current / ".env"
        if env_path.exists():
            value = read_kb_path_from_env_file(env_path)
            return resolve_env_value(value, current) if value else None
        if current == boundary:
            return None
        current = current.parent
