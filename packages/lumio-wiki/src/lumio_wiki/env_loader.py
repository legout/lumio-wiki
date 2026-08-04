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
- A missing, empty, malformed, or nonexistent ``.env`` yields ``None`` so the
  caller surfaces a single actionable error rather than a traceback.
- Discovery never searches above the nearest trusted project root (a directory
  containing a version-control marker).
"""

from __future__ import annotations

from pathlib import Path

#: The single configuration key this loader reads.
KB_PATH_ENV_VAR = "LUMIO_KB_PATH"

#: Version-control directories that mark a trusted project root (ADR-0017).
#: ``.env`` discovery never searches above the nearest directory containing one
#: of these markers. When no marker is found, the walk is bounded by the
#: filesystem root.
PROJECT_ROOT_MARKERS: tuple[str, ...] = (".git", ".hg")

#: Characters whose presence around a ``.env`` value mark a quoted string that
#: should be unquoted.
_QUOTE_CHARS = ("'", '"')


def read_key_from_env_file(env_path: Path, key: str) -> str | None:
    """Return the stripped value of ``key`` in ``env_path``, or ``None``.

    Reads only ``key`` and never mutates ``os.environ``. A missing file, a file
    without the key, an empty value, or a malformed line all yield ``None`` so
    the caller can keep walking up or surface one actionable error.

    ``KEY=value`` lines are matched on the key prefix so a value may legally
    contain ``=``. Surrounding single or double quotes are stripped only when
    they wrap the whole value. Blank lines and ``#`` comments are skipped.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    prefix = f"{key}="
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


def discover_kb_path_from_project_env(start_dir: str | Path | None = None) -> str | None:
    """Discover ``LUMIO_KB_PATH`` from the nearest trusted project ``.env``.

    Walks from ``start_dir`` (default: the current working directory) up through
    ancestor directories. At each directory it reads ``LUMIO_KB_PATH`` from a
    ``.env`` file when one is present. The first non-empty match (nearest to the
    start directory) wins and is resolved relative to that ``.env`` file's
    directory.

    The walk never crosses above the nearest trusted project root (a directory
    containing a version-control marker). When no project root is found, the
    walk is bounded by the filesystem root. Returns ``None`` when no value is
    found, leaving the caller to surface an actionable error.
    """
    current = Path(start_dir).resolve() if start_dir is not None else Path.cwd()
    while True:
        value = read_key_from_env_file(current / ".env", KB_PATH_ENV_VAR)
        if value:
            return resolve_env_value(value, current)
        if _is_project_root(current) or current == current.parent:
            return None
        current = current.parent
