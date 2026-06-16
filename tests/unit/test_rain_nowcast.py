#!/usr/bin/env python3
"""Unit tests for the rain-nowcast pure logic (no network).

Exercises analyze_precip_nowcast / precip_bucket_for_code / _round5 with
synthetic 15-minutely (and hourly) precipitation series. "Now" is supplied
explicitly, matching how the command derives it from the API's current.time.
"""

from modules.commands.rain_command import (
    RAIN_FAMILY,
    SNOW_FAMILY,
    NowcastResult,
    _iso_duration_hours,  # noqa: PLC2701 (testing internal helper)
    _nws_hourly,  # noqa: PLC2701
    _nws_weather_code,  # noqa: PLC2701
    _round5,  # noqa: PLC2701 (testing internal helper)
    analyze_precip_nowcast,
    city_display_name,
    decide_rain_notification,
    episode_probability_temp,
    format_amount_estimate,
    format_precip_amount,
    format_snow_amount,
    join_location,
    precip_bucket_for_code,
    precip_descriptor,
    titlecase_location,
)
from modules.region_capitals import REGION_DEFAULT_NOTE, region_capital_query

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


# --- amount estimate (amount_mm on the result) ------------------------------

def test_amount_dry_incoming_episode_sum():
    # Rain 14:30 + 14:45 (0.5 each), dry after -> episode total 1.0 mm.
    precip = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 0, 61, 61, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert abs(r.amount_mm - 1.0) < 1e-9


def test_amount_dry_incoming_open_ended_sums_to_window_edge():
    # Rain from 14:30 through 16:00 (+120, the window edge): 7 buckets x 0.5.
    precip = [0.0, 0.0] + [0.5] * 7
    codes = [0, 0] + [63] * 7
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert r.open_ended is True
    assert abs(r.amount_mm - 3.5) < 1e-9


def test_amount_raining_stopping_includes_current_bucket():
    # Raining now (0.5) + 14:15 (0.5), clears at 14:30 -> 1.0 mm remaining.
    precip = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [63, 63, 0, 0, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120)
    assert r.state == "raining_stopping"
    assert abs(r.amount_mm - 1.0) < 1e-9


def test_amount_raining_continuing_sums_current_plus_window():
    # Steady 0.5 across the current bucket + 8 upcoming -> 4.5 mm over 2h.
    precip = [0.5] * 9
    r = analyze_precip_nowcast(TIMES_15, precip, _codes(9, 65), NOW, window_minutes=120)
    assert r.state == "raining_continuing"
    assert abs(r.amount_mm - 4.5) < 1e-9


def test_amount_dry_clear_is_none():
    r = analyze_precip_nowcast(TIMES_15, [0.0] * 9, _codes(9, 0), NOW, window_minutes=120)
    assert r.state == "dry_clear"
    assert r.amount_mm is None


# --- format_precip_amount ---------------------------------------------------

def test_format_amount_inches_trims_zeros():
    assert format_precip_amount(5.08, "in") == "0.2 in"   # user's example
    assert format_precip_amount(25.4, "in") == "1 in"      # exact inch
    assert format_precip_amount(4.5, "in") == "0.18 in"


def test_format_amount_inches_trace_and_none():
    assert format_precip_amount(0.2, "in") == "<0.01 in"   # 0.008 in rounds away
    assert format_precip_amount(0.0, "in") is None
    assert format_precip_amount(None, "in") is None
    assert format_precip_amount(-1.0, "in") is None


def test_format_amount_mm_unit():
    assert format_precip_amount(5.0, "mm") == "5.0 mm"
    assert format_precip_amount(12.3, "mm") == "12.3 mm"
    assert format_precip_amount(0.05, "mm") == "<0.1 mm"


# --- snowfall amount (depth, not liquid equivalent) -------------------------

def test_amount_snow_uses_snowfall_series():
    # Snow incoming 14:30-14:45: 3.5 cm/bucket snow vs only 0.5 mm/bucket liquid.
    precip = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    snow = [0.0, 0.0, 3.5, 3.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 0, 73, 73, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, window_minutes=120, snow=snow)
    assert r.state == "dry_incoming"
    assert r.bucket == "snow"
    assert abs(r.amount_mm - 1.0) < 1e-9   # liquid equivalent
    assert abs(r.snow_cm - 7.0) < 1e-9     # actual snow depth (the useful number)


def test_amount_snow_none_series_defaults_zero():
    # No snow series -> snow_cm sums to 0; the rain path is unaffected.
    precip = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    r = analyze_precip_nowcast(TIMES_15, precip, _codes(9, 61), NOW, window_minutes=120)
    assert r.state == "dry_incoming"
    assert r.bucket == "rain"
    assert r.snow_cm == 0.0


def test_format_snow_amount():
    assert format_snow_amount(7.0, "in") == "2.8 in snow"    # 7 cm -> ~2.8"
    assert format_snow_amount(2.54, "in") == "1 in snow"      # exact inch
    assert format_snow_amount(0.1, "in") == "<0.1 in snow"
    assert format_snow_amount(0.0, "in") is None
    assert format_snow_amount(None, "in") is None
    assert format_snow_amount(12.5, "mm") == "12.5 cm snow"   # metric shows cm


def test_episode_probability_temp_dry_incoming():
    # Rain starts at 14:30 (+30min) -> prob/temp read at that bucket.
    s = {"times": TIMES_15, "now": NOW,
         "prob": [10, 20, 80, 80, 30, 0, 0, 0, 0],
         "temp": [20, 20, 1.0, 1.0, 5, 5, 5, 5, 5]}  # 1.0C -> 34F
    r = NowcastResult(state="dry_incoming", minutes=30, bucket="rain")
    assert episode_probability_temp(s, r) == (80, 34)


def test_episode_probability_temp_raining_now():
    s = {"times": TIMES_15, "now": NOW,
         "prob": [90, 90, 0, 0, 0, 0, 0, 0, 0],
         "temp": [0.0, 0.0, 0, 0, 0, 0, 0, 0, 0]}  # 0C -> 32F at "now" bucket
    r = NowcastResult(state="raining_continuing", bucket="rain")
    assert episode_probability_temp(s, r) == (90, 32)


def test_episode_probability_temp_dry_clear_and_missing():
    s = {"times": TIMES_15, "now": NOW, "prob": [0] * 9, "temp": [10] * 9}
    assert episode_probability_temp(s, NowcastResult(state="dry_clear")) == (None, None)
    s2 = {"times": TIMES_15, "now": NOW, "prob": [], "temp": []}
    assert episode_probability_temp(s2, NowcastResult(state="dry_incoming", minutes=30, bucket="rain")) == (None, None)


def test_format_amount_estimate_per_bucket():
    # rain/showers -> liquid; snow -> depth from snow_cm; freezing -> liquid + "ice".
    assert format_amount_estimate("rain", 5.08, 0.0, "in") == "0.2 in"
    assert format_amount_estimate("heavy_rain", 12.7, 0.0, "in") == "0.5 in"
    assert format_amount_estimate("snow", 8.6, 7.0, "in") == "2.8 in snow"
    assert format_amount_estimate("freezing", 2.54, 0.0, "in") == "0.1 in ice"
    assert format_amount_estimate("freezing", 5.0, 0.0, "mm") == "5.0 mm ice"
    assert format_amount_estimate("drizzle", 0.0, 0.0, "in") is None   # negligible -> no estimate


# --- precip family filter (the !rain vs !snow engine) -----------------------

def test_family_snow_ignores_rain():
    # Rain incoming, no snow.
    precip = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 0, 61, 61, 0, 0, 0, 0, 0]
    assert analyze_precip_nowcast(TIMES_15, precip, codes, NOW, family=SNOW_FAMILY).state == "dry_clear"
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, family=RAIN_FAMILY)
    assert r.state == "dry_incoming" and r.bucket == "rain"


def test_family_rain_ignores_snow():
    precip = [0.0, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    snow = [0.0, 0.0, 3.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 0, 73, 73, 0, 0, 0, 0, 0]
    assert analyze_precip_nowcast(TIMES_15, precip, codes, NOW, snow=snow, family=RAIN_FAMILY).state == "dry_clear"
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, snow=snow, family=SNOW_FAMILY)
    assert r.state == "dry_incoming" and r.bucket == "snow"


def test_family_smart_hunt_rain_now_snow_later():
    # Rain at 14:15, then snow at 14:45-15:00. !snow finds the later snow;
    # !rain finds the sooner rain. (The "smart hunt" the user asked for.)
    precip = [0.0, 0.5, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0]
    snow = [0.0, 0.0, 0.0, 3.0, 3.0, 0.0, 0.0, 0.0, 0.0]
    codes = [0, 61, 0, 73, 73, 0, 0, 0, 0]
    rsnow = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, snow=snow, family=SNOW_FAMILY)
    assert rsnow.state == "dry_incoming" and rsnow.bucket == "snow" and rsnow.minutes == 45
    rrain = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, snow=snow, family=RAIN_FAMILY)
    assert rrain.state == "dry_incoming" and rrain.bucket == "rain" and rrain.minutes == 15


def test_family_none_still_sees_any_precip():
    # No filter: a snow series is still detected (as the snow bucket).
    precip = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    snow = [3.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    codes = [73, 73, 0, 0, 0, 0, 0, 0, 0]
    r = analyze_precip_nowcast(TIMES_15, precip, codes, NOW, snow=snow,
                               current_precip=0.5, current_code=73)
    assert r.state == "raining_stopping" and r.bucket == "snow"


# --- join_location (the 'Spain, Spain' dedup) -------------------------------

def test_join_location_dedupes_equal_names():
    # The reported bug: '!rain spain' -> city 'Spain' + country 'Spain'.
    assert join_location("Spain", "Spain") == "Spain"
    assert join_location("Singapore", "Singapore") == "Singapore"
    assert join_location("spain", "Spain") == "Spain"   # case-insensitive


def test_join_location_keeps_distinct_names():
    assert join_location("Nashville", "TN") == "Nashville, TN"
    assert join_location("Paris", "France") == "Paris, France"


def test_join_location_handles_missing_sides():
    assert join_location("Nashville", None) == "Nashville"
    assert join_location("Nashville", "") == "Nashville"
    assert join_location(None, "France") == "France"
    assert join_location("", "France") == "France"
    assert join_location(None, None) == ""


def test_spain_end_to_end_label():
    # city_display_name + join_location together, as _resolve_location does it.
    suffix = "Spain"
    typed_city = city_display_name("spain", suffix)
    assert join_location(typed_city, suffix) == "Spain"   # not "Spain, Spain"


# --- region_capital_query (bare country/state -> capital) --------------------

def test_region_capital_country():
    assert region_capital_query("france") == "Paris, France"
    assert region_capital_query("spain") == "Madrid, Spain"
    assert region_capital_query("japan") == "Tokyo, Japan"


def test_region_capital_us_state():
    assert region_capital_query("texas") == "Austin, TX"
    assert region_capital_query("california") == "Sacramento, CA"
    assert region_capital_query("georgia") == "Atlanta, GA"   # US state wins over country


def test_region_capital_aliases_and_normalization():
    assert region_capital_query("uk") == "London, United Kingdom"
    assert region_capital_query("usa") == "Washington, United States"
    assert region_capital_query("FRANCE") == "Paris, France"     # case-insensitive
    assert region_capital_query("  texas  ") == "Austin, TX"      # trimmed


def test_region_capital_excludes_city_dominant_states():
    # New York / Washington almost always mean the city -> not defaulted.
    assert region_capital_query("new york") is None
    assert region_capital_query("washington") is None


def test_region_capital_non_regions_pass_through():
    assert region_capital_query("paris") is None          # a city, not a region
    assert region_capital_query("nashville") is None
    assert region_capital_query("paris, france") is None  # already qualified
    assert region_capital_query("37013") is None          # a ZIP
    assert region_capital_query("") is None
    assert region_capital_query(None) is None


def test_region_note_fits_channel_budget():
    # 160-byte channel cap minus 'WeatherBot-V3: ' prefix = 145 body bytes.
    budget = 160 - len(b"WeatherBot-V3") - 2
    worst_forecast = "🌧️ Heavy rain steady for 2h+ in Paris, France (est 0.5 in)"
    combined = f"{worst_forecast} {REGION_DEFAULT_NOTE}"
    assert len(combined.encode("utf-8")) <= budget


# --- NWS gridpoint source (fetch_precip_series_nws helpers) -------------------

def test_iso_duration_hours():
    assert _iso_duration_hours("PT1H") == 1
    assert _iso_duration_hours("PT6H") == 6
    assert _iso_duration_hours("P1DT6H") == 30
    assert _iso_duration_hours("PT3H") == 3
    assert _iso_duration_hours("") == 1       # unparseable -> at least 1 hour
    assert _iso_duration_hours("PT30M") == 1  # sub-hour -> 1


def test_nws_weather_code_classification():
    # NWS weather types map to WMO codes that classify into the same buckets.
    assert precip_bucket_for_code(_nws_weather_code([{"weather": "thunderstorms"}])) == "thunder"
    assert precip_bucket_for_code(_nws_weather_code([{"weather": "snow"}])) == "snow"
    assert precip_bucket_for_code(_nws_weather_code([{"weather": "rain"}])) == "rain"
    assert precip_bucket_for_code(_nws_weather_code([{"weather": "freezing_rain"}])) == "freezing"
    # a thunderstorm-with-rain still classifies as thunder (priority order)
    assert _nws_weather_code([{"weather": "rain"}, {"weather": "thunderstorms"}]) == 95
    assert _nws_weather_code([]) is None
    assert _nws_weather_code([{"weather": None}]) is None


def test_nws_hourly_divide_and_repeat():
    # a 6-hour QPF accumulation is split evenly across its hours
    spread = _nws_hourly(
        [{"validTime": "2026-06-08T12:00:00+00:00/PT6H", "value": 6.0}], divide=True
    )
    assert len(spread) == 6
    assert all(abs(v - 1.0) < 1e-9 for v in spread.values())
    # an hourly PoP is repeated (not divided)
    pop = _nws_hourly(
        [{"validTime": "2026-06-08T12:00:00+00:00/PT1H", "value": 70}], divide=False
    )
    assert list(pop.values()) == [70]
    # malformed / empty inputs are skipped, not fatal
    assert _nws_hourly([{"value": 5}], divide=True) == {}
    assert _nws_hourly(None, divide=False) == {}
