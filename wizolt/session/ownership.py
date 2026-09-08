"""Exclusive session ownership: the `flock` lease one runtime holds per session family.

A session family -- a parent snapshot and its `<uid>.w` worker -- has exactly one writable owner at
a time. The lease is an open file descriptor locked with `fcntl.flock(fd, LOCK_EX | LOCK_NB)`; the
descriptor, not a timestamp or a PID file, is the authority. The kernel releases it when the last
descriptor closes, so a crash needs no stale-lock cleanup. Lock files live under
`<data_dir>/session-locks/` so retention never deletes the lock a live session holds, and they are
never unlinked: replacing one inode would let two processes lock different files for the same
session.

This module is an OS-resource boundary. It imports the standard library and the base error type
only -- never Session, the store, the CLI, the engine, or the TUI.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os

from wizolt.base import WizoltError

LOCK_DIR_NAME = "session-locks"


class SessionBusyError(WizoltError):
    """Another live runtime owns the session this process tried to open.

    Raised only for actual lock contention. Permission and filesystem failures raise a plain
    `WizoltError`, so a broken lock directory is never misreported as a second Wizolt instance.
    """

    def __init__(self, uid: str, path: str = "") -> None:
        self.uid = uid
        self.path = path
        super().__init__(f"Session {uid} is already in use by another Wizolt instance.")


class SessionOwnershipError(WizoltError):
    """A write was attempted without a live, matching ownership capability."""


# Leases open in this process, so an after-fork child can drop inherited descriptors. A child that
# closed them without LOCK_UN cannot unlock the parent's shared open-file description; a child that
# kept them would keep ownership alive past the parent's death.
_OPEN_LEASES: set[SessionLease] = set()


def canonical_snapshot_path(path: str) -> str:
    """The absolute, symlink-resolved snapshot path that names an ownership family."""

    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def ownership_identity(snapshot_path: str) -> str:
    """The parent JSONL path a snapshot path belongs to; a `.w` worker maps to its parent."""

    if snapshot_path.endswith(".w.jsonl"):
        return snapshot_path[: -len(".w.jsonl")] + ".jsonl"
    return snapshot_path


def _uid_for(identity: str) -> str:
    name = os.path.basename(identity)
    return name.removesuffix(".jsonl")


def _close_after_fork() -> None:
    """Close inherited lease descriptors in a forked child, without unlocking the parent's."""

    for lease in tuple(_OPEN_LEASES):
        lease._close_in_child()
    _OPEN_LEASES.clear()


if hasattr(os, "register_at_fork"):  # POSIX only; the supported platforms all have it.
    os.register_at_fork(after_in_child=_close_after_fork)


class SessionLease:
    """An exclusively held session-family lock. The descriptor is the lease.

    `acquire` returns an owned descriptor or raises; it never falls back to unlocked operation.
    `close` is idempotent, and `assert_owned` refuses a closed, foreign, replaced, or
    non-owning-process lease, so a capability retained past its runtime cannot authorize a write.
    """

    def __init__(self, fd: int, identity: str, lock_path: str, owner_pid: int) -> None:
        self._fd = fd
        self._identity = identity
        self._lock_path = lock_path
        self._owner_pid = owner_pid

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def uid(self) -> str:
        return _uid_for(self._identity)

    @property
    def closed(self) -> bool:
        return self._fd is None

    @classmethod
    def acquire(cls, data_dir: str, root_snapshot_path: str) -> SessionLease:
        """Take the family lock nonblockingly, or raise `SessionBusyError` on contention."""

        identity = canonical_snapshot_path(ownership_identity(root_snapshot_path))
        lock_dir = os.path.join(os.path.abspath(os.path.expanduser(data_dir)), LOCK_DIR_NAME)
        try:
            os.makedirs(lock_dir, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(lock_dir, 0o700)
        except OSError as error:
            raise WizoltError(f"could not create the session lock directory {lock_dir}: {error}") from error
        lock_path = os.path.join(lock_dir, hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise WizoltError(f"could not open the session lock file {lock_path}: {error}") from error
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            # Only real contention means "another instance". A permission or filesystem failure is
            # reported as itself, never as a second owner.
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise SessionBusyError(_uid_for(identity), identity) from None
            raise WizoltError(f"could not lock session {_uid_for(identity)}: {error}") from error
        lease = cls(fd, identity, lock_path, os.getpid())
        _OPEN_LEASES.add(lease)
        return lease

    def assert_owned(self, root_snapshot_path: str) -> None:
        """Verify the live resource, identity, and owning process; raise otherwise."""

        if self._fd is None:
            raise SessionOwnershipError(f"session lease for {self.uid} is closed")
        if os.getpid() != self._owner_pid:
            raise SessionOwnershipError(f"session lease for {self.uid} belongs to pid {self._owner_pid}")
        identity = canonical_snapshot_path(ownership_identity(root_snapshot_path))
        if identity != self._identity:
            raise SessionOwnershipError(f"session lease for {self.uid} does not cover {identity}")
        try:
            held = os.fstat(self._fd)
            on_disk = os.stat(self._lock_path)
        except OSError as error:
            raise SessionOwnershipError(f"session lease for {self.uid} is no longer valid: {error}") from error
        if (held.st_dev, held.st_ino) != (on_disk.st_dev, on_disk.st_ino):
            raise SessionOwnershipError(f"session lock file for {self.uid} was replaced while held")

    def close(self) -> None:
        """Release the lease. Idempotent; never unlinks the lock file."""

        fd, self._fd = self._fd, None
        _OPEN_LEASES.discard(self)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _close_in_child(self) -> None:
        """Drop an inherited descriptor without LOCK_UN, which would unlock the shared description."""

        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
