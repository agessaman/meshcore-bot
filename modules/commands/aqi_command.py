#!/usr/bin/env python3
"""
AQI command for the MeshCore Bot
Provides Air Quality Index information using OpenMeteo API
"""

import asyncio
from pathlib import Path
from typing import Optional

import openmeteo_requests
import requests_cache
from retry_requests import retry

from ..location import (
    OPTIONS_AQI,
    ResolveOptions,
    geocode_city_best_effort,
    resolve_location,
)
from ..location import (
    get_neighborhood_queries as location_neighborhood_queries,
)
from ..models import MeshMessage
from ..utils import (
    abbreviate_location,
    get_nominatim_geocoder,
    is_valid_timezone,
    normalize_us_state,
)
from .base_command import BaseCommand


class AqiCommand(BaseCommand):
    """Handles AQI commands with location support using OpenMeteo API.

    Provides Air Quality Index information for specified locations, including
    cities, ZIP codes, and coordinates. Supports international locations and
    provides health impact categories.
    """

    # Plugin metadata
    # Read-only informational output; safe for scheduled {cmd:...} rendering.
    render_safe = True
    name = "aqi"
    keywords = ['aqi', 'air', 'airquality', 'air_quality']
    description = "Get Air Quality Index for a location (usage: aqi seattle, aqi greenwood, aqi vancouver canada, aqi 47.6,-122.3, or aqi help)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    requires_internet = True  # Requires internet access for OpenMeteo API and geocoding

    # Documentation
    short_description = "Get Air Quality Index for a location"
    usage = "aqi <city|neighborhood|coordinates|help>"
    examples = ["aqi seattle", "aqi 47.6,-122.3"]
    parameters = [
        {"name": "location", "description": "City, neighborhood, lat/lon, or 'help'"}
    ]

    # Error constants
    ERROR_FETCHING_DATA = "Error fetching AQI data"
    NO_DATA_AVAILABLE = "No AQI data available"

    # Web-viewer settings schema (see modules/settings_schema.py).
    # These are shared [Weather] defaults used by all weather commands.
    settings_schema = [
        {"key": "default_state", "label": "Default state", "type": "str", "section": "Weather",
         "default": "", "help": "2-letter state for city disambiguation (e.g. WA). Shared weather setting."},
        {"key": "default_country", "label": "Default country", "type": "str", "section": "Weather",
         "default": "US", "help": "2-letter country code (e.g. US). Shared weather setting."},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.aqi_enabled = self.get_config_value('Aqi_Command', 'enabled', fallback=True, value_type='bool')
        self.url_timeout = 10  # seconds

        # Get default state and country from config for city disambiguation
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
        self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')

        # Get timezone from config (validated); invalid or empty falls back to system; for API use default or UTC
        timezone_str = self.bot.config.get('Bot', 'timezone', fallback='').strip()
        if timezone_str and is_valid_timezone(timezone_str):
            self.timezone = timezone_str
        else:
            if timezone_str:
                self.logger.warning("Invalid timezone '%s', using system timezone", timezone_str)
            self.timezone = "America/Los_Angeles" if not timezone_str else "UTC"

        # Initialize geocoder (will use rate-limited helpers for actual calls)
        self.geolocator = get_nominatim_geocoder()

        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager

        # Setup the Open-Meteo API client with cache and retry on error.
        # requests_cache backs this with a SQLite file, so keep it beside the bot
        # database in the service-writable state directory. A relative path lands
        # in the working directory, which is read-only under the hardened service
        # unit and fails plugin load with "unable to open database file".
        cache_path = Path(self.db_manager.db_path).parent / 'aqi_http_cache'
        cache_session = requests_cache.CachedSession(str(cache_path), expire_after=3600)
        retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
        self.openmeteo = openmeteo_requests.Client(session=retry_session)

        # Snarky responses for astronomical objects
        self.astronomical_responses = {
            'sun': "The Sun's AQI is off the charts! Solar wind and coronal mass ejections make Earth's air look pristine. ☀️",
            'moon': "You like breathing regolith? The Moon has no atmosphere, so AQI is technically perfect (if you can breathe vacuum). 🌙",
            'the moon': "You like breathing regolith? The Moon has no atmosphere, so AQI is technically perfect (if you can breathe vacuum). 🌙",
            'mercury': "Mercury's atmosphere is so thin it's practically vacuum. AQI: Perfect, if you can survive 800°F temperature swings. ☿️",
            'venus': "Venus has an atmosphere of 96% CO2 with sulfuric acid clouds. AQI: Hazardous doesn't even begin to describe it. ♀️",
            'earth': "Earth's AQI varies by location. Try a specific city or coordinates! 🌍",
            'mars': "Mars has a thin CO2 atmosphere with dust storms. AQI: Generally good, but those dust storms are brutal. ♂️",
            'jupiter': "Jupiter is a gas giant with no solid surface. AQI: N/A (you'd be crushed by atmospheric pressure first). ♃",
            'saturn': "Saturn's atmosphere is mostly hydrogen and helium. AQI: Perfect, if you can survive the pressure and cold. ♄",
            'uranus': "Uranus has methane in its atmosphere. AQI: Smells like farts, but at least it's not toxic. ♅",
            'neptune': "Neptune's atmosphere has methane and hydrogen sulfide. AQI: Smells like rotten eggs, but you'd freeze first. ♆",
            'pluto': "Pluto's atmosphere is mostly nitrogen with some methane. AQI: Good, but it's so cold your lungs would freeze. ♇",
            'europa': "Europa has a thin oxygen atmosphere. AQI: Excellent, but you'd freeze solid in the vacuum of space. 🌑",
            'titan': "Titan has a thick nitrogen atmosphere with methane. AQI: Breathable, but it's -290°F and rains liquid methane. 🪐",
            'io': "Io has a thin sulfur dioxide atmosphere from volcanic activity. AQI: Toxic, but the radiation would kill you first. 🌋",
            'ganymede': "Ganymede has a thin oxygen atmosphere. AQI: Good, but you'd freeze in the vacuum of space. 🛸",
            'callisto': "Callisto has a thin carbon dioxide atmosphere. AQI: Decent, but it's -220°F and you're in space. ❄️",
            'enceladus': "Enceladus has water vapor from geysers. AQI: Perfect, but you'd freeze instantly in space. 💧",
            'triton': "Triton has a thin nitrogen atmosphere. AQI: Good, but it's -390°F and you're in deep space. 🥶",
            # Bonus fun responses
            'space': "Space has no atmosphere, so AQI is perfect! Just don't forget your spacesuit. 🚀",
            'void': "The void of space has excellent air quality - zero pollutants! Just remember to bring your own air. 🌌",
            'black hole': "Black holes have no atmosphere, but the tidal forces would be a bigger problem than air quality. 🕳️",
            'asteroid': "Asteroids have no atmosphere, so AQI is perfect! Just watch out for the vacuum of space. ☄️",
            'comet': "Comets have thin atmospheres of water vapor and dust. AQI: Variable, but you'd freeze in space anyway. ☄️"
        }

    def get_help_text(self) -> str:
        region = self.default_state or self.default_country
        return f"Usage: aqi <city|neighborhood|city country|lat,lon|help> - Get AQI for city/neighborhood in {region}, intl cities, coordinates, or help"

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.aqi_enabled:
            return False
        return super().can_execute(message)

    def get_pollutant_help(self) -> str:
        """Get help text explaining pollutant types within 130 characters.

        Returns:
            str: Compact help string explaining pollutant abbreviations.
        """
        # Compact explanation of all pollutants - fits within 130 chars
        return "AQI Help: PM2.5=fine particles, PM10=coarse, O3=ozone, NO2=nitrogen dioxide, CO=carbon monoxide, SO2=sulfur dioxide"

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the AQI command.

        Args:
            message: The input message trigger.

        Returns:
            bool: True if execution was successful.
        """
        content = message.content.strip()

        # Parse the command to extract location
        # Support formats: "aqi seattle", "aqi paris, tx", "aqi 47.6,-122.3", "air everett", "aqi help"
        parts = content.split()
        if len(parts) < 2:
            await self.send_response(message, "Usage: aqi <city|neighborhood|city country|lat,lon> - Example: aqi seattle, aqi greenwood, aqi vancouver canada, or aqi 47.6,-122.3")
            return True

        # Check for help command
        if len(parts) >= 2 and parts[1].lower() == 'help':
            help_text = self.get_pollutant_help()
            await self.send_response(message, help_text)
            return True

        # Join all parts after the command to handle "city, state" format
        location = ' '.join(parts[1:]).strip()

        # Check for astronomical objects first
        location_lower = location.lower()
        if location_lower in self.astronomical_responses:
            await self.send_response(message, self.astronomical_responses[location_lower])
            return True

        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Get AQI data for the location (single resolve with intl rewrite)
            aqi_data = await self.get_aqi_for_location(location)

            # Send the response
            await self.send_response(message, aqi_data)
            return True

        except Exception as e:
            self.logger.error(f"Error in AQI command: {e}")
            await self.send_response(message, f"Error getting AQI data: {e}")
            return True

    def _resolved_state_differs_from_default(self, address_info: Optional[dict]) -> bool:
        """True when reverse-geocode state/country differs from bot default_state."""
        if not address_info or not self.default_state:
            return False
        country = address_info.get("country", "")
        state = address_info.get("state", "")
        default_abbr, default_full = normalize_us_state(self.default_state)
        defaults = {d for d in (self.default_state, default_abbr, default_full) if d}
        if country in ("United States", "US", "United States of America"):
            abbr, full = normalize_us_state(state) if state else (None, None)
            actuals = {a for a in (abbr, full, state) if a}
            return bool(actuals) and actuals.isdisjoint(defaults)
        actual_state = country or address_info.get("province") or ""
        return bool(actual_state) and actual_state not in defaults

    async def get_aqi_for_location(
        self, location: str, location_type: Optional[str] = None
    ) -> str:
        """Get AQI data for a location (city, ZIP, or coordinates).

        Geocoding and the OpenMeteo fetch are both blocking HTTP calls, so the
        whole resolve-and-fetch runs on a worker thread; doing them inline would
        stall message handling and reconnection for up to several 10s timeouts.

        Args:
            location: Raw location string (city name, ZIP, or "lat,lon").
            location_type: Unused; kept for call-site/test compatibility.

        Returns:
            str: Formatted AQI string or error message.
        """
        return await asyncio.to_thread(self._get_aqi_for_location_sync, location, location_type)

    def _get_aqi_for_location_sync(
        self, location: str, location_type: Optional[str] = None
    ) -> str:
        """Blocking body of :meth:`get_aqi_for_location` (runs off the event loop)."""
        try:
            opts = ResolveOptions(
                default_state=self.default_state,
                default_country=self.default_country,
                use_international_cities=OPTIONS_AQI.use_international_cities,
                use_neighborhoods=OPTIONS_AQI.use_neighborhoods,
                use_structured_zip=OPTIONS_AQI.use_structured_zip,
                label_style=OPTIONS_AQI.label_style,
                include_address_info=True,
                timeout=10,
            )
            resolved = resolve_location(self.bot, location, options=opts)

            if resolved.error == "invalid_latitude":
                detail = resolved.error_detail or location
                return f"Invalid latitude: {detail}. Must be between -90 and 90."
            if resolved.error == "invalid_longitude":
                detail = resolved.error_detail or location
                return f"Invalid longitude: {detail}. Must be between -180 and 180."
            if resolved.error == "invalid_coordinates":
                return f"Invalid coordinates format: {location}. Use format: lat,lon (e.g., 47.6,-122.3)"
            if resolved.error == "no_location_zipcode":
                return f"Could not find ZIP code '{location.strip()}'"
            if resolved.error == "no_location_city":
                if "," in (resolved.query or location):
                    return f"Could not find city '{resolved.query or location}'"
                region = self.default_state or self.default_country
                return f"Could not find city '{resolved.query or location}' in {region}"
            if resolved.lat is None or resolved.lon is None:
                return f"Could not find location '{location}'"

            lat, lon = resolved.lat, resolved.lon
            address_info = resolved.address_info
            location_type = resolved.location_type or location_type

            aqi_data = self.get_openmeteo_aqi(lat, lon)
            if aqi_data == self.ERROR_FETCHING_DATA:
                return "Error fetching AQI data from OpenMeteo"

            location_prefix = ""
            if location_type == "coordinates":
                location_prefix = f"{lat:.3f},{lon:.3f}: "
            elif resolved.display_name:
                city_display = resolved.display_name
                test_output = f"{city_display}: {aqi_data}"
                if len(test_output) <= 130:
                    location_prefix = f"{city_display}: "
                elif location_type == "zipcode":
                    location_prefix = f"{location.strip()}: "
                elif self._resolved_state_differs_from_default(address_info):
                    # Over budget: only keep a short prefix when outside default region
                    short = abbreviate_location(city_display, max_length=20)
                    location_prefix = f"{short}: "
            elif location_type == "zipcode":
                location_prefix = f"{location.strip()}: "

            return f"{location_prefix}{aqi_data}"

        except Exception as e:
            self.logger.error(f"Error getting AQI for {location_type} {location}: {e}")
            return f"Error getting AQI data: {e}"

    def city_to_lat_lon(self, city: str) -> tuple:
        """Convert city name to latitude and longitude using default state.

        Thin wrapper over shared ``geocode_city_best_effort`` (kept for tests/compat).
        """
        lat, lon, address_info = geocode_city_best_effort(
            self.bot,
            city,
            default_state=self.default_state,
            default_country=self.default_country,
            use_neighborhoods=True,
            include_address_info=True,
            timeout=10,
        )
        if lat is None or lon is None:
            return (None, None, None)
        return lat, lon, address_info or {}

    def get_neighborhood_queries(self, city: str) -> list:
        """Generate neighborhood-specific search queries for major cities."""
        return location_neighborhood_queries(city)

    def get_openmeteo_aqi(self, lat: float, lon: float) -> str:
        """Get AQI data from OpenMeteo API.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            str: Formatted AQI string or error constant.
        """
        try:
            # Make sure all required weather variables are listed here
            # The order of variables in current is important to assign them correctly below
            url = "https://air-quality-api.open-meteo.com/v1/air-quality"
            # OpenMeteo requires a valid IANA timezone; empty config sends "" and returns "Invalid timezone"
            tz_for_api = (self.timezone or "UTC").strip() or "UTC"
            params = {
                "latitude": lat,
                "longitude": lon,
                "current": ["us_aqi", "european_aqi", "pm10", "pm2_5", "carbon_monoxide", "nitrogen_dioxide", "sulphur_dioxide", "ozone", "dust"],
                "timezone": tz_for_api,
                "forecast_days": 1,
            }
            responses = self.openmeteo.weather_api(url, params=params)

            # Process first location
            response = responses[0]

            # Process current data. The order of variables needs to be the same as requested.
            current = response.Current()
            current_us_aqi = current.Variables(0).Value()
            current_european_aqi = current.Variables(1).Value()
            current_pm10 = current.Variables(2).Value()
            current_pm2_5 = current.Variables(3).Value()
            current_carbon_monoxide = current.Variables(4).Value()
            current_nitrogen_dioxide = current.Variables(5).Value()
            current_sulphur_dioxide = current.Variables(6).Value()
            current_ozone = current.Variables(7).Value()
            current_dust = current.Variables(8).Value()

            # Format the AQI response
            return self.format_aqi_response(
                current_us_aqi, current_european_aqi, current_pm10, current_pm2_5,
                current_carbon_monoxide, current_nitrogen_dioxide, current_sulphur_dioxide,
                current_ozone, current_dust
            )

        except Exception as e:
            self.logger.error(f"Error fetching OpenMeteo AQI: {e}")
            return self.ERROR_FETCHING_DATA

    def format_aqi_response(self, us_aqi, european_aqi, pm10, pm2_5, co, no2, so2, ozone, dust) -> str:
        """Format AQI data for display within 130 characters.

        Args:
            us_aqi: US Air Quality Index value.
            european_aqi: European Air Quality Index value.
            pm10: PM10 concentration.
            pm2_5: PM2.5 concentration.
            co: Carbon Monoxide concentration.
            no2: Nitrogen Dioxide concentration.
            so2: Sulfur Dioxide concentration.
            ozone: Ozone concentration.
            dust: Dust concentration.

        Returns:
            str: Formatted AQI string.
        """
        try:
            # Start with US AQI as primary
            if us_aqi is not None and us_aqi > 0:
                aqi_emoji = self.get_aqi_emoji(us_aqi)
                aqi_category = self.get_aqi_category(us_aqi)
                aqi_str = f"{aqi_emoji} {us_aqi:.0f} ({aqi_category})"
            else:
                # Fallback to European AQI
                if european_aqi is not None and european_aqi > 0:
                    aqi_emoji = self.get_european_aqi_emoji(european_aqi)
                    aqi_str = f"{aqi_emoji} {european_aqi:.0f} (EU)"
                else:
                    aqi_str = "🌫️ N/A"

            # Add key pollutants if space allows - prioritize by health impact
            # Priority order: PM2.5 > PM10 > O3 > NO2 > CO > SO2
            pollutants = []

            # PM2.5 is most important (fine particles - most harmful to health)
            if pm2_5 is not None and pm2_5 > 0:
                pollutants.append(f"PM2.5:{pm2_5:.0f}")

            # PM10 is second most important (coarse particles)
            if pm10 is not None and pm10 > 0 and len(aqi_str + " ".join(pollutants)) < 100:
                pollutants.append(f"PM10:{pm10:.0f}")

            # Ozone if space allows (ground-level ozone - respiratory irritant)
            if ozone is not None and ozone > 0 and len(aqi_str + " ".join(pollutants)) < 110:
                pollutants.append(f"O3:{ozone:.0f}")

            # NO2 if space allows (nitrogen dioxide - respiratory irritant)
            if no2 is not None and no2 > 0 and len(aqi_str + " ".join(pollutants)) < 120:
                pollutants.append(f"NO2:{no2:.0f}")

            # CO if space allows (carbon monoxide - toxic gas)
            if co is not None and co > 0 and len(aqi_str + " ".join(pollutants)) < 125:
                pollutants.append(f"CO:{co:.0f}")

            # SO2 if space allows (sulfur dioxide - respiratory irritant)
            if so2 is not None and so2 > 0 and len(aqi_str + " ".join(pollutants)) < 130:
                pollutants.append(f"SO2:{so2:.0f}")

            # Combine everything
            result = f"{aqi_str} {' '.join(pollutants)}" if pollutants else aqi_str

            # Ensure we don't exceed 130 characters
            if len(result) > 130:
                # Try with fewer pollutants - remove least important ones first
                pollutants = []
                if pm2_5 is not None and pm2_5 > 0:
                    pollutants.append(f"PM2.5:{pm2_5:.0f}")
                if pm10 is not None and pm10 > 0:
                    pollutants.append(f"PM10:{pm10:.0f}")
                if ozone is not None and ozone > 0:
                    pollutants.append(f"O3:{ozone:.0f}")
                if no2 is not None and no2 > 0:
                    pollutants.append(f"NO2:{no2:.0f}")

                result = f"{aqi_str} {' '.join(pollutants)}"

                # If still too long, try with just the most important
                if len(result) > 130:
                    pollutants = []
                    if pm2_5 is not None and pm2_5 > 0:
                        pollutants.append(f"PM2.5:{pm2_5:.0f}")
                    if pm10 is not None and pm10 > 0:
                        pollutants.append(f"PM10:{pm10:.0f}")

                    result = f"{aqi_str} {' '.join(pollutants)}"

                    # If still too long, just return the AQI
                    if len(result) > 130:
                        result = aqi_str

            return result

        except Exception as e:
            self.logger.error(f"Error formatting AQI response: {e}")
            return "Error formatting AQI data"

    def get_aqi_emoji(self, aqi: float) -> str:
        """Get emoji for US AQI value.

        Args:
            aqi: US AQI value.

        Returns:
            str: Emoji representing AQI level (🟢, 🟡, 🟠, 🔴, 🟣, 🟤).
        """
        if aqi <= 50:
            return "🟢"  # Good
        elif aqi <= 100:
            return "🟡"  # Moderate
        elif aqi <= 150:
            return "🟠"  # Unhealthy for Sensitive Groups
        elif aqi <= 200:
            return "🔴"  # Unhealthy
        elif aqi <= 300:
            return "🟣"  # Very Unhealthy
        else:
            return "🟤"  # Hazardous

    def get_european_aqi_emoji(self, aqi: float) -> str:
        """Get emoji for European AQI value.

        Args:
            aqi: European AQI value.

        Returns:
            str: Emoji representing European AQI level.
        """
        if aqi <= 25:
            return "🟢"  # Good
        elif aqi <= 50:
            return "🟡"  # Fair
        elif aqi <= 75:
            return "🟠"  # Moderate
        elif aqi <= 100:
            return "🔴"  # Poor
        else:
            return "🟣"  # Very Poor

    def get_aqi_category(self, aqi: float) -> str:
        """Get category name for US AQI value.

        Args:
            aqi: US AQI value.

        Returns:
            str: Category description (e.g., "Good", "Moderate").
        """
        if aqi <= 50:
            return "Good"
        elif aqi <= 100:
            return "Moderate"
        elif aqi <= 150:
            return "Unhealthy for Sensitive Groups"
        elif aqi <= 200:
            return "Unhealthy"
        elif aqi <= 300:
            return "Very Unhealthy"
        else:
            return "Hazardous"
