"""Environment Canada weather alerts: the ``eccc`` alerts provider, from live fixtures.

The fixture is trimmed from a live ``weather-alerts`` response: one real alert
area (Apica Mountain, Quebec) plus variants of it for the cases the live data
did not happen to contain at fetch time (a red warning, a statement with no
colour, an ended alert, an expired alert) and one real area that does not
contain the test point.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from modules.weather_providers.alerts_eccc import EcccAlertsProvider
from modules.weather_providers.base import Coverage
from tests.unit.test_eccc_provider import _Host

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eccc"
NOW = datetime(2026, 11, 27, 21, 30, tzinfo=timezone.utc)
# Inside the Apica Mountain area, and outside the Baie-Saint-Paul one.
INSIDE = (48.194, -71.306)
OUTSIDE = (45.42, -75.70)


def _payload():
    return json.loads((FIXTURES / "alerts.json").read_text(encoding="utf-8"))


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _Session:
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


class _Clock:
    def __init__(self, monotonic=1000.0):
        self.monotonic = monotonic

    def advance(self, seconds):
        self.monotonic += seconds


def _provider(response=None, host=None, clock=None):
    clock = clock or _Clock()
    session = _Session(response if response is not None else _Response(200, _payload()))
    host = host or _Host()
    host.send_response = AsyncMock(return_value=True)
    host._pace_reply = AsyncMock()
    host.get_max_message_length = Mock(return_value=150)
    provider = EcccAlertsProvider(
        host, session=session, clock=lambda: clock.monotonic, now=lambda: NOW
    )
    return provider, session, host, clock


# --- matching ---------------------------------------------------------------


def test_only_the_polygons_that_contain_the_point_answer():
    provider, _session_, _host_, _clock = _provider()

    alerts = provider._alerts_for(*INSIDE)

    areas = {alert["area"] for alert in alerts}
    assert "Apica Mountain area" in areas
    assert "Baie-Saint-Paul area" not in areas


def test_a_point_outside_every_polygon_has_no_alerts():
    provider, _session_, _host_, _clock = _provider()

    assert provider._alerts_for(*OUTSIDE) == []


def test_an_ended_alert_is_dropped():
    provider, _session_, _host_, _clock = _provider()

    alerts = provider._alerts_for(*INSIDE)

    assert "Ended area" not in {alert["area"] for alert in alerts}


def test_an_expired_alert_is_dropped():
    provider, _session_, _host_, _clock = _provider()

    alerts = provider._alerts_for(*INSIDE)

    assert "Expired area" not in {alert["area"] for alert in alerts}


def test_the_risk_colour_ranks_the_alerts():
    provider, _session_, _host_, _clock = _provider()

    alerts = provider._alerts_for(*INSIDE)

    assert [alert["level"] for alert in alerts] == [4, 2, 1]
    assert [alert["event"] for alert in alerts] == [
        "Freezing rain", "Freezing rain", "Special weather",
    ]


def test_the_window_runs_from_validity_to_the_event_end():
    provider, _session_, _host_, _clock = _provider()

    alerts = provider._alerts_for(*INSIDE)
    yellow = [alert for alert in alerts if alert["level"] == 2][0]

    assert yellow["onset"] == datetime(2026, 11, 27, 20, 18, tzinfo=timezone.utc)
    assert yellow["expires"] == datetime(2026, 11, 28, 5, 0, tzinfo=timezone.utc)


def test_the_event_and_area_follow_the_bot_language():
    host = _Host(localization={"language": "fr"})
    provider, _session_, _host_, _clock = _provider(host=host)

    alerts = provider._alerts_for(*INSIDE)

    assert alerts[0]["event"] == "Pluie verglaçante"
    assert alerts[0]["area"] == "Secteur rouge"


def test_coverage_is_the_alert_service_extent():
    provider, _session_, _host_, _clock = _provider()

    assert provider.covers(*INSIDE) is True
    assert provider.covers(48.85, 2.35) is False
    assert provider.coverage_for(*INSIDE) is Coverage.COVERED
    assert provider.coverage_for(48.85, 2.35) is Coverage.NOT_COVERED


# --- the summary ------------------------------------------------------------


def test_the_summary_lists_the_alerts_most_severe_first():
    provider, _session_, host, _clock = _provider()

    summary = provider.summary(*INSIDE, 150)

    assert summary.count == 3
    assert summary.text.startswith("🔴 Freezing rain")
    assert "🟡 Freezing rain" in summary.text
    assert "ℹ️ Special weather" in summary.text
    assert host._count_display_width(summary.text) <= 150


def test_the_summary_counts_what_it_could_not_show():
    provider, _session_, host, _clock = _provider()

    summary = provider.summary(*INSIDE, 40)

    assert summary.count == 3
    assert summary.text.endswith("+2") or summary.text.endswith("+1")
    assert host._count_display_width(summary.text) <= 40


def test_the_summary_trims_a_first_line_that_is_too_long():
    provider, _session_, host, _clock = _provider()

    summary = provider.summary(*INSIDE, 20)

    assert host._count_display_width(summary.text) <= 20
    assert "\u2026" in summary.text


def test_no_alerts_means_no_summary():
    provider, _session_, _host_, _clock = _provider()

    assert provider.summary(*OUTSIDE, 150) is None


def test_a_failed_fetch_means_no_summary():
    provider, _session_, host, _clock = _provider(response=_Response(500))

    assert provider.summary(*INSIDE, 150) is None
    host.logger.warning.assert_called()


# --- the full list ----------------------------------------------------------


def test_the_full_list_is_one_line_per_alert():
    provider, _session_, host, _clock = _provider()

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    texts = [call.args[1] for call in host.send_response.call_args_list]
    assert texts[0].startswith("3 alerts:")
    lines = "\n".join(texts).split("\n")
    assert lines[1].startswith("1. 🔴 Freezing rain")
    assert "Red warning area" in lines[1]
    assert lines[2].startswith("2. 🟡 Freezing rain")
    assert lines[3].startswith("3. ℹ️ Special weather")


def test_the_full_list_packs_and_paces_like_nws():
    provider, _session_, host, _clock = _provider()
    host.get_max_message_length = Mock(return_value=60)

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    texts = [call.args[1] for call in host.send_response.call_args_list]
    assert len(texts) > 1
    assert all(host._count_display_width(text) <= 60 for text in texts)
    assert host._pace_reply.await_count == len(texts) - 1
    # Only the first message counts against the sender's rate limit.
    assert host.send_response.call_args_list[0].kwargs["skip_user_rate_limit"] is False
    assert host.send_response.call_args_list[1].kwargs["skip_user_rate_limit"] is True


def test_the_full_list_says_so_when_there_are_no_alerts():
    provider, _session_, host, _clock = _provider()

    assert asyncio.run(provider.send_full_list(Mock(), *OUTSIDE)) is True

    assert host.send_response.call_args.args[1] == "No weather alerts"


def test_a_failed_fetch_is_a_fetch_error_not_an_all_clear():
    provider, _session_, host, _clock = _provider(response=_Response(500))

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


def test_a_malformed_body_is_a_fetch_error():
    provider, _session_, host, _clock = _provider(response=_Response(200, ["not", "a", "body"]))

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


def test_a_body_without_a_feature_list_is_a_fetch_error():
    provider, _session_, host, _clock = _provider(response=_Response(200, {"features": "nope"}))

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


def test_junk_features_in_a_valid_body_are_skipped():
    provider, _session_, host, _clock = _provider(response=_Response(200, {"features": [None, 3]}))

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    assert host.send_response.call_args.args[1] == "No weather alerts"


def test_a_raising_session_is_a_fetch_error():
    def boom(call):
        raise RuntimeError("connection reset")

    provider, _session_, host, _clock = _provider(response=boom)

    assert asyncio.run(provider.send_full_list(Mock(), *INSIDE)) is True

    assert host.send_response.call_args.args[1] == "Error fetching weather alerts"


def test_the_failure_streak_logs_once():
    provider, _session_, host, _clock = _provider(response=_Response(500))

    provider._alerts_for(*INSIDE)
    provider._alerts_for(*INSIDE)

    assert host.logger.warning.call_count == 1


# --- the cache --------------------------------------------------------------


def test_the_alerts_are_cached_for_five_minutes():
    provider, session, _host_, clock = _provider()

    provider._alerts_for(*INSIDE)
    clock.advance(299)
    provider._alerts_for(*INSIDE)
    assert session.count() == 1

    clock.advance(2)
    provider._alerts_for(*INSIDE)
    assert session.count() == 2


def test_the_request_carries_the_point_box_and_language():
    provider, session, _host_, clock = _provider()

    provider._alerts_for(*INSIDE)

    call = session.calls[0]
    assert call["url"].endswith("/collections/weather-alerts/items")
    assert call["params"]["bbox"] == "-71.3560,48.1440,-71.2560,48.2440"
    assert call["params"]["lang"] == "en"
