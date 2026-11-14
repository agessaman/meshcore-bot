"""gwx leaves out missing current fields instead of inventing values or failing the reply."""

import configparser
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand

BASE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "open_meteo" / "phoenix.json").read_text())


def _reply(**current_overrides):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"temperature_unit": "fahrenheit"}, "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kw: f"<{key}{sorted(kw.items()) or ''}>"
    cmd = GlobalWxCommand(bot)
    cmd.logger = Mock()
    data = copy.deepcopy(BASE)
    for key, value in current_overrides.items():
        if value is ...:
            data["current"].pop(key, None)
        else:
            data["current"][key] = value
    response = Mock(ok=True)
    response.json.return_value = data
    with patch("modules.commands.alternatives.wx_international.requests.get", return_value=response):
        return cmd.get_open_meteo_weather(33.45, -112.07), cmd.logger


def test_ordinary_reply_has_all_parts():
    text, _ = _reply()
    assert "<commands.gwx.humidity" in text and "<commands.gwx.weather_descriptions." in text


@pytest.mark.parametrize("field", ["relative_humidity_2m", "wind_speed_10m", "wind_gusts_10m", "apparent_temperature"])
@pytest.mark.parametrize("missing", [None, ...])
def test_a_null_or_absent_optional_field_is_left_out(field, missing):
    text, logger = _reply(**{field: missing})
    assert not text.startswith("<commands.gwx.error")
    logger.error.assert_not_called()
    if field == "relative_humidity_2m":
        assert "<commands.gwx.humidity" not in text


def test_a_missing_weather_code_shows_no_condition_instead_of_clear():
    text, _ = _reply(weather_code=None)
    assert "weather_descriptions" not in text.split("|")[0]


@pytest.mark.parametrize("missing", [None, ...])
def test_no_current_temperature_is_an_error_reply_not_zero_degrees(missing):
    text, logger = _reply(temperature_2m=missing)
    assert text == "<commands.gwx.error_fetching>"
    assert "0°F" not in text
    logger.warning.assert_called_once_with("Open-Meteo response has no current temperature")


@pytest.mark.parametrize("missing", [None, ...])
def test_a_missing_wind_direction_is_not_reported_as_north(missing):
    text, _ = _reply(wind_direction_10m=missing, wind_speed_10m=12)
    assert "wind_directions" not in text
    assert "12" in text
