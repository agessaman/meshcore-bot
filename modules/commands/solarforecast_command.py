#!/usr/bin/env python3
"""
Solar Forecast command for the MeshCore Bot
Provides solar panel production forecasts using Forecast.Solar API
"""

import hashlib
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

from ..location import repeater_by_name_rows
from ..models import MeshMessage
from ..security_utils import sanitize_name
from ..utils import (
    abbreviate_location,
    geocode_city,
    geocode_zipcode,
    get_config_timezone,
    get_nominatim_geocoder,
    rate_limited_nominatim_reverse,
)
from .base_command import BaseCommand


def _solar_timestamp_to_naive(timestamp: str, local_tz: Any) -> datetime:
    """A forecast.solar timestamp as a naive local datetime; raises when it does not parse.

    Timestamps with a Z or + offset are converted to *local_tz*; plain ones
    ("2026-10-01 13:00:00") are already local.
    """
    if 'Z' in timestamp or '+' in timestamp:
        dt_with_tz = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if dt_with_tz.tzinfo:
            if local_tz:
                return dt_with_tz.astimezone(local_tz).replace(tzinfo=None)
            return dt_with_tz.astimezone().replace(tzinfo=None)
        return dt_with_tz
    return datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')

class SolarforecastCommand(BaseCommand):
    """Handles solar forecast commands with location support"""

    # Plugin metadata
    # Read-only informational output; safe for scheduled {cmd:...} rendering.
    render_safe = True
    name = "solarforecast"
    honors_skip_channel_check = False
    enabled_attr = "solarforecast_enabled"
    keywords = ['solarforecast', 'sf']
    description = "Get solar panel production forecast (usage: sf <location|repeater_name|coordinates|zipcode> [panel_size] [azimuth, 0=south] [angle])"
    category = "solar"
    cooldown_seconds = 10  # 10 second cooldown per user
    requires_internet = True  # Requires internet access for Forecast.Solar API and geocoding

    # Documentation
    short_description = "Get solar panel production forecast for a location or repeater"
    usage = "sf <location> [watts] [azimuth] [angle]"
    examples = ["sf seattle", "sf 47.6,-122.3 200"]
    parameters = [
        {"name": "location", "description": "City, coordinates, or repeater name"},
        {"name": "watts", "description": "Panel size in watts (default: 100)"},
        {"name": "azimuth", "description": "Panel direction, 0=south (default: 0)"},
        {"name": "angle", "description": "Panel tilt in degrees (default: 30)"}
    ]

    # Error constants - will use translations instead
    ERROR_FETCHING_DATA = "Error fetching forecast"  # Deprecated - use translate
    NO_DATA_AVAILABLE = "No forecast data"  # Deprecated - use translate

    # Forecast.Solar minimum panel size (10W)
    MIN_KWP = 0.01

    # Cache duration in seconds (30 minutes)
    CACHE_DURATION = 30 * 60

    # Web-viewer settings schema (see modules/settings_schema.py).
    settings_schema = [
        {"key": "forecast_solar_api_key", "label": "Forecast.Solar API key", "type": "str",
         "section": "External_Data", "default": "",
         "help": "Optional API key for forecast.solar. Free tier (2-day) works without one; "
                 "a paid key unlocks 3-6 day forecasts."},
        {"key": "default_state", "label": "Default state", "type": "str", "section": "Weather",
         "default": "", "help": "2-letter state for city disambiguation (e.g. WA). Shared weather setting."},
        {"key": "default_country", "label": "Default country", "type": "str", "section": "Weather",
         "default": "US", "help": "2-letter country code (e.g. US). Shared weather setting."},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.solarforecast_enabled = self.get_config_value('Solarforecast_Command', 'enabled', fallback=True, value_type='bool')
        self.url_timeout = 15  # seconds

        # Forecast cache: {cache_key: {'data': dict, 'timestamp': float}}
        self.forecast_cache = {}

        # Get default state from config for city disambiguation
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')

        # Initialize geocoder (will use rate-limited helpers for actual calls)
        self.geolocator = get_nominatim_geocoder()

        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        return self.translate('commands.solarforecast.usage')

    def _translate_day_abbreviation(self, day_abbr: str) -> str:
        """Translate English day abbreviation to localized version"""
        # Map English abbreviations to translation keys in common.date_time
        translation_key = f'common.date_time.day_abbreviations.{day_abbr}'
        translated = self.translate(translation_key)
        # If translation found (not just the key), return it
        if translated != translation_key:
            return translated
        # Fallback: return original if no translation found
        return day_abbr

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the solar forecast command"""
        content = message.content.strip()

        # Parse command: sf <location> [panel_size] [azimuth] [angle]
        parts = content.split()
        if len(parts) < 2:
            await self.send_response(message, self.translate('commands.solarforecast.usage_short'))
            return True

        try:
            # Record execution for this user (handles cooldown)
            self.record_execution(message.sender_id)

            # Parse arguments - location might be multiple words (e.g., "Hillcrest Repeater v2")
            # Try to find where location ends and numeric parameters begin
            # But be careful: 5-digit numbers might be zip codes, not panel sizes
            location_parts = []
            param_start_idx = None

            # Special case: if we only have 2 parts and the second is a non-5-digit number,
            # it's likely "sf 10" which is invalid (no location provided)
            if len(parts) == 2:
                try:
                    float(parts[1])
                    if not (len(parts[1]) == 5 and parts[1].isdigit()):
                        # Single non-5-digit number - invalid, no location provided
                        await self.send_response(message, self.translate('commands.solarforecast.usage_short'))
                        return True
                except ValueError:
                    pass  # Not a number, continue with normal parsing

            for i in range(1, len(parts)):
                # Check if this part is a number (could be panel size, azimuth, or angle)
                # Handle cases like "10w", "10W", or just "10"
                try:
                    # Try to parse as number (strip 'w' or 'W' suffix if present)
                    num_str = parts[i].strip().rstrip('wW')
                    float(num_str)
                    # Check if it's a 5-digit number (likely a zip code)
                    # But only if it doesn't have a 'w' suffix (zip codes don't have 'w')
                    if len(parts[i]) == 5 and parts[i].isdigit():
                        # Could be a zip code - include it in location
                        location_parts.append(parts[i])
                    else:
                        # This is a number and not a zip code - location ends before this
                        param_start_idx = i
                        break
                except ValueError:
                    # Not a number, part of location name
                    location_parts.append(parts[i])

            location_str = ' '.join(location_parts) if location_parts else (parts[1] if len(parts) > 1 else "")

            # Clean location string - remove control characters and non-printable characters
            location_str = self._clean_location_string(location_str)

            # Validate that we have a location
            if not location_str:
                await self.send_response(message, self.translate('commands.solarforecast.usage_short'))
                return True

            # Parse optional parameters
            panel_watts = 10.0  # Default 10W
            azimuth = 0  # Default south
            angle = 45  # Default 45° tilt

            # Try to parse panel size (in watts)
            # Handle cases like "10w", "10W", or just "10"
            if param_start_idx is not None and param_start_idx < len(parts):
                try:
                    panel_str = parts[param_start_idx].strip().rstrip('wW')
                    panel_watts = float(panel_str)
                    if panel_watts <= 0 or panel_watts > 1000:
                        await self.send_response(message, self.translate('commands.solarforecast.panel_size_range'))
                        return True
                except ValueError:
                    pass

            # Try to parse azimuth
            if param_start_idx is not None and param_start_idx + 1 < len(parts):
                try:
                    azimuth = float(parts[param_start_idx + 1])
                    if not (-180 <= azimuth <= 180):
                        await self.send_response(message, self.translate('commands.solarforecast.azimuth_range'))
                        return True
                except ValueError:
                    pass

            # Try to parse angle (tilt)
            if param_start_idx is not None and param_start_idx + 2 < len(parts):
                try:
                    angle = float(parts[param_start_idx + 2])
                    if not (0 <= angle <= 90):
                        await self.send_response(message, self.translate('commands.solarforecast.angle_range'))
                        return True
                except ValueError:
                    pass

            # Parse location (check for repeater name first, then coordinates, zip, or city)
            lat, lon, location_type = await self._parse_location(location_str)
            if lat is None or lon is None:
                await self.send_response(message, self.translate('commands.solarforecast.no_location', location=location_str))
                return True

            # Get location name for confirmation
            location_name = await self._get_location_name(lat, lon, location_str, location_type)

            # Get forecast
            forecast_text = await self._get_forecast(lat, lon, panel_watts, azimuth, angle, location_name)

            # Send response - handle multi-line messages
            await self._send_forecast_response(message, forecast_text)

            return True

        except Exception as e:
            self.logger.error(f"Error in solar forecast command: {e}")
            await self.send_response(message, self.translate('commands.solarforecast.error', error=str(e)))
            return True

    def _clean_location_string(self, location: str) -> str:
        """Clean location string by removing control characters and non-printable characters"""
        if not location:
            return location

        # Remove control characters (0x00-0x1F) except space, tab, newline, carriage return
        # Also remove DEL (0x7F) and other non-printable characters
        cleaned = ''.join(
            char for char in location
            if char.isprintable() or char in (' ', '\t', '\n', '\r')
        )

        # Strip whitespace from both ends
        cleaned = cleaned.strip()

        # Remove any remaining non-ASCII control characters
        cleaned = ''.join(char for char in cleaned if ord(char) >= 32 or char in ('\t', '\n', '\r'))

        # Remove trailing single '<' character (often appears as garbage from double-submit)
        # Also remove any trailing control-like characters
        while cleaned and (cleaned[-1] == '<' or (len(cleaned) > 1 and ord(cleaned[-1]) < 32)):
            cleaned = cleaned[:-1].rstrip()

        return cleaned

    async def _parse_location(self, location: str) -> tuple[Optional[float], Optional[float], str]:
        """Parse location string to lat/lon"""
        # First, check if it's a repeater name
        self.logger.debug(f"Checking if '{location}' is a repeater name...")
        lat, lon = await self._repeater_name_to_lat_lon(location)
        if lat is not None and lon is not None:
            self.logger.debug(f"Found repeater '{location}' at {lat}, {lon}")
            return lat, lon, "repeater"
        else:
            self.logger.debug(f"No repeater found for '{location}', trying other location types...")

        # Check if it's coordinates (lat,lon)
        if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
            try:
                lat_str, lon_str = location.split(',')
                lat = float(lat_str.strip())
                lon = float(lon_str.strip())
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon, "coordinates"
            except ValueError:
                pass

        # Check if it's a zipcode (5 digits)
        if re.match(r'^\s*\d{5}\s*$', location):
            lat, lon = await self._zipcode_to_lat_lon(location)
            if lat and lon:
                return lat, lon, "zipcode"

        # Otherwise, treat as city name
        lat, lon = await self._city_to_lat_lon(location)
        return lat, lon, "city"

    async def _repeater_name_to_lat_lon(self, repeater_name: str) -> tuple[Optional[float], Optional[float]]:
        """Look up repeater by name and return its lat/lon"""
        try:
            if not hasattr(self.bot, 'db_manager'):
                return None, None

            # Query complete_contact_tracking table for matching name
            # Use case-insensitive matching and allow partial matches
            # Filter for repeaters and roomservers only
            # Exact match first, then prefix, then substring (see repeater_by_name_rows)
            results = repeater_by_name_rows(self.bot, repeater_name, zero_rule="either")

            self.logger.debug(f"Repeater lookup query returned {len(results) if results else 0} results for '{repeater_name}'")

            if results and len(results) > 0:
                row = results[0]
                lat = row.get('latitude')
                lon = row.get('longitude')
                name = row.get('name', '')

                self.logger.debug(f"Repeater match: name='{name}', lat={lat}, lon={lon}")

                if lat is not None and lon is not None:
                    self.logger.debug(f"Found repeater '{name}' at {lat}, {lon}")
                    return float(lat), float(lon)
                else:
                    self.logger.debug(f"Repeater '{name}' found but missing coordinates")
            else:
                # Let's also check what repeaters exist in the database for debugging
                all_repeaters_query = '''
                    SELECT name, latitude, longitude
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                    AND latitude IS NOT NULL
                    AND longitude IS NOT NULL
                    AND latitude != 0
                    AND longitude != 0
                    ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                    LIMIT 10
                '''
                all_repeaters = self.bot.db_manager.execute_query(all_repeaters_query)
                if all_repeaters:
                    self.logger.debug(f"Sample repeaters in DB: {[sanitize_name(r.get('name', '')) for r in all_repeaters[:5]]}")

            return None, None
        except Exception as e:
            self.logger.debug(f"Error looking up repeater '{repeater_name}': {e}")
            return None, None

    async def _zipcode_to_lat_lon(self, zipcode: str) -> tuple[Optional[float], Optional[float]]:
        """Convert zipcode to lat/lon using shared geocoding function"""
        try:
            lat, lon = await geocode_zipcode(self.bot, zipcode, timeout=self.url_timeout)
            return lat, lon
        except Exception as e:
            self.logger.error(f"Error geocoding zipcode {zipcode}: {e}")
            return None, None

    async def _city_to_lat_lon(self, city: str) -> tuple[Optional[float], Optional[float]]:
        """Convert city name to lat/lon using shared geocoding function"""
        try:
            # Get defaults from config
            default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            lat, lon, _ = await geocode_city(
                self.bot, city,
                default_state=self.default_state,
                default_country=default_country,
                include_address_info=False,  # Don't need address info, just coordinates
                timeout=self.url_timeout
            )
            return lat, lon
        except Exception as e:
            self.logger.error(f"Error geocoding city {city}: {e}")
            return None, None

    async def _get_location_name(self, lat: float, lon: float, original_location: str,
                                 location_type: str) -> str:
        """Get location name for confirmation (city, state)"""
        # If it's a repeater, use the repeater name
        if location_type == "repeater":
            # Try to get the full repeater name from database
            try:
                if hasattr(self.bot, 'db_manager'):
                    query = '''
                        SELECT name
                        FROM complete_contact_tracking
                        WHERE role IN ('repeater', 'roomserver')
                        AND ABS(latitude - ?) < 0.001
                        AND ABS(longitude - ?) < 0.001
                        ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''
                    results = self.bot.db_manager.execute_query(query, (lat, lon))
                    if results and len(results) > 0:
                        return results[0].get('name', original_location)
            except Exception as e:
                self.logger.debug(f"Error getting repeater name: {e}")
            # Fallback to original location string
            return original_location

        try:
            import asyncio
            asyncio.get_event_loop()

            # For coordinates, always do reverse geocoding
            if location_type == "coordinates":
                location = await rate_limited_nominatim_reverse(
                    self.bot, f"{lat}, {lon}", timeout=self.url_timeout
                )
                if location and location.raw:
                    address = location.raw.get('address', {})
                    city = (address.get('city') or address.get('town') or
                           address.get('village') or address.get('municipality') or
                           address.get('suburb') or '')
                    state = address.get('state', '')
                    if city and state:
                        return f"{city}, {state}"
                    elif city:
                        return city
                    return original_location

            # For city/zipcode, use original if it worked, or reverse geocode
            if location_type in ["city", "zipcode"]:
                # Try reverse geocoding to get confirmed city name
                location = await rate_limited_nominatim_reverse(
                    self.bot, f"{lat}, {lon}", timeout=self.url_timeout
                )
                if location and location.raw:
                    address = location.raw.get('address', {})
                    city = (address.get('city') or address.get('town') or
                           address.get('village') or address.get('municipality') or
                           address.get('suburb') or '')
                    state = address.get('state', '')
                    if city and state:
                        return f"{city}, {state}"
                    elif city:
                        return city

                # Fallback to original location string
                return original_location

            return original_location
        except Exception as e:
            self.logger.debug(f"Error getting location name: {e}")
            return original_location

    async def _get_forecast(self, lat: float, lon: float, panel_watts: float,
                           azimuth: float, angle: float, location_name: str = "") -> str:
        """Get solar forecast from Forecast.Solar API"""
        try:
            # Convert panel watts to kWp
            kwp = panel_watts / 1000.0

            # Get API key (optional - free tier works without it)
            api_key = self.bot.config.get('External_Data', 'forecast_solar_api_key', fallback='')
            if not api_key:
                api_key = None

            # Query Forecast.Solar with scaling for small panels
            result = await self._query_forecast_solar_scaled(lat, lon, angle, azimuth, kwp, api_key)

            if not result:
                return self.translate('commands.solarforecast.error_fetching')

            # Check for rate limiting
            if result.get('rate_limited'):
                return self.translate('commands.solarforecast.rate_limit')

            # Format output to fit 130 characters
            return self._format_forecast(result, panel_watts, location_name, lat, lon)

        except Exception as e:
            self.logger.error(f"Error getting forecast: {e}")
            return self.translate('commands.solarforecast.error', error=str(e))

    def _get_cache_key(self, lat: float, lon: float, declination: float,
                      azimuth: float, kwp: float, api_key: Optional[str]) -> str:
        """Generate a cache key from request parameters"""
        # Round parameters to avoid cache misses due to floating point precision
        key_data = f"{lat:.4f},{lon:.4f},{declination:.1f},{azimuth:.1f},{kwp:.4f},{api_key or 'free'}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def _cleanup_expired_cache(self):
        """Remove all expired entries from cache"""
        current_time = time.time()
        expired_keys = []
        for key, cached in self.forecast_cache.items():
            age = current_time - cached['timestamp']
            if age >= self.CACHE_DURATION:
                expired_keys.append(key)

        for key in expired_keys:
            del self.forecast_cache[key]

        if expired_keys:
            self.logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")

    def _get_cached_forecast(self, cache_key: str) -> Optional[dict]:
        """Get cached forecast if available and not expired"""
        # Clean up expired entries periodically (every 10th access to avoid overhead)
        if len(self.forecast_cache) > 0 and len(self.forecast_cache) % 10 == 0:
            self._cleanup_expired_cache()

        if cache_key in self.forecast_cache:
            cached = self.forecast_cache[cache_key]
            age = time.time() - cached['timestamp']
            if age < self.CACHE_DURATION:
                self.logger.debug(f"Using cached forecast (age: {age:.0f}s)")
                return cached['data']
            else:
                # Cache expired, remove it
                del self.forecast_cache[cache_key]
                self.logger.debug(f"Cache expired (age: {age:.0f}s)")
        return None

    def _cache_forecast(self, cache_key: str, data: dict):
        """Cache forecast data"""
        self.forecast_cache[cache_key] = {
            'data': data,
            'timestamp': time.time()
        }
        self.logger.debug("Cached forecast data")

    async def _query_forecast_solar_scaled(self, lat: float, lon: float, declination: float,
                                          azimuth: float, kwp: float,
                                          api_key: Optional[str]) -> Optional[dict]:
        """Query Forecast.Solar API with automatic scaling for small panels"""
        # Generate cache key for the actual query parameters (before scaling)
        # We cache the base query (with MIN_KWP if scaling needed)
        query_kwp = self.MIN_KWP if kwp < self.MIN_KWP else kwp
        cache_key = self._get_cache_key(lat, lon, declination, azimuth, query_kwp, api_key)

        # Check cache first
        cached_result = self._get_cached_forecast(cache_key)
        if cached_result:
            # If we need scaling, apply it to cached data
            if kwp < self.MIN_KWP:
                scale_factor = kwp / self.MIN_KWP
                return {
                    'watts': {k: v * scale_factor for k, v in cached_result['watts'].items()},
                    'watt_hours': {k: v * scale_factor for k, v in cached_result['watt_hours'].items()},
                    'watt_hours_day': {k: v * scale_factor for k, v in cached_result['watt_hours_day'].items()},
                    'num_days': cached_result['num_days'],
                    'scaled': True,
                    'scale_factor': scale_factor
                }
            else:
                return cached_result

        kwp * 1000
        self.MIN_KWP * 1000

        # Check if scaling is needed
        if kwp < self.MIN_KWP:
            scale_factor = kwp / self.MIN_KWP
            # Query with minimum size
            result = await self._query_forecast_solar(lat, lon, declination, azimuth, self.MIN_KWP, api_key)
            if result:
                # Cache the base result (before scaling)
                self._cache_forecast(cache_key, result)

                # Scale all values for return
                return {
                    'watts': {k: v * scale_factor for k, v in result['watts'].items()},
                    'watt_hours': {k: v * scale_factor for k, v in result['watt_hours'].items()},
                    'watt_hours_day': {k: v * scale_factor for k, v in result['watt_hours_day'].items()},
                    'num_days': result['num_days'],
                    'scaled': True,
                    'scale_factor': scale_factor
                }
            return None
        else:
            # No scaling needed
            result = await self._query_forecast_solar(lat, lon, declination, azimuth, kwp, api_key)
            if result:
                # Cache the result
                self._cache_forecast(cache_key, result)
            return result

    async def _query_forecast_solar(self, lat: float, lon: float, declination: float,
                                    azimuth: float, kwp: float,
                                    api_key: Optional[str]) -> Optional[dict]:
        """Query Forecast.Solar API"""
        import asyncio
        # Build URL
        if api_key:
            base_url = f"https://api.forecast.solar/{api_key}/estimate"
        else:
            base_url = "https://api.forecast.solar/estimate"

        url = f"{base_url}/{lat}/{lon}/{declination}/{azimuth}/{kwp}"

        try:
            # Run HTTP request in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(url, timeout=self.url_timeout)
            )

            if response.status_code == 200:
                data = response.json()

                # Check for errors
                if 'message' in data:
                    msg = data['message']
                    if msg.get('type') != 'success':
                        self.logger.error(f"Forecast.Solar API error: {sanitize_name(msg.get('text', 'Unknown'))}")
                        return None

                result = data.get('result', {})
                return {
                    'watts': result.get('watts', {}),
                    'watt_hours': result.get('watt_hours', {}),
                    'watt_hours_day': result.get('watt_hours_day', {}),
                    'num_days': len(result.get('watt_hours_day', {})),
                    'scaled': False
                }
            elif response.status_code == 429:
                # Rate limit exceeded
                self.logger.warning("Forecast.Solar rate limit exceeded (429)")
                return {'rate_limited': True}
            else:
                self.logger.error(f"Forecast.Solar HTTP {response.status_code}")
                return None

        except Exception as e:
            self.logger.error(f"Error querying Forecast.Solar: {e}")
            return None

    def _format_forecast(self, result: dict, panel_watts: float, location_name: str = "",
                        lat: float = None, lon: float = None) -> str:
        """Format forecast data to fit 130 characters with user-friendly labels"""
        watt_hours_day = result.get('watt_hours_day', {})

        if not watt_hours_day:
            return self.translate('commands.solarforecast.no_data')

        local_tz, _ = get_config_timezone(self.bot.config, self.logger)
        now = datetime.now(local_tz)

        today = now.strftime('%Y-%m-%d')
        tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        day_after = (now + timedelta(days=2)).strftime('%Y-%m-%d')
        day_after_2 = (now + timedelta(days=3)).strftime('%Y-%m-%d')

        # Day+2 and Day+3 are labeled with (translated) weekday abbreviations.
        day_after_name = self._translate_day_abbreviation((now + timedelta(days=2)).strftime('%a'))
        day_after_2_name = self._translate_day_abbreviation((now + timedelta(days=3)).strftime('%a'))

        watts = result.get('watts', {})

        # Find the future peak first, to know which day it belongs to.
        # Forecast.Solar returns naive timestamps in the queried location's
        # local time; they are read as the bot's configured timezone.
        future_watts = {}
        peak_time_str = None
        peak_date = None
        max_watts = None

        if watts:
            for timestamp, power in watts.items():
                try:
                    dt_naive = _solar_timestamp_to_naive(timestamp, local_tz)
                    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
                    if dt_naive > now_naive:
                        future_watts[timestamp] = (power, dt_naive)
                except (ValueError, TypeError) as e:
                    self.logger.debug(f"Error parsing timestamp {timestamp}: {e}")

            # The peak shown is the highest future point today or tomorrow.
            today_tomorrow_watts = {
                timestamp: (power, dt_naive)
                for timestamp, (power, dt_naive) in future_watts.items()
                if dt_naive.strftime('%Y-%m-%d') in (today, tomorrow)
            }
            if today_tomorrow_watts:
                max_watts = max(power for power, dt in today_tomorrow_watts.values())
                for timestamp, (power, dt_naive) in today_tomorrow_watts.items():
                    if power == max_watts:
                        peak_time_str = dt_naive.strftime('%H:%M')
                        peak_date = dt_naive.strftime('%Y-%m-%d')
                        break

        def local_date_of(timestamp_str):
            """Local date string of a timestamp, or None when it does not parse."""
            try:
                return _solar_timestamp_to_naive(timestamp_str, local_tz).strftime('%Y-%m-%d')
            except:  # noqa: E722 - any unparseable timestamp counts as no date
                return None

        def production_hours(day, min_power_threshold):
            """Distinct clock hours on *day* at or above the threshold (several points can share an hour)."""
            unique_hours = set()
            for ts, power in watts.items():
                local_date = local_date_of(ts)
                if local_date == day and power >= min_power_threshold:
                    try:
                        unique_hours.add(f"{local_date}_{_solar_timestamp_to_naive(ts, local_tz).hour}")
                    except:  # noqa: E722
                        pass
            return len(unique_hours)

        def utilization(wh, hours):
            """Percent of the panel's rated output over *hours*; None when that is not positive."""
            typical_max_energy = panel_watts * hours
            if typical_max_energy > 0:
                return (wh / typical_max_energy) * 100
            return None

        # %util is shown on the first forecast day only (today, else tomorrow).
        # Note the two variants: today counts distinct hours at >= 0.1 W,
        # tomorrow counts data points at >= 1% of the panel (or 0.1 W).
        first_day_utilization = None
        first_day_date = None
        if today in watt_hours_day:
            first_day_date = today
            first_day_wh = watt_hours_day[today]
            first_day_prod_hours = production_hours(today, 0.1) if watts else 0
            if first_day_prod_hours > 0:
                first_day_utilization = utilization(first_day_wh, first_day_prod_hours)
        elif tomorrow in watt_hours_day:
            first_day_date = tomorrow
            first_day_wh = watt_hours_day[tomorrow]
            first_day_prod_hours = 0
            if watts:
                min_power_threshold = max(panel_watts * 0.01, 0.1)
                for ts, power in watts.items():
                    if local_date_of(ts) == tomorrow and power >= min_power_threshold:
                        first_day_prod_hours += 1
            if first_day_prod_hours > 0:
                first_day_utilization = utilization(first_day_wh, first_day_prod_hours)

        def day_part(day, label):
            """One day's summary: energy, production hours with %util or %, and the peak if it falls that day."""
            wh = watt_hours_day[day]
            prod_hours = production_hours(day, max(panel_watts * 0.01, 0.1)) if watts else 0
            part = label(wh)
            if prod_hours > 0:
                if first_day_utilization is not None and first_day_date == day:
                    part += self.translate('commands.solarforecast.labels.hours_util', hours=prod_hours, util=first_day_utilization)
                else:
                    day_utilization = utilization(wh, prod_hours)
                    if day_utilization is not None:
                        part += self.translate('commands.solarforecast.labels.hours_percent', hours=prod_hours, percent=day_utilization)
                    else:
                        part += self.translate('commands.solarforecast.labels.hours_only', hours=prod_hours)
            # The peak is only ever today or tomorrow.
            if peak_date == day and peak_time_str:
                part += " " + self.translate('commands.solarforecast.labels.peak', watts=max_watts, time=peak_time_str)
            return part

        lines = []

        # The first line starts with the panel size and location.
        panel_info = f"{panel_watts:.0f}W"
        if location_name:
            abbreviated_location = abbreviate_location(location_name, max_length=25)
            first_line_prefix = f"{abbreviated_location}: {panel_info} "
        else:
            first_line_prefix = f"{panel_info} "

        if today in watt_hours_day:
            today_part = day_part(today, lambda wh: self.translate('commands.solarforecast.labels.today', wh=wh))
            lines.append(f"{first_line_prefix}{today_part}")

        if tomorrow in watt_hours_day:
            lines.append(day_part(tomorrow, lambda wh: self.translate('commands.solarforecast.labels.tomorrow', wh=wh)))

        # Day+2 and Day+3 share one line.
        day_plus_line_parts = []
        for day, name in ((day_after, day_after_name), (day_after_2, day_after_2_name)):
            if day in watt_hours_day:
                day_plus_line_parts.append(day_part(
                    day, lambda wh, name=name: self.translate('commands.solarforecast.labels.day_format', day=name, wh=wh)
                ))
        if day_plus_line_parts:
            separator = self.translate('commands.solarforecast.labels.separator')
            lines.append(separator.join(day_plus_line_parts))

        # Once today's peak has passed, report it at the end.
        if not future_watts and watts:
            today_watts = [power for ts, power in watts.items() if local_date_of(ts) == today]
            if today_watts:
                lines.append(self.translate('commands.solarforecast.labels.peak_past', watts=max(today_watts)))

        return "\n".join(lines)

    async def _send_forecast_response(self, message: MeshMessage, forecast_text: str):
        """Send forecast response, splitting into multiple messages if needed"""
        import asyncio

        lines = forecast_text.split('\n')

        # If single line and under 130 chars, send as-is
        if len(lines) == 1 and len(forecast_text) <= 130:
            await self.send_response(message, forecast_text)
            return

        # Multi-line or long message - send each line as separate message if needed
        current_message = ""
        message_count = 0

        for i, line in enumerate(lines):
            # Check if adding this line would exceed 130 characters
            test_message = current_message + "\n" + line if current_message else line

            if len(test_message) > 130:
                # Send current message and start new one
                if current_message:
                    # Per-user rate limit applies only to first message (trigger); skip for continuations
                    await self.send_response(
                        message, current_message,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    # Wait between messages (same as other commands)
                    if message_count > 0 and i < len(lines):
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
            else:
                # Add line to current message
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line

        # Send the last message if there's content (continuation; skip per-user rate limit)
        if current_message:
            await self.send_response(message, current_message, skip_user_rate_limit=True)

