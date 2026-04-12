"""Tests for modules.commands.fortune_command."""

import configparser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands.fortune_command import FortuneCommand
from tests.conftest import mock_message


def _make_bot(fortune_file: str = "data/fortune/fortunes.txt", enabled: bool = True) -> MagicMock:
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Fortune_Command")
    config.set("Fortune_Command", "enabled", str(enabled).lower())
    config.set("Fortune_Command", "file", fortune_file)
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    return bot


# ---------------------------------------------------------------------------
# Fortune file parsing
# ---------------------------------------------------------------------------


class TestLoadFortunes:
    """Tests for _load_fortunes() — covers all BSD format edge cases."""

    def _cmd(self, tmp_path: Path, content: str) -> FortuneCommand:
        f = tmp_path / "fortunes.txt"
        f.write_text(content, encoding="utf-8")
        cmd = FortuneCommand(_make_bot(fortune_file=str(f)))
        return cmd

    def test_parses_percent_separated_entries(self, tmp_path):
        """Standard BSD format: entries separated by a line containing only %."""
        cmd = self._cmd(tmp_path, "First fortune\n%\nSecond fortune\n%\nThird fortune\n%\n")
        fortunes = cmd._load_fortunes()
        assert fortunes == ["First fortune", "Second fortune", "Third fortune"]

    def test_parses_without_trailing_percent(self, tmp_path):
        """File with no trailing % still returns last entry."""
        cmd = self._cmd(tmp_path, "Fortune one\n%\nFortune two")
        fortunes = cmd._load_fortunes()
        assert fortunes == ["Fortune one", "Fortune two"]

    def test_parses_multiline_fortune(self, tmp_path):
        """Multi-line fortunes are kept intact."""
        content = "Line one\nLine two\n\t-- Attribution\n%\nOther fortune\n%\n"
        cmd = self._cmd(tmp_path, content)
        fortunes = cmd._load_fortunes()
        assert len(fortunes) == 2
        assert "Line one" in fortunes[0]
        assert "Attribution" in fortunes[0]
        assert fortunes[1] == "Other fortune"

    def test_empty_file_returns_empty_list(self, tmp_path):
        """Empty file produces no fortunes."""
        cmd = self._cmd(tmp_path, "")
        assert cmd._load_fortunes() == []

    def test_only_percent_signs_returns_empty_list(self, tmp_path):
        """File with only % separators and blank entries produces nothing."""
        cmd = self._cmd(tmp_path, "%\n%\n%\n")
        assert cmd._load_fortunes() == []

    def test_strips_leading_trailing_whitespace_from_entries(self, tmp_path):
        """Whitespace around each fortune is stripped."""
        cmd = self._cmd(tmp_path, "  fortune one  \n%\n\nfortune two\n\n%\n")
        fortunes = cmd._load_fortunes()
        assert fortunes[0] == "fortune one"
        assert fortunes[1] == "fortune two"

    def test_percent_inside_line_not_treated_as_separator(self, tmp_path):
        """A '%' not on its own line is NOT a separator."""
        cmd = self._cmd(tmp_path, "100% done\n%\nSecond\n%\n")
        fortunes = cmd._load_fortunes()
        assert fortunes[0] == "100% done"
        assert fortunes[1] == "Second"

    def test_file_not_found_returns_empty_list(self, tmp_path):
        """Missing file logs an error and returns empty list."""
        cmd = FortuneCommand(_make_bot(fortune_file=str(tmp_path / "nonexistent.txt")))
        fortunes = cmd._load_fortunes()
        assert fortunes == []
        cmd.logger.error.assert_called()

    def test_dangerous_path_rejected(self):
        """validate_safe_path must reject dangerous system paths."""
        cmd = FortuneCommand(_make_bot(fortune_file="/etc/passwd"))
        fortunes = cmd._load_fortunes()
        assert fortunes == []
        cmd.logger.error.assert_called()

    def test_validate_safe_path_called(self, tmp_path):
        """validate_safe_path is always called before open()."""
        f = tmp_path / "fortunes.txt"
        f.write_text("a fortune\n%\n", encoding="utf-8")
        cmd = FortuneCommand(_make_bot(fortune_file=str(f)))
        with patch(
            "modules.commands.fortune_command.validate_safe_path",
            wraps=lambda p, **kw: None,
        ) as mock_vsp:
            result = cmd._load_fortunes()
        mock_vsp.assert_called_once()
        # validate_safe_path returned None → path rejected → empty
        assert result == []


# ---------------------------------------------------------------------------
# can_execute
# ---------------------------------------------------------------------------


class TestCanExecute:
    def test_disabled_command_cannot_execute(self):
        cmd = FortuneCommand(_make_bot(enabled=False))
        msg = mock_message(content="fortune")
        assert cmd.can_execute(msg) is False

    def test_enabled_command_can_execute(self):
        cmd = FortuneCommand(_make_bot())
        msg = mock_message(content="fortune")
        assert cmd.can_execute(msg) is True


# ---------------------------------------------------------------------------
# execute — async
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.asyncio
    async def test_sends_random_fortune(self, tmp_path):
        """execute() picks a random fortune and sends it."""
        f = tmp_path / "fortunes.txt"
        f.write_text("Fortune A\n%\nFortune B\n%\n", encoding="utf-8")
        bot = _make_bot(fortune_file=str(f))
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = FortuneCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)

        with patch("modules.commands.fortune_command.random.choice", return_value="Fortune A"):
            msg = mock_message(content="fortune")
            result = await cmd.execute(msg)

        assert result is True
        cmd.send_response.assert_called_once()
        call_text = cmd.send_response.call_args[0][1]
        assert call_text == "Fortune A"

    @pytest.mark.asyncio
    async def test_random_choice_called_with_all_fortunes(self, tmp_path):
        """random.choice receives the full list of parsed fortunes."""
        fortunes_text = "One\n%\nTwo\n%\nThree\n%\n"
        f = tmp_path / "fortunes.txt"
        f.write_text(fortunes_text, encoding="utf-8")
        cmd = FortuneCommand(_make_bot(fortune_file=str(f)))
        cmd.send_response = AsyncMock(return_value=True)

        with patch("modules.commands.fortune_command.random.choice", return_value="Two") as mock_choice:
            await cmd.execute(mock_message(content="fortune"))

        mock_choice.assert_called_once_with(["One", "Two", "Three"])

    @pytest.mark.asyncio
    async def test_empty_file_sends_no_fortunes_message(self, tmp_path):
        """When no fortunes are available, a friendly message is sent."""
        f = tmp_path / "fortunes.txt"
        f.write_text("", encoding="utf-8")
        cmd = FortuneCommand(_make_bot(fortune_file=str(f)))
        cmd.send_response = AsyncMock(return_value=True)

        result = await cmd.execute(mock_message(content="fortune"))

        assert result is True
        call_text = cmd.send_response.call_args[0][1]
        assert "No fortunes" in call_text or "available" in call_text

    @pytest.mark.asyncio
    async def test_execute_returns_true_on_file_error(self):
        """execute() never crashes — returns True even when the file is missing."""
        cmd = FortuneCommand(_make_bot(fortune_file="/does/not/exist.txt"))
        cmd.send_response = AsyncMock(return_value=True)

        result = await cmd.execute(mock_message(content="fortune"))

        assert result is True
        cmd.send_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_different_fortunes_returned_across_calls(self, tmp_path):
        """random.choice produces variety — different fortunes on different calls."""
        fortunes_text = "\n%\n".join(f"Fortune {i}" for i in range(10)) + "\n%\n"
        f = tmp_path / "fortunes.txt"
        f.write_text(fortunes_text, encoding="utf-8")
        cmd = FortuneCommand(_make_bot(fortune_file=str(f)))
        cmd.send_response = AsyncMock(return_value=True)

        results = []
        for i in range(10):
            with patch(
                "modules.commands.fortune_command.random.choice",
                return_value=f"Fortune {i}",
            ):
                await cmd.execute(mock_message(content="fortune"))
            results.append(cmd.send_response.call_args[0][1])

        assert len(set(results)) > 1, "Expected multiple distinct fortunes across calls"


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


class TestPluginMetadata:
    def test_name(self):
        assert FortuneCommand.name == "fortune"

    def test_keywords_contains_fortune(self):
        assert "fortune" in FortuneCommand.keywords

    def test_requires_internet_false(self):
        assert FortuneCommand.requires_internet is False

    def test_category_entertainment(self):
        assert FortuneCommand.category == "entertainment"

    def test_get_help_text(self):
        cmd = FortuneCommand(_make_bot())
        text = cmd.get_help_text()
        assert isinstance(text, str)
        assert len(text) > 0
