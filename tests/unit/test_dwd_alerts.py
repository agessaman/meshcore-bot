"""German weather service alerts: the ``dwd`` alerts provider, from Bright Sky.

No test here touches the network. The session answers from
``tests/fixtures/brightsky/``, trimmed from live ``/alerts`` responses (Berlin,
Munich, and the 404 body a point outside Germany gets), and both clocks are
fixed so the cache and the expiry filter do not depend on when the suite runs.

The variants the live data did not happen to contain at fetch time (a test
warning, an expired one, an open-ended one, a severe and an extreme one, an
onset two days out) are edits of the real Munich alerts, built in the test that
needs them rather than stored as fixtures.
"""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.wx_command import WxCommand
from modules.i18n import Translator
from modules.weather_providers.alerts_dwd import (
    ALERT_CACHE_SECONDS,
    BRIGHTSKY_BASE_URL,
    DwdAlertsProvider,
)
from modules.weather_providers.base import Coverage
from tests.unit.test_eccc_provider import _Host

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "brightsky"
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
BERLIN = (52.52, 13.40)
MUNICH = (48.14, 11.58)
PARIS = (48.86, 2.35)


def _payload(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


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


class _Clock:
    def __init__(self, monotonic=1000.0):
        self.monotonic = monotonic

    def advance(self, seconds):
        self.monotonic += seconds


def _provider(name="munich", response=None, host=None, clock=None, now=None):
    clock = clock or _Clock()
    session = _Session(response if response is not None else _Response(200, _payload(name)))
    host = host or _Host()
    host.send_response = AsyncMock(return_value=True)
    host._pace_reply = AsyncMock()
    host.get_max_message_length = Mock(return_value=150)
    provider = DwdAlertsProvider(
        host, session=session, clock=lambda: clock.monotonic, now=now or (lambda: NOW)
    )
    return provider, session, host, clock


def _width(host, text):
    return host._count_display_width(text)


def _variant(**changes):
    """The first real Munich alert with fields changed, for the edge cases."""
    alert = copy.deepcopy(_payload("munich")["alerts"][0])
    alert.update(changes)
    return {"alerts": [alert], "location": _payload("munich")["location"]}


# --- the lookup -------------------------------------------------------------


def test_berlin_has_one_alert_and_munich_two():
    berlin, _session_, _host_, _clock = _provider("berlin")
    munich, _session_, _host_, _clock = _provider("munich")

    assert len(berlin._alerts_for(*BERLIN)) == 1
    assert len(munich._alerts_for(*MUNICH)) == 2


def test_the_request_carries_the_point_and_a_user_agent():
    provider, session, _host_, _clock = _provider("berlin")

    provider._alerts_for(*BERLIN)

    call = session.calls[0]
    assert call["url"] == f"{BRIGHTSKY_BASE_URL}/alerts"
    assert call["params"] == {"lat": "52.5200", "lon": "13.4000"}
    assert "meshcore-bot" in call["headers"]["User-Agent"]


def test_a_point_outside_germany_is_not_covered():
    provider, _session_, _host_, _clock = _provider("paris")

    assert provider.covers(*PARIS) is False
    assert provider.coverage_for(*PARIS) is Coverage.NOT_COVERED
    assert provider.covers(*BERLIN) is True
    assert provider.coverage_for(*BERLIN) is Coverage.COVERED


def test_a_404_is_no_alerts_rather_than_a_failure():
    provider, _session_, host, _clock = _provider(
        response=_Response(404, _payload("paris"))
    )

    assert provider._alerts_for(*PARIS) == []
    host.logger.warning.assert_not_called()


def test_a_404_inside_germany_is_a_failure_not_an_all_clear():
    # Bright Sky answers 404 for a point outside its warning cells, but inside
    # them a 404 is the service failing, and an all-clear would be a lie.
    provider, _session_, host, _clock = _provider(
        response=_Response(404, _payload("paris"))
    )

    assert provider._alerts_for(*MUNICH) is None
    assert provider.summary(*MUNICH, 150) is None
    assert asyncio.run(provider.send_full_list(None, *MUNICH)) is True
    host.send_response.assert_awaited_once_with(
        None, host.translate("commands.wx.dwd.error_fetching")
    )


# --- filtering --------------------------------------------------------------


def test_a_test_warning_is_dropped():
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, _variant(status="test"))
    )

    assert provider._alerts_for(*MUNICH) == []


def test_an_expired_warning_is_dropped():
    expired = (NOW - timedelta(hours=1)).isoformat()
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, _variant(expires=expired))
    )

    assert provider._alerts_for(*MUNICH) == []


def test_an_open_ended_warning_is_kept():
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, _variant(expires=None))
    )

    alerts = provider._alerts_for(*MUNICH)

    assert len(alerts) == 1
    assert alerts[0]["expires"] is None


def test_a_warning_two_days_out_is_dropped():
    later = (NOW + timedelta(days=2)).isoformat()
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, _variant(onset=later, effective=later))
    )

    assert provider._alerts_for(*MUNICH) == []


def test_a_warning_later_today_is_kept():
    # The real Munich alerts start at 11:00Z and 15:00Z; the second is future.
    provider, _session_, _host_, _clock = _provider("munich")

    alerts = provider._alerts_for(*MUNICH)

    # 11:00Z and 15:00Z, shown in Munich's own offset.
    assert [alert["onset"].hour for alert in alerts] == [12, 16]


# --- severity and language --------------------------------------------------


def test_the_most_severe_warning_comes_first():
    severe = _variant(severity="severe", event_en="storm", event_de="STURM")
    minor = copy.deepcopy(_payload("munich")["alerts"][0])
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, {"alerts": [minor, severe["alerts"][0]],
                                 "location": severe["location"]})
    )

    alerts = provider._alerts_for(*MUNICH)

    assert [alert["level"] for alert in alerts] == [4, 2]
    assert alerts[0]["event"] == "Storm"


def test_the_event_follows_the_bot_language():
    english, _session_, _host_, _clock = _provider("munich")
    german_host = _Host(localization={"language": "de"})
    german, _session_, _host_, _clock = _provider("munich", host=german_host)

    assert english._alerts_for(*MUNICH)[0]["event"] == "Frost"
    # German events arrive in capitals and are title-cased for display.
    assert german._alerts_for(*MUNICH)[0]["event"] == "Frost"


@pytest.mark.parametrize("raw,expected", [
    ("FROST", "Frost"),
    ("GLÄTTE", "Glätte"),
    ("SCHWERES GEWITTER", "Schweres Gewitter"),
    ("GEWITTER MIT ORKANBÖEN", "Gewitter mit Orkanböen"),
    ("STURMBÖEN-UND-GEWITTER", "Sturmböen-und-Gewitter"),
    ("NORDSEE-KÜSTE", "Nordsee-Küste"),
])
def test_german_events_are_title_cased(raw, expected):
    german_host = _Host(localization={"language": "de"})
    provider, _session_, _host_, _clock = _provider(
        response=_Response(200, _variant(event_de=raw)), host=german_host
    )

    assert provider._alerts_for(*MUNICH)[0]["event"] == expected


def test_the_area_is_the_warning_cell_name():
    provider, _session_, _host_, _clock = _provider("munich")

    assert provider._alerts_for(*MUNICH)[0]["area"] == "München-Altstadt-Lehel"


# --- the summary and the full list ------------------------------------------


def test_the_summary_lists_every_alert():
    provider, _session_, host, _clock = _provider("munich")

    summary = provider.summary(*MUNICH, 150)

    assert summary.count == 2
    assert summary.text.startswith("🟡 Frost ")
    assert "; 🟡 Icy surfaces " in summary.text
    assert _width(host, summary.text) <= 150


def test_the_summary_trims_to_the_budget():
    provider, _session_, host, _clock = _provider("munich")

    summary = provider.summary(*MUNICH, 30)

    assert _width(host, summary.text) <= 30
    assert summary.text.endswith("+1")


def test_the_full_list_is_one_line_per_alert():
    provider, _session_, host, _clock = _provider("munich")

    assert asyncio.run(provider.send_full_list(None, *MUNICH)) is True

    texts = [call.args[1] for call in host.send_response.call_args_list]
    assert texts[0].startswith("2 alerts:\n1. 🟡 Frost from ")
    joined = "\n".join(texts)
    assert "\n2. 🟡 Icy surfaces from " in joined
    assert "München-Altstadt-Lehel" in joined


def test_the_full_list_paces_between_messages():
    provider, _session_, host, _clock = _provider("munich")
    host.get_max_message_length = Mock(return_value=60)

    assert asyncio.run(provider.send_full_list(None, *MUNICH)) is True

    assert host.send_response.await_count == 2
    assert host._pace_reply.await_count == 1


# --- failures ---------------------------------------------------------------


def test_a_failed_fetch_is_a_fetch_error_not_an_all_clear():
    provider, _session_, host, _clock = _provider(response=_Response(500))

    assert provider._alerts_for(*MUNICH) is None
    assert provider.summary(*MUNICH, 150) is None
    assert asyncio.run(provider.send_full_list(None, *MUNICH)) is True
    host.send_response.assert_awaited_once_with(
        None, host.translate("commands.wx.dwd.error_fetching")
    )


def test_no_alerts_says_so():
    provider, _session_, host, _clock = _provider(
        response=_Response(200, {"alerts": [], "location": _payload("munich")["location"]})
    )

    assert provider._alerts_for(*MUNICH) == []
    assert provider.summary(*MUNICH, 150) is None
    assert asyncio.run(provider.send_full_list(None, *MUNICH)) is True
    host.send_response.assert_awaited_once_with(None, "No weather alerts")


def test_a_malformed_body_does_not_raise():
    provider, _session_, host, _clock = _provider(response=_Response(200, ["not", "a", "dict"]))

    assert provider._alerts_for(*MUNICH) is None
    assert provider.summary(*MUNICH, 150) is None


def test_a_failure_streak_logs_once():
    provider, _session_, host, _clock = _provider(response=_Response(503))

    provider._alerts_for(*MUNICH)
    provider._alerts_for(*MUNICH)

    assert host.logger.warning.call_count == 1


# --- the cache --------------------------------------------------------------


def test_the_cache_answers_within_its_ttl():
    provider, session, _host_, clock = _provider("munich")

    provider._alerts_for(*MUNICH)
    clock.advance(ALERT_CACHE_SECONDS - 1)
    provider._alerts_for(*MUNICH)
    assert session.count() == 1

    clock.advance(2)
    provider._alerts_for(*MUNICH)
    assert session.count() == 2


# --- the command ------------------------------------------------------------


def _command(weather, localization=None):
    import configparser

    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": weather,
        "Wx_Command": {},
        "Bot": {},
        "Localization": localization or {},
    })
    bot = Mock()
    bot.config = config
    bot.translator = Translator("en")
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    return WxCommand(bot)


def test_wx_alerts_uses_the_dwd_provider_end_to_end():
    cmd = _command({"weather_provider": "metno", "alerts_provider": "dwd"})
    assert isinstance(cmd.alerts_provider, DwdAlertsProvider)

    session = _Session(_Response(200, _payload("berlin")))
    cmd.alerts_provider._session = session
    cmd.alerts_provider._now = lambda: NOW
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=150)
    cmd.geocode_location = Mock(return_value=(52.52, 13.40, {"city": "Berlin"}, None))
    cmd._format_location_display = Mock(return_value="Berlin, DE")

    message = SimpleNamespace(content="wx 52.52,13.40 alerts", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True

    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    assert texts[0].startswith("1 alerts:\n1. 🟡 Frost from ")
    assert session.calls[0]["params"] == {"lat": "52.5200", "lon": "13.4000"}


def test_wx_alerts_outside_germany_replies_not_available():
    cmd = _command({"weather_provider": "metno", "alerts_provider": "dwd"})
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=150)
    cmd.geocode_location = Mock(return_value=(48.86, 2.35, {"city": "Paris"}, None))
    cmd._format_location_display = Mock(return_value="Paris, FR")

    message = SimpleNamespace(content="wx 48.86,2.35 alerts", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True

    cmd.send_response.assert_awaited_once_with(
        message, cmd.translate("commands.gwx.source_option_not_available")
    )


def test_the_help_text_credits_dwd():
    cmd = _command({"weather_provider": "metno", "alerts_provider": "dwd"})

    assert cmd.get_help_text().endswith("Alerts: DWD via Bright Sky")


def test_the_help_text_has_no_credit_without_dwd():
    cmd = _command({"weather_provider": "metno", "alerts_provider": "none"})

    assert "DWD" not in cmd.get_help_text()
