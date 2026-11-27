"""MET Norway Locationforecast: the third forecast provider, from live fixtures.

No test here touches the network. The session is a fake that answers from
``tests/fixtures/metno/``, trimmed from three live documents (Egersund, Tokyo,
Seattle) with the field names and shapes untouched, and both clocks are fixed so
the cache TTLs and the current step can be walked forward.
"""

import configparser
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytz
from packaging.requirements import Requirement

from modules.i18n import Translator
from modules.weather_common import WeatherCommandMixin
from modules.weather_providers import metno
from modules.weather_providers.base import WeatherUnits
from modules.weather_providers.metno import (
    METNO_URL,
    SYMBOL_TO_WMO,
    MetNorwayProvider,
    _symbol_parts,
    local_offset_hours,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "metno"

# The fixtures start at 21:00Z on 27 November 2026; the clock sits half an hour
# into the first step.
NOW = datetime(2026, 11, 27, 21, 30, tzinfo=timezone.utc)
EGERSUND = (58.45, 6.0)
TOKYO = (35.68, 139.7)
SEATTLE = (47.6, -122.3)

HEADERS = {
    "Date": "Fri, 27 Nov 2026 21:25:34 GMT",
    "Last-Modified": "Fri, 27 Nov 2026 21:25:34 GMT",
    "Expires": "Fri, 27 Nov 2026 21:56:44 GMT",
}


def _payload(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = dict(HEADERS if headers is None else headers)
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _Session:
    """A fake session: one response, or a callable taking the request."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if callable(self.response):
            return self.response(self.calls[-1])
        return self.response

    def count(self):
        return len(self.calls)


class _Host(WeatherCommandMixin):
    """The command side a provider reads: the real formatting helpers, a fake bot."""

    def __init__(self, weather=None):
        config = configparser.ConfigParser()
        config.read_dict({"Weather": weather or {}, "Bot": {}})
        self.bot = SimpleNamespace(config=config)
        self.logger = Mock()
        self.translator = Translator("en")
        self.response_translator = self.translator

    def translate(self, key, **kwargs):
        return self.translator.translate(key, **kwargs)

    def get_max_message_length(self, message):
        return 150


class _Clock:
    def __init__(self, monotonic=1000.0, wall=NOW):
        self.monotonic = monotonic
        self.wall = wall

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def _provider(name="egersund", units=None, response=None, weather=None, host=None):
    clock = _Clock()
    session = _Session(response if response is not None else _Response(200, _payload(name)))
    host = host or _Host(weather)
    provider = MetNorwayProvider(
        host,
        units or WeatherUnits("fahrenheit", "mph", "inch"),
        session=session,
        clock=lambda: clock.monotonic,
        now=lambda: clock.wall,
    )
    return provider, session, host, clock


def _width(host, text):
    return host._count_display_width(text)


# --- the reply shapes -------------------------------------------------------


@pytest.mark.parametrize("name,point", [("egersund", EGERSUND), ("tokyo", TOKYO)])
@pytest.mark.parametrize("budget", [130, 150])
def test_every_forecast_type_fits_the_budget(name, point, budget):
    provider, _session_, host, _clock = _provider(name)
    host.get_max_message_length = Mock(return_value=budget)
    lat, lon = point

    for forecast_type, num_days in (("default", 7), ("tomorrow", 7), ("multiday", 3), ("hourly", 7)):
        reply = provider.forecast(lat, lon, forecast_type, num_days, 0, None)
        assert reply.ok is True
        assert reply.text
        assert _width(host, reply.text) <= budget, (forecast_type, reply.text)


def test_the_default_reply_has_the_open_meteo_shape():
    provider, _session_, host, _clock = _provider("egersund")

    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)

    # "Tonight: <emoji><description> <temp><unit> <dir><wind> <humidity> <pressure> | <high/low>"
    assert reply.text.startswith("Tonight: ☁️Overcast 37°F ")
    assert "84%RH" in reply.text
    assert "📊1031hPa" in reply.text
    assert " | " in reply.text
    assert reply.extra is None


def test_the_tomorrow_reply_names_the_day_and_its_high_low():
    provider, _session_, host, _clock = _provider("egersund")

    reply = provider.forecast(*EGERSUND, "tomorrow", 7, 0, None)

    assert reply.text.startswith("Tomorrow: ")
    assert "🌦️" in reply.text
    assert "in" in reply.text or "mm" in reply.text


def test_the_multiday_reply_is_one_line_per_day_from_tomorrow():
    provider, _session_, host, _clock = _provider("egersund")

    reply = provider.forecast(*EGERSUND, "multiday", 3, 0, None)

    lines = reply.text.split("\n")
    assert len(lines) == 3
    for line in lines:
        assert line.split(":")[0] in ("Sa", "Su", "M", "Tu", "We", "Th", "Fr")
        assert ":" in line


def test_the_hourly_reply_starts_at_the_next_local_hour():
    provider, _session_, host, _clock = _provider("egersund")

    reply = provider.forecast(*EGERSUND, "hourly", 7, 0, None)

    lines = reply.text.split("\n")
    # 21:00Z at longitude 6 is 21:00 local, so the first hour is 10PM.
    assert lines[0].startswith("10PM: ")
    assert all(":" in line for line in lines)


def test_the_location_prefix_comes_out_of_the_budget():
    provider, _session_, host, _clock = _provider("egersund")
    host.get_max_message_length = Mock(return_value=150)

    full = provider.forecast(*EGERSUND, "hourly", 7, 0, None)
    prefixed = provider.forecast(*EGERSUND, "hourly", 7, 12, None)

    assert _width(host, prefixed.text) <= 150 - 12
    assert _width(host, prefixed.text) <= _width(host, full.text)


# --- units ------------------------------------------------------------------


def test_units_convert_from_celsius_meters_and_millimeters():
    imperial, _session_, host, _clock = _provider("egersund")
    metric, _session_, _host_, _clock = _provider(
        "egersund", units=WeatherUnits("celsius", "kmh", "mm")
    )

    imperial_reply = imperial.forecast(*EGERSUND, "default", 7, 0, None)
    metric_reply = metric.forecast(*EGERSUND, "default", 7, 0, None)

    # 2.7 C is 37 F; 8.4 m/s is 19 mph and 30 km/h.
    assert "37°F" in imperial_reply.text
    assert "19" in imperial_reply.text
    assert "3°C" in metric_reply.text
    assert "30" in metric_reply.text
    assert "°F" not in metric_reply.text


def test_precipitation_converts_to_inches():
    imperial, _session_, _host_, _clock = _provider("egersund")
    metric, _session_, _host_, _clock = _provider(
        "egersund", units=WeatherUnits("celsius", "kmh", "mm")
    )

    imperial_reply = imperial.forecast(*EGERSUND, "tomorrow", 7, 0, None)
    metric_reply = metric.forecast(*EGERSUND, "tomorrow", 7, 0, None)

    assert "in" in imperial_reply.text
    assert "mm" in metric_reply.text


# --- symbols ----------------------------------------------------------------


def test_symbol_codes_map_onto_the_shared_descriptions():
    provider, _session_, _host_, _clock = _provider()

    assert provider._description("clearsky") == "Clear"
    assert provider._description("partlycloudy") == "Partly Cloudy"
    assert provider._description("heavyrain") == "Heavy Rain"
    assert provider._description("lightrainshowersandthunder") == "Thunderstorm"


def test_the_day_and_night_suffix_picks_the_moon_at_night():
    provider, _session_, _host_, _clock = _provider()

    assert provider._emoji("clearsky", is_day=True) == "☀️"
    assert provider._emoji("clearsky", is_day=False) == "🌙"
    assert provider._emoji("fair", is_day=False) == "🌙"
    # Only clear skies swap; a cloudy night keeps its cloud.
    assert provider._emoji("partlycloudy", is_day=False) == "⛅"


def test_an_unknown_symbol_code_is_described_as_unknown():
    provider, _session_, _host_, _clock = _provider()

    assert provider._description("volcanicsunset") == "Unknown"
    assert provider._emoji("volcanicsunset") == "🌤️"


def test_every_mapped_symbol_has_a_description():
    provider, _session_, _host_, _clock = _provider()

    for base in SYMBOL_TO_WMO:
        assert provider._description(base) != "Unknown"


# --- local days -------------------------------------------------------------


def test_the_local_offset_is_estimated_from_the_longitude():
    assert local_offset_hours(139.7) == 9
    assert local_offset_hours(-122.3) == -8
    assert local_offset_hours(6.0) == 0


def test_days_split_on_the_estimated_local_midnight():
    provider, _session_, _host_, _clock = _provider("tokyo")

    days = provider._daily(_payload("tokyo"), *TOKYO)

    # 21:00Z is 06:00 the next day in Tokyo, so the first local day starts there.
    assert days[0]["date"].isoformat() == "2026-11-28"
    assert days[0]["high"] >= days[0]["low"]

    provider, _session_, _host_, _clock = _provider("seattle")
    days = provider._daily(_payload("seattle"), *SEATTLE)
    # 21:00Z is 13:00 the same day in Seattle.
    assert days[0]["date"].isoformat() == "2026-11-27"


def test_the_daily_high_and_low_are_the_extremes_of_the_local_day():
    provider, _session_, _host_, _clock = _provider("egersund")
    payload = _payload("egersund")

    days = provider._daily(payload, *EGERSUND)

    first = days[0]
    temperatures = [
        step["data"]["instant"]["details"]["air_temperature"]
        for step in payload["properties"]["timeseries"]
        if step["time"].startswith("2026-11-27")
    ]
    assert first["high"] == max(temperatures)
    assert first["low"] == min(temperatures)


# --- the request ------------------------------------------------------------


def test_the_request_identifies_the_bot_and_rounds_the_point():
    provider, session, _host_, _clock = _provider()

    provider.forecast(58.4512345, 6.0498765, "default", 7, 0, None)

    call = session.calls[0]
    assert call["url"] == METNO_URL
    assert call["params"] == {"lat": "58.4512", "lon": "6.0499"}
    assert call["headers"]["User-Agent"].startswith("meshcore-bot")
    assert "github.com/agessaman/meshcore-bot" in call["headers"]["User-Agent"]


def test_the_user_agent_can_be_overridden_in_config():
    provider, session, _host_, _clock = _provider(
        weather={"metno_user_agent": "my-bot/1.0 (ops@example.com)"}
    )

    provider.forecast(*EGERSUND, "default", 7, 0, None)

    assert session.calls[0]["headers"]["User-Agent"] == "my-bot/1.0 (ops@example.com)"


def test_a_304_reuses_the_cached_document():
    provider, session, host, clock = _provider()
    provider.forecast(*EGERSUND, "default", 7, 0, None)

    # Past Expires (31m10s after Date), so the next request goes out with
    # If-Modified-Since.
    clock.advance(32 * 60)
    session.response = _Response(304, headers={"Expires": "Fri, 27 Nov 2026 22:30:00 GMT",
                                               "Date": "Fri, 27 Nov 2026 22:00:00 GMT"})
    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)

    assert session.count() == 2
    assert session.calls[1]["headers"]["If-Modified-Since"] == HEADERS["Last-Modified"]
    assert reply.ok is True
    assert reply.text.startswith("Tonight: ")


def test_expires_keeps_the_document_until_it_passes():
    provider, session, _host_, clock = _provider()
    provider.forecast(*EGERSUND, "default", 7, 0, None)

    clock.advance(20 * 60)
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 1

    clock.advance(15 * 60)
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 2


def test_a_missing_expires_falls_back_to_half_an_hour():
    provider, session, _host_, clock = _provider(
        response=_Response(200, _payload("egersund"), headers={"Last-Modified": "x"})
    )
    provider.forecast(*EGERSUND, "default", 7, 0, None)

    clock.advance(29 * 60)
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 1

    clock.advance(2 * 60)
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 2


# --- failures ---------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 500])
def test_an_http_failure_is_an_error_reply_and_never_raises(status):
    provider, _session_, host, _clock = _provider(response=_Response(status))

    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.text == "Error fetching weather data from MET Norway"
    assert host.logger.warning.called


def test_a_429_pauses_the_next_request():
    provider, session, _host_, clock = _provider(
        response=_Response(429, headers={"Retry-After": "120"})
    )
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 1

    session.response = _Response(200, _payload("egersund"))
    clock.advance(60)
    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert reply.ok is False
    assert session.count() == 1

    clock.advance(61)
    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert reply.ok is True
    assert session.count() == 2


@pytest.mark.parametrize("payload", [[], {"properties": {}}, {"properties": {"timeseries": [None]}}])
def test_a_malformed_document_is_an_error_reply(payload):
    provider, _session_, host, _clock = _provider(response=_Response(200, payload))

    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.text == "Error fetching weather data from MET Norway"


def test_a_connection_error_is_an_error_reply():
    def boom(call):
        raise ConnectionError("no route")

    provider, _session_, host, _clock = _provider(response=boom)

    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)

    assert reply.ok is False
    assert host.logger.warning.called


def test_a_document_without_a_temperature_is_an_error_reply():
    payload = _payload("egersund")
    for step in payload["properties"]["timeseries"]:
        step["data"]["instant"]["details"].pop("air_temperature", None)
    provider, _session_, host, _clock = _provider(response=_Response(200, payload))

    reply = provider.forecast(*EGERSUND, "default", 7, 0, None)

    assert reply.ok is False


# --- extreme conditions -----------------------------------------------------


def test_extreme_conditions_ride_along_as_the_second_message():
    provider, _session_, host, _clock = _provider("tokyo")

    reply = provider.forecast(*TOKYO, "default", 7, 0, None)

    assert reply.ok is True
    assert reply.extra == "⚠️ Heavy rain"


def test_extreme_conditions_do_not_ride_along_with_other_forecast_types():
    provider, _session_, host, _clock = _provider("tokyo")

    reply = provider.forecast(*TOKYO, "tomorrow", 7, 0, None)

    assert reply.extra is None


# --- Expires ----------------------------------------------------------------


def test_the_ttl_comes_from_expires_and_date():
    provider, _session_, _host_, _clock = _provider()

    ten_minutes = {"Date": "Fri, 27 Nov 2026 21:00:00 GMT",
                   "Expires": "Fri, 27 Nov 2026 21:10:00 GMT"}
    two_hours = {"Date": "Fri, 27 Nov 2026 21:00:00 GMT",
                 "Expires": "Fri, 27 Nov 2026 23:00:00 GMT"}

    assert provider._ttl(ten_minutes, 0.0) == 600
    assert provider._ttl(two_hours, 0.0) == 7200


def test_the_cache_honors_a_short_expires():
    provider, session, _host_, clock = _provider(
        response=_Response(200, _payload("egersund"), headers={
            "Date": "Fri, 27 Nov 2026 21:25:34 GMT",
            "Last-Modified": "Fri, 27 Nov 2026 21:25:34 GMT",
            "Expires": "Fri, 27 Nov 2026 21:35:34 GMT",
        })
    )
    provider.forecast(*EGERSUND, "default", 7, 0, None)

    clock.advance(9 * 60)
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 1

    clock.advance(2 * 60)
    provider.forecast(*EGERSUND, "default", 7, 0, None)
    assert session.count() == 2


# --- the hourly series across a DST change ----------------------------------


def _dst_payload():
    """Steps from 23:00Z on 24 October to 04:00Z on 25 October, hourly."""
    times = ["2026-10-24T23:00:00Z"] + [f"2026-10-25T{hour:02d}:00:00Z" for hour in range(5)]
    return {
        "properties": {
            "timeseries": [
                {
                    "time": moment,
                    "data": {
                        "instant": {"details": {"air_temperature": 15.0, "wind_speed": 2.0,
                                                "wind_from_direction": 180.0}},
                        "next_1_hours": {
                            "summary": {"symbol_code": "clearsky_day"},
                            "details": {"precipitation_amount": 0.0},
                        },
                    },
                }
                for moment in times
            ]
        }
    }


def test_a_dst_repeated_hour_is_kept(monkeypatch):
    zone = pytz.timezone("Europe/Madrid")
    monkeypatch.setattr(metno, "utc_offset_at",
                        lambda lat, lon, when: when.astimezone(zone).utcoffset())
    provider, _session_, _host_, _clock = _provider()
    provider._now = lambda: datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)

    text = provider._hourly(_dst_payload(), 40.42, -3.70, 400)

    # Madrid falls back at 01:00Z, so 00:00Z and 01:00Z are both 02:00 local.
    labels = [line.split(":")[0] for line in text.splitlines()]
    assert labels == ["2AM", "2AM", "3AM", "4AM", "5AM"]


# --- the current-conditions budget ------------------------------------------


def test_the_current_line_fits_a_small_budget():
    provider, _session_, host, _clock = _provider("egersund")

    text = provider._current(_payload("egersund"), *EGERSUND, 60)

    assert text is not None
    assert _width(host, text) <= 60
    # The least important parts go first, so the pressure is gone.
    assert "hPa" not in text


# --- polar twilight ---------------------------------------------------------


def test_polar_twilight_takes_the_moon():
    provider, _session_, _host_, _clock = _provider()

    base, is_day = _symbol_parts("clearsky_polartwilight")

    assert base == "clearsky"
    assert is_day is False
    # The sun at or below the horizon takes the moon, as ``_night`` does.
    assert provider._emoji("clearsky", is_day=False) == "🌙"
    assert provider._emoji("clearsky", is_day=True) == "☀️"


# --- the tzfpy dependency marker --------------------------------------------


def test_the_tzfpy_marker_names_each_64_bit_machine():
    root = Path(__file__).resolve().parents[2]
    lines = [line.strip() for line in (root / "requirements.txt").read_text().splitlines()
             if line.strip().startswith("tzfpy")]
    assert len(lines) == 1
    marker = Requirement(lines[0]).marker

    for machine in ("x86_64", "AMD64", "aarch64", "arm64"):
        assert marker.evaluate({"platform_machine": machine}) is True, machine
    # The substring form of the marker also matched 32-bit x86.
    for machine in ("x86", "armv7l", "i686"):
        assert marker.evaluate({"platform_machine": machine}) is False, machine
