"""Stage and apply SQLite restores without replacing a live database.

The web viewer runs in a separate process from the bot and both processes open
short-lived WAL connections.  Replacing the database from an HTTP request is
therefore unsafe: existing descriptors and WAL sidecars can continue referring
to the old database.  This module implements a two-phase restore instead:

* the viewer validates and copies a backup to a sibling pending file; and
* normal bot startup applies that pending file before any database manager,
  scheduler, or viewer process is created.

The startup caller is responsible for ensuring the service was restarted as a
unit (that is, no separately managed viewer process is still using the file).
"""

from __future__ import annotations

import configparser
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from modules.db_migrations import MIGRATIONS

DEFAULT_MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024

# Databases created before numbered migrations do not have ``schema_version``.
# Requiring bot_metadata plus multiple tables from the original MeshCore schema
# distinguishes those legitimate backups from an arbitrary SQLite file while
# still allowing MigrationRunner to upgrade them after startup.
LEGACY_SCHEMA_TABLES = {
    "channel_operations",
    "channels",
    "feed_activity",
    "feed_errors",
    "feed_message_queue",
    "feed_subscriptions",
    "generic_cache",
    "geocoding_cache",
}


class DatabaseRestoreError(RuntimeError):
    """Raised when a restore cannot be safely staged or applied."""


@dataclass(frozen=True)
class RestoreResult:
    """Description of a restore applied during startup."""

    database_path: Path
    recovery_backup_path: Path | None


def pending_restore_path(database_path: str | Path) -> Path:
    """Return the same-filesystem staging path for *database_path*."""

    database = Path(database_path).resolve()
    return database.with_name(f".{database.name}.restore-pending")


def validate_restore_database(database_path: str | Path) -> None:
    """Fail unless *database_path* is an intact, compatible MeshCore database."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise DatabaseRestoreError(f"Restore database does not exist: {path}")

    try:
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise DatabaseRestoreError("Restore file is not a SQLite database")

        uri = f"{path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
            integrity_row = conn.execute("PRAGMA integrity_check(1)").fetchone()
            if not integrity_row or str(integrity_row[0]).lower() != "ok":
                detail = str(integrity_row[0]) if integrity_row else "no result"
                raise DatabaseRestoreError(f"SQLite integrity check failed: {detail}")

            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "bot_metadata" not in tables:
                raise DatabaseRestoreError(
                    "Restore file is not a MeshCore Bot database; "
                    "missing table: bot_metadata"
                )

            if "schema_version" not in tables:
                legacy_matches = sorted(LEGACY_SCHEMA_TABLES & tables)
                if len(legacy_matches) < 2:
                    raise DatabaseRestoreError(
                        "Restore file is not a recognizable legacy MeshCore Bot "
                        "database; numbered migration history is absent and fewer "
                        "than two legacy schema tables were found"
                    )
            else:
                known_versions = {
                    int(version) for version, _description, _fn in MIGRATIONS
                }
                applied_versions = {
                    int(row[0])
                    for row in conn.execute(
                        "SELECT version FROM schema_version"
                    ).fetchall()
                    if row and row[0] is not None
                }
                unknown_versions = sorted(applied_versions - known_versions)
                if unknown_versions:
                    raise DatabaseRestoreError(
                        "Restore database was created by a newer or incompatible "
                        "version; unknown migration version(s): "
                        f"{unknown_versions}"
                    )
    except DatabaseRestoreError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise DatabaseRestoreError(f"Could not validate restore database: {exc}") from exc


def _safe_database_mode(database_path: Path) -> int:
    """Preserve owner/group access while never granting access to others."""

    try:
        current = stat.S_IMODE(database_path.stat().st_mode)
    except OSError:
        current = 0
    return 0o600 | (current & 0o060)


def _match_database_ownership(path: Path, database_path: Path) -> None:
    os.chmod(path, _safe_database_mode(database_path))
    try:
        active_stat = database_path.stat()
        os.chown(path, active_stat.st_uid, active_stat.st_gid)
    except (AttributeError, FileNotFoundError, PermissionError):
        # chown is unavailable on Windows and an unprivileged service can only
        # create files as itself.  In both cases the creator is the safe owner.
        pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage_database_restore(
    source_path: str | Path,
    database_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_RESTORE_BYTES,
) -> Path:
    """Validate and stage *source_path* for application on the next bot startup."""

    if max_bytes <= 0:
        raise DatabaseRestoreError("Restore size limit must be positive")

    source = Path(source_path).resolve()
    database = Path(database_path).resolve()
    pending = pending_restore_path(database)
    if source in {database, pending}:
        raise DatabaseRestoreError("Restore source must be a separate backup file")

    validate_restore_database(source)
    database.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            dir=database.parent,
            prefix=f".{database.name}.restore-",
            suffix=".tmp",
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        source_uri = f"{source.as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30.0) as source_conn:
            page_count = int(source_conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(source_conn.execute("PRAGMA page_size").fetchone()[0])
            if page_count * page_size > max_bytes:
                raise DatabaseRestoreError(
                    f"Restore database exceeds the {max_bytes}-byte safety limit"
                )
            with sqlite3.connect(str(temp_path), timeout=30.0) as destination_conn:
                # SQLite's online backup API produces a self-contained snapshot
                # and includes committed WAL content; a raw file copy does not.
                source_conn.backup(destination_conn)
                destination_conn.commit()

        if temp_path.stat().st_size > max_bytes:
            raise DatabaseRestoreError(
                f"Restore database exceeds the {max_bytes}-byte safety limit"
            )
        with temp_path.open("rb") as temp_handle:
            os.fsync(temp_handle.fileno())

        _match_database_ownership(temp_path, database)
        validate_restore_database(temp_path)
        os.replace(temp_path, pending)
        temp_path = None
        _fsync_directory(database.parent)
        return pending
    except DatabaseRestoreError:
        raise
    except OSError as exc:
        raise DatabaseRestoreError(f"Could not stage restore database: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _create_recovery_backup(database: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    recovery = database.with_name(f"{database.stem}.pre-restore-{timestamp}.db")
    descriptor, temp_name = tempfile.mkstemp(
        dir=database.parent,
        prefix=f".{database.name}.pre-restore-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(str(database), timeout=30.0) as source_conn:
            with sqlite3.connect(str(temp_path), timeout=30.0) as destination_conn:
                source_conn.backup(destination_conn)
                destination_conn.commit()
        _match_database_ownership(temp_path, database)
        validate_restore_database(temp_path)
        os.replace(temp_path, recovery)
        _fsync_directory(database.parent)
        return recovery
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _rollback_from_recovery(recovery: Path, database: Path) -> None:
    """Atomically restore *database* from the already verified recovery copy."""

    descriptor, temp_name = tempfile.mkstemp(
        dir=database.parent,
        prefix=f".{database.name}.rollback-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(recovery, temp_path)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _match_database_ownership(temp_path, database)
        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{database}{suffix}").unlink()
            except FileNotFoundError:
                pass
        os.replace(temp_path, database)
        _fsync_directory(database.parent)
        validate_restore_database(database)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def apply_pending_database_restore(database_path: str | Path) -> RestoreResult | None:
    """Apply a staged restore before any process opens *database_path*."""

    database = Path(database_path).resolve()
    pending = pending_restore_path(database)
    if not pending.exists():
        return None

    validate_restore_database(pending)
    database.parent.mkdir(parents=True, exist_ok=True)
    recovery: Path | None = None
    replaced = False
    try:
        if database.exists():
            recovery = _create_recovery_backup(database)
            active_stat = database.stat()
        else:
            active_stat = None

        _match_database_ownership(pending, database)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass

        os.replace(pending, database)
        replaced = True
        if active_stat is not None:
            os.chmod(database, 0o600 | (stat.S_IMODE(active_stat.st_mode) & 0o060))
            try:
                os.chown(database, active_stat.st_uid, active_stat.st_gid)
            except (AttributeError, PermissionError):
                pass
        else:
            os.chmod(database, 0o600)
        _fsync_directory(database.parent)
        validate_restore_database(database)
        return RestoreResult(database_path=database, recovery_backup_path=recovery)
    except (DatabaseRestoreError, OSError, sqlite3.Error) as exc:
        if replaced:
            try:
                # Preserve the staged candidate for diagnosis/retry before
                # putting the previous active database back.  Without this,
                # a failure after os.replace consumes one member of a
                # coordinated Bot/Viewer restore set.
                os.replace(database, pending)
                _fsync_directory(database.parent)
                if recovery is not None:
                    _rollback_from_recovery(recovery, database)
            except Exception as rollback_exc:
                raise DatabaseRestoreError(
                    "Pending restore failed after replacement and automatic recovery also "
                    f"failed. Recovery backup: {recovery}. Restore error: {exc}; "
                    f"rollback error: {rollback_exc}"
                ) from exc
            if recovery is not None:
                raise DatabaseRestoreError(
                    "Pending restore failed; the candidate was re-queued and the "
                    f"original database was restored from {recovery}: {exc}"
                ) from exc
            raise DatabaseRestoreError(
                "Pending restore failed; the candidate was re-queued and no active "
                f"database had existed: {exc}"
            ) from exc
        if isinstance(exc, DatabaseRestoreError):
            raise
        raise DatabaseRestoreError(f"Could not apply pending database restore: {exc}") from exc


def _configured_database_paths(
    config_path: str | Path, *, include_viewer: bool = True
) -> list[Path]:
    path = Path(config_path).resolve()
    if not path.exists():
        return []

    config = configparser.ConfigParser()
    try:
        loaded = config.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise DatabaseRestoreError(f"Could not read restore configuration: {exc}") from exc
    if not loaded:
        return []

    base = path.parent
    local_dir = Path(config.get("Bot", "local_dir_path", fallback="local"))
    if not local_dir.is_absolute():
        local_dir = base / local_dir
    local_config = local_dir / "config.ini"
    if local_config.exists():
        try:
            config.read(local_config, encoding="utf-8")
        except configparser.Error as exc:
            raise DatabaseRestoreError(f"Could not read local restore configuration: {exc}") from exc

    raw_paths = [config.get("Bot", "db_path", fallback="meshcore_bot.db")]
    viewer_path = config.get("Web_Viewer", "db_path", fallback="").strip()
    if viewer_path and include_viewer:
        raw_paths.append(viewer_path)

    resolved: list[Path] = []
    for raw in raw_paths:
        if raw == ":memory:":
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate = candidate.resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def configured_bot_database_path(config_path: str | Path) -> Path | None:
    """The bot database path from config, or None when it is unset or in-memory.

    Reads the config directly so startup can use it before the bot exists.
    """
    paths = _configured_database_paths(config_path, include_viewer=False)
    return paths[0] if paths else None


def apply_pending_restores_from_config(config_path: str | Path) -> list[RestoreResult]:
    """Apply pending bot/viewer restores before normal startup opens either DB."""

    database_paths = _configured_database_paths(config_path)
    pending_paths = [
        pending_restore_path(path)
        for path in database_paths
        if pending_restore_path(path).exists()
    ]
    # Validate the entire set before changing any active database.  This avoids
    # applying the bot restore only to discover that a split viewer restore is
    # corrupt or incompatible.
    for pending in pending_paths:
        validate_restore_database(pending)

    results: list[RestoreResult] = []
    try:
        for database_path in database_paths:
            result = apply_pending_database_restore(database_path)
            if result is not None:
                results.append(result)
    except Exception as exc:
        rollback_errors: list[str] = []
        for result in reversed(results):
            database = result.database_path
            pending = pending_restore_path(database)
            try:
                # Preserve the already-applied candidate so the complete restore
                # set can be retried after the failure is corrected.
                for suffix in ("-wal", "-shm"):
                    try:
                        Path(f"{database}{suffix}").unlink()
                    except FileNotFoundError:
                        pass
                os.replace(database, pending)
                _fsync_directory(database.parent)
                validate_restore_database(pending)
                if result.recovery_backup_path is not None:
                    _rollback_from_recovery(result.recovery_backup_path, database)
            except Exception as rollback_exc:
                rollback_errors.append(f"{database}: {rollback_exc}")

        if rollback_errors:
            raise DatabaseRestoreError(
                "A configured restore failed and rollback was incomplete. "
                f"Restore error: {exc}. Rollback error(s): "
                + "; ".join(rollback_errors)
            ) from exc
        raise DatabaseRestoreError(
            "A configured restore failed; every previously applied database was "
            f"rolled back and re-queued: {exc}"
        ) from exc
    return results
