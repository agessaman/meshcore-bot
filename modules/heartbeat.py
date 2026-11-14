"""Liveness file updated from the bot's event loop.

The health check only looks at this file's age. A wedged loop stops
touching it. An offline radio does not: reconnect stays inside the
process, and a container restart would not bring the radio back.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

HEARTBEAT_SUFFIX = ".heartbeat"
DEFAULT_INTERVAL_SECONDS = 30.0

# One warning per process. A full disk must not flood the log every interval.
_write_failure_logged = False


def heartbeat_path(database_path: str | Path | None) -> Path | None:
    """Path beside the database, or None when there is nowhere to write.

    ``:memory:`` has no directory. The instance lock uses the same parent,
    which the bot already keeps writable.
    """
    if database_path is None:
        return None
    text = database_path if isinstance(database_path, str) else str(database_path)
    if text == ":memory:":
        return None
    # Resolved so the bot and the health check name the same file when the
    # configured path is a symlink.
    database = Path(text).resolve()
    return database.with_name(database.name + HEARTBEAT_SUFFIX)


def touch_heartbeat(path: Path, logger: logging.Logger | None = None) -> None:
    """Update the file's mtime, creating it if needed. Never raises."""
    global _write_failure_logged
    try:
        path.touch()
    except OSError as exc:
        if _write_failure_logged:
            return
        _write_failure_logged = True
        log = logger if logger is not None else logging.getLogger(__name__)
        try:
            log.warning("Could not write heartbeat file %s: %s", path, exc)
        except Exception:
            # A broken log handler must not escape into the event loop.
            return


async def heartbeat_loop(
    database_path: str | Path,
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    logger: logging.Logger | None = None,
) -> None:
    """Touch until canceled. The first touch happens before the first sleep.

    Callers wait on the radio for a long time at startup. Waiting to touch
    would look like a dead loop during that wait.
    """
    path = heartbeat_path(database_path)
    if path is None:
        return
    while True:
        touch_heartbeat(path, logger)
        await asyncio.sleep(interval)


def start_heartbeat(
    database_path: str | Path,
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    logger: logging.Logger | None = None,
) -> asyncio.Task[None] | None:
    """Schedule the loop, or return None when the database is in-memory."""
    if heartbeat_path(database_path) is None:
        return None
    return asyncio.create_task(
        heartbeat_loop(database_path, interval=interval, logger=logger),
        name="heartbeat",
    )


async def cancel_heartbeat(task: asyncio.Task[None] | None) -> None:
    """Stop the loop. The file stays so the next start can replace it."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        # Write failures are already logged. Shutdown still finishes.
        return
