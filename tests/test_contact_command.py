"""Tests for modules.commands.contact_command."""

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.commands.contact_command import ContactCommand
from tests.conftest import mock_message

PUBKEY = "f5d2b56d19b24412756933e917d4632e088cdd5daeadc9002feca73bf5d2b56d"


def _make_bot(self_info=None):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.set("Bot", "respond_to_mentions", "also")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Contact_Command")
    config.set("Contact_Command", "enabled", "true")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.meshcore = MagicMock()
    bot.meshcore.self_info = self_info
    return bot


def _make_command(self_info=None):
    cmd = ContactCommand(_make_bot(self_info))
    cmd.send_response = AsyncMock(return_value=True)
    return cmd


class TestMatching:
    def test_matches_bare_keyword(self):
        cmd = _make_command()
        assert cmd.matches_keyword(mock_message("contact")) is True

    def test_does_not_match_with_arguments(self):
        cmd = _make_command()
        assert cmd.matches_keyword(mock_message("contact me")) is False

    def test_matches_configured_alias(self):
        bot = _make_bot()
        bot.config.set("Contact_Command", "aliases", "card")
        cmd = ContactCommand(bot)
        assert cmd.matches_keyword(mock_message("card")) is True

    def test_non_match_leaves_message_content_untouched(self):
        # Regression guard for #267: the keyword scan must not rewrite
        # overheard traffic when this command does not match.
        cmd = _make_command()
        message = mock_message("@[TestBot] wx 98101")
        assert cmd.matches_keyword(message) is False
        assert message.content == "@[TestBot] wx 98101"


class TestExecute:
    def test_sends_contact_card(self):
        cmd = _make_command({"public_key": PUBKEY, "name": "TestBot"})
        assert asyncio.run(cmd.execute(mock_message("contact"))) is True
        cmd.send_response.assert_awaited_once()
        assert cmd.send_response.await_args[0][1] == f"<{PUBKEY}:1:TestBot>"

    def test_reads_self_info_object(self):
        self_info = MagicMock(spec=["public_key", "name"])
        self_info.public_key = PUBKEY.upper()
        self_info.name = "TestBot"
        cmd = _make_command(self_info)
        assert asyncio.run(cmd.execute(mock_message("contact"))) is True
        assert cmd.send_response.await_args[0][1] == f"<{PUBKEY}:1:TestBot>"

    def test_missing_self_info_reports_unavailable(self):
        cmd = _make_command(None)
        assert asyncio.run(cmd.execute(mock_message("contact"))) is True
        assert cmd.send_response.await_args[0][1] == "commands.contact.unavailable"

    def test_malformed_public_key_reports_unavailable(self):
        cmd = _make_command({"public_key": "nothex", "name": "TestBot"})
        asyncio.run(cmd.execute(mock_message("contact")))
        assert cmd.send_response.await_args[0][1] == "commands.contact.unavailable"

    def test_missing_name_reports_unavailable(self):
        cmd = _make_command({"public_key": PUBKEY})
        asyncio.run(cmd.execute(mock_message("contact")))
        assert cmd.send_response.await_args[0][1] == "commands.contact.unavailable"


class TestEnabledFlag:
    def test_disabled_blocks_execution(self):
        bot = _make_bot({"public_key": PUBKEY, "name": "TestBot"})
        bot.config.set("Contact_Command", "enabled", "false")
        cmd = ContactCommand(bot)
        assert cmd.can_execute(mock_message("contact")) is False
