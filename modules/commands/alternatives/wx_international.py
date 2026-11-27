#!/usr/bin/env python3
"""
Global Weather command for the MeshCore Bot
Provides worldwide weather information using Open-Meteo API
"""

import asyncio
import math  # noqa: F401  re-exported
import re
from dataclasses import replace
from datetime import datetime, timedelta  # noqa: F401  re-exported
from typing import Any, Optional, Union

import requests  # noqa: F401  module attribute other code patches

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
from ...weather_common import (
    WeatherCommandMixin,
    load_open_meteo_model,  # noqa: F401  re-exported
    load_open_meteo_units,
)
from ...weather_providers.base import WeatherUnits
from ...weather_providers.openmeteo import (  # noqa: F401  re-exported
    GWX_MULTIDAY_MAX_DAYS,
    HPA_TO_MMHG,
    MI_TO_KM,
    OPEN_METEO_TIMEOUT,
    VISIBILITY_CAP_KM,
    VISIBILITY_CAP_MI,
    OpenMeteoProvider,
    weather_emoji,
)
from ..base_command import BaseCommand

# Kept for code that checked them; these imports used to be optional.
WXSIM_PARSER_AVAILABLE = True


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

        # The Open-Meteo fetch and formatting live in the provider; this command
        # keeps the request (parsing, geocoding, labels) and the sending.
        self.provider = OpenMeteoProvider(
            host=self,
            units=WeatherUnits(*load_open_meteo_units(self.bot.config, self.logger)),
        )

        # Get default location/state/country from config for fallback/disambiguation
        self.default_city = self.bot.config.get('Weather', 'default_city', fallback='').strip()
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
        self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
        self.always_show_location = self.bot.config.getboolean('Weather', 'always_show_location', fallback=False)

        # Initialize geocoder (will use rate-limited helpers for actual calls)
        self.geolocator = get_nominatim_geocoder()

        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager

    # Units and the model selection belong to the provider; these forward so
    # code that assigns them on the command (wx's Open-Meteo fallback does)
    # still reaches the request the provider builds.

    @property
    def temperature_unit(self) -> str:
        """Temperature unit the provider asks Open-Meteo for."""
        return self.provider.units.temperature

    @temperature_unit.setter
    def temperature_unit(self, value: str) -> None:
        self.provider.units = replace(self.provider.units, temperature=value)

    @property
    def wind_speed_unit(self) -> str:
        """Wind speed unit the provider asks Open-Meteo for."""
        return self.provider.units.wind_speed

    @wind_speed_unit.setter
    def wind_speed_unit(self, value: str) -> None:
        self.provider.units = replace(self.provider.units, wind_speed=value)

    @property
    def precipitation_unit(self) -> str:
        """Precipitation unit the provider asks Open-Meteo for."""
        return self.provider.units.precipitation

    @precipitation_unit.setter
    def precipitation_unit(self, value: str) -> None:
        self.provider.units = replace(self.provider.units, precipitation=value)

    @property
    def weather_model(self) -> Optional[str]:
        """Open-Meteo model selection, or None to omit the models parameter."""
        return self.provider.weather_model

    @weather_model.setter
    def weather_model(self, value: Optional[str]) -> None:
        self.provider.weather_model = value

    @property
    def metric_distance(self) -> bool:
        """Whether distances should be shown in kilometers.

        Derived from [Weather] temperature_unit rather than the response
        language so every unit in one reply agrees: a bot configured for
        Fahrenheit should not print kilometers just because it answers in
        Russian.
        """
        return self.provider.metric_distance

    def _load_weather_model(self) -> Optional[str]:
        """Load and normalize Open-Meteo model selection from config.

        Returns:
            Optional[str]: Model string, or None to omit the models parameter.
        """
        return self.provider.load_weather_model()

    def get_help_text(self, message: MeshMessage | None = None) -> str:
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
            return self._weather_for_point(
                location, lat, lon, address_info, geocode_result, forecast_type, num_days, message
            )

        except Exception as e:
            self.logger.error(f"Error getting weather for {location}: {e}")
            return self.translate('commands.gwx.error', error=str(e))

    def _weather_for_point(
        self,
        location: str,
        lat: float,
        lon: float,
        address_info: Optional[dict],
        geocode_result: Any,
        forecast_type: str = "default",
        num_days: int = 7,
        message: MeshMessage = None,
    ) -> Union[str, tuple[str, str, str]]:
        """The Open-Meteo reply for a place already geocoded, labeled when the label adds information.

        Also wx's answer for a place outside NWS coverage ([Weather] openmeteo_fallback).
        """
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
                return self._geocode_coordinates(location)

            # US ZIP code (5 digits): use geocode_zipcode_sync so the query is "zip, US"
            # and we don't get non‑US matches (e.g. "98104" -> Lithuania) from Nominatim.
            if self._ZIP_RE.match(location.strip()):
                return self._geocode_zipcode(location)

            return self._geocode_city(location)

        except Exception as e:
            self.logger.error(f"Error geocoding location {location}: {e}")
            return None, None, None, None

    def _geocode_coordinates(self, location: str) -> tuple:
        """geocode_location for a "lat,lon" string: validate, then reverse geocode for a label."""
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

    def _geocode_zipcode(self, location: str) -> tuple:
        """geocode_location for a US ZIP code (never falls through to a city lookup)."""
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

    def _geocode_city(self, location: str) -> tuple:
        """geocode_location for a place name, preferring the default state and country."""
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

        city = self._display_city(address_info, geocode_result)

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

        return self._fallback_location_label(fallback, country_code)

    def _display_city(self, address_info: dict, geocode_result: Any) -> Optional[str]:
        """The city part of _format_location_display: address fields, then display_name."""
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

        return city

    def _fallback_location_label(self, fallback: str, country_code: str) -> str:
        """_format_location_display's label when no city was found: built from the input."""
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

    # The Open-Meteo fetch and formatting live in OpenMeteoProvider. These keep
    # the command's own surface: the public ones are API contract, the private
    # ones are what tests and wx's fallback patch on the command.

    def get_open_meteo_weather(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, location_prefix_len: int = 0) -> str:
        """Get weather forecast from Open-Meteo API.

        Args:
            lat: Latitude.
            lon: Longitude.
            forecast_type: "default", "tomorrow", "multiday", or "hourly".
            num_days: Number of days for multiday forecast (2-16).
            message: The MeshMessage for dynamic length calculation.
            location_prefix_len: Length of location prefix (e.g., "City, CC: ") that will be added later.

        Returns:
            str: Formatted weather string or error message.
        """
        return self.provider.get_open_meteo_weather(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )

    def _get_open_meteo_weather_with_conditions(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, location_prefix_len: int = 0) -> tuple[str, dict]:
        """Return the reply and current conditions from the same Open-Meteo request."""
        return self.provider._get_open_meteo_weather_with_conditions(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )

    def _open_meteo_hourly(self, data: dict, max_length: int) -> str:
        """Hours after Open-Meteo's current local hour, packed into one reply."""
        return self.provider._open_meteo_hourly(data, max_length)

    def _open_meteo_current(self, data: dict, max_length: int, temp_symbol: str) -> Optional[str]:
        """Current conditions for the default gwx reply, or None without a temperature."""
        return self.provider._open_meteo_current(data, max_length, temp_symbol)

    def _visibility_miles(self, visibility, visibility_unit) -> Optional[float]:
        """Open-Meteo's visibility in miles, from feet or meters as current_units says."""
        return self.provider._visibility_miles(visibility, visibility_unit)

    def _open_meteo_extra_conditions(self, dewpoint, visibility_mi, pressure, temp_symbol: str) -> list:
        """Dew point, visibility and pressure strings, each only when its value is present."""
        return self.provider._open_meteo_extra_conditions(dewpoint, visibility_mi, pressure, temp_symbol)

    def _open_meteo_daily_tail(self, weather: str, daily: dict, max_length: int, temp_symbol: str) -> str:
        """Append today's high/low, then tomorrow and its precipitation while they fit."""
        return self.provider._open_meteo_daily_tail(weather, daily, max_length, temp_symbol)

    def _open_meteo_daytime(self, current: dict) -> bool:
        """Whether the sun is up at the location: Open-Meteo's is_day, else 06:00-18:00 local."""
        return self.provider._open_meteo_daytime(current)

    def _open_meteo_period_key(self, current: dict) -> str:
        """Period label key for the current conditions (today, overnight or tonight)."""
        return self.provider._open_meteo_period_key(current)

    @staticmethod
    def _open_meteo_local_hour(current: dict) -> int:
        """Hour of day at the location from Open-Meteo's current time, else the bot's."""
        return OpenMeteoProvider._open_meteo_local_hour(current)

    @staticmethod
    def _open_meteo_date(dates: list, index: int):
        """The date at *index* of Open-Meteo's daily time array, or None."""
        return OpenMeteoProvider._open_meteo_date(dates, index)

    def format_tomorrow_forecast(self, data: dict, max_length: Optional[int] = None) -> str:
        """Format a detailed forecast for tomorrow.

        Args:
            data: Weather data dictionary from Open-Meteo.
            max_length: UTF-8 byte budget; details are left out, least important
                first, until the reply fits. None keeps every detail.

        Returns:
            str: Formatted tomorrow forecast string.
        """
        return self.provider.format_tomorrow_forecast(data, max_length)

    def format_multiday_forecast(self, data: dict, num_days: int = 7) -> str:
        """Format a less detailed multi-day forecast summary.

        Args:
            data: Weather data dictionary from Open-Meteo.
            num_days: Number of days to include in forecast.

        Returns:
            str: Formatted multi-day forecast string (newlines separate days).
        """
        return self.provider.format_multiday_forecast(data, num_days)

    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction with emoji.

        Args:
            degrees: Wind direction in degrees.

        Returns:
            str: Compass direction string with emoji (e.g., "↗️NNE").
        """
        return self.provider._degrees_to_direction(degrees)

    def _get_weather_description(self, code: int) -> str:
        """Convert WMO weather code to description.

        Args:
            code: WMO weather code.

        Returns:
            str: Weather description.
        """
        return self.provider._get_weather_description(code)

    def _get_weather_emoji(self, code: int, is_day: Optional[bool] = True) -> str:
        """Convert WMO weather code to emoji.

        Args:
            code: WMO weather code.
            is_day: False at night, when clear skies show a moon instead of a sun.

        Returns:
            str: Weather emoji.
        """
        return weather_emoji(code, is_day)

    def _check_extreme_conditions(self, current: dict) -> Optional[str]:
        """Check for extreme weather conditions that warrant warnings.

        Args:
            current: Open-Meteo current conditions in the configured units.

        Returns:
            Optional[str]: Warning text if conditions found, None otherwise.
        """
        return self.provider._check_extreme_conditions(current)
