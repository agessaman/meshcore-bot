"""gwx names the current period and picks its clear-sky emoji from the sun at the location."""

import configparser
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.i18n import Translator

MODULE = "modules.commands.alternatives.wx_international"
SEATTLE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "open_meteo" / "seattle.json").read_text())


def _reply(time, is_day=..., code=0, language="en", budget=None):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"temperature_unit": "fahrenheit"}, "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator = Translator(language)
    cmd = GlobalWxCommand(bot)
    data = copy.deepcopy(SEATTLE)
    data["current"]["time"] = time
    data["current"]["weather_code"] = code
    if is_day is not ...:
        data["current"]["is_day"] = is_day
    response = Mock(ok=True)
    response.json.return_value = data
    kwargs = {}
    if budget is not None:
        cmd.get_max_message_length = Mock(return_value=budget)
        kwargs["message"] = Mock()
    with patch(f"{MODULE}.requests.get", return_value=response) as get:
        text = cmd.get_open_meteo_weather(47.6, -122.3, **kwargs)
    return text, get.call_args.kwargs["params"]


def test_is_day_is_requested():
    _, params = _reply("2026-10-02T05:58", is_day=0)
    assert "is_day" in params["current"].split(",")


@pytest.mark.parametrize(("time", "is_day", "label", "emoji"), [
    ("2026-10-02T05:58", 0, "Overnight", "🌙"),
    ("2026-10-02T00:30", 0, "Overnight", "🌙"),
    ("2026-10-02T21:00", 0, "Tonight", "🌙"),
    ("2026-10-02T19:30", 0, "Tonight", "🌙"),
    ("2026-10-02T07:30", 1, "Today", "☀️"),
    # is_day wins over the hour: dark at 06:30 in October, still light at 18:30 in June.
    ("2026-10-02T06:30", 0, "Overnight", "🌙"),
    ("2026-06-21T18:30", 1, "Today", "☀️"),
])
def test_label_and_emoji_follow_is_day(time, is_day, label, emoji):
    text, _ = _reply(time, is_day=is_day)
    assert text.startswith(f"{label}: {emoji}Clear ")


@pytest.mark.parametrize(("time", "label", "emoji"), [
    ("2026-10-02T05:58", "Overnight", "🌙"),
    ("2026-10-02T12:00", "Today", "☀️"),
    ("2026-10-02T20:00", "Tonight", "🌙"),
])
def test_without_is_day_the_local_hour_decides(time, label, emoji):
    text, _ = _reply(time)
    assert text.startswith(f"{label}: {emoji}Clear ")


@pytest.mark.parametrize("code", [2, 3, 45, 61])
def test_cloud_and_rain_emoji_are_the_same_at_night(code):
    day, _ = _reply("2026-10-02T12:00", is_day=1, code=code)
    night, _ = _reply("2026-10-02T21:00", is_day=0, code=code)
    assert day.split(":", 1)[1].split()[0] == night.split(":", 1)[1].split()[0]


def test_overnight_is_translated():
    text, _ = _reply("2026-10-02T05:58", is_day=0, language="de")
    assert text.startswith("Nachts: 🌙")


def test_tomorrow_names_its_weather_when_it_fits():
    text, _ = _reply("2026-10-02T05:58", is_day=0, budget=300)
    tomorrow = text.split(" | ")[-1]
    code = SEATTLE["daily"]["weather_code"][1]
    description = Translator("en").translate(f"commands.gwx.weather_descriptions.{code}")
    assert tomorrow.startswith("Tomorrow: ") and f"{description} H:" in tomorrow


def test_tomorrow_falls_back_to_the_emoji_when_the_description_does_not_fit():
    emoji = GlobalWxCommand._get_weather_emoji(None, SEATTLE["daily"]["weather_code"][1])
    tails = {_reply("2026-10-02T05:58", is_day=0, budget=b)[0].split(" | ")[-1] for b in range(100, 200)}
    # Some budget has room for tomorrow but not its description.
    assert any(t.startswith(f"Tomorrow: {emoji} H:") for t in tails)
