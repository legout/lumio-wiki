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

- Reads *only* the explicit Lumio configuration allowlist
  (:data:`ENV_ALLOWLIST`, issue #161 / ADR-0019). It never loads arbitrary
  keys into ``os.environ``; credentials are deliberately outside the
  allowlist.
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

import os
from pathlib import Path
from typing import Final

#: The single Knowledge Base location key this loader reads (ADR-0017).
KB_PATH_ENV_VAR: Final[str] = "LUMIO_KB_PATH"

#: Optional S3 publication destination recorded by ``setup --publish-to``
#: (issue #161, ADR-0019). An object-store URI such as ``s3://bucket/kb``.
PUBLISH_TO_ENV_VAR: Final[str] = "LUMIO_PUBLISH_TO"

#: Optional private Source Artifact Store URI recorded by ``setup
#: --source-store`` (issue #161, ADR-0020). Private; never commit it.
SOURCE_STORE_ENV_VAR: Final[str] = "LUMIO_SOURCE_STORE"

#: Source Artifact retention policy (issue #164, ADR-0020): ``required``
#: blocks publication activation while a referenced non-synthetic source
#: lacks a verified artifact; the default (unset/``disabled``) preserves the
#: hash-only behavior. Recorded by ``setup --artifact-retention``.
ARTIFACT_RETENTION_ENV_VAR: Final[str] = "LUMIO_ARTIFACT_RETENTION"

#: Retrieval *backend* choice (``zero-index`` or ``lancedb``). Deliberately a
#: separate key from the retrieval *mode* below: the backend selects the
#: adapter, the mode selects lexical/semantic/hybrid behavior (issue #161,
#: ADR-0019).
RETRIEVAL_BACKEND_ENV_VAR: Final[str] = "LUMIO_RETRIEVAL_BACKEND"

#: Retrieval *mode* choice (``lexical``/``semantic``/``hybrid``).
RETRIEVAL_MODE_ENV_VAR: Final[str] = "LUMIO_RETRIEVAL_MODE"

#: Optional public Reader deployment base URL (issue #177). An ``http(s)``
#: origin such as ``https://lumio.example.com``; when configured, CLI
#: citation output labels browser links against it. Validated and normalized
#: by :func:`lumio_wiki.citation_actions.normalize_reader_base_url`; never
#: derived from S3 object locations.
READER_BASE_URL_ENV_VAR: Final[str] = "LUMIO_READER_BASE_URL"

#: S3-compatible connection settings. Deployment configuration: ``setup``
#: never writes them, but they may be recorded in a project ``.env`` and are
#: read with exported-process precedence.
S3_REGION_ENV_VAR: Final[str] = "LUMIO_S3_REGION"
S3_ENDPOINT_ENV_VAR: Final[str] = "LUMIO_S3_ENDPOINT"

#: The explicit Lumio configuration allowlist (issue #161, ADR-0019).
#: ``.env`` discovery loads ONLY these keys; arbitrary project keys never
#: enter ``os.environ``. Credential keys are deliberately absent: setup never
#: writes credentials and standard AWS credential resolution stays
#: authoritative.
ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        KB_PATH_ENV_VAR,
        PUBLISH_TO_ENV_VAR,
        SOURCE_STORE_ENV_VAR,
        ARTIFACT_RETENTION_ENV_VAR,
        RETRIEVAL_BACKEND_ENV_VAR,
        RETRIEVAL_MODE_ENV_VAR,
        S3_REGION_ENV_VAR,
        S3_ENDPOINT_ENV_VAR,
        READER_BASE_URL_ENV_VAR,
    }
)

#: Version-control directories that mark a trusted project root (ADR-0017).
#: The nearest ancestor containing one bounds ``.env`` discovery from above.
PROJECT_ROOT_MARKERS: tuple[str, ...] = (".git", ".hg")

#: Characters whose presence around a ``.env`` value mark a quoted string that
#: should be unquoted.
_QUOTE_CHARS = ("'", '"')

#: URI schemes accepted by the CLI's object-store Knowledge Base resolver.
_OBJECT_STORE_SCHEMES: Final[frozenset[str]] = frozenset({"s3", "s3a", "gs", "gcs", "az", "abfs"})


def _read_env_value(text: str, key: str) -> str | None:
    """Return the non-empty ``key`` value in parsed ``.env`` text, or ``None``.

    Skips blank lines and ``#`` comments; unquotes values fully wrapped in
    matching quotes. Never mutates ``os.environ``.
    """
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
    return _read_env_value(text, KB_PATH_ENV_VAR)


def read_env_allowlist(env_path: Path) -> dict[str, str]:
    """Read only the explicit Lumio allowlist keys from ``env_path``.

    Returns a mapping of allowlisted keys to their non-empty raw values. The
    ``LUMIO_KB_PATH`` value is resolved against the ``.env`` directory like
    the dedicated reader; every other allowlisted key is a URI or a plain
    token and passes through unchanged. Never mutates ``os.environ`` and never
    returns a key outside :data:`ENV_ALLOWLIST`.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for key in sorted(ENV_ALLOWLIST):
        value = _read_env_value(text, key)
        if value is None:
            continue
        if key == KB_PATH_ENV_VAR:
            try:
                value = resolve_env_value(value, env_path.parent)
            except (OSError, RuntimeError, ValueError):
                # Malformed path values become the same actionable
                # missing-path diagnostic as a missing key.
                continue
        values[key] = value
    return values


def resolve_env_value(value: str, env_dir: str | Path) -> str:
    """Resolve a ``.env`` value relative to the ``.env`` file's directory.

    Absolute values and supported object-store URIs pass through unchanged.
    Relative local paths resolve against ``env_dir`` (the directory containing
    the ``.env`` file), never against the current working directory. Embedded
    NULs are rejected so malformed values become an actionable CLI diagnostic.
    """
    if "\x00" in value:
        raise ValueError("LUMIO_KB_PATH contains an embedded NUL")
    if "://" in value and value.split("://", 1)[0].lower() in _OBJECT_STORE_SCHEMES:
        return value
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


def discover_nearest_env_file(start_dir: str | Path | None = None) -> Path | None:
    """Return the nearest trusted project ``.env`` path, or ``None``.

    Walks from ``start_dir`` (default: the current working directory) up to the
    trusted project root and returns the first existing ``.env``. The nearest
    existing file is authoritative for every reader in this module; ``None``
    means no project ``.env`` exists within the trusted boundary.
    """
    current = Path(start_dir).resolve() if start_dir is not None else Path.cwd()
    boundary = _trusted_project_root(current)
    while True:
        env_path = current / ".env"
        if env_path.exists():
            return env_path
        if current == boundary:
            return None
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
    env_path = discover_nearest_env_file(start_dir)
    if env_path is None:
        return None
    value = read_kb_path_from_env_file(env_path)
    if not value:
        return None
    try:
        return resolve_env_value(value, env_path.parent)
    except (OSError, RuntimeError, ValueError):
        # Malformed path values (including embedded NULs and symlink loops)
        # become the same actionable missing-path diagnostic.
        return None


def load_project_config(start_dir: str | Path | None = None) -> dict[str, str]:
    """Load the explicit Lumio allowlist with exported-process precedence.

    For each allowlisted key, an exported process value retains precedence
    over the project ``.env`` value (issue #161, ADR-0019): an exported key is
    used as-is when non-empty, and an exported-but-empty value never falls
    back to ``.env``. Keys present in neither source are omitted. Never
    mutates ``os.environ`` and never returns keys outside
    :data:`ENV_ALLOWLIST`.
    """
    env_path = discover_nearest_env_file(start_dir)
    file_values = read_env_allowlist(env_path) if env_path is not None else {}
    config: dict[str, str] = {}
    for key in sorted(ENV_ALLOWLIST):
        if key in os.environ:
            exported = os.environ[key]
            if exported.strip():
                config[key] = exported
        elif key in file_values:
            config[key] = file_values[key]
    return config
