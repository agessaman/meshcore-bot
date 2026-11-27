"""The one rounding rule and the shared conversion factors.

Every displayed temperature, wind speed, gust and visibility goes through
``round_half_away``, so the reference values here are computed exactly with
``Fraction`` and rounded the same way, over the whole range the providers can
see. A float implementation that truncates or uses banker's rounding fails
these by one, which is the bug they exist to catch.
"""

from fractions import Fraction

import pytest

from modules.weather_providers._units import (
    c_to_f,
    hpa_to_mmhg,
    kmh_to,
    mm_to_inch,
    ms_to,
    round_half_away,
)

# The exact factors, as rationals, so the reference never shares the
# implementation's float arithmetic.
MS_FACTORS = {
    "mph": Fraction(1) / Fraction("0.44704"),
    "kmh": Fraction(36, 10),
    "ms": Fraction(1),
    "kn": Fraction(1) / (Fraction(1852) / 3600),
}
KMH_FACTORS = {
    "mph": Fraction(1) / Fraction("1.609344"),
    "kmh": Fraction(1),
    "ms": Fraction(1) / Fraction(36, 10),
    "kn": Fraction(1) / Fraction("1.852"),
}


def half_away(value: Fraction) -> int:
    """The reference: nearest whole number, halves away from zero."""
    sign = -1 if value < 0 else 1
    magnitude = abs(value)
    whole = magnitude.numerator // magnitude.denominator
    if magnitude - whole >= Fraction(1, 2):
        whole += 1
    return sign * whole


def celsius_values():
    """-50.0 to 50.0 °C in 0.1 steps."""
    return [Fraction(i, 10) for i in range(-500, 501)]


def speed_values():
    """0.0 to 40.0 in 0.1 steps."""
    return [Fraction(i, 10) for i in range(0, 401)]


@pytest.mark.unit
def test_celsius_to_fahrenheit_rounds_half_away_over_the_whole_range():
    mismatches = []
    for celsius in celsius_values():
        expected = half_away(celsius * 9 / 5 + 32)
        actual = round_half_away(c_to_f(float(celsius)))
        if actual != expected:
            mismatches.append((float(celsius), expected, actual))

    assert mismatches == []


@pytest.mark.unit
@pytest.mark.parametrize("unit", sorted(MS_FACTORS))
def test_metres_per_second_rounds_half_away_over_the_whole_range(unit):
    factor = MS_FACTORS[unit]
    mismatches = []
    for value in speed_values():
        expected = half_away(value * factor)
        actual = round_half_away(ms_to(float(value), unit))
        if actual != expected:
            mismatches.append((float(value), expected, actual))

    assert mismatches == []


@pytest.mark.unit
@pytest.mark.parametrize("unit", sorted(KMH_FACTORS))
def test_kilometres_per_hour_rounds_half_away_over_the_whole_range(unit):
    factor = KMH_FACTORS[unit]
    mismatches = []
    for value in speed_values():
        expected = half_away(value * factor)
        actual = round_half_away(kmh_to(float(value), unit))
        if actual != expected:
            mismatches.append((float(value), expected, actual))

    assert mismatches == []


@pytest.mark.unit
def test_the_edge_values():
    # 2.5 °C is 36.5 °F, which rounds up; -2.5 is a half away from zero.
    assert round_half_away(c_to_f(2.5)) == 37
    assert round_half_away(-2.5) == -3
    assert round_half_away(c_to_f(-40)) == -40
    assert round_half_away(c_to_f(0)) == 32
    # Truncation and banker's rounding both get these wrong.
    assert round_half_away(36.9) == 37
    assert round_half_away(-0.9) == -1
    assert round_half_away(0.5) == 1
    assert round_half_away(-0.5) == -1


@pytest.mark.unit
def test_millimetres_and_pressure():
    assert mm_to_inch(25.4) == pytest.approx(1.0)
    assert round_half_away(mm_to_inch(12.7)) == 1
    # One standard atmosphere is 1013.25 hPa and 760 mmHg, by definition.
    assert round_half_away(hpa_to_mmhg(1013.25)) == 760
