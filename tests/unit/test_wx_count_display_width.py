#!/usr/bin/env python3
"""Unit tests for WxCommand / GlobalWxCommand _count_display_width (UTF-8 byte length, PR #128)."""

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand


@pytest.fixture
def wx_bot(mock_bot):
    """Bot config for NOAA WxCommand (no openmeteo delegation)."""
    mock_bot.config.add_section("Wx_Command")
    mock_bot.config.set("Wx_Command", "enabled", "true")
    mock_bot.config.add_section("Weather")
    mock_bot.config.set("Weather", "weather_provider", "noaa")
    mock_bot.config.set("Weather", "default_country", "US")
    return mock_bot


@pytest.fixture
def wx_cmd(wx_bot):
    return WxCommand(wx_bot)


@pytest.fixture
def gwx_cmd(wx_bot):
    """GlobalWxCommand shares the same bot shape (db_manager, Weather section)."""
    return GlobalWxCommand(wx_bot)


@pytest.mark.unit
class TestWxCommandCountDisplayWidth:
    def test_ascii_matches_byte_length(self, wx_cmd):
        s = "Hello 72°F"
        assert wx_cmd._count_display_width(s) == len(s.encode("utf-8"))

    def test_emoji_one_char_four_bytes(self, wx_cmd):
        assert len("😀") == 1
        assert wx_cmd._count_display_width("😀") == 4

    def test_mixed_text_budget(self, wx_cmd):
        s = "WX " + "🌧️" * 3
        assert wx_cmd._count_display_width(s) == len(s.encode("utf-8"))


@pytest.mark.unit
class TestGlobalWxCommandCountDisplayWidth:
    def test_ascii_matches_byte_length(self, gwx_cmd):
        s = "Tokyo 15°C"
        assert gwx_cmd._count_display_width(s) == len(s.encode("utf-8"))

    def test_emoji_one_char_four_bytes(self, gwx_cmd):
        assert gwx_cmd._count_display_width("😀") == 4

    def test_mixed_text_budget(self, gwx_cmd):
        s = "GWX " + "❄️" * 2
        assert gwx_cmd._count_display_width(s) == len(s.encode("utf-8"))


@pytest.mark.unit
def test_wx_and_gwx_count_display_width_agree_on_same_string(wx_cmd, gwx_cmd):
    """Both commands use the same OTA byte semantics for layout."""
    samples = ["", "a", "résumé", "🎯📍❓", "line1\nline2"]
    for s in samples:
        assert wx_cmd._count_display_width(s) == gwx_cmd._count_display_width(s)
