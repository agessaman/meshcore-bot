"""Meteoalarm alerts, driven entirely from fixtures built from the live API.

No test here touches the network: the session is a fake that answers from
``tests/fixtures/meteoalarm/`` and records every call, and the provider's two
clocks are replaced so the cache TTLs can be walked forward. The fixtures keep
the live field names and shapes, with every pre-signed storage href replaced by
a placeholder.
"""

import asyncio
import configparser
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from modules.i18n import Translator
from modules.weather_providers.alerts_meteoalarm import (
    DEFAULT_BASE_URL,
    LOCATIONS_TTL,
    LOCATIONS_URL,
    MIN_REQUEST_INTERVAL,
    PAST_DAYS,
    WINDOW_TTL_PAST,
    WINDOW_TTL_TODAY,
    MeteoalarmAlertsProvider,
    geometry_contains,
    parse_cap,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "meteoalarm"
NO_URL = f"{DEFAULT_BASE_URL}/collections/warnings/locations/NO"
STORAGE = "https://storage.example.invalid"
CAP_GALE = f"{STORAGE}/warnings/alert-gale.xml"
CAP_EXACT = f"{STORAGE}/warnings/alert-exact.xml"
CAP_BBOX_MISS = f"{STORAGE}/warnings/alert-bbox-miss.xml"
CAP_EXPIRED = f"{STORAGE}/warnings/alert-expired.xml"
CAP_CANCEL = f"{STORAGE}/warnings/alert-cancel.xml"
CAP_LOW = f"{STORAGE}/warnings/alert-low.xml"
GEOM_GALE = f"{STORAGE}/features/f-gale-en.geojson"
GEOM_GALE_NO = f"{STORAGE}/features/f-gale-no.geojson"
GEOM_BBOX_MISS = f"{STORAGE}/features/f-bbox-miss.geojson"

# The point every fixture is built around: inside the Norwegian bbox and inside
# the exact outline behind it.
LAT, LON = 58.4, 6.0
# Inside no fixture country polygon.
FAR_LAT, FAR_LON = 10.0, 10.0

TODAY = "2026-11-27"


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Response:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    """A fake session: URL -> response, or a callable taking the query parameters."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        route = self.routes.get(url)
        if route is None:
            return _Response(404)
        if callable(route):
            return route(params or {})
        return route

    def count(self, url):
        return sum(1 for call in self.calls if call["url"] == url)

    def urls(self):
        return [call["url"] for call in self.calls]


class _Clock:
    """A fake monotonic clock and wall clock that move together."""

    def __init__(self, wall=f"{TODAY}T12:00:00+00:00"):
        self.monotonic = 1000.0
        self.wall = datetime.fromisoformat(wall)

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def _country_page(params):
    """Today's window carries the page; the three past days are empty (204)."""
    if str(params.get("datetime", "")).startswith(TODAY):
        return _Response(200, _json("NO_page1.json"))
    return _Response(204)


def _routes():
    return {
        LOCATIONS_URL: _Response(200, _json("locations.json")),
        NO_URL: _country_page,
        CAP_GALE: _Response(200, text=_text("alert-gale.xml")),
        CAP_EXACT: _Response(200, text=_text("alert-exact.xml")),
        CAP_BBOX_MISS: _Response(200, text=_text("alert-bbox-miss.xml")),
        CAP_EXPIRED: _Response(200, text=_text("alert-expired.xml")),
        CAP_CANCEL: _Response(200, text=_text("alert-cancel.xml")),
        CAP_LOW: _Response(200, text=_text("alert-low.xml")),
        GEOM_GALE: _Response(200, _json("f-exact.geojson")),
        GEOM_GALE_NO: _Response(200, _json("f-exact.geojson")),
        GEOM_BBOX_MISS: _Response(200, _json("f-bbox-miss.geojson")),
    }


def _host(language="en"):
    config = configparser.ConfigParser()
    config.read_dict({
        "Bot": {"bot_tx_rate_limit_seconds": "1.5"},
        "Localization": {"language": language},
    })
    host = Mock()
    host.bot = SimpleNamespace(config=config)
    host.translator = Translator("en")
    host.translate = host.translator.translate
    host._count_display_width = lambda text: len(text.encode("utf-8"))
    host.get_max_message_length = Mock(return_value=130)
    host.send_response = AsyncMock(return_value=True)
    host._pace_reply = AsyncMock()
    return host


def _provider(routes=None, token="test-token", language="en", base_url=None):
    session = _Session(routes if routes is not None else _routes())
    host = _host(language)
    provider = MeteoalarmAlertsProvider(host=host, token=token, session=session, base_url=base_url)
    clock = _Clock()
    provider._clock = lambda: clock.monotonic
    provider._utcnow = lambda: clock.wall
    # The pacing sleep moves the fake clock instead of the wall clock.
    provider._wait = clock.advance
    return provider, session, host, clock


def _warnings(host):
    return [call.args[0] for call in host.logger.warning.call_args_list]


def _windows(session):
    return [call["params"].get("datetime") for call in session.calls if call["url"] == NO_URL]


# --- geometry ---------------------------------------------------------------


def test_geometry_contains_handles_polygons_and_multipolygons():
    square = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
    assert geometry_contains(square, 1, 1)
    assert not geometry_contains(square, 3, 1)
    multi = {"type": "MultiPolygon", "coordinates": [square["coordinates"], [[[5, 5], [6, 5], [6, 6], [5, 5]]]]}
    assert geometry_contains(multi, 1, 1)
    assert geometry_contains(multi, 5.5, 5.2)
    assert not geometry_contains(None, 1, 1)


# --- covers -----------------------------------------------------------------


def test_covers_is_true_inside_a_country_polygon_and_false_outside():
    provider, session, _host_, _clock = _provider()
    assert provider.covers(LAT, LON) is True
    assert provider.covers(FAR_LAT, FAR_LON) is False
    assert session.count(LOCATIONS_URL) == 1


def test_the_locations_list_skips_the_all_feature():
    provider, _session_, _host_, _clock = _provider()
    countries = provider._country_geometries()
    assert [code for code, _geometry in countries] == ["NO", "AT", "DE"]


# --- windows ----------------------------------------------------------------


def test_a_cold_lookup_asks_for_one_window_per_day():
    provider, session, _host_, _clock = _provider()
    provider.summary(LAT, LON, 130)

    windows = _windows(session)
    assert len(windows) == 1 + PAST_DAYS
    assert windows[0] == f"{TODAY}T00:00:00Z/{TODAY}T12:00:00Z"
    assert windows[1] == "2026-11-26T00:00:00Z/2026-11-26T23:59:59Z"
    assert windows[2] == "2026-11-25T00:00:00Z/2026-11-25T23:59:59Z"
    assert windows[3] == "2026-11-24T00:00:00Z/2026-11-24T23:59:59Z"


def test_every_window_is_shorter_than_a_day_and_sends_active_now():
    provider, session, _host_, _clock = _provider()
    provider.summary(LAT, LON, 130)

    for call in session.calls:
        if call["url"] != NO_URL:
            continue
        start, end = call["params"]["datetime"].split("/")
        span = datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(
            start.replace("Z", "+00:00")
        )
        assert timedelta(0) < span < timedelta(hours=24)
        active = call["params"]["active"].split("/")
        assert active[0] == active[1]
        assert active[0].startswith(TODAY)
        assert call["params"]["page"] == 1


def test_todays_window_is_cached_for_ten_minutes_and_past_days_for_an_hour():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 4

    clock.advance(WINDOW_TTL_TODAY - 60)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 4

    clock.advance(120)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 5

    clock.advance(WINDOW_TTL_PAST + 60)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 9


def test_a_204_means_no_warnings_and_is_never_parsed_as_json():
    routes = _routes()
    routes[NO_URL] = _Response(204)
    provider, _session_, host, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130) is None
    assert _warnings(host) == []


def test_pagination_follows_total_pages():
    page_two = {
        "type": "FeatureCollection",
        "features": [f for f in _json("NO_page1.json")["features"] if f["id"] == "f-exact"],
        "metadata": {"page_size": 100, "total_count": 2, "total_pages": 2, "page": 2},
    }
    first = dict(_json("NO_page1.json"))
    first["metadata"] = {"page_size": 100, "total_count": 2, "total_pages": 2, "page": 1}
    first["features"] = [f for f in first["features"] if f["id"] == "f-gale-en"]

    def route(params):
        if not str(params.get("datetime", "")).startswith(TODAY):
            return _Response(204)
        return _Response(200, first if params.get("page") == 1 else page_two)

    routes = _routes()
    routes[NO_URL] = route
    provider, session, _host_, _clock = _provider(routes)
    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 2
    assert session.count(NO_URL) == 4 + 1


# --- throttling -------------------------------------------------------------


def test_a_429_honors_retry_after_and_serves_stale_data():
    provider, session, host, clock = _provider()
    assert provider.summary(LAT, LON, 130).count == 2

    routes = _routes()
    routes[NO_URL] = _Response(429, text="<html>Too Many Requests</html>", headers={"Retry-After": "120"})
    provider.session.routes = routes
    clock.advance(WINDOW_TTL_TODAY + 60)

    # The window is stale, the fetch is refused, and the cached alerts stand.
    assert provider.summary(LAT, LON, 130).count == 2
    assert len(_warnings(host)) == 1
    assert "rate limited" in _warnings(host)[0]
    assert session.count(NO_URL) == 5

    # Nothing goes out while the pause lasts.
    clock.advance(60)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 5

    clock.advance(120)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 6


def test_a_429_without_retry_after_pauses_for_five_minutes():
    routes = _routes()
    routes[NO_URL] = _Response(429, text="<html>Too Many Requests</html>")
    provider, session, _host_, clock = _provider(routes)

    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 1

    clock.advance(299)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 1

    clock.advance(2)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 2


def test_a_429_with_an_http_date_retry_after():
    routes = _routes()
    routes[NO_URL] = _Response(
        429, text="<html>Too Many Requests</html>",
        headers={"Retry-After": "Fri, 27 Nov 2026 12:02:00 GMT"},
    )
    provider, session, _host_, clock = _provider(routes)

    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 1

    clock.advance(118)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 1

    # The date has passed, so every window is asked for again.
    clock.advance(2)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 5


def test_requests_are_spaced_at_least_a_second_apart():
    provider, session, _host_, clock = _provider()
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    provider._wait = wait

    provider.summary(LAT, LON, 130)

    assert session.count(NO_URL) == 4
    assert waits
    assert all(wait == pytest.approx(MIN_REQUEST_INTERVAL) for wait in waits)


def test_an_html_error_body_does_not_raise():
    routes = {url: _Response(500, text="<html>Server Error</html>") for url in _routes()}
    provider, _session_, host, _clock = _provider(routes)

    assert provider.covers(LAT, LON) is False
    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
    assert _warnings(host)


# --- matching ---------------------------------------------------------------


def test_features_of_one_alert_are_grouped_and_matched_anywhere():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    # alert-gale has two features, one per CAP language, and is one alert.
    assert summary.count == 2
    assert session.count(CAP_GALE) == 1


def test_a_bbox_hit_whose_exact_outline_misses_is_rejected():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert "Rain" not in summary.text
    assert session.count(GEOM_BBOX_MISS) == 1
    assert session.count(CAP_BBOX_MISS) == 0


def test_an_exact_outline_is_cached_by_feature_id():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    assert session.count(GEOM_GALE) == 1

    clock.advance(WINDOW_TTL_TODAY + 60)
    provider.summary(LAT, LON, 130)
    assert session.count(GEOM_GALE) == 1


def test_a_superseded_alert_is_skipped():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 2
    assert not any("alert-superseded" in url for url in session.urls())


def test_an_expired_alert_is_skipped():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert "Flood" not in summary.text
    assert session.count(CAP_EXPIRED) == 1


def test_a_cancelled_alert_is_skipped():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert session.count(CAP_CANCEL) == 1
    assert summary.count == 2


def test_a_level_one_alert_is_skipped():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert "Ice" not in summary.text
    assert session.count(CAP_LOW) == 1


def test_a_feature_without_geometry_is_skipped_with_a_debug_line():
    provider, _session_, host, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 2
    assert any("no geometry" in str(call) for call in host.logger.debug.call_args_list)


def test_no_alerts_returns_none():
    routes = _routes()
    routes[NO_URL] = _Response(204)
    provider, _session_, _host_, _clock = _provider(routes)
    assert provider.summary(LAT, LON, 130) is None


# --- CAP --------------------------------------------------------------------


@pytest.mark.parametrize("language,event", [("en", "Gale"), ("no", "Kuling"), ("de", "Gale")])
def test_the_cap_info_block_follows_the_configured_language(language, event):
    provider, _session_, _host_, _clock = _provider(language=language)
    details = provider._cap_details("alert-gale", [_json("NO_page1.json")["features"][0]])
    assert details["event"] == event


def test_parse_cap_falls_back_to_english_then_to_the_first_info_block():
    details = parse_cap(_text("alert-gale.xml"), "fr")
    assert details["event"] == "Gale"
    assert details["msg_type"] == "Update"
    assert details["level"] == 2
    assert details["awareness_type"] == "Wind"

    # Without an English block the first one stands, whatever the language.
    text = _text("alert-gale.xml")
    english = text.split("<info>")[2]
    only_norwegian = text.split("<info>")[0] + "<info>" + text.split("<info>")[1].split("</info>")[0] + "</info>\n</alert>\n"
    assert english
    assert parse_cap(only_norwegian, "fr")["event"] == "Kuling"


# --- formatting -------------------------------------------------------------


def test_the_summary_orders_by_level_and_names_the_expiry():
    provider, _session_, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 2
    assert summary.text == "\U0001f7e0 Wind until 28 Nov 10:00; \U0001f7e1 Gale until 28 Nov 10:00"


def test_the_summary_truncates_with_a_plus_count():
    provider, _session_, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 40)

    assert summary.count == 2
    assert summary.text.endswith(" +1")


def test_the_full_list_sends_one_message_per_alert_and_paces_them():
    provider, _session_, host, _clock = _provider()
    message = SimpleNamespace(content="wx alerts", sender_id="u")

    assert asyncio.run(provider.send_full_list(message, LAT, LON)) is True

    texts = [call.args[1] for call in host.send_response.call_args_list]
    assert len(texts) == 2
    assert texts[0].startswith("1/2 \U0001f7e0 Wind")
    assert texts[1].startswith("2/2 \U0001f7e1 Gale")
    assert host._pace_reply.await_count == 1


def test_no_alerts_sends_the_nws_text():
    routes = _routes()
    routes[NO_URL] = _Response(204)
    provider, _session_, host, _clock = _provider(routes)

    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
    assert host.send_response.call_args.args[1] == "No weather alerts"


# --- failures ---------------------------------------------------------------


def test_a_rejected_token_logs_one_warning_without_the_token():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(401)
    provider, _session_, host, _clock = _provider(routes, token="s3cr3t-token")

    assert provider.covers(LAT, LON) is False
    assert provider.covers(LAT, LON) is False
    warnings = _warnings(host)
    assert len(warnings) == 1
    assert "rejected the token" in warnings[0]
    assert "s3cr3t-token" not in warnings[0]


def test_a_failure_streak_logs_once_and_resets_on_success():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, host, clock = _provider(routes)

    assert provider.covers(LAT, LON) is False
    assert provider.covers(LAT, LON) is False
    assert len(_warnings(host)) == 1

    routes[LOCATIONS_URL] = _Response(200, _json("locations.json"))
    assert provider.covers(LAT, LON) is True

    # Past the day-long cache, so the next failure is a fresh streak.
    clock.advance(LOCATIONS_TTL + 60)
    routes[LOCATIONS_URL] = _Response(500, text="<html>Server Error</html>")
    assert provider.covers(LAT, LON) is False
    assert len(_warnings(host)) == 2


def test_a_failed_country_fetch_answers_with_no_alerts():
    routes = _routes()
    routes[NO_URL] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, host, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130) is None
    assert len(_warnings(host)) == 1


def test_the_locations_list_is_cached_for_a_day():
    provider, session, _host_, clock = _provider()
    provider.covers(LAT, LON)
    assert session.count(LOCATIONS_URL) == 1

    clock.advance(LOCATIONS_TTL - 60)
    provider.covers(LAT, LON)
    assert session.count(LOCATIONS_URL) == 1

    clock.advance(120)
    provider.covers(LAT, LON)
    assert session.count(LOCATIONS_URL) == 2


def test_a_cap_document_is_cached_until_it_expires():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 1

    clock.advance(WINDOW_TTL_TODAY + 60)
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 1


# --- the token --------------------------------------------------------------


def test_the_token_goes_only_to_the_api_host():
    provider, session, _host_, _clock = _provider(token="s3cr3t-token")
    provider.summary(LAT, LON, 130)

    api_calls = [call for call in session.calls if call["url"].startswith(DEFAULT_BASE_URL)]
    storage_calls = [call for call in session.calls if call["url"].startswith(STORAGE)]
    assert api_calls and storage_calls
    for call in api_calls:
        assert call["headers"].get("Authorization") == "Bearer s3cr3t-token"
    for call in storage_calls:
        assert "Authorization" not in call["headers"]


def test_the_token_never_reaches_a_log_or_a_url():
    provider, session, host, _clock = _provider(token="s3cr3t-token")
    provider.summary(LAT, LON, 130)

    for call in session.calls:
        assert "s3cr3t-token" not in call["url"]
        assert "s3cr3t-token" not in json.dumps(call["params"])

    for level in ("debug", "info", "warning", "error", "critical"):
        for call in getattr(host.logger, level).call_args_list:
            assert "s3cr3t-token" not in str(call)


def test_signed_storage_urls_are_not_logged_above_debug():
    provider, _session_, host, _clock = _provider()
    provider.summary(LAT, LON, 130)

    for level in ("info", "warning", "error", "critical"):
        for call in getattr(host.logger, level).call_args_list:
            assert STORAGE not in str(call)


# --- base URL ---------------------------------------------------------------


def test_the_default_base_url_is_the_meteogate_gateway():
    provider, session, _host_, _clock = _provider()
    provider.summary(LAT, LON, 130)

    assert DEFAULT_BASE_URL == "https://api.meteogate.eu/warnings"
    assert provider.base_url == DEFAULT_BASE_URL
    assert session.count(f"{DEFAULT_BASE_URL}/collections/warnings/locations") == 1
    assert session.count(NO_URL) == 4


def test_a_custom_base_url_with_a_trailing_slash_is_used():
    base = "https://api.meteoalarm.org/edr/v1/"
    routes = _routes()
    routes["https://api.meteoalarm.org/edr/v1/collections/warnings/locations"] = _Response(
        200, _json("locations.json")
    )
    routes["https://api.meteoalarm.org/edr/v1/collections/warnings/locations/NO"] = _country_page
    provider, session, _host_, _clock = _provider(routes, token="s3cr3t-token", base_url=base)
    provider.summary(LAT, LON, 130)

    assert provider.base_url == "https://api.meteoalarm.org/edr/v1"
    assert session.count("https://api.meteoalarm.org/edr/v1/collections/warnings/locations") == 1
    assert session.count("https://api.meteoalarm.org/edr/v1/collections/warnings/locations/NO") == 4
    assert not any("api.meteogate.eu" in url for url in session.urls())
    for call in session.calls:
        if call["url"].startswith("https://api.meteoalarm.org"):
            assert call["headers"].get("Authorization") == "Bearer s3cr3t-token"
        else:
            assert "Authorization" not in call["headers"]


def test_the_locations_list_is_fetched_without_a_token_too():
    provider, session, _host_, _clock = _provider(token="")
    assert provider.covers(LAT, LON) is True
    locations = [call for call in session.calls if call["url"] == LOCATIONS_URL]
    assert locations
    assert all("Authorization" not in call["headers"] for call in locations)


def test_a_failing_fetch_never_raises_into_the_command():
    routes = {url: _Response(500, text="<html>Server Error</html>") for url in _routes()}
    provider, _session_, _host_, _clock = _provider(routes, token="s3cr3t-token")
    assert provider.covers(LAT, LON) is False
    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
