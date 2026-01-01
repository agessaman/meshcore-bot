#!/usr/bin/env python3
"""
Global Weather command for the MeshCore Bot
Provides worldwide weather information using Open-Meteo API
"""

import re
import requests
from datetime import datetime, timedelta
from geopy.geocoders import Nominatim
from ...utils import rate_limited_nominatim_geocode_sync, rate_limited_nominatim_reverse_sync, get_nominatim_geocoder, geocode_city_sync
from ..base_command import BaseCommand
from ...models import MeshMessage


class GlobalWxCommand(BaseCommand):
    """Handles global weather commands with city/location support"""
    
    # Plugin metadata
    name = "gwx"
    keywords = ['gwx', 'globalweather', 'gwxa']
    description = "Get weather information for any global location (usage: gwx Tokyo)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    
    # Error constants - will use translations instead
    ERROR_FETCHING_DATA = "ERROR_FETCHING_DATA"  # Placeholder, will use translate()
    NO_ALERTS = "No weather alerts available"
    
    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10  # seconds
        
        # Get default state and country from config for city disambiguation
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='WA')
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
    
    def get_help_text(self) -> str:
        return self.translate('commands.gwx.help')
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a weather keyword"""
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        content_lower = content.lower()
        for keyword in self.keywords:
            if content_lower.startswith(keyword + ' '):
                return True
        return False
    
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the weather command"""
        content = message.content.strip()
        
        # Parse the command to extract location and forecast type
        parts = content.split()
        if len(parts) < 2:
            await self.send_response(message, self.translate('commands.gwx.usage'))
            return True
        
        # Check for forecast type options: "tomorrow", or a number 2-7
        forecast_type = "default"
        num_days = 7  # Default for multi-day forecast
        location_parts = parts[1:]
        
        # Check last part for forecast type
        if len(location_parts) > 0:
            last_part = location_parts[-1].lower()
            if last_part == "tomorrow":
                forecast_type = "tomorrow"
                location_parts = location_parts[:-1]
            elif last_part.isdigit():
                # Check if it's a number between 2-7
                days = int(last_part)
                if 2 <= days <= 7:
                    forecast_type = "multiday"
                    num_days = days
                    location_parts = location_parts[:-1]
            elif last_part in ["7day", "7-day"]:
                forecast_type = "multiday"
                num_days = 7
                location_parts = location_parts[:-1]
        
        # Join remaining parts to handle "city, country" format
        location = ' '.join(location_parts).strip()
        
        if not location:
            await self.send_response(message, self.translate('commands.gwx.usage'))
            return True
        
        try:
            # Record execution for this user
            self.record_execution(message.sender_id)
            
            # Get weather data for the location
            weather_data = await self.get_weather_for_location(location, forecast_type, num_days)
            
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
    
    async def get_weather_for_location(self, location: str, forecast_type: str = "default", num_days: int = 7) -> str:
        """Get weather data for any global location
        
        Args:
            location: The location (city name, etc.)
            forecast_type: "default", "tomorrow", or "multiday"
            num_days: Number of days for multiday forecast (2-7)
        """
        try:
            # Convert location to lat/lon with address details
            result = self.geocode_location(location)
            if not result or result[0] is None or result[1] is None:
                return self.translate('commands.gwx.no_location', location=location)
            
            lat, lon, address_info, geocode_result = result
            
            # Format location name for display
            location_display = self._format_location_display(address_info, geocode_result, location)
            
            # Get weather forecast from Open-Meteo based on type
            if forecast_type == "tomorrow":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="tomorrow")
            elif forecast_type == "multiday":
                weather_text = self.get_open_meteo_weather(lat, lon, forecast_type="multiday", num_days=num_days)
            else:
                weather_text = self.get_open_meteo_weather(lat, lon)
            
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
        """Convert location string to lat/lon with address details
        
        Uses geocode_city_sync for proper default state/country handling,
        which prioritizes locations in the configured default state/country.
        """
        try:
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
    
    def _format_location_display(self, address_info: dict, geocode_result, fallback: str) -> str:
        """Format location name for display from address info - returns 'City, CountryCode' format"""
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
        """Convert full state name to abbreviation"""
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
    
    def get_open_meteo_weather(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7) -> str:
        """Get weather forecast from Open-Meteo API
        
        Args:
            lat: Latitude
            lon: Longitude
            forecast_type: "default", "tomorrow", or "multiday"
            num_days: Number of days for multiday forecast (2-7)
        """
        try:
            # Open-Meteo API endpoint with current weather and forecast
            api_url = "https://api.open-meteo.com/v1/forecast"
            
            # Determine forecast_days based on type
            if forecast_type == "multiday":
                forecast_days = min(num_days, 7)  # Open-Meteo supports up to 7 days
            elif forecast_type == "tomorrow":
                forecast_days = 2  # Need today and tomorrow
            else:
                forecast_days = 2  # Default
            
            params = {
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,dewpoint_2m,visibility,surface_pressure',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max',
                'hourly': 'temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m',
                'temperature_unit': self.temperature_unit,
                'wind_speed_unit': self.wind_speed_unit,
                'precipitation_unit': self.precipitation_unit,
                'timezone': 'auto',
                'forecast_days': forecast_days
            }
            
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
            temp_unit = current_units.get('temperature_2m', '°F')
            visibility_unit = current_units.get('visibility', 'm')
            
            # Extract current conditions
            current = data.get('current', {})
            daily = data.get('daily', {})
            hourly = data.get('hourly', {})
            
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
            if conditions and len(weather) < 120:
                weather += " " + " ".join(conditions)
            
            # Add forecast for today/tonight and tomorrow
            # API should return temperatures in Fahrenheit when requested
            if daily:
                today_high = int(daily['temperature_2m_max'][0])
                today_low = int(daily['temperature_2m_min'][0])
                
                weather += f" | {period_name}: {today_high}{temp_symbol}/{today_low}{temp_symbol}"
                
                # Add tomorrow if space allows (check length more carefully)
                if len(daily['temperature_2m_max']) > 1:
                    tomorrow_high = int(daily['temperature_2m_max'][1])
                    tomorrow_low = int(daily['temperature_2m_min'][1])
                    
                    tomorrow_code = daily['weather_code'][1]
                    tomorrow_emoji = self._get_weather_emoji(tomorrow_code)
                    
                    # Get tomorrow's period name
                    tomorrow_period = self.translate('commands.gwx.periods.tomorrow')
                    tomorrow_str = f" | {tomorrow_period}: {tomorrow_emoji}{tomorrow_high}{temp_symbol}/{tomorrow_low}{temp_symbol}"
                    
                    # Only add if we have space (leave room for potential precipitation)
                    if len(weather + tomorrow_str) < 180:  # Increased limit to prevent truncation
                        weather += tomorrow_str
                        
                        # Add precipitation probability if significant and space allows
                        if len(daily.get('precipitation_probability_max', [])) > 1:
                            precip_prob = daily['precipitation_probability_max'][1]
                            if precip_prob >= 30:
                                precip_str = f" 🌦️{precip_prob}%"
                                if len(weather + precip_str) <= 200:  # Reasonable message length limit
                                    weather += precip_str
            
            return weather
            
        except Exception as e:
            self.logger.error(f"Error fetching Open-Meteo weather: {e}")
            return self.translate('commands.gwx.error_fetching')
    
    def format_tomorrow_forecast(self, data: dict) -> str:
        """Format a detailed forecast for tomorrow"""
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
            
            # Get precipitation probability
            precip_info = ""
            if len(daily.get('precipitation_probability_max', [])) > 1:
                precip_prob = daily['precipitation_probability_max'][1]
                if precip_prob >= 30:
                    precip_info = f" 🌦️{precip_prob}%"
            
            tomorrow_period = self.translate('commands.gwx.periods.tomorrow')
            return f"{tomorrow_period}: {tomorrow_emoji}{tomorrow_desc} {tomorrow_high}{temp_symbol}/{tomorrow_low}{temp_symbol}{wind_info}{precip_info}"
            
        except Exception as e:
            self.logger.error(f"Error formatting tomorrow forecast: {e}")
            return self.translate('commands.gwx.tomorrow_error')
    
    def format_multiday_forecast(self, data: dict, num_days: int = 7) -> str:
        """Format a less detailed multi-day forecast summary"""
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
                
                parts.append(f"{day_abbrev}: {emoji}{desc_short} {high}{temp_symbol}/{low}{temp_symbol}")
            
            if not parts:
                return self.translate('commands.gwx.multiday_not_available', num_days=num_days)
            
            return "\n".join(parts)
            
        except Exception as e:
            self.logger.error(f"Error formatting {num_days}-day forecast: {e}")
            return self.translate('commands.gwx.multiday_error', num_days=num_days)
    
    def _count_display_width(self, text: str) -> int:
        """Count display width of text, accounting for emojis which may take 2 display units"""
        import re
        # Count regular characters
        width = len(text)
        # Emojis typically take 2 display units in terminals/clients
        # Count emoji characters (basic emoji pattern)
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags
            "\U00002702-\U000027B0"  # dingbats
            "\U000024C2-\U0001F251"  # enclosed characters
            "]+",
            flags=re.UNICODE
        )
        emoji_matches = emoji_pattern.findall(text)
        # Each emoji sequence adds 1 extra width unit (since len() already counts it as 1)
        # So we add 1 for each emoji sequence to account for display width
        width += len(emoji_matches)
        return width
    
    async def _send_multiday_forecast(self, message: MeshMessage, forecast_text: str):
        """Send multi-day forecast response, splitting into multiple messages if needed"""
        import asyncio
        
        lines = forecast_text.split('\n')
        
        # Remove empty lines
        lines = [line.strip() for line in lines if line.strip()]
        
        if not lines:
            return
        
        # If single line and under 130 chars, send as-is
        if self._count_display_width(forecast_text) <= 130:
            await self.send_response(message, forecast_text)
            return
        
        # Multi-line message - try to fit as many days as possible in one message
        # Only split when necessary (message would exceed 130 chars)
        current_message = ""
        message_count = 0
        
        for i, line in enumerate(lines):
            if not line:
                continue
            
            # Check if adding this line would exceed 130 characters (using display width)
            if current_message:
                test_message = current_message + "\n" + line
            else:
                test_message = line
            
            # Only split if message would exceed 130 chars (using display width)
            if self._count_display_width(test_message) > 130:
                # Send current message and start new one
                if current_message:
                    await self.send_response(message, current_message)
                    message_count += 1
                    # Wait between messages (same as other commands)
                    if i < len(lines):
                        await asyncio.sleep(2.0)
                    
                    current_message = line
                else:
                    # Single line is too long, send it anyway (will be truncated by bot)
                    await self.send_response(message, line)
                    message_count += 1
                    if i < len(lines) - 1:
                        await asyncio.sleep(2.0)
                    current_message = ""
            else:
                # Add line to current message (fits within 130 chars)
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line
        
        # Send the last message if there's content
        if current_message:
            await self.send_response(message, current_message)
    
    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction with emoji"""
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
        """Convert WMO weather code to description"""
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
        """Convert WMO weather code to emoji"""
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
    
    def _check_extreme_conditions(self, weather_text: str) -> str:
        """Check for extreme weather conditions that warrant warnings"""
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