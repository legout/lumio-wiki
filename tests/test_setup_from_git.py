"""Bounded git transport for ``setup --from`` (approved design 2026-09-15).

A non-``s3://`` ``--from`` value is a git clone URL: the CLI shells out to
``git clone`` with an argv list (never a shell), validates the clone as a
Knowledge Base, and records it as ``LUMIO_KB_PATH``. Everything else — the
missing-git bound, the clone-failure bound, the invalid-KB bound, and the
untouched ``s3://`` reader path — is proven here with local ``file://`` repos
only (no network).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest  # type: ignore[import-not-found]
from lumio_wiki import cli

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

_ENV_KEYS = ("LUMIO_KB_PATH", "LUMIO_PUBLISH_TO", "LUMIO_RETRIEVAL_BACKEND")


def _clean_env(monkeypatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _git(*argv: str, cwd: Path) -> None:
    """Run one git command with a fixed identity (test commits only)."""
    subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _origin_kb(tmp_path: Path, *, valid: bool) -> Path:
    """Create a local git repo holding a valid (or invalid) KB and return it.

    Lives under ``origin/`` so the default clone target (``team-kb``) in the
    project directory stays free.
    """
    origin = tmp_path / "origin" / "team-kb"
    origin.mkdir(parents=True)
    page = (
        FIXTURES / "valid" / "overview.md" if valid else (FIXTURES / "invalid" / "missing_title.md")
    )
    (origin / "overview.md").write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "kb", cwd=origin)
    return origin


def _env_value(project: Path, key: str) -> str | None:
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None


def test_setup_from_git_url_clones_valid_kb_and_writes_env(tmp_path, monkeypatch, capsys):
    """A file:// git URL clones, validates, and records LUMIO_KB_PATH (exit 0)."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    origin = _origin_kb(tmp_path, valid=True)

    rc = cli.main(["setup", "--from", f"file://{origin}"])

    assert rc == 0
    clone = tmp_path / "team-kb"
    assert (clone / "overview.md").exists()
    assert "Read-only Knowledge Base cloned at" in capsys.readouterr().out
    assert _env_value(tmp_path, "LUMIO_KB_PATH") == str(clone)
    # Reader-form wiring is preserved: .env + AGENTS.md, nothing else recorded.
    assert (tmp_path / "AGENTS.md").exists()
    assert "LUMIO_PUBLISH_TO" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_setup_from_git_missing_git_executable_is_bounded(tmp_path, monkeypatch, capsys):
    """Without a git executable, setup fails bounded and writes nothing."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    origin = _origin_kb(tmp_path, valid=True)
    empty_path = tmp_path / "no-git-bin"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    rc = cli.main(["setup", "--from", f"file://{origin}"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires the 'git' executable" in err
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "team-kb").exists()


def test_setup_from_git_failed_clone_is_bounded(tmp_path, monkeypatch, capsys):
    """A clone failure surfaces git's stderr summarized, with a non-zero exit."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)

    rc = cli.main(["setup", "--from", str(tmp_path / "does-not-exist")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--from git clone failed" in err
    assert "does-not-exist" in err  # git's own fatal line is summarized
    assert not (tmp_path / ".env").exists()


def test_setup_from_git_invalid_kb_clone_is_bounded(tmp_path, monkeypatch, capsys):
    """A cloned repo whose KB is invalid fails bounded; the clone is removed."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    origin = _origin_kb(tmp_path, valid=False)

    rc = cli.main(["setup", "--from", f"file://{origin}"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "not a valid Knowledge Base" in err
    assert "title" in err  # the concrete validation failure is reported
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "team-kb").exists()


def test_setup_from_git_invokes_git_clone_with_argv_list(tmp_path, monkeypatch, capsys):
    """The URL reaches ``git clone`` as one argv entry — never through a shell."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    calls: list[tuple[object, dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        target = Path(argv[3])  # [git, clone, <url>, <target>]
        target.mkdir(parents=True)
        (target / "overview.md").write_text(
            (FIXTURES / "valid" / "overview.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(["setup", "--from", "https://example.com/team/kb.git"])

    assert rc == 0
    argv, kwargs = calls[0]
    assert isinstance(argv, list) and all(isinstance(part, str) for part in argv)
    assert argv == ["git", "clone", "https://example.com/team/kb.git", str(tmp_path / "kb")]
    assert not kwargs.get("shell")


def test_setup_from_s3_uri_never_routes_to_git(tmp_path, monkeypatch, capsys):
    """s3:// values keep the S3 reader path: object-store rules, no git clone."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)

    def _no_git(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("s3:// --from must not use the git transport")

    monkeypatch.setattr(cli, "_clone_git_kb", _no_git)
    # An s3:// URI with embedded credentials is still refused by the
    # object-store URI rules (bounded, generic, never echoed).
    rc = cli.main(["setup", "--from", "s3://user:secret@bucket/kb"])

    assert rc == 2
    assert "must not include credentials" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
