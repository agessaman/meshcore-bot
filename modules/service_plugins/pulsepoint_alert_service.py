#!/usr/bin/env python3
"""
PulsePoint alert service for the MeshCore Bot.

Provides fire/EMS incident alerts from the PulsePoint API, both in query mode
(via the ``alert`` command) and polling mode (auto-posting new incidents to a
channel). This is a concrete :class:`BaseAlertService` implementation and the
reference example for building new alert services.

The bulk of the API-specific logic (encrypted response handling, query
parsing, incident ranking, compact formatting) was migrated from the original
``modules/commands/alert_command.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..utils import (
    calculate_distance,
    geocode_city_sync,
    geocode_zipcode_sync,
    rate_limited_nominatim_reverse_sync,
)
from .base_alert_service import (
    QUERY_TYPE_CITY,
    QUERY_TYPE_COORDINATES,
    QUERY_TYPE_COUNTY,
    QUERY_TYPE_STREET_CITY,
    QUERY_TYPE_ZIPCODE,
    BaseAlertService,
)

# Incident type codes -> human readable (short versions for mesh)
CALL_TYPES = {
    "AA": "Auto Aid", "MU": "Mutual Aid", "ST": "Strike Team",
    "AC": "Aircraft Crash", "AE": "Aircraft Emerg", "AES": "Aircraft Standby", "LZ": "Landing Zone",
    "AED": "AED Alarm", "OA": "Alarm", "CMA": "CO Alarm", "FA": "Fire Alarm",
    "MA": "Manual Alarm", "SD": "Smoke Detector", "TRBL": "Trouble Alarm", "WFA": "Waterflow",
    "FL": "Flooding", "LR": "Ladder Req", "LA": "Lift Assist",
    "PA": "Police Assist", "PS": "Public Svc", "SH": "Hydrant",
    "EX": "Explosion", "PE": "Pipeline Emerg", "TE": "Transformer",
    "AF": "Appliance Fire", "CHIM": "Chimney Fire", "CF": "Commercial Fire",
    "WSF": "Structure Fire", "WVEG": "Veg Fire", "CB": "Controlled Burn",
    "ELF": "Electrical Fire", "EF": "Extinguished", "FIRE": "Fire",
    "FULL": "Full Assignment", "IF": "Illegal Fire", "MF": "Marine Fire",
    "OF": "Outside Fire", "PF": "Pole Fire", "GF": "Garbage Fire",
    "RF": "Residential Fire", "SF": "Structure Fire", "VEG": "Veg Fire",
    "VF": "Vehicle Fire", "WCF": "Working Comm Fire", "WRF": "Working Res Fire",
    "BT": "Bomb Threat", "EE": "Electrical Emerg", "EM": "Emergency",
    "ER": "Emergency", "GAS": "Gas Leak", "HC": "Hazmat",
    "HMR": "Hazmat", "TD": "Tree Down", "WE": "Water Emerg",
    "AI": "Arson Inv", "HMI": "Hazmat Inv", "INV": "Investigation",
    "OI": "Odor Inv", "SI": "Smoke Inv",
    "LO": "Lockout", "CL": "Comm Lockout", "RL": "Res Lockout", "VL": "Vehicle Lockout",
    "IFT": "Med Transfer", "ME": "Medical", "MCI": "Mass Casualty",
    "EQ": "Earthquake", "FLW": "Flood Warn", "TOW": "Tornado Warn", "TSW": "Tsunami Warn",
    "CA": "Community", "FW": "Fire Watch", "NO": "Notification",
    "STBY": "Standby", "TEST": "Test", "TRNG": "Training", "UNK": "Unknown",
    "AR": "Animal Rescue", "CR": "Cliff Rescue", "CSR": "Confined Space",
    "ELR": "Elevator Rescue", "RES": "Rescue", "RR": "Rope Rescue",
    "TR": "Tech Rescue", "TNR": "Trench Rescue", "USAR": "Urban SAR",
    "VS": "Vessel Sinking", "WR": "Water Rescue",
    "TCE": "Major TC", "RTE": "Train Emerg",
    "TC": "Traffic Collision", "TCS": "TC w/Structure", "TCT": "TC w/Train",
    "WA": "Wires Arcing", "WD": "Wires Down"
}

# Unit dispatch status codes
UNIT_STATUS = {
    "DP": "Dispatched",
    "ER": "Enroute",
    "OS": "On Scene",
    "AV": "Available",
    "TR": "Transport",
    "TA": "Arrived",
    "CL": "Cleared"
}

# County alias short codes (kept for backward compatibility with the original
# alert command). Maps a short query token to a configured county key.
COUNTY_ALIASES = {
    "sno": "snohomish",
    "sea": "king",
    "tac": "pierce",
    "all": "puget_sound",
}

def _derive_key(salt: bytes) -> bytes:
    """Derive AES key from the obfuscated password.

    Args:
        salt: The salt bytes to use for derivation.

    Returns:
        bytes: The derived 32-byte key.
    """
    e = "CommonIncidents"
    password = e[13] + e[1] + e[2] + "brady" + "5" + "r" + e.lower()[6] + e[5] + "gs"

    hasher = hashlib.md5()
    key = b''
    block = None
    while len(key) < 32:
        if block:
            hasher.update(block)
        hasher.update(password.encode())
        hasher.update(salt)
        block = hasher.digest()
        hasher = hashlib.md5()
        key += block
    return key[:32]


def _decrypt(data: dict) -> dict:
    """Decrypt PulsePoint's encrypted response.

    Args:
        data: The encrypted data dictionary from the API.

    Returns:
        dict: The decrypted JSON data.
    """
    ct = base64.b64decode(data["ct"])
    iv = bytes.fromhex(data["iv"])
    salt = bytes.fromhex(data["s"])

    key = _derive_key(salt)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    out = decryptor.update(ct) + decryptor.finalize()

    out = out[1:out.rindex(b'"')].decode()
    out = out.replace(r'\"', r'"')
    return json.loads(out)


def _parse_time(iso_str: str) -> Optional[datetime]:
    """Parse ISO timestamp to datetime and convert to local time.

    Args:
        iso_str: ISO formatted timestamp string.

    Returns:
        Optional[datetime]: Parsed timezone-aware datetime, or None if invalid.
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        # Convert to local time if timezone-aware
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt
    except Exception:
        return None


def _time_ago(dt: Optional[datetime]) -> str:
    """Format datetime as relative time string (e.g., '5m ago').

    Args:
        dt: The datetime to compare against current time.

    Returns:
        str: Relative time string.
    """
    if not dt:
        return ""

    # Use local time for comparison
    now = datetime.now().astimezone()
    # Ensure dt is timezone-aware (should be after _parse_time conversion)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    diff = now - dt
    mins = int(diff.total_seconds() / 60)

    if mins < 1:
        return "now"
    elif mins < 60:
        return f"{mins}m ago"
    elif mins < 1440:
        return f"{mins // 60}h {mins % 60}m ago"
    else:
        return f"{mins // 1440}d ago"


class PulsePointAlertService(BaseAlertService):
    """Alert service backed by the PulsePoint incident API.

    Supports city, county, zipcode, coordinate and street+city queries, plus
    an optional polling mode that posts newly-dispatched incidents to one or
    more channels.
    """

    alert_service_id = "pulsepoint"
    config_section = "PulsePoint_Alert_Service"
    description = "Fire/EMS incident alerts from the PulsePoint API"

    # Web-viewer settings schema (see modules/settings_schema.py).
    # PulsePoint agency.* keys are dynamic and appear under "Other config values".
    settings_schema = [
        {"key": "enabled", "label": "Enabled", "type": "bool", "default": False,
         "help": "Enable the PulsePoint alert service."},
        {"key": "label", "label": "Service label", "type": "str", "default": "PP",
         "help": "Short label (<=6 chars) prefixed to messages, e.g. [PP]."},
        {"key": "max_distance_km", "label": "Max distance", "type": "float",
         "min": 0, "default": 20.0, "unit": "km",
         "help": "Only show incidents within this distance of the location."},
        {"key": "max_incident_age_hours", "label": "Max incident age", "type": "float",
         "min": 0, "default": 24.0, "unit": "hours",
         "help": "Ignore incidents older than this many hours."},
        {"key": "polling_enabled", "label": "Enable polling", "type": "bool",
         "default": False,
         "help": "Periodically post new incidents to polling_channels."},
        {"key": "polling_interval", "label": "Poll interval", "type": "int",
         "min": 30000, "default": 300000, "unit": "ms",
         "help": "How often to poll for new incidents, in milliseconds."},
        {"key": "polling_channels", "label": "Polling channels", "type": "str",
         "default": "",
         "help": "Comma-separated channels to post new incidents to (e.g. #alerts)."},
    ]

    # Web-viewer dynamic editor for the PulsePoint agency.* keys in this section.
    settings_dynamic_sections = [
        {
            "section": "PulsePoint_Alert_Service",
            "key_prefix": "agency.",
            "label": "PulsePoint agencies",
            "help": ("Map a region name to its PulsePoint agency IDs. Users query "
                     "with 'alert <region>'. Find IDs at web.pulsepoint.org."),
            "key_label": "Region name",
            "value_label": "Agency IDs (comma-separated)",
            "key_placeholder": "agency.city.seattle",
            "value_placeholder": "1234,5678",
        }
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.url_timeout = 10

        # Load agencies from config (separate cities and counties)
        self.city_agencies, self.county_agencies = self._load_agencies()

        # Max distance in km (default 20km, about 12 miles) and max incident age.
        self.max_distance_km = self.bot.config.getfloat(
            self.config_section, "max_distance_km", fallback=20.0
        )
        self.max_incident_age_hours = self.bot.config.getfloat(
            self.config_section, "max_incident_age_hours", fallback=24.0
        )

        # For polling: only report incidents dispatched at or after service start,
        # so the first poll does not dump every currently-active incident.
        self._poll_start_time = datetime.now(timezone.utc)

    def get_capabilities(self) -> dict[str, bool]:
        """PulsePoint understands every standard location query type."""
        return {
            QUERY_TYPE_COORDINATES: True,
            QUERY_TYPE_ZIPCODE: True,
            QUERY_TYPE_CITY: True,
            QUERY_TYPE_COUNTY: True,
            QUERY_TYPE_STREET_CITY: True,
        }

    # ------------------------------------------------------------------ #
    # Agency configuration.
    # ------------------------------------------------------------------ #
    def _load_agencies(self) -> tuple[dict[str, str], dict[str, str]]:
        """Load agency IDs from config, separating cities and counties.

        Reads ``agency.city.*`` / ``agency.county.*`` keys (plus legacy
        ``agency.*`` and ``agency_*`` forms, treated as counties) from this
        service's config section.

        Returns:
            Tuple[Dict[str, str], Dict[str, str]]: (cities_map, counties_map).
        """
        cities: dict[str, str] = {}
        counties: dict[str, str] = {}
        section = self.config_section
        if self.bot.config.has_section(section):
            for key, value in self.bot.config.items(section):
                if key.startswith('agency.city.'):
                    city = key.replace('agency.city.', '').lower()
                    cities[city] = value.strip()
                elif key.startswith('agency.county.'):
                    county = key.replace('agency.county.', '').lower()
                    counties[county] = value.strip()
                # Legacy format support: agency.* (treat as county)
                elif key.startswith('agency.'):
                    name = key.replace('agency.', '').lower()
                    counties[name] = value.strip()
                # Old format: agency_* (treat as county)
                elif key.startswith('agency_'):
                    name = key.replace('agency_', '').lower()
                    counties[name] = value.strip()
        return cities, counties

    def _normalize_location_key(self, location: str) -> str:
        """Normalize location name to match config key format (spaces -> underscores)."""
        return location.lower().replace(' ', '_')

    def _get_agency_ids(self, location: Optional[str] = None,
                        location_type: Optional[str] = None) -> Optional[str]:
        """Get agency IDs for a city or county, or default to all configured agencies.

        Args:
            location: Name of the city or county.
            location_type: Type of location ('city' or 'county').

        Returns:
            Optional[str]: Comma-separated agency IDs, or None if a specific
                location was requested but not found.
        """
        if location:
            location_lower = location.lower()
            location_normalized = self._normalize_location_key(location)

            if location_type == "city":
                if location_normalized in self.city_agencies:
                    return self.city_agencies[location_normalized]
                if location_lower in self.city_agencies:
                    return self.city_agencies[location_lower]
                return None
            elif location_type == "county":
                if location_normalized in self.county_agencies:
                    return self.county_agencies[location_normalized]
                if location_lower in self.county_agencies:
                    return self.county_agencies[location_lower]
                if location_lower in COUNTY_ALIASES:
                    alias_target = COUNTY_ALIASES[location_lower]
                    if alias_target in self.county_agencies:
                        return self.county_agencies[alias_target]
                return None

            # No location_type specified: check cities first, then counties.
            if location_normalized in self.city_agencies:
                return self.city_agencies[location_normalized]
            if location_lower in self.city_agencies:
                return self.city_agencies[location_lower]
            if location_normalized in self.county_agencies:
                return self.county_agencies[location_normalized]
            if location_lower in self.county_agencies:
                return self.county_agencies[location_lower]
            if location_lower in COUNTY_ALIASES:
                alias_target = COUNTY_ALIASES[location_lower]
                if alias_target in self.county_agencies:
                    return self.county_agencies[alias_target]

        # Default: combine all configured agencies from both cities and counties.
        all_agencies = list(self.city_agencies.values()) + list(self.county_agencies.values())
        if not all_agencies:
            return None
        return ",".join(all_agencies)

    # ------------------------------------------------------------------ #
    # Incident fetching.
    # ------------------------------------------------------------------ #
    def _fetch_incidents(self, agency_ids: str) -> list[dict]:
        """Fetch active incidents from PulsePoint.

        Args:
            agency_ids: Comma-separated string of agency IDs.

        Returns:
            List[Dict]: List of incident dictionaries.
        """
        url = "https://api.pulsepoint.org/v1/webapp"
        params = {"resource": "incidents", "agencyid": agency_ids}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://web.pulsepoint.org",
            "Referer": "https://web.pulsepoint.org/"
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self.url_timeout)
            resp.raise_for_status()

            encrypted = resp.json()
            decrypted = _decrypt(encrypted)

            incidents = []
            seen_ids = set()  # Track incident IDs to avoid duplicates

            # Only fetch active incidents (not recent/cleared).
            # Filter by age to exclude very old "active" incidents.
            now = datetime.now(timezone.utc)
            max_age = self.max_incident_age_hours * 3600  # hours -> seconds

            for inc in decrypted.get("incidents", {}).get("active", []):
                incident_id = inc.get("ID")

                if incident_id in seen_ids:
                    continue
                seen_ids.add(incident_id)

                call_type = inc.get("PulsePointIncidentCallType", "UNK")
                call_time = _parse_time(inc.get("CallReceivedDateTime"))

                # Filter out incidents older than max_incident_age_hours.
                if call_time:
                    if call_time.tzinfo is None:
                        call_time = call_time.replace(tzinfo=timezone.utc)
                    else:
                        call_time = call_time.astimezone(timezone.utc)

                    age_seconds = (now - call_time).total_seconds()
                    if age_seconds > max_age:
                        continue

                # Parse units with status
                units = []
                for u in inc.get("Unit", []):
                    unit_id = u.get("UnitID", "?")
                    status = u.get("PulsePointDispatchStatus", "?")
                    units.append({
                        "id": unit_id,
                        "status_code": status,
                        "status": UNIT_STATUS.get(status, status)
                    })

                # Parse address
                full_addr = inc.get("FullDisplayAddress", "Unknown")
                if ", " in full_addr:
                    addr_parts = full_addr.split(", ", 1)
                elif "," in full_addr:
                    addr_parts = full_addr.split(",", 1)
                else:
                    addr_parts = [full_addr]
                street = addr_parts[0].strip()
                city = addr_parts[1].strip() if len(addr_parts) > 1 else ""

                incidents.append({
                    "id": incident_id,
                    "type_code": call_type,
                    "type": CALL_TYPES.get(call_type, call_type),
                    "address": full_addr,
                    "street": street,
                    "city": city,
                    "latitude": float(inc.get("Latitude", 0)),
                    "longitude": float(inc.get("Longitude", 0)),
                    "agency": inc.get("AgencyID"),
                    "time": call_time,
                    "time_ago": _time_ago(call_time),
                    "units": units,
                    "unit_ids": [u["id"] for u in units],
                    "raw": inc
                })

            return incidents
        except Exception as e:
            self.logger.error(f"Error fetching PulsePoint incidents: {e}")
            return []

    # ------------------------------------------------------------------ #
    # Query parsing.
    # ------------------------------------------------------------------ #
    def parse_query(self, query: str) -> tuple[str, Optional[str], Optional[float], Optional[float]]:
        """Parse query string to determine search type.

        Args:
            query: The raw query string from the user.

        Returns:
            Tuple of (query_type, location, lat, lon). query_type is one of:
            "zipcode", "coordinates", "street_city", "city", "county".
        """
        query = query.strip()

        # Coordinates (lat,lon or lat, lon)
        coord_match = re.match(r'^(-?\d+\.?\d*),?\s*(-?\d+\.?\d*)$', query)
        if coord_match:
            try:
                lat = float(coord_match.group(1))
                lon = float(coord_match.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return (QUERY_TYPE_COORDINATES, None, lat, lon)
            except Exception:
                pass

        # Zipcode (5 digits)
        if re.match(r'^\d{5}$', query):
            return (QUERY_TYPE_ZIPCODE, query, None, None)

        # Street + city pattern (e.g. "178th seattle", "main seattle").
        parts = query.split()
        if len(parts) >= 2:
            first_word = parts[0].lower()

            looks_like_street = (
                bool(re.search(r'\d+(st|nd|rd|th)$', first_word)) or
                first_word in ['ne', 'nw', 'se', 'sw', 'n', 's', 'e', 'w'] or
                first_word.endswith(('st', 'nd', 'rd', 'th'))
            )

            if looks_like_street:
                for i in range(1, len(parts)):
                    street_part = ' '.join(parts[:i])
                    city_part = ' '.join(parts[i:])
                    city_lower = city_part.lower()
                    if (city_lower in self.city_agencies or
                        city_lower in self.county_agencies or
                        city_lower in COUNTY_ALIASES or
                        not city_lower.endswith(('st', 'street', 'ave', 'avenue', 'rd', 'road', 'blvd', 'boulevard',
                                                'dr', 'drive', 'ct', 'court', 'ln', 'lane', 'way', 'pl', 'place'))):
                        return (QUERY_TYPE_STREET_CITY, f"{street_part} {city_part}", None, None)

                # No good split found: first word is street, rest is city.
                street_part = parts[0]
                city_part = ' '.join(parts[1:])
                return (QUERY_TYPE_STREET_CITY, f"{street_part} {city_part}", None, None)

        # Known county alias short codes.
        query_lower = query.lower()
        if query_lower in COUNTY_ALIASES:
            return (QUERY_TYPE_COUNTY, query, None, None)

        query_normalized = self._normalize_location_key(query)

        # Configured city name (check cities first).
        if query_normalized in self.city_agencies or query_lower in self.city_agencies:
            return (QUERY_TYPE_CITY, query, None, None)

        # Configured county name.
        if query_normalized in self.county_agencies or query_lower in self.county_agencies:
            return (QUERY_TYPE_COUNTY, query, None, None)

        # Default: treat as city name (single- and multi-word).
        return (QUERY_TYPE_CITY, query, None, None)

    # ------------------------------------------------------------------ #
    # Matching / ranking helpers.
    # ------------------------------------------------------------------ #
    def _match_street_name(self, incidents: list[dict], street_query: str) -> tuple[list[dict], list[dict]]:
        """Split incidents into matched and unmatched by street name."""
        street_lower = street_query.lower().strip()
        matched = []
        unmatched = []
        for inc in incidents:
            street = inc.get("street", "").lower()
            if street_lower in street:
                matched.append(inc)
            else:
                unmatched.append(inc)
        return matched, unmatched

    def _matches_city(self, inc: dict, city_query: str) -> bool:
        """Check if incident matches the city name by substring on the address field."""
        city_query_lower = city_query.lower().strip()
        address = inc.get("address", "").lower().strip()
        return city_query_lower in address

    def _get_city_match_priority(self, inc: dict, city_query: str) -> int:
        """Get priority score for city match (higher = better match).

        Priority 2: city appears at the end of the address (most reliable).
        Priority 1: city appears anywhere in the address.
        Priority 0: no match.
        """
        city_query_lower = city_query.lower().strip()
        address = inc.get("address", "").lower().strip()
        city_query_clean = city_query_lower.split(',')[0].strip()

        end_pattern = r',\s*' + re.escape(city_query_clean) + r'(?:\s*,\s*[A-Z]{2})?$'
        if re.search(end_pattern, address, re.IGNORECASE):
            return 2
        if city_query_clean in address:
            return 1
        return 0

    def _match_city_name(self, incidents: list[dict], city_query: str) -> tuple[list[dict], list[dict]]:
        """Split incidents into matched and unmatched by city name."""
        matched = []
        unmatched = []
        for inc in incidents:
            if self._matches_city(inc, city_query):
                matched.append(inc)
            else:
                unmatched.append(inc)
        return matched, unmatched

    def _has_valid_coordinates(self, inc: dict) -> bool:
        """Check if incident has valid, non-zero coordinates."""
        inc_lat = inc.get("latitude", 0)
        inc_lon = inc.get("longitude", 0)
        return (inc_lat != 0.0 and inc_lon != 0.0 and
                -90 <= inc_lat <= 90 and -180 <= inc_lon <= 180)

    def _sort_by_time(self, incidents: list[dict]) -> list[dict]:
        """Sort incidents by time (most recent first)."""
        def get_time_key(inc):
            time = inc.get("time")
            if time is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if time.tzinfo is None:
                time = time.replace(tzinfo=timezone.utc)
            return time
        return sorted(incidents, key=get_time_key, reverse=True)

    def _sort_by_distance(self, incidents: list[dict], lat: float, lon: float,
                          max_distance: Optional[float] = None) -> list[dict]:
        """Sort incidents by distance from given coordinates (closest first)."""
        scored = []
        for inc in incidents:
            if not self._has_valid_coordinates(inc):
                continue
            distance = calculate_distance(lat, lon, inc.get("latitude", 0), inc.get("longitude", 0))
            inc["_distance"] = distance
            if max_distance is None or distance <= max_distance:
                scored.append(inc)
        return sorted(scored, key=lambda x: x.get("_distance", float('inf')))

    def _sort_by_distance_then_time(self, incidents: list[dict], lat: float, lon: float,
                                    max_distance: Optional[float] = None) -> list[dict]:
        """Sort by distance first, then by time (most recent first) within same distance."""
        scored = []
        for inc in incidents:
            if not self._has_valid_coordinates(inc):
                continue
            distance = calculate_distance(lat, lon, inc.get("latitude", 0), inc.get("longitude", 0))
            inc["_distance"] = distance
            if max_distance is None or distance <= max_distance:
                time = inc.get("time")
                if time is None:
                    time_key = datetime.min.replace(tzinfo=timezone.utc)
                else:
                    if time.tzinfo is None:
                        time = time.replace(tzinfo=timezone.utc)
                    time_key = time
                inc["_time_key"] = time_key
                scored.append(inc)
        return sorted(scored, key=lambda x: (x.get("_distance", float('inf')),
                                             -x.get("_time_key", datetime.min).timestamp()))

    def _rank_incidents(
        self,
        query_type: str,
        query: str,
        location: Optional[str],
        lat: Optional[float],
        lon: Optional[float],
        incidents: list[dict],
    ) -> list[dict]:
        """Filter and rank incidents for the query (runs off the event loop).

        The zipcode and street_city branches geocode over blocking HTTP, so
        this is called via ``asyncio.to_thread`` rather than inline.
        """
        if query_type == QUERY_TYPE_COORDINATES:
            incidents = self._sort_by_distance_then_time(incidents, lat, lon, max_distance=self.max_distance_km)
        elif query_type == QUERY_TYPE_ZIPCODE:
            zip_lat, zip_lon = geocode_zipcode_sync(self.bot, location)
            zip_city = None

            if zip_lat and zip_lon:
                try:
                    reverse_location = rate_limited_nominatim_reverse_sync(self.bot, f"{zip_lat}, {zip_lon}", timeout=10)
                    if reverse_location and reverse_location.raw:
                        address = reverse_location.raw.get('address', {})
                        zip_city = (address.get('city') or
                                   address.get('town') or
                                   address.get('village') or
                                   address.get('hamlet') or
                                   address.get('municipality') or
                                   address.get('suburb') or '')
                        if zip_city:
                            zip_city = zip_city.lower().strip()
                            self.logger.debug(f"Zipcode {location} maps to city: {zip_city}")
                except Exception as e:
                    self.logger.debug(f"Error getting city from zipcode: {e}")

                with_coords = [inc for inc in incidents if self._has_valid_coordinates(inc)]
                without_coords = [inc for inc in incidents if not self._has_valid_coordinates(inc)]

                if zip_city:
                    matched_coords, _ = self._match_city_name(with_coords, zip_city)
                    matched_no_coords, _ = self._match_city_name(without_coords, zip_city)
                    matched_coords = self._sort_by_distance_then_time(matched_coords, zip_lat, zip_lon, max_distance=None)
                    matched_no_coords = self._sort_by_time(matched_no_coords)

                    if len(matched_coords) > 0 or len(matched_no_coords) > 0:
                        incidents = matched_coords + matched_no_coords
                    else:
                        nearby_coords = self._sort_by_distance_then_time(with_coords, zip_lat, zip_lon, max_distance=self.max_distance_km)
                        nearby_no_coords = self._sort_by_time(without_coords)
                        incidents = nearby_coords + nearby_no_coords
                else:
                    incidents = self._sort_by_distance_then_time(incidents, zip_lat, zip_lon, max_distance=self.max_distance_km)
        elif query_type == QUERY_TYPE_STREET_CITY:
            parts = location.split(None, 1)
            if len(parts) == 2:
                street_query, city_query = parts
                result = geocode_city_sync(self.bot, city_query, include_address_info=False)
                city_lat, city_lon = None, None
                if len(result) >= 2:
                    city_lat, city_lon = result[0], result[1]

                with_coords = [inc for inc in incidents if self._has_valid_coordinates(inc)]
                without_coords = [inc for inc in incidents if not self._has_valid_coordinates(inc)]

                if city_lat and city_lon:
                    with_coords = self._sort_by_distance(with_coords, city_lat, city_lon, max_distance=self.max_distance_km)
                    matched_street_coords, unmatched_street_coords = self._match_street_name(with_coords, street_query)
                    matched_street_coords = self._sort_by_distance_then_time(matched_street_coords, city_lat, city_lon, max_distance=self.max_distance_km)
                    unmatched_street_coords = self._sort_by_distance_then_time(unmatched_street_coords, city_lat, city_lon, max_distance=self.max_distance_km)
                    with_coords = matched_street_coords + unmatched_street_coords
                else:
                    matched_street_coords, unmatched_street_coords = self._match_street_name(with_coords, street_query)
                    matched_city_coords, _ = self._match_city_name(matched_street_coords + unmatched_street_coords, city_query)
                    with_coords = self._sort_by_time(matched_city_coords)

                matched_street, unmatched_street = self._match_street_name(without_coords, street_query)
                matched_city, _ = self._match_city_name(matched_street + unmatched_street, city_query)
                without_coords = self._sort_by_time(matched_city)

                incidents = with_coords + without_coords
        elif query_type == QUERY_TYPE_CITY:
            matched, _ = self._match_city_name(incidents, location)
            high_priority = []
            for inc in matched:
                if self._get_city_match_priority(inc, location) >= 2:
                    high_priority.append(inc)
            incidents = self._sort_by_time(high_priority)
            self.logger.debug(f"City query '{location}': {len(incidents)} matches (city at end of address only)")
        elif query_type == QUERY_TYPE_COUNTY:
            incidents = self._sort_by_time(incidents)
            self.logger.debug(f"County query '{location}': returning {len(incidents)} incidents (no city filtering)")
        else:
            self.logger.warning(f"Unknown query type: {query_type} for query: {query}")
            incidents = self._sort_by_time(incidents)
        return incidents

    def _format_incident_compact(self, inc: dict) -> str:
        """Format a single incident in compact (<=130 char) format."""
        unit_str = ""
        if inc.get("units"):
            u = inc["units"][0]
            status_icon = {"DP": "⏳", "ER": "🚗", "OS": "📍", "TR": "🏥"}.get(u["status_code"], "")
            unit_str = f" [{u['id']}{status_icon}]"

        city = inc.get("city", "")
        if city:
            city = city.replace(", WA", "").replace(" COUNTY", " CO")
            city_part = f", {city}"
        else:
            city_part = ""

        time_ago = inc.get("time_ago", "")
        time_part = f" ({time_ago})" if time_ago else ""

        return f"{inc['type']}: {inc['street']}{city_part}{time_part}{unit_str}"

    # ------------------------------------------------------------------ #
    # BaseAlertService interface — query mode.
    # ------------------------------------------------------------------ #
    def _agency_ids_for_query(self, query_type: str, location: Optional[str]) -> Optional[str]:
        """Resolve the agency IDs to fetch for a parsed query."""
        if query_type == QUERY_TYPE_COUNTY:
            return self._get_agency_ids(location, "county")
        if query_type == QUERY_TYPE_CITY:
            agency_ids = self._get_agency_ids(location, "city")
            if agency_ids is None:
                # No city-specific agencies configured; fall back to all.
                agency_ids = self._get_agency_ids()
            return agency_ids
        # zipcode, coordinates, street_city: search all configured agencies.
        return self._get_agency_ids()

    async def query_alerts(self, query: str) -> list[str]:
        """Return formatted incident lines matching ``query`` (query mode).

        Args:
            query: The raw query text following the command keyword.

        Returns:
            A list of compact incident strings (each <=130 chars, no label).
        """
        query_type, location, lat, lon = self.parse_query(query)
        self.logger.debug(f"PulsePoint parsed query '{query}' as {query_type}, location={location}")

        agency_ids = self._agency_ids_for_query(query_type, location)
        if not agency_ids:
            self.logger.debug("PulsePoint: no agency IDs resolved for query '%s'", query)
            return []

        # Blocking HTTP — keep it off the event loop.
        incidents = await asyncio.to_thread(self._fetch_incidents, agency_ids)
        if not incidents:
            return []

        # Zipcode/street_city branches geocode over blocking HTTP; offload too.
        incidents = await asyncio.to_thread(
            self._rank_incidents, query_type, query, location, lat, lon, incidents
        )

        return [self._format_incident_compact(inc) for inc in incidents]

    # ------------------------------------------------------------------ #
    # BaseAlertService interface — polling mode.
    # ------------------------------------------------------------------ #
    async def fetch_new_incidents(self) -> list[tuple[str, str]]:
        """Return currently-active incidents as ``(incident_id, line)`` pairs.

        The base class filters out ids already posted, so this returns every
        active incident (across all configured agencies) dispatched at or after
        the service started. Incidents predating startup are skipped so the
        first poll does not replay history.
        """
        agency_ids = self._get_agency_ids()
        if not agency_ids:
            return []

        incidents = await asyncio.to_thread(self._fetch_incidents, agency_ids)
        results: list[tuple[str, str]] = []
        for inc in self._sort_by_time(incidents):
            inc_id = inc.get("id")
            if not inc_id:
                continue
            # Skip incidents that predate service start (avoid replaying history).
            inc_time = inc.get("time")
            if inc_time is not None:
                if inc_time.tzinfo is None:
                    inc_time = inc_time.replace(tzinfo=timezone.utc)
                if inc_time < self._poll_start_time:
                    continue
            results.append((str(inc_id), self._format_incident_compact(inc)))
        return results

