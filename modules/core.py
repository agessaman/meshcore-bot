#!/usr/bin/env python3
"""
Core MeshCore Bot functionality
Contains the main bot class and message processing logic
"""

import asyncio
import atexit
import configparser
import json
import logging
import signal
import sqlite3
import struct
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

import colorlog

# Import the official meshcore package
import meshcore
from meshcore import EventType

from .channel_manager import ChannelManager
from .command_manager import PUBLIC_CHANNEL_KEY_HEX, PUBLIC_CHANNEL_OVERRIDE_KEY, CommandManager
from .db_manager import AsyncDBManager, DBManager
from .feed_manager import FeedManager
from .i18n import Translator
from .message_handler import MessageHandler

# Import our modules
from .rate_limiter import BotTxRateLimiter, ChannelRateLimiter, NominatimRateLimiter, PerUserRateLimiter, RateLimiter
from .repeater_manager import RepeaterManager
from .scheduler import MessageScheduler
from .service_plugin_loader import ServicePluginLoader
from .solar_conditions import set_config
from .transmission_tracker import TransmissionTracker
from .utils import resolve_path
from .version_info import resolve_runtime_version
from .web_viewer.integration import WebViewerIntegration


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per line for log aggregation pipelines (Loki, Elasticsearch, etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))
        ms = int(record.msecs)
        obj: dict[str, Any] = {
            'timestamp': f'{ts}.{ms:03d}Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info:
            obj['exc_info'] = self.formatException(record.exc_info)
        if record.stack_info:
            obj['stack_info'] = self.formatStack(record.stack_info)
        return json.dumps(obj, ensure_ascii=False)


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

        # Volatile: DM-only admin command (channelpause) toggles this; not persisted across restarts.
        self.channel_responses_enabled = True

        # Bot start time for uptime tracking
        self.start_time = time.time()
        self.version_info = resolve_runtime_version(self.bot_root)
        self.bot_version = self.version_info.get("display", "unknown")
        self.logger.info(f"Bot version: {self.bot_version}")

        # Initialize database manager first (needed by plugins)
        db_path = self.config.get('Bot', 'db_path', fallback='meshcore_bot.db')

        # Resolve database path (relative paths resolved from bot root, absolute paths used as-is)
        db_path = resolve_path(db_path, self.bot_root)

        self.logger.info(f"Initializing database manager with database: {db_path}")
        try:
            self.db_manager = DBManager(self, db_path)
            self.async_db_manager = AsyncDBManager(str(db_path), self.logger)
            self.logger.info("Database manager initialized successfully")
        except (OSError, ValueError, sqlite3.Error) as e:
            self.logger.error(f"Failed to initialize database manager: {e}")
            raise

        # Set length of prefix
        self.prefix_bytes = self.config.getint("Bot", "prefix_bytes", fallback=1)
        self.prefix_hex_chars = self.prefix_bytes * 2
        self.logger.info(f"Prefix mode: {self.prefix_bytes} bytes ({self.prefix_hex_chars} hex chars)")

        # Store start time in database for web viewer access
        try:
            self.db_manager.set_bot_start_time(self.start_time)
            self.logger.info("Bot start time stored in database")
        except (OSError, sqlite3.Error, AttributeError) as e:
            self.logger.warning(f"Could not store start time in database: {e}")

        # Notify if Web_Viewer uses a different database (split-DB setup)
        if self.config.has_section('Web_Viewer') and self.config.has_option('Web_Viewer', 'db_path'):
            wv_raw = self.config.get('Web_Viewer', 'db_path').strip()
            if wv_raw:
                wv_path = Path(resolve_path(wv_raw, self.bot_root)).resolve()
                bot_path = Path(self.db_manager.db_path).resolve()
                if wv_path != bot_path:
                    self.logger.warning(
                        "Web viewer database path differs from bot database: viewer=%s, bot=%s. "
                        "For shared repeater/graph and packet stream data, set [Web_Viewer] db_path to the same as [Bot] db_path or remove it to use the bot database. See docs/web-viewer.md (migrating from a separate database).",
                        wv_path, bot_path
                    )

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
        # Per-user rate limiter: minimum seconds between replies to the same user (key = pubkey or name)
        self.per_user_rate_limit_enabled = self.config.getboolean(
            'Bot', 'per_user_rate_limit_enabled', fallback=True
        )
        self.per_user_rate_limiter = PerUserRateLimiter(
            seconds=self.config.getfloat('Bot', 'per_user_rate_limit_seconds', fallback=30.0),
            max_entries=1000
        )
        # Nominatim rate limiter: 1.1 seconds between requests (Nominatim policy: max 1 req/sec)
        self.nominatim_rate_limiter = NominatimRateLimiter(
            self.config.getfloat('Bot', 'nominatim_rate_limit_seconds', fallback=1.1)
        )
        # Per-channel rate limiter: loaded from [Rate_Limits] channel.<name>_seconds keys
        self.channel_rate_limiter = self._load_channel_rate_limiter()
        self.tx_delay_ms = self.config.getint('Bot', 'tx_delay_ms', fallback=250)

        # Initialize translator for localization BEFORE CommandManager
        # This ensures translated keywords are available when commands are loaded
        try:
            if self.config.has_section('Localization'):
                language = self.config.get('Localization', 'language', fallback='en')
                translation_path = self.config.get('Localization', 'translation_path', fallback='translations/')
            else:
                language = 'en'
                translation_path = 'translations/'
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

        # Callbacks invoked when the bot sends a channel message (e.g. Discord/Telegram bridges)
        self.channel_sent_listeners: list[Callable] = []

        self.scheduler = MessageScheduler(self)

        # Initialize feed manager
        self.logger.info("Initializing feed manager")
        try:
            self.feed_manager = FeedManager(self)
            self.logger.info("Feed manager initialized successfully")
        except (OSError, ValueError, AttributeError, ImportError, configparser.NoSectionError) as e:
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
            self.service_loader = ServicePluginLoader(
                self, local_services_dir=str(self._local_root / "service_plugins")
            )
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
            for _cmd_name, cmd_instance in self.command_manager.commands.items():
                if hasattr(cmd_instance, '_load_translated_keywords'):
                    cmd_instance._load_translated_keywords()

        # Advert tracking
        self.last_advert_time = None

        # Clock sync tracking
        self.last_clock_sync_time = None

        # Shutdown event for graceful shutdown
        self._shutdown_event = threading.Event()

        # Service plugin restart state (name -> timestamp of last failed restart)
        self._service_restart_failures: dict[str, float] = {}
        self._service_restarting: set = set()

    @property
    def bot_root(self) -> Path:
        """Get bot root directory (where config.ini is located)"""
        return Path(self.config_file).parent.resolve()

    @property
    def is_radio_zombie(self) -> bool:
        """True when the radio firmware has been confirmed unresponsive.

        All outbound radio sends should check this flag and abort immediately.
        Only a physical power cycle can recover the radio; the flag is cleared
        automatically when connect() succeeds after a power cycle.
        """
        return bool(getattr(self, '_radio_zombie_detected', False))

    @property
    def is_radio_offline(self) -> bool:
        """True when repeated outbound send timeouts have been detected.

        Distinct from zombie state — the radio may still be forwarding received
        packets but is not completing outbound sends.  Cleared automatically
        when a send succeeds, so no manual intervention is required.
        """
        return bool(getattr(self, '_radio_offline', False))

    def _record_send_failure(self, scheduler: "Any | None" = None) -> None:
        """Increment the consecutive-send-failure counter.

        Called by the scheduler when an outbound send times out at the
        ``future.result()`` level (i.e. the outer 60-second wall-clock
        timeout fired).  After ``radio_offline_threshold`` consecutive
        failures the bot transitions to radio-offline state, persists it
        to the DB for the web viewer banner, and optionally sends an alert
        email.
        """
        import datetime as _dt
        import threading as _threading

        self._send_consecutive_failures: int = (
            getattr(self, '_send_consecutive_failures', 0) + 1
        )
        threshold = self.config.getint('Bot', 'radio_offline_threshold', fallback=3)
        if self._send_consecutive_failures >= threshold and not self.is_radio_offline:
            self._radio_offline = True
            since = _dt.datetime.utcnow().isoformat()
            self.logger.critical(
                "RADIO OFFLINE: %d consecutive send timeouts (threshold %d). "
                "Bot will suppress further outbound sends until one succeeds. "
                "Check radio power and connection.",
                self._send_consecutive_failures,
                threshold,
            )
            try:
                self.db_manager.set_metadata('bot.radio_offline', 'true')
                self.db_manager.set_metadata('bot.radio_offline_since', since)
            except Exception:
                pass
            if scheduler is not None:
                _threading.Thread(
                    target=scheduler.send_radio_offline_alert_email,
                    args=(self._send_consecutive_failures, threshold),
                    daemon=True,
                ).start()

    def _record_send_success(self) -> None:
        """Clear the consecutive-send-failure counter after a successful send."""
        failures = getattr(self, '_send_consecutive_failures', 0)
        was_offline = self.is_radio_offline
        if failures > 0 or was_offline:
            self.logger.info(
                "Outbound send succeeded — clearing radio-offline state "
                "(was_offline=%s, failure_count=%d)",
                was_offline,
                failures,
            )
        self._send_consecutive_failures = 0
        if was_offline:
            self._radio_offline = False
            try:
                self.db_manager.set_metadata('bot.radio_offline', 'false')
                self.db_manager.set_metadata('bot.radio_offline_since', '')
            except Exception:
                pass

    def load_config(self) -> None:
        """Load configuration from file.

        Reads the configuration file specified in self.config_file. If the file
        does not exist, a default configuration is created first.
        """
        if not Path(self.config_file).exists():
            self.create_default_config()

        # Force UTF-8 so emoji and non-ASCII characters in config.ini parse on Windows.
        self.config.read(self.config_file, encoding="utf-8")
        # Resolve local plugins directory from [Bot] local_dir_path (default: local)
        self._local_root = Path(
            resolve_path(
                self.config.get("Bot", "local_dir_path", fallback="local"),
                self.bot_root,
            )
        )
        # Merge local config if present (user plugins/services settings)
        local_config = self._local_root / "config.ini"
        if local_config.exists():
            self.config.read(local_config, encoding="utf-8")

    def _get_radio_settings(self) -> dict[str, Any]:
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
            # radio_debug intentionally excluded — only needs a reconnect, not a full restart
        }

    def _load_channel_rate_limiter(self) -> ChannelRateLimiter:
        """Build a ChannelRateLimiter from [Rate_Limits] channel.<name>_seconds keys."""
        limits: dict[str, float] = {}
        if self.config.has_section('Rate_Limits'):
            for key, value in self.config.items('Rate_Limits'):
                if key.startswith('channel.') and key.endswith('_seconds'):
                    channel_name = key[len('channel.'):-len('_seconds')]
                    try:
                        # Normalize now; limiter will also normalize at use-time.
                        limits[channel_name.strip().lower()] = float(value)
                    except ValueError:
                        self.logger.warning(f"Invalid channel rate limit for {key}: {value!r}")
        return ChannelRateLimiter(limits)

    def reload_config(self) -> tuple[bool, str]:
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

            new_config.read(self.config_file, encoding="utf-8")

            # Get new radio settings
            new_radio_settings = {
                'connection_type': new_config.get('Connection', 'connection_type', fallback='ble').lower(),
                'serial_port': new_config.get('Connection', 'serial_port', fallback=''),
                'ble_device_name': new_config.get('Connection', 'ble_device_name', fallback=''),
                'hostname': new_config.get('Connection', 'hostname', fallback=''),
                'tcp_port': new_config.getint('Connection', 'tcp_port', fallback=5000),
                'timeout': new_config.getint('Connection', 'timeout', fallback=30),
                # radio_debug excluded — only needs a reconnect, not a full restart
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
            self.config.read(self.config_file, encoding="utf-8")
            local_config = self._local_root / "config.ini"
            if local_config.exists():
                self.config.read(local_config, encoding="utf-8")

            # Update rate limiters
            new_rate_limit = self.config.getint('Bot', 'rate_limit_seconds', fallback=10)
            self.rate_limiter = RateLimiter(new_rate_limit)

            new_bot_tx_rate_limit = self.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
            self.bot_tx_rate_limiter = BotTxRateLimiter(new_bot_tx_rate_limit)

            self.per_user_rate_limit_enabled = self.config.getboolean(
                'Bot', 'per_user_rate_limit_enabled', fallback=True
            )
            new_per_user_seconds = self.config.getfloat('Bot', 'per_user_rate_limit_seconds', fallback=30.0)
            self.per_user_rate_limiter = PerUserRateLimiter(seconds=new_per_user_seconds, max_entries=1000)

            new_nominatim_rate_limit = self.config.getfloat('Bot', 'nominatim_rate_limit_seconds', fallback=1.1)
            self.nominatim_rate_limiter = NominatimRateLimiter(new_nominatim_rate_limit)

            self.channel_rate_limiter = self._load_channel_rate_limiter()

            # Update transmission delay
            self.tx_delay_ms = self.config.getint('Bot', 'tx_delay_ms', fallback=250)

            # Update translator if language changed
            try:
                if self.config.has_section('Localization'):
                    new_language = self.config.get('Localization', 'language', fallback='en')
                    new_translation_path = self.config.get('Localization', 'translation_path', fallback='translations/')
                else:
                    new_language = 'en'
                    new_translation_path = 'translations/'
                if (not hasattr(self, 'translator') or
                    getattr(self.translator, 'language', None) != new_language or
                    getattr(self.translator, 'translation_path', None) != new_translation_path):
                    self.translator = Translator(new_language, new_translation_path)
                    self.logger.info(f"Translator reloaded with language: {new_language}")

                    # Reload translated keywords for all commands
                    if hasattr(self, 'command_manager'):
                        for _cmd_name, cmd_instance in self.command_manager.commands.items():
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
                self.command_manager.channel_keywords = self.command_manager.load_channel_keywords()
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

# Per-user rate limiting in seconds between replies to the same user
# Helps reduce airtime use from rapid repeated responses to one sender
per_user_rate_limit_seconds = 30

# Enable or disable per-user rate limiting
# true: Enforce per-user spacing (recommended)
# false: Disable per-user limiter
per_user_rate_limit_enabled = true

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
# {hops}: Total hop count only (e.g., "2" or "0"); same value as in path/connection_info
# {hops_label}: Same as hops with "hop"/"hops" and pluralization (e.g., "1 hop", "2 hops")
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

[Feed_Manager]
# Enable or disable RSS/API feed subscriptions
# true: Feed manager polls configured feeds and sends updates to channels
# false: Feed manager disabled (default)
feed_manager_enabled = false

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

[Joke_Command]
# Enable or disable the joke command (true/false)
enabled = true

# Enable seasonal joke defaults (October: spooky, December: Christmas)
# true: Seasonal defaults are applied (default)
# false: No seasonal defaults (always random)
seasonal_jokes = true

# Handle long jokes (over 130 characters)
# false: Fetch new jokes until we get a short one (default)
# true: Split long jokes into multiple messages
long_jokes = false

[DadJoke_Command]
# Enable or disable the dad joke command (true/false)
enabled = true

# Handle long jokes (over 130 characters)
# false: Fetch new jokes until we get a short one (default)
# true: Split long jokes into multiple messages
long_jokes = false

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
        If [Logging] section is missing, uses defaults (console/journal only, no file).
        """
        if self.config.has_section('Logging'):
            log_level = getattr(logging, self.config.get('Logging', 'log_level', fallback='INFO'))
            colored_output = self.config.getboolean('Logging', 'colored_output', fallback=True)
            log_file = self.config.get('Logging', 'log_file', fallback='meshcore_bot.log')
            meshcore_log_level = getattr(logging, self.config.get('Logging', 'meshcore_log_level', fallback='INFO'))
            json_logging = self.config.getboolean('Logging', 'json_logging', fallback=False)
        else:
            log_level = logging.INFO
            colored_output = True
            log_file = ''  # Console/journal only when no [Logging] section
            meshcore_log_level = logging.INFO
            json_logging = False

        # Create formatter
        if json_logging:
            formatter: logging.Formatter = _JsonFormatter()
        elif colored_output:
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

        # Normalize root logger early to avoid duplicate/basicConfig output from dependencies.
        # We intentionally keep root handlerless; modules that want logging should attach
        # handlers explicitly (MeshCoreBot and meshcore loggers below).
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(log_level)

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
                    log_max_bytes = self.config.getint('Logging', 'log_max_bytes', fallback=5 * 1024 * 1024)
                    log_backup_count = self.config.getint('Logging', 'log_backup_count', fallback=3)
                    file_handler = RotatingFileHandler(
                        log_file,
                        maxBytes=log_max_bytes,
                        backupCount=log_backup_count,
                        encoding='utf-8',
                    )
                    file_handler.setFormatter(formatter)
                    self.logger.addHandler(file_handler)
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"Could not open log file {log_file}: {e}. Using console logging only.")

        # Prevent propagation to root logger to avoid duplicate output
        self.logger.propagate = False

        # Save formatter for reuse (e.g. _configure_meshcore_debug_logging)
        self._log_formatter = formatter

        # Configure meshcore library logging (separate from bot logging)
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
            # Prevent duplicate output if root is later configured elsewhere.
            logger.propagate = False

        # Silence noisy third-party loggers that can emit unformatted console output.
        # APScheduler: keep INFO (but route through our formatter) and prevent propagation.
        # tzlocal: keep WARNING+ (it can be very chatty at DEBUG).
        apsched_logger_names = (
            "apscheduler",
            "apscheduler.scheduler",
            "apscheduler.executors",
            "apscheduler.jobstores",
        )
        for name in apsched_logger_names:
            third = logging.getLogger(name)
            third.handlers.clear()
            third.setLevel(logging.INFO)
            third.propagate = False
            if not third.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(formatter)
                third.addHandler(handler)

        tzlocal_logger = logging.getLogger("tzlocal")
        tzlocal_logger.handlers.clear()
        tzlocal_logger.setLevel(logging.WARNING)
        tzlocal_logger.propagate = False

        # Log the configuration for debugging
        mode = 'json' if json_logging else ('colored' if colored_output else 'plain')
        self.logger.info(f"Logging configured - Bot: {logging.getLevelName(log_level)}, MeshCore: {logging.getLevelName(meshcore_log_level)}, format: {mode}")

        # Setup routing info capture for web viewer
        self._setup_routing_capture()

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    def _configure_meshcore_debug_logging(self, enable: bool) -> None:
        """Route meshcore library output through the bot's handlers.

        When *enable* is True the meshcore loggers are set to DEBUG and share
        all of the bot's handlers (console + rotating file), so raw-protocol
        lines appear in the log file tagged as DEBUG.

        When *enable* is False the loggers revert to the ``meshcore_log_level``
        from config with a console-only StreamHandler (same as setup_logging).
        """
        meshcore_log_level = getattr(
            logging,
            self.config.get('Logging', 'meshcore_log_level', fallback='INFO')
            if self.config.has_section('Logging') else 'INFO',
        )
        level = logging.DEBUG if enable else meshcore_log_level
        loggers_to_configure = [
            'meshcore', 'meshcore_cli', 'meshcore.meshcore',
            'meshcore_cli.meshcore_cli', 'meshcore_cli.commands',
            'meshcore_cli.connection',
        ]
        formatter = getattr(self, '_log_formatter', None)
        for name in loggers_to_configure:
            mc_logger = logging.getLogger(name)
            mc_logger.setLevel(level)
            mc_logger.handlers.clear()
            mc_logger.propagate = False
            if enable:
                # Share the bot's handlers so debug output goes to the log file too
                for h in self.logger.handlers:
                    mc_logger.addHandler(h)
            else:
                # Revert to console-only StreamHandler (same as setup_logging baseline)
                h = logging.StreamHandler()
                if formatter:
                    h.setFormatter(formatter)
                mc_logger.addHandler(h)

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
        SIGHUP is intentionally handled by the process entrypoint so it can
        perform in-process config reload without triggering shutdown.
        """
        def signal_handler(signum, frame):
            self.logger.info(f"Received shutdown signal {signum}, initiating graceful shutdown...")
            # Set shutdown event to break main loop
            self._shutdown_event.set()
            # Set connected to False to break the while loop in start()
            self.connected = False

        # Register signal handlers
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    async def _attempt_reconnect(self) -> bool:
        """Attempt to reconnect to the MeshCore node with exponential backoff.

        Reads reconnect settings from [Connection]:
          reconnect_max_retries  – max attempts before giving up (0 = unlimited, default 0)
          reconnect_delay_seconds – initial wait between attempts (default 5)
          reconnect_max_delay_seconds – cap on wait time (default 60)

        Returns:
            bool: True if reconnection succeeded, False if max retries exhausted or shutdown.
        """
        max_retries = self.config.getint('Connection', 'reconnect_max_retries', fallback=0)
        delay = self.config.getfloat('Connection', 'reconnect_delay_seconds', fallback=5.0)
        max_delay = self.config.getfloat('Connection', 'reconnect_max_delay_seconds', fallback=60.0)

        attempt = 0
        while not self._shutdown_event.is_set():
            if max_retries > 0 and attempt >= max_retries:
                self.logger.error(f"Reconnect failed after {max_retries} attempt(s), giving up")
                return False

            attempt += 1
            retry_label = f"{attempt}/{max_retries}" if max_retries > 0 else str(attempt)
            self.logger.info(f"Reconnect attempt {retry_label}...")

            # Clean up the stale connection object
            old_meshcore = self.meshcore
            self.meshcore = None
            self.connected = False
            if old_meshcore is not None:
                try:
                    await asyncio.wait_for(old_meshcore.disconnect(), timeout=5.0)
                except Exception:
                    pass

            if await self.connect():
                self.logger.info("Reconnected successfully")
                if hasattr(self, 'transmission_tracker') and self.transmission_tracker:
                    self.transmission_tracker._update_bot_prefix()
                return True

            self.logger.warning(
                f"Reconnect attempt {retry_label} failed, retrying in {delay:.0f}s..."
            )
            # Interruptible sleep so shutdown isn't delayed
            elapsed = 0.0
            while elapsed < delay and not self._shutdown_event.is_set():
                await asyncio.sleep(1.0)
                elapsed += 1.0

            delay = min(delay * 2, max_delay)

        return False

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
            # radio_debug: config.ini baseline, overridden by bot_metadata (set via web UI)
            radio_debug = self.config.getboolean('Connection', 'radio_debug', fallback=False)
            try:
                meta_val = self.db_manager.get_metadata('radio.debug')
                if meta_val == 'true':
                    radio_debug = True
                elif meta_val == 'false':
                    radio_debug = False
            except Exception:
                pass
            self.logger.info(f"Using connection type: {connection_type}")
            if radio_debug:
                self.logger.info("Radio debug logging enabled — meshcore library output will be at DEBUG level")

            if connection_type == 'serial':
                # Create serial connection
                serial_port = self.config.get('Connection', 'serial_port', fallback='/dev/ttyUSB0')
                self.logger.info(f"Connecting via serial port: {serial_port}")
                self.meshcore = await meshcore.MeshCore.create_serial(serial_port, debug=radio_debug)
            elif connection_type == 'tcp':
                # Create TCP connection
                hostname = self.config.get('Connection', 'hostname', fallback=None)
                tcp_port = self.config.getint('Connection', 'tcp_port', fallback=5000)
                if not hostname:
                    self.logger.error("TCP connection requires 'hostname' to be set in config")
                    return False
                self.logger.info(f"Connecting via TCP: {hostname}:{tcp_port}")
                self.meshcore = await meshcore.MeshCore.create_tcp(hostname, tcp_port, debug=radio_debug)
            else:
                # Create BLE connection (default)
                ble_device_name = self.config.get('Connection', 'ble_device_name', fallback=None)
                self.logger.info("Connecting via BLE" + (f" to device: {ble_device_name}" if ble_device_name else ""))
                self.meshcore = await meshcore.MeshCore.create_ble(ble_device_name, debug=radio_debug)

            # Route meshcore library output through the bot's handlers (including log file)
            self._configure_meshcore_debug_logging(radio_debug)

            if self.meshcore.is_connected:
                self.connected = True
                self._update_radio_connected_metadata(True)
                # Track connection time to skip processing old cached messages
                self.connection_time = time.time()
                # Clear zombie state — a successful connect means the radio is alive again
                self._radio_zombie_detected = False
                self._radio_fail_count = 0
                try:
                    self.db_manager.set_metadata('bot.radio_zombie', 'false')
                    self.db_manager.set_metadata('bot.radio_zombie_since', '')
                except Exception:
                    pass
                self.logger.info(f"Connected to: {self.meshcore.self_info} at {self.connection_time}")

                # Wait for contacts to load
                await self.wait_for_contacts()

                # Fetch channels
                await self.channel_manager.fetch_channels()
                self._check_public_channel_guard()

                # Setup message event handlers
                await self.setup_message_handlers()

                # Set radio clock if needed
                await self.set_radio_clock()

                # Set device name to match config if needed
                await self.set_device_name()

                return True
            else:
                self.logger.error("Failed to connect to MeshCore node")
                return False

        except (OSError, ConnectionError, TimeoutError, ValueError, AttributeError) as e:
            self.logger.error(f"Connection failed: {e}")
            return False

    def _update_radio_connected_metadata(self, connected: bool) -> None:
        """Write radio connection state to bot_metadata for the web viewer."""
        try:
            self.db_manager.set_metadata('radio_connected', '1' if connected else '0')
        except Exception as e:
            self.logger.warning(f"Could not update radio_connected metadata: {e}")

    async def disconnect_radio(self) -> bool:
        """Disconnect from radio. Called by scheduler via operation queue."""
        import asyncio
        try:
            if self.meshcore:
                try:
                    await asyncio.wait_for(self.meshcore.disconnect(), timeout=10)
                except asyncio.TimeoutError:
                    self.logger.warning("Radio disconnect timed out after 10s — forcing disconnected state")
            self.connected = False
            self._update_radio_connected_metadata(False)
            self.logger.info("Radio disconnected via web viewer request")
            return True
        except Exception as e:
            self.logger.error(f"Error disconnecting radio: {e}")
            return False

    async def reboot_radio(self) -> bool:
        """Send firmware reboot command, disconnect, wait for reboot, then reconnect."""
        import asyncio
        try:
            if self.meshcore and self.meshcore.is_connected:
                self.logger.info("Sending firmware reboot command")
                try:
                    await asyncio.wait_for(self.meshcore.commands.reboot(), timeout=5)
                except (asyncio.TimeoutError, Exception) as e:
                    # Reboot command may drop the connection before a reply arrives
                    self.logger.debug(f"Reboot command response: {e} (expected on firmware reboot)")
            # Disconnect cleanly (firmware may have already dropped the link)
            try:
                if self.meshcore:
                    await asyncio.wait_for(self.meshcore.disconnect(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
            self.connected = False
            self._update_radio_connected_metadata(False)
            self.logger.info("Waiting for radio to reboot (8s)…")
            await asyncio.sleep(8)
            return await self.connect()
        except Exception as e:
            self.logger.error(f"Error rebooting radio: {e}")
            return False

    async def reconnect_radio(self) -> bool:
        """Disconnect then reconnect. Called by scheduler for connect ops."""
        import asyncio
        try:
            if self.meshcore:
                try:
                    await asyncio.wait_for(self.meshcore.disconnect(), timeout=10)
                except asyncio.TimeoutError:
                    self.logger.warning("Disconnect timed out during reconnect — proceeding")
            self.connected = False
            self._update_radio_connected_metadata(False)
            return await self.connect()
        except Exception as e:
            self.logger.error(f"Error reconnecting radio: {e}")
            return False

    def _check_public_channel_guard(self) -> None:
        """Refuse to run if any monitored channel's actual device key is the Public channel key.

        This is a second-layer check (after the name-based check in load_monitor_channels)
        that catches channels which have been renamed on the device but still use the Public key.
        """
        override = self.config.get("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, fallback="").strip().lower()
        if override == "true":
            return
        for ch_name in (self.command_manager.monitor_channels or []):
            num = self.channel_manager.get_channel_number(ch_name)
            if num is not None:
                key_hex = self.channel_manager.get_channel_key(num)
                if key_hex == PUBLIC_CHANNEL_KEY_HEX:
                    self.logger.error(
                        f"FATAL: Channel '{ch_name}' (slot {num}) has the Public channel key. "
                        "Running a bot on Public is disruptive to other mesh users. "
                        f"To override, add to [Bot]: {PUBLIC_CHANNEL_OVERRIDE_KEY} = true"
                    )
                    raise SystemExit(1)

    async def _probe_radio_health(self) -> bool:
        """Send a lightweight get_time() probe to verify the radio is responding.

        A connected serial transport does not guarantee the firmware is alive and
        processing commands.  This probe detects the 'zombie connection' state
        where the port is open and messages are received but all outgoing commands
        time out with no_event_received.

        When the configured fail threshold is reached the bot logs a CRITICAL
        message and sends an immediate alert email (if enabled).  It does NOT
        attempt to reconnect — a zombie radio requires a physical power cycle;
        disconnect/reconnect of the transport does nothing.  Probing stops once
        a zombie is confirmed to avoid log spam; it resumes automatically after
        the next successful connect() call.

        Returns True if the device responded, False otherwise.
        """
        # Stop probing once a zombie has been confirmed — only a power cycle
        # can recover it; further probes just generate noise.
        if getattr(self, '_radio_zombie_detected', False):
            return False

        if not self.meshcore or not self.meshcore.is_connected:
            return False
        try:
            from meshcore.events import EventType
            result = await asyncio.wait_for(
                self.meshcore.commands.get_time(), timeout=10.0
            )
            if result.type == EventType.ERROR:
                self._radio_fail_count = getattr(self, '_radio_fail_count', 0) + 1
                threshold = self.config.getint('Bot', 'radio_probe_fail_threshold', fallback=3)
                interval  = max(300, min(900, self.config.getint(
                    'Bot', 'radio_probe_interval_seconds', fallback=300
                )))
                self.logger.warning(
                    f"Radio health probe failed "
                    f"({self._radio_fail_count}/{threshold}): no response to get_time"
                )
                if self._radio_fail_count >= threshold:
                    fail_count = self._radio_fail_count
                    self._radio_fail_count = 0
                    self._radio_zombie_detected = True
                    self.logger.critical(
                        "ZOMBIE RADIO DETECTED after %d consecutive failed probes "
                        "(probe interval %ds). The radio firmware is unresponsive. "
                        "A physical POWER CYCLE is required — disconnect/reconnect "
                        "will NOT fix this. Probing suspended until next reconnect.",
                        fail_count, interval,
                    )
                    # Persist zombie state to db so the web viewer health API reflects it
                    try:
                        import datetime as _dt
                        self.db_manager.set_metadata('bot.radio_zombie', 'true')
                        self.db_manager.set_metadata(
                            'bot.radio_zombie_since',
                            _dt.datetime.utcnow().isoformat(),
                        )
                    except Exception:
                        pass
                    # Send immediate alert email via scheduler (non-blocking)
                    scheduler = getattr(self, 'scheduler', None)
                    if scheduler is not None:
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(
                            None,
                            scheduler.send_zombie_alert_email,
                            fail_count, threshold, interval,
                        )
                return False
            if getattr(self, '_radio_fail_count', 0) > 0:
                self.logger.info("Radio health probe recovered — resetting fail counter")
            self._radio_fail_count = 0
            return True
        except asyncio.TimeoutError:
            self.logger.warning("Radio health probe timed out")
            return False
        except Exception as e:
            self.logger.warning(f"Radio health probe error: {e}")
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

    async def set_device_name(self) -> bool:
        """Set device name to match bot_name from config if they differ.

        Checks the connected device's name and updates it to match the bot_name
        from config.ini if they differ. This ensures the device name matches the
        configured bot name before any adverts are sent.

        Returns:
            bool: True if check/update was successful (or not needed), False on error.
        """
        try:
            if not self.meshcore or not self.meshcore.is_connected:
                self.logger.warning("Cannot set device name - not connected to device")
                return False

            # Check if device name updates are enabled
            auto_update_name = self.config.getboolean('Bot', 'auto_update_device_name', fallback=True)
            if not auto_update_name:
                self.logger.debug("auto_update_device_name is disabled, skipping device name update")
                return True

            # Get desired name from config
            desired_name = self.config.get('Bot', 'bot_name', fallback=None)
            if not desired_name or desired_name.strip() == '':
                self.logger.debug("bot_name not set in config, skipping device name update")
                return True

            # Get current device name
            self.logger.info("Checking device name...")
            current_name = None

            try:
                if hasattr(self.meshcore, 'self_info') and self.meshcore.self_info:
                    self_info = self.meshcore.self_info
                    # Try to get name from self_info (could be dict or object)
                    if isinstance(self_info, dict):
                        current_name = self_info.get('name') or self_info.get('adv_name')
                    elif hasattr(self_info, 'name'):
                        current_name = self_info.name
                    elif hasattr(self_info, 'adv_name'):
                        current_name = self_info.adv_name
            except Exception as e:
                self.logger.debug(f"Could not get current device name: {e}")

            if current_name == desired_name:
                self.logger.info(f"Device name already matches config: '{desired_name}'")
                return True

            self.logger.info(f"Device name: '{current_name}', Config name: '{desired_name}'")
            self.logger.info("Updating device name to match config...")

            # Check if set_name command is available
            if not hasattr(self.meshcore, 'commands') or not hasattr(self.meshcore.commands, 'set_name'):
                self.logger.warning("Device does not support set_name command")
                return False

            # Set the device name
            result = await self.meshcore.commands.set_name(desired_name)
            if result.type == EventType.OK:
                self.logger.info(f"✓ Device name updated to: '{desired_name}'")
                return True
            else:
                self.logger.warning(f"Failed to update device name: {result.payload if hasattr(result, 'payload') else result}")
                return False

        except (OSError, AttributeError, ValueError, KeyError) as e:
            self.logger.warning(f"Error checking/setting device name: {e}")
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

        # Suppress noisy "Task exception was never retrieved" warnings that
        # originate from malformed/truncated MeshCore packets.  IndexError and
        # struct.error are raised deep inside meshcore_parser.parsePacketPayload
        # and are not programming errors we can fix on this side.
        _default_handler = self.main_event_loop.get_exception_handler()

        def _loop_exception_handler(
            loop: asyncio.AbstractEventLoop,
            context: dict[str, Any],
        ) -> None:
            exc = context.get("exception")
            if isinstance(exc, (IndexError, struct.error)):
                self.logger.debug(
                    "Suppressed meshcore parser exception in asyncio task: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return
            if _default_handler is not None:
                _default_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        self.main_event_loop.set_exception_handler(_loop_exception_handler)

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
                # Check if the underlying connection dropped
                if self.meshcore and not self.meshcore.is_connected:
                    self.logger.warning("Connection lost, attempting to reconnect...")
                    if not await self._attempt_reconnect():
                        self.logger.error("Could not reconnect, shutting down")
                        break
                    continue

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

                # Periodically probe radio responsiveness
                # Skip entirely once a zombie is confirmed — only a power cycle
                # can recover the firmware; probing just generates log noise.
                if not getattr(self, '_radio_zombie_detected', False):
                    if not hasattr(self, '_last_radio_probe'):
                        self._last_radio_probe = time.time()
                    probe_interval = max(300, min(900, self.config.getint(
                        'Bot', 'radio_probe_interval_seconds', fallback=300
                    )))
                    if time.time() - self._last_radio_probe >= probe_interval:
                        self._last_radio_probe = time.time()
                        asyncio.create_task(self._probe_radio_health())

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
        self._update_radio_connected_metadata(False)

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

        # Wait for scheduler thread to exit (it checks self.connected)
        if hasattr(self, 'scheduler') and self.scheduler and self.scheduler.scheduler_thread:
            self.scheduler.join(timeout=5.0)

        if self.meshcore:
            disconnect_timeout = self.config.getfloat('Bot', 'disconnect_timeout_seconds', fallback=10.0)
            try:
                await asyncio.wait_for(self.meshcore.disconnect(), timeout=disconnect_timeout)
            except asyncio.TimeoutError:
                self.logger.warning(
                    "MeshCore disconnect timed out after %.1fs; continuing shutdown",
                    disconnect_timeout,
                )
            except Exception as e:
                self.logger.warning("Error during meshcore disconnect: %s", e)

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

    async def get_system_health(self) -> dict[str, Any]:
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
                self.web_viewer_integration.stop_viewer()
        except (OSError, AttributeError, TypeError, ValueError):
            pass  # Do not log; stream may be closed during atexit

    def _cleanup_mesh_graph(self) -> None:
        """Cleanup mesh graph resources on exit.

        Called by atexit handler to ensure graph state is persisted
        properly when the bot shuts down.
        """
        try:
            if hasattr(self, 'mesh_graph') and self.mesh_graph:
                self.mesh_graph.shutdown()
        except (OSError, AttributeError, TypeError, ValueError):
            pass  # Do not log; stream may be closed during atexit

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

    def key_prefix(self, public_key: str) -> str:
        return public_key[:self.prefix_hex_chars]

    def is_valid_prefix(self, prefix: str) -> bool:
        return len(prefix) == self.prefix_hex_chars
