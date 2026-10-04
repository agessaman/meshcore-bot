#!/usr/bin/env python3
"""
Global Weather command for the MeshCore Bot
Provides worldwide weather information using Open-Meteo API
"""

import asyncio
import math
import re
from datetime import datetime, timedelta
from typing import Any, Optional, Union

import requests

from ...clients.mqtt_weather import (  # noqa: F401  re-exported
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)
from ...clients.wxsim_parser import WXSIMParser
from ...location import get_bot_lat_lon, get_companion_lat_lon
from ...models import MeshMessage
from ...utils import (  # noqa: F401  format_temperature_high_low and get_nominatim_geocoder re-exported
    format_temperature_high_low,
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
    rate_limited_nominatim_reverse_sync,
)
from ...weather_common import _ARROWS_8, _COMPASS_16, WeatherCommandMixin, load_open_meteo_model
from ..base_command import BaseCommand

# Kept for code that checked them; these imports used to be optional.
WXSIM_PARSER_AVAILABLE = True

# Multiday: plain digits, 7day/7-day, or suffix form 7d/10d (min 2, max below). Open-Meteo allows up to 16 forecast days.
GWX_MULTIDAY_MAX_DAYS = 16

MI_TO_KM = 1.609344
HPA_TO_MMHG = 0.750062
# Past ~20 mi / 32 km, visibility is reported as unlimited anyway.
VISIBILITY_CAP_MI = 20
VISIBILITY_CAP_KM = 32



def _int_or_none(value: Any) -> Optional[int]:
    """int(value), or None for a missing or null field."""
    return None if value is None else int(value)

class GlobalWxCommand(WeatherCommandMixin, BaseCommand):
    """Handles global weather commands with city/location support"""

    _COORDINATES_RE = re.compile(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$')
    _ZIP_RE = re.compile(r'^\d{5}$')

    # Plugin metadata
    # Every reply goes through send_response, so a scheduled {cmd:gwx ...} renders
    # without transmitting, as wx (which can delegate here) already does.
    render_safe = True
    name = "gwx"
    translation_ns = "commands.gwx"
    keywords = ['gwx', 'globalweather', 'gwxa']
    description = "Get weather information for any global location (usage: gwx Tokyo)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    # Open-Meteo/geocoding need the network; custom MQTT/WXSIM may be LAN-only.
    requires_internet = False

    # Documentation
    short_description = "Get weather for any global location using Open-Meteo API"
    usage = "gwx <location> [tomorrow|<N>d|hourly]"
    examples = ["gwx Tokyo", "gwx Paris, France"]
    parameters = [
        {"name": "location", "description": "City name, country, or coordinates"},
        {"name": "option", "description": "tomorrow, Nd (e.g. 7d, 10d), or hourly (optional)"}
    ]

    # Error constants - will use translations instead
    ERROR_FETCHING_DATA = "ERROR_FETCHING_DATA"  # Placeholder, will use translate()
    NO_ALERTS = "No weather alerts available"

    def __init__(self, bot: Any):
        """Initialize the global weather command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.url_timeout = 10  # seconds

        self.wxsim_parser = WXSIMParser()

        self.weather_model = self._load_weather_model()

        # Get default location/state/country from config for fallback/disambiguation
        self.default_city = self.bot.config.get('Weather', 'default_city', fallback='').strip()
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
        self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
        self.always_show_location = self.bot.config.getboolean('Weather', 'always_show_location', fallback=False)

        # Get unit preferences from config
        self.temperature_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
        self.wind_speed_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()
        self.precipitation_unit = self.bot.config.get('Weather', 'precipitation_unit', fallback='inch').lower()

        # Validate units
        if self.temperature_unit not in ['fahrenheit', 'celsius']:
            self.logger.warning(f"Invalid temperature_unit '{self.temperature_unit}', using 'fahrenheit'")
            self.temperature_unit = 'fahrenheit'
        if self.wind_speed_unit not in ['mph', 'kmh', 'ms', 'kn']:
            self.logger.warning(f"Invalid wind_speed_unit '{self.wind_speed_unit}', using 'mph'")
            self.wind_speed_unit = 'mph'
        if self.precipitation_unit not in ['inch', 'mm']:
            self.logger.warning(f"Invalid precipitation_unit '{self.precipitation_unit}', using 'inch'")
            self.precipitation_unit = 'inch'

        # Initialize geocoder (will use rate-limited helpers for actual calls)
        self.geolocator = get_nominatim_geocoder()

        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager

    @property
    def metric_distance(self) -> bool:
        """Whether distances should be shown in kilometers.

        Derived from [Weather] temperature_unit rather than the response
        language so every unit in one reply agrees: a bot configured for
        Fahrenheit should not print kilometers just because it answers in
        Russian.
        """
        return self.temperature_unit == 'celsius'

    def _load_weather_model(self) -> Optional[str]:
        """Load and normalize Open-Meteo model selection from config.

        Returns:
            Optional[str]: Model string, or None to omit the models parameter.
        """
        return load_open_meteo_model(self.bot.config, self.logger)

    def get_help_text(self) -> str:
        """Get help text for the command.

        Returns:
            str: Help text string.
        """
        return self.translate('commands.gwx.help')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a weather keyword.

        Args:
            message: The received message.

        Returns:
            bool: True if message matches a keyword, False otherwise.
        """
        return self._cleaned_content_matches(
            message,
            lambda content_lower: any(
                content_lower.startswith(keyword + ' ') or content_lower == keyword
                for keyword in self.keywords
            ),
        )

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
            return None

        if location:
            # Strip whitespace and normalize
            location = location.strip()
            location_lower = location.lower()

            # Look for keys matching custom.wxsim.<location> pattern
            prefix = 'custom.wxsim.'
            for key, value in self.bot.config.items(section):
                if key.startswith(prefix):
                    # Extract the location name from the key (e.g., "custom.wxsim.lethbridge" -> "lethbridge")
                    key_location = key[len(prefix):].strip()
                    if key_location.lower() == location_lower:
                        return value
        else:
            # Check for default source: custom.wxsim.default
            default_key = 'custom.wxsim.default'
            if self.bot.config.has_option(section, default_key):
                return self.bot.config.get(section, default_key)

        return None

    async def _fetch_wxsim(
        self, source: str, forecast_type: str, num_days: int, message: MeshMessage, location: Optional[str]
    ) -> str:
        # Blocking HTTP fetch of the WXSIM plaintext file.
        if location is None:
            return await asyncio.to_thread(self._get_wxsim_weather, source, forecast_type, num_days, message)
        return await asyncio.to_thread(
            self._get_wxsim_weather, source, forecast_type, num_days, message, location
        )

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
            return self.translate('commands.gwx.source_option_not_available')

        # Fetch WXSIM data
        text = self.wxsim_parser.fetch_from_url(source_url, timeout=self.url_timeout)
        if not text:
            return self.translate('commands.gwx.error_fetching')

        # Parse the data
        forecast = self.wxsim_parser.parse(text)

        # Validate forecast is not stale (as wx does)
        is_stale, stale_reason = self.wxsim_parser.is_forecast_stale(forecast, max_age_hours=48)
        if is_stale:
            self.logger.warning(f"WXSIM forecast appears stale: {stale_reason}")

        # Get unit preferences from config
        temp_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
        wind_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()

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
                return self.translate('commands.gwx.tomorrow_not_available')

        elif forecast_type == "multiday":
            # Format multiday forecast
            summary = self.wxsim_parser.format_forecast_summary(forecast, num_days, temp_unit, wind_unit)
            if location_name:
                return f"{location_name}:\n{summary}"
            return summary

        else:
            # Default: current conditions + today's forecast
            current = self.wxsim_parser.format_current_conditions(forecast, temp_unit, wind_unit)

            # Add today's high/low if available
            if forecast.periods:
                today = forecast.periods[0]
                high = self.wxsim_parser._convert_temp(today.high_temp, temp_unit) if today.high_temp is not None else None
                low = self.wxsim_parser._convert_temp(today.low_temp, temp_unit) if today.low_temp is not None else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                hl_today = self._format_high_low(high, low, temp_symbol)
                if hl_today:
                    current += f" | {hl_today}"

                # Add tomorrow if available
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

    def _coordinates_to_location_string(self, lat: float, lon: float) -> Optional[str]:
        """Convert coordinates to a location string (city name) using reverse geocoding.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            Optional[str]: Location string (city name) or None if geocoding fails.
        """
        try:
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
                country = address.get('country', '')

                if city:
                    if state and country:
                        return f"{city}, {state}, {country}"
                    elif state:
                        return f"{city}, {state}"
                    elif country:
                        return f"{city}, {country}"
                    return city
            return None
        except Exception as e:
            self.logger.debug(f"Error reverse geocoding coordinates {lat}, {lon}: {e}")
            return None


    async def execute(self, message: MeshMessage) -> bool:
        """Execute the weather command.

        Args:
            message: The received message.

        Returns:
            bool: True if execution was successful.
        """
        content = message.content.strip()

        # Parse the command to extract location and forecast type
        parts = content.split()

        # An option alone ("gwx hourly") applies to the no-location fallbacks below.
        parts, option_word, option_type, option_days = self._split_option_only(parts, GWX_MULTIDAY_MAX_DAYS)

        # If no location specified, check custom MQTT then WXSIM default sources
        if len(parts) < 2:
            mqtt_topic = self._get_custom_mqtt_weather_topic(None)
            if mqtt_topic:
                return await self._reply_from_mqtt(message, mqtt_topic, option_type, None, split_multiday=False)

            wxsim_source = self._get_custom_wxsim_source(None)  # Check for default
            if wxsim_source:
                return await self._reply_from_wxsim(message, wxsim_source, option_type, option_days)

            # No custom source: the sender's position, default city, then the bot's position.
            # Positions are forecast as coordinates: re-geocoding a reverse-geocoded
            # place name could move it to that town's center; the reply's label
            # comes from one reverse lookup in geocode_location.
            location_str, _ = await self._no_location_fallback(message)
            if location_str is None:
                await self.send_response(message, self.translate('commands.gwx.usage'))
                return True
            parts = [parts[0], location_str]

        if option_word:
            parts.append(option_word)

        if len(parts) == 2 and self._is_custom_source_name(parts[1]):
            location_parts, forecast_type, num_days = parts[1:], "default", 7
        else:
            location_parts, forecast_type, num_days = self._parse_forecast_suffix(
                parts[1:], GWX_MULTIDAY_MAX_DAYS, allow_hourly=True
            )
        if len(parts) > 2 and parts[-1].lower() == "alerts":
            location_parts, forecast_type = parts[1:-1], "alerts"

        # Join remaining parts to handle "city, country" format
        location = ' '.join(location_parts).strip()

        if not location:
            await self.send_response(message, self.translate('commands.gwx.usage'))
            return True

        # Custom MQTT before WXSIM
        mqtt_topic = self._get_custom_mqtt_weather_topic(location)
        if mqtt_topic:
            self.logger.info(f"Using custom MQTT weather topic for location '{location}': {mqtt_topic}")
            return await self._reply_from_mqtt(message, mqtt_topic, forecast_type, location)

        # Check for custom WXSIM source first (before normal geocoding)
        wxsim_source = self._get_custom_wxsim_source(location)
        if wxsim_source:
            return await self._reply_from_wxsim(message, wxsim_source, forecast_type, num_days, location)

        if forecast_type == "alerts":
            await self.send_response(message, self.translate('commands.gwx.source_option_not_available'))
            return True

        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Get weather data for the location
            weather_data = await self.get_weather_for_location(location, forecast_type, num_days, message)

            return await self._send_weather_reply(message, weather_data, forecast_type)

        except Exception as e:
            self.logger.error(f"Error in global weather command: {e}")
            await self.send_response(message, self.translate('commands.gwx.error', error=str(e)))
            return True

    async def get_weather_for_location(self, location: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None) -> Union[str, tuple[str, str, str]]:
        """Get weather data for any global location.

        Args:
            location: The location (city name, etc.).
            forecast_type: "default", "tomorrow", "multiday", or "hourly".
            num_days: Number of days for multiday forecast (2–16).
            message: The MeshMessage for dynamic length calculation.

        Geocoding and the Open-Meteo fetch are both blocking HTTP, so the whole
        body runs on a worker thread; inline it stalled every other coroutine for
        up to several chained 10s timeouts.

        Returns:
            Union[str, Tuple[str, str, str]]: Format string or tuple for multi-message response.
        """
        return await asyncio.to_thread(
            self._get_weather_for_location_sync, location, forecast_type, num_days, message
        )

    def _get_weather_for_location_sync(
        self,
        location: str,
        forecast_type: str = "default",
        num_days: int = 7,
        message: MeshMessage = None,
    ) -> Union[str, tuple[str, str, str]]:
        """Blocking body of :meth:`get_weather_for_location` (runs off the event loop)."""
        try:
            # Convert location to lat/lon with address details
            result = self.geocode_location(location)
            if not result or result[0] is None or result[1] is None:
                return self.translate('commands.gwx.no_location', location=location)

            lat, lon, address_info, geocode_result = result

            # Format location name for display, when it tells the user something
            location_display = ""
            if self._location_label_adds_information(location, address_info):
                location_display = self._format_location_display(address_info, geocode_result, location)
            self.logger.debug(f"Formatted location_display: '{location_display}' from location: '{location}'")
            prefix = f"{location_display}: " if location_display else ""

            # Calculate the length of the location prefix (location_display + ": ").
            # In UTF-8 bytes, not characters: the budget it is subtracted from is a
            # byte budget, and a non-ASCII city name ("München, DE: ") costs more
            # bytes than it has characters.
            location_prefix_len = self._count_display_width(prefix)

            # Get weather forecast from Open-Meteo based on type
            # Pass location_prefix_len so weather formatting can account for it
            current = {}
            if forecast_type == "tomorrow":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="tomorrow", message=message, location_prefix_len=location_prefix_len)
            elif forecast_type == "multiday":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="multiday", num_days=num_days, message=message, location_prefix_len=location_prefix_len)
            elif forecast_type == "hourly":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="hourly", message=message, location_prefix_len=location_prefix_len)
            else:
                weather_text, current = self._get_open_meteo_weather_with_conditions(lat, lon, message=message, location_prefix_len=location_prefix_len)

            # Check if it's an error (translated error message)
            error_fetching = self.translate('commands.gwx.error_fetching')
            if weather_text == error_fetching or weather_text == self.ERROR_FETCHING_DATA:
                return self.translate('commands.gwx.error_fetching_api')

            # Check for severe weather warnings (only for default forecast type)
            if forecast_type == "default":
                alert_text = self._check_extreme_conditions(current)

                if alert_text:
                    # Return multi-message format
                    return ("multi_message", f"{prefix}{weather_text}", alert_text)

            return f"{prefix}{weather_text}"

        except Exception as e:
            self.logger.error(f"Error getting weather for {location}: {e}")
            return self.translate('commands.gwx.error', error=str(e))

    def geocode_location(self, location: str) -> tuple:
        """Convert location string to lat/lon with address details.

        Handles both coordinate strings (lat,lon) and city names.
        Uses geocode_city_sync for proper default state/country handling,
        which prioritizes locations in the configured default state/country.

        Args:
            location: Location string (e.g., "Seattle" or "47.6,-122.3").

        Returns:
            tuple: (lat, lon, address_info, geocode_result) or (None, None, None, None) on failure.
        """
        try:
            # Check if location is coordinates (decimal numbers separated by comma, with optional spaces)
            # Handle formats like: "47.6,-122.3", "47.6, -122.3", "47.980525, -122.150649", " -47.6 , 122.3 "
            if self._COORDINATES_RE.match(location):
                # Parse lat,lon coordinates
                try:
                    lat_str, lon_str = location.split(',')
                    lat = float(lat_str.strip())
                    lon = float(lon_str.strip())

                    # Validate coordinate ranges
                    if not (-90 <= lat <= 90):
                        self.logger.warning(f"Invalid latitude: {lat}. Must be between -90 and 90.")
                        return None, None, None, None
                    if not (-180 <= lon <= 180):
                        self.logger.warning(f"Invalid longitude: {lon}. Must be between -180 and 180.")
                        return None, None, None, None

                    # Get address info via reverse geocoding
                    address_info = None
                    geocode_result = None
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(
                            self.bot, f"{lat}, {lon}", timeout=10
                        )
                        if reverse_location:
                            geocode_result = reverse_location
                            address_info = reverse_location.raw.get('address', {})
                    except Exception as e:
                        self.logger.debug(f"Reverse geocoding failed for coordinates: {e}")
                        address_info = {}

                    return lat, lon, address_info or {}, geocode_result
                except ValueError:
                    self.logger.warning(f"Invalid coordinates format: {location}")
                    return None, None, None, None

            # US ZIP code (5 digits): use geocode_zipcode_sync so the query is "zip, US"
            # and we don't get non‑US matches (e.g. "98104" -> Lithuania) from Nominatim.
            if self._ZIP_RE.match(location.strip()):
                lat, lon = geocode_zipcode_sync(
                    self.bot, location,
                    default_country=self.default_country,
                    timeout=10
                )
                if lat is not None and lon is not None:
                    # A ZIP code is not named in the reply (as in wx), so no reverse lookup,
                    # unless [Weather] always_show_location asks for every place to be named.
                    if not self.always_show_location:
                        return lat, lon, {}, None
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(
                            self.bot, f"{lat}, {lon}", timeout=10
                        )
                    except Exception as e:
                        self.logger.debug(f"Reverse geocoding failed for ZIP code {location}: {e}")
                        reverse_location = None
                    if reverse_location:
                        return lat, lon, reverse_location.raw.get('address', {}) or {}, reverse_location
                    return lat, lon, {}, None
                # Invalid or unknown US ZIP; do not fall through to city (avoids foreign matches)
                return None, None, None, None

            # Use the shared geocode_city_sync function which properly handles
            # default state and country for city disambiguation
            # This ensures "olympia" matches Olympia, WA (not Greece) when default_state=WA
            lat, lon, address_info = geocode_city_sync(
                self.bot, location,
                default_state=self.default_state,
                default_country=self.default_country,
                include_address_info=True,
                timeout=10
            )

            if lat is None or lon is None:
                return None, None, None, None

            # Get full geocode result for display name formatting
            # Try reverse geocoding to get the full result object
            geocode_result = None
            try:
                reverse_location = rate_limited_nominatim_reverse_sync(
                    self.bot, f"{lat}, {lon}", timeout=10
                )
                if reverse_location:
                    geocode_result = reverse_location
            except Exception:
                # If reverse geocoding fails, we still have lat/lon and address_info
                pass

            return lat, lon, address_info or {}, geocode_result

        except Exception as e:
            self.logger.error(f"Error geocoding location {location}: {e}")
            return None, None, None, None

    def _location_label_adds_information(self, location: str, address_info: Optional[dict]) -> bool:
        """Whether the reply should name the place, as wx decides: only when it adds information.

        Coordinates (typed, or the sender's or bot's own position) are named when a
        place was found for them. A ZIP code is not named. A city is named when it
        resolved to another country than [Weather] default_country or, in the US, to
        another state than default_state (or no default_state is set).

        With [Weather] always_show_location, any place a lookup found is named.
        """
        if not address_info:
            return False
        is_zip = bool(self._ZIP_RE.match(location.strip()))
        if is_zip and not self.always_show_location:
            return False
        if is_zip or self._COORDINATES_RE.match(location):
            # Without a place name the label would only repeat the ZIP code or coordinates.
            return any(address_info.get(field) for field in ('city', 'town', 'village', 'municipality', 'city_district'))
        if self.always_show_location:
            return True
        country = (address_info.get('country_code') or '').upper()
        default_country = (self.default_country or '').strip().upper()
        if country and default_country and country != default_country:
            return True
        if country != 'US':
            return False
        state = address_info.get('state') or ''
        if not state or not self.default_state:
            return bool(state)
        return self._state_key(state) != self._state_key(self.default_state)

    def _state_key(self, state: str) -> str:
        """A US state as its abbreviation, for comparing "Washington" with "WA"."""
        abbreviation, _ = normalize_us_state(state)
        return (abbreviation or self._get_state_abbreviation(state.strip())).upper()

    def _format_location_display(self, address_info: dict, geocode_result: Any, fallback: str) -> str:
        """Format location name for display from address info - returns 'City, CountryCode' format.

        Args:
            address_info: Dictionary containing address details.
            geocode_result: Full geocode result object.
            fallback: Fallback location string if detailed info is missing.

        Returns:
            str: Formatted location string (e.g., "Seattle, US").
        """
        # Get country code first (prefer this over full country name)
        country_code = ''
        if address_info:
            country_code = address_info.get('country_code', '').upper()

        # Try to get city name from address_info (this is more reliable than display_name)
        city = None
        if address_info:
            # Try various address fields in order of preference
            city = (address_info.get('city') or
                    address_info.get('town') or
                    address_info.get('village') or
                    address_info.get('municipality') or
                    address_info.get('city_district'))

            # If we still don't have a city, try parsing from display_name
            if not city and geocode_result and hasattr(geocode_result, 'raw'):
                display_name = geocode_result.raw.get('display_name', '')
                if display_name:
                    # Parse display_name - usually format is "Place, City, State/Province, Country"
                    # We want the city, not the specific place
                    parts = [p.strip() for p in display_name.split(',')]
                    # Skip the first part (specific location) and look for city in later parts
                    for i, part in enumerate(parts[1:], 1):
                        # Check if this part looks like a city (not a state/province or country)
                        if i < len(parts) - 1:  # Not the last part (country)
                            city = part
                            break

        # If still no city, try extracting from display_name first part (but clean it up)
        if not city and geocode_result and hasattr(geocode_result, 'raw'):
            display_name = geocode_result.raw.get('display_name', '')
            if display_name:
                parts = [p.strip() for p in display_name.split(',')]
                if parts:
                    # Take first part but try to extract city name
                    first_part = parts[0]
                    # Remove common venue/location suffixes
                    for suffix in [' Terminal', ' Station', ' Airport', ' Hotel', ' Building',
                                   ' Plaza', ' Center', ' Centre', ' Park', ' Square']:
                        if suffix in first_part:
                            first_part = first_part.replace(suffix, '').strip()
                    city = first_part

        # For US locations, include state abbreviation
        if country_code == 'US':
            state = None
            if address_info:
                state = address_info.get('state')
            if city and state:
                state_abbrev = self._get_state_abbreviation(state)
                return f"{city}, {state_abbrev}"
            elif city:
                return f"{city}, US"

        # For international locations, always use country code if available
        if city:
            if country_code:
                return f"{city}, {country_code}"
            elif address_info and address_info.get('country'):
                # Fallback to country name if no code available
                country = address_info.get('country')
                # Shorten very long country names
                if len(country) > 15:
                    return f"{city}, {country[:15]}"
                return f"{city}, {country}"
            else:
                return city

        # Final fallback: try to extract from input and capitalize
        if fallback:
            # Try to extract city name from input (before first comma if present)
            parts = fallback.split(',')
            city_part = parts[0].strip().title()
            # Remove common suffixes
            for suffix in [' Terminal', ' Station', ' Airport', ' Hotel', ' Building']:
                if suffix in city_part:
                    city_part = city_part.replace(suffix, '').strip()

            if country_code:
                return f"{city_part}, {country_code}"
            elif len(parts) > 1:
                # Try to get country from input
                country_part = parts[-1].strip()
                return f"{city_part}, {country_part[:10]}"  # Limit country name length
            return city_part

        return fallback.title()

    def _get_state_abbreviation(self, state: str) -> str:
        """Convert full state name to abbreviation.

        Args:
            state: Full state name (e.g., "Washington").

        Returns:
            str: Two-letter state abbreviation (e.g., "WA") or original string if not found.
        """
        state_map = {
            'Washington': 'WA', 'California': 'CA', 'New York': 'NY', 'Texas': 'TX',
            'Florida': 'FL', 'Illinois': 'IL', 'Pennsylvania': 'PA', 'Ohio': 'OH',
            'Georgia': 'GA', 'North Carolina': 'NC', 'Michigan': 'MI', 'New Jersey': 'NJ',
            'Virginia': 'VA', 'Tennessee': 'TN', 'Indiana': 'IN', 'Arizona': 'AZ',
            'Massachusetts': 'MA', 'Missouri': 'MO', 'Maryland': 'MD', 'Wisconsin': 'WI',
            'Colorado': 'CO', 'Minnesota': 'MN', 'South Carolina': 'SC', 'Alabama': 'AL',
            'Louisiana': 'LA', 'Kentucky': 'KY', 'Oregon': 'OR', 'Oklahoma': 'OK',
            'Connecticut': 'CT', 'Utah': 'UT', 'Iowa': 'IA', 'Nevada': 'NV',
            'Arkansas': 'AR', 'Mississippi': 'MS', 'Kansas': 'KS', 'New Mexico': 'NM',
            'Nebraska': 'NE', 'West Virginia': 'WV', 'Idaho': 'ID', 'Hawaii': 'HI',
            'New Hampshire': 'NH', 'Maine': 'ME', 'Montana': 'MT', 'Rhode Island': 'RI',
            'Delaware': 'DE', 'South Dakota': 'SD', 'North Dakota': 'ND', 'Alaska': 'AK',
            'Vermont': 'VT', 'Wyoming': 'WY'
        }
        return state_map.get(state, state)

    def get_open_meteo_weather(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, location_prefix_len: int = 0) -> str:
        """Get weather forecast from Open-Meteo API.

        Args:
            lat: Latitude.
            lon: Longitude.
            forecast_type: "default", "tomorrow", "multiday", or "hourly".
            num_days: Number of days for multiday forecast (2–16).
            message: The MeshMessage for dynamic length calculation.
            location_prefix_len: Length of location prefix (e.g., "City, CC: ") that will be added later.

        Returns:
            str: Formatted weather string or error message.
        """
        weather, _ = self._get_open_meteo_weather_with_conditions(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )
        return weather

    def _get_open_meteo_weather_with_conditions(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, location_prefix_len: int = 0) -> tuple[str, dict]:
        """Return the reply and current conditions from the same Open-Meteo request."""
        # Get max message length dynamically, then subtract location prefix length
        max_length = self.get_max_message_length(message) if message else 130
        max_length = max_length - location_prefix_len  # Account for location prefix
        try:
            # Open-Meteo API endpoint with current weather and forecast
            api_url = "https://api.open-meteo.com/v1/forecast"

            # Determine forecast_days based on type
            if forecast_type == "multiday":
                # Index 0 is today and the forecast starts tomorrow, so N days need N+1
                # (Open-Meteo returns at most 16).
                forecast_days = min(num_days + 1, 16)
            elif forecast_type == "tomorrow":
                forecast_days = 2  # Need today and tomorrow
            else:
                forecast_days = 2  # Default

            params = {
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,dewpoint_2m,visibility,surface_pressure,is_day',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max',
                'hourly': 'temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m',
                'temperature_unit': self.temperature_unit,
                'wind_speed_unit': self.wind_speed_unit,
                'precipitation_unit': self.precipitation_unit,
                'timezone': 'auto',
                'forecast_days': forecast_days
            }
            if self.weather_model:
                params['models'] = self.weather_model
            if forecast_type == "hourly":
                params['hourly'] += ',precipitation_probability,is_day'

            response = requests.get(api_url, params=params, timeout=self.url_timeout)

            if not response.ok:
                self.logger.warning(f"Error fetching weather from Open-Meteo: {response.status_code}")
                return self.translate('commands.gwx.error_fetching'), {}

            data = response.json()

            if forecast_type == "tomorrow":
                return self.format_tomorrow_forecast(data, max_length), {}
            if forecast_type == "multiday":
                return self.format_multiday_forecast(data, num_days), {}
            if forecast_type == "hourly":
                return self._open_meteo_hourly(data, max_length), {}

            temp_symbol = "°F" if self.temperature_unit == 'fahrenheit' else "°C"
            weather = self._open_meteo_current(data, max_length, temp_symbol)
            if weather is None:
                return self.translate('commands.gwx.error_fetching'), {}
            # Forecast high/low for today (the current conditions already name the period).
            daily = data.get('daily', {})
            if daily:
                weather = self._open_meteo_daily_tail(weather, daily, max_length, temp_symbol)
            return weather, data.get('current', {})

        except Exception as e:
            self.logger.error(f"Error fetching Open-Meteo weather: {e}")
            return self.translate('commands.gwx.error_fetching'), {}

    def _open_meteo_hourly(self, data: dict, max_length: int) -> str:
        """Hours after Open-Meteo's current local hour, packed into one reply."""
        try:
            current_time = datetime.fromisoformat(data.get('current', {}).get('time', ''))
        except (ValueError, TypeError):
            return self.translate('commands.gwx.hourly_not_available')
        next_hour = current_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        hourly = data.get('hourly', {})
        lines = []
        for i, time_str in enumerate(hourly.get('time', [])):
            try:
                start_time = datetime.fromisoformat(time_str)
            except (ValueError, TypeError):
                continue
            if start_time < next_hour:
                continue

            def value(field: str) -> Any:
                values = hourly.get(field, [])
                return values[i] if i < len(values) else None

            temperature = value('temperature_2m')
            code = value('weather_code')
            if temperature is None or code is None:
                break
            is_day = value('is_day')
            daytime = bool(is_day) if is_day is not None else 6 <= start_time.hour < 18
            parts = [f"{self._hour_label(time_str)}:", self._get_weather_emoji(code, is_day=daytime)]
            probability = value('precipitation_probability')
            if probability is not None and probability > 0:
                parts.append(f"{int(probability)}%")
            parts.append(self._short_hourly_description(self._get_weather_description(code)))
            parts.append(f"{int(temperature)}°")
            speed = value('wind_speed_10m')
            direction = value('wind_direction_10m')
            if speed is not None and direction is not None:
                parts.append(f"{self._without_arrow(self._degrees_to_direction(direction))}{int(speed)}")
            lines.append(" ".join(parts))
        return self._pack_hourly_lines(lines, max_length)

    def _open_meteo_current(self, data: dict, max_length: int, temp_symbol: str) -> Optional[str]:
        """Current conditions for the default gwx reply, with extra conditions when they fit.

        None when the response has no current temperature: a reply built from
        defaults would invent "0°, 0%RH" weather. Other missing or null
        current fields are left out of the reply one by one.
        """
        # Check units in response to verify API is respecting our unit requests
        current_units = data.get('current_units', {})
        visibility_unit = current_units.get('visibility', 'm')

        # Extract current conditions
        current = data.get('current', {})

        # Current conditions - API should return in Fahrenheit when requested
        if current.get('temperature_2m') is None:
            self.logger.warning("Open-Meteo response has no current temperature")
            return None
        temp = int(current['temperature_2m'])
        feels_like = _int_or_none(current.get('apparent_temperature'))
        if feels_like is None:
            feels_like = temp
        dewpoint = current.get('dewpoint_2m')
        humidity = _int_or_none(current.get('relative_humidity_2m'))
        wind_speed = _int_or_none(current.get('wind_speed_10m'))
        wind_direction = self._degrees_to_direction(current.get('wind_direction_10m'))
        wind_gusts = _int_or_none(current.get('wind_gusts_10m'))
        visibility = current.get('visibility')
        pressure = current.get('surface_pressure')
        weather_code = current.get('weather_code')

        # Convert visibility to miles based on actual unit from API
        # API returns visibility in feet when using imperial units
        if visibility is not None:
            if visibility_unit == 'ft' or 'ft' in str(visibility_unit).lower():
                # Convert from feet to miles (1 mile = 5280 feet)
                visibility_mi = visibility / 5280.0
            else:
                # Assume meters, convert to miles (1 mile = 1609.34 meters)
                visibility_mi = visibility / 1609.34
        else:
            visibility_mi = None

        # Pressure validation - account for high elevation locations
        # Normal sea level pressure is 1013 hPa, range is typically 950-1050 hPa
        # At high elevations (e.g., 2500m), pressure can be 750-800 hPa, which is normal
        # Only filter out extremely low pressures (< 600 hPa) which would be invalid
        if pressure is not None and pressure < 600:
            self.logger.warning(f"Extremely low pressure value: {pressure} hPa - might be invalid")
            pressure = None

        # Get weather description and emoji (none for a missing code, rather than "clear")
        if weather_code is None:
            condition = ""
        else:
            emoji = self._get_weather_emoji(weather_code, is_day=self._open_meteo_daytime(current))
            condition = f"{emoji}{self._get_weather_description(weather_code)} "

        period_name = self.translate(f'commands.gwx.periods.{self._open_meteo_period_key(current)}')

        # Build current weather string
        weather = f"{period_name}: {condition}{temp}{temp_symbol}"

        # Add feels like if significantly different
        if abs(feels_like - temp) >= 5:
            feels_str = self.translate('commands.gwx.feels_like', value=feels_like, unit=temp_symbol)
            weather += f" {feels_str}"

        # Add wind info (always show if >= 3 mph, show gusts if significant)
        if wind_speed is not None and wind_speed >= 3:
            weather += f" {wind_direction}{wind_speed}"
            if wind_gusts is not None and wind_gusts > wind_speed + 3:
                gust_str = self.translate('commands.gwx.gust', value=wind_gusts)
                weather += gust_str

        # Add humidity
        if humidity is not None:
            humidity_str = self.translate('commands.gwx.humidity', value=humidity)
            weather += f" {humidity_str}"

        # Add additional conditions if space allows
        conditions = []

        # Add dew point
        if dewpoint is not None:
            dewpoint_val = int(dewpoint)
            dew_str = self.translate('commands.gwx.dew_point', value=dewpoint_val, unit=temp_symbol)
            conditions.append(dew_str)

        # Add visibility (already converted to miles above)
        if visibility_mi is not None and visibility_mi > 0:
            # Beyond ~20 mi visibility is essentially unlimited, so cap the
            # display at that in whichever unit we are showing.
            if self.metric_distance:
                visibility_display = min(int(visibility_mi * MI_TO_KM), VISIBILITY_CAP_KM)
                vis_str = self.translate('commands.gwx.visibility_km', value=visibility_display)
            else:
                visibility_display = min(int(visibility_mi), VISIBILITY_CAP_MI)
                vis_str = self.translate('commands.gwx.visibility', value=visibility_display)
            conditions.append(vis_str)

        # Add pressure (convert from hPa to display format)
        if pressure is not None:
            pressure_hpa = int(pressure)
            # Which pressure unit reads as normal is a locale convention, not
            # a metric/imperial split: Russia uses mmHg, most of metric
            # Europe uses hPa. The catalog names its own.
            if self.translate('commands.gwx.pressure_unit').strip().lower() == 'mmhg':
                press_str = self.translate('commands.gwx.pressure_mmhg',
                                           value=round(pressure_hpa * HPA_TO_MMHG))
            else:
                press_str = self.translate('commands.gwx.pressure', value=pressure_hpa)
            conditions.append(press_str)

        # Add conditions to weather string if space allows
        # Reserve space for forecast data (high/low and tomorrow)
        conditions_max_length = max_length - 80  # Reserve ~80 chars for forecast data
        if conditions and self._count_display_width(weather) < conditions_max_length:
            weather += " " + " ".join(conditions)
        return weather

    def _open_meteo_daily_tail(self, weather: str, daily: dict, max_length: int, temp_symbol: str) -> str:
        """Append today's high/low, then tomorrow and its precipitation while they fit."""
        today_high = int(daily['temperature_2m_max'][0])
        today_low = int(daily['temperature_2m_min'][0])

        weather += f" | {self._format_high_low(today_high, today_low, temp_symbol)}"

        # Add tomorrow if space allows (check length more carefully)
        if len(daily['temperature_2m_max']) > 1:
            tomorrow_high = int(daily['temperature_2m_max'][1])
            tomorrow_low = int(daily['temperature_2m_min'][1])

            tomorrow_code = daily['weather_code'][1]
            tomorrow_emoji = self._get_weather_emoji(tomorrow_code)

            # Get tomorrow's period name
            tomorrow_period = self.translate('commands.gwx.periods.tomorrow')
            tomorrow_hl = self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
            tomorrow_str = f" | {tomorrow_period}: {tomorrow_emoji} {tomorrow_hl}"
            # Name tomorrow's weather when it fits; the emoji alone is ambiguous.
            if tomorrow_code is not None:
                described = (f" | {tomorrow_period}: {tomorrow_emoji}"
                             f"{self._get_weather_description(tomorrow_code)} {tomorrow_hl}")
                if self._count_display_width(weather + described) <= max_length - 10:
                    tomorrow_str = described

            # Only add if we have space (leave room for potential precipitation)
            # Use display width to account for emojis
            if self._count_display_width(weather + tomorrow_str) <= max_length - 10:  # Leave 10 chars buffer
                weather += tomorrow_str

                # Add precipitation probability and amount if significant and space allows
                if len(daily.get('precipitation_probability_max', [])) > 1:
                    precip_prob = daily['precipitation_probability_max'][1]
                    if precip_prob is not None and precip_prob >= 30:
                        # Get precipitation amount if available
                        precip_amount = None
                        if len(daily.get('precipitation_sum', [])) > 1:
                            precip_amount = daily['precipitation_sum'][1]

                        # Format precipitation info
                        if precip_amount is not None and precip_amount > 0:
                            # Show both probability and amount
                            precip_unit = "in" if self.precipitation_unit == 'inch' else "mm"
                            precip_str = f" 🌦️{precip_prob}% {precip_amount:.2f}{precip_unit}"
                        else:
                            # Only show probability if no amount available
                            precip_str = f" 🌦️{precip_prob}%"

                        # Use display width to check if we have space, with buffer to avoid cutting emojis
                        # Add buffer of 5 chars to ensure we don't truncate in middle of emoji
                        if self._count_display_width(weather + precip_str) <= max_length - 5:
                            weather += precip_str
        return weather

    def _open_meteo_daytime(self, current: dict) -> bool:
        """Whether the sun is up at the location: Open-Meteo's is_day, else 06:00-18:00 local."""
        flag = current.get('is_day') if isinstance(current, dict) else None
        if flag is not None:
            return bool(flag)
        return 6 <= self._open_meteo_local_hour(current) < 18

    def _open_meteo_period_key(self, current: dict) -> str:
        """Period label key for the current conditions: today while the sun is up,
        overnight from midnight to sunrise, tonight from sunset to midnight.

        Open-Meteo's current time is local to the location (timezone=auto); the
        bot's own clock is only the fallback.
        """
        if self._open_meteo_daytime(current):
            return 'today'
        return 'overnight' if self._open_meteo_local_hour(current) < 12 else 'tonight'

    @staticmethod
    def _open_meteo_local_hour(current: dict) -> int:
        """Hour of day at the location from Open-Meteo's current time ("2026-10-01T19:45"), else the bot's."""
        stamp = current.get('time') if isinstance(current, dict) else None
        if isinstance(stamp, str):
            try:
                return datetime.fromisoformat(stamp).hour
            except ValueError:
                pass
        return datetime.now().hour

    @staticmethod
    def _open_meteo_date(dates: list, index: int):
        """The date at *index* of Open-Meteo's daily time array, or None."""
        if index < len(dates) and isinstance(dates[index], str):
            try:
                return datetime.strptime(dates[index][:10], '%Y-%m-%d')
            except ValueError:
                return None
        return None

    def format_tomorrow_forecast(self, data: dict, max_length: Optional[int] = None) -> str:
        """Format a detailed forecast for tomorrow.

        Args:
            data: Weather data dictionary from Open-Meteo.
            max_length: UTF-8 byte budget; details are left out, least important
                first, until the reply fits. None keeps every detail.

        Returns:
            str: Formatted tomorrow forecast string.
        """
        try:
            daily = data.get('daily', {})
            if not daily or len(daily.get('temperature_2m_max', [])) < 2:
                return self.translate('commands.gwx.tomorrow_not_available')
            if daily['temperature_2m_max'][1] is None or daily['temperature_2m_min'][1] is None:
                return self.translate('commands.gwx.tomorrow_not_available')

            temp_symbol = "°F" if self.temperature_unit == 'fahrenheit' else "°C"
            tomorrow_high = int(daily['temperature_2m_max'][1])
            tomorrow_low = int(daily['temperature_2m_min'][1])
            tomorrow_code = daily['weather_code'][1]
            tomorrow_emoji = self._get_weather_emoji(tomorrow_code)
            tomorrow_desc = self._get_weather_description(tomorrow_code)

            # Get wind info if available
            wind_info = ""
            gust_info = ""
            if len(daily.get('wind_speed_10m_max', [])) > 1:
                wind_speed = int(daily['wind_speed_10m_max'][1])
                if wind_speed >= 3:
                    wind_info = f" {wind_speed}"
                    if len(daily.get('wind_gusts_10m_max', [])) > 1:
                        wind_gusts = int(daily['wind_gusts_10m_max'][1])
                        if wind_gusts > wind_speed + 3:
                            gust_info = self.translate('commands.gwx.gust', value=wind_gusts)

            # Get precipitation probability and amount
            precip_info = ""
            precip_chance = ""
            if len(daily.get('precipitation_probability_max', [])) > 1:
                precip_prob = daily['precipitation_probability_max'][1]
                if precip_prob is not None and precip_prob >= 30:
                    # Get precipitation amount if available
                    precip_amount = None
                    if len(daily.get('precipitation_sum', [])) > 1:
                        precip_amount = daily['precipitation_sum'][1]

                    # Format precipitation info
                    precip_chance = f" 🌦️{precip_prob}%"
                    if precip_amount is not None and precip_amount > 0:
                        # Show both probability and amount
                        precip_unit = "in" if self.precipitation_unit == 'inch' else "mm"
                        precip_info = f" 🌦️{precip_prob}% {precip_amount:.2f}{precip_unit}"
                    else:
                        # Only show probability if no amount available
                        precip_info = f" 🌦️{precip_prob}%"

            tomorrow_period = self.translate('commands.gwx.periods.tomorrow')
            hl = self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
            head = f"{tomorrow_period}: {tomorrow_emoji}"
            # Least important detail first: precipitation amount, gusts, wind, chance, description.
            candidates = [
                f"{head}{tomorrow_desc} {hl}{wind_info}{gust_info}{precip_info}",
                f"{head}{tomorrow_desc} {hl}{wind_info}{gust_info}{precip_chance}",
                f"{head}{tomorrow_desc} {hl}{wind_info}{precip_chance}",
                f"{head}{tomorrow_desc} {hl}{precip_chance}",
                f"{head}{tomorrow_desc} {hl}",
                f"{head} {hl}",
            ]
            if max_length is None:
                return candidates[0]
            for candidate in candidates:
                if self._count_display_width(candidate) <= max_length:
                    return candidate
            return candidates[-1]

        except Exception as e:
            self.logger.error(f"Error formatting tomorrow forecast: {e}")
            return self.translate('commands.gwx.tomorrow_error')

    def format_multiday_forecast(self, data: dict, num_days: int = 7) -> str:
        """Format a less detailed multi-day forecast summary.

        Args:
            data: Weather data dictionary from Open-Meteo.
            num_days: Number of days to include in forecast.

        Returns:
            str: Formatted multi-day forecast string (newlines separate days).
        """
        try:
            daily = data.get('daily', {})
            if not daily:
                return self.translate('commands.gwx.multiday_not_available', num_days=num_days)

            temp_symbol = "°F" if self.temperature_unit == 'fahrenheit' else "°C"
            temps_max = daily.get('temperature_2m_max', [])
            temps_min = daily.get('temperature_2m_min', [])
            weather_codes = daily.get('weather_code', [])

            if len(temps_max) < num_days + 1:  # +1 because index 0 is today
                num_days = len(temps_max) - 1

            # Map day names to 1-2 letter abbreviations
            day_abbrev_map = {
                'Monday': self.translate('commands.gwx.day_abbrev.Monday'),
                'Tuesday': self.translate('commands.gwx.day_abbrev.Tuesday'),
                'Wednesday': self.translate('commands.gwx.day_abbrev.Wednesday'),
                'Thursday': self.translate('commands.gwx.day_abbrev.Thursday'),
                'Friday': self.translate('commands.gwx.day_abbrev.Friday'),
                'Saturday': self.translate('commands.gwx.day_abbrev.Saturday'),
                'Sunday': self.translate('commands.gwx.day_abbrev.Sunday')
            }

            parts = []
            today = datetime.now()
            day_dates = daily.get('time') or []

            # Start from tomorrow (index 1)
            for i in range(1, min(num_days + 1, len(temps_max))):
                # Open-Meteo pads the days past a model's horizon with nulls
                # (up to 9 of 16 with icon_seamless), so the forecast ends there.
                if temps_max[i] is None or i >= len(temps_min) or temps_min[i] is None:
                    break
                # Label from Open-Meteo's own (location-local) date when present.
                day_date = self._open_meteo_date(day_dates, i) or (today + timedelta(days=i))
                day_name = day_date.strftime('%A')
                day_abbrev = day_abbrev_map.get(day_name, day_name[:2])

                high = int(temps_max[i])
                low = int(temps_min[i])
                code = weather_codes[i] if i < len(weather_codes) else 0
                emoji = self._get_weather_emoji(code)
                desc = self._get_weather_description(code)

                # Abbreviate description if needed
                desc_short = desc
                if len(desc) > 20:
                    desc_short = desc[:17] + "..."

                parts.append(f"{day_abbrev}: {emoji}{desc_short} {self._format_high_low(high, low, temp_symbol)}")

            if not parts:
                return self.translate('commands.gwx.multiday_not_available', num_days=num_days)

            return "\n".join(parts)

        except Exception as e:
            self.logger.error(f"Error formatting {num_days}-day forecast: {e}")
            return self.translate('commands.gwx.multiday_error', num_days=num_days)

    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction with emoji.

        Uses the nearest of the 16 compass points (as NOAA's own labels do) and
        the nearest 8-point arrow: 22.5° is NNE, 350° is N. Letters come from the
        translation catalog.

        Args:
            degrees: Wind direction in degrees.

        Returns:
            str: Compass direction string with emoji (e.g., "↗️NNE").
        """
        if degrees is None:
            return ""
        degrees = float(degrees)
        if not math.isfinite(degrees):
            return ""
        index = int((degrees % 360) / 22.5 + 0.5) % 16
        key = _COMPASS_16[index]
        arrow = _ARROWS_8[int(index / 2 + 0.5) % 8]
        translated = self.translate(f"common.wind_directions.{key}")
        return f"{arrow}{translated}"

    def _get_weather_description(self, code: int) -> str:
        """Convert WMO weather code to description.

        Args:
            code: WMO weather code.

        Returns:
            str: Weather description.
        """
        # Try to get from translations first
        key = f"commands.gwx.weather_descriptions.{code}"
        description = self.translate(key)

        # If translation returned the key (not found), try fallback
        if description == key:
            # Fallback to hardcoded descriptions
            weather_codes = {
                0: "Clear",
                1: "Mostly Clear",
                2: "Partly Cloudy",
                3: "Overcast",
                45: "Foggy",
                48: "Foggy",
                51: "Light Drizzle",
                53: "Drizzle",
                55: "Heavy Drizzle",
                56: "Light Freezing Drizzle",
                57: "Freezing Drizzle",
                61: "Light Rain",
                63: "Rain",
                65: "Heavy Rain",
                66: "Light Freezing Rain",
                67: "Freezing Rain",
                71: "Light Snow",
                73: "Snow",
                75: "Heavy Snow",
                77: "Snow Grains",
                80: "Light Showers",
                81: "Showers",
                82: "Heavy Showers",
                85: "Light Snow Showers",
                86: "Snow Showers",
                95: "Thunderstorm",
                96: "T-Storm w/Hail",
                97: "Heavy T-Storm",
                99: "Severe T-Storm"
            }
            return weather_codes.get(code, self.translate('commands.gwx.weather_descriptions.unknown'))

        return description

    def _get_weather_emoji(self, code: int, is_day: Optional[bool] = True) -> str:
        """Convert WMO weather code to emoji.

        Args:
            code: WMO weather code.
            is_day: False at night, when clear skies show a moon instead of a sun.

        Returns:
            str: Weather emoji.
        """
        emoji_map = {
            0: "☀️",      # Clear
            1: "🌤️",     # Mostly Clear
            2: "⛅",     # Partly Cloudy
            3: "☁️",      # Overcast
            45: "🌫️",    # Fog
            48: "🌫️",    # Fog
            51: "🌦️",    # Drizzle
            53: "🌦️",    # Drizzle
            55: "🌧️",    # Heavy Drizzle
            56: "🌧️",    # Freezing Drizzle
            57: "🌧️",    # Freezing Drizzle
            61: "🌧️",    # Rain
            63: "🌧️",    # Rain
            65: "🌧️",    # Heavy Rain
            66: "🌧️",    # Freezing Rain
            67: "🌧️",    # Freezing Rain
            71: "❄️",     # Snow
            73: "❄️",     # Snow
            75: "❄️",     # Heavy Snow
            77: "❄️",     # Snow Grains
            80: "🌦️",    # Showers
            81: "🌦️",    # Showers
            82: "🌧️",    # Heavy Showers
            85: "🌨️",    # Snow Showers
            86: "🌨️",    # Snow Showers
            95: "⛈️",     # Thunderstorm
            96: "⛈️",     # Thunderstorm with Hail
            97: "⛈️",     # Heavy Thunderstorm
            99: "⛈️"      # Severe Thunderstorm
        }

        if is_day is False and code in (0, 1):
            return "🌙"
        return emoji_map.get(code, "🌤️")

    def _check_extreme_conditions(self, current: dict) -> Optional[str]:
        """Check for extreme weather conditions that warrant warnings.

        Args:
            current: Open-Meteo current conditions in the configured units.

        Returns:
            Optional[str]: Warning text if conditions found, None otherwise.
        """
        warnings = []

        # Keep the thresholds in Fahrenheit and mph, regardless of reply units.
        temp = current.get('temperature_2m')
        if temp is not None:
            if self.temperature_unit == 'celsius':
                temp = temp * 9 / 5 + 32
            if temp >= 95:
                warnings.append(self.translate('commands.gwx.warnings.extreme_heat'))
            elif temp <= 20:
                warnings.append(self.translate('commands.gwx.warnings.extreme_cold'))

        code = current.get('weather_code')
        if code in (65, 82):
            warnings.append(self.translate('commands.gwx.warnings.heavy_rain'))

        if code in (95, 96, 97, 99):
            warnings.append(self.translate('commands.gwx.warnings.thunderstorms'))

        # The old "Snow Showers" match also covered light snow showers (85).
        if code in (75, 85, 86):
            warnings.append(self.translate('commands.gwx.warnings.heavy_snow'))

        # Sustained wind only, as before. The threshold is 30 mph; the warning shows the
        # speed in the configured unit, like the wind in the reply it follows.
        wind_factor = {'mph': 1, 'kmh': MI_TO_KM, 'ms': 0.44704, 'kn': 0.868976}[self.wind_speed_unit]
        wind = current.get('wind_speed_10m') or 0
        if wind / wind_factor >= 30:
            unit = self.translate(f'services.weather_service.wind_speed_units.{self.wind_speed_unit}')
            warnings.append(self.translate('commands.gwx.warnings.high_winds', wind_speed=int(wind), unit=unit))

        return " | ".join(warnings) if warnings else None
