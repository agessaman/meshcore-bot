#!/usr/bin/env python3
"""
Unit tests for {cmd:...} placeholders in scheduled messages.

The contract that matters most: rendering a command for its text must never put
anything on the air. Everything else (unknown command, disabled command, admin
command, timeout, failure) degrades to an empty substitution rather than leaking
raw placeholder text into a broadcast.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.models import MeshMessage


class _Recorder:
    """Stands in for the pieces of CommandManager the render path touches."""

    def __init__(self):
        self.sent_dms = []
        self.sent_channel = []


def _make_manager(bot, commands):
    from modules.command_manager import CommandManager

    mgr = object.__new__(CommandManager)
    mgr.bot = bot
    mgr.logger = MagicMock()
    mgr.commands = commands
    mgr._last_response = None
    return mgr


def _make_command(*, name, keywords, reply, enabled=True, admin=False, delay=0.0):
    cmd = MagicMock()
    cmd.name = name
    cmd.keywords = keywords
    cmd._derive_config_section_name.return_value = f"{name.title()}_Command"
    cmd.get_config_value.return_value = enabled
    cmd.requires_admin_access.return_value = admin
    # Off cooldown unless a test says otherwise.
    cmd.check_cooldown.return_value = (True, 0.0)

    async def execute(message):
        if delay:
            await asyncio.sleep(delay)
        if reply is not None:
            message.capture_sink.append(reply)
        return True

    cmd.execute = execute
    return cmd


@pytest.mark.unit
class TestRenderCommandOutput:
    @pytest.mark.asyncio
    async def test_returns_reply_text(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx", "weather"], reply="Seattle: 12C rain")
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("wx Seattle") == "Seattle: 12C rain"

    @pytest.mark.asyncio
    async def test_resolves_by_alias_keyword(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx", "weather"], reply="ok")
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("weather Tacoma") == "ok"

    @pytest.mark.asyncio
    async def test_passes_full_spec_as_message_content(self, mock_bot):
        seen = {}

        async def execute(message):
            seen['content'] = message.content
            seen['capture'] = message.capture_sink is not None
            message.capture_sink.append("x")
            return True

        wx = _make_command(name="wx", keywords=["wx"], reply="x")
        wx.execute = execute
        mgr = _make_manager(mock_bot, {"wx": wx})
        await mgr.render_command_output("wx Seattle 98101")
        assert seen['content'] == "wx Seattle 98101"
        assert seen['capture'] is True

    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self, mock_bot):
        mgr = _make_manager(mock_bot, {})
        assert await mgr.render_command_output("nope arg") is None

    @pytest.mark.asyncio
    async def test_disabled_command_returns_none(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx"], reply="ok", enabled=False)
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("wx") is None

    @pytest.mark.asyncio
    async def test_admin_command_is_refused(self, mock_bot):
        adm = _make_command(name="admin", keywords=["admin"], reply="secret", admin=True)
        mgr = _make_manager(mock_bot, {"admin": adm})
        assert await mgr.render_command_output("admin reboot") is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, mock_bot):
        slow = _make_command(name="slow", keywords=["slow"], reply="late", delay=0.5)
        mgr = _make_manager(mock_bot, {"slow": slow})
        assert await mgr.render_command_output("slow", timeout=0.05) is None

    @pytest.mark.asyncio
    async def test_raising_command_returns_none(self, mock_bot):
        boom = _make_command(name="boom", keywords=["boom"], reply=None)

        async def explode(message):
            raise RuntimeError("kaboom")

        boom.execute = explode
        mgr = _make_manager(mock_bot, {"boom": boom})
        assert await mgr.render_command_output("boom") is None

    @pytest.mark.asyncio
    async def test_silent_command_returns_none(self, mock_bot):
        quiet = _make_command(name="quiet", keywords=["quiet"], reply=None)
        mgr = _make_manager(mock_bot, {"quiet": quiet})
        assert await mgr.render_command_output("quiet") is None

    @pytest.mark.asyncio
    async def test_multiple_sends_are_joined(self, mock_bot):
        multi = _make_command(name="multi", keywords=["multi"], reply=None)

        async def two_parts(message):
            message.capture_sink.append("part one")
            message.capture_sink.append("part two")
            return True

        multi.execute = two_parts
        mgr = _make_manager(mock_bot, {"multi": multi})
        assert await mgr.render_command_output("multi") == "part one\npart two"


@pytest.mark.unit
class TestCaptureSuppressesTransmission:
    @pytest.mark.asyncio
    async def test_send_response_captures_instead_of_sending(self, mock_bot):
        """The whole point: a rendered command must not spend airtime."""
        from modules.command_manager import CommandManager

        mgr = object.__new__(CommandManager)
        mgr.bot = mock_bot
        mgr.logger = MagicMock()
        mgr._last_response = "previous real response"
        mgr.send_dm = AsyncMock()
        mgr.send_channel_message = AsyncMock()

        sink = []
        msg = MeshMessage(content="wx", channel="#general", is_dm=False, capture_sink=sink)

        assert await CommandManager.send_response(mgr, msg, "rendered text") is True
        assert sink == ["rendered text"]
        mgr.send_dm.assert_not_called()
        mgr.send_channel_message.assert_not_called()
        # A background render must not clobber the response captured for a real command.
        assert mgr._last_response == "previous real response"


def _make_scheduler(bot, render_result):
    from modules.scheduler import MessageScheduler

    sched = object.__new__(MessageScheduler)
    sched.bot = bot
    sched.logger = MagicMock()
    bot.command_manager = MagicMock()
    if isinstance(render_result, Exception):
        bot.command_manager.render_command_output = AsyncMock(side_effect=render_result)
    elif callable(render_result):
        bot.command_manager.render_command_output = AsyncMock(side_effect=render_result)
    else:
        bot.command_manager.render_command_output = AsyncMock(return_value=render_result)
    return sched


@pytest.mark.unit
class TestExpandCommandPlaceholders:
    def test_detects_placeholder(self, mock_bot):
        sched = _make_scheduler(mock_bot, "x")
        assert sched._has_command_placeholders("Forecast: {cmd:wx Seattle}") is True
        assert sched._has_command_placeholders("no placeholders here") is False

    @pytest.mark.asyncio
    async def test_substitutes_output_in_place(self, mock_bot):
        sched = _make_scheduler(mock_bot, "12C rain")
        out = await sched._expand_command_placeholders("Today: {cmd:wx Seattle}", "#general")
        assert out == "Today: 12C rain"

    @pytest.mark.asyncio
    async def test_passes_channel_and_spec(self, mock_bot):
        sched = _make_scheduler(mock_bot, "ok")
        await sched._expand_command_placeholders("{cmd:wx Seattle 98101}", "#weather")
        kwargs = mock_bot.command_manager.render_command_output.call_args
        assert kwargs[0][0] == "wx Seattle 98101"
        assert kwargs[1]["channel"] == "#weather"

    @pytest.mark.asyncio
    async def test_expands_several_placeholders(self, mock_bot):
        replies = iter(["sunny", "3 contacts"])
        sched = _make_scheduler(mock_bot, lambda *a, **k: next(replies))
        out = await sched._expand_command_placeholders(
            "wx={cmd:wx} net={cmd:stats}", "#general"
        )
        assert out == "wx=sunny net=3 contacts"

    @pytest.mark.asyncio
    async def test_failed_render_leaves_no_raw_placeholder(self, mock_bot):
        sched = _make_scheduler(mock_bot, None)
        out = await sched._expand_command_placeholders("Today: {cmd:bogus}", "#general")
        assert "{cmd:" not in out
        assert out == "Today: "

    @pytest.mark.asyncio
    async def test_raising_render_is_contained(self, mock_bot):
        sched = _make_scheduler(mock_bot, RuntimeError("boom"))
        out = await sched._expand_command_placeholders("A {cmd:wx} B", "#general")
        assert out == "A  B"

    @pytest.mark.asyncio
    async def test_command_output_is_not_rescanned(self, mock_bot):
        """A reply containing {cmd:...} must not trigger another render."""
        sched = _make_scheduler(mock_bot, "look: {cmd:wx}")
        out = await sched._expand_command_placeholders("{cmd:wx}", "#general")
        assert out == "look: {cmd:wx}"
        assert mock_bot.command_manager.render_command_output.await_count == 1


@pytest.mark.unit
class TestRenderWithRealCommand:
    """End-to-end through a real command's plumbing, not a mocked execute()."""

    @pytest.mark.asyncio
    async def test_real_dice_command_renders_without_transmitting(self, command_mock_bot):
        """dice is a real render_safe command with no network dependency."""
        from modules.command_manager import CommandManager
        from modules.commands.dice_command import DiceCommand

        command_mock_bot.config.add_section("Dice_Command")
        command_mock_bot.config.set("Dice_Command", "enabled", "true")

        ping = DiceCommand(command_mock_bot)

        mgr = object.__new__(CommandManager)
        mgr.bot = command_mock_bot
        mgr.logger = MagicMock()
        mgr.commands = {"dice": ping}
        mgr._last_response = None
        mgr.send_dm = AsyncMock()
        mgr.send_channel_message = AsyncMock()
        # The real command calls self.bot.command_manager.send_response(...)
        command_mock_bot.command_manager.send_response = (
            lambda message, content, **kw: CommandManager.send_response(mgr, message, content, **kw)
        )

        rendered = await mgr.render_command_output("dice", channel="#general")

        assert rendered, "a real dice roll should produce text"
        mgr.send_dm.assert_not_called()
        mgr.send_channel_message.assert_not_called()


@pytest.mark.unit
class TestCommandCooldownStillApplies:
    """A schedule is not a licence to outrun a command's configured cooldown."""

    @pytest.mark.asyncio
    async def test_render_refused_while_on_cooldown(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx"], reply="12C")
        wx.check_cooldown.return_value = (False, 42.0)
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("wx Seattle") is None

    @pytest.mark.asyncio
    async def test_render_records_execution_so_cooldown_advances(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx"], reply="12C")
        wx.check_cooldown.return_value = (True, 0.0)
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("wx Seattle") == "12C"
        wx.record_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_execution_recorded_even_if_command_then_fails(self, mock_bot):
        """Recorded before execute, so a failing render cannot be retried immediately."""
        boom = _make_command(name="boom", keywords=["boom"], reply=None)
        boom.check_cooldown.return_value = (True, 0.0)

        async def explode(message):
            raise RuntimeError("kaboom")

        boom.execute = explode
        mgr = _make_manager(mock_bot, {"boom": boom})
        assert await mgr.render_command_output("boom") is None
        boom.record_execution.assert_called_once()


@pytest.mark.unit
class TestMinimumIntervalFloor:
    """{cmd:...} schedules may not fire more often than every 15 minutes."""

    @staticmethod
    def _interval(cron):
        import datetime

        from apscheduler.triggers.cron import CronTrigger

        from modules.scheduler import MessageScheduler

        tz = datetime.timezone.utc
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
        return MessageScheduler._min_fire_interval_seconds(trigger, tz)

    def test_floor_is_fifteen_minutes(self):
        from modules.scheduler import MessageScheduler

        assert MessageScheduler.MIN_COMMAND_PLACEHOLDER_INTERVAL_SECONDS == 900

    @pytest.mark.parametrize("cron,expected", [
        ("*/5 * * * *", 300),
        ("*/15 * * * *", 900),
        ("*/30 * * * *", 1800),
        ("0 * * * *", 3600),
        ("0 6,12,18 * * *", 21600),
    ])
    def test_even_schedules_measured_correctly(self, cron, expected):
        assert self._interval(cron) == expected

    def test_uneven_cron_measured_by_its_tightest_gap(self):
        """0,1 * * * * is a 60-second schedule, not an hourly one."""
        assert self._interval("0,1 * * * *") == 60

    def test_daily_schedule_is_well_above_the_floor(self):
        from modules.scheduler import MessageScheduler

        assert self._interval("0 8 * * *") > MessageScheduler.MIN_COMMAND_PLACEHOLDER_INTERVAL_SECONDS

    @pytest.mark.parametrize("cron,allowed", [
        ("* * * * *", False),
        ("*/5 * * * *", False),
        ("*/14 * * * *", False),
        ("0,1 * * * *", False),
        ("*/15 * * * *", True),
        ("*/30 * * * *", True),
        ("0 6,12,18 * * *", True),
    ])
    def test_floor_admits_and_rejects_the_right_schedules(self, cron, allowed):
        from modules.scheduler import MessageScheduler

        interval = self._interval(cron)
        floor = MessageScheduler.MIN_COMMAND_PLACEHOLDER_INTERVAL_SECONDS
        assert (interval >= floor) is allowed


@pytest.mark.unit
class TestFloorEnforcedDuringSetup:
    """The floor has to actually stop the job being scheduled, not just compute a number."""

    @staticmethod
    def _run_setup(entries):
        import configparser
        from unittest.mock import patch

        from modules.scheduler import MessageScheduler

        config = configparser.ConfigParser()
        config["Bot"] = {"timezone": "UTC"}
        config["Scheduled_Messages"] = entries

        bot = MagicMock()
        bot.config = config

        sched = object.__new__(MessageScheduler)
        sched.bot = bot
        sched.logger = MagicMock()
        sched.scheduled_messages = {}
        sched._shutdown_apscheduler_if_running = MagicMock()
        sched._setup_device_mode_scheduler_jobs = MagicMock()
        sched.setup_interval_advertising = MagicMock()

        fake_scheduler = MagicMock()
        with patch("modules.scheduler.BackgroundScheduler", return_value=fake_scheduler):
            sched.setup_scheduled_messages()
        return sched, fake_scheduler

    def test_too_frequent_command_schedule_is_not_added(self):
        sched, apsched = self._run_setup({"*/5 * * * *": "Public:{cmd:wx Seattle}"})
        assert apsched.add_job.call_count == 0
        assert sched.scheduled_messages == {}
        assert sched.logger.error.called

    def test_uneven_cron_with_tight_gap_is_not_added(self):
        _, apsched = self._run_setup({"0,1 * * * *": "Public:{cmd:wx Seattle}"})
        assert apsched.add_job.call_count == 0

    def test_schedule_at_the_floor_is_added(self):
        sched, apsched = self._run_setup({"*/15 * * * *": "Public:{cmd:wx Seattle}"})
        assert apsched.add_job.call_count == 1
        assert sched.scheduled_messages

    def test_frequent_schedule_without_placeholder_is_unaffected(self):
        """The floor applies to command placeholders, not to plain scheduled text."""
        _, apsched = self._run_setup({"*/5 * * * *": "Public:static message"})
        assert apsched.add_job.call_count == 1


@pytest.mark.unit
class TestRenderSafetyIsOptIn:
    """Capture only intercepts send_response and send_response_chunked, so anything
    that transmits by other means must never be rendered. Opt-in, not a denylist."""

    @pytest.mark.asyncio
    async def test_command_not_marked_render_safe_is_refused(self, mock_bot):
        cmd = _make_command(name="advert", keywords=["advert"], reply="sent")
        cmd.render_safe = False
        mgr = _make_manager(mock_bot, {"advert": cmd})
        assert await mgr.render_command_output("advert") is None

    @pytest.mark.asyncio
    async def test_command_marked_render_safe_is_allowed(self, mock_bot):
        cmd = _make_command(name="wx", keywords=["wx"], reply="12C")
        cmd.render_safe = True
        mgr = _make_manager(mock_bot, {"wx": cmd})
        assert await mgr.render_command_output("wx") == "12C"

    @pytest.mark.asyncio
    async def test_a_command_missing_the_attribute_defaults_to_refused(self, mock_bot):
        """A new command must not become renderable by omission."""
        cmd = _make_command(name="brandnew", keywords=["brandnew"], reply="hi")
        del cmd.render_safe
        cmd.mock_add_spec(['name', 'keywords', '_derive_config_section_name',
                           'get_config_value', 'requires_admin_access',
                           'check_cooldown', 'record_execution', 'execute'])
        mgr = _make_manager(mock_bot, {"brandnew": cmd})
        assert await mgr.render_command_output("brandnew") is None

    def test_transmitting_commands_are_not_marked_render_safe(self):
        """Guards the allowlist itself against a careless future edit."""
        from modules.commands.advert_command import AdvertCommand
        from modules.commands.announcements_command import AnnouncementsCommand
        from modules.commands.schedule_command import ScheduleCommand

        for cls in (AdvertCommand, AnnouncementsCommand, ScheduleCommand):
            assert getattr(cls, 'render_safe', False) is False, cls.__name__

    def test_base_command_defaults_to_not_render_safe(self):
        from modules.commands.base_command import BaseCommand

        assert BaseCommand.render_safe is False


@pytest.mark.unit
class TestChunkedResponsesAreCaptured:
    @pytest.mark.asyncio
    async def test_chunked_send_captures_instead_of_transmitting(self, mock_bot):
        from modules.command_manager import CommandManager

        mgr = object.__new__(CommandManager)
        mgr.bot = mock_bot
        mgr.logger = MagicMock()
        mgr.send_dm = AsyncMock()
        mgr.send_channel_messages_chunked = AsyncMock()

        sink = []
        msg = MeshMessage(content="x", channel="#general", is_dm=False, capture_sink=sink)
        ok = await CommandManager.send_response_chunked(mgr, msg, ["one", "two"])

        assert ok is True
        assert sink == ["one", "two"]
        mgr.send_channel_messages_chunked.assert_not_called()
        mgr.send_dm.assert_not_called()


@pytest.mark.unit
class TestScheduledSendFitsTheRfBudget:
    """A {cmd:...} expansion can exceed one message; send_channel_message does not
    split, so an oversized scheduled message would fail at the device."""

    @staticmethod
    def _sched(bot_name="Bot"):
        import configparser

        from modules.scheduler import MessageScheduler

        cfg = configparser.ConfigParser()
        cfg["Bot"] = {"bot_name": bot_name}
        sched = object.__new__(MessageScheduler)
        sched.bot = MagicMock()
        sched.bot.config = cfg
        sched.bot.meshcore = None
        sched.logger = MagicMock()
        return sched

    def test_budget_accounts_for_the_username_prefix(self):
        sched = self._sched(bot_name="LongBotName")
        assert sched._channel_body_budget(None) == 160 - len("LongBotName") - 2

    def test_scoped_sends_get_a_smaller_budget(self):
        sched = self._sched()
        assert sched._channel_body_budget("#sea") < sched._channel_body_budget(None)

    def test_short_text_is_a_single_chunk(self):
        sched = self._sched()
        assert sched._split_to_budget("hello", 100) == ["hello"]

    def test_splits_on_line_boundaries(self):
        sched = self._sched()
        text = "\n".join(["line one", "line two", "line three"])
        chunks = sched._split_to_budget(text, 20)
        assert len(chunks) > 1
        assert all(len(c.encode("utf-8")) <= 20 for c in chunks)

    def test_every_chunk_respects_the_budget(self):
        sched = self._sched()
        chunks = sched._split_to_budget("x" * 500, 60)
        assert all(len(c.encode("utf-8")) <= 60 for c in chunks)
        assert "".join(chunks) == "x" * 500

    def test_multibyte_characters_are_not_split_mid_character(self):
        """Budget is in bytes; a 3-byte character must not be cut in half."""
        sched = self._sched()
        text = "☃" * 40  # 3 bytes each
        chunks = sched._split_to_budget(text, 20)
        assert all(len(c.encode("utf-8")) <= 20 for c in chunks)
        assert "".join(chunks) == text  # nothing lost or corrupted
