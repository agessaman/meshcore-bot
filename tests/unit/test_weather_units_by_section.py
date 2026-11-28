"""Units follow one rule for every weather command and provider.

The command's own section ([Wx_Command], [Gwx_Command]) wins when it sets a
unit, then [Weather]. Open-Meteo used to read [Weather] only, and gwx used to
read wx's [Wx_Command] overrides on the other providers.
"""

import configparser
from unittest.mock import Mock

import pytest

from modules.commands.gwx_command import GwxCommand
from modules.commands.wx_command import WxCommand


def _command(cls, sections):
    config = configparser.ConfigParser()
    config.read_dict(sections)
    bot = Mock()
    bot.config = config
    return cls(bot)


@pytest.mark.parametrize("provider", ["noaa", "openmeteo", "metno", "eccc"])
def test_wx_section_overrides_the_shared_units(provider):
    cmd = _command(WxCommand, {
        "Weather": {"weather_provider": provider, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"},
        "Wx_Command": {"temperature_unit": "celsius", "wind_speed_unit": "kmh"},
    })

    assert (cmd.provider.units.temperature, cmd.provider.units.wind_speed) == ("celsius", "kmh")


@pytest.mark.parametrize("provider", ["openmeteo", "metno"])
def test_gwx_reads_its_own_section_not_wx_s(provider):
    cmd = _command(GwxCommand, {
        "Weather": {"temperature_unit": "fahrenheit"},
        "Wx_Command": {"temperature_unit": "celsius"},
        "Gwx_Command": {"weather_provider": provider, "wind_speed_unit": "kn"},
    })

    assert cmd.provider.units.temperature == "fahrenheit"
    assert cmd.provider.units.wind_speed == "kn"


def test_without_overrides_the_shared_units_apply():
    cmd = _command(GwxCommand, {"Weather": {"temperature_unit": "celsius", "precipitation_unit": "mm"}})

    assert (cmd.provider.units.temperature, cmd.provider.units.precipitation) == ("celsius", "mm")


def test_an_invalid_shared_unit_still_warns_on_open_meteo():
    cmd = _command(WxCommand, {"Weather": {"weather_provider": "openmeteo", "temperature_unit": "kelvin"}})

    assert cmd.provider.units.temperature == "fahrenheit"
    # BaseCommand logs through bot.logger, a Mock here.
    warnings = [str(call.args[0]) for call in cmd.bot.logger.warning.call_args_list]
    assert "Invalid temperature_unit 'kelvin', using 'fahrenheit'" in warnings
