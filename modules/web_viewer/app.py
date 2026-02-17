#!/usr/bin/env python3
"""
MeshCore Bot Data Viewer
Bot montoring web interface using Flask-SocketIO 5.x
"""

import sqlite3
import json
import time
import configparser
import logging
import threading
from datetime import datetime, timedelta, date
from flask import Flask, render_template, jsonify, request, send_from_directory, make_response
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from pathlib import Path
import os
import sys
from typing import Dict, Any, Optional, List

# Add the project root to the path so we can import bot components
project_root = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, project_root)

from modules.db_manager import DBManager
from modules.repeater_manager import RepeaterManager
from modules.utils import resolve_path, calculate_distance

class BotDataViewer:
    """Complete web interface using Flask-SocketIO 5.x best practices"""
    
    def __init__(self, db_path="meshcore_bot.db", repeater_db_path=None, config_path="config.ini"):
        # Setup comprehensive logging
        self._setup_logging()
        
        # Set bot root directory (project root) for path validation
        # This is the directory containing the modules folder
        self.bot_root = Path(os.path.join(os.path.dirname(__file__), '..', '..')).resolve()
        # Resolve relative config path so viewer finds config when started as subprocess (cwd may differ)
        if not os.path.isabs(config_path):
            config_path = str(self.bot_root / config_path)
        
        self.app = Flask(
            __name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'),
            static_url_path='/static'
        )
        self.app.config['SECRET_KEY'] = 'meshcore_bot_viewer_secret'
        
        # Flask-SocketIO configuration following 5.x best practices
        self.socketio = SocketIO(
            self.app, 
            cors_allowed_origins="*",
            max_http_buffer_size=1000000,  # 1MB buffer limit
            ping_timeout=5,                # 5 second ping timeout (Flask-SocketIO 5.x default)
            ping_interval=25,             # 25 second ping interval (Flask-SocketIO 5.x default)
            logger=False,                  # Disable verbose logging
            engineio_logger=False,        # Disable EngineIO logging
            async_mode='threading'        # Use threading for better stability
        )
        
        self.repeater_db_path = repeater_db_path
        
        # Connection management using Flask-SocketIO built-ins
        self.connected_clients = {}  # Track client metadata
        self._clients_lock = threading.Lock()  # Thread safety for connected_clients
        self.max_clients = 10
        
        # Database connection pooling with thread safety
        self._db_connection = None
        self._db_lock = threading.Lock()
        self._db_last_used = 0
        self._db_timeout = 300  # 5 minutes connection timeout
        
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Use [Bot] db_path when [Web_Viewer] db_path is unset
        bot_db = self.config.get('Bot', 'db_path', fallback='meshcore_bot.db')
        if (self.config.has_section('Web_Viewer') and self.config.has_option('Web_Viewer', 'db_path')
                and self.config.get('Web_Viewer', 'db_path', fallback='').strip()):
            use_db = self.config.get('Web_Viewer', 'db_path').strip()
        else:
            use_db = bot_db
        self.db_path = str(resolve_path(use_db, self.bot_root))
        
        # Setup template context processor for global template variables
        self._setup_template_context()
        
        # Initialize databases
        self._init_databases()
        
        # Setup routes and SocketIO handlers
        self._setup_routes()
        self._setup_socketio_handlers()
        
        # Start database polling for real-time data
        self._start_database_polling()
        
        # Start periodic cleanup
        self._start_cleanup_scheduler()
        
        self.logger.info("BotDataViewer initialized with Flask-SocketIO 5.x best practices")
    
    def _setup_logging(self):
        """Setup comprehensive logging with rotation"""
        from logging.handlers import RotatingFileHandler
        
        # Create logs directory if it doesn't exist
        os.makedirs('logs', exist_ok=True)
        
        # Get or create logger (don't use basicConfig as it may conflict with existing logging)
        self.logger = logging.getLogger('modern_web_viewer')
        self.logger.setLevel(logging.DEBUG)
        
        # Remove existing handlers to avoid duplicates
        self.logger.handlers.clear()
        
        # Create rotating file handler (max 5MB per file, keep 3 backups)
        file_handler = RotatingFileHandler(
            'logs/web_viewer_modern.log',
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)
        
        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        # Prevent propagation to root logger to avoid duplicate messages
        self.logger.propagate = False
        
        self.logger.info("Web viewer logging initialized with rotation (5MB max, 3 backups)")
    
    def _load_config(self, config_path):
        """Load configuration from file"""
        config = configparser.ConfigParser()
        if os.path.exists(config_path):
            config.read(config_path)
        return config
    
    def _setup_template_context(self):
        """Setup template context processor to inject global variables"""
        @self.app.context_processor
        def inject_template_vars():
            """Inject variables available to all templates. Never raises so templates always render."""
            try:
                try:
                    greeter_enabled = self.config.getboolean('Greeter_Command', 'enabled', fallback=False)
                except (configparser.NoSectionError, configparser.NoOptionError, ValueError, TypeError):
                    greeter_enabled = False
                try:
                    feed_manager_enabled = self.config.getboolean('Feed_Manager', 'feed_manager_enabled', fallback=False)
                except (configparser.NoSectionError, configparser.NoOptionError, ValueError, TypeError):
                    feed_manager_enabled = False
                try:
                    bot_name = (self.config.get('Bot', 'bot_name', fallback='MeshCore Bot') or '').strip() or 'MeshCore Bot'
                except (configparser.NoSectionError, configparser.NoOptionError):
                    bot_name = 'MeshCore Bot'
                return dict(greeter_enabled=greeter_enabled, feed_manager_enabled=feed_manager_enabled, bot_name=bot_name)
            except Exception as e:
                self.logger.exception("Template context processor failed: %s", e)
                return dict(greeter_enabled=False, feed_manager_enabled=False, bot_name='MeshCore Bot')
    
    def _get_db_path(self):
        """Get the database path, falling back to [Bot] db_path if [Web_Viewer] db_path is unset"""
        # Use [Bot] db_path when [Web_Viewer] db_path is unset
        bot_db = self.config.get('Bot', 'db_path', fallback='meshcore_bot.db')
        if (self.config.has_section('Web_Viewer') and self.config.has_option('Web_Viewer', 'db_path')
                and self.config.get('Web_Viewer', 'db_path', fallback='').strip()):
            use_db = self.config.get('Web_Viewer', 'db_path').strip()
        else:
            use_db = bot_db
        return str(resolve_path(use_db, self.bot_root))
    
    def _init_databases(self):
        """Initialize database connections"""
        try:
            # Initialize database manager for metadata access
            from modules.db_manager import DBManager
            # Create a minimal bot object for DBManager
            class MinimalBot:
                def __init__(self, logger, config, db_manager=None):
                    self.logger = logger
                    self.config = config
                    self.db_manager = db_manager
            
            # Create DBManager first
            minimal_bot = MinimalBot(self.logger, self.config)
            self.db_manager = DBManager(minimal_bot, self.db_path)
            
            # Now set db_manager on the minimal bot for RepeaterManager
            minimal_bot.db_manager = self.db_manager
            
            # Initialize repeater manager for geocoding functionality
            self.repeater_manager = RepeaterManager(minimal_bot)
            
            # Initialize mesh graph for path resolution (uses same logic as path command)
            from modules.mesh_graph import MeshGraph
            minimal_bot.mesh_graph = MeshGraph(minimal_bot)
            self.mesh_graph = minimal_bot.mesh_graph
            
            # Initialize packet_stream table for real-time monitoring
            self._init_packet_stream_table()
            
            # Store database paths for direct connection
            self.db_path = self.db_path
            self.repeater_db_path = self.repeater_db_path
            self.logger.info("Database connections initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize databases: {e}")
            raise
    
    def _init_packet_stream_table(self):
        """Initialize the packet_stream table in the web viewer database (same as [Bot] db_path by default)."""
        conn = None
        try:
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                cursor = conn.cursor()
                
                # Create packet_stream table with schema matching the INSERT statements
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS packet_stream (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        data TEXT NOT NULL,
                        type TEXT NOT NULL
                    )
                ''')
                
                # Create index on timestamp for faster queries
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_packet_stream_timestamp 
                    ON packet_stream(timestamp)
                ''')
                
                # Create index on type for filtering by type
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_packet_stream_type 
                    ON packet_stream(type)
                ''')
                
                conn.commit()
                
                self.logger.info(f"Initialized packet_stream table in {self.db_path}")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize packet_stream table: {e}")
            # Don't raise - allow web viewer to continue even if table init fails
    
    def _get_db_connection(self):
        """Get database connection - create new connection for each request to avoid threading issues"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            self.logger.error(f"Failed to create database connection: {e}")
            raise
    
    def _resolve_path(self, path_input: str) -> Dict[str, Any]:
        """Resolve a hex path to repeater names and locations using the same algorithm as PathCommand.
        
        This method replicates the path command's logic to ensure consistency between
        the bot's path command and the web viewer's path resolution.
        
        Args:
            path_input: Hex path string (e.g., "7e,01,86" or "7e 01 86")
            
        Returns:
            Dictionary with node_ids, repeaters list, and valid flag
        """
        import re
        import math
        from datetime import datetime
        
        # Check if db_manager is available
        if not hasattr(self, 'db_manager') or not self.db_manager:
            return {
                'node_ids': [],
                'repeaters': [],
                'valid': False,
                'error': 'Database manager not initialized'
            }
        
        # Parse hex input - same logic as PathCommand._decode_path
        # Handle both comma/space-separated and continuous hex strings (e.g., "8601a5")
        # First, try to parse as continuous hex string
        path_input_clean = path_input.replace(',', '').replace(':', '').replace(' ', '')
        if re.match(r'^[0-9a-fA-F]{4,}$', path_input_clean):
            # Continuous hex string - split into pairs
            hex_matches = [path_input_clean[i:i+2] for i in range(0, len(path_input_clean), 2)]
        else:
            # Space/comma-separated format
            path_input = path_input.replace(',', ' ').replace(':', ' ')
            hex_pattern = r'[0-9a-fA-F]{2}'
            hex_matches = re.findall(hex_pattern, path_input)
        
        if not hex_matches:
            return {
                'node_ids': [],
                'repeaters': [],
                'valid': False,
                'error': 'No valid hex values found'
            }
        
        node_ids = [match.upper() for match in hex_matches]
        
        # Load all Path_Command config values (same as PathCommand.__init__)
        # Geographic guessing
        geographic_guessing_enabled = False
        bot_latitude = None
        bot_longitude = None
        
        try:
            if self.config.has_section('Bot'):
                lat = self.config.getfloat('Bot', 'bot_latitude', fallback=None)
                lon = self.config.getfloat('Bot', 'bot_longitude', fallback=None)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    bot_latitude = lat
                    bot_longitude = lon
                    geographic_guessing_enabled = True
        except Exception:
            pass
        
        # Path command settings
        proximity_method = self.config.get('Path_Command', 'proximity_method', fallback='simple')
        path_proximity_fallback = self.config.getboolean('Path_Command', 'path_proximity_fallback', fallback=True)
        max_proximity_range = self.config.getfloat('Path_Command', 'max_proximity_range', fallback=200.0)
        max_repeater_age_days = self.config.getint('Path_Command', 'max_repeater_age_days', fallback=14)
        
        recency_weight = self.config.getfloat('Path_Command', 'recency_weight', fallback=0.4)
        recency_weight = max(0.0, min(1.0, recency_weight))
        proximity_weight = 1.0 - recency_weight
        
        recency_decay_half_life_hours = self.config.getfloat('Path_Command', 'recency_decay_half_life_hours', fallback=12.0)
        
        # Check for preset first, then apply individual settings (preset can be overridden)
        preset = self.config.get('Path_Command', 'path_selection_preset', fallback='balanced').lower()
        
        # Apply preset defaults, then individual settings override
        if preset == 'geographic':
            preset_graph_confidence_threshold = 0.5
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.5
            preset_final_hop_weight = 0.4
        elif preset == 'graph':
            preset_graph_confidence_threshold = 0.9
            preset_distance_threshold = 50.0
            preset_distance_penalty = 0.2
            preset_final_hop_weight = 0.15
        else:  # 'balanced' (default)
            preset_graph_confidence_threshold = 0.7
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.3
            preset_final_hop_weight = 0.25
        
        graph_based_validation = self.config.getboolean('Path_Command', 'graph_based_validation', fallback=True)
        min_edge_observations = self.config.getint('Path_Command', 'min_edge_observations', fallback=3)
        
        graph_use_bidirectional = self.config.getboolean('Path_Command', 'graph_use_bidirectional', fallback=True)
        graph_use_hop_position = self.config.getboolean('Path_Command', 'graph_use_hop_position', fallback=True)
        graph_multi_hop_enabled = self.config.getboolean('Path_Command', 'graph_multi_hop_enabled', fallback=True)
        graph_multi_hop_max_hops = self.config.getint('Path_Command', 'graph_multi_hop_max_hops', fallback=2)
        graph_geographic_combined = self.config.getboolean('Path_Command', 'graph_geographic_combined', fallback=False)
        graph_geographic_weight = self.config.getfloat('Path_Command', 'graph_geographic_weight', fallback=0.7)
        graph_geographic_weight = max(0.0, min(1.0, graph_geographic_weight))
        graph_confidence_override_threshold = self.config.getfloat('Path_Command', 'graph_confidence_override_threshold', fallback=preset_graph_confidence_threshold)
        graph_confidence_override_threshold = max(0.0, min(1.0, graph_confidence_override_threshold))
        graph_distance_penalty_enabled = self.config.getboolean('Path_Command', 'graph_distance_penalty_enabled', fallback=True)
        graph_max_reasonable_hop_distance_km = self.config.getfloat('Path_Command', 'graph_max_reasonable_hop_distance_km', fallback=preset_distance_threshold)
        graph_distance_penalty_strength = self.config.getfloat('Path_Command', 'graph_distance_penalty_strength', fallback=preset_distance_penalty)
        graph_distance_penalty_strength = max(0.0, min(1.0, graph_distance_penalty_strength))
        graph_zero_hop_bonus = self.config.getfloat('Path_Command', 'graph_zero_hop_bonus', fallback=0.4)
        graph_zero_hop_bonus = max(0.0, min(1.0, graph_zero_hop_bonus))
        graph_prefer_stored_keys = self.config.getboolean('Path_Command', 'graph_prefer_stored_keys', fallback=True)
        
        # Final hop proximity settings for graph selection
        # Defaults based on LoRa ranges: typical < 30km, long up to 200km, very close < 10km
        graph_final_hop_proximity_enabled = self.config.getboolean('Path_Command', 'graph_final_hop_proximity_enabled', fallback=True)
        graph_final_hop_proximity_weight = self.config.getfloat('Path_Command', 'graph_final_hop_proximity_weight', fallback=preset_final_hop_weight)
        graph_final_hop_proximity_weight = max(0.0, min(1.0, graph_final_hop_proximity_weight))
        graph_final_hop_max_distance = self.config.getfloat('Path_Command', 'graph_final_hop_max_distance', fallback=0.0)
        graph_final_hop_proximity_normalization_km = self.config.getfloat('Path_Command', 'graph_final_hop_proximity_normalization_km', fallback=200.0)  # Long LoRa range
        graph_final_hop_very_close_threshold_km = self.config.getfloat('Path_Command', 'graph_final_hop_very_close_threshold_km', fallback=10.0)
        graph_final_hop_close_threshold_km = self.config.getfloat('Path_Command', 'graph_final_hop_close_threshold_km', fallback=30.0)  # Typical LoRa range
        graph_final_hop_max_proximity_weight = self.config.getfloat('Path_Command', 'graph_final_hop_max_proximity_weight', fallback=0.6)
        graph_final_hop_max_proximity_weight = max(0.0, min(1.0, graph_final_hop_max_proximity_weight))
        graph_path_validation_max_bonus = self.config.getfloat('Path_Command', 'graph_path_validation_max_bonus', fallback=0.3)
        graph_path_validation_max_bonus = max(0.0, min(1.0, graph_path_validation_max_bonus))
        graph_path_validation_obs_divisor = self.config.getfloat('Path_Command', 'graph_path_validation_obs_divisor', fallback=50.0)
        
        star_bias_multiplier = self.config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5)
        star_bias_multiplier = max(1.0, star_bias_multiplier)
        
        # Helper method to calculate recency scores (same as PathCommand._calculate_recency_weighted_scores)
        def calculate_recency_weighted_scores(repeaters):
            scored_repeaters = []
            now = datetime.now()
            
            for repeater in repeaters:
                most_recent_time = None
                
                for field in ['last_heard', 'last_advert_timestamp', 'last_seen']:
                    value = repeater.get(field)
                    if value:
                        try:
                            if isinstance(value, str):
                                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            else:
                                dt = value
                            if most_recent_time is None or dt > most_recent_time:
                                most_recent_time = dt
                        except:
                            pass
                
                if most_recent_time is None:
                    recency_score = 0.1
                else:
                    hours_ago = (now - most_recent_time).total_seconds() / 3600.0
                    recency_score = math.exp(-hours_ago / recency_decay_half_life_hours)
                    recency_score = max(0.0, min(1.0, recency_score))
                
                scored_repeaters.append((repeater, recency_score))
            
            scored_repeaters.sort(key=lambda x: x[1], reverse=True)
            return scored_repeaters
        
        # Helper to get node location (same as PathCommand._get_node_location)
        def get_node_location(node_id):
            try:
                if max_repeater_age_days > 0:
                    query = '''
                        SELECT latitude, longitude FROM complete_contact_tracking 
                        WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                        AND latitude != 0 AND longitude != 0 AND role IN ('repeater', 'roomserver')
                        AND (
                            (last_advert_timestamp IS NOT NULL AND last_advert_timestamp >= datetime('now', '-{} days'))
                            OR (last_advert_timestamp IS NULL AND last_heard >= datetime('now', '-{} days'))
                        )
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''.format(max_repeater_age_days, max_repeater_age_days)
                else:
                    query = '''
                        SELECT latitude, longitude FROM complete_contact_tracking 
                        WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                        AND latitude != 0 AND longitude != 0 AND role IN ('repeater', 'roomserver')
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''
                
                results = self.db_manager.execute_query(query, (f"{node_id}%",))
                if results:
                    return (results[0]['latitude'], results[0]['longitude'])
                return None
            except Exception:
                return None
        
        # Helper for simple proximity selection (same as PathCommand._select_by_simple_proximity)
        def select_by_simple_proximity(repeaters_with_location):
            scored_repeaters = calculate_recency_weighted_scores(repeaters_with_location)
            min_recency_threshold = 0.01
            scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
            
            if not scored_repeaters:
                return None, 0.0
            
            if len(scored_repeaters) == 1:
                repeater, recency_score = scored_repeaters[0]
                distance = calculate_distance(bot_latitude, bot_longitude, repeater['latitude'], repeater['longitude'])
                if max_proximity_range > 0 and distance > max_proximity_range:
                    return None, 0.0
                base_confidence = 0.4 + (recency_score * 0.5)
                return repeater, base_confidence
            
            combined_scores = []
            for repeater, recency_score in scored_repeaters:
                distance = calculate_distance(bot_latitude, bot_longitude, repeater['latitude'], repeater['longitude'])
                if max_proximity_range > 0 and distance > max_proximity_range:
                    continue
                
                normalized_distance = min(distance / 1000.0, 1.0)
                proximity_score = 1.0 - normalized_distance
                combined_score = (recency_score * recency_weight) + (proximity_score * proximity_weight)
                
                if repeater.get('is_starred', False):
                    combined_score *= star_bias_multiplier
                
                combined_scores.append((combined_score, distance, repeater))
            
            if not combined_scores:
                return None, 0.0
            
            combined_scores.sort(key=lambda x: x[0], reverse=True)
            best_score, best_distance, best_repeater = combined_scores[0]
            
            if len(combined_scores) == 1:
                confidence = 0.4 + (best_score * 0.5)
            else:
                second_best_score = combined_scores[1][0]
                score_ratio = best_score / second_best_score if second_best_score > 0 else 1.0
                if score_ratio > 1.5:
                    confidence = 0.9
                elif score_ratio > 1.2:
                    confidence = 0.8
                elif score_ratio > 1.1:
                    confidence = 0.7
                else:
                    confidence = 0.5
            
            return best_repeater, confidence
        
        # Helper for path proximity (simplified - for web viewer we'll use simple proximity)
        def select_by_path_proximity(repeaters_with_location, node_id, path_context, sender_location):
            scored_repeaters = calculate_recency_weighted_scores(repeaters_with_location)
            min_recency_threshold = 0.01
            recent_repeaters = [r for r, score in scored_repeaters if score >= min_recency_threshold]
            
            if not recent_repeaters:
                return None, 0.0
            
            current_index = path_context.index(node_id) if node_id in path_context else -1
            if current_index == -1:
                return None, 0.0
            
            is_last_repeater = (current_index == len(path_context) - 1)
            if is_last_repeater and geographic_guessing_enabled and bot_latitude and bot_longitude:
                bot_location = (bot_latitude, bot_longitude)
                return select_by_single_proximity(recent_repeaters, bot_location, "bot")
            
            # For other positions, use simple proximity
            return select_by_simple_proximity(recent_repeaters)
        
        # Helper for single proximity (same as PathCommand._select_by_single_proximity)
        def select_by_single_proximity(repeaters, reference_location, direction):
            scored_repeaters = calculate_recency_weighted_scores(repeaters)
            min_recency_threshold = 0.01
            scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
            
            if not scored_repeaters:
                return None, 0.0
            
            if direction == "bot" or direction == "sender":
                proximity_weight_local = 1.0
                recency_weight_local = 0.0
            else:
                proximity_weight_local = proximity_weight
                recency_weight_local = recency_weight
            
            best_repeater = None
            best_combined_score = 0.0
            
            for repeater, recency_score in scored_repeaters:
                distance = calculate_distance(reference_location[0], reference_location[1],
                                            repeater['latitude'], repeater['longitude'])
                
                if max_proximity_range > 0 and distance > max_proximity_range:
                    continue
                
                normalized_distance = min(distance / 1000.0, 1.0)
                proximity_score = 1.0 - normalized_distance
                combined_score = (recency_score * recency_weight_local) + (proximity_score * proximity_weight_local)
                
                if repeater.get('is_starred', False):
                    combined_score *= star_bias_multiplier
                
                if combined_score > best_combined_score:
                    best_combined_score = combined_score
                    best_repeater = repeater
            
            if best_repeater:
                confidence = 0.4 + (best_combined_score * 0.5)
                return best_repeater, confidence
            
            return None, 0.0
        
        # Helper for graph-based selection (same as PathCommand._select_repeater_by_graph)
        def select_repeater_by_graph(repeaters, node_id, path_context):
            if not graph_based_validation or not hasattr(self, 'mesh_graph') or not self.mesh_graph:
                return None, 0.0, None
            
            mesh_graph = self.mesh_graph
            
            try:
                current_index = path_context.index(node_id) if node_id in path_context else -1
            except:
                current_index = -1
            
            if current_index == -1:
                return None, 0.0, None
            
            prev_node_id = path_context[current_index - 1] if current_index > 0 else None
            next_node_id = path_context[current_index + 1] if current_index < len(path_context) - 1 else None
            
            best_repeater = None
            best_score = 0.0
            best_method = None
            
            for repeater in repeaters:
                candidate_prefix = repeater.get('public_key', '')[:2].lower() if repeater.get('public_key') else None
                candidate_public_key = repeater.get('public_key', '').lower() if repeater.get('public_key') else None
                if not candidate_prefix:
                    continue
                
                graph_score = mesh_graph.get_candidate_score(
                    candidate_prefix, prev_node_id, next_node_id, min_edge_observations,
                    hop_position=current_index if graph_use_hop_position else None,
                    use_bidirectional=graph_use_bidirectional,
                    use_hop_position=graph_use_hop_position
                )
                
                stored_key_bonus = 0.0
                if graph_prefer_stored_keys and candidate_public_key:
                    if prev_node_id:
                        prev_to_candidate_edge = mesh_graph.get_edge(prev_node_id, candidate_prefix)
                        if prev_to_candidate_edge:
                            stored_to_key = prev_to_candidate_edge.get('to_public_key', '').lower() if prev_to_candidate_edge.get('to_public_key') else None
                            if stored_to_key and stored_to_key == candidate_public_key:
                                stored_key_bonus = max(stored_key_bonus, 0.4)
                    
                    if next_node_id:
                        candidate_to_next_edge = mesh_graph.get_edge(candidate_prefix, next_node_id)
                        if candidate_to_next_edge:
                            stored_from_key = candidate_to_next_edge.get('from_public_key', '').lower() if candidate_to_next_edge.get('from_public_key') else None
                            if stored_from_key and stored_from_key == candidate_public_key:
                                stored_key_bonus = max(stored_key_bonus, 0.4)
                
                # Zero-hop bonus: If this repeater has been heard directly by the bot (zero-hop advert),
                # it's strong evidence it's close and should be preferred, even for intermediate hops
                zero_hop_bonus = 0.0
                hop_count = repeater.get('hop_count')
                if hop_count is not None and hop_count == 0:
                    # This repeater has been heard directly - strong evidence it's close to bot
                    zero_hop_bonus = graph_zero_hop_bonus
                
                graph_score_with_bonus = min(1.0, graph_score + stored_key_bonus + zero_hop_bonus)
                
                multi_hop_score = 0.0
                if graph_multi_hop_enabled and graph_score_with_bonus < 0.6 and prev_node_id and next_node_id:
                    intermediate_candidates = mesh_graph.find_intermediate_nodes(
                        prev_node_id, next_node_id, min_edge_observations,
                        max_hops=graph_multi_hop_max_hops
                    )
                    
                    for intermediate_prefix, intermediate_score in intermediate_candidates:
                        if intermediate_prefix == candidate_prefix:
                            multi_hop_score = intermediate_score
                            break
                
                candidate_score = max(graph_score_with_bonus, multi_hop_score)
                method = 'graph_multihop' if multi_hop_score > graph_score_with_bonus else 'graph'
                
                # Apply distance penalty for intermediate hops (prevents selecting very distant repeaters)
                # This is especially important when graph has strong evidence for long-distance links
                if graph_distance_penalty_enabled and next_node_id is not None:  # Not final hop
                    repeater_lat = repeater.get('latitude')
                    repeater_lon = repeater.get('longitude')
                    
                    if repeater_lat is not None and repeater_lon is not None:
                        max_distance = 0.0
                        
                        # Check distance from previous node to candidate (use stored edge distance if available)
                        if prev_node_id:
                            prev_to_candidate_edge = mesh_graph.get_edge(prev_node_id, candidate_prefix)
                            if prev_to_candidate_edge and prev_to_candidate_edge.get('geographic_distance'):
                                distance = prev_to_candidate_edge.get('geographic_distance')
                                max_distance = max(max_distance, distance)
                        
                        # Check distance from candidate to next node (use stored edge distance if available)
                        if next_node_id:
                            candidate_to_next_edge = mesh_graph.get_edge(candidate_prefix, next_node_id)
                            if candidate_to_next_edge and candidate_to_next_edge.get('geographic_distance'):
                                distance = candidate_to_next_edge.get('geographic_distance')
                                max_distance = max(max_distance, distance)
                        
                        # Apply penalty if distance exceeds reasonable hop distance
                        if max_distance > graph_max_reasonable_hop_distance_km:
                            excess_distance = max_distance - graph_max_reasonable_hop_distance_km
                            normalized_excess = min(excess_distance / graph_max_reasonable_hop_distance_km, 1.0)
                            penalty = normalized_excess * graph_distance_penalty_strength
                            candidate_score = candidate_score * (1.0 - penalty)
                        elif max_distance > 0:
                            # Even if under threshold, very long hops should get a small penalty
                            if max_distance > graph_max_reasonable_hop_distance_km * 0.8:
                                small_penalty = (max_distance - graph_max_reasonable_hop_distance_km * 0.8) / (graph_max_reasonable_hop_distance_km * 0.2) * graph_distance_penalty_strength * 0.5
                                candidate_score = candidate_score * (1.0 - small_penalty)
                
                # For final hop (next_node_id is None), add bot location proximity bonus
                # This is critical for final hop selection - the last repeater before the bot should be close
                if next_node_id is None and graph_final_hop_proximity_enabled:
                    if bot_latitude is not None and bot_longitude is not None:
                        repeater_lat = repeater.get('latitude')
                        repeater_lon = repeater.get('longitude')
                        
                        if repeater_lat is not None and repeater_lon is not None:
                            # Calculate distance to bot
                            distance = calculate_distance(
                                bot_latitude, bot_longitude,
                                repeater_lat, repeater_lon
                            )
                            
                            # Apply max distance threshold if configured
                            if graph_final_hop_max_distance > 0 and distance > graph_final_hop_max_distance:
                                # Beyond max distance - significantly penalize this candidate for final hop
                                candidate_score *= 0.3  # Heavy penalty for distant final hop
                            else:
                                # Normalize distance to 0-1 score (inverse: closer = higher score)
                                # Use configurable normalization distance (default 500km for more aggressive scoring)
                                normalized_distance = min(distance / graph_final_hop_proximity_normalization_km, 1.0)
                                proximity_score = 1.0 - normalized_distance
                                
                                # For final hop, use a higher effective weight to ensure proximity matters more
                                # The configured weight is a minimum; we boost it for very close repeaters
                                effective_weight = graph_final_hop_proximity_weight
                                if distance < graph_final_hop_very_close_threshold_km:
                                    # Very close - boost weight up to max
                                    effective_weight = min(graph_final_hop_max_proximity_weight, graph_final_hop_proximity_weight * 2.0)
                                elif distance < graph_final_hop_close_threshold_km:
                                    # Close - moderate boost
                                    effective_weight = min(0.5, graph_final_hop_proximity_weight * 1.5)
                                
                                # Combine with graph score using effective weight
                                candidate_score = candidate_score * (1.0 - effective_weight) + proximity_score * effective_weight
                
                # Path validation bonus: Check if candidate's stored paths match the current path context
                path_validation_bonus = 0.0
                if candidate_public_key and len(path_context) > 1:
                    try:
                        # Query stored paths from this repeater
                        query = '''
                            SELECT path_hex, observation_count, last_seen, from_prefix, to_prefix
                            FROM observed_paths
                            WHERE public_key = ? AND packet_type = 'advert'
                            ORDER BY observation_count DESC, last_seen DESC
                            LIMIT 10
                        '''
                        stored_paths = self.db_manager.execute_query(query, (candidate_public_key,))
                        
                        if stored_paths:
                            # Build the path we're decoding (full path context)
                            decoded_path_hex = ''.join([node.lower() for node in path_context])
                            # Build the path prefix up to (but not including) the current node
                            # This helps match paths where the candidate appears at the same position
                            path_prefix_up_to_current = ''.join([node.lower() for node in path_context[:current_index]])
                            
                            # Check if any stored path shares common segments with decoded path
                            for stored_path in stored_paths:
                                stored_hex = stored_path.get('path_hex', '').lower()
                                obs_count = stored_path.get('observation_count', 1)
                                
                                if stored_hex:
                                    # Check for shared path segments
                                    stored_nodes = [stored_hex[i:i+2] for i in range(0, len(stored_hex), 2)]
                                    decoded_nodes = [decoded_path_hex[i:i+2] for i in range(0, len(decoded_path_hex), 2)]
                                    
                                    # Count how many nodes appear in both paths (in order)
                                    common_segments = 0
                                    min_len = min(len(stored_nodes), len(decoded_nodes))
                                    for i in range(min_len):
                                        if stored_nodes[i] == decoded_nodes[i]:
                                            common_segments += 1
                                        else:
                                            break
                                    
                                    # Also check if stored path starts with the same prefix as the decoded path up to current position
                                    # This is important for matching paths where the candidate appears at the same position
                                    prefix_match = False
                                    if path_prefix_up_to_current and len(stored_hex) >= len(path_prefix_up_to_current):
                                        if stored_hex.startswith(path_prefix_up_to_current):
                                            # The stored path has the same prefix, and the candidate appears at the same position
                                            # This is a strong indicator of a match
                                            prefix_match = True
                                    
                                    # Bonus based on common segments and observation count
                                    if common_segments >= 2 or prefix_match:
                                        # Stronger bonus for prefix matches (indicates same path structure)
                                        if prefix_match and common_segments >= current_index:
                                            segment_bonus = min(graph_path_validation_max_bonus, 0.1 * (current_index + 1))
                                        else:
                                            segment_bonus = min(0.2, 0.05 * common_segments)
                                        obs_bonus = min(0.15, obs_count / graph_path_validation_obs_divisor)
                                        path_validation_bonus = max(path_validation_bonus, segment_bonus + obs_bonus)
                                        # Cap at max bonus
                                        path_validation_bonus = min(graph_path_validation_max_bonus, path_validation_bonus)
                                        if path_validation_bonus >= graph_path_validation_max_bonus * 0.9:
                                            break  # Strong match found, no need to check more
                    except Exception:
                        pass
                
                # Add path validation bonus to graph score
                candidate_score = min(1.0, candidate_score + path_validation_bonus)
                
                if repeater.get('is_starred', False):
                    candidate_score *= star_bias_multiplier
                
                if candidate_score > best_score:
                    best_score = candidate_score
                    best_repeater = repeater
                    best_method = method
            
            if best_repeater and best_score > 0.0:
                confidence = min(1.0, best_score) if best_score <= 1.0 else 0.95 + (min(0.05, (best_score - 1.0) / star_bias_multiplier))
                return best_repeater, confidence, best_method or 'graph'
            
            return None, 0.0, None
        
        # Main resolution logic (same as PathCommand._lookup_repeater_names)
        repeater_info = {}
        
        try:
            for node_id in node_ids:
                # Query database for matching repeaters
                if max_repeater_age_days > 0:
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
                    '''.format(max_repeater_age_days, max_repeater_age_days)
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
                results = self.db_manager.execute_query(query, (prefix_pattern,))
                
                if results:
                    repeaters_data = [
                        {
                            'name': row['name'],
                            'public_key': row['public_key'],
                            'device_type': row['device_type'],
                            'last_seen': row['last_seen'],
                            'last_heard': row.get('last_heard', row['last_seen']),
                            'last_advert_timestamp': row.get('last_advert_timestamp'),
                            'is_active': True,
                            'latitude': row['latitude'],
                            'longitude': row['longitude'],
                            'city': row['city'],
                            'state': row['state'],
                            'country': row['country'],
                            'hop_count': row.get('hop_count'),  # Include hop_count for zero-hop bonus
                            'is_starred': bool(row.get('is_starred', 0))
                        } for row in results
                    ]
                    
                    scored_repeaters = calculate_recency_weighted_scores(repeaters_data)
                    min_recency_threshold = 0.01
                    recent_repeaters = [r for r, score in scored_repeaters if score >= min_recency_threshold]
                    
                    if len(recent_repeaters) > 1:
                        # Multiple matches - use graph and geographic selection
                        graph_repeater = None
                        graph_confidence = 0.0
                        selection_method = None
                        geo_repeater = None
                        geo_confidence = 0.0
                        
                        if graph_based_validation and hasattr(self, 'mesh_graph') and self.mesh_graph:
                            graph_repeater, graph_confidence, selection_method = select_repeater_by_graph(
                                recent_repeaters, node_id, node_ids
                            )
                        
                        if geographic_guessing_enabled:
                            if proximity_method == 'path':
                                geo_repeater, geo_confidence = select_by_path_proximity(
                                    recent_repeaters, node_id, node_ids, None
                                )
                            else:
                                geo_repeater, geo_confidence = select_by_simple_proximity(recent_repeaters)
                        
                        # Combine or choose
                        selected_repeater = None
                        confidence = 0.0
                        final_method = None
                        
                        if graph_geographic_combined and graph_repeater and geo_repeater:
                            graph_pubkey = graph_repeater.get('public_key', '')
                            geo_pubkey = geo_repeater.get('public_key', '')
                            
                            if graph_pubkey and geo_pubkey and graph_pubkey == geo_pubkey:
                                combined_confidence = (
                                    graph_confidence * graph_geographic_weight +
                                    geo_confidence * (1.0 - graph_geographic_weight)
                                )
                                selected_repeater = graph_repeater
                                confidence = combined_confidence
                                final_method = 'graph_geographic_combined'
                            else:
                                if graph_confidence > geo_confidence:
                                    selected_repeater = graph_repeater
                                    confidence = graph_confidence
                                    final_method = selection_method or 'graph'
                                else:
                                    selected_repeater = geo_repeater
                                    confidence = geo_confidence
                                    final_method = 'geographic'
                        else:
                            # For final hop, prefer geographic selection if available and reasonable
                            # The final hop should be close to the bot, so geographic proximity is very important
                            is_final_hop = (node_id == node_ids[-1] if node_ids else False)
                            
                            if is_final_hop and geo_repeater and geo_confidence >= 0.6:
                                # For final hop, prefer geographic if it has decent confidence
                                # This ensures we pick the closest repeater for the last hop
                                if not graph_repeater or geo_confidence >= graph_confidence * 0.9:
                                    selected_repeater = geo_repeater
                                    confidence = geo_confidence
                                    final_method = 'geographic'
                                elif graph_repeater:
                                    selected_repeater = graph_repeater
                                    confidence = graph_confidence
                                    final_method = selection_method or 'graph'
                            elif graph_repeater and graph_confidence >= graph_confidence_override_threshold:
                                selected_repeater = graph_repeater
                                confidence = graph_confidence
                                final_method = selection_method or 'graph'
                            elif not graph_repeater or graph_confidence < graph_confidence_override_threshold:
                                if geo_repeater and (not graph_repeater or geo_confidence > graph_confidence):
                                    selected_repeater = geo_repeater
                                    confidence = geo_confidence
                                    final_method = 'geographic'
                                elif graph_repeater:
                                    selected_repeater = graph_repeater
                                    confidence = graph_confidence
                                    final_method = selection_method or 'graph'
                        
                        if selected_repeater and confidence >= 0.5:
                            repeater_info[node_id] = {
                                'name': selected_repeater['name'],
                                'public_key': selected_repeater['public_key'],
                                'device_type': selected_repeater['device_type'],
                                'last_seen': selected_repeater['last_seen'],
                                'is_active': selected_repeater['is_active'],
                                'found': True,
                                'collision': False,
                                'geographic_guess': (final_method == 'geographic'),
                                'graph_guess': (final_method == 'graph' or final_method == 'graph_multihop'),
                                'confidence': confidence,
                                'selection_method': final_method,
                                'latitude': selected_repeater.get('latitude'),
                                'longitude': selected_repeater.get('longitude')
                            }
                        else:
                            repeater_info[node_id] = {
                                'found': True,
                                'collision': True,
                                'matches': len(recent_repeaters),
                                'node_id': node_id
                            }
                    elif len(recent_repeaters) == 1:
                        repeater = recent_repeaters[0]
                        repeater_info[node_id] = {
                            'name': repeater['name'],
                            'public_key': repeater['public_key'],
                            'device_type': repeater['device_type'],
                            'last_seen': repeater['last_seen'],
                            'is_active': repeater['is_active'],
                            'found': True,
                            'collision': False,
                            'latitude': repeater.get('latitude'),
                            'longitude': repeater.get('longitude')
                        }
                    else:
                        repeater_info[node_id] = {
                            'found': False,
                            'node_id': node_id
                        }
                else:
                    repeater_info[node_id] = {
                        'found': False,
                        'node_id': node_id
                    }
        except Exception as e:
            self.logger.error(f"Error resolving path: {e}")
            return {
                'node_ids': node_ids,
                'repeaters': [],
                'valid': False,
                'error': str(e)
            }
        
        # Format response
        repeaters_list = []
        for node_id in node_ids:
            info = repeater_info.get(node_id, {'found': False, 'node_id': node_id})
            repeaters_list.append({
                'node_id': node_id,
                **info
            })
        
        return {
            'node_ids': node_ids,
            'repeaters': repeaters_list,
            'valid': True
        }
    
    def _setup_routes(self):
        """Setup all Flask routes - complete feature parity"""
        # Log full traceback for 500 errors so service logs show the real cause
        @self.app.errorhandler(500)
        def internal_error(e):
            self.logger.exception("Unhandled exception (500): %s", e)
            return make_response(("Internal Server Error", 500))

        @self.app.route('/')
        def index():
            """Main dashboard"""
            return render_template('index.html')
        
        @self.app.route('/realtime')
        def realtime():
            """Real-time monitoring dashboard"""
            return render_template('realtime.html')
        
        @self.app.route('/contacts')
        def contacts():
            """Contacts page - unified contact management and tracking"""
            return render_template('contacts.html')
        
        @self.app.route('/cache')
        def cache():
            """Cache management page"""
            return render_template('cache.html')
        
        
        @self.app.route('/stats')
        def stats():
            """Statistics page"""
            return render_template('stats.html')
        
        @self.app.route('/greeter')
        def greeter():
            """Greeter management page"""
            return render_template('greeter.html')
        
        @self.app.route('/feeds')
        def feeds():
            """Feed management page"""
            return render_template('feeds.html')
        
        @self.app.route('/radio')
        def radio():
            """Radio settings page"""
            return render_template('radio.html')
        
        @self.app.route('/mesh')
        def mesh():
            """Mesh graph visualization page"""
            return render_template('mesh.html')
        
        # Favicon routes
        @self.app.route('/apple-touch-icon.png')
        def apple_touch_icon():
            """Apple touch icon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'apple-touch-icon.png'
            )
        
        @self.app.route('/favicon-32x32.png')
        def favicon_32x32():
            """32x32 favicon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'favicon-32x32.png'
            )
        
        @self.app.route('/favicon-16x16.png')
        def favicon_16x16():
            """16x16 favicon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'favicon-16x16.png'
            )
        
        @self.app.route('/site.webmanifest')
        def site_webmanifest():
            """Web manifest file"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'site.webmanifest',
                mimetype='application/manifest+json'
            )
        
        @self.app.route('/favicon.ico')
        def favicon():
            """Default favicon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'favicon.ico'
            )
        
        
        # API Routes
        @self.app.route('/api/health')
        def api_health():
            """Health check endpoint"""
            # Get bot uptime
            bot_uptime = self._get_bot_uptime()
            
            with self._clients_lock:
                client_count = len(self.connected_clients)
            
            return jsonify({
                'status': 'healthy',
                'connected_clients': client_count,
                'max_clients': self.max_clients,
                'timestamp': time.time(),
                'bot_uptime': bot_uptime,
                'version': 'modern_2.0'
            })
        
        @self.app.route('/api/system-health')
        def api_system_health():
            """Get comprehensive system health status from database"""
            try:
                # Read health data from database (consistent with how other data is accessed)
                health_data = self.db_manager.get_system_health()
                
                if not health_data:
                    # If no health data in database, return minimal status
                    return jsonify({
                        'status': 'unknown',
                        'timestamp': time.time(),
                        'message': 'Health data not available yet',
                        'components': {}
                    })
                
                # Update timestamp to reflect current time (data may be slightly stale)
                health_data['timestamp'] = time.time()
                
                # Recalculate uptime if start_time is available
                start_time = self.db_manager.get_bot_start_time()
                if start_time:
                    health_data['uptime_seconds'] = time.time() - start_time
                
                return jsonify(health_data)
                
            except Exception as e:
                self.logger.error(f"Error getting system health: {e}")
                import traceback
                self.logger.debug(traceback.format_exc())
                return jsonify({
                    'error': str(e),
                    'status': 'error'
                }), 500
        
        @self.app.route('/api/stats')
        def api_stats():
            """Get comprehensive database statistics for dashboard"""
            try:
                # Get optional time window parameters for analytics
                top_users_window = request.args.get('top_users_window', 'all')
                top_commands_window = request.args.get('top_commands_window', 'all')
                top_paths_window = request.args.get('top_paths_window', 'all')
                top_channels_window = request.args.get('top_channels_window', 'all')
                stats = self._get_database_stats(
                    top_users_window=top_users_window,
                    top_commands_window=top_commands_window,
                    top_paths_window=top_paths_window,
                    top_channels_window=top_channels_window
                )
                return jsonify(stats)
            except Exception as e:
                self.logger.error(f"Error getting stats: {e}")
                return jsonify({'error': str(e)}), 500
        
        
        
        @self.app.route('/api/contacts')
        def api_contacts():
            """Get contact data. Optional query param: since=24h|7d|30d|90d|all (default 30d)."""
            try:
                since = request.args.get('since', '30d')
                if since not in ('24h', '7d', '30d', '90d', 'all'):
                    since = '30d'
                contacts = self._get_tracking_data(since=since)
                return jsonify(contacts)
            except Exception as e:
                self.logger.error(f"Error getting contacts: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/cache')
        def api_cache():
            """Get cache data"""
            try:
                cache_data = self._get_cache_data()
                return jsonify(cache_data)
            except Exception as e:
                self.logger.error(f"Error getting cache: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/database')
        def api_database():
            """Get database information"""
            try:
                db_info = self._get_database_info()
                return jsonify(db_info)
            except Exception as e:
                self.logger.error(f"Error getting database info: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/optimize-database', methods=['POST'])
        def api_optimize_database():
            """Optimize database using VACUUM, ANALYZE, and REINDEX"""
            try:
                result = self._optimize_database()
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error optimizing database: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500
        
        @self.app.route('/api/mesh/nodes')
        def api_mesh_nodes():
            """Get all repeater nodes with locations and metadata"""
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                query = '''
                    SELECT 
                        public_key,
                        SUBSTR(public_key, 1, 2) as prefix,
                        name,
                        latitude,
                        longitude,
                        role,
                        is_starred,
                        last_heard,
                        last_advert_timestamp
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                    AND latitude IS NOT NULL 
                    AND longitude IS NOT NULL
                    AND latitude != 0 
                    AND longitude != 0
                    ORDER BY name
                '''
                
                cursor.execute(query)
                rows = cursor.fetchall()
                
                nodes = []
                for row in rows:
                    nodes.append({
                        'public_key': row['public_key'],
                        'prefix': row['prefix'].lower(),
                        'name': row['name'] or f"Node {row['prefix']}",
                        'latitude': float(row['latitude']),
                        'longitude': float(row['longitude']),
                        'role': row['role'],
                        'is_starred': bool(row['is_starred']),
                        'last_heard': row['last_heard'],
                        'last_advert_timestamp': row['last_advert_timestamp']
                    })
                
                return jsonify({'nodes': nodes})
            except Exception as e:
                self.logger.error(f"Error getting mesh nodes: {e}")
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/mesh/edges')
        def api_mesh_edges():
            """Get all graph edges with metadata"""
            conn = None
            try:
                # Get optional query parameters
                min_observations = request.args.get('min_observations', type=int)
                days = request.args.get('days', type=int)
                min_distance = request.args.get('min_distance', type=float)
                max_distance = request.args.get('max_distance', type=float)
                
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                query = '''
                    SELECT 
                        from_prefix,
                        to_prefix,
                        from_public_key,
                        to_public_key,
                        observation_count,
                        first_seen,
                        last_seen,
                        avg_hop_position,
                        geographic_distance
                    FROM mesh_connections
                    WHERE 1=1
                '''
                params = []
                
                if min_observations is not None:
                    query += ' AND observation_count >= ?'
                    params.append(min_observations)
                
                if days is not None:
                    query += ' AND last_seen >= datetime("now", "-" || ? || " days")'
                    params.append(days)
                
                if min_distance is not None:
                    query += ' AND geographic_distance >= ?'
                    params.append(min_distance)
                
                if max_distance is not None:
                    query += ' AND geographic_distance <= ?'
                    params.append(max_distance)
                
                query += ' ORDER BY last_seen DESC'
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                edges = []
                for row in rows:
                    edges.append({
                        'from_prefix': row['from_prefix'].lower(),
                        'to_prefix': row['to_prefix'].lower(),
                        'from_public_key': row['from_public_key'],
                        'to_public_key': row['to_public_key'],
                        'observation_count': row['observation_count'],
                        'first_seen': row['first_seen'],
                        'last_seen': row['last_seen'],
                        'avg_hop_position': row['avg_hop_position'],
                        'geographic_distance': row['geographic_distance']
                    })
                
                return jsonify({'edges': edges})
            except Exception as e:
                self.logger.error(f"Error getting mesh edges: {e}")
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/mesh/stats')
        def api_mesh_stats():
            """Get graph statistics"""
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Get node count
                cursor.execute('''
                    SELECT COUNT(*) as count
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                    AND latitude IS NOT NULL 
                    AND longitude IS NOT NULL
                    AND latitude != 0 
                    AND longitude != 0
                ''')
                node_count = cursor.fetchone()['count']
                
                # Get edge statistics
                cursor.execute('''
                    SELECT 
                        COUNT(*) as total_edges,
                        SUM(observation_count) as total_observations,
                        AVG(observation_count) as avg_observations,
                        AVG(geographic_distance) as avg_distance,
                        MIN(geographic_distance) as min_distance,
                        MAX(geographic_distance) as max_distance,
                        COUNT(CASE WHEN from_public_key IS NOT NULL THEN 1 END) as edges_with_from_key,
                        COUNT(CASE WHEN to_public_key IS NOT NULL THEN 1 END) as edges_with_to_key,
                        COUNT(CASE WHEN from_public_key IS NOT NULL AND to_public_key IS NOT NULL THEN 1 END) as edges_with_both_keys
                    FROM mesh_connections
                ''')
                edge_stats = cursor.fetchone()
                
                # Get most connected nodes
                cursor.execute('''
                    SELECT 
                        from_prefix as prefix,
                        COUNT(*) as connection_count
                    FROM mesh_connections
                    GROUP BY from_prefix
                    UNION ALL
                    SELECT 
                        to_prefix as prefix,
                        COUNT(*) as connection_count
                    FROM mesh_connections
                    GROUP BY to_prefix
                ''')
                connection_counts = {}
                for row in cursor.fetchall():
                    prefix = row['prefix'].lower()
                    connection_counts[prefix] = connection_counts.get(prefix, 0) + row['connection_count']
                
                # Get top 10 most connected
                top_connected = sorted(connection_counts.items(), key=lambda x: x[1], reverse=True)[:10]
                
                # Get recent edges count (last 24 hours)
                cursor.execute('''
                    SELECT COUNT(*) as count
                    FROM mesh_connections
                    WHERE last_seen >= datetime("now", "-1 days")
                ''')
                recent_edges = cursor.fetchone()['count']
                
                stats = {
                    'node_count': node_count,
                    'total_edges': edge_stats['total_edges'] or 0,
                    'total_observations': edge_stats['total_observations'] or 0,
                    'avg_observations': round(edge_stats['avg_observations'] or 0, 2),
                    'avg_distance': round(edge_stats['avg_distance'] or 0, 2) if edge_stats['avg_distance'] else None,
                    'min_distance': round(edge_stats['min_distance'] or 0, 2) if edge_stats['min_distance'] else None,
                    'max_distance': round(edge_stats['max_distance'] or 0, 2) if edge_stats['max_distance'] else None,
                    'edges_with_from_key': edge_stats['edges_with_from_key'] or 0,
                    'edges_with_to_key': edge_stats['edges_with_to_key'] or 0,
                    'edges_with_both_keys': edge_stats['edges_with_both_keys'] or 0,
                    'top_connected': [{'prefix': prefix, 'count': count} for prefix, count in top_connected],
                    'recent_edges_24h': recent_edges
                }
                
                return jsonify(stats)
            except Exception as e:
                self.logger.error(f"Error getting mesh stats: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/mesh/resolve-path', methods=['POST'])
        def api_resolve_path():
            """Resolve a hex path to repeater names and locations using the same algorithm as path command"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'JSON body required'}), 400
                
                path_input = data.get('path', '').strip()
                if not path_input:
                    return jsonify({'error': 'Path input required'}), 400
                
                # Check if db_manager is initialized
                if not hasattr(self, 'db_manager') or not self.db_manager:
                    self.logger.error("db_manager not initialized")
                    return jsonify({'error': 'Database not initialized'}), 500
                
                resolved_path = self._resolve_path(path_input)
                return jsonify(resolved_path)
            except Exception as e:
                import traceback
                error_trace = traceback.format_exc()
                self.logger.error(f"Error resolving path: {e}\n{error_trace}")
                return jsonify({'error': str(e), 'traceback': error_trace}), 500
        
        @self.app.route('/api/stream_data', methods=['POST'])
        def api_stream_data():
            """API endpoint for receiving real-time data from bot"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No data provided'}), 400
                
                data_type = data.get('type')
                if data_type == 'command':
                    self._handle_command_data(data.get('data', {}))
                elif data_type == 'packet':
                    self._handle_packet_data(data.get('data', {}))
                elif data_type == 'mesh_edge':
                    self._handle_mesh_edge_data(data.get('data', {}))
                elif data_type == 'mesh_node':
                    self._handle_mesh_node_data(data.get('data', {}))
                else:
                    return jsonify({'error': 'Invalid data type'}), 400
                
                return jsonify({'status': 'success'})
            except Exception as e:
                self.logger.error(f"Error in stream_data endpoint: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/recent_commands')
        def api_recent_commands():
            """API endpoint to get recent commands from database"""
            try:
                import sqlite3
                import json
                import time
                
                # Get commands from last 60 minutes
                cutoff_time = time.time() - (60 * 60)  # 60 minutes ago
                
                with sqlite3.connect(self.db_path, timeout=30) as conn:
                    cursor = conn.cursor()
                    
                    cursor.execute('''
                        SELECT data FROM packet_stream 
                        WHERE type = 'command' AND timestamp > ?
                        ORDER BY timestamp DESC
                        LIMIT 100
                    ''', (cutoff_time,))
                    
                    rows = cursor.fetchall()
                    
                    # Parse and return commands
                    commands = []
                    for (data_json,) in rows:
                        try:
                            command_data = json.loads(data_json)
                            commands.append(command_data)
                        except Exception as e:
                            self.logger.debug(f"Error parsing command data: {e}")
                    
                    return jsonify({'commands': commands})
                
            except Exception as e:
                self.logger.error(f"Error getting recent commands: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/geocode-contact', methods=['POST'])
        def api_geocode_contact():
            """Manually geocode a contact by public_key"""
            conn = None
            try:
                data = request.get_json()
                if not data or 'public_key' not in data:
                    return jsonify({'error': 'public_key is required'}), 400
                
                public_key = data['public_key']
                
                # Get contact data from database
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT latitude, longitude, name, city, state, country
                    FROM complete_contact_tracking
                    WHERE public_key = ?
                ''', (public_key,))
                
                contact = cursor.fetchone()
                if not contact:
                    return jsonify({'error': 'Contact not found'}), 404
                
                lat = contact['latitude']
                lon = contact['longitude']
                name = contact['name']
                
                # Check if we have valid coordinates
                if lat is None or lon is None or lat == 0.0 or lon == 0.0:
                    return jsonify({'error': 'Contact does not have valid coordinates'}), 400
                
                # Perform geocoding
                self.logger.info(f"Manual geocoding requested for {name} ({public_key[:16]}...) at coordinates {lat}, {lon}")
                # sqlite3.Row objects use dictionary-style access with []
                current_city = contact['city']
                current_state = contact['state']
                current_country = contact['country']
                self.logger.debug(f"Current location data - city: {current_city}, state: {current_state}, country: {current_country}")
                
                try:
                    location_info = self.repeater_manager._get_full_location_from_coordinates(lat, lon)
                    self.logger.debug(f"Geocoding result for {name}: {location_info}")
                except Exception as geocode_error:
                    self.logger.error(f"Exception during geocoding for {name} at {lat}, {lon}: {geocode_error}", exc_info=True)
                    return jsonify({
                        'success': False,
                        'error': f'Geocoding exception: {str(geocode_error)}',
                        'location': {}
                    }), 500
                
                # Check if geocoding returned any useful data
                has_location_data = location_info.get('city') or location_info.get('state') or location_info.get('country')
                
                if not has_location_data:
                    self.logger.warning(f"Geocoding returned no location data for {name} at {lat}, {lon}. Result: {location_info}")
                    return jsonify({
                        'success': False,
                        'error': 'Geocoding returned no location data. The coordinates may be invalid or the geocoding service may be unavailable.',
                        'location': location_info
                    }), 500
                
                # Update database with new location data
                cursor.execute('''
                    UPDATE complete_contact_tracking
                    SET city = ?, state = ?, country = ?
                    WHERE public_key = ?
                ''', (
                    location_info.get('city'),
                    location_info.get('state'),
                    location_info.get('country'),
                    public_key
                ))
                
                conn.commit()
                
                # Build success message with what was found
                found_parts = []
                if location_info.get('city'):
                    found_parts.append(f"city: {location_info['city']}")
                if location_info.get('state'):
                    found_parts.append(f"state: {location_info['state']}")
                if location_info.get('country'):
                    found_parts.append(f"country: {location_info['country']}")
                
                success_message = f'Successfully geocoded {name} - Found {", ".join(found_parts)}'
                self.logger.info(f"Successfully geocoded {name}: {location_info}")
                
                return jsonify({
                    'success': True,
                    'location': location_info,
                    'message': success_message
                })
                
            except Exception as e:
                self.logger.error(f"Error geocoding contact: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/toggle-star-contact', methods=['POST'])
        def api_toggle_star_contact():
            """Toggle star status for a contact by public_key (only for repeaters and roomservers)"""
            conn = None
            try:
                data = request.get_json()
                if not data or 'public_key' not in data:
                    return jsonify({'error': 'public_key is required'}), 400
                
                public_key = data['public_key']
                
                # Get contact data from database
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Check if contact exists and is a repeater or roomserver
                cursor.execute('''
                    SELECT name, is_starred, role FROM complete_contact_tracking
                    WHERE public_key = ?
                ''', (public_key,))
                
                contact = cursor.fetchone()
                if not contact:
                    return jsonify({'error': 'Contact not found'}), 404
                
                # Only allow starring repeaters and roomservers
                # sqlite3.Row objects use dictionary-style access with []
                role = contact['role']
                if role and role.lower() not in ('repeater', 'roomserver'):
                    return jsonify({'error': 'Only repeaters and roomservers can be starred'}), 400
                
                # Toggle star status
                # sqlite3.Row objects use dictionary-style access with []
                current_starred = contact['is_starred']
                new_star_status = 1 if not current_starred else 0
                cursor.execute('''
                    UPDATE complete_contact_tracking
                    SET is_starred = ?
                    WHERE public_key = ?
                ''', (new_star_status, public_key))
                
                conn.commit()
                
                action = 'starred' if new_star_status else 'unstarred'
                self.logger.info(f"Contact {contact['name']} ({public_key[:16]}...) {action}")
                
                return jsonify({
                    'success': True,
                    'is_starred': bool(new_star_status),
                    'message': f'Contact {action} successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Error toggling star status: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/decode-path', methods=['POST'])
        def api_decode_path():
            """Decode path hex string to repeater names (similar to path command)"""
            try:
                data = request.get_json()
                if not data or 'path_hex' not in data:
                    return jsonify({'error': 'path_hex is required'}), 400
                
                path_hex = data['path_hex']
                if not path_hex:
                    return jsonify({'error': 'path_hex cannot be empty'}), 400
                
                # Decode the path
                decoded_path = self._decode_path_hex(path_hex)
                
                return jsonify({
                    'success': True,
                    'path': decoded_path
                })
                
            except Exception as e:
                self.logger.error(f"Error decoding path: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/delete-contact', methods=['POST'])
        def api_delete_contact():
            """Delete a contact from the complete contact tracking database"""
            conn = None
            try:
                data = request.get_json()
                if not data or 'public_key' not in data:
                    return jsonify({'error': 'public_key is required'}), 400
                
                public_key = data['public_key']
                
                # Get contact data from database to log what we're deleting
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Check if contact exists
                cursor.execute('''
                    SELECT name, role, device_type FROM complete_contact_tracking
                    WHERE public_key = ?
                ''', (public_key,))
                
                contact = cursor.fetchone()
                if not contact:
                    return jsonify({'error': 'Contact not found'}), 404
                
                contact_name = contact['name']
                contact_role = contact['role']
                contact_device_type = contact['device_type']
                
                # Delete from all related tables
                deleted_counts = {}
                
                # Delete from complete_contact_tracking
                cursor.execute('DELETE FROM complete_contact_tracking WHERE public_key = ?', (public_key,))
                deleted_counts['complete_contact_tracking'] = cursor.rowcount
                
                # Delete from daily_stats
                cursor.execute('DELETE FROM daily_stats WHERE public_key = ?', (public_key,))
                deleted_counts['daily_stats'] = cursor.rowcount
                
                # Delete from repeater_contacts if it exists
                try:
                    cursor.execute('DELETE FROM repeater_contacts WHERE public_key = ?', (public_key,))
                    deleted_counts['repeater_contacts'] = cursor.rowcount
                except sqlite3.OperationalError:
                    # Table might not exist, that's okay
                    deleted_counts['repeater_contacts'] = 0
                
                conn.commit()
                
                # Log the deletion
                self.logger.info(f"Contact deleted: {contact_name} ({public_key[:16]}...) - Role: {contact_role}, Device: {contact_device_type}")
                self.logger.debug(f"Deleted counts: {deleted_counts}")
                
                return jsonify({
                    'success': True,
                    'message': f'Contact "{contact_name}" has been deleted successfully',
                    'deleted_counts': deleted_counts
                })
                
            except Exception as e:
                self.logger.error(f"Error deleting contact: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/greeter')
        def api_greeter():
            """Get greeter data including rollout status, settings, and greeted users"""
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Check if greeter tables exist
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='greeter_rollout'")
                if not cursor.fetchone():
                    return jsonify({
                        'enabled': False,
                        'rollout_active': False,
                        'settings': {},
                        'greeted_users': [],
                        'error': 'Greeter tables not found'
                    })
                
                # Get active rollout status
                cursor.execute('''
                    SELECT id, rollout_started_at, rollout_days, rollout_completed,
                           datetime(rollout_started_at, '+' || rollout_days || ' days') as end_date,
                           datetime('now') as current_time
                    FROM greeter_rollout
                    WHERE rollout_completed = 0
                    ORDER BY rollout_started_at DESC
                    LIMIT 1
                ''')
                rollout = cursor.fetchone()
                
                rollout_active = False
                rollout_data = None
                time_remaining = None
                
                if rollout:
                    rollout_id = rollout['id']
                    started_at_str = rollout['rollout_started_at']
                    rollout_days = rollout['rollout_days']
                    end_date_str = rollout['end_date']
                    current_time_str = rollout['current_time']
                    
                    end_date = datetime.fromisoformat(end_date_str)
                    current_time = datetime.fromisoformat(current_time_str)
                    
                    if current_time < end_date:
                        rollout_active = True
                        remaining_seconds = (end_date - current_time).total_seconds()
                        time_remaining = {
                            'days': int(remaining_seconds // 86400),
                            'hours': int((remaining_seconds % 86400) // 3600),
                            'minutes': int((remaining_seconds % 3600) // 60),
                            'seconds': int(remaining_seconds % 60),
                            'total_seconds': int(remaining_seconds)
                        }
                        rollout_data = {
                            'id': rollout_id,
                            'started_at': started_at_str,
                            'days': rollout_days,
                            'end_date': end_date_str
                        }
                
                # Get greeter settings from config
                settings = {
                    'enabled': self.config.getboolean('Greeter_Command', 'enabled', fallback=False),
                    'greeting_message': self.config.get('Greeter_Command', 'greeting_message', 
                                                       fallback='Welcome to the mesh, {sender}!'),
                    'rollout_days': self.config.getint('Greeter_Command', 'rollout_days', fallback=7),
                    'include_mesh_info': self.config.getboolean('Greeter_Command', 'include_mesh_info', 
                                                               fallback=True),
                    'mesh_info_format': self.config.get('Greeter_Command', 'mesh_info_format',
                                                      fallback='\n\nMesh Info: {total_contacts} contacts, {repeaters} repeaters'),
                    'per_channel_greetings': self.config.getboolean('Greeter_Command', 'per_channel_greetings',
                                                                   fallback=False)
                }
                
                # Generate sample greeting
                sample_greeting = settings['greeting_message'].format(sender='SampleUser')
                if settings['include_mesh_info']:
                    sample_mesh_info = settings['mesh_info_format'].format(
                        total_contacts=100,
                        repeaters=5,
                        companions=95,
                        recent_activity_24h=10
                    )
                    sample_greeting += sample_mesh_info
                
                # Check if message_stats table exists for last seen data
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message_stats'")
                has_message_stats = cursor.fetchone() is not None
                
                # Get greeted users - use GROUP BY to ensure only one entry per (sender_id, channel)
                # This handles any potential duplicates that might exist in the database
                # We use MIN(greeted_at) to get the earliest (first) greeting time
                # If per_channel_greetings is False, we'll still show one entry per user (channel will be NULL)
                # If per_channel_greetings is True, we'll show one entry per user per channel
                cursor.execute('''
                    SELECT sender_id, channel, MIN(greeted_at) as greeted_at, 
                           MAX(rollout_marked) as rollout_marked
                    FROM greeted_users
                    GROUP BY sender_id, channel
                    ORDER BY MIN(greeted_at) DESC
                    LIMIT 500
                ''')
                greeted_users_rows = cursor.fetchall()
                greeted_users = []
                
                for row in greeted_users_rows:
                    # Access row data - handle both dict-style (Row) and tuple access
                    try:
                        sender_id = row['sender_id'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[0]
                        channel_raw = row['channel'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[1]
                        greeted_at = row['greeted_at'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[2]
                        rollout_marked = row['rollout_marked'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[3]
                    except (KeyError, IndexError, TypeError) as e:
                        self.logger.error(f"Error accessing row data: {e}, row type: {type(row)}")
                        continue
                    
                    sender_id = str(sender_id) if sender_id else ''
                    channel = str(channel_raw) if channel_raw else '(global)'
                    
                    # Get last seen timestamp from message_stats if available
                    last_seen = None
                    if has_message_stats:
                        # Get the most recent channel message (not DM) for this user
                        # If per_channel_greetings is enabled, match the specific channel
                        # Otherwise, get the most recent message from any channel
                        if channel_raw:  # Use the raw channel value, not the formatted one
                            cursor.execute('''
                                SELECT MAX(timestamp) as last_seen
                                FROM message_stats
                                WHERE sender_id = ? 
                                  AND channel = ?
                                  AND is_dm = 0
                                  AND channel IS NOT NULL
                            ''', (sender_id, channel_raw))
                        else:
                            # Global greeting - get last seen from any channel
                            cursor.execute('''
                                SELECT MAX(timestamp) as last_seen
                                FROM message_stats
                                WHERE sender_id = ? 
                                  AND is_dm = 0
                                  AND channel IS NOT NULL
                            ''', (sender_id,))
                        
                        result = cursor.fetchone()
                        if result and result['last_seen']:
                            last_seen = result['last_seen']
                    
                    greeted_users.append({
                        'sender_id': sender_id,
                        'channel': channel,
                        'greeted_at': str(greeted_at),
                        'rollout_marked': bool(rollout_marked),
                        'last_seen': last_seen
                    })
                
                return jsonify({
                    'enabled': settings['enabled'],
                    'rollout_active': rollout_active,
                    'rollout_data': rollout_data,
                    'time_remaining': time_remaining,
                    'settings': settings,
                    'sample_greeting': sample_greeting,
                    'greeted_users': greeted_users,
                    'total_greeted': len(greeted_users)
                })
                
            except Exception as e:
                self.logger.error(f"Error getting greeter data: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/greeter/end-rollout', methods=['POST'])
        def api_end_rollout():
            """End the active onboarding period"""
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Find active rollout
                cursor.execute('''
                    SELECT id FROM greeter_rollout
                    WHERE rollout_completed = 0
                    ORDER BY rollout_started_at DESC
                    LIMIT 1
                ''')
                rollout = cursor.fetchone()
                
                if not rollout:
                    return jsonify({'success': False, 'error': 'No active rollout found'}), 404
                
                rollout_id = rollout['id']
                
                # Mark rollout as completed
                cursor.execute('''
                    UPDATE greeter_rollout
                    SET rollout_completed = 1
                    WHERE id = ?
                ''', (rollout_id,))
                
                conn.commit()
                
                self.logger.info(f"Greeter rollout {rollout_id} ended manually via web viewer")
                
                return jsonify({
                    'success': True,
                    'message': 'Onboarding period ended successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Error ending rollout: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/greeter/ungreet', methods=['POST'])
        def api_ungreet_user():
            """Mark a user as ungreeted (remove from greeted_users table)"""
            conn = None
            try:
                data = request.get_json()
                if not data or 'sender_id' not in data:
                    return jsonify({'error': 'sender_id is required'}), 400
                
                sender_id = data['sender_id']
                channel = data.get('channel')  # Optional - if None, removes global greeting
                
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Check if user exists
                if channel and channel != '(global)':
                    cursor.execute('''
                        SELECT id FROM greeted_users
                        WHERE sender_id = ? AND channel = ?
                    ''', (sender_id, channel))
                else:
                    cursor.execute('''
                        SELECT id FROM greeted_users
                        WHERE sender_id = ? AND channel IS NULL
                    ''', (sender_id,))
                
                if not cursor.fetchone():
                    return jsonify({'error': 'User not found in greeted users'}), 404
                
                # Delete the record
                if channel and channel != '(global)':
                    cursor.execute('''
                        DELETE FROM greeted_users
                        WHERE sender_id = ? AND channel = ?
                    ''', (sender_id, channel))
                else:
                    cursor.execute('''
                        DELETE FROM greeted_users
                        WHERE sender_id = ? AND channel IS NULL
                    ''', (sender_id,))
                
                conn.commit()
                
                self.logger.info(f"User {sender_id} marked as ungreeted (channel: {channel or 'global'})")
                
                return jsonify({
                    'success': True,
                    'message': f'User {sender_id} marked as ungreeted'
                })
                
            except Exception as e:
                self.logger.error(f"Error ungreeting user: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        # Feed management API endpoints
        @self.app.route('/api/feeds')
        def api_feeds():
            """Get all feed subscriptions with statistics"""
            try:
                feeds = self._get_feed_subscriptions()
                return jsonify(feeds)
            except Exception as e:
                self.logger.error(f"Error getting feeds: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/<int:feed_id>')
        def api_feed_detail(feed_id):
            """Get detailed information about a specific feed"""
            try:
                feed = self._get_feed_subscription(feed_id)
                if not feed:
                    return jsonify({'error': 'Feed not found'}), 404
                
                # Get activity and errors
                activity = self._get_feed_activity(feed_id)
                errors = self._get_feed_errors(feed_id)
                
                feed['activity'] = activity
                feed['errors'] = errors
                
                return jsonify(feed)
            except Exception as e:
                self.logger.error(f"Error getting feed detail: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds', methods=['POST'])
        def api_create_feed():
            """Create a new feed subscription"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No data provided'}), 400
                
                feed_id = self._create_feed_subscription(data)
                return jsonify({'success': True, 'id': feed_id})
            except Exception as e:
                self.logger.error(f"Error creating feed: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/<int:feed_id>', methods=['PUT'])
        def api_update_feed(feed_id):
            """Update an existing feed subscription"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No data provided'}), 400
                
                success = self._update_feed_subscription(feed_id, data)
                if not success:
                    return jsonify({'error': 'Feed not found'}), 404
                
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Error updating feed: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/<int:feed_id>', methods=['DELETE'])
        def api_delete_feed(feed_id):
            """Delete a feed subscription"""
            try:
                success = self._delete_feed_subscription(feed_id)
                if not success:
                    return jsonify({'error': 'Feed not found'}), 404
                
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Error deleting feed: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/default-format', methods=['GET'])
        def api_get_default_format():
            """Get the default output format from config"""
            try:
                default_format = self.config.get('Feed_Manager', 'default_output_format', 
                                                fallback='{emoji} {body|truncate:100} - {date}\n{link|truncate:50}')
                return jsonify({'default_format': default_format})
            except Exception as e:
                self.logger.error(f"Error getting default format: {e}")
                return jsonify({'default_format': '{emoji} {body|truncate:100} - {date}\n{link|truncate:50}'})
        
        @self.app.route('/api/feeds/preview', methods=['POST'])
        def api_preview_feed():
            """Preview feed items with custom output format"""
            try:
                data = request.get_json()
                if not data or 'feed_url' not in data:
                    return jsonify({'error': 'feed_url is required'}), 400
                
                feed_url = data['feed_url']
                feed_type = data.get('feed_type', 'rss')
                output_format = data.get('output_format', '')
                api_config = data.get('api_config', {})
                filter_config = data.get('filter_config')
                sort_config = data.get('sort_config')
                
                # Get default format from config if not provided
                if not output_format:
                    output_format = self.config.get('Feed_Manager', 'default_output_format', 
                                                   fallback='{emoji} {body|truncate:100} - {date}\n{link|truncate:50}')
                
                # Fetch and format feed items
                preview_items = self._preview_feed_items(feed_url, feed_type, output_format, api_config, filter_config, sort_config)
                
                return jsonify({
                    'success': True,
                    'items': preview_items
                })
            except Exception as e:
                self.logger.error(f"Error previewing feed: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/test', methods=['POST'])
        def api_test_feed():
            """Test a feed URL and return preview of recent items"""
            try:
                data = request.get_json()
                if not data or 'url' not in data:
                    return jsonify({'error': 'URL is required'}), 400
                
                # This would require feed_manager - for now just validate URL
                from urllib.parse import urlparse
                url = data['url']
                result = urlparse(url)
                if not all([result.scheme in ['http', 'https'], result.netloc]):
                    return jsonify({'error': 'Invalid URL format'}), 400
                
                return jsonify({'success': True, 'message': 'URL validated (full test requires feed manager)'})
            except Exception as e:
                self.logger.error(f"Error testing feed: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/stats')
        def api_feed_stats():
            """Get aggregate feed statistics"""
            try:
                stats = self._get_feed_statistics()
                return jsonify(stats)
            except Exception as e:
                self.logger.error(f"Error getting feed stats: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/<int:feed_id>/activity')
        def api_feed_activity(feed_id):
            """Get activity log for a specific feed"""
            try:
                activity = self._get_feed_activity(feed_id, limit=50)
                return jsonify({'activity': activity})
            except Exception as e:
                self.logger.error(f"Error getting feed activity: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/<int:feed_id>/errors')
        def api_feed_errors(feed_id):
            """Get error history for a specific feed"""
            try:
                errors = self._get_feed_errors(feed_id, limit=20)
                return jsonify({'errors': errors})
            except Exception as e:
                self.logger.error(f"Error getting feed errors: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/feeds/<int:feed_id>/refresh', methods=['POST'])
        def api_refresh_feed(feed_id):
            """Manually trigger a feed check"""
            try:
                # This would trigger feed_manager to poll this feed immediately
                # For now, just acknowledge the request
                return jsonify({'success': True, 'message': 'Feed refresh queued'})
            except Exception as e:
                self.logger.error(f"Error refreshing feed: {e}")
                return jsonify({'error': str(e)}), 500
        
        # Channel management API endpoints
        @self.app.route('/api/channels')
        def api_channels():
            """Get all configured channels"""
            try:
                channels = self._get_channels()
                return jsonify({'channels': channels})
            except Exception as e:
                self.logger.error(f"Error getting channels: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/channels', methods=['POST'])
        def api_create_channel():
            """Create a new channel (hashtag or custom)"""
            try:
                data = request.get_json()
                if not data or 'name' not in data:
                    return jsonify({'error': 'Channel name is required'}), 400
                
                channel_name = data.get('name', '').strip()
                channel_idx = data.get('channel_idx')
                channel_key = data.get('channel_key', '').strip()
                
                if not channel_name:
                    return jsonify({'error': 'Channel name cannot be empty'}), 400
                
                # If channel_idx not provided, find the lowest available index
                if channel_idx is None:
                    channel_idx = self._get_lowest_available_channel_index()
                    if channel_idx is None:
                        max_channels = self.config.getint('Bot', 'max_channels', fallback=40)
                        return jsonify({'error': f'No available channel slots. All {max_channels} channels are in use.'}), 400
                
                # Determine if it's a hashtag channel
                is_hashtag = channel_name.startswith('#')
                
                # Validate custom channel has key
                if not is_hashtag and not channel_key:
                    return jsonify({'error': 'Channel key is required for custom channels (channels without # prefix)'}), 400
                
                # Validate key format if provided
                if channel_key:
                    if len(channel_key) != 32:
                        return jsonify({'error': 'Channel key must be exactly 32 hexadecimal characters'}), 400
                    if not all(c in '0123456789abcdefABCDEF' for c in channel_key):
                        return jsonify({'error': 'Channel key must contain only hexadecimal characters (0-9, a-f, A-F)'}), 400
                
                # Try to create channel via bot's channel manager
                result = self._add_channel_for_web(channel_idx, channel_name, channel_key if not is_hashtag else None)
                
                if result.get('success'):
                    if result.get('pending'):
                        # Operation is queued, return operation_id for polling
                        return jsonify({
                            'success': True,
                            'pending': True,
                            'operation_id': result.get('operation_id'),
                            'message': result.get('message', 'Channel operation queued')
                        })
                    else:
                        return jsonify({'success': True, 'message': 'Channel created successfully'})
                else:
                    return jsonify({'error': result.get('error', 'Failed to create channel')}), 500
                    
            except Exception as e:
                self.logger.error(f"Error creating channel: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/channels/<int:channel_idx>', methods=['DELETE'])
        def api_delete_channel(channel_idx):
            """Remove a channel"""
            try:
                result = self._remove_channel_for_web(channel_idx)
                if result.get('success'):
                    if result.get('pending'):
                        # Operation is queued, return operation_id for polling
                        return jsonify({
                            'success': True,
                            'pending': True,
                            'operation_id': result.get('operation_id'),
                            'message': result.get('message', 'Channel operation queued')
                        })
                    else:
                        return jsonify({'success': True, 'message': 'Channel deleted successfully'})
                else:
                    return jsonify({'error': result.get('error', 'Failed to delete channel')}), 500
            except Exception as e:
                self.logger.error(f"Error deleting channel: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/channel-operations/<int:operation_id>', methods=['GET'])
        def api_get_operation_status(operation_id):
            """Get status of a channel operation"""
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT status, error_message, result_data, processed_at
                    FROM channel_operations
                    WHERE id = ?
                ''', (operation_id,))
                
                result = cursor.fetchone()
                
                if not result:
                    return jsonify({'error': 'Operation not found'}), 404
                
                status, error_msg, result_data, processed_at = result
                
                return jsonify({
                    'operation_id': operation_id,
                    'status': status,
                    'error_message': error_msg,
                    'processed_at': processed_at,
                    'result_data': json.loads(result_data) if result_data else None
                })
            except Exception as e:
                self.logger.error(f"Error getting operation status: {e}")
                return jsonify({'error': str(e)}), 500
            finally:
                if conn:
                    conn.close()
        
        @self.app.route('/api/channels/validate', methods=['POST'])
        def api_validate_channel():
            """Validate if a channel exists or can be created"""
            try:
                data = request.get_json()
                if not data or 'name' not in data:
                    return jsonify({'error': 'Channel name is required'}), 400
                
                channel_name = data['name']
                # Check if channel exists
                channel_num = self._get_channel_number(channel_name)
                
                return jsonify({
                    'exists': channel_num is not None,
                    'channel_num': channel_num
                })
            except Exception as e:
                self.logger.error(f"Error validating channel: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/channels/<int:channel_idx>', methods=['PUT'])
        def api_update_channel(channel_idx):
            """Update channel name or configuration"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No data provided'}), 400
                
                # This would use channel_manager
                return jsonify({'success': True, 'message': 'Channel update requires bot connection'})
            except Exception as e:
                self.logger.error(f"Error updating channel: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/channels/stats')
        def api_channel_stats():
            """Get channel statistics and usage data"""
            try:
                stats = self._get_channel_statistics()
                return jsonify(stats)
            except Exception as e:
                self.logger.error(f"Error getting channel stats: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/channels/<int:channel_idx>/feeds')
        def api_channel_feeds(channel_idx):
            """Get all feed subscriptions for a specific channel"""
            try:
                feeds = self._get_feeds_by_channel(channel_idx)
                return jsonify({'feeds': feeds})
            except Exception as e:
                self.logger.error(f"Error getting channel feeds: {e}")
                return jsonify({'error': str(e)}), 500
    
    def _setup_socketio_handlers(self):
        """Setup SocketIO event handlers using modern patterns"""
        
        @self.socketio.on('connect')
        def handle_connect():
            """Handle client connection"""
            try:
                client_id = request.sid
                if not client_id:
                    self.logger.warning("Connect event received but client_id is None")
                    return False
                
                self.logger.info(f"Client connected: {client_id}")
                
                with self._clients_lock:
                    # Check client limit
                    if len(self.connected_clients) >= self.max_clients:
                        self.logger.warning(f"Client limit reached ({self.max_clients}), rejecting connection")
                        try:
                            disconnect()
                        except Exception as e:
                            self.logger.error(f"Error disconnecting client: {e}")
                        return False
                    
                    # Track client
                    self.connected_clients[client_id] = {
                        'connected_at': time.time(),
                        'last_activity': time.time(),
                        'subscribed_commands': False,
                        'subscribed_packets': False,
                        'subscribed_mesh': False
                    }
                    
                    # Connection status is shown via the green indicator in the navbar, no toast needed
                    self.logger.info(f"Client {client_id} connected. Total clients: {len(self.connected_clients)}")
            except Exception as e:
                self.logger.error(f"Error in handle_connect: {e}", exc_info=True)
                return False
        
        @self.socketio.on('disconnect')
        def handle_disconnect(data=None):
            """Handle client disconnection"""
            try:
                # Safely get client_id - it may be None if disconnect happens during error state
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        del self.connected_clients[client_id]
                        self.logger.info(f"Client {client_id} disconnected. Total clients: {len(self.connected_clients)}")
                    elif client_id:
                        # Client disconnected but wasn't in our tracking dict (might have been cleaned up)
                        self.logger.debug(f"Client {client_id} disconnected (not in tracking dict)")
                    else:
                        # No client_id available - this can happen during error states
                        self.logger.debug("Disconnect event received but client_id is None")
            except Exception as e:
                # Don't emit errors during disconnect as the connection may be broken
                self.logger.error(f"Error in handle_disconnect: {e}", exc_info=True)
        
        @self.socketio.on('subscribe_commands')
        def handle_subscribe_commands():
            """Handle command stream subscription"""
            try:
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        self.connected_clients[client_id]['subscribed_commands'] = True
                emit('status', {'message': 'Subscribed to command stream'})
                self.logger.debug(f"Client {client_id} subscribed to commands")
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_commands: {e}", exc_info=True)
        
        @self.socketio.on('subscribe_packets')
        def handle_subscribe_packets():
            """Handle packet stream subscription"""
            try:
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        self.connected_clients[client_id]['subscribed_packets'] = True
                emit('status', {'message': 'Subscribed to packet stream'})
                self.logger.debug(f"Client {client_id} subscribed to packets")
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_packets: {e}", exc_info=True)
        
        @self.socketio.on('subscribe_mesh')
        def handle_subscribe_mesh():
            """Handle mesh graph stream subscription"""
            try:
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        self.connected_clients[client_id]['subscribed_mesh'] = True
                emit('status', {'message': 'Subscribed to mesh graph stream'})
                self.logger.debug(f"Client {client_id} subscribed to mesh graph")
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_mesh: {e}", exc_info=True)
        
        @self.socketio.on('ping')
        def handle_ping():
            """Handle client ping (modern ping/pong pattern)"""
            try:
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        self.connected_clients[client_id]['last_activity'] = time.time()
                emit('pong')  # Server responds with pong (Flask-SocketIO 5.x pattern)
            except Exception as e:
                self.logger.error(f"Error in handle_ping: {e}", exc_info=True)
        
        @self.socketio.on_error_default
        def default_error_handler(e):
            """Handle SocketIO errors gracefully"""
            try:
                self.logger.error(f"SocketIO error: {e}", exc_info=True)
                # Only emit if we have a valid request context
                if hasattr(request, 'sid') and request.sid:
                    emit('error', {'message': str(e)})
            except Exception as emit_error:
                # If we can't emit, just log it
                self.logger.error(f"Error emitting error message: {emit_error}")
    
    def _handle_command_data(self, command_data):
        """Handle incoming command data from bot"""
        try:
            # Broadcast to subscribed clients
            with self._clients_lock:
                subscribed_clients = [
                    client_id for client_id, client_info in self.connected_clients.items()
                    if client_info.get('subscribed_commands', False)
                ]
            
            if subscribed_clients:
                self.socketio.emit('command_data', command_data, room=None)
                self.logger.debug(f"Broadcasted command data to {len(subscribed_clients)} clients")
        except Exception as e:
            self.logger.error(f"Error handling command data: {e}")
    
    def _handle_packet_data(self, packet_data):
        """Handle incoming packet data from bot"""
        try:
            # Broadcast to subscribed clients
            with self._clients_lock:
                subscribed_clients = [
                    client_id for client_id, client_info in self.connected_clients.items()
                    if client_info.get('subscribed_packets', False)
                ]
            
            if subscribed_clients:
                self.socketio.emit('packet_data', packet_data, room=None)
                self.logger.debug(f"Broadcasted packet data to {len(subscribed_clients)} clients")
        except Exception as e:
            self.logger.error(f"Error handling packet data: {e}")
    
    def _handle_mesh_edge_data(self, edge_data):
        """Handle incoming mesh edge data from bot"""
        try:
            # Broadcast to subscribed clients
            with self._clients_lock:
                subscribed_clients = [
                    client_id for client_id, client_info in self.connected_clients.items()
                    if client_info.get('subscribed_mesh', False)
                ]
            
            if subscribed_clients:
                event_type = 'mesh_edge_added' if edge_data.get('is_new', False) else 'mesh_edge_updated'
                self.socketio.emit(event_type, edge_data, room=None)
        except Exception as e:
            self.logger.error(f"Error handling mesh edge data: {e}", exc_info=True)
    
    def _handle_mesh_node_data(self, node_data):
        """Handle incoming mesh node data from bot"""
        try:
            # Broadcast to subscribed clients
            with self._clients_lock:
                subscribed_clients = [
                    client_id for client_id, client_info in self.connected_clients.items()
                    if client_info.get('subscribed_mesh', False)
                ]
            
            if subscribed_clients:
                self.socketio.emit('mesh_node_added', node_data, room=None)
        except Exception as e:
            self.logger.error(f"Error handling mesh node data: {e}", exc_info=True)
    
    def _start_database_polling(self):
        """Start background thread to poll database for new data"""
        import threading
        
        def poll_database():
            last_timestamp = 0
            consecutive_errors = 0
            max_consecutive_errors = 10
            
            while True:
                try:
                    import time
                    import sqlite3
                    import json
                    
                    # Check if database file exists and is accessible
                    db_file = Path(self.db_path)
                    if not db_file.exists():
                        consecutive_errors += 1
                        if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                            self.logger.warning(f"Database file does not exist: {self.db_path}")
                        time.sleep(5)
                        continue
                    
                    if not os.access(self.db_path, os.R_OK):
                        consecutive_errors += 1
                        if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                            self.logger.warning(f"Database file is not readable: {self.db_path}")
                        time.sleep(5)
                        continue
                    
                    # Connect to database with timeout to prevent hanging
                    # Use check_same_thread=False for thread safety, but be careful
                    try:
                        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
                        conn.row_factory = sqlite3.Row
                    except sqlite3.OperationalError as conn_error:
                        error_msg = str(conn_error)
                        if "locked" in error_msg.lower() or "database is locked" in error_msg.lower():
                            consecutive_errors += 1
                            if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                                self.logger.warning(f"Database is locked, waiting: {self.db_path}")
                            time.sleep(2)
                            continue
                        else:
                            raise
                    
                    try:
                        cursor = conn.cursor()
                        
                        # Get new data since last poll
                        cursor.execute('''
                            SELECT timestamp, data, type FROM packet_stream 
                            WHERE timestamp > ? 
                            ORDER BY timestamp ASC
                        ''', (last_timestamp,))
                        
                        rows = cursor.fetchall()
                        
                        # Process new data
                        for row in rows:
                            try:
                                timestamp = row[0]
                                data_json = row[1]
                                data_type = row[2]
                                data = json.loads(data_json)
                                
                                # Broadcast based on type
                                if data_type == 'command':
                                    self._handle_command_data(data)
                                elif data_type == 'packet':
                                    self._handle_packet_data(data)
                                elif data_type == 'routing':
                                    self._handle_packet_data(data)  # Treat routing as packet data
                                    
                            except Exception as e:
                                self.logger.warning(f"Error processing database data: {e}")
                        
                        # Update last timestamp
                        if rows:
                            last_timestamp = rows[-1][0]
                        
                        # Reset error counter on success
                        consecutive_errors = 0
                    finally:
                        conn.close()
                    
                    # Sleep before next poll
                    time.sleep(0.5)  # Poll every 500ms
                    
                except sqlite3.OperationalError as e:
                    consecutive_errors += 1
                    error_msg = str(e)
                    
                    # Provide more diagnostic information on first error or periodic errors
                    if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                        db_file = Path(self.db_path)
                        exists = db_file.exists()
                        readable = os.access(self.db_path, os.R_OK) if exists else False
                        writable = os.access(self.db_path, os.W_OK) if exists else False
                        self.logger.error(
                            f"Database polling error (attempt {consecutive_errors}): {error_msg}\n"
                            f"  Path: {self.db_path}\n"
                            f"  Exists: {exists}\n"
                            f"  Readable: {readable}\n"
                            f"  Writable: {writable}"
                        )
                    
                    # Log at appropriate level based on error frequency
                    if consecutive_errors >= max_consecutive_errors:
                        if consecutive_errors == max_consecutive_errors:
                            self.logger.error(f"Database polling persistent error (attempt {consecutive_errors}): {error_msg}")
                        # Exponential backoff for persistent errors
                        time.sleep(min(60, 2 ** min(consecutive_errors - max_consecutive_errors, 5)))
                    elif consecutive_errors > 3:
                        self.logger.warning(f"Database polling error (attempt {consecutive_errors}): {error_msg}")
                        time.sleep(5)  # Wait longer on repeated errors
                    else:
                        self.logger.debug(f"Database polling error (attempt {consecutive_errors}): {error_msg}")
                        time.sleep(1)  # Wait longer on error
                    
                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        if consecutive_errors == max_consecutive_errors:
                            self.logger.error(f"Database polling unexpected error (attempt {consecutive_errors}): {e}", exc_info=True)
                        time.sleep(min(60, 2 ** min(consecutive_errors - max_consecutive_errors, 5)))
                    else:
                        self.logger.warning(f"Database polling unexpected error (attempt {consecutive_errors}): {e}")
                        time.sleep(2)
                
        
        # Start polling thread
        polling_thread = threading.Thread(target=poll_database, daemon=True)
        polling_thread.start()
        self.logger.info("Database polling started")
    
    def _start_cleanup_scheduler(self):
        """Start background thread for periodic database cleanup"""
        import threading
        
        def cleanup_scheduler():
            import time
            while True:
                try:
                    # Clean up stale clients every 5 minutes
                    for _ in range(12):  # 12 x 5 minutes = 1 hour
                        time.sleep(300)  # 5 minutes
                        self._cleanup_stale_clients()
                    
                    # Clean up old data every hour (after 12 stale client cleanups)
                    self._cleanup_old_data(days_to_keep=7)
                    
                except Exception as e:
                    self.logger.error(f"Error in cleanup scheduler: {e}", exc_info=True)
                    time.sleep(60)  # Sleep on error
        
        # Start the cleanup thread
        cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
        cleanup_thread.start()
        self.logger.info("Cleanup scheduler started")
    
    def _cleanup_stale_clients(self, max_idle_seconds: int = 300):
        """Remove clients that haven't had activity in max_idle_seconds"""
        try:
            current_time = time.time()
            stale_clients = []
            
            with self._clients_lock:
                for client_id, client_info in self.connected_clients.items():
                    last_activity = client_info.get('last_activity', 0)
                    if current_time - last_activity > max_idle_seconds:
                        stale_clients.append(client_id)
                
                for client_id in stale_clients:
                    del self.connected_clients[client_id]
            
            if stale_clients:
                self.logger.info(f"Cleaned up {len(stale_clients)} stale client(s)")
                
        except Exception as e:
            self.logger.error(f"Error cleaning up stale clients: {e}")
    
    def _cleanup_old_data(self, days_to_keep: int = 7):
        """Clean up old packet stream data to prevent database bloat"""
        conn = None
        try:
            import sqlite3
            import time
            
            cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
            
            # Use shorter timeout and isolation_level for better concurrency
            conn = sqlite3.connect(self.db_path, timeout=10, isolation_level='DEFERRED')
            cursor = conn.cursor()
            
            # Use WAL mode for better concurrent access (if not already set)
            try:
                cursor.execute('PRAGMA journal_mode=WAL')
            except sqlite3.OperationalError:
                pass  # Ignore if database is locked - WAL may already be set
            
            # Delete in smaller batches to avoid long locks
            # This prevents blocking the polling thread for extended periods
            batch_size = 1000
            total_deleted = 0
            
            while True:
                cursor.execute(
                    'DELETE FROM packet_stream WHERE id IN '
                    '(SELECT id FROM packet_stream WHERE timestamp < ? LIMIT ?)',
                    (cutoff_time, batch_size)
                )
                deleted_count = cursor.rowcount
                conn.commit()
                
                if deleted_count == 0:
                    break
                    
                total_deleted += deleted_count
                
                # Brief sleep between batches to allow other operations
                if deleted_count == batch_size:
                    time.sleep(0.1)
            
            if total_deleted > 0:
                self.logger.info(f"Cleaned up {total_deleted} old packet stream entries (older than {days_to_keep} days)")
            
        except sqlite3.OperationalError as e:
            self.logger.warning(f"Database busy during cleanup (will retry next cycle): {e}")
        except Exception as e:
            self.logger.error(f"Error cleaning up old packet stream data: {e}", exc_info=True)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    
    def _get_database_stats(self, top_users_window='all', top_commands_window='all', 
                           top_paths_window='all', top_channels_window='all'):
        """Get comprehensive database statistics for dashboard"""
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get all available tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            
            with self._clients_lock:
                client_count = len(self.connected_clients)
            
            stats = {
                'timestamp': time.time(),
                'connected_clients': client_count,
                'tables': tables
            }
            
            # Contact and tracking statistics
            if 'complete_contact_tracking' in tables:
                cursor.execute("SELECT COUNT(*) FROM complete_contact_tracking")
                stats['total_contacts'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM complete_contact_tracking 
                    WHERE last_heard > datetime('now', '-24 hours')
                """)
                stats['contacts_24h'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM complete_contact_tracking 
                    WHERE last_heard > datetime('now', '-7 days')
                """)
                stats['contacts_7d'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM complete_contact_tracking 
                    WHERE is_currently_tracked = 1
                """)
                stats['tracked_contacts'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT AVG(hop_count) FROM complete_contact_tracking 
                    WHERE hop_count IS NOT NULL
                """)
                avg_hops = cursor.fetchone()[0]
                stats['avg_hop_count'] = round(avg_hops, 1) if avg_hops else 0
                
                cursor.execute("""
                    SELECT MAX(hop_count) FROM complete_contact_tracking 
                    WHERE hop_count IS NOT NULL
                """)
                stats['max_hop_count'] = cursor.fetchone()[0] or 0
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT role) FROM complete_contact_tracking 
                    WHERE role IS NOT NULL
                """)
                stats['unique_roles'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT device_type) FROM complete_contact_tracking 
                    WHERE device_type IS NOT NULL
                """)
                stats['unique_device_types'] = cursor.fetchone()[0]
            
            # Advertisement statistics using daily tracking table
            if 'daily_stats' in tables:
                # Total advertisements (all time)
                cursor.execute("""
                    SELECT SUM(advert_count) FROM daily_stats
                """)
                total_adverts = cursor.fetchone()[0]
                stats['total_advertisements'] = total_adverts or 0
                
                # 24h advertisements
                cursor.execute("""
                    SELECT SUM(advert_count) FROM daily_stats 
                    WHERE date = date('now')
                """)
                stats['advertisements_24h'] = cursor.fetchone()[0] or 0
                
                # 7d advertisements (last 7 days, excluding today)
                cursor.execute("""
                    SELECT SUM(advert_count) FROM daily_stats 
                    WHERE date >= date('now', '-7 days') AND date < date('now')
                """)
                stats['advertisements_7d'] = cursor.fetchone()[0] or 0
                
                # Nodes per day statistics
                cursor.execute("""
                    SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                    WHERE date = date('now')
                """)
                stats['nodes_24h'] = cursor.fetchone()[0] or 0
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                    WHERE date >= date('now', '-6 days')
                """)
                stats['nodes_7d'] = cursor.fetchone()[0] or 0
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT public_key) FROM daily_stats
                """)
                stats['nodes_all'] = cursor.fetchone()[0] or 0
            else:
                # Fallback to old method if daily table doesn't exist yet
                if 'complete_contact_tracking' in tables:
                    cursor.execute("""
                        SELECT SUM(advert_count) FROM complete_contact_tracking
                    """)
                    total_adverts = cursor.fetchone()[0]
                    stats['total_advertisements'] = total_adverts or 0
                    
                    cursor.execute("""
                        SELECT SUM(advert_count) FROM complete_contact_tracking 
                        WHERE last_heard > datetime('now', '-24 hours')
                    """)
                    stats['advertisements_24h'] = cursor.fetchone()[0] or 0
                    
                    cursor.execute("""
                        SELECT SUM(advert_count) FROM complete_contact_tracking 
                        WHERE last_heard > datetime('now', '-7 days')
                    """)
                    stats['advertisements_7d'] = cursor.fetchone()[0] or 0
            
            # Repeater contacts (if exists)
            if 'repeater_contacts' in tables:
                cursor.execute("SELECT COUNT(*) FROM repeater_contacts")
                stats['repeater_contacts'] = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM repeater_contacts WHERE is_active = 1")
                stats['active_repeater_contacts'] = cursor.fetchone()[0]
            
            # Cache statistics
            cache_tables = [t for t in tables if 'cache' in t]
            stats['cache_tables'] = cache_tables
            stats['total_cache_entries'] = 0
            stats['active_cache_entries'] = 0
            
            for table in cache_tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                stats['total_cache_entries'] += count
                stats[f'{table}_count'] = count
                
                # Get active entries (not expired)
                cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE expires_at > datetime('now')")
                active_count = cursor.fetchone()[0]
                stats['active_cache_entries'] += active_count
                stats[f'{table}_active'] = active_count
            
            # Message and command statistics (if stats tables exist)
            if 'message_stats' in tables:
                cursor.execute("SELECT COUNT(*) FROM message_stats")
                stats['total_messages'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM message_stats 
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
                stats['messages_24h'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT sender_id) FROM message_stats 
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
                stats['unique_senders_24h'] = cursor.fetchone()[0]
                
                # Total unique users and channels
                cursor.execute("SELECT COUNT(DISTINCT sender_id) FROM message_stats")
                stats['unique_users_total'] = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(DISTINCT channel) FROM message_stats WHERE channel IS NOT NULL")
                stats['unique_channels_total'] = cursor.fetchone()[0]
                
                # Top users (most frequent message senders) - filter by time window
                if top_users_window == '24h':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-24 hours')"
                elif top_users_window == '7d':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-7 days')"
                elif top_users_window == '30d':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-30 days')"
                else:  # 'all'
                    time_filter = ""
                
                query = f"""
                    SELECT sender_id, COUNT(*) as count 
                    FROM message_stats 
                    {time_filter}
                    GROUP BY sender_id 
                    ORDER BY count DESC 
                    LIMIT 15
                """
                cursor.execute(query)
                stats['top_users'] = [{'user': row[0], 'count': row[1]} for row in cursor.fetchall()]
            
            if 'command_stats' in tables:
                cursor.execute("SELECT COUNT(*) FROM command_stats")
                stats['total_commands'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
                stats['commands_24h'] = cursor.fetchone()[0]
                
                # Top commands - filter by time window
                if top_commands_window == '24h':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-24 hours')"
                elif top_commands_window == '7d':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-7 days')"
                elif top_commands_window == '30d':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-30 days')"
                else:  # 'all'
                    time_filter = ""
                
                query = f"""
                    SELECT command_name, COUNT(*) as count 
                    FROM command_stats 
                    {time_filter}
                    GROUP BY command_name 
                    ORDER BY count DESC 
                    LIMIT 15
                """
                cursor.execute(query)
                stats['top_commands'] = [{'command': row[0], 'count': row[1]} for row in cursor.fetchall()]
                
                # Bot reply rates (commands that got responses) - calculate for different time windows
                # 24 hour reply rate
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-24 hours') AND response_sent = 1
                """)
                replied_24h = cursor.fetchone()[0]
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
                total_24h = cursor.fetchone()[0]
                if total_24h > 0:
                    stats['bot_reply_rate_24h'] = round((replied_24h / total_24h) * 100, 1)
                else:
                    stats['bot_reply_rate_24h'] = 0
                
                # 7 day reply rate
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-7 days') AND response_sent = 1
                """)
                replied_7d = cursor.fetchone()[0]
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-7 days')
                """)
                total_7d = cursor.fetchone()[0]
                if total_7d > 0:
                    stats['bot_reply_rate_7d'] = round((replied_7d / total_7d) * 100, 1)
                else:
                    stats['bot_reply_rate_7d'] = 0
                
                # 30 day reply rate
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-30 days') AND response_sent = 1
                """)
                replied_30d = cursor.fetchone()[0]
                cursor.execute("""
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp > strftime('%s', 'now', '-30 days')
                """)
                total_30d = cursor.fetchone()[0]
                if total_30d > 0:
                    stats['bot_reply_rate_30d'] = round((replied_30d / total_30d) * 100, 1)
                else:
                    stats['bot_reply_rate_30d'] = 0
                
                # Top channels by message count - filter by time window
                if top_channels_window == '24h':
                    time_filter = "AND timestamp > strftime('%s', 'now', '-24 hours')"
                elif top_channels_window == '7d':
                    time_filter = "AND timestamp > strftime('%s', 'now', '-7 days')"
                elif top_channels_window == '30d':
                    time_filter = "AND timestamp > strftime('%s', 'now', '-30 days')"
                else:  # 'all'
                    time_filter = ""
                
                query = f"""
                    SELECT channel, COUNT(*) as message_count, COUNT(DISTINCT sender_id) as unique_users
                    FROM message_stats 
                    WHERE channel IS NOT NULL {time_filter}
                    GROUP BY channel 
                    ORDER BY message_count DESC 
                    LIMIT 10
                """
                cursor.execute(query)
                stats['top_channels'] = [
                    {'channel': row[0], 'messages': row[1], 'users': row[2]} 
                    for row in cursor.fetchall()
                ]
            
            # Path statistics (if path_stats table exists)
            if 'path_stats' in tables:
                cursor.execute("""
                    SELECT sender_id, path_length, path_string, timestamp
                    FROM path_stats 
                    ORDER BY path_length DESC 
                    LIMIT 1
                """)
                longest_path = cursor.fetchone()
                if longest_path:
                    stats['longest_path'] = {
                        'user': longest_path[0],
                        'path_length': longest_path[1],
                        'path_string': longest_path[2],
                        'timestamp': longest_path[3]
                    }
                
                # Top paths (longest paths) - filter by time window
                if top_paths_window == '24h':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-24 hours')"
                elif top_paths_window == '7d':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-7 days')"
                elif top_paths_window == '30d':
                    time_filter = "WHERE timestamp > strftime('%s', 'now', '-30 days')"
                else:  # 'all'
                    time_filter = ""
                
                query = f"""
                    SELECT sender_id, path_length, path_string, timestamp
                    FROM path_stats 
                    {time_filter}
                    ORDER BY path_length DESC 
                    LIMIT 5
                """
                cursor.execute(query)
                stats['top_paths'] = [
                    {
                        'user': row[0], 
                        'path_length': row[1], 
                        'path_string': row[2], 
                        'timestamp': row[3]
                    } 
                    for row in cursor.fetchall()
                ]
            
            # Network health metrics
            if 'complete_contact_tracking' in tables:
                cursor.execute("""
                    SELECT AVG(snr) FROM complete_contact_tracking 
                    WHERE snr IS NOT NULL AND last_heard > datetime('now', '-24 hours')
                """)
                avg_snr = cursor.fetchone()[0]
                stats['avg_snr_24h'] = round(avg_snr, 1) if avg_snr else 0
                
                cursor.execute("""
                    SELECT AVG(signal_strength) FROM complete_contact_tracking 
                    WHERE signal_strength IS NOT NULL AND last_heard > datetime('now', '-24 hours')
                """)
                avg_signal = cursor.fetchone()[0]
                stats['avg_signal_strength_24h'] = round(avg_signal, 1) if avg_signal else 0
            
            # Geographic distribution - only count currently tracked contacts heard in the last 30 days
            # Normalize country names to avoid duplicates (e.g., "United States" vs "United States of America")
            if 'complete_contact_tracking' in tables:
                cursor.execute("""
                    SELECT COUNT(DISTINCT 
                        CASE 
                            WHEN country IN ('United States', 'United States of America', 'US', 'USA') 
                            THEN 'United States'
                            ELSE country
                        END
                    ) FROM complete_contact_tracking 
                    WHERE country IS NOT NULL AND country != ''
                    AND last_heard > datetime('now', '-30 days')
                    AND is_currently_tracked = 1
                """)
                stats['countries'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT state) FROM complete_contact_tracking 
                    WHERE state IS NOT NULL AND state != ''
                    AND last_heard > datetime('now', '-30 days')
                    AND is_currently_tracked = 1
                """)
                stats['states'] = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT city) FROM complete_contact_tracking 
                    WHERE city IS NOT NULL AND city != ''
                    AND last_heard > datetime('now', '-30 days')
                    AND is_currently_tracked = 1
                """)
                stats['cities'] = cursor.fetchone()[0]
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error getting database stats: {e}")
            return {'error': str(e)}
        finally:
            if conn:
                conn.close()
    
    def _get_database_info(self):
        """Get comprehensive database information for database page"""
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get all tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            table_names = [row[0] for row in cursor.fetchall()]
            
            # Get table information
            tables = []
            total_records = 0
            
            for table_name in table_names:
                try:
                    # Get record count
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                    record_count = cursor.fetchone()[0]
                    total_records += record_count
                    
                    # Get table size (approximate)
                    cursor.execute(f"PRAGMA table_info({table_name})")
                    columns = cursor.fetchall()
                    
                    # Estimate size (rough calculation)
                    estimated_size = record_count * len(columns) * 50  # Rough estimate
                    size_str = f"{estimated_size:,} bytes" if estimated_size < 1024 else f"{estimated_size/1024:.1f} KB"
                    
                    # Get table description based on name
                    description = self._get_table_description(table_name)
                    
                    tables.append({
                        'name': table_name,
                        'record_count': record_count,
                        'size': size_str,
                        'description': description
                    })
                    
                except Exception as e:
                    self.logger.debug(f"Error getting info for table {table_name}: {e}")
                    tables.append({
                        'name': table_name,
                        'record_count': 0,
                        'size': 'Unknown',
                        'description': 'Error reading table'
                    })
            
            # Get database file size
            import os
            try:
                db_size_bytes = os.path.getsize(self.db_path)
                if db_size_bytes < 1024:
                    db_size = f"{db_size_bytes} bytes"
                elif db_size_bytes < 1024 * 1024:
                    db_size = f"{db_size_bytes/1024:.1f} KB"
                else:
                    db_size = f"{db_size_bytes/(1024*1024):.1f} MB"
            except:
                db_size = "Unknown"
            
            return {
                'total_tables': len(table_names),
                'total_records': total_records,
                'last_updated': time.strftime('%Y-%m-%d %H:%M:%S'),
                'db_size': db_size,
                'tables': tables
            }
            
        except Exception as e:
            self.logger.error(f"Error getting database info: {e}")
            return {
                'total_tables': 0,
                'total_records': 0,
                'last_updated': 'Error',
                'db_size': 'Unknown',
                'tables': []
            }
        finally:
            if conn:
                conn.close()
    
    def _get_table_description(self, table_name):
        """Get human-readable description for table"""
        descriptions = {
            'packet_stream': 'Real-time packet and command data stream',
            'complete_contact_tracking': 'Contact tracking and device information',
            'repeater_contacts': 'Repeater contact management',
            'message_stats': 'Message statistics and analytics',
            'command_stats': 'Command execution statistics',
            'path_stats': 'Network path statistics',
            'geocoding_cache': 'Geocoding service cache',
            'generic_cache': 'General purpose cache storage'
        }
        return descriptions.get(table_name, 'Database table')
    
    def _optimize_database(self):
        """Optimize database using VACUUM, ANALYZE, and REINDEX"""
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get initial database size
            import os
            initial_size = os.path.getsize(self.db_path)
            
            # Perform VACUUM to reclaim unused space
            self.logger.info("Starting database VACUUM...")
            cursor.execute("VACUUM")
            vacuum_size = os.path.getsize(self.db_path)
            vacuum_saved = initial_size - vacuum_size
            
            # Perform ANALYZE to update table statistics
            self.logger.info("Starting database ANALYZE...")
            cursor.execute("ANALYZE")
            
            # Get all tables for REINDEX
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            
            # Perform REINDEX on all tables
            self.logger.info("Starting database REINDEX...")
            reindexed_tables = []
            for table in tables:
                if table != 'sqlite_sequence':  # Skip system tables
                    try:
                        cursor.execute(f"REINDEX {table}")
                        reindexed_tables.append(table)
                    except Exception as e:
                        self.logger.debug(f"Could not reindex table {table}: {e}")
            
            # Get final database size
            final_size = os.path.getsize(self.db_path)
            total_saved = initial_size - final_size
            
            # Format size information
            def format_size(size_bytes):
                if size_bytes < 1024:
                    return f"{size_bytes} bytes"
                elif size_bytes < 1024 * 1024:
                    return f"{size_bytes/1024:.1f} KB"
                else:
                    return f"{size_bytes/(1024*1024):.1f} MB"
            
            return {
                'success': True,
                'vacuum_result': f"VACUUM completed - saved {format_size(vacuum_saved)}",
                'analyze_result': f"ANALYZE completed - updated statistics for {len(tables)} tables",
                'reindex_result': f"REINDEX completed - rebuilt indexes for {len(reindexed_tables)} tables",
                'initial_size': format_size(initial_size),
                'final_size': format_size(final_size),
                'total_saved': format_size(total_saved),
                'tables_processed': len(tables),
                'tables_reindexed': len(reindexed_tables)
            }
            
        except Exception as e:
            self.logger.error(f"Error optimizing database: {e}")
            return {
                'success': False,
                'error': str(e)
            }
        finally:
            if conn:
                conn.close()
    
    def _get_tracking_data(self, since='30d'):
        """Get contact tracking data. since: 24h, 7d, 30d, 90d, or all (heard in that window)."""
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get bot location from config
            bot_lat = self.config.getfloat('Bot', 'bot_latitude', fallback=None)
            bot_lon = self.config.getfloat('Bot', 'bot_longitude', fallback=None)
            
            # Filter by last_heard for performance (default: last 30 days)
            if since == 'all':
                where_clause = ''
                params = ()
            else:
                if since == '24h':
                    where_clause = " WHERE c.last_heard >= datetime('now', '-24 hours')"
                elif since == '7d':
                    where_clause = " WHERE c.last_heard >= datetime('now', '-7 days')"
                elif since == '30d':
                    where_clause = " WHERE c.last_heard >= datetime('now', '-30 days')"
                else:  # 90d
                    where_clause = " WHERE c.last_heard >= datetime('now', '-90 days')"
                params = ()
            
            # Query with LEFT JOIN to get all paths from observed_paths
            cursor.execute("""
                SELECT 
                    c.public_key, c.name, c.role, c.device_type, 
                    c.latitude, c.longitude, c.city, c.state, c.country,
                    c.snr, c.hop_count, c.first_heard, c.last_heard,
                    c.advert_count, c.is_currently_tracked,
                    c.raw_advert_data, c.signal_strength,
                    c.is_starred, c.out_path, c.out_path_len,
                    COUNT(*) as total_messages,
                    MAX(c.last_advert_timestamp) as last_message,
                    GROUP_CONCAT(op.path_hex, '|||') as all_paths_hex,
                    GROUP_CONCAT(op.path_length, '|||') as all_paths_length,
                    GROUP_CONCAT(op.observation_count, '|||') as all_paths_observations,
                    GROUP_CONCAT(op.last_seen, '|||') as all_paths_last_seen
                FROM complete_contact_tracking c
                LEFT JOIN observed_paths op ON c.public_key = op.public_key 
                    AND op.packet_type = 'advert'
                """ + where_clause + """
                GROUP BY c.public_key, c.name, c.role, c.device_type, 
                         c.latitude, c.longitude, c.city, c.state, c.country,
                         c.snr, c.hop_count, c.first_heard, c.last_heard,
                         c.advert_count, c.is_currently_tracked,
                         c.raw_advert_data, c.signal_strength, c.is_starred,
                         c.out_path, c.out_path_len
                ORDER BY c.last_heard DESC
            """, params)
            
            tracking = []
            for row in cursor.fetchall():
                # Parse raw advertisement data if available
                raw_advert_data_parsed = None
                if row['raw_advert_data']:
                    try:
                        import json
                        raw_advert_data_parsed = json.loads(row['raw_advert_data'])
                    except:
                        raw_advert_data_parsed = None
                
                # Calculate distance if both bot and contact have coordinates
                distance = None
                if (bot_lat is not None and bot_lon is not None and 
                    row['latitude'] is not None and row['longitude'] is not None):
                    distance = self._calculate_distance(bot_lat, bot_lon, row['latitude'], row['longitude'])
                
                # Parse all_paths from concatenated strings
                all_paths = []
                if row['all_paths_hex']:
                    paths_hex = row['all_paths_hex'].split('|||')
                    paths_length = row['all_paths_length'].split('|||') if row['all_paths_length'] else []
                    paths_observations = row['all_paths_observations'].split('|||') if row['all_paths_observations'] else []
                    paths_last_seen = row['all_paths_last_seen'].split('|||') if row['all_paths_last_seen'] else []
                    
                    for i, path_hex in enumerate(paths_hex):
                        if path_hex:  # Skip empty strings
                            all_paths.append({
                                'path_hex': path_hex,
                                'path_length': int(paths_length[i]) if i < len(paths_length) and paths_length[i] else 0,
                                'observation_count': int(paths_observations[i]) if i < len(paths_observations) and paths_observations[i] else 1,
                                'last_seen': paths_last_seen[i] if i < len(paths_last_seen) and paths_last_seen[i] else None
                            })
                
                tracking.append({
                    'user_id': row['public_key'],
                    'username': row['name'],
                    'role': row['role'],
                    'device_type': row['device_type'],
                    'latitude': row['latitude'],
                    'longitude': row['longitude'],
                    'city': row['city'],
                    'state': row['state'],
                    'country': row['country'],
                    'snr': row['snr'],
                    'hop_count': row['hop_count'],
                    'first_heard': row['first_heard'],
                    'last_seen': row['last_heard'],
                    'advert_count': row['advert_count'],
                    'is_currently_tracked': row['is_currently_tracked'],
                    'raw_advert_data': row['raw_advert_data'],
                    'raw_advert_data_parsed': raw_advert_data_parsed,
                    'signal_strength': row['signal_strength'],
                    'total_messages': row['total_messages'],
                    'last_message': row['last_message'],
                    'distance': distance,
                    'is_starred': bool(row['is_starred'] if row['is_starred'] is not None else 0),
                    'out_path': row['out_path'] if row['out_path'] is not None else '',
                    'out_path_len': row['out_path_len'] if row['out_path_len'] is not None else -1,
                    'all_paths': all_paths
                })
            
            # Get server statistics for daily tracking using direct database queries
            server_stats = {}
            try:
                # Check if daily_stats table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_stats'")
                if cursor.fetchone():
                    # 24h: Last 24 hours of advertisements
                    cursor.execute("""
                        SELECT SUM(advert_count) FROM daily_stats 
                        WHERE date >= date('now', '-1 day')
                    """)
                    server_stats['advertisements_24h'] = cursor.fetchone()[0] or 0
                    
                    # 7d: Previous 6 days (excluding today)
                    cursor.execute("""
                        SELECT SUM(advert_count) FROM daily_stats 
                        WHERE date >= date('now', '-7 days') AND date < date('now')
                    """)
                    server_stats['advertisements_7d'] = cursor.fetchone()[0] or 0
                    
                    # All: Everything
                    cursor.execute("""
                        SELECT SUM(advert_count) FROM daily_stats
                    """)
                    server_stats['total_advertisements'] = cursor.fetchone()[0] or 0
                    
                    # Nodes per day statistics
                    # Calculate today's unique nodes from complete_contact_tracking
                    # (last_heard in last 24 hours) since daily_stats might not have today's data yet
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM complete_contact_tracking 
                        WHERE last_heard >= datetime('now', '-24 hours')
                    """)
                    server_stats['nodes_24h'] = cursor.fetchone()[0] or 0
                    
                    # Get today's unique nodes by role for the stacked chart
                    cursor.execute("""
                        SELECT role, COUNT(DISTINCT public_key) as count
                        FROM complete_contact_tracking 
                        WHERE last_heard >= datetime('now', '-24 hours')
                        AND role IS NOT NULL AND role != ''
                        GROUP BY role
                    """)
                    today_by_role = {}
                    for row in cursor.fetchall():
                        role = row[0].lower() if row[0] else 'unknown'
                        count = row[1]
                        today_by_role[role] = count
                    
                    server_stats['nodes_24h_by_role'] = {
                        'companion': today_by_role.get('companion', 0),
                        'repeater': today_by_role.get('repeater', 0),
                        'roomserver': today_by_role.get('roomserver', 0),
                        'sensor': today_by_role.get('sensor', 0),
                        'other': sum(v for k, v in today_by_role.items() if k not in ['companion', 'repeater', 'roomserver', 'sensor'])
                    }
                    
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                        WHERE date >= date('now', '-7 days') AND date < date('now')
                    """)
                    server_stats['nodes_7d'] = cursor.fetchone()[0] or 0
                    
                    # Calculate day-over-day and period-over-period comparisons
                    # Today vs 7 days ago (single day comparison)
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                        WHERE date = date('now', '-7 days')
                    """)
                    result = cursor.fetchone()
                    server_stats['nodes_7d_ago'] = result[0] if result and result[0] else 0
                    
                    # Last 7 days vs previous 7 days (days 8-14 ago)
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                        WHERE date >= date('now', '-14 days') AND date < date('now', '-7 days')
                    """)
                    result = cursor.fetchone()
                    server_stats['nodes_prev_7d'] = result[0] if result and result[0] else 0
                    
                    # Last 30 days vs previous 30 days (days 31-60 ago)
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                        WHERE date >= date('now', '-60 days') AND date < date('now', '-30 days')
                    """)
                    result = cursor.fetchone()
                    server_stats['nodes_prev_30d'] = result[0] if result and result[0] else 0
                    
                    # Also get current period totals for comparison
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                        WHERE date >= date('now', '-7 days')
                    """)
                    server_stats['nodes_7d'] = cursor.fetchone()[0] or 0
                    
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats 
                        WHERE date >= date('now', '-30 days')
                    """)
                    server_stats['nodes_30d'] = cursor.fetchone()[0] or 0
                    
                    cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                    """)
                    server_stats['nodes_all'] = cursor.fetchone()[0] or 0
                    
                    # Get daily unique node counts by role for the last 30 days for the stacked graph
                    # Join daily_stats with complete_contact_tracking to get role information
                    # This gives us accurate historical daily counts by role
                    cursor.execute("""
                        SELECT ds.date, c.role, COUNT(DISTINCT ds.public_key) as daily_count
                        FROM daily_stats ds
                        LEFT JOIN complete_contact_tracking c ON ds.public_key = c.public_key
                        WHERE ds.date >= date('now', '-30 days') AND ds.date <= date('now')
                        AND (c.role IS NOT NULL AND c.role != '')
                        GROUP BY ds.date, c.role
                        ORDER BY ds.date ASC, c.role ASC
                    """)
                    daily_data_by_role = cursor.fetchall()
                    
                    # Organize data by date and role
                    daily_by_role = {}
                    for row in daily_data_by_role:
                        date_str = row[0]
                        role = (row[1] or 'unknown').lower()
                        count = row[2]
                        
                        if date_str not in daily_by_role:
                            daily_by_role[date_str] = {}
                        daily_by_role[date_str][role] = count
                    
                    # Convert to array format with all roles for each date
                    server_stats['daily_nodes_30d_by_role'] = []
                    for date_str in sorted(daily_by_role.keys()):
                        roles_data = daily_by_role[date_str]
                        server_stats['daily_nodes_30d_by_role'].append({
                            'date': date_str,
                            'companion': roles_data.get('companion', 0),
                            'repeater': roles_data.get('repeater', 0),
                            'roomserver': roles_data.get('roomserver', 0),
                            'sensor': roles_data.get('sensor', 0),
                            'other': sum(v for k, v in roles_data.items() if k not in ['companion', 'repeater', 'roomserver', 'sensor'])
                        })
                    
                    # Also keep the total count for backward compatibility
                    cursor.execute("""
                        SELECT date, COUNT(DISTINCT public_key) as daily_count
                        FROM daily_stats 
                        WHERE date >= date('now', '-30 days') AND date <= date('now')
                        GROUP BY date
                        ORDER BY date ASC
                    """)
                    daily_data = cursor.fetchall()
                    server_stats['daily_nodes_30d'] = [
                        {'date': row[0], 'count': row[1]} 
                        for row in daily_data
                    ]
                    
            except Exception as e:
                self.logger.debug(f"Could not get server stats: {e}")
            
            return {
                'tracking_data': tracking,
                'server_stats': server_stats
            }
        except Exception as e:
            self.logger.error(f"Error getting tracking data: {e}")
            return {'error': str(e)}
        finally:
            if conn:
                conn.close()
    
    def _calculate_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points using Haversine formula"""
        import math
        
        # Convert latitude and longitude from degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        
        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        # Radius of earth in kilometers
        r = 6371
        
        return c * r
    
    def _get_cache_data(self):
        """Get cache data"""
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get cache statistics
            cursor.execute("SELECT COUNT(*) FROM adverts")
            total_adverts = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(*) FROM adverts 
                WHERE timestamp > datetime('now', '-1 hour')
            """)
            recent_adverts = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(DISTINCT user_id) FROM adverts 
                WHERE timestamp > datetime('now', '-24 hours')
            """)
            active_users = cursor.fetchone()[0]
            
            return {
                'total_adverts': total_adverts,
                'recent_adverts_1h': recent_adverts,
                'active_users_24h': active_users,
                'timestamp': time.time()
            }
        except Exception as e:
            self.logger.error(f"Error getting cache data: {e}")
            return {'error': str(e)}
        finally:
            if conn:
                conn.close()
    
    
    def _get_feed_subscriptions(self, channel_filter=None):
        """Get all feed subscriptions, optionally filtered by channel"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if channel_filter:
                cursor.execute('''
                    SELECT * FROM feed_subscriptions
                    WHERE channel_name = ?
                    ORDER BY id
                ''', (channel_filter,))
            else:
                cursor.execute('''
                    SELECT * FROM feed_subscriptions
                    ORDER BY id
                ''')
            
            rows = cursor.fetchall()
            feeds = []
            for row in rows:
                feed = dict(row)
                # Get feed count for this channel
                cursor.execute('''
                    SELECT COUNT(*) FROM feed_activity
                    WHERE feed_id = ?
                ''', (feed['id'],))
                feed['item_count'] = cursor.fetchone()[0]
                
                # Get error count
                cursor.execute('''
                    SELECT COUNT(*) FROM feed_errors
                    WHERE feed_id = ? AND resolved_at IS NULL
                ''', (feed['id'],))
                feed['error_count'] = cursor.fetchone()[0]
                
                feeds.append(feed)
            
            return {'feeds': feeds, 'total': len(feeds)}
        except Exception as e:
            self.logger.error(f"Error getting feed subscriptions: {e}")
            return {'feeds': [], 'total': 0, 'error': str(e)}
        finally:
            if conn:
                conn.close()
    
    def _get_feed_subscription(self, feed_id):
        """Get a single feed subscription by ID"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM feed_subscriptions WHERE id = ?', (feed_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            self.logger.error(f"Error getting feed subscription: {e}")
            return None
        finally:
            if conn:
                conn.close()
    
    def _create_feed_subscription(self, data):
        """Create a new feed subscription"""
        import sqlite3
        import json
        conn = None
        try:
            feed_type = data.get('feed_type')
            feed_url = data.get('feed_url')
            channel_name = data.get('channel_name')
            feed_name = data.get('feed_name')
            check_interval = data.get('check_interval_seconds', 300)
            api_config = data.get('api_config')
            output_format = data.get('output_format')
            message_send_interval = data.get('message_send_interval_seconds')
            
            if not all([feed_type, feed_url, channel_name]):
                raise ValueError("feed_type, feed_url, and channel_name are required")
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            api_config_str = json.dumps(api_config) if api_config else None
            
            cursor.execute('''
                INSERT INTO feed_subscriptions 
                (feed_type, feed_url, channel_name, feed_name, check_interval_seconds, api_config, output_format, message_send_interval_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (feed_type, feed_url, channel_name, feed_name, check_interval, api_config_str, output_format, message_send_interval))
            
            conn.commit()
            return cursor.lastrowid
        except Exception as e:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
    
    def _update_feed_subscription(self, feed_id, data):
        """Update a feed subscription"""
        import sqlite3
        import json
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            updates = []
            params = []
            
            if 'feed_name' in data:
                updates.append('feed_name = ?')
                params.append(data['feed_name'])
            
            if 'check_interval_seconds' in data:
                updates.append('check_interval_seconds = ?')
                params.append(data['check_interval_seconds'])
            
            if 'enabled' in data:
                updates.append('enabled = ?')
                params.append(1 if data['enabled'] else 0)
            
            if 'api_config' in data:
                updates.append('api_config = ?')
                params.append(json.dumps(data['api_config']) if data['api_config'] else None)
            
            if 'output_format' in data:
                updates.append('output_format = ?')
                params.append(data['output_format'] if data['output_format'] else None)
            
            if 'message_send_interval_seconds' in data:
                updates.append('message_send_interval_seconds = ?')
                params.append(float(data['message_send_interval_seconds']) if data['message_send_interval_seconds'] else None)
            
            if 'filter_config' in data:
                updates.append('filter_config = ?')
                params.append(json.dumps(data['filter_config']) if data['filter_config'] else None)
            
            if 'sort_config' in data:
                updates.append('sort_config = ?')
                params.append(json.dumps(data['sort_config']) if data['sort_config'] else None)
            
            if 'message_send_interval_seconds' in data:
                updates.append('message_send_interval_seconds = ?')
                params.append(data['message_send_interval_seconds'])
            
            if not updates:
                return True  # Nothing to update
            
            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(feed_id)
            
            query = f'UPDATE feed_subscriptions SET {", ".join(updates)} WHERE id = ?'
            cursor.execute(query, params)
            conn.commit()
            
            return cursor.rowcount > 0
        except Exception as e:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
    
    def _delete_feed_subscription(self, feed_id):
        """Delete a feed subscription"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM feed_subscriptions WHERE id = ?', (feed_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
    
    def _get_feed_activity(self, feed_id, limit=50):
        """Get activity log for a feed"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM feed_activity
                WHERE feed_id = ?
                ORDER BY processed_at DESC
                LIMIT ?
            ''', (feed_id, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error getting feed activity: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    def _get_feed_errors(self, feed_id, limit=20):
        """Get error history for a feed"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM feed_errors
                WHERE feed_id = ?
                ORDER BY occurred_at DESC
                LIMIT ?
            ''', (feed_id, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error getting feed errors: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    def _get_feed_statistics(self):
        """Get aggregate feed statistics"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            stats = {}
            
            # Total subscriptions
            cursor.execute('SELECT COUNT(*) FROM feed_subscriptions')
            stats['total_subscriptions'] = cursor.fetchone()[0]
            
            # Enabled subscriptions
            cursor.execute('SELECT COUNT(*) FROM feed_subscriptions WHERE enabled = 1')
            stats['enabled_subscriptions'] = cursor.fetchone()[0]
            
            # Items processed in last 24h
            cursor.execute('''
                SELECT COUNT(*) FROM feed_activity
                WHERE processed_at > datetime('now', '-24 hours')
            ''')
            stats['items_24h'] = cursor.fetchone()[0]
            
            # Items processed in last 7d
            cursor.execute('''
                SELECT COUNT(*) FROM feed_activity
                WHERE processed_at > datetime('now', '-7 days')
            ''')
            stats['items_7d'] = cursor.fetchone()[0]
            
            # Error count
            cursor.execute('''
                SELECT COUNT(*) FROM feed_errors
                WHERE resolved_at IS NULL
            ''')
            stats['active_errors'] = cursor.fetchone()[0]
            
            # Most active channels
            cursor.execute('''
                SELECT channel_name, COUNT(*) as feed_count
                FROM feed_subscriptions
                WHERE enabled = 1
                GROUP BY channel_name
                ORDER BY feed_count DESC
                LIMIT 10
            ''')
            stats['top_channels'] = [{'channel': row[0], 'count': row[1]} for row in cursor.fetchall()]
            
            return stats
        except Exception as e:
            self.logger.error(f"Error getting feed statistics: {e}")
            return {'error': str(e)}
        finally:
            if conn:
                conn.close()
    
    def _get_feeds_by_channel(self, channel_idx):
        """Get all feeds for a specific channel index"""
        # First get channel name from index
        # This would require channel_manager access
        # For now, return empty list
        return []
    
    def _get_channels(self):
        """Get all configured channels from database plus additional decode-only channels"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT channel_idx, channel_name, channel_type, channel_key_hex, last_updated
                FROM channels
                ORDER BY channel_idx
            ''')

            rows = cursor.fetchall()
            channels = []
            existing_names = set()
            
            for row in rows:
                name = row['channel_name']
                channels.append({
                    'channel_idx': row['channel_idx'],
                    'index': row['channel_idx'],  # Alias for compatibility
                    'name': name,
                    'channel_name': name,  # Alias for compatibility
                    'type': row['channel_type'] or 'hashtag',
                    'key_hex': row['channel_key_hex'],
                    'last_updated': row['last_updated']
                })
                # Track names for deduplication (normalize to lowercase with #)
                normalized = name.lower() if name.startswith('#') else f'#{name.lower()}'
                existing_names.add(normalized)

            # Add additional decode-only hashtag channels from config
            additional_channels = self._get_additional_decode_channels()
            for channel_name in additional_channels:
                # Normalize name
                normalized = channel_name.lower() if channel_name.startswith('#') else f'#{channel_name.lower()}'
                if normalized not in existing_names:
                    channels.append({
                        'channel_idx': None,  # Not a real radio channel
                        'index': None,
                        'name': normalized,
                        'channel_name': normalized,
                        'type': 'hashtag',
                        'key_hex': None,  # Key will be derived client-side
                        'last_updated': None,
                        'decode_only': True  # Flag to indicate this is decode-only
                    })
                    existing_names.add(normalized)

            return channels
        except Exception as e:
            self.logger.error(f"Error getting channels: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    def _get_additional_decode_channels(self):
        """Get additional hashtag channels to decode from config"""
        channels = set()  # Use set for automatic deduplication
        
        try:
            # 1. Get channels from decode_hashtag_channels in [Web_Viewer]
            if self.config and self.config.has_option('Web_Viewer', 'decode_hashtag_channels'):
                channels_str = self.config.get('Web_Viewer', 'decode_hashtag_channels', fallback='')
                if channels_str:
                    for c in channels_str.split(','):
                        c = c.strip().lower()
                        if c:
                            # Remove # prefix if present for normalization
                            if c.startswith('#'):
                                c = c[1:]
                            channels.add(c)
            
            # 2. Import channels from [Channels_List] section
            if self.config and self.config.has_section('Channels_List'):
                for key in self.config.options('Channels_List'):
                    # Handle categorized channels like "sports.sounders" -> "sounders"
                    if '.' in key:
                        channel_name = key.split('.')[-1]  # Get part after last dot
                    else:
                        channel_name = key
                    
                    channel_name = channel_name.strip().lower()
                    if channel_name:
                        channels.add(channel_name)
        except Exception as e:
            self.logger.error(f"Error reading decode channels config: {e}")
        
        return list(channels)
    
    def _get_channel_number(self, channel_name):
        """Get channel number from channel name"""
        # This would use channel_manager
        # For now, return None
        return None
    
    def _get_lowest_available_channel_index(self):
        """Get the lowest available channel index (0 to max_channels-1)"""
        try:
            channels = self._get_channels()
            used_indices = {c['channel_idx'] for c in channels}
            
            # Get max_channels from config (default 40)
            max_channels = self.config.getint('Bot', 'max_channels', fallback=40)
            
            # Find the lowest available index
            for i in range(max_channels):
                if i not in used_indices:
                    return i
            
            # All channels are used
            return None
        except Exception as e:
            self.logger.error(f"Error getting lowest available channel index: {e}")
            return None
    
    def _get_channel_statistics(self):
        """Get channel statistics"""
        import sqlite3
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get feed count per channel
            cursor.execute('''
                SELECT channel_name, COUNT(*) as feed_count
                FROM feed_subscriptions
                WHERE enabled = 1
                GROUP BY channel_name
            ''')
            
            channel_feeds = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Get max_channels from config (default 40)
            max_channels = self.config.getint('Bot', 'max_channels', fallback=40)
            
            return {
                'channels_with_feeds': len(channel_feeds),
                'channel_feed_counts': channel_feeds,
                'max_channels': max_channels
            }
        except Exception as e:
            self.logger.error(f"Error getting channel statistics: {e}")
            return {'error': str(e)}
        finally:
            if conn:
                conn.close()
    
    def _preview_feed_items(self, feed_url: str, feed_type: str, output_format: str, api_config: dict = None, filter_config: dict = None, sort_config: dict = None) -> List[Dict[str, Any]]:
        """Preview feed items with custom output format (standalone, doesn't require bot)"""
        import feedparser
        import requests
        import html
        import re
        from datetime import datetime, timezone
        
        try:
            items = []
            
            if feed_type == 'rss':
                # Fetch RSS feed
                response = requests.get(feed_url, timeout=30, headers={'User-Agent': 'MeshCoreBot/1.0 FeedManager'})
                response.raise_for_status()
                parsed = feedparser.parse(response.text)
                
                # Get items (we'll filter and limit later)
                for entry in parsed.entries[:20]:  # Fetch more items to account for filtering
                    # Parse published date
                    published = None
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        try:
                            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                        except Exception:
                            pass
                    
                    items.append({
                        'title': entry.get('title', 'Untitled'),
                        'description': entry.get('description', ''),
                        'link': entry.get('link', ''),
                        'published': published
                    })
            
            elif feed_type == 'api':
                # Fetch API feed
                method = api_config.get('method', 'GET').upper()
                headers = api_config.get('headers', {})
                params = api_config.get('params', {})
                body = api_config.get('body')
                parser_config = api_config.get('response_parser', {})
                
                if method == 'POST':
                    response = requests.post(feed_url, headers=headers, params=params, json=body, timeout=30)
                else:
                    response = requests.get(feed_url, headers=headers, params=params, timeout=30)
                response.raise_for_status()
                
                # Try to parse JSON, handle cases where response might be a string
                try:
                    data = response.json()
                except ValueError:
                    # If JSON parsing fails, try to get text and see if it's an error message
                    text = response.text
                    raise Exception(f"API returned non-JSON response: {text[:200]}")
                
                # Check if response is an error message (string)
                if isinstance(data, str):
                    raise Exception(f"API returned error message: {data[:200]}")
                
                # Ensure data is a dict or list
                if not isinstance(data, (dict, list)):
                    raise Exception(f"API response is not a valid JSON object or array: {type(data).__name__} - {str(data)[:200]}")
                
                # Extract items using parser config
                items_path = parser_config.get('items_path', '')
                if items_path:
                    parts = items_path.split('.')
                    items_data = data
                    for part in parts:
                        if isinstance(items_data, dict):
                            items_data = items_data.get(part, [])
                        else:
                            raise Exception(f"Cannot navigate path '{items_path}': expected dict at '{part}', got {type(items_data).__name__}")
                else:
                    # If no items_path, data should be a list or we wrap it
                    if isinstance(data, list):
                        items_data = data
                    elif isinstance(data, dict):
                        # If it's a dict, try to find common array fields
                        items_data = data.get('items', data.get('data', data.get('results', [data])))
                    else:
                        items_data = [data]
                
                # Ensure items_data is a list
                if not isinstance(items_data, list):
                    items_data = [items_data]
                
                # Get items (we'll filter and limit later)
                id_field = parser_config.get('id_field', 'id')
                title_field = parser_config.get('title_field', 'title')
                description_field = parser_config.get('description_field', 'description')
                timestamp_field = parser_config.get('timestamp_field', 'created_at')
                
                # Helper function to get nested values
                def get_nested_value(data, path, default=''):
                    if not path or not data:
                        return default
                    parts = path.split('.')
                    value = data
                    for part in parts:
                        if isinstance(value, dict):
                            value = value.get(part)
                        elif isinstance(value, list):
                            try:
                                idx = int(part)
                                if 0 <= idx < len(value):
                                    value = value[idx]
                                else:
                                    return default
                            except (ValueError, TypeError):
                                return default
                        else:
                            return default
                        if value is None:
                            return default
                    return value if value is not None else default
                
                for item_data in items_data[:20]:  # Fetch more items to account for filtering
                    # Ensure item_data is a dict
                    if not isinstance(item_data, dict):
                        # If it's not a dict, try to convert or skip
                        if isinstance(item_data, str):
                            # If it's a string, create a simple dict
                            item_data = {'title': item_data, 'description': item_data}
                        else:
                            # Try to convert to dict or skip
                            continue
                    
                    # Parse timestamp if available - support nested paths
                    published = None
                    if timestamp_field:
                        ts_value = get_nested_value(item_data, timestamp_field)
                        if ts_value:
                            try:
                                if isinstance(ts_value, (int, float)):
                                    published = datetime.fromtimestamp(ts_value, tz=timezone.utc)
                                elif isinstance(ts_value, str):
                                    # Try Microsoft date format first
                                    if ts_value.startswith('/Date('):
                                        published = self._parse_microsoft_date(ts_value)
                                    else:
                                        # Try ISO format
                                        try:
                                            published = datetime.fromisoformat(ts_value.replace('Z', '+00:00'))
                                        except ValueError:
                                            # Try common formats
                                            for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                                                try:
                                                    published = datetime.strptime(ts_value, fmt)
                                                    if published.tzinfo is None:
                                                        published = published.replace(tzinfo=timezone.utc)
                                                    break
                                                except ValueError:
                                                    continue
                            except Exception:
                                pass
                    
                    # Get description - support nested paths
                    description = ''
                    if description_field:
                        desc_value = get_nested_value(item_data, description_field)
                        if desc_value:
                            description = str(desc_value)
                    
                    items.append({
                        'title': get_nested_value(item_data, title_field, 'Untitled'),
                        'description': description,
                        'link': item_data.get('link', '') if isinstance(item_data, dict) else '',
                        'published': published,
                        'raw': item_data  # Store raw data for format string access
                    })
            
            # Apply sorting if configured
            if sort_config:
                items = self._sort_items_preview(items, sort_config)
            
            # Apply filter if configured
            if filter_config:
                items = [item for item in items if self._should_include_item(item, filter_config)]
            
            # Limit to first 3 items after filtering
            items = items[:3]
            
            # Format items using output format
            formatted_items = []
            for item in items:
                formatted = self._format_feed_item(item, output_format, feed_name='')
                formatted_items.append({
                    'original': item,
                    'formatted': formatted
                })
            
            return formatted_items
            
        except Exception as e:
            self.logger.error(f"Error previewing feed: {e}")
            raise
    
    def _should_include_item(self, item: Dict[str, Any], filter_config: dict) -> bool:
        """Check if an item should be included based on filter configuration (standalone version for preview)"""
        import json
        import re
        
        if not filter_config:
            return True
        
        try:
            filter_config_dict = json.loads(filter_config) if isinstance(filter_config, str) else filter_config
        except (json.JSONDecodeError, TypeError):
            return True
        
        conditions = filter_config_dict.get('conditions', [])
        if not conditions:
            return True
        
        logic = filter_config_dict.get('logic', 'AND').upper()
        
        # Get raw data for field access
        raw_data = item.get('raw', {})
        
        # Helper to get nested values
        def get_nested_value(data, path, default=''):
            if not path or not data:
                return default
            parts = path.split('.')
            value = data
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list):
                    try:
                        idx = int(part)
                        if 0 <= idx < len(value):
                            value = value[idx]
                        else:
                            return default
                    except (ValueError, TypeError):
                        return default
                else:
                    return default
                if value is None:
                    return default
            return value if value is not None else default
        
        # Evaluate each condition
        results = []
        for condition in conditions:
            field_path = condition.get('field')
            operator = condition.get('operator', 'equals')
            
            if not field_path:
                continue
            
            # Get field value using nested access
            field_value = get_nested_value(raw_data, field_path, '')
            if not field_value and field_path.startswith('raw.'):
                field_value = get_nested_value(raw_data, field_path[4:], '')
            
            if not field_value:
                field_value = get_nested_value(item, field_path, '')
            
            # Convert to string for comparison
            field_value_str = str(field_value).lower() if field_value is not None else ''
            
            # Evaluate condition
            result = False
            if operator == 'equals':
                compare_value = str(condition.get('value', '')).lower()
                result = field_value_str == compare_value
            elif operator == 'not_equals':
                compare_value = str(condition.get('value', '')).lower()
                result = field_value_str != compare_value
            elif operator == 'in':
                values = [str(v).lower() for v in condition.get('values', [])]
                result = field_value_str in values
            elif operator == 'not_in':
                values = [str(v).lower() for v in condition.get('values', [])]
                result = field_value_str not in values
            elif operator == 'matches':
                pattern = condition.get('pattern', '')
                if pattern:
                    try:
                        result = bool(re.search(pattern, str(field_value), re.IGNORECASE))
                    except re.error:
                        result = False
            elif operator == 'not_matches':
                pattern = condition.get('pattern', '')
                if pattern:
                    try:
                        result = not bool(re.search(pattern, str(field_value), re.IGNORECASE))
                    except re.error:
                        result = True
            elif operator == 'contains':
                compare_value = str(condition.get('value', '')).lower()
                result = compare_value in field_value_str
            elif operator == 'not_contains':
                compare_value = str(condition.get('value', '')).lower()
                result = compare_value not in field_value_str
            else:
                result = True  # Default to allowing if operator is unknown
            
            results.append(result)
        
        # Apply logic (AND or OR)
        if logic == 'OR':
            return any(results)
        else:  # AND (default)
            return all(results)
    
    def _parse_microsoft_date(self, date_str: str) -> Optional[datetime]:
        """Parse Microsoft JSON date format: /Date(timestamp-offset)/"""
        import re
        from datetime import timezone
        
        if not date_str or not isinstance(date_str, str):
            return None
        
        # Match /Date(timestamp-offset)/ format
        match = re.match(r'/Date\((\d+)([+-]\d+)?\)/', date_str)
        if match:
            timestamp_ms = int(match.group(1))
            offset_str = match.group(2) if match.group(2) else '+0000'
            
            # Convert milliseconds to seconds
            timestamp = timestamp_ms / 1000.0
            
            # Parse offset (format: +0800 or -0800)
            try:
                offset_hours = int(offset_str[:3])
                offset_mins = int(offset_str[3:5])
                offset_seconds = (offset_hours * 3600) + (offset_mins * 60)
                if offset_str[0] == '-':
                    offset_seconds = -offset_seconds
                
                # Create timezone-aware datetime
                tz = timezone.utc
                if offset_seconds != 0:
                    from datetime import timedelta
                    tz = timezone(timedelta(seconds=offset_seconds))
                
                return datetime.fromtimestamp(timestamp, tz=tz)
            except (ValueError, IndexError):
                # Fallback to UTC if offset parsing fails
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        
        return None
    
    def _sort_items_preview(self, items: List[Dict[str, Any]], sort_config: dict) -> List[Dict[str, Any]]:
        """Sort items based on sort configuration (standalone version for preview)"""
        if not sort_config or not items:
            return items
        
        field_path = sort_config.get('field')
        order = sort_config.get('order', 'desc').lower()
        
        if not field_path:
            return items
        
        # Helper to get nested values
        def get_nested_value(data, path, default=''):
            if not path or not data:
                return default
            parts = path.split('.')
            value = data
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list):
                    try:
                        idx = int(part)
                        if 0 <= idx < len(value):
                            value = value[idx]
                        else:
                            return default
                    except (ValueError, TypeError):
                        return default
                else:
                    return default
                if value is None:
                    return default
            return value if value is not None else default
        
        def get_sort_value(item):
            """Get the sort value for an item"""
            # Try raw data first
            raw_data = item.get('raw', {})
            value = get_nested_value(raw_data, field_path, '')
            
            if not value and field_path.startswith('raw.'):
                value = get_nested_value(raw_data, field_path[4:], '')
            
            if not value:
                value = get_nested_value(item, field_path, '')
            
            # Handle Microsoft date format
            if isinstance(value, str) and value.startswith('/Date('):
                dt = self._parse_microsoft_date(value)
                if dt:
                    return dt.timestamp()
            
            # Handle datetime objects
            if isinstance(value, datetime):
                return value.timestamp()
            
            # Handle numeric values
            if isinstance(value, (int, float)):
                return float(value)
            
            # Handle string timestamps
            if isinstance(value, str):
                # Try to parse as ISO format
                try:
                    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    return dt.timestamp()
                except ValueError:
                    pass
                
                # Try common date formats
                for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                    try:
                        dt = datetime.strptime(value, fmt)
                        return dt.timestamp()
                    except ValueError:
                        continue
            
            # For strings, use lexicographic comparison
            return str(value)
        
        # Sort items
        try:
            sorted_items = sorted(items, key=get_sort_value, reverse=(order == 'desc'))
            return sorted_items
        except Exception as e:
            self.logger.warning(f"Error sorting items in preview: {e}")
            return items
    
    def _format_feed_item(self, item: Dict[str, Any], format_str: str, feed_name: str = '') -> str:
        """Format a feed item using the output format (standalone version)"""
        import html
        import re
        from datetime import datetime, timezone
        
        # Extract field values
        title = item.get('title', 'Untitled')
        body = item.get('description', '') or item.get('body', '')
        
        # Clean HTML from body if present
        if body:
            body = html.unescape(body)
            # Convert line break tags to newlines before stripping other HTML
            # Handle <br>, <br/>, <br />, <BR>, etc.
            body = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
            # Convert paragraph tags to newlines (with spacing)
            body = re.sub(r'</p>', '\n\n', body, flags=re.IGNORECASE)
            body = re.sub(r'<p[^>]*>', '', body, flags=re.IGNORECASE)
            # Remove remaining HTML tags
            body = re.sub(r'<[^>]+>', '', body)
            # Clean up whitespace (preserve intentional line breaks)
            # Replace multiple newlines with double newline, then normalize spaces within lines
            body = re.sub(r'\n\s*\n\s*\n+', '\n\n', body)  # Multiple newlines -> double newline
            lines = body.split('\n')
            body = '\n'.join(' '.join(line.split()) for line in lines)  # Normalize spaces per line
            body = body.strip()
        
        link = item.get('link', '')
        published = item.get('published')
        
        # Format timestamp
        date_str = ""
        if published:
            try:
                if published.tzinfo:
                    now = datetime.now(timezone.utc)
                else:
                    now = datetime.now()
                
                diff = now - published
                minutes = int(diff.total_seconds() / 60)
                
                if minutes < 1:
                    date_str = "now"
                elif minutes < 60:
                    date_str = f"{minutes}m ago"
                elif minutes < 1440:
                    hours = minutes // 60
                    mins = minutes % 60
                    date_str = f"{hours}h {mins}m ago"
                else:
                    days = minutes // 1440
                    date_str = f"{days}d ago"
            except Exception:
                pass
        
        # Choose emoji
        emoji = "📢"
        feed_name_lower = feed_name.lower()
        if 'emergency' in feed_name_lower or 'alert' in feed_name_lower:
            emoji = "🚨"
        elif 'warning' in feed_name_lower:
            emoji = "⚠️"
        elif 'info' in feed_name_lower or 'news' in feed_name_lower:
            emoji = "ℹ️"
        
        # Build replacements
        replacements = {
            'title': title,
            'body': body,
            'date': date_str,
            'link': link,
            'emoji': emoji
        }
        
        # Get raw API data if available (for preview, we don't have raw data, so this will be empty)
        raw_data = item.get('raw', {})
        
        # Helper to get nested values
        def get_nested_value(data, path, default=''):
            if not path or not data:
                return default
            parts = path.split('.')
            value = data
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list):
                    try:
                        idx = int(part)
                        if 0 <= idx < len(value):
                            value = value[idx]
                        else:
                            return default
                    except (ValueError, TypeError):
                        return default
                else:
                    return default
                if value is None:
                    return default
            return value if value is not None else default
        
        # Apply shortening, parsing, and conditional functions
        def apply_shortening(text: str, function: str) -> str:
            if not text:
                return ""
            
            if function.startswith('truncate:'):
                try:
                    max_len = int(function.split(':', 1)[1])
                    if len(text) <= max_len:
                        return text
                    return text[:max_len] + "..."
                except (ValueError, IndexError):
                    return text
            elif function.startswith('word_wrap:'):
                try:
                    max_len = int(function.split(':', 1)[1])
                    if len(text) <= max_len:
                        return text
                    truncated = text[:max_len]
                    last_space = truncated.rfind(' ')
                    if last_space > max_len * 0.7:
                        return truncated[:last_space] + "..."
                    return truncated + "..."
                except (ValueError, IndexError):
                    return text
            elif function.startswith('first_words:'):
                try:
                    num_words = int(function.split(':', 1)[1])
                    words = text.split()
                    if len(words) <= num_words:
                        return text
                    return ' '.join(words[:num_words]) + "..."
                except (ValueError, IndexError):
                    return text
            elif function.startswith('regex:'):
                try:
                    # Parse regex pattern and optional group number
                    # Format: regex:pattern:group or regex:pattern
                    # Need to handle patterns that contain colons, so split from the right
                    remaining = function[6:]  # Skip 'regex:' prefix
                    
                    # Try to find the last colon that's followed by a number (the group number)
                    # Look for pattern like :N at the end
                    last_colon_idx = remaining.rfind(':')
                    pattern = remaining
                    group_num = None
                    
                    if last_colon_idx > 0:
                        # Check if what's after the last colon is a number
                        potential_group = remaining[last_colon_idx + 1:]
                        if potential_group.isdigit():
                            pattern = remaining[:last_colon_idx]
                            group_num = int(potential_group)
                    
                    if not pattern:
                        return text
                    
                    # Apply regex
                    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                    if match:
                        if group_num is not None:
                            # Use specified group (0 = whole match, 1 = first group, etc.)
                            if 0 <= group_num <= len(match.groups()):
                                return match.group(group_num) if group_num > 0 else match.group(0)
                        else:
                            # Use first capture group if available, otherwise whole match
                            if match.groups():
                                return match.group(1)
                            else:
                                return match.group(0)
                    return ""  # No match found
                except (ValueError, IndexError, re.error) as e:
                    # Silently fail on regex errors in preview
                    return text
            elif function.startswith('if_regex:'):
                try:
                    # Parse: if_regex:pattern:then:else
                    # Split by ':' but need to handle regex patterns that contain ':'
                    parts = function[9:].split(':', 2)  # Skip 'if_regex:' prefix, split into [pattern, then, else]
                    if len(parts) < 3:
                        return text
                    
                    pattern = parts[0]
                    then_value = parts[1]
                    else_value = parts[2]
                    
                    if not pattern:
                        return text
                    
                    # Check if pattern matches
                    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                    if match:
                        return then_value
                    else:
                        return else_value
                except (ValueError, IndexError, re.error) as e:
                    # Silently fail on regex errors in preview
                    return text
            elif function.startswith('switch:'):
                try:
                    # Parse: switch:value1:result1:value2:result2:...:default
                    # Example: switch:highest:🔴:high:🟠:medium:🟡:low:⚪:⚪
                    parts = function[7:].split(':')  # Skip 'switch:' prefix
                    if len(parts) < 2:
                        return text
                    
                    # Pairs of value:result, last one is default
                    text_lower = text.lower().strip()
                    for i in range(0, len(parts) - 1, 2):
                        if i + 1 < len(parts):
                            value = parts[i].lower()
                            result = parts[i + 1]
                            if text_lower == value:
                                return result
                    
                    # Return last part as default if no match
                    return parts[-1] if parts else text
                except (ValueError, IndexError) as e:
                    # Silently fail on switch errors in preview
                    return text
            elif function.startswith('regex_cond:'):
                try:
                    # Parse: regex_cond:extract_pattern:check_pattern:then:group
                    parts = function[11:].split(':', 3)  # Skip 'regex_cond:' prefix
                    if len(parts) < 4:
                        return text
                    
                    extract_pattern = parts[0]
                    check_pattern = parts[1]
                    then_value = parts[2]
                    else_group = int(parts[3]) if parts[3].isdigit() else 1
                    
                    if not extract_pattern:
                        return text
                    
                    # Extract using extract_pattern
                    match = re.search(extract_pattern, text, re.IGNORECASE | re.DOTALL)
                    if match:
                        # Get the captured group
                        if match.groups():
                            extracted = match.group(else_group) if else_group <= len(match.groups()) else match.group(1)
                            # Strip whitespace from extracted text
                            extracted = extracted.strip()
                        else:
                            extracted = match.group(0).strip()
                        
                        # Check if extracted text matches check_pattern (exact match or contains)
                        if check_pattern:
                            # Try exact match first, then substring match
                            if extracted.lower() == check_pattern.lower() or re.search(check_pattern, extracted, re.IGNORECASE):
                                return then_value
                        
                        return extracted
                    return ""  # No match found
                except (ValueError, IndexError, re.error) as e:
                    # Silently fail on regex errors in preview
                    return text
            return text
        
        # Process format string
        def replace_placeholder(match):
            content = match.group(1)
            if '|' in content:
                field_name, function = content.split('|', 1)
                field_name = field_name.strip()
                function = function.strip()
                
                # Check if it's a raw field access
                if field_name.startswith('raw.'):
                    value = str(get_nested_value(raw_data, field_name[4:], ''))
                else:
                    value = replacements.get(field_name, '')
                
                return apply_shortening(value, function)
            else:
                field_name = content.strip()
                
                # Check if it's a raw field access
                if field_name.startswith('raw.'):
                    value = get_nested_value(raw_data, field_name[4:], '')
                    if value is None:
                        return ''
                    elif isinstance(value, (dict, list)):
                        try:
                            import json
                            return json.dumps(value)
                        except Exception:
                            return str(value)
                    else:
                        return str(value)
                else:
                    return replacements.get(field_name, '')
        
        message = re.sub(r'\{([^}]+)\}', replace_placeholder, format_str)
        
        # Final truncation (130 char limit)
        max_length = 130
        if len(message) > max_length:
            lines = message.split('\n')
            if len(lines) > 1:
                total_length = sum(len(line) + 1 for line in lines[:-1])
                remaining = max_length - total_length - 3
                if remaining > 20:
                    lines[-1] = lines[-1][:remaining] + "..."
                    message = '\n'.join(lines)
                else:
                    message = message[:max_length - 3] + "..."
            else:
                message = message[:max_length - 3] + "..."
        
        return message
    
    def _get_bot_uptime(self):
        """Get bot uptime in seconds from database"""
        try:
            # Get start time from database metadata
            start_time = self.db_manager.get_bot_start_time()
            if start_time:
                return int(time.time() - start_time)
            else:
                # Fallback: try to get earliest message timestamp
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                # Try to get earliest message timestamp as fallback
                cursor.execute("""
                    SELECT MIN(timestamp) FROM message_stats 
                    WHERE timestamp IS NOT NULL
                """)
                result = cursor.fetchone()
                if result and result[0]:
                    return int(time.time() - result[0])
                
                return 0
        except Exception as e:
            self.logger.debug(f"Could not get bot start time from database: {e}")
            return 0
    
    def _add_channel_for_web(self, channel_idx, channel_name, channel_key_hex=None):
        """
        Add a channel by queuing it in the database for the bot to process
        
        Args:
            channel_idx: Channel index (0-39)
            channel_name: Channel name (with or without # prefix)
            channel_key_hex: Optional hex key for custom channels (32 chars)
            
        Returns:
            dict with 'success' and optional 'error' key
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Insert operation into queue
            cursor.execute('''
                INSERT INTO channel_operations 
                (operation_type, channel_idx, channel_name, channel_key_hex, status)
                VALUES (?, ?, ?, ?, 'pending')
            ''', ('add', channel_idx, channel_name, channel_key_hex))
            
            operation_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            self.logger.info(f"Queued channel add operation: {channel_name} at index {channel_idx} (operation_id: {operation_id})")
            
            # Return immediately with operation_id - let frontend poll for status
            return {
                'success': True,
                'pending': True,
                'operation_id': operation_id,
                'message': 'Channel operation queued successfully'
            }
                
        except Exception as e:
            self.logger.error(f"Error in _add_channel_for_web: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _remove_channel_for_web(self, channel_idx):
        """
        Remove a channel by queuing it in the database for the bot to process
        
        Args:
            channel_idx: Channel index to remove
            
        Returns:
            dict with 'success' and optional 'error' key
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Insert operation into queue
            cursor.execute('''
                INSERT INTO channel_operations 
                (operation_type, channel_idx, status)
                VALUES (?, ?, 'pending')
            ''', ('remove', channel_idx))
            
            operation_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            self.logger.info(f"Queued channel remove operation: index {channel_idx} (operation_id: {operation_id})")
            
            # Return immediately with operation_id - let frontend poll for status
            return {
                'success': True,
                'pending': True,
                'operation_id': operation_id,
                'message': 'Channel operation queued successfully'
            }
                
        except Exception as e:
            self.logger.error(f"Error in _remove_channel_for_web: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _decode_path_hex(self, path_hex: str) -> List[Dict[str, Any]]:
        """
        Decode hex path string to repeater names using the same sophisticated logic as path command.
        Returns a list of dictionaries with node_id and repeater info.
        """
        import re
        import math
        from datetime import datetime
        
        # Parse the path input - handle various formats
        path_input_clean = path_hex.replace(' ', '').replace(',', '').replace(':', '')
        if re.match(r'^[0-9a-fA-F]{4,}$', path_input_clean):
            # Continuous hex string - split into pairs
            hex_matches = [path_input_clean[i:i+2] for i in range(0, len(path_input_clean), 2)]
        else:
            # Space/comma-separated format
            path_input = path_hex.replace(',', ' ').replace(':', ' ')
            hex_pattern = r'[0-9a-fA-F]{2}'
            hex_matches = re.findall(hex_pattern, path_input)
        
        if not hex_matches:
            return []
        
        # Convert to uppercase for consistency
        node_ids = [match.upper() for match in hex_matches]
        
        # Load Path_Command config values (same as path command)
        geographic_guessing_enabled = False
        bot_latitude = None
        bot_longitude = None
        
        try:
            if self.config.has_section('Bot'):
                lat = self.config.getfloat('Bot', 'bot_latitude', fallback=None)
                lon = self.config.getfloat('Bot', 'bot_longitude', fallback=None)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    bot_latitude = lat
                    bot_longitude = lon
                    geographic_guessing_enabled = True
        except Exception:
            pass
        
        proximity_method = self.config.get('Path_Command', 'proximity_method', fallback='simple')
        max_proximity_range = self.config.getfloat('Path_Command', 'max_proximity_range', fallback=200.0)
        max_repeater_age_days = self.config.getint('Path_Command', 'max_repeater_age_days', fallback=14)
        recency_weight = self.config.getfloat('Path_Command', 'recency_weight', fallback=0.4)
        recency_weight = max(0.0, min(1.0, recency_weight))
        proximity_weight = 1.0 - recency_weight
        recency_decay_half_life_hours = self.config.getfloat('Path_Command', 'recency_decay_half_life_hours', fallback=12.0)
        
        # Check for preset first, then apply individual settings (preset can be overridden)
        preset = self.config.get('Path_Command', 'path_selection_preset', fallback='balanced').lower()
        
        # Apply preset defaults, then individual settings override
        if preset == 'geographic':
            preset_graph_confidence_threshold = 0.5
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.5
            preset_final_hop_weight = 0.4
        elif preset == 'graph':
            preset_graph_confidence_threshold = 0.9
            preset_distance_threshold = 50.0
            preset_distance_penalty = 0.2
            preset_final_hop_weight = 0.15
        else:  # 'balanced' (default)
            preset_graph_confidence_threshold = 0.7
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.3
            preset_final_hop_weight = 0.25
        
        graph_based_validation = self.config.getboolean('Path_Command', 'graph_based_validation', fallback=True)
        min_edge_observations = self.config.getint('Path_Command', 'min_edge_observations', fallback=3)
        graph_use_bidirectional = self.config.getboolean('Path_Command', 'graph_use_bidirectional', fallback=True)
        graph_use_hop_position = self.config.getboolean('Path_Command', 'graph_use_hop_position', fallback=True)
        graph_multi_hop_enabled = self.config.getboolean('Path_Command', 'graph_multi_hop_enabled', fallback=True)
        graph_multi_hop_max_hops = self.config.getint('Path_Command', 'graph_multi_hop_max_hops', fallback=2)
        graph_geographic_combined = self.config.getboolean('Path_Command', 'graph_geographic_combined', fallback=False)
        graph_geographic_weight = self.config.getfloat('Path_Command', 'graph_geographic_weight', fallback=0.7)
        graph_geographic_weight = max(0.0, min(1.0, graph_geographic_weight))
        graph_confidence_override_threshold = self.config.getfloat('Path_Command', 'graph_confidence_override_threshold', fallback=preset_graph_confidence_threshold)
        graph_confidence_override_threshold = max(0.0, min(1.0, graph_confidence_override_threshold))
        graph_distance_penalty_enabled = self.config.getboolean('Path_Command', 'graph_distance_penalty_enabled', fallback=True)
        graph_max_reasonable_hop_distance_km = self.config.getfloat('Path_Command', 'graph_max_reasonable_hop_distance_km', fallback=preset_distance_threshold)
        graph_distance_penalty_strength = self.config.getfloat('Path_Command', 'graph_distance_penalty_strength', fallback=preset_distance_penalty)
        graph_distance_penalty_strength = max(0.0, min(1.0, graph_distance_penalty_strength))
        graph_zero_hop_bonus = self.config.getfloat('Path_Command', 'graph_zero_hop_bonus', fallback=0.4)
        graph_zero_hop_bonus = max(0.0, min(1.0, graph_zero_hop_bonus))
        graph_prefer_stored_keys = self.config.getboolean('Path_Command', 'graph_prefer_stored_keys', fallback=True)
        graph_final_hop_proximity_enabled = self.config.getboolean('Path_Command', 'graph_final_hop_proximity_enabled', fallback=True)
        graph_final_hop_proximity_weight = self.config.getfloat('Path_Command', 'graph_final_hop_proximity_weight', fallback=preset_final_hop_weight)
        graph_final_hop_proximity_weight = max(0.0, min(1.0, graph_final_hop_proximity_weight))
        graph_final_hop_max_distance = self.config.getfloat('Path_Command', 'graph_final_hop_max_distance', fallback=0.0)
        graph_final_hop_proximity_normalization_km = self.config.getfloat('Path_Command', 'graph_final_hop_proximity_normalization_km', fallback=200.0)  # Long LoRa range
        graph_final_hop_very_close_threshold_km = self.config.getfloat('Path_Command', 'graph_final_hop_very_close_threshold_km', fallback=10.0)
        graph_final_hop_close_threshold_km = self.config.getfloat('Path_Command', 'graph_final_hop_close_threshold_km', fallback=30.0)  # Typical LoRa range
        graph_final_hop_max_proximity_weight = self.config.getfloat('Path_Command', 'graph_final_hop_max_proximity_weight', fallback=0.6)
        graph_final_hop_max_proximity_weight = max(0.0, min(1.0, graph_final_hop_max_proximity_weight))
        graph_path_validation_max_bonus = self.config.getfloat('Path_Command', 'graph_path_validation_max_bonus', fallback=0.3)
        graph_path_validation_max_bonus = max(0.0, min(1.0, graph_path_validation_max_bonus))
        graph_path_validation_obs_divisor = self.config.getfloat('Path_Command', 'graph_path_validation_obs_divisor', fallback=50.0)
        star_bias_multiplier = self.config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5)
        star_bias_multiplier = max(1.0, star_bias_multiplier)
        
        # Use calculate_distance from utils (already imported)
        
        # Helper: calculate recency scores
        def calculate_recency_weighted_scores(repeaters):
            scored_repeaters = []
            now = datetime.now()
            
            for repeater in repeaters:
                most_recent_time = None
                for field in ['last_heard', 'last_advert_timestamp', 'last_seen']:
                    value = repeater.get(field)
                    if value:
                        try:
                            if isinstance(value, str):
                                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            else:
                                dt = value
                            if most_recent_time is None or dt > most_recent_time:
                                most_recent_time = dt
                        except:
                            pass
                
                if most_recent_time is None:
                    recency_score = 0.1
                else:
                    hours_ago = (now - most_recent_time).total_seconds() / 3600.0
                    recency_score = math.exp(-hours_ago / recency_decay_half_life_hours)
                    recency_score = max(0.0, min(1.0, recency_score))
                
                scored_repeaters.append((repeater, recency_score))
            
            scored_repeaters.sort(key=lambda x: x[1], reverse=True)
            return scored_repeaters
        
        # Helper: graph-based selection with final hop proximity and path validation
        def select_repeater_by_graph(repeaters, node_id, path_context):
            if not graph_based_validation or not hasattr(self, 'mesh_graph') or not self.mesh_graph:
                return None, 0.0, None
            
            mesh_graph = self.mesh_graph
            
            try:
                current_index = path_context.index(node_id) if node_id in path_context else -1
            except:
                current_index = -1
            
            if current_index == -1:
                return None, 0.0, None
            
            prev_node_id = path_context[current_index - 1] if current_index > 0 else None
            next_node_id = path_context[current_index + 1] if current_index < len(path_context) - 1 else None
            
            best_repeater = None
            best_score = 0.0
            best_method = None
            
            for repeater in repeaters:
                candidate_prefix = repeater.get('public_key', '')[:2].lower() if repeater.get('public_key') else None
                candidate_public_key = repeater.get('public_key', '').lower() if repeater.get('public_key') else None
                if not candidate_prefix:
                    continue
                
                graph_score = mesh_graph.get_candidate_score(
                    candidate_prefix, prev_node_id, next_node_id, min_edge_observations,
                    hop_position=current_index if graph_use_hop_position else None,
                    use_bidirectional=graph_use_bidirectional,
                    use_hop_position=graph_use_hop_position
                )
                
                stored_key_bonus = 0.0
                if graph_prefer_stored_keys and candidate_public_key:
                    if prev_node_id:
                        prev_to_candidate_edge = mesh_graph.get_edge(prev_node_id, candidate_prefix)
                        if prev_to_candidate_edge:
                            stored_to_key = prev_to_candidate_edge.get('to_public_key', '').lower() if prev_to_candidate_edge.get('to_public_key') else None
                            if stored_to_key and stored_to_key == candidate_public_key:
                                stored_key_bonus = max(stored_key_bonus, 0.4)
                    
                    if next_node_id:
                        candidate_to_next_edge = mesh_graph.get_edge(candidate_prefix, next_node_id)
                        if candidate_to_next_edge:
                            stored_from_key = candidate_to_next_edge.get('from_public_key', '').lower() if candidate_to_next_edge.get('from_public_key') else None
                            if stored_from_key and stored_from_key == candidate_public_key:
                                stored_key_bonus = max(stored_key_bonus, 0.4)
                
                # Zero-hop bonus: If this repeater has been heard directly by the bot (zero-hop advert),
                # it's strong evidence it's close and should be preferred, even for intermediate hops
                zero_hop_bonus = 0.0
                hop_count = repeater.get('hop_count')
                if hop_count is not None and hop_count == 0:
                    # This repeater has been heard directly - strong evidence it's close to bot
                    zero_hop_bonus = graph_zero_hop_bonus
                
                graph_score_with_bonus = min(1.0, graph_score + stored_key_bonus + zero_hop_bonus)
                
                multi_hop_score = 0.0
                if graph_multi_hop_enabled and graph_score_with_bonus < 0.6 and prev_node_id and next_node_id:
                    intermediate_candidates = mesh_graph.find_intermediate_nodes(
                        prev_node_id, next_node_id, min_edge_observations,
                        max_hops=graph_multi_hop_max_hops
                    )
                    for intermediate_prefix, intermediate_score in intermediate_candidates:
                        if intermediate_prefix == candidate_prefix:
                            multi_hop_score = intermediate_score
                            break
                
                candidate_score = max(graph_score_with_bonus, multi_hop_score)
                method = 'graph_multihop' if multi_hop_score > graph_score_with_bonus else 'graph'
                
                # Apply distance penalty for intermediate hops (prevents selecting very distant repeaters)
                # This is especially important when graph has strong evidence for long-distance links
                if graph_distance_penalty_enabled and next_node_id is not None:  # Not final hop
                    repeater_lat = repeater.get('latitude')
                    repeater_lon = repeater.get('longitude')
                    
                    if repeater_lat is not None and repeater_lon is not None:
                        max_distance = 0.0
                        
                        # Check distance from previous node to candidate (use stored edge distance if available)
                        if prev_node_id:
                            prev_to_candidate_edge = mesh_graph.get_edge(prev_node_id, candidate_prefix)
                            if prev_to_candidate_edge and prev_to_candidate_edge.get('geographic_distance'):
                                distance = prev_to_candidate_edge.get('geographic_distance')
                                max_distance = max(max_distance, distance)
                        
                        # Check distance from candidate to next node (use stored edge distance if available)
                        if next_node_id:
                            candidate_to_next_edge = mesh_graph.get_edge(candidate_prefix, next_node_id)
                            if candidate_to_next_edge and candidate_to_next_edge.get('geographic_distance'):
                                distance = candidate_to_next_edge.get('geographic_distance')
                                max_distance = max(max_distance, distance)
                        
                        # Apply penalty if distance exceeds reasonable hop distance
                        if max_distance > graph_max_reasonable_hop_distance_km:
                            excess_distance = max_distance - graph_max_reasonable_hop_distance_km
                            normalized_excess = min(excess_distance / graph_max_reasonable_hop_distance_km, 1.0)
                            penalty = normalized_excess * graph_distance_penalty_strength
                            candidate_score = candidate_score * (1.0 - penalty)
                        elif max_distance > 0:
                            # Even if under threshold, very long hops should get a small penalty
                            if max_distance > graph_max_reasonable_hop_distance_km * 0.8:
                                small_penalty = (max_distance - graph_max_reasonable_hop_distance_km * 0.8) / (graph_max_reasonable_hop_distance_km * 0.2) * graph_distance_penalty_strength * 0.5
                                candidate_score = candidate_score * (1.0 - small_penalty)
                
                # For final hop (next_node_id is None), add bot location proximity bonus
                # This is critical for final hop selection - the last repeater before the bot should be close
                if next_node_id is None and graph_final_hop_proximity_enabled:
                    if bot_latitude is not None and bot_longitude is not None:
                        repeater_lat = repeater.get('latitude')
                        repeater_lon = repeater.get('longitude')
                        
                        if repeater_lat is not None and repeater_lon is not None:
                            distance = calculate_distance(bot_latitude, bot_longitude, repeater_lat, repeater_lon)
                            
                            if graph_final_hop_max_distance > 0 and distance > graph_final_hop_max_distance:
                                # Beyond max distance - significantly penalize this candidate for final hop
                                candidate_score *= 0.3  # Heavy penalty for distant final hop
                            else:
                                # Normalize distance to 0-1 score (inverse: closer = higher score)
                                # Use configurable normalization distance (default 500km for more aggressive scoring)
                                normalized_distance = min(distance / graph_final_hop_proximity_normalization_km, 1.0)
                                proximity_score = 1.0 - normalized_distance
                                
                                # For final hop, use a higher effective weight to ensure proximity matters more
                                # The configured weight is a minimum; we boost it for very close repeaters
                                effective_weight = graph_final_hop_proximity_weight
                                if distance < graph_final_hop_very_close_threshold_km:
                                    # Very close - boost weight up to max
                                    effective_weight = min(graph_final_hop_max_proximity_weight, graph_final_hop_proximity_weight * 2.0)
                                elif distance < graph_final_hop_close_threshold_km:
                                    # Close - moderate boost
                                    effective_weight = min(0.5, graph_final_hop_proximity_weight * 1.5)
                                
                                # Combine with graph score using effective weight
                                candidate_score = candidate_score * (1.0 - effective_weight) + proximity_score * effective_weight
                
                # Path validation bonus: Check if candidate's stored paths match the current path context
                # This is especially important for prefix collision resolution
                path_validation_bonus = 0.0
                if candidate_public_key and len(path_context) > 1:
                    try:
                        query = '''
                            SELECT path_hex, observation_count, last_seen, from_prefix, to_prefix
                            FROM observed_paths
                            WHERE public_key = ? AND packet_type = 'advert'
                            ORDER BY observation_count DESC, last_seen DESC
                            LIMIT 10
                        '''
                        stored_paths = self.db_manager.execute_query(query, (candidate_public_key,))
                        
                        if stored_paths:
                            decoded_path_hex = ''.join([node.lower() for node in path_context])
                            # Build the path prefix up to (but not including) the current node
                            # This helps match paths where the candidate appears at the same position
                            path_prefix_up_to_current = ''.join([node.lower() for node in path_context[:current_index]])
                            
                            for stored_path in stored_paths:
                                stored_hex = stored_path.get('path_hex', '').lower()
                                obs_count = stored_path.get('observation_count', 1)
                                
                                if stored_hex:
                                    stored_nodes = [stored_hex[i:i+2] for i in range(0, len(stored_hex), 2)]
                                    decoded_nodes = [decoded_path_hex[i:i+2] for i in range(0, len(decoded_path_hex), 2)]
                                    
                                    # Check for exact path match (full path)
                                    common_segments = 0
                                    min_len = min(len(stored_nodes), len(decoded_nodes))
                                    for i in range(min_len):
                                        if stored_nodes[i] == decoded_nodes[i]:
                                            common_segments += 1
                                        else:
                                            break
                                    
                                    # Also check if stored path starts with the same prefix as the decoded path up to current position
                                    # This is important for matching paths where the candidate appears at the same position
                                    prefix_match = False
                                    if path_prefix_up_to_current and len(stored_hex) >= len(path_prefix_up_to_current):
                                        if stored_hex.startswith(path_prefix_up_to_current):
                                            # The stored path has the same prefix, and the candidate appears at the same position
                                            # This is a strong indicator of a match
                                            prefix_match = True
                                    
                                    if common_segments >= 2 or prefix_match:
                                        # Stronger bonus for prefix matches (indicates same path structure)
                                        if prefix_match and common_segments >= current_index:
                                            segment_bonus = min(graph_path_validation_max_bonus, 0.1 * (current_index + 1))
                                        else:
                                            segment_bonus = min(0.2, 0.05 * common_segments)
                                        obs_bonus = min(0.15, obs_count / graph_path_validation_obs_divisor)
                                        path_validation_bonus = max(path_validation_bonus, segment_bonus + obs_bonus)
                                        # Cap at max bonus
                                        path_validation_bonus = min(graph_path_validation_max_bonus, path_validation_bonus)
                                        if path_validation_bonus >= graph_path_validation_max_bonus * 0.9:
                                            break  # Strong match found, no need to check more
                    except Exception:
                        pass
                
                candidate_score = min(1.0, candidate_score + path_validation_bonus)
                
                if repeater.get('is_starred', False):
                    candidate_score *= star_bias_multiplier
                
                if candidate_score > best_score:
                    best_score = candidate_score
                    best_repeater = repeater
                    best_method = method
            
            if best_repeater and best_score > 0.0:
                confidence = min(1.0, best_score) if best_score <= 1.0 else 0.95 + (min(0.05, (best_score - 1.0) / star_bias_multiplier))
                return best_repeater, confidence, best_method or 'graph'
            
            return None, 0.0, None
        
        # Helper: simple proximity selection
        def select_by_simple_proximity(repeaters_with_location):
            scored_repeaters = calculate_recency_weighted_scores(repeaters_with_location)
            min_recency_threshold = 0.01
            scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= min_recency_threshold]
            
            if not scored_repeaters:
                return None, 0.0
            
            if len(scored_repeaters) == 1:
                repeater, recency_score = scored_repeaters[0]
                distance = calculate_distance(bot_latitude, bot_longitude, repeater['latitude'], repeater['longitude'])
                if max_proximity_range > 0 and distance > max_proximity_range:
                    return None, 0.0
                base_confidence = 0.4 + (recency_score * 0.5)
                return repeater, base_confidence
            
            combined_scores = []
            for repeater, recency_score in scored_repeaters:
                distance = calculate_distance(bot_latitude, bot_longitude, repeater['latitude'], repeater['longitude'])
                if max_proximity_range > 0 and distance > max_proximity_range:
                    continue
                
                normalized_distance = min(distance / 1000.0, 1.0)
                proximity_score = 1.0 - normalized_distance
                combined_score = (recency_score * recency_weight) + (proximity_score * proximity_weight)
                
                if repeater.get('is_starred', False):
                    combined_score *= star_bias_multiplier
                
                combined_scores.append((combined_score, distance, repeater))
            
            if not combined_scores:
                return None, 0.0
            
            combined_scores.sort(key=lambda x: x[0], reverse=True)
            best_score, best_distance, best_repeater = combined_scores[0]
            
            if len(combined_scores) == 1:
                confidence = 0.4 + (best_score * 0.5)
            else:
                second_best_score = combined_scores[1][0]
                score_ratio = best_score / second_best_score if second_best_score > 0 else 1.0
                if score_ratio > 1.5:
                    confidence = 0.9
                elif score_ratio > 1.2:
                    confidence = 0.8
                elif score_ratio > 1.1:
                    confidence = 0.7
                else:
                    confidence = 0.5
            
            return best_repeater, confidence
        
        # Main decoding logic (same as path command)
        decoded_path = []
        
        try:
            for node_id in node_ids:
                # Query database for matching repeaters
                if max_repeater_age_days > 0:
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
                    '''.format(max_repeater_age_days, max_repeater_age_days)
                else:
                    query = '''
                        SELECT name, public_key, device_type, last_heard, last_heard as last_seen, 
                               last_advert_timestamp, latitude, longitude, city, state, country,
                               advert_count, signal_strength, hop_count, role, is_starred
                        FROM complete_contact_tracking 
                        WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                        ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                    '''
                
                results = self.db_manager.execute_query(query, (f"{node_id}%",))
                
                if results:
                    repeaters_data = [
                        {
                            'name': row['name'],
                            'public_key': row['public_key'],
                            'device_type': row['device_type'],
                            'last_seen': row['last_seen'],
                            'last_heard': row.get('last_heard', row['last_seen']),
                            'last_advert_timestamp': row.get('last_advert_timestamp'),
                            'is_active': True,
                            'latitude': row['latitude'],
                            'longitude': row['longitude'],
                            'city': row['city'],
                            'state': row['state'],
                            'country': row['country'],
                            'hop_count': row.get('hop_count'),  # Include hop_count for zero-hop bonus
                            'is_starred': bool(row.get('is_starred', 0))
                        } for row in results
                    ]
                    
                    scored_repeaters = calculate_recency_weighted_scores(repeaters_data)
                    min_recency_threshold = 0.01
                    recent_repeaters = [r for r, score in scored_repeaters if score >= min_recency_threshold]
                    
                    if len(recent_repeaters) > 1:
                        # Multiple matches - use graph and geographic selection
                        graph_repeater = None
                        graph_confidence = 0.0
                        selection_method = None
                        geo_repeater = None
                        geo_confidence = 0.0
                        
                        if graph_based_validation and hasattr(self, 'mesh_graph') and self.mesh_graph:
                            graph_repeater, graph_confidence, selection_method = select_repeater_by_graph(
                                recent_repeaters, node_id, node_ids
                            )
                        
                        if geographic_guessing_enabled:
                            repeaters_with_location = [r for r in recent_repeaters if r.get('latitude') and r.get('longitude')]
                            if repeaters_with_location:
                                geo_repeater, geo_confidence = select_by_simple_proximity(repeaters_with_location)
                        
                        # Combine or choose
                        selected_repeater = None
                        confidence = 0.0
                        
                        if graph_geographic_combined and graph_repeater and geo_repeater:
                            graph_pubkey = graph_repeater.get('public_key', '')
                            geo_pubkey = geo_repeater.get('public_key', '')
                            
                            if graph_pubkey and geo_pubkey and graph_pubkey == geo_pubkey:
                                combined_confidence = (
                                    graph_confidence * graph_geographic_weight +
                                    geo_confidence * (1.0 - graph_geographic_weight)
                                )
                                selected_repeater = graph_repeater
                                confidence = combined_confidence
                            else:
                                if graph_confidence > geo_confidence:
                                    selected_repeater = graph_repeater
                                    confidence = graph_confidence
                                else:
                                    selected_repeater = geo_repeater
                                    confidence = geo_confidence
                        else:
                            # For final hop, prefer geographic selection if available and reasonable
                            # The final hop should be close to the bot, so geographic proximity is very important
                            is_final_hop = (node_id == node_ids[-1] if node_ids else False)
                            
                            if is_final_hop and geo_repeater and geo_confidence >= 0.6:
                                # For final hop, prefer geographic if it has decent confidence
                                # This ensures we pick the closest repeater for the last hop
                                if not graph_repeater or geo_confidence >= graph_confidence * 0.9:
                                    selected_repeater = geo_repeater
                                    confidence = geo_confidence
                                elif graph_repeater:
                                    selected_repeater = graph_repeater
                                    confidence = graph_confidence
                            elif graph_repeater and graph_confidence >= graph_confidence_override_threshold:
                                selected_repeater = graph_repeater
                                confidence = graph_confidence
                            elif not graph_repeater or graph_confidence < graph_confidence_override_threshold:
                                if geo_repeater and (not graph_repeater or geo_confidence > graph_confidence):
                                    selected_repeater = geo_repeater
                                    confidence = geo_confidence
                                elif graph_repeater:
                                    selected_repeater = graph_repeater
                                    confidence = graph_confidence
                        
                        if selected_repeater and confidence >= 0.5:
                            decoded_path.append({
                                'node_id': node_id,
                                'name': selected_repeater['name'],
                                'public_key': selected_repeater['public_key'],
                                'device_type': selected_repeater['device_type'],
                                'role': selected_repeater.get('role', 'repeater'),
                                'found': True,
                                'geographic_guess': confidence < 0.8,  # Mark as guess if confidence is lower
                                'collision': True,
                                'matches': len(recent_repeaters)
                            })
                        else:
                            # Fallback to first repeater if selection failed
                            decoded_path.append({
                                'node_id': node_id,
                                'name': recent_repeaters[0]['name'],
                                'public_key': recent_repeaters[0]['public_key'],
                                'device_type': recent_repeaters[0]['device_type'],
                                'role': recent_repeaters[0].get('role', 'repeater'),
                                'found': True,
                                'geographic_guess': True,
                                'collision': True,
                                'matches': len(recent_repeaters)
                            })
                    elif len(recent_repeaters) == 1:
                        # Single match - high confidence
                        repeater = recent_repeaters[0]
                        decoded_path.append({
                            'node_id': node_id,
                            'name': repeater['name'],
                            'public_key': repeater['public_key'],
                            'device_type': repeater['device_type'],
                            'role': repeater.get('role', 'repeater'),
                            'found': True,
                            'geographic_guess': False,
                            'collision': False,
                            'matches': 1
                        })
                    else:
                        decoded_path.append({
                            'node_id': node_id,
                            'name': None,
                            'found': False
                        })
                else:
                    decoded_path.append({
                        'node_id': node_id,
                        'name': None,
                        'found': False
                    })
        except Exception as e:
            self.logger.error(f"Error decoding path: {e}", exc_info=True)
            return []
        
        return decoded_path
    
    def run(self, host='127.0.0.1', port=8080, debug=False):
        """Run the modern web viewer"""
        self.logger.info(f"Starting modern web viewer on {host}:{port}")
        try:
            self.socketio.run(
                self.app,
                host=host,
                port=port,
                debug=debug,
                allow_unsafe_werkzeug=True
            )
        except Exception as e:
            self.logger.error(f"Error running web viewer: {e}")
            raise

def main():
    """Entry point for the meshcore-viewer command"""
    import argparse
    
    parser = argparse.ArgumentParser(description='MeshCore Bot Data Viewer')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )
    
    args = parser.parse_args()
    
    viewer = BotDataViewer(config_path=args.config)
    viewer.run(host=args.host, port=args.port, debug=args.debug)

if __name__ == '__main__':
    main()
