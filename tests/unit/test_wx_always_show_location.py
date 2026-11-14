"""[Weather] always_show_location makes wx (NOAA) name the place for cities in the default state and ZIP codes."""

import configparser
from unittest.mock import Mock, patch

import pytest

from modules.commands.wx_command import WxCommand


def _wx(always=None):
    weather = {"weather_provider": "noaa", "default_state": "WA"}
    if always is not None:
        weather["always_show_location"] = str(always).lower()
    config = configparser.ConfigParser()
    config.read_dict({"Weather": weather, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    return WxCommand(bot)


def _reply(cmd, location, location_type):
    with patch.object(WxCommand, "get_noaa_weather", Mock(return_value=("Today: Clear 60°F", {}))), patch.object(
        WxCommand, "get_weather_alerts_noaa", Mock(return_value=WxCommand.NO_ALERTS)
    ), patch.object(WxCommand, "zipcode_to_lat_lon", Mock(return_value=(48.0, -122.1))), patch.object(
        WxCommand, "city_to_lat_lon",
        Mock(return_value=(47.6, -122.3, {"city": "Seattle", "state": "Washington", "country_code": "us"})),
    ), patch.object(WxCommand, "_coordinates_to_location_string", Mock(return_value="Lake Stevens, WA")):
        return cmd._get_weather_for_location_sync(location, location_type, message=None)


@pytest.mark.parametrize(("location", "location_type", "expected"), [
    ("seattle", "city", "Seattle, WA: Today: Clear 60°F"),
    ("98258", "zipcode", "Lake Stevens, WA: Today: Clear 60°F"),
])
def test_always_show_location_names_the_place(location, location_type, expected):
    assert _reply(_wx(always=True), location, location_type) == expected


@pytest.mark.parametrize("always", [None, False])
@pytest.mark.parametrize(("location", "location_type"), [("seattle", "city"), ("98258", "zipcode")])
def test_off_by_default_a_city_in_the_default_state_and_a_zip_code_are_not_named(always, location, location_type):
    assert _reply(_wx(always=always), location, location_type) == "Today: Clear 60°F"


def test_the_option_is_in_the_settings_schema():
    entry = next(e for e in WxCommand.settings_schema if e["key"] == "always_show_location")
    assert entry["section"] == "Weather" and entry["type"] == "bool" and entry["default"] is False
