#!/usr/bin/env python3
"""
Utility functions for the MeshCore Bot
Shared helper functions used across multiple modules
"""

import re
import hashlib
import socket
import asyncio
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Tuple, Dict, Union, List, Any


def abbreviate_location(location: str, max_length: int = 20) -> str:
    """Abbreviate a location string to fit within character limits.
    
    Args:
        location: The location string to abbreviate.
        max_length: Maximum length for the abbreviated string (default: 20).
        
    Returns:
        str: Abbreviated location string.
    """
    if not location:
        return location
    
    # Apply common abbreviations first
    abbreviated = location
    
    abbreviations = [
        ('Central Business District', 'CBD'),
        ('United States of America', 'USA'),
        ('Business District', 'BD'),
        ('British Columbia', 'BC'),
        ('United States', 'USA'),
        ('United Kingdom', 'UK'),
        ('Washington', 'WA'),
        ('California', 'CA'),
        ('New York', 'NY'),
        ('Texas', 'TX'),
        ('Florida', 'FL'),
        ('Illinois', 'IL'),
        ('Pennsylvania', 'PA'),
        ('Ohio', 'OH'),
        ('Georgia', 'GA'),
        ('North Carolina', 'NC'),
        ('Michigan', 'MI'),
        ('New Jersey', 'NJ'),
        ('Virginia', 'VA'),
        ('Tennessee', 'TN'),
        ('Indiana', 'IN'),
        ('Arizona', 'AZ'),
        ('Massachusetts', 'MA'),
        ('Missouri', 'MO'),
        ('Maryland', 'MD'),
        ('Wisconsin', 'WI'),
        ('Colorado', 'CO'),
        ('Minnesota', 'MN'),
        ('South Carolina', 'SC'),
        ('Alabama', 'AL'),
        ('Louisiana', 'LA'),
        ('Kentucky', 'KY'),
        ('Oregon', 'OR'),
        ('Oklahoma', 'OK'),
        ('Connecticut', 'CT'),
        ('Utah', 'UT'),
        ('Iowa', 'IA'),
        ('Nevada', 'NV'),
        ('Arkansas', 'AR'),
        ('Mississippi', 'MS'),
        ('Kansas', 'KS'),
        ('New Mexico', 'NM'),
        ('Nebraska', 'NE'),
        ('West Virginia', 'WV'),
        ('Idaho', 'ID'),
        ('Hawaii', 'HI'),
        ('New Hampshire', 'NH'),
        ('Maine', 'ME'),
        ('Montana', 'MT'),
        ('Rhode Island', 'RI'),
        ('Delaware', 'DE'),
        ('South Dakota', 'SD'),
        ('North Dakota', 'ND'),
        ('Alaska', 'AK'),
        ('Vermont', 'VT'),
        ('Wyoming', 'WY')
    ]
    
    # Sort by length (longest first) to ensure longer matches are checked before shorter ones
    # This prevents "United States" from matching before "United States of America"
    abbreviations.sort(key=lambda x: len(x[0]), reverse=True)
    
    # Apply abbreviations in order
    for full_term, abbrev in abbreviations:
        if full_term in abbreviated:
            abbreviated = abbreviated.replace(full_term, abbrev)
    
    # If still too long after abbreviations, try to truncate intelligently
    if len(abbreviated) > max_length:
        # Try to keep the most important part (usually the city name)
        parts = abbreviated.split(', ')
        if len(parts) > 1:
            # Keep the first part (usually city) and truncate if needed
            first_part = parts[0]
            if len(first_part) <= max_length:
                abbreviated = first_part
            else:
                abbreviated = first_part[:max_length-3] + '...'
        else:
            # Just truncate with ellipsis
            abbreviated = abbreviated[:max_length-3] + '...'
    
    return abbreviated


def truncate_string(text: str, max_length: int, ellipsis: str = '...') -> str:
    """Truncate a string to a maximum length with ellipsis.
    
    Args:
        text: The string to truncate.
        max_length: Maximum length including ellipsis.
        ellipsis: String to append when truncating (default: '...').
        
    Returns:
        str: Truncated string.
    """
    if not text or len(text) <= max_length:
        return text
    
    return text[:max_length - len(ellipsis)] + ellipsis


def format_location_for_display(city: Optional[str], state: Optional[str] = None, 
                               country: Optional[str] = None, max_length: int = 20) -> Optional[str]:
    """Format location data for display with intelligent abbreviation.
    
    Args:
        city: City name (may include neighborhood/district).
        state: State/province name (optional).
        country: Country name (optional).
        max_length: Maximum length for the formatted location (default: 20).
        
    Returns:
        Optional[str]: Formatted location string or None if no city provided.
    """
    if not city:
        return None
    
    # Start with city (which may include neighborhood)
    location_parts = [city]
    
    # Add state if available and different from city
    if state and state not in location_parts:
        location_parts.append(state)
    
    # Join parts and abbreviate if needed
    full_location = ', '.join(location_parts)
    return abbreviate_location(full_location, max_length)


def get_major_city_queries(city: str, state_abbr: Optional[str] = None) -> List[str]:
    """Get prioritized geocoding queries for major cities that have multiple locations.
    
    This helps ensure that common city names resolve to the most likely major city
    rather than a small town with the same name.
    
    Args:
        city: City name (normalized, lowercase).
        state_abbr: Optional state abbreviation (e.g., "CA", "NY").
        
    Returns:
        List[str]: List of geocoding query strings in priority order.
    """
    city_lower = city.lower().strip()
    
    # Comprehensive mapping of major cities with multiple locations
    # Format: 'city_name': [list of queries in priority order]
    major_city_mappings = {
        'new york': ['New York, NY, USA', 'New York City, NY, USA'],
        'los angeles': ['Los Angeles, CA, USA'],
        'chicago': ['Chicago, IL, USA'],
        'houston': ['Houston, TX, USA'],
        'phoenix': ['Phoenix, AZ, USA'],
        'philadelphia': ['Philadelphia, PA, USA'],
        'san antonio': ['San Antonio, TX, USA'],
        'san diego': ['San Diego, CA, USA'],
        'dallas': ['Dallas, TX, USA'],
        'san jose': ['San Jose, CA, USA'],
        'austin': ['Austin, TX, USA'],
        'jacksonville': ['Jacksonville, FL, USA'],
        'san francisco': ['San Francisco, CA, USA'],
        'columbus': ['Columbus, OH, USA'],
        'fort worth': ['Fort Worth, TX, USA'],
        'charlotte': ['Charlotte, NC, USA'],
        'seattle': ['Seattle, WA, USA'],
        'denver': ['Denver, CO, USA'],
        'washington': ['Washington, DC, USA'],
        'boston': ['Boston, MA, USA'],
        'el paso': ['El Paso, TX, USA'],
        'detroit': ['Detroit, MI, USA'],
        'nashville': ['Nashville, TN, USA'],
        'portland': ['Portland, OR, USA', 'Portland, ME, USA'],
        'oklahoma city': ['Oklahoma City, OK, USA'],
        'las vegas': ['Las Vegas, NV, USA'],
        'memphis': ['Memphis, TN, USA'],
        'louisville': ['Louisville, KY, USA'],
        'baltimore': ['Baltimore, MD, USA'],
        'milwaukee': ['Milwaukee, WI, USA'],
        'albuquerque': ['Albuquerque, NM, USA'],
        'tucson': ['Tucson, AZ, USA'],
        'fresno': ['Fresno, CA, USA'],
        'sacramento': ['Sacramento, CA, USA'],
        'kansas city': ['Kansas City, MO, USA', 'Kansas City, KS, USA'],
        'mesa': ['Mesa, AZ, USA'],
        'atlanta': ['Atlanta, GA, USA'],
        'omaha': ['Omaha, NE, USA'],
        'colorado springs': ['Colorado Springs, CO, USA'],
        'raleigh': ['Raleigh, NC, USA'],
        'virginia beach': ['Virginia Beach, VA, USA'],
        'miami': ['Miami, FL, USA'],
        'oakland': ['Oakland, CA, USA'],
        'minneapolis': ['Minneapolis, MN, USA'],
        'tulsa': ['Tulsa, OK, USA'],
        'cleveland': ['Cleveland, OH, USA'],
        'wichita': ['Wichita, KS, USA'],
        'arlington': ['Arlington, TX, USA', 'Arlington, VA, USA'],
        'new orleans': ['New Orleans, LA, USA'],
        'honolulu': ['Honolulu, HI, USA'],
        # Cities with multiple locations that need disambiguation
        'albany': ['Albany, NY, USA', 'Albany, OR, USA', 'Albany, CA, USA'],
        'springfield': ['Springfield, IL, USA', 'Springfield, MO, USA', 'Springfield, MA, USA'],
        'franklin': ['Franklin, TN, USA', 'Franklin, MA, USA'],
        'georgetown': ['Georgetown, TX, USA', 'Georgetown, SC, USA'],
        'madison': ['Madison, WI, USA', 'Madison, AL, USA'],
        'auburn': ['Auburn, AL, USA', 'Auburn, WA, USA'],
        'troy': ['Troy, NY, USA', 'Troy, MI, USA'],
        'clinton': ['Clinton, IA, USA', 'Clinton, MS, USA'],
        'paris': ['Paris, TX, USA', 'Paris, IL, USA', 'Paris, TN, USA'],
    }
    
    # Check if this is a major city
    if city_lower in major_city_mappings:
        queries = major_city_mappings[city_lower].copy()
        
        # If state abbreviation was provided, prioritize queries with that state
        if state_abbr:
            state_upper = state_abbr.upper()
            # Move matching state queries to the front
            matching = [q for q in queries if f', {state_upper},' in q or q.endswith(f', {state_upper}')]
            non_matching = [q for q in queries if q not in matching]
            if matching:
                return matching + non_matching
        
        return queries
    
    # Not a major city - return empty list (caller should use standard geocoding)
    return []


def calculate_packet_hash(raw_hex: str, payload_type: int = None) -> str:
    """Calculate hash for packet identification - based on packet.cpp.
    
    Packet hashes are unique to the originally sent message, allowing
    identification of the same message arriving via different paths.
    
    Args:
        raw_hex: Raw packet data as hex string.
        payload_type: Optional payload type as integer (if None, extracted from header).
                      Must be numeric value (0-15).
        
    Returns:
        str: 16-character hex string (8 bytes) in uppercase, or "0000000000000000" on error.
    """
    try:
        # Parse the packet to extract payload type and payload data
        byte_data = bytes.fromhex(raw_hex)
        header = byte_data[0]
        
        # Get payload type from header (bits 2-5)
        if payload_type is None:
            payload_type = (header >> 2) & 0x0F
        else:
            # Ensure payload_type is an integer (handle enum.value if passed)
            if hasattr(payload_type, 'value'):
                payload_type = payload_type.value
            payload_type = int(payload_type) & 0x0F  # Ensure it's 0-15
        
        # Check if transport codes are present
        route_type = header & 0x03
        has_transport = route_type in [0x00, 0x03]  # TRANSPORT_FLOOD or TRANSPORT_DIRECT
        
        # Calculate path length offset dynamically based on transport codes
        offset = 1  # After header
        if has_transport:
            offset += 4  # Skip 4 bytes of transport codes
        
        # Validate we have enough bytes for path_len
        if len(byte_data) <= offset:
            return "0000000000000000"
        
        # Read path_len (1 byte on wire, but stored as uint16_t in C++)
        path_len = byte_data[offset]
        offset += 1
        
        # Validate we have enough bytes for the path
        if len(byte_data) < offset + path_len:
            return "0000000000000000"
        
        # Skip past the path to get to payload
        payload_start = offset + path_len
        
        # Validate we have payload data
        if len(byte_data) <= payload_start:
            return "0000000000000000"
        
        payload_data = byte_data[payload_start:]
        
        # Calculate hash exactly like MeshCore Packet::calculatePacketHash():
        # 1. Payload type (1 byte)
        # 2. Path length (2 bytes as uint16_t, little-endian) - ONLY for TRACE packets (type 9)
        # 3. Payload data
        hash_obj = hashlib.sha256()
        hash_obj.update(bytes([payload_type]))
        
        if payload_type == 9:  # PAYLOAD_TYPE_TRACE
            # C++ does: sha.update(&path_len, sizeof(path_len))
            # path_len is uint16_t, so sizeof(path_len) = 2 bytes
            # Convert path_len to 2-byte little-endian uint16_t
            hash_obj.update(path_len.to_bytes(2, byteorder='little'))
        
        hash_obj.update(payload_data)
        
        # Return first 16 hex characters (8 bytes) in uppercase
        return hash_obj.hexdigest()[:16].upper()
    except Exception as e:
        # Return default hash on error (caller should handle logging)
        return "0000000000000000"


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate haversine distance between two points in kilometers.
    
    Args:
        lat1: Latitude of first point in degrees.
        lon1: Longitude of first point in degrees.
        lat2: Latitude of second point in degrees.
        lon2: Longitude of second point in degrees.
        
    Returns:
        float: Distance in kilometers.
    """
    import math
    
    # Convert latitude and longitude from degrees to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Haversine formula
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    # Earth's radius in kilometers
    earth_radius = 6371.0
    return earth_radius * c


def get_nominatim_geocoder(user_agent: str = "meshcore-bot", timeout: int = 10) -> Any:
    """Get a Nominatim geocoder instance with proper User-Agent.
    
    Args:
        user_agent: User-Agent string for Nominatim (required by their policy).
        timeout: Request timeout in seconds.
        
    Returns:
        Any: Nominatim geocoder instance (from geopy).
    """
    from geopy.geocoders import Nominatim
    return Nominatim(user_agent=user_agent, timeout=timeout)


async def rate_limited_nominatim_geocode(bot: Any, query: str, timeout: int = 10) -> Optional[Any]:
    """Perform rate-limited Nominatim geocoding (forward geocoding).
    
    Args:
        bot: Bot instance (must have nominatim_rate_limiter attribute).
        query: Location query string.
        timeout: Request timeout in seconds.
        
    Returns:
        Optional[Any]: Geocoding result or None if failed/timed out.
    """
    if not hasattr(bot, 'nominatim_rate_limiter'):
        # Fallback if rate limiter not initialized
        geolocator = get_nominatim_geocoder(timeout=timeout)
        return geolocator.geocode(query, timeout=timeout)
    
    # Wait for rate limiter
    await bot.nominatim_rate_limiter.wait_for_request()
    
    # Make the request
    geolocator = get_nominatim_geocoder(timeout=timeout)
    result = geolocator.geocode(query, timeout=timeout)
    
    # Record the request
    bot.nominatim_rate_limiter.record_request()
    
    return result


async def rate_limited_nominatim_reverse(bot: Any, coordinates: str, timeout: int = 10) -> Optional[Any]:
    """Perform rate-limited Nominatim reverse geocoding.
    
    Args:
        bot: Bot instance (must have nominatim_rate_limiter attribute).
        coordinates: Coordinates string in format "lat, lon".
        timeout: Request timeout in seconds.
        
    Returns:
        Optional[Any]: Reverse geocoding result or None if failed/timed out.
    """
    if not hasattr(bot, 'nominatim_rate_limiter'):
        # Fallback if rate limiter not initialized
        geolocator = get_nominatim_geocoder(timeout=timeout)
        return geolocator.reverse(coordinates, timeout=timeout)
    
    # Wait for rate limiter
    await bot.nominatim_rate_limiter.wait_for_request()
    
    # Make the request
    geolocator = get_nominatim_geocoder(timeout=timeout)
    result = geolocator.reverse(coordinates, timeout=timeout)
    
    # Record the request
    bot.nominatim_rate_limiter.record_request()
    
    return result


def rate_limited_nominatim_geocode_sync(bot: Any, query: str, timeout: int = 10) -> Optional[Any]:
    """Perform rate-limited Nominatim geocoding (synchronous version).
    
    Args:
        bot: Bot instance (must have nominatim_rate_limiter attribute).
        query: Location query string.
        timeout: Request timeout in seconds.
        
    Returns:
        Optional[Any]: Geocoding result or None if failed/timed out.
    """
    if not hasattr(bot, 'nominatim_rate_limiter'):
        # Fallback if rate limiter not initialized
        geolocator = get_nominatim_geocoder(timeout=timeout)
        return geolocator.geocode(query, timeout=timeout)
    
    # Wait for rate limiter
    bot.nominatim_rate_limiter.wait_for_request_sync()
    
    # Make the request
    geolocator = get_nominatim_geocoder(timeout=timeout)
    result = geolocator.geocode(query, timeout=timeout)
    
    # Record the request
    bot.nominatim_rate_limiter.record_request()
    
    return result


def rate_limited_nominatim_reverse_sync(bot: Any, coordinates: str, timeout: int = 10) -> Optional[Any]:
    """Perform rate-limited Nominatim reverse geocoding (synchronous version).
    
    Args:
        bot: Bot instance (must have nominatim_rate_limiter attribute).
        coordinates: Coordinates string in format "lat, lon".
        timeout: Request timeout in seconds.
        
    Returns:
        Optional[Any]: Reverse geocoding result or None if failed/timed out.
    """
    if not hasattr(bot, 'nominatim_rate_limiter'):
        # Fallback if rate limiter not initialized
        geolocator = get_nominatim_geocoder(timeout=timeout)
        return geolocator.reverse(coordinates, timeout=timeout)
    
    # Wait for rate limiter
    bot.nominatim_rate_limiter.wait_for_request_sync()
    
    # Make the request
    geolocator = get_nominatim_geocoder(timeout=timeout)
    result = geolocator.reverse(coordinates, timeout=timeout)
    
    # Record the request
    bot.nominatim_rate_limiter.record_request()
    
    return result


async def geocode_zipcode(bot: Any, zipcode: str, default_country: str = None, timeout: int = 10) -> Tuple[Optional[float], Optional[float]]:
    """Shared function to geocode a ZIP code to lat/lon coordinates.
    
    Checks cache first, then makes rate-limited API call if needed.
    
    Args:
        bot: Bot instance (must have db_manager and nominatim_rate_limiter).
        zipcode: ZIP code string.
        default_country: Default country code (e.g., "US"). If None, reads from bot.config.
        timeout: Request timeout in seconds.
        
    Returns:
        Tuple[Optional[float], Optional[float]]: Tuple of (latitude, longitude) or (None, None) if not found.
    """
    try:
        # Get default country from config if not provided
        if default_country is None:
            default_country = bot.config.get('Weather', 'default_country', fallback='US')
        
        # Check cache first
        cache_query = f"{zipcode}, {default_country}"
        cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(cache_query)
        if cached_lat is not None and cached_lon is not None:
            return cached_lat, cached_lon
        
        # Use rate-limited Nominatim to geocode the zipcode
        location = await rate_limited_nominatim_geocode(bot, cache_query, timeout=timeout)
        if location:
            # Cache the result for future use
            bot.db_manager.cache_geocoding(cache_query, location.latitude, location.longitude)
            return location.latitude, location.longitude
        else:
            return None, None
    except Exception as e:
        bot.logger.error(f"Error geocoding zipcode {zipcode}: {e}")
        return None, None


def geocode_zipcode_sync(bot: Any, zipcode: str, default_country: str = None, timeout: int = 10) -> Tuple[Optional[float], Optional[float]]:
    """Synchronous version of geocode_zipcode.
    
    Args:
        bot: Bot instance (must have db_manager and nominatim_rate_limiter).
        zipcode: ZIP code string.
        default_country: Default country code (e.g., "US"). If None, reads from bot.config.
        timeout: Request timeout in seconds.
        
    Returns:
        Tuple[Optional[float], Optional[float]]: Tuple of (latitude, longitude) or (None, None) if not found.
    """
    try:
        # Get default country from config if not provided
        if default_country is None:
            default_country = bot.config.get('Weather', 'default_country', fallback='US')
        
        # Check cache first
        cache_query = f"{zipcode}, {default_country}"
        cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(cache_query)
        if cached_lat is not None and cached_lon is not None:
            return cached_lat, cached_lon
        
        # Use rate-limited Nominatim to geocode the zipcode
        location = rate_limited_nominatim_geocode_sync(bot, cache_query, timeout=timeout)
        if location:
            # Cache the result for future use
            bot.db_manager.cache_geocoding(cache_query, location.latitude, location.longitude)
            return location.latitude, location.longitude
        else:
            return None, None
    except Exception as e:
        bot.logger.error(f"Error geocoding zipcode {zipcode}: {e}")
        return None, None


async def geocode_city(bot: Any, city: str, default_state: str = None, 
                       default_country: str = None,
                       include_address_info: bool = False, 
                       timeout: int = 10) -> Tuple[Optional[float], Optional[float], Optional[Dict]]:
    """Shared function to geocode a city name to lat/lon coordinates.
    
    Uses intelligent fallback logic with major city prioritization.
    
    Args:
        bot: Bot instance (must have db_manager and nominatim_rate_limiter).
        city: City name (may include state/country, e.g., "Seattle, WA" or "Paris, France").
        default_state: Default state abbreviation (e.g., "WA"). If None, reads from bot.config.
        default_country: Default country code (e.g., "US"). If None, reads from bot.config.
        include_address_info: If True, also return address info via reverse geocoding.
        timeout: Request timeout in seconds.
        
    Returns:
        Tuple[Optional[float], Optional[float], Optional[Dict]]: 
            Tuple of (latitude, longitude, address_info_dict) or (None, None, None) if not found.
            address_info_dict is None if include_address_info is False.
    """
    try:
        # Get defaults from config if not provided
        if default_state is None:
            default_state = bot.config.get('Weather', 'default_state', fallback='WA')
        if default_country is None:
            default_country = bot.config.get('Weather', 'default_country', fallback='US')
        
        city_clean = city.strip()
        state_abbr = None
        
        # Parse city, state/country format if present
        if ',' in city_clean:
            parts = [p.strip() for p in city_clean.rsplit(',', 1)]
            if len(parts) == 2:
                city_clean = parts[0]
                state_abbr = parts[1].upper() if len(parts[1]) <= 2 else parts[1]
        
        # Handle major cities with multiple locations (prioritize major cities)
        major_city_queries = get_major_city_queries(city_clean, state_abbr)
        if major_city_queries:
            # Try major city options first
            for major_city_query in major_city_queries:
                cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(major_city_query)
                if cached_lat and cached_lon:
                    lat, lon = cached_lat, cached_lon
                else:
                    location = await rate_limited_nominatim_geocode(bot, major_city_query, timeout=timeout)
                    if location:
                        bot.db_manager.cache_geocoding(major_city_query, location.latitude, location.longitude)
                        lat, lon = location.latitude, location.longitude
                    else:
                        continue
                
                # Get address info if requested
                address_info = None
                if include_address_info:
                    # Check cache for reverse geocoding result
                    reverse_cache_key = f"reverse_{lat}_{lon}"
                    cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                    if cached_address:
                        address_info = cached_address
                    else:
                        try:
                            reverse_location = await rate_limited_nominatim_reverse(bot, f"{lat}, {lon}", timeout=timeout)
                            if reverse_location:
                                address_info = reverse_location.raw.get('address', {})
                                # Cache the reverse geocoding result
                                bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                        except:
                            address_info = {}
                
                return lat, lon, address_info
        
        # If state abbreviation was parsed, use it
        if state_abbr:
            state_query = f"{city_clean}, {state_abbr}, {default_country}"
            cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(state_query)
            if cached_lat and cached_lon:
                lat, lon = cached_lat, cached_lon
            else:
                location = await rate_limited_nominatim_geocode(bot, state_query, timeout=timeout)
                if location:
                    bot.db_manager.cache_geocoding(state_query, location.latitude, location.longitude)
                    lat, lon = location.latitude, location.longitude
                else:
                    lat, lon = None, None
            
            if lat and lon:
                address_info = None
                if include_address_info:
                    # Check cache for reverse geocoding result
                    reverse_cache_key = f"reverse_{lat}_{lon}"
                    cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                    if cached_address:
                        address_info = cached_address
                    else:
                        try:
                            reverse_location = await rate_limited_nominatim_reverse(bot, f"{lat}, {lon}", timeout=timeout)
                            if reverse_location:
                                address_info = reverse_location.raw.get('address', {})
                                # Cache the reverse geocoding result
                                bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                        except:
                            address_info = {}
                return lat, lon, address_info
        
        # Try with default state
        cache_query = f"{city_clean}, {default_state}, {default_country}"
        cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(cache_query)
        if cached_lat and cached_lon:
            lat, lon = cached_lat, cached_lon
        else:
            location = await rate_limited_nominatim_geocode(bot, cache_query, timeout=timeout)
            if location:
                bot.db_manager.cache_geocoding(cache_query, location.latitude, location.longitude)
                lat, lon = location.latitude, location.longitude
            else:
                lat, lon = None, None
        
        if lat and lon:
            address_info = None
            if include_address_info:
                # Check cache for reverse geocoding result
                reverse_cache_key = f"reverse_{lat}_{lon}"
                cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                if cached_address:
                    address_info = cached_address
                else:
                    try:
                        reverse_location = await rate_limited_nominatim_reverse(bot, f"{lat}, {lon}", timeout=timeout)
                        if reverse_location:
                            address_info = reverse_location.raw.get('address', {})
                            # Cache the reverse geocoding result
                            bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                    except:
                        address_info = {}
            return lat, lon, address_info
        
        # Try without state
        location = await rate_limited_nominatim_geocode(bot, f"{city_clean}, {default_country}", timeout=timeout)
        if location:
            bot.db_manager.cache_geocoding(f"{city_clean}, {default_country}", location.latitude, location.longitude)
            lat, lon = location.latitude, location.longitude
            
            address_info = None
            if include_address_info:
                # Check cache for reverse geocoding result
                reverse_cache_key = f"reverse_{lat}_{lon}"
                cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                if cached_address:
                    address_info = cached_address
                else:
                    try:
                        reverse_location = await rate_limited_nominatim_reverse(bot, f"{lat}, {lon}", timeout=timeout)
                        if reverse_location:
                            address_info = reverse_location.raw.get('address', {})
                            # Cache the reverse geocoding result
                            bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                    except:
                        address_info = {}
            return lat, lon, address_info
        
        # Try international (no country suffix)
        location = await rate_limited_nominatim_geocode(bot, city_clean, timeout=timeout)
        if location:
            bot.db_manager.cache_geocoding(city_clean, location.latitude, location.longitude)
            lat, lon = location.latitude, location.longitude
            
            address_info = None
            if include_address_info:
                # Check cache for reverse geocoding result
                reverse_cache_key = f"reverse_{lat}_{lon}"
                cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                if cached_address:
                    address_info = cached_address
                else:
                    try:
                        reverse_location = await rate_limited_nominatim_reverse(bot, f"{lat}, {lon}", timeout=timeout)
                        if reverse_location:
                            address_info = reverse_location.raw.get('address', {})
                            # Cache the reverse geocoding result
                            bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                    except:
                        address_info = {}
            return lat, lon, address_info
        
        return None, None, None
        
    except Exception as e:
        bot.logger.error(f"Error geocoding city {city}: {e}")
        return None, None, None


def geocode_city_sync(bot: Any, city: str, default_state: str = None,
                      default_country: str = None,
                      include_address_info: bool = False,
                      timeout: int = 10) -> Tuple[Optional[float], Optional[float], Optional[Dict]]:
    """Synchronous version of geocode_city.
    
    Args:
        bot: Bot instance (must have db_manager and nominatim_rate_limiter).
        city: City name (may include state/country, e.g., "Seattle, WA" or "Paris, France").
        default_state: Default state abbreviation (e.g., "WA"). If None, reads from bot.config.
        default_country: Default country code (e.g., "US"). If None, reads from bot.config.
        include_address_info: If True, also return address info via reverse geocoding.
        timeout: Request timeout in seconds.
        
    Returns:
        Tuple[Optional[float], Optional[float], Optional[Dict]]:
            Tuple of (latitude, longitude, address_info_dict) or (None, None, None) if not found.
            address_info_dict is None if include_address_info is False.
    """
    try:
        # Get defaults from config if not provided
        if default_state is None:
            default_state = bot.config.get('Weather', 'default_state', fallback='WA')
        if default_country is None:
            default_country = bot.config.get('Weather', 'default_country', fallback='US')
        
        city_clean = city.strip()
        state_abbr = None
        
        # Parse city, state/country format if present
        if ',' in city_clean:
            parts = [p.strip() for p in city_clean.rsplit(',', 1)]
            if len(parts) == 2:
                city_clean = parts[0]
                state_abbr = parts[1].upper() if len(parts[1]) <= 2 else parts[1]
        
        # Handle major cities with multiple locations (prioritize major cities)
        major_city_queries = get_major_city_queries(city_clean, state_abbr)
        if major_city_queries:
            # Try major city options first
            for major_city_query in major_city_queries:
                cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(major_city_query)
                if cached_lat and cached_lon:
                    lat, lon = cached_lat, cached_lon
                else:
                    location = rate_limited_nominatim_geocode_sync(bot, major_city_query, timeout=timeout)
                    if location:
                        bot.db_manager.cache_geocoding(major_city_query, location.latitude, location.longitude)
                        lat, lon = location.latitude, location.longitude
                    else:
                        continue
                
                # Get address info if requested
                address_info = None
                if include_address_info:
                    # Check cache for reverse geocoding result
                    reverse_cache_key = f"reverse_{lat}_{lon}"
                    cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                    if cached_address:
                        address_info = cached_address
                    else:
                        try:
                            reverse_location = rate_limited_nominatim_reverse_sync(bot, f"{lat}, {lon}", timeout=timeout)
                            if reverse_location:
                                address_info = reverse_location.raw.get('address', {})
                                # Cache the reverse geocoding result
                                bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                        except:
                            address_info = {}
                
                return lat, lon, address_info
        
        # If state abbreviation was parsed, use it
        if state_abbr:
            state_query = f"{city_clean}, {state_abbr}, {default_country}"
            cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(state_query)
            if cached_lat and cached_lon:
                lat, lon = cached_lat, cached_lon
            else:
                location = rate_limited_nominatim_geocode_sync(bot, state_query, timeout=timeout)
                if location:
                    bot.db_manager.cache_geocoding(state_query, location.latitude, location.longitude)
                    lat, lon = location.latitude, location.longitude
                else:
                    lat, lon = None, None
            
            if lat and lon:
                address_info = None
                if include_address_info:
                    # Check cache for reverse geocoding result
                    reverse_cache_key = f"reverse_{lat}_{lon}"
                    cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                    if cached_address:
                        address_info = cached_address
                    else:
                        try:
                            reverse_location = rate_limited_nominatim_reverse_sync(bot, f"{lat}, {lon}", timeout=timeout)
                            if reverse_location:
                                address_info = reverse_location.raw.get('address', {})
                                # Cache the reverse geocoding result
                                bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                        except:
                            address_info = {}
                return lat, lon, address_info
        
        # Try with default state
        cache_query = f"{city_clean}, {default_state}, {default_country}"
        cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(cache_query)
        if cached_lat and cached_lon:
            lat, lon = cached_lat, cached_lon
        else:
            location = rate_limited_nominatim_geocode_sync(bot, cache_query, timeout=timeout)
            if location:
                bot.db_manager.cache_geocoding(cache_query, location.latitude, location.longitude)
                lat, lon = location.latitude, location.longitude
            else:
                lat, lon = None, None
        
        if lat and lon:
            address_info = None
            if include_address_info:
                # Check cache for reverse geocoding result
                reverse_cache_key = f"reverse_{lat}_{lon}"
                cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                if cached_address:
                    address_info = cached_address
                else:
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(bot, f"{lat}, {lon}", timeout=timeout)
                        if reverse_location:
                            address_info = reverse_location.raw.get('address', {})
                            # Cache the reverse geocoding result
                            bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                    except:
                        address_info = {}
            return lat, lon, address_info
        
        # Try without state
        location = rate_limited_nominatim_geocode_sync(bot, f"{city_clean}, {default_country}", timeout=timeout)
        if location:
            bot.db_manager.cache_geocoding(f"{city_clean}, {default_country}", location.latitude, location.longitude)
            lat, lon = location.latitude, location.longitude
            
            address_info = None
            if include_address_info:
                # Check cache for reverse geocoding result
                reverse_cache_key = f"reverse_{lat}_{lon}"
                cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                if cached_address:
                    address_info = cached_address
                else:
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(bot, f"{lat}, {lon}", timeout=timeout)
                        if reverse_location:
                            address_info = reverse_location.raw.get('address', {})
                            # Cache the reverse geocoding result
                            bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                    except:
                        address_info = {}
            return lat, lon, address_info
        
        # Try international (no country suffix)
        location = rate_limited_nominatim_geocode_sync(bot, city_clean, timeout=timeout)
        if location:
            bot.db_manager.cache_geocoding(city_clean, location.latitude, location.longitude)
            lat, lon = location.latitude, location.longitude
            
            address_info = None
            if include_address_info:
                # Check cache for reverse geocoding result
                reverse_cache_key = f"reverse_{lat}_{lon}"
                cached_address = bot.db_manager.get_cached_json(reverse_cache_key, "geolocation")
                if cached_address:
                    address_info = cached_address
                else:
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(bot, f"{lat}, {lon}", timeout=timeout)
                        if reverse_location:
                            address_info = reverse_location.raw.get('address', {})
                            # Cache the reverse geocoding result
                            bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                    except:
                        address_info = {}
            return lat, lon, address_info
        
        return None, None, None
        
    except Exception as e:
        bot.logger.error(f"Error geocoding city {city}: {e}")
        return None, None, None


def resolve_path(file_path: Union[str, Path], base_dir: Union[str, Path] = '.') -> str:
    """Resolve a file path relative to a base directory.
    
    If the path is absolute, it is resolved and returned as-is.
    If the path is relative, it is resolved relative to the base directory.
    
    Args:
        file_path: Path to resolve (can be string or Path object).
        base_dir: Base directory for resolving relative paths (default: current directory).
    
    Returns:
        str: Resolved absolute path as a string.
    
    Examples:
        >>> resolve_path('data.db', '/opt/bot')
        '/opt/bot/data.db'
        >>> resolve_path('/var/lib/bot/data.db', '/opt/bot')
        '/var/lib/bot/data.db'
    """
    file_path = Path(file_path) if not isinstance(file_path, Path) else file_path
    base_dir = Path(base_dir) if not isinstance(base_dir, Path) else base_dir
    
    if file_path.is_absolute():
        return str(file_path.resolve())
    else:
        return str((base_dir.resolve() / file_path).resolve())


def check_internet_connectivity(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Check if internet connectivity is available by attempting to connect to a reliable host.
    
    First tries a lightweight DNS port check (faster, doesn't require DNS resolution).
    If that fails (e.g., DNS port is blocked), falls back to an HTTP request check.
    
    Args:
        host: Host to connect to (default: 8.8.8.8, Google's public DNS).
        port: Port to connect to (default: 53, DNS port).
        timeout: Connection timeout in seconds (default: 3.0).
        
    Returns:
        bool: True if connection successful, False otherwise.
    """
    # First try: DNS port check (fastest, works if DNS port is open)
    try:
        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        sock.close()
        socket.setdefaulttimeout(None)  # Reset to default
        return True
    except (socket.error, OSError, socket.timeout):
        socket.setdefaulttimeout(None)  # Reset to default
        # DNS check failed, try HTTP fallback
        pass
    
    # Fallback: HTTP request check (works even if DNS port is blocked)
    try:
        # Use a reliable HTTP endpoint that's likely to be accessible
        # Using IP address to avoid DNS resolution issues
        http_url = "http://1.1.1.1"  # Cloudflare DNS
        urllib.request.urlopen(http_url, timeout=timeout).close()
        return True
    except (urllib.error.URLError, OSError, socket.timeout):
        # If IP-based check fails, try a hostname-based check
        try:
            http_url = "http://www.google.com"
            urllib.request.urlopen(http_url, timeout=timeout).close()
            return True
        except (urllib.error.URLError, OSError, socket.timeout):
            return False


async def check_internet_connectivity_async(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Async version of check_internet_connectivity.
    
    First tries a lightweight DNS port check (faster, doesn't require DNS resolution).
    If that fails (e.g., DNS port is blocked), falls back to an HTTP request check.
    
    Args:
        host: Host to connect to (default: 8.8.8.8, Google's public DNS).
        port: Port to connect to (default: 53, DNS port).
        timeout: Connection timeout in seconds (default: 3.0).
        
    Returns:
        bool: True if connection successful, False otherwise.
    """
    # First try: DNS port check (fastest, works if DNS port is open)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError, socket.error, ConnectionError):
        # DNS check failed, try HTTP fallback
        pass
    except Exception:
        # Unexpected error, try HTTP fallback
        pass
    
    # Fallback: HTTP request check (works even if DNS port is blocked)
    # Run urllib in executor to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        # Use a reliable HTTP endpoint that's likely to be accessible
        # Using IP address to avoid DNS resolution issues
        http_url = "http://1.1.1.1"  # Cloudflare DNS
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(http_url, timeout=timeout).close()
            ),
            timeout=timeout
        )
        return True
    except (asyncio.TimeoutError, urllib.error.URLError, OSError, socket.timeout):
        # If IP-based check fails, try a hostname-based check
        try:
            http_url = "http://www.google.com"
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(http_url, timeout=timeout).close()
                ),
                timeout=timeout
            )
            return True
        except (asyncio.TimeoutError, urllib.error.URLError, OSError, socket.timeout):
            return False
    except Exception:
        return False


def parse_path_string(path_str: str) -> List[str]:
    """Parse a path string to extract node IDs.
    
    Handles various formats:
    - "11,98,a4,49,cd,5f,01" (comma-separated)
    - "11 98 a4 49 cd 5f 01" (space-separated)
    - "1198a449cd5f01" (continuous hex)
    - "01,5f (2 hops)" (with hop count suffix)
    
    Args:
        path_str: Path string in various formats.
        
    Returns:
        List[str]: List of 2-character uppercase hex node IDs.
    """
    if not path_str:
        return []
    
    # Remove hop count suffix if present (e.g., " (2 hops)")
    path_str = re.sub(r'\s*\([^)]*hops?[^)]*\)', '', path_str, flags=re.IGNORECASE)
    path_str = path_str.strip()
    
    # Replace common separators with spaces
    path_str = path_str.replace(',', ' ').replace(':', ' ')
    
    # Extract hex values using regex (2-character hex pairs)
    hex_pattern = r'[0-9a-fA-F]{2}'
    hex_matches = re.findall(hex_pattern, path_str)
    
    # Convert to uppercase for consistency
    return [match.upper() for match in hex_matches]


def calculate_path_distances(bot: Any, path_str: str) -> Tuple[str, str]:
    """Calculate path distance metrics from a path string.
    
    Args:
        bot: Bot instance (must have db_manager).
        path_str: Path string (e.g., "11,98,a4,49,cd,5f,01" or "01,5f (2 hops)" or "Direct").
        
    Returns:
        Tuple[str, str]: A tuple containing:
            - path_distance_str: Total distance with segment info (e.g., "123.4km (3 segs, 1 no-loc)").
            - firstlast_distance_str: Distance between first and last repeater (e.g., "45.6km").
    """
    if not path_str:
        return "directly (0 hops)", "N/A (direct)"
    
    # Check if it's a direct connection
    path_lower = path_str.lower()
    if "direct" in path_lower or "0 hops" in path_lower or path_str.strip() == "":
        return "directly (0 hops)", "N/A (direct)"
    
    if not hasattr(bot, 'db_manager'):
        return "unknown distance", "unknown"
    
    try:
        # Parse node IDs from path string
        node_ids = parse_path_string(path_str)
        
        if len(node_ids) == 0:
            # No nodes parsed - likely direct connection
            return "directly (0 hops)", "N/A (direct)"
        elif len(node_ids) == 1:
            # Single node - local/one hop (no first/last distance since only one node)
            return "locally (1 hop)", "N/A (1 hop)"
        elif len(node_ids) < 2:
            # Edge case - less than 2 nodes
            return "locally (1 hop)", "N/A (1 hop)"
        
        # Look up locations for each node ID
        node_locations = []
        for node_id in node_ids:
            location = _get_node_location_from_db(bot, node_id)
            node_locations.append(location)
        
        # Calculate total path distance (sum of all segments)
        total_distance = 0.0
        segments_with_location = 0
        segments_without_location = 0
        
        for i in range(len(node_locations) - 1):
            loc1 = node_locations[i]
            loc2 = node_locations[i + 1]
            
            if loc1 and loc2:
                # Both nodes have locations
                segment_distance = calculate_distance(
                    loc1[0], loc1[1],
                    loc2[0], loc2[1]
                )
                total_distance += segment_distance
                segments_with_location += 1
            else:
                # At least one node missing location
                segments_without_location += 1
        
        # Format path_distance string
        if total_distance > 0:
            path_distance_str = f"{total_distance:.1f}km"
            if segments_with_location > 0 or segments_without_location > 0:
                seg_info = []
                if segments_with_location > 0:
                    seg_info.append(f"{segments_with_location} segs")
                if segments_without_location > 0:
                    seg_info.append(f"{segments_without_location} no-loc")
                if seg_info:
                    path_distance_str += f" ({', '.join(seg_info)})"
        else:
            # No distance calculated (all segments missing locations)
            if segments_without_location > 0:
                # We have segments but no location data
                hop_count = len(node_ids)
                path_distance_str = f"unknown distance ({hop_count} hops, no locations)"
            else:
                # Fallback - shouldn't happen but provide meaningful text
                hop_count = len(node_ids)
                path_distance_str = f"unknown distance ({hop_count} hops)"
        
        # Calculate first-to-last distance
        firstlast_distance_str = ""
        first_location = node_locations[0]
        last_location = node_locations[-1]
        
        if first_location and last_location:
            firstlast_distance = calculate_distance(
                first_location[0], first_location[1],
                last_location[0], last_location[1]
            )
            firstlast_distance_str = f"{firstlast_distance:.1f}km"
        elif len(node_ids) >= 2:
            # We have 2+ nodes but missing location data
            firstlast_distance_str = "unknown (no locations)"
        
        return path_distance_str, firstlast_distance_str
        
    except Exception as e:
        # Log error but don't fail - return empty strings
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error calculating path distances: {e}")
        return "", ""


def _get_node_location_from_db(bot: Any, node_id: str) -> Optional[Tuple[float, float]]:
    """Get location for a node ID from the database.
    
    Args:
        bot: Bot instance (must have db_manager).
        node_id: 2-character hex node ID (e.g., "01", "5f").
        
    Returns:
        Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None if not found.
    """
    if not hasattr(bot, 'db_manager'):
        return None
    
    try:
        # Look up node by public key prefix (first 2 characters)
        query = '''
            SELECT latitude, longitude 
            FROM complete_contact_tracking 
            WHERE public_key LIKE ? 
            AND latitude IS NOT NULL AND longitude IS NOT NULL
            AND latitude != 0 AND longitude != 0
            AND role IN ('repeater', 'roomserver')
            ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
            LIMIT 1
        '''
        
        prefix_pattern = f"{node_id}%"
        results = bot.db_manager.execute_query(query, (prefix_pattern,))
        
        if results:
            row = results[0]
            lat = row.get('latitude')
            lon = row.get('longitude')
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
        
        return None
    except Exception as e:
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error getting node location for {node_id}: {e}")
        return None


def format_keyword_response_with_placeholders(
    response_format: str,
    message: Any,
    bot: Any,
    mesh_info: Optional[Dict[str, Any]] = None
) -> str:
    """Format a keyword response string with all available placeholders.
    
    Supports both message-based placeholders and mesh-info-based placeholders.
    This is a shared function used by both Keywords and Scheduled_Messages.
    
    Args:
        response_format: Response format string with placeholders.
        message: MeshMessage instance (can be None for scheduled messages).
        bot: Bot instance (must have config, db_manager).
        mesh_info: Optional mesh network info dict (for scheduled message placeholders).
        
    Returns:
        str: Formatted response string.
    """
    try:
        replacements = {}
        
        # Message-based placeholders (require message object)
        if message:
            # Basic message fields
            replacements['sender'] = message.sender_id or "Unknown"
            replacements['path'] = message.path or "Unknown"
            replacements['snr'] = message.snr or "Unknown"
            replacements['rssi'] = message.rssi or "Unknown"
            replacements['elapsed'] = message.elapsed or "Unknown"
            
            # Build connection_info
            routing_info = message.path or "Unknown routing"
            if "via ROUTE_TYPE_" in routing_info:
                parts = routing_info.split(" via ROUTE_TYPE_")
                if len(parts) > 0:
                    routing_info = parts[0]
            
            snr_info = f"SNR: {message.snr or 'Unknown'} dB"
            rssi_info = f"RSSI: {message.rssi or 'Unknown'} dBm"
            connection_info = f"{routing_info} | {snr_info} | {rssi_info}"
            replacements['connection_info'] = connection_info
            
            # Calculate path distances
            path_distance, firstlast_distance = calculate_path_distances(bot, message.path or "")
            replacements['path_distance'] = path_distance
            replacements['firstlast_distance'] = firstlast_distance
            
            # Format timestamp
            try:
                timezone_str = bot.config.get('Bot', 'timezone', fallback='')
                if timezone_str:
                    try:
                        import pytz
                        from datetime import datetime
                        tz = pytz.timezone(timezone_str)
                        dt = datetime.now(tz)
                    except Exception:
                        from datetime import datetime
                        dt = datetime.now()
                else:
                    from datetime import datetime
                    dt = datetime.now()
                
                time_str = dt.strftime("%H:%M:%S")
            except Exception:
                time_str = "Unknown"
            
            replacements['timestamp'] = time_str
        else:
            # No message - use defaults for message-based placeholders
            replacements['sender'] = "Unknown"
            replacements['path'] = "Unknown"
            replacements['snr'] = "Unknown"
            replacements['rssi'] = "Unknown"
            replacements['elapsed'] = "Unknown"
            replacements['connection_info'] = "Unknown"
            replacements['path_distance'] = ""
            replacements['firstlast_distance'] = ""
            replacements['timestamp'] = "Unknown"
        
        # Mesh-info-based placeholders (from scheduled messages)
        if mesh_info:
            replacements.update({
                'total_contacts': mesh_info.get('total_contacts', 0),
                'total_repeaters': mesh_info.get('total_repeaters', 0),
                'total_companions': mesh_info.get('total_companions', 0),
                'total_roomservers': mesh_info.get('total_roomservers', 0),
                'total_sensors': mesh_info.get('total_sensors', 0),
                'recent_activity_24h': mesh_info.get('recent_activity_24h', 0),
                'new_companions_7d': mesh_info.get('new_companions_7d', 0),
                'new_repeaters_7d': mesh_info.get('new_repeaters_7d', 0),
                'new_roomservers_7d': mesh_info.get('new_roomservers_7d', 0),
                'new_sensors_7d': mesh_info.get('new_sensors_7d', 0),
                'total_contacts_30d': mesh_info.get('total_contacts_30d', 0),
                'total_repeaters_30d': mesh_info.get('total_repeaters_30d', 0),
                'total_companions_30d': mesh_info.get('total_companions_30d', 0),
                'total_roomservers_30d': mesh_info.get('total_roomservers_30d', 0),
                'total_sensors_30d': mesh_info.get('total_sensors_30d', 0),
                # Legacy placeholders
                'repeaters': mesh_info.get('total_repeaters', 0),
                'companions': mesh_info.get('total_companions', 0),
            })
        else:
            # No mesh_info - use defaults
            mesh_defaults = {
                'total_contacts': 0,
                'total_repeaters': 0,
                'total_companions': 0,
                'total_roomservers': 0,
                'total_sensors': 0,
                'recent_activity_24h': 0,
                'new_companions_7d': 0,
                'new_repeaters_7d': 0,
                'new_roomservers_7d': 0,
                'new_sensors_7d': 0,
                'total_contacts_30d': 0,
                'total_repeaters_30d': 0,
                'total_companions_30d': 0,
                'total_roomservers_30d': 0,
                'total_sensors_30d': 0,
                'repeaters': 0,
                'companions': 0,
            }
            replacements.update(mesh_defaults)
        
        # Format the response with all replacements
        return response_format.format(**replacements)
        
    except (KeyError, ValueError) as e:
        # If formatting fails, return as-is (might not have all placeholders)
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error formatting response with placeholders: {e}")
        return response_format
