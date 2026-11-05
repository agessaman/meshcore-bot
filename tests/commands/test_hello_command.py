"""Tests for modules.commands.hello_command."""

from unittest.mock import patch

import pytest

from modules.commands.hello_command import HelloCommand
from tests.conftest import mock_message


class TestHelloCommand:
    """Tests for HelloCommand."""

    def test_is_emoji_only_message_vulcan_salute(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("🖖") is True

    def test_is_emoji_only_message_with_whitespace(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("  🖖  ") is True

    def test_is_emoji_only_message_text_returns_false(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("hello") is False

    def test_is_emoji_only_message_empty_returns_false(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("") is False

    @patch("datetime.datetime")
    def test_get_random_greeting_deterministic_with_mocked_time(self, mock_datetime, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        mock_now = mock_datetime.now.return_value
        mock_now.hour = 10  # morning
        cmd = HelloCommand(command_mock_bot)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = cmd.get_random_greeting()
        assert isinstance(result, str) and len(result) > 0

    def test_get_emoji_response_vulcan(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = cmd.get_emoji_response("🖖", "TestBot")
        assert "🖖" in result or "TestBot" in result

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        msg = mock_message(content="hello", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "false")
        cmd = HelloCommand(command_mock_bot)
        msg = mock_message(content="hello", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_text_greeting_sends_response(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        msg = mock_message(content="hello", is_dm=True)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert isinstance(response, str) and len(response) > 0


class TestHelloCommandIncludeSender:
    """Tests for the Hello_Command include_sender setting."""

    @staticmethod
    def _command(command_mock_bot, include_sender):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        command_mock_bot.config.set("Hello_Command", "include_sender", str(include_sender).lower())
        return HelloCommand(command_mock_bot)

    def test_mention_empty_when_setting_off(self, command_mock_bot):
        cmd = self._command(command_mock_bot, False)
        assert cmd.get_sender_mention(mock_message(sender_id="Bob")) == ""

    def test_mention_built_for_channel_sender(self, command_mock_bot):
        cmd = self._command(command_mock_bot, True)
        assert cmd.get_sender_mention(mock_message(sender_id="Bob")) == "@[Bob]"

    def test_mention_empty_for_dm(self, command_mock_bot):
        """A DM has one recipient; naming them spends airtime to say nothing."""
        cmd = self._command(command_mock_bot, True)
        assert cmd.get_sender_mention(mock_message(sender_id="Bob", is_dm=True)) == ""

    def test_mention_empty_for_unnamed_channel_sender(self, command_mock_bot):
        """message_handler uses this literal when the packet has no name prefix."""
        cmd = self._command(command_mock_bot, True)
        assert cmd.get_sender_mention(mock_message(sender_id="Channel User")) == ""

    def test_mention_empty_when_sender_id_missing(self, command_mock_bot):
        cmd = self._command(command_mock_bot, True)
        assert cmd.get_sender_mention(mock_message(sender_id=None)) == ""

    def test_mention_strips_brackets_that_would_forge_a_second_mention(self, command_mock_bot):
        """Channel names come off the wire unsanitized (handle_channel_message)."""
        cmd = self._command(command_mock_bot, True)
        mention = cmd.get_sender_mention(mock_message(sender_id="Bob] hi @[Alice"))
        assert mention == "@[Bob hi @Alice]"
        assert mention.count("[") == 1 and mention.count("]") == 1

    def test_mention_strips_control_characters(self, command_mock_bot):
        cmd = self._command(command_mock_bot, True)
        assert cmd.get_sender_mention(mock_message(sender_id="Bo\nb\x00")) == "@[Bob]"

    def test_mention_strips_braces_that_would_break_formatting(self, command_mock_bot):
        """The mention is embedded before .format(bot_name=...) runs."""
        cmd = self._command(command_mock_bot, True)
        assert cmd.get_sender_mention(mock_message(sender_id="Bo{b}")) == "@[Bob]"

    def test_greeting_uses_mention_in_place_of_descriptor(self, command_mock_bot):
        cmd = self._command(command_mock_bot, True)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            with_mention = cmd.get_random_greeting("@[Bob]")
            without = cmd.get_random_greeting()
        assert "@[Bob]" in with_mention
        # The name takes the descriptor's slot rather than riding in front of it.
        assert not with_mention.startswith("@[Bob]")
        assert with_mention.split()[0] == without.split()[0]

    def test_emoji_response_prefixes_mention(self, command_mock_bot):
        """Canned emoji lines have no descriptor slot, so the mention leads."""
        cmd = self._command(command_mock_bot, True)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = cmd.get_emoji_response("🖖", "TestBot", "@[Bob]")
        assert result.startswith("@[Bob] ")

    @pytest.mark.asyncio
    async def test_execute_names_channel_sender(self, command_mock_bot):
        cmd = self._command(command_mock_bot, True)
        msg = mock_message(content="hello", sender_id="Bob")
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            assert await cmd.execute(msg) is True
        response = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "@[Bob]" in response

    @pytest.mark.asyncio
    async def test_execute_drops_mention_that_would_overflow_the_budget(self, command_mock_bot):
        """Nothing downstream truncates, so an over-budget reply is cut on air."""
        cmd = self._command(command_mock_bot, True)
        msg = mock_message(content="hello", sender_id="B" * 60)
        with patch.object(cmd, "get_max_message_length", return_value=60):
            with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
                assert await cmd.execute(msg) is True
        response = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "@[" not in response
        assert len(response.encode("utf-8")) <= 60
