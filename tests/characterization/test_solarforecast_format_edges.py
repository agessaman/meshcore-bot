"""Characterization of solarforecast thresholds, sparse data and malformed values.

Use captured Seattle/London energy totals and copied days, replacing the power
samples where their order, threshold or clock hour is the behavior under test.
"""

import copy
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests.characterization.golden_util import assert_golden
from tests.characterization.test_solarforecast_format import MODULE, _cmd, _days, _four_days, _frozen, _load


def _run(cmd, result, now, panel_watts=400.0):
    with patch(f"{MODULE}.datetime", _frozen(now)):
        try:
            text = cmd._format_forecast(copy.deepcopy(result), panel_watts)
        except Exception as e:  # preserve both the escaping exception and earlier logs
            text = f"raised {type(e).__name__}: {e}"
    logs = [(c[0], c[1][0]) for c in cmd.logger.method_calls if c[0] in ("debug", "info", "warning", "error")]
    return {"text": text, "logs": logs}


def _base(name):
    data = _load(name)
    result = _four_days(data["result"])
    now = datetime.strptime(_days(result)[0], "%Y-%m-%d") + timedelta(hours=5)
    return data["timezone"], result, now


def _samples(base, samples, keep_days=None):
    result = copy.deepcopy(base)
    days = _days(base)
    result["watts"] = {f"{days[day]} {time}": power for day, time, power in samples}
    if keep_days is not None:
        result["watt_hours_day"] = {days[i]: base["watt_hours_day"][days[i]] for i in keep_days}
    return result


@pytest.mark.parametrize("name", ["seattle", "london"])
def test_solarforecast_power_edges(name):
    tz, base, now = _base(name)
    results = {}
    tied = [(0, "15:00:00", 100), (0, "10:00:00", 100), (1, "12:00:00", 100)]
    for label, samples in (
        ("late-first", tied),
        ("early-first", [tied[1], tied[0], tied[2]]),
        ("tomorrow-first", tied[::-1]),
    ):
        results[f"tied-{label}"] = _run(_cmd(tz), _samples(base, samples), now)
    for label, samples, panel, keep in (
        ("today-exact-0.1", [(0, "10:00:00", 0.1), (0, "11:00:00", 0.099)], 10.0, [0]),
        ("today-exact-one-percent", [(0, "10:00:00", 4.0), (0, "11:00:00", 3.99)], 400.0, [0]),
        ("tomorrow-exact-0.1", [(1, "10:00:00", 0.1), (1, "11:00:00", 0.099)], 5.0, [1]),
        ("tomorrow-exact-one-percent", [(1, "10:00:00", 4.0), (1, "11:00:00", 3.99)], 400.0, [1]),
        ("today-same-hour", [(0, "10:00:00", 20), (0, "10:15:00", 30), (0, "10:45:00", 40)], 400.0, [0]),
        ("tomorrow-same-hour", [(1, "10:00:00", 20), (1, "10:15:00", 30), (1, "10:45:00", 40)], 400.0, [1]),
    ):
        results[label] = _run(_cmd(tz), _samples(base, samples, keep), now, panel)
    for day in (0, 1):
        for position in ("first", "last"):
            samples = [(day, "10:00:00", float("nan")), (day, "11:00:00", 40)]
            if position == "last":
                samples.reverse()
            results[f"nan-power-day{day}-{position}"] = _run(_cmd(tz), _samples(base, samples, [day]), now)
        result = _samples(base, [(day, "10:00:00", 40)], [day])
        results[f"nan-panel-day{day}"] = _run(_cmd(tz), result, now, float("nan"))
    result = copy.deepcopy(base)
    result["watts"]["not a time"] = 50
    results["invalid-timestamp-debug"] = _run(_cmd(tz), result, now)
    assert_golden(f"solarforecast_edges_power_{name}", results)


@pytest.mark.parametrize("name", ["seattle", "london"])
def test_solarforecast_sparse_edges(name):
    tz, base, now = _base(name)
    days = _days(base)
    results = {}
    for label, keep in (
        ("neither-today-nor-tomorrow", [2, 3]),
        ("day3-without-day2", [0, 1, 3]),
        ("only-day2", [2]),
    ):
        result = copy.deepcopy(base)
        result["watt_hours_day"] = {days[i]: base["watt_hours_day"][days[i]] for i in keep}
        result["watts"] = {ts: power for ts, power in base["watts"].items() if ts[:10] in result["watt_hours_day"]}
        results[label] = _run(_cmd(tz), result, now)
    assert_golden(f"solarforecast_edges_sparse_{name}", results)


@pytest.mark.parametrize("name", ["seattle", "london"])
def test_solarforecast_malformed_edges(name):
    tz, base, now = _base(name)
    results = {}
    result = copy.deepcopy(base)
    result["watt_hours_day"] = _days(base)
    results["watt-hours-day-list"] = _run(_cmd(tz), result, now)
    result = copy.deepcopy(base)
    result["watts"] = None
    results["watts-none"] = _run(_cmd(tz), result, now)
    for day in range(4):
        result = _samples(base, [(i, "10:00:00", None if i == day else 40) for i in range(4)])
        results[f"none-power-day{day}"] = _run(_cmd(tz), result, now)
    result = _samples(base, [(1, "10:00:00", None)], [1])
    results["none-power-tomorrow-first"] = _run(_cmd(tz), result, now)
    assert_golden(f"solarforecast_edges_malformed_{name}", results)
