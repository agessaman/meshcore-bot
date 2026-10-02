"""Characterization of Open-Meteo request failures and formatter edge branches.

Distort copies of the captured London/Phoenix responses. Width-boundary cases
omit optional current conditions so their inclusion cannot move the boundary.
"""

import copy
from unittest.mock import Mock, patch

import pytest
import requests

from tests.characterization.golden_util import assert_golden
from tests.characterization.test_open_meteo_format import DAY, MODULE, UNITS, _frozen, _gwx, _load, _response


def _run(cmd, data, forecast_type="default", *, budget=None, failure=None, num_days=7):
    response = _response(data, ok=failure != "http")
    get = Mock(return_value=response)
    if failure == "timeout":
        get.side_effect = requests.exceptions.Timeout("edge timeout")
    elif failure == "connection":
        get.side_effect = requests.exceptions.ConnectionError("edge connection lost")
    elif failure == "json":
        response.json.side_effect = ValueError("edge invalid JSON")
    kwargs = {}
    if budget is None and forecast_type == "tomorrow":
        budget = 600  # the details, not the trimming, are what these cases check
    if budget is not None:
        cmd.get_max_message_length = Mock(return_value=budget)
        kwargs["message"] = Mock()
    with patch(f"{MODULE}.datetime", _frozen(DAY)), patch(f"{MODULE}.requests.get", get):
        try:
            text = cmd.get_open_meteo_weather(
                data["latitude"], data["longitude"], forecast_type=forecast_type, num_days=num_days, **kwargs
            )
        except Exception as e:  # pin exceptions escaping the request wrapper, too
            text = f"raised {type(e).__name__}: {e}"
    logs = [(c[0], c[1][0]) for c in cmd.logger.method_calls if c[0] in ("info", "warning", "error")]
    return {"text": text, "requests": get.call_count, "params": get.call_args.kwargs["params"], "logs": logs}


@pytest.mark.parametrize("name", ["london", "phoenix"])
def test_open_meteo_request_edges(name):
    results = {}
    for label, extra in (
        ("no-override", {}),
        ("model-omitted", {"weather_model": ""}),
        ("model-override", {"weather_model": "ecmwf_ifs025"}),
    ):
        for forecast_type in ("default", "tomorrow", "multiday"):
            for failure in (None, "timeout", "connection", "json", "http"):
                key = f"{label}-{forecast_type}-{failure or 'success'}"
                results[key] = _run(_gwx(UNITS[name], extra), _load(name), forecast_type, failure=failure, num_days=20)
    assert_golden(f"open_meteo_gwx_edges_requests_{name}", results)


def _wet_data(name):
    data = copy.deepcopy(_load(name))
    data["daily"]["precipitation_probability_max"][1] = 80
    data["daily"]["precipitation_sum"][1] = 4.2
    return data


def _array_variants(data, fields):
    for field in fields:
        for shape in ("missing", "empty", "one"):
            variant = copy.deepcopy(data)
            if shape == "missing":
                variant["daily"].pop(field)
            else:
                variant["daily"][field] = variant["daily"][field][:0 if shape == "empty" else 1]
            yield f"{field}-{shape}", variant


@pytest.mark.parametrize("name", ["london", "phoenix"])
def test_open_meteo_daily_tail_arrays(name):
    results = {}
    data = _wet_data(name)
    for key, variant in _array_variants(data, ("precipitation_probability_max", "precipitation_sum")):
        results[key] = _run(_gwx(UNITS[name]), variant, budget=600)
    for field in ("precipitation_probability_max", "precipitation_sum"):
        variant = copy.deepcopy(data)
        variant["daily"][field][1] = None
        results[f"{field}-null"] = _run(_gwx(UNITS[name]), variant, budget=600)
    assert_golden(f"open_meteo_gwx_edges_daily_arrays_{name}", results)


@pytest.mark.parametrize("name", ["london", "phoenix"])
def test_open_meteo_daily_tail_width(name):
    data = _wet_data(name)
    for field in ("dewpoint_2m", "visibility", "surface_pressure"):
        data["current"].pop(field)
    # With the key-returning translator, tomorrow's total width is 350/320;
    # adding precipitation makes it 368/338. These budgets pin <= at equality.
    tomorrow_budget, precip_budget = (360, 373) if name == "london" else (330, 343)
    results = {}
    for segment, boundary in (("tomorrow", tomorrow_budget), ("precipitation", precip_budget)):
        for offset in (-1, 0, 1):
            budget = boundary + offset
            result = _run(_gwx(UNITS[name]), data, budget=budget)
            results[f"{segment}-boundary{offset:+d}"] = {"budget": budget, **result}
    assert_golden(f"open_meteo_gwx_edges_daily_width_{name}", results)


@pytest.mark.parametrize("name", ["london", "phoenix"])
def test_open_meteo_tomorrow_edges(name):
    data = _wet_data(name)
    data["daily"]["wind_speed_10m_max"][1] = 10
    data["daily"]["wind_gusts_10m_max"][1] = 20
    fields = ("wind_speed_10m_max", "wind_gusts_10m_max", "precipitation_probability_max", "precipitation_sum")
    results = {key: _run(_gwx(UNITS[name]), variant, "tomorrow") for key, variant in _array_variants(data, fields)}
    for key, field, value in (
        ("wind-below3", "wind_speed_10m_max", 2),
        ("gust-equal-wind-plus3", "wind_gusts_10m_max", 13),
        ("gust-below-wind-plus3", "wind_gusts_10m_max", 12),
        ("null-high", "temperature_2m_max", None),
        ("null-low", "temperature_2m_min", None),
        ("null-probability", "precipitation_probability_max", None),
    ):
        variant = copy.deepcopy(data)
        variant["daily"][field][1] = value
        results[key] = _run(_gwx(UNITS[name]), variant, "tomorrow")
    assert_golden(f"open_meteo_gwx_edges_tomorrow_{name}", results)


@pytest.mark.parametrize("name", ["london", "phoenix"])
def test_open_meteo_multiday_edges(name):
    data = _load(name)
    results = {}
    for length in (19, 20, 21):
        cmd = _gwx(UNITS[name])
        translate = cmd.bot.translator.translate.side_effect
        cmd.bot.translator.translate.side_effect = (
            lambda key, **kw: "<" + "d" * (length - 2) + ">"
            if key.startswith("commands.gwx.weather_descriptions.") else translate(key, **kw)
        )
        results[f"description-length{length}"] = _run(cmd, data, "multiday", num_days=2)
    variant = copy.deepcopy(data)
    variant["daily"]["weather_code"] = variant["daily"]["weather_code"][:2]
    cmd = _gwx(UNITS[name])
    translate = cmd.bot.translator.translate.side_effect
    cmd.bot.translator.translate.side_effect = (
        lambda key, **kw: f"<WMO {key.rsplit('.', 1)[1]}>"
        if key.startswith("commands.gwx.weather_descriptions.") else translate(key, **kw)
    )
    results["short-weather-codes"] = _run(cmd, variant, "multiday", num_days=4)
    cmd = _gwx(UNITS[name])
    translate = cmd.bot.translator.translate.side_effect
    # A missing catalog entry returns the raw key. Friday is tomorrow at DAY.
    cmd.bot.translator.translate.side_effect = (
        lambda key, **kw: key if key == "commands.gwx.day_abbrev.Friday" else translate(key, **kw)
    )
    results["missing-friday-translation"] = _run(cmd, data, "multiday", num_days=2)
    for days in (2, 20):
        results[f"num-days{days}"] = _run(_gwx(UNITS[name]), data, "multiday", num_days=days)
    for field in ("temperature_2m_max", "temperature_2m_min"):
        for position in ("middle", "end"):
            variant = copy.deepcopy(data)
            # Restrict to five complete captured days before inserting a null.
            variant["daily"] = {key: values[:5] for key, values in variant["daily"].items()}
            variant["daily"][field][3 if position == "middle" else 4] = None
            results[f"{field}-null-{position}"] = _run(_gwx(UNITS[name]), variant, "multiday", num_days=7)
    assert_golden(f"open_meteo_gwx_edges_multiday_{name}", results)
