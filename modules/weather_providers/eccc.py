"""Environment and Climate Change Canada: the ``eccc`` forecast provider.

The public OGC API at api.weather.gc.ca needs no key. A point is answered by the
nearest city page in ``citypageweather-realtime``: the API takes a bbox and
returns every city page inside it, so the provider widens the box until it finds
one and then picks the closest by great-circle distance. Past 150 km the point
is outside the city-page network, which the reply reports as no coverage so the
command can fall back to Open-Meteo.

Every text field is a ``{"en": ..., "fr": ...}`` pair, so the reply language is
chosen once per request from ``[Localization] language``.

The data is metric (°C, km/h, mm, kPa) and the reply is built to Open-Meteo's
line shapes and byte budgets, so a bot that switches providers keeps the same
look. The forecast group covers today plus six days, which is ``max_days``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from ..http_retry import make_retry_session
from ._geo import haversine_km
from ._tz import utc_offset_at
from .base import Capability, ForecastReply, ProviderHost, WeatherProvider, WeatherUnits
from .openmeteo import weather_emoji

ECCC_BASE_URL = "https://api.weather.gc.ca"
CITYPAGE_COLLECTION = "citypageweather-realtime"
ECCC_TIMEOUT = (5, 6)  # (connect, read) seconds, per attempt
CITY_CACHE_SECONDS = 600
CITY_SEARCH_RADII = (0.3, 1.0, 2.0)  # degrees, widened in turn
MAX_CITY_DISTANCE_KM = 150.0
ECCC_MULTIDAY_MAX_DAYS = 6

# Roughly Canada: the city-page network's own extent, used for coverage so the
# command can fall back to Open-Meteo elsewhere without a request.
CANADA_LON = (-141.0, -52.0)
CANADA_LAT = (41.6, 83.2)

# ECCC icon codes to the WMO codes the shared emoji and description tables use.
# The codes in the samples (2, 3, 10, 12, 16, 28, 32, 33, 36) are confirmed
# against their own text; the rest follow the published icon set. Codes the
# table does not carry fall back to a keyword match on the description.
ICON_WMO = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 3, 5: 3, 6: 3, 7: 3, 8: 3, 9: 3,
    10: 3, 11: 61, 12: 63, 13: 65, 14: 80, 15: 81, 16: 71, 17: 73, 18: 75,
    19: 95, 20: 45, 21: 45, 22: 73, 23: 73, 24: 66, 25: 67, 26: 66, 27: 67,
    28: 53, 29: 51, 30: 45, 31: 45, 32: 2, 33: 3, 34: 3, 35: 3, 36: 80,
    37: 45, 38: 71,
}

# Keyword fallback, most specific first: the description text is always present
# even when the icon code is not one the table carries.
KEYWORD_WMO = (
    ("freezing rain", 66),
    ("freez", 66),
    ("thunder", 95),
    ("flurr", 71),
    ("snow", 73),
    ("drizzle", 53),
    ("shower", 80),
    ("rain", 63),
    ("fog", 45),
    ("haze", 45),
    ("smoke", 45),
    ("mix of sun", 2),
    ("sun", 0),
    ("clear", 0),
    ("cloud", 3),
    ("overcast", 3),
)


def _wmo_for(icon_code: Any, description: str) -> Optional[int]:
    """The WMO code for an ECCC icon code, else from the description's words."""
    try:
        code = int(icon_code)
    except (TypeError, ValueError):
        code = None
    if code is not None and code in ICON_WMO:
        return ICON_WMO[code]
    text = (description or "").lower()
    for keyword, wmo in KEYWORD_WMO:
        if keyword in text:
            return wmo
    return None


def _parse_time(value: Any) -> Optional[datetime]:
    """An ISO 8601 timestamp from the API, as an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class EcccProvider(WeatherProvider):
    """Environment Canada city pages: Canada only, in the reply's units."""

    name = "eccc"
    translation_ns = "commands.gwx"
    location_style = "global"
    capabilities = frozenset({
        Capability.CURRENT,
        Capability.TOMORROW,
        Capability.MULTIDAY,
        Capability.HOURLY,
    })
    max_days = ECCC_MULTIDAY_MAX_DAYS
    default_alerts_provider = "eccc"

    def __init__(self, host: ProviderHost, units: WeatherUnits, session: Any = None,
                 clock: Any = None, now: Any = None):
        """Bind the provider to its host and units.

        *session*, *clock* and *now* are for tests: a fake session, a fake
        monotonic clock for the cache and a fixed wall clock, so neither the
        cache nor the period label depends on when the suite runs.
        """
        super().__init__(host, units)
        self._session = session
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cache: dict = {}

    # --- coverage ---------------------------------------------------------

    def covers(self, lat: float, lon: float) -> bool:
        """Whether the point is inside the city-page network's extent."""
        return (
            CANADA_LAT[0] <= lat <= CANADA_LAT[1]
            and CANADA_LON[0] <= lon <= CANADA_LON[1]
        )

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
        """The Environment Canada reply for a point, without the location label.

        *location_prefix_len* is the display width of the location prefix the
        command prepends; the body is formatted to what is left of the frame
        budget.
        """
        max_length = self.host.get_max_message_length(message) if message else 130
        max_length = max_length - location_prefix_len

        feature, failed = self._lookup(lat, lon)
        if failed:
            return ForecastReply(
                text=self.host.translate('commands.gwx.eccc.error_fetching_api'), ok=False
            )
        if feature is None:
            return ForecastReply(
                text=self.host.translate('commands.gwx.eccc.error_fetching_api'),
                ok=False,
                no_coverage=True,
            )

        try:
            if forecast_type == "tomorrow":
                return ForecastReply(text=self._tomorrow(feature, lat, lon, max_length))
            if forecast_type == "multiday":
                return ForecastReply(text=self._multiday(feature, lat, lon, num_days, max_length))
            if forecast_type == "hourly":
                return ForecastReply(text=self._hourly(feature, lat, lon, max_length))
            text = self._current(feature, lat, lon, max_length)
            if text is None:
                return ForecastReply(
                    text=self.host.translate('commands.gwx.eccc.error_fetching_api'), ok=False
                )
            return ForecastReply(text=text)
        except Exception as error:
            self.host.logger.error(f"Error formatting Environment Canada weather: {error}")
            if forecast_type == "tomorrow":
                return ForecastReply(text=self.host.translate('commands.gwx.tomorrow_error'))
            if forecast_type == "multiday":
                return ForecastReply(
                    text=self.host.translate('commands.gwx.multiday_error', num_days=num_days)
                )
            if forecast_type == "hourly":
                return ForecastReply(text=self.host.translate('commands.gwx.hourly_not_available'))
            return ForecastReply(
                text=self.host.translate('commands.gwx.eccc.error_fetching_api'), ok=False
            )

    def _lookup(self, lat: float, lon: float) -> tuple[Optional[dict], bool]:
        """The nearest city page for a point, and whether the fetch failed.

        ``(feature, False)`` is an answer, ``(None, False)`` means no city page
        is close enough, and ``(None, True)`` means the request itself failed.
        """
        key = (round(lat, 2), round(lon, 2))
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1], cached[2]

        feature, failed, ttl = self._fetch_city_page(lat, lon)
        with self._lock:
            self._cache[key] = (now + ttl, feature, failed)
        return feature, failed

    def _fetch_city_page(self, lat: float, lon: float) -> tuple[Optional[dict], bool, float]:
        """Query the city pages around a point, widening the box until one answers."""
        language = self._language()
        for radius in CITY_SEARCH_RADII:
            params = {
                "f": "json",
                "lang": language,
                "bbox": (
                    f"{lon - radius:.4f},{lat - radius:.4f},"
                    f"{lon + radius:.4f},{lat + radius:.4f}"
                ),
            }
            try:
                response = self._get(f"{ECCC_BASE_URL}/collections/{CITYPAGE_COLLECTION}/items", params)
            except Exception as error:
                self.host.logger.warning(f"Error fetching Environment Canada city pages: {error}")
                return None, True, CITY_CACHE_SECONDS
            if response is None:
                return None, True, CITY_CACHE_SECONDS
            if not getattr(response, "ok", False):
                self.host.logger.warning(
                    "Error fetching weather from Environment Canada: "
                    f"{getattr(response, 'status_code', '?')}"
                )
                return None, True, CITY_CACHE_SECONDS

            features = self._features(response)
            nearest = self._nearest(features, lat, lon)
            if nearest is not None:
                return nearest, False, self._ttl(response)
        return None, False, CITY_CACHE_SECONDS

    def _get(self, url: str, params: dict):
        """One GET, with the provider's own session or a fresh one."""
        if self._session is not None:
            return self._session.get(url, params=params, timeout=ECCC_TIMEOUT)
        with make_retry_session() as session:
            return session.get(url, params=params, timeout=ECCC_TIMEOUT)

    def _features(self, response) -> list:
        """The GeoJSON features of a successful response, or an empty list."""
        try:
            body = response.json()
        except Exception:
            return []
        features = body.get("features") if isinstance(body, dict) else None
        return [feature for feature in (features or []) if isinstance(feature, dict)]

    def _nearest(self, features: list, lat: float, lon: float) -> Optional[dict]:
        """The city page closest to the point, if one is within reach."""
        best = None
        best_distance = None
        for feature in features:
            point = (feature.get("geometry") or {}).get("coordinates") or []
            if len(point) < 2:
                continue
            try:
                distance = haversine_km(lat, lon, float(point[1]), float(point[0]))
            except (TypeError, ValueError):
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = feature, distance
        if best is None or best_distance is None or best_distance > MAX_CITY_DISTANCE_KM:
            return None
        return best

    def _ttl(self, response) -> float:
        """The cache lifetime: ten minutes, or less when ``Expires`` says so."""
        headers = getattr(response, "headers", None) or {}
        expires = headers.get("Expires")
        if not expires:
            return CITY_CACHE_SECONDS
        try:
            moment = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return CITY_CACHE_SECONDS
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        remaining = (moment - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(CITY_CACHE_SECONDS, remaining))

    # --- reading the data -------------------------------------------------

    def _language(self) -> str:
        """The reply language: French when the bot's language is French, else English."""
        try:
            value = (self.host.bot.config.get('Localization', 'language', fallback='') or '').strip()
        except Exception:
            value = ''
        return 'fr' if value.lower().startswith('fr') else 'en'

    def _text(self, node: Any) -> str:
        """One localized text field, in the reply language."""
        if isinstance(node, dict):
            value = node.get(self._language())
            if value is None:
                value = node.get('en')
            return str(value) if value is not None else ""
        return str(node) if node is not None else ""

    def _value(self, node: Any) -> Any:
        """One localized numeric field, in the reply language."""
        if isinstance(node, dict):
            value = node.get(self._language())
            if value is None:
                value = node.get('en')
            return value
        return node

    def _properties(self, feature: dict) -> dict:
        return feature.get("properties") or {}

    def _days(self, feature: dict) -> list:
        """The forecast periods paired into days: high, low, summary and icon."""
        forecasts = ((self._properties(feature).get("forecastGroup") or {}).get("forecasts")) or []
        days: list = []
        for period in forecasts:
            if not isinstance(period, dict):
                continue
            temperatures = ((period.get("temperatures") or {}).get("temperature")) or []
            high = low = None
            for entry in temperatures:
                if not isinstance(entry, dict):
                    continue
                kind = self._text(entry.get("class")).lower()
                value = self._value(entry.get("value"))
                if kind == "high":
                    high = value
                elif kind == "low":
                    low = value
            abbreviated = period.get("abbreviatedForecast") or {}
            summary = self._text(abbreviated.get("textSummary"))
            icon = (abbreviated.get("icon") or {}).get("value")
            name = self._text((period.get("period") or {}).get("textForecastName"))
            if high is not None:
                days.append({"high": high, "low": None, "summary": summary, "icon": icon, "name": name})
            elif low is not None:
                if days and days[-1]["low"] is None:
                    days[-1]["low"] = low
                else:
                    days.append({"high": None, "low": low, "summary": summary, "icon": icon, "name": name})
        return days

    def _temperature(self, celsius: Any) -> Optional[int]:
        """A Celsius reading in the reply's temperature unit, rounded."""
        if celsius is None:
            return None
        try:
            value = float(celsius)
        except (TypeError, ValueError):
            return None
        if self.units.temperature == 'fahrenheit':
            value = value * 9 / 5 + 32
        return int(round(value))

    def _wind_speed(self, kmh: Any) -> Optional[int]:
        """A km/h reading in the reply's wind unit, rounded."""
        if kmh is None:
            return None
        try:
            value = float(kmh)
        except (TypeError, ValueError):
            return None
        if self.units.wind_speed == 'mph':
            value = value * 0.621371
        elif self.units.wind_speed == 'ms':
            value = value / 3.6
        elif self.units.wind_speed == 'kn':
            value = value / 1.852
        return int(round(value))

    def _precipitation(self, millimetres: Any) -> Optional[float]:
        """A millimetre reading in the reply's precipitation unit."""
        if millimetres is None:
            return None
        try:
            value = float(millimetres)
        except (TypeError, ValueError):
            return None
        return value / 25.4 if self.units.precipitation == 'inch' else value

    @property
    def temp_symbol(self) -> str:
        return "°F" if self.units.temperature == 'fahrenheit' else "°C"

    @property
    def precipitation_symbol(self) -> str:
        return "in" if self.units.precipitation == 'inch' else "mm"

    def _condition(self, icon: Any, description: str, is_day: bool = True) -> str:
        """The emoji and description for one icon code and its own text."""
        wmo = _wmo_for(icon, description)
        emoji = weather_emoji(wmo, is_day=is_day) if wmo is not None else ""
        text = description or (self.host.translate(f'commands.gwx.weather_descriptions.{wmo}')
                               if wmo is not None else "")
        return f"{emoji}{text}"

    def _emoji(self, icon: Any, is_day: bool = True) -> str:
        """Just the emoji for an icon code, for lines that carry the data's own text."""
        wmo = _wmo_for(icon, "")
        return weather_emoji(wmo, is_day=is_day) if wmo is not None else ""

    def _offset(self, lat: float, lon: float, when: datetime) -> timedelta:
        """The point's UTC offset at *when*, so a series can cross a DST change."""
        return utc_offset_at(lat, lon, when)

    def _period_key(self, local_hour: int) -> str:
        """The period label: today while the sun is up, overnight before noon."""
        if 6 <= local_hour < 18:
            return 'today'
        return 'overnight' if local_hour < 12 else 'tonight'

    # --- the replies ------------------------------------------------------

    def _current(self, feature: dict, lat: float, lon: float, max_length: int) -> Optional[str]:
        """Current conditions, today's high and low, then tomorrow while it fits."""
        properties = self._properties(feature)
        current = properties.get("currentConditions") or {}
        temperature = self._temperature(self._value((current.get("temperature") or {}).get("value")))
        if temperature is None:
            self.host.logger.warning("Environment Canada response has no current temperature")
            return None

        days = self._days(feature)
        now = self._now()
        local_hour = (now + self._offset(lat, lon, now)).hour
        is_day = 6 <= local_hour < 18

        icon = (current.get("iconCode") or {}).get("value")
        description = ""
        if icon is None and days:
            # The current icon is sometimes only a format; the first period's
            # own summary describes the same conditions.
            icon = days[0]["icon"]
            description = days[0]["summary"]
        condition = self._condition(icon, description, is_day)
        period_name = self.host.translate(f'commands.gwx.periods.{self._period_key(local_hour)}')
        weather = f"{period_name}: {condition} {temperature}{self.temp_symbol}" if condition \
            else f"{period_name}: {temperature}{self.temp_symbol}"

        wind = current.get("wind") or {}
        speed = self._wind_speed(self._value((wind.get("speed") or {}).get("value")))
        direction = self._text((wind.get("direction") or {}).get("value"))
        if speed is not None and speed >= 3:
            weather += f" {direction}{speed}"

        humidity = self._value((current.get("relativeHumidity") or {}).get("value"))
        if humidity is not None:
            try:
                weather += f" {self.host.translate('commands.gwx.humidity', value=int(round(float(humidity))))}"
            except (TypeError, ValueError):
                pass

        pressure = self._value((current.get("pressure") or {}).get("value"))
        conditions = []
        if pressure is not None:
            try:
                conditions.append(self.host.translate('commands.gwx.pressure', value=int(round(float(pressure) * 10))))
            except (TypeError, ValueError):
                pass
        if conditions and self.host._count_display_width(weather) < max_length - 80:
            weather += " " + " ".join(conditions)

        if days and days[0]["high"] is not None and days[0]["low"] is not None:
            weather += f" | {self.host._format_high_low(self._temperature(days[0]['high']), self._temperature(days[0]['low']), self.temp_symbol)}"
            if len(days) > 1:
                weather = self._tomorrow_tail(weather, days[1], max_length)
        return weather

    def _tomorrow_tail(self, weather: str, day: dict, max_length: int) -> str:
        """Append tomorrow's summary and high/low while they fit."""
        if day["high"] is None or day["low"] is None:
            return weather
        period = self.host.translate('commands.gwx.periods.tomorrow')
        high_low = self.host._format_high_low(
            self._temperature(day["high"]), self._temperature(day["low"]), self.temp_symbol
        )
        emoji = self._emoji(day["icon"])
        summary = day["summary"]
        # The data's summaries are phrases, so they keep a space after the emoji.
        described = f" | {period}: {emoji} {summary} {high_low}"
        plain = f" | {period}: {emoji} {high_low}"
        for candidate in (described, plain):
            if self.host._count_display_width(weather + candidate) <= max_length - 10:
                return weather + candidate
        return weather

    def _tomorrow(self, feature: dict, lat: float, lon: float, max_length: int) -> str:
        """Tomorrow's forecast, with the details that fit the budget."""
        days = self._days(feature)
        if len(days) < 2 or days[1]["high"] is None or days[1]["low"] is None:
            return self.host.translate('commands.gwx.tomorrow_not_available')
        day = days[1]
        period = self.host.translate('commands.gwx.periods.tomorrow')
        high_low = self.host._format_high_low(
            self._temperature(day["high"]), self._temperature(day["low"]), self.temp_symbol
        )
        emoji = self._emoji(day["icon"])
        head = f"{period}: {emoji}"
        candidates = [
            f"{head} {day['summary']} {high_low}",
            f"{head} {high_low}",
        ]
        for candidate in candidates:
            if self.host._count_display_width(candidate) <= max_length:
                return candidate
        return candidates[-1]

    def _multiday(self, feature: dict, lat: float, lon: float, num_days: int,
                  max_length: int = 130) -> str:
        """One line per day from tomorrow, as Open-Meteo's multiday reply is."""
        days = self._days(feature)
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

        # The summaries are longer than Open-Meteo's, so they are shortened in
        # turn until the whole reply fits the frame: full, then cut, then none.
        text = ""
        for limit in (20, 12, 0):
            parts = []
            for day in days[1:num_days + 1]:
                if day["high"] is None or day["low"] is None:
                    break
                # The period's own name is the weekday ("Saturday"), localized.
                day_name = day["name"] or ""
                day_abbrev = day_abbrev_map.get(day_name, day_name[:2])
                summary = day["summary"]
                if limit and len(summary) > limit:
                    summary = summary[:limit - 3] + "..."
                elif not limit:
                    summary = ""
                emoji = self._emoji(day["icon"])
                high_low = self.host._format_high_low(
                    self._temperature(day["high"]), self._temperature(day["low"]), self.temp_symbol
                )
                parts.append(f"{day_abbrev}: {emoji} {summary} {high_low}".replace("  ", " "))
            if not parts:
                return self.host.translate('commands.gwx.multiday_not_available', num_days=num_days)
            text = "\n".join(parts)
            if self.host._count_display_width(text) <= max_length:
                return text
        return text

    def _hourly(self, feature: dict, lat: float, lon: float, max_length: int) -> str:
        """The hours after the current one, packed into one reply."""
        properties = self._properties(feature)
        hours = ((properties.get("hourlyForecastGroup") or {}).get("hourlyForecasts")) or []
        if not hours:
            return self.host.translate('commands.gwx.hourly_not_available')

        now = self._now()
        next_hour = (now + self._offset(lat, lon, now)).replace(minute=0, second=0, microsecond=0)
        next_hour += timedelta(hours=1)

        lines = []
        for hour in hours:
            if not isinstance(hour, dict):
                continue
            when = _parse_time(hour.get("timestamp"))
            if when is None:
                continue
            moment = when + self._offset(lat, lon, when)
            if moment < next_hour:
                continue
            temperature = self._temperature(self._value((hour.get("temperature") or {}).get("value")))
            if temperature is None:
                break
            icon = (hour.get("iconCode") or {}).get("value")
            description = self._text(hour.get("condition"))
            is_day = 6 <= moment.hour < 18
            parts = [
                f"{self.host._hour_label(moment.isoformat())}:",
                self._condition(icon, "", is_day).strip(),
                self.host._short_hourly_description(description),
                f"{temperature}°",
            ]
            probability = self._value((hour.get("lop") or {}).get("value"))
            if probability is not None:
                try:
                    if int(probability) > 0:
                        parts.insert(2, f"{int(probability)}%")
                except (TypeError, ValueError):
                    pass
            wind = hour.get("wind") or {}
            speed = self._wind_speed(self._value((wind.get("speed") or {}).get("value")))
            direction = self._text((wind.get("direction") or {}).get("value"))
            if speed is not None:
                parts.append(f"{self.host._without_arrow(direction)}{speed}")
            lines.append(" ".join(parts))
        return self.host._pack_hourly_lines(lines, max_length, self.translation_ns)
