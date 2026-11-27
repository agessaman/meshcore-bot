#!/usr/bin/env python3
"""
Weather command for the MeshCore Bot
Provides weather information using zip codes and NOAA APIs
"""

import asyncio
import re
import threading
import xml.dom.minidom  # noqa: F401  re-exported
from datetime import datetime, timedelta, timezone  # noqa: F401  re-exported
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
from ..nws_alerts import (  # noqa: F401  re-exported
    WX_SPECIAL_RULES,
    entry_nws_headline,
    entry_summary,
    entry_title,
    parse_alert_fields,
)
from ..nws_coverage import NWSNoCoverageCache
from ..template_reference import template_spec
from ..utils import (
    format_temperature_high_low,  # noqa: F401  re-exported
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
)
from ..weather_common import WeatherCommandMixin
from ..weather_providers.alerts_nws import NwsAlertsProvider
from ..weather_providers.noaa import (
    ERROR_FETCHING_DATA as _ERROR_FETCHING_DATA,
)
from ..weather_providers.noaa import (
    MIN_BODY_BUDGET as _MIN_BODY_BUDGET,
)
from ..weather_providers.noaa import (
    NO_ALERTS as _NO_ALERTS,
)
from ..weather_providers.noaa import (
    WX_MULTIDAY_MAX_DAYS,
    NoaaProvider,
    resolve_units,
)
from .alternatives.wx_international import GlobalWxCommand
from .base_command import BaseCommand
from .rain_command import nws_http_means_no_coverage  # noqa: F401  re-exported

# Kept for code that checked them; these imports used to be optional.
WX_INTERNATIONAL_AVAILABLE = True
WXSIM_PARSER_AVAILABLE = True

# Placeholders of the [Weather] temperature_*_format templates (utils.format_temperature_high_low).
_TEMP_FORMAT_PLACEHOLDERS = {
    "high": "Day's high, rounded",
    "low": "Day's low, rounded",
    "units": "Unit symbol, e.g. °F",
    "high_label": "H, or its translation in the reply language",
    "low_label": "L, or its translation in the reply language",
}
_TEMP_FORMAT_NOTES = [
    "A mistake makes the bot log a warning and use the default format instead.",
    "Keep {high_label}/{low_label} to follow the reply language; the preview shows English.",
]

_P = ParamSpec("_P")
_T = TypeVar("_T")


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
        {"key": "temperature_unit", "label": "Temperature unit", "type": "enum", "section": "Weather",
         "options": [
             {"value": "fahrenheit", "label": "Fahrenheit (°F)"},
             {"value": "celsius", "label": "Celsius (°C)"},
         ],
         "default": "fahrenheit",
         "help": "For wx, gwx, rain and the weather service. Celsius also shows visibility in km. "
                 "Shared weather setting."},
        {"key": "wind_speed_unit", "label": "Wind speed unit", "type": "enum", "section": "Weather",
         "options": [
             {"value": "mph", "label": "Miles per hour (mph)"},
             {"value": "kmh", "label": "Kilometers per hour (km/h)"},
             {"value": "ms", "label": "Meters per second (m/s)"},
             {"value": "kn", "label": "Knots (kn)"},
         ],
         "default": "mph", "help": "For wx, gwx and the weather service. Shared weather setting."},
        {"key": "precipitation_unit", "label": "Precipitation unit", "type": "enum", "section": "Weather",
         "options": [{"value": "inch", "label": "Inches"}, {"value": "mm", "label": "Millimeters"}],
         "default": "inch", "help": "For wx, gwx and the weather service. Shared weather setting."},
        # [Wx_Command] overrides of the shared units. "" (the default) means no
        # override; the save endpoint removes the key rather than writing it blank.
        {
            "key": "temperature_unit",
            "label": "wx-only temperature unit",
            "type": "enum",
            "options": [
                {"value": "", "label": "Same as [Weather]"},
                {"value": "fahrenheit", "label": "Fahrenheit (°F)"},
                {"value": "celsius", "label": "Celsius (°C)"},
            ],
            "default": "",
            "help": "Overrides the shared temperature unit for wx only.",
        },
        {
            "key": "wind_speed_unit",
            "label": "wx-only wind speed unit",
            "type": "enum",
            "options": [
                {"value": "", "label": "Same as [Weather]"},
                {"value": "mph", "label": "Miles per hour (mph)"},
                {"value": "kmh", "label": "Kilometers per hour (km/h)"},
                {"value": "ms", "label": "Meters per second (m/s)"},
                {"value": "kn", "label": "Knots (kn)"},
            ],
            "default": "",
            "help": "Overrides the shared wind speed unit for wx only.",
        },
        {"key": "weather_provider", "label": "Weather provider", "type": "enum", "section": "Weather",
         "options": [
             {"value": "noaa", "label": "NOAA (US, includes alerts)"},
             {"value": "openmeteo", "label": "Open-Meteo (global)"},
         ],
         "default": "noaa", "help": "API used for forecasts. Shared weather setting."},
        {"key": "openmeteo_fallback", "label": "Open-Meteo outside NWS coverage", "type": "bool", "section": "Weather",
         "default": False,
         "help": "Only with the NOAA provider: answer wx from Open-Meteo for places NWS does not cover "
                 "(outside the US) instead of replying with an error. Shared weather setting."},
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
        {"key": "temperature_high_low_format", "label": "High/low format", "type": "str", "section": "Weather",
         "default": "{high_label}:{high}{units} {low_label}:{low}{units}",
         "help": "How a day's high and low are shown in wx, gwx and the weather service. Shared weather setting.",
         "template": template_spec("format", _TEMP_FORMAT_PLACEHOLDERS, notes=_TEMP_FORMAT_NOTES,
                                   escapes=False, preview="temp_pair",
                                   blank_default="{high_label}:{high}{units} {low_label}:{low}{units}")},
        {"key": "temperature_high_only_format", "label": "High-only format", "type": "str", "section": "Weather",
         "default": "{high_label}:{high}{units}",
         "help": "Used when a day has only a high. Shared weather setting.",
         "template": template_spec("format", _TEMP_FORMAT_PLACEHOLDERS, notes=_TEMP_FORMAT_NOTES,
                                   escapes=False, preview="temp_high",
                                   blank_default="{high_label}:{high}{units}")},
        {"key": "temperature_low_only_format", "label": "Low-only format", "type": "str", "section": "Weather",
         "default": "{low_label}:{low}{units}",
         "help": "Used when a day has only a low, such as tonight's forecast. Shared weather setting.",
         "template": template_spec("format", _TEMP_FORMAT_PLACEHOLDERS, notes=_TEMP_FORMAT_NOTES,
                                   escapes=False, preview="temp_low",
                                   blank_default="{low_label}:{low}{units}")},
        {"key": "use_bot_location_when_no_location", "label": "Bot location as last resort", "type": "bool",
         "section": "Weather", "default": False,
         "help": "A bare wx/gwx with no custom source, companion location or default city uses [Bot] bot_latitude/bot_longitude instead of showing usage. Shared weather setting."},
    ]

    # Error constants
    NO_DATA_NOGPS = "No GPS data available"
    ERROR_FETCHING_DATA = _ERROR_FETCHING_DATA
    NO_ALERTS = _NO_ALERTS

    # Floor for the forecast body once a location prefix has been reserved out of
    # the frame budget, so a long "City, State: " can never squeeze the forecast
    # down to nothing (or negative, which the -10/-20 arithmetic in the period
    # formatters would turn into dropped fields).
    MIN_BODY_BUDGET = _MIN_BODY_BUDGET

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

        # With NOAA, places outside NWS coverage can be answered from Open-Meteo instead.
        self._openmeteo = None
        if self.delegate_command is None and bot.config.getboolean('Weather', 'openmeteo_fallback', fallback=False):
            self._openmeteo = GlobalWxCommand(bot)
            self.logger.info("openmeteo_fallback enabled: wx answers places outside NWS coverage from Open-Meteo")

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
            session = self._create_retry_session()

            # requests.Session and WXSIM parser instances are shared by this command.
            # Keep provider calls serialized as they were before moving them to worker
            # threads. The lock is acquired inside the worker, never on the event loop.
            self._sync_provider_lock = threading.Lock()

            self._nws_no_coverage = NWSNoCoverageCache()

            # The NOAA fetch and formatting live in the providers; this command keeps
            # the request (parsing, geocoding, labels) and the sending. Both providers
            # share one session, one worker lock and one coverage cache.
            fallback_enabled = self._openmeteo is not None
            self.provider = NoaaProvider(
                host=self,
                units=resolve_units(self._unit_setting),
                session=session,
                lock=self._sync_provider_lock,
                coverage=self._nws_no_coverage,
                fallback_enabled=fallback_enabled,
            )
            self.alerts_provider = NwsAlertsProvider(
                host=self,
                session=session,
                lock=self._sync_provider_lock,
                coverage=self._nws_no_coverage,
                fallback_enabled=fallback_enabled,
            )

    @property
    def noaa_session(self) -> requests.Session:
        """The retry session the NOAA providers share."""
        return self.provider.session

    @noaa_session.setter
    def noaa_session(self, session: requests.Session) -> None:
        self.provider.session = session
        self.alerts_provider.session = session

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
        """Configured (temperature_unit, wind_speed_unit) for NOAA replies."""
        return self.provider._noaa_units()

    @property
    def _noaa_metric_distance(self) -> bool:
        """Kilometers instead of miles, following the temperature unit as gwx does."""
        return self.provider._noaa_metric_distance

    def _noaa_units_url(self, url: str) -> str:
        """A NOAA forecast URL asking for SI units (°C, km/h, also in the forecast text) when Celsius is configured."""
        return self.provider._noaa_units_url(url)

    def _noaa_wind_convert(self, number: str, wind_speed_text: str) -> str:
        """A NOAA wind number in the configured wind_speed_unit (NOAA sends mph, or km/h in SI mode)."""
        return self.provider._noaa_wind_convert(number, wind_speed_text)

    @staticmethod
    def _noaa_period_temp_symbol(period: dict) -> str:
        return NoaaProvider._noaa_period_temp_symbol(period)

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

            if self._outside_nws_coverage(lat, lon):
                return self._openmeteo_weather(location, location_type, lat, lon, forecast_type, num_days, message)

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
                # NWS had no data: a point outside its coverage goes to Open-Meteo when enabled.
                if self._outside_nws_coverage(lat, lon):
                    return self._openmeteo_weather(location, location_type, lat, lon, forecast_type, num_days, message)
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
        return self.provider._noaa_forecast_body(lat, lon, forecast_type, num_days, body_max_length)

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

    def _outside_nws_coverage(self, lat: float, lon: float) -> bool:
        """Whether the Open-Meteo fallback is on and NWS is known not to cover this point."""
        return self._openmeteo is not None and self._nws_no_coverage.is_unavailable(lat, lon)

    def _openmeteo_weather(self, location: str, location_type: str, lat: float, lon: float,
                           forecast_type: str, num_days: int, message: Optional[MeshMessage]):
        """The Open-Meteo (gwx) reply for a point outside NWS coverage, in wx's units."""
        openmeteo = self._openmeteo
        # wx's units, [Wx_Command] overrides included, so one command never mixes unit systems.
        openmeteo.temperature_unit, openmeteo.wind_speed_unit = self._noaa_units()
        precipitation = self._unit_setting('precipitation_unit', openmeteo.precipitation_unit)
        if precipitation in ('inch', 'mm'):
            openmeteo.precipitation_unit = precipitation
        # The label needs the country, which wx's own prefix leaves out. A city's
        # lookup is answered from the geocoding cache wx's lookup just filled; a
        # position needs gwx's reverse lookup.
        geocode_result = None
        if location_type == "coordinates" or location_type == "zipcode":
            _, _, address_info, geocode_result = openmeteo.geocode_location(location)
        else:
            result = self.city_to_lat_lon(location)
            address_info = result[2] if len(result) == 3 else None
        self.logger.info(
            "%s,%s is outside NWS coverage; answering wx from Open-Meteo", round(lat, 2), round(lon, 2)
        )
        return openmeteo._weather_for_point(
            location, lat, lon, address_info or {}, geocode_result, forecast_type, num_days, message
        )

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

    def _noaa_fetch(self, url: str, what: str, point: Optional[tuple[float, float]] = None):
        """GET *url* through the NOAA session; None, after a warning naming *what*,
        on an HTTP error status, a timeout or a connection error.

        With the Open-Meteo fallback on, a *point* request also records whether
        NWS covers that point.
        """
        return self.provider._noaa_fetch(url, what, point)

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
        return self.provider.get_noaa_weather(lat, lon, return_periods, max_length)

    def _noaa_current_summary(self, current: dict, weather_json: dict, max_length: int) -> str:
        """The current period's line: name, sky, temperature, wind and as many details as fit."""
        return self.provider._noaa_current_summary(current, weather_json, max_length)

    def _append_noaa_tomorrow(self, weather: str, period: dict, is_current_tonight: bool, is_current_night: bool, max_length: int) -> str:
        """``weather`` with the Tomorrow period appended when it fits; a night reply shortens its text and is more lenient."""
        return self.provider._append_noaa_tomorrow(weather, period, is_current_tonight, is_current_night, max_length)

    def _append_noaa_period(self, weather: str, period: dict, max_length: int) -> str:
        """``weather`` with a Today or Tonight period appended when it fits.

        The period gets wind and details only while the reply so far leaves room,
        so the current period keeps its full details first.
        """
        return self.provider._append_noaa_period(weather, period, max_length)

    def _noaa_followup_periods(self, forecast: list, current: dict) -> tuple:
        """The ``(index, period)`` pairs for Today, Tonight and Tomorrow after ``current``, and whether
        ``current`` is Tonight or any night period: ``(today, tonight, tomorrow, is_tonight, is_night)``."""
        return self.provider._noaa_followup_periods(forecast, current)

    def _noaa_period_str(self, period: dict, forecast_text: Optional[str] = None) -> Optional[str]:
        """``" | Name: <emoji><forecast> <high/low or temp°>"`` for a forecast period.

        None when the period has no temperature or no short forecast.
        ``forecast_text`` replaces the short forecast in the text (the emoji
        still comes from the short forecast).
        """
        return self.provider._noaa_period_str(period, forecast_text)

    def _noaa_period_wind(self, weather: str, period_str: str, period: dict, threshold: int, max_length: int) -> str:
        """Append the period's wind to ``period_str`` when ``weather + period_str`` is under ``threshold``
        display columns and the result still fits ``max_length``."""
        return self.provider._noaa_period_wind(weather, period_str, period, threshold, max_length)

    @staticmethod
    def _abbreviate_noaa_forecast(period_short: str) -> str:
        """Shorten a long short-forecast ("A then B and C") to its main conditions."""
        return NoaaProvider._abbreviate_noaa_forecast(period_short)

    def get_noaa_hourly_weather(self, lat: float, lon: float) -> tuple:
        """Get hourly weather forecast from NOAA

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Tuple of (hourly_periods_list, points_data)
        """
        return self.provider.get_noaa_hourly_weather(lat, lon)

    def format_hourly_forecast(self, hourly_periods: list, max_length: int = 130) -> str:
        """Format hourly forecast to fit as many hours as possible in max_length bytes

        Args:
            hourly_periods: List of hourly forecast periods from NOAA
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Formatted string with one hour per line
        """
        return self.provider.format_hourly_forecast(hourly_periods, max_length)

    @staticmethod
    def _parse_noaa_start_time(start_time_str: str) -> datetime:
        """Parse a NOAA ISO ``startTime``; a trailing ``Z`` means UTC."""
        return NoaaProvider._parse_noaa_start_time(start_time_str)

    def _future_hourly_periods(self, hourly_periods: list) -> list:
        """Periods starting after now; unparseable or missing times are kept.

        NOAA times carry the location's UTC offset, so they are compared as
        absolute times. Dropping the offset compared the location's wall clock
        with the bot's, which kept past hours or dropped future ones whenever
        the two were in different time zones.
        """
        return self.provider._future_hourly_periods(hourly_periods)

    def _hourly_line(self, period: dict) -> str:
        """One hour as "10AM: 🌦️ 26% Chance Light Rain 49° SSW5"."""
        return self.provider._hourly_line(period)

    @staticmethod
    def _period_date(period: dict):
        """The local calendar date a NOAA period starts on, from its startTime, or None.

        NOAA writes startTime in the location's own offset ("2026-10-01T18:00:00-07:00"),
        so its date part is the local date there, whatever the bot's clock says.
        """
        return NoaaProvider._period_date(period)

    def _forecast_today(self, forecast: list):
        """Today's date at the forecast's location, or the bot's date when no period has an offset.

        The location's "now" is the current instant in the UTC offset NOAA writes on
        its period times. The first period's own date is not enough: at 1 AM the
        first period can still be the night that started yesterday evening.
        """
        return self.provider._forecast_today(forecast)

    def _period_dates(self, forecast: list) -> list:
        """Each period's local date: its startTime date, or inferred from its neighbors when it has none.

        NOAA periods alternate day and night in order, so an undated period is the
        same date as a preceding daytime period when it is a night, else the day after.
        """
        return self.provider._period_dates(forecast)

    def _find_tomorrow_periods(self, forecast: list) -> list:
        """Tomorrow's NOAA periods: those dated the day after today at the location;
        without dates, those named "Tomorrow", else named for tomorrow's weekday, else the
        (up to two) periods after today's.
        """
        return self.provider._find_tomorrow_periods(forecast)

    def format_tomorrow_forecast(self, forecast: list, max_length: int = 130) -> str:
        """Format a detailed forecast for tomorrow"""
        return self.provider.format_tomorrow_forecast(forecast, max_length)

    def _fit_tomorrow_parts(self, parts: list[tuple[str, str]], max_length: int) -> str:
        """Join tomorrow's periods within *max_length* UTF-8 bytes.

        Each part is (with wind, without wind). The later periods' wind goes
        first, then the first period's, then the later periods themselves. When
        even the first period alone does not fit, it is sent anyway (the send
        path splits it) rather than leaving the reply empty.
        """
        return self.provider._fit_tomorrow_parts(parts, max_length)

    def format_multiday_forecast(self, forecast: list, num_days: int = 7, max_length: int = 130) -> str:
        """Format a less detailed multi-day forecast summary"""
        return self.provider.format_multiday_forecast(forecast, num_days, max_length)

    def _multiday_lines_by_date(self, forecast: list, num_days: int) -> list[str]:
        """One line per local date after today at the location, up to *num_days*.

        Grouping by startTime date (instead of weekday name against the bot's
        clock) keeps holiday-named periods ("Christmas Day") and does not depend
        on the bot sharing the location's time zone. A daytime period wins over
        that date's night period.
        """
        return self.provider._multiday_lines_by_date(forecast, num_days)

    def _day_abbrev(self, weekday: int) -> str:
        """The multi-day label for a weekday (0 = Monday), translated (English "M", "Th", "Sa")."""
        return self.provider._day_abbrev(weekday)

    @staticmethod
    def _multiday_day_name(period_name_lower: str) -> str | None:
        """The weekday a NOAA period belongs to, or None to skip it.

        Part-of-day periods (Tonight, This Afternoon) count only when they name a
        weekday; otherwise Today and Tomorrow map to the current and next weekday.
        """
        return NoaaProvider._multiday_day_name(period_name_lower)

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
        return self.provider._add_period_details(period_str, detailed_forecast, current_weather_length, max_length, observation_data)

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
        return self.alerts_provider.get_weather_alerts_noaa(lat, lon, return_full_data)

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
        return self.alerts_provider._differentiate_duplicate_statements(alerts)

    def _prioritize_alerts(self, alerts: list) -> list:
        """Prioritize alerts using hybrid scoring system

        Scoring:
        - Severity: Extreme=100, Severe=75, Moderate=50, Minor=25, Unknown=0
        - Urgency: Immediate=50, Expected=30, Future=10, Past=0
        - Event Type: Warning=40, Watch=30, Advisory=20, Statement=10
        - Time: (hours until expiration) * -5 (sooner = higher score)

        Returns sorted list (highest priority first)
        """
        return self.alerts_provider._prioritize_alerts(alerts)

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
        return self.alerts_provider._format_alert_compact(alert, include_details)

    def _format_alerts_compact_summary(self, alerts: list, alert_count: int, max_length: int = 130) -> str:
        """Format multiple alerts with prioritized first alert and summary of others

        Args:
            alerts: List of prioritized alert dicts
            alert_count: Total number of alerts
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Compact formatted string: "4 alerts: 🟠High Wind Warn til 6AM | +3: 🌊Flood Watch, ❄️Freeze Adv, 🌫️Dense Fog Adv"
        """
        return self.alerts_provider._format_alerts_compact_summary(alerts, alert_count, max_length)

    def _get_event_emoji(self, event: str, event_type: str) -> str:
        """Get emoji for event type"""
        return self.alerts_provider._get_event_emoji(event, event_type)

    def _format_alert_full(self, alert: dict, index: int = None) -> str:
        """Format a single alert with full details for multi-message display

        Args:
            alert: Alert dict
            index: Optional alert number (1-based)

        Returns:
            Formatted alert string with start/stop times
        """
        return self.alerts_provider._format_alert_full(alert, index)

    async def _send_full_alert_list(self, message: MeshMessage, lat: float, lon: float) -> bool:
        """Send full list of alerts with details, splitting across multiple messages if needed.

        Returns whether every message was sent; a refused first message ends the list.
        """
        return await self.alerts_provider.send_full_list(message, lat, lon)

    def abbreviate_city_name(self, city: str) -> str:
        """Abbreviate city names for compact display (e.g., Seattle -> SEA)"""
        return alert_format.abbreviate_city_name(city)

    def compact_time(self, time_str: str) -> str:
        """Compact time format: '6:00AM' -> '6AM', 'December 16 at 3:12PM' -> 'Dec 16 3:12PM'
        Also handles ISO format: '2025-12-17T01:00:00-08:00' -> 'Dec 17 1AM'"""
        return self.provider.compact_time(time_str)

    def abbreviate_wind_direction(self, direction: str) -> str:
        """Abbreviate wind direction to emoji + 2-3 characters"""
        return self.provider.abbreviate_wind_direction(direction)

    def _wind_letters(self, point: str) -> str:
        """A 16-point compass abbreviation in the reply's language ("NE" is "NO" in German), as gwx shows it."""
        return self.provider._wind_letters(point)

    def extract_humidity(self, text: str) -> str:
        """Extract humidity percentage from forecast text"""
        return self.provider.extract_humidity(text)

    def extract_precip_chance(self, text: str) -> str:
        """Extract precipitation chance from forecast text"""
        return self.provider.extract_precip_chance(text)

    def extract_high_low(self, text: str, units_str: str = "°F") -> str:
        """Extract high/low temperatures from forecast text; format via [Weather] templates."""
        return self.provider.extract_high_low(text, units_str)

    def extract_uv_index(self, text: str) -> str:
        """Extract UV index from forecast text"""
        return self.provider.extract_uv_index(text)

    def extract_dew_point(self, text: str) -> str:
        """Extract dew point temperature from forecast text"""
        return self.provider.extract_dew_point(text)

    def extract_visibility(self, text: str) -> str:
        """Extract visibility from forecast text"""
        return self.provider.extract_visibility(text)

    def extract_precip_probability(self, text: str) -> str:
        """Extract precipitation probability from forecast text"""
        return self.provider.extract_precip_probability(text)

    def extract_wind_gusts(self, text: str) -> str:
        """Extract wind gusts from forecast text"""
        return self.provider.extract_wind_gusts(text)

    def _forecast_text_gusts(self, text: str) -> str:
        """Gusts from the forecast text ("gusts up to 30 mph" or "48 km/h") in the configured unit."""
        return self.provider._forecast_text_gusts(text)

    def extract_pressure(self, text: str) -> str:
        """Extract barometric pressure from forecast text"""
        return self.provider.extract_pressure(text)

    def get_observation_data(self, points_data: dict) -> dict:
        """Get observation station data from NOAA and return as a dict

        Returns:
            Dict with keys: humidity, dew_point, visibility, wind_gusts, pressure
            Values are strings ready for display, or None if not available
        """
        return self.provider.get_observation_data(points_data)

    def get_weather_emoji(self, condition: str) -> str:
        """Get emoji for weather condition"""
        return self.provider.get_weather_emoji(condition)

    # NOAA sometimes names forecast periods after federal holidays (e.g. "Washington's Birthday")
    # instead of the weekday. Match these so we can resolve to weekday via startTime.

    def _noaa_period_display_name(self, period: dict) -> str:
        """Return display label for a NOAA forecast period. Resolves holiday names to weekday."""
        return self.provider._noaa_period_display_name(period)

    def abbreviate_noaa(self, text: str) -> str:
        """Replace long strings with shorter ones for display"""
        return self.provider.abbreviate_noaa(text)

    async def get_weather_for_zipcode(self, zipcode: str) -> str:
        """Get weather data for a specific zipcode (legacy method)"""
        return await self.get_weather_for_location(zipcode, "zipcode")

    def abbreviate_alert_title(self, title: str) -> str:
        """Abbreviate alert title for brevity"""
        return self.alerts_provider.abbreviate_alert_title(title)

    def get_current_conditions(self, points_data: dict) -> str:
        """Get additional current conditions data from NOAA using existing points data (legacy method)"""
        return self.provider.get_current_conditions(points_data)
