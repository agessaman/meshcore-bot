"""Characterization: how command executions and responses are accounted for.

Pins, with an advancing clock, when ``execute_commands`` records a cooldown,
what a second ``record_execution`` inside ``execute`` does to the boundary,
which stats rows are written for each outcome, and that local-plugin commands
with zero-argument cooldown methods keep working. Some of this is defective
today (``_last_response`` is never reset, so a silent command after any reply
is recorded as having responded); it is recorded as-is so the refactor cannot
change it by accident. The accounting fix will update these expectations.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.command_manager import CommandManager
from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Probe(BaseCommand):
    name = "probe"
    keywords = ["probe"]
    cooldown_seconds = 30

    def __init__(self, bot, *, reply: bool = True, rerecord_after: float | None = None, clock=None):
        super().__init__(bot)
        self.reply = reply
        self.rerecord_after = rerecord_after
        self.clock = clock

    async def execute(self, message):
        if self.rerecord_after is not None:
            self.clock.advance(self.rerecord_after)
            self.record_execution(message.sender_id)
        if self.reply:
            await self.send_response(message, "probe reply")
        return True


class _LegacyPluginCommand(BaseCommand):
    """A local plugin written against the old zero-argument cooldown API."""

    name = "legacy"
    keywords = ["legacy"]
    cooldown_seconds = 30

    def __init__(self, bot):
        super().__init__(bot)
        self.recorded = 0

    def _record_execution(self):  # type: ignore[override]
        self.recorded += 1
        self._last_execution_time = __import__("time").time()

    def get_remaining_cooldown(self):  # type: ignore[override]
        return 7

    async def execute(self, message):
        return True


@pytest.fixture
def harness():
    clock = _Clock()
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.set("Bot", "bot_name", "AcctBot")
    cfg.add_section("Channels")
    cfg.set("Channels", "monitor_channels", "general")
    cfg.set("Channels", "respond_to_dms", "true")
    bot = Mock()
    bot.config = cfg
    bot.logger = Mock()
    bot.bot_root = Path("/tmp")
    bot._local_root = None
    bot.translator = Mock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.meshcore = None
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.web_viewer_integration = None
    bot.transmission_tracker = None
    with patch("modules.command_manager.PluginLoader") as loader_cls:
        loader = Mock()
        loader.load_all_plugins = Mock(return_value={})
        loader_cls.return_value = loader
        cm = CommandManager(bot)
    bot.command_manager = cm
    sent: list[str] = []

    async def _send_response(message, content, *args, **kwargs):
        cm._last_response = content
        sent.append(content)
        return True

    cm.send_response = AsyncMock(side_effect=_send_response)
    stats = Mock()
    cm.commands = {"stats": stats}
    with patch("time.time", clock), patch("modules.command_manager.asyncio.sleep", AsyncMock()):
        yield cm, bot, clock, stats, sent


def _msg(text: str = "probe", sender: str = "Ann") -> MeshMessage:
    return MeshMessage(content=text, sender_id=sender, channel="general", is_dm=False)


def _stats_rows(stats):
    return [(c.args[1], c.args[2]) for c in stats.record_command.call_args_list]


async def test_cooldown_is_recorded_before_execute(harness):
    cm, bot, clock, stats, sent = harness
    probe = _Probe(bot, clock=clock)
    cm.commands["probe"] = probe
    start = clock.now
    await cm.execute_commands(_msg())
    assert probe._user_cooldowns["Ann"] == start
    assert sent == ["probe reply"]
    assert _stats_rows(stats) == [("probe", True)]


async def test_second_record_inside_execute_moves_the_boundary(harness):
    cm, bot, clock, stats, sent = harness
    probe = _Probe(bot, clock=clock, rerecord_after=5.0)
    cm.commands["probe"] = probe
    start = clock.now
    await cm.execute_commands(_msg())
    assert probe._user_cooldowns["Ann"] == start + 5.0
    clock.advance(26)  # 31 s after the manager's record, 26 s after the command's
    allowed, remaining = probe.check_cooldown("Ann")
    assert (allowed, round(remaining)) == (False, 4)


async def test_cooldown_rejection_replies_and_records_stats(harness):
    cm, bot, clock, stats, sent = harness
    probe = _Probe(bot, clock=clock)
    cm.commands["probe"] = probe
    await cm.execute_commands(_msg())
    clock.advance(10)
    await cm.execute_commands(_msg())
    assert sent[-1] == "errors.cooldown"
    assert _stats_rows(stats) == [("probe", True), ("probe", True)]


async def test_silent_command_after_any_reply_counts_as_responded(harness):
    """Defect pinned on purpose: _last_response is never reset."""
    cm, bot, clock, stats, sent = harness
    cm.commands["probe"] = _Probe(bot, clock=clock)
    cm.commands["quiet"] = quiet = _Probe(bot, reply=False, clock=clock)
    quiet.name = "quiet"
    quiet.keywords = ["quiet"]
    await cm.execute_commands(_msg("probe"))
    await cm.execute_commands(_msg("quiet", sender="Bob"))
    assert _stats_rows(stats) == [("probe", True), ("quiet", True)]


async def test_silent_command_with_no_prior_reply_counts_as_silent(harness):
    cm, bot, clock, stats, sent = harness
    cm.commands["quiet"] = quiet = _Probe(bot, reply=False, clock=clock)
    quiet.name = "quiet"
    quiet.keywords = ["quiet"]
    await cm.execute_commands(_msg("quiet"))
    assert _stats_rows(stats) == [("quiet", False)]


async def test_zero_argument_plugin_cooldown_methods_still_work(harness):
    cm, bot, clock, stats, sent = harness
    legacy = _LegacyPluginCommand(bot)
    cm.commands["legacy"] = legacy
    await cm.execute_commands(_msg("legacy"))
    assert legacy.recorded == 1
    clock.advance(1)
    with patch.object(legacy, "can_execute_now", return_value=False):
        await cm.execute_commands(_msg("legacy"))
    assert sent[-1] == "errors.cooldown"


async def test_keyword_format_commands_are_skipped_by_execute_commands(harness):
    cm, bot, clock, stats, sent = harness
    probe = _Probe(bot, clock=clock)
    probe.get_response_format = lambda: "fmt {sender}"
    cm.commands["probe"] = probe
    await cm.execute_commands(_msg())
    assert sent == []
    assert "Ann" not in probe._user_cooldowns
    assert _stats_rows(stats) == []
