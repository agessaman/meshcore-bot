"""Characterization: solarforecast's reply formatting (_format_forecast).

Replays real api.forecast.solar responses captured on 2026-10-01 (Seattle,
Phoenix, London; tests/fixtures/forecast_solar) with the clock frozen at times
before, during and after the forecast days. The free tier returns two days, so
a 4-day variant copies the second day forward to reach the later-day branches;
other variants change the timestamp format, panel size and location label, and
drop or distort fields.
"""

import configparser
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.characterization.golden_util import assert_golden

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "forecast_solar"
MODULE = "modules.commands.solarforecast_command"


def _frozen(naive_local):
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return naive_local
            if hasattr(tz, "localize"):
                return tz.localize(naive_local)
            return naive_local.replace(tzinfo=tz)

    return _FrozenDateTime


def _cmd(timezone):
    from modules.commands.solarforecast_command import SolarforecastCommand

    config = configparser.ConfigParser()
    config.read_dict({"Bot": {"timezone": timezone}, "Solarforecast_Command": {}, "Weather": {}})
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.translator.translate.side_effect = lambda key, **kwargs: f"<{key}{sorted(kwargs.items()) or ''}>"
    cmd = SolarforecastCommand(bot)
    cmd.logger = Mock()
    return cmd


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _days(result):
    return sorted(result["watt_hours_day"])


def _four_days(result):
    """Copy the last captured day forward twice so Day+2 and Day+3 exist."""
    result = copy.deepcopy(result)
    last = _days(result)[-1]
    for offset in (1, 2):
        new_day = (datetime.strptime(last, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")
        for ts, w in list(result["watts"].items()):
            if ts.startswith(last):
                result["watts"][new_day + ts[10:]] = w * (1 + offset / 10)
        result["watt_hours_day"][new_day] = result["watt_hours_day"][last] * (1 + offset / 10)
    return result


def _shift_timestamps(result, suffix):
    result = copy.deepcopy(result)
    result["watts"] = {ts.replace(" ", "T") + suffix: w for ts, w in result["watts"].items()}
    return result


def _run(cmd, result, now, panel_watts=400.0, location_name=""):
    with patch(f"{MODULE}.datetime", _frozen(now)):
        text = cmd._format_forecast(copy.deepcopy(result), panel_watts, location_name)
    logs = [(c[0], c[1][0]) for c in cmd.logger.method_calls if c[0] in ("info", "warning", "error")]
    return [text, logs] if logs else text


def _times(result):
    first = datetime.strptime(_days(result)[0], "%Y-%m-%d")
    return {
        "eve-before": first - timedelta(hours=4),
        "d1-0500": first + timedelta(hours=5),
        "d1-1030": first + timedelta(hours=10, minutes=30),
        "d1-1330": first + timedelta(hours=13, minutes=30),
        "d1-2100": first + timedelta(hours=21),
        "d2-0900": first + timedelta(days=1, hours=9),
        "d2-2200": first + timedelta(days=1, hours=22),
    }


@pytest.mark.parametrize("name", ["seattle", "phoenix", "london"])
def test_solarforecast_text(name):
    data = _load(name)
    tz = data["timezone"]
    variants = {"captured": data["result"], "four-days": _four_days(data["result"])}
    results = {}
    for vname, result in variants.items():
        for tname, now in _times(result).items():
            results[f"{vname}-{tname}"] = _run(_cmd(tz), result, now)
    four = variants["four-days"]
    noon = _times(four)["d1-1030"]
    for watts in (0.0, 5.0, 100.0, 1000.0, 6500.0):
        results[f"panel{watts:g}"] = _run(_cmd(tz), four, noon, panel_watts=watts)
    for label in ("Seattle, WA", "Llanfairpwllgwyngyll, Isle of Anglesey, Wales"):
        results[f"label-{label[:8]}"] = _run(_cmd(tz), four, noon, location_name=label)
    assert_golden(f"solarforecast_{name}", results)


def _distortions(result):
    days = _days(result)
    first_ts = sorted(result["watts"])[0]

    def drop_today(r):
        r["watt_hours_day"].pop(days[0])

    def none_power(r):
        r["watts"][sorted(r["watts"])[5]] = None

    def bad_ts(r):
        r["watts"]["not a time"] = 50

    def tz_mix(r):
        w = r["watts"].pop(first_ts)
        r["watts"][first_ts.replace(" ", "T") + "+02:00"] = w

    return {
        "z-suffix": _shift_timestamps(result, "Z"),
        "offset-suffix": _shift_timestamps(result, "-07:00"),
        "no-watts": {**copy.deepcopy(result), "watts": {}},
        "no-days": {**copy.deepcopy(result), "watt_hours_day": {}},
        "drop-today": drop_today,
        "none-power": none_power,
        "bad-timestamp": bad_ts,
        "one-offset-timestamp": tz_mix,
        "zero-watts": {**copy.deepcopy(result), "watts": dict.fromkeys(result["watts"], 0)},
    }


@pytest.mark.parametrize("name", ["seattle", "london"])
def test_solarforecast_text_distorted(name):
    data = _load(name)
    tz = data["timezone"]
    base = _four_days(data["result"])
    results = {}
    for key, distortion in _distortions(base).items():
        if callable(distortion):
            result = copy.deepcopy(base)
            distortion(result)
        else:
            result = distortion
        for tname in ("eve-before", "d1-1030", "d1-2100", "d2-0900"):
            now = _times(base)[tname]
            try:
                results[f"{key}-{tname}"] = _run(_cmd(tz), result, now)
            except Exception as e:  # pin the exception that escapes, too
                results[f"{key}-{tname}"] = f"raised {type(e).__name__}: {e}"
    results["utc-config"] = _run(_cmd("UTC"), base, _times(base)["d1-1030"])
    assert_golden(f"solarforecast_distorted_{name}", results)
