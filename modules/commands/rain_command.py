#!/usr/bin/env python3
"""
Rain nowcast command - minute-level "rain starting/stopping in ~N min" using
Open-Meteo's 15-minutely precipitation forecast. Works worldwide, no API key.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import MeshMessage
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


def fetch_precip_series(
    session: Any,
    lat: float,
    lon: float,
    *,
    weather_model: str = "",
    timeout: int = 10,
    logger: Any = None,
) -> Optional[dict]:
    """Fetch + normalize an Open-Meteo precipitation series using `session`.

    Prefers 15-minutely data; falls back to hourly when a model doesn't provide
    minutely_15. The caller owns the session's lifecycle. Returns a dict with
    keys times, precip, codes, now, current_precip, current_code, step — or None
    on any error. Precipitation is requested in mm (detection is unit-independent).
    """
    api_url = "https://api.open-meteo.com/v1/forecast"
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "minutely_15": "precipitation,weather_code",
        "hourly": "precipitation,weather_code",
        "current": "precipitation,weather_code",
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

    m15 = data.get("minutely_15", {}) or {}
    m_times = m15.get("time") or []
    m_precip = m15.get("precipitation") or []
    if m_times and any(p is not None for p in m_precip):
        return {
            "times": m_times,
            "precip": m_precip,
            "codes": m15.get("weather_code") or [],
            "now": now,
            "current_precip": current.get("precipitation"),
            "current_code": current.get("weather_code"),
            "step": 15,
        }

    hourly = data.get("hourly", {}) or {}
    h_times = hourly.get("time") or []
    if not h_times:
        return None
    return {
        "times": h_times,
        "precip": hourly.get("precipitation") or [],
        "codes": hourly.get("weather_code") or [],
        "now": now,
        "current_precip": current.get("precipitation"),
        "current_code": current.get("weather_code"),
        "step": 60,
    }


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
    """
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

    return {
        "times": times,
        "precip": precip,
        "codes": codes,
        "now": now.isoformat(timespec="minutes"),
        "current_precip": precip[0] if precip else None,
        "current_code": codes[0] if codes else None,
        "step": 60,
    }


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
    """
    if not times or not precip:
        return None
    n = min(len(times), len(precip))
    try:
        now = datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        return None

    parsed: list[tuple[datetime, float, Optional[int]]] = []
    for i in range(n):
        try:
            t = datetime.fromisoformat(times[i])
        except (TypeError, ValueError):
            continue
        amt = precip[i]
        amt = 0.0 if amt is None else float(amt)
        code = codes[i] if i < len(codes) else None
        parsed.append((t, amt, code))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])

    # Index of the bucket containing "now" (largest start time <= now).
    cur_idx = -1
    for i, (t, _amt, _c) in enumerate(parsed):
        if t <= now:
            cur_idx = i
        else:
            break

    # Upcoming buckets strictly after now, within the window.
    upcoming: list[tuple[float, float, Optional[int]]] = []  # (minutes_from_now, amount, code)
    for t, amt, code in parsed[cur_idx + 1:]:
        mins = (t - now).total_seconds() / 60.0
        if mins <= 0:
            continue
        if mins > window_minutes:
            break
        upcoming.append((mins, amt, code))

    # "Raining now?" — prefer the API's instantaneous value, else the current bucket.
    if current_precip is not None:
        raining_now = float(current_precip) >= threshold
    elif cur_idx >= 0:
        raining_now = parsed[cur_idx][1] >= threshold
    else:
        raining_now = False

    if raining_now:
        now_code = current_code if current_code is not None else (parsed[cur_idx][2] if cur_idx >= 0 else None)
        bucket = precip_bucket_for_code(now_code) or "rain"
        for mins, amt, _code in upcoming:
            if amt < threshold:
                return NowcastResult(state="raining_stopping", minutes=_round5(mins), bucket=bucket)
        return NowcastResult(state="raining_continuing", open_ended=True, bucket=bucket)

    # Dry now: find the first upcoming precipitating bucket.
    for idx, (mins, amt, code) in enumerate(upcoming):
        if amt >= threshold:
            bucket = precip_bucket_for_code(code) or "rain"
            # How long does it last? Walk until it drops below threshold.
            end_mins: Optional[float] = None
            for mins2, amt2, _c2 in upcoming[idx + 1:]:
                if amt2 < threshold:
                    end_mins = mins2
                    break
            if end_mins is None:
                return NowcastResult(
                    state="dry_incoming", minutes=_round5(mins), open_ended=True, bucket=bucket
                )
            return NowcastResult(
                state="dry_incoming",
                minutes=_round5(mins),
                duration_minutes=_round5(end_mins - mins),
                bucket=bucket,
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
    keywords = ["rain", "nowcast"]
    description = "Rain nowcast: when precipitation starts or stops in the next couple hours"
    category = "weather"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "Rain nowcast (when rain starts/stops) for a location"
    usage = "rain [city|zipcode|lat,lon]"
    examples = ["rain", "rain seattle", "rain 98101", "rain 47.6,-122.3"]
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
        return f"{city}, {suffix}" if suffix else city

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
                        name = f"{city}, {st}" if st else city
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
                    label = f"{self.default_city}, {suffix}" if suffix else self.default_city
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
        label = f"{typed_city}, {suffix}" if suffix else typed_city
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

    def _format_result(self, result: NowcastResult, location_label: str) -> str:
        """Render a NowcastResult into a single mesh-friendly line."""
        emoji = _BUCKET_EMOJI.get(result.bucket or "rain", "🌧️")
        if result.state == "dry_clear":
            return self.translate(
                "commands.rain.clear", window=self._window_label(), location=location_label
            )
        if result.state == "dry_incoming":
            if result.open_ended or not result.duration_minutes:
                extra = self.translate("commands.rain.duration_open")
            else:
                extra = self.translate("commands.rain.duration_for", duration=result.duration_minutes)
            return self.translate(
                "commands.rain.starting",
                emoji=emoji,
                ptype=self._ptype(result.bucket),
                minutes=result.minutes,
                location=location_label,
                extra=extra,
            )
        if result.state == "raining_stopping":
            return self.translate(
                "commands.rain.stopping",
                emoji=emoji,
                ptype=self._ptype(result.bucket),
                minutes=result.minutes,
                location=location_label,
            )
        # raining_continuing
        return self.translate(
            "commands.rain.continuing",
            emoji=emoji,
            ptype=self._ptype(result.bucket),
            window=self._window_label(),
            location=location_label,
        )

    async def execute(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if content.startswith("!"):
            content = content[1:].strip()
        parts = content.split()
        location: Optional[str] = " ".join(parts[1:]).strip() if len(parts) >= 2 else None

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

        result = analyze_precip_nowcast(
            series["times"],
            series["precip"],
            series["codes"],
            series["now"],
            window_minutes=self.window_minutes,
            threshold=self.threshold_mm,
            current_precip=series.get("current_precip"),
            current_code=series.get("current_code"),
        )
        if result is None:
            await self.send_response(message, self.translate("commands.rain.error_fetching"))
            return True

        response = self._format_result(result, location_label or f"{lat:.1f},{lon:.1f}")
        max_len = self.get_max_message_length(message)
        if len(response) > max_len:
            response = response[: max_len - 3] + "..."
        await self.send_response(message, response)
        return True
