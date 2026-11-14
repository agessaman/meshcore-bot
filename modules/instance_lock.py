"""Single-instance guard for the bot process.

Two bots started against the same database share one radio. Companion servers
that allow a single client drop the older connection when a new one arrives, so
the two instances evict each other in an endless reconnect loop. The guard is an
advisory lock on a file beside the bot database, held for the life of the
process and released by the OS when the process exits, however it exits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO

LOCK_SUFFIX = ".lock"

# Windows locks a byte range and blocks other handles from reading it. Locking
# far past the PID text keeps the PID readable by the instance that is refused.
_WINDOWS_LOCK_OFFSET = 1 << 30


class InstanceAlreadyRunning(RuntimeError):
    """Another bot process holds the lock for this database."""

    def __init__(self, lock_path: Path, pid: int | None):
        self.lock_path = lock_path
        self.pid = pid
        holder = f"PID {pid}" if pid is not None else "another process"
        super().__init__(f"{holder} already holds {lock_path}")


class InstanceLock:
    """An acquired lock. Keep a reference for as long as the bot runs."""

    def __init__(self, path: Path, handle: IO[str]):
        self.path = path
        self._handle: IO[str] | None = handle

    def release(self) -> None:
        """Release the lock. The file stays: unlinking it races a starting instance."""
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


def instance_lock_path(database_path: str | Path) -> Path:
    """Lock file path for a bot database."""
    database = Path(database_path)
    return database.with_name(database.name + LOCK_SUFFIX)


def _lock_exclusive(handle: IO[str]) -> None:
    """Take a non-blocking exclusive lock, raising OSError when it is held."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _read_pid(handle: IO[str]) -> int | None:
    try:
        handle.seek(0)
        return int(handle.read(32).strip())
    except (OSError, ValueError):
        return None


def acquire_instance_lock(database_path: str | Path) -> InstanceLock:
    """Lock the bot database for this process.

    Raises:
        InstanceAlreadyRunning: another process holds the lock.
        OSError: the lock file could not be created or opened.
    """
    path = instance_lock_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" creates the file without truncating the holder's PID. Python opens
    # files non-inheritable, so the web viewer subprocess does not keep the
    # lock alive after the bot exits.
    handle = open(path, "a+", encoding="ascii")  # noqa: SIM115 - held for process life
    try:
        _lock_exclusive(handle)
    except OSError as exc:
        pid = _read_pid(handle)
        handle.close()
        raise InstanceAlreadyRunning(path, pid) from exc

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        # The lock is what matters; the PID only improves the error message.
        pass
    return InstanceLock(path, handle)
