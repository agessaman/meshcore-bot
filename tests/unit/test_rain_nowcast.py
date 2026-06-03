#!/usr/bin/env python3
"""Unit tests for the rain-nowcast pure logic (no network).

Exercises analyze_precip_nowcast / precip_bucket_for_code / _round5 with
synthetic 15-minutely (and hourly) precipitation series. "Now" is supplied
explicitly, matching how the command derives it from the API's current.time.
"""

from modules.commands.rain_command import (
    NowcastResult,
    _round5,  # noqa: PLC2701 (testing internal helper)
    analyze_precip_nowcast,
    city_display_name,
    decide_rain_notification,
    precip_bucket_for_code,
    precip_descriptor,
    titlecase_location,
)

NOW = "2026-06-03T14:00"

# 9 ascending 15-min buckets starting at NOW (covers a 120-min window).
TIMES_15 = [
    "2026-06-03T14:00",
    "2026-06-03T14:15",
    "2026-06-03T14:30",
    "2026-06-03T14:45",
    "2026-06-03T15:00",
    "2026-06-03T15:15",
    "2026-06-03T15:30",
    "2026-06-03T15:45",
    "2026-06-03T16:00",
]


def _codes(n, code=61):
    return [code] * n


# --- precip_bucket_for_code -------------------------------------------------

def test_bucket_mapping_basic():
    assert precip_bucket_for_code(61) == "rain"
    assert precip_bucket_for_code(65) == "heavy_rain"
    assert precip_bucket_for_code(82) == "heavy_rain"
    assert precip_bucket_for_code(71) == "snow"
    assert precip_bucket_for_code(86) == "snow"
    assert precip_bucket_for_code(56) == "freezing"
    assert precip_bucket_for_code(80) == "showers"
    assert precip_bucket_for_code(95) == "thunder"


def test_bucket_mapping_non_precip_and_invalid():
    assert precip_bucket_for_code(0) is None       # clear
    assert precip_bucket_for_code(3) is None        # overcast
    assert precip_bucket_for_code(None) is None
    assert precip_bucket_for_code("nope") is None


# --- _round5 ----------------------------------------------------------------

def test_round5():
    assert _round5(0) == 0
    assert _round5(2) == 5      # positive but rounds to 0 -> floor of 5
    assert _round5(7) == 5
    assert _round5(13) == 15
    assert _round5(30) == 30
    assert _round5(60) == 60


# --- dry now ----------------------------------------------------------------

def test_dry_clear():
    precip = [0.0] * 9
    r = analyze_precip_nowcast(TIMES_15, precip, _codes(9, 0), NOW, window_minutes=120)
    assert r.state == "dry_clear"
    assert r.minutes is None


def test_dry_incoming_with_duration():
    # Dry now; rain at 14:30 and 14:45, dry again at 15:00.
    precip = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 0, 61, 61, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert r.minutes == 30
    assert r.duration_minutes == 30   # 14:30 -> 15:00
    assert r.open_ended is False
    assert r.bucket == "rain"


def test_dry_incoming_open_ended():
    # Rain starts at 14:30 and never clears within the window.
    precip = [0.0, 0.0] + [0.5] * 7
    codes = [0, 0] + [63] * 7
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert r.minutes == 30
    assert r.open_ended is True
    assert r.duration_minutes is None


def test_dry_incoming_snow_bucket():
    precip = [0.0, 0.0, 0.0, 0.4, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 0, 0, 73, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert r.minutes == 45
    assert r.bucket == "snow"


# --- raining now ------------------------------------------------------------

def test_raining_stopping_from_bucket():
    # Raining at NOW (current bucket), clears at 14:30.
    precip = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [63, 63, 0, 0, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "raining_stopping"
    assert r.minutes == 30
    assert r.bucket == "rain"


def test_raining_continuing():
    precip = [0.5] * 9
    codes = _codes(9, 65)
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "raining_continuing"
    assert r.open_ended is True
    assert r.bucket == "heavy_rain"


def test_current_precip_override_makes_it_raining():
    # Series bucket at NOW reads 0, but live current.precipitation says it's raining.
    precip = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0] * 9
    r = analyze_precip_nowcast(
        TIMES_15, precip, codes, NOW, window_minutes=120,
        current_precip=0.6, current_code=63,
    )
    assert r.state == "raining_stopping"
    assert r.minutes == 15          # first dry bucket is 14:15
    assert r.bucket == "rain"


# --- window boundary --------------------------------------------------------

def test_rain_at_window_edge_is_included():
    # Rain only at 16:00 == exactly 120 min out.
    precip = [0.0] * 8 + [0.5]
    codes = [0] * 8 + [61]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert r.minutes == 120


def test_rain_past_window_is_ignored():
    times = TIMES_15 + ["2026-06-03T16:15", "2026-06-03T16:30"]
    precip = [0.0] * 9 + [0.8, 0.8]   # rain only at 16:15 (135 min) and beyond
    codes = [0] * 9 + [61, 61]
    r = analyze_precip_nowcast(times, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_clear"


# --- step-agnostic (hourly fallback shape) ----------------------------------

def test_hourly_series_detects_incoming():
    times = ["2026-06-03T14:00", "2026-06-03T15:00", "2026-06-03T16:00", "2026-06-03T17:00"]
    precip = [0.0, 0.6, 0.0, 0.0]
    codes = [0, 61, 0, 0]
    r = analyze_precip_nowcast(times, precip, codes, NOW, window_minutes=180)
    assert r.state == "dry_incoming"
    assert r.minutes == 60
    assert r.bucket == "rain"


# --- robustness -------------------------------------------------------------

def test_empty_series_returns_none():
    assert analyze_precip_nowcast([], [], [], NOW) is None


def test_bad_now_returns_none():
    assert analyze_precip_nowcast(TIMES_15, [0.0] * 9, _codes(9, 0), "not-a-time") is None


def test_none_precip_treated_as_zero():
    precip = [None] * 9
    r = analyze_precip_nowcast(TIMES_15, precip, _codes(9, 0), NOW, window_minutes=120)
    assert r.state == "dry_clear"


def test_threshold_respected():
    # 0.05mm buckets are below the default 0.1 threshold -> still dry.
    precip = [0.0, 0.0, 0.05, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0]
    r = analyze_precip_nowcast(TIMES_15, precip, _codes(9, 61), NOW, window_minutes=120)
    assert r.state == "dry_clear"
    # Lower the threshold and the same drizzle now registers.
    r2 = analyze_precip_nowcast(TIMES_15, precip, _codes(9, 61), NOW, window_minutes=120, threshold=0.01)
    assert r2.state == "dry_incoming"
    assert r2.minutes == 30


def test_result_is_dataclass():
    r = analyze_precip_nowcast(TIMES_15, [0.0] * 9, _codes(9, 0), NOW)
    assert isinstance(r, NowcastResult)


# --- precip_descriptor ------------------------------------------------------

def test_titlecase_location():
    assert titlecase_location("middlesboro, ky") == "Middlesboro, KY"
    assert titlecase_location("MIDDLESBORO, KY") == "Middlesboro, KY"
    assert titlecase_location("memphis") == "Memphis"
    assert titlecase_location("new york") == "New York"
    assert titlecase_location("paris, france") == "Paris, France"
    assert titlecase_location("nashville,tn") == "Nashville, TN"
    assert titlecase_location("") == ""


def test_city_display_name():
    # Trailing US state code dropped whether or not there's a comma.
    assert city_display_name("london ky") == "London"
    assert city_display_name("london, ky") == "London"
    assert city_display_name("LONDON KY") == "London"
    assert city_display_name("oklahoma city ok") == "Oklahoma City"
    assert city_display_name("new york ny") == "New York"
    # No trailing state -> unchanged (multi-word cities preserved).
    assert city_display_name("oklahoma city") == "Oklahoma City"
    assert city_display_name("miami") == "Miami"
    assert city_display_name("new york") == "New York"
    # 'paris, france' -> city part only (country added separately by the geocoder).
    assert city_display_name("paris, france") == "Paris"


def test_city_display_name_strips_suffix():
    # Trailing country / multi-word region matching the geocoder suffix is dropped.
    assert city_display_name("paris france", "France") == "Paris"
    assert city_display_name("london united kingdom", "United Kingdom") == "London"
    assert city_display_name("paris ky", "KY") == "Paris"
    assert city_display_name("medellin colombia", "Colombia") == "Medellin"
    # Suffix that isn't actually trailing leaves the name intact.
    assert city_display_name("miami", "FL") == "Miami"
    assert city_display_name("san francisco", "CA") == "San Francisco"


def test_precip_descriptor():
    assert precip_descriptor("snow") == ("🌨️", "Snow")
    assert precip_descriptor("heavy_rain") == ("🌧️", "Heavy rain")
    assert precip_descriptor("thunder") == ("⛈️", "Thunderstorms")
    # Unknown / None default to rain
    assert precip_descriptor(None) == ("🌧️", "Rain")
    assert precip_descriptor("bogus") == ("🌧️", "Rain")


# --- decide_rain_notification (proactive push state machine) -----------------

def _decide(state, minutes, *, start=False, end=False, since_start=None, since_end=None,
            lead=60, renotify=30, announce_ending=True):
    return decide_rain_notification(
        state, minutes, lead_minutes=lead, start_announced=start, end_announced=end,
        seconds_since_last_start=since_start, seconds_since_last_end=since_end,
        renotify_minutes=renotify, announce_ending=announce_ending,
    )


def test_decide_dry_clear_rearms():
    # dry_clear always ends the episode (both flags -> False), never sends.
    assert _decide("dry_clear", None, start=True, end=True) == (None, False, False)
    assert _decide("dry_clear", None) == (None, False, False)


def test_decide_raining_continuing_marks_started():
    # Raining with no break in window: mark the start done, fire nothing here.
    assert _decide("raining_continuing", None) == (None, True, False)


def test_decide_incoming_fresh_fires_starting():
    assert _decide("dry_incoming", 30) == ("starting", True, False)


def test_decide_incoming_already_announced_suppressed():
    assert _decide("dry_incoming", 30, start=True) == (None, True, False)


def test_decide_incoming_outside_lead_waits():
    assert _decide("dry_incoming", 90) == (None, False, False)
    assert _decide("dry_incoming", 90, start=True) == (None, True, False)


def test_decide_incoming_none_minutes_waits():
    assert _decide("dry_incoming", None) == (None, False, False)


def test_decide_start_cooldown_holds_then_releases():
    assert _decide("dry_incoming", 20, since_start=5 * 60) == (None, False, False)
    assert _decide("dry_incoming", 20, since_start=31 * 60) == ("starting", True, False)


# --- ending notice (symmetric "rain stopping") ---

def test_decide_ending_fires_once():
    assert _decide("raining_stopping", 20) == ("ending", True, True)
    # Already announced this episode -> suppressed.
    assert _decide("raining_stopping", 20, start=True, end=True) == (None, True, True)


def test_decide_ending_outside_lead_waits():
    assert _decide("raining_stopping", 90) == (None, True, False)


def test_decide_ending_disabled_by_flag():
    assert _decide("raining_stopping", 20, announce_ending=False) == (None, True, False)


def test_decide_ending_cooldown_holds_then_releases():
    assert _decide("raining_stopping", 20, since_end=5 * 60) == (None, True, False)
    assert _decide("raining_stopping", 20, since_end=31 * 60) == ("ending", True, True)


def test_decide_full_episode_sequence():
    """Simulate the service poll loop across a full episode: 'starting' once,
    then 'ending' once, deduped across polls, both reset on clear."""
    start_ann, end_ann = False, False
    last_start, last_end, clock = None, None, 0
    polls = [
        ("dry_clear", None),            # quiet
        ("dry_incoming", 30),           # rain incoming -> starting
        ("dry_incoming", 15),           # dedup
        ("raining_continuing", None),   # raining, no break -> nothing
        ("raining_stopping", 20),       # clear-up incoming -> ending
        ("raining_stopping", 10),       # dedup
        ("dry_clear", None),            # cleared -> reset
    ]
    kinds = []
    for state, minutes in polls:
        ss = None if last_start is None else (clock - last_start)
        se = None if last_end is None else (clock - last_end)
        kind, start_ann, end_ann = decide_rain_notification(
            state, minutes, lead_minutes=60, start_announced=start_ann, end_announced=end_ann,
            seconds_since_last_start=ss, seconds_since_last_end=se, renotify_minutes=30,
        )
        if kind == "starting":
            last_start = clock
        elif kind == "ending":
            last_end = clock
        kinds.append(kind)
        clock += 15 * 60  # advance 15 min between polls

    assert kinds == [None, "starting", None, None, "ending", None, None]
