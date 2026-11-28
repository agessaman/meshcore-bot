"""Unit conversions and the one rounding rule the weather replies use.

Every displayed temperature, wind speed, gust and visibility goes through
:func:`round_half_away`. Python's ``round()`` is banker's rounding (2.5 becomes
2) and ``int()`` truncates (36.9 becomes 36), so both showed a degree or a mile
off from what a person reading the number expects. Halves go away from zero,
which is what a thermometer and a speedometer do.

The conversion factors live here too, so the four forecast providers share one
implementation instead of each carrying its own approximation.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Exact definitions, not approximations: 1 in = 25.4 mm, 1 mph = 0.44704 m/s,
# 1 kn = 1852 m/h, 1 mmHg = 133.322387415 Pa.
MM_PER_INCH = 25.4
M_PER_MILE = 1609.344
MS_PER_MPH = 0.44704
MS_PER_KN = 1852 / 3600
HPA_PER_MMHG = 133.322387415 / 100


def round_half_away(value) -> int:
    """Round to the nearest whole number, halves away from zero.

    ``Decimal(str(value))`` keeps the value the data carried (36.5 stays 36.5,
    not the nearest binary float) and ``ROUND_HALF_UP`` rounds away from zero,
    so -2.5 is -3.

    A null or non-numeric field raises the TypeError ``int()`` used to raise,
    so a malformed response still reads as a missing number in the log rather
    than as a Decimal internals error.
    """
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation:
        raise TypeError(
            "int() argument must be a string, a bytes-like object or a real number, "
            f"not '{type(value).__name__}'"
        ) from None
    return int(decimal_value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def c_to_f(celsius) -> float:
    """Celsius to Fahrenheit."""
    return float(celsius) * 9 / 5 + 32


def ms_to(value, unit: str) -> float:
    """Metres per second to mph, km/h or knots; m/s is returned unchanged."""
    factor = {
        'mph': 1 / MS_PER_MPH,
        'kmh': 3.6,
        'ms': 1.0,
        'kn': 1 / MS_PER_KN,
    }.get(unit, 1 / MS_PER_MPH)
    return float(value) * factor


def kmh_to(value, unit: str) -> float:
    """Kilometres per hour to mph, m/s or knots; km/h is returned unchanged."""
    factor = {
        'mph': 1 / (MS_PER_MPH * 3.6),
        'kmh': 1.0,
        'ms': 1 / 3.6,
        'kn': 1 / 1.852,
    }.get(unit, 1.0)
    return float(value) * factor


def mm_to_inch(millimetres) -> float:
    """Millimetres to inches."""
    return float(millimetres) / MM_PER_INCH


def hpa_to_mmhg(hectopascals) -> float:
    """Hectopascals to millimetres of mercury."""
    return float(hectopascals) / HPA_PER_MMHG
