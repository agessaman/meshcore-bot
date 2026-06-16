#!/usr/bin/env python3
"""Live smoke test against the real Open-Meteo API.

Opt-in (network): SKIPPED unless RAIN_LIVE_SMOKE is set, so CI and the normal
offline suite never depend on it. The mocked suites can't catch upstream schema
drift (Open-Meteo renaming/dropping a field, or no longer serving 15-min
probability/temperature); this one can, and it exercises the real fetch end to
end through the command. It does NOT geocode (Nominatim) — coordinates are fixed
and only the label is stubbed.

    RAIN_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/unit/test_rain_live_smoke.py -o addopts="" -s -q
"""

import os

import pytest
import requests

from modules.commands.rain_command import fetch_precip_series
from tests.unit._rain_harness import build_cmd, render

pytestmark = pytest.mark.skipif(
    os.environ.get("RAIN_LIVE_SMOKE") in (None, "", "0"),
    reason="live network test; set RAIN_LIVE_SMOKE=1 to run",
)

# (label, lat, lon) — a geographic spread so at least one usually has weather.
PLACES = [
    ("Nashville, TN", 36.1627, -86.7816),
    ("London, UK", 51.5072, -0.1276),
    ("Seattle, WA", 47.6062, -122.3321),
]

# Keys the analysis + formatting rely on; prob/temp are the newer dependencies.
REQUIRED_KEYS = {"times", "precip", "snow", "prob", "temp", "codes", "now", "step"}


def test_live_fetch_schema_is_intact():
    """The real API still returns every field we consume, at aligned lengths,
    with 15-min probability + temperature populated (the features added last)."""
    session = requests.Session()
    try:
        for label, lat, lon in PLACES:
            series = fetch_precip_series(session, lat, lon, timeout=15)
            assert series is not None, f"{label}: no series returned"
            missing = REQUIRED_KEYS - series.keys()
            assert not missing, f"{label}: missing keys {missing}"

            n = len(series["times"])
            assert n > 0, f"{label}: empty time series"
            assert series["step"] in (15, 60)
            for k in ("precip", "codes"):
                assert len(series[k]) == n, f"{label}: {k} length {len(series[k])} != times {n}"

            prob_ok = sum(p is not None for p in series["prob"])
            temp_ok = sum(t is not None for t in series["temp"])
            assert prob_ok > 0, f"{label}: probability series all None — upstream drift?"
            assert temp_ok > 0, f"{label}: temperature series all None — upstream drift?"

            print(
                f"\n{label:14s} step={series['step']}min n={n} "
                f"prob_nonnull={prob_ok} temp_nonnull={temp_ok} now={series['now']}"
            )
    finally:
        session.close()


def test_live_command_renders_for_real_places():
    """Drive execute() with a real fetch for each place and print the real reply.
    Asserts only invariants (non-empty, fits budget, no stray 'None')."""
    print("\n--- live !rain / !snow ---")
    for label, lat, lon in PLACES:
        for word in ("rain", "snow"):
            # series=None -> real Open-Meteo fetch; location is fixed (no geocoding).
            cmd, cap = build_cmd(series=None, coords=(lat, lon), label=label)
            resp = render(cmd, cap, f"!{word}")
            assert resp, f"{label}: empty !{word} reply"
            assert "None" not in resp, f"{label}: stray None in !{word} reply: {resp!r}"
            print(f"!{word:4s} {label:14s} -> {resp}")
