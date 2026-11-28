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
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from modules.i18n import Translator
from modules.weather_providers.alerts_meteoalarm import (
    DEFAULT_BASE_URL,
    FAILURE_TTL,
    GEOMETRY_CACHE_MAX,
    LOCATIONS_TTL,
    LOCATIONS_URL,
    MAX_PAUSE,
    MIN_REQUEST_INTERVAL,
    PAST_DAYS,
    RATE_LIMIT_FLOOR,
    WINDOW_TTL_PAST,
    WINDOW_TTL_TODAY,
    MeteoalarmAlertsProvider,
    _make_session,
    _reset_seconds,
    _retry_after_seconds,
    geometry_contains,
    parse_cap,
    reset_rate_limits,
)
from modules.weather_providers.base import Coverage

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
    host.response_translator = host.translator
    host._count_display_width = lambda text: len(text.encode("utf-8"))
    host.get_max_message_length = Mock(return_value=130)
    host.send_response = AsyncMock(return_value=True)
    host._pace_reply = AsyncMock()
    return host


def _provider(routes=None, token="test-token", language="en", base_url=None):
    # The pause and the budget are shared per origin, so each case starts clean.
    reset_rate_limits()
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


def test_a_malformed_country_polygon_is_skipped_rather_than_raised():
    routes = _routes()
    locations = _json("locations.json")
    broken = {
        "type": "Feature",
        "id": "XX",
        "properties": {"name": "Broken"},
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], "not a position"]]},
    }
    locations["features"] = [broken, *locations["features"]]
    routes[LOCATIONS_URL] = _Response(200, locations)
    provider, _session_, host, _clock = _provider(routes)

    # covers() reads every cached polygon, so a bad ring must never reach it.
    assert provider.covers(LAT, LON) is True
    assert provider.covers(FAR_LAT, FAR_LON) is False
    assert [code for code, _geometry in provider._country_geometries()] == ["NO", "AT", "DE"]
    assert any("unusable geometry" in call.args[0] for call in host.logger.debug.call_args_list)


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


def test_every_window_is_shorter_than_a_day_and_asks_by_sent_date_only():
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
        # Activity is decided from the CAP, not by the query.
        assert "active" not in call["params"]
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


def test_a_locations_body_without_a_feature_list_is_unavailable():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(200, {})
    provider, _session_, host, _clock = _provider(routes)

    # No list is not an empty list: the point is possibly covered, and the
    # lookup reports a fetch failure rather than an all-clear.
    assert provider._country_geometries() is None
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert provider.summary(LAT, LON, 130) is None
    assert any("no feature list" in message for message in _warnings(host))


def test_a_country_body_without_a_feature_list_is_unavailable():
    routes = _routes()
    routes[NO_URL] = lambda params: (
        _Response(200, {"type": "FeatureCollection"})
        if str(params.get("datetime", "")).startswith(TODAY)
        else _Response(204)
    )
    provider, _session_, host, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130) is None
    assert any("no feature list" in message for message in _warnings(host))


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

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
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
    details, _incomplete = provider._cap_details(
        "alert-gale", [_json("NO_page1.json")["features"][0]], {0, 1}
    )
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
    # The expiry goes through alert_format, so it reads as NWS's "until" does.
    assert summary.text == "\U0001f7e0 Wind til Nov 28 10AM; \U0001f7e1 Gale til Nov 28 10AM"


def test_the_summary_truncates_with_a_plus_count():
    provider, _session_, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 40)

    assert summary.count == 2
    assert summary.text.endswith(" +1")


def test_the_full_list_packs_like_the_nws_list_and_paces_them():
    provider, _session_, host, _clock = _provider()
    message = SimpleNamespace(content="wx alerts", sender_id="u")

    assert asyncio.run(provider.send_full_list(message, LAT, LON)) is True

    texts = [call.args[1] for call in host.send_response.call_args_list]
    # A count header, then whole alert lines: the second line does not fit
    # beside the first within 130 columns, so it starts a new message.
    assert len(texts) == 2
    assert texts[0].startswith("2 alerts:\n1. \U0001f7e0 Wind from ")
    assert texts[0].endswith("Aana-Sira - Karmoey")
    assert texts[1].startswith("2. \U0001f7e1 Gale from ")
    assert host._pace_reply.await_count == 1
    assert host.send_response.call_args_list[1].kwargs["skip_user_rate_limit"] is True


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

    # Unknown coverage inside Europe: possibly covered, so the path goes on.
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert provider.coverage_for(FAR_LAT, FAR_LON) is Coverage.NOT_COVERED
    warnings = _warnings(host)
    assert len(warnings) == 1
    assert "rejected the token" in warnings[0]
    assert "s3cr3t-token" not in warnings[0]


def test_a_failure_streak_logs_once_and_resets_on_success():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, host, clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert len(_warnings(host)) == 1

    routes[LOCATIONS_URL] = _Response(200, _json("locations.json"))
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED

    # Past the day-long cache, so the next failure is a fresh streak.
    clock.advance(LOCATIONS_TTL + 60)
    routes[LOCATIONS_URL] = _Response(500, text="<html>Server Error</html>")
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
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


def test_a_cap_document_is_cached_for_the_window_ttl_not_until_it_expires():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 1

    clock.advance(WINDOW_TTL_TODAY - 60)
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 1

    # The document expires in 2026-11-28, but a later Cancel or Update must
    # not hide behind it, so the cache stops at the window TTL.
    clock.advance(120)
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 2


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
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True


# --- unavailable is not "no alerts" -----------------------------------------


def test_an_unavailable_fetch_replies_with_the_error_not_no_alerts():
    routes = _routes()
    routes[NO_URL] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, host, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


def test_a_429_pause_replies_with_the_error_when_nothing_is_cached():
    routes = _routes()
    routes[NO_URL] = _Response(429, text="<html>Too Many Requests</html>", headers={"Retry-After": "60"})
    provider, _session_, host, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


# --- coverage is a tri-state ------------------------------------------------


def test_coverage_is_possibly_covered_inside_europe_when_the_list_fails():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, _host_, _clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert provider.coverage_for(FAR_LAT, FAR_LON) is Coverage.NOT_COVERED


def test_a_stale_locations_list_still_answers_coverage():
    provider, _session_, _host_, clock = _provider()
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED

    provider.session.routes[LOCATIONS_URL] = _Response(500, text="<html>Server Error</html>")
    clock.advance(LOCATIONS_TTL + 60)

    # The stale list says NO covers the point and that 40 N, 16.4 E is in none.
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert provider.coverage_for(40.0, 16.4) is Coverage.NOT_COVERED


# --- summary trimming -------------------------------------------------------


def test_the_summary_trims_a_first_line_that_is_too_long():
    provider, _session_, host, _clock = _provider()
    summary = provider.summary(LAT, LON, 12)

    assert summary.count == 2
    assert "\u2026" in summary.text
    assert host._count_display_width(summary.text) <= 12


# --- failure streaks clear on success ---------------------------------------


def test_the_auth_streak_clears_on_the_next_success():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(401)
    provider, _session_, host, _clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert len(_warnings(host)) == 1

    routes[LOCATIONS_URL] = _Response(200, _json("locations.json"))
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED

    routes[LOCATIONS_URL] = _Response(401)
    provider._locations = None
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert len(_warnings(host)) == 2


def test_the_http_streak_clears_on_the_next_success():
    routes = _routes()
    routes[LOCATIONS_URL] = Mock(side_effect=requests.ConnectionError("down"))
    provider, session, host, _clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert len(_warnings(host)) == 1

    session.routes[LOCATIONS_URL] = _Response(200, _json("locations.json"))
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED

    session.routes[LOCATIONS_URL] = Mock(side_effect=requests.ConnectionError("down"))
    provider._locations = None
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert len(_warnings(host)) == 2


# --- an outline that cannot be fetched --------------------------------------


def test_a_feature_whose_outline_cannot_be_fetched_is_skipped():
    routes = _routes()
    routes[GEOM_GALE] = _Response(500, text="<html>Server Error</html>")
    routes[GEOM_GALE_NO] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, host, _clock = _provider(routes)

    summary = provider.summary(LAT, LON, 130)

    # The bbox hit is not proof, so the gale alert is dropped, not kept.
    assert summary.count == 1
    assert "Gale" not in summary.text
    assert any("outline" in str(call) for call in host.logger.debug.call_args_list)


# --- pagination metadata ----------------------------------------------------


def test_a_non_integer_total_pages_is_treated_as_one_page():
    page = dict(_json("NO_page1.json"))
    page["metadata"] = {"page_size": 100, "total_count": 9, "total_pages": "many", "page": 1}

    def route(params):
        if not str(params.get("datetime", "")).startswith(TODAY):
            return _Response(204)
        return _Response(200, page)

    routes = _routes()
    routes[NO_URL] = route
    provider, session, _host_, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130).count == 2
    assert session.count(NO_URL) == 4


def test_the_page_cap_logs_once_at_debug():
    page = dict(_json("NO_page1.json"))
    page["metadata"] = {"page_size": 100, "total_count": 900, "total_pages": 9, "page": 1}

    def route(params):
        if not str(params.get("datetime", "")).startswith(TODAY):
            return _Response(204)
        return _Response(200, page)

    routes = _routes()
    routes[NO_URL] = route
    provider, session, host, _clock = _provider(routes)

    provider.summary(LAT, LON, 130)

    assert session.count(NO_URL) == 4 + 4
    capped = [call for call in host.logger.debug.call_args_list if "capped at 5" in str(call)]
    assert len(capped) == 1


# --- the rate limit budget --------------------------------------------------


def test_a_429_without_retry_after_uses_the_rate_limit_reset():
    routes = _routes()
    routes[NO_URL] = _Response(
        429, text="<html>Too Many Requests</html>", headers={"X-RateLimit-Reset": "90"}
    )
    provider, session, _host_, clock = _provider(routes)

    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 1

    clock.advance(89)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 1

    # The reset has passed and the gateway answers again.
    routes[NO_URL] = _country_page
    clock.advance(2)
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) == 5


def test_a_low_remaining_budget_serves_cache_until_the_reset():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(
        200, _json("locations.json"),
        headers={"X-RateLimit-Remaining": str(RATE_LIMIT_FLOOR), "X-RateLimit-Reset": "600"},
    )
    provider, session, _host_, clock = _provider(routes)

    # The budget is at the floor, so no window request goes out at all.
    assert provider.summary(LAT, LON, 130) is None
    assert session.count(NO_URL) == 0

    clock.advance(601)
    assert provider.summary(LAT, LON, 130).count == 2
    assert session.count(NO_URL) == 4


def test_a_healthy_remaining_budget_keeps_refreshing():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(
        200, _json("locations.json"),
        headers={"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "600"},
    )
    provider, session, _host_, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130).count == 2
    assert session.count(NO_URL) == 4


# --- the matched area decides which warning is shown ------------------------

CAP_TWO_AREAS = f"{STORAGE}/warnings/alert-two-areas.xml"


def _feature(template, feature_id, alert_id, index_info, geometry):
    """One warning area, cloned from a live feature and pointed at a fixture."""
    feature = json.loads(json.dumps(template))
    feature["id"] = feature_id
    feature["properties"]["OBJECTID"] = feature_id
    feature["properties"]["alertId"] = alert_id
    feature["properties"]["indexInfo"] = index_info
    feature["properties"]["geometryType"] = "exact"
    feature["geometry"] = geometry
    feature["links"] = [{"type": "application/xml", "rel": "xml", "href": CAP_TWO_AREAS}]
    return feature


def _square(west, south, east, north):
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }


def _two_area_page():
    """One alertId with two areas: only the second one contains the point."""
    template = _json("NO_page1.json")["features"][0]
    return {
        "type": "FeatureCollection",
        "features": [
            _feature(template, "f-two-areas-0", "alert-two-areas", 0, _square(10.0, 10.0, 11.0, 11.0)),
            _feature(template, "f-two-areas-1", "alert-two-areas", 1, _square(5.0, 58.0, 7.0, 59.0)),
        ],
    }


def test_the_warning_of_the_matched_area_is_the_one_shown():
    routes = _routes()
    routes[NO_URL] = lambda params: (
        _Response(200, _two_area_page()) if str(params.get("datetime", "")).startswith(TODAY)
        else _Response(204)
    )
    routes[CAP_TWO_AREAS] = _Response(200, text=_text("alert-two-areas.xml"))
    provider, _session_, _host_, _clock = _provider(routes)

    summary = provider.summary(LAT, LON, 130)

    # Both info blocks are English; only indexInfo 1 covers the point.
    assert summary.count == 1
    assert "Flash flood" in summary.text
    assert "Wind" not in summary.text


# --- an incomplete lookup is not an all-clear -------------------------------


def test_a_failed_cap_for_every_candidate_is_unavailable_not_an_all_clear():
    routes = _routes()
    routes[CAP_GALE] = _Response(500, text="<html>Server Error</html>")
    routes[CAP_EXACT] = _Response(500, text="<html>Server Error</html>")
    provider, _session_, host, _clock = _provider(routes)

    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


def test_a_failed_window_does_not_hide_the_alerts_another_window_confirmed():
    routes = _routes()

    def page(params):
        if str(params.get("datetime", "")).startswith(TODAY):
            return _Response(200, _json("NO_page1.json"))
        return _Response(500, text="<html>Server Error</html>")

    routes[NO_URL] = page
    provider, _session_, _host_, _clock = _provider(routes)

    summary = provider.summary(LAT, LON, 130)

    assert summary is not None
    assert summary.count == 2


# --- activity is decided from the CAP, not from the query -------------------


def _cap(identifier, event="Wind", level="2; yellow; Moderate",
         onset="2026-11-26T18:00:00+00:00", expires="2026-11-28T10:00:00+00:00",
         references=None, msg_type="Update"):
    refs = f"<references>{references}</references>" if references else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>{identifier}</identifier>
  <sent>2026-11-27T08:00:00+00:00</sent>
  <msgType>{msg_type}</msgType>
  {refs}
  <info>
    <language>en-GB</language>
    <event>{event}</event>
    <onset>{onset}</onset>
    <expires>{expires}</expires>
    <headline>{event}</headline>
    <parameter><valueName>awareness_level</valueName><value>{level}</value></parameter>
    <parameter><valueName>awareness_type</valueName><value>1; Wind</value></parameter>
  </info>
</alert>"""


def _page_of(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def test_a_warning_that_is_not_active_yet_is_hidden_and_one_that_is_shown():
    template = _json("NO_page1.json")["features"][0]
    future = _feature(template, "f-future", "alert-future", 0, _square(5.0, 58.0, 7.0, 59.0))
    past = _feature(template, "f-past", "alert-past", 0, _square(5.0, 58.0, 7.0, 59.0))
    future["links"] = [{"type": "application/xml", "rel": "xml", "href": f"{STORAGE}/warnings/future.xml"}]
    past["links"] = [{"type": "application/xml", "rel": "xml", "href": f"{STORAGE}/warnings/past.xml"}]

    routes = _routes()
    routes[NO_URL] = lambda params: (
        _Response(200, _page_of(future, past)) if str(params.get("datetime", "")).startswith(TODAY)
        else _Response(204)
    )
    routes[f"{STORAGE}/warnings/future.xml"] = _Response(
        200, text=_cap("alert-future", event="Ice", onset="2026-11-28T06:00:00+00:00")
    )
    routes[f"{STORAGE}/warnings/past.xml"] = _Response(
        200, text=_cap("alert-past", event="Gale", onset="2026-11-26T06:00:00+00:00")
    )
    provider, _session_, _host_, _clock = _provider(routes)

    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 1
    assert "Gale" in summary.text
    assert "Ice" not in summary.text


def test_an_alert_named_in_a_newer_references_is_dropped():
    template = _json("NO_page1.json")["features"][0]
    old = _feature(template, "f-old", "alert-old", 0, _square(5.0, 58.0, 7.0, 59.0))
    new = _feature(template, "f-new", "alert-new", 0, _square(5.0, 58.0, 7.0, 59.0))
    old["links"] = [{"type": "application/xml", "rel": "xml", "href": f"{STORAGE}/warnings/old.xml"}]
    new["links"] = [{"type": "application/xml", "rel": "xml", "href": f"{STORAGE}/warnings/new.xml"}]

    routes = _routes()
    routes[NO_URL] = lambda params: (
        _Response(200, _page_of(old, new)) if str(params.get("datetime", "")).startswith(TODAY)
        else _Response(204)
    )
    routes[f"{STORAGE}/warnings/old.xml"] = _Response(200, text=_cap("alert-old", event="Gale"))
    routes[f"{STORAGE}/warnings/new.xml"] = _Response(
        200,
        text=_cap(
            "alert-new", event="Storm surge", level="3; orange; Severe",
            references="meteorologi@met.no,alert-old,2026-11-27T08:00:00+00:00",
        ),
    )
    provider, _session_, _host_, _clock = _provider(routes)

    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 1
    assert "Storm surge" in summary.text
    assert "Gale" not in summary.text


# --- the pause cannot be bypassed -------------------------------------------


def test_the_gateway_session_never_retries_a_429():
    session = _make_session()
    retry = session.get_adapter(DEFAULT_BASE_URL).max_retries

    assert 429 not in retry.status_forcelist
    assert retry.respect_retry_after_header is False
    assert 500 in retry.status_forcelist


def test_the_pause_is_shared_by_every_provider_on_the_origin():
    routes = _routes()
    routes[NO_URL] = _Response(
        429, text="<html>Too Many Requests</html>", headers={"Retry-After": "120"}
    )
    first, first_session, _host_, clock = _provider(routes)
    first.summary(LAT, LON, 130)
    assert first_session.count(NO_URL) == 1

    # A second provider on the same origin, built without resetting the pause.
    second_session = _Session(routes)
    second = MeteoalarmAlertsProvider(host=_host(), token="test-token", session=second_session)
    second._clock = lambda: clock.monotonic
    second._utcnow = lambda: clock.wall
    second._wait = clock.advance

    assert second.summary(LAT, LON, 130) is None
    assert second_session.calls == []


def test_the_pause_is_rechecked_after_the_pacing_sleep():
    provider, session, _host_, clock = _provider()

    def wait(seconds):
        # Another thread spends the budget while this one is pacing.
        clock.advance(seconds)
        with provider._rate.lock:
            provider._rate.retry_until = clock.monotonic + 60

    provider._wait = wait
    provider._rate.last_request = clock.monotonic

    assert provider.summary(LAT, LON, 130) is None
    assert session.calls == []


# --- credentials ------------------------------------------------------------


def test_the_token_is_not_sent_over_plain_http():
    base = "http://api.meteogate.eu/warnings"
    routes = _routes()
    routes[f"{base}/collections/warnings/locations"] = routes.pop(LOCATIONS_URL)
    routes[f"{base}/collections/warnings/locations/NO"] = routes.pop(NO_URL)
    provider, session, _host_, _clock = _provider(routes, base_url=base)

    provider.summary(LAT, LON, 130)

    api_calls = [call for call in session.calls if call["url"].startswith(base)]
    assert api_calls
    assert all("Authorization" not in call["headers"] for call in api_calls)


# --- bounded caches and shared cold fetches ---------------------------------


def test_two_threads_share_one_cold_locations_fetch():
    routes = _routes()

    def slow_locations(params):
        time.sleep(0.2)
        return _Response(200, _json("locations.json"))

    routes[LOCATIONS_URL] = slow_locations
    provider, session, _host_, _clock = _provider(routes)

    results = []
    threads = [threading.Thread(target=lambda: results.append(provider.coverage_for(LAT, LON)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.count(LOCATIONS_URL) == 1
    assert results == [Coverage.COVERED, Coverage.COVERED]


def test_expired_caps_and_old_windows_are_evicted():
    provider, _session_, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    assert provider._windows and provider._caps

    old_day = clock.wall.date() - timedelta(days=5)
    provider._windows[("NO", old_day)] = (clock.monotonic, [])
    provider._caps[("alert-old", ())] = (clock.wall - timedelta(minutes=1), {"identifier": "alert-old"})

    provider._evict()

    assert ("NO", old_day) not in provider._windows
    assert ("alert-old", ()) not in provider._caps
    assert provider._windows


def test_the_geometry_cache_is_bounded():
    provider, _session_, _host_, _clock = _provider()
    for index in range(GEOMETRY_CACHE_MAX + 5):
        provider._geometries[f"f-{index}"] = _square(0.0, 0.0, 1.0, 1.0)

    provider._evict()

    assert len(provider._geometries) == GEOMETRY_CACHE_MAX


# --- the configured language picks its own info block -----------------------

TWO_LANGUAGES_URL = f"{STORAGE}/warnings/alert-two-languages.xml"
GEOM_TWO_LANGUAGES = f"{STORAGE}/features/f-two-languages.geojson"


def _two_language_routes():
    """One alert with a Norwegian and an English feature, same id and outline."""
    routes = _routes()
    routes[NO_URL] = lambda params: (
        _Response(200, _json("NO_two_languages.json"))
        if str(params.get("datetime", "")).startswith(TODAY)
        else _Response(204)
    )
    routes[TWO_LANGUAGES_URL] = _Response(200, text=_text("alert-two-languages.xml"))
    routes[GEOM_TWO_LANGUAGES] = _Response(200, _json("f-exact.geojson"))
    return routes


def test_the_configured_language_picks_its_own_info_block():
    provider, _session_, _host_, _clock = _provider(_two_language_routes(), language="en")

    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 1
    assert "Gale" in summary.text
    assert "Kuling" not in summary.text


def test_switching_language_on_a_warm_cache_picks_the_new_block():
    provider, session, host, _clock = _provider(_two_language_routes(), language="en")
    assert "Gale" in provider.summary(LAT, LON, 130).text

    # The same provider, the same caches, a different configured language.
    host.bot.config.set("Localization", "language", "no")
    summary = provider.summary(LAT, LON, 130)

    assert "Kuling" in summary.text
    assert "Gale" not in summary.text
    # The CAP document itself is not refetched: only the parse is per language.
    assert session.count(TWO_LANGUAGES_URL) == 1


def test_a_document_failing_in_the_new_language_is_retried_after_a_minute():
    """A copy cached as good in English must not keep its ten minutes once it fails in Norwegian."""
    import re

    full = _text("alert-two-languages.xml")
    blocks = full.split("<info>")
    blocks = [
        re.sub(r"<expires>.*?</expires>", "", block, flags=re.S) if "<language>no</language>" in block else block
        for block in blocks
    ]
    broken_norwegian = "<info>".join(blocks)
    assert broken_norwegian != full
    routes = _two_language_routes()
    routes[TWO_LANGUAGES_URL] = _Response(200, text=broken_norwegian)
    provider, session, host, clock = _provider(routes, language="en")
    assert "Gale" in provider.summary(LAT, LON, 130).text

    host.bot.config.set("Localization", "language", "no")
    assert provider.summary(LAT, LON, 130) is None
    routes[TWO_LANGUAGES_URL] = _Response(200, text=full)
    clock.advance(30)
    assert provider.summary(LAT, LON, 130) is None
    assert session.count(TWO_LANGUAGES_URL) == 1

    clock.advance(31)
    summary = provider.summary(LAT, LON, 130)

    assert session.count(TWO_LANGUAGES_URL) == 2
    assert summary is not None and "Kuling" in summary.text


def test_the_norwegian_language_picks_the_norwegian_block():
    provider, _session_, _host_, _clock = _provider(_two_language_routes(), language="no")

    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 1
    assert "Kuling" in summary.text
    assert "Gale" not in summary.text


def test_a_language_the_alert_lacks_falls_back_to_english():
    provider, _session_, _host_, _clock = _provider(_two_language_routes(), language="de")

    summary = provider.summary(LAT, LON, 130)

    assert summary.count == 1
    assert "Gale" in summary.text


# --- regressions: a broken document never costs the forecast -----------------

CAP_CANCEL_GALE = f"{STORAGE}/warnings/alert-cancel-gale.xml"

OFFSET_CAP = """<?xml version="1.0" encoding="utf-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>2.49.0.1.578.0.offset</identifier>
  <sent>2026-11-27T08:00:00+01:00</sent>
  <msgType>Alert</msgType>
  <info>
    <language>en-GB</language>
    <event>Gale</event>
    <onset>2026-11-26T18:00:00+01:00</onset>
    <expires>2026-11-28T10:00:00+01:00</expires>
    <headline>Gale, yellow level, Aana-Sira - Karmoey</headline>
    <parameter><valueName>awareness_level</valueName><value>2; yellow; Moderate</value></parameter>
    <parameter><valueName>awareness_type</valueName><value>1; Wind</value></parameter>
  </info>
</alert>
"""

REGIONAL_CAP = """<?xml version="1.0" encoding="utf-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>2.49.0.1.578.0.regional</identifier>
  <sent>2026-11-27T08:00:00+00:00</sent>
  <msgType>Alert</msgType>
  <info>
    <language>fr-FR</language>
    <event>Vent fort</event>
    <expires>2026-11-28T10:00:00+00:00</expires>
  </info>
  <info>
    <language>pt-PT</language>
    <event>Vento forte</event>
    <expires>2026-11-28T10:00:00+00:00</expires>
  </info>
</alert>
"""


def test_a_broken_locations_body_is_unavailable_not_a_crash():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(200, [])
    routes[NO_URL] = _Response(200, {"features": [None]})
    provider, _session_, host, _clock = _provider(routes)

    # A list where an object belongs, and a null feature: no alerts, no raise.
    assert provider.summary(LAT, LON, 130) is None
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert _warnings(host)


def test_a_failed_shared_refresh_is_unavailable_to_every_caller():
    provider, _session_, host, _clock = _provider()

    def boom():
        raise RuntimeError("boom")

    # The owner's exception becomes None for it and for every waiter.
    assert provider._shared(("window", "NO", TODAY), boom) is None
    assert _warnings(host)

    # A None refresh is an incomplete window and an incomplete CAP, never a raise.
    provider._shared = lambda key, fetch: None
    assert provider._window_features(
        "NO", date(2026, 11, 27), f"{TODAY}T00:00:00Z/{TODAY}T12:00:00Z", WINDOW_TTL_TODAY
    ) == ([], True)
    assert provider._cap_details("alert-gale", [], {1}) == (None, True)


def test_a_warm_summary_fetches_no_documents_and_does_not_pace():
    provider, session, _host_, _clock = _provider()
    provider.summary(LAT, LON, 130)
    storage_before = [url for url in session.urls() if url.startswith(STORAGE)]
    assert storage_before

    waits = []
    provider._wait = waits.append
    provider.summary(LAT, LON, 130)

    assert [url for url in session.urls() if url.startswith(STORAGE)] == storage_before
    assert waits == []


def test_times_show_in_the_alerts_own_offset():
    provider, _session_, _host_, _clock = _provider()
    details = parse_cap(OFFSET_CAP, "en")
    details["active"] = True

    # 10:00+01:00 is 10AM where the alert is, not 9AM UTC.
    assert provider._summary_line(details) == "\U0001f7e1 Gale til Nov 28 10AM"
    assert provider._full_alert_text(details, 1, 130) == (
        "1. \U0001f7e1 Gale from Nov 26 6PM til Nov 28 10AM Aana-Sira - Karmoey"
    )


def _cancel_routes():
    """The usual page plus a Cancel that names the gale alert it replaces."""
    routes = _routes()
    page = _json("NO_page1.json")
    page["features"].append({
        "id": "f-cancel-gale",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[5.0, 58.0], [7.0, 58.0], [7.0, 59.0], [5.0, 59.0], [5.0, 58.0]]],
        },
        "properties": {"alertId": "alert-cancel-gale", "indexInfo": 1, "geometryType": "exact"},
        "links": [{"rel": "xml", "href": CAP_CANCEL_GALE}],
    })
    routes[NO_URL] = lambda params: (
        _Response(200, page)
        if str(params.get("datetime", "")).startswith(TODAY)
        else _Response(204)
    )
    routes[CAP_CANCEL_GALE] = _Response(200, text=_text("alert-cancel-gale.xml"))
    return routes


def test_a_cancel_removes_the_alert_it_references():
    gale = "2.49.0.1.578.0.20261127070414.016"

    plain, _session_, _host_, _clock = _provider()
    assert gale in [alert["identifier"] for alert in plain._alerts_for(LAT, LON)]

    provider, _session_, _host_, _clock = _provider(_cancel_routes())
    assert gale not in [alert["identifier"] for alert in provider._alerts_for(LAT, LON)]


def test_a_regional_language_matches_its_primary_subtag():
    assert parse_cap(REGIONAL_CAP, "fr-ca")["event"] == "Vent fort"
    assert parse_cap(REGIONAL_CAP, "pt-br")["event"] == "Vento forte"
    assert parse_cap(REGIONAL_CAP, "fr-FR")["event"] == "Vent fort"


def test_a_rate_limit_pause_is_clamped_and_epoch_values_are_absolute():
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

    assert _retry_after_seconds({"Retry-After": "999999"}, now) == MAX_PAUSE
    assert _retry_after_seconds({"Retry-After": str(now.timestamp() + 120)}, now) == pytest.approx(120)
    assert _reset_seconds({"X-RateLimit-Reset": "999999"}, 300) == MAX_PAUSE
    assert _reset_seconds({"X-RateLimit-Reset": str(time.time() + 60)}, 300) == pytest.approx(60, abs=5)


def test_two_providers_with_the_same_origin_and_token_share_their_caches():
    reset_rate_limits()
    host = _host()
    first_session, second_session = _Session(_routes()), _Session(_routes())
    first = MeteoalarmAlertsProvider(host=host, token="shared", session=first_session)
    second = MeteoalarmAlertsProvider(host=host, token="shared", session=second_session)
    other = MeteoalarmAlertsProvider(host=host, token="different", session=_Session(_routes()))
    for provider in (first, second, other):
        provider._wait = lambda seconds: None

    assert first._cache is second._cache
    assert other._cache is not first._cache
    # The session is per thread, not shared behind a lock.
    assert not hasattr(first, "_session_lock")

    first.summary(LAT, LON, 130)
    second.summary(LAT, LON, 130)

    assert first_session.count(LOCATIONS_URL) == 1
    assert second_session.count(LOCATIONS_URL) == 0


def test_each_thread_gets_its_own_session_and_no_lock_is_held_during_http():
    reset_rate_limits()
    host = _host()
    provider = MeteoalarmAlertsProvider(host=host, token="shared")
    provider._wait = lambda seconds: None
    seen = []
    in_flight = threading.Event()
    release = threading.Event()

    class _SlowSession:
        def get(self, url, params=None, headers=None, timeout=None):
            # The cache lock must be free while a request is in flight.
            assert provider._lock.acquire(blocking=False) is True
            provider._lock.release()
            seen.append(threading.current_thread().name)
            in_flight.set()
            release.wait(5)
            return _Response(200, _json("locations.json"))

    provider._session_for_thread = lambda: _SlowSession()

    worker = threading.Thread(target=provider._country_geometries, name="worker")
    worker.start()
    assert in_flight.wait(5) is True
    # The main thread is not blocked by the worker's request.
    assert provider._lock.acquire(blocking=False) is True
    provider._lock.release()
    release.set()
    worker.join(5)

    assert seen == ["worker"]


def test_the_dead_coverage_state_and_translation_keys_are_gone():
    assert not hasattr(Coverage, "UNKNOWN")

    root = Path(__file__).resolve().parents[2]
    keys = json.loads((root / "translations" / "en.json").read_text(encoding="utf-8"))
    meteoalarm = keys["commands"]["wx"]["meteoalarm"]
    assert "from" not in meteoalarm
    assert "until" not in meteoalarm


# --- a CAP document that does not parse -------------------------------------


def test_an_html_cap_is_retried_after_a_minute():
    """An HTML 200 must not be cached for the window TTL and hide the alert."""
    routes = _routes()
    routes[CAP_GALE] = _Response(200, text="<html>Bad Gateway</html>")
    provider, session, _host_, clock = _provider(routes)

    first = provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 1
    # The gale alert is lost, the other one still answers.
    assert first is not None and first.count == 1

    clock.advance(FAILURE_TTL + 1)
    routes[CAP_GALE] = _Response(200, text=_text("alert-gale.xml"))
    second = provider.summary(LAT, LON, 130)

    assert session.count(CAP_GALE) == 2
    assert second is not None and second.count == 2


def test_a_cached_html_cap_keeps_its_original_deadline():
    """A cache hit must not renew the failure TTL, or steady requests hide the alert."""
    routes = _routes()
    routes[CAP_GALE] = _Response(200, text="<html>Bad Gateway</html>")
    provider, session, _host_, clock = _provider(routes)

    provider.summary(LAT, LON, 130)
    routes[CAP_GALE] = _Response(200, text=_text("alert-gale.xml"))
    clock.advance(30)
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_GALE) == 1

    clock.advance(31)
    recovered = provider.summary(LAT, LON, 130)

    assert session.count(CAP_GALE) == 2
    assert recovered is not None and recovered.count == 2


@pytest.mark.parametrize("geometry", [None, {}])
def test_an_empty_country_geometry_counts_as_unusable(geometry):
    """A null or empty geometry must not become a day-long "not covered"."""
    locations = _json("locations.json")
    for feature in locations["features"]:
        if feature.get("id") == "NO":
            feature["geometry"] = geometry
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(200, locations)
    provider, session, _host_, clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    provider.summary(LAT, LON, 130)
    assert session.count(NO_URL) > 0

    clock.advance(FAILURE_TTL + 1)
    provider.coverage_for(LAT, LON)
    assert session.count(LOCATIONS_URL) == 2


# --- a country polygon that does not parse ----------------------------------


def test_a_malformed_country_polygon_is_retried_after_a_minute():
    """A list with a country missing is cached briefly, not for a day."""
    locations = _json("locations.json")
    for feature in locations["features"]:
        if feature.get("id") == "NO":
            feature["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0], [1]]]}
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(200, locations)
    provider, session, _host_, clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert session.count(LOCATIONS_URL) == 1

    clock.advance(FAILURE_TTL + 1)
    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    assert session.count(LOCATIONS_URL) == 2


def test_a_point_outside_every_valid_polygon_is_possibly_covered_in_europe():
    """Norway's polygon is unreadable, so the point cannot be ruled out."""
    locations = _json("locations.json")
    for feature in locations["features"]:
        if feature.get("id") == "NO":
            feature["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0], [1]]]}
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(200, locations)
    provider, _session_, _host_, _clock = _provider(routes)

    assert provider.coverage_for(LAT, LON) is Coverage.COVERED
    # Outside Europe the missing polygon changes nothing.
    assert provider.coverage_for(FAR_LAT, FAR_LON) is Coverage.NOT_COVERED
