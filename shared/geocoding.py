#!/usr/bin/env python3
"""
Geocoding utilities shared by the bot and web viewer.

Includes Haversine distance, Nominatim wrappers (rate-limited, sync and async),
ZIP/city geocoding with caching, and location-string normalization helpers.
"""

import asyncio
from typing import Any, Optional

def get_major_city_queries(city: str, state_abbr: Optional[str] = None) -> list[str]:
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


# Optional geocoding helper libraries
try:
    import pycountry
    PYCOUNTRY_AVAILABLE = True
except ImportError:
    PYCOUNTRY_AVAILABLE = False

try:
    import us
    US_AVAILABLE = True
except ImportError:
    US_AVAILABLE = False


def normalize_country_name(country_input: str) -> tuple[Optional[str], Optional[str]]:
    """Normalize country name to ISO code and standard name.

    Args:
        country_input: Country name or code (e.g., "Sweden", "SE", "United States", "USA", "US")

    Returns:
        tuple: (iso_code, standard_name) or (None, None) if not found
        Example: ("SE", "Sweden") or ("US", "United States")
    """
    if not PYCOUNTRY_AVAILABLE:
        return None, None

    if not country_input:
        return None, None

    country_input = country_input.strip()

    # Try to find by alpha_2 code (e.g., "US", "SE")
    if len(country_input) == 2:
        try:
            country = pycountry.countries.get(alpha_2=country_input.upper())
            if country:
                return country.alpha_2, country.name
        except (KeyError, AttributeError):
            pass

    # Try to find by alpha_3 code (e.g., "USA", "SWE")
    if len(country_input) == 3:
        try:
            country = pycountry.countries.get(alpha_3=country_input.upper())
            if country:
                return country.alpha_2, country.name
        except (KeyError, AttributeError):
            pass

    # Try to find by name (case-insensitive, handles common variants)
    country_input_lower = country_input.lower()

    # Handle common variants
    country_variants = {
        'usa': 'United States',
        'u.s.a.': 'United States',
        'u.s.': 'United States',
        'uk': 'United Kingdom',
        'u.k.': 'United Kingdom',
        'great britain': 'United Kingdom',
    }

    search_name = country_variants.get(country_input_lower, country_input)

    try:
        # Try exact match first
        country = pycountry.countries.get(name=search_name)
        if country:
            return country.alpha_2, country.name

        # Try fuzzy search
        for country in pycountry.countries:
            if country.name.lower() == search_name.lower():
                return country.alpha_2, country.name
    except (KeyError, AttributeError):
        pass

    return None, None


def normalize_us_state(state_input: str) -> tuple[Optional[str], Optional[str]]:
    """Normalize US state name to abbreviation and full name.

    Args:
        state_input: State name or abbreviation (e.g., "Washington", "WA", "California", "CA")

    Returns:
        tuple: (abbreviation, full_name) or (None, None) if not found
        Example: ("WA", "Washington") or ("CA", "California")
    """
    if not US_AVAILABLE:
        return None, None

    if not state_input:
        return None, None

    state_input = state_input.strip()

    # Try to find by abbreviation
    if len(state_input) == 2:
        try:
            state = us.states.lookup(state_input.upper())
            if state:
                return state.abbr, state.name
        except (AttributeError, KeyError):
            pass

    # Try to find by name
    try:
        state = us.states.lookup(state_input)
        if state:
            return state.abbr, state.name
    except (AttributeError, KeyError):
        pass

    return None, None


def is_country_name(text: str) -> bool:
    """Check if text is likely a country name.

    Args:
        text: Text to check

    Returns:
        bool: True if text appears to be a country name
    """
    if not text:
        return False

    if PYCOUNTRY_AVAILABLE:
        iso_code, _ = normalize_country_name(text)
        if iso_code is not None:
            return True

    if US_AVAILABLE:
        state_abbr, _ = normalize_us_state(text)
        if state_abbr:
            return False  # It's a US state, not a country

    if len(text) <= 2:
        return False  # Unknown 2-char (not a known country or US state)

    return len(text) > 2  # Longer text, assume country


def is_us_state(text: str) -> bool:
    """Check if text is likely a US state name or abbreviation.

    Args:
        text: Text to check

    Returns:
        bool: True if text appears to be a US state
    """
    if not text:
        return False

    if US_AVAILABLE:
        state_abbr, _ = normalize_us_state(text)
        return state_abbr is not None

    return False


def parse_location_string(location: str) -> tuple[str, Optional[str], Optional[str]]:
    """Parse a location string into city, state/country parts.

    Args:
        location: Location string (e.g., "Stockholm, Sweden" or "Seattle, WA")

    Returns:
        tuple: (city, state_or_country, type) where type is "state", "country", or None
        Example: ("Stockholm", "Sweden", "country") or ("Seattle", "WA", "state")
    """
    if ',' not in location:
        return location.strip(), None, None

    parts = [p.strip() for p in location.rsplit(',', 1)]
    if len(parts) != 2:
        return location.strip(), None, None

    city, second_part = parts

    # Check if it's a US state
    if is_us_state(second_part):
        state_abbr, _ = normalize_us_state(second_part)
        return city, state_abbr, "state"

    # Check if it's a country
    if is_country_name(second_part):
        iso_code, country_name = normalize_country_name(second_part)
        if iso_code:
            return city, country_name, "country"

    # If 2 chars or less, assume state abbreviation
    if len(second_part) <= 2:
        return city, second_part.upper(), "state"

    # Otherwise, assume country
    return city, second_part, "country"


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


async def geocode_zipcode(bot: Any, zipcode: str, default_country: Optional[str] = None, timeout: int = 10) -> tuple[Optional[float], Optional[float]]:
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


def geocode_zipcode_sync(bot: Any, zipcode: str, default_country: Optional[str] = None, timeout: int = 10) -> tuple[Optional[float], Optional[float]]:
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


async def geocode_city(bot: Any, city: str, default_state: Optional[str] = None,
                       default_country: Optional[str] = None,
                       include_address_info: bool = False,
                       timeout: int = 10) -> tuple[Optional[float], Optional[float], Optional[dict]]:
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
            default_state = bot.config.get('Weather', 'default_state', fallback='')
        if default_country is None:
            default_country = bot.config.get('Weather', 'default_country', fallback='US')

        city_clean = city.strip()
        state_abbr = None
        country_name = None

        # Parse city, state/country format if present
        if ',' in city_clean:
            parts = [p.strip() for p in city_clean.rsplit(',', 1)]
            if len(parts) == 2:
                city_clean = parts[0]
                second_part = parts[1]

                # Use geocoding helpers to determine if it's a state or country
                try:

                    _, parsed_part, part_type = parse_location_string(f"{city_clean}, {second_part}")

                    if part_type == "state":
                        state_abbr, _ = normalize_us_state(second_part)
                        if not state_abbr:
                            state_abbr = second_part.upper() if len(second_part) <= 2 else None
                    elif part_type == "country":
                        iso_code, country_name = normalize_country_name(second_part)
                        if iso_code:
                            # Use the normalized country name for better geocoding
                            country_name = country_name
                        else:
                            country_name = second_part
                    else:
                        # Fallback to original logic
                        if len(second_part) <= 2:
                            state_abbr = second_part.upper()
                        else:
                            country_name = second_part
                except ImportError:
                    # Fallback if helpers not available
                    if len(second_part) <= 2:
                        state_abbr = second_part.upper()
                    else:
                        country_name = second_part

        # Handle major cities with multiple locations (prioritize major cities).
        # Skip when user specified a country (e.g. "Paris, FR") so we honor their choice.
        major_city_queries = get_major_city_queries(city_clean, state_abbr)
        if major_city_queries and not country_name:
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

        # If country name was parsed (not a state abbreviation), try geocoding with country first
        if country_name:
            # Try with country name directly (e.g., "Stockholm, Sweden")
            country_query = f"{city_clean}, {country_name}"
            cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(country_query)
            if cached_lat and cached_lon:
                lat, lon = cached_lat, cached_lon
            else:
                location = await rate_limited_nominatim_geocode(bot, country_query, timeout=timeout)
                if location:
                    bot.db_manager.cache_geocoding(country_query, location.latitude, location.longitude)
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

        # If no country/state specified, try city name alone first (finds most prominent international city)
        # This handles cases like "Tokyo" -> Tokyo, Japan (not Tokyo, WA)
        if not state_abbr and not country_name:
            location = await rate_limited_nominatim_geocode(bot, city_clean, timeout=timeout)
            if location:
                # Check if result is in default country and is a small/obscure location
                # If so, we'll try with default country/state as fallback
                result_in_default_country = False
                is_obscure_location = False

                # Always get address info to check the result
                try:
                    reverse_location = await rate_limited_nominatim_reverse(bot, f"{location.latitude}, {location.longitude}", timeout=timeout)
                    if reverse_location:
                        address = reverse_location.raw.get('address', {})
                        result_country = address.get('country', '').upper()
                        result_country_code = address.get('country_code', '').upper()

                        # Check if result is in default country
                        default_country_upper = default_country.upper()
                        if (result_country == default_country_upper or
                            result_country_code == default_country_upper or
                            'United States' in result_country and default_country_upper == 'US'):
                            result_in_default_country = True

                            # Check if it's an obscure location (county, township, small town)
                            place_type = address.get('type', '').lower()
                            place_name = (address.get('city') or
                                        address.get('town') or
                                        address.get('village') or
                                        address.get('municipality') or
                                        address.get('county', '')).lower()

                            # Obscure if it's a county, township, or if city name doesn't match the place name
                            if ('county' in place_type or
                                'township' in place_type or
                                (place_name and city_clean.lower() not in place_name and place_name not in city_clean.lower())):
                                is_obscure_location = True
                except:
                    pass

                # If result is in default country and is obscure, skip it and try with default country/state
                if result_in_default_country and is_obscure_location:
                    # Fall through to try with default country/state
                    pass
                else:
                    # Use the international result (either not in default country, or is a proper city match)
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
                                if not reverse_location:
                                    reverse_location = await rate_limited_nominatim_reverse(bot, f"{lat}, {lon}", timeout=timeout)
                                if reverse_location:
                                    address_info = reverse_location.raw.get('address', {})
                                    # Cache the reverse geocoding result
                                    bot.db_manager.cache_json(reverse_cache_key, address_info, "geolocation", cache_hours=720)
                            except:
                                address_info = {}
                    return lat, lon, address_info

        # Try with default state (fallback for US cities when no country specified).
        # Skip when default_state is empty (e.g. non-US default_country or key unset).
        if default_state and default_state.strip():
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

        return None, None, None

    except Exception as e:
        bot.logger.error(f"Error geocoding city {city}: {e}")
        return None, None, None


def geocode_city_sync(bot: Any, city: str, default_state: Optional[str] = None,
                      default_country: Optional[str] = None,
                      include_address_info: bool = False,
                      timeout: int = 10) -> tuple[Optional[float], Optional[float], Optional[dict]]:
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
            default_state = bot.config.get('Weather', 'default_state', fallback='')
        if default_country is None:
            default_country = bot.config.get('Weather', 'default_country', fallback='US')

        city_clean = city.strip()
        state_abbr = None

        # Parse city, state/country format if present
        state_abbr = None
        country_name = None
        if ',' in city_clean:
            parts = [p.strip() for p in city_clean.rsplit(',', 1)]
            if len(parts) == 2:
                city_clean = parts[0]
                second_part = parts[1]

                # Use geocoding helpers to determine if it's a state or country
                try:

                    _, parsed_part, part_type = parse_location_string(f"{city_clean}, {second_part}")

                    if part_type == "state":
                        state_abbr, _ = normalize_us_state(second_part)
                        if not state_abbr:
                            state_abbr = second_part.upper() if len(second_part) <= 2 else None
                    elif part_type == "country":
                        iso_code, country_name = normalize_country_name(second_part)
                        if iso_code:
                            # Use the normalized country name for better geocoding
                            country_name = country_name
                        else:
                            country_name = second_part
                    else:
                        # Fallback to original logic
                        if len(second_part) <= 2:
                            state_abbr = second_part.upper()
                        else:
                            country_name = second_part
                except ImportError:
                    # Fallback if helpers not available
                    if len(second_part) <= 2:
                        state_abbr = second_part.upper()
                    else:
                        country_name = second_part

        # Handle major cities with multiple locations (prioritize major cities).
        # Skip when user specified a country (e.g. "Paris, FR") so we honor their choice.
        major_city_queries = get_major_city_queries(city_clean, state_abbr)
        if major_city_queries and not country_name:
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

        # If country name was parsed (not a state abbreviation), try geocoding with country first
        if country_name:
            # Try with country name directly (e.g., "Stockholm, Sweden")
            country_query = f"{city_clean}, {country_name}"
            cached_lat, cached_lon = bot.db_manager.get_cached_geocoding(country_query)
            if cached_lat and cached_lon:
                lat, lon = cached_lat, cached_lon
            else:
                location = rate_limited_nominatim_geocode_sync(bot, country_query, timeout=timeout)
                if location:
                    bot.db_manager.cache_geocoding(country_query, location.latitude, location.longitude)
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

        # If no country/state specified, try city name alone first (finds most prominent international city)
        # This handles cases like "Tokyo" -> Tokyo, Japan (not Tokyo, WA)
        if not state_abbr and not country_name:
            location = rate_limited_nominatim_geocode_sync(bot, city_clean, timeout=timeout)
            if location:
                # Check if result is in default country and is a small/obscure location
                # If so, we'll try with default country/state as fallback
                result_in_default_country = False
                is_obscure_location = False

                if include_address_info:
                    try:
                        reverse_location = rate_limited_nominatim_reverse_sync(bot, f"{location.latitude}, {location.longitude}", timeout=timeout)
                        if reverse_location:
                            address = reverse_location.raw.get('address', {})
                            result_country = address.get('country', '').upper()
                            result_country_code = address.get('country_code', '').upper()

                            # Check if result is in default country
                            default_country_upper = default_country.upper()
                            if (result_country == default_country_upper or
                                result_country_code == default_country_upper or
                                'United States' in result_country and default_country_upper == 'US'):
                                result_in_default_country = True

                                # Check if it's an obscure location (county, township, small town)
                                place_type = address.get('type', '').lower()
                                place_name = address.get('city') or address.get('town') or address.get('village') or ''

                                # Obscure if it's a county, township, or if city name doesn't match
                                if ('county' in place_type or
                                    'township' in place_type or
                                    city_clean.lower() not in place_name.lower()):
                                    is_obscure_location = True
                    except:
                        pass

                # If result is in default country and is obscure, try with default country/state
                if result_in_default_country and is_obscure_location:
                    # Fall through to try with default country/state
                    pass
                else:
                    # Use the international result
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

        # Try with default state (fallback for US cities when no country specified).
        # Skip when default_state is empty (e.g. non-US default_country or key unset).
        if default_state and default_state.strip():
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

        return None, None, None

    except Exception as e:
        bot.logger.error(f"Error geocoding city {city}: {e}")
        return None, None, None


