"""Environment Canada city pages: the ``eccc`` forecast provider, from live fixtures.

No test here touches the network. The session answers from
``tests/fixtures/eccc/``, trimmed from live documents (Vancouver, Ottawa) with
the field names and shapes untouched, and both clocks are fixed so the cache and
the period label can be walked forward.
"""

import configparser
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.i18n import Translator
from modules.weather_common import WeatherCommandMixin
from modules.weather_providers.base import WeatherUnits
from modules.weather_providers.eccc import (
    CITYPAGE_COLLECTION,
    ECCC_BASE_URL,
    EcccProvider,
    _wmo_for,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eccc"

NOW = datetime(2026, 11, 27, 21, 30, tzinfo=timezone.utc)
VANCOUVER = (49.28, -123.12)
OTTAWA = (45.42, -75.70)
# A point in the Pacific, far from any city page.
OFFSHORE = (48.0, -140.0)


def _payload(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = dict(headers or {})
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
        self.calls.append({"url": url, "params": dict(params or {})})
        if callable(self.response):
            return self.response(self.calls[-1])
        return self.response

    def count(self):
        return len(self.calls)


class _Host(WeatherCommandMixin):
    """The command side a provider reads: the real formatting helpers, a fake bot."""

    def __init__(self, weather=None, localization=None):
        config = configparser.ConfigParser()
        config.read_dict({
            "Weather": weather or {},
            "Bot": {},
            "Localization": localization or {},
        })
        self.bot = SimpleNamespace(config=config)
        self.logger = Mock()
        self.translator = Translator("en")
        self.response_translator = self.translator

    def translate(self, key, **kwargs):
        return self.translator.translate(key, **kwargs)

    def get_max_message_length(self, message):
        return 150


class _Clock:
    def __init__(self, monotonic=1000.0):
        self.monotonic = monotonic

    def advance(self, seconds):
        self.monotonic += seconds


def _provider(name="vancouver", units=None, response=None, host=None, clock=None):
    clock = clock or _Clock()
    session = _Session(response if response is not None else _Response(200, _payload(name)))
    host = host or _Host()
    provider = EcccProvider(
        host,
        units or WeatherUnits("fahrenheit", "mph", "inch"),
        session=session,
        clock=lambda: clock.monotonic,
        now=lambda: NOW,
    )
    return provider, session, host, clock


def _width(host, text):
    return host._count_display_width(text)


# --- the reply shapes -------------------------------------------------------


@pytest.mark.parametrize("name,point", [("vancouver", VANCOUVER), ("ottawa", OTTAWA)])
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
    provider, _session_, host, _clock = _provider("vancouver")

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    # "Today: <emoji><description> <temp><unit> <dir><wind> <humidity> <pressure> | <high/low> | Tomorrow: ..."
    # 21:30Z is 13:30 in Vancouver, so the period is today's.
    assert reply.text.startswith("Today: ")
    assert "°F" in reply.text
    assert "100%RH" in reply.text
    assert "📊1021hPa" in reply.text
    assert " | " in reply.text
    assert "Tomorrow:" in reply.text


def test_the_tomorrow_reply_names_the_day_and_its_high_low():
    provider, _session_, host, _clock = _provider("vancouver")

    reply = provider.forecast(*VANCOUVER, "tomorrow", 7, 0, None)

    assert reply.text.startswith("Tomorrow: ")
    assert "Periods of drizzle" in reply.text
    assert "°F" in reply.text


def test_the_multiday_reply_is_one_line_per_day():
    provider, _session_, host, _clock = _provider("vancouver")

    reply = provider.forecast(*VANCOUVER, "multiday", 3, 0, None)

    lines = reply.text.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("Sa: ")
    assert all("°F" in line for line in lines)


def test_the_hourly_reply_starts_after_the_current_hour():
    provider, _session_, host, _clock = _provider("vancouver")

    reply = provider.forecast(*VANCOUVER, "hourly", 7, 0, None)

    # 21:30Z is 13:30 in Vancouver, so the first hour shown is 2PM.
    assert reply.text.startswith("2PM:")
    assert "°" in reply.text


def test_the_reply_language_follows_the_bot_language():
    host = _Host(localization={"language": "fr"})
    provider, _session_, _host_, _clock = _provider("vancouver", host=host)

    reply = provider.forecast(*VANCOUVER, "tomorrow", 7, 0, None)

    # The data's own text is French; the catalog keys follow the bot's translator.
    assert "Bruine intermittente" in reply.text


# --- the city-page lookup ---------------------------------------------------


def test_the_nearest_city_page_wins():
    provider, session, _host_, _clock = _provider("vancouver")

    feature, failed = provider._lookup(*VANCOUVER)

    assert failed is False
    # Vancouver is 4.5 km away; West Vancouver and Richmond are further.
    assert feature["properties"]["name"]["en"] == "Vancouver"
    assert session.count() == 1
    assert session.calls[0]["url"] == f"{ECCC_BASE_URL}/collections/{CITYPAGE_COLLECTION}/items"
    assert session.calls[0]["params"]["bbox"] == "-123.4200,48.9800,-122.8200,49.5800"


def test_the_search_box_widens_until_a_city_page_answers():
    empty = _Response(200, {"type": "FeatureCollection", "features": []})
    full = _Response(200, _payload("vancouver"))
    session = _Session(lambda call: empty if call["params"]["bbox"].startswith("-123.42") else full)
    provider = EcccProvider(
        _Host(), WeatherUnits("fahrenheit", "mph", "inch"),
        session=session, clock=lambda: 1000.0, now=lambda: NOW,
    )

    feature, failed = provider._lookup(*VANCOUVER)

    assert failed is False
    assert feature["properties"]["name"]["en"] == "Vancouver"
    assert session.count() == 2
    assert session.calls[1]["params"]["bbox"] == "-124.1200,48.2800,-122.1200,50.2800"


def test_a_point_past_150_km_has_no_coverage():
    far = _payload("vancouver")
    for feature in far["features"]:
        feature["geometry"]["coordinates"] = [-100.0, 60.0]
    provider, _session_, _host_, _clock = _provider(response=_Response(200, far))

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.no_coverage is True
    assert reply.text == "Error fetching weather data from Environment Canada"


def test_coverage_is_the_city_page_network_extent():
    provider, _session_, _host_, _clock = _provider()

    assert provider.covers(*VANCOUVER) is True
    assert provider.covers(*OTTAWA) is True
    assert provider.covers(48.85, 2.35) is False  # Paris
    assert provider.covers(25.76, -80.19) is False  # Miami


# --- units ------------------------------------------------------------------


def test_metric_units_are_used_as_they_arrive():
    provider, _session_, host, _clock = _provider(
        "vancouver", units=WeatherUnits("celsius", "kmh", "mm")
    )

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert "9°C" in reply.text
    assert "°F" not in reply.text


def test_imperial_units_are_converted():
    provider, _session_, host, _clock = _provider(
        "vancouver", units=WeatherUnits("fahrenheit", "mph", "inch")
    )

    assert provider._temperature(10) == 50
    assert provider._wind_speed(20) == 12
    assert provider._precipitation(25.4) == pytest.approx(1.0)
    assert provider.temp_symbol == "°F"
    assert provider.precipitation_symbol == "in"


def test_knots_and_metres_per_second():
    provider, _session_, host, _clock = _provider(
        "vancouver", units=WeatherUnits("celsius", "kn", "mm")
    )
    assert provider._wind_speed(18.52) == 10

    provider, _session_, host, _clock = _provider(
        "vancouver", units=WeatherUnits("celsius", "ms", "mm")
    )
    assert provider._wind_speed(36) == 10


# --- the icon map -----------------------------------------------------------


@pytest.mark.parametrize("code,description,wmo", [
    (2, "A mix of sun and cloud", 2),
    (3, "Mainly cloudy", 3),
    (10, "Cloudy", 3),
    (12, "Rain", 63),
    (16, "Chance of flurries", 71),
    (28, "Periods of drizzle", 53),
    (32, "Cloudy periods", 2),
    (36, "Chance of showers", 80),
])
def test_the_icon_codes_map_to_the_shared_emoji_table(code, description, wmo):
    assert _wmo_for(code, description) == wmo


def test_an_unknown_icon_code_falls_back_to_the_description():
    assert _wmo_for(99, "Thunderstorm") == 95
    assert _wmo_for(None, "Periods of snow") == 73
    assert _wmo_for(None, "Freezing rain") == 66
    assert _wmo_for(None, "") is None


def test_the_condition_carries_the_emoji_and_the_data_text():
    provider, _session_, host, _clock = _provider()

    assert provider._condition(12, "A few showers") == "🌧️A few showers"
    assert provider._condition(None, "Thunderstorm") == "⛈️Thunderstorm"


# --- failures ---------------------------------------------------------------


def test_a_server_error_is_an_error_reply():
    provider, _session_, host, _clock = _provider(response=_Response(500))

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.no_coverage is False
    assert reply.text == "Error fetching weather data from Environment Canada"
    host.logger.warning.assert_called()


def test_malformed_json_is_an_error_reply():
    provider, _session_, host, _clock = _provider(response=_Response(200, None))

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.text == "Error fetching weather data from Environment Canada"


def test_a_raising_session_is_an_error_reply():
    def boom(call):
        raise RuntimeError("connection reset")

    provider, _session_, host, _clock = _provider(response=boom)

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.text == "Error fetching weather data from Environment Canada"


def test_a_city_page_without_a_temperature_is_an_error_reply():
    payload = _payload("vancouver")
    payload["features"][2]["properties"]["currentConditions"]["temperature"] = {}
    provider, _session_, host, _clock = _provider(response=_Response(200, payload))

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    host.logger.warning.assert_called()


# --- the cache --------------------------------------------------------------


def test_the_city_page_is_cached_for_ten_minutes():
    provider, session, _host_, clock = _provider("vancouver")

    provider._lookup(*VANCOUVER)
    clock.advance(599)
    provider._lookup(*VANCOUVER)
    assert session.count() == 1

    clock.advance(2)
    provider._lookup(*VANCOUVER)
    assert session.count() == 2


def test_an_expires_header_shortens_the_cache():
    payload = _payload("vancouver")
    headers = {"Expires": "Fri, 27 Nov 2026 21:35:00 GMT"}
    provider, session, _host_, clock = _provider(response=_Response(200, payload, headers))

    provider._lookup(*VANCOUVER)
    clock.advance(200)
    provider._lookup(*VANCOUVER)

    assert session.count() == 2


def test_a_failed_lookup_is_cached_too():
    provider, session, _host_, clock = _provider(response=_Response(500))

    provider._lookup(*VANCOUVER)
    provider._lookup(*VANCOUVER)

    assert session.count() == 1


def test_the_cache_is_per_rounded_point():
    provider, session, _host_, clock = _provider("vancouver")

    provider._lookup(*VANCOUVER)
    provider._lookup(VANCOUVER[0] + 0.001, VANCOUVER[1] + 0.001)
    assert session.count() == 1

    # A different point is a different key, even when the answer is the same.
    provider._lookup(*OTTAWA)
    assert session.count() > 1


def test_the_cache_does_not_grow_without_bound_while_it_is_warm():
    provider, session, _host_, clock = _provider("vancouver")

    for step in range(3):
        provider._lookup(VANCOUVER[0] + step, VANCOUVER[1])
    clock.advance(601)
    provider._lookup(*VANCOUVER)

    assert len(provider._cache) == 3


def test_the_period_label_follows_the_local_hour():
    provider, _session_, host, _clock = _provider()

    assert provider._period_key(13) == "today"
    assert provider._period_key(3) == "overnight"
    assert provider._period_key(21) == "tonight"


def test_the_days_pair_highs_with_their_nights():
    provider, _session_, host, _clock = _provider("vancouver")

    days = provider._days(_payload("vancouver")["features"][2])

    assert days[0]["high"] == 10 and days[0]["low"] == 7
    assert days[1]["high"] == 9 and days[1]["low"] == 5
    assert days[1]["name"] == "Saturday"
    assert len(days) == 7


def test_the_offset_comes_from_the_point_time_zone():
    provider, _session_, host, _clock = _provider()

    assert provider._offset(*VANCOUVER, NOW) == timedelta(hours=-8)
