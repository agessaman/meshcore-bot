#!/usr/bin/env python3
"""
Core MeshCore Bot functionality
Contains the main bot class and message processing logic
"""

import asyncio
import configparser
import json
import logging
import colorlog
import time
import threading
import schedule
import signal
import atexit
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass

# Import the official meshcore package
import meshcore
from meshcore import EventType

# Import command functions from meshcore-cli
from meshcore_cli.meshcore_cli import send_cmd, send_chan_msg

# Import our modules
from .rate_limiter import RateLimiter, BotTxRateLimiter, NominatimRateLimiter
from .message_handler import MessageHandler
from .command_manager import CommandManager
from .channel_manager import ChannelManager
from .scheduler import MessageScheduler
from .repeater_manager import RepeaterManager
from .db_manager import DBManager
from .i18n import Translator
from .solar_conditions import set_config
from .web_viewer.integration import WebViewerIntegration
from .feed_manager import FeedManager
from .service_plugin_loader import ServicePluginLoader
from .transmission_tracker import TransmissionTracker
from .utils import resolve_path


class MeshCoreBot:
    """MeshCore Bot using official meshcore package.
    
    This class handles the core functionality of the bot, including connection management,
    message processing initialization, and module coordination.
    """
    
    def __init__(self, config_file: str = "config.ini"):
        self.config_file = config_file
        self.config = configparser.ConfigParser()
        self.load_config()
        
        # Setup logging
        self.setup_logging()
        
        # Connection
        self.meshcore = None
        self.connected = False
        self.connection_time = None  # Track when connection was established to skip old cached messages
        
        # Bot start time for uptime tracking
        self.start_time = time.time()
        
        # Initialize database manager first (needed by plugins)
        db_path = self.config.get('Bot', 'db_path', fallback='meshcore_bot.db')
        
        # Resolve database path (relative paths resolved from bot root, absolute paths used as-is)
        db_path = resolve_path(db_path, self.bot_root)
        
        self.logger.info(f"Initializing database manager with database: {db_path}")
        try:
            self.db_manager = DBManager(self, db_path)
            self.logger.info("Database manager initialized successfully")
        except (OSError, ValueError, sqlite3.Error) as e:
            self.logger.error(f"Failed to initialize database manager: {e}")
            raise
        
        # Store start time in database for web viewer access
        try:
            self.db_manager.set_bot_start_time(self.start_time)
            self.logger.info("Bot start time stored in database")
        except (OSError, sqlite3.Error, AttributeError) as e:
            self.logger.warning(f"Could not store start time in database: {e}")
        
        # Initialize web viewer integration (after database manager)
        try:
            self.web_viewer_integration = WebViewerIntegration(self)
            self.logger.info("Web viewer integration initialized")
            
            # Register cleanup handler for web viewer
            atexit.register(self._cleanup_web_viewer)
        except (OSError, ValueError, AttributeError, ImportError) as e:
            self.logger.warning(f"Web viewer integration failed: {e}")
            self.web_viewer_integration = None
        
        # Initialize modules
        self.rate_limiter = RateLimiter(
            self.config.getint('Bot', 'rate_limit_seconds', fallback=10)
        )
        self.bot_tx_rate_limiter = BotTxRateLimiter(
            self.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
        )
        # Nominatim rate limiter: 1.1 seconds between requests (Nominatim policy: max 1 req/sec)
        self.nominatim_rate_limiter = NominatimRateLimiter(
            self.config.getfloat('Bot', 'nominatim_rate_limit_seconds', fallback=1.1)
        )
        self.tx_delay_ms = self.config.getint('Bot', 'tx_delay_ms', fallback=250)
        
        # Initialize translator for localization BEFORE CommandManager
        # This ensures translated keywords are available when commands are loaded
        try:
            language = self.config.get('Localization', 'language', fallback='en')
            translation_path = self.config.get('Localization', 'translation_path', fallback='translations/')
            self.translator = Translator(language, translation_path)
            self.logger.info(f"Localization initialized: {language}")
        except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
            self.logger.warning(f"Failed to initialize translator: {e}")
            # Create a dummy translator that just returns keys
            class DummyTranslator:
                def translate(self, key, **kwargs):
                    return key
                def get_value(self, key):
                    return None
            self.translator = DummyTranslator()
        
        # Initialize solar conditions configuration
        set_config(self.config)
        
        self.message_handler = MessageHandler(self)
        self.command_manager = CommandManager(self)
        
        # Initialize transmission tracker for monitoring TX success
        try:
            self.transmission_tracker = TransmissionTracker(self)
            self.logger.info("Transmission tracker initialized")
        except Exception as e:
            self.logger.warning(f"Failed to initialize transmission tracker: {e}")
            self.transmission_tracker = None
        
        # Load max_channels from config (default 40, MeshCore supports up to 40 channels)
        max_channels = self.config.getint('Bot', 'max_channels', fallback=40)
        self.channel_manager = ChannelManager(self, max_channels=max_channels)
        
        self.scheduler = MessageScheduler(self)
        
        # Initialize feed manager
        self.logger.info("Initializing feed manager")
        try:
            self.feed_manager = FeedManager(self)
            self.logger.info("Feed manager initialized successfully")
        except (OSError, ValueError, AttributeError, ImportError) as e:
            self.logger.warning(f"Failed to initialize feed manager: {e}")
            self.feed_manager = None
        
        # Initialize repeater manager
        self.logger.info("Initializing repeater manager")
        try:
            self.repeater_manager = RepeaterManager(self)
            self.logger.info("Repeater manager initialized successfully")
        except (OSError, ValueError, AttributeError) as e:
            self.logger.error(f"Failed to initialize repeater manager: {e}")
            raise
        
        # Initialize mesh graph for path validation
        self.logger.info("Initializing mesh graph")
        try:
            from .mesh_graph import MeshGraph
            self.mesh_graph = MeshGraph(self)
            self.logger.info("Mesh graph initialized successfully")
            
            # Register cleanup handler for mesh graph (independent of web viewer)
            # This ensures pending graph writes are flushed during shutdown
            atexit.register(self._cleanup_mesh_graph)
        except (OSError, ValueError, AttributeError, ImportError) as e:
            self.logger.warning(f"Failed to initialize mesh graph: {e}")
            self.mesh_graph = None
        
        # Initialize service plugin loader and load all services
        self.logger.info("Initializing service plugin loader")
        try:
            self.service_loader = ServicePluginLoader(self)
            self.services = self.service_loader.load_all_services()
            self.logger.info(f"Service plugin loader initialized with {len(self.services)} service(s)")
        except (OSError, ImportError, AttributeError, ValueError) as e:
            self.logger.error(f"Failed to initialize service plugin loader: {e}")
            self.service_loader = None
            self.services = {}
        
        # Backward compatibility: expose packet_capture_service for existing code
        # This allows code that references self.packet_capture_service to continue working
        # Try to find by service name first, then by class name
        self.packet_capture_service = None
        for service_name, service_instance in self.services.items():
            if (service_name == 'packetcapture' or 
                service_instance.__class__.__name__ == 'PacketCaptureService'):
                self.packet_capture_service = service_instance
                break
        
        # Reload translated keywords for all commands now that translator is available
        # This ensures keywords are loaded even if translator wasn't ready during command init
        if hasattr(self, 'command_manager') and hasattr(self, 'translator'):
            for cmd_name, cmd_instance in self.command_manager.commands.items():
                if hasattr(cmd_instance, '_load_translated_keywords'):
                    cmd_instance._load_translated_keywords()
        
        # Advert tracking
        self.last_advert_time = None
        
        # Clock sync tracking
        self.last_clock_sync_time = None
        
        # Shutdown event for graceful shutdown
        self._shutdown_event = threading.Event()

        # Service plugin restart state (name -> timestamp of last failed restart)
        self._service_restart_failures: Dict[str, float] = {}
        self._service_restarting: set = set()

    @property
    def bot_root(self) -> Path:
        """Get bot root directory (where config.ini is located)"""
        return Path(self.config_file).parent.resolve()
    
    def load_config(self) -> None:
        """Load configuration from file.
        
        Reads the configuration file specified in self.config_file. If the file
        does not exist, a default configuration is created first.
        """
        if not Path(self.config_file).exists():
            self.create_default_config()
        
        self.config.read(self.config_file)
    
    def _get_radio_settings(self) -> Dict[str, Any]:
        """Get current radio/connection settings from config.
        
        Returns:
            Dict[str, Any]: Dictionary containing all radio-related settings.
        """
        return {
            'connection_type': self.config.get('Connection', 'connection_type', fallback='ble').lower(),
            'serial_port': self.config.get('Connection', 'serial_port', fallback=''),
            'ble_device_name': self.config.get('Connection', 'ble_device_name', fallback=''),
            'hostname': self.config.get('Connection', 'hostname', fallback=''),
            'tcp_port': self.config.getint('Connection', 'tcp_port', fallback=5000),
            'timeout': self.config.getint('Connection', 'timeout', fallback=30),
        }
    
    def reload_config(self) -> Tuple[bool, str]:
        """Reload configuration from file without restarting the bot.
        
        This method reloads the configuration file and updates all components
        that depend on it. It will reject the reload if radio/connection settings
        have changed, as those require a full restart.
        
        Returns:
            Tuple[bool, str]: (success, message) tuple indicating if reload succeeded
                and a descriptive message.
        """
        try:
            # Store current radio settings before reload
            old_radio_settings = self._get_radio_settings()
            
            # Create a temporary config parser to check new settings
            import configparser
            new_config = configparser.ConfigParser()
            if not Path(self.config_file).exists():
                return (False, "Config file not found")
            
            new_config.read(self.config_file)
            
            # Get new radio settings
            new_radio_settings = {
                'connection_type': new_config.get('Connection', 'connection_type', fallback='ble').lower(),
                'serial_port': new_config.get('Connection', 'serial_port', fallback=''),
                'ble_device_name': new_config.get('Connection', 'ble_device_name', fallback=''),
                'hostname': new_config.get('Connection', 'hostname', fallback=''),
                'tcp_port': new_config.getint('Connection', 'tcp_port', fallback=5000),
                'timeout': new_config.getint('Connection', 'timeout', fallback=30),
            }
            
            # Check if radio settings changed
            if old_radio_settings != new_radio_settings:
                changed_settings = []
                for key in old_radio_settings:
                    if old_radio_settings[key] != new_radio_settings[key]:
                        changed_settings.append(f"{key}: {old_radio_settings[key]} -> {new_radio_settings[key]}")
                return (False, f"Radio settings changed. Restart required. Changes: {', '.join(changed_settings)}")
            
            # Radio settings unchanged, proceed with reload
            self.logger.info("Reloading configuration (radio settings unchanged)")
            
            # Reload the config
            self.config.read(self.config_file)
            
            # Update rate limiters
            new_rate_limit = self.config.getint('Bot', 'rate_limit_seconds', fallback=10)
            self.rate_limiter = RateLimiter(new_rate_limit)
            
            new_bot_tx_rate_limit = self.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
            self.bot_tx_rate_limiter = BotTxRateLimiter(new_bot_tx_rate_limit)
            
            new_nominatim_rate_limit = self.config.getfloat('Bot', 'nominatim_rate_limit_seconds', fallback=1.1)
            self.nominatim_rate_limiter = NominatimRateLimiter(new_nominatim_rate_limit)
            
            # Update transmission delay
            self.tx_delay_ms = self.config.getint('Bot', 'tx_delay_ms', fallback=250)
            
            # Update translator if language changed
            try:
                new_language = self.config.get('Localization', 'language', fallback='en')
                new_translation_path = self.config.get('Localization', 'translation_path', fallback='translations/')
                if (not hasattr(self, 'translator') or 
                    getattr(self.translator, 'language', None) != new_language or
                    getattr(self.translator, 'translation_path', None) != new_translation_path):
                    self.translator = Translator(new_language, new_translation_path)
                    self.logger.info(f"Translator reloaded with language: {new_language}")
                    
                    # Reload translated keywords for all commands
                    if hasattr(self, 'command_manager'):
                        for cmd_name, cmd_instance in self.command_manager.commands.items():
                            if hasattr(cmd_instance, '_load_translated_keywords'):
                                cmd_instance._load_translated_keywords()
            except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
                self.logger.warning(f"Failed to reload translator: {e}")
            
            # Update solar conditions config
            set_config(self.config)
            
            # Update command manager (keywords, custom syntax, banned users, monitor channels)
            if hasattr(self, 'command_manager'):
                self.command_manager.keywords = self.command_manager.load_keywords()
                self.command_manager.custom_syntax = self.command_manager.load_custom_syntax()
                self.command_manager.banned_users = self.command_manager.load_banned_users()
                self.command_manager.monitor_channels = self.command_manager.load_monitor_channels()
                self.logger.info("Command manager config reloaded")
            
            # Update scheduler (scheduled messages)
            if hasattr(self, 'scheduler'):
                self.scheduler.setup_scheduled_messages()
                self.logger.info("Scheduler config reloaded")
            
            # Update channel manager max_channels if changed
            if hasattr(self, 'channel_manager'):
                new_max_channels = self.config.getint('Bot', 'max_channels', fallback=40)
                if hasattr(self.channel_manager, 'max_channels'):
                    old_max_channels = self.channel_manager.max_channels
                    self.channel_manager.max_channels = new_max_channels
                    if old_max_channels != new_max_channels:
                        self.logger.info(f"Channel manager max_channels updated to {new_max_channels}")
                # Note: We don't invalidate the channel cache here because channels are fetched
                # from the device, not from config. The cache should remain valid after reload.
            
            # Note: Service plugins check config on-demand, so they'll pick up changes automatically
            # Feed manager and other services that need explicit reload can be added here if needed
            
            self.logger.info("Configuration reloaded successfully")
            return (True, "Configuration reloaded successfully")
            
        except Exception as e:
            error_msg = f"Error reloading configuration: {e}"
            self.logger.error(error_msg)
            import traceback
            self.logger.error(traceback.format_exc())
            return (False, error_msg)
    
    def create_default_config(self) -> None:
        """Create default configuration file.
        
        Writes a default 'config.ini' file to disk with standard settings
        and comments explaining each option.
        """
        default_config = """[Connection]
# Connection type: serial, ble, or tcp
# serial: Connect via USB serial port
# ble: Connect via Bluetooth Low Energy
# tcp: Connect via TCP/IP
connection_type = serial

# Serial port (for serial connection)
# Common ports: /dev/ttyUSB0, /dev/tty.usbserial-*, COM3 (Windows)
serial_port = /dev/ttyUSB0

# BLE device name (for BLE connection)
# Leave commented out for auto-detection, or specify exact device name
#ble_device_name = MeshCore

# TCP hostname or IP address (for TCP connection)
#hostname = 192.168.1.60
# TCP port (for TCP connection)
#tcp_port = 5000

# Connection timeout in seconds
timeout = 30

[Bot]
# Bot name for identification and logging
bot_name = MeshCoreBot

# RF Data Correlation Settings
# Time window for correlating RF data with messages (seconds)
rf_data_timeout = 15.0

# Time to wait for RF data correlation (seconds)
message_correlation_timeout = 10.0

# Enable enhanced correlation strategies
enable_enhanced_correlation = true

# Bot node ID (leave empty for auto-assignment)
node_id = 

# Enable/disable bot responses
# true: Bot will respond to keywords and commands
# false: Bot will only listen and log messages
enabled = true

# Passive mode (only listen, don't respond)
# true: Bot will not send any messages
# false: Bot will respond normally
passive_mode = false

# Rate limiting in seconds between messages
# Prevents spam by limiting how often the bot can send messages
rate_limit_seconds = 2

# Bot transmission rate limit in seconds between bot messages
# Prevents bot from overwhelming the mesh network
bot_tx_rate_limit_seconds = 1.0

# Transmission delay in milliseconds before sending messages
# Helps prevent message collisions on the mesh network
# Recommended: 100-500ms for busy networks, 0 for quiet networks
tx_delay_ms = 250

# DM retry settings for improved reliability (meshcore-2.1.6+)
# Maximum number of retry attempts for failed DM sends
dm_max_retries = 3

# Maximum flood attempts (when path reset is needed)
dm_max_flood_attempts = 2

# Number of attempts before switching to flood mode
dm_flood_after = 2

# Timezone for bot operations
# Use standard timezone names (e.g., "America/New_York", "Europe/London", "UTC")
# Leave empty to use system timezone
timezone = 

# Bot location for geographic proximity calculations and astronomical data
# Default latitude for bot location (decimal degrees)
# Example: 40.7128 for New York City, 48.50 for Victoria BC
bot_latitude = 40.7128

# Default longitude for bot location (decimal degrees)
# Example: -74.0060 for New York City, -123.00 for Victoria BC
bot_longitude = -74.0060

# Interval-based advertising settings
# Send periodic flood adverts at specified intervals
# 0: Disabled (default)
# >0: Send flood advert every N hours
advert_interval_hours = 0

# Send startup advert when bot finishes initializing
# false: No startup advert (default)
# zero-hop: Send local broadcast advert
# flood: Send network-wide flood advert
startup_advert = false

# Auto-manage contact list when new contacts are discovered
# device: Device handles auto-addition using standard auto-discovery mode, bot manages contact list capacity (purge old contacts when near limits)
# bot: Bot automatically adds new companion contacts to device, bot manages contact list capacity (purge old contacts when near limits)
# false: Manual mode - no automatic actions, use !repeater commands to manage contacts (default)
auto_manage_contacts = false

[Jokes]
# Enable or disable the joke command
# true: Joke command is available
# false: Joke command is disabled
joke_enabled = true

# Enable seasonal joke defaults
# When enabled, October defaults to spooky jokes, December defaults to Christmas jokes
# true: Seasonal defaults are applied
# false: No seasonal defaults (always random)
seasonal_jokes = true

# Enable or disable the dad joke command
# true: Dad joke command is available
# false: Dad joke command is disabled
dadjoke_enabled = true

# Handle long jokes (over 130 characters)
# false: Fetch new jokes until we get a short one
# true: Split long jokes into multiple messages
long_jokes = false

[Admin_ACL]
# Admin Access Control List (ACL) for restricted commands
# Only users with public keys listed here can execute admin commands
# Format: comma-separated list of public keys (without spaces)
# Example: f5d2b56d19b24412756933e917d4632e088cdd5daeadc9002feca73bf5d2b56d,another_key_here
admin_pubkeys = 

# Commands that require admin access (comma-separated)
# These commands will only work for users in the admin_pubkeys list
admin_commands = repeater

[Keywords]
# Keyword-response pairs (keyword = response format)
# Available fields: {sender}, {connection_info}, {snr}, {rssi}, {timestamp}, {path}, {path_distance}, {firstlast_distance}
# {sender}: Name/ID of message sender
# {connection_info}: Path info, SNR, and RSSI combined (e.g., "01,5f (2 hops) | SNR: 15 dB | RSSI: -120 dBm")
# {snr}: Signal-to-noise ratio in dB
# {rssi}: Received signal strength indicator in dBm
# {timestamp}: Message timestamp in HH:MM:SS format
# {path}: Message routing path (e.g., "01,5f (2 hops)")
# {path_distance}: Total distance between all hops in path with locations (e.g., "123.4km (3 segs, 1 no-loc)")
# {firstlast_distance}: Distance between first and last repeater in path (e.g., "45.6km" or empty if locations missing)
test = "ack [@{sender}]{phrase_part} | {connection_info} | Received at: {timestamp}"
ping = "Pong!"
pong = "Ping!"
help = "Bot Help: test, ping, help, hello, cmd, advert, t phrase, @string, wx, aqi, sun, moon, solar, hfcond, satpass | Use 'help <command>' for details"
cmd = "Available commands: test, ping, help, hello, cmd, advert, t phrase, @string, wx, aqi, sun, moon, solar, hfcond, satpass"

[Channels]
# Channels to monitor (comma-separated)
# Bot will only respond to messages on these channels
# Use exact channel names as configured on your MeshCore node
monitor_channels = general,test,emergency

# Enable DM responses
# true: Bot will respond to direct messages
# false: Bot will ignore direct messages
respond_to_dms = true

[Banned_Users]
# List of banned sender names (comma-separated). Matching is prefix (starts-with):
# "Awful Username" also matches "Awful Username 🍆". No bot responses in channels or DMs.
banned_users = 

[Scheduled_Messages]
# Scheduled message format: HHMM = channel:message
# Time format: HHMM (24-hour, no colon)
# Bot will send these messages at the specified times daily
0800 = general:Good morning! Bot is online and ready.
1200 = general:Midday status check - all systems operational.
1800 = general:Evening update - bot status: Good

[Logging]
# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
# DEBUG: Most verbose, shows all details
# INFO: Standard logging level
# WARNING: Only warnings and errors
# ERROR: Only errors
# CRITICAL: Only critical errors
log_level = INFO

# Log file path (leave empty for console only)
# Bot will write logs to this file in addition to console
# Use absolute path for Docker compatibility (e.g., /data/logs/meshcore_bot.log)
# Relative paths will resolve relative to the config file directory
log_file = meshcore_bot.log

# Enable colored console output
# true: Use colors in console output
# false: Plain text output
colored_output = true

# MeshCore library log level (separate from bot log level)
# Controls debug output from the meshcore library itself
# Options: DEBUG, INFO, WARNING, ERROR, CRITICAL
meshcore_log_level = INFO

[Custom_Syntax]
# Custom syntax patterns for special message formats
# Format: pattern = "response_format"
# Available fields: {sender}, {phrase}, {connection_info}, {snr}, {timestamp}, {path}
# {phrase}: The text after the trigger (for t_phrase syntax)
# 
# Special syntax: Messages starting with "t " or "T " followed by a phrase
# Example: "t hello world" -> "ack {sender}: hello world | {connection_info}"
t_phrase = "ack {sender}: {phrase} | {connection_info}"


[External_Data]
# Weather API key (future feature)
weather_api_key = 

# Weather update interval in seconds (future feature)
weather_update_interval = 3600

# Tide API key (future feature)
tide_api_key = 

# Tide update interval in seconds (future feature)
tide_update_interval = 1800

# N2YO API key for satellite pass information
# Get free key at: https://www.n2yo.com/login/
n2yo_api_key = 

# AirNow API key for AQI data
# Get free key at: https://docs.airnowapi.org/
airnow_api_key = 

# Repeater prefix API URL for prefix command
# Leave empty to disable prefix command functionality
# Configure your own regional API endpoint
repeater_prefix_api_url = 

# Repeater prefix cache duration in hours
# How long to cache prefix data before refreshing from API
# Recommended: 1-6 hours (data doesn't change frequently)
repeater_prefix_cache_hours = 1

[Prefix_Command]
# Enable or disable repeater geolocation in prefix command
# true: Show city names with repeaters when location data is available
# false: Show only repeater names without location information
show_repeater_locations = true

# Use reverse geocoding for coordinates without city names
# true: Automatically look up city names from GPS coordinates
# false: Only show coordinates if no city name is available
use_reverse_geocoding = true

# Hide prefix source information
# true: Hide "Source: domain.com" line from prefix command output
# false: Show source information (default)
hide_source = false

# Prefix heard time window (days)
# Number of days to look back when showing prefix results (default command behavior)
# Only repeaters heard within this window will be shown by default
# Use "prefix XX all" to show all repeaters regardless of time
prefix_heard_days = 7

# Prefix free time window (days)
# Number of days to look back when determining which prefixes are "free"
# Only repeaters heard within this window will be considered as using a prefix
# Repeaters not heard in this window will be excluded from used prefixes list
prefix_free_days = 30

[Weather]
# Default state for city name disambiguation
# When users type "wx seattle", it will search for "seattle, WA, USA"
# Use 2-letter state abbreviation (e.g., WA, CA, NY, TX)
default_state = WA

# Default country for city name disambiguation (for international weather plugin)
# Use 2-letter country code (e.g., US, CA, GB, AU)
default_country = US

# Temperature unit for weather display
# Options: fahrenheit, celsius
# Default: fahrenheit
temperature_unit = fahrenheit

# Wind speed unit for weather display
# Options: mph, kmh, ms (meters per second)
# Default: mph
wind_speed_unit = mph

# Precipitation unit for weather display
# Options: inch, mm
# Default: inch
precipitation_unit = inch

[Path_Command]
# Geographic proximity calculation method
# simple: Use proximity to bot location (default)
# path: Use proximity to previous/next nodes in the path for more realistic routing
proximity_method = simple

# Enable path proximity fallback
# When path proximity can't be calculated (missing location data), fall back to simple proximity
# true: Fall back to bot location proximity when path data unavailable
# false: Show collision warning when path proximity unavailable
path_proximity_fallback = true

# Maximum range for geographic proximity guessing (kilometers)
# Repeaters beyond this distance will have reduced confidence or be rejected
# Set to 0 to disable range limiting
max_proximity_range = 200

# Maximum age for repeater data in path matching (days)
# Only include repeaters that have been heard within this many days
# Helps filter out stale or inactive repeaters from path decoding
# Set to 0 to disable age filtering
max_repeater_age_days = 14

# Confidence indicator symbols for path command
# High confidence (>= 0.9): Shows when path decoding is very reliable
high_confidence_symbol = 🎯

# Medium confidence (>= 0.8): Shows when path decoding is reasonably reliable
medium_confidence_symbol = 📍

# Low confidence (< 0.8): Shows when path decoding has uncertainty
low_confidence_symbol = ❓

[Solar_Config]
# URL timeout for external API calls (seconds)
url_timeout = 10

# Use Zulu/UTC time for astronomical data
# true: Use 24-hour UTC format
# false: Use 12-hour local format
use_zulu_time = false
"""
        with open(self.config_file, 'w') as f:
            f.write(default_config)
        # Note: Using print here since logger may not be initialized yet
        print(f"Created default config file: {self.config_file}")
    
    def setup_logging(self) -> None:
        """Setup logging configuration.
        
        Configures the logging system based on settings in the config file.
        Sets up console and file handlers, formatters, and log levels for
        both the bot and the underlying meshcore library.
        """
        log_level = getattr(logging, self.config.get('Logging', 'log_level', fallback='INFO'))
        
        # Create formatter
        if self.config.getboolean('Logging', 'colored_output', fallback=True):
            formatter = colorlog.ColoredFormatter(
                '%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S',
                log_colors={
                    'DEBUG': 'cyan',
                    'INFO': 'green',
                    'WARNING': 'yellow',
                    'ERROR': 'red',
                    'CRITICAL': 'red,bg_white',
                }
            )
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        
        # Setup logger
        self.logger = logging.getLogger('MeshCoreBot')
        self.logger.setLevel(log_level)
        
        # Clear any existing handlers to prevent duplicates
        self.logger.handlers.clear()
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        # File handler
        log_file = self.config.get('Logging', 'log_file', fallback='meshcore_bot.log')
        
        # Strip whitespace and check if empty
        log_file = log_file.strip() if log_file else ''
        
        # If log_file is empty, skip file logging (console only)
        if not log_file:
            self.logger.info("No log file specified, using console logging only")
        else:
            # Resolve log file path (relative paths resolved from bot root, absolute paths used as-is)
            log_file = resolve_path(log_file, self.bot_root)
            
            # Ensure the log directory exists
            log_dir = Path(log_file).parent
            if not log_dir.exists():
                try:
                    log_dir.mkdir(parents=True, exist_ok=True)
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"Could not create log directory {log_dir}: {e}. Using console logging only.")
                    log_file = None
            
            if log_file:
                try:
                    file_handler = logging.FileHandler(log_file)
                    file_handler.setFormatter(formatter)
                    self.logger.addHandler(file_handler)
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"Could not open log file {log_file}: {e}. Using console logging only.")
        
        # Prevent propagation to root logger to avoid duplicate output
        self.logger.propagate = False
        
        # Configure meshcore library logging (separate from bot logging)
        meshcore_log_level = getattr(logging, self.config.get('Logging', 'meshcore_log_level', fallback='INFO'))
        
        # Configure all possible meshcore-related loggers
        meshcore_loggers = [
            'meshcore',
            'meshcore_cli', 
            'meshcore.meshcore',
            'meshcore_cli.meshcore_cli',
            'meshcore_cli.commands',
            'meshcore_cli.connection'
        ]
        
        for logger_name in meshcore_loggers:
            logger = logging.getLogger(logger_name)
            logger.setLevel(meshcore_log_level)
            # Remove any existing handlers to prevent duplicate output
            logger.handlers.clear()
            # Add our formatter
            if not logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(formatter)
                logger.addHandler(handler)
        
        # Configure root logger to prevent other libraries from using DEBUG
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        
        # Log the configuration for debugging
        self.logger.info(f"Logging configured - Bot: {logging.getLevelName(log_level)}, MeshCore: {logging.getLevelName(meshcore_log_level)}")
        
        # Setup routing info capture for web viewer
        self._setup_routing_capture()
        
        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()
    
    def _setup_routing_capture(self) -> None:
        """Setup routing information capture for web viewer.
        
        Initializes the mechanism to capture message routing information
        if the web viewer integration is enabled.
        """
        # Web viewer doesn't need complex routing capture
        # It uses direct database access instead of complex integration
        if not (hasattr(self, 'web_viewer_integration') and 
                self.web_viewer_integration):
            return
        
        self.logger.info("Web viewer routing capture setup complete")
    
    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown.
        
        Registers handlers for SIGTERM and SIGINT to ensure the bot can
        clean up resources and disconnect properly when stopped.
        """
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            # Set shutdown event to break main loop
            self._shutdown_event.set()
            # Set connected to False to break the while loop in start()
            self.connected = False
        
        # Register signal handlers
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    async def connect(self) -> bool:
        """Connect to MeshCore node using official package.
        
        Establishes a connection to the mesh node via Serial, TCP, or BLE
        based on the configuration.
        
        Returns:
            bool: True if connection was successful, False otherwise.
        """
        try:
            self.logger.info("Connecting to MeshCore node...")
            
            # Get connection type from config
            connection_type = self.config.get('Connection', 'connection_type', fallback='ble').lower()
            self.logger.info(f"Using connection type: {connection_type}")
            
            if connection_type == 'serial':
                # Create serial connection
                serial_port = self.config.get('Connection', 'serial_port', fallback='/dev/ttyUSB0')
                self.logger.info(f"Connecting via serial port: {serial_port}")
                self.meshcore = await meshcore.MeshCore.create_serial(serial_port, debug=False)
            elif connection_type == 'tcp':
                # Create TCP connection
                hostname = self.config.get('Connection', 'hostname', fallback=None)
                tcp_port = self.config.getint('Connection', 'tcp_port', fallback=5000)
                if not hostname:
                    self.logger.error("TCP connection requires 'hostname' to be set in config")
                    return False
                self.logger.info(f"Connecting via TCP: {hostname}:{tcp_port}")
                self.meshcore = await meshcore.MeshCore.create_tcp(hostname, tcp_port, debug=False)
            else:
                # Create BLE connection (default)
                ble_device_name = self.config.get('Connection', 'ble_device_name', fallback=None)
                self.logger.info(f"Connecting via BLE" + (f" to device: {ble_device_name}" if ble_device_name else ""))
                self.meshcore = await meshcore.MeshCore.create_ble(ble_device_name, debug=False)
            
            if self.meshcore.is_connected:
                self.connected = True
                # Track connection time to skip processing old cached messages
                self.connection_time = time.time()
                self.logger.info(f"Connected to: {self.meshcore.self_info} at {self.connection_time}")
                
                # Wait for contacts to load
                await self.wait_for_contacts()
                
                # Fetch channels
                await self.channel_manager.fetch_channels()
                
                # Setup message event handlers
                await self.setup_message_handlers()
                
                # Set radio clock if needed
                await self.set_radio_clock()
                
                return True
            else:
                self.logger.error("Failed to connect to MeshCore node")
                return False
                
        except (OSError, ConnectionError, TimeoutError, ValueError, AttributeError) as e:
            self.logger.error(f"Connection failed: {e}")
            return False
    
    async def set_radio_clock(self) -> bool:
        """Set radio clock if device time is earlier than system time.
        
        Checks the connected device's time and updates it to match the system
        time if the device is lagging behind.
        
        Returns:
            bool: True if check/update was successful (or not needed), False on error.
        """
        try:
            if not self.meshcore or not self.meshcore.is_connected:
                self.logger.warning("Cannot set radio clock - not connected to device")
                return False
            
            # Get current device time
            self.logger.info("Checking device time...")
            time_result = await self.meshcore.commands.get_time()
            if time_result.type == EventType.ERROR:
                self.logger.warning("Device does not support time commands")
                return False
            
            device_time = time_result.payload.get('time', 0)
            current_time = int(time.time())
            
            self.logger.info(f"Device time: {device_time}, System time: {current_time}")
            
            # Only set time if device time is earlier than current time
            if device_time < current_time:
                time_diff = current_time - device_time
                self.logger.info(f"Device time is {time_diff} seconds behind, updating...")
                
                result = await self.meshcore.commands.set_time(current_time)
                if result.type == EventType.OK:
                    self.logger.info(f"✓ Radio clock updated to: {current_time}")
                    self.last_clock_sync_time = current_time
                    return True
                else:
                    self.logger.warning(f"Failed to update radio clock: {result}")
                    return False
            else:
                self.logger.info("Device time is current or ahead - no update needed")
                return True
                
        except (OSError, AttributeError, ValueError, KeyError) as e:
            self.logger.warning(f"Error checking/setting radio clock: {e}")
            return False
    
    async def wait_for_contacts(self) -> None:
        """Wait for contacts to be loaded from the device.
        
        Polls the device for contact list or waits for automatic loading.
        Times out after 30 seconds if contacts are not loaded.
        """
        self.logger.info("Waiting for contacts to load...")
        
        # Try to manually load contacts first
        try:
            from meshcore_cli.meshcore_cli import next_cmd
            self.logger.info("Manually requesting contacts from device...")
            result = await next_cmd(self.meshcore, ["contacts"])
            self.logger.info(f"Contacts command result: {len(result) if result else 0} contacts")
        except (OSError, AttributeError, ValueError) as e:
            self.logger.warning(f"Error manually loading contacts: {e}")
        
        # Check if contacts are loaded (even if empty list)
        if hasattr(self.meshcore, 'contacts'):
            self.logger.info(f"Contacts loaded: {len(self.meshcore.contacts)} contacts")
            return
        
        # Wait up to 30 seconds for contacts to load
        max_wait = 30
        wait_time = 0
        while wait_time < max_wait:
            if hasattr(self.meshcore, 'contacts'):
                self.logger.info(f"Contacts loaded: {len(self.meshcore.contacts)} contacts")
                return
            
            await asyncio.sleep(5)
            wait_time += 5
            self.logger.info(f"Still waiting for contacts... ({wait_time}s)")
        
        self.logger.warning(f"Contacts not loaded after {max_wait} seconds, proceeding anyway")
    
    async def setup_message_handlers(self) -> None:
        """Setup event handlers for messages.
        
        Registers callbacks for various meshcore events including contact messages,
        channel messages, RF data, and raw data packets.
        """
        # Handle contact messages (DMs)
        async def on_contact_message(event, metadata=None):
            await self.message_handler.handle_contact_message(event, metadata)
        
        # Handle channel messages
        async def on_channel_message(event, metadata=None):
            await self.message_handler.handle_channel_message(event, metadata)
        
        # Handle RF log data for SNR information
        async def on_rf_data(event, metadata=None):
            await self.message_handler.handle_rf_log_data(event, metadata)
        
        # Handle raw data events (full packet data)
        async def on_raw_data(event, metadata=None):
            await self.message_handler.handle_raw_data(event, metadata)
        
        # Handle new contact events
        async def on_new_contact(event, metadata=None):
            await self.message_handler.handle_new_contact(event, metadata)
        
        # Subscribe to events
        self.meshcore.subscribe(EventType.CONTACT_MSG_RECV, on_contact_message)
        self.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, on_channel_message)
        self.meshcore.subscribe(EventType.RX_LOG_DATA, on_rf_data)
        
        # Subscribe to RAW_DATA events for full packet data
        self.meshcore.subscribe(EventType.RAW_DATA, on_raw_data)
        
        # Note: Debug mode commands are not available in current meshcore-cli version
        # The meshcore library handles debug output automatically when needed
        
        # Start auto message fetching
        await self.meshcore.start_auto_message_fetching()
        
        # Delay NEW_CONTACT subscription to ensure device is fully ready
        self.logger.info("Delaying NEW_CONTACT subscription to ensure device readiness...")
        await asyncio.sleep(5)  # Wait 5 seconds for device to be fully ready
        
        # Subscribe to NEW_CONTACT events for automatic contact management
        self.meshcore.subscribe(EventType.NEW_CONTACT, on_new_contact)
        self.logger.info("NEW_CONTACT subscription active - ready to receive new contact events")
        
        self.logger.info("Message handlers setup complete")
    
    async def start(self) -> None:
        """Start the bot.
        
        Initiates the connection to the node, sets up scheduling, services,
        and starts the main execution loop.
        """
        self.logger.info("Starting MeshCore Bot...")
        
        # Store reference to main event loop for scheduler thread access
        self.main_event_loop = asyncio.get_running_loop()
        
        # Connect to MeshCore node
        if not await self.connect():
            self.logger.error("Failed to connect to MeshCore node")
            return

        # Update transmission tracker bot prefix now that we're connected
        if hasattr(self, 'transmission_tracker') and self.transmission_tracker:
            self.transmission_tracker._update_bot_prefix()

        # Setup scheduled messages
        self.scheduler.setup_scheduled_messages()
        
        # Initialize feed manager (if enabled)
        if self.feed_manager:
            await self.feed_manager.initialize()
        
        # Start scheduler thread
        self.scheduler.start()
        
        # Start web viewer if enabled
        if self.web_viewer_integration and self.web_viewer_integration.enabled:
            self.web_viewer_integration.start_viewer()
            self.logger.info("Web viewer started")
        
        # Send startup advert if enabled
        await self.send_startup_advert()
        
        # Start all loaded services
        for service_name, service_instance in self.services.items():
            try:
                await service_instance.start()
                self.logger.info(f"Service '{service_name}' started")
            except Exception as e:
                self.logger.error(f"Failed to start service '{service_name}': {e}")
        
        # Start command queue processor if needed
        if hasattr(self.command_manager, '_start_queue_processor'):
            self.command_manager._start_queue_processor()
        
        # Keep running
        self.logger.info("Bot is running. Press Ctrl+C to stop.")
        try:
            while self.connected and not self._shutdown_event.is_set():
                # Monitor web viewer process and health
                if self.web_viewer_integration and self.web_viewer_integration.enabled:
                    # Check if process died
                    if (self.web_viewer_integration and 
                        self.web_viewer_integration.viewer_process and 
                        self.web_viewer_integration.viewer_process.poll() is not None):
                        try:
                            self.logger.warning("Web viewer process died, restarting...")
                        except (AttributeError, TypeError):
                            print("Web viewer process died, restarting...")
                        self.web_viewer_integration.restart_viewer()
                    
                    # Simple health check for web viewer
                    if (self.web_viewer_integration and 
                        not self.web_viewer_integration.is_viewer_healthy()):
                        try:
                            self.logger.warning("Web viewer health check failed, restarting...")
                            self.web_viewer_integration.restart_viewer()
                        except (AttributeError, TypeError) as e:
                            print(f"Web viewer health check failed: {e}")
                
                # Periodically update system health in database (every 30 seconds)
                if not hasattr(self, '_last_health_update'):
                    self._last_health_update = 0
                if time.time() - self._last_health_update >= 30:
                    try:
                        await self.get_system_health()  # This stores it in the database
                        self._last_health_update = time.time()
                    except Exception as e:
                        self.logger.debug(f"Error updating system health: {e}")

                    # Service health check and restart
                    restart_backoff = self.config.getint(
                        'Bot', 'service_restart_backoff_seconds', fallback=300
                    )
                    now = time.time()
                    for name, service in self.services.items():
                        if not getattr(service, 'enabled', True):
                            continue
                        try:
                            healthy = service.is_healthy()
                        except Exception:
                            healthy = False
                        if healthy:
                            continue
                        if name in self._service_restarting:
                            continue
                        if name in self._service_restart_failures and (
                            now - self._service_restart_failures[name]
                        ) < restart_backoff:
                            continue
                        self.logger.warning(
                            f"Service '{name}' unhealthy, attempting restart..."
                        )
                        asyncio.create_task(self._restart_service(name, service))

                await asyncio.sleep(5)  # Check every 5 seconds
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """Stop the bot.
        
        Performs graceful shutdown by stopping services, scheduling, and
        disconnecting from the mesh node.
        """
        try:
            self.logger.info("Stopping MeshCore Bot...")
        except (AttributeError, TypeError):
            print("Stopping MeshCore Bot...")
        
        self.connected = False
        
        # Shutdown mesh graph first to flush pending writes
        if hasattr(self, 'mesh_graph') and self.mesh_graph:
            try:
                self.mesh_graph.shutdown()
            except Exception as e:
                self.logger.warning(f"Error shutting down mesh graph: {e}")
        
        # Stop feed manager
        if self.feed_manager:
            await self.feed_manager.stop()
        
        # Stop all loaded services
        for service_name, service_instance in self.services.items():
            try:
                await service_instance.stop()
                self.logger.info(f"Service '{service_name}' stopped")
            except Exception as e:
                self.logger.error(f"Failed to stop service '{service_name}': {e}")
        
        # Stop web viewer with proper shutdown sequence
        if self.web_viewer_integration:
            # Web viewer has simpler shutdown
            self.web_viewer_integration.stop_viewer()
            try:
                self.logger.info("Web viewer stopped")
            except (AttributeError, TypeError):
                print("Web viewer stopped")
        
        if self.meshcore:
            await self.meshcore.disconnect()
        
        try:
            self.logger.info("Bot stopped")
        except (AttributeError, TypeError):
            print("Bot stopped")

    async def _restart_service(self, service_name: str, service_instance: Any) -> bool:
        """Stop and start a service. Used when is_healthy() is False.
        Returns True on success, False on failure. Exceptions are caught and logged.
        """
        self._service_restarting.add(service_name)
        try:
            await service_instance.stop()
            await service_instance.start()
            self._service_restart_failures.pop(service_name, None)
            return True
        except Exception as e:
            self.logger.error(f"Failed to restart service '{service_name}': {e}")
            self._service_restart_failures[service_name] = time.time()
            return False
        finally:
            self._service_restarting.discard(service_name)

    async def get_system_health(self) -> Dict[str, Any]:
        """Aggregate health status from all components.
        
        Collects status information from the meshcore connection, database,
        services, and other components to provide a system health report.
        
        Returns:
            Dict[str, Any]: Dictionary containing overall health status and component details.
        """
        health = {
            'status': 'healthy',
            'timestamp': time.time(),
            'uptime_seconds': time.time() - self.start_time,
            'components': {}
        }
        
        # Check core connection
        health['components']['meshcore'] = {
            'healthy': self.connected and self.meshcore is not None,
            'message': 'Connected' if (self.connected and self.meshcore is not None) else 'Disconnected'
        }
        
        # Check database
        try:
            stats = self.db_manager.get_database_stats()
            health['components']['database'] = {
                'healthy': True,
                'entries': stats.get('geocoding_cache_entries', 0) + stats.get('generic_cache_entries', 0),
                'message': 'Operational'
            }
        except Exception as e:
            health['components']['database'] = {
                'healthy': False,
                'error': str(e),
                'message': f'Error: {str(e)}'
            }
        
        # Check services
        if hasattr(self, 'services') and self.services:
            for name, service in self.services.items():
                try:
                    is_healthy = service.is_healthy()
                    health['components'][f'service_{name}'] = {
                        'healthy': is_healthy,
                        'message': 'Running' if is_healthy else 'Stopped',
                        'enabled': getattr(service, 'enabled', True)
                    }
                except Exception as e:
                    health['components'][f'service_{name}'] = {
                        'healthy': False,
                        'error': str(e),
                        'message': f'Error: {str(e)}'
                    }
        
        # Check web viewer if available
        if hasattr(self, 'web_viewer_integration') and self.web_viewer_integration:
            try:
                is_healthy = self.web_viewer_integration.is_viewer_healthy() if hasattr(
                    self.web_viewer_integration, 'is_viewer_healthy'
                ) else True
                health['components']['web_viewer'] = {
                    'healthy': is_healthy,
                    'message': 'Operational' if is_healthy else 'Unhealthy'
                }
            except Exception as e:
                health['components']['web_viewer'] = {
                    'healthy': False,
                    'error': str(e),
                    'message': f'Error: {str(e)}'
                }
        
        # Determine overall status
        unhealthy = [
            k for k, v in health['components'].items()
            if not v.get('healthy', True)
        ]
        if unhealthy:
            if len(unhealthy) < len(health['components']):
                health['status'] = 'degraded'
            else:
                health['status'] = 'unhealthy'
        
        # Store health data in database for web viewer access
        try:
            self.db_manager.set_system_health(health)
        except Exception as e:
            self.logger.debug(f"Could not store system health in database: {e}")
        
        return health
    
    def _cleanup_web_viewer(self) -> None:
        """Cleanup web viewer resources on exit.
        
        Called by atexit handler to ensure the web viewer process is terminated
        properly when the bot shuts down.
        """
        try:
            if hasattr(self, 'web_viewer_integration') and self.web_viewer_integration:
                # Web viewer has simpler cleanup
                self.web_viewer_integration.stop_viewer()
                try:
                    self.logger.info("Web viewer cleanup completed")
                except (AttributeError, TypeError):
                    print("Web viewer cleanup completed")
        except (OSError, AttributeError, TypeError) as e:
            try:
                self.logger.error(f"Error during web viewer cleanup: {e}")
            except (AttributeError, TypeError):
                print(f"Error during web viewer cleanup: {e}")
    
    def _cleanup_mesh_graph(self) -> None:
        """Cleanup mesh graph resources on exit.
        
        Called by atexit handler to ensure graph state is persisted
        properly when the bot shuts down.
        """
        try:
            if hasattr(self, 'mesh_graph') and self.mesh_graph:
                self.mesh_graph.shutdown()
                try:
                    self.logger.info("Mesh graph cleanup completed")
                except (AttributeError, TypeError):
                    print("Mesh graph cleanup completed")
        except (OSError, AttributeError, TypeError) as e:
            try:
                self.logger.error(f"Error during mesh graph cleanup: {e}")
            except (AttributeError, TypeError):
                print(f"Error during mesh graph cleanup: {e}")
    
    async def send_startup_advert(self) -> None:
        """Send a startup advertisement if configured.
        
        Sends a 'bot online' status message to the mesh network. Can be configured
        as a local zero-hop broadcast or a flood message.
        """
        try:
            # Check if startup advert is enabled
            startup_advert = self.config.get('Bot', 'startup_advert', fallback='false').lower()
            if startup_advert == 'false':
                self.logger.debug("Startup advert disabled")
                return
            
            self.logger.info(f"Sending startup advert: {startup_advert}")
            
            # Add a small delay to ensure connection is fully established
            await asyncio.sleep(2)
            
            # Send the appropriate type of advert using meshcore.commands
            if startup_advert == 'zero-hop':
                self.logger.debug("Sending zero-hop advert")
                await self.meshcore.commands.send_advert(flood=False)
            elif startup_advert == 'flood':
                self.logger.debug("Sending flood advert")
                await self.meshcore.commands.send_advert(flood=True)
            else:
                self.logger.warning(f"Unknown startup_advert option: {startup_advert}")
                return
            
            # Update last advert time
            import time
            self.last_advert_time = time.time()
            
            self.logger.info(f"Startup {startup_advert} advert sent successfully")
                
        except (OSError, AttributeError, ValueError, RuntimeError) as e:
            self.logger.error(f"Error sending startup advert: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
