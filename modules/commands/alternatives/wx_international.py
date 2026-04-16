#!/usr/bin/env python3
"""
Global Weather command for the MeshCore Bot
Provides worldwide weather information using Open-Meteo API
"""

import re
from datetime import datetime, timedelta
from typing import Any, Optional, Union

import requests

from ...models import MeshMessage
from ...utils import (
    format_temperature_high_low,
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    rate_limited_nominatim_reverse_sync,
)
from ..base_command import BaseCommand

# Import WXSIM parser for custom weather sources
try:
    from ...clients.wxsim_parser import WXSIMParser
    WXSIM_PARSER_AVAILABLE = True
except ImportError:
    WXSIM_PARSER_AVAILABLE = False
    WXSIMParser = None

from ...clients.mqtt_weather import (
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)

# Multiday: plain digits, 7day/7-day, or suffix form 7d/10d (min 2, max below). Open-Meteo allows up to 16 forecast days.
GWX_MULTIDAY_MAX_DAYS = 16


class GlobalWxCommand(BaseCommand):
    """Handles global weather commands with city/location support"""

    # Plugin metadata
    name = "gwx"
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

        # Initialize WXSIM parser if available
        if WXSIM_PARSER_AVAILABLE:
            self.wxsim_parser = WXSIMParser()
        else:
            self.wxsim_parser = None

        self.weather_model = self._load_weather_model()

        # Get default state and country from config for city disambiguation
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
        self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')

        # Get unit preferences from config
        self.temperature_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
        self.wind_speed_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()
        self.precipitation_unit = self.bot.config.get('Weather', 'precipitation_unit', fallback='inch').lower()

        # Validate units
        if self.temperature_unit not in ['fahrenheit', 'celsius']:
            self.logger.warning(f"Invalid temperature_unit '{self.temperature_unit}', using 'fahrenheit'")
            self.temperature_unit = 'fahrenheit'
        if self.wind_speed_unit not in ['mph', 'kmh', 'ms']:
            self.logger.warning(f"Invalid wind_speed_unit '{self.wind_speed_unit}', using 'mph'")
            self.wind_speed_unit = 'mph'
        if self.precipitation_unit not in ['inch', 'mm']:
            self.logger.warning(f"Invalid precipitation_unit '{self.precipitation_unit}', using 'inch'")
            self.precipitation_unit = 'inch'

        # Initialize geocoder (will use rate-limited helpers for actual calls)
        self.geolocator = get_nominatim_geocoder()

        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager

    def _format_high_low(self, high: Optional[Union[int, float]], low: Optional[Union[int, float]], temp_symbol: str) -> str:
        """Format high/low using [Weather] temperature_*_format templates."""
        return format_temperature_high_low(self.bot.config, high, low, temp_symbol, self.logger)

    def _load_weather_model(self) -> Optional[str]:
        """Load and normalize Open-Meteo model selection from config.

        Returns:
            Optional[str]: Model string, or None to omit the models parameter.
        """
        if self.bot.config.has_option('Weather', 'weather_model'):
            model = self.bot.config.get('Weather', 'weather_model', fallback='').strip().lower()
            if not model:
                # Explicitly blank means "let Open-Meteo auto-select".
                return None
        else:
            # Unset falls back to Open-Meteo's best_match model.
            model = 'best_match'

        if not re.fullmatch(r'[a-z0-9_,.-]+', model):
            self.logger.warning(f"Invalid weather_model '{model}', using 'best_match'")
            return 'best_match'

        return model

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
        content_lower = self.cleanup_message_for_matching(message)
        return any(content_lower.startswith(keyword + ' ') or content_lower == keyword for keyword in self.keywords)

    def _get_companion_location(self, message: MeshMessage) -> Optional[tuple[float, float]]:
        """Get companion/sender location from database.

        Args:
            message: The message object.

        Returns:
            Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None.
        """
        try:
            sender_pubkey = message.sender_pubkey
            if not sender_pubkey:
                self.logger.debug("No sender_pubkey in message for companion location lookup")
                return None

            query = '''
                SELECT latitude, longitude
                FROM complete_contact_tracking
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''

            results = self.bot.db_manager.execute_query(query, (sender_pubkey,))

            if results:
                row = results[0]
                lat = row['latitude']
                lon = row['longitude']
                self.logger.debug(f"Found companion location: {lat}, {lon} for pubkey {sender_pubkey[:16]}...")
                return (lat, lon)
            else:
                self.logger.debug(f"No location found in database for pubkey {sender_pubkey[:16]}...")
            return None
        except Exception as e:
            self.logger.warning(f"Error getting companion location: {e}")
            return None

    def _get_bot_location(self) -> Optional[tuple[float, float]]:
        """Get bot location from config.

        Returns:
            Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None.
        """
        try:
            lat = self.bot.config.getfloat('Bot', 'bot_latitude', fallback=None)
            lon = self.bot.config.getfloat('Bot', 'bot_longitude', fallback=None)

            if lat is not None and lon is not None:
                # Validate coordinates
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return (lat, lon)
            return None
        except Exception as e:
            self.logger.debug(f"Error getting bot location: {e}")
            return None

    def _get_custom_mqtt_weather_topic(self, location: Optional[str] = None) -> Optional[str]:
        return get_mqtt_weather_topic(self.bot.config, location)

    def _mqtt_weather_line(
        self,
        topic: str,
        forecast_type: str,
        location_name: Optional[str],
    ) -> str:
        if forecast_type != "default":
            return self.translate("commands.gwx.mqtt_forecast_not_supported")

        fmt = load_mqtt_weather_format_config(self.bot.config)
        cache = getattr(self.bot, "mqtt_weather_cache", None)
        text, err = mqtt_weather_display_for_topic(topic, cache, fmt)
        if text is not None:
            if location_name:
                return f"{location_name}: {text}"
            return text
        if err == "no_cache":
            return self.translate("commands.gwx.mqtt_weather_no_subscriber")
        if err in ("no_data", "empty_payload", "empty_after_sanitize"):
            return self.translate("commands.gwx.mqtt_weather_no_data")
        if err == "stale":
            return self.translate("commands.gwx.mqtt_weather_stale")
        detail = (err or "unknown").replace("_", " ")
        return self.translate("commands.gwx.mqtt_weather_payload_error", detail=detail)

    def _get_custom_wxsim_source(self, location: Optional[str] = None) -> Optional[str]:
        """Get custom WXSIM source URL from config.

        Looks for keys in [Weather] section with pattern: custom.wxsim.<name> = <url>
        Similar to how Channels_List handles dotted keys.

        Args:
            location: Location name or None for default source

        Returns:
            Optional[str]: Source URL or None if not found
        """
        if not self.wxsim_parser:
            return None

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
        if not self.wxsim_parser:
            return self.translate('commands.gwx.error', error="WXSIM parser not available")

        # Fetch WXSIM data
        text = self.wxsim_parser.fetch_from_url(source_url, timeout=self.url_timeout)
        if not text:
            return self.translate('commands.gwx.error_fetching')

        # Parse the data
        forecast = self.wxsim_parser.parse(text)

        # Get unit preferences from config
        temp_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
        wind_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()

        # Format based on forecast type
        if forecast_type == "tomorrow":
            # Get tomorrow's forecast
            if len(forecast.periods) > 1:
                tomorrow = forecast.periods[1]
                high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp else None
                low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp else None
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
                high = self.wxsim_parser._convert_temp(today.high_temp, temp_unit) if today.high_temp else None
                low = self.wxsim_parser._convert_temp(today.low_temp, temp_unit) if today.low_temp else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                hl_today = self._format_high_low(high, low, temp_symbol)
                if hl_today:
                    current += f" | {hl_today}"

                # Add tomorrow if available
                if len(forecast.periods) > 1:
                    tomorrow = forecast.periods[1]
                    tomorrow_high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp else None
                    tomorrow_low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp else None

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

        # If no location specified, check custom MQTT then WXSIM default sources
        if len(parts) < 2:
            mqtt_topic = self._get_custom_mqtt_weather_topic(None)
            if mqtt_topic:
                try:
                    self.record_execution(message.sender_id)
                    weather_data = self._mqtt_weather_line(mqtt_topic, "default", None)
                    await self.send_response(message, weather_data)
                    return True
                except Exception as e:
                    self.logger.error(f"Error reading MQTT weather: {e}")
                    await self.send_response(message, self.translate("commands.gwx.error", error=str(e)))
                    return True

            wxsim_source = self._get_custom_wxsim_source(None)  # Check for default
            if wxsim_source:
                # Use custom WXSIM default source
                try:
                    self.record_execution(message.sender_id)
                    weather_data = self._get_wxsim_weather(wxsim_source, "default", 7, message)
                    await self.send_response(message, weather_data)
                    return True
                except Exception as e:
                    self.logger.error(f"Error fetching WXSIM weather: {e}")
                    await self.send_response(message, self.translate('commands.gwx.error', error=str(e)))
                    return True

            # No custom source, try companion location
            companion_location = self._get_companion_location(message)
            if companion_location:
                # Convert coordinates to location string
                location_str = self._coordinates_to_location_string(companion_location[0], companion_location[1])
                if location_str:
                    # Use the location string as if user provided it
                    parts = [parts[0], location_str]
                    self.logger.info(f"Using companion location: {location_str} ({companion_location[0]}, {companion_location[1]})")
                else:
                    # If reverse geocoding fails, use coordinates directly (geocode_location can handle "lat,lon" format)
                    location_str = f"{companion_location[0]},{companion_location[1]}"
                    parts = [parts[0], location_str]
                    self.logger.info(f"Using companion coordinates: {location_str}")
            else:
                # No companion location: optionally use bot's configured coordinates
                use_bot = self.get_config_value(
                    'Wx_Command',
                    'use_bot_location_when_no_location',
                    fallback=False,
                    value_type='bool',
                )
                bot_loc = self._get_bot_location() if use_bot else None
                if bot_loc:
                    location_str = self._coordinates_to_location_string(bot_loc[0], bot_loc[1])
                    if location_str:
                        parts = [parts[0], location_str]
                        self.logger.info(
                            f"Using bot location (no args): {location_str} "
                            f"({bot_loc[0]}, {bot_loc[1]})"
                        )
                    else:
                        location_str = f"{bot_loc[0]},{bot_loc[1]}"
                        parts = [parts[0], location_str]
                        self.logger.info(f"Using bot coordinates (no args): {location_str}")
                else:
                    if use_bot:
                        self.logger.debug(
                            "use_bot_location_when_no_location enabled but bot_latitude/bot_longitude "
                            "not set; showing usage"
                        )
                    else:
                        self.logger.debug("No companion location found, showing usage")
                    await self.send_response(message, self.translate('commands.gwx.usage'))
                    return True

        # Check for forecast type options: "tomorrow", Nd (7d, 10d), or plain digit days 2–GWX_MULTIDAY_MAX_DAYS
        forecast_type = "default"
        num_days = 7  # Default for multi-day forecast
        location_parts = parts[1:]

        # Check last part for forecast type
        if len(location_parts) > 0:
            last_part = location_parts[-1].lower()
            if last_part == "tomorrow":
                forecast_type = "tomorrow"
                location_parts = location_parts[:-1]
            elif last_part in ["7day", "7-day"]:
                forecast_type = "multiday"
                num_days = 7
                location_parts = location_parts[:-1]
            else:
                nd_match = re.fullmatch(r"(\d+)d", last_part)
                if nd_match:
                    days = int(nd_match.group(1))
                    if 2 <= days <= GWX_MULTIDAY_MAX_DAYS:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]
                elif last_part.isdigit():
                    days = int(last_part)
                    if 2 <= days <= GWX_MULTIDAY_MAX_DAYS:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]

        # Join remaining parts to handle "city, country" format
        location = ' '.join(location_parts).strip()

        if not location:
            await self.send_response(message, self.translate('commands.gwx.usage'))
            return True

        # Custom MQTT before WXSIM
        mqtt_topic = self._get_custom_mqtt_weather_topic(location)
        if mqtt_topic:
            self.logger.info(f"Using custom MQTT weather topic for location '{location}': {mqtt_topic}")
            try:
                self.record_execution(message.sender_id)
                weather_data = self._mqtt_weather_line(mqtt_topic, forecast_type, location)
                if forecast_type == "multiday":
                    await self._send_multiday_forecast(message, weather_data)
                else:
                    await self.send_response(message, weather_data)
                return True
            except Exception as e:
                self.logger.error(f"Error reading MQTT weather: {e}")
                await self.send_response(message, self.translate("commands.gwx.error", error=str(e)))
                return True

        # Check for custom WXSIM source first (before normal geocoding)
        wxsim_source = self._get_custom_wxsim_source(location)
        if wxsim_source:
            # Use custom WXSIM source
            try:
                self.record_execution(message.sender_id)
                weather_data = self._get_wxsim_weather(wxsim_source, forecast_type, num_days, message, location_name=location)
                if forecast_type == "multiday":
                    await self._send_multiday_forecast(message, weather_data)
                else:
                    await self.send_response(message, weather_data)
                return True
            except Exception as e:
                self.logger.error(f"Error fetching WXSIM weather: {e}")
                await self.send_response(message, self.translate('commands.gwx.error', error=str(e)))
                return True

        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Get weather data for the location
            weather_data = await self.get_weather_for_location(location, forecast_type, num_days, message)

            # Check if we need to send multiple messages (for alerts)
            if isinstance(weather_data, tuple) and weather_data[0] == "multi_message":
                # Send weather data first
                await self.send_response(message, weather_data[1])

                # Wait for bot TX rate limiter
                import asyncio
                rate_limit = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
                sleep_time = max(rate_limit + 1.0, 2.0)
                await asyncio.sleep(sleep_time)

                # Send alerts
                await self.send_response(message, weather_data[2])
            elif forecast_type == "multiday":
                # Use message splitting for multi-day forecasts
                await self._send_multiday_forecast(message, weather_data)
            else:
                await self.send_response(message, weather_data)

            return True

        except Exception as e:
            self.logger.error(f"Error in global weather command: {e}")
            await self.send_response(message, self.translate('commands.gwx.error', error=str(e)))
            return True

    async def get_weather_for_location(self, location: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None) -> Union[str, tuple[str, str, str]]:
        """Get weather data for any global location.

        Args:
            location: The location (city name, etc.).
            forecast_type: "default", "tomorrow", or "multiday".
            num_days: Number of days for multiday forecast (2–16).
            message: The MeshMessage for dynamic length calculation.

        Returns:
            Union[str, Tuple[str, str, str]]: Format string or tuple for multi-message response.
        """
        try:
            # Convert location to lat/lon with address details
            result = self.geocode_location(location)
            if not result or result[0] is None or result[1] is None:
                return self.translate('commands.gwx.no_location', location=location)

            lat, lon, address_info, geocode_result = result

            # Format location name for display
            location_display = self._format_location_display(address_info, geocode_result, location)
            self.logger.debug(f"Formatted location_display: '{location_display}' from location: '{location}'")

            # Calculate the length of the location prefix (location_display + ": ")
            location_prefix_len = len(f"{location_display}: ")

            # Get weather forecast from Open-Meteo based on type
            # Pass location_prefix_len so weather formatting can account for it
            if forecast_type == "tomorrow":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="tomorrow", message=message, location_prefix_len=location_prefix_len)
            elif forecast_type == "multiday":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="multiday", num_days=num_days, message=message, location_prefix_len=location_prefix_len)
            else:
                weather_text = self.get_open_meteo_weather(lat, lon, message=message, location_prefix_len=location_prefix_len)

            # Check if it's an error (translated error message)
            error_fetching = self.translate('commands.gwx.error_fetching')
            if weather_text == error_fetching or weather_text == self.ERROR_FETCHING_DATA:
                return self.translate('commands.gwx.error_fetching_api')

            # Check for severe weather warnings (only for default forecast type)
            if forecast_type == "default":
                alert_text = self._check_extreme_conditions(weather_text)

                if alert_text:
                    # Return multi-message format
                    return ("multi_message", f"{location_display}: {weather_text}", alert_text)

            return f"{location_display}: {weather_text}"

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
            if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
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
            if re.match(r'^\d{5}$', location.strip()):
                lat, lon = geocode_zipcode_sync(
                    self.bot, location,
                    default_country=self.default_country,
                    timeout=10
                )
                if lat is not None and lon is not None:
                    address_info = {}
                    geocode_result = None
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(
                            self.bot, f"{lat}, {lon}", timeout=10
                        )
                        if reverse_location:
                            geocode_result = reverse_location
                            address_info = reverse_location.raw.get('address', {})
                    except Exception as e:
                        self.logger.debug(f"Reverse geocoding failed for zip {location}: {e}")
                    return lat, lon, address_info or {}, geocode_result
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
            forecast_type: "default", "tomorrow", or "multiday".
            num_days: Number of days for multiday forecast (2–16).
            message: The MeshMessage for dynamic length calculation.
            location_prefix_len: Length of location prefix (e.g., "City, CC: ") that will be added later.

        Returns:
            str: Formatted weather string or error message.
        """
        # Get max message length dynamically, then subtract location prefix length
        max_length = self.get_max_message_length(message) if message else 130
        max_length = max_length - location_prefix_len  # Account for location prefix
        try:
            # Open-Meteo API endpoint with current weather and forecast
            api_url = "https://api.open-meteo.com/v1/forecast"

            # Determine forecast_days based on type
            if forecast_type == "multiday":
                forecast_days = min(num_days, 16)  # Open-Meteo supports up to 16 days
            elif forecast_type == "tomorrow":
                forecast_days = 2  # Need today and tomorrow
            else:
                forecast_days = 2  # Default

            params = {
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,dewpoint_2m,visibility,surface_pressure',
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

            # For tomorrow or multiday, return raw data for formatting
            if forecast_type in ["tomorrow", "multiday"]:
                response = requests.get(api_url, params=params, timeout=self.url_timeout)

                if not response.ok:
                    self.logger.warning(f"Error fetching weather from Open-Meteo: {response.status_code}")
                    return self.translate('commands.gwx.error_fetching')

                data = response.json()

                if forecast_type == "tomorrow":
                    return self.format_tomorrow_forecast(data)
                elif forecast_type == "multiday":
                    return self.format_multiday_forecast(data, num_days)

            response = requests.get(api_url, params=params, timeout=self.url_timeout)

            if not response.ok:
                self.logger.warning(f"Error fetching weather from Open-Meteo: {response.status_code}")
                return self.translate('commands.gwx.error_fetching')

            data = response.json()

            # Check units in response to verify API is respecting our unit requests
            current_units = data.get('current_units', {})
            current_units.get('temperature_2m', '°F')
            visibility_unit = current_units.get('visibility', 'm')

            # Extract current conditions
            current = data.get('current', {})
            daily = data.get('daily', {})
            data.get('hourly', {})

            # Current conditions - API should return in Fahrenheit when requested
            temp = int(current.get('temperature_2m', 0))
            feels_like = int(current.get('apparent_temperature', temp))
            dewpoint = current.get('dewpoint_2m')
            humidity = int(current.get('relative_humidity_2m', 0))
            wind_speed = int(current.get('wind_speed_10m', 0))
            wind_direction = self._degrees_to_direction(current.get('wind_direction_10m', 0))
            wind_gusts = int(current.get('wind_gusts_10m', 0))
            visibility = current.get('visibility')
            pressure = current.get('surface_pressure')
            weather_code = current.get('weather_code', 0)

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

            # Get weather description and emoji
            weather_desc = self._get_weather_description(weather_code)
            weather_emoji = self._get_weather_emoji(weather_code)

            # Determine temperature unit symbol
            temp_symbol = "°F" if self.temperature_unit == 'fahrenheit' else "°C"

            # Determine if it's day or night for forecast period name
            now = datetime.now()
            hour = now.hour
            if 6 <= hour < 18:
                period_name = self.translate('commands.gwx.periods.today')
            else:
                period_name = self.translate('commands.gwx.periods.tonight')

            # Build current weather string
            weather = f"{period_name}: {weather_emoji}{weather_desc} {temp}{temp_symbol}"

            # Add feels like if significantly different
            if abs(feels_like - temp) >= 5:
                weather += f" (feels {feels_like}{temp_symbol})"

            # Add wind info (always show if >= 3 mph, show gusts if significant)
            if wind_speed >= 3:
                weather += f" {wind_direction}{wind_speed}"
                if wind_gusts > wind_speed + 3:
                    weather += f"G{wind_gusts}"

            # Add humidity
            weather += f" {humidity}%RH"

            # Add additional conditions if space allows
            conditions = []

            # Add dew point
            if dewpoint is not None:
                dewpoint_val = int(dewpoint)
                conditions.append(f"💧{dewpoint_val}{temp_symbol}")

            # Add visibility (already converted to miles above)
            if visibility_mi is not None and visibility_mi > 0:
                # Cap visibility at 20 miles for display (beyond that is essentially unlimited)
                visibility_display = int(visibility_mi)
                if visibility_display > 20:
                    visibility_display = 20
                conditions.append(f"👁️{visibility_display}mi")

            # Add pressure (convert from hPa to display format)
            if pressure is not None:
                pressure_hpa = int(pressure)
                conditions.append(f"📊{pressure_hpa}hPa")

            # Add conditions to weather string if space allows
            # Reserve space for forecast data (high/low and tomorrow)
            conditions_max_length = max_length - 80  # Reserve ~80 chars for forecast data
            if conditions and len(weather) < conditions_max_length:
                weather += " " + " ".join(conditions)

            # Add forecast high/low for today (without repeating period name since current conditions already show it)
            # API should return temperatures in Fahrenheit when requested
            if daily:
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
                    tomorrow_str = f" | {tomorrow_period}: {tomorrow_emoji} {self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)}"

                    # Only add if we have space (leave room for potential precipitation)
                    # Use display width to account for emojis
                    if self._count_display_width(weather + tomorrow_str) <= max_length - 10:  # Leave 10 chars buffer
                        weather += tomorrow_str

                        # Add precipitation probability and amount if significant and space allows
                        if len(daily.get('precipitation_probability_max', [])) > 1:
                            precip_prob = daily['precipitation_probability_max'][1]
                            if precip_prob >= 30:
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

        except Exception as e:
            self.logger.error(f"Error fetching Open-Meteo weather: {e}")
            return self.translate('commands.gwx.error_fetching')

    def format_tomorrow_forecast(self, data: dict) -> str:
        """Format a detailed forecast for tomorrow.

        Args:
            data: Weather data dictionary from Open-Meteo.

        Returns:
            str: Formatted tomorrow forecast string.
        """
        try:
            daily = data.get('daily', {})
            if not daily or len(daily.get('temperature_2m_max', [])) < 2:
                return self.translate('commands.gwx.tomorrow_not_available')

            temp_symbol = "°F" if self.temperature_unit == 'fahrenheit' else "°C"
            tomorrow_high = int(daily['temperature_2m_max'][1])
            tomorrow_low = int(daily['temperature_2m_min'][1])
            tomorrow_code = daily['weather_code'][1]
            tomorrow_emoji = self._get_weather_emoji(tomorrow_code)
            tomorrow_desc = self._get_weather_description(tomorrow_code)

            # Get wind info if available
            wind_info = ""
            if len(daily.get('wind_speed_10m_max', [])) > 1:
                wind_speed = int(daily['wind_speed_10m_max'][1])
                if wind_speed >= 3:
                    wind_info = f" {wind_speed}"
                    if len(daily.get('wind_gusts_10m_max', [])) > 1:
                        wind_gusts = int(daily['wind_gusts_10m_max'][1])
                        if wind_gusts > wind_speed + 3:
                            wind_info += f"G{wind_gusts}"

            # Get precipitation probability and amount
            precip_info = ""
            if len(daily.get('precipitation_probability_max', [])) > 1:
                precip_prob = daily['precipitation_probability_max'][1]
                if precip_prob >= 30:
                    # Get precipitation amount if available
                    precip_amount = None
                    if len(daily.get('precipitation_sum', [])) > 1:
                        precip_amount = daily['precipitation_sum'][1]

                    # Format precipitation info
                    if precip_amount is not None and precip_amount > 0:
                        # Show both probability and amount
                        precip_unit = "in" if self.precipitation_unit == 'inch' else "mm"
                        precip_info = f" 🌦️{precip_prob}% {precip_amount:.2f}{precip_unit}"
                    else:
                        # Only show probability if no amount available
                        precip_info = f" 🌦️{precip_prob}%"

            tomorrow_period = self.translate('commands.gwx.periods.tomorrow')
            hl = self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
            return f"{tomorrow_period}: {tomorrow_emoji}{tomorrow_desc} {hl}{wind_info}{precip_info}"

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
                'Monday': 'M',
                'Tuesday': 'T',
                'Wednesday': 'W',
                'Thursday': 'Th',
                'Friday': 'F',
                'Saturday': 'Sa',
                'Sunday': 'Su'
            }

            parts = []
            today = datetime.now()

            # Start from tomorrow (index 1)
            for i in range(1, min(num_days + 1, len(temps_max))):
                day_date = today + timedelta(days=i)
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

    def _count_display_width(self, text: str) -> int:
        """Count UTF-8 byte length of text. Matches RF packet byte limit from get_max_message_length()."""
        return len(text.encode('utf-8'))

    async def _send_multiday_forecast(self, message: MeshMessage, forecast_text: str) -> None:
        """Send multi-day forecast response, splitting into multiple messages if needed.

        Args:
            message: The original message (for reply context).
            forecast_text: The full forecast text (lines separated by \n).
        """
        import asyncio

        # Get max message length dynamically
        max_length = self.get_max_message_length(message)

        lines = forecast_text.split('\n')

        # Remove empty lines
        lines = [line.strip() for line in lines if line.strip()]

        if not lines:
            return

        # If single line and under max_length chars, send as-is
        if self._count_display_width(forecast_text) <= max_length:
            await self.send_response(message, forecast_text)
            return

        # Multi-line message - try to fit as many days as possible in one message
        # Only split when necessary (message would exceed max_length chars)
        current_message = ""
        message_count = 0

        for i, line in enumerate(lines):
            if not line:
                continue

            # Check if adding this line would exceed max_length characters (using display width)
            test_message = current_message + "\n" + line if current_message else line

            # Only split if message would exceed max_length chars (using display width)
            if self._count_display_width(test_message) > max_length:
                # Send current message and start new one
                if current_message:
                    # Per-user rate limit applies only to first message (trigger); skip for continuations
                    await self.send_response(
                        message, current_message,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    # Wait between messages (same as other commands)
                    if i < len(lines):
                        await asyncio.sleep(2.0)

                    current_message = line
                else:
                    # Single line is too long, send it anyway (will be truncated by bot)
                    await self.send_response(
                        message, line,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    if i < len(lines) - 1:
                        await asyncio.sleep(2.0)
                    current_message = ""
            else:
                # Add line to current message (fits within max_length)
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line

        # Send the last message if there's content (continuation; skip per-user rate limit)
        if current_message:
            await self.send_response(message, current_message, skip_user_rate_limit=True)

    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction with emoji.

        Args:
            degrees: Wind direction in degrees.

        Returns:
            str: Compass direction string with emoji (e.g., "⬆️N").
        """
        if degrees is None:
            return ""

        directions = [
            (0, "⬆️N"), (22.5, "↗️NE"), (45, "↗️NE"), (67.5, "➡️E"),
            (90, "➡️E"), (112.5, "↘️SE"), (135, "↘️SE"), (157.5, "⬇️S"),
            (180, "⬇️S"), (202.5, "↙️SW"), (225, "↙️SW"), (247.5, "⬅️W"),
            (270, "⬅️W"), (292.5, "↖️NW"), (315, "↖️NW"), (337.5, "⬆️N"),
            (360, "⬆️N")
        ]

        # Find closest direction
        for i in range(len(directions) - 1):
            if directions[i][0] <= degrees < directions[i + 1][0]:
                return directions[i][1]

        return "⬆️N"  # Default to North

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
                99: "Severe T-Storm"
            }
            return weather_codes.get(code, self.translate('commands.gwx.weather_descriptions.unknown'))

        return description

    def _get_weather_emoji(self, code: int) -> str:
        """Convert WMO weather code to emoji.

        Args:
            code: WMO weather code.

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
            99: "⛈️"      # Severe Thunderstorm
        }

        return emoji_map.get(code, "🌤️")

    def _check_extreme_conditions(self, weather_text: str) -> Optional[str]:
        """Check for extreme weather conditions that warrant warnings.

        Args:
            weather_text: The formatted weather text to check.

        Returns:
            Optional[str]: Warning text if conditions found, None otherwise.
        """
        warnings = []

        # Extract temperature from weather text
        temp_match = re.search(r'(\d+)°F', weather_text)
        if temp_match:
            temp = int(temp_match.group(1))
            if temp >= 95:
                warnings.append(self.translate('commands.gwx.warnings.extreme_heat'))
            elif temp <= 20:
                warnings.append(self.translate('commands.gwx.warnings.extreme_cold'))

        # Check for severe weather indicators
        # Note: We check for English strings here since weather descriptions might be in English
        # In a fully localized version, we'd need to check translated strings too
        heavy_rain_en = "Heavy Rain"
        heavy_showers_en = "Heavy Showers"
        thunderstorm_en = "Thunderstorm"
        t_storm_en = "T-Storm"
        heavy_snow_en = "Heavy Snow"
        snow_showers_en = "Snow Showers"

        # Also get translated versions for checking
        heavy_rain_trans = self.translate('commands.gwx.weather_descriptions.65')
        heavy_showers_trans = self.translate('commands.gwx.weather_descriptions.82')
        thunderstorm_trans = self.translate('commands.gwx.weather_descriptions.95')
        t_storm_trans = self.translate('commands.gwx.weather_descriptions.96')
        heavy_snow_trans = self.translate('commands.gwx.weather_descriptions.75')
        snow_showers_trans = self.translate('commands.gwx.weather_descriptions.86')

        if (heavy_rain_en in weather_text or heavy_showers_en in weather_text or
            heavy_rain_trans in weather_text or heavy_showers_trans in weather_text):
            warnings.append(self.translate('commands.gwx.warnings.heavy_rain'))

        if (thunderstorm_en in weather_text or t_storm_en in weather_text or
            thunderstorm_trans in weather_text or t_storm_trans in weather_text):
            warnings.append(self.translate('commands.gwx.warnings.thunderstorms'))

        if (heavy_snow_en in weather_text or snow_showers_en in weather_text or
            heavy_snow_trans in weather_text or snow_showers_trans in weather_text):
            warnings.append(self.translate('commands.gwx.warnings.heavy_snow'))

        # Check for high winds
        wind_match = re.search(r'[NESW]{1,2}(\d+)', weather_text)
        if wind_match:
            wind_speed = int(wind_match.group(1))
            if wind_speed >= 30:
                warnings.append(self.translate('commands.gwx.warnings.high_winds', wind_speed=wind_speed))

        return " | ".join(warnings) if warnings else None
