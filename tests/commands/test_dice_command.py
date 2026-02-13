"""Tests for modules.commands.dice_command."""

import pytest
from unittest.mock import patch

from modules.commands.dice_command import DiceCommand
from tests.conftest import command_mock_bot, mock_message


class TestParseDiceNotation:
    """Tests for parse_dice_notation()."""

    def test_d20(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        sides, count, is_decade = cmd.parse_dice_notation("d20")
        assert sides == 20
        assert count == 1
        assert is_decade is False

    def test_2d6(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        sides, count, is_decade = cmd.parse_dice_notation("2d6")
        assert sides == 6
        assert count == 2
        assert is_decade is False

    def test_decade(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        sides, count, is_decade = cmd.parse_dice_notation("decade")
        assert sides == 10
        assert count == 1
        assert is_decade is True

    def test_direct_number_20(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        sides, count, is_decade = cmd.parse_dice_notation("20")
        assert sides == 20
        assert count == 1

    def test_invalid_d7(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        sides, count, is_decade = cmd.parse_dice_notation("d7")
        assert sides is None
        assert count is None


class TestParseMixedDice:
    """Tests for parse_mixed_dice()."""

    def test_d10_d6(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        result = cmd.parse_mixed_dice("d10 d6")
        assert len(result) == 2
        assert result[0] == (10, 1, False)
        assert result[1] == (6, 1, False)

    def test_2d6_d20(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        result = cmd.parse_mixed_dice("2d6 d20")
        assert len(result) == 2
        assert result[0] == (6, 2, False)
        assert result[1] == (20, 1, False)


class TestRollDice:
    """Tests for roll_dice()."""

    def test_decade_values_in_range(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        for _ in range(20):
            results = cmd.roll_dice(10, 1, is_decade=True)
            assert len(results) == 1
            assert results[0] in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90)

    def test_standard_dice_range(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        results = cmd.roll_dice(6, 10)
        assert len(results) == 10
        for r in results:
            assert 1 <= r <= 6


class TestFormatDiceResult:
    """Tests for format_dice_result()."""

    def test_single_die(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        result = cmd.format_dice_result(6, 1, [4])
        assert "4" in result and "6" in result

    def test_decade_format(self, command_mock_bot):
        cmd = DiceCommand(command_mock_bot)
        result = cmd.format_dice_result(10, 1, [50], is_decade=True)
        assert "50" in result
        assert "decade" in result.lower()


class TestDiceExecute:
    """Tests for execute()."""

    @pytest.mark.asyncio
    async def test_dice_defaults_to_d6(self, command_mock_bot):
        command_mock_bot.config.add_section("Dice_Command")
        command_mock_bot.config.set("Dice_Command", "enabled", "true")
        cmd = DiceCommand(command_mock_bot)
        msg = mock_message(content="dice", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert "6" in response or "d6" in response or "🎲" in response

    @pytest.mark.asyncio
    async def test_dice_invalid_returns_error(self, command_mock_bot):
        command_mock_bot.config.add_section("Dice_Command")
        command_mock_bot.config.set("Dice_Command", "enabled", "true")
        cmd = DiceCommand(command_mock_bot)
        msg = mock_message(content="dice invalid", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        response = call_args[0][1]
        assert "invalid" in response.lower() or "d4" in response or "d6" in response
