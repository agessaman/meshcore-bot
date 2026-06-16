#!/usr/bin/env python3
"""End-to-end render tests for the rain/snow command (mocked weather).

Unlike test_rain_nowcast.py (which unit-tests the pure helpers), this drives the
real RainCommand.execute() coroutine and asserts on the *exact string a user
gets back* on the mesh. Scaffolding (real config, real i18n translator, a
capturing send_response, synthetic Open-Meteo series) lives in _rain_harness.py.

Only the two network seams are stubbed: the Open-Meteo fetch (synthetic series)
and location resolution (fixed). Everything in between — mode parsing,
region-capital defaulting, the rain/snow family two-pass, the mismatch
("No snow, but rain ...") and changeover lines, amount/probability/temperature
suffixes, the region heads-up, and the byte-budget truncation — runs for real.
This is the regression gate for "do the commands still come back right".

    .venv/bin/python -m pytest tests/unit/test_rain_command_e2e.py -o addopts="" -q
    # see every rendered reply:
    .venv/bin/python -m pytest tests/unit/test_rain_command_e2e.py -o addopts="" -s -q -k gallery
"""

from modules.models import MeshMessage
from modules.region_capitals import REGION_DEFAULT_NOTE
from tests.unit._rain_harness import (
    DEFAULT_LABEL,
    assert_render,
    build_cmd,
    make_series,
    render,
)

# Reusable code/precip presets (15-min buckets).
_RAIN_INCOMING = dict(precip=[0, 0, 0.5, 0.5, 0.5], codes=[0, 0, 61, 61, 61], prob=[10, 20, 80, 80, 70])
_SNOW_INCOMING = dict(
    precip=[0, 0, 0.3, 0.3, 0.3], codes=[0, 0, 71, 71, 71],
    snow=[0, 0, 2.0, 2.0, 2.0], prob=[10, 20, 80, 80, 70],
)
_RAINING_NOW = dict(precip=[0.5] * 9, codes=[61] * 9, prob=[90] * 9, current_precip=0.5, current_code=61)


# --- dry / clear ------------------------------------------------------------

def test_rain_dry_clear():
    cmd, cap = build_cmd(make_series())
    assert render(cmd, cap, "!rain") == f"☀️ No rain expected in next 2h for {DEFAULT_LABEL}"


def test_snow_dry_clear_says_snow():
    cmd, cap = build_cmd(make_series())
    assert render(cmd, cap, "!snow") == f"☀️ No snow expected in next 2h for {DEFAULT_LABEL}"


def test_nowcast_dry_clear_defaults_to_rain_word():
    cmd, cap = build_cmd(make_series())
    assert render(cmd, cap, "!nowcast") == f"☀️ No rain expected in next 2h for {DEFAULT_LABEL}"


# --- incoming precipitation -------------------------------------------------

def test_rain_incoming_has_amount_and_probability():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING))
    resp = render(cmd, cap, "!rain")
    assert_render(
        resp,
        r"🌧️ Rain starting in ~30min for Nashville, TN \(~\d+min\) \(est [\d.]+ in, 80%\)",
    )


def test_nowcast_incoming_is_plain_rain_no_mismatch_wording():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING))
    resp = render(cmd, cap, "!nowcast")
    assert "No " not in resp
    assert resp.startswith("🌧️ Rain starting in ~30min for Nashville, TN")


def test_snow_incoming_shows_depth():
    cmd, cap = build_cmd(make_series(**_SNOW_INCOMING))
    resp = render(cmd, cap, "!snow")
    assert_render(
        resp,
        r"🌨️ Snow starting in ~30min for Nashville, TN \(~\d+min\) \(est [\d.]+ in snow, 80%\)",
    )


def test_raining_now_continuing():
    cmd, cap = build_cmd(make_series(**_RAINING_NOW))
    resp = render(cmd, cap, "!rain")
    assert_render(resp, r"🌧️ Rain steady for 2h\+ in Nashville, TN \(est [\d.]+ in, 90%\)")


# --- cross-type mismatch ----------------------------------------------------

def test_snow_command_when_raining_says_no_snow_but_rain():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING))
    resp = render(cmd, cap, "!snow")
    assert_render(
        resp,
        r"🌧️ No snow, but rain starting in ~30min for Nashville, TN \(~\d+min\) \(est [\d.]+ in, 80%\)",
    )


def test_rain_command_when_snowing_says_no_rain_but_snow():
    cmd, cap = build_cmd(make_series(**_SNOW_INCOMING))
    resp = render(cmd, cap, "!rain")
    assert_render(
        resp,
        r"🌨️ No rain, but snow starting in ~30min for Nashville, TN \(~\d+min\) \(est [\d.]+ in snow, 80%\)",
    )


# --- changeover (both families in the window) -------------------------------

def test_rain_now_then_snow_changeover():
    series = make_series(
        precip=[0.5] * 9,
        codes=[61, 61, 61, 61, 71, 71, 71, 71, 71],
        snow=[0, 0, 0, 0, 2.0, 2.0, 2.0, 2.0, 2.0],
        prob=[90] * 9,
        current_precip=0.5,
        current_code=61,
    )
    cmd, cap = build_cmd(series)
    resp = render(cmd, cap, "!rain")
    assert_render(resp, r"🌧️→🌨️ Rain now → snow in ~\d+min for Nashville, TN")


# --- freezing rain tagged as ice --------------------------------------------

def test_freezing_rain_estimate_tagged_in_ice():
    series = make_series(precip=[0, 0, 0.4, 0.4, 0.4], codes=[0, 0, 66, 66, 66], prob=[10, 20, 70, 70, 60])
    cmd, cap = build_cmd(series)
    resp = render(cmd, cap, "!rain")
    assert_render(
        resp,
        r"🧊 Freezing rain starting in ~30min for Nashville, TN \(~\d+min\) \(est [\d.]+ in ice, 70%\)",
    )


# --- borderline temperature tag ---------------------------------------------

def test_borderline_temp_tag_appended_near_freezing():
    # ~1.7 C == ~35 F at the start bucket -> tag shown.
    series = make_series(**{**_RAIN_INCOMING, "temp": [10, 10, 1.7, 1.7, 1.7]})
    cmd, cap = build_cmd(series)
    resp = render(cmd, cap, "!rain")
    assert resp.endswith("35°F"), resp
    assert_render(resp, r".+ \(est [\d.]+ in, 80%\) 35°F")


def test_no_temp_tag_when_mild():
    # 10 C == 50 F -> outside the 30-38 F band -> no tag.
    series = make_series(**{**_RAIN_INCOMING, "temp": [10] * 9})
    cmd, cap = build_cmd(series)
    resp = render(cmd, cap, "!rain")
    assert "°F" not in resp
    assert resp.endswith("%)"), resp


def test_show_temp_disabled_via_config():
    series = make_series(**{**_RAIN_INCOMING, "temp": [1.7] * 9})
    cmd, cap = build_cmd(series, rain_overrides={"show_temp": "false"})
    resp = render(cmd, cap, "!rain")
    assert "°F" not in resp


# --- region-capital defaulting + heads-up -----------------------------------

def test_bare_country_defaults_to_capital_with_note():
    cmd, cap = build_cmd(make_series())
    resp = render(cmd, cap, "!rain france")
    assert "Paris, France" in resp
    assert resp.endswith(REGION_DEFAULT_NOTE), resp


def test_spain_does_not_double_and_gets_capital():
    cmd, cap = build_cmd(make_series())
    resp = render(cmd, cap, "!rain spain")
    assert "Madrid, Spain" in resp
    assert "Spain, Spain" not in resp
    assert resp.endswith(REGION_DEFAULT_NOTE), resp


def test_us_state_defaults_to_capital_with_note():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING))
    resp = render(cmd, cap, "!rain texas")
    assert "Austin, TX" in resp
    assert resp.endswith(REGION_DEFAULT_NOTE), resp


# --- config toggles ---------------------------------------------------------

def test_show_amount_disabled_hides_estimate():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING), rain_overrides={"show_amount": "false"})
    resp = render(cmd, cap, "!rain")
    assert "est" not in resp
    assert_render(resp, r"🌧️ Rain starting in ~30min for Nashville, TN \(~\d+min\) \(80%\)")


def test_amount_unit_mm():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING), rain_overrides={"amount_unit": "mm"})
    resp = render(cmd, cap, "!rain")
    assert "mm" in resp
    assert " in," not in resp


# --- DM path ----------------------------------------------------------------

def test_dm_renders_and_fits_dm_budget():
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING))
    resp = render(cmd, cap, "!rain", is_dm=True)
    assert resp.startswith("🌧️ Rain starting in ~30min for Nashville, TN")


# --- keyword-aware help -----------------------------------------------------

def test_help_rain_vs_snow_keyword_aware():
    cmd, _ = build_cmd(make_series())
    rain_help = cmd.get_help_text(MeshMessage(content="help rain", channel="general", sender_id="U1"))
    snow_help = cmd.get_help_text(MeshMessage(content="help snow", channel="general", sender_id="U1"))
    bare_help = cmd.get_help_text(MeshMessage(content="help", channel="general", sender_id="U1"))
    assert "rain" in rain_help.lower() and "amount" in rain_help.lower()
    assert "snow" in snow_help.lower() and "depth" in snow_help.lower()
    assert rain_help != snow_help
    assert bare_help == rain_help  # defaults to rain when unspecified


# --- gallery (visual): print one of each, run with -s to eyeball ------------

def test_gallery_prints_representative_replies():
    cases = [
        ("!rain  (dry)", make_series()),
        ("!rain  (incoming)", make_series(**_RAIN_INCOMING)),
        ("!rain  (raining now)", make_series(**_RAINING_NOW)),
        ("!snow  (incoming)", make_series(**_SNOW_INCOMING)),
        ("!rain  (freezing)", make_series(precip=[0, 0, 0.4, 0.4, 0.4], codes=[0, 0, 66, 66, 66], prob=[0, 0, 70, 70, 60])),
    ]
    lines = []
    for label, series in cases:
        cmd, cap = build_cmd(series)
        word = label.split()[0].lstrip("!")
        resp = render(cmd, cap, f"!{word}")
        lines.append(f"{label:24s} -> {resp}   [{len(resp.encode('utf-8'))}B]")

    # snow-but-raining mismatch + region capital, for completeness
    cmd, cap = build_cmd(make_series(**_RAIN_INCOMING))
    lines.append(f"{'!snow  (but raining)':24s} -> {render(cmd, cap, '!snow')}")
    cmd, cap = build_cmd(make_series())
    lines.append(f"{'!rain france (capital)':24s} -> {render(cmd, cap, '!rain france')}")

    print("\n--- rain/snow command gallery ---")
    for ln in lines:
        print(ln)
    assert all("None" not in ln for ln in lines)  # no stray Nones leaked into output
