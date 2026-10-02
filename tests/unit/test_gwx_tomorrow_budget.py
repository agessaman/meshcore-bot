"""gwx's tomorrow forecast fits its byte budget, leaving out the least important details first."""

import configparser
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.i18n import Translator

MODULE = "modules.commands.alternatives.wx_international"
REYKJAVIK = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "open_meteo" / "reykjavik.json").read_text())


def _tomorrow(budget, language="ru"):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"temperature_unit": "celsius", "wind_speed_unit": "ms", "precipitation_unit": "mm"},
                      "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator = Translator(language)
    cmd = GlobalWxCommand(bot)
    cmd.get_max_message_length = Mock(return_value=budget)
    data = copy.deepcopy(REYKJAVIK)
    data["daily"]["wind_gusts_10m_max"][1] = data["daily"]["wind_speed_10m_max"][1] + 10
    data["daily"]["precipitation_probability_max"][1] = 80
    data["daily"]["precipitation_sum"][1] = 4.2
    response = Mock(ok=True)
    response.json.return_value = data
    with patch(f"{MODULE}.requests.get", return_value=response):
        return cmd.get_open_meteo_weather(64.1, -21.9, forecast_type="tomorrow", message=Mock())


def test_with_room_every_detail_is_kept():
    text = _tomorrow(400)
    assert "🌦️80% 4.20mm" in text


@pytest.mark.parametrize("budget", range(30, 140, 2))
def test_tomorrow_fits_whenever_its_shortest_form_does(budget):
    text = _tomorrow(budget)
    # Over budget only when even the shortest form (no details at all) does not fit.
    assert len(text.encode()) <= budget or "🌦️" not in text


def test_details_go_least_important_first():
    full = _tomorrow(400)
    trimmed = _tomorrow(len(full.encode()) - 1)
    assert "🌦️80%" in trimmed and "4.20mm" not in trimmed


def test_the_shortest_form_keeps_only_the_high_and_low():
    shortest, full = _tomorrow(1), _tomorrow(400)
    assert "🌦️" not in shortest and len(shortest.encode()) < len(full.encode())
    assert shortest.split(": ", 1)[0] == full.split(": ", 1)[0]  # still names tomorrow
