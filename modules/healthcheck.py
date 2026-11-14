"""Exit 0 when the bot event loop has written a fresh heartbeat.

Docker runs this every 30 seconds on small boards. Importing it must stay
cheap: the config reader and the heartbeat path, not the bot or the web stack.
"""

from __future__ import annotations

import argparse
import configparser
import sys
import time
from pathlib import Path

from modules.database_restore import DatabaseRestoreError, configured_bot_database_path
from modules.heartbeat import heartbeat_path

DEFAULT_MAX_AGE_SECONDS = 120.0

_NOTHING_TO_CHECK = "in-memory database; nothing to check"


def main(argv: list[str] | None = None) -> int:
    """Return 0 when the heartbeat is fresh or there is nothing to check."""
    parser = argparse.ArgumentParser(description="Check the bot liveness heartbeat.")
    parser.add_argument("--config", required=True, help="Path to the bot config.ini")
    parser.add_argument(
        "--max-age",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
        help="Maximum heartbeat age in seconds (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    code, line = _status(str(args.config), float(args.max_age))
    if line:
        print(line, flush=True)
    return code


def _status(config_path: str, max_age: float) -> tuple[int, str | None]:
    # A missing config must fail. Passing it would report a container healthy
    # whenever the check is pointed at the wrong path.
    if not Path(config_path).is_file():
        return 1, f"config not found: {config_path}"
    try:
        database = configured_bot_database_path(config_path)
    except (DatabaseRestoreError, configparser.Error, OSError):
        return 1, "config unreadable"
    if database is None:
        return 0, _NOTHING_TO_CHECK
    path = heartbeat_path(database)
    if path is None:
        return 0, _NOTHING_TO_CHECK
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return 1, "heartbeat file missing"
    except OSError:
        return 1, "heartbeat file missing"
    age = time.time() - mtime
    # A heartbeat from the future means the clock moved back. A live bot
    # rewrites it within one interval; one that stays ahead is stale.
    if age < -max_age:
        return 1, "heartbeat is ahead of the clock"
    if age > max_age:
        return 1, f"heartbeat stale by {_overdue_seconds(age, max_age)} seconds"
    return 0, None


def _overdue_seconds(age: float, max_age: float) -> int:
    overdue = age - max_age
    whole = int(overdue)
    if overdue > whole:
        whole += 1
    return max(1, whole)


if __name__ == "__main__":
    sys.exit(main())
