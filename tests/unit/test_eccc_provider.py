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
from modules.weather_providers import eccc
from modules.weather_providers.base import WeatherUnits
from modules.weather_providers.eccc import (
    CITYPAGE_COLLECTION,
    COMPASS_DEGREES,
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
# 17:00 in Ottawa on 27 November, when the first period is already "Tonight".
EVENING = datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc)
# A stand-in for the MeshMessage: without one the provider falls back to a fixed
# 130-byte budget and the mocked ``get_max_message_length`` is never consulted.
MESSAGE = SimpleNamespace(content="wx Vancouver")


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


def _provider(name="vancouver", units=None, response=None, host=None, clock=None, now=None):
    clock = clock or _Clock()
    session = _Session(response if response is not None else _Response(200, _payload(name)))
    host = host or _Host()
    provider = EcccProvider(
        host,
        units or WeatherUnits("fahrenheit", "mph", "inch"),
        session=session,
        clock=lambda: clock.monotonic,
        now=now or (lambda: NOW),
    )
    return provider, session, host, clock


def _width(host, text):
    return host._count_display_width(text)


def _pinned_offset(lat, lon, when):
    """Each fixture point's offset, pinned so the expectations do not follow the tz database.

    British Columbia stopped observing daylight saving in 2026, so Vancouver is
    permanently UTC-7, which the installed pytz predates; Ottawa still changes on
    1 November 2026.
    """
    if lon < -100:
        return timedelta(hours=-7)
    if when < datetime(2026, 11, 1, 6, tzinfo=timezone.utc):
        return timedelta(hours=-4)
    return timedelta(hours=-5)


@pytest.fixture(autouse=True)
def _pin_the_time_zones(monkeypatch):
    monkeypatch.setattr(eccc, "utc_offset_at", _pinned_offset)


# --- the reply shapes -------------------------------------------------------


@pytest.mark.parametrize("name,point", [("vancouver", VANCOUVER), ("ottawa", OTTAWA)])
@pytest.mark.parametrize("budget", [130, 150])
def test_every_forecast_type_fits_the_budget(name, point, budget):
    provider, _session_, host, _clock = _provider(name)
    host.get_max_message_length = Mock(return_value=budget)
    lat, lon = point

    for forecast_type, num_days in (("default", 7), ("tomorrow", 7), ("multiday", 3), ("hourly", 7)):
        reply = provider.forecast(lat, lon, forecast_type, num_days, 0, MESSAGE)
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

    # 21:30Z is 14:30 in Vancouver (UTC-7), so the first hour shown is 3PM.
    assert reply.text.startswith("3PM:")
    assert "°" in reply.text


def test_the_day_lines_carry_the_data_summary_once():
    provider, _session_, _host_, _clock = _provider("vancouver")

    tomorrow = provider.forecast(*VANCOUVER, "tomorrow", 7, 0, None).text
    multiday = provider.forecast(*VANCOUVER, "multiday", 3, 0, None).text

    # The emoji comes from the icon code and the words from the data, once:
    # the catalog's own description for the code must not lead the summary.
    assert tomorrow.startswith("Tomorrow: 🌦️ Periods of drizzle H:")
    assert "Drizzle" not in tomorrow
    assert multiday.splitlines()[0].startswith("Sa: 🌦️ Periods of drizzle H:")


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
    # A 150 km box, widened in longitude for the latitude.
    assert session.calls[0]["params"]["bbox"] == "-125.1915,47.9286,-121.0485,50.6314"


def test_the_search_box_widens_until_a_city_page_answers():
    empty = _Response(200, {"type": "FeatureCollection", "features": []})
    full = _Response(200, _payload("vancouver"))
    session = _Session(lambda call: empty if call["params"]["bbox"].startswith("-125.19") else full)
    provider = EcccProvider(
        _Host(), WeatherUnits("fahrenheit", "mph", "inch"),
        session=session, clock=lambda: 1000.0, now=lambda: NOW,
    )

    feature, failed = provider._lookup(*VANCOUVER)

    assert failed is False
    assert feature["properties"]["name"]["en"] == "Vancouver"
    assert session.count() == 2
    # The second box is twice as wide, for a sparse area.
    assert session.calls[1]["params"]["bbox"] == "-127.2629,46.5773,-118.9771,51.9827"


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


# --- failures ---------------------------------------------------------------


def test_a_transient_failure_is_retried_after_a_minute():
    calls = {"n": 0}
    good = _Response(200, _payload("vancouver"))

    def respond(_call):
        calls["n"] += 1
        return _Response(503) if calls["n"] == 1 else good

    provider, session, _host_, clock = _provider(response=respond)

    assert provider.forecast(*VANCOUVER, "default", 7, 0, None).ok is False
    clock.advance(61)

    assert provider.forecast(*VANCOUVER, "default", 7, 0, None).ok is True
    assert session.count() == 2


def test_a_malformed_body_is_a_failure_not_no_coverage():
    # An HTML 200 is not a feature collection: it says nothing about whether a
    # city page is near the point, so it must not read as "no coverage".
    provider, _session_, _host_, _clock = _provider(response=_Response(200, None))

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.no_coverage is False


def test_a_body_without_a_feature_list_is_a_failure():
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, {"features": "nope"})
    )

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert reply.ok is False
    assert reply.no_coverage is False


def test_a_malformed_body_is_not_cached_as_no_coverage():
    calls = {"n": 0}
    good = _Response(200, _payload("vancouver"))

    def respond(_call):
        calls["n"] += 1
        return _Response(200, None) if calls["n"] == 1 else good

    provider, session, _host_, clock = _provider(response=respond)

    assert provider.forecast(*VANCOUVER, "default", 7, 0, None).ok is False
    clock.advance(61)

    assert provider.forecast(*VANCOUVER, "default", 7, 0, None).ok is True
    assert session.count() == 2


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

    # British Columbia is permanently UTC-7 from 2026.
    assert provider._offset(*VANCOUVER, NOW) == timedelta(hours=-7)


# --- the hourly lines -------------------------------------------------------


@pytest.mark.parametrize("name,point", [("vancouver", VANCOUVER), ("ottawa", OTTAWA)])
def test_an_hourly_line_carries_one_description(name, point):
    provider, _session_, host, _clock = _provider(name)

    reply = provider.forecast(*point, "hourly", 7, 0, None)

    # "<hour>: <emoji> <POP>% <description> <temp>° <wind>", as Open-Meteo builds it.
    first = reply.text.splitlines()[0]
    assert first.split(":")[0].endswith(("AM", "PM"))
    assert "Overcast Mainly cloudy" not in first
    assert "Rain A few showers" not in first


@pytest.mark.parametrize("name,point", [("vancouver", VANCOUVER), ("ottawa", OTTAWA)])
def test_the_hourly_reply_packs_as_many_hours_as_open_meteo(name, point):
    provider, _session_, host, _clock = _provider(name)
    host.get_max_message_length = Mock(return_value=150)

    reply = provider.forecast(*point, "hourly", 7, 0, MESSAGE)

    # Open-Meteo fits three or four hours in the same frame.
    assert 3 <= len(reply.text.splitlines()) <= 4
    assert _width(host, reply.text) <= 150


def test_the_hourly_pop_sits_where_open_meteo_puts_it():
    provider, _session_, _host_, _clock = _provider("vancouver")

    reply = provider.forecast(*VANCOUVER, "hourly", 7, 0, MESSAGE)

    # Emoji, then the probability, then the description.
    assert reply.text.splitlines()[0].startswith("3PM: 🌧️ 80% A few showers ")


# --- the evening default reply ----------------------------------------------


def test_the_evening_default_shows_tonight_and_tomorrow():
    provider, _session_, host, _clock = _provider(
        "ottawa_evening", units=WeatherUnits("celsius", "kmh", "mm"), now=lambda: EVENING
    )

    reply = provider.forecast(*OTTAWA, "default", 7, 0, None)

    # The first period is "Tonight", so the label follows the data, not the
    # clock, and the pair becomes tonight's low alone.
    assert reply.text.startswith("Tonight: ")
    assert "| L:3°C" in reply.text
    assert "H:" not in reply.text.split("|")[1]
    # Then tomorrow's own period, with its high and low.
    assert "Tomorrow: " in reply.text
    assert "H:5°C L:0°C" in reply.text
    assert _width(host, reply.text) <= 150


def test_the_period_label_follows_the_period_data():
    provider, _session_, _host_, _clock = _provider()

    # A day period carries a high; a night period does not.
    assert provider._period_key(13, has_high=True) == "today"
    assert provider._period_key(17, has_high=False) == "tonight"
    assert provider._period_key(3, has_high=False) == "overnight"


@pytest.mark.parametrize("when", [
    # 19:00 and midnight in Vancouver, when the clock alone would say night.
    datetime(2026, 11, 27, 3, 0, tzinfo=timezone.utc),
    datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc),
])
def test_the_label_follows_the_data_even_when_the_clock_disagrees(when):
    provider, _session_, _host_, _clock = _provider("vancouver", now=lambda: when)

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    # The first period names itself "Today", so the clock does not relabel it.
    assert reply.text.startswith("Today: ")


def test_a_calm_wind_is_named_rather_than_dropped():
    payload = _payload("vancouver")
    current = payload["features"][2]["properties"]["currentConditions"]
    current["wind"]["speed"]["value"] = {"en": "calm", "fr": "calme"}
    provider, _session_, host, _clock = _provider(
        response=_Response(200, payload), units=WeatherUnits("celsius", "kmh", "mm")
    )

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, None)

    assert host.translate("commands.gwx.calm") in reply.text
    assert "SE" not in reply.text


def test_the_french_evening_period_is_tonight():
    host = _Host(localization={"language": "fr"})
    provider, _session_, _host_, _clock = _provider(
        "ottawa_evening", units=WeatherUnits("celsius", "kmh", "mm"),
        host=host, now=lambda: EVENING,
    )

    # "Ce soir et cette nuit" covers the evening, so it is tonight, not
    # overnight: the same label the English "Tonight" gets.
    assert provider._period_key_from_name("Ce soir et cette nuit") == "tonight"
    assert provider._period_key_from_name("Tonight") == "tonight"
    assert provider._period_key_from_name("Cette nuit") == "overnight"

    # The label itself comes from the catalog, so it is the tonight key's text;
    # what the French data decides is which key that is.
    reply = provider.forecast(*OTTAWA, "default", 7, 0, None)
    assert reply.text.startswith(f"{host.translate('commands.gwx.periods.tonight')}: ")


def test_pressure_follows_the_catalog_unit():
    payload = _payload("vancouver")
    current = payload["features"][2]["properties"]["currentConditions"]
    current["pressure"]["value"] = {"en": 102.1, "fr": 102.1}

    metric, _session_, _host_, _clock = _provider(
        response=_Response(200, payload), units=WeatherUnits("celsius", "kmh", "mm")
    )
    assert "📊1021hPa" in metric.forecast(*VANCOUVER, "default", 7, 0, None).text

    host = _Host()
    host.translator = Translator("ru")  # the catalog that asks for mmHg
    host.response_translator = host.translator
    mercury, _session_, _host_, _clock = _provider(
        response=_Response(200, payload), units=WeatherUnits("celsius", "kmh", "mm"), host=host
    )
    assert "мм рт. ст." in mercury.forecast(*VANCOUVER, "default", 7, 0, None).text


def test_the_wind_direction_is_an_arrow_and_a_compass_point():
    payload = _payload("ottawa")
    current = payload["features"][0]["properties"]["currentConditions"]
    current["wind"]["speed"]["value"] = {"en": 20, "fr": 20}
    current["wind"]["direction"]["value"] = {"en": "ENE", "fr": "ENE"}
    current["wind"]["bearing"]["value"] = {"en": 73.0, "fr": 73.0}
    provider, _session_, host, _clock = _provider(
        response=_Response(200, payload), units=WeatherUnits("celsius", "kmh", "mm")
    )

    reply = provider.forecast(*OTTAWA, "default", 7, 0, None)

    # 73° is ENE, and the arrow is the one Open-Meteo and MET Norway use.
    assert f"➡️{host.translate('common.wind_directions.ENE')}20" in reply.text


def test_the_wind_direction_falls_back_to_the_compass_text():
    payload = _payload("ottawa")
    current = payload["features"][0]["properties"]["currentConditions"]
    current["wind"]["speed"]["value"] = {"en": 20, "fr": 20}
    current["wind"]["direction"]["value"] = {"en": "SW", "fr": "SO"}
    current["wind"].pop("bearing", None)
    provider, _session_, host, _clock = _provider(
        response=_Response(200, payload), units=WeatherUnits("celsius", "kmh", "mm")
    )

    reply = provider.forecast(*OTTAWA, "default", 7, 0, None)

    assert f"↙️{host.translate('common.wind_directions.SW')}20" in reply.text


def test_the_city_page_query_asks_for_a_bounded_page():
    provider, session, _host_, _clock = _provider("vancouver")

    provider._lookup(*VANCOUVER)

    assert session.calls[0]["params"]["limit"] == 50


# --- the budget -------------------------------------------------------------


def test_pressure_goes_before_the_high_low_and_tomorrow():
    provider, _session_, host, _clock = _provider(
        "ottawa_evening", units=WeatherUnits("celsius", "kmh", "mm"), now=lambda: EVENING
    )
    host.get_max_message_length = Mock(return_value=50)

    reply = provider.forecast(*OTTAWA, "default", 7, 0, SimpleNamespace(content="wx Ottawa"))

    # Pressure is the first part to go, and the high/low stays.
    assert _width(host, reply.text) <= 50
    assert "hPa" not in reply.text
    assert "97%RH" in reply.text
    assert "| L:3°C" in reply.text


# --- the repeated hour when the clocks go back ------------------------------


def test_the_hourly_reply_keeps_the_repeated_hour_when_the_clocks_go_back():
    """Ottawa's clocks go back at 06:00Z on 1 November 2026, so 1 AM happens twice."""
    payload = _payload("ottawa")
    properties = payload["features"][0]["properties"]
    template = properties["hourlyForecastGroup"]["hourlyForecasts"][0]
    properties["hourlyForecastGroup"]["hourlyForecasts"] = [
        {**json.loads(json.dumps(template)), "timestamp": stamp}
        for stamp in (
            "2026-11-01T04:00:00Z",
            "2026-11-01T05:00:00Z",
            "2026-11-01T06:00:00Z",
            "2026-11-01T07:00:00Z",
        )
    ]
    # 01:30 EDT: the current hour is the first 1 AM, so the next step is the
    # second one, an hour later on the clock but the same wall time.
    now = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    provider, _session_, _host_, _clock = _provider(
        "ottawa", response=_Response(200, payload), now=lambda: now
    )

    reply = provider.forecast(*OTTAWA, "hourly", 7, 0, MESSAGE)

    labels = [line.split(":")[0] for line in reply.text.splitlines()]
    assert labels == ["1AM", "2AM"]


# --- the search box ---------------------------------------------------------


def test_the_search_box_widens_with_the_latitude():
    """A city page 114 km east at 70N is inside the box; a square one misses it.

    Two degrees of longitude are 76 km at that latitude, so the old square box
    never reached the page however far it widened.
    """
    page = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-93.0, 70.0]},
        "properties": _payload("ottawa")["features"][0]["properties"],
    }

    def respond(call):
        west, south, east, north = (float(part) for part in call["params"]["bbox"].split(","))
        inside = west <= -93.0 <= east and south <= 70.0 <= north
        return _Response(200, {"type": "FeatureCollection", "features": [page] if inside else []})

    provider, session, _host_, _clock = _provider("ottawa", response=respond)

    reply = provider.forecast(70.0, -90.0, "default", 7, 0, MESSAGE)

    assert reply.ok is True
    assert session.count() == 1


# --- the French compass points ----------------------------------------------


@pytest.mark.parametrize("text,degrees", [
    ("O", 270.0), ("ONO", 292.5), ("NO", 315.0),
    ("OSO", 247.5), ("SO", 225.0), ("SSO", 202.5),
])
def test_the_french_compass_points_are_not_mirrored(text, degrees):
    assert COMPASS_DEGREES[text] == degrees


def test_a_french_wind_direction_matches_its_bearing():
    payload = _payload("ottawa")
    current = payload["features"][0]["properties"]["currentConditions"]
    current["wind"]["speed"]["value"] = {"en": 20, "fr": 20}
    current["wind"]["direction"]["value"] = {"en": "W", "fr": "O"}
    current["wind"].pop("bearing", None)
    host = _Host(localization={"language": "fr"})
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, payload),
        units=WeatherUnits("celsius", "kmh", "mm"),
        host=host,
    )

    reply = provider.forecast(*OTTAWA, "default", 7, 0, MESSAGE)

    # "O" is ouest: the arrow points west, not east.
    assert f"⬅️{host.translate('common.wind_directions.W')}20" in reply.text


# --- the budget order -------------------------------------------------------


def _is_subsequence(short: list, long: list) -> bool:
    """Whether every token of *short* appears in *long*, in the same order."""
    remaining = iter(long)
    return all(token in remaining for token in short)


@pytest.mark.parametrize("budget", range(40, 131, 5))
def test_a_dropped_part_keeps_the_rest_in_order(budget):
    """Dropping a part must blank it in place, not shift the survivors along.

    A shifted list moves the condition text after the temperature and the
    humidity into the pressure slot, so the tight line stops being the wide line
    with parts removed.
    """
    provider, _session_, _host_, _clock = _provider("vancouver")
    feature = _payload("vancouver")["features"][0]
    full = provider._current(feature, *VANCOUVER, 400).split(" | ")[0]

    line = provider._current(feature, *VANCOUVER, budget)

    assert line is not None
    assert line.startswith("Today: ")
    assert _is_subsequence(line.split(" | ")[0].split(), full.split())


# --- the pressure unit ------------------------------------------------------


def test_the_pressure_is_rounded_once_after_the_unit_is_chosen():
    """1000.5 hPa is 750 mmHg; rounding to hPa first would give 751."""
    payload = _payload("vancouver")
    # Every city page in the fixture, since the nearest one answers.
    for feature in payload["features"]:
        feature["properties"]["currentConditions"]["pressure"]["value"] = {
            "en": 100.05, "fr": 100.05
        }
    host = _Host()
    # The Russian catalog is the one that asks for mmHg.
    host.translator = Translator("ru")
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, payload),
        units=WeatherUnits("celsius", "kmh", "mm"),
        host=host,
    )

    reply = provider.forecast(*VANCOUVER, "default", 7, 0, MESSAGE)

    assert host.translate('commands.gwx.pressure_mmhg', value=750) in reply.text
