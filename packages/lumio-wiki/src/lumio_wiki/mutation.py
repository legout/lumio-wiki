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
Plan 05 extends this module with ordinary-failure rollback; this module
stays free of Knowledge Base page semantics.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

__all__ = [
    "MutationLockError",
    "atomic_write_bytes",
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
