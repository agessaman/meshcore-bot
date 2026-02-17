"""Tests for modules.commands.roll_command."""

import re
import pytest

from modules.commands.roll_command import RollCommand
from tests.conftest import command_mock_bot, mock_message


class TestParseRollNotation:
    """Tests for parse_roll_notation()."""

    def test_valid_number(self, command_mock_bot):
        cmd = RollCommand(command_mock_bot)
        assert cmd.parse_roll_notation("50") == 50
        assert cmd.parse_roll_notation("100") == 100
        assert cmd.parse_roll_notation("1000") == 1000

    def test_out_of_range(self, command_mock_bot):
        cmd = RollCommand(command_mock_bot)
        assert cmd.parse_roll_notation("0") is None
        assert cmd.parse_roll_notation("10001") is None

    def test_invalid_input(self, command_mock_bot):
        cmd = RollCommand(command_mock_bot)
        assert cmd.parse_roll_notation("abc") is None


class TestRollCommandMatches:
    """Tests for matches_keyword()."""

    def test_exact_roll_match(self, command_mock_bot):
        cmd = RollCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="roll")) is True

    def test_roll_with_number_match(self, command_mock_bot):
        cmd = RollCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="roll 50")) is True

    def test_roll_invalid_no_match(self, command_mock_bot):
        cmd = RollCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="roll abc")) is False


class TestRollCommandExecute:
    """Tests for execute()."""

    @pytest.mark.asyncio
    async def test_roll_default_1_to_100(self, command_mock_bot):
        command_mock_bot.config.add_section("Roll_Command")
        command_mock_bot.config.set("Roll_Command", "enabled", "true")
        cmd = RollCommand(command_mock_bot)
        msg = mock_message(content="roll", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        # Should be a number between 1 and 100
        nums = re.findall(r'\d+', response)
        assert len(nums) >= 1
        val = int(nums[0])
        assert 1 <= val <= 100

    @pytest.mark.asyncio
    async def test_roll_50(self, command_mock_bot):
        command_mock_bot.config.add_section("Roll_Command")
        command_mock_bot.config.set("Roll_Command", "enabled", "true")
        cmd = RollCommand(command_mock_bot)
        msg = mock_message(content="roll 50", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        nums = re.findall(r'\d+', response)
        assert len(nums) >= 1
        val = int(nums[0])
        assert 1 <= val <= 50
