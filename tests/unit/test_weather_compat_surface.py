"""Names and hooks local plugins may rely on survive the weather refactor."""

import configparser
from types import SimpleNamespace
from unittest.mock import Mock

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
