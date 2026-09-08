"""Internal mutation critical sections for durable private state (Plan 02 / P3).

This module is the ONE filesystem boundary every mutating owner —
:class:`~lumio_wiki.source_registry.SourceRegistry`,
:class:`~lumio_wiki.ingest.IngestStore`, and
:class:`~lumio_wiki.proposal_pipeline.ProposalPipeline` — uses to serialize
cooperating writers per resource across processes (Plan 02 findings B02/B03).

Contract (one lock/reload boundary, not a transaction system):

* **Normalized resource identity.** :func:`resource_identity` maps a resource
  root to one canonical string (symlinks resolved, case lower-cased), so two
  instances opened over equivalent paths — a standalone
  ``SourceRegistry(root)`` and an ``IngestStore(root).source_registry``, or
  two differently cased spellings of one case-insensitive location — share
  exactly one lock.
* **Real interprocess locks.** :func:`mutation_lock` takes a POSIX ``flock``
  (or ``msvcrt.locking`` on Windows) on a lock file, not merely a threading
  lock. A per-process :class:`threading.RLock` layer makes the OS lock
  reentrant for nested calls in one process/thread. If the platform cannot
  provide interprocess locking, mutation fails with :class:`MutationLockError`
  — it never silently degrades to unsynchronized writes.
* **Lock files stay out of portable artifacts.** Lock files live under
  :func:`lock_directory` in the system temp directory keyed by the resource
  identity, never inside a Knowledge Base root, ingest store, or export.
  Lock files are never unlinked: removing one while another process holds it
  open would break mutual exclusion (a new opener would create a second
  inode). They are contentless, tiny, and bound one per mutated resource.
* **Consistent global order.** :func:`mutation_lock` always sorts the
  requested resource identities, so every multi-resource acquisition — KB
  root + ingest store + registry for a publish, store + registry for a
  discard — uses the same total order and cannot deadlock against another
  cooperating writer. Registry operations never acquire a second resource,
  so the registry is the leaf of the order. Sorting per call alone cannot
  make NESTED calls safe: a nested call that expands an already-held set
  downward in the canonical order is the one remaining ABBA shape (two
  writers each holding one resource and nesting the wider set wait on each
  other forever). :func:`mutation_lock` therefore refuses that expansion
  with :class:`MutationLockError` before acquiring anything; a nested call
  may only re-enter held resources or add resources that sort after
  everything the thread already holds.
* **Lock ownership covers the whole critical section.** Callers hold the
  lock through read/check/mutate/commit/rollback — not merely around file
  replacement — and recompute each mutation from durable state re-read
  UNDER the lock. A reload immediately before one ``os.replace`` is not
  sufficient: another process may have committed between the read and the
  write.
* **Unique temp paths.** :func:`atomic_write_bytes` writes each durable
  file through a uniquely named temp file in the destination directory and
  swaps it in with :func:`os.replace`, so concurrent writers can never
  clobber each other's temp file and readers never observe torn bytes. The
  temp file is removed if the write fails.

Locking here serializes cooperating processes only; it is advisory against
arbitrary external editors, and it is not crash atomicity (Plan 02 limits).
Plan 02 / P5 extends this module with ordinary-failure rollback support:
:class:`MutationBackup` snapshots the exact pre-mutation state of affected
paths into a per-operation backup directory OUTSIDE every resource root, and
:class:`MutationRollbackError` reports a restoration that could not fully
succeed. This module stays free of Knowledge Base page semantics: the backup
primitives speak only of paths, bytes, and entry types; which paths a publish
must snapshot is the caller's decision.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat as stat_module
import tempfile
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = [
    "MutationBackup",
    "MutationLockError",
    "MutationRollbackError",
    "atomic_write_bytes",
    "backup_root",
    "lock_directory",
    "mutation_lock",
    "resource_identity",
]


class MutationLockError(RuntimeError):
    """An interprocess mutation lock could not be taken or is unavailable.

    Raised instead of silently degrading: mutating durable registry, store,
    or Knowledge Base state without a real OS lock would lose updates across
    cooperating processes (Plan 02 B02/B03). The message names the resource
    identity and the underlying reason so the operator can act on it.
    """


def resource_identity(resource: str | os.PathLike[str]) -> str:
    """Return the canonical identity string for one mutable resource root.

    Equivalent paths — relative vs. absolute, symlinked parents, differing
    case — normalize to one identity, so separately opened instances over
    the same registry/store/Knowledge Base contend on exactly one lock.

    Case is lower-cased explicitly and conservatively on EVERY platform:
    :func:`os.path.normcase` is a no-op on POSIX, so on a case-insensitive
    macOS filesystem it would leave differently cased spellings of one
    location with separate lock identities that could race. Over-serializing
    two genuinely distinct case-sensitive paths is the accepted cost — the
    safe failure mode is extra mutual exclusion, never a missed lock.
    """
    path = Path(resource)
    try:
        resolved = path.resolve(strict=False)
    except OSError:  # pragma: no cover - defensive: unresolvable parents
        resolved = path.absolute()
    return os.path.normcase(str(resolved)).lower()


def lock_directory() -> Path:
    """Return the directory holding interprocess mutation lock files.

    Deliberately OUTSIDE every resource root: lock files must never travel
    with a portable Knowledge Base, an export, or a fingerprint.
    """
    return Path(tempfile.gettempdir()) / "lumio-mutation-locks"


def _lock_file_path(identity: str) -> Path:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return lock_directory() / f"{digest}.lock"


class _ResourceLock:
    """Per-process state for one resource identity.

    The reentrant ``RLock`` serializes threads in this process and lets the
    same thread nest acquisitions; the OS-level lock is taken once, by the
    depth-zero entrant, and held until the outermost release.
    """

    __slots__ = ("depth", "descriptor", "rlock")

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.descriptor: int | None = None


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, _ResourceLock] = {}

# Per-thread stack of held resource identity sets, innermost hold last. Each
# :func:`mutation_lock` frame pushes the set it holds and pops it on exit, so
# a nested call can compare its requested identities against everything its
# thread already holds and refuse the one expansion shape per-call sorting
# cannot make deadlock-safe (see :func:`mutation_lock`).
_HELD_STACKS = threading.local()


def _held_identities() -> frozenset[str]:
    """Return the resource identities the current thread already holds."""
    stack = getattr(_HELD_STACKS, "stack", None)
    if not stack:
        return frozenset()
    return stack[-1]


def _lock_descriptor(descriptor: int, identity: str, lock_path: Path) -> None:
    """Take the OS-level exclusive lock on an open lock-file descriptor."""
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
    except ImportError as exc:  # pragma: no cover - exotic platforms
        raise MutationLockError(
            f"interprocess mutation locking is unavailable on this platform "
            f"(no fcntl/msvcrt); refusing to mutate {identity} without it: {exc}"
        ) from exc
    except OSError as exc:
        raise MutationLockError(
            f"could not acquire the interprocess mutation lock for {identity} ({lock_path}): {exc}"
        ) from exc


def _open_lock_file(identity: str) -> int:
    lock_path = _lock_file_path(identity)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MutationLockError(
            f"could not create the mutation lock directory {lock_path.parent}: {exc}"
        ) from exc
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise MutationLockError(
            f"could not open the mutation lock file for {identity} ({lock_path}): {exc}"
        ) from exc
    try:
        _lock_descriptor(descriptor, identity, lock_path)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _close_lock_file(descriptor: int) -> None:
    """Release the OS lock and close the descriptor (best effort)."""
    # Unlocking a lock whose owner already died is harmless; closing always.
    with suppress(OSError):
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


@contextmanager
def mutation_lock(*resources: str | os.PathLike[str]) -> Iterator[None]:
    """Hold one reentrant interprocess critical section over resource roots.

    Yields once inside the critical section. Every caller passes its resource
    roots (Knowledge Base root, ingest store root, registry root); the roots
    are normalized and acquired in sorted-identity order, which is the single
    global lock order — nested and multi-resource acquisitions therefore
    cannot deadlock against another cooperating process. Acquiring the same
    resource again (from nested calls in the same thread) is reentrant.

    Sorting each call is not sufficient for NESTED calls: a nested call that
    expands an already-held set with a resource sorting BEFORE something the
    thread holds is the one shape per-call sorting cannot order (two writers
    each holding one resource and nesting the wider set would wait on each
    other forever). That expansion is refused up front with
    :class:`MutationLockError` — never entered. A nested call may re-request
    held resources (reentrant) or add resources that sort after everything
    its thread already holds; request every resource a critical section needs
    in the single outermost call, which acquires them in sorted order.

    Holds the lock through the caller's read/check/mutate/commit/rollback
    sequence; owners recompute mutations from state read under the lock.
    """
    if not resources:
        raise MutationLockError("mutation_lock requires at least one resource identity")
    identities = sorted({resource_identity(resource) for resource in resources})
    held = _held_identities()
    if held:
        # The one explicit deadlock-safety invariant: a thread's held identity
        # set may only grow UPWARD in the canonical order. Check every new
        # identity BEFORE acquiring anything, so a refusal never leaves a
        # partially acquired hold behind.
        ceiling = max(held)
        for identity in identities:
            if identity in held:
                continue  # reentrant re-request of an already-held resource
            if identity < ceiling:
                raise MutationLockError(
                    f"unsafe nested mutation_lock expansion: resource {identity!r} "
                    f"precedes the already-held {ceiling!r} in the canonical resource "
                    f"order, so acquiring it here could deadlock against a cooperating "
                    f"writer that holds {identity!r} and nests the wider set. Acquire "
                    f"every resource the critical section needs in the single outermost "
                    f"mutation_lock call (which acquires them in sorted order)."
                )
    acquired: list[_ResourceLock] = []
    stack: list[frozenset[str]] = getattr(_HELD_STACKS, "stack", None) or []
    if not stack:
        stack = []
        _HELD_STACKS.stack = stack
    pushed = False
    try:
        for identity in identities:
            with _LOCKS_GUARD:
                entry = _LOCKS.get(identity)
                if entry is None:
                    entry = _ResourceLock()
                    _LOCKS[identity] = entry
            entry.rlock.acquire()
            try:
                if entry.depth == 0:
                    entry.descriptor = _open_lock_file(identity)
                entry.depth += 1
            except BaseException:
                entry.rlock.release()
                raise
            acquired.append(entry)
        stack.append(held | frozenset(identities))
        pushed = True
        yield
    finally:
        if pushed:
            stack.pop()
        for entry in reversed(acquired):
            entry.depth -= 1
            try:
                if entry.depth == 0:
                    descriptor, entry.descriptor = entry.descriptor, None
                    if descriptor is not None:
                        _close_lock_file(descriptor)
            finally:
                entry.rlock.release()


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Persist ``data`` at ``path`` atomically via a unique temp file.

    The temp file is created with :func:`tempfile.mkstemp` in the destination
    directory (same filesystem, so the final :func:`os.replace` is atomic and
    the unique name can never collide with a concurrent writer's temp file).
    Readers observe either the previous or the new complete file — never torn
    bytes. A failed write removes the temp file and leaves the destination
    untouched. The parent directory must already exist.
    """
    destination = Path(path)
    descriptor, temp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, destination)
    except BaseException:
        with suppress(OSError):
            temp.unlink()
        raise


class MutationRollbackError(RuntimeError):
    """A failed mutation's restoration could not fully succeed (Plan 02 / P5).

    Raised instead of ever claiming a rollback succeeded: the original
    mutation failure is chained as ``__cause__``, ``backup_dir`` names the
    RETAINED pre-mutation backup directory for manual recovery, and
    ``failed_paths`` lists each path (with its reason) the restore could not
    bring back. Recovery is manual from the retained backup; success is never
    reported for a partially restored mutation.
    """

    def __init__(self, message: str, *, backup_dir: Path | None, failed_paths: list[str]) -> None:
        super().__init__(message)
        self.backup_dir = backup_dir
        self.failed_paths = tuple(failed_paths)


def backup_root() -> Path:
    """Return the directory holding per-operation mutation backup trees.

    Deliberately OUTSIDE every resource root — like :func:`lock_directory`,
    backups must never travel with (or pollute) a portable Knowledge Base, an
    ingest store, or an export.
    """
    return Path(tempfile.gettempdir()) / "lumio-mutation-backups"


@dataclass(frozen=True)
class _CapturedPath:
    """One path's pre-mutation state captured by :class:`MutationBackup`.

    ``kind`` is ``"absent"``, ``"file"`` (``backup_name`` carries the copied
    bytes inside the backup directory and ``mode`` the captured permission
    bits), ``"symlink"`` (``symlink_target`` the read ``os.readlink`` value),
    or ``"unmodelled"`` (a directory or special entry whose bytes cannot be
    captured safely — it is described, never opened, and restoration succeeds
    only if the mutation left that type of entry untouched).
    """

    relative_path: str
    kind: str
    backup_name: str | None = None
    symlink_target: str | None = None
    mode: int = 0
    file_format: str = ""


class MutationBackup:
    """A per-operation pre-mutation backup for ordinary-failure rollback (P5).

    The caller captures the exact paths its mutation will touch BEFORE the
    first live byte, runs the mutation, and on any failure calls
    :meth:`restore_all` to put every captured path back — replacements and
    deletions restored byte-for-byte (permission bits preserved), newly
    created files removed, moves undone, and (for basename sweeps) disposable
    derived artifacts the mutation created removed. On full success
    :meth:`cleanup` removes the backup; on failed restoration the backup is
    RETAINED and :class:`MutationRollbackError` reports it.

    Backups live under :func:`backup_root`, never inside a mutated resource.
    Capture is read-only and safe: entry types are classified on ``lstat``
    metadata, regular files are the only entries ever opened, and symlinks
    are recorded as links (never followed). Directories and special entries
    are not reconstructible, so they are modelled as ``"unmodelled"``:
    restoration verifies the mutation never converted them.

    Ancestor directories that did not exist at capture time are recorded and
    pruned after a restore when empty, so a mutation that created a fresh
    directory tree does not leave it behind. Entries created by the mutation
    at untracked paths (below the ``remove_untracked_basenames`` sweep of
    :meth:`restore_all`) are removed the same way. Empty-directory pruning is
    best-effort: a directory that gained back content is kept.
    """

    def __init__(self, label: str) -> None:
        self._label = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._-") or "mutation"
        self._directory: Path | None = None
        self._captured: dict[str, _CapturedPath] = {}
        self._created_dirs: list[str] = []

    @property
    def directory(self) -> Path | None:
        """The per-operation backup directory (``None`` before the first copy)."""
        return self._directory

    def _ensure_directory(self) -> Path:
        if self._directory is None:
            directory = backup_root() / f"{self._label}-{uuid.uuid4().hex}"
            directory.mkdir(parents=True, exist_ok=True)
            self._directory = directory
        return self._directory

    def capture_path(self, root: str | os.PathLike[str], relative_path: str) -> None:
        """Snapshot one root-relative path's exact current state (read-only).

        Records existence, entry type, bytes (regular files, copied into the
        backup directory), symlink targets, and permission bits. An OSError
        here means the state could not be snapshotted safely — the caller is
        still BEFORE any live mutation and should fail without mutating.
        """
        root_path = Path(root)
        relative = PurePosixPath(relative_path).as_posix()
        if relative in self._captured:
            return
        self._record_created_ancestors(root_path, relative)
        path = root_path / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            self._captured[relative] = _CapturedPath(relative, "absent")
            return
        if stat_module.S_ISLNK(info.st_mode):
            self._captured[relative] = _CapturedPath(
                relative, "symlink", symlink_target=os.readlink(path)
            )
            return
        if stat_module.S_ISREG(info.st_mode):
            data = path.read_bytes()
            directory = self._ensure_directory()
            safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", relative).strip("._-") or "path"
            backup_name = f"{safe_prefix}-{uuid.uuid4().hex}.bin"
            (directory / backup_name).write_bytes(data)
            self._captured[relative] = _CapturedPath(
                relative,
                "file",
                backup_name=backup_name,
                mode=stat_module.S_IMODE(info.st_mode),
            )
            return
        if stat_module.S_ISDIR(info.st_mode):
            self._captured[relative] = _CapturedPath(
                relative, "unmodelled", file_format="directory"
            )
            return
        self._captured[relative] = _CapturedPath(
            relative,
            "unmodelled",
            file_format="special entry (FIFO, socket, or device)",
        )

    def capture_basename_sweep(
        self, root: str | os.PathLike[str], basenames: Iterable[str]
    ) -> None:
        """Snapshot every existing entry whose filename matches ``basenames``.

        Case-insensitive on the basename (reserved-artifact discovery folds
        case), exact on the parent path. The walk never follows symlinked
        directories and never opens anything — capture classifies entries on
        ``lstat`` metadata, so a FIFO named like a reserved artifact is
        recorded without ever being read.
        """
        wanted = {name.lower() for name in basenames}
        root_path = Path(root)
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                if filename.lower() not in wanted:
                    continue
                relative = (Path(dirpath) / filename).relative_to(root_path).as_posix()
                self.capture_path(root_path, relative)

    def restore_all(
        self,
        root: str | os.PathLike[str],
        *,
        remove_untracked_basenames: Iterable[str] | None = None,
    ) -> list[str]:
        """Restore every captured path; return one message per failed path.

        An empty list means the captured pre-mutation state was fully
        restored: replaced/deleted files back to their exact bytes, symlinks
        recreated, created files and (empty, freshly created) directories
        removed. ``remove_untracked_basenames`` sweeps disposable derived
        entries the mutation may have created at untracked paths (matched
        case-insensitively on the basename like the capture sweep); only
        entries NOT present in the capture are removed — pre-existing ones
        are restored from their snapshot instead.

        Never raises for individual path failures: each failure is reported
        in the returned list so the caller can raise ONE actionable recovery
        error that names every failed path and the retained backup.
        """
        root_path = Path(root)
        failures: list[str] = []
        for relative in sorted(self._captured):
            try:
                self._restore_one(root_path, self._captured[relative])
            except OSError as exc:
                failures.append(f"{relative} ({exc})")
        if remove_untracked_basenames:
            failures.extend(self._remove_untracked(root_path, remove_untracked_basenames))
        self._prune_created_directories(root_path)
        return failures

    def cleanup(self) -> None:
        """Remove the backup directory after the mutation fully succeeded.

        Best effort by contract: a cleanup failure KEEPS the disposable
        backup on disk and never changes the mutation's success semantics.
        """
        if self._directory is None:
            return
        # Best effort by contract: a cleanup failure KEEPS the disposable
        # backup on disk and never changes the mutation's success semantics.
        with suppress(OSError):
            shutil.rmtree(self._directory)

    def discard(self) -> None:
        """Drop the backup before any live mutation happened (capture failure)."""
        if self._directory is not None:
            with suppress(OSError):
                shutil.rmtree(self._directory)
            self._directory = None
        self._captured.clear()
        self._created_dirs.clear()

    # --- internals ---------------------------------------------------------

    def _record_created_ancestors(self, root_path: Path, relative: str) -> None:
        """Record which ancestor directories did not exist at capture time."""
        current = ""
        for part in PurePosixPath(relative).parts[:-1]:
            current = f"{current}/{part}" if current else part
            if current in self._created_dirs:
                continue
            try:
                (root_path / current).lstat()
            except OSError as exc:
                if not isinstance(exc, FileNotFoundError):
                    return  # unreadable ancestor: prune nothing we are unsure about
                self._created_dirs.append(current)

    def _restore_one(self, root_path: Path, captured: _CapturedPath) -> None:
        path = root_path / captured.relative_path
        if captured.kind == "absent":
            if os.path.lexists(path):
                self._unlink_entry(path)
            return
        if captured.kind == "file":
            if (
                self._directory is None or captured.backup_name is None
            ):  # pragma: no cover - defensive
                raise OSError("no captured backup bytes for this path")
            data = (self._directory / captured.backup_name).read_bytes()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, data)
            with suppress(OSError):
                os.chmod(path, captured.mode)
            return
        if captured.kind == "symlink":
            if os.path.lexists(path):
                self._unlink_entry(path)
            os.symlink(captured.symlink_target or "", path)
            return
        # Unmodelled entries (directories, special files) cannot be
        # reconstructed from bytes. Restoration succeeds only when the
        # mutation left the same type of entry in place.
        try:
            info = path.lstat()
        except OSError:
            info = None
        current_format = None
        if info is not None:
            if stat_module.S_ISDIR(info.st_mode):
                current_format = "directory"
            elif not stat_module.S_ISLNK(info.st_mode) and not stat_module.S_ISREG(info.st_mode):
                current_format = "special entry (FIFO, socket, or device)"
        if current_format != captured.file_format:
            raise OSError(
                f"cannot reconstruct the captured {captured.file_format or 'entry'} "
                "from bytes; the entry changed type or vanished during the "
                "mutation"
            )

    def _unlink_entry(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            raise OSError("the path is now a directory, which cannot be removed as a file")
        path.unlink()

    def _remove_untracked(self, root_path: Path, basenames: Iterable[str]) -> list[str]:
        """Remove disposable entries the mutation created at untracked paths."""
        wanted = {name.lower() for name in basenames}
        failures: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                if filename.lower() not in wanted:
                    continue
                relative = (Path(dirpath) / filename).relative_to(root_path).as_posix()
                if relative in self._captured:
                    continue  # pre-existing: restored from its snapshot above
                try:
                    self._unlink_entry(root_path / relative)
                except OSError as exc:
                    failures.append(f"{relative} ({exc})")
        return failures

    def _prune_created_directories(self, root_path: Path) -> None:
        """Best-effort removal of directories the mutation created (now empty).

        Deepest first; a directory that is not empty (it holds restored
        content, or it pre-existed) or cannot be removed is simply kept —
        leftover empty directories are cosmetic, never data loss, and are
        deliberately not reported as restoration failures.
        """
        for relative in sorted(self._created_dirs, key=lambda item: -item.count("/")):
            with suppress(OSError):
                (root_path / relative).rmdir()
