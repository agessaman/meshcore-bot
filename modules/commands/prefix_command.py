#!/usr/bin/env python3
"""
Prefix command for the MeshCore Bot
Handles repeater prefix lookups
"""

import asyncio
import aiohttp
import time
import json
import random
from typing import Dict, List, Optional, Any, Tuple
from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import abbreviate_location, format_location_for_display, calculate_distance


class PrefixCommand(BaseCommand):
    """Handles repeater prefix lookups"""
    
    # Plugin metadata
    name = "prefix"
    keywords = ['prefix', 'repeater', 'lookup']
    description = "Look up repeaters by two-character prefix (e.g., 'prefix 1A')"
    category = "meshcore_info"
    requires_dm = False
    cooldown_seconds = 2
    requires_internet = False  # Will be set to True in __init__ if API is configured
    
    def __init__(self, bot: Any):
        """Initialize the prefix command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.prefix_enabled = self.get_config_value('Prefix_Command', 'enabled', fallback=True, value_type='bool')
        # Get API URL from config, no fallback to regional API
        self.api_url = self.bot.config.get('External_Data', 'repeater_prefix_api_url', fallback="")
        
        # Only require internet if API is configured
        if self.api_url and self.api_url.strip():
            self.requires_internet = True
        self.cache_data = {}
        self.cache_timestamp = 0
        # Get cache duration from config, with fallback to 1 hour
        self.cache_duration = self.bot.config.getint('External_Data', 'repeater_prefix_cache_hours', fallback=1) * 3600
        self.session = None
        
        # Get geolocation settings from config
        self.show_repeater_locations = self.bot.config.getboolean('Prefix_Command', 'show_repeater_locations', fallback=True)
        self.use_reverse_geocoding = self.bot.config.getboolean('Prefix_Command', 'use_reverse_geocoding', fallback=True)
        self.hide_source = self.bot.config.getboolean('Prefix_Command', 'hide_source', fallback=False)
        
        # Get time window settings from config
        self.prefix_heard_days = self.bot.config.getint('Prefix_Command', 'prefix_heard_days', fallback=7)
        self.prefix_free_days = self.bot.config.getint('Prefix_Command', 'prefix_free_days', fallback=7)
        
        # Get bot location and radius filter settings
        self.bot_latitude = self.bot.config.getfloat('Bot', 'bot_latitude', fallback=None)
        self.bot_longitude = self.bot.config.getfloat('Bot', 'bot_longitude', fallback=None)
        self.max_prefix_range = self.bot.config.getfloat('Prefix_Command', 'max_prefix_range', fallback=200.0)
        
        # Check if we have valid bot location for distance filtering
        self.distance_filtering_enabled = (
            self.bot_latitude is not None and 
            self.bot_longitude is not None and
            self.max_prefix_range > 0
        )
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.prefix_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        """Get help text for the prefix command.
        
        Returns:
            str: The help text for this command.
        """
        location_note = self.translate('commands.prefix.location_note') if self.show_repeater_locations else ""
        if not self.api_url or self.api_url.strip() == "":
            return self.translate('commands.prefix.help_no_api', location_note=location_note)
        return self.translate('commands.prefix.help_api', location_note=location_note)
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with 'prefix' keyword"""
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Check if message starts with 'prefix' (with or without space)
        content_lower = content.lower()
        return content_lower == 'prefix' or content_lower.startswith('prefix ')
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the prefix command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Parse the command
        parts = content.split()
        if len(parts) < 2:
            response = self.get_help_text()
            return await self.send_response(message, response)
        
        command = parts[1].upper()
        
        # Handle refresh command
        if command == "REFRESH":
            if not self.api_url or self.api_url.strip() == "":
                response = self.translate('commands.prefix.refresh_not_available')
                return await self.send_response(message, response)
            await self.refresh_cache()
            response = self.translate('commands.prefix.cache_refreshed')
            return await self.send_response(message, response)
        
        # Handle free/available command
        if command == "FREE" or command == "AVAILABLE":
            free_prefixes, total_free, has_data = await self.get_free_prefixes()
            if not has_data:
                response = self.translate('commands.prefix.unable_determine_free')
                return await self.send_response(message, response)
            else:
                response = self.format_free_prefixes_response(free_prefixes, total_free)
                await self._send_prefix_response(message, response)
                return True
        
        # Check for "all" modifier
        include_all = False
        if len(parts) >= 3 and parts[2].upper() == "ALL":
            include_all = True
        
        # Validate prefix format
        if len(command) != 2 or not command.isalnum():
            response = self.translate('commands.prefix.invalid_format')
            return await self.send_response(message, response)
        
        # Get prefix data
        prefix_data = await self.get_prefix_data(command, include_all=include_all)
        
        if prefix_data is None:
            response = self.translate('commands.prefix.no_repeaters_found', prefix=command)
            return await self.send_response(message, response)
        
        # Add include_all flag to data for formatting
        prefix_data['include_all'] = include_all
        
        # Format response
        response = self.format_prefix_response(command, prefix_data)
        await self._send_prefix_response(message, response)
        return True
    
    async def get_prefix_data(self, prefix: str, include_all: bool = False) -> Optional[Dict[str, Any]]:
        """Get prefix data from API first, enhanced with local database location data
        
        Args:
            prefix: The two-character prefix to look up
            include_all: If True, show all repeaters regardless of last_heard time.
                        If False (default), only show repeaters heard within prefix_heard_days.
        """
        # Only refresh cache if API is configured
        if self.api_url and self.api_url.strip():
            current_time = time.time()
            if current_time - self.cache_timestamp > self.cache_duration:
                await self.refresh_cache()
        
        # Get API data first (prioritize comprehensive repeater data)
        api_data = None
        if self.api_url and self.api_url.strip() and prefix in self.cache_data:
            api_data = self.cache_data.get(prefix)
        
        # Get local database data for location enhancement
        db_data = await self.get_prefix_data_from_db(prefix, include_all=include_all)
        
        # If we have API data, enhance it with local location data
        if api_data and db_data:
            return self._enhance_api_data_with_locations(api_data, db_data)
        elif api_data:
            return api_data
        elif db_data:
            return db_data
        
        return None
    
    def _find_flexible_match(self, api_name: str, db_locations: Dict[str, str]) -> Optional[str]:
        """
        Find a flexible match for an API name in the database locations.
        
        Matching strategy:
        1. Exact match (highest priority)
        2. Version number variations (e.g., "Name v4" matches "Name")
        3. Partial match (e.g., "DN Field Repeater" matches "DN Field Repeater v4")
        
        Preserves numbered nodes (e.g., "Airhack 1" vs "Airhack 2" remain distinct)
        """
        # First try exact match
        if api_name in db_locations:
            return api_name
        
        # Try version number variations
        # Remove common version patterns: v1, v2, v3, v4, v5, etc.
        import re
        base_name = re.sub(r'\s+v\d+$', '', api_name, flags=re.IGNORECASE)
        
        if base_name != api_name:  # Version was removed
            # Try to find a database entry that matches the base name
            for db_name in db_locations.keys():
                if db_name.lower() == base_name.lower():
                    return db_name
                # Also try with version numbers
                for version in ['v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7', 'v8', 'v9']:
                    versioned_name = f"{base_name} {version}"
                    if db_name.lower() == versioned_name.lower():
                        return db_name
        
        # Try partial matching (but be careful with numbered nodes)
        # Only do partial matching if the API name is shorter than the DB name
        # This helps with cases like "DN Field Repeater" matching "DN Field Repeater v4"
        for db_name in db_locations.keys():
            # Check if API name is a prefix of DB name (but not vice versa)
            if (len(api_name) < len(db_name) and 
                db_name.lower().startswith(api_name.lower()) and
                # Avoid matching numbered nodes (e.g., "Airhack" shouldn't match "Airhack 1")
                not re.search(r'\s+\d+$', api_name)):  # API name doesn't end with a number
                return db_name
        
        return None
    
    def _enhance_api_data_with_locations(self, api_data: Dict[str, Any], db_data: Dict[str, Any]) -> Dict[str, Any]:
        """Enhance API data with location information from local database using flexible matching"""
        try:
            # Create a mapping of repeater names to location data from database
            db_locations = {}
            for db_repeater in db_data.get('node_names', []):
                # Extract name and location from database format: "Name (Location)"
                if ' (' in db_repeater and db_repeater.endswith(')'):
                    name, location = db_repeater.rsplit(' (', 1)
                    location = location.rstrip(')')
                    # Store just the city/neighborhood part (not full location)
                    db_locations[name] = location
                else:
                    # No location data in database
                    db_locations[db_repeater] = None
            
            # Enhance API node names with location data using flexible matching
            enhanced_names = []
            for api_name in api_data.get('node_names', []):
                # Try to find a flexible match
                matched_db_name = self._find_flexible_match(api_name, db_locations)
                
                if matched_db_name and db_locations[matched_db_name]:
                    # Use the API name but add location from database
                    enhanced_name = f"{api_name} ({db_locations[matched_db_name]})"
                else:
                    enhanced_name = api_name
                enhanced_names.append(enhanced_name)
            
            # Return enhanced API data
            enhanced_data = api_data.copy()
            enhanced_data['node_names'] = enhanced_names
            # Keep original source - we're just caching geocoding results
            
            return enhanced_data
            
        except Exception as e:
            self.logger.error(f"Error enhancing API data with locations: {e}")
            # Return original API data if enhancement fails
            return api_data
    
    async def refresh_cache(self) -> None:
        """Refresh the cache from the API."""
        try:
            # Check if API URL is configured
            if not self.api_url or self.api_url.strip() == "":
                self.logger.info("Repeater prefix API URL not configured - skipping API refresh")
                return
            
            self.logger.info("Refreshing repeater prefix cache from API")
            
            # Create session if it doesn't exist
            if self.session is None:
                self.session = aiohttp.ClientSession()
            
            # Fetch data from API
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.get(self.api_url, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Clear existing cache
                    self.cache_data.clear()
                    
                    # Process and cache the data
                    for item in data.get('data', []):
                        prefix = item.get('prefix', '').upper()
                        if prefix:
                            self.cache_data[prefix] = {
                                'node_count': int(item.get('node_count', 0)),
                                'node_names': item.get('node_names', [])
                            }
                    
                    self.cache_timestamp = time.time()
                    self.logger.info(f"Cache refreshed with {len(self.cache_data)} prefixes")
                    
                else:
                    self.logger.error(f"API request failed with status {response.status}")
                    
        except asyncio.TimeoutError:
            self.logger.error("API request timed out")
        except aiohttp.ClientError as e:
            self.logger.error(f"API request failed: {e}")
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse API response: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error refreshing cache: {e}")
    
    async def get_prefix_data_from_db(self, prefix: str, include_all: bool = False) -> Optional[Dict[str, Any]]:
        """Get prefix data from the bot's SQLite database as fallback
        
        Args:
            prefix: The two-character prefix to look up
            include_all: If True, show all repeaters regardless of last_heard time.
                        If False (default), only show repeaters heard within prefix_heard_days.
        """
        try:
            if include_all:
                self.logger.info(f"Looking up prefix '{prefix}' in local database (all entries)")
            else:
                self.logger.info(f"Looking up prefix '{prefix}' in local database (last {self.prefix_heard_days} days)")
            
            # Query the complete_contact_tracking table for repeaters with matching prefix
            # By default, only include repeaters heard within prefix_heard_days
            # If include_all is True, include all repeaters regardless of last_heard time
            if include_all:
                query = '''
                    SELECT name, public_key, device_type, last_heard as last_seen, latitude, longitude, city, state, country, role
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                    ORDER BY name
                '''
            else:
                query = f'''
                    SELECT name, public_key, device_type, last_heard as last_seen, latitude, longitude, city, state, country, role
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                    AND last_heard >= datetime('now', '-{self.prefix_heard_days} days')
                    ORDER BY name
                '''
            
            # The prefix should match the first two characters of the public key
            prefix_pattern = f"{prefix}%"
            
            results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
            
            if not results:
                self.logger.info(f"No repeaters found in database with prefix '{prefix}'")
                return None
            
            # Extract node names and count, filtering by distance if enabled
            node_names = []
            for row in results:
                # Filter by distance if distance filtering is enabled
                if self.distance_filtering_enabled:
                    # Check if repeater has valid coordinates
                    if (row['latitude'] is not None and 
                        row['longitude'] is not None and
                        not (row['latitude'] == 0.0 and row['longitude'] == 0.0)):
                        distance = calculate_distance(
                            self.bot_latitude, self.bot_longitude,
                            row['latitude'], row['longitude']
                        )
                        # Skip repeaters beyond maximum range
                        if distance > self.max_prefix_range:
                            continue
                    # Note: Repeaters without coordinates are included (can't filter unknown locations)
                
                name = row['name']
                device_type = row['device_type']
                
                # Add device type indicator for clarity
                if device_type == 2:
                    name += self.translate('commands.prefix.device_repeater')
                elif device_type == 3:
                    name += self.translate('commands.prefix.device_roomserver')
                
                # Add location information if enabled and available
                if self.show_repeater_locations:
                    # Use the utility function to format location with abbreviation
                    location_str = format_location_for_display(
                        city=row['city'],
                        state=row['state'],
                        country=row['country'],
                        max_length=20  # Reasonable limit for location in prefix output
                    )
                    
                    # If we have coordinates but no city, try reverse geocoding
                    # Skip 0,0 coordinates as they indicate "hidden" location
                    if (not location_str and 
                        row['latitude'] is not None and 
                        row['longitude'] is not None and 
                        not (row['latitude'] == 0.0 and row['longitude'] == 0.0) and
                        self.use_reverse_geocoding):
                        try:
                            # Use the enhanced reverse geocoding from repeater manager
                            if hasattr(self.bot, 'repeater_manager'):
                                city = self.bot.repeater_manager._get_city_from_coordinates(
                                    row['latitude'], row['longitude']
                                )
                                if city:
                                    location_str = abbreviate_location(city, 20)
                            else:
                                # Fallback to basic geocoding
                                from ..utils import rate_limited_nominatim_reverse_sync
                                location = rate_limited_nominatim_reverse_sync(
                                    self.bot, f"{row['latitude']}, {row['longitude']}", timeout=10
                                )
                                if location:
                                    address = location.raw.get('address', {})
                                    # Try neighborhood first, then city, then town, etc.
                                    raw_location = (address.get('neighbourhood') or
                                                  address.get('suburb') or
                                                  address.get('city') or
                                                  address.get('town') or
                                                  address.get('village') or
                                                  address.get('hamlet') or
                                                  address.get('municipality'))
                                    if raw_location:
                                        location_str = abbreviate_location(raw_location, 20)
                        except Exception as e:
                            self.logger.debug(f"Error reverse geocoding {row['latitude']}, {row['longitude']}: {e}")
                    
                    # Add location to name if we have any location info
                    if location_str:
                        name += f" ({location_str})"
                
                node_names.append(name)
            
            self.logger.info(f"Found {len(node_names)} repeaters in database with prefix '{prefix}'")
            
            return {
                'node_count': len(node_names),
                'node_names': node_names,
                'source': 'database'
            }
            
        except Exception as e:
            self.logger.error(f"Error querying database for prefix '{prefix}': {e}")
            return None
    
    
    async def get_free_prefixes(self) -> Tuple[List[str], int, bool]:
        """Get list of available (unused) prefixes and total count
        
        Returns:
            Tuple of (selected_prefixes, total_free, has_data)
            - selected_prefixes: List of up to 10 randomly selected free prefixes
            - total_free: Total number of free prefixes
            - has_data: True if we have valid data (from cache or database), False otherwise
        """
        try:
            # Get all used prefixes - prioritize API cache over database
            used_prefixes = set()
            has_data = False
            
            # Always try to refresh cache if it's empty or stale (only if API URL is configured)
            current_time = time.time()
            if self.api_url and self.api_url.strip():
                if not self.cache_data or current_time - self.cache_timestamp > self.cache_duration:
                    self.logger.info("Refreshing cache for free prefixes lookup")
                    await self.refresh_cache()
            
            # Prioritize API cache - if we have API data and API is configured, use it exclusively
            # The API is the authoritative source and matches what MeshCore Analyzer shows
            if self.api_url and self.api_url.strip() and self.cache_data:
                for prefix in self.cache_data.keys():
                    used_prefixes.add(prefix.upper())
                has_data = True
                self.logger.info(f"Found {len(used_prefixes)} used prefixes from API cache")
            else:
                # Fallback to database only if API cache is unavailable
                # When using database, use prefix_free_days to filter which prefixes are considered "used"
                # Only repeaters heard within prefix_free_days will be considered as using a prefix
                try:
                    # If distance filtering is enabled, we need location data to filter
                    if self.distance_filtering_enabled:
                        query = f'''
                            SELECT DISTINCT SUBSTR(public_key, 1, 2) as prefix, latitude, longitude
                            FROM complete_contact_tracking 
                            WHERE role IN ('repeater', 'roomserver')
                            AND LENGTH(public_key) >= 2
                            AND last_heard >= datetime('now', '-{self.prefix_free_days} days')
                        '''
                    else:
                        query = f'''
                            SELECT DISTINCT SUBSTR(public_key, 1, 2) as prefix
                            FROM complete_contact_tracking 
                            WHERE role IN ('repeater', 'roomserver')
                            AND LENGTH(public_key) >= 2
                            AND last_heard >= datetime('now', '-{self.prefix_free_days} days')
                        '''
                    results = self.bot.db_manager.execute_query(query)
                    for row in results:
                        prefix = row['prefix'].upper()
                        if len(prefix) == 2:
                            # Filter by distance if enabled
                            if self.distance_filtering_enabled:
                                # Check if repeater has valid coordinates
                                if (row.get('latitude') is not None and 
                                    row.get('longitude') is not None and
                                    not (row.get('latitude') == 0.0 and row.get('longitude') == 0.0)):
                                    distance = calculate_distance(
                                        self.bot_latitude, self.bot_longitude,
                                        row['latitude'], row['longitude']
                                    )
                                    # Skip repeaters beyond maximum range
                                    if distance > self.max_prefix_range:
                                        continue
                                # Note: Repeaters without coordinates are included in used prefixes (conservative approach)
                            used_prefixes.add(prefix)
                    has_data = True
                    self.logger.info(f"Found {len(used_prefixes)} used prefixes from database (fallback)")
                except Exception as e:
                    self.logger.warning(f"Error getting prefixes from database: {e}")
            
            # If we don't have any data from either source, return early
            if not has_data:
                self.logger.warning("No data available for free prefixes lookup (empty cache and database)")
                return [], 0, False
            
            # Generate all valid hex prefixes (01-FE, excluding 00 and FF)
            all_prefixes = []
            for i in range(1, 255):  # 1 to 254 (exclude 0 and 255)
                prefix = f"{i:02X}"
                all_prefixes.append(prefix)
            
            # Find free prefixes
            free_prefixes = []
            for prefix in all_prefixes:
                if prefix not in used_prefixes:
                    free_prefixes.append(prefix)
            
            self.logger.info(f"Found {len(free_prefixes)} free prefixes out of {len(all_prefixes)} total valid prefixes")
            
            # Randomly select up to 10 free prefixes
            total_free = len(free_prefixes)
            if len(free_prefixes) <= 10:
                selected_prefixes = free_prefixes
            else:
                selected_prefixes = random.sample(free_prefixes, 10)
            
            return selected_prefixes, total_free, True
            
        except Exception as e:
            self.logger.error(f"Error getting free prefixes: {e}")
            return [], 0, False
    
    def format_free_prefixes_response(self, free_prefixes: List[str], total_free: int) -> str:
        """Format the free prefixes response.
        
        Args:
            free_prefixes: List of free prefixes to display.
            total_free: Total count of free prefixes.
            
        Returns:
            str: Formatted response string.
        """
        if not free_prefixes:
            return self.translate('commands.prefix.no_free_prefixes')
        
        response = self.translate('commands.prefix.available_prefixes', shown=len(free_prefixes), total=total_free) + "\n"
        
        # Format as a grid for better readability
        for i, prefix in enumerate(free_prefixes, 1):
            response += f"{prefix}"
            if i % 5 == 0:  # New line every 5 prefixes
                response += "\n"
            elif i < len(free_prefixes):  # Add space if not the last item
                response += " "
        
        # Add newline if the last line wasn't complete
        if len(free_prefixes) % 5 != 0:
            response += "\n"
        
        response += "\n" + self.translate('commands.prefix.generate_key')
        
        return response
    
    def format_prefix_response(self, prefix: str, data: Dict[str, Any]) -> str:
        """Format the prefix response.
        
        Args:
            prefix: The prefix being queried.
            data: The prefix data dictionary.
            
        Returns:
            str: Formatted response string.
        """
        node_count = data['node_count']
        node_names = data['node_names']
        source = data.get('source', 'api')
        include_all = data.get('include_all', True)  # Default to True for API responses
        
        # Get bot name for database responses
        bot_name = self.bot.config.get('Bot', 'bot_name', fallback='Bot')
        
        # Handle pluralization
        plural = 's' if node_count != 1 else ''
        
        if source == 'database':
            # Database response format - keep brief for character limit
            if include_all:
                response = self.translate('commands.prefix.prefix_db_all', prefix=prefix, count=node_count, plural=plural) + "\n"
            else:
                # Show time period for default behavior - use abbreviated form
                days_str = f"{self.prefix_heard_days}d" if self.prefix_heard_days != 7 else "7d"
                response = self.translate('commands.prefix.prefix_db_recent', prefix=prefix, count=node_count, plural=plural, days=days_str) + "\n"
        else:
            # API response format
            response = self.translate('commands.prefix.prefix_api', prefix=prefix, count=node_count, plural=plural) + "\n"
        
        for i, name in enumerate(node_names, 1):
            response += self.translate('commands.prefix.item_format', index=i, name=name) + "\n"
        
        # Add source info (unless hidden by config)
        if not self.hide_source:
            if source == 'database':
                # No additional info needed for database responses
                pass
            else:
                # Add API source info - extract domain from API URL
                try:
                    from urllib.parse import urlparse
                    parsed_url = urlparse(self.api_url)
                    domain = parsed_url.netloc
                    response += "\n" + self.translate('commands.prefix.source_domain', domain=domain)
                except Exception:
                    # Fallback if URL parsing fails
                    response += "\n" + self.translate('commands.prefix.source_api')
        else:
            # Remove trailing newline when source is hidden
            response = response.rstrip('\n')
        
        return response
    
    async def _send_prefix_response(self, message: MeshMessage, response: str) -> None:
        """Send prefix response, splitting into multiple messages if necessary.
        
        Args:
            message: The original message to respond to.
            response: The complete response string.
        """
        # Store the complete response for web viewer integration BEFORE splitting
        # command_manager will prioritize command.last_response over _last_response
        # This ensures capture_command gets the full response, not just the last split message
        self.last_response = response
        
        # Get dynamic max message length based on message type and bot username
        max_length = self.get_max_message_length(message)
        
        if len(response) <= max_length:
            # Single message is fine
            await self.send_response(message, response)
        else:
            # Split into multiple messages for over-the-air transmission
            # But keep the full response in last_response for web viewer
            lines = response.split('\n')
            
            # Calculate continuation markers length for planning
            continuation_end = self.translate('commands.path.continuation_end')
            continuation_start_template = self.translate('commands.path.continuation_start', line='PLACEHOLDER')
            # Estimate continuation overhead (account for variable line length in template)
            continuation_overhead = len(continuation_end) + len(continuation_start_template) - len('PLACEHOLDER')
            
            # Estimate how many messages we'll need based on total content
            total_content_length = sum(len(line) for line in lines) + (len(lines) - 1)  # +1 for each newline between lines
            # Account for continuation markers in multi-message scenarios
            estimated_messages = max(2, (total_content_length + continuation_overhead * 2) // max(max_length - continuation_overhead, 1) + 1)
            target_lines_per_message = max(1, (len(lines) + estimated_messages - 1) // estimated_messages)  # Ceiling division
            
            current_message = ""
            message_count = 0
            lines_in_current = 0
            
            for i, line in enumerate(lines):
                # Calculate if adding this line would exceed max_length
                test_message = current_message
                if test_message:
                    test_message += f"\n{line}"
                else:
                    test_message = line
                
                # Determine if we should split
                must_split = len(test_message) > max_length
                
                # Smart splitting: try to balance lines across messages
                # Calculate remaining lines and messages for balancing
                remaining_lines = len(lines) - i - 1  # Lines after current one
                remaining_messages = estimated_messages - message_count - 1  # Messages after current one
                
                # For first message, be more aggressive about fitting lines (prioritize earlier messages)
                # For subsequent messages, balance more evenly
                if message_count == 0:
                    # First message: try to fit more lines, only split if we must
                    # Be very conservative about splitting the first message - only if we absolutely must
                    # or if we're way over target and have plenty of room left
                    first_message_target = max(target_lines_per_message, 3)  # At least 3 lines in first message if possible
                    should_balance_split = (
                        lines_in_current >= first_message_target and  # We've hit minimum target for first message
                        remaining_lines > 0 and  # There are more lines
                        remaining_messages > 0 and  # There are more messages
                        len(test_message) > max_length * 0.95  # Very close to limit (95% threshold - be aggressive)
                    )
                else:
                    # Subsequent messages: balance more evenly, but still try to fit reasonable amounts
                    should_balance_split = (
                        lines_in_current >= max(target_lines_per_message, 2) and  # At least 2 lines per message
                        remaining_lines > 0 and  # There are more lines
                        remaining_messages > 0 and  # There are more messages
                        (remaining_lines >= remaining_messages or lines_in_current >= target_lines_per_message + 1) and  # Can distribute or we've exceeded target
                        len(test_message) > max_length * 0.88  # Getting close to limit (88% threshold)
                    )
                
                if (must_split or should_balance_split) and current_message:
                    # Send current message and start new one
                    # Add ellipsis on new line to end of continued message
                    current_message += continuation_end
                    await self.send_response(message, current_message.rstrip())
                    await asyncio.sleep(3.0)  # Delay between messages (same as other commands)
                    message_count += 1
                    lines_in_current = 0
                    
                    # Start new message with ellipsis on new line at beginning
                    current_message = self.translate('commands.path.continuation_start', line=line)
                    lines_in_current = 1
                else:
                    # Add line to current message
                    if current_message:
                        current_message += f"\n{line}"
                    else:
                        current_message = line
                    lines_in_current += 1
            
            # Send the last message if there's content
            if current_message:
                await self.send_response(message, current_message)
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()
