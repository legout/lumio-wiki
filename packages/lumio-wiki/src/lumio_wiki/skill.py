"""Locate, install, inspect, and update the packaged Lumio Wiki Agent Skill.

The wheel is the canonical source for the complete skill bundle. Installation is
an explicit, atomic copy into either the cross-client ``.agents/skills``
convention or a supported client's compatibility directory. A manifest makes a
copy traceable to its distribution version and bundle hash.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

SKILL_MODULE = "lumio_wiki.data.skill"
PROTOCOL_MODULE = SKILL_MODULE
SKILL_FILENAME = "SKILL.md"
PROTOCOL_FILENAME = "PROTOCOL.md"
MANIFEST_FILENAME = ".lumio-skill-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
DISTRIBUTION_NAME = "lumio-wiki"
SKILL_DIR_NAME: Final = "lumio-wiki"
_UPDATE_JOURNAL_SUFFIX: Final = ".update.json"

SUPPORTED_AGENTS: Final[dict[str, str]] = {
    "pi": ".pi/agent/skills",
    "hermes": ".hermes/skills",
    "codex": ".codex/skills",
    "claude-code": ".claude/skills",
}
SUPPORTED_SCOPES: Final[tuple[str, ...]] = ("user", "project")
SkillState = Literal["missing", "current", "stale", "corrupt"]


class SkillError(Exception):
    """A skill locate, install, status, or update operation failed."""


@dataclass(frozen=True)
class SkillStatus:
    """Inspection result for one resolved skill destination."""

    state: SkillState
    target: Path
    target_kind: str
    packaged_version: str
    packaged_hash: str
    installed_version: str | None = None
    installed_hash: str | None = None
    detail: str = ""


def _resource_path(package: str, resource: str) -> Path:
    """Resolve an absolute path to a packaged resource file."""
    from importlib.resources import files

    return Path(str(files(package).joinpath(resource)))


def resolve_skill_path() -> Path:
    """Return the absolute path of the packaged ``SKILL.md``."""
    return _resource_path(SKILL_MODULE, SKILL_FILENAME)


def resolve_protocol_path() -> Path:
    """Return the absolute path of the packaged ``PROTOCOL.md``."""
    return _resource_path(PROTOCOL_MODULE, PROTOCOL_FILENAME)


def resolve_skill_directory() -> Path:
    """Return the absolute directory holding the packaged skill bundle."""
    from importlib.resources import files

    return Path(str(files(SKILL_MODULE)))


def package_version() -> str:
    """Return the version of the canonical packaged contract."""
    from lumio_wiki import __version__

    return __version__


def _bundle_hash(skill_path: Path, protocol_path: Path) -> str:
    digest = hashlib.sha256()
    for name, path in (
        (SKILL_FILENAME, skill_path),
        (PROTOCOL_FILENAME, protocol_path),
    ):
        content = path.read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def packaged_contract_hash() -> str:
    """Return a deterministic hash of the canonical wheel-owned bundle."""
    return _bundle_hash(resolve_skill_path(), resolve_protocol_path())


def agent_skill_dir(agent: str, *, home: Path | None = None) -> Path:
    """Return a supported client's conventional user-level destination."""
    if agent not in SUPPORTED_AGENTS:
        raise SkillError(f"unsupported agent {agent!r}; supported: {sorted(SUPPORTED_AGENTS)}")
    base = home if home is not None else Path.home()
    return base / SUPPORTED_AGENTS[agent] / SKILL_DIR_NAME


def scope_skill_dir(
    scope: str,
    *,
    home: Path | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Return the shared Agent Skills destination for ``user`` or ``project``."""
    if scope not in SUPPORTED_SCOPES:
        raise SkillError(f"unsupported skill scope {scope!r}; supported: {SUPPORTED_SCOPES}")
    if scope == "user":
        base = home if home is not None else Path.home()
    else:
        base = project_dir if project_dir is not None else Path.cwd()
    return base / ".agents" / "skills" / SKILL_DIR_NAME


def resolve_skill_target(
    agent: str | None = None,
    *,
    scope: str | None = None,
    dest: Path | None = None,
    home: Path | None = None,
    project_dir: Path | None = None,
) -> tuple[Path, str]:
    """Resolve one explicit compatibility, shared-scope, or custom target."""
    if agent is not None and scope is not None:
        raise SkillError("choose either an agent target or a shared scope, not both")
    if dest is not None:
        if scope is not None:
            raise SkillError("--dest cannot be combined with a shared scope")
        if agent is None:
            raise SkillError("a custom destination requires an agent target")
        agent_skill_dir(agent, home=home)  # validate the compatibility target
        return Path(dest), f"agent:{agent}:custom"
    if agent is not None:
        return agent_skill_dir(agent, home=home), f"agent:{agent}"
    selected_scope = scope or "user"
    return (
        scope_skill_dir(selected_scope, home=home, project_dir=project_dir),
        f"scope:{selected_scope}",
    )


def _manifest_payload(*, target_kind: str) -> dict[str, object]:
    contract_hash = packaged_contract_hash()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "distribution": DISTRIBUTION_NAME,
        "distribution_version": package_version(),
        "content_hash": contract_hash,
        "source_contract_hash": contract_hash,
        "target": target_kind,
    }


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise SkillError(f"could not remove temporary skill path {path}: {exc}") from exc


def _cleanup_path_best_effort(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        _remove_path(path)
    except SkillError:
        return


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise SkillError(f"could not persist staged skill file {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SkillError(f"could not persist skill directory {path}: {exc}") from exc


def _bundle_files_are_regular(target: Path) -> bool:
    return all(
        path.is_file() and not path.is_symlink()
        for path in (target / SKILL_FILENAME, target / PROTOCOL_FILENAME)
    )


def _journal_path(target: Path) -> Path:
    return target.parent / f".{target.name}{_UPDATE_JOURNAL_SUFFIX}"


@contextmanager
def _target_update_lock(target: Path) -> Iterator[None]:
    """Serialize recovery and activation for one destination across processes."""
    lock_path = target.parent / f".{target.name}.update.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SkillError(f"could not open skill update lock {lock_path}: {exc}") from exc
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(descriptor)
        raise SkillError(f"could not lock skill destination {target}: {exc}") from exc
    try:
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _write_update_journal(target: Path, staged: Path, backup: Path) -> Path:
    journal = _journal_path(target)
    temporary = journal.with_suffix(f"{journal.suffix}.tmp")
    payload = {
        "schema_version": 1,
        "target": target.name,
        "staged": staged.name,
        "backup": backup.name,
    }
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
        _fsync_directory(target.parent)
    except (OSError, SkillError) as exc:
        _cleanup_path_best_effort(temporary)
        raise SkillError(f"could not create recoverable skill update journal: {exc}") from exc
    return journal


def _read_update_journal(target: Path) -> tuple[Path, Path] | None:
    journal = _journal_path(target)
    if not journal.is_file():
        return None
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"skill update journal is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("target") != target.name:
        raise SkillError("skill update journal has an invalid target")
    staged_name = payload.get("staged")
    backup_name = payload.get("backup")
    if not isinstance(staged_name, str) or not isinstance(backup_name, str):
        raise SkillError("skill update journal paths are invalid")
    if Path(staged_name).name != staged_name or Path(backup_name).name != backup_name:
        raise SkillError("skill update journal paths escape the destination directory")
    return target.parent / staged_name, target.parent / backup_name


def _recover_interrupted_update(target: Path) -> None:
    recorded = _read_update_journal(target)
    if recorded is None:
        return
    staged, backup = recorded
    if os.path.lexists(target):
        _cleanup_path_best_effort(staged)
        _cleanup_path_best_effort(backup)
        _journal_path(target).unlink(missing_ok=True)
        _fsync_directory(target.parent)
        return
    if not os.path.lexists(backup):
        raise SkillError(
            "interrupted skill update cannot be recovered because its backup is missing"
        )
    try:
        os.replace(backup, target)
        _fsync_directory(target.parent)
    except (OSError, SkillError) as exc:
        raise SkillError(f"could not restore interrupted skill update: {exc}") from exc
    _cleanup_path_best_effort(staged)
    _journal_path(target).unlink(missing_ok=True)
    _fsync_directory(target.parent)


def _atomic_directory_exchange(left: Path, right: Path) -> bool:
    """Atomically exchange two paths on Linux, or report unsupported."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result == 0:
        return True
    error_code = ctypes.get_errno()
    if error_code in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EXDEV}:
        return False
    raise OSError(error_code, os.strerror(error_code))


def _write_staged_bundle(parent: Path, target_kind: str) -> Path:
    source_skill = resolve_skill_path()
    source_protocol = resolve_protocol_path()
    if not source_skill.is_file():
        raise SkillError(f"packaged skill not found: {source_skill}")
    if not source_protocol.is_file():
        raise SkillError(f"packaged protocol not found: {source_protocol}")

    staged = Path(tempfile.mkdtemp(prefix=f".{SKILL_DIR_NAME}.tmp-", dir=parent))
    try:
        shutil.copyfile(source_skill, staged / SKILL_FILENAME)
        shutil.copyfile(source_protocol, staged / PROTOCOL_FILENAME)
        (staged / MANIFEST_FILENAME).write_text(
            json.dumps(_manifest_payload(target_kind=target_kind), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for filename in (SKILL_FILENAME, PROTOCOL_FILENAME, MANIFEST_FILENAME):
            _fsync_file(staged / filename)
        _fsync_directory(staged)
    except (OSError, TypeError, ValueError) as exc:
        _cleanup_path_best_effort(staged)
        raise SkillError(f"could not stage packaged skill bundle: {exc}") from exc
    return staged


def _atomic_replace_bundle(target: Path, staged: Path) -> None:
    if not os.path.lexists(target):
        os.replace(staged, target)
        _fsync_directory(target.parent)
        return
    if _atomic_directory_exchange(target, staged):
        _fsync_directory(target.parent)
        _cleanup_path_best_effort(staged)
        return

    # Portable fallback: a durable journal makes the two-rename activation
    # recoverable. Linux uses the atomic exchange above, so its live target is
    # never absent even if the process is killed during refresh.
    backup = Path(tempfile.mkdtemp(prefix=f".{SKILL_DIR_NAME}.backup-", dir=target.parent))
    backup.rmdir()
    journal = _write_update_journal(target, staged, backup)
    try:
        os.replace(target, backup)
        _fsync_directory(target.parent)
        os.replace(staged, target)
        _fsync_directory(target.parent)
    except (OSError, SkillError) as exc:
        _recover_interrupted_update(target)
        raise SkillError(f"could not activate staged skill bundle: {exc}") from exc
    journal.unlink(missing_ok=True)
    _fsync_directory(target.parent)
    _cleanup_path_best_effort(backup)


def install_skill(
    agent: str | None = None,
    *,
    scope: str | None = None,
    dest: Path | None = None,
    overwrite: bool = False,
    home: Path | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Atomically install the canonical bundle into one explicit destination."""
    target, target_kind = resolve_skill_target(
        agent,
        scope=scope,
        dest=dest,
        home=home,
        project_dir=project_dir,
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _target_update_lock(target):
            _recover_interrupted_update(target)
            if os.path.lexists(target) and not overwrite:
                raise SkillError(
                    f"destination already exists: {target} (run 'skill status' and "
                    "use 'skill update' or --overwrite explicitly)"
                )
            staged = _write_staged_bundle(target.parent, target_kind)
            try:
                _atomic_replace_bundle(target, staged)
            except (OSError, SkillError):
                _cleanup_path_best_effort(staged)
                raise
    except OSError as exc:
        raise SkillError(f"could not install skill to {target}: {exc}") from exc
    return target


def skill_status(
    agent: str | None = None,
    *,
    scope: str | None = None,
    dest: Path | None = None,
    home: Path | None = None,
    project_dir: Path | None = None,
) -> SkillStatus:
    """Report whether a destination is missing, current, stale, or corrupt."""
    target, target_kind = resolve_skill_target(
        agent,
        scope=scope,
        dest=dest,
        home=home,
        project_dir=project_dir,
    )
    packaged_version = package_version()
    packaged_hash = packaged_contract_hash()
    if _journal_path(target).exists():
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            detail="an interrupted update is pending recovery; run skill update",
        )
    if not os.path.lexists(target):
        return SkillStatus(
            "missing",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            detail="destination does not exist",
        )

    manifest_path = target / MANIFEST_FILENAME
    if os.path.lexists(manifest_path) and (
        manifest_path.is_symlink() or not manifest_path.is_file()
    ):
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            detail="manifest is not a regular file",
        )
    if not os.path.lexists(manifest_path):
        if not _bundle_files_are_regular(target):
            return SkillStatus(
                "corrupt",
                target,
                target_kind,
                packaged_version,
                packaged_hash,
                detail="legacy bundle is incomplete or contains symlinked files",
            )
        try:
            legacy_hash = _bundle_hash(
                target / SKILL_FILENAME,
                target / PROTOCOL_FILENAME,
            )
        except OSError as exc:
            return SkillStatus(
                "corrupt",
                target,
                target_kind,
                packaged_version,
                packaged_hash,
                detail=f"legacy bundle is incomplete: {exc}",
            )
        return SkillStatus(
            "stale",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_hash=legacy_hash,
            detail="legacy manifest-less bundle requires an explicit update",
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            detail=f"manifest missing or unreadable: {exc}",
        )
    if not isinstance(payload, dict):
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            detail="manifest root is not an object",
        )

    installed_version = payload.get("distribution_version")
    installed_hash = payload.get("content_hash")
    required = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "distribution": DISTRIBUTION_NAME,
        "target": target_kind,
    }
    if any(payload.get(key) != value for key, value in required.items()):
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_version if isinstance(installed_version, str) else None,
            installed_hash if isinstance(installed_hash, str) else None,
            "manifest identity, schema, or target does not match",
        )
    if not isinstance(installed_version, str) or not isinstance(installed_hash, str):
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            detail="manifest version or hash is invalid",
        )
    if payload.get("source_contract_hash") != installed_hash:
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_version,
            installed_hash,
            "manifest source contract hash does not match its content hash",
        )
    if not _bundle_files_are_regular(target):
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_version,
            installed_hash,
            "installed bundle is incomplete or contains symlinked files",
        )
    try:
        actual_hash = _bundle_hash(
            target / SKILL_FILENAME,
            target / PROTOCOL_FILENAME,
        )
    except OSError as exc:
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_version,
            installed_hash,
            f"installed bundle is incomplete: {exc}",
        )
    if actual_hash != installed_hash:
        return SkillStatus(
            "corrupt",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_version,
            installed_hash,
            "installed content does not match its manifest",
        )
    if installed_version != packaged_version or installed_hash != packaged_hash:
        return SkillStatus(
            "stale",
            target,
            target_kind,
            packaged_version,
            packaged_hash,
            installed_version,
            installed_hash,
            "installed contract differs from the current wheel",
        )
    return SkillStatus(
        "current",
        target,
        target_kind,
        packaged_version,
        packaged_hash,
        installed_version,
        installed_hash,
        "installed contract matches the current wheel",
    )


def update_skill(
    agent: str | None = None,
    *,
    scope: str | None = None,
    dest: Path | None = None,
    home: Path | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Explicitly refresh a stale or corrupt copy from the current wheel."""
    status = skill_status(
        agent,
        scope=scope,
        dest=dest,
        home=home,
        project_dir=project_dir,
    )
    if status.state == "missing":
        raise SkillError(f"skill is not installed at {status.target}; run 'skill install' first")
    if status.state == "current":
        return status.target
    return install_skill(
        agent,
        scope=scope,
        dest=dest,
        overwrite=True,
        home=home,
        project_dir=project_dir,
    )


__all__ = [
    "DISTRIBUTION_NAME",
    "MANIFEST_FILENAME",
    "PROTOCOL_FILENAME",
    "SKILL_DIR_NAME",
    "SKILL_FILENAME",
    "SUPPORTED_AGENTS",
    "SUPPORTED_SCOPES",
    "SkillError",
    "SkillStatus",
    "agent_skill_dir",
    "install_skill",
    "package_version",
    "packaged_contract_hash",
    "resolve_protocol_path",
    "resolve_skill_directory",
    "resolve_skill_path",
    "resolve_skill_target",
    "scope_skill_dir",
    "skill_status",
    "update_skill",
]
