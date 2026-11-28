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


def test_the_old_class_name_is_the_new_class():
    from modules.commands.gwx_command import GwxCommand

    assert GlobalWxCommand is GwxCommand
    assert wx_international.GlobalWxCommand is GwxCommand


def test_the_compat_module_keeps_the_names_code_imported():
    for name in ("OPEN_METEO_TIMEOUT", "GWX_MULTIDAY_MAX_DAYS", "MI_TO_KM", "HPA_TO_MMHG",
                 "VISIBILITY_CAP_MI", "VISIBILITY_CAP_KM", "OpenMeteoProvider", "weather_emoji",
                 "WXSIMParser", "WeatherCommandMixin", "load_open_meteo_model", "BaseCommand",
                 "MeshMessage", "datetime", "timedelta", "requests", "asyncio", "math", "re"):
        assert hasattr(wx_international, name), name


@pytest.mark.parametrize(("method", "args"), [
    ("get_open_meteo_weather", (47.6, -122.3)),
    ("format_tomorrow_forecast", ({},)),
    ("format_multiday_forecast", ({},)),
])
def test_the_open_meteo_forwarders_warn_and_reach_the_provider(method, args):
    cmd = GlobalWxCommand(_bot())
    reached = Mock(return_value="ANSWER")
    setattr(cmd.provider, method, reached)

    with pytest.warns(DeprecationWarning, match=f"cmd.provider.{method}; removed in 1.4"):
        assert getattr(cmd, method)(*args) == "ANSWER"

    assert reached.call_args.args[:len(args)] == args


@pytest.mark.parametrize(("attribute", "expected"), [
    ("temperature_unit", "fahrenheit"),
    ("wind_speed_unit", "mph"),
    ("precipitation_unit", "inch"),
    ("weather_model", "best_match"),
    ("metric_distance", False),
])
def test_the_unit_and_model_properties_warn_and_read_the_provider(attribute, expected):
    cmd = GlobalWxCommand(_bot())

    with pytest.warns(DeprecationWarning, match="removed in 1.4"):
        assert getattr(cmd, attribute) == expected


def test_the_unit_setters_warn_and_reach_the_provider():
    cmd = GlobalWxCommand(_bot())

    with pytest.warns(DeprecationWarning, match="removed in 1.4"):
        cmd.temperature_unit = "celsius"
    with pytest.warns(DeprecationWarning, match="removed in 1.4"):
        cmd.wind_speed_unit = "kmh"
    with pytest.warns(DeprecationWarning, match="removed in 1.4"):
        cmd.precipitation_unit = "mm"
    with pytest.warns(DeprecationWarning, match="removed in 1.4"):
        cmd.weather_model = "ecmwf_ifs04"

    assert cmd.provider.units.temperature == "celsius"
    assert cmd.provider.units.wind_speed == "kmh"
    assert cmd.provider.units.precipitation == "mm"
    assert cmd.provider.weather_model == "ecmwf_ifs04"


def test_a_forwarder_on_another_provider_says_so():
    cmd = GlobalWxCommand(_bot({"Gwx_Command": {"weather_provider": "noaa"}}))

    with pytest.warns(DeprecationWarning):
        with pytest.raises(AttributeError, match="needs the Open-Meteo provider"):
            cmd.get_open_meteo_weather(47.6, -122.3)
