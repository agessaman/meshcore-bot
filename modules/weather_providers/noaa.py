#!/usr/bin/env python3
"""NOAA (api.weather.gov) forecasts: the US provider behind wx.

The fetch, the formatting and the message catalog live here. The command keeps
the request (option parsing, geocoding, location labels) and the sending, and
hands this provider the units to ask for. Formatting helpers shared with the
command (byte width, high/low templates, hour labels) come from the host, so
one implementation serves both.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests

from .. import alert_format
from ..commands.rain_command import nws_http_means_no_coverage
from ..http_retry import make_retry_session
from ..nws_coverage import NWSNoCoverageCache
from ..utils import format_temperature_high_low
from ..weather_common import _ARROWS_8, _COMPASS_16
from ._units import M_PER_MILE, c_to_f, kmh_to, ms_to, round_half_away
from .base import Capability, ForecastReply, ProviderHost, WeatherProvider, WeatherUnits

# Placeholder a failed fetch is compared against; the reply the user sees is the
# translated commands.wx.error_fetching.
ERROR_FETCHING_DATA = "Error fetching weather data"
NO_ALERTS = "No weather alerts"

# Floor for the forecast body once a location prefix has been reserved out of
# the frame budget, so a long "City, State: " can never squeeze the forecast
# down to nothing (or negative, which the -10/-20 arithmetic in the period
# formatters would turn into dropped fields).
MIN_BODY_BUDGET = 40

# One NOAA request; the retry session adds its own attempts on top.
NOAA_TIMEOUT = 8

# NOAA sometimes names forecast periods after federal holidays (e.g. "Washington's Birthday")
# instead of the weekday. Match these so we can resolve to weekday via startTime.
_NOAA_HOLIDAY_NAME_PATTERNS = (
    "washington's birthday", "presidents day", "president's day",
    "martin luther king", "mlk day", "memorial day", "labor day",
    "independence day", "juneteenth", "columbus day", "veterans day",
    "thanksgiving", "christmas day", "new year's day", "new year's eve",
)

# Multiday: plain digits (e.g. 7), 7day/7-day, or suffix form 7d/10d (min 2, max below).
WX_MULTIDAY_MAX_DAYS = 16

_WEEKDAYS_LOWER = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
# Multi-day line labels, Monday first (date.weekday() order); the catalog's
# commands.wx.day_abbrev translates them, and these are the English fallback.
_DAY_ABBREVS = ('M', 'T', 'W', 'Th', 'F', 'Sa', 'Su')
_WEEKDAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')

# Forecast-text patterns for the extract_* readers, tried in order.
# extract_humidity: "humidity 45%" or "45% humidity"
_HUMIDITY_PATTERNS = (
    r'humidity\s+(\d+)%',
    r'(\d+)%\s+humidity',
    r'relative humidity\s+(\d+)%',
    r'(\d+)%\s+relative humidity',
)

# extract_precip_chance: "20% chance" or "chance of rain 30%"
_PRECIP_CHANCE_PATTERNS = (
    r'(\d+)%\s+chance',
    r'chance\s+of\s+\w+\s+(\d+)%',
    r'(\d+)%\s+probability',
    r'probability\s+of\s+\w+\s+(\d+)%',
)

# extract_uv_index
_UV_INDEX_PATTERNS = (
    r'uv\s+index\s+(\d+)',
    r'uv\s+(\d+)',
    r'ultraviolet\s+index\s+(\d+)',
)

# extract_dew_point
_DEW_POINT_PATTERNS = (
    r'dew point\s+(\d+)',
    r'dewpoint\s+(\d+)',
    r'dew\s+point\s+(\d+)°',
)

# extract_visibility
_VISIBILITY_PATTERNS = (
    r'visibility\s+(\d+)\s+miles',
    r'visibility\s+(\d+)\s+mi',
    r'(\d+)\s+mile\s+visibility',
    r'(\d+)\s+mi\s+visibility',
)

# extract_precip_probability
_PRECIP_PROBABILITY_PATTERNS = (
    r'(\d+)%\s+chance\s+of\s+(?:rain|precipitation|showers)',
    r'chance\s+of\s+(?:rain|precipitation|showers)\s+(\d+)%',
    r'chance\s+of\s+(?:rain|precipitation|showers)\s+is\s+(\d+)%',  # NOAA: "Chance of precipitation is 60%."
    r'(\d+)%\s+probability\s+of\s+(?:rain|precipitation|showers)',
    r'probability\s+of\s+(?:rain|precipitation|showers)\s+(\d+)%',
    r'(\d+)%\s+chance',
    r'chance\s+(\d+)%',
)

# extract_wind_gusts
_WIND_GUST_PATTERNS = (
    r'gusts\s+to\s+(\d+)\s+mph',
    r'gusts\s+up\s+to\s+(\d+)\s+mph',
    r'wind\s+gusts\s+to\s+(\d+)\s+mph',
    r'wind\s+gusts\s+up\s+to\s+(\d+)\s+mph',
    r'gusts\s+(\d+)\s+mph',
    r'wind\s+gusts\s+(\d+)\s+mph',
    r'gusts\s+as\s+high\s+as\s+(\d+)\s+mph',  # NOAA: "with gusts as high as 25 mph."
)

# extract_wind_gusts in SI forecast text ("Wind gusts up to 48 km/h")
_WIND_GUST_KMH_PATTERNS = tuple(p.replace(r"\s+mph", r"\s*km/h") for p in _WIND_GUST_PATTERNS)

# extract_pressure
_PRESSURE_PATTERNS = (
    r'pressure\s+(\d+)\s*hpa',
    r'pressure\s+(\d+)\s*mb',
    r'barometric\s+pressure\s+(\d+)\s*hpa',
    r'barometric\s+pressure\s+(\d+)\s*mb',
    r'(\d+)\s*hpa',
    r'(\d+)\s*mb\s+pressure',
)


def _has_temp(value) -> bool:
    """Whether a NOAA temperature is present; 0° is a temperature (common in Celsius)."""
    return value is not None and value != ''


def _first_match(text: str, patterns: tuple[str, ...], low: int | None = None, high: int | None = None) -> str:
    """Group 1 of the first pattern found in the lowercased *text*, within [low, high] when given.

    Each pattern is tried at its first match only; a value out of range moves on
    to the next pattern, not to a later match of the same one.
    """
    if not text:
        return ""
    lowered = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            value = match.group(1)
            if low is None or high is None:
                return value
            try:
                if low <= int(value) <= high:
                    return value
            except ValueError:
                continue
    return ""


def resolve_units(setting: Callable[[str, str], str]) -> WeatherUnits:
    """The units a provider replies in, from a (key, fallback) -> value lookup.

    Same lookup as the WXSIM path: the command's own section, falling back to [Weather].
    Invalid values fall back to fahrenheit/mph/inch, as gwx does.
    """
    temperature = setting('temperature_unit', 'fahrenheit')
    wind_speed = setting('wind_speed_unit', 'mph')
    precipitation = setting('precipitation_unit', 'inch')
    if temperature not in ('fahrenheit', 'celsius'):
        temperature = 'fahrenheit'
    if wind_speed not in ('mph', 'kmh', 'ms', 'kn'):
        wind_speed = 'mph'
    if precipitation not in ('inch', 'mm'):
        precipitation = 'inch'
    return WeatherUnits(temperature=temperature, wind_speed=wind_speed, precipitation=precipitation)


class NoaaProvider(WeatherProvider):
    """NOAA (api.weather.gov) forecasts for US points, in the reply's units."""

    name = "noaa"
    translation_ns = "commands.wx"
    location_style = "us"
    capabilities = frozenset({
        Capability.CURRENT,
        Capability.TOMORROW,
        Capability.MULTIDAY,
        Capability.HOURLY,
    })
    max_days = WX_MULTIDAY_MAX_DAYS
    default_alerts_provider = "nws"
    uses_command_session = True

    def __init__(
        self,
        host: ProviderHost,
        units: WeatherUnits,
        session: Optional[requests.Session] = None,
        lock: Optional[threading.Lock] = None,
        coverage: Optional[NWSNoCoverageCache] = None,
        fallback_enabled: bool = False,
    ):
        """Bind the provider to its host, its units and the command's NOAA state.

        *session*, *lock* and *coverage* are shared with the alerts provider, so
        one requests.Session, one worker lock and one coverage cache serve both.
        *fallback_enabled* says the command can answer points outside NWS
        coverage from another provider, which is when coverage is worth recording.
        """
        super().__init__(host, units)
        self.session = session if session is not None else make_retry_session()
        self.lock = lock if lock is not None else threading.Lock()
        self.coverage = coverage if coverage is not None else NWSNoCoverageCache()
        self.fallback_enabled = fallback_enabled
        self.url_timeout = NOAA_TIMEOUT

    def covers(self, lat: float, lon: float) -> bool:
        """Whether NWS is not known to lack coverage for this point."""
        return not self.coverage.is_unavailable(lat, lon)

    def forecast(
        self,
        lat: float,
        lon: float,
        forecast_type: str = "default",
        num_days: int = 7,
        location_prefix_len: int = 0,
        message: Any = None,
    ) -> ForecastReply:
        """The NOAA reply for a point, without the command's location label.

        *location_prefix_len* is the display width of the location prefix the
        command prepends; the body is formatted to what is left of the frame
        budget, never below MIN_BODY_BUDGET.
        """
        max_length = self.host.get_max_message_length(message) if message else 130
        body_max_length = max(max_length - location_prefix_len, MIN_BODY_BUDGET)
        reply, weather = self._noaa_forecast_body(lat, lon, forecast_type, num_days, body_max_length)
        if reply is not None:
            return ForecastReply(text=reply, ok=False, no_coverage=not self.covers(lat, lon))
        return ForecastReply(text=weather)

    def _noaa_units(self) -> tuple[str, str]:
        """Configured (temperature_unit, wind_speed_unit) for NOAA replies."""
        return self.units.temperature, self.units.wind_speed

    @property
    def _noaa_metric_distance(self) -> bool:
        """Kilometers instead of miles, following the temperature unit as gwx does."""
        return self.units.temperature == 'celsius'

    def _noaa_units_url(self, url: str) -> str:
        """A NOAA forecast URL asking for SI units (°C, km/h, also in the forecast text) when Celsius is configured."""
        if self._noaa_units()[0] != 'celsius':
            return url
        return f"{url}{'&' if '?' in url else '?'}units=si"

    def _noaa_wind_convert(self, number: str, wind_speed_text: str) -> str:
        """A NOAA wind number in the configured wind_speed_unit (NOAA sends mph, or km/h in SI mode)."""
        source = 'kmh' if 'km/h' in wind_speed_text else 'mph'
        target = self._noaa_units()[1]
        if source == target:
            return number
        mph = int(number) if source == 'mph' else int(number) / 1.609344
        value = {'mph': mph, 'kmh': mph * 1.609344, 'ms': mph * 0.44704, 'kn': mph * 0.868976}[target]
        return str(int(round(value)))

    @staticmethod
    def _noaa_period_temp_symbol(period: dict) -> str:
        u = (period.get("temperatureUnit") or "F").upper()
        return "°F" if u == "F" else "°C"

    def _noaa_forecast_body(self, lat: float, lon: float, forecast_type: str, num_days: int, body_max_length: int) -> tuple:
        """(error reply or None, formatted forecast) for one forecast type."""
        # Get weather forecast based on type
        if forecast_type == "tomorrow":
            forecast_periods, points_data = self.get_noaa_weather(lat, lon, return_periods=True, max_length=body_max_length)
            if forecast_periods == ERROR_FETCHING_DATA:
                return self.host.translate('commands.wx.error_fetching'), None
            weather = self.format_tomorrow_forecast(forecast_periods, max_length=body_max_length)
        elif forecast_type == "multiday":
            forecast_periods, points_data = self.get_noaa_weather(lat, lon, return_periods=True, max_length=body_max_length)
            if forecast_periods == ERROR_FETCHING_DATA:
                return self.host.translate('commands.wx.error_fetching'), None
            weather = self.format_multiday_forecast(forecast_periods, num_days, max_length=body_max_length)
        elif forecast_type == "hourly":
            hourly_periods, points_data = self.get_noaa_hourly_weather(lat, lon)
            if hourly_periods == ERROR_FETCHING_DATA:
                return self.host.translate('commands.wx.error_fetching'), None
            weather = self.format_hourly_forecast(hourly_periods, max_length=body_max_length)
        else:  # default
            weather, points_data = self.get_noaa_weather(lat, lon, max_length=body_max_length)
            if weather == ERROR_FETCHING_DATA:
                return self.host.translate('commands.wx.error_fetching'), None

            # Note: Current conditions are now integrated directly into the current period
            # via _add_period_details() using observation station data
        return None, weather

    def _noaa_fetch(self, url: str, what: str, point: Optional[tuple[float, float]] = None):
        """GET *url* through the NOAA session; None, after a warning naming *what*,
        on an HTTP error status, a timeout or a connection error.

        With the Open-Meteo fallback on, a *point* request also records whether
        NWS covers that point.
        """
        # Only a point request with the fallback on records coverage.
        point = point if self.fallback_enabled else None
        try:
            response = self.session.get(url, timeout=self.url_timeout)
            if not response.ok:
                if point is not None and nws_http_means_no_coverage(response.status_code):
                    self.coverage.mark_unavailable(*point)
                    self.host.logger.debug(
                        "NWS has no coverage for %s,%s (HTTP %s)",
                        round(point[0], 2), round(point[1], 2), response.status_code,
                    )
                    return None
                self.host.logger.warning(f"Error fetching {what} from NOAA: HTTP {response.status_code}")
                return None
            if point is not None:
                self.coverage.mark_available(*point)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self.host.logger.warning(f"Timeout/connection error fetching {what} from NOAA: {e}")
            return None
        return response

    def get_noaa_weather(self, lat: float, lon: float, return_periods: bool = False, max_length: int = 130) -> tuple:
        """Get weather forecast from NOAA and return both weather string and points data

        Args:
            lat: Latitude
            lon: Longitude
            return_periods: If True, return forecast periods array instead of formatted string
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Tuple of (weather_string_or_periods, points_data)
        """
        try:
            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            # Get weather data from NOAA
            weather_api = f"https://api.weather.gov/points/{lat_rounded},{lon_rounded}"

            # Get the forecast URL (with retry logic)
            weather_data = self._noaa_fetch(weather_api, "weather data", point=(lat, lon))
            if weather_data is None:
                return ERROR_FETCHING_DATA, None

            weather_json = weather_data.json()
            forecast_url = weather_json['properties']['forecast']

            # Get the forecast (with retry logic)
            forecast_data = self._noaa_fetch(self._noaa_units_url(forecast_url), "weather forecast")
            if forecast_data is None:
                return ERROR_FETCHING_DATA, None

            forecast_json = forecast_data.json()
            forecast = forecast_json['properties']['periods']

            # If return_periods is True, return the periods array directly
            if return_periods:
                if not forecast:
                    return ERROR_FETCHING_DATA, None
                return forecast, weather_json

            # Format the forecast - focus on current conditions and key info
            if not forecast:
                return "No forecast data available", weather_json

            current = forecast[0]
            weather = self._noaa_current_summary(current, weather_json, max_length)

            # Add next period (Today, Tonight) and Tomorrow if available
            today_period, tonight_period, tomorrow_period, is_current_tonight, is_current_night = (
                self._noaa_followup_periods(forecast, current)
            )

            # If current is a night period, prioritize adding Today (the upcoming daytime)
            # When today_period is a day name (like "Tuesday"), we still add it as tomorrow's daytime period
            if is_current_night and today_period:
                # Always add today_period - it represents tomorrow's daytime when current is Tonight
                weather = self._append_noaa_period(weather, today_period[1], max_length)

            # Add Tonight if it's the immediate next period (and current is not already Tonight)
            # If we already added Today, we can still add Tonight if it's the next period after Today
            if tonight_period and not is_current_tonight:
                # Only add if it's the immediate next period, or if current is night and we haven't added Today yet
                should_add_tonight = False
                if is_current_night and today_period:
                    # If current is night and we added Today, check if Tonight comes after Today
                    if tonight_period[0] > today_period[0]:
                        should_add_tonight = True
                elif tonight_period[0] == 1:
                    # If current is not night, Tonight should be the immediate next period
                    should_add_tonight = True

                if should_add_tonight:
                    weather = self._append_noaa_period(weather, tonight_period[1], max_length)

            # Always try to add Tomorrow if available (especially if current is Tonight)
            # Prioritize adding Tomorrow when current is Tonight to use more of the available message length
            if tomorrow_period:
                weather = self._append_noaa_tomorrow(weather, tomorrow_period[1], is_current_tonight, is_current_night, max_length)

            return weather, weather_json

        except Exception as e:
            self.host.logger.error(f"Error fetching NOAA weather: {e}")
            return ERROR_FETCHING_DATA, None

    def _noaa_current_summary(self, current: dict, weather_json: dict, max_length: int) -> str:
        """The current period's line: name, sky, temperature, wind and as many details as fit."""
        day_name = self._noaa_period_display_name(current)
        temp = current.get('temperature', 'N/A')
        temp_unit = current.get('temperatureUnit', 'F')
        short_forecast = current.get('shortForecast', 'Unknown')
        wind_speed = current.get('windSpeed', '')
        wind_direction = current.get('windDirection', '')
        detailed_forecast = current.get('detailedForecast', '')

        # Extract additional useful info from detailed forecast
        self.extract_humidity(detailed_forecast)
        precip_chance = self.extract_precip_chance(detailed_forecast)

        # Create compact but complete weather string with emoji
        weather_emoji = self.get_weather_emoji(short_forecast)
        weather = f"{day_name}: {weather_emoji}{short_forecast} {temp}°{temp_unit}"

        # Add wind info if available
        if wind_speed and wind_direction:
            wind_match = re.search(r'(\d+)', wind_speed)
            if wind_match:
                wind_num = self._noaa_wind_convert(wind_match.group(1), wind_speed)
                wind_dir = self.abbreviate_wind_direction(wind_direction)
                if wind_dir:
                    weather += f" {wind_dir}{wind_num}"

        # PRIORITIZE: Add all available details to current period first
        # Get observation station data for more accurate current conditions
        observation_data = self.get_observation_data(weather_json)

        # Use most of the max_length limit (max_length - 10 chars) to ensure current period gets full details
        # Additional periods will only be added if there's remaining space
        # Pass observation_data to use real-time station data instead of parsing from text
        current_period_max = max_length - 10
        weather = self._add_period_details(weather, detailed_forecast, 0, max_length=current_period_max, observation_data=observation_data)

        # Also add precipitation chance if available (not in helper function)
        if precip_chance and self.host._count_display_width(weather) < current_period_max:
            weather += f" 🌦️{precip_chance}%"

        # Also add UV index if available (not in helper function)
        uv_index = self.extract_uv_index(detailed_forecast)
        if uv_index and self.host._count_display_width(weather) < current_period_max:
            weather += f" UV{uv_index}"

        return weather

    def _append_noaa_tomorrow(self, weather: str, period: dict, is_current_tonight: bool, is_current_night: bool, max_length: int) -> str:
        """``weather`` with the Tomorrow period appended when it fits; a night reply shortens its text and is more lenient."""
        period_detailed = period.get('detailedForecast', '')
        period_short = period.get('shortForecast', '')
        night = is_current_tonight or is_current_night
        period_head = None
        if _has_temp(period.get('temperature', '')) and period_short:
            # Shorten long forecast text (especially when current is a night period)
            forecast_text = (
                self._abbreviate_noaa_forecast(period_short) if night and len(period_short) > 20 else period_short
            )
            period_head = self._noaa_period_str(period, forecast_text)
        if period_head:
            # Be more aggressive about adding wind when current is a night period
            wind_threshold = 115 if night else 120
            period_str = self._noaa_period_wind(weather, period_head, period, wind_threshold, max_length)

            # Add additional details (humidity, dew point, visibility, etc.)
            # But only if current period isn't too long - prioritize current period details
            current_weather_len = self.host._count_display_width(weather)
            # Only add details to additional periods if current period is under max_length - 20 chars
            # This ensures we prioritize current period details first
            if current_weather_len < max_length - 20:
                max_chars = max_length - 2 if (is_current_tonight or is_current_night) else max_length
                period_str = self._add_period_details(period_str, period_detailed, current_weather_len, max_chars)

            # Only add if we have space (using display width, prioritize current period)
            # Be more aggressive about adding tomorrow_period when current is Tonight and we have space
            max_chars = max_length - 2 if (is_current_tonight or is_current_night) else max_length
            # If current is Tonight and we have plenty of space, be more lenient with the length check
            if is_current_tonight or is_current_night:
                # Allow adding tomorrow_period if we're under max_length - 10 chars (more lenient)
                if current_weather_len < max_length - 10 and self.host._count_display_width(weather + period_str) <= max_chars:
                    weather += period_str
            else:
                # For non-night periods, use the stricter check
                if current_weather_len < max_length - 20 and self.host._count_display_width(weather + period_str) <= max_chars:
                    weather += period_str
        return weather

    def _append_noaa_period(self, weather: str, period: dict, max_length: int) -> str:
        """``weather`` with a Today or Tonight period appended when it fits.

        The period gets wind and details only while the reply so far leaves room,
        so the current period keeps its full details first.
        """
        period_detailed = period.get('detailedForecast', '')
        period_head = self._noaa_period_str(period)
        if period_head:
            period_str = self._noaa_period_wind(weather, period_head, period, max_length - 10, max_length)

            # Add additional details (humidity, dew point, visibility, etc.)
            # But only if current period isn't too long - prioritize current period details
            current_weather_len = self.host._count_display_width(weather)
            # Only add details to additional periods if current period is under max_length - 20 chars
            # This ensures we prioritize current period details first
            if current_weather_len < max_length - 20:
                period_str = self._add_period_details(period_str, period_detailed, current_weather_len, max_length=max_length)

            # Only add if we have space (using display width)
            # Be more conservative - only add if current period is reasonable length
            if current_weather_len < max_length - 20 and self.host._count_display_width(weather + period_str) <= max_length:
                weather += period_str
        return weather

    def _noaa_followup_periods(self, forecast: list, current: dict) -> tuple:
        """The ``(index, period)`` pairs for Today, Tonight and Tomorrow after ``current``, and whether
        ``current`` is Tonight or any night period: ``(today, tonight, tomorrow, is_tonight, is_night)``."""
        # First, find Today, Tonight, and Tomorrow periods
        today_period = None
        tonight_period = None
        tomorrow_period = None
        current_period_name = current.get('name', '').lower()
        is_current_tonight = 'tonight' in current_period_name
        is_current_night = any(word in current_period_name for word in ['tonight', 'overnight', 'night'])

        # Check if current period is a night period (Overnight, Tonight, etc.)
        # If so, we should prioritize showing the upcoming daytime period (Today)
        for i, period in enumerate(forecast):
            period_name = period.get('name', '').lower()
            # Look for "Today" period (daytime forecast)
            if 'today' in period_name and today_period is None and i > 0:
                # Make sure it's not a night period
                if 'night' not in period_name and 'tonight' not in period_name:
                    today_period = (i, period)
            elif 'tonight' in period_name and tonight_period is None:
                tonight_period = (i, period)
            elif 'tomorrow' in period_name and tomorrow_period is None:
                tomorrow_period = (i, period)

        # If current is a night period and we haven't found Today yet, look for next daytime period
        if is_current_night and not today_period:
            # Look for the next period that's not a night period
            for i, period in enumerate(forecast):
                if i > 0:  # Skip current period
                    period_name = period.get('name', '').lower()
                    # Look for daytime periods (Today, or day names without "night")
                    if 'today' in period_name and 'night' not in period_name:
                        today_period = (i, period)
                        break
                    # Also check for day names that aren't night periods
                    day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                    if any(day in period_name for day in day_names) and 'night' not in period_name:
                        today_period = (i, period)
                        break

        # If current is Tonight and we haven't found Tomorrow yet, look for next day's periods
        if is_current_tonight and not tomorrow_period:
            # If today_period is a day name (not "Today"), look for the next period after it
            if today_period:
                period_name_lower = today_period[1].get('name', '').lower()
                day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                if any(day in period_name_lower for day in day_names) and 'today' not in period_name_lower:
                    # today_period is actually tomorrow's daytime period - look for the night period after it
                    today_period_index = today_period[0]
                    # Look for the next period after today_period (should be the night period for that day)
                    for i, period in enumerate(forecast):
                        if i > today_period_index:  # Look for periods after today_period
                            period_name = period.get('name', '').lower()
                            # Look for the night period for the same day, or the next day
                            if any(word in period_name for word in ['night', 'tomorrow', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
                                tomorrow_period = (i, period)
                                break
                    # If we didn't find a night period, use today_period as tomorrow_period
                    if not tomorrow_period:
                        tomorrow_period = today_period
                else:
                    # Look for periods after Tonight (next day)
                    for i, period in enumerate(forecast):
                        if i > 0:  # Skip current period
                            period_name = period.get('name', '').lower()
                            # Skip if this period is already set as today_period (avoid duplicates)
                            if today_period and today_period[0] == i:
                                continue
                            # Look for tomorrow, next day, or day names
                            if any(word in period_name for word in ['tomorrow', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
                                tomorrow_period = (i, period)
                                break
            else:
                # Look for periods after Tonight (next day)
                for i, period in enumerate(forecast):
                    if i > 0:  # Skip current period
                        period_name = period.get('name', '').lower()
                        # Look for tomorrow, next day, or day names
                        if any(word in period_name for word in ['tomorrow', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
                            tomorrow_period = (i, period)
                            break
        return today_period, tonight_period, tomorrow_period, is_current_tonight, is_current_night

    def _noaa_period_str(self, period: dict, forecast_text: Optional[str] = None) -> Optional[str]:
        """``" | Name: <emoji><forecast> <high/low or temp°>"`` for a forecast period.

        None when the period has no temperature or no short forecast.
        ``forecast_text`` replaces the short forecast in the text (the emoji
        still comes from the short forecast).
        """
        period_name = self._noaa_period_display_name(period)
        period_temp = period.get('temperature', '')
        period_short = period.get('shortForecast', '')
        if not (_has_temp(period_temp) and period_short):
            return None
        period_high_low = self.extract_high_low(
            period.get('detailedForecast', ''), self._noaa_period_temp_symbol(period)
        )
        period_emoji = self.get_weather_emoji(period_short)
        text = period_short if forecast_text is None else forecast_text
        if period_high_low:
            return f" | {period_name}: {period_emoji}{text} {period_high_low}"
        return f" | {period_name}: {period_emoji}{text} {period_temp}°"

    def _noaa_period_wind(self, weather: str, period_str: str, period: dict, threshold: int, max_length: int) -> str:
        """Append the period's wind to ``period_str`` when ``weather + period_str`` is under ``threshold``
        display columns and the result still fits ``max_length``."""
        period_wind_speed = period.get('windSpeed', '')
        period_wind_direction = period.get('windDirection', '')
        if period_wind_speed and period_wind_direction:
            test_str = weather + period_str
            if self.host._count_display_width(test_str) < threshold:
                wind_match = re.search(r'(\d+)', period_wind_speed)
                if wind_match:
                    wind_num = self._noaa_wind_convert(wind_match.group(1), period_wind_speed)
                    wind_dir = self.abbreviate_wind_direction(period_wind_direction)
                    if wind_dir:
                        wind_info = f" {wind_dir}{wind_num}"
                        if self.host._count_display_width(test_str + wind_info) <= max_length:
                            return period_str + wind_info
        return period_str

    @staticmethod
    def _abbreviate_noaa_forecast(period_short: str) -> str:
        """Shorten a long short-forecast ("A then B and C") to its main conditions."""
        abbreviated_forecast = period_short
        # Try to shorten forecast text to fit more info
        # Remove transitional words and keep meaningful conditions
        words = period_short.split()
        # Transitional words to skip
        transitions = {'then', 'and', 'or', 'becoming', 'followed', 'by', 'with'}

        # If there's a "then" pattern, take first condition and last significant condition
        if 'then' in words:
            then_index = words.index('then')
            # Take first condition (before "then")
            first_part = words[:then_index]
            # Take last significant condition (after "then", skip small words)
            if then_index + 1 < len(words):
                last_part = [w for w in words[then_index + 1:] if w.lower() not in transitions]
                # Combine: first condition + last significant condition (max 2 words)
                if last_part:
                    abbreviated_forecast = ' '.join(first_part)
                    if len(last_part) <= 2:
                        abbreviated_forecast += ' ' + ' '.join(last_part)
                    else:
                        # Take last 2 words of the last part
                        abbreviated_forecast += ' ' + ' '.join(last_part[-2:])
                else:
                    abbreviated_forecast = ' '.join(first_part)
            else:
                abbreviated_forecast = ' '.join(first_part)
        else:
            # Filter out transitional words and take first meaningful words
            meaningful_words = [w for w in words if w.lower() not in transitions]
            if len(meaningful_words) > 3:
                abbreviated_forecast = ' '.join(meaningful_words[:3])
            else:
                abbreviated_forecast = ' '.join(meaningful_words)
        return abbreviated_forecast

    def get_noaa_hourly_weather(self, lat: float, lon: float) -> tuple:
        """Get hourly weather forecast from NOAA

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Tuple of (hourly_periods_list, points_data)
        """
        try:
            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            # Get weather data from NOAA
            weather_api = f"https://api.weather.gov/points/{lat_rounded},{lon_rounded}"

            # Get the forecast URL (with retry logic)
            weather_data = self._noaa_fetch(weather_api, "weather data", point=(lat, lon))
            if weather_data is None:
                return ERROR_FETCHING_DATA, None

            weather_json = weather_data.json()
            hourly_forecast_url = weather_json['properties'].get('forecastHourly')

            if not hourly_forecast_url:
                self.host.logger.warning("Hourly forecast not available for this location")
                return ERROR_FETCHING_DATA, None

            # Get the hourly forecast (with retry logic)
            hourly_data = self._noaa_fetch(self._noaa_units_url(hourly_forecast_url), "hourly forecast")
            if hourly_data is None:
                return ERROR_FETCHING_DATA, None

            hourly_json = hourly_data.json()
            hourly_periods = hourly_json['properties']['periods']

            if not hourly_periods:
                self.host.logger.warning("No hourly periods returned from NOAA")
                return ERROR_FETCHING_DATA, None

            return hourly_periods, weather_json

        except Exception as e:
            self.host.logger.error(f"Error fetching NOAA hourly weather: {e}")
            return ERROR_FETCHING_DATA, None

    def format_hourly_forecast(self, hourly_periods: list, max_length: int = 130) -> str:
        """Format hourly forecast to fit as many hours as possible in max_length bytes

        Args:
            hourly_periods: List of hourly forecast periods from NOAA
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Formatted string with one hour per line
        """
        try:
            if not hourly_periods:
                return self.host.translate('commands.wx.hourly_not_available')

            future_periods = self._future_hourly_periods(hourly_periods)
            if not future_periods:
                return "No future hourly periods available"

            return self.host._pack_hourly_lines(
                (self._hourly_line(period) for period in future_periods), max_length
            )

        except Exception as e:
            self.host.logger.error(f"Error formatting hourly forecast: {e}")
            return f"Error formatting hourly forecast: {str(e)}"

    @staticmethod
    def _parse_noaa_start_time(start_time_str: str) -> datetime:
        """Parse a NOAA ISO ``startTime``; a trailing ``Z`` means UTC."""
        if 'Z' in start_time_str:
            return datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        return datetime.fromisoformat(start_time_str)

    def _future_hourly_periods(self, hourly_periods: list) -> list:
        """Periods starting after now; unparseable or missing times are kept.

        NOAA times carry the location's UTC offset, so they are compared as
        absolute times. Dropping the offset compared the location's wall clock
        with the bot's, which kept past hours or dropped future ones whenever
        the two were in different time zones.
        """
        now_utc = datetime.now(timezone.utc)
        now_naive = datetime.now()
        future_periods = []
        for period in hourly_periods:
            start_time_str = period.get('startTime', '')
            if not start_time_str:
                future_periods.append(period)
                continue
            try:
                start_time = self._parse_noaa_start_time(start_time_str)
            except (ValueError, TypeError):
                future_periods.append(period)
                continue
            now = now_utc if start_time.tzinfo else now_naive
            if start_time > now:
                future_periods.append(period)
        return future_periods

    def _hourly_line(self, period: dict) -> str:
        """One hour as "10AM: 🌦️ 26% Chance Light Rain 49° SSW5"."""
        temp = period.get('temperature', '')
        short_forecast = period.get('shortForecast', '')
        wind_speed = period.get('windSpeed', '')
        wind_direction = period.get('windDirection', '')
        precip_prob = period.get('probabilityOfPrecipitation', {}).get('value')
        time_str = self.host._hour_label(period.get('startTime', ''))
        emoji = self.get_weather_emoji(short_forecast)

        forecast_short = self.host._short_hourly_description(short_forecast)

        line_parts = []
        if time_str:
            line_parts.append(f"{time_str}:")
        line_parts.append(emoji)
        if precip_prob is not None and precip_prob > 0:
            line_parts.append(f"{precip_prob}%")
        line_parts.append(forecast_short)
        if _has_temp(temp):
            line_parts.append(f"{temp}°")
        if wind_speed and wind_direction:
            wind_match = re.search(r'(\d+)', wind_speed)
            if wind_match:
                wind_dir_abbrev = self.host._without_arrow(self.abbreviate_wind_direction(wind_direction))
                line_parts.append(f"{wind_dir_abbrev}{self._noaa_wind_convert(wind_match.group(1), wind_speed)}")
        return " ".join(line_parts)

    @staticmethod
    def _period_date(period: dict):
        """The local calendar date a NOAA period starts on, from its startTime, or None.

        NOAA writes startTime in the location's own offset ("2026-10-01T18:00:00-07:00"),
        so its date part is the local date there, whatever the bot's clock says.
        """
        start = period.get('startTime') if isinstance(period, dict) else None
        if not isinstance(start, str) or len(start) < 10:
            return None
        try:
            return datetime.strptime(start[:10], '%Y-%m-%d').date()
        except ValueError:
            return None

    def _forecast_today(self, forecast: list):
        """Today's date at the forecast's location, or the bot's date when no period has an offset.

        The location's "now" is the current instant in the UTC offset NOAA writes on
        its period times. The first period's own date is not enough: at 1 AM the
        first period can still be the night that started yesterday evening.
        """
        for period in forecast:
            start = period.get('startTime') if isinstance(period, dict) else None
            if not isinstance(start, str):
                continue
            try:
                start_dt = self._parse_noaa_start_time(start)
            except (ValueError, TypeError):
                continue
            if start_dt.tzinfo is not None:
                return datetime.now(timezone.utc).astimezone(start_dt.tzinfo).date()
        return datetime.now().date()

    def _period_dates(self, forecast: list) -> list:
        """Each period's local date: its startTime date, or inferred from its neighbors when it has none.

        NOAA periods alternate day and night in order, so an undated period is the
        same date as a preceding daytime period when it is a night, else the day after.
        """
        dates = [self._period_date(p) for p in forecast]

        def is_night(period) -> bool:
            day = period.get('isDaytime') if isinstance(period, dict) else None
            if day is not None:
                return not day
            return 'night' in str(period.get('name', '') if isinstance(period, dict) else '').lower()

        for i in range(1, len(dates)):
            if dates[i] is None and dates[i - 1] is not None:
                same_day = not is_night(forecast[i - 1]) and is_night(forecast[i])
                dates[i] = dates[i - 1] if same_day else dates[i - 1] + timedelta(days=1)
        for i in range(len(dates) - 2, -1, -1):
            if dates[i] is None and dates[i + 1] is not None:
                same_day = not is_night(forecast[i]) and is_night(forecast[i + 1])
                dates[i] = dates[i + 1] if same_day else dates[i + 1] - timedelta(days=1)
        return dates

    def _find_tomorrow_periods(self, forecast: list) -> list:
        """Tomorrow's NOAA periods: those dated the day after today at the location;
        without dates, those named "Tomorrow", else named for tomorrow's weekday, else the
        (up to two) periods after today's.
        """
        # Dated periods: tomorrow is the day after today at the location.
        if any(self._period_date(p) for p in forecast):
            tomorrow = self._forecast_today(forecast) + timedelta(days=1)
            dated = [p for p, d in zip(forecast, self._period_dates(forecast), strict=True) if d == tomorrow]
            if dated:
                return dated

        # No usable startTime: fall back to period names and the bot's clock.
        tomorrow_day_name = (datetime.now() + timedelta(days=1)).strftime('%A')

        tomorrow_periods = [p for p in forecast if 'tomorrow' in p.get('name', '').lower()]
        if tomorrow_periods:
            return tomorrow_periods

        for period in forecast:
            period_name_lower = period.get('name', '').lower()
            if tomorrow_day_name.lower() in period_name_lower:
                # A name like "Monday" can also be today's; skip those.
                today_day_name = datetime.now().strftime('%A')
                if today_day_name.lower() not in period_name_lower:
                    tomorrow_periods.append(period)
        if tomorrow_periods:
            return tomorrow_periods

        # Generic names: take the periods after today's (Today, This Afternoon,
        # This Evening, Tonight), usually tomorrow's day and night.
        found_tonight = False
        for period in forecast:
            period_name = period.get('name', '').lower()
            if any(word in period_name for word in ['today', 'this afternoon', 'this evening', 'tonight']):
                found_tonight = True
                continue
            if found_tonight:
                tomorrow_periods.append(period)
                if len(tomorrow_periods) >= 2:
                    break
        return tomorrow_periods

    def format_tomorrow_forecast(self, forecast: list, max_length: int = 130) -> str:
        """Format a detailed forecast for tomorrow"""
        try:
            tomorrow_periods = self._find_tomorrow_periods(forecast)

            if not tomorrow_periods:
                return self.host.translate('commands.wx.tomorrow_not_available')

            # Build detailed forecast for tomorrow
            parts = []
            for period in tomorrow_periods:
                period_name = self._noaa_period_display_name(period)
                temp = period.get('temperature', '')
                temp_unit = period.get('temperatureUnit', 'F')
                short_forecast = period.get('shortForecast', '')
                detailed_forecast = period.get('detailedForecast', '')
                wind_speed = period.get('windSpeed', '')
                wind_direction = period.get('windDirection', '')

                if not _has_temp(temp) or not short_forecast:
                    continue

                # Create period string
                emoji = self.get_weather_emoji(short_forecast)
                period_str = f"{period_name}: {emoji}{short_forecast} {temp}°{temp_unit}"

                # Wind info, which goes first when the reply is too long
                wind = ""
                if wind_speed and wind_direction:
                    wind_match = re.search(r'(\d+)', wind_speed)
                    if wind_match:
                        wind_num = self._noaa_wind_convert(wind_match.group(1), wind_speed)
                        wind_dir = self.abbreviate_wind_direction(wind_direction)
                        if wind_dir:
                            wind = f" {wind_dir}{wind_num}"

                # Try to extract high/low
                high_low = self.extract_high_low(
                    detailed_forecast, self._noaa_period_temp_symbol(period)
                )
                if high_low and '°' not in (period_str + wind).split()[-1]:  # Avoid duplicate temp
                    period_str = period_str.replace(f" {temp}°{temp_unit}", f" {high_low}")

                parts.append((period_str + wind, period_str))

            if not parts:
                return self.host.translate('commands.wx.tomorrow_not_available')

            return self._fit_tomorrow_parts(parts, max_length)

        except Exception as e:
            self.host.logger.error(f"Error formatting tomorrow forecast: {e}")
            return self.host.translate('commands.wx.tomorrow_error')

    def _fit_tomorrow_parts(self, parts: list[tuple[str, str]], max_length: int) -> str:
        """Join tomorrow's periods within *max_length* UTF-8 bytes.

        Each part is (with wind, without wind). The later periods' wind goes
        first, then the first period's, then the later periods themselves. When
        even the first period alone does not fit, it is sent anyway (the send
        path splits it) rather than leaving the reply empty.
        """
        full = [with_wind for with_wind, _ in parts]
        bare = [without for _, without in parts]
        candidates = [full]
        if len(parts) > 1:
            candidates.append(full[:1] + bare[1:])
        candidates += [bare, full[:1], bare[:1]]
        for candidate in candidates:
            text = " | ".join(candidate)
            if self.host._count_display_width(text) <= max_length:
                return text
        return bare[0]

    def format_multiday_forecast(self, forecast: list, num_days: int = 7, max_length: int = 130) -> str:
        """Format a less detailed multi-day forecast summary"""
        try:
            if any(self._period_date(p) for p in forecast):
                parts = self._multiday_lines_by_date(forecast, num_days)
                if not parts:
                    return self.host.translate('commands.wx.multiday_not_available', num_days=num_days)
                return "\n".join(parts)

            # No usable startTime: group by weekday name against the bot's clock.
            # One entry per weekday; a day period wins over a night one.
            days: dict[str, dict] = {}
            for period in forecast:
                period_name_lower = period.get('name', '').lower()
                day_name = self._multiday_day_name(period_name_lower)
                if not day_name:
                    continue

                # Prefer the high/low from the detailed text over the bare temperature.
                temp = period.get('temperature', '')
                detailed_forecast = period.get('detailedForecast', '')
                high_low = self.extract_high_low(
                    detailed_forecast, self._noaa_period_temp_symbol(period)
                )
                if high_low:
                    temp_str = high_low
                elif _has_temp(temp):
                    temp_str = f"{temp}°"
                else:
                    continue

                short_forecast = period.get('shortForecast', '')
                if not short_forecast:
                    continue

                # "tonight" contains "night", so this also covers Tonight.
                is_day = 'night' not in period_name_lower
                if day_name not in days or is_day or not days[day_name]['is_day']:
                    days[day_name] = {'temp': temp_str, 'forecast': short_forecast, 'is_day': is_day}

            if not days:
                return self.host.translate('commands.wx.multiday_not_available', num_days=num_days)

            # Format as compact summary
            parts = []
            day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

            # Get today's day name to start ordering
            today = datetime.now().strftime('%A')

            # Reorder days starting from today
            if today in day_order:
                start_idx = day_order.index(today)
                ordered_days = day_order[start_idx:] + day_order[:start_idx]
            else:
                ordered_days = day_order

            # Limit to requested number of days
            # Map day names to short abbreviations in the reply's language
            day_abbrev_map = {name: self._day_abbrev(i) for i, name in enumerate(_WEEKDAY_NAMES)}

            # Collect days up to num_days, starting from tomorrow (skip today)
            days_collected = 0
            for day in ordered_days[1:]:  # Skip today, start from tomorrow
                if days_collected >= num_days:
                    break
                if day in days:
                    day_data = days[day]
                    day_abbrev = day_abbrev_map.get(day, day[:2])  # Use 2-letter abbrev
                    emoji = self.get_weather_emoji(day_data['forecast'])
                    # Abbreviate forecast text
                    forecast_short = self.abbreviate_noaa(day_data['forecast'])
                    # Further shorten if needed to fit on one line (but be less aggressive)
                    if len(forecast_short) > 25:
                        forecast_short = forecast_short[:22] + "..."

                    parts.append(f"{day_abbrev}: {emoji}{forecast_short} {day_data['temp']}")
                    days_collected += 1

            if not parts:
                return self.host.translate('commands.wx.multiday_not_available', num_days=num_days)

            # Join with newlines instead of pipes
            result = "\n".join(parts)

            return result

        except Exception as e:
            self.host.logger.error(f"Error formatting {num_days}-day forecast: {e}")
            return self.host.translate('commands.wx.multiday_error', num_days=num_days)

    def _multiday_lines_by_date(self, forecast: list, num_days: int) -> list[str]:
        """One line per local date after today at the location, up to *num_days*.

        Grouping by startTime date (instead of weekday name against the bot's
        clock) keeps holiday-named periods ("Christmas Day") and does not depend
        on the bot sharing the location's time zone. A daytime period wins over
        that date's night period.
        """
        today = self._forecast_today(forecast)
        days: dict = {}
        for period, day in zip(forecast, self._period_dates(forecast), strict=True):
            if day is None or day <= today:
                continue
            temp = period.get('temperature', '')
            high_low = self.extract_high_low(
                period.get('detailedForecast', ''), self._noaa_period_temp_symbol(period)
            )
            if high_low:
                temp_str = high_low
            elif _has_temp(temp):
                temp_str = f"{temp}°"
            else:
                continue
            short_forecast = period.get('shortForecast', '')
            if not short_forecast:
                continue
            is_day = period.get('isDaytime')
            if is_day is None:
                is_day = 'night' not in period.get('name', '').lower()
            if day not in days or (is_day and not days[day]['is_day']):
                days[day] = {'temp': temp_str, 'forecast': short_forecast, 'is_day': bool(is_day)}

        parts = []
        for day in sorted(days)[:num_days]:
            data = days[day]
            forecast_short = self.abbreviate_noaa(data['forecast'])
            if len(forecast_short) > 25:
                forecast_short = forecast_short[:22] + "..."
            abbrev = self._day_abbrev(day.weekday())
            parts.append(f"{abbrev}: {self.get_weather_emoji(data['forecast'])}{forecast_short} {data['temp']}")
        return parts

    def _day_abbrev(self, weekday: int) -> str:
        """The multi-day label for a weekday (0 = Monday), translated (English "M", "Th", "Sa")."""
        key = f'commands.wx.day_abbrev.{_WEEKDAY_NAMES[weekday]}'
        label = self.host.translate(key)
        return label if isinstance(label, str) and label and label != key else _DAY_ABBREVS[weekday]

    @staticmethod
    def _multiday_day_name(period_name_lower: str) -> str | None:
        """The weekday a NOAA period belongs to, or None to skip it.

        Part-of-day periods (Tonight, This Afternoon) count only when they name a
        weekday; otherwise Today and Tomorrow map to the current and next weekday.
        """
        for day in _WEEKDAYS_LOWER:
            if day in period_name_lower:
                return day.capitalize()
        if any(word in period_name_lower for word in ['tonight', 'afternoon', 'morning', 'evening']):
            return None
        if 'tomorrow' in period_name_lower:
            return (datetime.now() + timedelta(days=1)).strftime('%A')
        if 'today' in period_name_lower:
            return datetime.now().strftime('%A')
        return None

    def _add_period_details(self, period_str: str, detailed_forecast: str, current_weather_length: int, max_length: int = 130, observation_data: Optional[dict] = None) -> str:
        """Add additional details (humidity, dew point, visibility, etc.) to a period string

        Args:
            period_str: The base period string (e.g., " | Today: ☀️Sunny 75°")
            detailed_forecast: The detailed forecast text to extract info from
            current_weather_length: Current length of the weather string (to check total length)
            max_length: Maximum total length allowed (default 130)
            observation_data: Optional dict with observation station data (humidity, dew_point, visibility, wind_gusts, pressure)

        Returns:
            Updated period string with additional details if space allows
        """
        # Prefer the station observation (more accurate); fall back to parsing the forecast text.
        observed = observation_data or {}
        humidity = observed.get('humidity') or self.extract_humidity(detailed_forecast)
        dew_point = observed.get('dew_point') or self.extract_dew_point(detailed_forecast)
        visibility = observed.get('visibility') or self.extract_visibility(detailed_forecast)
        wind_gusts = observed.get('wind_gusts') or self._forecast_text_gusts(detailed_forecast)
        pressure = observed.get('pressure') or self.extract_pressure(detailed_forecast)
        # Precipitation probability only comes from the forecast text.
        precip_prob = self.extract_precip_probability(detailed_forecast)

        # Add each available detail, in this order, as long as the total still fits.
        result = period_str
        for value, template in (
            (humidity, " {}%RH"),
            (dew_point, " 💧{}°"),
            (visibility, " 👁️{}km" if self._noaa_metric_distance else " 👁️{}mi"),
            (precip_prob, " 🌦️{}%"),
            (wind_gusts, " 💨{}"),
            (pressure, " 📊{}hPa"),
        ):
            if value:
                piece = template.format(value)
                if self.host._count_display_width(result + piece) + current_weather_length <= max_length:
                    result += piece
        return result

    def compact_time(self, time_str: str) -> str:
        """Compact time format: '6:00AM' -> '6AM', 'December 16 at 3:12PM' -> 'Dec 16 3:12PM'
        Also handles ISO format: '2025-12-17T01:00:00-08:00' -> 'Dec 17 1AM'"""
        return alert_format.compact_time(time_str, self.host.response_translator)

    def abbreviate_wind_direction(self, direction: str) -> str:
        """Abbreviate wind direction to emoji + 2-3 characters"""
        if not direction:
            return ""

        direction = direction.upper()
        # NOAA sends 16-point abbreviations ("WNW"); keep them, with the nearest
        # 8-point arrow. Before, they fell through to the 2-character fallback
        # below, which turned "WNW" into "WN" and dropped the arrow.
        if direction in _COMPASS_16:
            arrow = _ARROWS_8[int(_COMPASS_16.index(direction) / 2 + 0.5) % 8]
            return f"{arrow}{self._wind_letters(direction)}"
        replacements = {
            "NORTHWEST": "NW",
            "NORTHEAST": "NE",
            "SOUTHWEST": "SW",
            "SOUTHEAST": "SE",
            "NORTH": "N",
            "EAST": "E",
            "SOUTH": "S",
            "WEST": "W"
        }

        for full, point in replacements.items():
            if full in direction:
                arrow = _ARROWS_8[_COMPASS_16.index(point) // 2]
                return f"{arrow}{self._wind_letters(point)}"

        # If no match, return first 2 characters with generic wind emoji
        return f"💨{direction[:2]}" if len(direction) >= 2 else f"💨{direction}"

    def _wind_letters(self, point: str) -> str:
        """A 16-point compass abbreviation in the reply's language ("NE" is "NO" in German), as gwx shows it."""
        key = f"common.wind_directions.{point}"
        label = self.host.translate(key)
        return label if isinstance(label, str) and label and label != key else point

    def extract_humidity(self, text: str) -> str:
        """Extract humidity percentage from forecast text"""
        return _first_match(text, _HUMIDITY_PATTERNS)

    def extract_precip_chance(self, text: str) -> str:
        """Extract precipitation chance from forecast text"""
        return _first_match(text, _PRECIP_CHANCE_PATTERNS)

    def extract_high_low(self, text: str, units_str: str = "°F") -> str:
        """Extract high/low temperatures from forecast text; format via [Weather] templates."""
        if not text:
            return ""

        # NOAA writes cold values as plain negatives ("Low around -5."); the ranges
        # cover the coldest and hottest forecasts it issues.
        def _single_ok(val: int) -> bool:
            if units_str == "°C":
                return -55 <= val <= 55
            return -65 <= val <= 130

        def _pair_ok(hi: int, lo: int) -> bool:
            return _single_ok(hi) and _single_ok(lo) and hi > lo

        pair_patterns = [
            r'high\s+near\s+(-?\d+).*?low\s+around\s+(-?\d+)',
            r'high\s+(-?\d+).*?low\s+(-?\d+)',
            r'(-?\d+)\s+to\s+(-?\d+)\s+degrees',
            r'temperature\s+(-?\d+)\s+to\s+(-?\d+)',
            r'high\s+near\s+(-?\d+).*?temperatures\s+falling\s+to\s+around\s+(-?\d+)',
        ]
        for pattern in pair_patterns:
            match = re.search(pattern, text.lower())
            if match and len(match.groups()) == 2:
                high, low = match.groups()
                try:
                    high_val = int(high)
                    low_val = int(low)
                    if _pair_ok(high_val, low_val):
                        return format_temperature_high_low(
                            self.host.bot.config, high_val, low_val, units_str, self.host.logger,
                            translator=self.host.response_translator,
                        )
                except ValueError:
                    continue

        low_match = re.search(r'low\s+around\s+(-?\d+)', text.lower())
        if low_match:
            try:
                low_val = int(low_match.group(1))
                if _single_ok(low_val):
                    return format_temperature_high_low(
                        self.host.bot.config, None, low_val, units_str, self.host.logger,
                        translator=self.host.response_translator,
                    )
            except ValueError:
                pass

        high_match = re.search(r'high\s+near\s+(-?\d+)', text.lower())
        if high_match:
            try:
                high_val = int(high_match.group(1))
                if _single_ok(high_val):
                    return format_temperature_high_low(
                        self.host.bot.config, high_val, None, units_str, self.host.logger,
                        translator=self.host.response_translator,
                    )
            except ValueError:
                pass

        return ""

    def extract_uv_index(self, text: str) -> str:
        """Extract UV index from forecast text"""
        return _first_match(text, _UV_INDEX_PATTERNS, 0, 15)

    def extract_dew_point(self, text: str) -> str:
        """Extract dew point temperature from forecast text"""
        return _first_match(text, _DEW_POINT_PATTERNS, -20, 80)

    def extract_visibility(self, text: str) -> str:
        """Extract visibility from forecast text"""
        return _first_match(text, _VISIBILITY_PATTERNS, 0, 20)

    def extract_precip_probability(self, text: str) -> str:
        """Extract precipitation probability from forecast text"""
        return _first_match(text, _PRECIP_PROBABILITY_PATTERNS, 0, 100)

    def extract_wind_gusts(self, text: str) -> str:
        """Extract wind gusts from forecast text"""
        return _first_match(text, _WIND_GUST_PATTERNS, 10, 100)

    def _forecast_text_gusts(self, text: str) -> str:
        """Gusts from the forecast text ("gusts up to 30 mph" or "48 km/h") in the configured unit."""
        mph = self.extract_wind_gusts(text)
        if mph:
            return self._noaa_wind_convert(mph, "mph")
        kmh = _first_match(text, _WIND_GUST_KMH_PATTERNS, 16, 161)
        if kmh:
            return self._noaa_wind_convert(kmh, "km/h")
        return ""

    def extract_pressure(self, text: str) -> str:
        """Extract barometric pressure from forecast text"""
        return _first_match(text, _PRESSURE_PATTERNS, 600, 1100)

    def get_observation_data(self, points_data: dict) -> dict:
        """Get observation station data from NOAA and return as a dict

        Returns:
            Dict with keys: humidity, dew_point, visibility, wind_gusts, pressure
            Values are strings ready for display, or None if not available
        """
        try:
            if not points_data:
                return {}

            weather_json = points_data
            station_url = weather_json['properties'].get('observationStations')
            if not station_url:
                return {}

            # Get the nearest station (with retry logic)
            # Use shorter timeout for optional observation data to avoid blocking main response
            obs_timeout = min(self.url_timeout, 5)  # Cap at 5 seconds for optional data
            try:
                stations_data = self.session.get(station_url, timeout=obs_timeout)
                if not stations_data.ok:
                    return {}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                return {}

            stations_json = stations_data.json()
            if not stations_json.get('features'):
                return {}

            # Get current observations from the nearest station (with retry logic)
            station_id = stations_json['features'][0]['properties']['stationIdentifier']
            obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"

            try:
                obs_data = self.session.get(obs_url, timeout=obs_timeout)
                if not obs_data.ok:
                    return {}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                return {}

            obs_json = obs_data.json()
            if not obs_json.get('properties'):
                return {}

            props = obs_json['properties']
            obs_data_dict = {}

            # Extract useful current conditions
            # Check for None explicitly to handle cases where value exists but is None
            humidity_val = props.get('relativeHumidity', {}).get('value')
            if humidity_val is not None:
                humidity = int(humidity_val)
                obs_data_dict['humidity'] = str(humidity)

            temp_unit, wind_unit = self._noaa_units()

            dewpoint_val = props.get('dewpoint', {}).get('value')
            if dewpoint_val is not None:
                if temp_unit == 'celsius':
                    dewpoint = round_half_away(dewpoint_val)
                else:
                    dewpoint = round_half_away(c_to_f(dewpoint_val))
                obs_data_dict['dew_point'] = str(dewpoint)

            visibility_val = props.get('visibility', {}).get('value')
            if visibility_val is not None and str(props.get('visibility', {}).get('unitCode', '')).endswith(':km'):
                visibility_val *= 1000  # NOAA normally reports meters; handle km too
            if visibility_val is not None:
                if self._noaa_metric_distance:
                    visibility = round_half_away(visibility_val / 1000)  # Convert m to km
                else:
                    visibility = round_half_away(visibility_val / M_PER_MILE)  # Convert m to miles
                if visibility > 0:
                    obs_data_dict['visibility'] = str(visibility)

            wind_gust_val = props.get('windGust', {}).get('value')
            if wind_gust_val is not None:
                # NOAA observations declare their unit; station gusts are usually km/h, not m/s.
                if 'km_h' in str(props.get('windGust', {}).get('unitCode', '')):
                    wind_gust_val = kmh_to(wind_gust_val, 'ms')
                # Shown only above 10 mph, whatever unit it is shown in.
                if round_half_away(ms_to(wind_gust_val, 'mph')) > 10:
                    obs_data_dict['wind_gusts'] = str(round_half_away(ms_to(wind_gust_val, wind_unit)))

            pressure_val = props.get('barometricPressure', {}).get('value')
            if pressure_val is not None:
                pressure = int(pressure_val / 100)  # Convert Pa to hPa
                obs_data_dict['pressure'] = str(pressure)

            return obs_data_dict

        except Exception as e:
            self.host.logger.debug(f"Error getting observation data: {e}")
            return {}

    def get_weather_emoji(self, condition: str) -> str:
        """Get emoji for weather condition"""
        if not condition:
            return ""

        condition_lower = condition.lower()

        # Weather condition emojis
        # Order matters: thunderstorms outrank everything, even sun ("Sunny then Slight
        # Chance Showers And Thunderstorms"); precipitation outranks cloud ("Chance Rain
        # Showers then Mostly Cloudy" is a rain forecast); and a specific phrase has to be
        # tested before a word it contains ("partly cloudy" before "cloudy").
        if any(word in condition_lower for word in ['thunderstorm', 't-storm']):
            return "⛈️"
        elif any(word in condition_lower for word in ['sunny', 'clear']):
            return "☀️"
        elif any(word in condition_lower for word in ['heavy rain', 'heavy showers', 'excessive rain']):
            return "🌧️"  # Cloud with rain - more rain, less sun
        elif any(word in condition_lower for word in ['rain', 'showers']):
            return "🌦️"
        elif any(word in condition_lower for word in ['snow', 'snow showers']):
            return "❄️"
        elif any(word in condition_lower for word in ['partly cloudy', 'mostly cloudy']):
            return "⛅"
        elif any(word in condition_lower for word in ['cloudy', 'overcast']):
            return "☁️"
        elif any(word in condition_lower for word in ['fog', 'mist', 'haze']):
            return "🌫️"
        elif any(word in condition_lower for word in ['smoke']) or any(word in condition_lower for word in ['windy', 'breezy']):
            return "💨"
        else:
            return "🌤️"  # Default weather emoji

    def _noaa_period_display_name(self, period: dict) -> str:
        """Return display label for a NOAA forecast period. Resolves holiday names to weekday."""
        name = period.get('name', '') or ''
        start_time_str = period.get('startTime')
        name_lower = name.lower()
        is_holiday = any(p in name_lower for p in _NOAA_HOLIDAY_NAME_PATTERNS)
        if is_holiday and start_time_str:
            try:
                # startTime is ISO 8601, e.g. 2025-02-17T08:00:00-08:00
                dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                # Python weekday(): Mon=0 .. Sun=6
                weekdays = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
                day_abbrev = weekdays[dt.weekday()]
                if 'night' in name_lower or 'overnight' in name_lower:
                    return f"{day_abbrev} Night"
                return day_abbrev
            except (ValueError, TypeError):
                pass
        return self.abbreviate_noaa(name)

    def abbreviate_noaa(self, text: str) -> str:
        """Replace long strings with shorter ones for display"""
        replacements = {
            "monday": "Mon",
            "tuesday": "Tue",
            "wednesday": "Wed",
            "thursday": "Thu",
            "friday": "Fri",
            "saturday": "Sat",
            "sunday": "Sun",
            "northwest": "NW",
            "northeast": "NE",
            "southwest": "SW",
            "southeast": "SE",
            "north": "N",
            "south": "S",
            "east": "E",
            "west": "W",
            "precipitation": "precip",
            "showers": "shwrs",
            "thunderstorms": "t-storms",
            "thunderstorm": "t-storm",
            "quarters": "qtrs",
            "quarter": "qtr",
            "january": "Jan",
            "february": "Feb",
            "march": "Mar",
            "april": "Apr",
            "may": "May",
            "june": "Jun",
            "july": "Jul",
            "august": "Aug",
            "september": "Sep",
            "october": "Oct",
            "november": "Nov",
            "december": "Dec",
            "degrees": "°",
            "percent": "%",
            "department": "Dept.",
            "amounts less than a tenth of an inch possible.": "< 0.1in",
            "temperatures": "temps.",
            "temperature": "temp.",
        }

        line = text
        for key, value in replacements.items():
            # Case insensitive replace
            line = line.replace(key, value).replace(key.capitalize(), value).replace(key.upper(), value)

        return line

    def get_current_conditions(self, points_data: dict) -> str:
        """Get additional current conditions data from NOAA using existing points data (legacy method)"""
        obs_data = self.get_observation_data(points_data)
        if not obs_data:
            return ""

        conditions = []

        # Build conditions list in priority order
        if 'humidity' in obs_data:
            conditions.append(f"{obs_data['humidity']}%RH")

        if 'dew_point' in obs_data:
            conditions.append(f"💧{obs_data['dew_point']}°")

        if 'visibility' in obs_data:
            conditions.append(f"👁️{obs_data['visibility']}{'km' if self._noaa_metric_distance else 'mi'}")

        if 'wind_gusts' in obs_data:
            conditions.append(f"💨{obs_data['wind_gusts']}")

        if 'pressure' in obs_data:
            conditions.append(f"📊{obs_data['pressure']}hPa")

        return " ".join(conditions[:3])  # Limit to 3 conditions to avoid overflow

