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
# MessageScheduler — retention timer not immediate
# ---------------------------------------------------------------------------


class TestSchedulerRetentionTimer:
    def test_last_data_retention_run_is_recent_at_init(self):
        bot = Mock()
        bot.logger = Mock()
        bot.config = ConfigParser()
        bot.config.add_section("Bot")
        sched = MessageScheduler(bot)
        assert time.time() - sched.last_data_retention_run < 3.0
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


# ---------------------------------------------------------------------------
# MaintenanceRunner.run_data_retention
# ---------------------------------------------------------------------------


def _make_retention_bot():
    """MagicMock bot with optional attrs set to None so hasattr gates pass."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = ConfigParser()
    bot.web_viewer_integration = None
    bot.repeater_manager = None
    bot.command_manager = None
    bot.mesh_graph = None
    return bot


class TestMaintenanceRunDataRetention:
    def test_sets_ran_at_on_success(self):
        bot = _make_retention_bot()
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.run_data_retention()
        assert "ran_at" in runner._last_retention_stats

    def test_calls_db_cleanup_expired_cache(self):
        bot = _make_retention_bot()
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.run_data_retention()
        bot.db_manager.cleanup_expired_cache.assert_called_once()

    def test_calls_mesh_graph_cleanup_when_present(self):
        bot = _make_retention_bot()
        mg = Mock()
        bot.mesh_graph = mg
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.run_data_retention()
        mg.delete_expired_edges_from_db.assert_called_once()

    def test_calls_stats_cmd_cleanup_when_present(self):
        bot = _make_retention_bot()
        stats_cmd = Mock()
        bot.command_manager = Mock()
        bot.command_manager.commands = {"stats": stats_cmd}
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.run_data_retention()
        stats_cmd.cleanup_old_stats.assert_called_once_with(7)

    def test_sets_error_on_exception(self):
        bot = _make_retention_bot()
        bot.db_manager.cleanup_expired_cache.side_effect = RuntimeError("disk full")
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.run_data_retention()
        assert "error" in runner._last_retention_stats

    def test_calls_web_viewer_cleanup_when_present(self):
        bot = _make_retention_bot()
        bi = Mock()
        wvi = Mock()
        wvi.bot_integration = bi
        bot.web_viewer_integration = wvi
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.run_data_retention()
        bi.cleanup_old_data.assert_called_once()


# ---------------------------------------------------------------------------
# MaintenanceRunner.collect_email_stats
# ---------------------------------------------------------------------------


class TestMaintenanceCollectEmailStats:
    def _make_runner(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = ConfigParser()
        bot.connection_time = None
        return bot

    def test_returns_unknown_uptime_when_no_connection_time(self):
        bot = self._make_runner()
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        stats = runner.collect_email_stats()
        assert stats["uptime"] == "unknown"

    def test_returns_uptime_string_when_connected(self):
        bot = self._make_runner()
        bot.connection_time = time.time() - 3700  # ~1h 1m ago
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        stats = runner.collect_email_stats()
        assert "h" in stats["uptime"]

    def test_includes_retention_key(self):
        bot = self._make_runner()
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner._last_retention_stats = {"ran_at": "2026-01-01T03:00:00"}
        stats = runner.collect_email_stats()
        assert stats["retention"]["ran_at"] == "2026-01-01T03:00:00"

    def test_db_size_unknown_when_path_missing(self):
        bot = self._make_runner()
        bot.db_manager.db_path = "/nonexistent/path.db"
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        stats = runner.collect_email_stats()
        assert stats.get("db_size_mb") == "unknown"


# ---------------------------------------------------------------------------
# MaintenanceRunner.format_email_body
# ---------------------------------------------------------------------------


class TestMaintenanceFormatEmailBody:
    def _runner(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.connected = True
        return MaintenanceRunner(bot, get_current_time=datetime.datetime.now)

    def test_contains_period_and_sections(self):
        runner = self._runner()
        body = runner.format_email_body(
            {"uptime": "2h 5m", "contacts_24h": 3, "db_size_mb": "1.2"},
            "2026-04-07 06:00 UTC",
            "2026-04-08 06:00 UTC",
        )
        assert "2026-04-07 06:00 UTC" in body
        assert "BOT STATUS" in body
        assert "NETWORK ACTIVITY" in body
        assert "DATABASE" in body

    def test_retention_ran_at_included_when_set(self):
        runner = self._runner()
        runner._last_retention_stats = {"ran_at": "2026-04-07T03:00:00"}
        body = runner.format_email_body({}, "s", "e")
        assert "2026-04-07T03:00:00" in body

    def test_log_section_included_when_log_file_present(self):
        runner = self._runner()
        body = runner.format_email_body(
            {"log_file": "/var/log/bot.log", "log_rotated_24h": True, "log_backup_size_mb": "0.5"},
            "s", "e",
        )
        assert "LOG FILES" in body
        assert "Rotated : yes" in body


# ---------------------------------------------------------------------------
# MaintenanceRunner.send_nightly_email
# ---------------------------------------------------------------------------


class TestMaintenanceSendNightlyEmail:
    def _runner_with_notif(self, overrides: dict):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = ConfigParser()
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        defaults: dict = {
            "nightly_enabled": "true",
            "smtp_host": "smtp.example.com",
            "from_email": "bot@example.com",
            "recipients": "admin@example.com",
            "smtp_security": "starttls",
        }
        defaults.update(overrides)
        runner.get_notif = Mock(side_effect=lambda k: defaults.get(k, ""))
        runner.get_maint = Mock(return_value="")
        return runner

    def test_skips_when_disabled(self):
        runner = self._runner_with_notif({"nightly_enabled": "false"})
        runner.send_nightly_email()
        runner.bot.db_manager.set_metadata.assert_not_called()

    def test_warns_when_smtp_incomplete(self):
        runner = self._runner_with_notif({"smtp_host": "", "from_email": "", "recipients": ""})
        runner.send_nightly_email()
        runner.bot.logger.warning.assert_called()

    def test_sends_via_starttls_when_configured(self):
        runner = self._runner_with_notif({})
        with patch("modules.maintenance.validate_external_url", return_value=True), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__ = Mock(return_value=mock_smtp.return_value)
            mock_smtp.return_value.__exit__ = Mock(return_value=False)
            runner.send_nightly_email()
        mock_smtp.assert_called_once()

    def test_logs_error_on_smtp_failure(self):
        runner = self._runner_with_notif({})
        with patch("modules.maintenance.validate_external_url", return_value=True), \
             patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            runner.send_nightly_email()
        runner.bot.logger.error.assert_called()


# ---------------------------------------------------------------------------
# MaintenanceRunner.apply_log_rotation_config
# ---------------------------------------------------------------------------


class TestMaintenanceApplyLogRotationConfig:
    def _runner(self, max_bytes="", backup_count=""):
        bot = MagicMock()
        bot.logger = Mock()
        runner = MaintenanceRunner(bot, get_current_time=datetime.datetime.now)
        runner.get_maint = Mock(side_effect=lambda k: {
            "log_max_bytes": max_bytes,
            "log_backup_count": backup_count,
        }.get(k, ""))
        return runner

    def test_no_op_when_no_metadata(self):
        runner = self._runner()
        runner.apply_log_rotation_config()
        runner.bot.db_manager.set_metadata.assert_not_called()

    def test_replaces_rotating_file_handler(self, tmp_path: Path):
        import logging
        from logging.handlers import RotatingFileHandler

        log_file = tmp_path / "test.log"
        log_file.touch()
        handler = RotatingFileHandler(str(log_file))
        runner = self._runner(max_bytes="1048576", backup_count="3")
        logger = logging.getLogger("test_maintenance_replace")
        logger.handlers = [handler]
        runner.bot.logger = logger

        runner.apply_log_rotation_config()

        assert runner._last_log_rotation_applied == {
            "max_bytes": "1048576", "backup_count": "3"
        }

    def test_same_config_twice_is_no_op(self):
        runner = self._runner(max_bytes="5242880", backup_count="3")
        runner._last_log_rotation_applied = {"max_bytes": "5242880", "backup_count": "3"}
        runner.apply_log_rotation_config()
        runner.bot.db_manager.set_metadata.assert_not_called()


# ---------------------------------------------------------------------------
# MaintenanceRunner.maybe_run_db_backup — daily schedule paths
# ---------------------------------------------------------------------------


class TestMaintenanceMaybeRunDbBackupDaily:
    def _runner(self, now: datetime.datetime, maint_overrides: dict):
        bot = MagicMock()
        bot.logger = Mock()
        bot.db_manager.get_metadata = Mock(return_value=None)
        runner = MaintenanceRunner(bot, get_current_time=lambda: now)
        defaults = {
            "db_backup_enabled": "true",
            "db_backup_schedule": "daily",
            "db_backup_time": f"{now.hour:02d}:{now.minute:02d}",
        }
        defaults.update(maint_overrides)
        runner.get_maint = Mock(side_effect=lambda k: defaults.get(k, ""))
        return runner

    def test_manual_schedule_never_fires(self):
        now = datetime.datetime(2026, 4, 7, 2, 1, 0)
        runner = self._runner(now, {"db_backup_schedule": "manual"})
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_not_called()

    def test_disabled_never_fires(self):
        now = datetime.datetime(2026, 4, 7, 2, 1, 0)
        runner = self._runner(now, {"db_backup_enabled": "false"})
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_not_called()

    def test_daily_fires_in_window(self):
        now = datetime.datetime(2026, 4, 7, 2, 1, 0)
        runner = self._runner(now, {})
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_called_once()

    def test_daily_skips_outside_window(self):
        now = datetime.datetime(2026, 4, 7, 3, 30, 0)
        runner = self._runner(now, {"db_backup_time": "02:00"})
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_not_called()

    def test_daily_skips_when_already_ran_today(self):
        now = datetime.datetime(2026, 4, 7, 2, 1, 0)
        runner = self._runner(now, {})
        runner._last_db_backup_stats = {"ran_at": "2026-04-07T01:00:00"}
        with patch.object(runner, "run_db_backup") as mock_run:
            runner.maybe_run_db_backup()
        mock_run.assert_not_called()
