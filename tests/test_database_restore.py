"""Regression tests for the staged, startup-only database restore workflow."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

import modules.database_restore as database_restore_module
from modules.database_restore import (
    DatabaseRestoreError,
    apply_pending_database_restore,
    apply_pending_restores_from_config,
    pending_restore_path,
    stage_database_restore,
    validate_restore_database,
)
from modules.db_migrations import MigrationRunner


def _create_meshcore_database(path: Path, marker: str) -> None:
    with sqlite3.connect(path) as conn:
        MigrationRunner(conn, logging.getLogger(__name__)).run()
        conn.execute(
            "INSERT OR REPLACE INTO bot_metadata (key, value) VALUES ('restore.marker', ?)",
            (marker,),
        )
        conn.commit()


def _marker(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM bot_metadata WHERE key = 'restore.marker'"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_stage_does_not_replace_live_database(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    backup = tmp_path / "backup.db"
    _create_meshcore_database(active, "active")
    _create_meshcore_database(backup, "backup")

    pending = stage_database_restore(backup, active)

    assert pending == pending_restore_path(active)
    assert pending.exists()
    assert _marker(active) == "active"
    assert _marker(pending) == "backup"
    assert pending.stat().st_mode & 0o007 == 0


def test_apply_pending_restore_creates_recovery_and_clears_sidecars(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.db"
    backup = tmp_path / "backup.db"
    _create_meshcore_database(active, "active")
    _create_meshcore_database(backup, "backup")
    pending = stage_database_restore(backup, active)
    Path(f"{active}-wal").write_bytes(b"stale")
    Path(f"{active}-shm").write_bytes(b"stale")

    result = apply_pending_database_restore(active)

    assert result is not None
    assert result.database_path == active.resolve()
    assert result.recovery_backup_path is not None
    assert result.recovery_backup_path.exists()
    assert _marker(result.recovery_backup_path) == "active"
    assert _marker(active) == "backup"
    assert not pending.exists()
    assert not Path(f"{active}-wal").exists()
    assert not Path(f"{active}-shm").exists()


@pytest.mark.parametrize("contents", [b"not sqlite", b"SQLite format 3\x00broken"])
def test_invalid_pending_restore_never_changes_active_database(
    tmp_path: Path,
    contents: bytes,
) -> None:
    active = tmp_path / "active.db"
    _create_meshcore_database(active, "active")
    pending_restore_path(active).write_bytes(contents)

    with pytest.raises(DatabaseRestoreError):
        apply_pending_database_restore(active)

    assert _marker(active) == "active"


def test_foreign_sqlite_database_is_rejected(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()

    with pytest.raises(DatabaseRestoreError, match="missing table"):
        validate_restore_database(foreign)


def test_recognizable_pre_migration_database_is_accepted_and_upgradable(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.db"
    active = tmp_path / "active.db"
    with sqlite3.connect(legacy) as conn:
        conn.executescript(
            """
            CREATE TABLE bot_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE generic_cache (
                id INTEGER PRIMARY KEY,
                cache_key TEXT UNIQUE NOT NULL,
                cache_value TEXT NOT NULL,
                cache_type TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL
            );
            CREATE TABLE channels (
                channel_idx INTEGER PRIMARY KEY,
                channel_name TEXT NOT NULL
            );
            INSERT INTO bot_metadata (key, value)
            VALUES ('restore.marker', 'legacy');
            """
        )
        conn.commit()

    validate_restore_database(legacy)
    stage_database_restore(legacy, active)
    apply_pending_database_restore(active)
    with sqlite3.connect(active) as conn:
        MigrationRunner(conn, logging.getLogger(__name__)).run()
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert _marker(active) == "legacy"


def test_restore_size_limit_is_enforced_before_staging(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    backup = tmp_path / "backup.db"
    _create_meshcore_database(active, "active")
    _create_meshcore_database(backup, "backup")

    with pytest.raises(DatabaseRestoreError, match="safety limit"):
        stage_database_restore(backup, active, max_bytes=1024)

    assert not pending_restore_path(active).exists()
    assert _marker(active) == "active"


def test_post_replace_failure_automatically_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    backup = tmp_path / "backup.db"
    _create_meshcore_database(active, "active")
    _create_meshcore_database(backup, "backup")
    stage_database_restore(backup, active)
    real_validate = database_restore_module.validate_restore_database
    validation_count = 0

    def fail_first_post_replace_validation(path: str | Path) -> None:
        nonlocal validation_count
        validation_count += 1
        # pending validation, recovery validation, then replaced active DB
        if validation_count == 3:
            raise DatabaseRestoreError("simulated post-replace validation failure")
        real_validate(path)

    monkeypatch.setattr(
        database_restore_module,
        "validate_restore_database",
        fail_first_post_replace_validation,
    )

    with pytest.raises(DatabaseRestoreError, match="candidate was re-queued"):
        apply_pending_database_restore(active)

    assert _marker(active) == "active"
    assert _marker(pending_restore_path(active)) == "backup"


def test_configured_bot_and_viewer_restores_apply_before_startup(tmp_path: Path) -> None:
    active_bot = tmp_path / "bot.db"
    active_viewer = tmp_path / "viewer.db"
    backup_bot = tmp_path / "bot-backup.db"
    backup_viewer = tmp_path / "viewer-backup.db"
    for path, marker in (
        (active_bot, "old-bot"),
        (active_viewer, "old-viewer"),
        (backup_bot, "new-bot"),
        (backup_viewer, "new-viewer"),
    ):
        _create_meshcore_database(path, marker)
    stage_database_restore(backup_bot, active_bot)
    stage_database_restore(backup_viewer, active_viewer)
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Bot]\n"
        "db_path = bot.db\n"
        "local_dir_path = local\n"
        "[Web_Viewer]\n"
        "db_path = viewer.db\n",
        encoding="utf-8",
    )

    results = apply_pending_restores_from_config(config_path)

    assert {result.database_path for result in results} == {
        active_bot.resolve(),
        active_viewer.resolve(),
    }
    assert _marker(active_bot) == "new-bot"
    assert _marker(active_viewer) == "new-viewer"


def test_configured_restore_failure_rolls_back_and_requeues_prior_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_bot = tmp_path / "bot.db"
    active_viewer = tmp_path / "viewer.db"
    backup_bot = tmp_path / "bot-backup.db"
    backup_viewer = tmp_path / "viewer-backup.db"
    for path, marker in (
        (active_bot, "old-bot"),
        (active_viewer, "old-viewer"),
        (backup_bot, "new-bot"),
        (backup_viewer, "new-viewer"),
    ):
        _create_meshcore_database(path, marker)
    stage_database_restore(backup_bot, active_bot)
    stage_database_restore(backup_viewer, active_viewer)
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Bot]\n"
        "db_path = bot.db\n"
        "[Web_Viewer]\n"
        "db_path = viewer.db\n",
        encoding="utf-8",
    )
    real_apply = database_restore_module.apply_pending_database_restore

    def fail_viewer(path: str | Path):
        if Path(path).resolve() == active_viewer.resolve():
            raise DatabaseRestoreError("simulated viewer restore failure")
        return real_apply(path)

    monkeypatch.setattr(
        database_restore_module,
        "apply_pending_database_restore",
        fail_viewer,
    )

    with pytest.raises(DatabaseRestoreError, match="rolled back and re-queued"):
        apply_pending_restores_from_config(config_path)

    assert _marker(active_bot) == "old-bot"
    assert _marker(active_viewer) == "old-viewer"
    assert _marker(pending_restore_path(active_bot)) == "new-bot"
    assert _marker(pending_restore_path(active_viewer)) == "new-viewer"


def _use_wal(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()


def test_stage_and_apply_leave_no_stray_sidecar_files(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    backup = tmp_path / "backup.db"
    _create_meshcore_database(active, "active")
    _create_meshcore_database(backup, "backup")
    _use_wal(active)
    _use_wal(backup)

    stage_database_restore(backup, active)
    result = apply_pending_database_restore(active)

    assert result is not None
    # Only the active database's own name may carry sidecars; the staging,
    # pre-restore and pending copies must not strand any under their names.
    leftovers = sorted(
        p.name for p in tmp_path.iterdir()
        if p.name.startswith(".active.db") or ".pending" in p.name
        or (p.name.startswith("active.") and p.name.endswith(("-wal", "-shm")))
    )
    assert leftovers == []
