"""Tests for ScheduleCommand."""

from configparser import ConfigParser
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.schedule_command import ScheduleCommand
from tests.conftest import mock_message


@pytest.fixture
def bot(mock_logger):
    b = Mock()
    b.logger = mock_logger
    b.config = ConfigParser()
    b.config.add_section("Bot")
    b.config.set("Bot", "bot_name", "TestBot")
    b.translator = Mock()
    b.translator.translate = Mock(side_effect=lambda k, **kw: k)
    b.translator.get_value = Mock(return_value=None)
    b.command_manager = Mock()
    b.command_manager.send_response = AsyncMock(return_value=True)
    b.command_manager.monitor_channels = ["general"]
    b.scheduler = Mock()
    b.scheduler.scheduled_messages = {}
    return b


def make_cmd(bot):
    return ScheduleCommand(bot)


# ---------------------------------------------------------------------------
# TestCanExecute
# ---------------------------------------------------------------------------


class TestCanExecute:
    def test_dm_allowed_by_default(self, bot):
        cmd = make_cmd(bot)
        msg = mock_message(is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_channel_blocked_when_dm_only(self, bot):
        cmd = make_cmd(bot)
        msg = mock_message(channel="general", is_dm=False)
        assert cmd.can_execute(msg) is False

    def test_channel_allowed_when_dm_only_false(self, bot):
        bot.config.add_section("Schedule_Command")
        bot.config.set("Schedule_Command", "dm_only", "false")
        cmd = make_cmd(bot)
        msg = mock_message(channel="general", is_dm=False)
        assert cmd.can_execute(msg) is True

    def test_disabled_blocks_all(self, bot):
        bot.config.add_section("Schedule_Command")
        bot.config.set("Schedule_Command", "enabled", "false")
        cmd = make_cmd(bot)
        msg = mock_message(is_dm=True)
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# TestBuildResponse — no scheduled messages
# ---------------------------------------------------------------------------


class TestBuildResponseEmpty:
    def test_no_schedules_says_none_configured(self, bot):
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "No scheduled messages configured" in response

    def test_no_advert_interval_omits_advert_line(self, bot):
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "Advert" not in response


# ---------------------------------------------------------------------------
# TestBuildResponse — with scheduled messages
# ---------------------------------------------------------------------------


class TestBuildResponseWithSchedules:
    def test_shows_scheduled_count(self, bot):
        bot.scheduler.scheduled_messages = {
            "0900": ("general", "Good morning!", "09:00"),
            "1800": ("general", "Good evening!", "18:00"),
        }
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "Scheduled (2)" in response

    def test_formats_time_as_hhmm(self, bot):
        bot.scheduler.scheduled_messages = {"0930": ("general", "Hello", "09:30")}
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "09:30" in response

    def test_shows_channel_and_message(self, bot):
        bot.scheduler.scheduled_messages = {
            "1200": ("alerts", "Noon check-in", "12:00"),
        }
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "#alerts" in response
        assert "Noon check-in" in response

    def test_long_message_is_truncated(self, bot):
        long_msg = "A" * 100
        bot.scheduler.scheduled_messages = {"0800": ("general", long_msg, "08:00")}
        cmd = make_cmd(bot)
        response = cmd._build_response()
        # Preview should be ≤43 chars (40 + "...")
        for line in response.splitlines():
            if "08:00" in line:
                # Extract the message part after the channel
                parts = line.split(": ", 2)
                if len(parts) == 3:
                    assert len(parts[2]) <= 43

    def test_entries_sorted_by_time(self, bot):
        bot.scheduler.scheduled_messages = {
            "1800": ("general", "Evening", "18:00"),
            "0600": ("general", "Morning", "06:00"),
            "1200": ("general", "Noon", "12:00"),
        }
        cmd = make_cmd(bot)
        response = cmd._build_response()
        lines = [l for l in response.splitlines() if ":" in l and "#" in l]
        times = [l.strip().split(" ")[0] for l in lines]
        assert times == sorted(times)

    def test_shows_cron_schedule_string(self, bot):
        bot.scheduler.scheduled_messages = {
            "0 9 * * *": ("general", "Cron hello", "0 9 * * *"),
        }
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "0 9 * * *" in response
        assert "Cron hello" in response

    def test_shows_at_preset_label(self, bot):
        bot.scheduler.scheduled_messages = {
            "@hourly": ("general", "tick", "@hourly"),
        }
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "@hourly" in response
        assert "tick" in response


# ---------------------------------------------------------------------------
# TestAdvertInfo
# ---------------------------------------------------------------------------


class TestAdvertInfo:
    def test_advert_interval_shown(self, bot):
        bot.config.set("Bot", "advert_interval_hours", "4")
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "Advert interval: every 4h" in response

    def test_zero_interval_omitted(self, bot):
        bot.config.set("Bot", "advert_interval_hours", "0")
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "Advert" not in response

    def test_missing_advert_interval_omitted(self, bot):
        # advert_interval_hours not in config → fallback 0 → omitted
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "Advert" not in response


# ---------------------------------------------------------------------------
# TestNoScheduler
# ---------------------------------------------------------------------------


class TestNoScheduler:
    def test_no_scheduler_attr_returns_gracefully(self, bot):
        del bot.scheduler
        cmd = make_cmd(bot)
        response = cmd._build_response()
        assert "No scheduled messages configured" in response


# ---------------------------------------------------------------------------
# TestExecute
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_calls_send_response(self, bot):
        cmd = make_cmd(bot)
        msg = mock_message(is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        bot.command_manager.send_response.assert_called_once()
        call_args = bot.command_manager.send_response.call_args
        response_text = call_args[0][1]  # second positional arg is content
        assert isinstance(response_text, str)
        assert len(response_text) > 0
