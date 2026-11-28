#!/usr/bin/env python3
"""MET Norway Locationforecast 2.0: the worldwide provider behind ``metno``.

The API answers one GeoJSON document per point: ``properties.timeseries`` is a
list of steps, hourly for about two and a half days and every six hours after
that, out to about nine days. Each step carries the conditions at its own time
(``data.instant.details``) and the symbol and precipitation for the hour or the
six hours that follow it.

The reply is built to match Open-Meteo's line for line, so a bot that switches
providers keeps the same shape: the same period labels, the same high/low
templates, the same hourly packing and the same byte budget. The data is
thinner, so a few details are simply absent: there is no apparent temperature,
no dew point, no visibility and no gust, and precipitation comes as an amount
rather than a probability.

Terms of service, all of which this module honors: an identifying User-Agent
with contact information (``[Weather] metno_user_agent`` overrides it),
coordinates rounded to four decimals, ``Expires`` respected with
``If-Modified-Since`` on the next request, and a pause after a 429.

The forecast carries no timezones, so the local offset comes from the point's
IANA zone through :mod:`._tz` (real zones on 64-bit installs, the longitude
estimate on armv7). It is used only to label hours and to split days.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from ..http_retry import make_retry_session
from ..version_info import resolve_application_version
from ..weather_common import _ARROWS_8, _COMPASS_16
from ._tz import estimate_offset_hours, utc_offset_at
from ._units import c_to_f, hpa_to_mmhg, mm_to_inch, ms_to, round_half_away
from .base import Capability, ForecastReply, ProviderHost, WeatherProvider, WeatherUnits

METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# The API answers from a CDN in about a second; a short read timeout plus the
# session's retries gets past a stall without holding a worker for long.
METNO_TIMEOUT = (5, 6)  # (connect, read) seconds, per attempt
USER_AGENT_CONTACT = "(+https://github.com/agessaman/meshcore-bot)"

# Expires is normally half an hour out. A missing or unusable header falls back
# to that, and the value is clamped so a bad header cannot pin a stale document.
DEFAULT_TTL = 30 * 60
MIN_TTL = 60
MAX_TTL = 6 * 60 * 60
# A failed refresh may answer from the last document, but only while it is still
# a forecast: past this age the reply is an error instead of yesterday's weather.
STALE_MAX_SECONDS = 6 * 60 * 60
# A 429 without Retry-After parks the provider for this long.
RATE_LIMIT_PAUSE = 300.0


# MET Norway's symbol codes, without the _day/_night/_polartwilight suffix,
# mapped onto the WMO codes the shared catalog already describes and draws. One
# map serves the description, the emoji and the extreme-conditions check.
SYMBOL_TO_WMO = {
    "clearsky": 0,
    "fair": 1,
    "partlycloudy": 2,
    "cloudy": 3,
    "fog": 45,
    "lightrain": 61,
    "rain": 63,
    "heavyrain": 65,
    "lightrainshowers": 80,
    "rainshowers": 81,
    "heavyrainshowers": 82,
    "lightsleet": 66,
    "sleet": 66,
    "heavysleet": 67,
    "lightsleetshowers": 66,
    "sleetshowers": 66,
    "heavysleetshowers": 67,
    "lightsnow": 71,
    "snow": 73,
    "heavysnow": 75,
    "lightsnowshowers": 85,
    "snowshowers": 86,
    "heavysnowshowers": 86,
    "lightrainandthunder": 95,
    "rainandthunder": 95,
    "heavyrainandthunder": 95,
    "lightrainshowersandthunder": 95,
    "rainshowersandthunder": 95,
    "heavyrainshowersandthunder": 95,
    "lightsleetandthunder": 95,
    "sleetandthunder": 95,
    "heavysleetandthunder": 95,
    "lightsleetshowersandthunder": 95,
    "sleetshowersandthunder": 95,
    "heavysleetshowersandthunder": 95,
    "lightsnowandthunder": 95,
    "snowandthunder": 95,
    "heavysnowandthunder": 95,
    "lightsnowshowersandthunder": 95,
    "snowshowersandthunder": 95,
    "heavysnowshowersandthunder": 95,
}

# The fallback descriptions, used only when the catalog has no entry for a code.
WMO_DESCRIPTIONS = {
    0: "Clear",
    1: "Mostly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Foggy",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    66: "Light Freezing Rain",
    67: "Freezing Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    80: "Light Showers",
    81: "Showers",
    82: "Heavy Showers",
    85: "Light Snow Showers",
    86: "Snow Showers",
    95: "Thunderstorm",
}


def local_offset_hours(lon: float) -> int:
    """The longitude estimate of a location's UTC offset in hours.

    ``round(lon / 15)``: exact where civil time follows solar time, and off by
    an hour or two where it does not (Spain, western China, the edges of a wide
    zone). The provider prefers the real zone from :mod:`._tz` and uses this
    only when no zone is known.
    """
    return estimate_offset_hours(lon)


def _default_user_agent() -> str:
    """The identifying User-Agent the API's terms ask for, with the version."""
    version = ""
    try:
        version = (resolve_application_version().get("baked") or "").strip()
    except Exception:
        version = ""
    name = f"meshcore-bot/{version}" if version else "meshcore-bot"
    return f"{name} {USER_AGENT_CONTACT}"


def _parse_time(value: Any) -> Optional[datetime]:
    """An ISO 8601 UTC timestamp from the API, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_http_time(value: Any) -> Optional[datetime]:
    """An HTTP-date from a response header, or an ISO 8601 timestamp, or None.

    ``Expires`` and ``Date`` are RFC 7231 dates, which ``fromisoformat`` cannot
    read, so they go through ``parsedate_to_datetime`` first.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        return _parse_time(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _symbol_parts(code: Any) -> tuple[str, bool]:
    """(base symbol, is_day) for a symbol code such as ``partlycloudy_night``."""
    if not isinstance(code, str) or not code:
        return "", True
    base, _, suffix = code.partition("_")
    # ``_polartwilight`` is the sun at or below the horizon, so it takes the
    # moon emoji the same way ``_night`` does.
    return base, suffix not in ("night", "polartwilight")


def _wmo_for(base: str) -> Optional[int]:
    """The WMO code a base symbol maps to, or None for an unknown symbol."""
    if base in SYMBOL_TO_WMO:
        return SYMBOL_TO_WMO[base]
    # A code the map has not caught yet still says what it is.
    if base.endswith("andthunder"):
        return 95
    if "snow" in base:
        return 73
    if "sleet" in base:
        return 66
    if "rain" in base:
        return 63
    return None


class _Entry:
    """One cached document: when it expires, when it was fetched, and the payload."""

    __slots__ = ("expires", "fetched", "last_modified", "payload")

    def __init__(self, expires: float, fetched: float, last_modified: Optional[str], payload: dict):
        self.expires = expires
        self.fetched = fetched
        self.last_modified = last_modified
        self.payload = payload


class MetNorwayProvider(WeatherProvider):
    """MET Norway (api.met.no) forecasts, worldwide, in the reply's units."""

    name = "metno"
    translation_ns = "commands.gwx"
    location_style = "global"
    capabilities = frozenset({
        Capability.CURRENT,
        Capability.TOMORROW,
        Capability.MULTIDAY,
        Capability.HOURLY,
    })
    max_days = 9

    def __init__(self, host: ProviderHost, units: WeatherUnits,
                 session: Any = None, user_agent: Optional[str] = None,
                 clock: Any = None, now: Any = None):
        """Bind the provider to its host, units and (for tests) a session.

        *session* is a requests.Session the caller owns; without one, each
        request opens its own, because the command calls providers from worker
        threads and a session is not thread-safe. *clock* is a monotonic clock
        and *now* a wall clock, both replaced in tests so the cache TTLs and the
        current step can be walked forward.
        """
        super().__init__(host, units)
        self.session = session
        self._user_agent = user_agent
        self._default_user_agent: Optional[str] = None
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cache: dict[tuple, _Entry] = {}
        self._pause_until = 0.0

    # --- units ------------------------------------------------------------

    @property
    def metric_distance(self) -> bool:
        """Whether distances should be shown in kilometers (as Open-Meteo does)."""
        return self.units.temperature == 'celsius'

    @property
    def temp_symbol(self) -> str:
        """The degree symbol the reply prints."""
        return "°F" if self.units.temperature == 'fahrenheit' else "°C"

    def _temperature(self, celsius: Any) -> Optional[int]:
        """A Celsius reading in the reply's unit, rounded to a whole degree."""
        if celsius is None:
            return None
        value = float(celsius)
        if self.units.temperature == 'fahrenheit':
            value = c_to_f(value)
        return round_half_away(value)

    def _wind_speed(self, meters_per_second: Any) -> Optional[int]:
        """A m/s wind speed in the reply's unit, rounded to a whole number."""
        if meters_per_second is None:
            return None
        return round_half_away(ms_to(meters_per_second, self.units.wind_speed))

    def _precipitation(self, millimeters: Any) -> Optional[float]:
        """A precipitation amount in the reply's unit."""
        if millimeters is None:
            return None
        value = float(millimeters)
        return mm_to_inch(value) if self.units.precipitation == 'inch' else value

    @property
    def precipitation_symbol(self) -> str:
        """The precipitation unit the reply prints."""
        return "in" if self.units.precipitation == 'inch' else "mm"

    # --- the request ------------------------------------------------------

    def forecast(
        self,
        lat: float,
        lon: float,
        forecast_type: str = "default",
        num_days: int = 7,
        location_prefix_len: int = 0,
        message: Any = None,
    ) -> ForecastReply:
        """The MET Norway reply for a point, without the command's location label.

        *location_prefix_len* is the display width of the location prefix the
        command prepends; the body is formatted to what is left of the frame
        budget.
        """
        max_length = self.host.get_max_message_length(message) if message else 130
        max_length = max_length - location_prefix_len

        data = self._fetch(lat, lon)
        if data is None:
            return ForecastReply(
                text=self.host.translate('commands.gwx.metno.error_fetching_api'), ok=False
            )

        try:
            if forecast_type == "tomorrow":
                return ForecastReply(text=self._tomorrow(data, lat, lon, max_length))
            if forecast_type == "multiday":
                return ForecastReply(text=self._multiday(data, lat, lon, num_days))
            if forecast_type == "hourly":
                return ForecastReply(text=self._hourly(data, lat, lon, max_length))
            text = self._current(data, lat, lon, max_length)
            if text is None:
                return ForecastReply(
                    text=self.host.translate('commands.gwx.metno.error_fetching_api'), ok=False
                )
            extra = self._extreme_conditions(data, lon)
            return ForecastReply(text=text, extra=extra)
        except Exception as error:
            self.host.logger.error(f"Error formatting MET Norway weather: {error}")
            if forecast_type == "tomorrow":
                return ForecastReply(text=self.host.translate('commands.gwx.tomorrow_error'))
            if forecast_type == "multiday":
                return ForecastReply(
                    text=self.host.translate('commands.gwx.multiday_error', num_days=num_days)
                )
            return ForecastReply(
                text=self.host.translate('commands.gwx.metno.error_fetching_api'), ok=False
            )

    def _user_agent_header(self) -> str:
        """The User-Agent to send: the configured override, else the default."""
        override = ""
        try:
            override = self.host.bot.config.get(
                'Weather', 'metno_user_agent', fallback='', raw=True
            ).strip()
        except Exception:
            override = ""
        if override:
            return override
        if self._user_agent:
            return self._user_agent
        if self._default_user_agent is None:
            self._default_user_agent = _default_user_agent()
        return self._default_user_agent

    def _ttl(self, headers: Any, now: float) -> float:
        """Seconds until the document expires, from Expires and Date."""
        expires = _parse_http_time((headers or {}).get("Expires"))
        if expires is None:
            return DEFAULT_TTL
        issued = _parse_http_time((headers or {}).get("Date")) or self._now()
        ttl = (expires - issued).total_seconds()
        return min(max(ttl, MIN_TTL), MAX_TTL)

    def _stale(self, entry: Optional[_Entry], now: float) -> Optional[dict]:
        """The last document, while it is still a forecast; None once it is too old."""
        if entry is None or now - entry.fetched > STALE_MAX_SECONDS:
            return None
        return entry.payload

    def _fetch(self, lat: float, lon: float) -> Optional[dict]:
        """The document for a point, from the cache or the API; None on failure."""
        key = (round(lat, 4), round(lon, 4))
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires > now:
                return entry.payload
            if now < self._pause_until:
                # A 429 parks every request; a stale document still answers.
                return self._stale(entry, now)

        headers = {"User-Agent": self._user_agent_header()}
        if entry is not None and entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        params = {"lat": f"{key[0]:.4f}", "lon": f"{key[1]:.4f}"}

        try:
            if self.session is not None:
                response = self.session.get(
                    METNO_URL, params=params, headers=headers, timeout=METNO_TIMEOUT
                )
            else:
                with make_retry_session() as session:
                    response = session.get(
                        METNO_URL, params=params, headers=headers, timeout=METNO_TIMEOUT
                    )
        except Exception as error:
            self.host.logger.warning(f"Error fetching weather from MET Norway: {error}")
            return self._stale(entry, self._clock())

        if response.status_code == 304 and entry is not None:
            with self._lock:
                entry.expires = self._clock() + self._ttl(response.headers, now)
            return entry.payload

        if response.status_code == 429:
            delay = self._retry_after(response.headers)
            with self._lock:
                self._pause_until = self._clock() + delay
            self.host.logger.warning(
                f"MET Norway rate limited the request; pausing for {int(delay)} s"
            )
            return self._stale(entry, self._clock())

        if not response.ok:
            self.host.logger.warning(
                f"Error fetching weather from MET Norway: {response.status_code}"
            )
            return self._stale(entry, self._clock())

        try:
            payload = response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not self._steps(payload):
            self.host.logger.warning("MET Norway response has no forecast series")
            return self._stale(entry, self._clock())

        with self._lock:
            self._cache[key] = _Entry(
                self._clock() + self._ttl(response.headers, now),
                self._clock(),
                (response.headers or {}).get("Last-Modified"),
                payload,
            )
        return payload

    def _retry_after(self, headers: Any) -> float:
        """Retry-After in seconds, from a number or an HTTP date, else the default."""
        raw = (headers or {}).get("Retry-After")
        if not raw:
            return RATE_LIMIT_PAUSE
        text = str(raw).strip()
        try:
            return min(max(float(text), 0.0), 3600.0)
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return RATE_LIMIT_PAUSE
        if when is None:
            return RATE_LIMIT_PAUSE
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return min(max((when - self._now()).total_seconds(), 0.0), 3600.0)

    # --- the series -------------------------------------------------------

    @staticmethod
    def _steps(data: Any) -> list:
        """The usable steps of a document: dicts with a time and a data block."""
        if not isinstance(data, dict):
            return []
        properties = data.get("properties")
        if not isinstance(properties, dict):
            return []
        series = properties.get("timeseries")
        if not isinstance(series, list):
            return []
        return [
            step for step in series
            if isinstance(step, dict) and _parse_time(step.get("time")) is not None
        ]

    @staticmethod
    def _instant(step: dict) -> dict:
        """The conditions at a step's own time."""
        data = step.get("data")
        if not isinstance(data, dict):
            return {}
        instant = data.get("instant")
        if not isinstance(instant, dict):
            return {}
        details = instant.get("details")
        return details if isinstance(details, dict) else {}

    @staticmethod
    def _next(step: dict, period: str) -> dict:
        """The ``next_1_hours``/``next_6_hours``/``next_12_hours`` block of a step."""
        data = step.get("data")
        if not isinstance(data, dict):
            return {}
        block = data.get(period)
        return block if isinstance(block, dict) else {}

    def _step_symbol(self, step: dict) -> tuple[str, bool]:
        """(base symbol, is_day) for a step, from the shortest period it carries."""
        for period in ("next_1_hours", "next_6_hours", "next_12_hours"):
            summary = self._next(step, period).get("summary")
            if isinstance(summary, dict) and summary.get("symbol_code"):
                return _symbol_parts(summary["symbol_code"])
        return "", True

    def _step_precipitation(self, step: dict) -> float:
        """The precipitation a step accounts for, in millimeters."""
        for period in ("next_1_hours", "next_6_hours"):
            details = self._next(step, period).get("details")
            if isinstance(details, dict) and details.get("precipitation_amount") is not None:
                try:
                    return float(details["precipitation_amount"])
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _current_step(self, steps: list) -> dict:
        """The last step at or before now, else the first one."""
        now = self._now()
        chosen = steps[0]
        for step in steps:
            if _parse_time(step["time"]) <= now:
                chosen = step
            else:
                break
        return chosen

    def _offset(self, lat: float, lon: float, when_utc: datetime) -> timedelta:
        """The point's UTC offset at *when_utc*, so a series can cross a DST change."""
        return utc_offset_at(lat, lon, when_utc)

    def _daily(self, data: dict, lat: float, lon: float) -> list:
        """Per local day: high, low, precipitation and the daytime symbol.

        Steps before the current local day are dropped, so a document fetched
        yesterday cannot label yesterday as today.
        """
        now = self._now()
        today = (now + self._offset(lat, lon, now)).date()
        days: dict = {}
        order: list = []
        for step in self._steps(data):
            when = _parse_time(step["time"])
            moment = when + self._offset(lat, lon, when)
            day = moment.date()
            if day < today:
                continue
            if day not in days:
                days[day] = {
                    "date": day,
                    "temps": [],
                    "precipitation": 0.0,
                    "symbols": Counter(),
                    "daytime_symbols": Counter(),
                }
                order.append(day)
            entry = days[day]
            temperature = self._instant(step).get("air_temperature")
            if temperature is not None:
                try:
                    entry["temps"].append(float(temperature))
                except (TypeError, ValueError):
                    pass
            entry["precipitation"] += self._step_precipitation(step)
            base, _is_day = self._step_symbol(step)
            if base:
                entry["symbols"][base] += 1
                if 6 <= moment.hour < 18:
                    entry["daytime_symbols"][base] += 1

        result = []
        for day in order:
            entry = days[day]
            temps = entry["temps"]
            symbols = entry["daytime_symbols"] or entry["symbols"]
            result.append({
                "date": day,
                "high": max(temps) if temps else None,
                "low": min(temps) if temps else None,
                "precipitation": entry["precipitation"],
                "symbol": symbols.most_common(1)[0][0] if symbols else "",
            })
        return result

    # --- formatting -------------------------------------------------------

    def _description(self, base: str) -> str:
        """The catalog's description for a base symbol, else a plain fallback."""
        wmo = _wmo_for(base)
        if wmo is None:
            return self.host.translate('commands.gwx.weather_descriptions.unknown')
        key = f"commands.gwx.weather_descriptions.{wmo}"
        description = self.host.translate(key)
        if description == key:
            return WMO_DESCRIPTIONS.get(
                wmo, self.host.translate('commands.gwx.weather_descriptions.unknown')
            )
        return description

    def _emoji(self, base: str, is_day: bool = True) -> str:
        """The emoji for a base symbol; clear skies show a moon at night."""
        wmo = _wmo_for(base)
        if wmo is None:
            return "🌤️"
        if not is_day and wmo in (0, 1):
            return "🌙"
        return {
            0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
            51: "🌦️", 53: "🌦️", 55: "🌧️", 56: "🌧️", 57: "🌧️",
            61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
            71: "❄️", 73: "❄️", 75: "❄️", 77: "❄️",
            80: "🌦️", 81: "🌦️", 82: "🌧️", 85: "🌨️", 86: "🌨️",
            95: "⛈️", 96: "⛈️", 97: "⛈️", 99: "⛈️",
        }.get(wmo, "🌤️")

    def _compass(self, degrees: Any) -> str:
        """A wind direction in degrees as the nearest compass point with its arrow."""
        if degrees is None:
            return ""
        try:
            value = float(degrees)
        except (TypeError, ValueError):
            return ""
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        index = int((value % 360) / 22.5 + 0.5) % 16
        key = _COMPASS_16[index]
        arrow = _ARROWS_8[int(index / 2 + 0.5) % 8]
        return f"{arrow}{self.host.translate(f'common.wind_directions.{key}')}"

    def _period_key(self, local_hour: int) -> str:
        """The period label: today while the sun is up, overnight before noon."""
        if 6 <= local_hour < 18:
            return 'today'
        return 'overnight' if local_hour < 12 else 'tonight'

    def _current(self, data: dict, lat: float, lon: float, max_length: int) -> Optional[str]:
        """Current conditions for the default reply, with the details that fit."""
        steps = self._steps(data)
        if not steps:
            return None
        step = self._current_step(steps)
        details = self._instant(step)
        temperature = self._temperature(details.get("air_temperature"))
        if temperature is None:
            self.host.logger.warning("MET Norway response has no current temperature")
            return None

        when = _parse_time(step["time"])
        local_hour = (when + self._offset(lat, lon, when)).hour
        base, is_day = self._step_symbol(step)
        condition = f"{self._emoji(base, is_day)}{self._description(base)}" if base else ""
        period_name = self.host.translate(f'commands.gwx.periods.{self._period_key(local_hour)}')

        wind_speed = self._wind_speed(details.get("wind_speed"))
        wind = ""
        if wind_speed is not None and wind_speed >= 3:
            wind = f"{self._compass(details.get('wind_from_direction'))}{wind_speed}"

        humidity = details.get("relative_humidity")
        humidity_text = ""
        if humidity is not None:
            humidity_text = self.host.translate(
                'commands.gwx.humidity', value=int(round(float(humidity)))
            )

        pressure = " ".join(self._extra_conditions(details))

        high_low = ""
        days = self._daily(data, lat, lon)
        if days and days[0]["high"] is not None and days[0]["low"] is not None:
            high_low = self.host._format_high_low(
                self._temperature(days[0]["high"]),
                self._temperature(days[0]["low"]),
                self.temp_symbol,
            )

        # The parts are added in priority order and the least important ones
        # dropped in turn until the line fits: pressure, then humidity, then
        # wind, then the condition text. The period, temperature and high/low
        # always stay.
        optional = [pressure, humidity_text, wind, condition]
        weather = ""
        for dropped in range(len(optional) + 1):
            pressure_, humidity_, wind_, condition_ = optional[dropped:] + [""] * dropped
            weather = self._current_line(
                period_name, condition_, temperature, wind_, humidity_, pressure_, high_low
            )
            if self.host._count_display_width(weather) <= max_length:
                break

        if high_low and len(days) > 1:
            weather = self._tomorrow_tail(weather, days[1], max_length)
        return weather

    def _current_line(self, period_name: str, condition: str, temperature: int,
                      wind: str, humidity: str, pressure: str, high_low: str) -> str:
        """One current-conditions line from the parts that survived the budget."""
        line = f"{period_name}: "
        if condition:
            line += f"{condition} "
        line += f"{temperature}{self.temp_symbol}"
        for piece in (wind, humidity, pressure):
            if piece:
                line += f" {piece}"
        if high_low:
            line += f" | {high_low}"
        return line

    def _extra_conditions(self, details: dict) -> list:
        """The pressure string, when the step carries one.

        MET Norway's compact endpoint has no dew point and no visibility, so
        those two conditions Open-Meteo adds have nothing to show here.
        """
        conditions = []
        pressure = details.get("air_pressure_at_sea_level")
        if pressure is not None:
            pressure_hpa = round_half_away(pressure)
            if self.host.translate('commands.gwx.pressure_unit').strip().lower() == 'mmhg':
                conditions.append(self.host.translate(
                    'commands.gwx.pressure_mmhg', value=round_half_away(hpa_to_mmhg(pressure_hpa))
                ))
            else:
                conditions.append(self.host.translate('commands.gwx.pressure', value=pressure_hpa))
        return conditions

    def _tomorrow_tail(self, weather: str, day: dict, max_length: int) -> str:
        """Append tomorrow's high/low and precipitation while they fit."""
        if day["high"] is None or day["low"] is None:
            return weather
        high = self._temperature(day["high"])
        low = self._temperature(day["low"])
        emoji = self._emoji(day["symbol"])
        period = self.host.translate('commands.gwx.periods.tomorrow')
        high_low = self.host._format_high_low(high, low, self.temp_symbol)
        described = f" | {period}: {emoji}{self._description(day['symbol'])} {high_low}"
        if self.host._count_display_width(weather + described) <= max_length - 10:
            weather += described
        else:
            plain = f" | {period}: {emoji} {high_low}"
            if self.host._count_display_width(weather + plain) <= max_length - 10:
                weather += plain
            else:
                return weather

        amount = self._precipitation(day["precipitation"])
        if amount is not None and amount > 0:
            precip = f" 🌦️{amount:.2f}{self.precipitation_symbol}"
            if self.host._count_display_width(weather + precip) <= max_length - 5:
                weather += precip
        return weather

    def _tomorrow(self, data: dict, lat: float, lon: float, max_length: int) -> str:
        """Tomorrow's forecast, with the details that fit the budget."""
        days = self._daily(data, lat, lon)
        if len(days) < 2 or days[1]["high"] is None or days[1]["low"] is None:
            return self.host.translate('commands.gwx.tomorrow_not_available')
        day = days[1]
        high = self._temperature(day["high"])
        low = self._temperature(day["low"])
        emoji = self._emoji(day["symbol"])
        description = self._description(day["symbol"])
        period = self.host.translate('commands.gwx.periods.tomorrow')
        high_low = self.host._format_high_low(high, low, self.temp_symbol)
        head = f"{period}: {emoji}"

        amount = self._precipitation(day["precipitation"])
        precip_info = ""
        if amount is not None and amount > 0:
            precip_info = f" 🌦️{amount:.2f}{self.precipitation_symbol}"

        candidates = [
            f"{head}{description} {high_low}{precip_info}",
            f"{head}{description} {high_low}",
            f"{head} {high_low}",
        ]
        for candidate in candidates:
            if self.host._count_display_width(candidate) <= max_length:
                return candidate
        return candidates[-1]

    def _multiday(self, data: dict, lat: float, lon: float, num_days: int) -> str:
        """One line per day from tomorrow, as Open-Meteo's multiday reply is."""
        days = self._daily(data, lat, lon)
        if len(days) < 2:
            return self.host.translate('commands.gwx.multiday_not_available', num_days=num_days)

        day_abbrev_map = {
            'Monday': self.host.translate('commands.gwx.day_abbrev.Monday'),
            'Tuesday': self.host.translate('commands.gwx.day_abbrev.Tuesday'),
            'Wednesday': self.host.translate('commands.gwx.day_abbrev.Wednesday'),
            'Thursday': self.host.translate('commands.gwx.day_abbrev.Thursday'),
            'Friday': self.host.translate('commands.gwx.day_abbrev.Friday'),
            'Saturday': self.host.translate('commands.gwx.day_abbrev.Saturday'),
            'Sunday': self.host.translate('commands.gwx.day_abbrev.Sunday'),
        }

        parts = []
        for day in days[1:num_days + 1]:
            if day["high"] is None or day["low"] is None:
                break
            day_name = day["date"].strftime('%A')
            day_abbrev = day_abbrev_map.get(day_name, day_name[:2])
            description = self._description(day["symbol"])
            if len(description) > 20:
                description = description[:17] + "..."
            parts.append(
                f"{day_abbrev}: {self._emoji(day['symbol'])}{description} "
                f"{self.host._format_high_low(self._temperature(day['high']), self._temperature(day['low']), self.temp_symbol)}"
            )

        if not parts:
            return self.host.translate('commands.gwx.multiday_not_available', num_days=num_days)
        return "\n".join(parts)

    def _hourly(self, data: dict, lat: float, lon: float, max_length: int) -> str:
        """The hours after the current one, packed into one reply."""
        steps = self._steps(data)
        if not steps:
            return self.host.translate('commands.gwx.hourly_not_available')
        current = self._current_step(steps)
        current_when = _parse_time(current["time"])
        # The cutoff is a UTC instant, so a fall-back day keeps both of its
        # 02:00 steps; the offset is only used to label each one.
        next_hour = current_when.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

        lines = []
        for step in steps:
            if not self._next(step, "next_1_hours"):
                # The series turns six-hourly after about two and a half days.
                continue
            when = _parse_time(step["time"])
            if when < next_hour:
                continue
            moment = when + self._offset(lat, lon, when)
            details = self._instant(step)
            temperature = self._temperature(details.get("air_temperature"))
            base, is_day = self._step_symbol(step)
            if temperature is None or not base:
                break
            parts = [
                f"{self.host._hour_label(moment.isoformat())}:",
                self._emoji(base, is_day),
                self.host._short_hourly_description(self._description(base)),
                f"{temperature}°",
            ]
            wind_speed = self._wind_speed(details.get("wind_speed"))
            if wind_speed is not None:
                parts.append(f"{self.host._without_arrow(self._compass(details.get('wind_from_direction')))}{wind_speed}")
            lines.append(" ".join(parts))
        return self.host._pack_hourly_lines(lines, max_length, self.translation_ns)

    def _extreme_conditions(self, data: dict, lon: float) -> Optional[str]:
        """The same warnings Open-Meteo's current conditions raise, from the same data."""
        steps = self._steps(data)
        if not steps:
            return None
        step = self._current_step(steps)
        details = self._instant(step)
        base, _is_day = self._step_symbol(step)
        wmo = _wmo_for(base)

        warnings = []
        temperature = details.get("air_temperature")
        if temperature is not None:
            fahrenheit = float(temperature) * 9 / 5 + 32
            if fahrenheit >= 95:
                warnings.append(self.host.translate('commands.gwx.warnings.extreme_heat'))
            elif fahrenheit <= 20:
                warnings.append(self.host.translate('commands.gwx.warnings.extreme_cold'))

        if wmo in (65, 82):
            warnings.append(self.host.translate('commands.gwx.warnings.heavy_rain'))
        if wmo in (95, 96, 97, 99):
            warnings.append(self.host.translate('commands.gwx.warnings.thunderstorms'))
        if wmo in (75, 85, 86):
            warnings.append(self.host.translate('commands.gwx.warnings.heavy_snow'))

        # The threshold is 30 mph, as Open-Meteo's is; the warning shows the
        # speed in the configured unit, like the wind in the reply it follows.
        meters_per_second = details.get("wind_speed")
        if meters_per_second is not None and ms_to(meters_per_second, 'mph') >= 30:
            wind = self._wind_speed(meters_per_second) or 0
            unit = self.host.translate(f'services.weather_service.wind_speed_units.{self.units.wind_speed}')
            warnings.append(self.host.translate(
                'commands.gwx.warnings.high_winds', wind_speed=round_half_away(wind), unit=unit
            ))

        return " | ".join(warnings) if warnings else None
