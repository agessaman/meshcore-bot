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
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Set
from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import (
    abbreviate_location, format_location_for_display, calculate_distance,
    geocode_zipcode, geocode_city
)


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
    
    # Documentation
    short_description = "Look up repeaters by two-character prefix and show their locations (if known)"
    usage = "prefix <XX|free|refresh>"
    examples = ["prefix 1A", "prefix free"]
    parameters = [
        {"name": "prefix", "description": "Two-character prefix (e.g., 1A, 2B)"},
        {"name": "free", "description": "Show available/unused prefixes"}
    ]
    
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
        
        # Prefix best location feature configuration
        try:
            self.prefix_best_enabled = self.get_config_value('Prefix_Command', 'prefix_best_enabled', fallback=True, value_type='bool')
            self.prefix_best_min_edge_observations = self.get_config_value('Prefix_Command', 'prefix_best_min_edge_observations', fallback=2, value_type='int')
            self.prefix_best_max_edge_age_days = self.get_config_value('Prefix_Command', 'prefix_best_max_edge_age_days', fallback=30, value_type='int')
            self.prefix_best_location_radius_km = self.get_config_value('Prefix_Command', 'prefix_best_location_radius_km', fallback=50.0, value_type='float')
            
            # Parse do_not_suggest list from config
            do_not_suggest_str = self.get_config_value('Prefix_Command', 'prefix_best_do_not_suggest', fallback='', value_type='str')
            if do_not_suggest_str and isinstance(do_not_suggest_str, str):
                # Split by comma, strip whitespace, convert to uppercase, filter empty strings
                self.prefix_best_do_not_suggest = [p.strip().upper() for p in do_not_suggest_str.split(',') if p.strip()]
            else:
                self.prefix_best_do_not_suggest = []
        except Exception as e:
            # Fallback to safe defaults if config parsing fails
            self.logger.warning(f"Error loading prefix_best configuration, using defaults: {e}")
            self.prefix_best_enabled = True
            self.prefix_best_min_edge_observations = 2
            self.prefix_best_max_edge_age_days = 30
            self.prefix_best_location_radius_km = 50.0
            self.prefix_best_do_not_suggest = []
    
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
    
    async def _parse_location_to_lat_lon(self, location: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """Parse location string to latitude/longitude coordinates.
        
        Supports: coordinates (lat,lon), zipcode, city name, repeater name.
        
        Args:
            location: Location string to parse.
            
        Returns:
            Tuple of (latitude, longitude, location_type) or (None, None, None) if not found.
            location_type can be: "coordinates", "zipcode", "city", "repeater", or None.
        """
        location = location.strip()
        
        # First, check if it's a repeater name
        lat, lon = await self._repeater_name_to_lat_lon(location)
        if lat is not None and lon is not None:
            return lat, lon, "repeater"
        
        # Check if it's coordinates (lat,lon)
        if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
            try:
                lat_str, lon_str = location.split(',')
                lat = float(lat_str.strip())
                lon = float(lon_str.strip())
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon, "coordinates"
            except ValueError:
                pass
        
        # Check if it's a zipcode (5 digits)
        if re.match(r'^\s*\d{5}\s*$', location):
            lat, lon = await geocode_zipcode(self.bot, location)
            if lat and lon:
                return lat, lon, "zipcode"
        
        # Otherwise, treat as city name
        lat, lon, _ = await geocode_city(self.bot, location)
        if lat and lon:
            return lat, lon, "city"
        
        return None, None, None
    
    async def _repeater_name_to_lat_lon(self, repeater_name: str) -> Tuple[Optional[float], Optional[float]]:
        """Look up repeater by name and return its lat/lon.
        
        Args:
            repeater_name: Name of the repeater to look up.
            
        Returns:
            Tuple of (latitude, longitude) or (None, None) if not found.
        """
        try:
            if not hasattr(self.bot, 'db_manager'):
                return None, None
            
            # Query complete_contact_tracking table for matching name
            # Use case-insensitive matching and allow partial matches
            # Filter for repeaters and roomservers only
            query = '''
                SELECT latitude, longitude, name 
                FROM complete_contact_tracking 
                WHERE role IN ('repeater', 'roomserver')
                AND latitude IS NOT NULL 
                AND longitude IS NOT NULL
                AND latitude != 0 
                AND longitude != 0
                AND LOWER(name) LIKE LOWER(?)
                ORDER BY 
                    CASE 
                        WHEN LOWER(name) = LOWER(?) THEN 1
                        WHEN LOWER(name) LIKE LOWER(?) THEN 2
                        ELSE 3
                    END,
                    COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''
            
            # Try exact match first, then partial match
            exact_pattern = repeater_name.strip()
            partial_pattern = f"%{exact_pattern}%"
            
            results = self.bot.db_manager.execute_query(
                query, 
                (partial_pattern, exact_pattern, f"{exact_pattern}%")
            )
            
            if results:
                row = results[0]
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is not None and lon is not None:
                    return float(lat), float(lon)
        except Exception as e:
            self.logger.debug(f"Error looking up repeater '{repeater_name}': {e}")
        
        return None, None
    
    def _find_repeaters_near_location(self, latitude: float, longitude: float, radius_km: float) -> List[Dict[str, Any]]:
        """Find all repeaters within a specified radius of a location.
        
        Args:
            latitude: Target latitude.
            longitude: Target longitude.
            radius_km: Search radius in kilometers.
            
        Returns:
            List of repeater dictionaries with prefix, public_key, name, latitude, longitude, distance.
        """
        try:
            # Query all repeaters with valid coordinates
            query = '''
                SELECT SUBSTR(public_key, 1, 2) as prefix, public_key, name, 
                       latitude, longitude,
                       COALESCE(last_advert_timestamp, last_heard) as last_seen
                FROM complete_contact_tracking 
                WHERE role IN ('repeater', 'roomserver')
                AND latitude IS NOT NULL 
                AND longitude IS NOT NULL
                AND latitude != 0 
                AND longitude != 0
            '''
            
            results = self.bot.db_manager.execute_query(query)
            
            repeaters = []
            for row in results:
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is None or lon is None:
                    continue
                
                # Calculate distance
                distance = calculate_distance(latitude, longitude, float(lat), float(lon))
                
                if distance <= radius_km:
                    repeaters.append({
                        'prefix': row['prefix'].upper(),
                        'public_key': row.get('public_key'),
                        'name': row.get('name'),
                        'latitude': float(lat),
                        'longitude': float(lon),
                        'distance': distance,
                        'last_seen': row.get('last_seen')
                    })
            
            return repeaters
        except Exception as e:
            self.logger.error(f"Error finding repeaters near location: {e}")
            return []
    
    def _collect_neighbor_prefixes(self, repeaters: List[Dict[str, Any]]) -> Set[str]:
        """Collect all first and second-hop neighbor prefixes for repeaters at a location.
        
        Args:
            repeaters: List of repeater dictionaries from _find_repeaters_near_location.
            
        Returns:
            Set of neighbor prefixes (first and second hop).
        """
        neighbor_prefixes: Set[str] = set()
        
        if not hasattr(self.bot, 'mesh_graph') or not self.bot.mesh_graph:
            self.logger.debug("Mesh graph not available, cannot collect neighbor prefixes")
            return neighbor_prefixes
        
        mesh_graph = self.bot.mesh_graph
        cutoff_date = datetime.now() - timedelta(days=self.prefix_best_max_edge_age_days)
        
        # Track prefixes we've already processed to avoid duplicate work
        processed_prefixes: Set[str] = set()
        
        for repeater in repeaters:
            prefix = repeater['prefix'].lower()
            
            # Skip if we've already processed this prefix
            if prefix in processed_prefixes:
                continue
            processed_prefixes.add(prefix)
            
            # Get first-hop neighbors
            outgoing_edges = mesh_graph.get_outgoing_edges(prefix)
            
            for edge in outgoing_edges:
                # Filter by observation count and recency
                if edge['observation_count'] < self.prefix_best_min_edge_observations:
                    continue
                
                # Check last_seen timestamp
                last_seen = edge.get('last_seen')
                if last_seen:
                    if isinstance(last_seen, str):
                        try:
                            last_seen = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                        except ValueError:
                            continue
                    if last_seen < cutoff_date:
                        continue
                
                neighbor_prefix = edge['to_prefix'].lower()
                neighbor_prefixes.add(neighbor_prefix)
                
                # Get second-hop neighbors (only if we haven't processed this prefix yet)
                if neighbor_prefix not in processed_prefixes:
                    second_hop_edges = mesh_graph.get_outgoing_edges(neighbor_prefix)
                    for second_edge in second_hop_edges:
                        if second_edge['observation_count'] < self.prefix_best_min_edge_observations:
                            continue
                        
                        second_last_seen = second_edge.get('last_seen')
                        if second_last_seen:
                            if isinstance(second_last_seen, str):
                                try:
                                    second_last_seen = datetime.fromisoformat(second_last_seen.replace('Z', '+00:00'))
                                except ValueError:
                                    continue
                            if second_last_seen < cutoff_date:
                                continue
                        
                        second_hop_prefix = second_edge['to_prefix'].lower()
                        neighbor_prefixes.add(second_hop_prefix)
        
        return neighbor_prefixes
    
    def _find_candidate_prefixes(self, neighbor_prefixes: Set[str], location_lat: float, location_lon: float) -> List[Dict[str, Any]]:
        """Find candidate prefixes that are not neighbors and not in do_not_suggest list.
        
        Includes both prefixes that exist in the database AND free prefixes that have never been used.
        
        Args:
            neighbor_prefixes: Set of neighbor prefixes to exclude.
            location_lat: Target location latitude.
            location_lon: Target location longitude.
            
        Returns:
            List of candidate prefix dictionaries with prefix, repeater_count, avg_distance, etc.
        """
        try:
            # Get all known prefixes from database
            query = '''
                SELECT SUBSTR(public_key, 1, 2) as prefix, 
                       COUNT(*) as repeater_count,
                       AVG(latitude) as avg_lat,
                       AVG(longitude) as avg_lon,
                       MAX(COALESCE(last_advert_timestamp, last_heard)) as most_recent
                FROM complete_contact_tracking 
                WHERE role IN ('repeater', 'roomserver')
                AND LENGTH(public_key) >= 2
                GROUP BY prefix
            '''
            
            results = self.bot.db_manager.execute_query(query)
            
            # Build set of prefixes found in database
            prefixes_in_db = set()
            candidates = []
            
            for row in results:
                prefix = row['prefix'].upper()
                prefix_lower = prefix.lower()
                prefixes_in_db.add(prefix_lower)
                
                # Exclude if it's a neighbor
                if prefix_lower in neighbor_prefixes:
                    continue
                
                # Exclude if in do_not_suggest list
                if prefix in self.prefix_best_do_not_suggest:
                    continue
                
                # Calculate average distance from location
                avg_lat = row.get('avg_lat')
                avg_lon = row.get('avg_lon')
                avg_distance = None
                if avg_lat is not None and avg_lon is not None:
                    avg_distance = calculate_distance(location_lat, location_lon, float(avg_lat), float(avg_lon))
                
                # Check if prefix is already used at/near the location
                # Query for repeaters with this prefix and calculate distances
                nearby_query = '''
                    SELECT latitude, longitude
                    FROM complete_contact_tracking 
                    WHERE role IN ('repeater', 'roomserver')
                    AND public_key LIKE ?
                    AND latitude IS NOT NULL 
                    AND longitude IS NOT NULL
                    AND latitude != 0 
                    AND longitude != 0
                '''
                nearby_results = self.bot.db_manager.execute_query(nearby_query, (f"{prefix}%",))
                nearby_count = 0
                if nearby_results:
                    for nearby_row in nearby_results:
                        nearby_lat = nearby_row.get('latitude')
                        nearby_lon = nearby_row.get('longitude')
                        if nearby_lat is not None and nearby_lon is not None:
                            nearby_distance = calculate_distance(location_lat, location_lon, float(nearby_lat), float(nearby_lon))
                            if nearby_distance <= self.prefix_best_location_radius_km:
                                nearby_count += 1
                
                candidates.append({
                    'prefix': prefix,
                    'repeater_count': row.get('repeater_count', 0),
                    'avg_distance': avg_distance,
                    'nearby_count': nearby_count,
                    'most_recent': row.get('most_recent')
                })
            
            # Also include free prefixes (not in database) that aren't neighbors or excluded
            # Generate all valid hex prefixes (01-FE, excluding 00 and FF)
            for i in range(1, 255):  # 1 to 254 (exclude 0 and 255)
                prefix = f"{i:02X}"
                prefix_lower = prefix.lower()
                
                # Skip if already in database (already processed above)
                if prefix_lower in prefixes_in_db:
                    continue
                
                # Exclude if it's a neighbor
                if prefix_lower in neighbor_prefixes:
                    continue
                
                # Exclude if in do_not_suggest list
                if prefix in self.prefix_best_do_not_suggest:
                    continue
                
                # Check if prefix is used nearby (even if not in main query)
                nearby_query = '''
                    SELECT latitude, longitude
                    FROM complete_contact_tracking 
                    WHERE role IN ('repeater', 'roomserver')
                    AND public_key LIKE ?
                    AND latitude IS NOT NULL 
                    AND longitude IS NOT NULL
                    AND latitude != 0 
                    AND longitude != 0
                '''
                nearby_results = self.bot.db_manager.execute_query(nearby_query, (f"{prefix}%",))
                nearby_count = 0
                if nearby_results:
                    for nearby_row in nearby_results:
                        nearby_lat = nearby_row.get('latitude')
                        nearby_lon = nearby_row.get('longitude')
                        if nearby_lat is not None and nearby_lon is not None:
                            nearby_distance = calculate_distance(location_lat, location_lon, float(nearby_lat), float(nearby_lon))
                            if nearby_distance <= self.prefix_best_location_radius_km:
                                nearby_count += 1
                
                # Free prefix (not in database) - no repeaters, no location, never seen
                candidates.append({
                    'prefix': prefix,
                    'repeater_count': 0,  # Never used
                    'avg_distance': None,  # No location data
                    'nearby_count': nearby_count,
                    'most_recent': None  # Never seen
                })
            
            return candidates
        except Exception as e:
            self.logger.error(f"Error finding candidate prefixes: {e}")
            return []
    
    def _score_prefix_candidates(self, candidates: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], float]]:
        """Score and rank candidate prefixes.
        
        Args:
            candidates: List of candidate prefix dictionaries.
            
        Returns:
            List of (candidate_dict, score) tuples sorted by score (highest first).
        """
        scored = []
        
        for candidate in candidates:
            score = 0.0
            
            # Availability score: Prefixes with fewer existing repeaters score higher
            # Normalize: 0 repeaters = 1.0, 10+ repeaters = 0.0
            repeater_count = candidate.get('repeater_count', 0)
            availability_score = max(0.0, 1.0 - (repeater_count / 10.0))
            score += availability_score * 0.4  # 40% weight
            
            # Distance score: Prefixes with repeaters far from location score higher
            # Normalize: 0km = 0.0, 200km+ = 1.0
            avg_distance = candidate.get('avg_distance')
            if avg_distance is not None:
                distance_score = min(1.0, avg_distance / 200.0)
            else:
                distance_score = 0.5  # Unknown distance gets neutral score
            score += distance_score * 0.3  # 30% weight
            
            # Recency score: Prefixes with stale repeaters score higher (likely available)
            # Normalize: very recent = 0.0, 90+ days old = 1.0
            most_recent = candidate.get('most_recent')
            recency_score = 0.5  # Default neutral
            if most_recent:
                try:
                    if isinstance(most_recent, str):
                        most_recent_dt = datetime.fromisoformat(most_recent.replace('Z', '+00:00'))
                    else:
                        most_recent_dt = most_recent
                    
                    days_ago = (datetime.now() - most_recent_dt).days
                    recency_score = min(1.0, days_ago / 90.0)
                except (ValueError, TypeError):
                    pass
            score += recency_score * 0.2  # 20% weight
            
            # Nearby count penalty: Prefixes already used nearby get penalty
            # Normalize: 0 nearby = 1.0, 3+ nearby = 0.0
            nearby_count = candidate.get('nearby_count', 0)
            nearby_penalty = max(0.0, 1.0 - (nearby_count / 3.0))
            score *= nearby_penalty  # Multiplicative penalty (10% weight effect)
            
            scored.append((candidate, score))
        
        # Sort by score (highest first)
        scored.sort(key=lambda x: x[1], reverse=True)
        
        return scored
    
    async def find_best_prefix_for_location(self, location: str) -> Optional[Dict[str, Any]]:
        """Find the best prefix for a given location.
        
        Args:
            location: Location string (coordinates, zipcode, city name, or repeater name).
            
        Returns:
            Dictionary with best prefix and metadata, or None if not found.
        """
        # Parse location
        lat, lon, location_type = await self._parse_location_to_lat_lon(location)
        if lat is None or lon is None:
            return {
                'success': False,
                'reason': 'location_not_found',
                'error': f"Could not parse location: {location}"
            }
        
        # Find repeaters at location
        repeaters = self._find_repeaters_near_location(lat, lon, self.prefix_best_location_radius_km)
        
        # If no repeaters at location, try expanding search radius
        expanded_radius = False
        if not repeaters:
            expanded_radius_km = self.prefix_best_location_radius_km * 2
            repeaters = self._find_repeaters_near_location(lat, lon, expanded_radius_km)
            if repeaters:
                expanded_radius = True
                self.logger.debug(f"No repeaters found within {self.prefix_best_location_radius_km}km, expanded to {expanded_radius_km}km")
        
        # Collect neighbor prefixes (with fallback if no graph data)
        neighbor_prefixes = self._collect_neighbor_prefixes(repeaters)
        has_graph_data = hasattr(self.bot, 'mesh_graph') and self.bot.mesh_graph is not None
        
        # If no graph data, fall back to geographic-only suggestions
        if not has_graph_data:
            self.logger.debug("Mesh graph not available, using geographic-only suggestions")
            # Still exclude prefixes already used nearby
            neighbor_prefixes = set()  # Clear neighbor prefixes since we can't determine them
        
        # Find candidate prefixes
        candidates = self._find_candidate_prefixes(neighbor_prefixes, lat, lon)
        
        if not candidates:
            # No candidates found - all prefixes are neighbors or in do_not_suggest
            # Try geographic-only fallback if we were using graph data
            if has_graph_data and neighbor_prefixes:
                self.logger.debug("All prefixes are neighbors, trying geographic-only fallback")
                candidates = self._find_candidate_prefixes(set(), lat, lon)  # Empty neighbor set
            
            if not candidates:
                return {
                    'success': False,
                    'reason': 'all_neighbors' if neighbor_prefixes else 'no_candidates',
                    'neighbor_count': len(neighbor_prefixes),
                    'repeaters_at_location': len(repeaters),
                    'has_graph_data': has_graph_data
                }
        
        # Score and rank candidates
        scored_candidates = self._score_prefix_candidates(candidates)
        
        if not scored_candidates:
            return {
                'success': False,
                'reason': 'scoring_failed',
                'candidate_count': len(candidates)
            }
        
        # Get top 3 candidates
        top_candidates = scored_candidates[:3]
        
        best = top_candidates[0][0]
        best_score = top_candidates[0][1]
        
        return {
            'success': True,
            'best_prefix': best['prefix'],
            'best_score': best_score,
            'best_repeater_count': best.get('repeater_count', 0),
            'best_avg_distance': best.get('avg_distance'),
            'best_nearby_count': best.get('nearby_count', 0),
            'alternatives': [
                {
                    'prefix': cand[0]['prefix'],
                    'score': cand[1]
                }
                for cand in top_candidates[1:]
            ],
            'repeaters_at_location': len(repeaters),
            'neighbor_count': len(neighbor_prefixes),
            'location_type': location_type,
            'has_graph_data': has_graph_data,
            'expanded_radius': expanded_radius
        }
    
    def format_best_prefix_response(self, result: Dict[str, Any], message: Optional[MeshMessage] = None) -> str:
        """Format the best prefix response.
        
        Args:
            result: Result dictionary from find_best_prefix_for_location.
            message: Optional message to calculate max length for.
            
        Returns:
            Formatted response string (fits within max message length).
        """
        if not result.get('success'):
            reason = result.get('reason', 'unknown')
            
            if reason == 'all_neighbors':
                neighbor_count = result.get('neighbor_count', 0)
                has_graph_data = result.get('has_graph_data', True)
                if has_graph_data:
                    return (f"No suitable prefix. All prefixes are neighbors "
                           f"({neighbor_count} found). High conflict area.")
                else:
                    return (f"No suitable prefix. All prefixes in use nearby. "
                           f"High usage area.")
            
            elif reason == 'location_not_found':
                error = result.get('error', 'Unknown error')
                return f"Error: {error}"
            
            elif reason == 'no_candidates':
                return ("No suitable prefix. All are neighbors, excluded, or in use.")
            
            elif reason == 'scoring_failed':
                candidate_count = result.get('candidate_count', 0)
                return f"Error: {candidate_count} candidates but scoring failed."
            
            else:
                return f"Could not find suitable prefix."
        
        # Get max message length if message provided
        max_length = self.get_max_message_length(message) if message else 150
        
        # Build concise response
        best_prefix = result['best_prefix']
        score = result.get('best_score', 0.0)
        
        # Confidence level (abbreviated)
        if score >= 0.8:
            conf = "High"
        elif score >= 0.6:
            conf = "Med"
        else:
            conf = "Low"
        
        # Start with essential info
        response = f"Best: {best_prefix} ({conf} {score:.0%})\n"
        
        # Add neighbor info (concise)
        neighbor_count = result.get('neighbor_count', 0)
        has_graph_data = result.get('has_graph_data', True)
        if has_graph_data:
            response += f"Not neighbor ({neighbor_count} found)\n"
        else:
            response += "Geo-only (no graph data)\n"
        
        # Add usage info (concise)
        repeater_count = result.get('best_repeater_count', 0)
        nearby_count = result.get('best_nearby_count', 0)
        if repeater_count > 0:
            if nearby_count > 0:
                response += f"Used: {repeater_count} ({nearby_count} nearby)\n"
            else:
                response += f"Used: {repeater_count}\n"
        else:
            response += "Not in use\n"
        
        # Add alternatives (compact, only if space allows)
        alternatives = result.get('alternatives', [])
        if alternatives:
            # Calculate space remaining
            current_length = len(response)
            space_remaining = max_length - current_length
            
            # Build alternatives line
            alt_prefixes = [alt['prefix'] for alt in alternatives]
            alt_line = f"Alt: {', '.join(alt_prefixes)}"
            
            # Only add if it fits
            if len(alt_line) <= space_remaining:
                response += alt_line
            elif space_remaining > 10:
                # Try to fit at least one alternative
                if len(alt_prefixes) > 0:
                    single_alt = f"Alt: {alt_prefixes[0]}"
                    if len(single_alt) <= space_remaining:
                        response += single_alt
        
        # Ensure response fits within max_length
        if len(response) > max_length:
            # Truncate at last complete line before limit
            lines = response.split('\n')
            truncated = []
            current_len = 0
            for line in lines:
                test_len = current_len + len(line) + (1 if truncated else 0)  # +1 for newline
                if test_len > max_length:
                    break
                truncated.append(line)
                current_len = test_len
            response = '\n'.join(truncated)
        
        return response
    
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
        
        # Handle best command: "prefix best <location>"
        if command == "BEST":
            if not self.prefix_best_enabled:
                response = "Prefix best command is disabled."
                return await self.send_response(message, response)
            
            if len(parts) < 3:
                response = ("Usage: prefix best <location>\n"
                           "Location can be: coordinates (lat,lon), zipcode, city name, or repeater name")
                return await self.send_response(message, response)
            
            location_str = ' '.join(parts[2:])
            result = await self.find_best_prefix_for_location(location_str)
            
            if not result:
                response = f"Could not find suitable prefix for location: {location_str}"
                return await self.send_response(message, response)
            
            # Format response with suggested prefix and reasoning
            response = self.format_best_prefix_response(result, message)
            return await self.send_response(message, response)
        
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
            return
        else:
            # Multi-message: per-user rate limit applies only to the first message (the trigger)
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
                    # Per-user rate limit applies only to the first message (trigger); skip for continuations
                    skip_user_limit = message_count > 0
                    await self.send_response(message, current_message.rstrip(), skip_user_rate_limit=skip_user_limit)
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
            
            # Send the last message if there's content (continuation; skip per-user rate limit)
            if current_message:
                await self.send_response(message, current_message, skip_user_rate_limit=True)
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()
