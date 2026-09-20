#!/usr/bin/env python3
"""Regression: config aliases work across commands that had hardcoded matchers."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.channels_command import ChannelsCommand
from modules.commands.dice_command import DiceCommand
from modules.commands.hacker_command import HackerCommand
from modules.commands.multitest_command import MultitestCommand
from modules.commands.path_command import PathCommand
from modules.commands.prefix_command import PrefixCommand
from modules.commands.roll_command import RollCommand
from modules.commands.test_command import TestCommand as MeshTestCommand
from modules.commands.trace_command import TraceCommand
from tests.conftest import mock_message
from tests.test_command_manager import make_manager
from tests.unit.test_command_path_byte_gating import _base_bot


def _with_aliases(bot, section: str, aliases: str):
    if not bot.config.has_section(section):
        bot.config.add_section(section)
    bot.config.set(section, "enabled", "true")
    bot.config.set(section, "aliases", aliases)
    return bot


@pytest.mark.unit
@pytest.mark.parametrize(
    "factory,section,alias,content",
    [
        (MeshTestCommand, "Test_Command", "path", "!path"),
        (DiceCommand, "Dice_Command", "d", "d d20"),
        (DiceCommand, "Dice_Command", "d", "d"),
        (RollCommand, "Roll_Command", "r", "r"),
        (RollCommand, "Roll_Command", "r", "r 50"),
        (TraceCommand, "Trace_Command", "tr", "tr"),
        (TraceCommand, "Trace_Command", "tr", "tr 01,7a"),
        (PrefixCommand, "Prefix_Command", "pfx", "pfx"),
        (PrefixCommand, "Prefix_Command", "pfx", "pfx free"),
        (MultitestCommand, "Multitest_Command", "mtest", "mtest"),
        (MultitestCommand, "Multitest_Command", "mtest", "mtest long"),
        (ChannelsCommand, "Channels_Command", "ch", "ch"),
        (PathCommand, "Path_Command", "routehex", "routehex"),
    ],
)
def test_config_alias_matches(factory, section, alias, content):
    bot = _with_aliases(_base_bot(), section, alias)
    cmd = factory(bot)
    assert alias in [k.lower() for k in cmd.keywords]
    assert cmd.matches_keyword(mock_message(content=content, is_dm=True)) is True


@pytest.mark.unit
def test_roll_alias_still_rejects_non_numeric_args():
    bot = _with_aliases(_base_bot(), "Roll_Command", "r")
    cmd = RollCommand(bot)
    assert cmd.matches_keyword(mock_message(content="r abc", is_dm=True)) is False


@pytest.mark.unit
def test_channels_alias_does_not_match_as_subcommand():
    bot = _with_aliases(_base_bot(), "Channels_Command", "ch")
    cmd = ChannelsCommand(bot)
    # "stats channels" style: first word is not a channels keyword/alias
    assert cmd.matches_keyword(mock_message(content="stats ch", is_dm=True)) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dice_alias_execute_uses_args():
    bot = _with_aliases(_base_bot(), "Dice_Command", "d")
    cmd = DiceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.roll_dice = Mock(return_value=[4])
    cmd.format_dice_result = Mock(return_value="ok")

    await cmd.execute(mock_message(content="!d d20", is_dm=True))

    cmd.roll_dice.assert_called()
    cmd.send_response.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_roll_alias_execute_parses_max():
    bot = _with_aliases(_base_bot(), "Roll_Command", "r")
    cmd = RollCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.roll_number = Mock(return_value=7)
    cmd.format_roll_result = Mock(return_value="rolled")

    await cmd.execute(mock_message(content="!r 50", is_dm=True))

    cmd.roll_number.assert_called_once_with(50)


@pytest.mark.unit
def test_trace_parse_path_arg_honors_alias():
    bot = _with_aliases(_base_bot(), "Trace_Command", "tr")
    cmd = TraceCommand(bot)
    assert cmd._parse_path_arg("!tr 01,7a") == ["01", "7a"]


@pytest.mark.unit
def test_hacker_config_alias_matches():
    bot = _with_aliases(_base_bot(), "Hacker_Command", "hack")
    # Hacker may use a different enabled key; force on
    bot.config.set("Hacker_Command", "enabled", "true")
    cmd = HackerCommand(bot)
    cmd.enabled = True
    assert cmd.matches_keyword(mock_message(content="hack", is_dm=True)) is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_channels_alias_execute_honors_list_subcommand():
    bot = _with_aliases(_base_bot(), "Channels_Command", "ch")
    cmd = ChannelsCommand(bot)
    cmd._show_all_categories = AsyncMock()
    cmd._show_specific_channel = AsyncMock()
    cmd.send_response = AsyncMock(return_value=True)

    await cmd.execute(mock_message(content="!ch list", is_dm=True))

    cmd._show_all_categories.assert_awaited_once()
    cmd._show_specific_channel.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hacker_alias_execute_routes_inner_command():
    bot = _with_aliases(_base_bot(), "Hacker_Command", "hack")
    bot.config.set("Hacker_Command", "enabled", "true")
    cmd = HackerCommand(bot)
    cmd.enabled = True
    cmd.get_hacker_error = Mock(return_value="denied")
    cmd.send_response = AsyncMock(return_value=True)

    await cmd.execute(mock_message(content="!hack sudo ls", is_dm=True))
    cmd.get_hacker_error.assert_called_once_with("sudo ls")

    cmd.get_hacker_error.reset_mock()
    await cmd.execute(mock_message(content="!sudo ls", is_dm=True))
    cmd.get_hacker_error.assert_called_once_with("sudo ls")


@pytest.mark.unit
def test_test_stems_still_match_when_path_is_aliased():
    bot = _with_aliases(_base_bot(), "Test_Command", "path, p")
    cmd = MeshTestCommand(bot)
    assert cmd.matches_keyword(mock_message(content="test", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="t", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="path", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="p", is_dm=True)) is True


@pytest.mark.unit
def test_disabled_path_still_matches_but_cannot_execute():
    """A disabled command keeps matching; ``can_execute`` is what holds it back.

    The dispatcher (``check_keywords`` / ``execute_commands``) continues past a
    command that cannot execute, which is what lets another command's alias claim
    the same trigger. Gating ``matches_keyword`` on ``enabled`` as well would be a
    second, per-command mechanism for a problem already solved once.
    """
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")
    cmd = PathCommand(bot)
    for content in ("path", "p", "!path"):
        msg = mock_message(content=content, is_dm=True)
        assert cmd.matches_keyword(msg) is True
        assert cmd.can_execute(msg) is False


@pytest.mark.unit
def test_p_shortcut_disabled_does_not_leak_between_instances():
    """``enable_p_shortcut = false`` must really drop ``p``.

    Appending to the inherited class list would leave ``p`` on
    ``PathCommand.keywords`` for every instance built afterwards, so a reload with
    the shortcut turned off would still answer ``p``.
    """
    on = _base_bot()
    on.config.add_section("Path_Command")
    on.config.set("Path_Command", "enable_p_shortcut", "true")
    assert "p" in PathCommand(on).keywords

    off = _base_bot()
    off.config.add_section("Path_Command")
    off.config.set("Path_Command", "enable_p_shortcut", "false")
    cmd = PathCommand(off)
    assert "p" not in cmd.keywords
    assert "p" not in PathCommand.keywords
    assert cmd.matches_keyword(mock_message(content="p", is_dm=True)) is False


@pytest.mark.unit
def test_test_matching_tolerates_control_characters():
    """Mesh clients embed stray control bytes; a test request must still match.

    A test is exactly the message someone sends when their link is marginal, so
    dropping this tolerance would fail the requests that most need answering.
    """
    cmd = MeshTestCommand(_base_bot())
    for content in ("test\x01", "\x02test", "test\x00 hello", "test\x7f", "test\nhi"):
        assert cmd.matches_keyword(mock_message(content=content, is_dm=True)) is True


@pytest.mark.unit
def test_non_matching_message_keeps_its_original_content():
    """Test runs early in the command scan; it must not reformat other commands' text."""
    cmd = MeshTestCommand(_base_bot())
    body = "schedule add 09:00  line one\nline two"
    msg = mock_message(content=body, is_dm=True)
    assert cmd.matches_keyword(msg) is False
    assert msg.content == body


@pytest.mark.unit
def test_split_trigger_honours_configured_command_prefix():
    """``!`` is only stripped in legacy no-prefix mode, not for every bot."""
    bot = _with_aliases(_base_bot(), "Test_Command", "path")
    bot.config.set("Bot", "command_prefix", "/")
    cmd = MeshTestCommand(bot)
    assert cmd.split_trigger_and_args("/test hello") == ("test", "hello")
    # "!" is not this bot's prefix, so it is part of the word and matches nothing
    assert cmd.split_trigger_and_args("!test hello") == (None, "!test hello")


@pytest.mark.unit
def test_split_trigger_matches_multi_word_keywords():
    cmd = HackerCommand(_base_bot())
    assert cmd.split_trigger_and_args("ps aux | grep bot") == ("ps aux", "| grep bot")
    # Longest trigger wins over a shorter stem that also matches
    assert cmd.split_trigger_and_args("rm -rf /") == ("rm -rf", "/")


@pytest.mark.unit
def test_test_alias_format_response_uses_mesh_path_not_unknown():
    """An alias trigger must not be mistaken for path hex in ``{path}``."""
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "aliases", "path, p")
    bot.config.set("Test_Command", "response_format", "ack {path}")

    cmd = MeshTestCommand(bot)
    msg = mock_message(
        content="!path",
        is_dm=True,
        path="aa,bb (2 hops)",
        snr=5.5,
        rssi=-80,
        routing_info={"path_length": 2, "path_nodes": ["aa", "bb"]},
    )
    out = cmd.format_response(msg, cmd.get_response_format())
    assert "Unknown" not in out
    assert "aa" in out and "bb" in out


@pytest.mark.unit
def test_test_alias_phrase_is_not_the_trigger_word():
    bot = _with_aliases(_base_bot(), "Test_Command", "path, p")
    cmd = MeshTestCommand(bot)
    msg = mock_message(content="path hello there", is_dm=True, sender_id="Sam")
    out = cmd.format_response(msg, "ack{phrase_part}")
    assert out == "ack: hello there"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_disabled_path_does_not_execute_when_test_owns_alias():
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "aliases", "path, p")
    bot.config.set("Test_Command", "response_format", "ack-from-test")

    path_cmd = PathCommand(bot)
    path_cmd.execute = AsyncMock(return_value=True)
    test_cmd = MeshTestCommand(bot)
    manager = make_manager(bot, commands={"path": path_cmd, "test": test_cmd})
    manager.send_response = AsyncMock(return_value=True)

    msg = mock_message(content="!path", is_dm=True, path="aa,bb (2 hops)")
    matches = manager.check_keywords(msg)
    assert matches == [("test", "ack-from-test")]

    await manager.execute_commands(msg)
    path_cmd.execute.assert_not_called()


@pytest.mark.unit
def test_check_keywords_prefers_test_alias_when_path_disabled():
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "aliases", "path, p")
    bot.config.set("Test_Command", "response_format", "ack-from-test")

    path_cmd = PathCommand(bot)
    test_cmd = MeshTestCommand(bot)
    manager = make_manager(bot, commands={"path": path_cmd, "test": test_cmd})

    matches = manager.check_keywords(mock_message(content="!path", is_dm=True))
    assert matches == [("test", "ack-from-test")]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execute_commands_skips_disabled_and_runs_next():
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")

    path_cmd = PathCommand(bot)
    path_cmd.should_execute = Mock(return_value=True)
    path_cmd.get_response_format = Mock(return_value=None)
    path_cmd.execute = AsyncMock(return_value=True)

    other = MagicMock()
    other.is_channel_allowed = Mock(return_value=True)
    other.should_execute = Mock(return_value=True)
    other.get_response_format = Mock(return_value=None)
    other.can_execute_now = Mock(return_value=True)
    other.requires_internet = False
    other.cooldown_seconds = 0
    other.execute = AsyncMock(return_value=True)
    other._record_execution = Mock()
    other.last_response = None

    manager = make_manager(bot, commands={"path": path_cmd, "other": other})
    manager.send_response = AsyncMock(return_value=True)

    await manager.execute_commands(mock_message(content="!path", is_dm=True))

    path_cmd.execute.assert_not_called()
    other.execute.assert_awaited_once()
