"""Characterization: how wx and gwx ``execute`` dispatch a request.

Drives both commands through a matrix of inputs and environments with every
provider mocked at its call boundary, and records the ordered calls each
request makes (lookups, fetches, sends, pacing, log lines) plus the return
value. The environments cover the no-location fallback chain (custom MQTT and
WXSIM defaults, the sender's position, default_city, the bot's position),
named custom sources, provider failures, the two-part alert reply, and wx's
alert-only geocoding.
"""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.characterization.golden_util import assert_golden

INPUTS = [
    "", "hourly", "HOURLY", "tomorrow", "5d", "3", "alerts", "alerts alerts",
    "98101", "98101 alerts", "seattle, wa", "seattle  wa 3", "paris 7d", "paris 30d", "paris 1",
    "47.6,-122.3", "47.6,-122.3 alerts", "95.0,10.0 alerts", "x alerts", "a b alerts",
    "home", "home 3", "home alerts", "home hourly", "cabin", "cabin tomorrow", "cabin alerts",
    "both", "both 5d",
]

# Failure environments only change what happens after dispatch, so they run a
# smaller set: no location, an option alone, each location type, alerts.
CORE_INPUTS = ["", "hourly", "5d", "alerts", "98101", "98101 alerts", "seattle, wa", "47.6,-122.3 alerts", "x alerts"]

MULTI = ("multi_message", "WEATHER", "ALERTS")

FAILURE_ENVS = {"multi_first_refused", "weather_raises", "geocode_fails", "city_pair", "send_refused"}

ENVS = {
    "plain": {},
    "default_city": {"default_city": "Seattle", "default_state": "WA", "default_country": "US"},
    "default_city_only": {"default_city": "Tacoma"},
    "companion_named": {"companion": (47.6062, -122.3321), "display": "Seattle, WA"},
    "companion_unnamed": {"companion": (47.6062, -122.3321)},
    "bot_location": {"use_bot": True, "bot": (48.75, -122.48), "display": "Bellingham"},
    "bot_location_unnamed": {"use_bot": True, "bot": (48.75, -122.48)},
    "bot_location_missing": {"use_bot": True},
    "mqtt": {"mqtt": {None: "wx/default", "home": "wx/home", "both": "wx/both"}},
    "mqtt_raises": {"mqtt": {None: "wx/default", "home": "wx/home"}, "mqtt_raises": True},
    "wxsim": {"wxsim": {None: "https://w.test/default", "cabin": "https://w.test/cabin", "both": "https://w.test/both"}},
    "wxsim_raises": {"wxsim": {None: "https://w.test/default", "cabin": "https://w.test/cabin"}, "wxsim_raises": True},
    "both_sources": {"mqtt": {"both": "wx/both"}, "wxsim": {"both": "https://w.test/both"}},
    "multi": {"weather": MULTI},
    "multi_first_refused": {"weather": MULTI, "send_results": [False]},
    "weather_raises": {"weather_raises": True},
    "geocode_fails": {"geocode_fails": True},
    "city_pair": {"city_pair": True},
    "send_refused": {"send_results": [False] * 4},
}


def _build(cls_name, env):
    from modules.commands.alternatives.wx_international import GlobalWxCommand
    from modules.commands.wx_command import WxCommand

    cls = {"wx": WxCommand, "gwx": GlobalWxCommand}[cls_name]
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {
            "weather_provider": "noaa",
            "default_city": env.get("default_city", ""),
            "default_state": env.get("default_state", ""),
            "default_country": env.get("default_country", ""),
        },
        "Wx_Command": {"use_bot_location_when_no_location": str(env.get("use_bot", False)).lower()},
        "Gwx_Command": {},
        "Bot": {"bot_tx_rate_limit_seconds": "1.5"},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kw: f"<{key}{sorted(kw.items()) or ''}>")
    cmd = cls(bot)

    rec = Mock()
    cmd.logger = rec.logger

    mqtt = env.get("mqtt", {})
    wxsim = env.get("wxsim", {})

    def attach(name, mock):
        rec.attach_mock(mock, name)
        setattr(cmd, name, mock)

    attach("_get_custom_mqtt_weather_topic", Mock(side_effect=lambda loc=None: mqtt.get(loc)))
    attach("_get_custom_wxsim_source", Mock(side_effect=lambda loc=None: wxsim.get(loc)))
    if env.get("mqtt_raises"):
        attach("_mqtt_weather_line", Mock(side_effect=RuntimeError("mqtt down")))
    else:
        attach("_mqtt_weather_line", Mock(side_effect=lambda topic, ft, loc: f"MQTT[{topic},{ft},{loc}]"))
    wxsim_effect = RuntimeError("wxsim down") if env.get("wxsim_raises") else (
        lambda src, ft="default", nd=7, msg=None, loc=None: f"WXSIM[{src},{ft},{nd},{loc}]"
    )
    if cls_name == "wx":
        attach("_get_wxsim_weather_async", AsyncMock(side_effect=wxsim_effect))
    else:
        attach("_get_wxsim_weather", Mock(side_effect=wxsim_effect))
    attach("_get_companion_location", Mock(return_value=env.get("companion")))
    attach("_get_bot_location", Mock(return_value=env.get("bot")))
    attach("_coordinates_to_location_string_async", AsyncMock(return_value=env.get("display")))
    if env.get("geocode_fails"):
        zip_result, city_result = (None, None), (None, None, None)
    elif env.get("city_pair"):
        zip_result, city_result = (47.61, -122.33), (47.6, -122.3)
    else:
        zip_result, city_result = (47.61, -122.33), (47.6, -122.3, {"city": "Seattle"})
    attach("_zipcode_to_lat_lon_async", AsyncMock(return_value=zip_result))
    attach("_city_to_lat_lon_async", AsyncMock(return_value=city_result))
    attach("_send_full_alert_list", AsyncMock(return_value=True))
    if env.get("weather_raises"):
        attach("get_weather_for_location", AsyncMock(side_effect=ValueError("api down")))
    else:
        attach("get_weather_for_location", AsyncMock(return_value=env.get("weather", "WEATHER")))
    results = list(env.get("send_results", []))
    attach("send_response", AsyncMock(side_effect=lambda *a, **k: results.pop(0) if results else True))
    attach("_pace_reply", AsyncMock())
    attach("_send_multiday_forecast", AsyncMock(return_value=True))
    attach("record_execution", Mock())
    return cmd, rec


def _message(keyword, text):
    content = f"{keyword} {text}".strip() if text else keyword
    return SimpleNamespace(content=content, sender_id="u1", sender_pubkey="pk", channel="general", is_dm=False)


def _trace(cls_name, env, text):
    cmd, rec = _build(cls_name, env)
    message = _message(cmd.keywords[0], text)
    result = asyncio.run(cmd.execute(message))
    calls = []
    for c in rec.mock_calls:
        name, args, kwargs = c
        args = ["<msg>" if a is message else a for a in args]
        kwargs = {k: ("<msg>" if v is message else v) for k, v in kwargs.items()}
        calls.append(f"{name}{tuple(args)!r}{kwargs or ''}")
    return {"result": result, "calls": calls}


@pytest.mark.parametrize("cls_name", ["wx", "gwx"])
def test_execute_dispatch(cls_name):
    data = {
        f"{env_name} | {text!r}": _trace(cls_name, env, text)
        for env_name, env in ENVS.items()
        for text in (CORE_INPUTS if env_name in FAILURE_ENVS else INPUTS)
    }
    assert_golden(f"weather_execute_dispatch_{cls_name}", data)
