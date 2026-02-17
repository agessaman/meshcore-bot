#!/usr/bin/env python3
"""
Alert command for the MeshCore Bot
Provides PulsePoint incident alerts for locations, zip codes, and street addresses
"""

import re
import base64
import hashlib
import json
import requests
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import geocode_zipcode_sync, geocode_city_sync, calculate_distance, rate_limited_nominatim_reverse_sync

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
    except:
        return None


def _time_ago(dt: datetime) -> str:
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


class AlertCommand(BaseCommand):
    """Handles alert/incident commands using PulsePoint API.
    
    Retrieves and displays active fire and emergency incidents for specified
    locations (city, county, zipcode, or coordinates).
    """
    
    # Plugin metadata
    name = "alert"
    keywords = ['alert', 'alerts', 'incident', 'incidents']
    description = "Get active emergency incidents (usage: alert seattle, alert 98258, alert 178th seattle, alert seattle all)"
    category = "emergency"
    cooldown_seconds = 10  # 10 second cooldown to prevent API abuse
    
    # Documentation
    short_description = "Get active emergency incidents from PulsePoint"
    usage = "alert <location> [all]"
    examples = ["alert seattle", "alert 98101 all"]
    parameters = [
        {"name": "location", "description": "City, zip code, or street address"},
        {"name": "all", "description": "Show all incidents (not just nearby)"}
    ]
    requires_internet = True  # Requires internet access for PulsePoint API
    
    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10
        self.db_manager = bot.db_manager
        
        # Load agencies from config (separate cities and counties)
        self.city_agencies, self.county_agencies = self._load_agencies()
        
        # Get max distance from config (default 20km, about 12 miles)
        self.max_distance_km = self.get_config_value('Alert_Command', 'max_distance_km', fallback=20.0, value_type='float')
        
        # Get max incident age in hours (default 24 hours) - filter out incidents older than this
        self.max_incident_age_hours = self.get_config_value('Alert_Command', 'max_incident_age_hours', fallback=24.0, value_type='float')

        # Load enabled (standard enabled; alert_enabled legacy)
        self.alert_enabled = self.get_config_value('Alert_Command', 'enabled', fallback=None, value_type='bool')
        if self.alert_enabled is None:
            self.alert_enabled = self.get_config_value('Alert_Command', 'alert_enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.alert_enabled:
            return False
        
        # Call parent can_execute() which includes channel checking, cooldown, etc.
        return super().can_execute(message)
    
    def _load_agencies(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Load agency IDs from config, separating cities and counties.
        
        Returns:
            Tuple[Dict[str, str], Dict[str, str]]: Tuple of (cities_map, counties_map).
        """
        cities = {}
        counties = {}
        if self.bot.config.has_section('Alert_Command'):
            for key, value in self.bot.config.items('Alert_Command'):
                # New format: agency.city.seattle or agency.county.king
                if key.startswith('agency.city.'):
                    city = key.replace('agency.city.', '').lower()
                    cities[city] = value.strip()
                elif key.startswith('agency.county.'):
                    county = key.replace('agency.county.', '').lower()
                    counties[county] = value.strip()
                # Legacy format support: agency.* (treat as county for backward compatibility)
                elif key.startswith('agency.'):
                    name = key.replace('agency.', '').lower()
                    counties[name] = value.strip()
                # Old format: agency_* (treat as county for backward compatibility)
                elif key.startswith('agency_'):
                    name = key.replace('agency_', '').lower()
                    counties[name] = value.strip()
        return cities, counties
    
    def _normalize_location_key(self, location: str) -> str:
        """Normalize location name to match config key format (spaces -> underscores).
        
        Args:
            location: The raw location string.
            
        Returns:
            str: Normalized location string.
        """
        return location.lower().replace(' ', '_')
    
    def _get_agency_ids(self, location: str = None, location_type: str = None) -> Optional[str]:
        """Get agency IDs for a city or county, or default to all configured agencies.
        
        Args:
            location: Name of the city or county.
            location_type: Type of location ('city' or 'county').
            
        Returns:
            Optional[str]: Comma-separated agency IDs, or None if specific location not found.
        """
        if location:
            location_lower = location.lower()
            location_normalized = self._normalize_location_key(location)
            
            # If location_type is specified, only check that type
            if location_type == "city":
                # Try normalized first (with underscore), then original (with space)
                if location_normalized in self.city_agencies:
                    return self.city_agencies[location_normalized]
                if location_lower in self.city_agencies:
                    return self.city_agencies[location_lower]
                # City not found in config, return None to indicate we should use all agencies
                return None
            elif location_type == "county":
                # Try normalized first (with underscore), then original (with space)
                if location_normalized in self.county_agencies:
                    return self.county_agencies[location_normalized]
                if location_lower in self.county_agencies:
                    return self.county_agencies[location_lower]
                # County not found, check aliases
                aliases = {
                    'sno': 'snohomish',
                    'sea': 'king',  # 'sea' alias maps to King County
                    'tac': 'pierce',
                    'all': 'puget_sound'
                }
                if location_lower in aliases:
                    alias_target = aliases[location_lower]
                    if alias_target in self.county_agencies:
                        return self.county_agencies[alias_target]
                return None
            
            # If no location_type specified, check both (city first, then county)
            # Try normalized first (with underscore), then original (with space)
            if location_normalized in self.city_agencies:
                return self.city_agencies[location_normalized]
            if location_lower in self.city_agencies:
                return self.city_agencies[location_lower]
            if location_normalized in self.county_agencies:
                return self.county_agencies[location_normalized]
            if location_lower in self.county_agencies:
                return self.county_agencies[location_lower]
            
            # Check aliases (these map to counties)
            aliases = {
                'sno': 'snohomish',
                'sea': 'king',  # 'sea' alias maps to King County
                'tac': 'pierce',
                'all': 'puget_sound'
            }
            if location_lower in aliases:
                alias_target = aliases[location_lower]
                if alias_target in self.county_agencies:
                    return self.county_agencies[alias_target]
        
        # Default: combine all agencies from both cities and counties
        all_agencies = []
        for agency_list in list(self.city_agencies.values()) + list(self.county_agencies.values()):
            all_agencies.append(agency_list)
        return ",".join(all_agencies)
    
    def _fetch_incidents(self, agency_ids: str) -> List[Dict]:
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
            
            # Only fetch active incidents (not recent/cleared)
            # Filter by age to exclude very old "active" incidents
            now = datetime.now(timezone.utc)
            max_age = self.max_incident_age_hours * 3600  # Convert hours to seconds
            
            for inc in decrypted.get("incidents", {}).get("active", []):
                incident_id = inc.get("ID")
                
                # Skip if we've already seen this incident ID (deduplication)
                if incident_id in seen_ids:
                    continue
                seen_ids.add(incident_id)
                
                call_type = inc.get("PulsePointIncidentCallType", "UNK")
                call_time = _parse_time(inc.get("CallReceivedDateTime"))
                
                # Filter out incidents older than max_incident_age_hours
                if call_time:
                    # Ensure timezone-aware for comparison
                    if call_time.tzinfo is None:
                        call_time = call_time.replace(tzinfo=timezone.utc)
                    else:
                        call_time = call_time.astimezone(timezone.utc)
                    
                    age_seconds = (now - call_time).total_seconds()
                    if age_seconds > max_age:
                        # Incident is too old, skip it
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
                # Try splitting on ", " first (most common), then on "," if that fails
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
    
    def _parse_query(self, query: str) -> Tuple[str, Optional[str], Optional[float], Optional[float]]:
        """Parse query string to determine search type.
        
        Args:
            query: The raw query string from the user.
            
        Returns:
            Tuple[str, Optional[str], Optional[float], Optional[float]]: 
                Tuple of (query_type, location, lat, lon).
                query_type can be: "zipcode", "coordinates", "street_city", "city", "county".
        """
        query = query.strip()
        
        # Check for coordinates (lat,lon or lat, lon)
        coord_match = re.match(r'^(-?\d+\.?\d*),?\s*(-?\d+\.?\d*)$', query)
        if coord_match:
            try:
                lat = float(coord_match.group(1))
                lon = float(coord_match.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return ("coordinates", None, lat, lon)
            except:
                pass
        
        # Check for zipcode (5 digits)
        if re.match(r'^\d{5}$', query):
            return ("zipcode", query, None, None)
        
        # Check for street + city pattern (e.g., "178th seattle", "main seattle")
        # If query has 2+ words, check if first word looks like a street name
        parts = query.split()
        if len(parts) >= 2:
            first_word = parts[0].lower()
            
            # Check if first word looks like a street name:
            # - Ends with a number (e.g., "178th", "5th", "123rd")
            # - Ends with street suffix (e.g., "main", "oak", "park" - but these are ambiguous)
            # - Is a known street prefix (e.g., "ne", "nw", "se", "sw" for directional)
            looks_like_street = (
                bool(re.search(r'\d+(st|nd|rd|th)$', first_word)) or  # Ends with number + ordinal
                first_word in ['ne', 'nw', 'se', 'sw', 'n', 's', 'e', 'w'] or  # Directional prefixes
                first_word.endswith(('st', 'nd', 'rd', 'th'))  # Ordinal suffix
            )
            
            if looks_like_street:
                # First word looks like a street, try to split into street and city
                for i in range(1, len(parts)):
                    street_part = ' '.join(parts[:i])
                    city_part = ' '.join(parts[i:])
                    
                    # Check if city_part looks like a city name (not a street suffix)
                    # If city_part is a known city/county, or doesn't end with street suffix, it's likely a city
                    city_lower = city_part.lower()
                    if (city_lower in self.city_agencies or 
                        city_lower in self.county_agencies or
                        city_lower in ['sno', 'sea', 'tac', 'all'] or
                        not city_lower.endswith(('st', 'street', 'ave', 'avenue', 'rd', 'road', 'blvd', 'boulevard',
                                                'dr', 'drive', 'ct', 'court', 'ln', 'lane', 'way', 'pl', 'place'))):
                        # This looks like street + city
                        return ("street_city", f"{street_part} {city_part}", None, None)
                
                # If no good split found, assume first word is street, rest is city
                street_part = parts[0]
                city_part = ' '.join(parts[1:])
                return ("street_city", f"{street_part} {city_part}", None, None)
            # else: first word doesn't look like a street, treat entire query as city name (fall through)
        
        # Check if it's a known county alias (short codes only)
        # County aliases: 'sno' (snohomish), 'sea' (king), 'tac' (pierce), 'all' (all counties)
        query_lower = query.lower()
        if query_lower in ['sno', 'sea', 'tac', 'all']:
            return ("county", query, None, None)
        
        # Normalize query to match config key format (spaces -> underscores)
        # Config keys use underscores (e.g., "lake_stevens"), but queries may use spaces
        query_normalized = self._normalize_location_key(query)
        
        # Check if it's a configured city name (from config) - check cities first
        # Try both normalized (with underscore) and original (with space) formats
        if query_normalized in self.city_agencies or query_lower in self.city_agencies:
            return ("city", query, None, None)
        
        # Check if it's a configured county name (from config)
        # Try both normalized (with underscore) and original (with space) formats
        if query_normalized in self.county_agencies or query_lower in self.county_agencies:
            return ("county", query, None, None)
        
        # Default: treat as city name (handles single-word and multi-word city names)
        return ("city", query, None, None)
    
    def _match_street_name(self, incidents: List[Dict], street_query: str) -> Tuple[List[Dict], List[Dict]]:
        """Split incidents into matched and unmatched by street name.
        
        Args:
            incidents: List of incidents to filter.
            street_query: Street name to search for.
            
        Returns:
            Tuple[List[Dict], List[Dict]]: (matched_incidents, unmatched_incidents).
        """
        street_lower = street_query.lower().strip()
        matched = []
        unmatched = []
        
        for inc in incidents:
            street = inc.get("street", "").lower()
            # Check if street query appears in the incident's street name
            # This handles cases like "178th" matching "178TH AVE" or "NE 178TH ST"
            if street_lower in street:
                matched.append(inc)
            else:
                unmatched.append(inc)
        
        return matched, unmatched
    
    def _matches_city(self, inc: Dict, city_query: str) -> bool:
        """Check if incident matches the city name by substring matching on address field.
        
        Args:
            inc: Incident dictionary.
            city_query: City name to check.
            
        Returns:
            bool: True if matched, False otherwise.
        """
        city_query_lower = city_query.lower().strip()
        address = inc.get("address", "").lower().strip()
        city = inc.get("city", "").lower().strip()
        
        # Check if city name appears in the address field
        return city_query_lower in address
    
    def _get_city_match_priority(self, inc: Dict, city_query: str) -> int:
        """Get priority score for city match (higher = better match).
        
        We prioritize matches where the city name appears at the end of the address
        (after a comma), as this is the most reliable indicator. The city field
        can be inaccurate (e.g., showing "SEATTLE" for addresses in King County
        but not actually in Seattle).
        
        Args:
            inc: Incident dictionary.
            city_query: City name to match.
            
        Returns:
            int: Priority score (2=suffix match, 1=substring match, 0=no match).
        """
        city_query_lower = city_query.lower().strip()
        address = inc.get("address", "").lower().strip()
        
        city_query_clean = city_query_lower.split(',')[0].strip()
        
        # Priority 2: City appears at end of address (most reliable - typical format: "STREET, CITY" or "STREET, CITY, STATE")
        # This is the most trustworthy match since it's based on the actual address format
        import re
        end_pattern = r',\s*' + re.escape(city_query_clean) + r'(?:\s*,\s*[A-Z]{2})?$'
        if re.search(end_pattern, address, re.IGNORECASE):
            return 2
        
        # Priority 1: City appears anywhere in address (substring match, less reliable)
        # This catches cases where city might be mentioned but not at the end
        if city_query_clean in address:
            return 1
        
        # Priority 0: No match
        return 0
    
    def _match_city_name(self, incidents: List[Dict], city_query: str) -> Tuple[List[Dict], List[Dict]]:
        """Split incidents into matched and unmatched by city name.
        
        Args:
            incidents: List of incidents to filter.
            city_query: City name to filter by.
            
        Returns:
            Tuple[List[Dict], List[Dict]]: (matched_incidents, unmatched_incidents).
        """
        matched = []
        unmatched = []
        
        for inc in incidents:
            if self._matches_city(inc, city_query):
                matched.append(inc)
            else:
                unmatched.append(inc)
        
        return matched, unmatched
    
    def _sort_by_time(self, incidents: List[Dict]) -> List[Dict]:
        """Sort incidents by time (most recent first).
        
        Args:
            incidents: List of incidents to sort.
            
        Returns:
            List[Dict]: Sorted list of incidents.
        """
        def get_time_key(inc):
            time = inc.get("time")
            if time is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            # Ensure timezone-aware
            if time.tzinfo is None:
                time = time.replace(tzinfo=timezone.utc)
            return time
        
        return sorted(incidents, key=get_time_key, reverse=True)
    
    def _sort_by_distance_then_time(self, incidents: List[Dict], lat: float, lon: float, max_distance: float = None) -> List[Dict]:
        """Sort incidents by distance first, then by time (most recent first) within same distance.
        
        Args:
            incidents: List of incidents to sort.
            lat: Reference latitude.
            lon: Reference longitude.
            max_distance: Optional max distance in km to filter.
            
        Returns:
            List[Dict]: Sorted list of incidents.
        """
        scored_incidents = []
        for inc in incidents:
            if not self._has_valid_coordinates(inc):
                continue
            
            inc_lat = inc.get("latitude", 0)
            inc_lon = inc.get("longitude", 0)
            distance = calculate_distance(lat, lon, inc_lat, inc_lon)
            inc["_distance"] = distance
            
            # Filter by max_distance if specified
            if max_distance is None or distance <= max_distance:
                # Get time for secondary sort
                time = inc.get("time")
                if time is None:
                    time_key = datetime.min.replace(tzinfo=timezone.utc)
                else:
                    if time.tzinfo is None:
                        time = time.replace(tzinfo=timezone.utc)
                    time_key = time
                inc["_time_key"] = time_key
                scored_incidents.append(inc)
        
        # Sort by distance first, then by time (most recent first)
        return sorted(scored_incidents, key=lambda x: (x.get("_distance", float('inf')), -x.get("_time_key", datetime.min).timestamp()))
    
    def _has_valid_coordinates(self, inc: Dict) -> bool:
        """Check if incident has valid coordinates.
        
        Args:
            inc: Incident dictionary.
            
        Returns:
            bool: True if coordinates are valid and non-zero, False otherwise.
        """
        inc_lat = inc.get("latitude", 0)
        inc_lon = inc.get("longitude", 0)
        # Valid if both are non-zero and within valid ranges
        return (inc_lat != 0.0 and inc_lon != 0.0 and 
                -90 <= inc_lat <= 90 and -180 <= inc_lon <= 180)
    
    def _sort_by_distance(self, incidents: List[Dict], lat: float, lon: float, max_distance: float = None) -> List[Dict]:
        """Sort incidents by distance from given coordinates.
        
        Args:
            incidents: List of incident dicts.
            lat: Target latitude.
            lon: Target longitude.
            max_distance: Optional maximum distance in km (incidents beyond this are excluded).
        
        Returns:
            List[Dict]: Sorted list of incidents (closest first). Only includes incidents with valid coordinates.
        """
        scored_incidents = []
        for inc in incidents:
            if not self._has_valid_coordinates(inc):
                # Skip incidents without valid coordinates - they'll be handled separately
                continue
            
            inc_lat = inc.get("latitude", 0)
            inc_lon = inc.get("longitude", 0)
            distance = calculate_distance(lat, lon, inc_lat, inc_lon)
            inc["_distance"] = distance
            
            # Filter by max_distance if specified
            if max_distance is None or distance <= max_distance:
                scored_incidents.append(inc)
        
        # Sort by distance (closest first)
        return sorted(scored_incidents, key=lambda x: x.get("_distance", float('inf')))
    
    def _format_incident_compact(self, inc: Dict) -> str:
        """Format a single incident in compact format.
        
        Args:
            inc: Incident dictionary.
            
        Returns:
            str: Formatted incident string for display.
        """
        # Get first unit with status icon
        unit_str = ""
        if inc.get("units"):
            u = inc["units"][0]
            status_icon = {"DP": "⏳", "ER": "🚗", "OS": "📍", "TR": "🏥"}.get(u["status_code"], "")
            unit_str = f" [{u['id']}{status_icon}]"
        
        # Shorten city name
        city = inc.get("city", "")
        if city:
            city = city.replace(", WA", "").replace(" COUNTY", " CO")
            city_part = f", {city}"
        else:
            city_part = ""
        
        time_ago = inc.get("time_ago", "")
        time_part = f" ({time_ago})" if time_ago else ""
        
        return f"{inc['type']}: {inc['street']}{city_part}{time_part}{unit_str}"
    
    def _format_response(self, incidents: List[Dict], max_length: int = 130) -> str:
        """Format incidents into a single message, limiting to max_length.
        
        Args:
            incidents: List of incidents to format.
            max_length: Maximum length of the output string (default 130 for LoRa).
            
        Returns:
            str: Formatted response string.
        """
        if not incidents:
            return "🚨 No active incidents"
        
        lines = ["🚨"]
        # Start with emoji (2 chars) + newline (1 char) = 3 chars
        current_length = 3
        
        remaining = len(incidents)
        
        for i, inc in enumerate(incidents):
            line = self._format_incident_compact(inc)
            # Length includes the line content + newline character
            line_length = len(line) + 1
            
            # Check if we can fit this line at all
            if current_length + line_length > max_length:
                # Can't fit this line
                if len(lines) > 1:  # At least one incident shown
                    lines.append(f"({remaining} more)")
                break
            
            # Check if this is the last incident
            is_last = (remaining == 1)
            
            if is_last:
                # Last incident, no "(X more)" needed, add it
                lines.append(line)
                current_length += line_length
                remaining -= 1
            else:
                # Not the last incident, check if we can fit line + "(X more)"
                more_text = f" ({remaining - 1} more)"
                if current_length + line_length + len(more_text) > max_length:
                    # Can't fit both line and "(X more)", show count instead
                    if len(lines) > 1:  # At least one incident shown
                        lines.append(f"({remaining} more)")
                    break
                else:
                    # Can fit both line and "(X more)", add the line
                    lines.append(line)
                    current_length += line_length
                    remaining -= 1
        
        # Build final message
        final_message = "\n".join(lines)
        
        # Safety check: ensure we don't exceed max_length (shouldn't happen, but be safe)
        if len(final_message) > max_length:
            # Find the last complete line before max_length
            # Look for the last newline that would keep us under the limit
            last_newline = final_message.rfind('\n', 0, max_length - 15)  # Reserve 15 chars for "(X more)"
            if last_newline > 0 and len(lines) > 1:
                # Truncate at the last complete line
                final_message = final_message[:last_newline]
                # Add count if there are remaining incidents
                if remaining > 0:
                    final_message += f"\n({remaining} more)"
            else:
                # Fallback: just truncate (shouldn't happen with proper logic above)
                final_message = final_message[:max_length].rstrip()
        
        return final_message
    
    async def _send_all_response(self, message: MeshMessage, incidents: List[Dict]) -> None:
        """Send up to 10 incidents in multiple messages, grouping efficiently.
        
        Args:
            message: The message to respond to.
            incidents: List of incidents to send.
        """
        import asyncio
        
        if not incidents:
            await self.send_response(message, "🚨 No active incidents")
            return
        
        # Build messages efficiently, grouping incidents to fit within 130 chars
        messages = []
        header = f"🚨 {len(incidents)} incident(s):"
        
        # Start first message with header
        current_lines = [header]
        current_length = len(header)
        
        for inc in incidents:
            incident_text = self._format_incident_compact(inc)
            incident_length = len(incident_text)
            
            # Calculate what the message would look like with this incident added
            # Need to account for newline character between lines
            test_length = current_length + 1 + incident_length  # +1 for newline
            
            # Check if this incident fits in the current message
            if test_length <= 130:
                # It fits, add it
                current_lines.append(incident_text)
                current_length = test_length
            else:
                # Doesn't fit - finalize current message and start new one
                if len(current_lines) > 1:  # Has at least header + one incident
                    messages.append("\n".join(current_lines))
                else:
                    # Only header, must add at least one incident even if it exceeds limit
                    current_lines.append(incident_text)
                    messages.append("\n".join(current_lines))
                    current_lines = []
                    current_length = 0
                    continue
                
                # Start new message with this incident (no header for subsequent messages)
                current_lines = [incident_text]
                current_length = incident_length
        
        # Add the last message if it has content
        if current_lines:
            # If we only have header, add first incident
            if len(current_lines) == 1 and len(incidents) > 0:
                current_lines.append(self._format_incident_compact(incidents[0]))
            messages.append("\n".join(current_lines))
        
        # Send all messages with delays between them
        for i, msg in enumerate(messages):
            await self.send_response(message, msg)
            # Wait between messages (except after the last one)
            if i < len(messages) - 1:
                await asyncio.sleep(2.0)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the alert command.
        
        Parses query, fetches incidents, filters/sorts, and sends response.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()
        
        # Parse command
        parts = content.split(None, 1)
        if len(parts) < 2:
            # No query provided, use default location or show help
            await self.send_response(message, "Usage: alert <city|zipcode|street city|lat,lon|county> [all]")
            return True
        
        query = parts[1].strip()
        
        # Check for "all" flag at the end
        show_all = False
        if query.lower().endswith(' all'):
            show_all = True
            query = query[:-4].strip()  # Remove " all" from the end
        
        try:
            # Parse the query
            query_type, location, lat, lon = self._parse_query(query)
            self.logger.debug(f"Parsed query '{query}' as type: {query_type}, location: {location}")
            
            # Get agency IDs based on query type
            if query_type == "county":
                agency_ids = self._get_agency_ids(location, "county")
            elif query_type == "city":
                # For city queries, try to get city-specific agencies, fall back to all
                agency_ids = self._get_agency_ids(location, "city")
                if agency_ids is None:
                    # No city-specific agencies configured, use all agencies
                    agency_ids = self._get_agency_ids()
            else:
                # For other queries (zipcode, coordinates, street_city), use all agencies
                agency_ids = self._get_agency_ids()  # Default to all
            
            # Fetch incidents
            incidents = self._fetch_incidents(agency_ids)
            
            if not incidents:
                await self.send_response(message, "🚨 No active incidents")
                return True
            
            # Process based on query type
            if query_type == "coordinates":
                # Sort by distance with configurable max distance, then by time
                incidents = self._sort_by_distance_then_time(incidents, lat, lon, max_distance=self.max_distance_km)
            elif query_type == "zipcode":
                # Geocode zipcode and get city name
                zip_lat, zip_lon = geocode_zipcode_sync(self.bot, location)
                zip_city = None
                
                if zip_lat and zip_lon:
                    # Get city name from zipcode via reverse geocoding
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
                    
                    # Split incidents by coordinate validity
                    with_coords = [inc for inc in incidents if self._has_valid_coordinates(inc)]
                    without_coords = [inc for inc in incidents if not self._has_valid_coordinates(inc)]
                    
                    # Prioritize incidents that match the zipcode's city
                    if zip_city:
                        # Filter by city name match, then sort by distance and time
                        matched_coords, _ = self._match_city_name(with_coords, zip_city)
                        matched_no_coords, _ = self._match_city_name(without_coords, zip_city)
                        
                        # Sort matched incidents by distance (for those with coords), then time
                        matched_coords = self._sort_by_distance_then_time(matched_coords, zip_lat, zip_lon, max_distance=None)
                        matched_no_coords = self._sort_by_time(matched_no_coords)
                        
                        # If we have matches, show those. Otherwise, show nearby incidents within max distance
                        if len(matched_coords) > 0 or len(matched_no_coords) > 0:
                            incidents = matched_coords + matched_no_coords
                        else:
                            # No city matches, show nearby incidents within max distance
                            nearby_coords = self._sort_by_distance_then_time(with_coords, zip_lat, zip_lon, max_distance=self.max_distance_km)
                            nearby_no_coords = self._sort_by_time(without_coords)
                            incidents = nearby_coords + nearby_no_coords
                    else:
                        # No city name available, just sort by distance
                        incidents = self._sort_by_distance_then_time(incidents, zip_lat, zip_lon, max_distance=self.max_distance_km)
            elif query_type == "street_city":
                # Extract street and city
                parts = location.split(None, 1)
                if len(parts) == 2:
                    street_query, city_query = parts
                    # Geocode city first
                    result = geocode_city_sync(self.bot, city_query, include_address_info=False)
                    city_lat, city_lon = None, None
                    if len(result) >= 2:
                        city_lat, city_lon = result[0], result[1]
                    
                    # Split incidents by coordinate validity
                    with_coords = [inc for inc in incidents if self._has_valid_coordinates(inc)]
                    without_coords = [inc for inc in incidents if not self._has_valid_coordinates(inc)]
                    
                    # Process incidents with coordinates
                    if city_lat and city_lon:
                        # Sort by distance (closest first) with max distance filter
                        with_coords = self._sort_by_distance(with_coords, city_lat, city_lon, max_distance=self.max_distance_km)
                        # Prioritize street-matched incidents by re-sorting
                        matched_street_coords, unmatched_street_coords = self._match_street_name(with_coords, street_query)
                        # Re-sort both groups by distance then time to maintain proximity ordering
                        matched_street_coords = self._sort_by_distance_then_time(matched_street_coords, city_lat, city_lon, max_distance=self.max_distance_km)
                        unmatched_street_coords = self._sort_by_distance_then_time(unmatched_street_coords, city_lat, city_lon, max_distance=self.max_distance_km)
                        with_coords = matched_street_coords + unmatched_street_coords
                    else:
                        # Geocoding failed - fall back to address matching for incidents with coordinates
                        matched_street_coords, unmatched_street_coords = self._match_street_name(with_coords, street_query)
                        matched_city_coords, _ = self._match_city_name(matched_street_coords + unmatched_street_coords, city_query)
                        with_coords = matched_city_coords
                        # Sort by time
                        with_coords = self._sort_by_time(with_coords)
                    
                    # Process incidents without coordinates: match by street name and city name
                    matched_street, unmatched_street = self._match_street_name(without_coords, street_query)
                    # Also match by city name in address for those without coordinates
                    matched_city, _ = self._match_city_name(matched_street + unmatched_street, city_query)
                    without_coords = matched_city
                    # Sort by time
                    without_coords = self._sort_by_time(without_coords)
                    
                    # Combine: incidents with coordinates (sorted by distance or matched by address) first, then address-matched ones without coordinates
                    incidents = with_coords + without_coords
            elif query_type == "city":
                # Filter incidents by city name match - ONLY show matches where city appears at end of address
                # This is the most reliable indicator and avoids false positives
                matched, _ = self._match_city_name(incidents, location)
                
                # Only keep incidents where city name appears at end of address (Priority 2)
                # This ensures we only show incidents that are actually in the queried city
                high_priority = []
                for inc in matched:
                    priority = self._get_city_match_priority(inc, location)
                    if priority >= 2:  # Only city at end of address
                        high_priority.append(inc)
                    else:
                        # Log why an incident was excluded (for debugging)
                        self.logger.debug(f"Excluding incident: {inc.get('address', 'N/A')[:60]} (priority: {priority})")
                
                # Sort by time (most recent first)
                incidents = self._sort_by_time(high_priority)
                self.logger.debug(f"City query '{location}': {len(incidents)} matches (city at end of address only), {len(matched) - len(high_priority)} excluded")
            elif query_type == "county":
                # For county queries, return all incidents from that county (no city filtering)
                # Sort by time (most recent first)
                incidents = self._sort_by_time(incidents)
                self.logger.debug(f"County query '{location}': returning {len(incidents)} incidents (no city filtering)")
            else:
                # Unknown query type - this shouldn't happen, but log it
                self.logger.warning(f"Unknown query type: {query_type} for query: {query}")
                # Default: sort by time
                incidents = self._sort_by_time(incidents)
            
            # Limit to 10 incidents if "all" mode is enabled
            if show_all:
                incidents = incidents[:10]
                await self._send_all_response(message, incidents)
            else:
                # Format and send response (compact mode)
                response = self._format_response(incidents)
                await self.send_response(message, response)
            return True
            
        except Exception as e:
            self.logger.error(f"Error in alert command: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            await self.send_response(message, f"Error fetching alerts: {str(e)}")
            return True


