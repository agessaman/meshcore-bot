#!/usr/bin/env python3
"""
Rain nowcast command - minute-level "rain starting/stopping in ~N min" using
Open-Meteo's 15-minutely precipitation forecast. Works worldwide, no API key.
"""

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import MeshMessage
from ..region_capitals import REGION_DEFAULT_NOTE, region_capital_query
from ..utils import geocode_city_sync, geocode_zipcode_sync, normalize_us_state
from .base_command import BaseCommand

# WMO weather code -> precipitation "bucket". Buckets map to an emoji and a
# translatable label (commands.rain.precip_types.<bucket>). Codes not listed
# here are non-precipitating and never trigger a nowcast.
_PRECIP_BUCKETS: dict[int, str] = {
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "freezing", 57: "freezing",
    61: "rain", 63: "rain", 65: "heavy_rain",
    66: "freezing", 67: "freezing",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "showers", 81: "showers", 82: "heavy_rain",
    85: "snow", 86: "snow",
    95: "thunder", 96: "thunder", 99: "thunder",
}

# Emoji per bucket (leads the response line).
_BUCKET_EMOJI: dict[str, str] = {
    "drizzle": "🌦️",
    "rain": "🌧️",
    "heavy_rain": "🌧️",
    "freezing": "🧊",
    "snow": "🌨️",
    "showers": "🌦️",
    "thunder": "⛈️",
}

# English fallbacks for precip type labels (translations override via
# commands.rain.precip_types.<bucket>; missing keys fall back to en.json).
_BUCKET_LABEL_EN: dict[str, str] = {
    "drizzle": "Drizzle",
    "rain": "Rain",
    "heavy_rain": "Heavy rain",
    "freezing": "Freezing rain",
    "snow": "Snow",
    "showers": "Showers",
    "thunder": "Thunderstorms",
}

# Precip "families" for the !rain vs !snow modes. A command looks for its own
# family across the window first; only if none is coming does it fall back to
# the other type with a "No <type>, but ..." heads-up. Freezing rain is liquid,
# so it lives in the rain family (a !snow ice-event reads "No snow, but ...").
RAIN_FAMILY = frozenset({"drizzle", "rain", "heavy_rain", "showers", "thunder", "freezing"})
SNOW_FAMILY = frozenset({"snow"})

# Upper bound on the per-instance geocoding caches so a long-running bot that's
# queried for many distinct locations can't grow them without limit.
_GEOCODE_CACHE_CAP = 256


def _cache_put(cache: dict, key: Any, value: Any) -> None:
    """Insert into a size-capped cache, evicting the oldest entry when full.

    Relies on dicts preserving insertion order (Python 3.7+).
    """
    if key not in cache and len(cache) >= _GEOCODE_CACHE_CAP:
        cache.pop(next(iter(cache)))
    cache[key] = value


def precip_bucket_for_code(code: Optional[int]) -> Optional[str]:
    """Map a WMO weather code to a precipitation bucket, or None if not precip."""
    if code is None:
        return None
    try:
        return _PRECIP_BUCKETS.get(int(code))
    except (TypeError, ValueError):
        return None


def titlecase_location(text: str) -> str:
    """Tidy a user-typed location for display.

    'middlesboro, ky' -> 'Middlesboro, KY'; 'paris, france' -> 'Paris, France';
    'memphis' -> 'Memphis'. A 2-letter token after a comma is treated as a
    state/country code and upper-cased; everything else is title-cased.
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return text.strip()
    out = []
    for i, p in enumerate(parts):
        if i > 0 and len(p) == 2 and p.isalpha():
            out.append(p.upper())
        else:
            out.append(p.title())
    return ", ".join(out)


# US state / territory 2-letter codes — used to drop a trailing state from a
# typed location like "london ky" (no comma) so it doesn't become "London Ky".
US_STATE_ABBRS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "AS", "GU", "MP", "PR", "VI",
})


def city_display_name(typed_location: str, suffix: Optional[str] = None) -> str:
    """City part of a typed location for display, dropping a trailing region the
    user appended without a comma.

    'london ky' -> 'London'; 'paris france' -> 'Paris'; 'london, ky' -> 'London';
    'oklahoma city' -> 'Oklahoma City'. `suffix` is the geocoder's authoritative
    state/country (e.g. 'KY' or 'France'); when the typed text ends with it, it's
    stripped so it isn't doubled into the city name. The state/country is added
    back separately by the caller.
    """
    head = typed_location.split(",")[0].strip()
    # Drop a trailing region matching the geocoder's suffix — handles country
    # names and multi-word regions ("paris france", "london united kingdom").
    if suffix and head.lower().endswith(" " + suffix.lower()):
        head = head[: -len(suffix)].strip()
    # Drop a trailing US state abbreviation ("london ky" -> "london").
    tokens = head.split()
    if len(tokens) >= 2 and tokens[-1].upper() in US_STATE_ABBRS:
        head = " ".join(tokens[:-1])
    return titlecase_location(head)


def join_location(city: Optional[str], suffix: Optional[str]) -> str:
    """Join a city and its state/country suffix as 'City, Suffix'.

    Collapses to a single name when one side is missing or the two name the same
    place (case-insensitive) — so a country typed as the city ('spain' -> 'Spain',
    not 'Spain, Spain') or a city-state ('Singapore', not 'Singapore, Singapore')
    renders once.
    """
    city = (city or "").strip()
    suffix = (suffix or "").strip()
    if not suffix:
        return city
    if not city or city.lower() == suffix.lower():
        return suffix
    return f"{city}, {suffix}"


def reverse_geocode_region(
    bot: Any, lat: float, lon: float, *, timeout: int = 10, logger: Any = None
) -> tuple[Optional[str], Optional[str]]:
    """Reverse-geocode to (city, suffix), respecting the bot's Nominatim rate limiter.

    suffix is the US state abbreviation ('TN') for US points, else the English
    country name ('Japan'). Requests language='en' so country names aren't
    localized. No caching (callers cache as needed). Shared by the rain command
    and the Weather_Service proactive push so both label locations identically.
    """
    city: Optional[str] = None
    suffix: Optional[str] = None
    try:
        from ..utils import get_nominatim_geocoder
        limiter = getattr(bot, "nominatim_rate_limiter", None)
        if limiter is not None:
            limiter.wait_for_request_sync()
        geolocator = get_nominatim_geocoder(timeout=timeout)
        # language="en" so country names come back in English ("Japan", not "日本").
        result = geolocator.reverse(f"{lat}, {lon}", timeout=timeout, language="en")
        if limiter is not None:
            limiter.record_request()
        if result is not None and hasattr(result, "raw"):
            address = result.raw.get("address", {})
            city = (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("municipality")
                or address.get("county")
                or None
            )
            country_code = (address.get("country_code") or "").lower()
            if country_code == "us":
                iso = address.get("ISO3166-2-lvl4") or address.get("ISO3166-2-lvl6") or ""
                if "-" in iso:
                    suffix = iso.rsplit("-", 1)[-1]
                else:
                    state_abbr, _ = normalize_us_state(address.get("state", ""))
                    suffix = state_abbr or address.get("state") or None
            else:
                suffix = address.get("country") or None
    except Exception as e:
        if logger:
            logger.debug(f"Error reverse geocoding {lat},{lon}: {e}")
    return city, suffix


def precip_descriptor(bucket: Optional[str]) -> tuple[str, str]:
    """Return (emoji, English label) for a precip bucket; defaults to rain.

    The label is English; localized callers use the commands.rain.precip_types
    translation keys instead. Shared so the Weather_Service can build proactive
    nowcast messages without duplicating the bucket tables.
    """
    b = bucket or "rain"
    return _BUCKET_EMOJI.get(b, "🌧️"), _BUCKET_LABEL_EN.get(b, "Rain")


def format_precip_amount(mm: Optional[float], unit: str = "in") -> Optional[str]:
    """Format an accumulated precip total (mm) for display, or None if negligible.

    ``unit`` "in" renders inches (the US default), anything else millimetres.
    Returns None for a non-positive total so callers can omit the estimate
    entirely. Inches trim trailing zeros: 0.20 -> "0.2 in", 0.05 -> "0.05 in".
    """
    if mm is None or mm <= 0:
        return None
    if unit == "mm":
        return f"{mm:.1f} mm" if mm >= 0.1 else "<0.1 mm"
    inches = mm * 0.0393701
    if inches < 0.01:
        return "<0.01 in"
    return f"{inches:.2f}".rstrip("0").rstrip(".") + " in"


def format_snow_amount(cm: Optional[float], unit: str = "in") -> Optional[str]:
    """Format a snowfall total (cm of actual snow) for display, or None if negligible.

    Snow is reported as depth, not liquid equivalent (Open-Meteo's ``precipitation``
    is the melted equivalent, ~7x less). ``unit`` "in" renders inches of snow (US
    default), anything else centimetres. The trailing " snow" keeps it distinct
    from the liquid rain estimate; 0.1 precision suits snow's coarseness.
    """
    if cm is None or cm <= 0:
        return None
    if unit == "mm":
        return f"{cm:.1f} cm snow" if cm >= 0.1 else "<0.1 cm snow"
    inches = cm * 0.393701
    if inches < 0.1:
        return "<0.1 in snow"
    return f"{inches:.1f}".rstrip("0").rstrip(".") + " in snow"


def format_amount_estimate(
    bucket: Optional[str], amount_mm: Optional[float], snow_cm: Optional[float], unit: str = "in"
) -> Optional[str]:
    """The estimate string for a precip bucket, or None if negligible.

    Picks the right quantity per type: snow shows depth ("3 in snow"); freezing
    rain shows its liquid/glaze amount tagged "ice" ("0.1 in ice", ~1:1 with
    accretion); everything else is plain liquid ("0.2 in").
    """
    if bucket == "snow":
        return format_snow_amount(snow_cm, unit)
    amt = format_precip_amount(amount_mm, unit)
    if amt and bucket == "freezing":
        return f"{amt} ice"
    return amt


def episode_probability_temp(series: dict, result: "NowcastResult") -> tuple[Optional[int], Optional[int]]:
    """Precip probability (%) and 2 m temperature (°F) at the episode's defining
    moment — the current bucket when precipitating now, else the start bucket.
    Returns (None, None) for dry_clear or when the data is missing.
    """
    if result is None or result.state == "dry_clear":
        return None, None
    times = series.get("times") or []
    probs = series.get("prob") or []
    temps = series.get("temp") or []
    try:
        now = datetime.fromisoformat(series["now"])
    except (TypeError, ValueError, KeyError):
        return None, None
    if result.state in ("raining_stopping", "raining_continuing"):
        target = now
    else:  # dry_incoming: align with when precip begins
        target = now + timedelta(minutes=result.minutes or 0)
    best_i: Optional[int] = None
    best_d: Optional[float] = None
    for i, t in enumerate(times):
        try:
            tt = datetime.fromisoformat(t)
        except (TypeError, ValueError):
            continue
        d = abs((tt - target).total_seconds())
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    if best_i is None:
        return None, None
    prob = probs[best_i] if best_i < len(probs) else None
    tc = temps[best_i] if best_i < len(temps) else None
    prob_pct = int(round(prob)) if prob is not None else None
    temp_f = int(round(tc * 9 / 5 + 32)) if tc is not None else None
    return prob_pct, temp_f


# Short-lived cache of fetched series, keyed by rounded coords + model, so the
# command and the proactive poll can reuse one fetch instead of re-hitting
# Open-Meteo. Bounded; entries expire by the caller's cache_ttl.
_SERIES_CACHE: dict[tuple, tuple[float, dict]] = {}
_SERIES_CACHE_CAP = 64


def fetch_precip_series(
    session: Any,
    lat: float,
    lon: float,
    *,
    weather_model: str = "",
    timeout: int = 10,
    logger: Any = None,
    cache_ttl: float = 0.0,
) -> Optional[dict]:
    """Fetch + normalize an Open-Meteo precipitation series using `session`.

    Prefers 15-minutely data; falls back to hourly when a model doesn't provide
    minutely_15. The caller owns the session's lifecycle. Returns a dict with keys
    times, precip, snow, prob, temp, codes, now, current_precip, current_code,
    step — or None on any error. Precipitation is requested in mm (detection is
    unit-independent); snowfall in cm; prob is precip probability (%); temp is
    2 m temperature (°C). When cache_ttl > 0, a fresh prior result for the same
    rounded location is reused.
    """
    cache_key = (round(lat, 2), round(lon, 2), weather_model)
    if cache_ttl > 0:
        hit = _SERIES_CACHE.get(cache_key)
        if hit is not None and (time.time() - hit[0]) < cache_ttl:
            return hit[1]

    api_url = "https://api.open-meteo.com/v1/forecast"
    variables = "precipitation,snowfall,weather_code,precipitation_probability,temperature_2m"
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "minutely_15": variables,
        "hourly": variables,
        "current": "precipitation,snowfall,weather_code,temperature_2m",
        "precipitation_unit": "mm",
        "timezone": "auto",
        "forecast_days": 2,  # cover the window even when "now" is late in the day
    }
    if weather_model:
        params["models"] = weather_model

    try:
        response = session.get(api_url, params=params, timeout=timeout)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if logger:
            logger.debug(f"Open-Meteo nowcast timeout/connection error: {e}")
        return None
    if not response.ok:
        if logger:
            logger.warning(f"Open-Meteo nowcast error: HTTP {response.status_code}")
        return None
    data = response.json()

    current = data.get("current", {}) or {}
    now = current.get("time")
    if not now:
        return None

    common = {
        "now": now,
        "current_precip": current.get("precipitation"),
        "current_code": current.get("weather_code"),
    }
    series: Optional[dict] = None
    m15 = data.get("minutely_15", {}) or {}
    m_times = m15.get("time") or []
    m_precip = m15.get("precipitation") or []
    if m_times and any(p is not None for p in m_precip):
        series = {
            "times": m_times, "precip": m_precip,
            "snow": m15.get("snowfall") or [],
            "prob": m15.get("precipitation_probability") or [],
            "temp": m15.get("temperature_2m") or [],
            "codes": m15.get("weather_code") or [], "step": 15, **common,
        }
    else:
        hourly = data.get("hourly", {}) or {}
        h_times = hourly.get("time") or []
        if not h_times:
            return None
        series = {
            "times": h_times, "precip": hourly.get("precipitation") or [],
            "snow": hourly.get("snowfall") or [],
            "prob": hourly.get("precipitation_probability") or [],
            "temp": hourly.get("temperature_2m") or [],
            "codes": hourly.get("weather_code") or [], "step": 60, **common,
        }

    if cache_ttl > 0:
        if len(_SERIES_CACHE) >= _SERIES_CACHE_CAP:
            _SERIES_CACHE.pop(next(iter(_SERIES_CACHE)))
        _SERIES_CACHE[cache_key] = (time.time(), series)
    return series


def nws_http_means_no_coverage(status_code: int) -> bool:
    """True when an NWS HTTP status means the point has no US weather.gov coverage."""
    return status_code in (400, 404)


# --- NWS gridpoint precip source ---------------------------------------------
# WHY THIS EXISTS: the Open-Meteo *forecast model* (fetch_precip_series, above)
# smooths away scattered, pop-up convection, so the nowcast can miss rain that is
# actually happening. Observed near Nashville (36.16, -86.78): Open-Meteo reported
# 0.00 in / ~12% precip across the next 3 h while NWS's own gridpoint showed
# 65-74% probability with measurable QPF — and thunderstorms were occurring. The
# model-based push therefore never fired. NWS's gridpoint forecast is
# forecaster-adjusted and does capture convective chances, so for US points we
# prefer it (fetch_precip_series_nws) and fall back to Open-Meteo only where NWS
# has no coverage (outside the US) or the request fails.

# NWS gridpoint "weather" type -> a representative WMO code, so precip_bucket_for_code()
# classifies the NWS series exactly like it classifies the Open-Meteo one.
_NWS_WEATHER_CODE = [
    ("thunderstorm", 95),
    ("snow", 73), ("blowing_snow", 73), ("snow_showers", 73),
    ("ice", 66), ("sleet", 66), ("freezing", 66), ("ice_pellets", 66),
    ("drizzle", 53),
    ("rain_showers", 81), ("showers", 81),
    ("rain", 63),
]


def _iso_duration_hours(dur: str) -> int:
    """Hours spanned by an ISO-8601 duration like 'PT6H', 'PT1H', 'P1DT6H' (min 1)."""
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", dur or "")
    if not m:
        return 1
    days, hours, mins = (int(g) if g else 0 for g in m.groups())
    return max(1, days * 24 + hours + (1 if mins else 0))


def _nws_hourly(values: Optional[list], *, divide: bool) -> dict:
    """Map hour-start (naive UTC datetime) -> value from an NWS gridpoint property.

    NWS reports each property as time-bucketed values whose validTime is an ISO
    interval like '2026-06-08T12:00:00+00:00/PT6H'. ``divide`` splits an
    accumulation (e.g. 6-hour QPF) evenly across its hours; otherwise the period's
    value is repeated for each hour (hourly PoP, the weather-type list).
    """
    out: dict = {}
    for v in values or []:
        try:
            start_s, _, dur = (v.get("validTime") or "").partition("/")
            start = datetime.fromisoformat(start_s).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, AttributeError):
            continue
        n = _iso_duration_hours(dur)
        raw = v.get("value")
        share = (raw / n) if (divide and raw is not None) else raw
        for k in range(n):
            out[start + timedelta(hours=k)] = share
    return out


def _nws_weather_code(value: Any) -> Optional[int]:
    """Pick a representative WMO code from an NWS gridpoint ``weather`` value (list of segments)."""
    if not value:
        return None
    blob = " ".join(
        str(seg.get("weather") or "") for seg in value if isinstance(seg, dict)
    ).lower()
    if not blob.strip():
        return None
    for needle, code in _NWS_WEATHER_CODE:
        if needle in blob or needle.replace("_", " ") in blob:
            return code
    return 63  # precip of unknown type -> rain


def fetch_precip_series_nws(
    session: Any,
    lat: float,
    lon: float,
    *,
    timeout: int = 10,
    logger: Any = None,
    pop_floor: int = 50,
    cache_ttl: float = 0.0,
) -> Optional[dict]:
    """Build a precip nowcast series from the NWS gridpoint forecast (US only).

    Returns the same shape as fetch_precip_series (times/precip/codes/now/
    current_precip/current_code/step), or None when NWS has no coverage (e.g.
    outside the US) so the caller can fall back to Open-Meteo.

    NWS exposes 6-hour QPF (mm) and hourly PoP (%). We build an hourly series in
    which each hour's precip is its QPF share, but zeroed when that hour's PoP is
    below ``pop_floor`` -- so the predicted rain-start tracks the hourly
    probability rather than snapping to coarse 6-hour QPF boundaries, and a trace
    of QPF at a low chance is not reported as rain. Times are naive UTC ISO strings
    (they only need to be self-consistent: the nowcast works on relative minutes).

    When cache_ttl > 0, a fresh prior result for the same rounded location is reused
    (shared bounded cache with fetch_precip_series).
    """
    cache_key = (round(lat, 2), round(lon, 2), "nws")
    if cache_ttl > 0:
        hit = _SERIES_CACHE.get(cache_key)
        if hit is not None and (time.time() - hit[0]) < cache_ttl:
            return hit[1]

    headers = {"User-Agent": "(meshcore-bot, weather-nowcast)", "Accept": "application/geo+json"}
    try:
        pts = session.get(
            f"https://api.weather.gov/points/{round(lat, 4)},{round(lon, 4)}",
            headers=headers, timeout=timeout,
        )
        if not pts.ok:
            return None  # no NWS coverage (outside the US) -> caller falls back to Open-Meteo
        grid_url = (pts.json().get("properties") or {}).get("forecastGridData")
        if not grid_url:
            return None
        gp = session.get(grid_url, headers=headers, timeout=timeout)
        if not gp.ok:
            return None
        props = gp.json().get("properties") or {}
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if logger:
            logger.debug(f"NWS nowcast timeout/connection error: {e}")
        return None
    except (ValueError, KeyError, TypeError) as e:
        if logger:
            logger.debug(f"NWS nowcast parse error: {e}")
        return None

    qpf = _nws_hourly((props.get("quantitativePrecipitation") or {}).get("values"), divide=True)
    pop = _nws_hourly((props.get("probabilityOfPrecipitation") or {}).get("values"), divide=False)
    wx = _nws_hourly((props.get("weather") or {}).get("values"), divide=False)
    if not qpf and not pop:
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = now.replace(minute=0, second=0, microsecond=0)
    hours = [base + timedelta(hours=i) for i in range(0, 6)]  # current hour + 5 ahead (covers the window)

    times: list[str] = []
    precip: list[Optional[float]] = []
    codes: list[Optional[int]] = []
    for h in hours:
        p = pop.get(h)
        q = qpf.get(h)
        # Count an hour as precipitating only when NWS gives a real chance; the
        # amount is its QPF share. (QPF is 6-hourly, PoP hourly -- PoP sets timing.)
        amt = q if (q is not None and p is not None and p >= pop_floor) else 0.0
        times.append(h.isoformat(timespec="minutes"))
        precip.append(amt)
        codes.append(_nws_weather_code(wx.get(h)) if amt else None)

    result = {
        "times": times,
        "precip": precip,
        "codes": codes,
        "now": now.isoformat(timespec="minutes"),
        "current_precip": precip[0] if precip else None,
        "current_code": codes[0] if codes else None,
        "step": 60,
    }
    if cache_ttl > 0:
        if len(_SERIES_CACHE) >= _SERIES_CACHE_CAP:
            _SERIES_CACHE.pop(next(iter(_SERIES_CACHE)))
        _SERIES_CACHE[cache_key] = (time.time(), result)
    return result


@dataclass
class NowcastResult:
    """Outcome of a precipitation nowcast analysis.

    state is one of:
      - "dry_clear":          dry now, no precip within the window
      - "dry_incoming":       dry now, precip starts in `minutes`
      - "raining_stopping":   precip now, drops below threshold in `minutes`
      - "raining_continuing": precip now, never clears within the window
    """

    state: str
    minutes: Optional[int] = None          # until start (dry_incoming) or stop (raining_stopping)
    duration_minutes: Optional[int] = None  # for dry_incoming: how long the precip lasts
    open_ended: bool = False                # precip extends past the analysis window
    bucket: Optional[str] = None            # precip bucket (drizzle/rain/snow/...) when raining/incoming
    amount_mm: Optional[float] = None       # estimated liquid precip total (mm) over the episode within the window
    snow_cm: Optional[float] = None         # estimated snowfall total (cm) over the episode (snow depth, not liquid)


def _round5(minutes: float) -> int:
    """Round to the nearest 5 minutes, with a floor of 5 for positive values."""
    r = int(round(minutes / 5.0) * 5)
    if minutes > 0 and r < 5:
        return 5
    return max(0, r)


def analyze_precip_nowcast(
    times: list[str],
    precip: list[Optional[float]],
    codes: list[Optional[int]],
    now_iso: str,
    *,
    window_minutes: int = 120,
    threshold: float = 0.1,
    current_precip: Optional[float] = None,
    current_code: Optional[int] = None,
    snow: Optional[list[Optional[float]]] = None,
    family: Optional[frozenset[str]] = None,
) -> Optional[NowcastResult]:
    """Pure nowcast analysis over a precipitation time series.

    All times are naive ISO-8601 local strings (e.g. "2026-06-03T14:15") in the
    same timezone as `now_iso`, so "now" is derived from the API rather than the
    host clock. Returns None when the series is too sparse to analyze.

    Args:
        times: Bucket start times (ascending), ISO local strings.
        precip: Precipitation amount per bucket, in mm. None is treated as 0.
        codes: WMO weather code per bucket (parallel to `times`); used for the
            precip type only.
        now_iso: Current time, ISO local string (from the API's current.time).
        window_minutes: How far ahead to look.
        threshold: mm-per-bucket at/above which a bucket counts as precipitating.
        current_precip: Optional instantaneous precip (API current.precipitation);
            preferred over the bucket value for the "raining right now" decision.
        current_code: Optional current WMO code, used for the precip type when
            raining now.
        snow: Optional snowfall per bucket, in cm (parallel to `times`). When a
            snow episode is reported, snow_cm gives the depth estimate (snowfall
            is the actual accumulation; precip is only its liquid equivalent).
        family: Optional set of bucket names (e.g. {"snow"}); a bucket then counts
            as precipitating only if its code maps into the family. None (default)
            counts any precip — used to answer "is *snow* coming?" vs "rain?".
    """
    if not times or not precip:
        return None

    def counts(amt: float, code: Optional[int]) -> bool:
        """Whether a bucket is precipitating for this query (>= threshold, and in
        the requested precip family when one is given)."""
        if amt < threshold:
            return False
        return family is None or precip_bucket_for_code(code) in family
    n = min(len(times), len(precip))
    try:
        now = datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        return None

    parsed: list[tuple[datetime, float, Optional[int], float]] = []
    for i in range(n):
        try:
            t = datetime.fromisoformat(times[i])
        except (TypeError, ValueError):
            continue
        amt = precip[i]
        amt = 0.0 if amt is None else float(amt)
        code = codes[i] if i < len(codes) else None
        sf = snow[i] if (snow is not None and i < len(snow)) else None
        sf = 0.0 if sf is None else float(sf)
        parsed.append((t, amt, code, sf))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])

    # Index of the bucket containing "now" (largest start time <= now).
    cur_idx = -1
    for i, (t, _amt, _c, _sf) in enumerate(parsed):
        if t <= now:
            cur_idx = i
        else:
            break

    # Upcoming buckets strictly after now, within the window.
    upcoming: list[tuple[float, float, Optional[int], float]] = []  # (mins, precip_mm, code, snow_cm)
    for t, amt, code, sf in parsed[cur_idx + 1:]:
        mins = (t - now).total_seconds() / 60.0
        if mins <= 0:
            continue
        if mins > window_minutes:
            break
        upcoming.append((mins, amt, code, sf))

    # "Precipitating now?" (of the requested family) — prefer the API's
    # instantaneous value + current code, else the current bucket.
    now_code = current_code if current_code is not None else (parsed[cur_idx][2] if cur_idx >= 0 else None)
    if current_precip is not None:
        raining_now = counts(float(current_precip), now_code)
    elif cur_idx >= 0:
        raining_now = counts(parsed[cur_idx][1], parsed[cur_idx][2])
    else:
        raining_now = False

    if raining_now:
        bucket = precip_bucket_for_code(now_code) or "rain"
        # Accumulate the episode totals: liquid (mm) and snowfall (cm), the current
        # bucket plus each upcoming bucket until precip drops below threshold.
        total = max(0.0, parsed[cur_idx][1]) if cur_idx >= 0 else 0.0
        total_snow = max(0.0, parsed[cur_idx][3]) if cur_idx >= 0 else 0.0
        for mins, amt, code, sf in upcoming:
            if not counts(amt, code):
                return NowcastResult(
                    state="raining_stopping", minutes=_round5(mins), bucket=bucket,
                    amount_mm=total, snow_cm=total_snow,
                )
            total += amt
            total_snow += sf
        return NowcastResult(
            state="raining_continuing", open_ended=True, bucket=bucket,
            amount_mm=total, snow_cm=total_snow,
        )

    # Dry now (of the requested family): find the first upcoming precip bucket.
    for idx, (mins, amt, code, sf) in enumerate(upcoming):
        if counts(amt, code):
            bucket = precip_bucket_for_code(code) or "rain"
            # How long does it last, and how much falls? Walk until it drops below
            # threshold, summing liquid (mm) and snowfall (cm) for the estimate.
            total = amt
            total_snow = sf
            end_mins: Optional[float] = None
            for mins2, amt2, code2, sf2 in upcoming[idx + 1:]:
                if not counts(amt2, code2):
                    end_mins = mins2
                    break
                total += amt2
                total_snow += sf2
            if end_mins is None:
                return NowcastResult(
                    state="dry_incoming", minutes=_round5(mins), open_ended=True,
                    bucket=bucket, amount_mm=total, snow_cm=total_snow,
                )
            return NowcastResult(
                state="dry_incoming",
                minutes=_round5(mins),
                duration_minutes=_round5(end_mins - mins),
                bucket=bucket,
                amount_mm=total,
                snow_cm=total_snow,
            )

    return NowcastResult(state="dry_clear")


def decide_rain_notification(
    state: str,
    minutes: Optional[int],
    *,
    lead_minutes: int,
    start_announced: bool,
    end_announced: bool,
    seconds_since_last_start: Optional[float] = None,
    seconds_since_last_end: Optional[float] = None,
    renotify_minutes: int,
    announce_ending: bool = True,
) -> tuple[Optional[str], bool, bool]:
    """Decide whether to push a proactive rain notice, and which kind.

    A small state machine that keeps the Weather_Service from spamming a channel
    every poll. Returns ``(kind, start_announced, end_announced)`` where ``kind``
    is ``None``, ``"starting"`` (rain about to begin), or ``"ending"`` (rain about
    to stop); the two booleans are the caller's updated per-episode flags.

      - ``dry_clear`` ends the episode and re-arms both notices.
      - ``dry_incoming`` fires ``"starting"`` once when precip enters the
        ``lead_minutes`` window.
      - ``raining_stopping`` fires ``"ending"`` once when the clear-up enters the
        window (unless ``announce_ending`` is False).
      - Each notice fires at most once per episode; a ``renotify_minutes`` cooldown
        (tracked separately per kind) absorbs forecast flapping.
    """
    if state == "dry_clear":
        return (None, False, False)

    if state == "dry_incoming":
        if minutes is None or minutes > lead_minutes:
            return (None, start_announced, end_announced)  # coming, but not yet within lead
        if start_announced:
            return (None, True, end_announced)
        if seconds_since_last_start is not None and seconds_since_last_start < renotify_minutes * 60:
            return (None, start_announced, end_announced)  # cooldown: hold off, stay re-armed
        return ("starting", True, end_announced)

    # Raining now: the "starting" moment has passed (or was missed) — mark it so a
    # late "starting" never fires.
    if state == "raining_continuing":
        return (None, True, end_announced)

    if state == "raining_stopping":
        if not announce_ending or minutes is None or minutes > lead_minutes:
            return (None, True, end_announced)
        if end_announced:
            return (None, True, True)
        if seconds_since_last_end is not None and seconds_since_last_end < renotify_minutes * 60:
            return (None, True, end_announced)
        return ("ending", True, True)

    return (None, start_announced, end_announced)


class RainCommand(BaseCommand):
    """Minute-level rain nowcast for a location (Open-Meteo 15-minutely precip)."""

    name = "rain"
    keywords = ["rain", "nowcast", "snow"]
    description = "Rain/snow nowcast: when precip starts or stops in the next ~2h, with amount"
    category = "weather"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "Rain/snow nowcast (when precip starts/stops) for a location"
    usage = "rain|snow [city|zipcode|lat,lon]"
    examples = ["rain", "snow", "rain seattle", "snow 98101", "rain 47.6,-122.3"]
    parameters = [
        {"name": "location", "description": "Optional: city, US ZIP, or lat,lon. Default: companion or bot location."}
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.rain_enabled = self.get_config_value("Rain_Command", "enabled", fallback=True, value_type="bool")
        self.default_state = self.bot.config.get("Weather", "default_state", fallback="")
        self.default_country = self.bot.config.get("Weather", "default_country", fallback="US")
        self.weather_model = self.bot.config.get("Weather", "weather_model", fallback="").strip()
        self.url_timeout = 10
        self.window_minutes = self.get_config_value(
            "Rain_Command", "window_minutes", fallback=120, value_type="int"
        )
        # mm-per-15min at/above which a bucket counts as precipitating.
        self.threshold_mm = self.get_config_value(
            "Rain_Command", "precip_threshold_mm", fallback=0.1, value_type="float"
        )
        # Optional precip-amount estimate appended to the nowcast line, e.g.
        # "(est 0.2 in)". Unit "in" (US default) or "mm"; show_amount toggles it.
        self.show_amount = self.get_config_value(
            "Rain_Command", "show_amount", fallback=True, value_type="bool"
        )
        self.amount_unit = self.bot.config.get(
            "Rain_Command", "amount_unit", fallback="in"
        ).strip().lower()
        # Show precip probability "(…, 70%)" and a borderline-temperature tag
        # "34°F" (only when ~30-38°F, where rain/snow/ice is in doubt).
        self.show_probability = self.get_config_value(
            "Rain_Command", "show_probability", fallback=True, value_type="bool"
        )
        self.show_temp = self.get_config_value(
            "Rain_Command", "show_temp", fallback=True, value_type="bool"
        )
        # Reuse a fetched series for this many seconds (shared with the proactive
        # poll); 0 disables. Short so the nowcast's "now" stays fresh.
        self.cache_ttl = self.get_config_value(
            "Rain_Command", "cache_seconds", fallback=300, value_type="int"
        )
        # Display names. The bot's own location prefers [Weather] default_city +
        # default_state; other coordinates are reverse-geocoded (state for US,
        # country otherwise). Results cached.
        self.default_city = self.bot.config.get("Weather", "default_city", fallback="").strip()
        # US ZIP -> city via Zippopotam.us. Opt-out for anyone who'd rather not
        # add the external lookup; falls back to reverse geocoding when disabled.
        self.zip_city_lookup = self.get_config_value(
            "Rain_Command", "zip_city_lookup", fallback=True, value_type="bool"
        )
        self._reverse_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}
        self._zip_cache: dict[str, str] = {}

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.rain_enabled:
            return False
        return super().can_execute(message)

    def _create_retry_session(self) -> requests.Session:
        """Session with light retry/backoff for the Open-Meteo call."""
        session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_companion_location(self, message: MeshMessage) -> Optional[tuple[float, float]]:
        """Get companion/sender location from the contact-tracking database."""
        try:
            sender_pubkey = getattr(message, "sender_pubkey", None)
            if not sender_pubkey:
                return None
            query = """
                SELECT latitude, longitude
                FROM complete_contact_tracking
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            """
            results = self.bot.db_manager.execute_query(query, (sender_pubkey,))
            if results:
                row = results[0]
                return (float(row["latitude"]), float(row["longitude"]))
            return None
        except Exception as e:
            self.logger.debug(f"Error getting companion location: {e}")
            return None

    def _get_bot_location(self) -> Optional[tuple[float, float]]:
        """Get bot location from config ([Bot] bot_latitude, bot_longitude)."""
        try:
            lat = self.bot.config.getfloat("Bot", "bot_latitude", fallback=None)
            lon = self.bot.config.getfloat("Bot", "bot_longitude", fallback=None)
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
            return None
        except Exception as e:
            self.logger.debug(f"Error getting bot location: {e}")
            return None

    def _reverse_geocode(self, lat: float, lon: float) -> tuple[Optional[str], Optional[str]]:
        """Reverse-geocode to (city, suffix), cached. suffix is the US state
        abbreviation ('TN') for US points, else the country name ('Colombia')."""
        key = f"{lat:.3f},{lon:.3f}"
        if key in self._reverse_cache:
            return self._reverse_cache[key]
        city, suffix = reverse_geocode_region(self.bot, lat, lon, timeout=self.url_timeout, logger=self.logger)
        if city or suffix:
            _cache_put(self._reverse_cache, key, (city, suffix))
        return city, suffix

    def _coordinates_to_location_string(self, lat: float, lon: float) -> Optional[str]:
        """'City, ST' (US) or 'City, Country' (non-US) from reverse geocoding."""
        city, suffix = self._reverse_geocode(lat, lon)
        if not city:
            return None
        return join_location(city, suffix)

    def _suffix_for_coords(self, lat: float, lon: float) -> Optional[str]:
        """US state abbreviation or country name for coordinates (enriches a known city)."""
        return self._reverse_geocode(lat, lon)[1]

    def _zip_to_city_string(self, zipcode: str) -> Optional[str]:
        """US ZIP -> 'City, ST' via Zippopotam.us (free, no key, cached).

        OSM/Nominatim often lacks the USPS city for a ZIP centroid (returns the
        county instead), so for 5-digit US ZIPs this gives a far better name.
        Returns None on failure (caller falls back to reverse geocoding).
        """
        z = zipcode.strip()
        if z in self._zip_cache:
            return self._zip_cache[z]
        name: Optional[str] = None
        try:
            resp = requests.get(f"https://api.zippopotam.us/us/{z}", timeout=self.url_timeout)
            if resp.ok:
                places = resp.json().get("places") or []
                if places:
                    city = (places[0].get("place name") or "").strip()
                    st = (places[0].get("state abbreviation") or "").strip()
                    if city:
                        name = join_location(city, st)
        except Exception as e:
            self.logger.debug(f"Zippopotam ZIP lookup failed for {z}: {e}")
        if name:
            _cache_put(self._zip_cache, z, name)
        return name

    def _resolve_location(
        self, message: MeshMessage, location: Optional[str]
    ) -> tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
        """Resolve to (lat, lon, location_label, error_key).

        Mirrors the aurora command: no input falls back to companion location,
        then a [Rain_Command] default, then the bot location. Coordinate-based
        labels are reverse-geocoded to a city name for display.
        """
        if not location or not location.strip():
            co = self._get_companion_location(message)
            if co:
                label = self._coordinates_to_location_string(co[0], co[1]) or f"{co[0]:.1f},{co[1]:.1f}"
                return (co[0], co[1], label, None)
            default_lat = default_lon = None
            if self.bot.config.has_section("Rain_Command"):
                default_lat = self.bot.config.getfloat("Rain_Command", "default_lat", fallback=None)
                default_lon = self.bot.config.getfloat("Rain_Command", "default_lon", fallback=None)
            if default_lat is not None and default_lon is not None:
                if -90 <= default_lat <= 90 and -180 <= default_lon <= 180:
                    label = self._coordinates_to_location_string(default_lat, default_lon) or f"{default_lat:.1f},{default_lon:.1f}"
                    return (default_lat, default_lon, label, None)
            bot_loc = self._get_bot_location()
            if bot_loc:
                # Prefer the configured default city + state for the bot's own location.
                if self.default_city:
                    suffix = self.default_state or self._suffix_for_coords(bot_loc[0], bot_loc[1])
                    label = join_location(self.default_city, suffix)
                else:
                    label = self._coordinates_to_location_string(bot_loc[0], bot_loc[1]) or f"{bot_loc[0]:.1f},{bot_loc[1]:.1f}"
                return (bot_loc[0], bot_loc[1], label, None)
            return (None, None, None, "commands.rain.no_location")

        loc = location.strip()
        # Declared Optional up front: the coordinate branch assigns floats while
        # the ZIP/city geocoders return Optional[float]; each is narrowed before use.
        lat: Optional[float]
        lon: Optional[float]

        # Coordinates "lat,lon"
        if re.match(r"^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$", loc):
            try:
                a, b = loc.split(",", 1)
                lat, lon = float(a.strip()), float(b.strip())
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    return (None, None, None, "commands.rain.error")
                return (lat, lon, self._coordinates_to_location_string(lat, lon) or loc, None)
            except ValueError:
                return (None, None, None, "commands.rain.error")

        # US ZIP (5 digits)
        if re.match(r"^\s*\d{5}\s*$", loc):
            lat, lon = geocode_zipcode_sync(
                self.bot, loc, default_country=self.default_country, timeout=self.url_timeout
            )
            if lat is None or lon is None:
                return (None, None, None, "commands.rain.no_location_zipcode")
            # Name the ZIP "City, ST (zip)": Zippopotam first (reliable USPS city),
            # then reverse geocoding, else just the ZIP.
            zip_city = self._zip_to_city_string(loc) if self.zip_city_lookup else None
            city = zip_city or self._coordinates_to_location_string(lat, lon)
            label = f"{city} ({loc})" if city else loc
            return (lat, lon, label, None)

        # City
        lat, lon, _ = geocode_city_sync(
            self.bot,
            loc,
            default_state=self.default_state,
            default_country=self.default_country,
            include_address_info=False,
            timeout=self.url_timeout,
        )
        if lat is None or lon is None:
            return (None, None, None, "commands.rain.no_location_city")
        # Keep the typed city name (more accurate than reverse geocoding for small
        # towns), but append the state (US) or country (non-US) from the geocoder
        # — stripping any region the user already typed so it isn't doubled.
        suffix = self._suffix_for_coords(lat, lon)
        typed_city = city_display_name(loc, suffix)
        label = join_location(typed_city, suffix)
        return (lat, lon, label, None)

    def _fetch_series(self, lat: float, lon: float) -> Optional[dict]:
        """Fetch the precip series (own short-lived session).

        Prefers the NWS gridpoint (US) so a "!rain"/"!snow" matches the proactive
        push and reflects the forecaster-adjusted convective chances the Open-Meteo
        model can miss; falls back to Open-Meteo for non-US locations (no NWS
        coverage) or on any failure.
        """
        session = self._create_retry_session()
        try:
            series = fetch_precip_series_nws(
                session, lat, lon, timeout=self.url_timeout, logger=self.logger,
            )
            if series:
                return series
            return fetch_precip_series(
                session, lat, lon,
                weather_model=self.weather_model, timeout=self.url_timeout, logger=self.logger,
                cache_ttl=self.cache_ttl,
            )
        finally:
            session.close()

    def _window_label(self) -> str:
        """Human window length, e.g. '2h' or '90min'."""
        if self.window_minutes % 60 == 0:
            return f"{self.window_minutes // 60}h"
        return f"{self.window_minutes}min"

    def _ptype(self, bucket: Optional[str]) -> str:
        """Translatable precip-type label for a bucket."""
        b = bucket or "rain"
        return self.translate(f"commands.rain.precip_types.{b}")

    def _detail_suffix(self, result: NowcastResult, prob: Optional[int], temp_f: Optional[int]) -> str:
        """Trailing detail: ' (est 0.2 in, 70%) 34°F' — amount + probability in the
        parens, plus a temperature tag only when borderline (~30-38°F)."""
        parts: list[str] = []
        if self.show_amount:
            amt = format_amount_estimate(result.bucket, result.amount_mm, result.snow_cm, self.amount_unit)
            if amt:
                parts.append(f"est {amt}")
        if self.show_probability and prob is not None:
            parts.append(f"{prob}%")
        paren = f" ({', '.join(parts)})" if parts else ""
        temp = f" {temp_f}°F" if (self.show_temp and temp_f is not None and 30 <= temp_f <= 38) else ""
        return paren + temp

    def _format_result(
        self, result: NowcastResult, location_label: str,
        *, asked_word: Optional[str] = None, mismatch: bool = False,
        prob: Optional[int] = None, temp_f: Optional[int] = None,
    ) -> str:
        """Render a NowcastResult into a single mesh-friendly line.

        ``asked_word`` is the precip the user asked for ("rain"/"snow"); when
        ``mismatch`` is set the result is the *other* type, rendered as
        "No <asked>, but <actual> ..." so a !snow that finds rain still helps.
        ``prob``/``temp_f`` add a probability and borderline-temperature tag.
        """
        emoji = _BUCKET_EMOJI.get(result.bucket or "rain", "🌧️")
        if result.state == "dry_clear":
            return self.translate(
                "commands.rain.clear", precip=asked_word or "rain",
                window=self._window_label(), location=location_label,
            )
        if mismatch and asked_word:
            ptype = f"No {asked_word}, but {self._ptype(result.bucket).lower()}"
        else:
            ptype = self._ptype(result.bucket)
        if result.state == "dry_incoming":
            if result.open_ended or not result.duration_minutes:
                extra = self.translate("commands.rain.duration_open")
            else:
                extra = self.translate("commands.rain.duration_for", duration=result.duration_minutes)
            return self.translate(
                "commands.rain.starting",
                emoji=emoji, ptype=ptype, minutes=result.minutes,
                location=location_label, extra=extra,
            ) + self._detail_suffix(result, prob, temp_f)
        if result.state == "raining_stopping":
            return self.translate(
                "commands.rain.stopping",
                emoji=emoji, ptype=ptype, minutes=result.minutes, location=location_label,
            ) + self._detail_suffix(result, prob, temp_f)
        # raining_continuing
        return self.translate(
            "commands.rain.continuing",
            emoji=emoji, ptype=ptype, window=self._window_label(), location=location_label,
        ) + self._detail_suffix(result, prob, temp_f)

    def _format_changeover(self, rain_r: NowcastResult, snow_r: NowcastResult, location_label: str) -> str:
        """A rain<->snow transition line, e.g. '🌧️→🌨️ Rain now → snow in ~60min
        for X' — whichever type comes first leads."""
        def start(r: NowcastResult) -> int:
            return 0 if r.state in ("raining_stopping", "raining_continuing") else (r.minutes or 0)
        first_r, second_r = (rain_r, snow_r) if start(rain_r) <= start(snow_r) else (snow_r, rain_r)
        when = self.translate("commands.rain.now") if start(first_r) == 0 else f"in ~{start(first_r)}min"
        return self.translate(
            "commands.rain.changeover",
            from_emoji=_BUCKET_EMOJI.get(first_r.bucket or "rain", "🌧️"),
            to_emoji=_BUCKET_EMOJI.get(second_r.bucket or "snow", "🌨️"),
            first=self._ptype(first_r.bucket), when=when,
            second=self._ptype(second_r.bucket).lower(),
            minutes=start(second_r), location=location_label,
        )

    def get_help_text(self, message: Any = None) -> str:
        """Help tailored to the keyword asked about: 'help snow' talks snow
        (depth), 'help rain'/'help nowcast' talk rain (amount). Falls back to
        the rain variant when the queried word can't be determined."""
        word = "rain"
        content = (getattr(message, "content", "") or "").strip()
        if content.startswith("!"):
            content = content[1:].strip()
        parts = content.split()
        if len(parts) >= 2:
            word = parts[1].lower()
        key = "commands.rain.help_snow" if word == "snow" else "commands.rain.help_rain"
        return self.translate(key)

    async def execute(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if content.startswith("!"):
            content = content[1:].strip()
        parts = content.split()
        location: Optional[str] = " ".join(parts[1:]).strip() if len(parts) >= 2 else None

        # Which keyword triggered us sets the precip we're asked about. !snow leads
        # with snow, !rain with rain; !nowcast (or anything else) has no preference.
        mode = parts[0].lower() if parts else "rain"
        asked_word: Optional[str] = "snow" if mode == "snow" else (None if mode == "nowcast" else "rain")

        # Bare country/US state (e.g. "france", "texas") -> default to its capital
        # and append a heads-up, since one centroid point isn't representative.
        region_note: Optional[str] = None
        cap_query = region_capital_query(location)
        if cap_query:
            location = cap_query
            region_note = REGION_DEFAULT_NOTE

        lat, lon, location_label, err_key = self._resolve_location(message, location)
        if lat is None or lon is None:
            region = self.default_state or self.default_country
            if err_key == "commands.rain.no_location":
                await self.send_response(message, self.translate("commands.rain.no_location"))
            elif err_key == "commands.rain.no_location_zipcode":
                await self.send_response(
                    message, self.translate("commands.rain.no_location_zipcode", location=location or "")
                )
            elif err_key == "commands.rain.no_location_city":
                await self.send_response(
                    message,
                    self.translate("commands.rain.no_location_city", location=location or "", state=region),
                )
            else:
                await self.send_response(
                    message, self.translate("commands.rain.error", error="Invalid location or coordinates")
                )
            return True

        try:
            self.record_execution(message.sender_id)
            loop = asyncio.get_event_loop()
            series = await loop.run_in_executor(None, lambda: self._fetch_series(lat, lon))
        except Exception as e:
            self.logger.error(f"Error fetching rain nowcast: {e}")
            await self.send_response(message, self.translate("commands.rain.error_fetching"))
            return True

        if not series:
            await self.send_response(message, self.translate("commands.rain.error_fetching"))
            return True

        def run(fam: Optional[frozenset[str]]) -> Optional[NowcastResult]:
            return analyze_precip_nowcast(
                series["times"], series["precip"], series["codes"], series["now"],
                window_minutes=self.window_minutes, threshold=self.threshold_mm,
                current_precip=series.get("current_precip"), current_code=series.get("current_code"),
                snow=series.get("snow"), family=fam,
            )

        # Analyze each precip family. Both present -> a changeover line; otherwise
        # the asked-for type, falling back to the other with a "No <asked>, but …".
        rain_r = run(RAIN_FAMILY)
        snow_r = run(SNOW_FAMILY)
        if rain_r is None or snow_r is None:
            await self.send_response(message, self.translate("commands.rain.error_fetching"))
            return True
        rain_ok = rain_r.state != "dry_clear"
        snow_ok = snow_r.state != "dry_clear"
        label = location_label or f"{lat:.1f},{lon:.1f}"

        if rain_ok and snow_ok:
            response = self._format_changeover(rain_r, snow_r, label)
        else:
            if asked_word == "snow":
                result, mismatch = (snow_r, False) if snow_ok else ((rain_r, True) if rain_ok else (snow_r, False))
            elif asked_word == "rain":
                result, mismatch = (rain_r, False) if rain_ok else ((snow_r, True) if snow_ok else (rain_r, False))
            else:  # nowcast: no type preference
                result, mismatch = (rain_r if rain_ok else snow_r), False
            prob, temp_f = episode_probability_temp(series, result)
            response = self._format_result(
                result, label, asked_word=asked_word, mismatch=mismatch, prob=prob, temp_f=temp_f,
            )
        if region_note:
            response = f"{response} {region_note}"
        max_len = self.get_max_message_length(message)
        if len(response) > max_len:
            response = response[: max_len - 3] + "..."
        await self.send_response(message, response)
        return True
