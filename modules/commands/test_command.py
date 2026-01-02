#!/usr/bin/env python3
"""
Test command for the MeshCore Bot
Handles the 'test' keyword response
"""

import re
import math
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any
from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import calculate_distance


class TestCommand(BaseCommand):
    """Handles the test command.
    
    Responds to 'test' or 't' with connection info. Supports an optional phrase.
    Can utilize repeater geographic location data to estimate path distance.
    """
    
    # Plugin metadata
    name = "test"
    keywords = ['test', 't']
    description = "Responds to 'test' or 't' with connection info"
    category = "basic"
    
    def __init__(self, bot):
        super().__init__(bot)
        # Get bot location from config for geographic proximity calculations
        self.geographic_guessing_enabled = False
        self.bot_latitude = None
        self.bot_longitude = None
        
        # Get recency/proximity weighting from config (same as path command)
        recency_weight = bot.config.getfloat('Path_Command', 'recency_weight', fallback=0.2)
        self.recency_weight = max(0.0, min(1.0, recency_weight))  # Clamp to 0.0-1.0
        self.proximity_weight = 1.0 - self.recency_weight
        
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
                        self.logger.debug(f"Test command: Geographic proximity enabled with bot location: {lat:.4f}, {lon:.4f}")
                    else:
                        self.logger.warning(f"Invalid bot coordinates in config: {lat}, {lon}")
        except Exception as e:
            self.logger.warning(f"Error reading bot location from config: {e}")
    
    def get_help_text(self) -> str:
        """Get help text for the command.
        
        Returns:
            str: Help text string.
        """
        return self.translate('commands.test.help')
    
    def clean_content(self, content: str) -> str:
        """Clean content by removing control characters and normalizing whitespace.
        
        Args:
            content: The raw message content.
            
        Returns:
            str: Cleaned and normalized content string.
        """
        import re
        # Remove control characters (except newline, tab, carriage return)
        cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', content)
        # Normalize whitespace
        cleaned = ' '.join(cleaned.split())
        return cleaned
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Override to implement special test keyword matching with optional phrase.
        
        Matches 'test', 't', 'test <phrase>', or 't <phrase>'.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if the message matches the keyword patterns.
        """
        # Clean content to remove control characters and normalize whitespace
        content = self.clean_content(message.content)
        
        # Strip exclamation mark if present (for command-style messages)
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Handle "test" alone or "test " with phrase
        if content.lower() == "test":
            return True  # Just "test" by itself
        elif (content.startswith('test ') or content.startswith('Test ')) and len(content) > 5:
            phrase = content[5:].strip()  # Get everything after "test " and strip whitespace
            return bool(phrase)  # Make sure there's actually a phrase
        
        # Handle "t" alone or "t " with phrase
        elif content.lower() == "t":
            return True  # Just "t" by itself
        elif (content.startswith('t ') or content.startswith('T ')) and len(content) > 2:
            phrase = content[2:].strip()  # Get everything after "t " and strip whitespace
            return bool(phrase)  # Make sure there's actually a phrase
        
        return False
    
    def get_response_format(self) -> Optional[str]:
        """Get the response format from config.
        
        Returns:
            Optional[str]: The configured response format string, or None if not set.
        """
        if self.bot.config.has_section('Keywords'):
            format_str = self.bot.config.get('Keywords', 'test', fallback=None)
            return self._strip_quotes_from_config(format_str) if format_str else None
        return None
    
    def _extract_path_node_ids(self, message: MeshMessage) -> List[str]:
        """Extract path node IDs from message path string.
        
        Args:
            message: The message object containing the path.
            
        Returns:
            List[str]: List of valid 2-character hex node IDs.
        """
        if not message.path:
            return []
        
        # Check if it's a direct connection
        if "Direct" in message.path or "0 hops" in message.path:
            return []
        
        # Extract path nodes from the path string
        # Path strings are typically in format: "node1,node2,node3 via ROUTE_TYPE_*"
        # or just "node1,node2,node3"
        path_string = message.path
        
        # Remove route type suffix if present
        if " via ROUTE_TYPE_" in path_string:
            path_string = path_string.split(" via ROUTE_TYPE_")[0]
        
        # Check if it looks like a comma-separated path
        if ',' in path_string:
            # Clean up any extra info (like hop counts in parentheses)
            # Example: "01,7e,55,86 (4 hops)" -> "01,7e,55,86"
            if '(' in path_string:
                path_string = path_string.split('(')[0].strip()
            
            # Validate that all parts are 2-character hex values
            parts = path_string.split(',')
            valid_parts = []
            for part in parts:
                part = part.strip()
                # Check if it's a 2-character hex value
                if len(part) == 2 and all(c in '0123456789abcdefABCDEF' for c in part):
                    valid_parts.append(part.upper())
            
            return valid_parts
        
        return []
    
    
    def _lookup_repeater_location(self, node_id: str, path_context: Optional[List[str]] = None) -> Optional[Tuple[float, float]]:
        """Look up repeater location for a node ID using geographic proximity selection when path context is available.
        
        Args:
            node_id: The node ID to look up.
            path_context: Optional list of all node IDs in the path for context-aware selection.
            
        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None if not found.
        """
        try:
            if not hasattr(self.bot, 'db_manager'):
                return None
            
            # Query for all repeaters with matching prefix
            query = '''
                SELECT latitude, longitude, public_key, name,
                       last_advert_timestamp, last_heard, advert_count, is_starred
                FROM complete_contact_tracking 
                WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
            '''
            
            prefix_pattern = f"{node_id}%"
            results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
            
            if not results or len(results) == 0:
                return None
            
            # Convert to list of dicts for processing
            repeaters = []
            for row in results:
                repeaters.append({
                    'latitude': row.get('latitude'),
                    'longitude': row.get('longitude'),
                    'public_key': row.get('public_key'),
                    'name': row.get('name'),
                    'last_advert_timestamp': row.get('last_advert_timestamp'),
                    'last_heard': row.get('last_heard'),
                    'advert_count': row.get('advert_count', 0),
                    'is_starred': bool(row.get('is_starred', 0))
                })
            
            # If only one repeater, return it
            if len(repeaters) == 1:
                r = repeaters[0]
                return (float(r['latitude']), float(r['longitude']))
            
            # Multiple repeaters - use geographic proximity selection if path context available
            if path_context and len(path_context) > 1:
                # Get sender location if available (for first repeater selection)
                sender_location = self._get_sender_location()
                selected = self._select_by_path_proximity(repeaters, node_id, path_context, sender_location)
                if selected:
                    return (float(selected['latitude']), float(selected['longitude']))
            
            # Fall back to most recent repeater
            scored = self._calculate_recency_weighted_scores(repeaters)
            if scored:
                best_repeater = scored[0][0]
                return (float(best_repeater['latitude']), float(best_repeater['longitude']))
            
            return None
        except Exception as e:
            self.logger.debug(f"Error looking up repeater location for {node_id}: {e}")
            return None
    
    def _get_sender_location(self) -> Optional[Tuple[float, float]]:
        """Get sender location from current message if available.
        
        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None if unavailable/error.
        """
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
    
    def _calculate_recency_weighted_scores(self, repeaters: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], float]]:
        """Calculate recency-weighted scores for repeaters (0.0 to 1.0, higher = more recent).
        
        Args:
            repeaters: List of repeater dictionaries.
            
        Returns:
            List[Tuple[Dict[str, Any], float]]: List of (repeater, score) tuples sorting by score descending.
        """
        scored_repeaters = []
        now = datetime.now()
        
        for repeater in repeaters:
            most_recent_time = None
            
            # Check last_heard
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
            
            if most_recent_time is None:
                recency_score = 0.1
            else:
                hours_ago = (now - most_recent_time).total_seconds() / 3600.0
                recency_score = math.exp(-hours_ago / 12.0)
                recency_score = max(0.0, min(1.0, recency_score))
            
            scored_repeaters.append((repeater, recency_score))
        
        # Sort by recency score (highest first)
        scored_repeaters.sort(key=lambda x: x[1], reverse=True)
        return scored_repeaters
    
    def _get_node_location_simple(self, node_id: str) -> Optional[Tuple[float, float]]:
        """Simple lookup without proximity selection - used for reference nodes.
        
        Args:
            node_id: The node ID to look up.
            
        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None if not found.
        """
        try:
            if not hasattr(self.bot, 'db_manager'):
                return None
            
            query = '''
                SELECT latitude, longitude 
                FROM complete_contact_tracking 
                WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''
            
            prefix_pattern = f"{node_id}%"
            results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
            
            if results and len(results) > 0:
                row = results[0]
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is not None and lon is not None:
                    return (float(lat), float(lon))
            
            return None
        except Exception as e:
            self.logger.debug(f"Error in simple location lookup for {node_id}: {e}")
            return None
    
    def _select_by_path_proximity(self, repeaters: List[Dict[str, Any]], node_id: str, path_context: List[str], sender_location: Optional[Tuple[float, float]] = None) -> Optional[Dict[str, Any]]:
        """Select repeater based on proximity to previous/next nodes in path.
        
        Args:
            repeaters: List of candidate repeaters.
            node_id: Current node ID being resolved.
            path_context: Full path of node IDs.
            sender_location: Optional sender location for first hop optimization.
            
        Returns:
            Optional[Dict[str, Any]]: Selected repeater dict or None.
        """
        try:
            # Filter by recency first
            scored_repeaters = self._calculate_recency_weighted_scores(repeaters)
            min_recency_threshold = 0.01  # Approximately 55 hours ago or less
            recent_repeaters = [r for r, score in scored_repeaters if score >= min_recency_threshold]
            
            if not recent_repeaters:
                return None
            
            # Find current node position in path
            current_index = path_context.index(node_id) if node_id in path_context else -1
            if current_index == -1:
                return None
            
            # Get previous and next node locations
            prev_location = None
            next_location = None
            
            if current_index > 0:
                prev_node_id = path_context[current_index - 1]
                prev_location = self._get_node_location_simple(prev_node_id)
            
            if current_index < len(path_context) - 1:
                next_node_id = path_context[current_index + 1]
                next_location = self._get_node_location_simple(next_node_id)
            
            # For the first repeater in the path, prioritize sender location as the source
            # The first repeater's primary job is to receive from the sender, so use sender location if available
            is_first_repeater = (current_index == 0)
            if is_first_repeater and sender_location:
                # For first repeater, use sender location only (not averaged with next node)
                self.logger.debug(f"Test command: Using sender location for proximity calculation of first repeater: {sender_location[0]:.4f}, {sender_location[1]:.4f}")
                return self._select_by_single_proximity(recent_repeaters, sender_location, "sender")
            
            # For the last repeater in the path, prioritize bot location as the destination
            # The last repeater's primary job is to deliver to the bot, so use bot location only
            is_last_repeater = (current_index == len(path_context) - 1)
            if is_last_repeater and self.geographic_guessing_enabled:
                if self.bot_latitude is not None and self.bot_longitude is not None:
                    # For last repeater, use bot location only (not averaged with previous node)
                    bot_location = (self.bot_latitude, self.bot_longitude)
                    self.logger.debug(f"Test command: Using bot location for proximity calculation of last repeater: {self.bot_latitude:.4f}, {self.bot_longitude:.4f}")
                    return self._select_by_single_proximity(recent_repeaters, bot_location, "bot")
            
            # For non-first/non-last repeaters, use both previous and next locations if available
            # Use proximity selection
            if prev_location and next_location:
                return self._select_by_dual_proximity(recent_repeaters, prev_location, next_location)
            elif prev_location:
                return self._select_by_single_proximity(recent_repeaters, prev_location, "previous")
            elif next_location:
                return self._select_by_single_proximity(recent_repeaters, next_location, "next")
            else:
                return None
                
        except Exception as e:
            self.logger.debug(f"Error in path proximity selection: {e}")
            return None
    
    def _select_by_dual_proximity(self, repeaters: List[Dict[str, Any]], prev_location: Tuple[float, float], next_location: Tuple[float, float]) -> Optional[Dict[str, Any]]:
        """Select repeater based on proximity to both previous and next nodes.
        
        Args:
            repeaters: List of candidate repeaters.
            prev_location: Coordinates of previous node.
            next_location: Coordinates of next node.
            
        Returns:
            Optional[Dict[str, Any]]: Best matching repeater or None.
        """
        scored_repeaters = self._calculate_recency_weighted_scores(repeaters)
        min_recency_threshold = 0.01
        scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
        
        if not scored_repeaters:
            return None
        
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
            
            # Use configurable weighting (from Path_Command config)
            combined_score = (recency_score * self.recency_weight) + (proximity_score * self.proximity_weight)
            
            # Apply star bias multiplier if repeater is starred (use same config as path command)
            star_bias_multiplier = self.bot.config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5)
            if repeater.get('is_starred', False):
                combined_score *= star_bias_multiplier
            
            if combined_score > best_combined_score:
                best_combined_score = combined_score
                best_repeater = repeater
        
        return best_repeater
    
    def _select_by_single_proximity(self, repeaters: List[Dict[str, Any]], reference_location: Tuple[float, float], direction: str = "unknown") -> Optional[Dict[str, Any]]:
        """Select repeater based on proximity to single reference node.
        
        Args:
            repeaters: List of candidate repeaters.
            reference_location: Coordinates of reference node (sender, bot, next, previous).
            direction: Direction indicator ('sender', 'bot', 'next', 'previous').
            
        Returns:
            Optional[Dict[str, Any]]: Best matching repeater or None.
        """
        scored_repeaters = self._calculate_recency_weighted_scores(repeaters)
        min_recency_threshold = 0.01
        scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
        
        if not scored_repeaters:
            return None
        
        # For last repeater (direction="bot") or first repeater (direction="sender"), use 100% proximity (0% recency)
        # The final hop to the bot and first hop from sender should prioritize distance above all else
        # Recency still matters for filtering (min_recency_threshold), but not for scoring
        if direction == "bot" or direction == "sender":
            proximity_weight = 1.0
            recency_weight = 0.0
        else:
            # Use configurable weighting for other cases (from Path_Command config)
            proximity_weight = self.proximity_weight
            recency_weight = self.recency_weight
        
        best_repeater = None
        best_combined_score = 0.0
        
        for repeater, recency_score in scored_repeaters:
            distance = calculate_distance(
                reference_location[0], reference_location[1],
                repeater['latitude'], repeater['longitude']
            )
            
            # Proximity score (closer = higher score)
            normalized_distance = min(distance / 1000.0, 1.0)
            proximity_score = 1.0 - normalized_distance
            
            # Use appropriate weighting based on direction
            combined_score = (recency_score * recency_weight) + (proximity_score * proximity_weight)
            
            # Apply star bias multiplier if repeater is starred (use same config as path command)
            star_bias_multiplier = self.bot.config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5)
            if repeater.get('is_starred', False):
                combined_score *= star_bias_multiplier
            
            if combined_score > best_combined_score:
                best_combined_score = combined_score
                best_repeater = repeater
        
        return best_repeater
    
    def _calculate_path_distance(self, message: MeshMessage) -> str:
        """Calculate total distance along path (sum of distances between consecutive repeaters with locations).
        
        Args:
            message: The message containing the path.
            
        Returns:
            str: Formatted distance string used in response.
        """
        node_ids = self._extract_path_node_ids(message)
        if len(node_ids) < 2:
            # Check if it's a direct connection
            if not message.path or "Direct" in message.path or "0 hops" in message.path:
                return "N/A"  # Direct connection, no path to calculate
            return ""  # Path exists but insufficient nodes
        
        total_distance = 0.0
        valid_segments = 0
        skipped_nodes = 0
        
        # Get locations for all nodes using path context for proximity selection
        locations = []
        for i, node_id in enumerate(node_ids):
            location = self._lookup_repeater_location(node_id, path_context=node_ids)
            if location:
                locations.append((node_id, location))
            else:
                skipped_nodes += 1
        
        # Calculate distances between consecutive nodes with locations
        # This skips nodes without locations but continues the path
        for i in range(len(locations) - 1):
            prev_node_id, prev_location = locations[i]
            next_node_id, next_location = locations[i + 1]
            
            # Calculate distance between consecutive repeaters with locations
            distance = calculate_distance(
                prev_location[0], prev_location[1],
                next_location[0], next_location[1]
            )
            total_distance += distance
            valid_segments += 1
        
        if valid_segments == 0:
            return ""  # No valid segments found
        
        # Format the result compactly
        if skipped_nodes > 0:
            return f"{total_distance:.1f}km ({valid_segments} segs, {skipped_nodes} no-loc)"
        else:
            return f"{total_distance:.1f}km ({valid_segments} segs)"
    
    def _calculate_firstlast_distance(self, message: MeshMessage) -> str:
        """Calculate straight-line distance between first and last repeater in path.
        
        Args:
            message: The message containing the path.
            
        Returns:
            str: Formatted distance string used in response.
        """
        node_ids = self._extract_path_node_ids(message)
        if len(node_ids) < 2:
            # Check if it's a direct connection
            if not message.path or "Direct" in message.path or "0 hops" in message.path:
                return "N/A"  # Direct connection, no path to calculate
            return ""  # Path exists but insufficient nodes
        
        # Get first and last node IDs
        first_node_id = node_ids[0]
        last_node_id = node_ids[-1]
        
        # Use path context for better selection when multiple repeaters share prefix
        first_location = self._lookup_repeater_location(first_node_id, path_context=node_ids)
        last_location = self._lookup_repeater_location(last_node_id, path_context=node_ids)
        
        # Both locations must be available
        if not first_location or not last_location:
            return ""  # Fail if either location is missing
        
        # Calculate straight-line distance
        distance = calculate_distance(
            first_location[0], first_location[1],
            last_location[0], last_location[1]
        )
        
        return f"{distance:.1f}km"
    
    def format_response(self, message: MeshMessage, response_format: str) -> str:
        """Override to handle phrase extraction.
        
        Args:
            message: The original message.
            response_format: The format string.
            
        Returns:
            str: Formatted response string.
        """
        # Clean content to remove control characters and normalize whitespace
        content = self.clean_content(message.content)
        
        # Strip exclamation mark if present (for command-style messages)
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Extract phrase if present, otherwise use empty string
        if content.lower() == "test":
            phrase = ""
        elif content.lower() == "t":
            phrase = ""
        elif content.startswith('test ') or content.startswith('Test '):
            phrase = content[5:].strip()  # Get everything after "test "
        elif content.startswith('t ') or content.startswith('T '):
            phrase = content[2:].strip()  # Get everything after "t "
        else:
            phrase = ""
        
        try:
            connection_info = self.build_enhanced_connection_info(message)
            timestamp = self.format_timestamp(message)
            elapsed = self.format_elapsed(message)
            
            # Calculate distance placeholders
            path_distance = self._calculate_path_distance(message)
            firstlast_distance = self._calculate_firstlast_distance(message)
            
            # Format phrase part - add colon and space if phrase exists
            phrase_part = f": {phrase}" if phrase else ""
            
            return response_format.format(
                sender=message.sender_id or self.translate('common.unknown_sender'),
                phrase=phrase,
                phrase_part=phrase_part,
                connection_info=connection_info,
                path=message.path or self.translate('common.unknown_path'),
                timestamp=timestamp,
                elapsed=elapsed,
                snr=message.snr or self.translate('common.unknown'),
                path_distance=path_distance or "",
                firstlast_distance=firstlast_distance or ""
            )
        except (KeyError, ValueError) as e:
            self.logger.warning(f"Error formatting test response: {e}")
            return response_format
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the test command.
        
        Args:
            message: The input message trigger.
            
        Returns:
            bool: True if execution was successful.
        """
        # Store the current message for use in location lookups
        self._current_message = message
        return await self.handle_keyword_match(message)
