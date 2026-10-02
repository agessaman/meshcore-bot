"""Characterization: gwx's Open-Meteo forecast formatting.

Replays real api.open-meteo.com responses captured on 2026-10-01 (London,
Tokyo, Reykjavik in metric units; Phoenix, Seattle in imperial;
tests/fixtures/open_meteo) with the clock frozen. Variants change the time of
day, the length budget, the forecast type, and drop or distort current fields.
"""

import configparser
import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.characterization.golden_util import assert_golden

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "open_meteo"
UNITS = {
    "london": ("celsius", "kmh", "mm"),
    "tokyo": ("celsius", "kmh", "mm"),
    "reykjavik": ("celsius", "ms", "mm"),
    "phoenix": ("fahrenheit", "mph", "inch"),
    "seattle": ("fahrenheit", "mph", "inch"),
}
DAY = datetime(2026, 10, 1, 10, 15, 0)
NIGHT = datetime(2026, 10, 1, 21, 45, 0)
MODULE = "modules.commands.alternatives.wx_international"


def _frozen(now):
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    return _FrozenDateTime


def _gwx(units, extra_weather=None):
    from modules.commands.alternatives.wx_international import GlobalWxCommand

    temperature, wind, precipitation = units
    weather = {
        "temperature_unit": temperature,
        "wind_speed_unit": wind,
        "precipitation_unit": precipitation,
    }
    weather.update(extra_weather or {})
    config = configparser.ConfigParser()
    config.read_dict({"Weather": weather, "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.translator.translate.side_effect = lambda key, **kwargs: f"<{key}{sorted(kwargs.items()) or ''}>"
    return GlobalWxCommand(bot)


def _response(payload, ok=True):
    response = Mock()
    response.ok = ok
    response.status_code = 200 if ok else 503
    response.json.return_value = payload
    return response


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _run(cmd, data, now, budget=None, **kwargs):
    if budget is not None:
        # A message makes the command ask for its budget instead of using 130.
        cmd.get_max_message_length = Mock(return_value=budget)
        kwargs["message"] = Mock()
    get = Mock(return_value=_response(data))
    with patch(f"{MODULE}.datetime", _frozen(now)), patch(f"{MODULE}.requests.get", get):
        text = cmd.get_open_meteo_weather(data["latitude"], data["longitude"], **kwargs)
    params = get.call_args.kwargs["params"] if get.call_args else None
    logs = [(c[0], c[1][0]) for c in cmd.logger.method_calls if c[0] in ("info", "warning", "error")]
    return {"text": text, "requests": get.call_count, "params": params, "logs": logs}


def _text_and_logs(result):
    return [result["text"], result["logs"]] if result["logs"] else result["text"]


@pytest.mark.parametrize("name", sorted(UNITS))
def test_open_meteo_text(name):
    data = _load(name)
    results = {}
    for label, now in (("day", DAY), ("night", NIGHT)):
        for prefix in (0, 30, 60):
            cmd = _gwx(UNITS[name])
            results[f"default-{label}-prefix{prefix}"] = _run(cmd, data, now, location_prefix_len=prefix)
        for budget in (160, 200, 240, 300, 600):
            cmd = _gwx(UNITS[name])
            results[f"default-{label}-budget{budget}"] = _run(cmd, data, now, budget=budget, location_prefix_len=20)
        cmd = _gwx(UNITS[name])
        results[f"tomorrow-{label}"] = _run(cmd, data, now, forecast_type="tomorrow")
        for days in (3, 7, 16):
            cmd = _gwx(UNITS[name])
            results[f"multiday{days}-{label}"] = _run(cmd, data, now, forecast_type="multiday", num_days=days)
    assert_golden(f"open_meteo_gwx_{name}", results)


DISTORTIONS = {
    "no_visibility": lambda d: d["current"].pop("visibility"),
    "zero_visibility": lambda d: d["current"].__setitem__("visibility", 0),
    "feet_visibility": lambda d: d["current_units"].__setitem__("visibility", "ft"),
    "low_pressure": lambda d: d["current"].__setitem__("surface_pressure", 512.0),
    "no_pressure": lambda d: d["current"].pop("surface_pressure"),
    "no_dewpoint": lambda d: d["current"].pop("dewpoint_2m"),
    "gusty": lambda d: d["current"].update(wind_speed_10m=12.0, wind_gusts_10m=31.0),
    "calm": lambda d: d["current"].update(wind_speed_10m=1.0, wind_gusts_10m=2.0),
    "feels_cold": lambda d: d["current"].update(apparent_temperature=d["current"]["temperature_2m"] - 9),
    "wet_tomorrow": lambda d: d["daily"].update(
        precipitation_probability_max=[10, 80] + d["daily"]["precipitation_probability_max"][2:],
        precipitation_sum=[0.0, 4.2] + d["daily"]["precipitation_sum"][2:],
    ),
    "likely_dry_tomorrow": lambda d: d["daily"].update(
        precipitation_probability_max=[10, 45] + d["daily"]["precipitation_probability_max"][2:],
        precipitation_sum=[0.0, 0.0] + d["daily"]["precipitation_sum"][2:],
    ),
    "one_day": lambda d: d["daily"].update({k: v[:1] for k, v in d["daily"].items()}),
    "no_daily": lambda d: d.pop("daily"),
    "no_current": lambda d: d.pop("current"),
    "null_temp": lambda d: d["current"].__setitem__("temperature_2m", None),
    "null_highs": lambda d: d["daily"].update(temperature_2m_max=[None] * len(d["daily"]["temperature_2m_max"])),
    "null_code": lambda d: d["daily"].update(weather_code=[None] * len(d["daily"]["weather_code"])),
}


@pytest.mark.parametrize("name", ["london", "phoenix"])
def test_open_meteo_text_distorted(name):
    results = {}
    for key, distort in DISTORTIONS.items():
        data = copy.deepcopy(_load(name))
        distort(data)
        for forecast_type in ("default", "tomorrow", "multiday"):
            cmd = _gwx(UNITS[name])
            results[f"{key}-{forecast_type}"] = _text_and_logs(_run(cmd, data, DAY, forecast_type=forecast_type, num_days=5))
        cmd = _gwx(UNITS[name])
        results[f"{key}-default-budget260"] = _text_and_logs(_run(cmd, data, DAY, budget=260))
        cmd = _gwx(UNITS[name])
        results[f"{key}-default-budget600"] = _text_and_logs(_run(cmd, data, DAY, budget=600))
    # A model override, and a catalog whose pressure unit is mmHg.
    data = _load(name)
    cmd = _gwx(UNITS[name], {"weather_model": "ecmwf_ifs025"})
    results["model-override"] = _run(cmd, data, DAY)
    cmd = _gwx(UNITS[name])
    base_translate = cmd.bot.translator.translate.side_effect
    cmd.bot.translator.translate.side_effect = (
        lambda key, **kw: "mmHg" if key.endswith("gwx.pressure_unit") else base_translate(key, **kw)
    )
    results["mmhg"] = _text_and_logs(_run(cmd, data, DAY, budget=260))
    cmd = _gwx(UNITS[name])
    get = Mock(return_value=_response({}, ok=False))
    with patch(f"{MODULE}.requests.get", get):
        results["http-error"] = [
            cmd.get_open_meteo_weather(1.0, 2.0, forecast_type=t) for t in ("default", "tomorrow", "multiday")
        ]
    assert_golden(f"open_meteo_gwx_distorted_{name}", results)
