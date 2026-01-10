#!/usr/bin/env python3
"""
Path Decode Command for the MeshCore Bot
Decodes hex path data to show which repeaters were involved in message routing
"""

import re
import time
import asyncio
from typing import List, Optional, Dict, Any, Tuple
from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import calculate_distance


class PathCommand(BaseCommand):
    """Command for decoding path data to repeater names"""
    
    # Plugin metadata
    name = "path"
    keywords = ["path", "decode", "route"]
    description = "Decode hex path data to show which repeaters were involved in message routing"
    requires_dm = False
    cooldown_seconds = 1
    category = "meshcore_info"
    
    def __init__(self, bot):
        super().__init__(bot)
        self.path_enabled = self.get_config_value('Path_Command', 'enabled', fallback=True, value_type='bool')
        # Get bot location from config for geographic proximity calculations
        # Check if geographic guessing is enabled (bot has location configured)
        self.geographic_guessing_enabled = False
        self.bot_latitude = None
        self.bot_longitude = None
        
        # Get proximity calculation method from config
        self.proximity_method = bot.config.get('Path_Command', 'proximity_method', fallback='simple')
        self.path_proximity_fallback = bot.config.getboolean('Path_Command', 'path_proximity_fallback', fallback=True)
        self.max_proximity_range = bot.config.getfloat('Path_Command', 'max_proximity_range', fallback=200.0)
        self.max_repeater_age_days = bot.config.getint('Path_Command', 'max_repeater_age_days', fallback=14)
        
        # Get recency/proximity weighting (0.0 to 1.0, where 1.0 = 100% recency, 0.0 = 100% proximity)
        # Default 0.4 means 40% recency, 60% proximity (more balanced for path routing)
        recency_weight = bot.config.getfloat('Path_Command', 'recency_weight', fallback=0.4)
        self.recency_weight = max(0.0, min(1.0, recency_weight))  # Clamp to 0.0-1.0
        self.proximity_weight = 1.0 - self.recency_weight
        
        # Get star bias multiplier (how much to boost starred repeaters' scores)
        # Default 2.5 means starred repeaters get 2.5x their normal score
        self.star_bias_multiplier = bot.config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5)
        self.star_bias_multiplier = max(1.0, self.star_bias_multiplier)  # Ensure at least 1.0
        
        # Get confidence indicator symbols from config
        self.high_confidence_symbol = bot.config.get('Path_Command', 'high_confidence_symbol', fallback='🎯')
        self.medium_confidence_symbol = bot.config.get('Path_Command', 'medium_confidence_symbol', fallback='📍')
        self.low_confidence_symbol = bot.config.get('Path_Command', 'low_confidence_symbol', fallback='❓')
        
        # Check if "p" shortcut is enabled (off by default)
        self.enable_p_shortcut = bot.config.getboolean('Path_Command', 'enable_p_shortcut', fallback=False)
        if self.enable_p_shortcut:
            # Add "p" to keywords if enabled
            if "p" not in self.keywords:
                self.keywords.append("p")
        
        try:
            # Try to get location from Bot section
            if bot.config.has_section('Bot'):
                lat = bot.config.getfloat('Bot', 'bot_latitude', fallback=None)
                lon = bot.config.getfloat('Bot', 'bot_longitude', fallback=None)
                
                if lat is not None and lon is not None:
                    # Validate coordinates
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        self.bot_latitude = lat
                        self.bot_longitude = lon
                        self.geographic_guessing_enabled = True
                        self.logger.info(f"Geographic proximity guessing enabled with bot location: {lat:.4f}, {lon:.4f}")
                        self.logger.info(f"Proximity method: {self.proximity_method}")
                        self.logger.info(f"Max repeater age: {self.max_repeater_age_days} days")
                    else:
                        self.logger.warning(f"Invalid bot coordinates in config: {lat}, {lon}")
                else:
                    self.logger.info("Bot location not configured - geographic proximity guessing disabled")
            else:
                self.logger.info("Bot section not found - geographic proximity guessing disabled")
        except Exception as e:
            self.logger.warning(f"Error reading bot location from config: {e} - geographic proximity guessing disabled")
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.path_enabled:
            return False
        return super().can_execute(message)
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with 'path' keyword or 'p' shortcut (if enabled)"""
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        content_lower = content.lower()
        
        # Handle "p" shortcut if enabled
        if self.enable_p_shortcut:
            if content_lower == "p":
                return True  # Just "p" by itself
            elif (content.startswith('p ') or content.startswith('P ')) and len(content) > 2:
                return True  # "p " followed by path data
        
        # Check if message starts with any of our keywords
        for keyword in self.keywords:
            # Check for exact match or keyword followed by space
            if content_lower == keyword or content_lower.startswith(keyword + ' '):
                return True
        return False
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute path decode command"""
        self.logger.info(f"Path command executed with content: {message.content}")
        
        # Store the current message for use in _extract_path_from_recent_messages
        self._current_message = message
        
        # Parse the message content to extract path data
        content = message.content.strip()
        parts = content.split()
        
        if len(parts) < 2:
            # No arguments provided - try to extract path from current message
            response = await self._extract_path_from_recent_messages()
        else:
            # Extract path data from the command
            path_input = " ".join(parts[1:])
            response = await self._decode_path(path_input)
        
        # Send the response (may be split into multiple messages if long)
        await self._send_path_response(message, response)
        return True
    
    async def _decode_path(self, path_input: str) -> str:
        """Decode hex path data to repeater names"""
        try:
            # Parse the path input - handle various formats
            # Examples: "11,98,a4,49,cd,5f,01" or "11 98 a4 49 cd 5f 01" or "1198a449cd5f01"
            path_input = path_input.replace(',', ' ').replace(':', ' ')
            
            # Extract hex values using regex
            hex_pattern = r'[0-9a-fA-F]{2}'
            hex_matches = re.findall(hex_pattern, path_input)
            
            if not hex_matches:
                return self.translate('commands.path.no_valid_hex')
            
            # Convert to uppercase for consistency
            # hex_matches preserves the order from the original path
            node_ids = [match.upper() for match in hex_matches]
            
            self.logger.info(f"Decoding path with {len(node_ids)} nodes: {','.join(node_ids)}")
            
            # Look up repeater names for each node ID (order preserved)
            repeater_info = await self._lookup_repeater_names(node_ids)
            
            # Format the response
            return self._format_path_response(node_ids, repeater_info)
            
        except Exception as e:
            self.logger.error(f"Error decoding path: {e}")
            return self.translate('commands.path.error_decoding', error=str(e))
    
    async def _lookup_repeater_names(self, node_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Look up repeater names for given node IDs"""
        repeater_info = {}
        
        try:
            # Skip API cache for path decoding - use database with improved proximity logic
            # API cache doesn't have recency-based proximity selection needed for path decoding
            api_data = None
            
            # Query the database for repeaters with matching prefixes
            # Node IDs are typically the first 2 characters of the public key
            for node_id in node_ids:
                # First try complete tracking database (all heard contacts, filtered by role)
                if hasattr(self.bot, 'repeater_manager'):
                    try:
                        # Get repeater devices from complete database (repeaters and roomservers)
                        complete_db = await self.bot.repeater_manager.get_repeater_devices(include_historical=True)
                        
                        results = []
                        for row in complete_db:
                            if row['public_key'].startswith(node_id):
                                results.append({
                                    'name': row['name'],
                                    'public_key': row['public_key'],
                                    'device_type': row['device_type'],
                                    'last_seen': row['last_heard'],
                                    'last_heard': row['last_heard'],  # Include last_heard for recency calculation
                                    'last_advert_timestamp': row.get('last_advert_timestamp'),  # Include last_advert_timestamp for recency calculation
                                    'is_active': row['is_currently_tracked'],
                                    'latitude': row['latitude'],
                                    'longitude': row['longitude'],
                                    'city': row['city'],
                                    'state': row['state'],
                                    'country': row['country'],
                                    'advert_count': row['advert_count'],
                                    'signal_strength': row['signal_strength'],
                                    'hop_count': row['hop_count'],
                                    'role': row['role'],
                                    'is_starred': bool(row.get('is_starred', 0))  # Include star status for bias
                                })
                    except Exception as e:
                        self.logger.debug(f"Error getting complete database: {e}")
                        results = []
                
                # If complete tracking database failed, try direct query to complete_contact_tracking
                if not results:
                    try:
                        # Build query with age filtering if configured
                        # Use last_advert_timestamp if available, otherwise fall back to last_heard
                        if self.max_repeater_age_days > 0:
                            query = '''
                                SELECT name, public_key, device_type, last_heard, last_heard as last_seen, 
                                       last_advert_timestamp, latitude, longitude, city, state, country,
                                       advert_count, signal_strength, hop_count, role, is_starred
                                FROM complete_contact_tracking 
                                WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                                AND (
                                    (last_advert_timestamp IS NOT NULL AND last_advert_timestamp >= datetime('now', '-{} days'))
                                    OR (last_advert_timestamp IS NULL AND last_heard >= datetime('now', '-{} days'))
                                )
                                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                            '''.format(self.max_repeater_age_days, self.max_repeater_age_days)
                        else:
                            query = '''
                                SELECT name, public_key, device_type, last_heard, last_heard as last_seen, 
                                       last_advert_timestamp, latitude, longitude, city, state, country,
                                       advert_count, signal_strength, hop_count, role, is_starred
                                FROM complete_contact_tracking 
                                WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                            '''
                        
                        prefix_pattern = f"{node_id}%"
                        results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                        
                        # Convert results to expected format
                        if results:
                            results = [
                                {
                                    'name': row['name'],
                                    'public_key': row['public_key'],
                                    'device_type': row['device_type'],
                                    'last_seen': row['last_seen'],
                                    'last_heard': row.get('last_heard', row['last_seen']),  # Include last_heard for recency calculation
                                    'last_advert_timestamp': row.get('last_advert_timestamp'),  # Include last_advert_timestamp for recency calculation
                                    'is_active': True,  # Assume active for path purposes
                                    'latitude': row['latitude'],
                                    'longitude': row['longitude'],
                                    'city': row['city'],
                                    'state': row['state'],
                                    'country': row['country'],
                                    'advert_count': row.get('advert_count', 0),
                                    'signal_strength': row.get('signal_strength'),
                                    'hop_count': row.get('hop_count'),
                                    'role': row.get('role'),
                                    'is_starred': bool(row.get('is_starred', 0))  # Include star status for bias
                                } for row in results
                            ]
                    except Exception as e:
                        self.logger.debug(f"Error querying complete_contact_tracking directly: {e}")
                        results = []
                
                if results:
                    # Build repeaters_data with all necessary fields
                    repeaters_data = [
                        {
                            'name': row['name'],
                            'public_key': row['public_key'],
                            'device_type': row['device_type'],
                            'last_seen': row['last_seen'],
                            'last_heard': row.get('last_heard', row['last_seen']),  # Include last_heard for recency calculation
                            'last_advert_timestamp': row.get('last_advert_timestamp'),  # Include last_advert_timestamp for recency calculation
                            'is_active': row['is_active'],
                            'latitude': row['latitude'],
                            'longitude': row['longitude'],
                            'city': row['city'],
                            'state': row['state'],
                            'country': row['country'],
                            'is_starred': row.get('is_starred', False)  # Include star status for bias
                        } for row in results
                    ]
                    
                    # Filter out repeaters with very low recency scores BEFORE collision detection
                    # This prevents old repeaters from causing false collisions
                    scored_repeaters = self._calculate_recency_weighted_scores(repeaters_data)
                    min_recency_threshold = 0.01  # Approximately 55 hours ago or less
                    recent_repeaters = [r for r, score in scored_repeaters if score >= min_recency_threshold]
                    
                    # Check for ID collisions (multiple repeaters with same prefix) AFTER filtering
                    if len(recent_repeaters) > 1:
                        # Multiple recent matches - try geographic proximity selection
                        # Only attempt if geographic guessing is enabled
                        if self.geographic_guessing_enabled:
                            # Get sender location if available (for first repeater selection)
                            sender_location = self._get_sender_location()
                            selected_repeater, confidence = self._select_repeater_by_proximity(recent_repeaters, node_id, node_ids, sender_location)
                            
                            if selected_repeater and confidence >= 0.5:
                                # High confidence geographic selection
                                repeater_info[node_id] = {
                                    'name': selected_repeater['name'],
                                    'public_key': selected_repeater['public_key'],
                                    'device_type': selected_repeater['device_type'],
                                    'last_seen': selected_repeater['last_seen'],
                                    'is_active': selected_repeater['is_active'],
                                    'found': True,
                                    'collision': False,
                                    'geographic_guess': True,
                                    'confidence': confidence
                                }
                            else:
                                # Low confidence or no geographic data - show collision warning
                                repeater_info[node_id] = {
                                    'found': True,
                                    'collision': True,
                                    'matches': len(recent_repeaters),
                                    'node_id': node_id,
                                    'repeaters': recent_repeaters
                                }
                        else:
                            # Geographic guessing disabled - show collision warning
                            repeater_info[node_id] = {
                                'found': True,
                                'collision': True,
                                'matches': len(recent_repeaters),
                                'node_id': node_id,
                                'repeaters': recent_repeaters
                            }
                    elif len(recent_repeaters) == 1:
                        # Single recent match after filtering - no choice made, so no confidence indicator
                        repeater = recent_repeaters[0]
                        repeater_info[node_id] = {
                            'name': repeater['name'],
                            'public_key': repeater['public_key'],
                            'device_type': repeater['device_type'],
                            'last_seen': repeater['last_seen'],
                            'is_active': repeater['is_active'],
                            'found': True,
                            'collision': False
                        }
                    else:
                        # All repeaters filtered out (too old) - show as not found
                        repeater_info[node_id] = {
                            'found': False,
                            'node_id': node_id
                        }
                else:
                    # Also check device contacts for active repeaters
                    device_matches = []
                    if hasattr(self.bot.meshcore, 'contacts'):
                        for contact_key, contact_data in self.bot.meshcore.contacts.items():
                            public_key = contact_data.get('public_key', contact_key)
                            if public_key.startswith(node_id):
                                # Check if this is a repeater
                                if hasattr(self.bot, 'repeater_manager') and self.bot.repeater_manager._is_repeater_device(contact_data):
                                    name = contact_data.get('adv_name', contact_data.get('name', self.translate('commands.path.unknown_name')))
                                    device_matches.append({
                                        'name': name,
                                        'public_key': public_key,
                                        'device_type': contact_data.get('type', 'Unknown'),
                                        'last_seen': 'Active',
                                        'is_active': True,
                                        'source': 'device'
                                    })
                    
                    if device_matches:
                        if len(device_matches) > 1:
                            # Multiple device matches - show collision warning
                            repeater_info[node_id] = {
                                'found': True,
                                'collision': True,
                                'matches': len(device_matches),
                                'node_id': node_id,
                                'repeaters': device_matches
                            }
                        else:
                            # Single device match
                            match = device_matches[0]
                            repeater_info[node_id] = {
                                'name': match['name'],
                                'public_key': match['public_key'],
                                'device_type': match['device_type'],
                                'last_seen': match['last_seen'],
                                'is_active': match['is_active'],
                                'found': True,
                                'collision': False,
                                'source': 'device'
                            }
                    else:
                        repeater_info[node_id] = {
                            'found': False,
                            'node_id': node_id
                        }
        
        except Exception as e:
            self.logger.error(f"Error looking up repeater names: {e}")
            # Return basic info for all nodes
            for node_id in node_ids:
                repeater_info[node_id] = {
                    'found': False,
                    'node_id': node_id,
                    'error': str(e)
                }
        
        return repeater_info
    
    async def _get_api_cache_data(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Get API cache data from the prefix command if available"""
        try:
            # Try to get the prefix command instance and its cache data
            if hasattr(self.bot, 'command_manager'):
                prefix_cmd = self.bot.command_manager.commands.get('prefix')
                if prefix_cmd and hasattr(prefix_cmd, 'cache_data'):
                    # Check if cache is valid
                    current_time = time.time()
                    if current_time - prefix_cmd.cache_timestamp > prefix_cmd.cache_duration:
                        await prefix_cmd.refresh_cache()
                    return prefix_cmd.cache_data
        except Exception as e:
            self.logger.warning(f"Could not get API cache data: {e}")
        return None
    
    
    def _get_sender_location(self) -> Optional[Tuple[float, float]]:
        """Get sender location from current message if available"""
        try:
            if not hasattr(self, '_current_message') or not self._current_message:
                return None
            
            sender_pubkey = self._current_message.sender_pubkey
            if not sender_pubkey:
                return None
            
            # Look up sender location from database (any role, not just repeaters)
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
            self.logger.debug(f"Error getting sender location: {e}")
            return None
    
    def _select_repeater_by_proximity(self, repeaters: List[Dict[str, Any]], node_id: str = None, path_context: List[str] = None, sender_location: Optional[Tuple[float, float]] = None) -> Tuple[Optional[Dict[str, Any]], float]:
        """
        Select the most likely repeater based on geographic proximity.
        
        Args:
            repeaters: List of repeaters to choose from
            node_id: The current node ID being processed
            path_context: Full path for context (for path proximity method)
            sender_location: Optional sender location (for first repeater selection)
        
        Returns:
            Tuple of (selected_repeater, confidence_score)
            confidence_score: 0.0 to 1.0, where 1.0 is very confident
        """
        if not repeaters:
            return None, 0.0
        
        # Check if geographic guessing is enabled
        if not self.geographic_guessing_enabled:
            return None, 0.0
        
        # Filter repeaters that have location data
        repeaters_with_location = []
        for repeater in repeaters:
            lat = repeater.get('latitude')
            lon = repeater.get('longitude')
            if lat is not None and lon is not None:
                # Skip 0,0 coordinates (hidden location)
                if not (lat == 0.0 and lon == 0.0):
                    repeaters_with_location.append(repeater)
        
        # If no repeaters have location data, we can't make a geographic guess
        if not repeaters_with_location:
            return None, 0.0
        
        # Choose proximity calculation method
        if self.proximity_method == 'path' and path_context and node_id:
            result = self._select_by_path_proximity(repeaters_with_location, node_id, path_context, sender_location)
            if result[0] is not None:
                return result
            elif self.path_proximity_fallback:
                # Fall back to simple proximity if path proximity fails
                return self._select_by_simple_proximity(repeaters_with_location)
            else:
                return None, 0.0
        else:
            return self._select_by_simple_proximity(repeaters_with_location)
    
    def _select_by_simple_proximity(self, repeaters_with_location: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select repeater based on proximity to bot location with strong recency bias"""
        # Calculate recency-weighted scores for all repeaters
        scored_repeaters = self._calculate_recency_weighted_scores(repeaters_with_location)
        
        # Filter out repeaters with very low recency scores (too old to be considered)
        # Minimum recency score threshold: 0.01 (approximately 55 hours ago or less)
        # This prevents selecting repeaters that haven't advertised in several days
        min_recency_threshold = 0.01
        scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
        
        if not scored_repeaters:
            return None, 0.0  # No recent repeaters found
        
        # If only one repeater, check if it's within range
        if len(scored_repeaters) == 1:
            repeater, recency_score = scored_repeaters[0]
            distance = calculate_distance(
                self.bot_latitude, self.bot_longitude,
                repeater['latitude'], repeater['longitude']
            )
            # Apply maximum range threshold
            if self.max_proximity_range > 0 and distance > self.max_proximity_range:
                return None, 0.0  # Reject if beyond maximum range
            
            # Confidence based on recency score
            base_confidence = 0.4 + (recency_score * 0.5)  # 0.4 to 0.9 based on recency
            return repeater, base_confidence
        
        # Calculate combined proximity + recency scores
        combined_scores = []
        for repeater, recency_score in scored_repeaters:
            distance = calculate_distance(
                self.bot_latitude, self.bot_longitude,
                repeater['latitude'], repeater['longitude']
            )
            
            # Apply maximum range threshold
            if self.max_proximity_range > 0 and distance > self.max_proximity_range:
                continue  # Skip if beyond maximum range
            
            # Combined score: proximity (lower is better) + recency (higher is better)
            # Normalize distance to 0-1 scale (assuming max 1000km range)
            normalized_distance = min(distance / 1000.0, 1.0)
            proximity_score = 1.0 - normalized_distance  # Invert so closer = higher score
            
            # Use configurable weighting (default: 40% recency, 60% proximity)
            combined_score = (recency_score * self.recency_weight) + (proximity_score * self.proximity_weight)
            
            # Apply star bias multiplier if repeater is starred
            if repeater.get('is_starred', False):
                combined_score *= self.star_bias_multiplier
                self.logger.debug(f"Applied star bias ({self.star_bias_multiplier}x) to {repeater.get('name', 'unknown')}")
            
            combined_scores.append((combined_score, distance, repeater))
        
        if not combined_scores:
            return None, 0.0  # All repeaters beyond range
        
        # Sort by combined score (highest first)
        combined_scores.sort(key=lambda x: x[0], reverse=True)
        
        best_score, best_distance, best_repeater = combined_scores[0]
        
        # Calculate confidence based on score difference
        if len(combined_scores) == 1:
            confidence = 0.4 + (best_score * 0.5)  # 0.4 to 0.9 based on score
        else:
            second_best_score = combined_scores[1][0]
            score_ratio = best_score / second_best_score if second_best_score > 0 else 1.0
            
            # Higher confidence if there's a significant score difference
            if score_ratio > 1.5:  # Best is 50% better than second
                confidence = 0.9
            elif score_ratio > 1.2:  # Best is 20% better than second
                confidence = 0.8
            elif score_ratio > 1.1:  # Best is 10% better than second
                confidence = 0.7
            else:
                # Scores are too similar, use tie-breaker
                distances_for_tiebreaker = [(d, r) for _, d, r in combined_scores]
                selected_repeater = self._apply_tie_breakers(distances_for_tiebreaker)
                confidence = 0.5  # Moderate confidence for tie-breaker selection
                return selected_repeater, confidence
        
        return best_repeater, confidence
    
    def _calculate_recency_weighted_scores(self, repeaters: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], float]]:
        """Calculate recency-weighted scores for all repeaters (0.0 to 1.0, higher = more recent)"""
        from datetime import datetime, timedelta
        
        scored_repeaters = []
        now = datetime.now()
        
        for repeater in repeaters:
            # Get the most recent timestamp from multiple fields
            most_recent_time = None
            
            # Check last_heard from complete_contact_tracking
            last_heard = repeater.get('last_heard')
            if last_heard:
                try:
                    if isinstance(last_heard, str):
                        dt = datetime.fromisoformat(last_heard.replace('Z', '+00:00'))
                    else:
                        dt = last_heard
                    if most_recent_time is None or dt > most_recent_time:
                        most_recent_time = dt
                except:
                    pass
            
            # Check last_advert_timestamp
            last_advert = repeater.get('last_advert_timestamp')
            if last_advert:
                try:
                    if isinstance(last_advert, str):
                        dt = datetime.fromisoformat(last_advert.replace('Z', '+00:00'))
                    else:
                        dt = last_advert
                    if most_recent_time is None or dt > most_recent_time:
                        most_recent_time = dt
                except:
                    pass
            
            # Check last_seen from complete_contact_tracking table
            last_seen = repeater.get('last_seen')
            if last_seen:
                try:
                    if isinstance(last_seen, str):
                        dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                    else:
                        dt = last_seen
                    if most_recent_time is None or dt > most_recent_time:
                        most_recent_time = dt
                except:
                    pass
            
            if most_recent_time is None:
                # No timestamp found, give very low score
                recency_score = 0.1
            else:
                # Calculate recency score using exponential decay
                hours_ago = (now - most_recent_time).total_seconds() / 3600.0
                
                # Strong recency bias: recent devices get high scores, older devices get exponentially lower scores
                # Score = e^(-hours/12) - this gives:
                # - 1 hour ago: ~0.92
                # - 6 hours ago: ~0.61
                # - 12 hours ago: ~0.37
                # - 24 hours ago: ~0.14
                # - 48 hours ago: ~0.02
                # - 72 hours ago: ~0.002
                import math
                recency_score = math.exp(-hours_ago / 12.0)
                
                # Ensure score is between 0.0 and 1.0
                recency_score = max(0.0, min(1.0, recency_score))
            
            scored_repeaters.append((repeater, recency_score))
        
        # Sort by recency score (highest first)
        scored_repeaters.sort(key=lambda x: x[1], reverse=True)
        
        return scored_repeaters
    
    def _filter_recent_repeaters(self, repeaters: List[Dict[str, Any]], cutoff_hours: int = 24) -> List[Dict[str, Any]]:
        """Filter repeaters to only include those that have advertised recently"""
        from datetime import datetime, timedelta
        
        recent_repeaters = []
        cutoff_time = datetime.now() - timedelta(hours=cutoff_hours)
        
        for repeater in repeaters:
            # Check recency using multiple timestamp fields
            is_recent = False
            
            # Check last_heard from complete_contact_tracking
            last_heard = repeater.get('last_heard')
            if last_heard:
                try:
                    if isinstance(last_heard, str):
                        last_heard_dt = datetime.fromisoformat(last_heard.replace('Z', '+00:00'))
                    else:
                        last_heard_dt = last_heard
                    is_recent = last_heard_dt > cutoff_time
                except:
                    pass
            
            # Check last_advert_timestamp if last_heard check failed
            if not is_recent:
                last_advert = repeater.get('last_advert_timestamp')
                if last_advert:
                    try:
                        if isinstance(last_advert, str):
                            last_advert_dt = datetime.fromisoformat(last_advert.replace('Z', '+00:00'))
                        else:
                            last_advert_dt = last_advert
                        is_recent = last_advert_dt > cutoff_time
                    except:
                        pass
            
            # Check last_seen from complete_contact_tracking table
            if not is_recent:
                last_seen = repeater.get('last_seen')
                if last_seen:
                    try:
                        if isinstance(last_seen, str):
                            last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                        else:
                            last_seen_dt = last_seen
                        is_recent = last_seen_dt > cutoff_time
                    except:
                        pass
            
            if is_recent:
                recent_repeaters.append(repeater)
        
        return recent_repeaters

    def _apply_tie_breakers(self, distances: List[Tuple[float, Dict[str, Any]]]) -> Dict[str, Any]:
        """Apply tie-breaker strategies when repeaters have identical coordinates"""
        from datetime import datetime
        
        # Get all repeaters with the same (minimum) distance
        min_distance = distances[0][0]
        tied_repeaters = [repeater for distance, repeater in distances if distance == min_distance]
        
        # Tie-breaker 1: Prefer active repeaters
        active_repeaters = [r for r in tied_repeaters if r.get('is_active', True)]
        if len(active_repeaters) == 1:
            return active_repeaters[0]
        elif len(active_repeaters) > 1:
            tied_repeaters = active_repeaters
        
        # Tie-breaker 2: Prefer repeaters with more recent activity (enhanced recency check)
        def get_recent_timestamp(repeater):
            """Get the most recent timestamp from multiple fields"""
            timestamps = []
            
            # Check last_heard from complete_contact_tracking
            last_heard = repeater.get('last_heard')
            if last_heard:
                try:
                    if isinstance(last_heard, str):
                        dt = datetime.fromisoformat(last_heard.replace('Z', '+00:00'))
                    else:
                        dt = last_heard
                    timestamps.append(dt)
                except:
                    pass
            
            # Check last_advert_timestamp
            last_advert = repeater.get('last_advert_timestamp')
            if last_advert:
                try:
                    if isinstance(last_advert, str):
                        dt = datetime.fromisoformat(last_advert.replace('Z', '+00:00'))
                    else:
                        dt = last_advert
                    timestamps.append(dt)
                except:
                    pass
            
            # Check last_seen from complete_contact_tracking table
            last_seen = repeater.get('last_seen')
            if last_seen:
                try:
                    if isinstance(last_seen, str):
                        dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                    else:
                        dt = last_seen
                    timestamps.append(dt)
                except:
                    pass
            
            # Return the most recent timestamp, or epoch if none found
            if timestamps:
                return max(timestamps)
            else:
                return datetime.min  # Use epoch as fallback
        
        try:
            # Sort by most recent activity (more recent first)
            tied_repeaters.sort(key=get_recent_timestamp, reverse=True)
        except:
            pass  # If sorting fails, continue with next tie-breaker
        
        # Tie-breaker 3: Prefer repeaters with higher advertisement count (more active)
        try:
            tied_repeaters.sort(key=lambda r: r.get('advert_count', 0), reverse=True)
        except:
            pass
        
        # Tie-breaker 4: Alphabetical order (deterministic)
        tied_repeaters.sort(key=lambda r: r.get('name', ''))
        
        return tied_repeaters[0]
    
    def _select_by_path_proximity(self, repeaters_with_location: List[Dict[str, Any]], node_id: str, path_context: List[str], sender_location: Optional[Tuple[float, float]] = None) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select repeater based on proximity to previous/next nodes in path"""
        try:
            # Filter out repeaters with very low recency scores first
            scored_repeaters = self._calculate_recency_weighted_scores(repeaters_with_location)
            min_recency_threshold = 0.01  # Approximately 55 hours ago or less
            recent_repeaters = [r for r, score in scored_repeaters if score >= min_recency_threshold]
            
            if not recent_repeaters:
                return None, 0.0  # No recent repeaters found
            
            # Find current node position in path
            current_index = path_context.index(node_id) if node_id in path_context else -1
            if current_index == -1:
                return None, 0.0
            
            # Get previous and next node locations
            prev_location = None
            next_location = None
            
            # Get previous node location
            if current_index > 0:
                prev_node_id = path_context[current_index - 1]
                prev_location = self._get_node_location(prev_node_id)
            
            # Get next node location  
            if current_index < len(path_context) - 1:
                next_node_id = path_context[current_index + 1]
                next_location = self._get_node_location(next_node_id)
            
            # For the first repeater in the path, prioritize sender location as the source
            # The first repeater's primary job is to receive from the sender, so use sender location if available
            is_first_repeater = (current_index == 0)
            if is_first_repeater and sender_location:
                # For first repeater, use sender location only (not averaged with next node)
                self.logger.debug(f"Using sender location for proximity calculation of first repeater: {sender_location[0]:.4f}, {sender_location[1]:.4f}")
                return self._select_by_single_proximity(recent_repeaters, sender_location, "sender")
            
            # For the last repeater in the path, prioritize bot location as the destination
            # The last repeater's primary job is to deliver to the bot, so use bot location only
            is_last_repeater = (current_index == len(path_context) - 1)
            if is_last_repeater and self.geographic_guessing_enabled:
                if self.bot_latitude is not None and self.bot_longitude is not None:
                    # For last repeater, use bot location only (not averaged with previous node)
                    bot_location = (self.bot_latitude, self.bot_longitude)
                    self.logger.debug(f"Using bot location for proximity calculation of last repeater: {self.bot_latitude:.4f}, {self.bot_longitude:.4f}")
                    return self._select_by_single_proximity(recent_repeaters, bot_location, "bot")
            
            # For non-first/non-last repeaters, use both previous and next locations if available
            # If we have both previous and next locations, use both for proximity
            if prev_location and next_location:
                return self._select_by_dual_proximity(recent_repeaters, prev_location, next_location)
            elif prev_location:
                return self._select_by_single_proximity(recent_repeaters, prev_location, "previous")
            elif next_location:
                return self._select_by_single_proximity(recent_repeaters, next_location, "next")
            else:
                return None, 0.0
                
        except Exception as e:
            self.logger.warning(f"Error in path proximity calculation: {e}")
            return None, 0.0
    
    def _get_node_location(self, node_id: str) -> Optional[Tuple[float, float]]:
        """Get location for a node ID from the complete_contact_tracking database"""
        try:
            # Build query with age filtering if configured
            # Use last_advert_timestamp if available, otherwise fall back to last_heard
            if self.max_repeater_age_days > 0:
                query = '''
                    SELECT latitude, longitude, is_starred FROM complete_contact_tracking 
                    WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                    AND latitude != 0 AND longitude != 0 AND role IN ('repeater', 'roomserver')
                    AND (
                        (last_advert_timestamp IS NOT NULL AND last_advert_timestamp >= datetime('now', '-{} days'))
                        OR (last_advert_timestamp IS NULL AND last_heard >= datetime('now', '-{} days'))
                    )
                    ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                    LIMIT 1
                '''.format(self.max_repeater_age_days, self.max_repeater_age_days)
            else:
                query = '''
                    SELECT latitude, longitude, is_starred FROM complete_contact_tracking 
                    WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                    AND latitude != 0 AND longitude != 0 AND role IN ('repeater', 'roomserver')
                    ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                    LIMIT 1
                '''
            
            prefix_pattern = f"{node_id}%"
            results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
            
            if results:
                row = results[0]
                return (row['latitude'], row['longitude'])
            return None
        except Exception as e:
            self.logger.warning(f"Error getting location for node {node_id}: {e}")
            return None
    
    def _select_by_dual_proximity(self, repeaters: List[Dict[str, Any]], prev_location: Tuple[float, float], next_location: Tuple[float, float]) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select repeater based on proximity to both previous and next nodes with strong recency bias"""
        # Calculate recency-weighted scores for all repeaters
        scored_repeaters = self._calculate_recency_weighted_scores(repeaters)
        
        # Filter out repeaters with very low recency scores
        min_recency_threshold = 0.01  # Approximately 55 hours ago or less
        scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
        
        if not scored_repeaters:
            return None, 0.0  # No recent repeaters found
        
        best_repeater = None
        best_combined_score = 0.0
        
        for repeater, recency_score in scored_repeaters:
            # Calculate distance to previous node
            prev_distance = calculate_distance(
                prev_location[0], prev_location[1],
                repeater['latitude'], repeater['longitude']
            )
            
            # Calculate distance to next node
            next_distance = calculate_distance(
                next_location[0], next_location[1],
                repeater['latitude'], repeater['longitude']
            )
            
            # Combined proximity score (lower distance = higher score)
            avg_distance = (prev_distance + next_distance) / 2
            normalized_distance = min(avg_distance / 1000.0, 1.0)
            proximity_score = 1.0 - normalized_distance
            
            # Use configurable weighting (default: 40% recency, 60% proximity)
            combined_score = (recency_score * self.recency_weight) + (proximity_score * self.proximity_weight)
            
            # Apply star bias multiplier if repeater is starred
            if repeater.get('is_starred', False):
                combined_score *= self.star_bias_multiplier
                self.logger.debug(f"Applied star bias ({self.star_bias_multiplier}x) to {repeater.get('name', 'unknown')}")
            
            if combined_score > best_combined_score:
                best_combined_score = combined_score
                best_repeater = repeater
        
        if best_repeater:
            # Apply maximum range threshold
            if self.max_proximity_range > 0:
                # Check if any distance is beyond range
                prev_dist = calculate_distance(
                    prev_location[0], prev_location[1],
                    best_repeater['latitude'], best_repeater['longitude']
                )
                next_dist = calculate_distance(
                    next_location[0], next_location[1],
                    best_repeater['latitude'], best_repeater['longitude']
                )
                if prev_dist > self.max_proximity_range or next_dist > self.max_proximity_range:
                    return None, 0.0  # Reject if beyond maximum range
            
            # Confidence based on combined score
            confidence = 0.4 + (best_combined_score * 0.5)  # 0.4 to 0.9 based on score
            return best_repeater, confidence
        
        return None, 0.0
    
    def _select_by_single_proximity(self, repeaters: List[Dict[str, Any]], reference_location: Tuple[float, float], direction: str) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select repeater based on proximity to single reference node with strong recency bias"""
        # Calculate recency-weighted scores for all repeaters
        scored_repeaters = self._calculate_recency_weighted_scores(repeaters)
        
        # Filter out repeaters with very low recency scores
        min_recency_threshold = 0.01  # Approximately 55 hours ago or less
        scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
        
        if not scored_repeaters:
            return None, 0.0  # No recent repeaters found
        
        # For last repeater (direction="bot") or first repeater (direction="sender"), use 100% proximity (0% recency)
        # The final hop to the bot and first hop from sender should prioritize distance above all else
        # Recency still matters for filtering (min_recency_threshold), but not for scoring
        if direction == "bot" or direction == "sender":
            proximity_weight = 1.0
            recency_weight = 0.0
        else:
            # Use configurable weighting for other cases (from config: recency_weight, proximity_weight)
            proximity_weight = self.proximity_weight
            recency_weight = self.recency_weight
        
        best_repeater = None
        best_combined_score = 0.0
        all_scores = []  # For debug logging
        
        for repeater, recency_score in scored_repeaters:
            distance = calculate_distance(
                reference_location[0], reference_location[1],
                repeater['latitude'], repeater['longitude']
            )
            
            # Apply maximum range threshold
            if self.max_proximity_range > 0 and distance > self.max_proximity_range:
                continue  # Skip if beyond maximum range
            
            # Proximity score (closer = higher score)
            normalized_distance = min(distance / 1000.0, 1.0)
            proximity_score = 1.0 - normalized_distance
            
            # Use appropriate weighting based on direction
            combined_score = (recency_score * recency_weight) + (proximity_score * proximity_weight)
            
            # Apply star bias multiplier if repeater is starred
            if repeater.get('is_starred', False):
                combined_score *= self.star_bias_multiplier
                self.logger.debug(f"Applied star bias ({self.star_bias_multiplier}x) to {repeater.get('name', 'unknown')}")
            
            all_scores.append((repeater.get('name', 'unknown'), distance, recency_score, proximity_score, combined_score))
            
            if combined_score > best_combined_score:
                best_combined_score = combined_score
                best_repeater = repeater
        
        # Debug logging for last repeater selection
        if direction == "bot" and all_scores:
            self.logger.debug(f"Last repeater selection scores (proximity_weight={proximity_weight:.1%}, recency_weight={recency_weight:.1%}):")
            for name, dist, rec, prox, combined in sorted(all_scores, key=lambda x: x[4], reverse=True):
                self.logger.debug(f"  {name}: distance={dist:.1f}km, recency={rec:.3f}, proximity={prox:.3f}, combined={combined:.3f}")
        
        if best_repeater:
            # Confidence based on combined score
            confidence = 0.4 + (best_combined_score * 0.5)  # 0.4 to 0.9 based on score
            return best_repeater, confidence
        
        return None, 0.0
    
    def _format_path_response(self, node_ids: List[str], repeater_info: Dict[str, Dict[str, Any]]) -> str:
        """Format the path decode response
        
        Maintains the order of repeaters as they appear in the path (first to last)
        """
        # Build response lines in path order (first to last as message traveled)
        lines = []
        
        # Process nodes in path order (first to last as message traveled)
        for node_id in node_ids:
            info = repeater_info.get(node_id, {})
            
            if info.get('found', False):
                if info.get('collision', False):
                    # Multiple repeaters with same prefix
                    matches = info.get('matches', 0)
                    line = self.translate('commands.path.node_collision', node_id=node_id, matches=matches)
                elif info.get('geographic_guess', False):
                    # Geographic proximity selection
                    name = info['name']
                    confidence = info.get('confidence', 0.0)
                    
                    # Truncate name if too long
                    truncation = self.translate('commands.path.truncation')
                    if len(name) > 20:
                        name = name[:17] + truncation
                    
                    # Add confidence indicator
                    if confidence >= 0.9:
                        confidence_indicator = self.high_confidence_symbol
                    elif confidence >= 0.8:
                        confidence_indicator = self.medium_confidence_symbol
                    else:
                        confidence_indicator = self.low_confidence_symbol
                    
                    line = self.translate('commands.path.node_geographic', node_id=node_id, name=name, confidence=confidence_indicator)
                else:
                    # Single repeater found
                    name = info['name']
                    
                    # Truncate name if too long
                    truncation = self.translate('commands.path.truncation')
                    if len(name) > 27:
                        name = name[:24] + truncation
                    
                    line = self.translate('commands.path.node_format', node_id=node_id, name=name)
            else:
                # Unknown repeater
                line = self.translate('commands.path.node_unknown', node_id=node_id)
            
            # Ensure line fits within 130 character limit
            if len(line) > 130:
                truncation = self.translate('commands.path.truncation')
                line = line[:127] + truncation
            
            lines.append(line)
        
        # Return all lines - let _send_path_response handle the splitting
        return "\n".join(lines)
    
    async def _send_path_response(self, message: MeshMessage, response: str):
        """Send path response, splitting into multiple messages if necessary"""
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
            current_message = ""
            message_count = 0
            
            for i, line in enumerate(lines):
                # Check if adding this line would exceed max_length characters
                if len(current_message) + len(line) + 1 > max_length:  # +1 for newline
                    # Send current message and start new one
                    if current_message:
                        # Add ellipsis on new line to end of continued message (if not the last message)
                        if i < len(lines):
                            current_message += self.translate('commands.path.continuation_end')
                        await self.send_response(message, current_message.rstrip())
                        await asyncio.sleep(3.0)  # Delay between messages (same as other commands)
                        message_count += 1
                    
                    # Start new message with ellipsis on new line at beginning (if not first message)
                    if message_count > 0:
                        current_message = self.translate('commands.path.continuation_start', line=line)
                    else:
                        current_message = line
                else:
                    # Add line to current message
                    if current_message:
                        current_message += f"\n{line}"
                    else:
                        current_message = line
            
            # Send the last message if there's content
            if current_message:
                await self.send_response(message, current_message)
    
    async def _extract_path_from_recent_messages(self) -> str:
        """Extract path from the current message's path information (same as test command)"""
        try:
            # Use the path information from the current message being processed
            # This is the same reliable source that the test command uses
            if hasattr(self, '_current_message') and self._current_message and self._current_message.path:
                path_string = self._current_message.path
                
                # Check if it's a direct connection
                if "Direct" in path_string or "0 hops" in path_string:
                    return self.translate('commands.path.direct_connection')
                
                # Try to extract path nodes from the path string
                # Path strings are typically in format: "node1,node2,node3 via ROUTE_TYPE_*"
                if " via ROUTE_TYPE_" in path_string:
                    # Extract just the path part before the route type
                    path_part = path_string.split(" via ROUTE_TYPE_")[0]
                else:
                    path_part = path_string
                
                # Check if it looks like a comma-separated path
                if ',' in path_part:
                    path_input = path_part
                    return await self._decode_path(path_input)
                else:
                    # Try to decode even single nodes (e.g., "01" should be decoded to a repeater name)
                    # Check if path_part looks like it contains hex values
                    hex_pattern = r'[0-9a-fA-F]{2}'
                    if re.search(hex_pattern, path_part):
                        # Looks like hex values, try to decode
                        return await self._decode_path(path_part)
                    else:
                        # Unknown format - just show the raw path string
                        return self.translate('commands.path.path_prefix', path_string=path_string)
            else:
                return self.translate('commands.path.no_path')
                
        except Exception as e:
            self.logger.error(f"Error extracting path from current message: {e}")
            return self.translate('commands.path.error_extracting', error=str(e))
    
    def get_help(self) -> str:
        """Get help text for the path command"""
        return self.translate('commands.path.help')
    
    def get_help_text(self) -> str:
        """Get help text for the path command (used by help system)"""
        return self.get_help()
