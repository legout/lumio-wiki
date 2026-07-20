"""Locate and install the packaged Agent Skill and coding-agent protocol.

The Agent Skill (``SKILL.md``) and the short coding-agent protocol
(``PROTOCOL.md``) ship as wheel data under ``lumio_wiki/data/`` so a coding
agent can resolve them deterministically via :mod:`importlib.resources`
without cloning the Lumio repository. This module is the single source of
truth for resolving those paths and for installing the skill into a coding
agent's conventional skill directory.

Supported coding agents (ADR-0003 names the mattpocock skill backbone; this
list is the minimum set called out by the parent PRD #93 user story 10):

============  ==============================================================
Agent          Conventional skill directory
============  ==============================================================
pi            ``~/.pi/agent/skills/lumio-wiki/``
hermes        ``~/.hermes/skills/lumio-wiki/``
codex         ``~/.codex/skills/lumio-wiki/``
claude-code   ``~/.claude/skills/lumio-wiki/``
============  ==============================================================

The install is a plain filesystem copy: no templating, no network. The
``--overwrite`` flag controls whether an existing destination is replaced.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

SKILL_MODULE = "lumio_wiki.data.skill"
PROTOCOL_MODULE = "lumio_wiki.data.protocol"
SKILL_FILENAME = "SKILL.md"
PROTOCOL_FILENAME = "PROTOCOL.md"

#: The directory name the skill installs under inside an agent's skill tree.
#: Matches the package distribution name so ``/<agent-skills>/lumio-wiki/``
#: is unambiguous across agents.
SKILL_DIR_NAME: Final = "lumio-wiki"

#: Supported coding agents and their conventional skill base directories.
SUPPORTED_AGENTS: Final[dict[str, str]] = {
    "pi": ".pi/agent/skills",
    "hermes": ".hermes/skills",
    "codex": ".codex/skills",
    "claude-code": ".claude/skills",
}


class SkillError(Exception):
    """A skill locate/install operation failed."""


def _resource_path(package: str, resource: str) -> Path:
    """Resolve an absolute path to a packaged resource file.

    Uses :func:`importlib.resources.files` so the path resolves correctly
    under a normal install, an editable install, AND a built wheel.
    """
    from importlib.resources import files

    return Path(str(files(package).joinpath(resource)))


def resolve_skill_path() -> Path:
    """Return the absolute path of the packaged ``SKILL.md``."""
    return _resource_path(SKILL_MODULE, SKILL_FILENAME)


def resolve_protocol_path() -> Path:
    """Return the absolute path of the packaged ``PROTOCOL.md``."""
    return _resource_path(PROTOCOL_MODULE, PROTOCOL_FILENAME)


def resolve_skill_directory() -> Path:
    """Return the absolute directory holding the packaged skill data."""
    from importlib.resources import files

    return Path(str(files(SKILL_MODULE)))


def resolve_protocol_directory() -> Path:
    """Return the absolute directory holding the packaged protocol data."""
    from importlib.resources import files

    return Path(str(files(PROTOCOL_MODULE)))


def agent_skill_dir(agent: str, *, home: Path | None = None) -> Path:
    """Return the conventional skill destination directory for ``agent``.

    ``home`` defaults to the current user's home directory. The returned path
    is ``<home>/<agent-relative-skills>/lumio-wiki/``.
    """
    if agent not in SUPPORTED_AGENTS:
        raise SkillError(
            f"unsupported agent {agent!r}; supported: {sorted(SUPPORTED_AGENTS)}"
        )
    base = home if home is not None else Path.home()
    return base / SUPPORTED_AGENTS[agent] / SKILL_DIR_NAME


def install_skill(
    agent: str,
    *,
    dest: Path | None = None,
    overwrite: bool = False,
    home: Path | None = None,
) -> Path:
    """Install the packaged Agent Skill and protocol for ``agent``.

    Copies ``SKILL.md`` and ``PROTOCOL.md`` into the destination directory
    (defaulting to the agent's conventional skill directory). Creates the
    destination if needed. Raises :class:`SkillError` if the destination
    already contains a ``SKILL.md`` and ``overwrite`` is false, or if a
    filesystem error prevents the install.

    The install is atomic: if either copy fails, any partially-copied file
    is removed so the destination never holds a half-installed skill. Both
    files are checked for existence before ``overwrite`` is decided, so a
    stale ``PROTOCOL.md`` without a ``SKILL.md`` does not get silently
    replaced when ``overwrite`` is false.

    Returns the destination directory.
    """
    source_skill = resolve_skill_path()
    source_protocol = resolve_protocol_path()
    if not source_skill.is_file():
        raise SkillError(f"packaged skill not found: {source_skill}")
    if not source_protocol.is_file():
        raise SkillError(f"packaged protocol not found: {source_protocol}")

    target_dir = Path(dest) if dest is not None else agent_skill_dir(agent, home=home)
    target_skill = target_dir / SKILL_FILENAME
    target_protocol = target_dir / PROTOCOL_FILENAME
    if (target_skill.exists() or target_protocol.exists()) and not overwrite:
        raise SkillError(
            f"destination already contains a skill file: {target_dir} "
            f"(pass overwrite=True to replace)"
        )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # Copy to temp names first, then rename atomically so a failure
        # between the two copies leaves no partial installation.
        tmp_skill = target_dir / f".{SKILL_FILENAME}.tmp"
        tmp_protocol = target_dir / f".{PROTOCOL_FILENAME}.tmp"
        shutil.copyfile(source_skill, tmp_skill)
        shutil.copyfile(source_protocol, tmp_protocol)
        os.replace(tmp_skill, target_skill)
        os.replace(tmp_protocol, target_protocol)
    except OSError as exc:
        # Clean up any partial state before reporting.
        for tmp in (target_dir / f".{SKILL_FILENAME}.tmp",
                    target_dir / f".{PROTOCOL_FILENAME}.tmp"):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise SkillError(f"could not install skill to {target_dir}: {exc}") from exc
    return target_dir


__all__ = [
    "PROTOCOL_FILENAME",
    "SKILL_DIR_NAME",
    "SKILL_FILENAME",
    "SUPPORTED_AGENTS",
    "SkillError",
    "agent_skill_dir",
    "install_skill",
    "resolve_protocol_directory",
    "resolve_protocol_path",
    "resolve_skill_directory",
    "resolve_skill_path",
]
