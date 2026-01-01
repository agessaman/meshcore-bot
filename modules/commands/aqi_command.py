#!/usr/bin/env python3
"""
AQI command for the MeshCore Bot
Provides Air Quality Index information using OpenMeteo API
"""

import re
import openmeteo_requests
import requests_cache
from retry_requests import retry
from datetime import datetime
from geopy.geocoders import Nominatim
from ..utils import rate_limited_nominatim_geocode_sync, rate_limited_nominatim_reverse_sync, get_nominatim_geocoder, abbreviate_location, geocode_zipcode_sync, geocode_city_sync
from .base_command import BaseCommand
from ..models import MeshMessage


class AqiCommand(BaseCommand):
    """Handles AQI commands with location support using OpenMeteo API"""
    
    # Plugin metadata
    name = "aqi"
    keywords = ['aqi', 'air', 'airquality', 'air_quality']
    description = "Get Air Quality Index for a location (usage: aqi seattle, aqi greenwood, aqi vancouver canada, aqi 47.6,-122.3, or aqi help)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    requires_internet = True  # Requires internet access for OpenMeteo API and geocoding
    
    # Error constants
    ERROR_FETCHING_DATA = "Error fetching AQI data"
    NO_DATA_AVAILABLE = "No AQI data available"
    
    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10  # seconds
        
        # Get default state from config for city disambiguation
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='WA')
        
        # Get timezone from config
        self.timezone = self.bot.config.get('Bot', 'timezone', fallback='America/Los_Angeles')
        
        # Initialize geocoder (will use rate-limited helpers for actual calls)
        self.geolocator = get_nominatim_geocoder()
        
        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager
        
        # Setup the Open-Meteo API client with cache and retry on error
        cache_session = requests_cache.CachedSession('.cache', expire_after=3600)
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
        return f"Usage: aqi <city|neighborhood|city country|lat,lon|help> - Get AQI for city/neighborhood in {self.default_state}, international cities, coordinates, or pollutant help"
    
    def get_pollutant_help(self) -> str:
        """Get help text explaining pollutant types within 130 characters"""
        # Compact explanation of all pollutants - fits within 130 chars
        return "AQI Help: PM2.5=fine particles, PM10=coarse, O3=ozone, NO2=nitrogen dioxide, CO=carbon monoxide, SO2=sulfur dioxide"
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the AQI command"""
        content = message.content.strip()
        
        # Parse the command to extract location
        # Support formats: "aqi seattle", "aqi paris, tx", "aqi 47.6,-122.3", "air everett", "aqi help"
        parts = content.split()
        if len(parts) < 2:
            await self.send_response(message, f"Usage: aqi <city|neighborhood|city country|lat,lon> - Example: aqi seattle, aqi greenwood, aqi vancouver canada, or aqi 47.6,-122.3")
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
        
        # Check if it's lat,lon coordinates (decimal numbers separated by comma, with optional spaces)
        # Handle formats like: "47.6,-122.3", "47.6, -122.3", "47.980525, -122.150649", " -47.6 , 122.3 "
        if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
            location_type = "coordinates"
        # Check if it's a US ZIP code (5 digits)
        elif re.match(r'^\s*\d{5}\s*$', location):
            location_type = "zipcode"
            # Keep the original ZIP code for structured queries
            # Don't modify the location string here - let the geocoding logic handle it
        else:
            # It's a city name (possibly with state/country)
            # Check if it might be "city country" format (space-separated)
            location_parts = location.split()
            if len(location_parts) >= 2:
                potential_city = location_parts[0]
                potential_country = location_parts[1]
                country_indicators = ['canada', 'mexico', 'uk', 'united', 'kingdom', 'france', 'germany', 'italy', 'spain', 'australia', 'japan', 'china', 'india', 'brazil']
                
                # Check if second word is a country indicator
                if potential_country.lower() in country_indicators:
                    # Convert space-separated to comma-separated format
                    if potential_country.lower() in ['united', 'kingdom']:
                        # Handle "united kingdom" case
                        if len(location_parts) >= 3 and location_parts[2].lower() == 'kingdom':
                            location = f"{potential_city}, uk"
                        else:
                            location = f"{potential_city}, {potential_country}"
                    else:
                        location = f"{potential_city}, {potential_country}"
            else:
                # Single word city - check if it's a well-known international city
                international_cities = {
                    'beijing': 'beijing, china',
                    'shanghai': 'shanghai, china',
                    'tokyo': 'tokyo, japan',
                    'london': 'london, uk',
                    'paris': 'paris, france',
                    'berlin': 'berlin, germany',
                    'rome': 'rome, italy',
                    'madrid': 'madrid, spain',
                    'moscow': 'moscow, russia',
                    'sydney': 'sydney, australia',
                    'melbourne': 'melbourne, australia',
                    'toronto': 'toronto, canada',
                    'vancouver': 'vancouver, canada',
                    'mumbai': 'mumbai, india',
                    'delhi': 'delhi, india',
                    'bangalore': 'bangalore, india',
                    'sao paulo': 'sao paulo, brazil',
                    'rio de janeiro': 'rio de janeiro, brazil',
                    'mexico city': 'mexico city, mexico',
                    'cairo': 'cairo, egypt',
                    'istanbul': 'istanbul, turkey',
                    'seoul': 'seoul, south korea',
                    'bangkok': 'bangkok, thailand',
                    'singapore': 'singapore, singapore',
                    'hong kong': 'hong kong, china',
                    'dubai': 'dubai, uae',
                    'tel aviv': 'tel aviv, israel',
                    'johannesburg': 'johannesburg, south africa',
                    'nairobi': 'nairobi, kenya',
                    'lagos': 'lagos, nigeria',
                    'buenos aires': 'buenos aires, argentina',
                    'lima': 'lima, peru',
                    'santiago': 'santiago, chile',
                    'bogota': 'bogota, colombia',
                    'caracas': 'caracas, venezuela',
                    'havana': 'havana, cuba',
                    'kingston': 'kingston, jamaica',
                    'san juan': 'san juan, puerto rico',
                    'reykjavik': 'reykjavik, iceland',
                    'oslo': 'oslo, norway',
                    'stockholm': 'stockholm, sweden',
                    'copenhagen': 'copenhagen, denmark',
                    'helsinki': 'helsinki, finland',
                    'warsaw': 'warsaw, poland',
                    'prague': 'prague, czech republic',
                    'budapest': 'budapest, hungary',
                    'bucharest': 'bucharest, romania',
                    'sofia': 'sofia, bulgaria',
                    'zagreb': 'zagreb, croatia',
                    'belgrade': 'belgrade, serbia',
                    'athens': 'athens, greece',
                    'lisbon': 'lisbon, portugal',
                    'dublin': 'dublin, ireland',
                    'brussels': 'brussels, belgium',
                    'amsterdam': 'amsterdam, netherlands',
                    'zurich': 'zurich, switzerland',
                    'vienna': 'vienna, austria',
                    'lucerne': 'lucerne, switzerland',
                    'geneva': 'geneva, switzerland',
                    'monaco': 'monaco, monaco',
                    'andorra': 'andorra, andorra',
                    'san marino': 'san marino, san marino',
                    'vatican': 'vatican city, vatican',
                    'luxembourg': 'luxembourg, luxembourg',
                    'malta': 'valletta, malta',
                    'cyprus': 'nicosia, cyprus',
                    'albania': 'tirana, albania',
                    'macedonia': 'skopje, macedonia',
                    'montenegro': 'podgorica, montenegro',
                    'bosnia': 'sarajevo, bosnia',
                    'slovenia': 'ljubljana, slovenia',
                    'slovakia': 'bratislava, slovakia',
                    'lithuania': 'vilnius, lithuania',
                    'latvia': 'riga, latvia',
                    'estonia': 'tallinn, estonia',
                    'belarus': 'minsk, belarus',
                    'ukraine': 'kiev, ukraine',
                    'moldova': 'chisinau, moldova',
                    'georgia': 'tbilisi, georgia',
                    'armenia': 'yerevan, armenia',
                    'azerbaijan': 'baku, azerbaijan',
                    'kazakhstan': 'almaty, kazakhstan',
                    'uzbekistan': 'tashkent, uzbekistan',
                    'kyrgyzstan': 'bishkek, kyrgyzstan',
                    'tajikistan': 'dushanbe, tajikistan',
                    'turkmenistan': 'ashgabat, turkmenistan',
                    'afghanistan': 'kabul, afghanistan',
                    'pakistan': 'islamabad, pakistan',
                    'bangladesh': 'dhaka, bangladesh',
                    'sri lanka': 'colombo, sri lanka',
                    'nepal': 'kathmandu, nepal',
                    'bhutan': 'thimphu, bhutan',
                    'myanmar': 'yangon, myanmar',
                    'laos': 'vientiane, laos',
                    'cambodia': 'phnom penh, cambodia',
                    'vietnam': 'hanoi, vietnam',
                    'malaysia': 'kuala lumpur, malaysia',
                    'indonesia': 'jakarta, indonesia',
                    'philippines': 'manila, philippines',
                    'taiwan': 'taipei, taiwan',
                    'north korea': 'pyongyang, north korea',
                    'mongolia': 'ulaanbaatar, mongolia',
                    'kazakhstan': 'nur-sultan, kazakhstan'
                }
                
                # Check if it's a known international city
                city_lower = location.lower()
                if city_lower in international_cities:
                    location = international_cities[city_lower]
            
            location_type = "city"
        
        try:
            # Record execution for this user
            self.record_execution(message.sender_id)
            
            # Get AQI data for the location
            aqi_data = await self.get_aqi_for_location(location, location_type)
            
            # Send the response
            await self.send_response(message, aqi_data)
            return True
            
        except Exception as e:
            self.logger.error(f"Error in AQI command: {e}")
            await self.send_response(message, f"Error getting AQI data: {e}")
            return True
    
    async def get_aqi_for_location(self, location: str, location_type: str) -> str:
        """Get AQI data for a location (city or coordinates)"""
        try:
            # Define state abbreviation map for US states (needed for all location types)
            state_abbrev_map = {
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
            # Convert location to lat/lon
            if location_type == "coordinates":
                # Parse lat,lon coordinates
                try:
                    lat_str, lon_str = location.split(',')
                    lat = float(lat_str.strip())
                    lon = float(lon_str.strip())
                    
                    # Validate coordinate ranges
                    if not (-90 <= lat <= 90):
                        return f"Invalid latitude: {lat}. Must be between -90 and 90."
                    if not (-180 <= lon <= 180):
                        return f"Invalid longitude: {lon}. Must be between -180 and 180."
                    
                    address_info = None
                except ValueError:
                    return f"Invalid coordinates format: {location}. Use format: lat,lon (e.g., 47.6,-122.3)"
            elif location_type == "zipcode":
                # Handle ZIP code geocoding with AQI-specific structured queries
                try:
                    zip_code = location.strip()
                    
                    # Check for known problematic ZIP codes that need specific mapping
                    zip_code_mappings = {
                        '98013': 'Vashon, WA, USA',
                        '98014': 'Vashon Island, WA, USA',
                        # Add other problematic ZIP codes here as needed
                    }
                    
                    location_result = None
                    
                    # First check if we have a specific mapping for this ZIP code
                    if zip_code in zip_code_mappings:
                        mapped_location = zip_code_mappings[zip_code]
                        self.logger.debug(f"Using specific mapping for ZIP {zip_code}: {mapped_location}")
                        try:
                            result = rate_limited_nominatim_geocode_sync(self.bot, mapped_location, timeout=10)
                            if result and result.address:
                                location_result = result
                                self.logger.debug(f"Found mapped location for ZIP {zip_code}: {result.address}")
                        except Exception as e:
                            self.logger.debug(f"Mapping failed for ZIP {zip_code}: {e}")
                    
                    # If no mapping, try structured queries (AQI-specific feature for better ZIP handling)
                    if not location_result:
                        structured_queries = [
                            # Direct postalcode search
                            {"postalcode": zip_code, "country": "US"},
                            # Postalcode with state
                            {"postalcode": zip_code, "state": self.default_state, "country": "US"},
                            # Postalcode with country code
                            {"postalcode": zip_code, "countrycode": "US"},
                        ]
                        
                        for query in structured_queries:
                            try:
                                result = rate_limited_nominatim_geocode_sync(self.bot, query, timeout=10)
                                if result and result.address:
                                    # Check if it's a US location
                                    if 'united states' in result.address.lower() or 'usa' in result.address.lower():
                                        # Additional validation: check if it's in the expected state
                                        if self.default_state in result.address or 'washington' in result.address.lower():
                                            location_result = result
                                            self.logger.debug(f"Found US location in {self.default_state} for ZIP {zip_code}: {result.address}")
                                            break
                                        else:
                                            # Found US location but not in expected state - log warning but continue searching
                                            self.logger.warning(f"ZIP {zip_code} found in wrong state: {result.address}")
                                            if not location_result:  # Keep as fallback if no better result found
                                                location_result = result
                            except Exception as e:
                                self.logger.debug(f"Structured query failed for {query}: {e}")
                                continue
                    
                    # If structured queries didn't work, use shared function as fallback
                    if not location_result:
                        lat, lon = geocode_zipcode_sync(self.bot, zip_code, timeout=10)
                        if lat and lon:
                            # Use the shared function result directly
                            pass
                        else:
                            lat, lon = None, None
                    else:
                        lat = location_result.latitude
                        lon = location_result.longitude
                    
                    if lat and lon:
                        
                        # Get detailed address info via reverse geocoding (check cache first)
                        reverse_cache_key = f"reverse_{lat}_{lon}"
                        cached_address = self.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                        if cached_address:
                            address_info = cached_address
                        else:
                            try:
                                reverse_location = rate_limited_nominatim_reverse_sync(self.bot, f"{lat}, {lon}", timeout=10)
                                if reverse_location:
                                    address_info = reverse_location.raw.get('address', {})
                                    # Cache the reverse geocoding result
                                    self.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                                else:
                                    address_info = {}
                            except:
                                address_info = {}
                        
                        # Validate that the found location makes sense for the ZIP code
                        if address_info:
                            found_state = address_info.get('state', '')
                            found_country = address_info.get('country', '')
                            
                            # If we found a location but it's not in the expected state, warn the user
                            if found_country == 'United States' and found_state != self.default_state:
                                self.logger.warning(f"ZIP code {zip_code} found in {found_state} instead of {self.default_state}")
                    else:
                        lat, lon = None, None
                        address_info = None
                    
                    if lat is None or lon is None:
                        return f"Could not find ZIP code '{zip_code}'"
                except Exception as e:
                    self.logger.error(f"Error geocoding ZIP code {location}: {e}")
                    return f"Error geocoding ZIP code: {e}"
            else:  # city
                
                result = self.city_to_lat_lon(location)
                if len(result) == 3:
                    lat, lon, address_info = result
                else:
                    lat, lon = result
                    address_info = None
                
                if lat is None or lon is None:
                    # Check if it's an international city to provide better error message
                    if ',' in location and any(country in location.lower() for country in ['canada', 'mexico', 'uk', 'france', 'germany', 'italy', 'spain', 'australia', 'japan', 'china', 'india', 'brazil', 'uae', 'russia', 'korea', 'thailand', 'singapore', 'egypt', 'turkey']):
                        return f"Could not find city '{location}'"
                    else:
                        return f"Could not find city '{location}' in {self.default_state}"
                
                # Check if the found city is in a different state than default
                actual_city = location
                actual_state = self.default_state
                
                if address_info:
                    # Try to get the best city name from various address fields
                    actual_city = (address_info.get('city') or 
                                 address_info.get('town') or 
                                 address_info.get('village') or 
                                 address_info.get('hamlet') or 
                                 address_info.get('municipality') or 
                                 location)
                    
                    # Get state/province/country info - handle US vs international addresses
                    country = address_info.get('country', '')
                    state = address_info.get('state', '')
                    
                    # For US cities, use the state; for international cities, use the country
                    if country == "United States" or country == "US" or country == "United States of America":
                        # US city - use the state
                        actual_state = state or self.default_state
                        # Convert full state name to abbreviation if needed
                        if len(actual_state) > 2 and actual_state in state_abbrev_map:
                            actual_state = state_abbrev_map.get(actual_state, actual_state)
                    else:
                        # International city - use the country
                        actual_state = (country or 
                                      address_info.get('province') or 
                                      self.default_state)
                        # Normalize "United States" variants to "USA" to save characters
                        if actual_state == "United States" or actual_state == "United States of America":
                            actual_state = "USA"
                    
                    # Also check if the default state needs to be converted for comparison
                    default_state_full = self.default_state
                    if len(self.default_state) == 2:
                        # Convert abbreviation to full name for comparison
                        abbrev_to_full_map = {v: k for k, v in state_abbrev_map.items()}
                        default_state_full = abbrev_to_full_map.get(self.default_state, self.default_state)
            
            # Get AQI data from OpenMeteo
            aqi_data = self.get_openmeteo_aqi(lat, lon)
            
            if aqi_data == self.ERROR_FETCHING_DATA:
                return "Error fetching AQI data from OpenMeteo"
            
            # Add location info for better user confirmation
            location_prefix = ""
            if location_type == "city" and address_info:
                # Always try to include city name if there's space
                # Use abbreviate_location to shorten long location strings (e.g., "United States of America" -> "USA")
                full_location = f"{actual_city}, {actual_state}"
                city_display = abbreviate_location(full_location, max_length=30)
                
                # Check if we have space for the city name
                test_output = f"{city_display}: {aqi_data}"
                if len(test_output) <= 130:
                    location_prefix = f"{city_display}: "
                else:
                    # If no space, only show if it's a different state than default
                    states_different = (actual_state != self.default_state and 
                                      actual_state != default_state_full)
                    if states_different:
                        # Use abbreviated version for shorter display
                        city_display_short = abbreviate_location(full_location, max_length=20)
                        location_prefix = f"{city_display_short}: "
            elif location_type == "zipcode":
                # Add location info for ZIP codes to confirm geocoding accuracy
                if address_info:
                    # Try to get city from address_info first
                    actual_city = (address_info.get('city') or 
                                 address_info.get('town') or 
                                 address_info.get('village') or 
                                 address_info.get('hamlet') or 
                                 address_info.get('municipality'))
                    
                    # If no city found in address_info, try to extract from the original geocoding result
                    if not actual_city and location_result and location_result.address:
                        # Extract city name from the geocoding result address
                        address_parts = location_result.address.split(',')
                        if len(address_parts) > 0:
                            # The first part usually contains the city name
                            potential_city = address_parts[0].strip()
                            # Remove any house numbers or road names
                            if not any(char.isdigit() for char in potential_city):
                                actual_city = potential_city
                    
                    # Fallback to 'Unknown' if still no city found
                    if not actual_city:
                        actual_city = 'Unknown'
                    
                    # Get state info
                    country = address_info.get('country', '')
                    state = address_info.get('state', '')
                    
                    if country == "United States" or country == "US" or country == "United States of America":
                        # US city - use the state
                        actual_state = state or self.default_state
                        # Convert full state name to abbreviation if needed
                        if len(actual_state) > 2 and actual_state in state_abbrev_map:
                            actual_state = state_abbrev_map.get(actual_state, actual_state)
                    else:
                        # International city - use the country
                        actual_state = (country or 
                                      address_info.get('province') or 
                                      self.default_state)
                        # Normalize "United States" variants to "USA" to save characters
                        if actual_state == "United States" or actual_state == "United States of America":
                            actual_state = "USA"
                    
                    # Use abbreviate_location to shorten long location strings (e.g., "United States of America" -> "USA")
                    full_location = f"{actual_city}, {actual_state}"
                    city_display = abbreviate_location(full_location, max_length=30)
                    
                    # Check if we have space for the city name
                    test_output = f"{city_display}: {aqi_data}"
                    if len(test_output) <= 130:
                        location_prefix = f"{city_display}: "
                    else:
                        # If no space, only show if it's a different state than default
                        states_different = (actual_state != self.default_state and 
                                          actual_state != default_state_full)
                        if states_different:
                            # Use abbreviated version for shorter display
                            city_display_short = abbreviate_location(full_location, max_length=20)
                            location_prefix = f"{city_display_short}: "
                else:
                    # No address info available
                    location_prefix = f"{zip_code}: "
            elif location_type == "coordinates":
                # Add coordinate info for clarity
                location_prefix = f"{lat:.3f},{lon:.3f}: "
            
            return f"{location_prefix}{aqi_data}"
            
        except Exception as e:
            self.logger.error(f"Error getting AQI for {location_type} {location}: {e}")
            return f"Error getting AQI data: {e}"
    
    def city_to_lat_lon(self, city: str) -> tuple:
        """Convert city name to latitude and longitude using default state"""
        try:
            # Check if the input contains a comma (city, state/country format)
            if ',' in city:
                # Parse city, state/country format
                city_parts = [part.strip() for part in city.split(',')]
                if len(city_parts) >= 2:
                    city_name = city_parts[0]
                    state_or_country = city_parts[1]
                    
                    # AQI-specific: Check if it's a country (not a US state)
                    country_indicators = ['canada', 'mexico', 'uk', 'united kingdom', 'france', 'germany', 'italy', 'spain', 'australia', 'japan', 'china', 'india', 'brazil', 'uae', 'russia', 'korea', 'thailand', 'singapore', 'egypt', 'turkey', 'israel', 'south africa', 'kenya', 'nigeria', 'argentina', 'peru', 'chile', 'colombia', 'venezuela', 'cuba', 'jamaica', 'puerto rico', 'iceland', 'norway', 'sweden', 'denmark', 'finland', 'poland', 'czech republic', 'hungary', 'romania', 'bulgaria', 'croatia', 'serbia', 'greece', 'portugal', 'ireland', 'belgium', 'netherlands', 'switzerland', 'austria', 'monaco', 'andorra', 'san marino', 'vatican', 'luxembourg', 'malta', 'cyprus', 'albania', 'macedonia', 'montenegro', 'bosnia', 'slovenia', 'slovakia', 'lithuania', 'latvia', 'estonia', 'belarus', 'ukraine', 'moldova', 'georgia', 'armenia', 'azerbaijan', 'kazakhstan', 'uzbekistan', 'kyrgyzstan', 'tajikistan', 'turkmenistan', 'afghanistan', 'pakistan', 'bangladesh', 'sri lanka', 'nepal', 'bhutan', 'myanmar', 'laos', 'cambodia', 'vietnam', 'malaysia', 'indonesia', 'philippines', 'taiwan', 'north korea', 'mongolia']
                    is_country = state_or_country.lower() in country_indicators
                    
                    if is_country:
                        # Handle international cities explicitly (AQI-specific feature)
                        location = rate_limited_nominatim_geocode_sync(self.bot, f"{city_name}, {state_or_country}", timeout=10)
                        if location:
                            # Cache the result
                            self.db_manager.cache_geocoding(f"{city_name}, {state_or_country}", location.latitude, location.longitude)
                            
                            # Use reverse geocoding to get detailed address info (check cache first)
                            reverse_cache_key = f"reverse_{location.latitude}_{location.longitude}"
                            cached_address = self.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                            if cached_address:
                                return location.latitude, location.longitude, cached_address
                            else:
                                try:
                                    reverse_location = rate_limited_nominatim_reverse_sync(self.bot, f"{location.latitude}, {location.longitude}", timeout=10)
                                    if reverse_location:
                                        address_info = reverse_location.raw.get('address', {})
                                        # Cache the reverse geocoding result
                                        self.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                                        return location.latitude, location.longitude, address_info
                                except:
                                    pass
                            return location.latitude, location.longitude, location.raw.get('address', {})
            
            # Use shared geocode_city_sync function with address info
            default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            lat, lon, address_info = geocode_city_sync(
                self.bot, city, default_state=self.default_state,
                default_country=default_country,
                include_address_info=True, timeout=10
            )
            
            if lat and lon:
                return lat, lon, address_info or {}
            
            # AQI-specific fallback: Try neighborhood-specific queries for major cities
            neighborhood_queries = self.get_neighborhood_queries(city)
            if neighborhood_queries:
                for query in neighborhood_queries:
                    location = rate_limited_nominatim_geocode_sync(self.bot, query, timeout=10)
                    if location:
                        # Cache the result
                        self.db_manager.cache_geocoding(query, location.latitude, location.longitude)
                        
                        # Use reverse geocoding to get detailed address info (check cache first)
                        reverse_cache_key = f"reverse_{location.latitude}_{location.longitude}"
                        cached_address = self.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                        if cached_address:
                            return location.latitude, location.longitude, cached_address
                        else:
                            try:
                                reverse_location = rate_limited_nominatim_reverse_sync(self.bot, f"{location.latitude}, {location.longitude}", timeout=10)
                                if reverse_location:
                                    address_info = reverse_location.raw.get('address', {})
                                    # Cache the reverse geocoding result
                                    self.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                                    return location.latitude, location.longitude, address_info
                            except:
                                pass
                            return location.latitude, location.longitude, location.raw.get('address', {})
            
            return (None, None, None)
        except Exception as e:
            self.logger.error(f"Error geocoding city {city}: {e}")
            return (None, None, None)
    
    def get_neighborhood_queries(self, city: str) -> list:
        """Generate neighborhood-specific search queries for major cities"""
        city_lower = city.lower()
        
        # Seattle neighborhoods
        if city_lower in ['greenwood', 'ballard', 'capitol hill', 'fremont', 'queen anne', 
                         'wallingford', 'university district', 'pike place', 'pioneer square',
                         'belltown', 'first hill', 'central district', 'beacon hill', 'columbia city',
                         'west seattle', 'magnolia', 'phinney ridge', 'crown hill', 'loyal heights']:
            return [
                f"{city}, Seattle, WA, USA",
                f"{city}, Seattle, USA"
            ]
        
        # New York neighborhoods
        elif city_lower in ['greenwich village', 'soho', 'tribeca', 'chinatown', 'little italy',
                           'east village', 'west village', 'chelsea', 'hells kitchen', 'upper east side',
                           'upper west side', 'harlem', 'brooklyn heights', 'dumbo', 'williamsburg',
                           'park slope', 'red hook', 'coney island']:
            return [
                f"{city}, New York, NY, USA",
                f"{city}, New York, USA"
            ]
        
        # San Francisco neighborhoods
        elif city_lower in ['mission district', 'haight-ashbury', 'castro', 'soma', 'financial district',
                           'north beach', 'chinatown', 'russian hill', 'pacific heights', 'marina district',
                           'sunset district', 'richmond district', 'bernal heights', 'noe valley']:
            return [
                f"{city}, San Francisco, CA, USA",
                f"{city}, San Francisco, USA"
            ]
        
        # Los Angeles neighborhoods
        elif city_lower in ['hollywood', 'beverly hills', 'santa monica', 'venice', 'manhattan beach',
                           'hermosa beach', 'redondo beach', 'pasadena', 'glendale', 'burbank',
                           'west hollywood', 'culver city', 'marina del rey', 'playa del rey']:
            return [
                f"{city}, Los Angeles, CA, USA",
                f"{city}, Los Angeles, USA"
            ]
        
        # Chicago neighborhoods
        elif city_lower in ['loop', 'magnificent mile', 'gold coast', 'lincoln park', 'wrigleyville',
                           'lakeview', 'wicker park', 'bucktown', 'logan square', 'pilsen', 'hyde park']:
            return [
                f"{city}, Chicago, IL, USA",
                f"{city}, Chicago, USA"
            ]
        
        # Boston neighborhoods
        elif city_lower in ['back bay', 'beacon hill', 'north end', 'south end', 'charlestown',
                           'east boston', 'dorchester', 'roxbury', 'jamaica plain', 'allston',
                           'brighton', 'cambridge', 'somerville']:
            return [
                f"{city}, Boston, MA, USA",
                f"{city}, Boston, USA"
            ]
        
        # Portland neighborhoods
        elif city_lower in ['pearl district', 'alphabet district', 'nob hill', 'mississippi district',
                           'hawthorne', 'belmont', 'sellwood', 'st. johns', 'kenton', 'overlook']:
            return [
                f"{city}, Portland, OR, USA",
                f"{city}, Portland, USA"
            ]
        
        # No neighborhood-specific queries for this city
        return []
    
    def get_openmeteo_aqi(self, lat: float, lon: float) -> str:
        """Get AQI data from OpenMeteo API"""
        try:
            # Make sure all required weather variables are listed here
            # The order of variables in current is important to assign them correctly below
            url = "https://air-quality-api.open-meteo.com/v1/air-quality"
            params = {
                "latitude": lat,
                "longitude": lon,
                "current": ["us_aqi", "european_aqi", "pm10", "pm2_5", "carbon_monoxide", "nitrogen_dioxide", "sulphur_dioxide", "ozone", "dust"],
                "timezone": self.timezone,
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
        """Format AQI data for display within 130 characters"""
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
            if pollutants:
                result = f"{aqi_str} {' '.join(pollutants)}"
            else:
                result = aqi_str
            
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
        """Get emoji for US AQI value"""
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
        """Get emoji for European AQI value"""
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
        """Get category name for US AQI value"""
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