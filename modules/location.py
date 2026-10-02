#!/usr/bin/env python3
"""Shared location classification and best-effort geocoding for command plugins.

Low-level Nominatim / geocode_city / geocode_zipcode helpers remain in
``modules.utils``. This module is the high-level front door (coords | ZIP |
city | optional repeater / region capitals / neighborhoods) with opt-in
``ResolveOptions`` so commands can migrate without changing semantics.
"""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Optional, Union

import requests

from .region_capitals import REGION_DEFAULT_NOTE, region_capital_query
from .utils import (
    abbreviate_location,
    geocode_city_sync,
    geocode_zipcode_sync,
    normalize_us_state,
    rate_limited_nominatim_geocode_sync,
    rate_limited_nominatim_reverse_sync,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COORD_RE = re.compile(r"^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$")
ZIP_RE = re.compile(r"^\s*\d{5}\s*$")

_COUNTRY_WORD_INDICATORS = frozenset({
    "canada", "mexico", "uk", "united", "kingdom", "france", "germany", "italy",
    "spain", "australia", "japan", "china", "india", "brazil",
})

# Explicit allowlist for "city, country" intl geocode shortcut (legacy AQI list).
# Do not use utils.is_country_name here — its len>2 heuristic is too broad.
_COUNTRY_TOKENS = frozenset({
    "canada", "mexico", "uk", "united kingdom", "france", "germany", "italy",
    "spain", "australia", "japan", "china", "india", "brazil", "uae", "russia",
    "korea", "thailand", "singapore", "egypt", "turkey", "israel", "south africa",
    "kenya", "nigeria", "argentina", "peru", "chile", "colombia", "venezuela",
    "cuba", "jamaica", "puerto rico", "iceland", "norway", "sweden", "denmark",
    "finland", "poland", "czech republic", "hungary", "romania", "bulgaria",
    "croatia", "serbia", "greece", "portugal", "ireland", "belgium",
    "netherlands", "switzerland", "austria", "monaco", "andorra", "san marino",
    "vatican", "luxembourg", "malta", "cyprus", "albania", "macedonia",
    "montenegro", "bosnia", "slovenia", "slovakia", "lithuania", "latvia",
    "estonia", "belarus", "ukraine", "moldova", "georgia", "armenia",
    "azerbaijan", "kazakhstan", "uzbekistan", "kyrgyzstan", "tajikistan",
    "turkmenistan", "afghanistan", "pakistan", "bangladesh", "sri lanka",
    "nepal", "bhutan", "myanmar", "laos", "cambodia", "vietnam", "malaysia",
    "indonesia", "philippines", "taiwan", "north korea", "south korea",
    "mongolia",
})

GEOCODE_CACHE_CAP = 256

# Bare place → "city, country" (full-string match, including multi-word keys).
INTERNATIONAL_CITIES: dict[str, str] = {
    "beijing": "beijing, china",
    "shanghai": "shanghai, china",
    "tokyo": "tokyo, japan",
    "london": "london, uk",
    "paris": "paris, france",
    "berlin": "berlin, germany",
    "rome": "rome, italy",
    "madrid": "madrid, spain",
    "moscow": "moscow, russia",
    "sydney": "sydney, australia",
    "melbourne": "melbourne, australia",
    "toronto": "toronto, canada",
    "vancouver": "vancouver, canada",
    "mumbai": "mumbai, india",
    "delhi": "delhi, india",
    "bangalore": "bangalore, india",
    "sao paulo": "sao paulo, brazil",
    "rio de janeiro": "rio de janeiro, brazil",
    "mexico city": "mexico city, mexico",
    "cairo": "cairo, egypt",
    "istanbul": "istanbul, turkey",
    "seoul": "seoul, south korea",
    "bangkok": "bangkok, thailand",
    "singapore": "singapore, singapore",
    "hong kong": "hong kong, china",
    "dubai": "dubai, uae",
    "tel aviv": "tel aviv, israel",
    "johannesburg": "johannesburg, south africa",
    "nairobi": "nairobi, kenya",
    "lagos": "lagos, nigeria",
    "buenos aires": "buenos aires, argentina",
    "lima": "lima, peru",
    "santiago": "santiago, chile",
    "bogota": "bogota, colombia",
    "caracas": "caracas, venezuela",
    "havana": "havana, cuba",
    "kingston": "kingston, jamaica",
    "san juan": "san juan, puerto rico",
    "reykjavik": "reykjavik, iceland",
    "oslo": "oslo, norway",
    "stockholm": "stockholm, sweden",
    "copenhagen": "copenhagen, denmark",
    "helsinki": "helsinki, finland",
    "warsaw": "warsaw, poland",
    "prague": "prague, czech republic",
    "budapest": "budapest, hungary",
    "bucharest": "bucharest, romania",
    "sofia": "sofia, bulgaria",
    "zagreb": "zagreb, croatia",
    "belgrade": "belgrade, serbia",
    "athens": "athens, greece",
    "lisbon": "lisbon, portugal",
    "dublin": "dublin, ireland",
    "brussels": "brussels, belgium",
    "amsterdam": "amsterdam, netherlands",
    "zurich": "zurich, switzerland",
    "vienna": "vienna, austria",
    "lucerne": "lucerne, switzerland",
    "geneva": "geneva, switzerland",
    "monaco": "monaco, monaco",
    "andorra": "andorra, andorra",
    "san marino": "san marino, san marino",
    "vatican": "vatican city, vatican",
    "luxembourg": "luxembourg, luxembourg",
    "malta": "valletta, malta",
    "cyprus": "nicosia, cyprus",
    "albania": "tirana, albania",
    "macedonia": "skopje, macedonia",
    "montenegro": "podgorica, montenegro",
    "bosnia": "sarajevo, bosnia",
    "slovenia": "ljubljana, slovenia",
    "slovakia": "bratislava, slovakia",
    "lithuania": "vilnius, lithuania",
    "latvia": "riga, latvia",
    "estonia": "tallinn, estonia",
    "belarus": "minsk, belarus",
    "ukraine": "kiev, ukraine",
    "moldova": "chisinau, moldova",
    "georgia": "tbilisi, georgia",
    "armenia": "yerevan, armenia",
    "azerbaijan": "baku, azerbaijan",
    "kazakhstan": "nur-sultan, kazakhstan",
    "uzbekistan": "tashkent, uzbekistan",
    "kyrgyzstan": "bishkek, kyrgyzstan",
    "tajikistan": "dushanbe, tajikistan",
    "turkmenistan": "ashgabat, turkmenistan",
    "afghanistan": "kabul, afghanistan",
    "pakistan": "islamabad, pakistan",
    "bangladesh": "dhaka, bangladesh",
    "sri lanka": "colombo, sri lanka",
    "nepal": "kathmandu, nepal",
    "bhutan": "thimphu, bhutan",
    "myanmar": "yangon, myanmar",
    "laos": "vientiane, laos",
    "cambodia": "phnom penh, cambodia",
    "vietnam": "hanoi, vietnam",
    "malaysia": "kuala lumpur, malaysia",
    "indonesia": "jakarta, indonesia",
    "philippines": "manila, philippines",
    "taiwan": "taipei, taiwan",
    "north korea": "pyongyang, north korea",
    "mongolia": "ulaanbaatar, mongolia",
}

ZIP_CODE_OVERRIDES: dict[str, str] = {
    "98013": "Vashon, WA, USA",
    "98014": "Vashon Island, WA, USA",
}

US_STATE_ABBRS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "AS", "GU", "MP", "PR", "VI",
})

_NEIGHBORHOODS: dict[str, tuple[str, ...]] = {
    "seattle": (
        "greenwood", "ballard", "capitol hill", "fremont", "queen anne",
        "wallingford", "university district", "pike place", "pioneer square",
        "belltown", "first hill", "central district", "beacon hill", "columbia city",
        "west seattle", "magnolia", "phinney ridge", "crown hill", "loyal heights",
    ),
    "new york": (
        "greenwich village", "soho", "tribeca", "chinatown", "little italy",
        "east village", "west village", "chelsea", "hells kitchen", "upper east side",
        "upper west side", "harlem", "brooklyn heights", "dumbo", "williamsburg",
        "park slope", "red hook", "coney island",
    ),
    "san francisco": (
        "mission district", "haight-ashbury", "castro", "soma", "financial district",
        "north beach", "chinatown", "russian hill", "pacific heights", "marina district",
        "sunset district", "richmond district", "bernal heights", "noe valley",
    ),
    "los angeles": (
        "hollywood", "beverly hills", "santa monica", "venice", "manhattan beach",
        "hermosa beach", "redondo beach", "pasadena", "glendale", "burbank",
        "west hollywood", "culver city", "marina del rey", "playa del rey",
    ),
    "chicago": (
        "loop", "magnificent mile", "gold coast", "lincoln park", "wrigleyville",
        "lakeview", "wicker park", "bucktown", "logan square", "pilsen", "hyde park",
    ),
    "boston": (
        "back bay", "beacon hill", "north end", "south end", "charlestown",
        "east boston", "dorchester", "roxbury", "jamaica plain", "allston",
        "brighton", "cambridge", "somerville",
    ),
    "portland": (
        "pearl district", "alphabet district", "nob hill", "mississippi district",
        "hawthorne", "belmont", "sellwood", "st. johns", "kenton", "overlook",
    ),
}

_NEIGHBORHOOD_PARENT: dict[str, tuple[str, str]] = {
    "seattle": ("Seattle", "WA"),
    "new york": ("New York", "NY"),
    "san francisco": ("San Francisco", "CA"),
    "los angeles": ("Los Angeles", "CA"),
    "chicago": ("Chicago", "IL"),
    "boston": ("Boston", "MA"),
    "portland": ("Portland", "OR"),
}

LabelStyle = Literal["numeric", "query", "abbreviated", "city_region"]


@dataclass(frozen=True)
class ResolveOptions:
    """Opt-in semantics for ``resolve_location``."""

    default_state: Optional[str] = None
    default_country: Optional[str] = None
    timeout: int = 10
    use_international_cities: bool = True
    use_neighborhoods: bool = True
    use_structured_zip: bool = True
    use_region_capitals: bool = False
    use_zippopotam_labels: bool = False
    allow_repeater_names: bool = False
    fallback_coords: Optional[tuple[float, float]] = None
    fallback_label: Optional[str] = None
    include_address_info: bool = True
    label_style: LabelStyle = "abbreviated"


@dataclass(frozen=True)
class ResolvedLocation:
    lat: Optional[float]
    lon: Optional[float]
    location_type: Optional[str]
    query: str
    display_name: Optional[str]
    address_info: Optional[dict]
    error: Optional[str] = None
    error_detail: Optional[str] = None
    region_note: Optional[str] = None


OPTIONS_AQI = ResolveOptions()
OPTIONS_WX = ResolveOptions(
    use_neighborhoods=False, use_structured_zip=False, label_style="city_region",
)
OPTIONS_AURORA = ResolveOptions(
    use_international_cities=False, use_neighborhoods=False, use_structured_zip=False,
    label_style="numeric", include_address_info=False,
)
OPTIONS_RAIN = ResolveOptions(
    use_region_capitals=True, use_zippopotam_labels=True,
    use_international_cities=False, use_neighborhoods=False, use_structured_zip=False,
    label_style="city_region", include_address_info=False,
)
OPTIONS_PREFIX = ResolveOptions(
    allow_repeater_names=True, use_international_cities=False,
    use_neighborhoods=False, use_structured_zip=False,
    label_style="query", include_address_info=False,
)
OPTIONS_SOLARFORECAST = OPTIONS_PREFIX


def titlecase_location(text: str) -> str:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return text.strip()
    out = []
    for i, p in enumerate(parts):
        if i > 0 and len(p) == 2 and p.isalpha():
            out.append(p.upper())
        else:
            out.append(p.title())
    return ", ".join(out)


def city_display_name(typed_location: str, suffix: Optional[str] = None) -> str:
    head = typed_location.split(",")[0].strip()
    if suffix and head.lower().endswith(" " + suffix.lower()):
        head = head[: -len(suffix)].strip()
    tokens = head.split()
    if len(tokens) >= 2 and tokens[-1].upper() in US_STATE_ABBRS:
        head = " ".join(tokens[:-1])
    return titlecase_location(head)


def join_location(city: Optional[str], suffix: Optional[str]) -> str:
    city = (city or "").strip()
    suffix = (suffix or "").strip()
    if not suffix:
        return city
    if not city or city.lower() == suffix.lower():
        return suffix
    return f"{city}, {suffix}"


def reverse_geocode_region(
    bot: Any, lat: float, lon: float, *, timeout: int = 10, logger: Any = None
) -> tuple[Optional[str], Optional[str]]:
    city: Optional[str] = None
    suffix: Optional[str] = None
    try:
        from .utils import get_nominatim_geocoder
        limiter = getattr(bot, "nominatim_rate_limiter", None)
        if limiter is not None:
            # Reserve before the request: this runs on worker threads.
            limiter.wait_and_request_sync()
        geolocator = get_nominatim_geocoder(timeout=timeout)
        result = geolocator.reverse(f"{lat}, {lon}", timeout=timeout, language="en")
        if result is not None and hasattr(result, "raw"):
            address = result.raw.get("address", {})
            city = (
                address.get("city") or address.get("town") or address.get("village")
                or address.get("municipality") or address.get("county") or None
            )
            country_code = (address.get("country_code") or "").lower()
            if country_code == "us":
                iso = address.get("ISO3166-2-lvl4") or address.get("ISO3166-2-lvl6") or ""
                if "-" in iso:
                    suffix = iso.rsplit("-", 1)[-1]
                else:
                    state_abbr, _ = normalize_us_state(address.get("state", ""))
                    suffix = state_abbr or address.get("state") or None
            else:
                suffix = address.get("country") or None
    except Exception as e:
        if logger:
            logger.debug(f"Error reverse geocoding {lat},{lon}: {e}")
    return city, suffix


# Guards the check-then-evict below. These caches are plain dicts reached from
# geocoding, which now runs on worker threads: without this, two concurrent
# evictions raise KeyError or "dictionary changed size during iteration" and the
# exception unwinds all the way out of the command, so the user gets no reply.
_CACHE_PUT_LOCK = threading.Lock()


def cache_put(
    cache: dict, key: Any, value: Any, *, cap: int = GEOCODE_CACHE_CAP
) -> None:
    """Insert into a size-capped cache, evicting the oldest entry when full."""
    with _CACHE_PUT_LOCK:
        if key not in cache and len(cache) >= cap:
            cache.pop(next(iter(cache)))
        cache[key] = value


def zip_to_city_string(
    zipcode: str, *, timeout: int = 10, cache: Optional[dict[str, str]] = None, logger: Any = None
) -> Optional[str]:
    z = zipcode.strip()
    if cache is not None and z in cache:
        return cache[z]
    name: Optional[str] = None
    try:
        resp = requests.get(f"https://api.zippopotam.us/us/{z}", timeout=timeout)
        if resp.ok:
            places = resp.json().get("places") or []
            if places:
                city = (places[0].get("place name") or "").strip()
                st = (places[0].get("state abbreviation") or "").strip()
                if city:
                    name = join_location(city, st)
    except Exception as e:
        if logger:
            logger.debug(f"Zippopotam ZIP lookup failed for {z}: {e}")
    if name and cache is not None:
        cache_put(cache, z, name)
    return name


def parse_coordinates(raw: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) when valid; None for bad format or out-of-range."""
    coords, _error, _detail = parse_coordinates_detailed(raw)
    return coords


def parse_coordinates_detailed(
    raw: str,
) -> tuple[Optional[tuple[float, float]], Optional[str], Optional[str]]:
    """Return ((lat, lon)|None, error_code|None, error_detail|None)."""
    if not COORD_RE.match(raw or ""):
        return None, "invalid_coordinates", None
    try:
        a, b = raw.split(",", 1)
        lat, lon = float(a.strip()), float(b.strip())
    except ValueError:
        return None, "invalid_coordinates", None
    if not (-90 <= lat <= 90):
        return None, "invalid_latitude", str(lat)
    if not (-180 <= lon <= 180):
        return None, "invalid_longitude", str(lon)
    return (lat, lon), None, None


def _rewrite_space_country(location: str) -> str:
    parts = location.split()
    if len(parts) < 2:
        return location
    potential_country = parts[1].lower()
    if potential_country not in _COUNTRY_WORD_INDICATORS:
        return location
    city = parts[0]
    if potential_country in ("united", "kingdom"):
        if len(parts) >= 3 and parts[2].lower() == "kingdom":
            return f"{city}, uk"
        return f"{city}, {parts[1]}"
    return f"{city}, {parts[1]}"


def classify_location(
    raw: str,
    *,
    use_international_cities: bool = True,
) -> tuple[str, str]:
    """Return (normalized_location, location_type)."""
    location = (raw or "").strip()
    if COORD_RE.match(location):
        return location, "coordinates"
    if ZIP_RE.match(location):
        return location.strip(), "zipcode"

    if use_international_cities:
        city_lower = location.lower()
        if city_lower in INTERNATIONAL_CITIES:
            return INTERNATIONAL_CITIES[city_lower], "city"

    location = _rewrite_space_country(location)
    return location, "city"


def get_neighborhood_queries(city: str) -> list[str]:
    city_lower = city.lower().strip()
    for parent_key, names in _NEIGHBORHOODS.items():
        if city_lower in names:
            parent, st = _NEIGHBORHOOD_PARENT[parent_key]
            return [f"{city}, {parent}, {st}, USA", f"{city}, {parent}, USA"]
    return []


def _is_country_token(text: str) -> bool:
    return text.strip().lower() in _COUNTRY_TOKENS


def _address_from_result(bot: Any, lat: float, lon: float, timeout: int) -> dict:
    db = getattr(bot, "db_manager", None)
    reverse_cache_key = f"reverse_{lat}_{lon}"
    if db is not None:
        cached = db.get_cached_json(reverse_cache_key, "geolocation")
        if cached:
            return cached
    try:
        rev = rate_limited_nominatim_reverse_sync(bot, f"{lat}, {lon}", timeout=timeout)
        if rev:
            info = rev.raw.get("address", {}) or {}
            if db is not None:
                db.cache_json(reverse_cache_key, info, "geolocation", cache_hours=720)
            return info
    except Exception:
        pass
    return {}


def _cache_geocode(bot: Any, query: str, lat: float, lon: float) -> None:
    db = getattr(bot, "db_manager", None)
    if db is not None:
        try:
            db.cache_geocoding(query, lat, lon)
        except Exception:
            pass


def geocode_city_best_effort(
    bot: Any,
    city: str,
    *,
    default_state: Optional[str] = None,
    default_country: Optional[str] = None,
    use_neighborhoods: bool = True,
    include_address_info: bool = True,
    timeout: int = 10,
) -> tuple[Optional[float], Optional[float], Optional[dict]]:
    try:
        if "," in city:
            parts = [p.strip() for p in city.split(",")]
            if len(parts) >= 2 and _is_country_token(parts[1]):
                query = f"{parts[0]}, {parts[1]}"
                loc = rate_limited_nominatim_geocode_sync(bot, query, timeout=timeout)
                if loc:
                    _cache_geocode(bot, query, loc.latitude, loc.longitude)
                    addr = _address_from_result(bot, loc.latitude, loc.longitude, timeout)
                    if not addr:
                        addr = loc.raw.get("address", {}) or {}
                    return loc.latitude, loc.longitude, addr if include_address_info else None

        if default_state is None:
            default_state = bot.config.get("Weather", "default_state", fallback="")
        if default_country is None:
            default_country = bot.config.get("Weather", "default_country", fallback="US")

        lat, lon, address_info = geocode_city_sync(
            bot, city, default_state=default_state, default_country=default_country,
            include_address_info=include_address_info, timeout=timeout,
        )
        if lat is not None and lon is not None:
            return lat, lon, address_info or ({} if include_address_info else None)

        if use_neighborhoods:
            for query in get_neighborhood_queries(city):
                loc = rate_limited_nominatim_geocode_sync(bot, query, timeout=timeout)
                if loc:
                    _cache_geocode(bot, query, loc.latitude, loc.longitude)
                    addr = _address_from_result(bot, loc.latitude, loc.longitude, timeout)
                    if not addr:
                        addr = loc.raw.get("address", {}) or {}
                    return loc.latitude, loc.longitude, addr if include_address_info else None
        return None, None, None
    except Exception:
        return None, None, None


def geocode_zipcode_best_effort(
    bot: Any,
    zipcode: str,
    *,
    default_state: Optional[str] = None,
    use_structured_zip: bool = True,
    include_address_info: bool = True,
    timeout: int = 10,
) -> tuple[Optional[float], Optional[float], Optional[dict]]:
    zip_code = zipcode.strip()
    if default_state is None:
        default_state = bot.config.get("Weather", "default_state", fallback="")
    location_result = None
    try:
        if zip_code in ZIP_CODE_OVERRIDES:
            mapped = ZIP_CODE_OVERRIDES[zip_code]
            try:
                result = rate_limited_nominatim_geocode_sync(bot, mapped, timeout=timeout)
                if result and getattr(result, "address", None):
                    location_result = result
            except Exception:
                pass

        if use_structured_zip and not location_result:
            structured_queries: list[Union[str, Mapping[str, str]]] = [
                {"postalcode": zip_code, "country": "US"},
                {"postalcode": zip_code, "state": default_state or "", "country": "US"},
                {"postalcode": zip_code, "countrycode": "US"},
            ]
            for query in structured_queries:
                try:
                    result = rate_limited_nominatim_geocode_sync(bot, query, timeout=timeout)
                    if result and getattr(result, "address", None):
                        addr_l = result.address.lower()
                        if "united states" in addr_l or "usa" in addr_l:
                            if default_state and (
                                default_state in result.address or "washington" in addr_l
                            ):
                                location_result = result
                                break
                            if not location_result:
                                location_result = result
                except Exception:
                    continue

        if location_result:
            lat, lon = location_result.latitude, location_result.longitude
        else:
            lat, lon = geocode_zipcode_sync(bot, zip_code, timeout=timeout)

        if lat is None or lon is None:
            return None, None, None
        address_info = _address_from_result(bot, lat, lon, timeout) if include_address_info else None
        return lat, lon, address_info
    except Exception:
        return None, None, None


def get_bot_lat_lon(bot: Any, logger: Any = None) -> Optional[tuple[float, float]]:
    """[Bot] bot_latitude/bot_longitude when both are set and in range, else None."""
    try:
        lat = bot.config.getfloat("Bot", "bot_latitude", fallback=None)
        lon = bot.config.getfloat("Bot", "bot_longitude", fallback=None)
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except Exception as e:
        if logger is not None:
            logger.debug(f"Error getting bot location: {e}")
    return None


def get_config_default_lat_lon(bot: Any, section: str) -> Optional[tuple[float, float]]:
    try:
        if not bot.config.has_section(section):
            return None
        lat = bot.config.getfloat(section, "default_lat", fallback=None)
        lon = bot.config.getfloat(section, "default_lon", fallback=None)
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except Exception:
        pass
    return None


def get_companion_lat_lon(
    bot: Any, message: Any, logger: Any = None, error_level: str = "debug", trace: bool = False
) -> Optional[tuple[float, float]]:
    """The sender's most recent advertised position from contact tracking, or None.

    A lookup error is logged on ``logger`` at ``error_level`` and returns None.
    With ``trace`` (wx and gwx), the lookup's outcome is also logged at DEBUG.
    """
    try:
        sender_pubkey = getattr(message, "sender_pubkey", None)
        if not sender_pubkey:
            if trace and logger is not None:
                logger.debug("No sender_pubkey in message for companion location lookup")
            return None
        if not hasattr(bot, "db_manager") and not trace:
            return None
        query = """
            SELECT latitude, longitude
            FROM complete_contact_tracking
            WHERE public_key = ?
            AND latitude IS NOT NULL AND longitude IS NOT NULL
            AND latitude != 0 AND longitude != 0
            ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
            LIMIT 1
        """
        results = bot.db_manager.execute_query(query, (sender_pubkey,))
        if results:
            row = results[0]
            lat, lon = float(row["latitude"]), float(row["longitude"])
            if trace and logger is not None:
                logger.debug(f"Found companion location: {lat}, {lon} for pubkey {sender_pubkey[:16]}...")
            return (lat, lon)
        if trace and logger is not None:
            logger.debug(f"No location found in database for pubkey {sender_pubkey[:16]}...")
    except Exception as e:
        if logger is not None:
            getattr(logger, error_level)(f"Error getting companion location: {e}")
    return None


def lookup_repeater_lat_lon(
    bot: Any, repeater_name: str
) -> Optional[tuple[float, float, str]]:
    try:
        if not hasattr(bot, "db_manager"):
            return None
        query = """
            SELECT latitude, longitude, name
            FROM complete_contact_tracking
            WHERE role IN ('repeater', 'roomserver')
            AND latitude IS NOT NULL AND longitude IS NOT NULL
            AND latitude != 0 AND longitude != 0
            AND LOWER(name) LIKE LOWER(?)
            ORDER BY
                CASE
                    WHEN LOWER(name) = LOWER(?) THEN 1
                    WHEN LOWER(name) LIKE LOWER(?) THEN 2
                    ELSE 3
                END,
                COALESCE(last_advert_timestamp, last_heard) DESC
            LIMIT 1
        """
        exact = repeater_name.strip()
        results = bot.db_manager.execute_query(
            query, (f"%{exact}%", exact, f"{exact}%")
        )
        if results:
            row = results[0]
            lat, lon = row.get("latitude"), row.get("longitude")
            if lat is not None and lon is not None:
                return float(lat), float(lon), row.get("name") or exact
    except Exception:
        pass
    return None


def _display_from_address(
    address_info: Optional[dict],
    *,
    query: str,
    default_state: Optional[str],
    label_style: LabelStyle,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    bot: Any = None,
    timeout: int = 10,
    zippopotam: bool = False,
    location_type: Optional[str] = None,
) -> Optional[str]:
    if label_style == "numeric" and lat is not None and lon is not None:
        return f"{lat:.1f},{lon:.1f}"
    if label_style == "query":
        return query
    if label_style == "city_region":
        if location_type == "zipcode" and zippopotam:
            zname = zip_to_city_string(query, timeout=timeout)
            if zname:
                return f"{zname} ({query.strip()})"
        if lat is not None and lon is not None and bot is not None:
            city, suffix = reverse_geocode_region(bot, lat, lon, timeout=timeout)
            if location_type == "city":
                typed = city_display_name(query, suffix)
                return join_location(typed, suffix) or query
            if city:
                return join_location(city, suffix)
        return query
    if address_info:
        city = (
            address_info.get("city") or address_info.get("town")
            or address_info.get("village") or address_info.get("hamlet")
            or address_info.get("municipality") or query
        )
        country = address_info.get("country", "")
        state = address_info.get("state", "")
        if country in ("United States", "US", "United States of America"):
            abbr, _ = normalize_us_state(state) if state else (None, None)
            suffix = abbr or state or default_state or ""
        else:
            suffix = country or address_info.get("province") or default_state or ""
            if suffix in ("United States", "United States of America"):
                suffix = "USA"
        full = f"{city}, {suffix}" if suffix else str(city)
        return abbreviate_location(full, max_length=30)
    if label_style == "abbreviated" and location_type == "coordinates" and lat is not None and lon is not None:
        return f"{lat:.3f},{lon:.3f}"
    if location_type == "zipcode":
        return query.strip()
    return query


def resolve_location(
    bot: Any,
    raw: Optional[str],
    *,
    options: Optional[ResolveOptions] = None,
) -> ResolvedLocation:
    opts = options or OPTIONS_AQI
    default_state = opts.default_state
    default_country = opts.default_country
    if default_state is None:
        default_state = bot.config.get("Weather", "default_state", fallback="")
    if default_country is None:
        default_country = bot.config.get("Weather", "default_country", fallback="US")

    # Declared up front: the fallback/coordinates/repeater branches bind plain
    # floats while the zipcode/city branches bind float | None from best-effort
    # geocoding, so one Optional type keeps mypy from pinning the first binding.
    lat: Optional[float]
    lon: Optional[float]

    if raw is None or not str(raw).strip():
        if opts.fallback_coords:
            lat, lon = opts.fallback_coords
            label = opts.fallback_label or _display_from_address(
                None, query=f"{lat},{lon}", default_state=default_state,
                label_style=opts.label_style, lat=lat, lon=lon, bot=bot,
                timeout=opts.timeout,
            )
            return ResolvedLocation(
                lat=lat, lon=lon, location_type="fallback", query="",
                display_name=label, address_info=None,
            )
        return ResolvedLocation(
            lat=None, lon=None, location_type=None, query="",
            display_name=None, address_info=None, error="no_location",
        )

    text = str(raw).strip()
    if opts.allow_repeater_names:
        hit = lookup_repeater_lat_lon(bot, text)
        if hit:
            lat, lon, name = hit
            return ResolvedLocation(
                lat=lat, lon=lon, location_type="repeater", query=name,
                display_name=name, address_info=None,
            )

    region_note: Optional[str] = None
    query, location_type = classify_location(text, use_international_cities=False)

    if location_type == "city":
        if opts.use_region_capitals:
            cap = region_capital_query(query)
            if cap:
                query = cap
                region_note = REGION_DEFAULT_NOTE
        if opts.use_international_cities:
            query, _ = classify_location(query, use_international_cities=True)

    if location_type == "coordinates":
        parsed, err, detail = parse_coordinates_detailed(query)
        if err or not parsed:
            return ResolvedLocation(
                lat=None, lon=None, location_type="coordinates", query=query,
                display_name=None, address_info=None,
                error=err or "invalid_coordinates", error_detail=detail,
            )
        lat, lon = parsed
        display = _display_from_address(
            None, query=query, default_state=default_state,
            label_style=opts.label_style, lat=lat, lon=lon, bot=bot,
            timeout=opts.timeout, location_type="coordinates",
        )
        return ResolvedLocation(
            lat=lat, lon=lon, location_type="coordinates", query=query,
            display_name=display, address_info=None, region_note=region_note,
        )

    if location_type == "zipcode":
        lat, lon, address_info = geocode_zipcode_best_effort(
            bot, query, default_state=default_state,
            use_structured_zip=opts.use_structured_zip,
            include_address_info=opts.include_address_info, timeout=opts.timeout,
        )
        if lat is None or lon is None:
            return ResolvedLocation(
                lat=None, lon=None, location_type="zipcode", query=query,
                display_name=None, address_info=None, error="no_location_zipcode",
            )
        display = _display_from_address(
            address_info, query=query, default_state=default_state,
            label_style=opts.label_style, lat=lat, lon=lon, bot=bot,
            timeout=opts.timeout, zippopotam=opts.use_zippopotam_labels,
            location_type="zipcode",
        )
        return ResolvedLocation(
            lat=lat, lon=lon, location_type="zipcode", query=query.strip(),
            display_name=display, address_info=address_info, region_note=region_note,
        )

    lat, lon, address_info = geocode_city_best_effort(
        bot, query, default_state=default_state, default_country=default_country,
        use_neighborhoods=opts.use_neighborhoods,
        include_address_info=opts.include_address_info, timeout=opts.timeout,
    )
    if lat is None or lon is None:
        return ResolvedLocation(
            lat=None, lon=None, location_type="city", query=query,
            display_name=None, address_info=None, error="no_location_city",
            region_note=region_note,
        )
    display = _display_from_address(
        address_info, query=query, default_state=default_state,
        label_style=opts.label_style, lat=lat, lon=lon, bot=bot,
        timeout=opts.timeout, location_type="city",
    )
    return ResolvedLocation(
        lat=lat, lon=lon, location_type="city", query=query,
        display_name=display, address_info=address_info, region_note=region_note,
    )


async def resolve_location_async(
    bot: Any,
    raw: Optional[str],
    *,
    options: Optional[ResolveOptions] = None,
) -> ResolvedLocation:
    """Async twin of ``resolve_location`` (same behavior via thread offload)."""
    opts = options or OPTIONS_PREFIX
    return await asyncio.to_thread(resolve_location, bot, raw, options=opts)


def resolve_with_message_fallbacks(
    bot: Any,
    raw: Optional[str],
    message: Any,
    *,
    config_section: Optional[str] = None,
    options: Optional[ResolveOptions] = None,
) -> ResolvedLocation:
    opts = options or OPTIONS_AQI
    if raw is None or not str(raw).strip():
        for coords in (
            get_companion_lat_lon(bot, message),
            get_config_default_lat_lon(bot, config_section) if config_section else None,
            get_bot_lat_lon(bot),
        ):
            if coords:
                return resolve_location(bot, None, options=replace(opts, fallback_coords=coords))
        return ResolvedLocation(
            lat=None, lon=None, location_type=None, query="",
            display_name=None, address_info=None, error="no_location",
        )
    return resolve_location(bot, raw, options=opts)
