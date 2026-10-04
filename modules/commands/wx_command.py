#!/usr/bin/env python3
"""
Weather command for the MeshCore Bot
Provides weather information using zip codes and NOAA APIs
"""

import asyncio
import re
import threading
import xml.dom.minidom
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, ParamSpec, TypeVar

import requests

from .. import alert_format
from ..clients.mqtt_weather import (  # noqa: F401  re-exported
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)

# First-party modules with only required dependencies; they always import.
from ..clients.wxsim_parser import WXSIMParser
from ..http_retry import make_retry_session
from ..location import get_bot_lat_lon, get_companion_lat_lon
from ..models import MeshMessage
from ..nws_alerts import WX_SPECIAL_RULES, entry_nws_headline, entry_summary, entry_title, parse_alert_fields
from ..nws_coverage import NWSNoCoverageCache
from ..utils import (
    format_temperature_high_low,
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
)
from ..weather_common import _ARROWS_8, _COMPASS_16, WeatherCommandMixin
from .alternatives.wx_international import GlobalWxCommand
from .base_command import BaseCommand
from .rain_command import nws_http_means_no_coverage

# Kept for code that checked them; these imports used to be optional.
WX_INTERNATIONAL_AVAILABLE = True
WXSIM_PARSER_AVAILABLE = True

# Multiday: plain digits (e.g. 7), 7day/7-day, or suffix form 7d/10d (min 2, max below).
WX_MULTIDAY_MAX_DAYS = 16

_P = ParamSpec("_P")
_T = TypeVar("_T")


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


class WxCommand(WeatherCommandMixin, BaseCommand):
    """Handles weather commands with zipcode support"""

    # Plugin metadata
    # Read-only informational output; safe for scheduled {cmd:...} rendering.
    render_safe = True
    name = "wx"
    translation_ns = "commands.wx"
    keywords = ['wx', 'weather', 'wxa', 'wxalert']
    description = "Get weather information for a zip code (usage: wx 12345)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    # NOAA/geocoding need the network, but custom WXSIM/MQTT sources may be LAN-only; check connectivity inside execute paths.
    requires_internet = False

    # Documentation
    short_description = "Get weather for a US location using NOAA weather data"
    usage = "wx <zipcode|city> [tomorrow|<N>d|hourly|alerts]"
    examples = ["wx 98101", "wx seattle", "wx 90210 7d"]
    parameters = [
        {"name": "location", "description": "US zip code or city name"},
        {"name": "option", "description": "tomorrow, Nd (e.g. 7d, 10d), hourly, or alerts (optional)"}
    ]

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {
            "key": "temperature_unit",
            "label": "Temperature unit",
            "type": "enum",
            "options": [
                {"value": "fahrenheit", "label": "Fahrenheit (°F)"},
                {"value": "celsius", "label": "Celsius (°C)"},
            ],
            "default": "fahrenheit",
            "help": "Unit used when reporting temperatures.",
        },
        {
            "key": "wind_speed_unit",
            "label": "Wind speed unit",
            "type": "enum",
            "options": [
                {"value": "mph", "label": "Miles per hour (mph)"},
                {"value": "kmh", "label": "Kilometers per hour (km/h)"},
                {"value": "ms", "label": "Meters per second (m/s)"},
                {"value": "kn", "label": "Knots (kn)"},
            ],
            "default": "mph",
            "help": "Unit used when reporting wind speed.",
        },
        {"key": "weather_provider", "label": "Weather provider", "type": "enum", "section": "Weather",
         "options": [
             {"value": "noaa", "label": "NOAA (US, includes alerts)"},
             {"value": "openmeteo", "label": "Open-Meteo (global)"},
         ],
         "default": "noaa", "help": "API used for forecasts. Shared weather setting."},
        {"key": "default_city", "label": "Default city", "type": "str", "section": "Weather",
         "default": "", "help": "City used for a bare 'wx' when no location is given. Shared weather setting."},
        {"key": "default_state", "label": "Default state", "type": "str", "section": "Weather",
         "default": "", "help": "2-letter state for city disambiguation (e.g. WA). Shared weather setting."},
        {"key": "default_country", "label": "Default country", "type": "str", "section": "Weather",
         "default": "US", "help": "2-letter country code (e.g. US). Shared weather setting."},
        {"key": "always_show_location", "label": "Always name the location", "type": "bool", "section": "Weather",
         "default": False,
         "help": "Name the place in every wx/gwx reply a place is found for, not only when it is outside the "
                 "default state or country. Costs message length, and a reverse lookup for ZIP codes. "
                 "Shared weather setting."},
    ]

    # Error constants
    NO_DATA_NOGPS = "No GPS data available"
    ERROR_FETCHING_DATA = "Error fetching weather data"
    NO_ALERTS = "No weather alerts"

    # Floor for the forecast body once a location prefix has been reserved out of
    # the frame budget, so a long "City, State: " can never squeeze the forecast
    # down to nothing (or negative, which the -10/-20 arithmetic in the period
    # formatters would turn into dropped fields).
    MIN_BODY_BUDGET = 40

    def __init__(self, bot):
        super().__init__(bot)
        self.wx_enabled = self.get_config_value('Wx_Command', 'enabled', fallback=True, value_type='bool')

        self.wxsim_parser = WXSIMParser()

        # Check weather provider setting - delegate to international command if using Open-Meteo
        weather_provider = bot.config.get('Weather', 'weather_provider', fallback='noaa').lower()
        if weather_provider == 'openmeteo':
            # Delegate to international weather command
            self.delegate_command = GlobalWxCommand(bot)
            # Use wx triggers plus any [Wx_Command] aliases loaded by BaseCommand.
            self.delegate_command.keywords = list(self.keywords)
            self.delegate_command.description = "Get weather information for any location (usage: wx Tokyo)"
            self.logger.info("Weather provider set to 'openmeteo', delegating wx command to wx_international")
        else:
            self.delegate_command = None

        # Only initialize NOAA-specific attributes if not delegating
        if self.delegate_command is None:
            self.url_timeout = 8  # seconds (reduced from 10 for faster failure detection)
            self.forecast_duration = 3  # days
            self.num_wx_alerts = 2  # number of alerts to show
            self.use_metric = False  # Use imperial units by default
            self.zulu_time = False  # Use local time by default

            # Get default location/state/country from config for fallback/disambiguation
            self.default_city = self.bot.config.get('Weather', 'default_city', fallback='').strip()
            self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
            self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            self.always_show_location = self.bot.config.getboolean('Weather', 'always_show_location', fallback=False)

            # Initialize geocoder (will use rate-limited helpers for actual calls)
            # Keep geolocator for backwards compatibility, but prefer rate-limited helpers
            self.geolocator = get_nominatim_geocoder()

            # Get database manager for geocoding cache
            self.db_manager = bot.db_manager

            # Create a retry-enabled session for NOAA API calls
            # This makes the API more resilient to timeouts and transient errors
            self.noaa_session = self._create_retry_session()

            # requests.Session and WXSIM parser instances are shared by this command.
            # Keep provider calls serialized as they were before moving them to worker
            # threads. The lock is acquired inside the worker, never on the event loop.
            self._sync_provider_lock = threading.Lock()

            self._nws_no_coverage = NWSNoCoverageCache()

    def _unit_setting(self, key: str, fallback: str) -> str:
        """A unit setting: [Wx_Command] when it overrides, else the shared [Weather] one.

        Read directly rather than through get_config_value, whose [Weather] fallback
        logs a notice to move the setting into [Wx_Command]; for units, [Weather] is
        where they belong.
        """
        config = self.bot.config
        for section in ('Wx_Command', 'Weather'):
            if config.has_section(section) and config.has_option(section, key):
                return str(config.get(section, key)).strip().lower()
        return fallback

    def _noaa_units(self) -> tuple[str, str]:
        """Configured (temperature_unit, wind_speed_unit) for NOAA replies.

        Same lookup as the WXSIM path: [Wx_Command], falling back to [Weather].
        Invalid values fall back to fahrenheit/mph, as gwx does.
        """
        temp = self._unit_setting('temperature_unit', 'fahrenheit')
        wind = self._unit_setting('wind_speed_unit', 'mph')
        if temp not in ('fahrenheit', 'celsius'):
            temp = 'fahrenheit'
        if wind not in ('mph', 'kmh', 'ms', 'kn'):
            wind = 'mph'
        return temp, wind

    @property
    def _noaa_metric_distance(self) -> bool:
        """Kilometers instead of miles, following the temperature unit as gwx does."""
        return self._noaa_units()[0] == 'celsius'

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

    def _create_retry_session(self) -> requests.Session:
        """Create a requests session with retry logic for NOAA API calls"""
        return make_retry_session()

    def _run_sync_provider(
        self,
        operation: Callable[_P, _T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        """Run one shared synchronous provider operation under its worker lock."""
        with self._sync_provider_lock:
            return operation(*args, **kwargs)

    async def _run_sync_provider_async(
        self,
        operation: Callable[_P, _T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        """Move blocking provider work off-loop without concurrent Session use."""
        return await asyncio.to_thread(self._run_sync_provider, operation, *args, **kwargs)

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        """Get help text, delegating to international command if using Open-Meteo"""
        if self.delegate_command:
            return self.delegate_command.get_help_text()
        return self.translate('commands.wx.description')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a weather keyword"""
        if self.delegate_command:
            return self.delegate_command.matches_keyword(message)

        return self._cleaned_content_matches(
            message,
            lambda content_lower: any(
                content_lower.startswith(keyword + ' ') or content_lower == keyword
                for keyword in self.keywords
            ),
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Override to delegate or use base class cooldown"""
        # Check if wx command is enabled
        if not self.wx_enabled:
            return False

        if self.delegate_command:
            # Enforce [Wx_Command] channels first; delegate uses skip_channel_check
            # so [Wx_Command] channels override is honored when using Open-Meteo
            if not self.is_channel_allowed(message):
                return False
            return self.delegate_command.can_execute(message, skip_channel_check=True)

        # Use base class for cooldown and other checks
        return super().can_execute(message)

    def get_remaining_cooldown(self, user_id: Optional[str] = None) -> int:
        """Get remaining cooldown time for a specific user"""
        if self.delegate_command:
            return self.delegate_command.get_remaining_cooldown(user_id)

        # Use base class method
        return super().get_remaining_cooldown(user_id)

    def _get_companion_location(self, message: MeshMessage) -> Optional[tuple[float, float]]:
        """Get companion/sender location from the contact-tracking database."""
        return get_companion_lat_lon(self.bot, message, self.logger, error_level="warning", trace=True)

    def _get_bot_location(self) -> Optional[tuple[float, float]]:
        """Get bot location from config ([Bot] bot_latitude, bot_longitude)."""
        return get_bot_lat_lon(self.bot, self.logger)

    def _get_custom_wxsim_source(self, location: Optional[str] = None) -> Optional[str]:
        """Get custom WXSIM source URL from config.

        Looks for keys in [Weather] section with pattern: custom.wxsim.<name> = <url>
        Similar to how Channels_List handles dotted keys.

        Args:
            location: Location name or None for default source

        Returns:
            Optional[str]: Source URL or None if not found
        """

        section = 'Weather'
        if not self.bot.config.has_section(section):
            self.logger.debug(f"Config section '{section}' does not exist")
            return None

        if location:
            # Strip whitespace and normalize
            location = location.strip()
            location_lower = location.lower()
            self.logger.debug(f"Checking for WXSIM source for location: '{location}' (normalized: '{location_lower}')")

            # Look for keys matching custom.wxsim.<location> pattern
            prefix = 'custom.wxsim.'
            for key, value in self.bot.config.items(section):
                if key.startswith(prefix):
                    # Extract the location name from the key (e.g., "custom.wxsim.lethbridge" -> "lethbridge")
                    key_location = key[len(prefix):].strip()
                    if key_location.lower() == location_lower:
                        self.logger.debug(f"Found WXSIM source: {key} = {value}")
                        return value

            self.logger.debug(f"No WXSIM source found for location '{location}'")
        else:
            # Check for default source: custom.wxsim.default
            default_key = 'custom.wxsim.default'
            if self.bot.config.has_option(section, default_key):
                url = self.bot.config.get(section, default_key)
                self.logger.debug(f"Found default WXSIM source: {url}")
                return url
            self.logger.debug("No default WXSIM source configured")

        return None

    def _get_wxsim_weather(self, source_url: str, forecast_type: str = "default",
                                num_days: int = 7, message: MeshMessage = None,
                                location_name: Optional[str] = None) -> str:
        """Get and format weather from WXSIM source.

        Args:
            source_url: URL to WXSIM plaintext.txt file
            forecast_type: "default", "tomorrow", or "multiday"
            num_days: Number of days for multiday forecast
            message: The MeshMessage for dynamic length calculation
            location_name: Optional location name for display

        Returns:
            str: Formatted weather string
        """

        if forecast_type in ("hourly", "alerts"):
            return self.translate("commands.wx.source_option_not_available")

        # Fetch WXSIM data
        text = self.wxsim_parser.fetch_from_url(source_url, timeout=self.url_timeout)
        if not text:
            return self.translate('commands.wx.error', error="Failed to fetch WXSIM data")

        # Parse the data
        forecast = self.wxsim_parser.parse(text)

        # Validate forecast is not stale
        is_stale, stale_reason = self.wxsim_parser.is_forecast_stale(forecast, max_age_hours=48)
        if is_stale:
            self.logger.warning(f"WXSIM forecast appears stale: {stale_reason}")
            # Still return the forecast, but log the warning
            # Optionally, we could return an error message here instead

        # Get unit preferences from config. Canonical section is [Wx_Command];
        # get_config_value falls back to legacy [Weather] for existing setups.
        temp_unit = self._unit_setting('temperature_unit', 'fahrenheit')
        wind_unit = self._unit_setting('wind_speed_unit', 'mph')

        # Format based on forecast type
        if forecast_type == "tomorrow":
            # Get tomorrow's forecast
            if len(forecast.periods) > 1:
                tomorrow = forecast.periods[1]
                high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp is not None else None
                low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp is not None else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                result = f"Tomorrow: {tomorrow.conditions}"
                hl = self._format_high_low(high, low, temp_symbol)
                if hl:
                    result += f" {hl}"

                if tomorrow.precip_chance and tomorrow.precip_chance > 30:
                    result += f" {tomorrow.precip_chance}% PoP"

                if location_name:
                    return f"{location_name}: {result}"
                return result
            else:
                return self.translate('commands.wx.error', error="Tomorrow forecast not available")

        elif forecast_type == "multiday":
            # Format multiday forecast
            summary = self.wxsim_parser.format_forecast_summary(forecast, num_days, temp_unit, wind_unit)
            if location_name:
                return f"{location_name}:\n{summary}"
            return summary

        else:
            # Default: current conditions + today's forecast
            current = self.wxsim_parser.format_current_conditions(forecast, temp_unit, wind_unit)

            # Add today's high/low if available (use first period as "today")
            if forecast.periods:
                today = forecast.periods[0]
                high = self.wxsim_parser._convert_temp(today.high_temp, temp_unit) if today.high_temp is not None else None
                low = self.wxsim_parser._convert_temp(today.low_temp, temp_unit) if today.low_temp is not None else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                hl_today = self._format_high_low(high, low, temp_symbol)
                if hl_today:
                    current += f" | {hl_today}"

                # Add tomorrow if available (second period)
                if len(forecast.periods) > 1:
                    tomorrow = forecast.periods[1]
                    tomorrow_high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp is not None else None
                    tomorrow_low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp is not None else None

                    hl_tom = self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
                    if hl_tom:
                        current += f" | Tomorrow: {hl_tom}"

            if location_name:
                return f"{location_name}: {current}"
            return current

    async def _fetch_wxsim(
        self, source: str, forecast_type: str, num_days: int, message: MeshMessage, location: Optional[str]
    ) -> str:
        if location is None:
            return await self._get_wxsim_weather_async(source, forecast_type, num_days, message)
        return await self._get_wxsim_weather_async(
            source, forecast_type, num_days, message, location_name=location
        )

    async def _fallback_place_name(self, lat: float, lon: float) -> Optional[str]:
        return await self._coordinates_to_location_string_async(lat, lon)

    async def _get_wxsim_weather_async(
        self,
        source_url: str,
        forecast_type: str = "default",
        num_days: int = 7,
        message: MeshMessage = None,
        location_name: Optional[str] = None,
    ) -> str:
        return await self._run_sync_provider_async(
            self._get_wxsim_weather,
            source_url,
            forecast_type,
            num_days,
            message,
            location_name,
        )

    def _coordinates_to_location_string(self, lat: float, lon: float) -> Optional[str]:
        """Convert coordinates to a location string (city name) using reverse geocoding.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            Optional[str]: Location string (city name) or None if geocoding fails.
        """
        try:
            from ..utils import rate_limited_nominatim_reverse_sync
            result = rate_limited_nominatim_reverse_sync(self.bot, f"{lat}, {lon}", timeout=10)
            if result and hasattr(result, 'raw'):
                # Extract city name from address
                address = result.raw.get('address', {})
                city = (address.get('city') or
                       address.get('town') or
                       address.get('village') or
                       address.get('municipality') or
                       address.get('county', ''))
                state = address.get('state', '')

                # Normalize state to abbreviation
                if state:
                    state_abbr, _ = normalize_us_state(state)
                    if state_abbr:
                        state = state_abbr

                if city:
                    if state:
                        return f"{city}, {state}"
                    return city
            return None
        except Exception as e:
            self.logger.debug(f"Error reverse geocoding coordinates {lat}, {lon}: {e}")
            return None

    async def _coordinates_to_location_string_async(
        self, lat: float, lon: float
    ) -> Optional[str]:
        return await self._run_sync_provider_async(
            self._coordinates_to_location_string, lat, lon
        )

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the weather command"""
        # Delegate to international command if using Open-Meteo provider
        if self.delegate_command:
            return await self.delegate_command.execute(message)

        content = message.content.strip()

        # Parse the command to extract location and forecast type
        # Support formats: "wx 12345", "wx seattle", "wx paris, tx", "weather everett", "wxa bellingham"
        # New formats: "wx 12345 tomorrow", "wx 12345 7", "wx 12345 7d", "wx 12345 7day", "wx 12345 alerts"
        parts = content.split()

        # Track if we're using companion location (so we always show location in response)
        using_companion_location = False

        # An option alone ("wx hourly") applies to the no-location fallbacks below.
        parts, option_word, option_type, option_days = self._split_option_only(parts, WX_MULTIDAY_MAX_DAYS)

        # If no location specified, check custom MQTT then WXSIM default sources
        if len(parts) < 2:
            mqtt_topic = self._get_custom_mqtt_weather_topic(None)
            if mqtt_topic:
                return await self._reply_from_mqtt(message, mqtt_topic, option_type, None, split_multiday=False)

            wxsim_source = self._get_custom_wxsim_source(None)  # Check for default
            if wxsim_source:
                return await self._reply_from_wxsim(message, wxsim_source, option_type, option_days)

            # No custom source: the sender's position, default city, then the bot's position
            location_str, using_companion_location = await self._no_location_fallback(message)
            if location_str is None:
                await self.send_response(message, self.translate('commands.wx.usage'))
                return True
            parts = [parts[0], location_str]

        if option_word:
            parts.append(option_word)

        # Check for "alerts" keyword first (special handling)
        show_full_alerts = False
        if len(parts) > 2 and parts[-1].lower() == "alerts":
            show_full_alerts = True
            location_parts = parts[1:-1]  # Remove "alerts" from location
        else:
            location_parts = parts[1:]

        forecast_type = "alerts" if show_full_alerts else "default"
        num_days = 7  # Default for multi-day forecast
        if not show_full_alerts and not (len(parts) == 2 and self._is_custom_source_name(parts[1])):
            location_parts, forecast_type, num_days = self._parse_forecast_suffix(
                location_parts, WX_MULTIDAY_MAX_DAYS, allow_hourly=True
            )

        # Join remaining parts to handle "city, state" format
        location = ' '.join(location_parts).strip()

        if not location:
            await self.send_response(message, self.translate('commands.wx.usage'))
            return True

        # Custom MQTT before WXSIM
        mqtt_topic = self._get_custom_mqtt_weather_topic(location)
        if mqtt_topic:
            self.logger.info(f"Using custom MQTT weather topic for location '{location}': {mqtt_topic}")
            return await self._reply_from_mqtt(message, mqtt_topic, forecast_type, location)

        # Check for custom WXSIM source first (before checking location type)
        wxsim_source = self._get_custom_wxsim_source(location)
        if wxsim_source:
            self.logger.info(f"Using custom WXSIM source for location '{location}': {wxsim_source}")
            return await self._reply_from_wxsim(message, wxsim_source, forecast_type, num_days, location)
        self.logger.debug(f"No custom WXSIM source found for location '{location}', using normal weather API")

        # Check if it's coordinates, zipcode, or city name
        if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
            # It's coordinates (lat,lon format)
            location_type = "coordinates"
        elif re.match(r'^\s*\d{5}\s*$', location):
            # It's a zipcode (allow surrounding whitespace; strip later in geocode)
            location_type = "zipcode"
        else:
            # It's a city name (possibly with state)
            location_type = "city"

        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Special handling for "alerts" command
            if show_full_alerts:
                # Get alerts only (no weather forecast)
                return await self._send_alert_list_for(message, location, location_type)

            # Get weather data for the location
            weather_data = await self.get_weather_for_location(location, location_type, forecast_type, num_days, message, using_companion_location=using_companion_location)

            return await self._send_weather_reply(message, weather_data, forecast_type)

        except Exception as e:
            self.logger.error(f"Error in weather command: {e}")
            await self.send_response(message, self.translate('commands.wx.error', error=str(e)))
            return True

    async def _send_alert_list_for(self, message: MeshMessage, location: str, location_type: str) -> bool:
        """The "wx alerts" reply: geocode the location, then send its full alert list."""
        lat, lon = None, None
        if location_type == "coordinates":
            try:
                lat_str, lon_str = location.split(',')
                lat = float(lat_str.strip())
                lon = float(lon_str.strip())
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    await self.send_response(message, self.translate('commands.wx.error', error="Invalid coordinates"))
                    return True
            except ValueError:
                await self.send_response(message, self.translate('commands.wx.error', error=f"Invalid coordinates format: {location}"))
                return True
        elif location_type == "zipcode":
            lat, lon = await self._zipcode_to_lat_lon_async(location)
            if lat is None or lon is None:
                await self.send_response(message, self.translate('commands.wx.no_location_zipcode', location=location))
                return True
        else:  # city
            result = await self._city_to_lat_lon_async(location)
            if len(result) == 3:
                lat, lon, address_info = result
            else:
                lat, lon = result
            if lat is None or lon is None:
                region = self.default_state or self.default_country
                await self.send_response(message, self.translate('commands.wx.no_location_city', location=location, state=region))
                return True

        # Get and display full alert list
        return await self._send_full_alert_list(message, lat, lon)

    async def get_weather_for_location(self, location: str, location_type: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, using_companion_location: bool = False) -> str:
        """Run the ordered synchronous geocode/NOAA workflow off the event loop."""
        return await self._run_sync_provider_async(
            self._get_weather_for_location_sync,
            location,
            location_type,
            forecast_type,
            num_days,
            message,
            using_companion_location,
        )

    def _get_weather_for_location_sync(self, location: str, location_type: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, using_companion_location: bool = False) -> str:
        """Get weather data for a location (coordinates, zipcode, or city)

        Args:
            location: The location (coordinates "lat,lon", zipcode, or city name)
            location_type: "coordinates", "zipcode", or "city"
            forecast_type: "default", "tomorrow", "multiday", or "hourly"
            num_days: Number of days for multiday forecast (2–16)
            message: The MeshMessage for dynamic length calculation
            using_companion_location: If True, always include location prefix even if same state
        """
        try:
            # Convert location to lat/lon based on type, with the location prefix the reply carries
            if location_type == "coordinates":
                reply, lat, lon, location_prefix = self._locate_coordinates(location)
            elif location_type == "zipcode":
                reply, lat, lon, location_prefix = self._locate_zipcode(location, using_companion_location)
            else:  # city
                # An unrecognized type geocodes as a city but never carries the city prefix
                reply, lat, lon, location_prefix = self._locate_city(
                    location, using_companion_location, labeled=location_type == "city"
                )
            if reply is not None:
                return reply

            # Get max message length dynamically
            max_length = self.get_max_message_length(message) if message else 130

            # location_prefix is prepended to the formatted body below, so it has to
            # come out of the same frame budget. Formatting the body to the full
            # budget and then prefixing it overruns the frame by the prefix's own
            # length -- "Lockhart, Texas: " alone is 17 bytes, enough to push an
            # emoji-dense forecast past the 160-byte firmware limit. The alert text
            # goes out as its own message and keeps the full budget.
            body_max_length = max(
                max_length - self._count_display_width(location_prefix), self.MIN_BODY_BUDGET
            )

            reply, weather = self._noaa_forecast_body(lat, lon, forecast_type, num_days, body_max_length)
            if reply is not None:
                return reply

            # Get weather alerts (only for default forecast type to avoid cluttering)
            if forecast_type == "default":
                return self._with_noaa_alerts(lat, lon, location_prefix, weather, max_length)

            return f"{location_prefix}{weather}"

        except Exception as e:
            self.logger.error(f"Error getting weather for {location_type} {location}: {e}")
            return self.translate('commands.wx.error', error=str(e))

    def _locate_coordinates(self, location: str) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a "lat,lon" location."""
        # Parse coordinates from "lat,lon" format
        try:
            lat_str, lon_str = location.split(',')
            lat = float(lat_str.strip())
            lon = float(lon_str.strip())

            # Validate coordinate ranges
            if not (-90 <= lat <= 90):
                return self.translate('commands.wx.error', error=f"Invalid latitude: {lat}"), None, None, ""
            if not (-180 <= lon <= 180):
                return self.translate('commands.wx.error', error=f"Invalid longitude: {lon}"), None, None, ""

            # Get address_info for location display via reverse geocoding
            location_str = self._coordinates_to_location_string(lat, lon)
            if location_str:
                # Parse the location string to get city and state for address_info
                parts = location_str.split(',')
                if len(parts) >= 2:
                    city = parts[0].strip()
                    state = parts[1].strip()
                    address_info = {'city': city, 'state': state}
                else:
                    address_info = {'city': location_str}
            else:
                address_info = {}
        except ValueError:
            return self.translate('commands.wx.error', error=f"Invalid coordinates format: {location}"), None, None, ""

        location_prefix = ""
        if address_info:
            # For coordinates, always show location if we have address info
            city = address_info.get('city', '')
            state = address_info.get('state', '')
            if city and state:
                # Normalize state to abbreviation
                state_abbr, _ = normalize_us_state(state)
                if state_abbr:
                    state = state_abbr
                location_prefix = f"{city}, {state}: "
            elif city:
                location_prefix = f"{city}: "
        return None, lat, lon, location_prefix

    def _locate_zipcode(self, location: str, using_companion_location: bool) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a zipcode location."""
        lat, lon = self.zipcode_to_lat_lon(location)
        if lat is None or lon is None:
            return self.translate('commands.wx.no_location_zipcode', location=location), None, None, ""

        location_prefix = ""
        if using_companion_location or self.always_show_location:
            # For zipcode with companion location, try to get city name from reverse geocoding
            location_str = self._coordinates_to_location_string(lat, lon)
            if location_str:
                location_prefix = f"{location_str}: "
        return None, lat, lon, location_prefix

    def _locate_city(self, location: str, using_companion_location: bool, labeled: bool = True) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a city location."""
        result = self.city_to_lat_lon(location)
        if len(result) == 3:
            lat, lon, address_info = result
        else:
            lat, lon = result
            address_info = None

        if lat is None or lon is None:
            region = self.default_state or self.default_country
            return self.translate('commands.wx.no_location_city', location=location, state=region), None, None, ""

        # Check if the found city is in a different state than default
        actual_city = location
        actual_state = self.default_state or self.default_country
        if address_info:
            # Try to get the best city name from various address fields
            actual_city = (address_info.get('city') or
                         address_info.get('town') or
                         address_info.get('village') or
                         address_info.get('hamlet') or
                         address_info.get('municipality') or
                         location)
            actual_state = address_info.get('state', self.default_state)
            # Convert full state name to abbreviation if needed using the us library
            if len(actual_state) > 2:
                state_abbr, _ = normalize_us_state(actual_state)
                if state_abbr:
                    actual_state = state_abbr

            # Also check if the default state needs to be converted for comparison
            default_state_full = self.default_state
            if len(self.default_state) == 2:
                # Convert abbreviation to full name for comparison
                _, default_state_full = normalize_us_state(self.default_state)
                if not default_state_full:
                    default_state_full = self.default_state

        # Add location info if city is in a different state than default, or if using companion location
        location_prefix = ""
        if labeled and address_info:
            # Compare states (handle both full names and abbreviations)
            states_different = (actual_state != self.default_state and
                              actual_state != default_state_full)
            # Always show location if using companion location, or if state is different
            if using_companion_location or states_different or self.always_show_location:
                location_prefix = f"{actual_city}, {actual_state}: " if actual_state else f"{actual_city}: "
        return None, lat, lon, location_prefix

    def _noaa_forecast_body(self, lat: float, lon: float, forecast_type: str, num_days: int, body_max_length: int) -> tuple:
        """(error reply or None, formatted forecast) for one forecast type."""
        # Get weather forecast based on type
        if forecast_type == "tomorrow":
            forecast_periods, points_data = self.get_noaa_weather(lat, lon, return_periods=True, max_length=body_max_length)
            if forecast_periods == self.ERROR_FETCHING_DATA:
                return self.translate('commands.wx.error_fetching'), None
            weather = self.format_tomorrow_forecast(forecast_periods, max_length=body_max_length)
        elif forecast_type == "multiday":
            forecast_periods, points_data = self.get_noaa_weather(lat, lon, return_periods=True, max_length=body_max_length)
            if forecast_periods == self.ERROR_FETCHING_DATA:
                return self.translate('commands.wx.error_fetching'), None
            weather = self.format_multiday_forecast(forecast_periods, num_days, max_length=body_max_length)
        elif forecast_type == "hourly":
            hourly_periods, points_data = self.get_noaa_hourly_weather(lat, lon)
            if hourly_periods == self.ERROR_FETCHING_DATA:
                return self.translate('commands.wx.error_fetching'), None
            weather = self.format_hourly_forecast(hourly_periods, max_length=body_max_length)
        else:  # default
            weather, points_data = self.get_noaa_weather(lat, lon, max_length=body_max_length)
            if weather == self.ERROR_FETCHING_DATA:
                return self.translate('commands.wx.error_fetching'), None

            # Note: Current conditions are now integrated directly into the current period
            # via _add_period_details() using observation station data
        return None, weather

    def _with_noaa_alerts(self, lat: float, lon: float, location_prefix: str, weather: str, max_length: int) -> str | tuple:
        """The default forecast reply, as a two-message tuple when NOAA has active alerts."""
        alerts_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=False)
        if alerts_result == self.ERROR_FETCHING_DATA or alerts_result == self.NO_ALERTS:
            pass
        else:
            full_alert_text, abbreviated_alert_text, alert_count = alerts_result
            if alert_count > 0:
                # Get full alert data for prioritized formatting
                alerts_full_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=True)
                if alerts_full_result not in [self.ERROR_FETCHING_DATA, self.NO_ALERTS]:
                    alerts_list, _ = alerts_full_result
                    # Format with prioritization and summary
                    formatted_alert_text = self._format_alerts_compact_summary(alerts_list, alert_count, max_length=max_length)
                else:
                    # Fallback to old format
                    formatted_alert_text = full_alert_text

                # Always send weather first, then alerts in separate message
                self.logger.info(f"Found {alert_count} alerts - using two-message mode")
                return ("multi_message", f"{location_prefix}{weather}", formatted_alert_text, alert_count)

        return f"{location_prefix}{weather}"

    def zipcode_to_lat_lon(self, zipcode: str) -> tuple:
        """Convert zipcode to latitude and longitude"""
        try:
            lat, lon = geocode_zipcode_sync(self.bot, zipcode, timeout=10)
            return lat, lon
        except Exception as e:
            self.logger.error(f"Error geocoding zipcode {zipcode}: {e}")
            return None, None

    async def _zipcode_to_lat_lon_async(self, zipcode: str) -> tuple:
        return await self._run_sync_provider_async(self.zipcode_to_lat_lon, zipcode)

    def city_to_lat_lon(self, city: str) -> tuple:
        """Convert city name to latitude and longitude using default state"""
        try:
            # Use shared geocode_city_sync function with address info
            default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            lat, lon, address_info = geocode_city_sync(
                self.bot, city, default_state=self.default_state,
                default_country=default_country,
                include_address_info=True, timeout=10
            )

            # Explicit None checks: 0 is a valid latitude (equator) or longitude (prime meridian).
            if lat is not None and lon is not None:
                return lat, lon, address_info or {}
            else:
                return None, None, None
        except Exception as e:
            self.logger.error(f"Error geocoding city {city}: {e}")
            return None, None, None

    async def _city_to_lat_lon_async(self, city: str) -> tuple:
        return await self._run_sync_provider_async(self.city_to_lat_lon, city)

    def _noaa_fetch(self, url: str, what: str):
        """GET *url* through the NOAA session; None, after a warning naming *what*,
        on an HTTP error status, a timeout or a connection error.
        """
        try:
            response = self.noaa_session.get(url, timeout=self.url_timeout)
            if not response.ok:
                self.logger.warning(f"Error fetching {what} from NOAA: HTTP {response.status_code}")
                return None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self.logger.warning(f"Timeout/connection error fetching {what} from NOAA: {e}")
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
            weather_data = self._noaa_fetch(weather_api, "weather data")
            if weather_data is None:
                return self.ERROR_FETCHING_DATA, None

            weather_json = weather_data.json()
            forecast_url = weather_json['properties']['forecast']

            # Get the forecast (with retry logic)
            forecast_data = self._noaa_fetch(self._noaa_units_url(forecast_url), "weather forecast")
            if forecast_data is None:
                return self.ERROR_FETCHING_DATA, None

            forecast_json = forecast_data.json()
            forecast = forecast_json['properties']['periods']

            # If return_periods is True, return the periods array directly
            if return_periods:
                if not forecast:
                    return self.ERROR_FETCHING_DATA, None
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
            self.logger.error(f"Error fetching NOAA weather: {e}")
            return self.ERROR_FETCHING_DATA, None

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
        if precip_chance and self._count_display_width(weather) < current_period_max:
            weather += f" 🌦️{precip_chance}%"

        # Also add UV index if available (not in helper function)
        uv_index = self.extract_uv_index(detailed_forecast)
        if uv_index and self._count_display_width(weather) < current_period_max:
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
            current_weather_len = self._count_display_width(weather)
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
                if current_weather_len < max_length - 10 and self._count_display_width(weather + period_str) <= max_chars:
                    weather += period_str
            else:
                # For non-night periods, use the stricter check
                if current_weather_len < max_length - 20 and self._count_display_width(weather + period_str) <= max_chars:
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
            current_weather_len = self._count_display_width(weather)
            # Only add details to additional periods if current period is under max_length - 20 chars
            # This ensures we prioritize current period details first
            if current_weather_len < max_length - 20:
                period_str = self._add_period_details(period_str, period_detailed, current_weather_len, max_length=max_length)

            # Only add if we have space (using display width)
            # Be more conservative - only add if current period is reasonable length
            if current_weather_len < max_length - 20 and self._count_display_width(weather + period_str) <= max_length:
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
            if self._count_display_width(test_str) < threshold:
                wind_match = re.search(r'(\d+)', period_wind_speed)
                if wind_match:
                    wind_num = self._noaa_wind_convert(wind_match.group(1), period_wind_speed)
                    wind_dir = self.abbreviate_wind_direction(period_wind_direction)
                    if wind_dir:
                        wind_info = f" {wind_dir}{wind_num}"
                        if self._count_display_width(test_str + wind_info) <= max_length:
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
            weather_data = self._noaa_fetch(weather_api, "weather data")
            if weather_data is None:
                return self.ERROR_FETCHING_DATA, None

            weather_json = weather_data.json()
            hourly_forecast_url = weather_json['properties'].get('forecastHourly')

            if not hourly_forecast_url:
                self.logger.warning("Hourly forecast not available for this location")
                return self.ERROR_FETCHING_DATA, None

            # Get the hourly forecast (with retry logic)
            hourly_data = self._noaa_fetch(self._noaa_units_url(hourly_forecast_url), "hourly forecast")
            if hourly_data is None:
                return self.ERROR_FETCHING_DATA, None

            hourly_json = hourly_data.json()
            hourly_periods = hourly_json['properties']['periods']

            if not hourly_periods:
                self.logger.warning("No hourly periods returned from NOAA")
                return self.ERROR_FETCHING_DATA, None

            return hourly_periods, weather_json

        except Exception as e:
            self.logger.error(f"Error fetching NOAA hourly weather: {e}")
            return self.ERROR_FETCHING_DATA, None

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
                return self.translate('commands.wx.hourly_not_available')

            future_periods = self._future_hourly_periods(hourly_periods)
            if not future_periods:
                return "No future hourly periods available"

            return self._pack_hourly_lines(
                (self._hourly_line(period) for period in future_periods), max_length
            )

        except Exception as e:
            self.logger.error(f"Error formatting hourly forecast: {e}")
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
        time_str = self._hour_label(period.get('startTime', ''))
        emoji = self.get_weather_emoji(short_forecast)

        forecast_short = self._short_hourly_description(short_forecast)

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
                wind_dir_abbrev = self._without_arrow(self.abbreviate_wind_direction(wind_direction))
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
                return self.translate('commands.wx.tomorrow_not_available')

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
                return self.translate('commands.wx.tomorrow_not_available')

            return self._fit_tomorrow_parts(parts, max_length)

        except Exception as e:
            self.logger.error(f"Error formatting tomorrow forecast: {e}")
            return self.translate('commands.wx.tomorrow_error')

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
            if self._count_display_width(text) <= max_length:
                return text
        return bare[0]

    def format_multiday_forecast(self, forecast: list, num_days: int = 7, max_length: int = 130) -> str:
        """Format a less detailed multi-day forecast summary"""
        try:
            if any(self._period_date(p) for p in forecast):
                parts = self._multiday_lines_by_date(forecast, num_days)
                if not parts:
                    return self.translate('commands.wx.multiday_not_available', num_days=num_days)
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
                return self.translate('commands.wx.multiday_not_available', num_days=num_days)

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
                return self.translate('commands.wx.multiday_not_available', num_days=num_days)

            # Join with newlines instead of pipes
            result = "\n".join(parts)

            return result

        except Exception as e:
            self.logger.error(f"Error formatting {num_days}-day forecast: {e}")
            return self.translate('commands.wx.multiday_error', num_days=num_days)

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
        label = self.translate(key)
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

    def _add_period_details(self, period_str: str, detailed_forecast: str, current_weather_length: int, max_length: int = 130, observation_data: dict = None) -> str:
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
                if self._count_display_width(result + piece) + current_weather_length <= max_length:
                    result += piece
        return result

    def get_weather_alerts_noaa(self, lat: float, lon: float, return_full_data: bool = False) -> tuple:
        """Get weather alerts from NOAA with full metadata extraction and prioritization

        Args:
            lat: Latitude
            lon: Longitude
            return_full_data: If True, return list of alert dicts instead of formatted strings

        Returns:
            If return_full_data=False: (full_first_alert_text, abbreviated_first_alert_text, alert_count)
            If return_full_data=True: (list of alert dicts, alert_count)
        """
        try:
            if self._nws_no_coverage.is_unavailable(lat, lon):
                self.logger.debug("Skipping NWS weather alerts for cached point %s,%s outside NWS coverage", round(lat, 2), round(lon, 2))
                return self.ERROR_FETCHING_DATA

            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            alert_url = f"https://api.weather.gov/alerts/active.atom?point={lat_rounded},{lon_rounded}"

            try:
                alert_data = self.noaa_session.get(alert_url, timeout=self.url_timeout)
                if not alert_data.ok:
                    if nws_http_means_no_coverage(alert_data.status_code):
                        if self._nws_no_coverage.mark_unavailable(lat, lon):
                            self.logger.warning(
                                "NWS weather alerts unavailable (HTTP %s); NOAA alerts are US-only; "
                                "point %s,%s is outside NWS coverage",
                                alert_data.status_code, round(lat, 2), round(lon, 2),
                            )
                    else:
                        self.logger.warning(
                            f"Error fetching weather alerts from NOAA: HTTP {alert_data.status_code}"
                        )
                    return self.ERROR_FETCHING_DATA
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching weather alerts from NOAA: {e}")
                return self.ERROR_FETCHING_DATA

            self._nws_no_coverage.mark_available(lat, lon)

            alerts = []  # Store structured alert data
            alertxml = xml.dom.minidom.parseString(alert_data.text)

            for entry in alertxml.getElementsByTagName("entry"):
                try:
                    title = entry_title(entry)
                    summary = entry_summary(entry)
                    nws_headline = entry_nws_headline(entry)
                    alerts.append(parse_alert_fields(entry, title, summary, nws_headline, WX_SPECIAL_RULES))

                except Exception as e:
                    self.logger.warning(f"Error parsing alert entry: {e}")
                    # Fallback: just use title
                    if title:
                        alerts.append({
                            'title': title,
                            'summary': '',
                            'nws_headline': '',
                            'event': title.split()[0] if title else "",
                            'event_type': 'Unknown',
                            'severity': 'Unknown',
                            'urgency': 'Unknown',
                            'certainty': 'Unknown',
                            'effective': '',
                            'expires': '',
                            'area_desc': '',
                            'office': ''
                        })

            if not alerts:
                return self.NO_ALERTS

            # Post-process alerts to differentiate duplicate Special Statements
            # If multiple statements have the same event, add distinguishing details
            alerts = self._differentiate_duplicate_statements(alerts)

            # Prioritize alerts using hybrid scoring
            alerts = self._prioritize_alerts(alerts)

            if return_full_data:
                return alerts, len(alerts)

            # Format for compact display (backward compatibility)
            # Return first alert formatted, plus count
            first_alert = alerts[0]
            full_first_alert_text = self._format_alert_compact(first_alert, include_details=True)
            abbreviated_first_alert_text = self._format_alert_compact(first_alert, include_details=False)

            return full_first_alert_text, abbreviated_first_alert_text, len(alerts)

        except Exception as e:
            self.logger.error(f"Error fetching NOAA weather alerts: {e}")
            return self.ERROR_FETCHING_DATA

    async def _get_weather_alerts_noaa_async(
        self, lat: float, lon: float, return_full_data: bool = False
    ) -> tuple:
        return await self._run_sync_provider_async(
            self.get_weather_alerts_noaa,
            lat,
            lon,
            return_full_data,
        )


    def _differentiate_duplicate_statements(self, alerts: list) -> list:
        """Differentiate Special Statements that have the same event type by adding unique details

        Args:
            alerts: List of alert dicts

        Returns:
            List of alerts with differentiated event names for duplicate statements
        """
        # Group alerts by event type and event name
        statement_groups = {}
        for alert in alerts:
            if alert.get('event_type') == 'Statement':
                event = alert.get('event', 'Special')
                if event not in statement_groups:
                    statement_groups[event] = []
                statement_groups[event].append(alert)

        # For each group with multiple statements, differentiate them
        for event, group in statement_groups.items():
            if len(group) > 1:
                # Multiple statements with same event - need to differentiate
                for i, alert in enumerate(group):
                    nws_headline = alert.get('nws_headline', '')
                    summary = alert.get('summary', '')
                    effective = alert.get('effective', '')

                    # Try to extract unique distinguishing details
                    distinguishing_detail = ""

                    # Strategy 1: Extract unique keywords from headline that aren't in other headlines
                    if nws_headline:
                        headline_lower = nws_headline.lower()
                        # Look for unique time references
                        if 'today' in headline_lower or 'now' in headline_lower:
                            distinguishing_detail = " (Today)"
                        elif 'week' in headline_lower or 'past week' in headline_lower:
                            distinguishing_detail = " (Week)"
                        elif 'continues' in headline_lower or 'remains' in headline_lower:
                            distinguishing_detail = " (Ongoing)"

                        # Look for unique severity/impact words
                        if not distinguishing_detail:
                            if 'increased' in headline_lower or 'increasing' in headline_lower:
                                distinguishing_detail = " (Increased)"
                            elif 'new' in headline_lower:
                                distinguishing_detail = " (New)"
                            elif 'update' in headline_lower:
                                distinguishing_detail = " (Update)"

                    # Strategy 2: Use timing to differentiate (morning vs afternoon vs evening)
                    if not distinguishing_detail and effective:
                        try:
                            from datetime import datetime
                            # Try to parse effective time
                            if 'T' in effective:
                                dt = datetime.fromisoformat(effective.replace('Z', '+00:00'))
                                hour = dt.hour
                                if 5 <= hour < 12:
                                    distinguishing_detail = " (AM)"
                                elif 12 <= hour < 17:
                                    distinguishing_detail = " (PM)"
                                elif 17 <= hour < 21:
                                    distinguishing_detail = " (Eve)"
                                else:
                                    distinguishing_detail = " (Night)"
                        except:
                            pass

                    # Strategy 3: Extract unique topic from summary if headline didn't help
                    if not distinguishing_detail and summary:
                        summary_lower = summary.lower()
                        # Look for secondary topics that might be unique
                        # Check for specific locations, conditions, or impacts
                        if 'burn' in summary_lower or 'burned area' in summary_lower:
                            distinguishing_detail = " (Burn)"
                        elif 'coastal' in summary_lower:
                            distinguishing_detail = " (Coastal)"
                        elif 'urban' in summary_lower:
                            distinguishing_detail = " (Urban)"
                        elif 'mountain' in summary_lower or 'cascade' in summary_lower:
                            distinguishing_detail = " (Mtn)"

                    # Strategy 4: Use index as last resort (but make it subtle)
                    if not distinguishing_detail:
                        distinguishing_detail = f" ({i+1})"

                    # Update the event name with distinguishing detail
                    alert['event'] = event + distinguishing_detail

        return alerts

    def _prioritize_alerts(self, alerts: list) -> list:
        """Prioritize alerts using hybrid scoring system

        Scoring:
        - Severity: Extreme=100, Severe=75, Moderate=50, Minor=25, Unknown=0
        - Urgency: Immediate=50, Expected=30, Future=10, Past=0
        - Event Type: Warning=40, Watch=30, Advisory=20, Statement=10
        - Time: (hours until expiration) * -5 (sooner = higher score)

        Returns sorted list (highest priority first)
        """
        def calculate_score(alert):
            score = 0

            # Severity score
            severity_scores = {
                'Extreme': 100,
                'Severe': 75,
                'Moderate': 50,
                'Minor': 25,
                'Unknown': 0
            }
            score += severity_scores.get(alert.get('severity', 'Unknown'), 0)

            # Urgency score
            urgency_scores = {
                'Immediate': 50,
                'Expected': 30,
                'Future': 10,
                'Past': 0,
                'Unknown': 0
            }
            score += urgency_scores.get(alert.get('urgency', 'Unknown'), 0)

            # Event type score
            event_type_scores = {
                'Warning': 40,
                'Watch': 30,
                'Advisory': 20,
                'Statement': 10,
                'Unknown': 0
            }
            score += event_type_scores.get(alert.get('event_type', 'Unknown'), 0)

            # Time urgency (estimate hours until expiration)
            expires = alert.get('expires', '')
            expires_hours = 999  # Default to far future
            if expires:
                try:
                    # Try to parse expiration time
                    if 'at' in expires.lower():
                        # Rough estimate: if it says "6:00AM" assume it's today or tomorrow
                        time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', expires, re.IGNORECASE)
                        if time_match:
                            # For simplicity, assume alerts expire within 48 hours
                            expires_hours = 24  # Default estimate
                except:
                    pass

            # Time score: sooner expiration = higher priority
            # Subtract hours (sooner = higher score)
            score += max(0, 50 - expires_hours)

            return score

        # Sort by score (descending), then by event type, then by title
        sorted_alerts = sorted(alerts, key=lambda a: (
            -calculate_score(a),  # Negative for descending
            {'Warning': 0, 'Watch': 1, 'Advisory': 2, 'Statement': 3, 'Unknown': 4}.get(a.get('event_type', 'Unknown'), 4),
            a.get('title', '')
        ))

        return sorted_alerts

    def _format_alert_compact(self, alert: dict, include_details: bool = True) -> str:
        """Format a single alert compactly

        Shares its formatting with the proactive WeatherService broadcasts via
        ``modules.alert_format``, so both localize from one code path.

        Args:
            alert: Alert dict with event, event_type, severity, expires, office, etc.
            include_details: If True, include expiration time and office

        Returns:
            Formatted alert string, e.g. "🟠High Wind Warn til 6AM by NWS SEA"
        """
        return alert_format.format_alert_compact(
            alert,
            self.response_translator,
            include_details=include_details,
            # !wx alerts has never shown the area description; the proactive
            # broadcast does, so the shared helper keeps it optional.
            include_location=False,
        )

    def _format_alerts_compact_summary(self, alerts: list, alert_count: int, max_length: int = 130) -> str:
        """Format multiple alerts with prioritized first alert and summary of others

        Args:
            alerts: List of prioritized alert dicts
            alert_count: Total number of alerts
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Compact formatted string: "4 alerts: 🟠High Wind Warn til 6AM | +3: 🌊Flood Watch, ❄️Freeze Adv, 🌫️Dense Fog Adv"
        """
        if not alerts:
            return f"{alert_count} alerts"

        # Format first (highest priority) alert with details
        first_alert = alerts[0]
        first_alert_text = self._format_alert_compact(first_alert, include_details=True)

        # If only one alert, return it
        if alert_count == 1:
            return f"{alert_count} alert: {first_alert_text}"

        # Build summary of remaining alerts
        remaining_alerts = alerts[1:]
        remaining_count = len(remaining_alerts)

        # Format remaining alerts as event types only
        remaining_parts = []
        for alert in remaining_alerts[:5]:  # Limit to 5 to avoid overflow
            event = alert.get('event', '')
            event_type = alert.get('event_type', '')

            # Get emoji for event type
            event_emoji = self._get_event_emoji(event, event_type)

            # Build compact event string. Trimmed harder than the lead alert:
            # first word only, since these are a comma-joined tail.
            event_short = alert_format.shorten_event(event, limit=12, max_words=1)
            event_type_abbrev = alert_format.event_type_abbrev(event_type, self.response_translator)
            if event:
                remaining_parts.append(f"{event_emoji}{event_short} {event_type_abbrev}")
            else:
                remaining_parts.append(f"{event_emoji}{event_type_abbrev}")

        # Build summary
        if remaining_count > 5:
            remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:5])}..."
        else:
            remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts)}"

        # Combine: first alert + summary
        result = f"{alert_count} alerts: {first_alert_text} | {remaining_summary}"

        # Check if it fits in max_length chars, truncate if needed
        if self._count_display_width(result) > max_length:
            # Try shorter first alert
            first_alert_text_short = self._format_alert_compact(first_alert, include_details=False)
            result = f"{alert_count} alerts: {first_alert_text_short} | {remaining_summary}"

            # If still too long, truncate remaining summary
            if self._count_display_width(result) > max_length:
                max_remaining = 3
                while max_remaining > 0 and self._count_display_width(result) > max_length:
                    if remaining_count > max_remaining:
                        remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:max_remaining])}..."
                    else:
                        remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:max_remaining])}"
                    result = f"{alert_count} alerts: {first_alert_text_short} | {remaining_summary}"
                    max_remaining -= 1

        return result

    def _get_event_emoji(self, event: str, event_type: str) -> str:
        """Get emoji for event type"""
        event_lower = event.lower() if event else ""

        # Weather event emojis
        if any(word in event_lower for word in ['flood', 'flooding']):
            return '🌊'
        elif any(word in event_lower for word in ['wind', 'gale']):
            return '💨'
        elif any(word in event_lower for word in ['snow', 'winter', 'blizzard']):
            return '❄️'
        elif any(word in event_lower for word in ['fog', 'smoke', 'haze']):
            return '🌫️'
        elif any(word in event_lower for word in ['heat', 'excessive heat']):
            return '🌡️'
        elif any(word in event_lower for word in ['freeze', 'frost']):
            return '🧊'
        elif any(word in event_lower for word in ['thunderstorm', 'tornado']):
            return '⛈️'
        elif any(word in event_lower for word in ['fire', 'red flag']):
            return '🔥'
        elif any(word in event_lower for word in ['hurricane', 'tropical']):
            return '🌀'
        elif any(word in event_lower for word in ['tsunami']):
            return '🌊'
        else:
            # Default by event type
            return {
                'Warning': '⚠️',
                'Watch': '👁️',
                'Advisory': 'ℹ️',
                'Statement': '📢'
            }.get(event_type, '⚠️')

    def _format_alert_full(self, alert: dict, index: int = None) -> str:
        """Format a single alert with full details for multi-message display

        Args:
            alert: Alert dict
            index: Optional alert number (1-based)

        Returns:
            Formatted alert string with start/stop times
        """
        translator = self.response_translator
        parts = []

        if index is not None:
            parts.append(f"{index}.")

        # No trimming here — this form is sent across as many messages as it needs.
        parts.append(
            alert_format.severity_emoji(alert.get('severity', 'Unknown'))
            + alert_format.format_event_label(
                alert.get('event', ''), alert.get('event_type', ''), translator, limit=None
            )
        )

        window = alert_format.format_alert_window(alert, translator)
        if window:
            parts.append(window)

        # The full form is not length-capped, so it keeps the longer fallback.
        office = alert_format.format_office(alert.get('office', ''), translator, limit=15)
        if office:
            parts.append(office)

        return " ".join(parts)

    async def _send_full_alert_list(self, message: MeshMessage, lat: float, lon: float) -> bool:
        """Send full list of alerts with details, splitting across multiple messages if needed.

        Returns whether every message was sent; a refused first message ends the list.
        """
        # Get full alert data
        alerts_result = await self._get_weather_alerts_noaa_async(
            lat, lon, return_full_data=True
        )
        if alerts_result == self.ERROR_FETCHING_DATA:
            return bool(await self.send_response(message, self.translate('commands.wx.error_fetching')))
        elif alerts_result == self.NO_ALERTS:
            return bool(await self.send_response(message, "No weather alerts"))

        alerts, alert_count = alerts_result

        if not alerts:
            return bool(await self.send_response(message, "No weather alerts"))

        # Format each alert with full details
        alert_lines = []
        for i, alert in enumerate(alerts, 1):
            alert_line = self._format_alert_full(alert, index=i)
            alert_lines.append(alert_line)

        # Send alerts, splitting into multiple messages if needed
        rate_limit = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
        sleep_time = max(rate_limit + 1.0, 2.0)

        # Get max message length dynamically
        max_length = self.get_max_message_length(message)

        # Group alerts into messages that fit within max_length chars
        current_message = f"{alert_count} alerts:"
        messages = []

        for line in alert_lines:
            # Check if adding this line would exceed limit
            test_message = current_message + "\n" + line if current_message else line
            if self._count_display_width(test_message) > max_length:
                # Current message is full, start new one
                if current_message:
                    messages.append(current_message)
                current_message = line
            else:
                # Add to current message
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line

        # Add last message
        if current_message:
            messages.append(current_message)

        # Send all messages (per-user rate limit applies only to first; skip for continuations)
        for i, msg in enumerate(messages):
            if not await self.send_response(message, msg, skip_user_rate_limit=(i > 0)):
                return False
            if i < len(messages) - 1:
                await self._pace_reply(message, sleep_time)
        return True

    def abbreviate_city_name(self, city: str) -> str:
        """Abbreviate city names for compact display (e.g., Seattle -> SEA)"""
        return alert_format.abbreviate_city_name(city)

    def compact_time(self, time_str: str) -> str:
        """Compact time format: '6:00AM' -> '6AM', 'December 16 at 3:12PM' -> 'Dec 16 3:12PM'
        Also handles ISO format: '2025-12-17T01:00:00-08:00' -> 'Dec 17 1AM'"""
        return alert_format.compact_time(time_str, self.response_translator)

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
        label = self.translate(key)
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
                            self.bot.config, high_val, low_val, units_str, self.logger,
                            translator=self.response_translator,
                        )
                except ValueError:
                    continue

        low_match = re.search(r'low\s+around\s+(-?\d+)', text.lower())
        if low_match:
            try:
                low_val = int(low_match.group(1))
                if _single_ok(low_val):
                    return format_temperature_high_low(
                        self.bot.config, None, low_val, units_str, self.logger,
                        translator=self.response_translator,
                    )
            except ValueError:
                pass

        high_match = re.search(r'high\s+near\s+(-?\d+)', text.lower())
        if high_match:
            try:
                high_val = int(high_match.group(1))
                if _single_ok(high_val):
                    return format_temperature_high_low(
                        self.bot.config, high_val, None, units_str, self.logger,
                        translator=self.response_translator,
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
                stations_data = self.noaa_session.get(station_url, timeout=obs_timeout)
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
                obs_data = self.noaa_session.get(obs_url, timeout=obs_timeout)
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
                    dewpoint = int(round(dewpoint_val))
                else:
                    dewpoint = int(dewpoint_val * 9/5 + 32)  # Convert C to F
                obs_data_dict['dew_point'] = str(dewpoint)

            visibility_val = props.get('visibility', {}).get('value')
            if visibility_val is not None and str(props.get('visibility', {}).get('unitCode', '')).endswith(':km'):
                visibility_val *= 1000  # NOAA normally reports meters; handle km too
            if visibility_val is not None:
                if self._noaa_metric_distance:
                    visibility = int(visibility_val / 1000)  # Convert m to km
                else:
                    visibility = int(visibility_val * 0.000621371)  # Convert m to miles
                if visibility > 0:
                    obs_data_dict['visibility'] = str(visibility)

            wind_gust_val = props.get('windGust', {}).get('value')
            if wind_gust_val is not None:
                # NOAA observations declare their unit; station gusts are usually km/h, not m/s.
                if 'km_h' in str(props.get('windGust', {}).get('unitCode', '')):
                    wind_gust_val = wind_gust_val / 3.6
                # Shown only above 10 mph, whatever unit it is shown in.
                if int(wind_gust_val * 2.237) > 10:
                    factor = {'mph': 2.237, 'kmh': 3.6, 'ms': 1.0, 'kn': 1.944}[wind_unit]
                    obs_data_dict['wind_gusts'] = str(int(wind_gust_val * factor))

            pressure_val = props.get('barometricPressure', {}).get('value')
            if pressure_val is not None:
                pressure = int(pressure_val / 100)  # Convert Pa to hPa
                obs_data_dict['pressure'] = str(pressure)

            return obs_data_dict

        except Exception as e:
            self.logger.debug(f"Error getting observation data: {e}")
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

    # NOAA sometimes names forecast periods after federal holidays (e.g. "Washington's Birthday")
    # instead of the weekday. Match these so we can resolve to weekday via startTime.
    _NOAA_HOLIDAY_NAME_PATTERNS = (
        "washington's birthday", "presidents day", "president's day",
        "martin luther king", "mlk day", "memorial day", "labor day",
        "independence day", "juneteenth", "columbus day", "veterans day",
        "thanksgiving", "christmas day", "new year's day", "new year's eve",
    )

    def _noaa_period_display_name(self, period: dict) -> str:
        """Return display label for a NOAA forecast period. Resolves holiday names to weekday."""
        name = period.get('name', '') or ''
        start_time_str = period.get('startTime')
        name_lower = name.lower()
        is_holiday = any(p in name_lower for p in self._NOAA_HOLIDAY_NAME_PATTERNS)
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

    async def get_weather_for_zipcode(self, zipcode: str) -> str:
        """Get weather data for a specific zipcode (legacy method)"""
        return await self.get_weather_for_location(zipcode, "zipcode")

    def abbreviate_alert_title(self, title: str) -> str:
        """Abbreviate alert title for brevity"""
        # Common alert type abbreviations
        replacements = {
            "warning": "Warn",
            "watch": "Watch",
            "advisory": "Adv",
            "statement": "Stmt",
            "severe thunderstorm": "SvrT-Storm",
            "tornado": "Tornado",
            "flash flood": "FlashFlood",
            "flood": "Flood",
            "winter storm": "WinterStorm",
            "blizzard": "Blizzard",
            "ice storm": "IceStorm",
            "freeze": "Freeze",
            "frost": "Frost",
            "heat": "Heat",
            "excessive heat": "ExHeat",
            "extreme heat": "ExtHeat",
            "wind": "Wind",
            "high wind": "HighWind",
            "wind advisory": "WindAdv",
            "fire weather": "FireWx",
            "red flag": "RedFlag",
            "dense fog": "DenseFog",
            "issued": "iss",
            "until": "til",
            "effective": "eff",
            "expires": "exp",
            "dense smoke": "DenseSmoke",
            "air quality": "AirQuality",
            "coastal flood": "CoastalFlood",
            "lakeshore flood": "LakeshoreFlood",
            "rip current": "RipCurrent",
            "high surf": "HighSurf",
            "hurricane": "Hurricane",
            "tropical storm": "TropStorm",
            "tropical depression": "TropDep",
            "storm surge": "StormSurge",
            "tsunami": "Tsunami",
            "earthquake": "Earthquake",
            "volcano": "Volcano",
            "avalanche": "Avalanche",
            "landslide": "Landslide",
            "debris flow": "DebrisFlow",
            "dust storm": "DustStorm",
            "sandstorm": "Sandstorm",
            "blowing dust": "BlwDust",
            "blowing sand": "BlwSand"
        }

        result = title
        for key, value in replacements.items():
            # Case insensitive replace
            result = result.replace(key, value).replace(key.capitalize(), value).replace(key.upper(), value)

        # Limit to reasonable length
        if len(result) > 30:
            result = result[:27] + "..."

        return result

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
