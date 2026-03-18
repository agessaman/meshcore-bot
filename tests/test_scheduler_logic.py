"""Tests for MessageScheduler pure logic (no threading, no asyncio)."""

import datetime
import time
from configparser import ConfigParser
from unittest.mock import Mock, patch

import pytest

from modules.scheduler import MessageScheduler


@pytest.fixture
def scheduler(mock_logger):
    """MessageScheduler with mock bot for pure logic tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Bot")
    return MessageScheduler(bot)


class TestIsValidTimeFormat:
    """Tests for _is_valid_time_format()."""

    def test_valid_time_0000(self, scheduler):
        assert scheduler._is_valid_time_format("0000") is True

    def test_valid_time_2359(self, scheduler):
        assert scheduler._is_valid_time_format("2359") is True

    def test_valid_time_1200(self, scheduler):
        assert scheduler._is_valid_time_format("1200") is True

    def test_invalid_time_2400(self, scheduler):
        assert scheduler._is_valid_time_format("2400") is False

    def test_invalid_time_0060(self, scheduler):
        assert scheduler._is_valid_time_format("0060") is False

    def test_invalid_time_short(self, scheduler):
        assert scheduler._is_valid_time_format("123") is False

    def test_invalid_time_letters(self, scheduler):
        assert scheduler._is_valid_time_format("abcd") is False

    def test_invalid_time_empty(self, scheduler):
        assert scheduler._is_valid_time_format("") is False


class TestGetCurrentTime:
    """Tests for timezone-aware time retrieval."""

    def test_valid_timezone(self, scheduler):
        scheduler.bot.config.set("Bot", "timezone", "US/Pacific")
        result = scheduler.get_current_time()
        assert result.tzinfo is not None

    def test_invalid_timezone_falls_back(self, scheduler):
        scheduler.bot.config.set("Bot", "timezone", "Invalid/Zone")
        result = scheduler.get_current_time()
        # Should still return a datetime (system time fallback)
        assert result is not None
        scheduler.bot.logger.warning.assert_called()

    def test_empty_timezone_uses_system(self, scheduler):
        scheduler.bot.config.set("Bot", "timezone", "")
        result = scheduler.get_current_time()
        assert result is not None


class TestHasMeshInfoPlaceholders:
    """Tests for _has_mesh_info_placeholders()."""

    def test_detects_placeholder(self, scheduler):
        assert scheduler._has_mesh_info_placeholders("Contacts: {total_contacts}") is True

    def test_no_placeholder_returns_false(self, scheduler):
        assert scheduler._has_mesh_info_placeholders("Hello world") is False

    def test_detects_legacy_placeholder(self, scheduler):
        assert scheduler._has_mesh_info_placeholders("Repeaters: {repeaters}") is True


# ---------------------------------------------------------------------------
# TestSetupScheduledMessages
# ---------------------------------------------------------------------------


class TestSetupScheduledMessages:
    """Tests for setup_scheduled_messages() — config parsing and APScheduler job registration."""

    def _setup_and_call(self, scheduler):
        """Run setup_scheduled_messages() with a real (but isolated) APScheduler."""
        scheduler.setup_scheduled_messages()

    def _teardown(self, scheduler):
        if scheduler._apscheduler is not None:
            try:
                scheduler._apscheduler.shutdown(wait=False)
            except Exception:
                pass

    def test_valid_entry_is_registered_and_stored(self, scheduler):
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "0900", "general: Good morning!")
        self._setup_and_call(scheduler)
        assert "0900" in scheduler.scheduled_messages
        channel, message = scheduler.scheduled_messages["0900"]
        assert channel == "general"
        assert "Good morning!" in message
        assert len(scheduler._apscheduler.get_jobs()) == 1
        self._teardown(scheduler)

    def test_invalid_time_format_is_skipped(self, scheduler):
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "9999", "general: Bad time")
        self._setup_and_call(scheduler)
        assert "9999" not in scheduler.scheduled_messages
        self._teardown(scheduler)

    def test_missing_colon_separator_is_skipped(self, scheduler):
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "0800", "no colon here")
        self._setup_and_call(scheduler)
        assert "0800" not in scheduler.scheduled_messages
        self._teardown(scheduler)

    def test_no_scheduled_messages_section_does_not_raise(self, scheduler):
        # No [Scheduled_Messages] section in config
        self._setup_and_call(scheduler)
        assert scheduler.scheduled_messages == {}
        self._teardown(scheduler)

    def test_multiple_entries_all_registered(self, scheduler):
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "0700", "general: Morning")
        scheduler.bot.config.set("Scheduled_Messages", "1200", "general: Noon")
        scheduler.bot.config.set("Scheduled_Messages", "1800", "general: Evening")
        self._setup_and_call(scheduler)
        assert len(scheduler.scheduled_messages) == 3
        assert len(scheduler._apscheduler.get_jobs()) == 3
        self._teardown(scheduler)

    def test_message_escape_sequences_decoded(self, scheduler):
        """\\n in config value should be decoded to a real newline in the stored message."""
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "1000", r"general: Line1\nLine2")
        self._setup_and_call(scheduler)
        _, message = scheduler.scheduled_messages["1000"]
        assert "\n" in message
        self._teardown(scheduler)

    def test_reload_replaces_existing_jobs(self, scheduler):
        """Calling setup_scheduled_messages() twice should not duplicate jobs."""
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "0700", "general: Morning")
        self._setup_and_call(scheduler)
        self._setup_and_call(scheduler)  # second call — should replace, not add
        assert len(scheduler._apscheduler.get_jobs()) == 1
        self._teardown(scheduler)


# ---------------------------------------------------------------------------
# TestSetupIntervalAdvertising
# ---------------------------------------------------------------------------


class TestSetupIntervalAdvertising:
    """Tests for setup_interval_advertising()."""

    def test_positive_interval_initialises_last_advert_time(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "6")
        del scheduler.bot.last_advert_time  # ensure hasattr returns False
        scheduler.bot.last_advert_time = None
        scheduler.setup_interval_advertising()
        assert scheduler.bot.last_advert_time is not None

    def test_last_advert_time_not_overwritten_when_already_set(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "6")
        scheduler.bot.last_advert_time = 12345.0
        scheduler.setup_interval_advertising()
        assert scheduler.bot.last_advert_time == 12345.0

    def test_zero_interval_logs_disabled(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "0")
        scheduler.setup_interval_advertising()
        scheduler.bot.logger.info.assert_called()

    def test_default_interval_zero_when_unset(self, scheduler):
        # advert_interval_hours not in config → fallback 0 → disabled
        scheduler.setup_interval_advertising()
        scheduler.bot.logger.info.assert_called()


# ---------------------------------------------------------------------------
# check_interval_advertising
# ---------------------------------------------------------------------------

class TestCheckIntervalAdvertising:

    def test_disabled_when_interval_zero(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "0")
        scheduler.bot.last_advert_time = time.time() - 99999
        with patch.object(scheduler, "send_interval_advert") as mock_send:
            scheduler.check_interval_advertising()
        mock_send.assert_not_called()

    def test_first_call_sets_last_advert_time(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "1")
        scheduler.bot.last_advert_time = None
        scheduler.check_interval_advertising()
        assert scheduler.bot.last_advert_time is not None

    def test_not_enough_time_passed_no_advert(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "1")
        scheduler.bot.last_advert_time = time.time() - 1800  # 30 min ago, need 60 min
        with patch.object(scheduler, "send_interval_advert") as mock_send:
            scheduler.check_interval_advertising()
        mock_send.assert_not_called()

    def test_enough_time_passed_sends_advert(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "1")
        scheduler.bot.last_advert_time = time.time() - 3700  # > 1 hour ago
        with patch.object(scheduler, "send_interval_advert") as mock_send:
            scheduler.check_interval_advertising()
        mock_send.assert_called_once()

    def test_last_advert_time_updated_after_send(self, scheduler):
        scheduler.bot.config.set("Bot", "advert_interval_hours", "1")
        old_time = time.time() - 3700
        scheduler.bot.last_advert_time = old_time
        with patch.object(scheduler, "send_interval_advert"):
            scheduler.check_interval_advertising()
        assert scheduler.bot.last_advert_time > old_time


# ---------------------------------------------------------------------------
# _get_notif / _get_maint
# ---------------------------------------------------------------------------

class TestGetNotifAndMaint:

    def test_get_notif_returns_value(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(return_value="smtp.example.com")
        assert scheduler._get_notif("smtp_host") == "smtp.example.com"
        scheduler.bot.db_manager.get_metadata.assert_called_with("notif.smtp_host")

    def test_get_notif_returns_empty_on_none(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(return_value=None)
        assert scheduler._get_notif("smtp_host") == ""

    def test_get_notif_returns_empty_on_exception(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(side_effect=Exception("db error"))
        assert scheduler._get_notif("smtp_host") == ""

    def test_get_maint_returns_value(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(return_value="daily")
        assert scheduler._get_maint("db_backup_schedule") == "daily"
        scheduler.bot.db_manager.get_metadata.assert_called_with("maint.db_backup_schedule")

    def test_get_maint_returns_empty_on_none(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(return_value=None)
        assert scheduler._get_maint("db_backup_enabled") == ""

    def test_get_maint_returns_empty_on_exception(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(side_effect=RuntimeError("fail"))
        assert scheduler._get_maint("any") == ""


# ---------------------------------------------------------------------------
# _format_email_body
# ---------------------------------------------------------------------------

class TestFormatEmailBody:

    def _minimal_stats(self):
        return {
            "uptime": "2h 15m",
            "contacts_24h": 5,
            "contacts_new_24h": 1,
            "contacts_total": 42,
            "db_size_mb": "12.3",
            "errors_24h": 0,
            "criticals_24h": 0,
        }

    def test_basic_output_contains_uptime(self, scheduler):
        scheduler.bot.connected = True
        body = scheduler._format_email_body(self._minimal_stats(), "2026-03-15 00:00", "2026-03-16 00:00")
        assert "2h 15m" in body

    def test_basic_output_contains_contact_count(self, scheduler):
        scheduler.bot.connected = True
        body = scheduler._format_email_body(self._minimal_stats(), "start", "end")
        assert "42" in body

    def test_log_file_section_included_when_present(self, scheduler):
        scheduler.bot.connected = False
        stats = self._minimal_stats()
        stats["log_file"] = "/var/log/meshcore.log"
        stats["log_size_mb"] = "1.5"
        stats["log_rotated_24h"] = False
        body = scheduler._format_email_body(stats, "start", "end")
        assert "meshcore.log" in body
        assert "Rotated : no" in body

    def test_log_rotated_true_shows_backup_size(self, scheduler):
        scheduler.bot.connected = False
        stats = self._minimal_stats()
        stats["log_file"] = "/var/log/meshcore.log"
        stats["log_size_mb"] = "1.5"
        stats["log_rotated_24h"] = True
        stats["log_backup_size_mb"] = "3.2"
        body = scheduler._format_email_body(stats, "start", "end")
        assert "Rotated : yes" in body
        assert "3.2" in body

    def test_retention_ran_at_shown_when_present(self, scheduler):
        scheduler.bot.connected = True
        scheduler._last_retention_stats = {"ran_at": "2026-03-15T02:00:00"}
        body = scheduler._format_email_body(self._minimal_stats(), "start", "end")
        assert "2026-03-15T02:00:00" in body

    def test_connected_no_shows_in_body(self, scheduler):
        scheduler.bot.connected = False
        body = scheduler._format_email_body(self._minimal_stats(), "start", "end")
        assert "no" in body


# ---------------------------------------------------------------------------
# _maybe_run_db_backup
# ---------------------------------------------------------------------------

class TestMaybeRunDbBackup:

    def _setup(self, scheduler, enabled="true", schedule="daily",
               time_str="02:00", last_ran=""):
        def maint(key):
            return {
                "db_backup_enabled": enabled,
                "db_backup_schedule": schedule,
                "db_backup_time": time_str,
                "db_backup_retention_count": "7",
                "db_backup_dir": "/tmp/backup",
            }.get(key, "")
        scheduler._get_maint = Mock(side_effect=maint)
        scheduler._last_db_backup_stats = {"ran_at": last_ran}

    def test_disabled_does_not_run(self, scheduler):
        self._setup(scheduler, enabled="false")
        with patch.object(scheduler, "_run_db_backup") as mock_run:
            scheduler._maybe_run_db_backup()
        mock_run.assert_not_called()

    def test_manual_schedule_does_not_run(self, scheduler):
        self._setup(scheduler, schedule="manual")
        with patch.object(scheduler, "_run_db_backup") as mock_run:
            scheduler._maybe_run_db_backup()
        mock_run.assert_not_called()

    def test_already_ran_today_does_not_run(self, scheduler):
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        self._setup(scheduler, time_str="00:00", last_ran=f"{today}T00:01:00")
        with patch.object(scheduler, "_run_db_backup") as mock_run:
            scheduler._maybe_run_db_backup()
        mock_run.assert_not_called()

    def test_runs_when_time_passed_and_not_run_today(self, scheduler):
        # Use yesterday as last_ran so today triggers a run
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self._setup(scheduler, time_str="00:00", last_ran=f"{yesterday}T00:01:00")
        with patch.object(scheduler, "_run_db_backup") as mock_run:
            scheduler._maybe_run_db_backup()
        mock_run.assert_called_once()

    def test_weekly_on_wrong_day_does_not_run(self, scheduler):
        # Force a day that isn't Monday (weekday != 0) by using a Tuesday
        self._setup(scheduler, schedule="weekly", time_str="00:00", last_ran="")
        fake_now = Mock()
        fake_now.weekday.return_value = 1  # Tuesday
        fake_now.replace.return_value = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        fake_now.__lt__ = lambda s, o: False  # now >= scheduled time
        fake_now.strftime = datetime.datetime.now().strftime
        fake_now.isocalendar.return_value = (2026, 11, 2)
        with patch.object(scheduler, "get_current_time", return_value=fake_now):
            with patch.object(scheduler, "_run_db_backup") as mock_run:
                scheduler._maybe_run_db_backup()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _apply_log_rotation_config
# ---------------------------------------------------------------------------

class TestApplyLogRotationConfig:

    def test_no_maint_settings_returns_early(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(return_value=None)
        # No logger handlers to worry about
        scheduler._apply_log_rotation_config()  # Should not raise

    def test_same_settings_not_reapplied(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(side_effect=lambda k: {
            "maint.log_max_bytes": "5242880",
            "maint.log_backup_count": "3",
        }.get(k))
        scheduler._last_log_rotation_applied = {
            "max_bytes": "5242880",
            "backup_count": "3",
        }
        # No RotatingFileHandler in mock logger
        scheduler.bot.logger.handlers = []
        scheduler._apply_log_rotation_config()  # Should not raise or modify

    def test_invalid_value_logs_warning(self, scheduler):
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(side_effect=lambda k: {
            "maint.log_max_bytes": "not-a-number",
            "maint.log_backup_count": "3",
        }.get(k))
        scheduler._last_log_rotation_applied = {}
        scheduler.bot.logger.handlers = []
        scheduler._apply_log_rotation_config()
        scheduler.bot.logger.warning.assert_called()

    def test_rotating_handler_replaced(self, scheduler):
        import os
        import tempfile
        from logging.handlers import RotatingFileHandler
        scheduler.bot.db_manager = Mock()
        scheduler.bot.db_manager.get_metadata = Mock(side_effect=lambda k: {
            "maint.log_max_bytes": "10485760",
            "maint.log_backup_count": "5",
        }.get(k))
        scheduler._last_log_rotation_applied = {}

        # Create a real RotatingFileHandler pointed at a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as tf:
            tmp_path = tf.name
        try:
            handler = RotatingFileHandler(tmp_path, maxBytes=1024, backupCount=1)
            scheduler.bot.logger.handlers = [handler]
            scheduler._apply_log_rotation_config()
            new_handler = scheduler.bot.logger.handlers[0]
            assert new_handler.maxBytes == 10485760
            assert new_handler.backupCount == 5
            new_handler.close()
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# TestAPSchedulerLifecycle
# ---------------------------------------------------------------------------


class TestAPSchedulerLifecycle:
    """Tests for APScheduler start/shutdown lifecycle in MessageScheduler."""

    def test_apscheduler_created_on_setup(self, scheduler):
        scheduler.setup_scheduled_messages()
        assert scheduler._apscheduler is not None
        assert scheduler._apscheduler.running
        scheduler.join(timeout=1)

    def test_join_shuts_down_apscheduler(self, scheduler):
        scheduler.setup_scheduled_messages()
        assert scheduler._apscheduler.running
        scheduler.join(timeout=1)
        assert not scheduler._apscheduler.running

    def test_join_with_no_apscheduler_does_not_raise(self, scheduler):
        assert scheduler._apscheduler is None
        scheduler.join(timeout=0.1)  # must not raise

    def test_cron_trigger_hour_minute(self, scheduler):
        """Registered jobs get a CronTrigger with the correct hour/minute."""
        scheduler.bot.config.add_section("Scheduled_Messages")
        scheduler.bot.config.set("Scheduled_Messages", "1430", "ch: hello")
        scheduler.setup_scheduled_messages()
        jobs = scheduler._apscheduler.get_jobs()
        assert len(jobs) == 1
        trigger = jobs[0].trigger
        # CronTrigger fields: hour=14, minute=30
        field_map = {f.name: f for f in trigger.fields}
        assert str(field_map["hour"]) == "14"
        assert str(field_map["minute"]) == "30"
        scheduler.join(timeout=1)
