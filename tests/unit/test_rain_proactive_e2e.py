#!/usr/bin/env python3
"""End-to-end tests for the proactive rain/snow push (Weather_Service).

Drives WeatherService._check_rain_nowcast() with synthetic Open-Meteo series and
asserts on the heads-up that gets pushed — in particular the probability gate
(low-confidence "incoming" alerts are suppressed and left un-announced so they
can still fire later), snow depth, the rain-ending notice, and once-per-episode
dedup.

Sends are captured at bot.command_manager.send_channel_message; with a single
rain_channel configured the proactive push results in exactly one send, which
the assertions check.
"""

import asyncio
import configparser
from unittest.mock import Mock

from modules.service_plugins.weather_service import WeatherService
from tests.unit._rain_harness import make_series

# 15-min bucket presets (snowfall in cm, prob in %, temp in C).
_INCOMING_HI = dict(precip=[0, 0, 0.5, 0.5, 0.5], codes=[0, 0, 61, 61, 61], prob=[10, 10, 80, 80, 80])
_INCOMING_LO = dict(precip=[0, 0, 0.5, 0.5, 0.5], codes=[0, 0, 61, 61, 61], prob=[10, 10, 30, 30, 30])
_SNOW_INCOMING = dict(
    precip=[0, 0, 0.3, 0.3, 0.3], codes=[0, 0, 71, 71, 71],
    snow=[0, 0, 2.0, 2.0, 2.0], prob=[10, 10, 80, 80, 80],
)
_ENDING = dict(precip=[0.5, 0.5, 0, 0, 0], codes=[61, 61, 0, 0, 0], prob=[80] * 9,
               current_precip=0.5, current_code=61)


def build_service(series, monkeypatch, *, overrides=None):
    """A WeatherService whose Open-Meteo fetch returns `series`, with the send
    captured. Returns (service, sends) where sends is a list of (channel, text)."""
    cfg = configparser.ConfigParser()
    cfg.add_section("Weather")
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "36.16")
    cfg.set("Weather_Service", "my_position_lon", "-86.78")
    cfg.set("Weather_Service", "rain_nowcast_enabled", "true")
    cfg.set("Weather_Service", "rain_channel", "weather")  # single -> one send in both versions
    for key, val in (overrides or {}).items():
        cfg.set("Weather_Service", key, val)

    bot = Mock()
    bot.logger = Mock()
    bot.config = cfg
    bot.db_manager = Mock()

    sends: list[tuple[str, str]] = []

    async def _send_channel_message(channel, text, **kwargs):
        sends.append((channel, text))
        return True

    bot.command_manager.send_channel_message = _send_channel_message

    service = WeatherService(bot)
    service.api_session = Mock()
    # Pin the location label so _format_rain_nowcast doesn't reverse-geocode.
    service._cached_rain_location = "Nashville, TN"
    # get_mesh_flood_scope lazily imports heavy deps; stub it.
    service.get_mesh_flood_scope = Mock(return_value=None)
    # NWS gridpoint is tried first now; return None ("no coverage") so these
    # source-agnostic nowcast-logic tests run on the canned Open-Meteo series.
    monkeypatch.setattr(
        "modules.service_plugins.weather_service.fetch_precip_series_nws",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "modules.service_plugins.weather_service.fetch_precip_series",
        lambda *a, **k: series,
    )
    return service, sends


# --- NWS path (WeatherService passes cache_ttl) ------------------------------

def test_nws_path_accepts_cache_ttl_without_crashing(monkeypatch):
    """Proactive poll calls fetch_precip_series_nws(..., cache_ttl=...); must not TypeError."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = now.replace(minute=0, second=0, microsecond=0)
    nws_series = {
        "times": [(base + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(3)],
        "precip": [0.0, 0.5, 0.5],
        "codes": [None, 61, 61],
        "now": now.isoformat(timespec="minutes"),
        "current_precip": 0.0,
        "current_code": None,
        "step": 60,
    }
    nws_kwargs: list[dict] = []
    open_meteo_called: list[bool] = []

    def fake_nws(*_a, **kwargs):
        nws_kwargs.append(kwargs)
        return nws_series

    def fake_open_meteo(*_a, **_k):
        open_meteo_called.append(True)
        return None

    cfg = configparser.ConfigParser()
    cfg.add_section("Weather")
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "36.16")
    cfg.set("Weather_Service", "my_position_lon", "-86.78")
    cfg.set("Weather_Service", "rain_nowcast_enabled", "true")
    cfg.set("Weather_Service", "rain_channel", "weather")
    cfg.set("Weather_Service", "rain_nowcast_cache_seconds", "300")

    bot = Mock()
    bot.logger = Mock()
    bot.config = cfg
    bot.db_manager = Mock()

    sends: list[tuple[str, str]] = []

    async def _send(channel, text, **kwargs):
        sends.append((channel, text))
        return True

    bot.command_manager.send_channel_message = _send

    monkeypatch.setattr(
        "modules.service_plugins.weather_service.fetch_precip_series_nws", fake_nws
    )
    monkeypatch.setattr(
        "modules.service_plugins.weather_service.fetch_precip_series", fake_open_meteo
    )

    service = WeatherService(bot)
    service.api_session = Mock()
    service._cached_rain_location = "Nashville, TN"
    service.get_mesh_flood_scope = Mock(return_value=None)

    asyncio.run(service._check_rain_nowcast())

    assert nws_kwargs, "NWS fetch should have been tried"
    assert nws_kwargs[0].get("cache_ttl") == 300
    assert open_meteo_called == [], "Open-Meteo fallback should not run when NWS succeeds"
    assert len(sends) == 1
    assert "Heads up" in sends[0][1]
    bot.logger.error.assert_not_called()


# --- probability gate -------------------------------------------------------

def test_incoming_above_threshold_pushes(monkeypatch):
    service, sends = build_service(make_series(**_INCOMING_HI), monkeypatch)
    asyncio.run(service._check_rain_nowcast())

    assert len(sends) == 1
    channel, text = sends[0]
    assert channel == "weather"
    assert text.startswith("🌧️ Heads up — Rain starting in ~30min")
    assert "est" in text and "80%" in text
    assert text.endswith("near Nashville, TN")
    assert service._rain_start_announced is True
    assert service._last_rain_start_time is not None
    assert len(text.encode("utf-8")) <= 145


def test_incoming_below_threshold_is_gated_and_left_unannounced(monkeypatch):
    service, sends = build_service(make_series(**_INCOMING_LO), monkeypatch)
    asyncio.run(service._check_rain_nowcast())

    assert sends == []  # 30% < default 50% -> suppressed
    # Critically: flag stays False so a later, higher-confidence poll can fire.
    assert service._rain_start_announced is False
    assert service._last_rain_start_time is None


def test_gate_respects_configured_min_probability(monkeypatch):
    # Same 30% series, but lower the bar to 20% -> it should push.
    service, sends = build_service(
        make_series(**_INCOMING_LO), monkeypatch,
        overrides={"rain_nowcast_min_probability": "20"},
    )
    asyncio.run(service._check_rain_nowcast())
    assert len(sends) == 1
    assert "30%" in sends[0][1]


# --- snow -------------------------------------------------------------------

def test_snow_incoming_pushes_depth(monkeypatch):
    service, sends = build_service(make_series(**_SNOW_INCOMING), monkeypatch)
    asyncio.run(service._check_rain_nowcast())

    assert len(sends) == 1
    text = sends[0][1]
    assert text.startswith("🌨️ Heads up — Snow starting in ~30min")
    assert "in snow" in text
    assert text.endswith("near Nashville, TN")


# --- ending -----------------------------------------------------------------

def test_rain_ending_pushes_when_enabled(monkeypatch):
    service, sends = build_service(make_series(**_ENDING), monkeypatch)
    asyncio.run(service._check_rain_nowcast())

    assert len(sends) == 1
    text = sends[0][1]
    assert text.startswith("🌧️ Heads up — Rain ending in ~")
    assert service._rain_end_announced is True


def test_rain_ending_suppressed_when_disabled(monkeypatch):
    service, sends = build_service(
        make_series(**_ENDING), monkeypatch,
        overrides={"rain_nowcast_announce_ending": "false"},
    )
    asyncio.run(service._check_rain_nowcast())
    assert sends == []


# --- dedup ------------------------------------------------------------------

def test_fires_once_per_episode(monkeypatch):
    service, sends = build_service(make_series(**_INCOMING_HI), monkeypatch)
    asyncio.run(service._check_rain_nowcast())
    asyncio.run(service._check_rain_nowcast())  # same episode, already announced
    assert len(sends) == 1


# --- gallery (visual): run with -s to eyeball the pushes --------------------

def test_gallery_prints_proactive_pushes(monkeypatch):
    rows = []
    for label, series, ov in [
        ("rain incoming (80%)", make_series(**_INCOMING_HI), None),
        ("rain incoming (30%)", make_series(**_INCOMING_LO), None),
        ("snow incoming (80%)", make_series(**_SNOW_INCOMING), None),
        ("rain ending", make_series(**_ENDING), None),
    ]:
        service, sends = build_service(series, monkeypatch, overrides=ov)
        asyncio.run(service._check_rain_nowcast())
        out = sends[0][1] if sends else "(gated — no push)"
        rows.append(f"{label:22s} -> {out}")

    print("\n--- proactive rain/snow push gallery ---")
    for r in rows:
        print(r)
    assert any("Heads up" in r for r in rows)
    assert any("no push" in r for r in rows)
