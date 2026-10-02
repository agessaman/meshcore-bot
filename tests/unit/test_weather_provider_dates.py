"""wx and gwx date their forecasts from the provider's own times, not the bot's clock.

The bot often runs in another time zone (UTC on a server) than the location it
forecasts, and Open-Meteo/NOAA both report location-local times.
"""

import configparser
import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEATTLE = json.loads((FIXTURES / "noaa" / "seattle.json").read_text())
MIAMI = json.loads((FIXTURES / "noaa" / "miami.json").read_text())


def _clock(aware_now):
    """A datetime whose now() is *aware_now*; naive now() is that instant in UTC (a server's clock)."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return aware_now.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            return aware_now.astimezone(tz)

    return _Clock


def _bot(extra=None):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa", **(extra or {})}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kw: f"<{key}{sorted(kw.items()) or ''}>"
    return bot


# 2026-10-01 18:30 in Seattle is already 2026-10-02 01:30 UTC on the bot's server.
SEATTLE_EVENING = datetime(2026, 10, 1, 18, 30, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_wx_tomorrow_is_the_day_after_the_forecasts_today_not_the_servers():
    wx = WxCommand(_bot())
    periods = SEATTLE["forecast"]["properties"]["periods"]
    with patch("modules.commands.wx_command.datetime", _clock(SEATTLE_EVENING)):
        text = wx.format_tomorrow_forecast(periods)
    # Seattle's tomorrow is Friday 2026-10-02; the server's would be Saturday.
    assert text.startswith("Fri: ")
    assert "Sat" not in text


def test_wx_multiday_starts_the_day_after_the_forecasts_today():
    wx = WxCommand(_bot())
    periods = SEATTLE["forecast"]["properties"]["periods"]
    with patch("modules.commands.wx_command.datetime", _clock(SEATTLE_EVENING)):
        lines = wx.format_multiday_forecast(periods, num_days=3).split("\n")
    assert [line.split(":")[0] for line in lines] == ["F", "Sa", "Su"]


def test_wx_multiday_keeps_a_holiday_named_period():
    periods = copy.deepcopy(SEATTLE["forecast"]["properties"]["periods"])
    for p in periods:
        if p["name"] == "Monday":
            p["name"] = "Columbus Day"
    wx = WxCommand(_bot())
    with patch("modules.commands.wx_command.datetime", _clock(SEATTLE_EVENING)):
        lines = wx.format_multiday_forecast(periods, num_days=5).split("\n")
    assert [line.split(":")[0] for line in lines] == ["F", "Sa", "Su", "M", "T"]
    assert "68°F" in lines[3]  # Monday's (Columbus Day's) daytime high, not its night low


def test_wx_hourly_drops_hours_already_past_at_the_location():
    # 15:30 in Seattle is 18:30 in Miami: the 6 PM Miami hour has started.
    wx = WxCommand(_bot())
    hourly = MIAMI["hourly"]["properties"]["periods"]
    at = datetime(2026, 10, 1, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("modules.commands.wx_command.datetime", _clock(at)):
        text = wx.format_hourly_forecast(hourly, max_length=400)
    assert not text.startswith("6PM")
    assert text.startswith("7PM")


def _open_meteo(days=8, current_time="2026-10-01T19:15"):
    start = datetime(2026, 10, 1)
    dates = [(start.replace(day=1 + i)).strftime("%Y-%m-%d") for i in range(days)]
    return {
        "current": {"time": current_time, "temperature_2m": 15.0, "relative_humidity_2m": 70, "weather_code": 3},
        "daily": {
            "time": dates,
            "weather_code": [3] * days,
            "temperature_2m_max": [20.0 + i for i in range(days)],
            "temperature_2m_min": [10.0 + i for i in range(days)],
        },
    }


def _gwx_run(forecast_type, num_days=7, data=None):
    gwx = GlobalWxCommand(_bot())
    response = Mock(ok=True)
    response.json.return_value = data or _open_meteo()
    get = Mock(return_value=response)
    # The server's clock says it is already Friday in UTC.
    with patch("modules.commands.alternatives.wx_international.requests.get", get), patch(
        "modules.commands.alternatives.wx_international.datetime", _clock(SEATTLE_EVENING)
    ):
        text = gwx.get_open_meteo_weather(47.6, -122.3, forecast_type=forecast_type, num_days=num_days)
    return text, get.call_args.kwargs["params"]


def test_gwx_requests_one_extra_day_so_n_days_means_n_future_days():
    text, params = _gwx_run("multiday", num_days=7)
    assert params["forecast_days"] == 8
    assert len(text.split("\n")) == 7


def test_gwx_sixteen_days_is_capped_at_what_open_meteo_returns():
    _, params = _gwx_run("multiday", num_days=16)
    assert params["forecast_days"] == 16


def test_gwx_day_labels_come_from_open_meteos_dates():
    text, _ = _gwx_run("multiday", num_days=2)
    # Daily index 1 is 2026-10-02, a Friday, whatever the server's date.
    assert text.split("\n")[0].startswith("<commands.gwx.day_abbrev.Friday>")


def test_gwx_today_or_tonight_follows_the_locations_clock():
    evening, _ = _gwx_run("default", data=_open_meteo(current_time="2026-10-01T19:15"))
    morning, _ = _gwx_run("default", data=_open_meteo(current_time="2026-10-02T09:00"))
    assert evening.startswith("<commands.gwx.periods.tonight>")
    assert morning.startswith("<commands.gwx.periods.today>")


def test_wx_today_is_the_locations_date_even_when_the_first_period_began_yesterday():
    # 01:00 on Oct 2 in Seattle: the first period is still "Tonight", which started Oct 1 at 18:00.
    after_midnight = datetime(2026, 10, 2, 1, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    wx = WxCommand(_bot())
    periods = SEATTLE["forecast"]["properties"]["periods"]
    with patch("modules.commands.wx_command.datetime", _clock(after_midnight)):
        tomorrow = wx.format_tomorrow_forecast(periods)
        lines = wx.format_multiday_forecast(periods, num_days=2).split("\n")
    assert tomorrow.startswith("Sat: ")
    assert [line.split(":")[0] for line in lines] == ["Sa", "Su"]


def test_wx_an_undated_period_keeps_its_place():
    periods = copy.deepcopy(SEATTLE["forecast"]["properties"]["periods"])
    friday = next(p for p in periods if p["name"] == "Friday")
    del friday["startTime"]
    wx = WxCommand(_bot())
    with patch("modules.commands.wx_command.datetime", _clock(SEATTLE_EVENING)):
        tomorrow = wx.format_tomorrow_forecast(periods)
        lines = wx.format_multiday_forecast(periods, num_days=2).split("\n")
    assert tomorrow.startswith("Fri: ") and "Fri Night" in tomorrow
    assert lines[0].startswith("F:") and "70°F" in lines[0]  # Friday's high, not Friday night's low
