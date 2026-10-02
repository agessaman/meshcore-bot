"""gwx names the place in its reply only when that adds information, as wx does."""

import configparser
from unittest.mock import Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand

MODULE = "modules.commands.alternatives.wx_international"


def _gwx(default_country="US", default_state=""):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"default_country": default_country, "default_state": default_state},
        "Gwx_Command": {},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    cmd = GlobalWxCommand(bot)
    cmd.get_open_meteo_weather = Mock(return_value="Clear 20°C")
    cmd._get_open_meteo_weather_with_conditions = Mock(return_value=("Clear 20°C", {}))
    return cmd


def _reply(cmd, location, address):
    cmd.geocode_location = Mock(return_value=(1.0, 2.0, address, None))
    return cmd._get_weather_for_location_sync(location, message=None)


PARIS = {"city": "Paris", "country_code": "fr"}
LONDON = {"city": "London", "country_code": "gb"}
SEATTLE = {"city": "Seattle", "state": "Washington", "country_code": "us"}
PORTLAND = {"city": "Portland", "state": "Oregon", "country_code": "us"}


@pytest.mark.parametrize(("default_country", "default_state", "location", "address", "expected"), [
    ("GB", "", "London", LONDON, "Clear 20°C"),
    ("GB", "", "Paris", PARIS, "Paris, FR: Clear 20°C"),
    ("US", "WA", "Seattle", SEATTLE, "Clear 20°C"),
    ("US", "Washington", "Seattle", SEATTLE, "Clear 20°C"),
    ("US", "WA", "Portland", PORTLAND, "Portland, OR: Clear 20°C"),
    ("US", "", "Seattle", SEATTLE, "Seattle, WA: Clear 20°C"),
    ("US", "WA", "Paris", PARIS, "Paris, FR: Clear 20°C"),
])
def test_cities_are_named_when_they_leave_the_default_region(default_country, default_state, location, address, expected):
    assert _reply(_gwx(default_country, default_state), location, address) == expected


@pytest.mark.parametrize(("address", "expected"), [
    (SEATTLE, "Seattle, WA: Clear 20°C"),
    ({"country_code": "us", "state": "Washington"}, "Clear 20°C"),  # no place name: no label
    ({}, "Clear 20°C"),  # the reverse lookup failed
])
def test_coordinates_are_named_when_a_place_is_found(address, expected):
    # Even inside the default state: the user did not type a place name.
    assert _reply(_gwx("US", "WA"), "47.60620,-122.33210", address) == expected


def test_a_zip_code_is_not_named_and_not_reverse_geocoded():
    cmd = _gwx()
    with patch(f"{MODULE}.geocode_zipcode_sync", return_value=(47.6, -122.3)), patch(
        f"{MODULE}.rate_limited_nominatim_reverse_sync"
    ) as reverse:
        assert cmd.geocode_location("98104") == (47.6, -122.3, {}, None)
    reverse.assert_not_called()
    assert _reply(cmd, "98104", {}) == "Clear 20°C"


def test_an_unnamed_reply_keeps_the_whole_budget():
    cmd = _gwx("GB")
    _reply(cmd, "London", LONDON)
    assert cmd._get_open_meteo_weather_with_conditions.call_args.kwargs["location_prefix_len"] == 0
