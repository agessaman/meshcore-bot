#!/usr/bin/env python3
"""
Airplanes command for the MeshCore Bot
Provides aircraft tracking using ADS-B data from airplanes.live or compatible APIs
"""

import re
import math
import asyncio
import requests
from typing import Optional, List, Dict, Any, Tuple
from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import calculate_distance


class AirplanesCommand(BaseCommand):
    """Handles aircraft tracking commands using ADS-B data.
    
    Provides aircraft information overhead at companion location, bot location,
    or specified coordinates. Supports filtering and detailed single-aircraft display.
    """
    
    # Plugin metadata
    name = "airplanes"
    keywords = ['airplanes', 'aircraft', 'planes', 'adsb', 'overhead']
    description = "Get aircraft overhead (usage: airplanes [location] [options] or overhead [lat,lon])"
    category = "general"
    cooldown_seconds = 2  # Respect API rate limit of 1 req/sec with buffer
    requires_internet = True
    
    # Documentation
    short_description = "Get aircraft overhead using ADS-B data"
    usage = "airplanes [lat,lon|here] [radius=N] [options]"
    examples = ["airplanes", "overhead 47.6,-122.3"]
    parameters = [
        {"name": "location", "description": "Coordinates or here for your companion's location if advertised"},
        {"name": "radius", "description": "Search radius in nautical miles (default: 25)"},
        {"name": "filters", "description": "alt=, type=, military, closest, etc."}
    ]
    
    def __init__(self, bot):
        super().__init__(bot)
        self.airplanes_enabled = self.get_config_value('Airplanes_Command', 'enabled', fallback=True, value_type='bool')
        self.api_url = self.get_config_value('Airplanes_Command', 'api_url', fallback='http://api.airplanes.live/v2/', value_type='str')
        self.default_radius = self.get_config_value('Airplanes_Command', 'default_radius', fallback=25, value_type='float')
        self.max_results = self.get_config_value('Airplanes_Command', 'max_results', fallback=10, value_type='int')
        self.url_timeout = self.get_config_value('Airplanes_Command', 'url_timeout', fallback=10, value_type='int')
        
        # Ensure API URL ends with /
        if self.api_url and not self.api_url.endswith('/'):
            self.api_url += '/'
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.airplanes_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.airplanes.description')
    
    def _calculate_bearing(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2 in degrees.
        
        Args:
            lat1: Latitude of point 1 in degrees.
            lon1: Longitude of point 1 in degrees.
            lat2: Latitude of point 2 in degrees.
            lon2: Longitude of point 2 in degrees.
            
        Returns:
            float: Bearing in degrees (0-360, where 0 is North).
        """
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlon_rad = math.radians(lon2 - lon1)
        
        y = math.sin(dlon_rad) * math.cos(lat2_rad)
        x = math.cos(lat1_rad) * math.sin(lat2_rad) - \
            math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
        bearing_rad = math.atan2(y, x)
        bearing_deg = (math.degrees(bearing_rad) + 360) % 360
        
        return bearing_deg
    
    def _bearing_to_cardinal(self, bearing: float) -> str:
        """Convert bearing in degrees to cardinal direction.
        
        Args:
            bearing: Bearing in degrees (0-360).
            
        Returns:
            str: Cardinal direction (N, NE, E, SE, S, SW, W, NW).
        """
        directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        index = round(bearing / 45) % 8
        return directions[index]
    
    def _get_companion_location(self, message: MeshMessage) -> Optional[Tuple[float, float]]:
        """Get companion/sender location from database.
        
        Args:
            message: The message object.
            
        Returns:
            Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None.
        """
        try:
            sender_pubkey = message.sender_pubkey
            if not sender_pubkey:
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
                return (row['latitude'], row['longitude'])
            return None
        except Exception as e:
            self.logger.debug(f"Error getting companion location: {e}")
            return None
    
    def _get_bot_location(self) -> Optional[Tuple[float, float]]:
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
    
    def _parse_coordinates(self, args: str) -> Optional[Tuple[float, float]]:
        """Parse latitude and longitude from command arguments.
        
        Args:
            args: Command arguments string.
            
        Returns:
            Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None.
        """
        # Handle formats: "47.6,-122.3", "47.6 -122.3", "47.6, -122.3"
        pattern = r'^\s*(-?\d+\.?\d*)\s*[, ]\s*(-?\d+\.?\d*)\s*$'
        match = re.match(pattern, args)
        
        if match:
            try:
                lat = float(match.group(1))
                lon = float(match.group(2))
                
                # Validate ranges
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return (lat, lon)
            except ValueError:
                pass
        
        return None
    
    def _parse_filters(self, args: List[str]) -> Dict[str, Any]:
        """Parse filter options from command arguments.
        
        Args:
            args: List of command argument strings.
            
        Returns:
            Dict[str, Any]: Dictionary of filter options.
        """
        filters = {
            'radius': self.default_radius,
            'alt_min': None,
            'alt_max': None,
            'speed_min': None,
            'speed_max': None,
            'aircraft_type': None,
            'callsign': None,
            'military': False,
            'ladd': False,
            'pia': False,
            'squawk': None,
            'limit': self.max_results,
            'sort': 'distance'  # distance, altitude, speed
        }
        
        for arg in args:
            arg_lower = arg.lower()
            
            # Radius
            if arg_lower.startswith('radius='):
                try:
                    filters['radius'] = float(arg_lower.split('=')[1])
                    filters['radius'] = min(250, max(1, filters['radius']))  # Clamp 1-250nm
                except (ValueError, IndexError):
                    pass
            
            # Altitude range
            elif arg_lower.startswith('alt='):
                try:
                    alt_str = arg_lower.split('=')[1]
                    if '-' in alt_str:
                        parts = alt_str.split('-')
                        filters['alt_min'] = float(parts[0])
                        filters['alt_max'] = float(parts[1])
                    else:
                        filters['alt_min'] = float(alt_str)
                except (ValueError, IndexError):
                    pass
            
            # Speed range
            elif arg_lower.startswith('speed='):
                try:
                    speed_str = arg_lower.split('=')[1]
                    if '-' in speed_str:
                        parts = speed_str.split('-')
                        filters['speed_min'] = float(parts[0])
                        filters['speed_max'] = float(parts[1])
                    else:
                        filters['speed_min'] = float(speed_str)
                except (ValueError, IndexError):
                    pass
            
            # Aircraft type
            elif arg_lower.startswith('type='):
                filters['aircraft_type'] = arg_lower.split('=')[1].upper()
            
            # Callsign
            elif arg_lower.startswith('callsign='):
                filters['callsign'] = arg_lower.split('=')[1].upper()
            
            # Squawk
            elif arg_lower.startswith('squawk='):
                filters['squawk'] = arg_lower.split('=')[1]
            
            # Limit
            elif arg_lower.startswith('limit='):
                try:
                    filters['limit'] = int(arg_lower.split('=')[1])
                    filters['limit'] = min(50, max(1, filters['limit']))  # Clamp 1-50
                except (ValueError, IndexError):
                    pass
            
            # Flags
            elif arg_lower == 'military':
                filters['military'] = True
            elif arg_lower == 'ladd':
                filters['ladd'] = True
            elif arg_lower == 'pia':
                filters['pia'] = True
            
            # Sort options
            elif arg_lower in ['closest', 'distance']:
                filters['sort'] = 'distance'
            elif arg_lower in ['highest', 'altitude']:
                filters['sort'] = 'altitude'
            elif arg_lower == 'fastest':
                filters['sort'] = 'speed'
        
        return filters
    
    def _fetch_aircraft_data(self, lat: float, lon: float, radius: float) -> Optional[Dict[str, Any]]:
        """Fetch aircraft data from API.
        
        Args:
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            radius: Search radius in nautical miles.
            
        Returns:
            Optional[Dict[str, Any]]: API response JSON or None on error.
        """
        try:
            # Convert radius from nautical miles to approximate degrees (rough conversion)
            # 1 nm ≈ 0.0167 degrees at equator, but we'll use a simple approximation
            # More accurate: use the API's native radius parameter if it accepts nm
            url = f"{self.api_url}point/{lat}/{lon}/{radius}"
            
            self.logger.debug(f"Fetching aircraft data from {url}")
            response = requests.get(url, timeout=self.url_timeout)
            response.raise_for_status()
            
            data = response.json()
            return data
        except requests.exceptions.Timeout:
            self.logger.warning("API request timed out")
            return None
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"API request failed: {e}")
            return None
        except ValueError as e:
            self.logger.warning(f"Invalid JSON response: {e}")
            return None
    
    def _filter_aircraft(self, aircraft_list: List[Dict[str, Any]], filters: Dict[str, Any], query_lat: float, query_lon: float) -> List[Dict[str, Any]]:
        """Filter and sort aircraft based on criteria.
        
        Args:
            aircraft_list: List of aircraft dictionaries.
            filters: Filter criteria dictionary.
            query_lat: Query latitude for distance calculation.
            query_lon: Query longitude for distance calculation.
            
        Returns:
            List[Dict[str, Any]]: Filtered and sorted aircraft list.
        """
        filtered = []
        
        for aircraft in aircraft_list:
            # Skip if no position
            if 'lat' not in aircraft or 'lon' not in aircraft:
                continue
            
            # Calculate distance
            distance_km = calculate_distance(query_lat, query_lon, aircraft['lat'], aircraft['lon'])
            distance_nm = distance_km / 1.852  # Convert km to nautical miles
            aircraft['_distance_nm'] = distance_nm
            aircraft['_distance_km'] = distance_km
            
            # Calculate bearing
            bearing = self._calculate_bearing(query_lat, query_lon, aircraft['lat'], aircraft['lon'])
            aircraft['_bearing'] = bearing
            aircraft['_bearing_cardinal'] = self._bearing_to_cardinal(bearing)
            
            # Apply filters
            # Altitude filtering
            if filters['alt_min'] is not None or filters['alt_max'] is not None:
                alt = aircraft.get('alt_baro') or aircraft.get('alt_geom')
                if alt is None or (isinstance(alt, str) and alt.lower() == 'ground'):
                    continue
                if isinstance(alt, str):
                    continue
                if filters['alt_min'] is not None and alt < filters['alt_min']:
                    continue
                if filters['alt_max'] is not None and alt > filters['alt_max']:
                    continue
            
            if filters['speed_min'] is not None:
                gs = aircraft.get('gs')
                if gs is None or gs < filters['speed_min']:
                    continue
            
            if filters['speed_max'] is not None:
                gs = aircraft.get('gs')
                if gs is None or gs > filters['speed_max']:
                    continue
            
            if filters['aircraft_type']:
                ac_type = aircraft.get('t', '').upper()
                if filters['aircraft_type'] not in ac_type:
                    continue
            
            if filters['callsign']:
                callsign = aircraft.get('flight', '').upper()
                if filters['callsign'] not in callsign:
                    continue
            
            if filters['squawk']:
                squawk = aircraft.get('squawk', '')
                if str(filters['squawk']) != str(squawk):
                    continue
            
            if filters['military']:
                db_flags = aircraft.get('dbFlags', 0)
                if not (db_flags & 1):  # Military flag
                    continue
            
            if filters['ladd']:
                db_flags = aircraft.get('dbFlags', 0)
                if not (db_flags & 8):  # LADD flag
                    continue
            
            if filters['pia']:
                db_flags = aircraft.get('dbFlags', 0)
                if not (db_flags & 4):  # PIA flag
                    continue
            
            # Check radius (already calculated distance)
            if distance_nm > filters['radius']:
                continue
            
            filtered.append(aircraft)
        
        # Sort
        if filters['sort'] == 'distance':
            filtered.sort(key=lambda x: x.get('_distance_nm', float('inf')))
        elif filters['sort'] == 'altitude':
            filtered.sort(key=lambda x: (x.get('alt_baro') or x.get('alt_geom') or 0), reverse=True)
        elif filters['sort'] == 'speed':
            filtered.sort(key=lambda x: (x.get('gs') or 0), reverse=True)
        
        # Apply limit
        return filtered[:filters['limit']]
    
    def _format_single_aircraft(self, aircraft: Dict[str, Any], query_lat: float, query_lon: float, max_length: int = 130) -> str:
        """Format detailed single aircraft response (~130 characters).
        
        More user-friendly format: puts distance/bearing first, uses commas in numbers,
        and formats in a more readable way.
        
        Args:
            aircraft: Aircraft data dictionary.
            query_lat: Query latitude.
            query_lon: Query longitude.
            max_length: Maximum message length (default 130).
            
        Returns:
            str: Formatted aircraft string.
        """
        # Get basic info
        callsign = aircraft.get('flight', '').strip() or aircraft.get('r', 'N/A')
        ac_type = aircraft.get('t', '')
        operator = aircraft.get('ownOp', '').strip()
        
        # Distance and bearing (most important for overhead command - put first)
        distance_nm = aircraft.get('_distance_nm', 0)
        bearing_cardinal = aircraft.get('_bearing_cardinal', 'N')
        
        # Altitude with comma formatting
        alt = aircraft.get('alt_baro') or aircraft.get('alt_geom')
        if isinstance(alt, str):
            alt_str = alt
        elif alt is not None:
            alt_str = f"{int(alt):,}ft"  # Add comma separator for readability
        else:
            alt_str = "N/A"
        
        # Speed
        gs = aircraft.get('gs')
        speed_str = f"{int(gs)}kt" if gs is not None else "N/A"
        
        # Track
        track = aircraft.get('track')
        track_str = f"{int(track)}°" if track is not None else "N/A"
        
        # Vertical rate
        baro_rate = aircraft.get('baro_rate')
        geom_rate = aircraft.get('geom_rate')
        vs = baro_rate or geom_rate
        vs_str = ""
        if vs is not None:
            vs_str = f"{'+' if vs > 0 else ''}{int(vs)}fpm"
        
        # Build user-friendly response: "Callsign (Type) Operator distance bearing: altitude @ speed, heading, vertical_rate"
        # Example: "QXE2307 (E75L) Horizon Air 7.5nm NW: 21,675ft @ 366kt, 354°, +1600fpm"
        response_parts = []
        
        # Callsign and type
        if ac_type:
            response_parts.append(f"{callsign} ({ac_type})")
        else:
            response_parts.append(callsign)
        
        # Add operator/airline if available
        if operator:
            # Abbreviate long operator names to fit within limits
            # Common abbreviations for major airlines
            operator_abbrev = {
                'ALASKA AIRLINES INC': 'Alaska',
                'ALASKA AIRLINES': 'Alaska',
                'HORIZON AIR': 'Horizon Air',
                'HORIZON AIR INDUSTRIES': 'Horizon Air',
                'DELTA AIR LINES INC': 'Delta',
                'DELTA AIR LINES': 'Delta',
                'AMERICAN AIRLINES INC': 'American',
                'AMERICAN AIRLINES': 'American',
                'UNITED AIR LINES INC': 'United',
                'UNITED AIR LINES': 'United',
                'SOUTHWEST AIRLINES CO': 'Southwest',
                'SOUTHWEST AIRLINES': 'Southwest',
                'JETBLUE AIRWAYS CORP': 'JetBlue',
                'JETBLUE AIRWAYS': 'JetBlue',
                'SPIRIT AIRLINES INC': 'Spirit',
                'SPIRIT AIRLINES': 'Spirit',
                'FRONTIER AIRLINES INC': 'Frontier',
                'FRONTIER AIRLINES': 'Frontier',
                'ALLEGIANT AIR LLC': 'Allegiant',
                'ALLEGIANT AIR': 'Allegiant',
            }
            
            # Try to find abbreviation, otherwise use original (truncated if too long)
            operator_display = operator_abbrev.get(operator.upper(), operator)
            
            # If still too long, truncate intelligently (keep first part)
            if len(operator_display) > 20:
                # Try to truncate at a word boundary
                words = operator_display.split()
                operator_display = words[0]
                if len(operator_display) > 20:
                    operator_display = operator_display[:17] + "..."
            
            response_parts.append(operator_display)
        
        # Distance and bearing (most important)
        response_parts.append(f"{distance_nm:.1f}nm {bearing_cardinal}")
        
        # Main info with colon separator
        main_info = f"{alt_str} @ {speed_str}, {track_str}"
        if vs_str:
            main_info += f", {vs_str}"
        
        response = f"{' '.join(response_parts)}: {main_info}"
        
        # Truncate if too long (shouldn't happen, but safety check)
        if len(response) > max_length:
            response = response[:max_length-3] + "..."
        
        return response
    
    def _format_aircraft_list(self, aircraft_list: List[Dict[str, Any]], query_lat: float, query_lon: float, max_length: int = 130) -> str:
        """Format compact list for multiple aircraft.
        
        Args:
            aircraft_list: List of aircraft dictionaries.
            query_lat: Query latitude.
            query_lon: Query longitude.
            max_length: Maximum message length (default 130).
            
        Returns:
            str: Formatted aircraft list.
        """
        lines = []
        for aircraft in aircraft_list:
            callsign = aircraft.get('flight', '').strip() or aircraft.get('r', 'N/A')
            
            alt = aircraft.get('alt_baro') or aircraft.get('alt_geom')
            if isinstance(alt, str):
                alt_str = alt
            elif alt is not None:
                alt_str = f"{int(alt)}ft"
            else:
                alt_str = "N/A"
            
            gs = aircraft.get('gs')
            speed_str = f"{int(gs)}kt" if gs is not None else "N/A"
            
            distance_nm = aircraft.get('_distance_nm', 0)
            bearing_cardinal = aircraft.get('_bearing_cardinal', 'N')
            
            line = f"{callsign} {alt_str} {speed_str} {distance_nm:.1f}nm {bearing_cardinal}"
            lines.append(line)
        
        # Build response, truncating if necessary
        response_lines = []
        current_length = 0
        
        for line in lines:
            line_length = len(line) + 1  # +1 for newline
            if current_length + line_length > max_length:
                # Can't fit this line
                if response_lines:
                    remaining = len(lines) - len(response_lines)
                    if remaining > 0:
                        response_lines.append(f"({remaining} more)")
                break
            response_lines.append(line)
            current_length += line_length
        
        return '\n'.join(response_lines)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the airplanes command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            content = message.content.strip()
            parts = content.split()
            
            if len(parts) < 1:
                help_text = self.translate('commands.airplanes.usage')
                await self.send_response(message, help_text)
                return True
            
            # Check if this is the "overhead" command
            is_overhead_command = False
            command_word = parts[0].lower()
            
            # Check if command is "overhead" or "airplanes overhead" / "aircraft overhead" etc.
            if command_word == 'overhead':
                is_overhead_command = True
                args = parts[1:] if len(parts) > 1 else []
            elif len(parts) > 1 and parts[1].lower() == 'overhead':
                # Handle "airplanes overhead" or "aircraft overhead" etc.
                is_overhead_command = True
                args = parts[2:] if len(parts) > 2 else []
            else:
                args = parts[1:] if len(parts) > 1 else []
            
            # Parse location and filters
            location = None
            location_source = None
            
            # For overhead command, only use companion location or specified coordinates
            if is_overhead_command:
                # Check if coordinates are provided
                if args:
                    # Try joining first two args (handles "47.444356, -122.309483")
                    coords_str = ' '.join(args[:2])
                    coords = self._parse_coordinates(coords_str)
                    if coords:
                        location = coords
                        location_source = "specified"
                        args = args[2:]
                    else:
                        # Try parsing just first arg as coordinates (lat,lon format like "47.444356,-122.309483")
                        coords = self._parse_coordinates(args[0])
                        if coords:
                            location = coords
                            location_source = "specified"
                            args = args[1:]
                        elif len(args) >= 2:
                            # Handle case where first arg has trailing comma: "47.444356," + "-122.309483"
                            first_arg = args[0].rstrip(',')
                            second_arg = args[1]
                            coords_str = f"{first_arg},{second_arg}"
                            coords = self._parse_coordinates(coords_str)
                            if coords:
                                location = coords
                                location_source = "specified"
                                args = args[2:]
                
                # If no coordinates, try companion location only
                if location is None:
                    location = self._get_companion_location(message)
                    if location:
                        location_source = "companion"
                
                # If still no location, show specific error
                if location is None:
                    error_msg = self.translate('commands.airplanes.overhead_no_location')
                    await self.send_response(message, error_msg)
                    return True
            else:
                # Regular airplanes command - check for "here" keyword
                if args and args[0].lower() == 'here':
                    location = self._get_bot_location()
                    location_source = "bot"
                    args = args[1:]
                # Check if first arg is coordinates
                elif args:
                    # Try joining first two args (handles "47.444356, -122.309483")
                    coords_str = ' '.join(args[:2])
                    coords = self._parse_coordinates(coords_str)
                    if coords:
                        location = coords
                        location_source = "specified"
                        args = args[2:]
                    else:
                        # Try parsing just first arg as coordinates (lat,lon format like "47.444356,-122.309483")
                        coords = self._parse_coordinates(args[0])
                        if coords:
                            location = coords
                            location_source = "specified"
                            args = args[1:]
                        elif len(args) >= 2:
                            # Handle case where first arg has trailing comma: "47.444356," + "-122.309483"
                            first_arg = args[0].rstrip(',')
                            second_arg = args[1]
                            coords_str = f"{first_arg},{second_arg}"
                            coords = self._parse_coordinates(coords_str)
                            if coords:
                                location = coords
                                location_source = "specified"
                                args = args[2:]
                
                # If no location specified, try companion then bot
                if location is None:
                    location = self._get_companion_location(message)
                    if location:
                        location_source = "companion"
                    else:
                        location = self._get_bot_location()
                        if location:
                            location_source = "bot"
                
                if location is None:
                    error_msg = self.translate('commands.airplanes.no_location')
                    await self.send_response(message, error_msg)
                    return True
            
            # Parse filters
            filters = self._parse_filters(args)
            
            # For overhead command, force single closest aircraft
            if is_overhead_command:
                filters['limit'] = 1
                filters['sort'] = 'distance'
            
            # Fetch aircraft data
            api_data = self._fetch_aircraft_data(location[0], location[1], filters['radius'])
            
            if api_data is None:
                error_msg = self.translate('commands.airplanes.api_error')
                await self.send_response(message, error_msg)
                return True
            
            # Extract aircraft list (API uses 'ac' key, not 'aircraft')
            aircraft_list = api_data.get('ac', api_data.get('aircraft', []))
            
            if not aircraft_list:
                error_msg = self.translate('commands.airplanes.no_aircraft', radius=filters['radius'])
                await self.send_response(message, error_msg)
                return True
            
            # Filter and sort
            filtered = self._filter_aircraft(aircraft_list, filters, location[0], location[1])
            
            if not filtered:
                error_msg = self.translate('commands.airplanes.no_aircraft_filtered', radius=filters['radius'])
                await self.send_response(message, error_msg)
                return True
            
            # Get max message length for this message type
            max_length = self.get_max_message_length(message)
            
            # Format response
            # For overhead command, always use single aircraft format
            if is_overhead_command or len(filtered) == 1:
                response = self._format_single_aircraft(filtered[0], location[0], location[1], max_length)
            else:
                response = self._format_aircraft_list(filtered, location[0], location[1], max_length)
            
            # Check if response needs to be split (for very long lists)
            if len(response) <= max_length:
                await self.send_response(message, response)
            else:
                # Split into multiple messages if needed
                await self._send_split_response(message, response, max_length)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error executing airplanes command: {e}")
            error_msg = self.translate('commands.airplanes.error', error=str(e))
            await self.send_response(message, error_msg)
            return False
    
    async def _send_split_response(self, message: MeshMessage, response: str, max_length: int):
        """Send response split into multiple messages if it exceeds max_length.
        
        Args:
            message: The message to respond to.
            response: The full response text.
            max_length: Maximum message length.
        """
        lines = response.split('\n')
        current_message = ""
        message_count = 0
        
        for i, line in enumerate(lines):
            # Check if adding this line would exceed max_length
            if len(current_message) + len(line) + 1 > max_length:  # +1 for newline
                # Send current message and start new one
                if current_message:
                    # Per-user rate limit applies only to first message (trigger); skip for continuations
                    await self.send_response(
                        message, current_message.rstrip(),
                        skip_user_rate_limit=(message_count > 0)
                    )
                    await asyncio.sleep(2.0)  # Delay between messages
                    message_count += 1
                
                # Start new message
                current_message = line
            else:
                # Add line to current message
                if current_message:
                    current_message += f"\n{line}"
                else:
                    current_message = line
        
        # Send the last message if there's content (continuation; skip per-user rate limit)
        if current_message:
            await self.send_response(message, current_message, skip_user_rate_limit=True)
