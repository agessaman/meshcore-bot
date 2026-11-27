"""Meteoalarm alerts, driven entirely from fixtures built from the OpenAPI spec.

No test here touches the network: the session is a fake that answers from
``tests/fixtures/meteoalarm/`` and records every call, and the provider's two
clocks are replaced so the cache TTLs can be walked forward.
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
    COUNTRY_TTL,
    COUNTRY_URL,
    LOCATIONS_TTL,
    LOCATIONS_URL,
    MeteoalarmAlertsProvider,
    geometry_contains,
    parse_cap,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "meteoalarm"
AT_URL = COUNTRY_URL.format(country="AT")
DE_URL = COUNTRY_URL.format(country="DE")
CAP_URL = "https://storage.meteoalarm.org/api/warnings/{}.xml"
GEOMETRY_URL = "https://storage.meteoalarm.org/api/features/{}.geojson"

# The point every fixture is built around: Vienna, inside both AT and DE.
LAT, LON = 48.2, 16.4


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

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


class _Clock:
    """A fake monotonic clock and wall clock that move together."""

    def __init__(self, wall="2035-06-01T12:00:00+00:00"):
        self.monotonic = 1000.0
        self.wall = datetime.fromisoformat(wall)

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def _routes():
    return {
        LOCATIONS_URL: _Response(200, _json("locations.json")),
        AT_URL: _Response(200, _json("AT_page1.json")),
        DE_URL: _Response(200, {"type": "FeatureCollection", "metadata": {"total_pages": 1}, "features": []}),
        GEOMETRY_URL.format("bbox-hit"): _Response(200, _json("bbox-hit.geojson")),
        GEOMETRY_URL.format("bbox-miss"): _Response(200, _json("bbox-miss.geojson")),
        CAP_URL.format("alert-exact"): _Response(200, text=_text("alert-exact.xml")),
        CAP_URL.format("alert-bbox-hit"): _Response(200, text=_text("alert-bbox-hit.xml")),
        CAP_URL.format("alert-bbox-miss"): _Response(200, text=_text("alert-bbox-miss.xml")),
        CAP_URL.format("alert-superseded"): _Response(200, text=_text("alert-superseded.xml")),
        CAP_URL.format("alert-expired"): _Response(200, text=_text("alert-expired.xml")),
        CAP_URL.format("alert-nogeom"): _Response(200, text=_text("alert-nogeom.xml")),
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


def _provider(routes=None, token="test-token", language="en"):
    session = _Session(routes if routes is not None else _routes())
    host = _host(language)
    provider = MeteoalarmAlertsProvider(host=host, token=token, session=session)
    clock = _Clock()
    provider._clock = lambda: clock.monotonic
    provider._utcnow = lambda: clock.wall
    return provider, session, host, clock


def _warnings(host):
    return [call.args[0] for call in host.logger.warning.call_args_list]


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
    assert provider.covers(40.0, 16.4) is False
    assert session.count(LOCATIONS_URL) == 1


def test_the_locations_list_skips_the_all_feature():
    provider, _session_, _host_, _clock = _provider()
    countries = provider._country_geometries()
    assert [code for code, _geometry in countries] == ["AT", "DE", "FR"]


# --- matching ---------------------------------------------------------------


def test_a_bbox_hit_and_a_bbox_miss():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)
    assert "Wind" in summary.text
    assert "Rain" in summary.text
    assert "Heat" not in summary.text
    assert session.count(GEOMETRY_URL.format("bbox-miss")) == 1


def test_an_exact_outline_rejects_a_bbox_hit():
    provider, session, _host_, _clock = _provider()
    provider.summary(LAT, LON, 130)
    # The bbox contained the point, so the outline was fetched and rejected it.
    assert session.count(GEOMETRY_URL.format("bbox-miss")) == 1
    assert "Heat" not in provider.summary(LAT, LON, 130).text


def test_a_superseded_alert_is_skipped():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)
    assert "Storm" not in summary.text
    assert session.count(CAP_URL.format("alert-superseded")) == 0


def test_an_expired_alert_is_skipped():
    provider, session, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)
    assert "Fog" not in summary.text
    # It was fetched, then dropped on its expiry.
    assert session.count(CAP_URL.format("alert-expired")) == 1


def test_a_feature_without_geometry_is_skipped_with_a_debug_line():
    provider, session, host, _clock = _provider()
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_URL.format("alert-nogeom")) == 0
    debug = [call.args[0] for call in host.logger.debug.call_args_list]
    assert any("no geometry" in line for line in debug)


def test_no_alerts_returns_none():
    provider, _session_, _host_, _clock = _provider()
    assert provider.summary(40.0, 16.4, 130) is None


# --- language ---------------------------------------------------------------


@pytest.mark.parametrize("language,event", [("de", "Sturm"), ("en", "Wind"), ("fr", "Wind")])
def test_the_cap_info_block_follows_the_configured_language(language, event):
    provider, _session_, _host_, _clock = _provider(language=language)
    summary = provider.summary(LAT, LON, 130)
    assert event in summary.text


def test_parse_cap_falls_back_to_the_first_info_block():
    document = """<?xml version="1.0"?>
    <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
      <info><language>it-IT</language><event>Vento</event>
        <expires>2035-06-01T18:00:00+02:00</expires>
        <parameter><valueName>awareness_level</valueName><value>3; orange; Severe</value></parameter>
      </info>
    </alert>"""
    details = parse_cap(document, "en")
    assert details["event"] == "Vento"
    assert details["level"] == 3


# --- summary ----------------------------------------------------------------


def test_the_summary_orders_by_level_and_names_the_expiry():
    provider, _session_, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 130)
    assert summary.count == 2
    assert summary.text == "\U0001f7e0 Wind until 16:00; \U0001f7e1 Rain until 02 Jun 04:00"


def test_the_summary_truncates_with_a_plus_count():
    provider, _session_, _host_, _clock = _provider()
    summary = provider.summary(LAT, LON, 30)
    assert summary.count == 2
    assert summary.text == "\U0001f7e0 Wind until 16:00 +1"


# --- the full list ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_full_list_sends_one_message_per_alert_and_paces_them():
    provider, _session_, host, _clock = _provider()
    message = SimpleNamespace(content="wx 48.2,16.4 alerts", sender_id="u1")
    assert await provider.send_full_list(message, LAT, LON) is True

    assert host.send_response.await_count == 2
    first, second = host.send_response.await_args_list
    assert first.args[0] is message
    assert first.args[1].startswith("1/2 \U0001f7e0 Wind")
    assert "Strong wind in parts of Austria" in first.args[1]
    assert "from 10:00" in first.args[1]
    assert "until 16:00" in first.args[1]
    assert second.args[1].startswith("2/2 \U0001f7e1 Rain")
    assert second.kwargs == {"skip_user_rate_limit": True}
    host._pace_reply.assert_awaited_once_with(message, 2.5)


@pytest.mark.asyncio
async def test_the_full_list_says_so_when_there_are_no_alerts():
    provider, _session_, host, _clock = _provider()
    message = SimpleNamespace(content="wx 40,16 alerts", sender_id="u1")
    assert await provider.send_full_list(message, 40.0, 16.4) is True
    host.send_response.assert_awaited_once_with(message, "No weather alerts")


@pytest.mark.asyncio
async def test_the_full_list_cuts_each_message_to_the_frame():
    provider, _session_, host, _clock = _provider()
    host.get_max_message_length = Mock(return_value=20)
    message = SimpleNamespace(content="wx 48.2,16.4 alerts", sender_id="u1")
    assert await provider.send_full_list(message, LAT, LON) is True
    for call in host.send_response.await_args_list:
        assert len(call.args[1].encode("utf-8")) <= 20


# --- failures ---------------------------------------------------------------


def test_a_rejected_token_logs_one_warning_without_the_token():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(401)
    provider, _session_, host, _clock = _provider(routes, token="s3cr3t-token")
    assert provider.covers(LAT, LON) is False
    assert provider.covers(LAT, LON) is False
    warnings = _warnings(host)
    assert len(warnings) == 1
    assert "401" in warnings[0]
    assert "s3cr3t-token" not in warnings[0]


def test_a_failure_streak_logs_once_and_resets_on_success():
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(500)
    provider, _session_, host, _clock = _provider(routes)

    assert provider.covers(LAT, LON) is False
    assert provider.covers(LAT, LON) is False
    assert len(_warnings(host)) == 1

    routes[LOCATIONS_URL] = _Response(200, _json("locations.json"))
    assert provider.covers(LAT, LON) is True

    routes[LOCATIONS_URL] = _Response(500)
    _clock.advance(LOCATIONS_TTL + 1)
    assert provider.covers(LAT, LON) is False
    assert len(_warnings(host)) == 2


def test_a_failed_country_fetch_answers_with_no_alerts():
    routes = _routes()
    routes[AT_URL] = _Response(500)
    provider, _session_, host, _clock = _provider(routes)
    assert provider.summary(LAT, LON, 130) is None
    assert any("AT" in warning for warning in _warnings(host))


# --- caches -----------------------------------------------------------------


def test_the_locations_list_is_cached_for_a_day():
    provider, session, _host_, clock = _provider()
    provider.covers(LAT, LON)
    provider.covers(LAT, LON)
    assert session.count(LOCATIONS_URL) == 1

    clock.advance(LOCATIONS_TTL - 1)
    provider.covers(LAT, LON)
    assert session.count(LOCATIONS_URL) == 1

    clock.advance(2)
    provider.covers(LAT, LON)
    assert session.count(LOCATIONS_URL) == 2


def test_a_country_is_cached_for_ten_minutes():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    provider.summary(LAT, LON, 130)
    assert session.count(AT_URL) == 1

    clock.advance(COUNTRY_TTL - 1)
    provider.summary(LAT, LON, 130)
    assert session.count(AT_URL) == 1

    clock.advance(2)
    provider.summary(LAT, LON, 130)
    assert session.count(AT_URL) == 2


def test_a_cap_document_is_cached_until_it_expires():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_URL.format("alert-bbox-hit")) == 1

    clock.advance(3600)
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_URL.format("alert-bbox-hit")) == 1

    clock.advance(20 * 3600)
    provider.summary(LAT, LON, 130)
    assert session.count(CAP_URL.format("alert-bbox-hit")) == 2


def test_an_exact_outline_is_cached_by_url():
    provider, session, _host_, clock = _provider()
    provider.summary(LAT, LON, 130)
    provider.summary(LAT, LON, 130)
    assert session.count(GEOMETRY_URL.format("bbox-hit")) == 1

    clock.advance(COUNTRY_TTL + 1)
    provider.summary(LAT, LON, 130)
    assert session.count(GEOMETRY_URL.format("bbox-hit")) == 1


# --- the token --------------------------------------------------------------


def test_the_token_is_a_bearer_header_and_never_reaches_a_log():
    provider, session, host, _clock = _provider(token="s3cr3t-token")
    provider.summary(LAT, LON, 130)

    assert session.calls
    for call in session.calls:
        assert call["headers"].get("Authorization") == "Bearer s3cr3t-token"
        assert "s3cr3t-token" not in call["url"]
        assert "s3cr3t-token" not in json.dumps(call["params"])

    for level in ("debug", "info", "warning", "error", "critical"):
        for call in getattr(host.logger, level).call_args_list:
            assert "s3cr3t-token" not in str(call)


def test_a_failing_fetch_never_raises_into_the_command():
    routes = {url: _Response(500) for url in _routes()}
    provider, _session_, _host_, _clock = _provider(routes, token="s3cr3t-token")
    assert provider.covers(LAT, LON) is False
    assert provider.summary(LAT, LON, 130) is None
    assert asyncio.run(provider.send_full_list(SimpleNamespace(content="x", sender_id="u"), LAT, LON)) is True
