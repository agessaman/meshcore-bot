"""Tests for modules.commands.ia_command."""

from unittest.mock import Mock, patch
import requests
import pytest

from modules.commands.ia_command import IaCommand
from tests.conftest import command_mock_bot, mock_message


class TestIaCommand:
    """Tests for IaCommand."""

    def _enable_ia(self, bot):
        if not bot.config.has_section("Ia_Command"):
            bot.config.add_section("Ia_Command")
        bot.config.set("Ia_Command", "enabled", "true")

    def test_can_execute_when_enabled(self, command_mock_bot):
        self._enable_ia(command_mock_bot)
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="ia hello", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        if not command_mock_bot.config.has_section("Ia_Command"):
            command_mock_bot.config.add_section("Ia_Command")
        command_mock_bot.config.set("Ia_Command", "enabled", "false")
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="ia hello", is_dm=True)
        assert cmd.can_execute(msg) is False

    def test_matches_keyword_no_prefix(self, command_mock_bot):
        """With no command_prefix configured, bare 'ia <text>' should match."""
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="ia hello", is_dm=True)
        assert cmd.matches_keyword(msg) is True

    def test_matches_keyword_with_configured_prefix(self, command_mock_bot):
        """With command_prefix='/', '!ia <text>' should NOT match but '/ia <text>' should."""
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "/")
        cmd = IaCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="/ia hello", is_dm=True)) is True
        assert cmd.matches_keyword(mock_message(content="!ia hello", is_dm=True)) is False

    @pytest.mark.asyncio
    async def test_execute_without_prompt_returns_usage_no_prefix(self, command_mock_bot):
        """Usage string must use the configured command prefix."""
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="ia", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        assert command_mock_bot.command_manager.send_response.call_args[0][1] == "Usage: ia <question>"

    @pytest.mark.asyncio
    async def test_execute_without_prompt_returns_usage_with_prefix(self, command_mock_bot):
        """Usage string reflects a non-slash configured prefix."""
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "!")
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="!ia", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        assert command_mock_bot.command_manager.send_response.call_args[0][1] == "Usage: !ia <question>"

    @pytest.mark.asyncio
    async def test_execute_success_calls_llama_endpoint(self, command_mock_bot):
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Ia_Command", "endpoint", "http://127.0.0.1:8080/v1/chat/completions")
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="ia what is mesh?", is_dm=True)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "<think>chain</think><thinking>chain2</thinking>Mesh is a decentralized radio network."
                }
            }]
        }

        with patch("modules.commands.ia_command.requests.post", return_value=mock_response) as post_mock:
            result = await cmd.execute(msg)

        assert result is True
        post_mock.assert_called_once()
        sent_text = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "Mesh is a decentralized radio network." in sent_text
        assert "<think>" not in sent_text
        assert "<thinking>" not in sent_text

    @pytest.mark.asyncio
    async def test_execute_handles_connection_errors(self, command_mock_bot):
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = IaCommand(command_mock_bot)
        msg = mock_message(content="ia hello", is_dm=True)

        with patch("modules.commands.ia_command.requests.post", side_effect=requests.RequestException("boom")):
            result = await cmd.execute(msg)

        assert result is True
        sent_text = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "IA unavailable" in sent_text

    def test_get_help_text_uses_configured_prefix(self, command_mock_bot):
        """get_help_text() must reflect the configured command prefix, not a hardcoded one."""
        self._enable_ia(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "!")
        cmd = IaCommand(command_mock_bot)
        help_text = cmd.get_help_text()
        assert "!ia" in help_text
        assert "/ia" not in help_text
