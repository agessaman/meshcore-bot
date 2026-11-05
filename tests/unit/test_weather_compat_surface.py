"""Names and hooks local plugins may rely on survive the weather refactor."""

import configparser
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import modules.commands.alternatives.wx_international as wx_international
import modules.commands.wx_command as wx_command
from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.solarforecast_command import SolarforecastCommand
from modules.commands.wx_command import WxCommand


def _bot(sections=None):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}, **(sections or {})})
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    return bot


def test_wx_keeps_its_attributes():
    wx = WxCommand(_bot())
    assert (wx.forecast_duration, wx.num_wx_alerts, wx.use_metric, wx.zulu_time) == (3, 2, False, False)
    assert wx.geolocator is not None


def test_gwx_and_solarforecast_keep_geolocator():
    assert GlobalWxCommand(_bot()).geolocator is not None
    assert SolarforecastCommand(_bot()).geolocator is not None


def test_availability_flags_still_exist():
    assert wx_command.WX_INTERNATIONAL_AVAILABLE is True
    assert wx_command.WXSIM_PARSER_AVAILABLE is True
    assert wx_international.WXSIM_PARSER_AVAILABLE is True


def test_mqtt_error_text_can_be_overridden():
    class LocalWx(WxCommand):
        def _mqtt_weather_error_key(self, err):
            return f"local:{err}"

    wx = LocalWx(_bot())
    wx.bot.mqtt_weather_cache = None  # no subscriber -> "no_cache"
    assert wx._mqtt_weather_line("weather/topic", "default", None) == "local:no_cache"


def test_solarforecast_honours_its_enabled_setting():
    cmd = SolarforecastCommand(_bot({"Solarforecast_Command": {"enabled": "false"}}))
    message = SimpleNamespace(content="sf Seattle", channel="general", is_dm=True, sender_id="u", sender_pubkey="pk")
    assert cmd.can_execute(message) is False


@pytest.mark.parametrize("cls", [WxCommand, GlobalWxCommand])
@pytest.mark.parametrize(("pubkey", "rows", "logged"), [
    (None, [], "No sender_pubkey in message for companion location lookup"),
    ("ab" * 16, [], "No location found in database for pubkey " + "ab" * 8 + "..."),
    ("ab" * 16, [{"latitude": 47.5, "longitude": -122.25}],
     "Found companion location: 47.5, -122.25 for pubkey " + "ab" * 8 + "..."),
])
def test_wx_and_gwx_log_the_companion_lookup_as_on_dev(cls, pubkey, rows, logged):
    bot = _bot()
    bot.db_manager.execute_query = Mock(return_value=rows)
    cmd = cls(bot)
    cmd.logger = Mock()
    cmd._get_companion_location(SimpleNamespace(sender_pubkey=pubkey))
    cmd.logger.debug.assert_any_call(logged)


def test_rain_keeps_its_quiet_companion_lookup():
    from modules.commands.rain_command import RainCommand

    bot = _bot()
    bot.db_manager.execute_query = Mock(return_value=[])
    rain = RainCommand(bot)
    rain.logger = Mock()
    rain._get_companion_location(SimpleNamespace(sender_pubkey="ab" * 16))
    rain.logger.debug.assert_not_called()
