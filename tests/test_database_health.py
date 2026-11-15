"""Tests for SQLite corruption detection, quarantine, and startup recovery."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

import modules.database_health as database_health
import modules.db_manager as db_manager_module
from modules.database_health import (
    check_database,
    is_corruption_error,
    quarantine_database,
    recover_corrupt_databases_from_config,
)
from modules.database_restore import DatabaseRestoreError
from modules.db_manager import DBManager
from modules.db_migrations import MigrationRunner


def _logger() -> logging.Logger:
    return logging.getLogger(__name__)


def _create_meshcore_database(
    path: Path,
    *,
    marker: str | None = None,
    backup_dir: Path | None = None,
) -> None:
    with sqlite3.connect(path) as conn:
        MigrationRunner(conn, _logger()).run()
        if marker is not None:
            conn.execute(
                "INSERT OR REPLACE INTO bot_metadata (key, value) VALUES ('test.marker', ?)",
                (marker,),
            )
        if backup_dir is not None:
            conn.execute(
                "INSERT OR REPLACE INTO bot_metadata (key, value) "
                "VALUES ('maint.db_backup_dir', ?)",
                (str(backup_dir),),
            )
        conn.commit()


def _marker(path: Path) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM bot_metadata WHERE key = 'test.marker'"
        ).fetchone()
    return None if row is None else str(row[0])


def _corrupt_middle_page(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS payload (id INTEGER PRIMARY KEY, body BLOB)"
        )
        conn.executemany(
            "INSERT INTO payload (body) VALUES (?)",
            [(os.urandom(700),) for _ in range(300)],
        )
        conn.commit()
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])

    assert page_count > 4
    target_page = page_count // 2
    with path.open("r+b") as handle:
        handle.seek(target_page * page_size)
        handle.write(b"\xde\xad\xbe\xef" * (page_size // 4))


def _write_config(
    tmp_path: Path,
    *,
    db_name: str = "bot.db",
    db_auto_recover: bool | None = None,
) -> Path:
    config_path = tmp_path / "config.ini"
    lines = ["[Bot]", f"db_path = {db_name}", "local_dir_path = local"]
    if db_auto_recover is not None:
        lines.append(f"db_auto_recover = {'true' if db_auto_recover else 'false'}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


class TestCheckDatabase:
    def test_healthy_database_returns_none(self, tmp_path: Path) -> None:
        database = tmp_path / "healthy.db"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sample (id INTEGER)")
            conn.execute("INSERT INTO sample VALUES (1)")
            conn.commit()

        assert check_database(database) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert check_database(tmp_path / "missing.db") is None

    def test_garbage_bytes_are_detected(self, tmp_path: Path) -> None:
        database = tmp_path / "garbage.db"
        database.write_bytes(b"this is not a SQLite database at all")

        detail = check_database(database)

        assert detail is not None
        assert "not a database" in detail.lower() or "malformed" in detail.lower()

    def test_overwritten_data_page_is_detected(self, tmp_path: Path) -> None:
        database = tmp_path / "damaged.db"
        _create_meshcore_database(database)
        _corrupt_middle_page(database)

        detail = check_database(database)

        assert detail is not None
        assert detail.strip().lower() != "ok"

    def test_locked_database_is_not_reported_as_corrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = tmp_path / "locked.db"
        _create_meshcore_database(database)

        locker = sqlite3.connect(database)
        locker.execute("BEGIN EXCLUSIVE")
        real_connect = sqlite3.connect

        def fast_connect(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(database_health.sqlite3, "connect", fast_connect)
        try:
            with pytest.raises(sqlite3.OperationalError):
                check_database(database)
        finally:
            locker.rollback()
            locker.close()

        assert database.exists()
        assert not list(tmp_path.glob("locked.db.corrupt-*"))

    def test_locked_database_is_not_quarantined_by_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        database = tmp_path / "locked.db"
        _create_meshcore_database(database)
        config_path = _write_config(tmp_path, db_name="locked.db")

        locker = sqlite3.connect(database)
        locker.execute("BEGIN EXCLUSIVE")
        real_connect = sqlite3.connect

        def fast_connect(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(database_health.sqlite3, "connect", fast_connect)
        try:
            # A lock must not stop startup: warn and let normal startup proceed.
            assert recover_corrupt_databases_from_config(config_path) == []
        finally:
            locker.rollback()
            locker.close()

        assert "could not check database integrity" in capsys.readouterr().err
        assert database.exists()
        assert not list(tmp_path.glob("locked.db.corrupt-*"))


class TestIsCorruptionError:
    @pytest.mark.parametrize(
        "message",
        [
            "database disk image is malformed",
            "file is not a database",
        ],
    )
    def test_corruption_messages_are_true(self, message: str) -> None:
        assert is_corruption_error(sqlite3.DatabaseError(message))

    @pytest.mark.parametrize(
        "error",
        [
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("unable to open database file"),
            sqlite3.OperationalError("disk I/O error"),
            sqlite3.DatabaseError("disk I/O error"),
            PermissionError("permission denied"),
            OSError("read-only file system"),
        ],
    )
    def test_transient_failures_are_false(self, error: BaseException) -> None:
        assert not is_corruption_error(error)

    def test_error_code_takes_precedence_when_available(self) -> None:
        exc = sqlite3.DatabaseError("opaque failure")
        exc.sqlite_errorcode = 11
        assert is_corruption_error(exc)

        exc.sqlite_errorcode = 5
        assert not is_corruption_error(exc)


class TestQuarantine:
    def test_moves_database_and_sidecars_without_deleting(
        self, tmp_path: Path
    ) -> None:
        database = tmp_path / "bot.db"
        database.write_bytes(b"main database")
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        wal.write_bytes(b"committed wal")
        shm.write_bytes(b"shared memory")

        quarantined = quarantine_database(database)

        assert quarantined.parent == tmp_path
        assert quarantined.name.startswith("bot.db.corrupt-")
        assert quarantined.read_bytes() == b"main database"
        assert not database.exists()
        assert not wal.exists()
        assert not shm.exists()
        assert Path(f"{quarantined}-wal").read_bytes() == b"committed wal"
        assert Path(f"{quarantined}-shm").read_bytes() == b"shared memory"
        assert len(list(tmp_path.iterdir())) == 3


class TestRecovery:
    def test_restores_newest_valid_backup_and_skips_corrupt_newer(
        self, tmp_path: Path
    ) -> None:
        database = tmp_path / "bot.db"
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        _create_meshcore_database(database, marker="original", backup_dir=backup_dir)
        _corrupt_middle_page(database)

        valid_backup = backup_dir / "bot_20260101T000000.db"
        _create_meshcore_database(valid_backup, marker="good-backup")
        corrupt_backup = backup_dir / "bot_20260202T000000.db"
        corrupt_backup.write_bytes(b"not a database")

        older = 1_700_000_000
        os.utime(valid_backup, (older, older))
        os.utime(corrupt_backup, (older + 100, older + 100))

        results = recover_corrupt_databases_from_config(
            _write_config(tmp_path, db_name="bot.db")
        )

        assert len(results) == 1
        result = results[0]
        assert result.database_path == database.resolve()
        assert result.quarantined_path.exists()
        assert result.restored_from == valid_backup.resolve()
        assert _marker(database) == "good-backup"
        assert not database.with_name(".bot.db.restore-pending").exists()

    def test_without_backup_the_path_is_left_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = tmp_path / "bot.db"
        database.write_bytes(b"not a database at all")
        monkeypatch.setattr(
            database_health, "DEFAULT_BACKUP_DIR", tmp_path / "no-such-backups"
        )

        results = recover_corrupt_databases_from_config(
            _write_config(tmp_path, db_name="bot.db")
        )

        assert len(results) == 1
        assert results[0].restored_from is None
        assert results[0].quarantined_path.exists()
        assert not database.exists()

    def test_marker_is_removed_after_a_healthy_check(self, tmp_path: Path) -> None:
        database = tmp_path / "bot.db"
        _create_meshcore_database(database)
        marker = Path(f"{database}.corrupt-detected")
        marker.write_text("stale marker", encoding="utf-8")

        results = recover_corrupt_databases_from_config(
            _write_config(tmp_path, db_name="bot.db")
        )

        assert results == []
        assert not marker.exists()

    def test_marker_is_removed_after_corruption_is_confirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = tmp_path / "bot.db"
        database.write_bytes(b"not a database at all")
        marker = Path(f"{database}.corrupt-detected")
        marker.write_text("runtime failure", encoding="utf-8")
        monkeypatch.setattr(
            database_health, "DEFAULT_BACKUP_DIR", tmp_path / "no-such-backups"
        )

        results = recover_corrupt_databases_from_config(
            _write_config(tmp_path, db_name="bot.db")
        )

        assert len(results) == 1
        assert not marker.exists()

    def test_disabled_auto_recover_raises_without_moving(
        self, tmp_path: Path
    ) -> None:
        database = tmp_path / "bot.db"
        database.write_bytes(b"not a database at all")

        with pytest.raises(DatabaseRestoreError, match="db_auto_recover"):
            recover_corrupt_databases_from_config(
                _write_config(tmp_path, db_name="bot.db", db_auto_recover=False)
            )

        assert database.exists()
        assert not list(tmp_path.glob("bot.db.corrupt-*"))


class _CorruptConnection:
    def __init__(self) -> None:
        self.row_factory = None

    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith("SELECT"):
            raise sqlite3.DatabaseError("database disk image is malformed")
        return Mock()

    def close(self) -> None:
        pass


class TestDBManagerCorruption:
    def test_connection_marks_corruption_and_handles_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logger = Mock()
        bot = Mock()
        bot.logger = logger
        bot._shutdown_event = threading.Event()
        bot.main_event_loop = None
        database = tmp_path / "runtime.db"
        manager = DBManager(bot, str(database))

        monkeypatch.setattr(manager, "_apply_sqlite_pragmas", lambda conn, **kwargs: None)
        monkeypatch.setattr(
            db_manager_module.sqlite3, "connect", lambda *args, **kwargs: _CorruptConnection()
        )

        for _ in range(2):
            with pytest.raises(sqlite3.DatabaseError, match="malformed"):
                with manager.connection() as conn:
                    conn.execute("SELECT 1")

        marker = Path(f"{database}.corrupt-detected")
        assert marker.exists()
        marker_text = marker.read_text(encoding="utf-8")
        assert "database disk image is malformed" in marker_text
        assert logger.critical.call_count == 1
        assert bot.fatal_exit_code == 75
        assert bot._shutdown_event.is_set()
