"""Detect SQLite corruption and recover a usable database at startup.

The bot and the web viewer keep the database on a filesystem an operator
controls.  A network share, a power loss, or a killed writer can leave SQLite
reporting ``database disk image is malformed``.  Nothing previously checked the
live database, so the failure surfaced only when a query happened to touch the
damaged page and every later start failed the same way.

This module adds two pieces:

* a bounded integrity check that distinguishes corruption from ordinary
  transient failures such as a lock or a permissions problem; and
* startup recovery that moves a corrupt database aside and, when one is
  available, restores the newest backup that still validates.

Detection at runtime lives in :class:`modules.db_manager.DBManager`, which
writes ``<db>.corrupt-detected`` and asks the process to restart into this
startup check.
"""

from __future__ import annotations

import configparser
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database_restore import (
    DatabaseRestoreError,
    _configured_database_paths,
    _fsync_directory,
    apply_pending_database_restore,
    stage_database_restore,
    validate_restore_database,
)

SQLITE_CORRUPT = 11
SQLITE_NOTADB = 26
DEFAULT_BACKUP_DIR = Path("/data/backups")
_ANNOUNCE_CHECK_BYTES = 256 * 1_048_576

# Python 3.11+ exposes sqlite_errorcode; older versions only have the message.
_CORRUPTION_MESSAGES = (
    "database disk image is malformed",
    "file is not a database",
)


def is_corruption_error(exc: BaseException) -> bool:
    """Return True only when *exc* means the database file itself is damaged."""

    if not isinstance(exc, sqlite3.DatabaseError):
        return False

    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (SQLITE_CORRUPT, SQLITE_NOTADB)

    message = str(exc).lower()
    return any(fragment in message for fragment in _CORRUPTION_MESSAGES)


def corruption_marker_path(database: str | Path) -> Path:
    """The runtime marker left by ``DBManager`` for the next startup check."""

    return Path(f"{database}.corrupt-detected")


def check_database(path: str | Path) -> str | None:
    """Return None when *path* is missing or intact, else a short detail string.

    The connection is opened normally rather than read-only so a leftover WAL
    is replayed before the check.  Corruption errors are returned; every other
    SQLite failure (a lock, an I/O error, a permissions problem) is re-raised so
    a healthy database is never quarantined for a transient reason.
    """

    database = Path(path)
    if not database.exists():
        return None

    try:
        conn = sqlite3.connect(str(database), timeout=10.0)
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        if is_corruption_error(exc):
            return str(exc) or type(exc).__name__
        raise

    if not row:
        return "quick_check returned no result"
    detail = str(row[0])
    if detail.strip().lower() == "ok":
        return None
    return detail


def _unique_quarantine_path(database: Path, timestamp: str) -> Path:
    target = database.with_name(f"{database.name}.corrupt-{timestamp}")
    index = 1
    while target.exists():
        target = database.with_name(f"{database.name}.corrupt-{timestamp}-{index}")
        index += 1
    return target


def quarantine_database(path: str | Path) -> Path:
    """Move *path* and its WAL sidecars aside, returning the new main path.

    Nothing is deleted: the damaged file and any sidecars stay next to the
    database for inspection.  Sidecars keep their ``-wal``/``-shm`` suffix
    after the quarantine timestamp.
    """

    database = Path(path)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    quarantined = _unique_quarantine_path(database, timestamp)

    os.replace(database, quarantined)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists():
            os.replace(sidecar, Path(f"{quarantined}{suffix}"))

    _fsync_directory(database.parent)
    return quarantined


def _backup_dir_for_database(database: Path) -> Path | None:
    """Best-effort backup directory read from a database that may be damaged."""

    configured: str | None = None
    try:
        conn = sqlite3.connect(str(database), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT value FROM bot_metadata WHERE key = 'maint.db_backup_dir'"
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            configured = str(row[0])
    except Exception:
        # The damaged file may not be readable at all; the default is enough.
        pass

    backup_dir = Path(configured) if configured else DEFAULT_BACKUP_DIR
    if backup_dir.is_dir():
        return backup_dir
    return None


def find_restorable_backup(db_path: str | Path, backup_dir: str | Path) -> Path | None:
    """Return the newest backup for *db_path* that passes restore validation."""

    database = Path(db_path)
    directory = Path(backup_dir)
    if not directory.is_dir():
        return None

    try:
        candidates = sorted(
            directory.glob(f"{database.stem}_*.db"),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )
    except OSError:
        return None

    for candidate in candidates:
        try:
            validate_restore_database(candidate)
        except Exception as exc:
            print(
                f"Skipping invalid database backup {candidate}: {exc}",
                file=sys.stderr,
            )
            continue
        return candidate
    return None


@dataclass(frozen=True)
class RecoveryResult:
    """Description of a corrupt database handled during startup."""

    database_path: Path
    quarantined_path: Path
    restored_from: Path | None
    detail: str


def _read_config(config_path: str | Path) -> configparser.ConfigParser | None:
    """Read the config and its local overlay the same way restore does."""

    path = Path(config_path).resolve()
    if not path.exists():
        return None

    config = configparser.ConfigParser()
    try:
        loaded = config.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise DatabaseRestoreError(f"Could not read database recovery configuration: {exc}") from exc
    if not loaded:
        return None

    base = path.parent
    local_dir = Path(config.get("Bot", "local_dir_path", fallback="local"))
    if not local_dir.is_absolute():
        local_dir = base / local_dir
    local_config = local_dir / "config.ini"
    if local_config.exists():
        try:
            config.read(local_config, encoding="utf-8")
        except configparser.Error as exc:
            raise DatabaseRestoreError(
                f"Could not read local database recovery configuration: {exc}"
            ) from exc
    return config


def _auto_recover_enabled(config_path: str | Path) -> bool:
    config = _read_config(config_path)
    if config is None:
        return True
    try:
        return config.getboolean("Bot", "db_auto_recover", fallback=True)
    except (configparser.Error, ValueError):
        return True


def _discard_marker(marker: Path) -> None:
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"Could not remove corruption marker {marker}: {exc}", file=sys.stderr)


def _manual_recovery_message(database: Path, detail: str) -> str:
    return (
        f"Database {database} is corrupt ({detail}) and [Bot] db_auto_recover is "
        "disabled. Stop the bot, move the database and its -wal and -shm files "
        "aside, then restart so migrations create a fresh database or restore a "
        "backup."
    )


def recover_corrupt_databases_from_config(
    config_path: str | Path,
) -> list[RecoveryResult]:
    """Check every configured database and recover the corrupt ones.

    Runs before the bot imports and constructs :class:`MeshCoreBot`, while the
    instance lock is held and no process has the database open.  A valid backup
    is restored through the existing staged-restore machinery; without one the
    path is left absent so migrations create a fresh database.
    """

    results: list[RecoveryResult] = []
    auto_recover = _auto_recover_enabled(config_path)

    for database in _configured_database_paths(config_path):
        marker = corruption_marker_path(database)
        if not database.exists():
            _discard_marker(marker)
            continue

        try:
            size = database.stat().st_size
        except OSError:
            size = 0
        if size >= _ANNOUNCE_CHECK_BYTES:
            # quick_check reads the whole file; on a Pi's SD card a large
            # database takes long enough to look like a hang.
            print(
                f"Checking database integrity: {database} ({size // 1_048_576} MB)",
                file=sys.stderr,
            )

        try:
            detail = check_database(database)
        except sqlite3.Error as exc:
            # A lock or I/O problem is not evidence of corruption. Leave the
            # file alone and let normal startup report it as it always has.
            print(
                f"Warning: could not check database integrity of {database}: {exc}",
                file=sys.stderr,
            )
            continue
        finally:
            # The marker explains why the previous process exited.  Whether or
            # not the check confirms corruption, it has served its purpose.
            _discard_marker(marker)

        if detail is None:
            continue
        if not auto_recover:
            raise DatabaseRestoreError(_manual_recovery_message(database, detail))

        # Read the backup location before quarantine moves the file away.
        backup_dir = _backup_dir_for_database(database)
        quarantined = quarantine_database(database)

        restored_from: Path | None = None
        if backup_dir is not None:
            backup = find_restorable_backup(database, backup_dir)
            if backup is not None:
                stage_database_restore(backup, database)
                apply_pending_database_restore(database)
                restored_from = backup

        results.append(
            RecoveryResult(
                database_path=database,
                quarantined_path=quarantined,
                restored_from=restored_from,
                detail=detail,
            )
        )

    return results
