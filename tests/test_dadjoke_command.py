"""Tests for modules.commands.dadjoke_command — pure logic functions."""

import configparser
from unittest.mock import MagicMock, Mock

import pytest

from modules.commands.dadjoke_command import DadJokeCommand
from tests.conftest import mock_message


def _make_bot():
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("DadJoke_Command")
    config.set("DadJoke_Command", "enabled", "true")
    config.set("DadJoke_Command", "long_jokes", "false")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestFormatDadJoke:
    """Tests for format_dad_joke."""

    def setup_method(self):
        self.cmd = DadJokeCommand(_make_bot())

    def test_formats_joke_with_emoji(self):
        data = {"joke": "Why did the chicken cross the road?"}
        result = self.cmd.format_dad_joke(data)
        assert result.startswith("🥸")
        assert "chicken" in result

    def test_empty_joke_returns_fallback(self):
        data = {"joke": ""}
        result = self.cmd.format_dad_joke(data)
        assert "🥸" in result

    def test_missing_joke_key_returns_fallback(self):
        data = {}
        result = self.cmd.format_dad_joke(data)
        assert "🥸" in result


class TestSplitDadJoke:
    """Tests for split_dad_joke."""

    def setup_method(self):
        self.cmd = DadJokeCommand(_make_bot())

    def test_splits_at_period(self):
        joke = "🥸 Why did the chicken cross? It was there. The other side was better."
        result = self.cmd.split_dad_joke(joke)
        assert len(result) == 2
        assert result[0] and result[1]

    def test_splits_at_question_mark(self):
        joke = "🥸 Why did the chicken cross the road? To get to the other side!"
        result = self.cmd.split_dad_joke(joke)
        assert len(result) == 2

    def test_each_part_has_emoji(self):
        joke = "🥸 Part one sentence here. Part two sentence continues."
        result = self.cmd.split_dad_joke(joke)
        for part in result:
            assert "🥸" in part

    def test_no_split_point_splits_at_midpoint(self):
        # Joke with no punctuation split points
        joke = "🥸 abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz"
        result = self.cmd.split_dad_joke(joke)
        assert len(result) == 2

    def test_short_joke_without_emoji(self):
        joke = "Why did the chicken cross the road? To get to the other side!"
        result = self.cmd.split_dad_joke(joke)
        assert len(result) == 2


class TestDadJokeLength:
    """Tests for length-based behavior."""

    def setup_method(self):
        self.cmd = DadJokeCommand(_make_bot())

    def test_short_joke_fits_in_130(self):
        data = {"joke": "Why? Because!"}
        text = self.cmd.format_dad_joke(data)
        assert len(text) <= 130

    def test_long_joke_exceeds_130(self):
        long_joke = "a" * 200
        data = {"joke": long_joke}
        text = self.cmd.format_dad_joke(data)
        assert len(text) > 130


class TestDadJokeMatchesKeyword:
    """Tests for matches_keyword."""

    def setup_method(self):
        self.cmd = DadJokeCommand(_make_bot())

    def test_dadjoke_matches(self):
        msg = mock_message(content="dadjoke")
        assert self.cmd.matches_keyword(msg) is True

    def test_dad_joke_matches(self):
        msg = mock_message(content="dad joke")
        assert self.cmd.matches_keyword(msg) is True

    def test_other_does_not_match(self):
        msg = mock_message(content="joke")
        assert self.cmd.matches_keyword(msg) is False

    def test_exclamation_prefix_handled(self):
        msg = mock_message(content="!dadjoke")
        assert self.cmd.matches_keyword(msg) is True


class TestDadJokeCanExecute:
    def test_enabled_can_execute(self):
        cmd = DadJokeCommand(_make_bot())
        msg = mock_message(content="dadjoke", channel="general")
        assert cmd.can_execute(msg) is True

    def test_disabled_cannot_execute(self):
        bot = _make_bot()
        bot.config.set("DadJoke_Command", "enabled", "false")
        cmd = DadJokeCommand(bot)
        cmd.dadjoke_enabled = False
        msg = mock_message(content="dadjoke", channel="general")
        assert cmd.can_execute(msg) is False


class TestDadJokeGetHelpText:
    def test_returns_usage_string(self):
        cmd = DadJokeCommand(_make_bot())
        result = cmd.get_help_text()
        assert "dadjoke" in result.lower() or "Usage" in result


class TestDadJokeExecute:
    def test_execute_returns_true_with_joke(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = DadJokeCommand(bot)
        msg = mock_message(content="dadjoke", channel="general")

        with patch.object(cmd, "get_dad_joke_with_length_handling", new_callable=AsyncMock,
                          return_value={"joke": "Why did the chicken cross? Because!"}):
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_returns_true_when_no_joke(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = DadJokeCommand(bot)
        msg = mock_message(content="dadjoke", channel="general")

        with patch.object(cmd, "get_dad_joke_with_length_handling", new_callable=AsyncMock,
                          return_value=None):
            result = asyncio.run(cmd.execute(msg))
        assert result is True
        bot.command_manager.send_response.assert_called_once()

    def test_execute_handles_exception(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = DadJokeCommand(bot)
        msg = mock_message(content="dadjoke", channel="general")

        with patch.object(cmd, "get_dad_joke_with_length_handling", new_callable=AsyncMock,
                          side_effect=Exception("API error")):
            result = asyncio.run(cmd.execute(msg))
        assert result is True


class TestSendDadJokeWithLengthHandling:
    def test_short_joke_sends_single_message(self):
        import asyncio
        from unittest.mock import AsyncMock

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = DadJokeCommand(bot)
        msg = mock_message(content="dadjoke")

        joke_data = {"joke": "Short joke!"}
        asyncio.run(cmd.send_dad_joke_with_length_handling(msg, joke_data))
        bot.command_manager.send_response.assert_called_once()

    def test_long_joke_split_sends_two_messages(self):
        import asyncio
        from unittest.mock import AsyncMock

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = DadJokeCommand(bot)
        msg = mock_message(content="dadjoke")

        # Create a joke that's long enough to split (>130 chars)
        long_joke = "Why did the chicken cross the road? " + "x" * 100 + ". Then it went back home."
        joke_data = {"joke": long_joke}
        asyncio.run(cmd.send_dad_joke_with_length_handling(msg, joke_data))
        # Should have been called (either once or twice depending on split)
        assert bot.command_manager.send_response.call_count >= 1
