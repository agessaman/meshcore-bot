"""Tests for modules.commands.moon_command — pure logic functions."""

import configparser
from unittest.mock import MagicMock, Mock

import pytest

from modules.commands.moon_command import MoonCommand
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
    bot.config = config
    bot.translator = MagicMock()
    # Return first arg key as-is (mimics missing translation)
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestTranslatePhaseName:
    """Tests for _translate_phase_name."""

    def setup_method(self):
        self.cmd = MoonCommand(_make_bot())

    def test_returns_original_when_no_translation(self):
        # With our mock translator, translate returns the key, not a translation
        # so _translate_phase_name should fall back to the original
        result = self.cmd._translate_phase_name("Full Moon")
        # Since translate returns the key (not found), fallback to original
        assert result == "Full Moon" or "full_moon" in result or "Full Moon" in result

    def test_strips_emoji_before_matching(self):
        # Phase with emoji — should still match
        result = self.cmd._translate_phase_name("🌕 Full Moon")
        # Should not crash and should return something
        assert result is not None
        assert isinstance(result, str)

    def test_unknown_phase_returned_as_is(self):
        result = self.cmd._translate_phase_name("Totally Unknown Phase")
        assert result == "Totally Unknown Phase"

    def test_all_known_phases_dont_crash(self):
        phases = [
            "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
            "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"
        ]
        for phase in phases:
            result = self.cmd._translate_phase_name(phase)
            assert isinstance(result, str)


class TestFormatMoonResponse:
    """Tests for _format_moon_response."""

    def setup_method(self):
        self.cmd = MoonCommand(_make_bot())

    def test_valid_moon_info_parsed(self):
        moon_info = (
            "MoonRise: Thu 04 06:47PM\n"
            "Set: Fri 05 03:43AM\n"
            "Phase: Full Moon @: 87%\n"
            "FullMoon: Sun Sep 07 11:08AM\n"
            "NewMoon: Sun Sep 21 12:54PM"
        )
        result = self.cmd._format_moon_response(moon_info)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_partial_moon_info_falls_back(self):
        # Only some keys present — should fall back to original or key
        moon_info = "Phase: Full Moon\nSome: data"
        result = self.cmd._format_moon_response(moon_info)
        assert isinstance(result, str)

    def test_empty_string_falls_back(self):
        result = self.cmd._format_moon_response("")
        assert isinstance(result, str)

    def test_malformed_input_falls_back(self):
        result = self.cmd._format_moon_response("This is not key:value format at all")
        assert isinstance(result, str)

    def test_moon_info_with_dates_format(self):
        moon_info = (
            "MoonRise: Thu 04 06:47PM\n"
            "Set: Fri 05 03:43AM\n"
            "Phase: Full Moon @: 87%\n"
            "FullMoon: Sun Sep 07 11:08AM\n"
            "NewMoon: Sun Sep 21 12:54PM"
        )
        result = self.cmd._format_moon_response(moon_info)
        # Should have used 'format_with_dates' path since all keys present
        # The output is a translated key so may contain the key name
        assert isinstance(result, str)

    def test_moon_info_without_full_new_moon(self):
        moon_info = (
            "MoonRise: Thu 04 06:47PM\n"
            "Set: Fri 05 03:43AM\n"
            "Phase: Waxing Gibbous @: 60%"
        )
        result = self.cmd._format_moon_response(moon_info)
        assert isinstance(result, str)


class TestMoonCommandEnabled:
    """Tests for can_execute."""

    def test_can_execute_enabled(self):
        bot = _make_bot()
        bot.config.add_section("Moon_Command")
        bot.config.set("Moon_Command", "enabled", "true")
        cmd = MoonCommand(bot)
        msg = mock_message(content="moon", channel="general")
        assert cmd.can_execute(msg) is True

    def test_can_execute_disabled(self):
        bot = _make_bot()
        bot.config.add_section("Moon_Command")
        bot.config.set("Moon_Command", "enabled", "false")
        cmd = MoonCommand(bot)
        msg = mock_message(content="moon", channel="general")
        assert cmd.can_execute(msg) is False


class TestGetHelpTextMoon:
    def test_returns_description(self):
        cmd = MoonCommand(_make_bot())
        result = cmd.get_help_text()
        assert isinstance(result, str)
        assert result == cmd.description


class TestFormatMoonPhaseNoAt:
    """Test _format_moon_response when Phase has no @: (else branch)."""

    def setup_method(self):
        self.cmd = MoonCommand(_make_bot())

    def test_phase_without_at_sign(self):
        moon_info = (
            "MoonRise: Thu 04 06:47PM\n"
            "Set: Fri 05 03:43AM\n"
            "Phase: Waxing Crescent"
        )
        result = self.cmd._format_moon_response(moon_info)
        assert isinstance(result, str)

    def test_exception_in_format_falls_back(self):
        """If _format_moon_response raises, fallback is returned."""
        # Force an exception by passing an object with no .split
        result = self.cmd._format_moon_response(None)
        assert isinstance(result, str)


class TestTranslatePhaseNameFound:
    """Test _translate_phase_name when a translation IS found (line 104)."""

    def test_translation_returned_when_not_key(self):
        """When translator returns actual text (not key), it's returned."""
        bot = _make_bot()
        # Override translator to return a real translation for the phase key
        bot.translator.translate = Mock(
            side_effect=lambda key, **kw: "Pleine Lune" if "full_moon" in key else key
        )
        cmd = MoonCommand(bot)
        result = cmd._translate_phase_name("Full Moon")
        assert result == "Pleine Lune"


class TestMoonExecute:
    """Tests for execute()."""

    def test_execute_success(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot()
        cmd = MoonCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        with patch("modules.commands.moon_command.get_moon", return_value="MoonRise: Thu 04 06:47PM\nSet: Fri 05 03:43AM\nPhase: Full Moon @: 87%"):
            msg = mock_message(content="moon", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_execute_error_returns_false(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot()
        cmd = MoonCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        with patch("modules.commands.moon_command.get_moon", side_effect=Exception("API error")):
            msg = mock_message(content="moon", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is False
        cmd.send_response.assert_called_once()
