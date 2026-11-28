"""Per-command weather settings: provider lookup, WXSIM, the coverage label, the settings view.

These pin fixes from the review of gwx becoming a normal command.
"""

import configparser
from unittest.mock import Mock

import pytest

from modules.commands.gwx_command import GwxCommand
from modules.commands.wx_command import WxCommand
from modules.config_schema import SEVERITY_WARNING, validate_config_keys
from modules.settings_schema import build_plugin_settings_view
from modules.weather_providers.base import ForecastReply


def _command(cls, sections, defaults=None):
    config = configparser.ConfigParser(defaults=defaults)
    config.read_dict(sections)
    bot = Mock()
    bot.config = config
    return cls(bot)


# --- provider lookup --------------------------------------------------------


def test_a_broken_shared_provider_is_not_read_when_the_command_sets_its_own():
    cmd = _command(GwxCommand, {
        "Weather": {"weather_provider": "%(undefined)s"},
        "Gwx_Command": {"weather_provider": "eccc"},
    })

    assert cmd.provider_name == "eccc"


def test_a_broken_shared_provider_is_not_read_when_gwx_has_a_default():
    cmd = _command(GwxCommand, {"Weather": {"weather_provider": "%(undefined)s"}})

    assert cmd.provider_name == "openmeteo"


def test_a_default_section_provider_is_not_a_command_override():
    cmd = _command(GwxCommand, {"Gwx_Command": {"enabled": "true"}}, defaults={"weather_provider": "eccc"})

    assert cmd.provider_name == "openmeteo"


def test_a_command_section_can_still_set_the_provider_beside_a_default():
    cmd = _command(
        GwxCommand, {"Gwx_Command": {"weather_provider": "metno"}}, defaults={"weather_provider": "eccc"}
    )

    assert cmd.provider_name == "metno"


def test_a_default_section_unit_does_not_override_the_weather_section():
    cmd = _command(
        GwxCommand,
        {"Weather": {"temperature_unit": "celsius"}, "Gwx_Command": {"enabled": "true"}},
        defaults={"temperature_unit": "fahrenheit"},
    )

    assert cmd.provider.units.temperature == "celsius"


# --- WXSIM ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["custom.wxsim.cabin", "custom.wxsim.default"])
def test_the_wxsim_lookup_does_not_log_the_station_url(key):
    cmd = _command(GwxCommand, {"Weather": {key: "https://w.test/plaintext.txt?token=SECRET"}})
    cmd.logger = Mock()

    location = None if key.endswith("default") else "cabin"
    assert cmd._get_custom_wxsim_source(location) == "https://w.test/plaintext.txt?token=SECRET"
    logged = " ".join(str(call) for call in cmd.logger.method_calls)
    assert "SECRET" not in logged


# --- the coverage fallback keeps a global label -----------------------------


def test_the_coverage_fallback_keeps_a_label_the_global_lookup_found():
    cmd = _command(GwxCommand, {
        "Weather": {"openmeteo_fallback": "true", "always_show_location": "true"},
        "Gwx_Command": {"weather_provider": "eccc"},
    })
    cmd._locate_for_forecast = Mock(return_value=(None, 35.67, 139.65, "Tokyo, JP: "))
    cmd.provider.covers = Mock(return_value=False)
    cmd.city_to_lat_lon = Mock(side_effect=AssertionError("looked the place up again"))
    cmd.geocode_location = Mock(side_effect=AssertionError("looked the place up again"))
    cmd._forecast_or_error = Mock(return_value=ForecastReply(text="Sunny 20C", ok=True))

    reply = cmd._forecast_for_location("35.67,139.65", "city")

    assert reply == "Tokyo, JP: Sunny 20C"


# --- settings view and validation -------------------------------------------


def _view_entry(config, name):
    return next(e for e in build_plugin_settings_view(config) if e["name"] == name)


@pytest.mark.parametrize("name, section", [("wx", "Wx_Command"), ("gwx", "Gwx_Command")])
def test_the_settings_view_shows_each_commands_own_overrides(name, section):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"weather_provider": "noaa"},
        section: {"weather_provider": "eccc", "temperature_unit": "celsius",
                  "wind_speed_unit": "kmh", "precipitation_unit": "mm"},
    })

    entry = _view_entry(config, name)

    own = {f["key"]: f["value"] for f in entry["fields"] if not f.get("section")}
    assert own == {"weather_provider": "eccc", "temperature_unit": "celsius",
                   "wind_speed_unit": "kmh", "precipitation_unit": "mm", "channels": own.get("channels")}
    shared = {f["key"]: f["value"] for f in entry["fields"] if f.get("section") == "Weather"}
    assert shared["weather_provider"] == "noaa"


def test_the_settings_view_never_sends_a_default_section_token_raw():
    config = configparser.ConfigParser(defaults={"meteoalarm_token": "SECRET"})
    config.read_dict({"Weather": {}, "Wx_Command": {"enabled": "true"}, "Gwx_Command": {"enabled": "true"}})

    for name in ("wx", "gwx"):
        assert "meteoalarm_token" not in _view_entry(config, name)["values"]


@pytest.mark.parametrize("section", ["Wx_Command", "Gwx_Command"])
def test_validation_knows_the_per_command_weather_keys(section):
    config = configparser.ConfigParser()
    config.read_dict({section: {"weather_provider": "eccc", "temperature_unit": "celsius",
                                "wind_speed_unit": "kmh", "precipitation_unit": "mm"}})

    warnings = [m for sev, m in validate_config_keys(config) if sev == SEVERITY_WARNING]

    assert not [m for m in warnings if f"[{section}]" in m]


def test_wx_and_gwx_keep_separate_command_classes():
    assert GwxCommand.default_weather_provider == "openmeteo"
    assert WxCommand.default_weather_provider is None


# --- Nd with every provider -------------------------------------------------


@pytest.mark.parametrize("provider, cap", [("metno", 9), ("eccc", 6), ("openmeteo", 16)])
def test_nd_up_to_16_is_an_option_and_the_provider_gets_what_it_has(provider, cap):
    cmd = _command(WxCommand, {"Weather": {"weather_provider": provider}})

    rest, forecast_type, num_days = cmd._parse_forecast_suffix(["Oslo", "12d"], 16, allow_hourly=True)
    assert (rest, forecast_type, num_days) == (["Oslo"], "multiday", 12)

    cmd.provider.forecast = Mock(return_value=ForecastReply(text="days"))
    cmd._forecast_or_error(cmd.provider, 59.9, 10.7, "multiday", 12, 0, None)
    assert cmd.provider.forecast.call_args.args[3] == min(12, cap)
