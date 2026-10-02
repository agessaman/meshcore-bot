"""wx and gwx forecast the sender's (or bot's) own point, and accept zero coordinates."""

import asyncio
import configparser
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand
from modules.location import get_companion_lat_lon


def _cmd(cls, use_bot=False):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"weather_provider": "noaa", "default_city": ""},
        "Wx_Command": {"use_bot_location_when_no_location": "true" if use_bot else "false"},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    cmd = cls(bot)
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.record_execution = Mock()
    cmd.get_weather_for_location = AsyncMock(return_value="ok")
    # A reverse lookup that succeeds: gwx used to forecast this name instead of the point.
    cmd._coordinates_to_location_string = Mock(return_value="Somewhere, WA")
    cmd._coordinates_to_location_string_async = AsyncMock(return_value="Somewhere, WA")
    return cmd


def _message():
    return SimpleNamespace(content="wx", sender_id="u", sender_pubkey="pk", channel="general", is_dm=False)


@pytest.mark.parametrize("cls", [GlobalWxCommand, WxCommand])
def test_companion_point_is_forecast_not_its_place_name(cls):
    cmd = _cmd(cls)
    cmd._get_companion_location = Mock(return_value=(47.6062, -122.3321))
    asyncio.run(cmd.execute(_message()))
    assert cmd.get_weather_for_location.await_args.args[0] == "47.60620,-122.33210"


@pytest.mark.parametrize("cls", [GlobalWxCommand, WxCommand])
def test_bot_point_is_forecast_not_its_place_name(cls):
    cmd = _cmd(cls, use_bot=True)
    cmd._get_companion_location = Mock(return_value=None)
    cmd._get_bot_location = Mock(return_value=(-33.8688, 151.2093))
    asyncio.run(cmd.execute(_message()))
    assert cmd.get_weather_for_location.await_args.args[0] == "-33.86880,151.20930"


def test_gwx_no_longer_reverse_geocodes_just_to_pick_the_location():
    cmd = _cmd(GlobalWxCommand)
    cmd._get_companion_location = Mock(return_value=(47.6062, -122.3321))
    asyncio.run(cmd.execute(_message()))
    cmd._coordinates_to_location_string.assert_not_called()


@pytest.mark.parametrize("cls", [GlobalWxCommand, WxCommand])
def test_tiny_coordinates_stay_in_the_coordinate_form(cls):
    # str(0.00001) is "1e-05", which the "lat,lon" pattern does not match.
    cmd = _cmd(cls)
    cmd._get_companion_location = Mock(return_value=(0.00001, 32.5))
    asyncio.run(cmd.execute(_message()))
    location = cmd.get_weather_for_location.await_args.args[0]
    assert "e" not in location
    assert location == "0.00001,32.50000"


def _db_bot(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE complete_contact_tracking (public_key TEXT, latitude REAL, longitude REAL,"
        " last_advert_timestamp TEXT, last_heard TEXT)"
    )
    conn.executemany("INSERT INTO complete_contact_tracking VALUES (?, ?, ?, ?, ?)", rows)
    bot = Mock()
    bot.db_manager.execute_query = lambda q, p=(): [dict(r) for r in conn.execute(q, p).fetchall()]
    return bot


@pytest.mark.parametrize("point", [(0.0, 9.5), (51.48, 0.0)])
def test_companion_on_the_equator_or_prime_meridian_is_found(point):
    bot = _db_bot([("pk", point[0], point[1], "2026-10-01", "2026-10-01")])
    assert get_companion_lat_lon(bot, SimpleNamespace(sender_pubkey="pk")) == point


def test_companion_at_zero_zero_is_still_treated_as_no_position():
    bot = _db_bot([("pk", 0.0, 0.0, "2026-10-01", "2026-10-01")])
    assert get_companion_lat_lon(bot, SimpleNamespace(sender_pubkey="pk")) is None


def test_wx_city_lookup_accepts_a_zero_coordinate(monkeypatch):
    cmd = _cmd(WxCommand)
    monkeypatch.setattr(
        "modules.commands.wx_command.geocode_city_sync", lambda *a, **k: (0.0, 6.73, {"city": "São Tomé"})
    )
    assert cmd.city_to_lat_lon("Sao Tome") == (0.0, 6.73, {"city": "São Tomé"})
