"""Unit tests for the project ``.env`` loader (issue #152).

The CLI must resolve ``LUMIO_KB_PATH`` from the nearest trusted project
``.env`` so a fresh subprocess honors what ``lumio-wiki setup`` wrote. These
tests cover the parser and discovery directly; the CLI precedence and the
authoritative fresh-subprocess journey live in ``test_cli.py``.

Constraints under test (ADR-0017):

- reads only ``LUMIO_KB_PATH`` (never loads arbitrary keys into ``os.environ``);
- relative values resolve against the ``.env`` directory;
- missing/empty/malformed/nonexistent ``.env`` is safe;
- discovery stops at the nearest trusted project root.
"""

from __future__ import annotations

import os
from pathlib import Path

from lumio_wiki.env_loader import (
    KB_PATH_ENV_VAR,
    discover_kb_path_from_project_env,
    read_key_from_env_file,
    resolve_env_value,
)


def test_read_key_returns_value_for_simple_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"{KB_PATH_ENV_VAR}=/opt/kb\n", encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) == "/opt/kb"


def test_read_key_strips_surrounding_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f'{KB_PATH_ENV_VAR}="/opt/my kb"\n', encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) == "/opt/my kb"


def test_read_key_only_unquotes_when_value_is_fully_wrapped(tmp_path: Path) -> None:
    # A value with a stray internal quote but no matching wrap is left intact.
    env = tmp_path / ".env"
    env.write_text(f'{KB_PATH_ENV_VAR}=path"with quote\n', encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) == 'path"with quote'


def test_read_key_allows_equals_in_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"{KB_PATH_ENV_VAR}=a=b=c\n", encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) == "a=b=c"


def test_read_key_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        f"   {KB_PATH_ENV_VAR}=  /spaced  \n"
        "OTHER=ignore\n",
        encoding="utf-8",
    )
    # Leading/trailing whitespace around the value is stripped.
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) == "/spaced"


def test_read_key_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert read_key_from_env_file(tmp_path / ".env", KB_PATH_ENV_VAR) is None


def test_read_key_returns_none_when_key_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=value\n", encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) is None


def test_read_key_returns_none_for_empty_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"{KB_PATH_ENV_VAR}=\n", encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) is None


def test_read_key_returns_none_for_malformed_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    # A line with no '=' is malformed and ignored.
    env.write_text("not-a-key-value-line\n", encoding="utf-8")
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) is None


def test_read_key_reads_only_requested_key(tmp_path: Path) -> None:
    """The loader reads a named key; it never scans every key in the file."""
    env = tmp_path / ".env"
    env.write_text(
        f"{KB_PATH_ENV_VAR}=/kb\nSECRET=should-not-be-touched\n",
        encoding="utf-8",
    )
    assert read_key_from_env_file(env, KB_PATH_ENV_VAR) == "/kb"
    # Asking for a different key still works (single-key read), but the loader
    # never loads arbitrary keys into os.environ (asserted separately).
    assert read_key_from_env_file(env, "SECRET") == "should-not-be-touched"


def test_resolve_env_value_keeps_absolute_path() -> None:
    assert resolve_env_value("/opt/kb", "/some/project") == "/opt/kb"


def test_resolve_env_value_makes_relative_against_env_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    resolved = resolve_env_value("./knowledge-base", project)
    assert Path(resolved).is_absolute()
    assert Path(resolved) == (project / "knowledge-base").resolve()


def test_discover_finds_env_in_start_dir(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (tmp_path / ".env").write_text(f"{KB_PATH_ENV_VAR}={kb}\n", encoding="utf-8")
    assert discover_kb_path_from_project_env(tmp_path) == str(kb)


def test_discover_walks_up_to_parent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    kb = project / "kb"
    kb.mkdir()
    # .env lives in the project root; the command runs from a subdirectory.
    (project / ".env").write_text(f"{KB_PATH_ENV_VAR}={kb}\n", encoding="utf-8")
    assert discover_kb_path_from_project_env(subdir) == str(kb)


def test_discover_nearest_env_wins(tmp_path: Path) -> None:
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    near_kb = subdir / "near"
    near_kb.mkdir()
    far_kb = project / "far"
    far_kb.mkdir()
    (subdir / ".env").write_text(f"{KB_PATH_ENV_VAR}={near_kb}\n", encoding="utf-8")
    (project / ".env").write_text(f"{KB_PATH_ENV_VAR}={far_kb}\n", encoding="utf-8")
    assert discover_kb_path_from_project_env(subdir) == str(near_kb)


def test_discover_resolves_relative_value_against_env_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(f"{KB_PATH_ENV_VAR}=./wiki\n", encoding="utf-8")
    discovered = discover_kb_path_from_project_env(project)
    assert discovered is not None
    assert Path(discovered) == (project / "wiki").resolve()


def test_discover_returns_none_when_no_env_anywhere(tmp_path: Path) -> None:
    # A tmp_path with no .env and no VCS root walks up but finds nothing.
    assert discover_kb_path_from_project_env(tmp_path) is None


def test_discover_ignores_empty_value_and_keeps_searching(tmp_path: Path) -> None:
    """An empty value in a nearer .env does not stop the walk."""
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    real_kb = project / "kb"
    real_kb.mkdir()
    # Nearer .env has an empty value; the parent's is used instead.
    (subdir / ".env").write_text(f"{KB_PATH_ENV_VAR}=\n", encoding="utf-8")
    (project / ".env").write_text(f"{KB_PATH_ENV_VAR}={real_kb}\n", encoding="utf-8")
    assert discover_kb_path_from_project_env(subdir) == str(real_kb)


def test_discover_stops_at_trusted_project_root(tmp_path: Path) -> None:
    """Discovery never searches above a version-control project root."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # The project is a git root; an .env ABOVE it must never be read.
    (project / ".git").mkdir()
    (outside / ".env").write_text(f"{KB_PATH_ENV_VAR}=/should/never/win\n", encoding="utf-8")
    # The project root has no .env, so discovery returns None despite the
    # outside .env existing above it.
    assert discover_kb_path_from_project_env(project) is None


def test_discover_reads_env_at_project_root_before_stopping(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    kb = project / "kb"
    kb.mkdir()
    (project / ".env").write_text(f"{KB_PATH_ENV_VAR}={kb}\n", encoding="utf-8")
    # The root's own .env is read before the boundary terminates the walk.
    assert discover_kb_path_from_project_env(project) == str(kb)


def test_loader_never_pollutes_os_environ(tmp_path: Path) -> None:
    """Reading a .env must not inject arbitrary keys into os.environ."""
    env = tmp_path / ".env"
    arbitrary_key = "LUMIO_TEST_ARBITRARY_KEY_152"
    env.write_text(
        f"{KB_PATH_ENV_VAR}=/kb\n{arbitrary_key}=secret\n",
        encoding="utf-8",
    )
    os.environ.pop(arbitrary_key, None)
    try:
        discover_kb_path_from_project_env(tmp_path)
        assert arbitrary_key not in os.environ
    finally:
        os.environ.pop(arbitrary_key, None)
