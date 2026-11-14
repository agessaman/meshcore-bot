#!/usr/bin/env python3
"""The location prefix has to be reserved out of the RF byte budget.

`get_weather_for_location` formats the forecast body to `max_length` and *then*
prepends "City, State: ". Unless the prefix is reserved first, the assembled
reply overruns the frame by the prefix's own length -- which is how an
emoji-dense forecast that looks like 121 characters lands as 152 bytes and gets
split in two.
"""

import configparser
from unittest.mock import Mock, patch

from modules.commands.wx_command import WxCommand

# Emoji-dense body in the shape the gwx formatter produces: 112 characters but
# 136 bytes, because °F, ☀️, 💨, 💧, 👁️ and 📊 all cost more than one byte.
EMOJI_BODY = (
    "This Afternoon: ☀️Partly Sunny 100°F \U0001f4a8ES10 41%RH \U0001f4a769° "
    "\U0001f441️9mi \U0001f4ca1021hPa | Tonight: ☁️Partly Cloudy L:75°F \U0001f4a8ES0"
)


def _wx_command(mock_logger):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "weather_provider", "noaa")
    config.add_section("Wx_Command")
    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.db_manager.cache_geocoding = Mock()
    bot.command_manager = Mock()
    return WxCommand(bot)


class TestEmojiByteCost:
    """Establishes why a 'short' forecast overruns the frame at all."""

    def test_emoji_body_costs_far_more_bytes_than_characters(self):
        assert len(EMOJI_BODY) < 130
        assert len(EMOJI_BODY.encode("utf-8")) > 130

    def test_display_width_counts_bytes_not_characters(self, mock_logger):
        cmd = _wx_command(mock_logger)
        assert cmd._count_display_width(EMOJI_BODY) == len(EMOJI_BODY.encode("utf-8"))
        assert cmd._count_display_width("°") == 2
        assert cmd._count_display_width("\U0001f4a8") == 4


class TestLocationPrefixIsReserved:
    """End-to-end: the budget reaching the formatters already excludes the prefix."""

    BUDGET = 143  # channel body budget for a 15-byte bot name

    @staticmethod
    def _run(cmd, budget, body):
        """Drive the sync path with NOAA stubbed by a formatter that obeys its budget.

        The real formatters trim to the max_length they are handed; a stub that
        ignored it would make the end-to-end assertion vacuous. Returns
        (result, budget_handed_to_the_formatter, body_it_returned).
        """
        seen = {}

        def fake_get_noaa_weather(lat, lon, return_periods=False, max_length=130):
            seen["max_length"] = max_length
            trimmed = body
            while len(trimmed.encode("utf-8")) > max_length:
                trimmed = trimmed[:-1]
            seen["body"] = trimmed
            return trimmed, {}

        with patch.object(
            WxCommand, "get_max_message_length", Mock(return_value=budget)
        ), patch.object(
            WxCommand, "get_noaa_weather", Mock(side_effect=fake_get_noaa_weather)
        ), patch.object(
            WxCommand, "get_weather_alerts_noaa", Mock(return_value=WxCommand.NO_ALERTS)
        ):
            result = cmd._get_weather_for_location_sync(
                "30.8,-97.6", "coordinates", message=Mock()
            )
        return result, seen.get("max_length"), seen.get("body")

    def test_a_prefix_is_actually_in_play(self, mock_logger):
        """Guards the two tests below: without a prefix they would prove nothing."""
        cmd = _wx_command(mock_logger)
        result, given, body = self._run(cmd, self.BUDGET, EMOJI_BODY)
        assert given is not None, "get_noaa_weather was never reached"
        assert result.endswith(body)
        assert result != body, "no location prefix was prepended"

    def test_formatter_budget_excludes_the_prefix(self, mock_logger):
        cmd = _wx_command(mock_logger)
        result, given, body = self._run(cmd, self.BUDGET, EMOJI_BODY)
        prefix = result[: len(result) - len(body)]
        assert given == self.BUDGET - cmd._count_display_width(prefix)

    def test_assembled_reply_fits_the_frame(self, mock_logger):
        """The bug: body sized to the full budget, then prefixed, overran the frame."""
        cmd = _wx_command(mock_logger)
        result, _, _ = self._run(cmd, self.BUDGET, EMOJI_BODY)
        assert cmd._count_display_width(result) <= self.BUDGET

    def test_fits_the_frame_for_a_range_of_budgets(self, mock_logger):
        cmd = _wx_command(mock_logger)
        for budget in (100, 120, 130, 143, 158):
            result, _, _ = self._run(cmd, budget, EMOJI_BODY)
            assert cmd._count_display_width(result) <= budget, budget


class TestBodyBudgetArithmetic:
    """Direct cover for the reservation, independent of the geocoding seam."""

    def test_prefix_bytes_come_off_the_budget(self, mock_logger):
        cmd = _wx_command(mock_logger)
        prefix = "Lockhart, Texas: "
        budget = 143
        body_budget = max(
            budget - cmd._count_display_width(prefix), WxCommand.MIN_BODY_BUDGET
        )
        assert body_budget == 126
        # A body formatted to the reduced budget still fits once prefixed.
        assert body_budget + cmd._count_display_width(prefix) <= budget

    def test_multibyte_city_name_is_charged_in_bytes(self, mock_logger):
        cmd = _wx_command(mock_logger)
        prefix = "München, Bayern: "
        assert cmd._count_display_width(prefix) > len(prefix)
        body_budget = max(
            143 - cmd._count_display_width(prefix), WxCommand.MIN_BODY_BUDGET
        )
        assert body_budget + cmd._count_display_width(prefix) <= 143

    def test_long_prefix_cannot_starve_the_body(self, mock_logger):
        cmd = _wx_command(mock_logger)
        prefix = "A" * 200 + ": "
        body_budget = max(
            143 - cmd._count_display_width(prefix), WxCommand.MIN_BODY_BUDGET
        )
        assert body_budget == WxCommand.MIN_BODY_BUDGET
        assert body_budget > 0

    def test_no_prefix_keeps_the_whole_budget(self, mock_logger):
        cmd = _wx_command(mock_logger)
        body_budget = max(143 - cmd._count_display_width(""), WxCommand.MIN_BODY_BUDGET)
        assert body_budget == 143
