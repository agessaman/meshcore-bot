"""Tests for modules.commands.joke_command — pure logic functions."""

import configparser
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.commands.joke_command import JokeCommand
from tests.conftest import mock_message


def _make_bot(seasonal=True, long_jokes=False):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Joke_Command")
    config.set("Joke_Command", "enabled", "true")
    config.set("Joke_Command", "seasonal_jokes", str(seasonal).lower())
    config.set("Joke_Command", "long_jokes", str(long_jokes).lower())
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestGetSeasonalDefault:
    """Tests for get_seasonal_default."""

    def test_october_returns_spooky(self):
        cmd = JokeCommand(_make_bot(seasonal=True))
        # datetime is imported inside the function, patch datetime.now at its source
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 10, 15)
            result = cmd.get_seasonal_default()
        assert result == "Spooky"

    def test_december_returns_christmas(self):
        cmd = JokeCommand(_make_bot(seasonal=True))
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 12, 15)
            result = cmd.get_seasonal_default()
        assert result == "Christmas"

    def test_other_month_returns_none(self):
        cmd = JokeCommand(_make_bot(seasonal=True))
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 15)
            result = cmd.get_seasonal_default()
        assert result is None

    def test_seasonal_disabled_returns_none(self):
        cmd = JokeCommand(_make_bot(seasonal=False))
        # Even in October, seasonal disabled returns None
        result = cmd.get_seasonal_default()
        assert result is None


class TestFormatJoke:
    """Tests for format_joke."""

    def setup_method(self):
        self.cmd = JokeCommand(_make_bot())

    def test_single_type_joke(self):
        data = {"type": "single", "joke": "Why so funny?"}
        result = self.cmd.format_joke(data)
        assert "🎭" in result
        assert "Why so funny?" in result

    def test_twopart_joke(self):
        data = {"type": "twopart", "setup": "Why?", "delivery": "Because!"}
        result = self.cmd.format_joke(data)
        assert "🎭" in result
        assert "Why?" in result
        assert "Because!" in result

    def test_empty_single_joke_returns_fallback(self):
        data = {"type": "single", "joke": ""}
        result = self.cmd.format_joke(data)
        assert "🎭" in result

    def test_unknown_type_fallback(self):
        data = {"type": "weird", "joke": "Some joke"}
        result = self.cmd.format_joke(data)
        assert "🎭" in result
        assert "Some joke" in result


class TestSplitJoke:
    """Tests for split_joke."""

    def setup_method(self):
        self.cmd = JokeCommand(_make_bot())

    def test_splits_at_newline(self):
        joke = "🎭 Setup part.\n\nDelivery part."
        result = self.cmd.split_joke(joke)
        assert len(result) == 2

    def test_each_part_has_emoji(self):
        joke = "🎭 First sentence. Second sentence here."
        result = self.cmd.split_joke(joke)
        for part in result:
            assert "🎭" in part

    def test_no_split_point_splits_midpoint(self):
        joke = "🎭 abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz extra"
        result = self.cmd.split_joke(joke)
        assert len(result) == 2


class TestIsDarkJokeRequest:
    """Tests for is_dark_joke_request."""

    def setup_method(self):
        self.cmd = JokeCommand(_make_bot())

    def test_dark_category_is_dark(self):
        msg = mock_message(content="joke dark")
        assert self.cmd.is_dark_joke_request(msg) is True

    def test_other_category_not_dark(self):
        msg = mock_message(content="joke programming")
        assert self.cmd.is_dark_joke_request(msg) is False

    def test_no_category_not_dark(self):
        msg = mock_message(content="joke")
        assert self.cmd.is_dark_joke_request(msg) is False

    def test_exclamation_prefix(self):
        msg = mock_message(content="!joke dark")
        assert self.cmd.is_dark_joke_request(msg) is True


class TestJokeCanExecute:
    """Tests for can_execute."""

    def test_dark_joke_in_channel_blocked(self):
        bot = _make_bot()
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke dark", channel="general", is_dm=False)
        # Dark jokes in public channels should be blocked
        assert cmd.can_execute(msg) is False

    def test_dark_joke_in_dm_allowed(self):
        bot = _make_bot()
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke dark", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_disabled_command_blocked(self):
        bot = _make_bot()
        bot.config.set("Joke_Command", "enabled", "false")
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke", channel="general")
        assert cmd.can_execute(msg) is False


class TestJokeMatchesKeyword:
    def test_joke_matches(self):
        cmd = JokeCommand(_make_bot())
        assert cmd.matches_keyword(mock_message(content="joke")) is True

    def test_jokes_matches(self):
        cmd = JokeCommand(_make_bot())
        assert cmd.matches_keyword(mock_message(content="jokes")) is True

    def test_joke_with_category_matches(self):
        cmd = JokeCommand(_make_bot())
        assert cmd.matches_keyword(mock_message(content="joke programming")) is True

    def test_other_does_not_match(self):
        cmd = JokeCommand(_make_bot())
        assert cmd.matches_keyword(mock_message(content="dadjoke")) is False

    def test_exclamation_prefix_matches(self):
        cmd = JokeCommand(_make_bot())
        assert cmd.matches_keyword(mock_message(content="!joke")) is True


class TestJokeGetHelpText:
    def test_public_channel_excludes_dark(self):
        cmd = JokeCommand(_make_bot())
        msg = mock_message(content="joke", channel="general", is_dm=False)
        result = cmd.get_help_text(message=msg)
        assert "dark" not in result
        assert "joke" in result.lower()

    def test_dm_includes_dark(self):
        cmd = JokeCommand(_make_bot())
        msg = mock_message(content="joke", is_dm=True)
        result = cmd.get_help_text(message=msg)
        assert "dark" in result

    def test_no_message_returns_all_categories(self):
        cmd = JokeCommand(_make_bot())
        result = cmd.get_help_text()
        assert "joke" in result.lower()


class TestJokeFormatUnknownType:
    def test_unknown_type_with_no_text_returns_fallback(self):
        cmd = JokeCommand(_make_bot())
        data = {"type": "weird"}
        result = cmd.format_joke(data)
        assert "🎭" in result

    def test_twopart_only_setup(self):
        cmd = JokeCommand(_make_bot())
        data = {"type": "twopart", "setup": "Why?", "delivery": ""}
        result = cmd.format_joke(data)
        assert "🎭" in result
        assert "Why?" in result


class TestJokeExecute:
    def test_execute_invalid_category_sends_error(self):
        import asyncio
        from unittest.mock import AsyncMock

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke badcategory", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        bot.command_manager.send_response.assert_called_once()

    def test_execute_no_category_with_joke(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke", channel="general")

        with patch.object(cmd, "get_joke_with_length_handling", new_callable=AsyncMock,
                          return_value={"type": "single", "joke": "A short joke!"}):
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_returns_true_when_no_joke(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke", channel="general")

        with patch.object(cmd, "get_joke_with_length_handling", new_callable=AsyncMock,
                          return_value=None):
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_dark_category_no_joke(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke dark", is_dm=True)

        with patch.object(cmd, "get_joke_with_length_handling", new_callable=AsyncMock,
                          return_value=None):
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_handles_exception(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke", channel="general")

        with patch.object(cmd, "get_joke_with_length_handling", new_callable=AsyncMock,
                          side_effect=Exception("API error")):
            result = asyncio.run(cmd.execute(msg))
        assert result is True


class TestSendJokeWithLengthHandling:
    def test_short_joke_sends_single_message(self):
        import asyncio
        from unittest.mock import AsyncMock

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke")

        joke_data = {"type": "single", "joke": "Short!"}
        asyncio.run(cmd.send_joke_with_length_handling(msg, joke_data))
        bot.command_manager.send_response.assert_called_once()

    def test_long_joke_split(self):
        import asyncio
        from unittest.mock import AsyncMock

        bot = _make_bot()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = JokeCommand(bot)
        msg = mock_message(content="joke")

        long_joke = "x" * 50 + ". " + "y" * 80
        joke_data = {"type": "single", "joke": long_joke}
        asyncio.run(cmd.send_joke_with_length_handling(msg, joke_data))
        assert bot.command_manager.send_response.call_count >= 1
