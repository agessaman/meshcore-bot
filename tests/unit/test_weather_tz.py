"""Real time zones for MET Norway's local times, and the estimate without tzfpy.

tzfpy is not installed in the test environment and must not be: every real-zone
case stubs ``sys.modules['tzfpy']`` with a coordinate table, and the fallback
cases make the import fail. The provider's own tests keep passing either way,
because the estimate reproduces the old ``round(lon / 15)`` behavior exactly.
"""

import configparser
import logging
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.i18n import Translator
from modules.weather_common import WeatherCommandMixin
from modules.weather_providers import _tz
from modules.weather_providers.base import WeatherUnits
from modules.weather_providers.metno import MetNorwayProvider, local_offset_hours

UTC = timezone.utc
MADRID = (40.42, -3.70)
TOKYO = (35.68, 139.70)
SEATTLE = (47.61, -122.33)


class _FakeTzfpy:
    """A stand-in for tzfpy: a coordinate table and a call log."""

    def __init__(self, zones):
        self.zones = zones
        self.calls = []

    def get_tz(self, lon, lat):
        self.calls.append((lon, lat))
        return self.zones.get((round(lat, 2), round(lon, 2)), "")


class _Host(WeatherCommandMixin):
    """The command side a provider reads, with the real formatting helpers."""

    def __init__(self):
        config = configparser.ConfigParser()
        config.read_dict({"Weather": {}, "Bot": {}})
        self.bot = SimpleNamespace(config=config)
        self.logger = Mock()
        self.translator = Translator("en")
        self.response_translator = self.translator

    def translate(self, key, **kwargs):
        return self.translator.translate(key, **kwargs)

    def get_max_message_length(self, message):
        return 150


def _provider():
    return MetNorwayProvider(_Host(), WeatherUnits("fahrenheit", "mph", "inch"))


def _payload(times):
    """A minimal Locationforecast document with one step per time."""
    return {
        "properties": {
            "timeseries": [
                {
                    "time": moment,
                    "data": {
                        "instant": {"details": {"air_temperature": 10.0}},
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


@pytest.fixture
def zones(monkeypatch):
    """Stub tzfpy with three zones, and forget any earlier import attempt."""
    fake = _FakeTzfpy({
        MADRID: "Europe/Madrid",
        TOKYO: "Asia/Tokyo",
        SEATTLE: "America/Los_Angeles",
    })
    monkeypatch.setitem(sys.modules, "tzfpy", fake)
    _tz._reset()
    yield fake
    _tz._reset()


@pytest.fixture
def no_tzfpy(monkeypatch):
    """Make ``import tzfpy`` fail, as it does on armv7."""
    monkeypatch.setitem(sys.modules, "tzfpy", None)
    _tz._reset()
    yield
    _tz._reset()


# --- the real zone ----------------------------------------------------------


def test_madrid_is_an_hour_ahead_in_winter_and_two_in_summer(zones):
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    assert _tz.utc_offset_at(*MADRID, winter) == timedelta(hours=1)
    assert _tz.utc_offset_at(*MADRID, summer) == timedelta(hours=2)


def test_tokyo_is_nine_hours_ahead_all_year(zones):
    for month in (1, 7):
        moment = datetime(2026, month, 15, 12, 0, tzinfo=UTC)
        assert _tz.utc_offset_at(*TOKYO, moment) == timedelta(hours=9)


def test_seattle_follows_daylight_saving(zones):
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    assert _tz.utc_offset_at(*SEATTLE, winter) == timedelta(hours=-8)
    assert _tz.utc_offset_at(*SEATTLE, summer) == timedelta(hours=-7)


def test_the_offset_changes_across_the_spring_transition(zones):
    # Madrid moves from CET to CEST at 01:00Z on the last Sunday of March.
    before = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    after = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)

    assert _tz.utc_offset_at(*MADRID, before) == timedelta(hours=1)
    assert _tz.utc_offset_at(*MADRID, after) == timedelta(hours=2)


def test_a_naive_instant_is_read_as_utc(zones):
    naive = datetime(2026, 1, 15, 12, 0)

    assert _tz.utc_offset_at(*MADRID, naive) == timedelta(hours=1)


# --- the estimate -----------------------------------------------------------


def test_the_estimate_is_the_old_longitude_rule():
    assert local_offset_hours(139.7) == 9
    assert local_offset_hours(-122.3) == -8
    assert local_offset_hours(6.0) == 0
    # Madrid's longitude says UTC, its zone says UTC+1: the known limitation.
    assert local_offset_hours(-3.7) == 0


def test_without_tzfpy_the_estimate_is_used_and_logged_once(no_tzfpy, caplog):
    with caplog.at_level(logging.INFO, logger="modules.weather_providers._tz"):
        first = _tz.utc_offset_at(*TOKYO, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
        second = _tz.utc_offset_at(*SEATTLE, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

    assert first == timedelta(hours=9)
    assert second == timedelta(hours=-8)
    records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(records) == 1
    assert "tzfpy is not installed" in records[0].getMessage()


def test_an_unknown_point_falls_back_at_debug(zones, caplog):
    with caplog.at_level(logging.DEBUG, logger="modules.weather_providers._tz"):
        offset = _tz.utc_offset_at(0.0, 30.0, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

    assert offset == timedelta(hours=2)  # round(30 / 15)
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert [r for r in caplog.records if r.levelno == logging.DEBUG]


def test_an_unknown_zone_name_falls_back(zones):
    zones.zones[MADRID] = "Not/AZone"

    offset = _tz.utc_offset_at(*MADRID, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

    assert offset == timedelta(0)


def test_a_raising_lookup_falls_back(zones):
    zones.get_tz = Mock(side_effect=RuntimeError("bad coordinate"))

    offset = _tz.utc_offset_at(*TOKYO, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

    assert offset == timedelta(hours=9)


# --- caching ----------------------------------------------------------------


def test_the_zone_name_is_looked_up_once_per_point(zones):
    moment = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    _tz.utc_offset_at(*TOKYO, moment)
    _tz.utc_offset_at(*TOKYO, moment + timedelta(hours=6))
    _tz.utc_offset_at(*SEATTLE, moment)

    assert len(zones.calls) == 2


def test_the_lookup_gets_longitude_first(zones):
    _tz.utc_offset_at(*TOKYO, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

    assert zones.calls == [(139.7, 35.68)]


# --- the provider -----------------------------------------------------------


def test_the_daily_split_uses_the_real_zone(zones):
    provider = _provider()
    payload = _payload(["2026-01-15T23:30:00Z", "2026-01-16T00:30:00Z"])

    days = provider._daily(payload, *MADRID)

    # Both steps are after local midnight in Madrid, so they share one day.
    assert [day["date"].isoformat() for day in days] == ["2026-01-16"]


def test_the_daily_split_falls_back_to_the_estimate(no_tzfpy):
    provider = _provider()
    payload = _payload(["2026-01-15T23:30:00Z", "2026-01-16T00:30:00Z"])

    days = provider._daily(payload, *MADRID)

    # The longitude estimate puts Madrid at UTC, so the steps straddle midnight.
    assert [day["date"].isoformat() for day in days] == ["2026-01-15", "2026-01-16"]


def test_the_hourly_labels_use_the_real_zone(zones):
    provider = _provider()
    provider._now = lambda: datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    payload = _payload(["2026-01-15T12:00:00Z", "2026-01-15T13:00:00Z"])

    text = provider._hourly(payload, *MADRID, 150)

    # 13:00Z is 2PM in Madrid, not 1PM as the estimate would say.
    assert text.startswith("2PM:")
