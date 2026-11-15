"""Unit tests for modules.maintenance helpers and MaintenanceRunner."""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from modules.maintenance import (
    MaintenanceRunner,
    _count_log_errors_last_24h,
    _iso_week_key_from_ran_at,
    _row_n,
)
from modules.scheduler import MessageScheduler

# ---------------------------------------------------------------------------
# _iso_week_key_from_ran_at
# ---------------------------------------------------------------------------


class TestIsoWeekKeyFromRanAt:
    def test_empty_returns_empty(self):
        assert _iso_week_key_from_ran_at("") == ""
        assert _iso_week_key_from_ran_at("   ") == ""

    def test_invalid_returns_empty(self):
        assert _iso_week_key_from_ran_at("not-a-date") == ""

    def test_naive_iso_matches_isocalendar(self):
        # 2026-03-17 is a Monday
        wk = _iso_week_key_from_ran_at("2026-03-17T02:00:00")
        y, week, _ = datetime.date(2026, 3, 17).isocalendar()
        assert wk == f"{y}-W{week}"

    def test_z_suffix_parsed(self):
        wk = _iso_week_key_from_ran_at("2026-03-17T02:00:00Z")
        y, week, _ = datetime.date(2026, 3, 17).isocalendar()
        assert wk == f"{y}-W{week}"

    def test_same_calendar_week_same_key(self):
        a = _iso_week_key_from_ran_at("2026-03-17T08:00:00")
        b = _iso_week_key_from_ran_at("2026-03-18T15:30:00")
        assert a == b


# ---------------------------------------------------------------------------
# _row_n
# ---------------------------------------------------------------------------


class TestRowN:
    def test_sqlite_row(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        cur = conn.cursor()
        cur.execute("SELECT n AS n FROM t")
        assert _row_n(cur) == 42
        conn.close()

    def test_dict_row(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")

        class _Cur:
            def fetchone(self):
                return {"n": 7}

        cur = _Cur()
        assert _row_n(cur) == 7
        conn.close()


# ---------------------------------------------------------------------------
# _count_log_errors_last_24h
# ---------------------------------------------------------------------------


class TestCountLogErrorsLast24h:
    def _write(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_counts_recent_text_errors(self, tmp_path: Path):
        now = datetime.datetime.now()
        old = now - datetime.timedelta(hours=25)
        recent = now - datetime.timedelta(hours=1)
        log = tmp_path / "bot.log"
        self._write(
            log,
            [
                f'{old.strftime("%Y-%m-%d %H:%M:%S")} - MeshCoreBot - ERROR - stale',
                f'{recent.strftime("%Y-%m-%d %H:%M:%S")} - MeshCoreBot - ERROR - fresh',
                f'{recent.strftime("%Y-%m-%d %H:%M:%S")} - MeshCoreBot - CRITICAL - bad',
            ],
        )
        err, crit = _count_log_errors_last_24h(log)
        assert err == 1
        assert crit == 1

    def test_skips_old_text_lines(self, tmp_path: Path):
        now = datetime.datetime.now()
        old = now - datetime.timedelta(days=2)
        log = tmp_path / "bot.log"
        self._write(
            log,
            [f'{old.strftime("%Y-%m-%d %H:%M:%S")} - MeshCoreBot - ERROR - ancient'],
        )
        err, crit = _count_log_errors_last_24h(log)
        assert err == 0
        assert crit == 0

    def test_json_recent_error(self, tmp_path: Path):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        old = (now - datetime.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log = tmp_path / "json.log"
        self._write(
            log,
            [
                json.dumps({"timestamp": old, "level": "ERROR", "message": "x"}),
                json.dumps({"timestamp": recent, "level": "ERROR", "message": "y"}),
                json.dumps({"timestamp": recent, "level": "CRITICAL", "message": "z"}),
            ],
        )
        err, crit = _count_log_errors_last_24h(log)
        assert err == 1
        assert crit == 1

    def test_missing_file_returns_na(self, tmp_path: Path):
        err, crit = _count_log_errors_last_24h(tmp_path / "nope.log")
        assert err == "n/a"
        assert crit == "n/a"


# ---------------------------------------------------------------------------
# MaintenanceRunner.maybe_run_db_backup — weekly dedup after restart
# ---------------------------------------------------------------------------


class TestMaybeRunDbBackupWeeklyDedup:
    """DB metadata seeds week_key so weekly backup does not repeat same ISO week."""

    def _make_runner(self, now: datetime.datetime, db_ran_at: str):
        bot = MagicMock()
        bot.logger = Mock()

        def get_maint(key: str) -> str:
            return {
                "db_backup_enabled": "true",
                "db_backup_schedule": "weekly",
                "db_backup_time": f"{now.hour:02d}:{now.minute:02d}",
                "db_backup_retention_count": "7",
                "db_backup_dir": "/tmp",
            }.get(key, "")

        bot.db_manager.get_metadata = Mock(
            side_effect=lambda k: (
                db_ran_at if k == "maint.status.db_backup_ran_at" else None
            )
        )

        runner = MaintenanceRunner(bot, get_current_time=lambda: now)
        runner.get_maint = Mock(side_effect=get_maint)
        return runner

    def test_weekly_skips_when_db_ran_same_iso_week(self):
        # Monday 10:01, window 10:00–10:02; DB says backup already ran this Monday morning
        now = datetime.datetime(2026, 3, 16, 10, 1, 0)  # Monday
        assert now.weekday() == 0
        db_ran = "2026-03-16T09:30:00"
        runner = self._make_runner(now, db_ran)
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_not_called()
        assert runner._last_db_backup_stats.get("ran_at", "").startswith("2026-03-16")
        wk = f"{now.year}-W{now.isocalendar()[1]}"
        assert runner._last_db_backup_stats.get("week_key") == wk

    def test_weekly_runs_when_db_ran_previous_week(self):
        now = datetime.datetime(2026, 3, 16, 10, 1, 0)  # Monday
        db_ran = "2026-03-09T09:00:00"  # prior Monday, different ISO week
        runner = self._make_runner(now, db_ran)
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# MessageScheduler — startup retention timing
# ---------------------------------------------------------------------------


class TestSchedulerRetentionTimer:
    def test_retention_is_due_shortly_after_startup_but_email_is_not(self):
        bot = Mock()
        bot.logger = Mock()
        bot.config = ConfigParser()
        bot.config.add_section("Bot")
        sched = MessageScheduler(bot)
        until_retention_due = (
            sched._data_retention_interval_seconds
            - (time.time() - sched.last_data_retention_run)
        )
        assert 57.0 < until_retention_due <= sched._data_retention_startup_delay_seconds
        assert time.time() - sched.last_nightly_email_time < 3.0


# ---------------------------------------------------------------------------
# Nice-to-have: run_db_backup with temp SQLite (integration-style)
# ---------------------------------------------------------------------------


class TestRunDbBackupIntegration:
    def test_creates_backup_file(self, tmp_path: Path):
        db_file = tmp_path / "live.db"
        src = sqlite3.connect(str(db_file))
        src.execute("CREATE TABLE x (i INTEGER)")
        src.execute("INSERT INTO x VALUES (1)")
        src.commit()
        src.close()

        bot = MagicMock()
        bot.logger = Mock()
        bot.db_manager.db_path = db_file

        def get_maint(key: str) -> str:
            return {
                "db_backup_dir": str(tmp_path / "bk"),
                "db_backup_retention_count": "3",
            }.get(key, "")

        bot.db_manager.get_metadata = Mock(return_value=None)
        bot.db_manager.set_metadata = Mock()

        runner = MaintenanceRunner(bot, get_current_time=lambda: datetime.datetime.now())
        runner.get_maint = Mock(side_effect=get_maint)

        runner.run_db_backup()

        backups = list((tmp_path / "bk").glob("live_*.db"))
        assert len(backups) == 1
        dst = sqlite3.connect(str(backups[0]))
        assert dst.execute("SELECT i FROM x").fetchone()[0] == 1
        dst.close()

    def _runner(self, tmp_path: Path, db_file: Path) -> MaintenanceRunner:
        bot = MagicMock()
        bot.logger = Mock()
        bot.db_manager.db_path = db_file
        bot.db_manager.get_metadata = Mock(return_value=None)
        bot.db_manager.set_metadata = Mock()
        runner = MaintenanceRunner(bot, get_current_time=lambda: datetime.datetime.now())
        runner.get_maint = Mock(side_effect=lambda key: {
            "db_backup_dir": str(tmp_path / "bk"),
            "db_backup_retention_count": "3",
        }.get(key, ""))
        return runner

    def test_backup_records_its_location_beside_the_database(self, tmp_path: Path):
        db_file = tmp_path / "live.db"
        sqlite3.connect(str(db_file)).close()

        self._runner(tmp_path, db_file).run_db_backup()

        pointer = Path(f"{db_file}.backup-dir")
        assert pointer.read_text(encoding="utf-8").strip() == str((tmp_path / "bk").resolve())

    def test_failed_backup_leaves_no_partial_file(self, tmp_path: Path, monkeypatch):
        db_file = tmp_path / "live.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE x (i INTEGER)")
        conn.commit()
        conn.close()
        real_connect = sqlite3.connect

        class FailingBackup:
            def __init__(self, inner):
                self._inner = inner

            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

            def __getattr__(self, name):
                return getattr(self._inner, name)

        calls = []

        def connect(path, *args, **kwargs):
            calls.append(path)
            inner = real_connect(path, *args, **kwargs)
            return FailingBackup(inner) if len(calls) == 1 else inner

        monkeypatch.setattr(sqlite3, "connect", connect)
        runner = self._runner(tmp_path, db_file)
        runner.run_db_backup()
        monkeypatch.setattr(sqlite3, "connect", real_connect)

        assert list((tmp_path / "bk").glob("live_*.db")) == []
        assert "error" in runner._last_db_backup_stats

    def test_backup_of_wal_database_uses_rollback_journal(self, tmp_path: Path):
        db_file = tmp_path / "live.db"
        src = sqlite3.connect(str(db_file))
        src.execute("PRAGMA journal_mode=WAL")
        src.execute("CREATE TABLE x (i INTEGER)")
        src.commit()

        bot = MagicMock()
        bot.logger = Mock()
        bot.db_manager.db_path = db_file
        bot.db_manager.get_metadata = Mock(return_value=None)
        bot.db_manager.set_metadata = Mock()
        runner = MaintenanceRunner(bot, get_current_time=lambda: datetime.datetime.now())
        runner.get_maint = Mock(side_effect=lambda key: {
            "db_backup_dir": str(tmp_path / "bk"),
            "db_backup_retention_count": "3",
        }.get(key, ""))

        try:
            runner.run_db_backup()
        finally:
            src.close()

        backups = list((tmp_path / "bk").glob("live_*.db"))
        assert len(backups) == 1
        dst = sqlite3.connect(f"{backups[0].as_uri()}?mode=ro", uri=True)
        try:
            assert dst.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            dst.close()
        assert sorted(p.name for p in (tmp_path / "bk").iterdir()) == [backups[0].name]

    def test_bad_backup_is_deleted_and_older_backups_are_not_pruned(
        self, tmp_path: Path
    ):
        db_file = tmp_path / "live.db"
        src = sqlite3.connect(str(db_file))
        src.execute("CREATE TABLE x (i INTEGER)")
        src.execute("INSERT INTO x VALUES (1)")
        src.commit()
        src.close()

        backup_dir = tmp_path / "bk"
        backup_dir.mkdir()
        older = backup_dir / "live_20260101T000000.db"
        older.write_bytes(b"older known-good backup")

        bot = MagicMock()
        bot.logger = Mock()
        bot.db_manager.db_path = db_file
        bot.db_manager.get_metadata = Mock(return_value=None)
        bot.db_manager.set_metadata = Mock()

        def get_maint(key: str) -> str:
            return {
                "db_backup_dir": str(backup_dir),
                "db_backup_retention_count": "1",
            }.get(key, "")

        runner = MaintenanceRunner(bot, get_current_time=lambda: datetime.datetime.now())
        runner.get_maint = Mock(side_effect=get_maint)

        with patch(
            "modules.maintenance._quick_check_backup",
            return_value="database disk image is malformed",
        ):
            runner.run_db_backup()

        backups = list(backup_dir.glob("live_*.db"))
        assert backups == [older]
        assert older.read_bytes() == b"older known-good backup"
        outcomes = [
            call.args[1]
            for call in bot.db_manager.set_metadata.call_args_list
            if call.args[0] == "maint.status.db_backup_outcome"
        ]
        assert outcomes
        assert "integrity check" in outcomes[-1]
        assert "error" in runner._last_db_backup_stats


class TestSmtpHelpers:
    """read_smtp_settings / send_smtp_message are shared by the digest and the radio alerts."""

    @staticmethod
    def _getter(values):
        return lambda key: values.get(key, '')

    def test_port_defaults_follow_security(self):
        from modules.maintenance import read_smtp_settings

        assert read_smtp_settings(self._getter({'smtp_security': 'ssl'})).port == 465
        assert read_smtp_settings(self._getter({})).port == 587
        assert read_smtp_settings(self._getter({})).security == 'starttls'
        assert read_smtp_settings(self._getter({'smtp_port': 'abc', 'smtp_security': 'ssl'})).port == 587
        assert read_smtp_settings(self._getter({'smtp_port': '2525'})).port == 2525

    def test_from_header_and_allow_local(self):
        from modules.maintenance import read_smtp_settings

        s = read_smtp_settings(self._getter({'from_email': 'bot@example.com', 'allow_local_smtp': 'TRUE'}))
        assert s.from_header == 'MeshCore Bot <bot@example.com>'
        assert s.allow_local is True

    def test_ssl_transport(self):
        from modules.maintenance import read_smtp_settings, send_smtp_message

        s = read_smtp_settings(self._getter({
            'smtp_host': 'mail', 'smtp_security': 'ssl', 'smtp_user': 'u', 'smtp_password': 'p',
        }))
        with patch('smtplib.SMTP_SSL') as ssl_cls, patch('smtplib.SMTP') as plain_cls:
            send_smtp_message(s, 'MSG')
        plain_cls.assert_not_called()
        assert ssl_cls.call_args.args == ('mail', 465)
        assert ssl_cls.call_args.kwargs['timeout'] == 30
        server = ssl_cls.return_value.__enter__.return_value
        server.login.assert_called_once_with('u', 'p')
        server.send_message.assert_called_once_with('MSG')

    def test_starttls_transport_order(self):
        from modules.maintenance import read_smtp_settings, send_smtp_message

        s = read_smtp_settings(self._getter({'smtp_host': 'mail', 'smtp_user': 'u', 'smtp_password': 'p'}))
        with patch('smtplib.SMTP') as plain_cls:
            send_smtp_message(s, 'MSG')
        server = plain_cls.return_value.__enter__.return_value
        assert [c[0] for c in server.method_calls] == ['ehlo', 'starttls', 'ehlo', 'login', 'send_message']

    def test_plain_transport_without_credentials(self):
        from modules.maintenance import read_smtp_settings, send_smtp_message

        s = read_smtp_settings(self._getter({'smtp_host': 'mail', 'smtp_security': 'none'}))
        with patch('smtplib.SMTP') as plain_cls:
            send_smtp_message(s, 'MSG')
        server = plain_cls.return_value.__enter__.return_value
        assert [c[0] for c in server.method_calls] == ['send_message']
