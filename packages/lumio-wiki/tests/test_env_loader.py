"""Unit tests for the project ``.env`` loader (issue #152).

The CLI must resolve ``LUMIO_KB_PATH`` from the nearest trusted project
``.env`` so a fresh subprocess honors what ``lumio-wiki setup`` wrote. These
tests cover the parser and discovery directly; the CLI precedence and the
authoritative fresh-subprocess journey live in ``test_cli.py``.

Constraints under test (ADR-0017):

- reads only ``LUMIO_KB_PATH`` (never loads arbitrary keys into ``os.environ``);
- relative values resolve against the ``.env`` directory;
- the nearest ``.env`` containing the key is authoritative (empty -> error, no
  fall-through to an ancestor);
- discovery is bounded by the trusted project root; the invocation directory is
  the root when no version-control marker is found.
"""

from __future__ import annotations

import os
from pathlib import Path

from lumio_wiki.env_loader import (
    KB_PATH_ENV_VAR,
    discover_kb_path_from_project_env,
    read_kb_path_from_env_file,
    resolve_env_value,
)


def _write_env(directory: Path, value: str) -> Path:
    """Write a .env file in ``directory`` with ``LUMIO_KB_PATH=<value>``."""
    env = directory / ".env"
    env.write_text(f"{KB_PATH_ENV_VAR}={value}\n", encoding="utf-8")
    return env


def test_read_returns_value_for_simple_line(tmp_path: Path) -> None:
    _write_env(tmp_path, "/opt/kb")
    assert read_kb_path_from_env_file(tmp_path / ".env") == "/opt/kb"


def test_read_strips_surrounding_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f'{KB_PATH_ENV_VAR}="/opt/my kb"\n', encoding="utf-8")
    assert read_kb_path_from_env_file(env) == "/opt/my kb"


def test_read_only_unquotes_when_value_is_fully_wrapped(tmp_path: Path) -> None:
    # A value with a stray internal quote but no matching wrap is left intact.
    env = tmp_path / ".env"
    env.write_text(f'{KB_PATH_ENV_VAR}=path"with quote\n', encoding="utf-8")
    assert read_kb_path_from_env_file(env) == 'path"with quote'


def test_read_allows_equals_in_value(tmp_path: Path) -> None:
    _write_env(tmp_path, "a=b=c")
    assert read_kb_path_from_env_file(tmp_path / ".env") == "a=b=c"


def test_read_skips_comments_blank_lines_and_other_keys(tmp_path: Path) -> None:
    """Only LUMIO_KB_PATH is read; other keys and comments are ignored."""
    env = tmp_path / ".env"
    env.write_text(
        f"# a comment\n\n   {KB_PATH_ENV_VAR}=  /spaced  \nOTHER=ignore\n",
        encoding="utf-8",
    )
    # Leading/trailing whitespace around the value is stripped.
    assert read_kb_path_from_env_file(env) == "/spaced"


def test_read_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert read_kb_path_from_env_file(tmp_path / ".env") is None


def test_read_returns_none_when_key_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=value\n", encoding="utf-8")
    assert read_kb_path_from_env_file(env) is None


def test_read_returns_none_for_empty_value(tmp_path: Path) -> None:
    _write_env(tmp_path, "")
    assert read_kb_path_from_env_file(tmp_path / ".env") is None


def test_read_returns_none_for_malformed_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    # A line with no '=' is malformed and ignored.
    env.write_text("not-a-key-value-line\n", encoding="utf-8")
    assert read_kb_path_from_env_file(env) is None


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
    _write_env(tmp_path, str(kb))
    assert discover_kb_path_from_project_env(tmp_path) == str(kb)


def test_discover_walks_up_within_a_trusted_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    kb = project / "kb"
    kb.mkdir()
    # The project root bounds the walk; .env lives at the root and the command
    # runs from a subdirectory.
    (project / ".git").mkdir()
    _write_env(project, str(kb))
    assert discover_kb_path_from_project_env(subdir) == str(kb)


def test_discover_nearest_env_wins(tmp_path: Path) -> None:
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    near_kb = subdir / "near"
    near_kb.mkdir()
    far_kb = project / "far"
    far_kb.mkdir()
    (project / ".git").mkdir()
    _write_env(subdir, str(near_kb))
    _write_env(project, str(far_kb))
    assert discover_kb_path_from_project_env(subdir) == str(near_kb)


def test_discover_resolves_relative_value_against_env_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_env(project, "./wiki")
    discovered = discover_kb_path_from_project_env(project)
    assert discovered is not None
    assert Path(discovered) == (project / "wiki").resolve()


def test_discover_returns_none_when_no_env_in_trusted_root(tmp_path: Path) -> None:
    # tmp_path has no .env and no VCS root, so the invocation dir is the root.
    assert discover_kb_path_from_project_env(tmp_path) is None


def test_discover_does_not_walk_above_invocation_dir_without_vcs(tmp_path: Path) -> None:
    """Without a VCS root the invocation dir is the trusted root: ancestors
    are never walked (ADR-0017 'bounded' discovery)."""
    project = tmp_path / "project"
    project.mkdir()
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    # Only the PARENT has a .env; no .git anywhere -> subdir is the root.
    _write_env(project, "/should/never/win")
    assert discover_kb_path_from_project_env(subdir) is None


def test_discover_treats_empty_nearest_value_as_authoritative(tmp_path: Path) -> None:
    """A nearest .env with an empty LUMIO_KB_PATH is authoritative: it yields
    None (an actionable error) rather than falling through to an ancestor."""
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    real_kb = project / "kb"
    real_kb.mkdir()
    (project / ".git").mkdir()
    # Nearest .env has an empty value; the parent has a real one.
    _write_env(subdir, "")
    _write_env(project, str(real_kb))
    assert discover_kb_path_from_project_env(subdir) is None


def test_discover_malformed_nearest_env_is_authoritative(tmp_path: Path) -> None:
    """A nearest .env without LUMIO_KB_PATH yields an actionable error rather
    than silently selecting an ancestor Knowledge Base path."""
    project = tmp_path / "project"
    subdir = project / "subdir"
    subdir.mkdir(parents=True)
    kb = project / "kb"
    kb.mkdir()
    (project / ".git").mkdir()
    # The nearest .env is malformed for this feature; the parent has a valid
    # path, but it must not mask the nearest file's invalid configuration.
    (subdir / ".env").write_text("UNRELATED=ignored\n", encoding="utf-8")
    _write_env(project, str(kb))
    assert discover_kb_path_from_project_env(subdir) is None


def test_discover_stops_at_trusted_project_root(tmp_path: Path) -> None:
    """Discovery never searches above a version-control project root."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # The project is a git root; an .env ABOVE it must never be read.
    (project / ".git").mkdir()
    _write_env(outside, "/should/never/win")
    # The project root has no .env, so discovery returns None despite the
    # outside .env existing above it.
    assert discover_kb_path_from_project_env(project) is None


def test_discover_reads_env_at_project_root_before_stopping(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    kb = project / "kb"
    kb.mkdir()
    _write_env(project, str(kb))
    # The root's own .env is read before the boundary terminates the walk.
    assert discover_kb_path_from_project_env(project) == str(kb)


def test_loader_never_pollutes_os_environ(tmp_path: Path) -> None:
    """Reading a .env must not inject arbitrary keys into os.environ."""
    arbitrary_key = "LUMIO_TEST_ARBITRARY_KEY_152"
    env = tmp_path / ".env"
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
