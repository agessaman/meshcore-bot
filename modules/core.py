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
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Import the official meshcore package
import meshcore  # noqa: F401  (tests patch modules.core.meshcore.MeshCore)
from meshcore import EventType

from .admin_server import BotAdminServer
from .channel_manager import ChannelManager
from .command_manager import CommandManager
from .db_manager import AsyncDBManager, DBManager
from .feed_manager import FeedManager
from .i18n import Translator
from .logging_setup import configure_bot_logging, configure_meshcore_loggers, meshcore_log_level
from .message_handler import MessageHandler
from .radio_link import RadioLinkMixin, _radio_session_held, _serialize_command_frames  # noqa: F401
from .radio_offline import RadioOfflineBreaker

# Import our modules
from .rate_limiter import BotTxRateLimiter, ChannelRateLimiter, NominatimRateLimiter, PerUserRateLimiter, RateLimiter
from .repeater_manager import RepeaterManager
from .scheduler import MessageScheduler
from .service_plugin_loader import ServicePluginLoader
from .solar_conditions import set_config
from .transmission_tracker import TransmissionTracker
from .utils import resolve_path
from .web_viewer.integration import WebViewerIntegration


class MeshCoreBot(RadioLinkMixin, RadioOfflineBreaker):
    """MeshCore Bot using official meshcore package.

    This class handles the core functionality of the bot, including connection management,
    message processing initialization, and module coordination.
    """

    def __init__(self, config_file: str = "config.ini"):
        self.config_file = config_file
        # Reload writers are serialized, while readers rely on the atomic
        # replacement of this parser reference.  Never mutate a published
        # ConfigParser in place: readers do not take this lock.
        self._config_reload_lock = threading.RLock()
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
            self.logger.error("Web viewer integration failed: %s", e)
            self.web_viewer_integration = None

        # Admin HTTP server (optional — [Admin] section)
        self._admin_server: BotAdminServer | None = None
        if self.config.getboolean('Admin', 'enabled', fallback=False):
            admin_port = self.config.getint('Admin', 'port', fallback=5001)
            admin_token = self.config.get('Admin', 'token', fallback='')
            if admin_token:
                self._admin_server = BotAdminServer(self, admin_port, admin_token)
            else:
                self.logger.warning("Admin server enabled but no token configured — skipping")

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
            seconds=self.config.getfloat('Bot', 'per_user_rate_limit_seconds', fallback=5.0),
            max_entries=1000
        )
        # Nominatim rate limiter: 1.1 seconds between requests (Nominatim policy: max 1 req/sec)
        self.nominatim_rate_limiter = NominatimRateLimiter(
            self.config.getfloat('Bot', 'nominatim_rate_limit_seconds', fallback=1.1)
        )
        # Per-channel rate limiter: loaded from [Rate_Limits] channel.<name>_seconds keys
        self.channel_rate_limiter = self._load_channel_rate_limiter()
        self.tx_delay_ms = self.config.getint('Bot', 'tx_delay_ms', fallback=250)

        # Radio health, set before any command or service plugin is built, since
        # their constructors may read is_radio_offline / is_radio_zombie. Zombie: the firmware stopped acting on commands and only a
        # power cycle recovers it. Offline: repeated send timeouts. The probe
        # timestamp starts on the first health-loop pass, hence None until then.
        self._radio_zombie_detected = False
        self._radio_fail_count = 0
        self._tcp_probe_fail_count = 0
        self._radio_offline = False
        self._send_consecutive_failures = 0
        self._last_radio_probe: float | None = None
        self._last_health_update = 0.0

        # Initialize translator for localization BEFORE CommandManager
        # This ensures translated keywords are available when commands are loaded
        try:
            default_local_translations = self._default_local_translation_path(self.config)
            if self.config.has_section('Localization'):
                language = self.config.get('Localization', 'language', fallback='en')
                translation_path = self.config.get('Localization', 'translation_path', fallback='translations/')
                local_translation_path = self.config.get(
                    'Localization', 'local_translation_path', fallback=default_local_translations
                )
            else:
                language = 'en'
                translation_path = 'translations/'
                local_translation_path = default_local_translations
            self.translation_path = translation_path
            self.local_translation_path = local_translation_path
            self._translator_cache: dict[str, Any] = {}
            self.translator = Translator(language, translation_path, local_translation_path)
            self._translator_cache[language] = self.translator
            self.logger.info(f"Localization initialized: {language}")
        except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
            self.logger.warning(f"Failed to initialize translator: {e}")
            # Create a dummy translator that just returns keys
            class DummyTranslator:
                language = 'en'
                base_language = 'en'

                def translate(self, key, **kwargs):
                    return key

                def get_value(self, key):
                    return None

                def get_available_languages(self):
                    return []

            self.translator = DummyTranslator()
            self.translation_path = 'translations/'
            # get_translator() reads both paths when it builds a per-language
            # translator, so neither may be left unset here.
            self.local_translation_path = 'local/translations/'
            self._translator_cache = {}

        # Initialize solar conditions configuration
        set_config(self.config)

        self.message_handler = MessageHandler(self)
        self.command_manager = CommandManager(self)

        # Regional flood-scope tallies, and the opt-in warning they can drive.
        try:
            from .region_warning import RegionWarningMonitor
            self.region_warning_monitor = RegionWarningMonitor(self)
        except Exception as e:
            self.logger.warning(f"Failed to initialize region warning monitor: {e}")
            self.region_warning_monitor = None

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

        # Shutdown event for graceful shutdown
        self._shutdown_event = threading.Event()

        # Idempotent async shutdown (see stop()); lock created when event loop is available
        self._shutdown_lock: asyncio.Lock | None = None
        self._shutdown_complete = False

        # Service plugin restart state (name -> timestamp of last failed restart)
        self._service_restart_failures: dict[str, float] = {}
        self._service_restarting: set = set()

        # Transport reconnect (serial/BLE/TCP) — lock created when event loop runs
        self._transport_reconnect_lock: asyncio.Lock | None = None
        self._transport_reconnect_in_progress = False
        # Web-viewer reboot/reconnect ops in flight (a count, since they can overlap)
        self._radio_relinks_in_progress = 0


        # Serialize host->radio commands: one companion frame in flight at a
        # time, with a minimum inter-command gap so the firmware's single
        # serial loop can drain its RX buffer between frames. Prevents the
        # USB-CDC overrun / parser-desync failure mode. Lock is created lazily
        # once an event loop is running (see _get_radio_cmd_lock).
        self._radio_cmd_lock: asyncio.Lock | None = None
        self._radio_cmd_last_ts: float = 0.0
        self._radio_cmd_min_interval = max(
            0.0,
            self.config.getfloat(
                'Connection', 'command_min_interval_ms', fallback=30.0
            ) / 1000.0,
        )

    def _default_local_translation_path(self, config: configparser.ConfigParser) -> str:
        """Default local catalog directory: ``<local_dir_path>/translations``.

        ``local_dir_path`` already selects where an operator's own commands, service
        plugins and config overlay live, so the local translation catalog belongs in
        that same tree rather than in a second, separately-configured location. The
        result is absolute, so it does not depend on the process's cwd.
        """
        local_dir = config.get('Bot', 'local_dir_path', fallback='local')
        return str(Path(resolve_path(local_dir, self.bot_root)) / 'translations')

    @property
    def bot_root(self) -> Path:
        """Get bot root directory (where config.ini is located)"""
        return Path(self.config_file).parent.resolve()


    @property
    def keep_running(self) -> bool:
        """True while the main loop and scheduler thread should stay alive.

        ``connected`` alone is not enough: it drops to False while a transport
        reconnect or a web-viewer reboot/reconnect re-establishes the link, and
        treating that window as a stop kills the bot on every transport blip.
        A reconnect that gives up leaves ``connected`` False and clears its
        in-progress flag, which still ends the loops.
        """
        if self._shutdown_event.is_set():
            return False
        return bool(
            self.connected
            or self._transport_reconnect_in_progress
            or self._radio_relinks_in_progress
        )

    def load_config(self) -> None:
        """Load configuration from file.

        Reads the configuration file specified in self.config_file. If the file
        does not exist, a default configuration is created first.
        """
        if not Path(self.config_file).exists():
            self.create_default_config()

        self.config, self._local_root = self._read_config_snapshot()

    def _read_config_snapshot(self) -> tuple[configparser.ConfigParser, Path]:
        """Read base and local overlay into a new, unpublished parser.

        ``ConfigParser.read`` mutates its receiver.  Keeping that receiver
        private until both files have parsed guarantees that concurrent readers
        only ever observe a complete old or complete new snapshot.
        """
        snapshot = configparser.ConfigParser()
        loaded = snapshot.read(self.config_file, encoding="utf-8")
        if not loaded:
            raise FileNotFoundError(self.config_file)

        # The overlay location is selected by the base file.  An overlay cannot
        # silently relocate itself midway through the same read operation.
        local_root = Path(
            resolve_path(
                snapshot.get("Bot", "local_dir_path", fallback="local"),
                self.bot_root,
            )
        )
        local_config = local_root / "config.ini"
        if local_config.exists():
            snapshot.read(local_config, encoding="utf-8")
        return snapshot, local_root

    def _get_radio_settings(
        self, config: configparser.ConfigParser | None = None
    ) -> dict[str, Any]:
        """Get current radio/connection settings from config.

        Returns:
            Dict[str, Any]: Dictionary containing all radio-related settings.
        """
        source = config if config is not None else self.config
        return {
            'connection_type': source.get('Connection', 'connection_type', fallback='ble').lower(),
            'serial_port': source.get('Connection', 'serial_port', fallback=''),
            'ble_device_name': source.get('Connection', 'ble_device_name', fallback=''),
            'hostname': source.get('Connection', 'hostname', fallback=''),
            'tcp_port': source.getint('Connection', 'tcp_port', fallback=5000),
            'timeout': source.getint('Connection', 'timeout', fallback=30),
            # radio_debug intentionally excluded — only needs a reconnect, not a full restart
        }

    def _load_channel_rate_limiter(
        self, config: configparser.ConfigParser | None = None
    ) -> ChannelRateLimiter:
        """Build a ChannelRateLimiter from [Rate_Limits] channel.<name>_seconds keys."""
        limits: dict[str, float] = {}
        source = config if config is not None else self.config
        if source.has_section('Rate_Limits'):
            for key, value in source.items('Rate_Limits'):
                if key.startswith('channel.') and key.endswith('_seconds'):
                    channel_name = key[len('channel.'):-len('_seconds')]
                    try:
                        # Normalize now; limiter will also normalize at use-time.
                        limits[channel_name.strip().lower()] = float(value)
                    except ValueError:
                        self.logger.warning(f"Invalid channel rate limit for {key}: {value!r}")
        return ChannelRateLimiter(limits)

    def _available_translation_codes(self) -> set[str]:
        """Return concrete locale codes from filesystem or bundled catalogs."""
        try:
            return {
                code
                for code in self.translator.get_available_languages()
                if code
            }
        except (AttributeError, OSError) as e:
            self.logger.debug("Could not enumerate translation files: %s", e)
            return set()

    def get_translator(self, language: str) -> Any:
        """Return a cached translator without changing the bot-wide default."""
        if not language:
            return self.translator
        available_codes = self._available_translation_codes()
        resolved_language = language
        if language not in available_codes:
            locale_matches = sorted(
                code
                for code in available_codes
                if code.replace("_", "-").split("-", 1)[0] == language
            )
            if locale_matches:
                resolved_language = locale_matches[0]
        cached = self._translator_cache.get(resolved_language)
        if cached is not None:
            return cached
        try:
            translator = Translator(
                resolved_language, self.translation_path, self.local_translation_path
            )
        except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
            self.logger.warning(
                "Failed to build translator for %r: %s", resolved_language, e
            )
            return self.translator
        self._translator_cache[resolved_language] = translator
        return translator

    def available_languages(self) -> set[str]:
        """Return detectable base languages from filesystem or bundled catalogs."""
        available = {
            language.replace("_", "-").split("-", 1)[0]
            for language in self._available_translation_codes()
            if language
        }
        available.add(
            getattr(self.translator, "base_language", None)
            or getattr(self.translator, "language", "en")
        )
        return available

    @staticmethod
    def _config_section_values(
        config: configparser.ConfigParser, section: str
    ) -> dict[str, str]:
        if not config.has_section(section):
            return {}
        return dict(config.items(section, raw=True))

    def _restart_only_config_changes(
        self,
        old_config: configparser.ConfigParser,
        new_config: configparser.ConfigParser,
    ) -> list[str]:
        """Return changed settings whose owning component is startup-only.

        Service plugins commonly cache constructor settings.  Until they expose
        a transactional reload contract, rejecting those edits is safer than a
        split state where on-demand reads see new values and cached fields do not.
        """
        changed: list[str] = []
        # Built-in services may currently be disabled and therefore absent from
        # ``self.services``.  Their enable flags and constructor-cached settings
        # are still restart-only; otherwise a successful reload would claim to
        # start a service that was never instantiated.
        startup_sections = {
            "Admin",
            "Connection",
            "DARC_MoWaS_Service",
            "DiscordBridge",
            "Earthquake_Service",
            "Feed_Manager",
            "Logging",
            "MapUploader",
            "MqttWeather",
            "PacketCapture",
            "RepeaterPrefixCollision_Service",
            "Service_Overrides",
            "TelegramBridge",
            "Weather_Service",
            "Webhook",
            "Web_Viewer",
            "Worldcup_Service",
        }
        for service in getattr(self, "services", {}).values():
            section = getattr(service, "config_section", None)
            if not section:
                derive = getattr(service, "_derive_config_section", None)
                if callable(derive):
                    section = derive()
            if isinstance(section, str) and section:
                startup_sections.add(section)
            if service.__class__.__name__ == "WeatherService":
                # WeatherService also caches units from the shared [Weather]
                # section during construction.
                startup_sections.add("Weather")

        for section in sorted(startup_sections):
            if self._config_section_values(old_config, section) != self._config_section_values(
                new_config, section
            ):
                changed.append(f"[{section}]")

        for key in ("db_path", "local_dir_path", "prefix_bytes"):
            old_value = old_config.get("Bot", key, fallback="")
            new_value = new_config.get("Bot", key, fallback="")
            if old_value != new_value:
                changed.append(f"[Bot] {key}")
        return changed

    @staticmethod
    def _validate_config_snapshot(config: configparser.ConfigParser) -> None:
        """Validate the fully merged candidate before it can be published."""
        from .config_schema import SECTIONS
        from .config_validation import REQUIRED_SECTIONS

        missing = sorted(REQUIRED_SECTIONS - set(config.sections()))
        if missing:
            raise ValueError(
                "Missing required configuration section(s): " + ", ".join(missing)
            )

        # Expand interpolation across every final value, not just the base file.
        # This catches malformed '%' expressions in an overlay before publish.
        for section in config.sections():
            list(config.items(section))

        # Validate all typed keys currently covered by the project schema.
        for section, section_meta in SECTIONS.items():
            if not config.has_section(section):
                continue
            for key, meta in section_meta.keys.items():
                if not config.has_option(section, key):
                    continue
                if meta.type == "int":
                    config.getint(section, key)
                elif meta.type == "bool":
                    config.getboolean(section, key)
                elif meta.type == "enum" and meta.values:
                    value = config.get(section, key).strip().lower()
                    if value not in meta.values:
                        raise ValueError(
                            f"[{section}] {key} must be one of "
                            f"{', '.join(meta.values)} (got {value!r})"
                        )

    _COMMAND_CONFIG_STATE = (
        "keywords",
        "custom_syntax",
        "banned_users",
        "monitor_channels",
        "channel_keywords",
        "command_prefixes",
        "require_command_prefix",
        "_command_prefix_default",
        "flood_scope_allow_global",
        "flood_scope_keys",
        "plugin_loader",
        "commands",
    )

    @classmethod
    def _command_config_state(cls, manager: CommandManager) -> dict[str, Any]:
        return {name: getattr(manager, name) for name in cls._COMMAND_CONFIG_STATE}

    @classmethod
    def _apply_command_config_state(
        cls, manager: CommandManager, state: dict[str, Any]
    ) -> None:
        for name in cls._COMMAND_CONFIG_STATE:
            setattr(manager, name, state[name])

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
            with self._config_reload_lock:
                old_config = self.config
                if not Path(self.config_file).exists():
                    return (False, "Config file not found")
                new_config, new_local_root = self._read_config_snapshot()
                self._validate_config_snapshot(new_config)

                old_radio_settings = self._get_radio_settings(old_config)
                new_radio_settings = self._get_radio_settings(new_config)
                if old_radio_settings != new_radio_settings:
                    changed_settings = [
                        f"{key}: {old_radio_settings[key]} -> {new_radio_settings[key]}"
                        for key in old_radio_settings
                        if old_radio_settings[key] != new_radio_settings[key]
                    ]
                    return (
                        False,
                        "Radio settings changed. Restart required. Changes: "
                        + ", ".join(changed_settings),
                    )

                restart_changes = self._restart_only_config_changes(old_config, new_config)
                if restart_changes:
                    return (
                        False,
                        "Startup-only or cached service settings changed. Restart required: "
                        + ", ".join(restart_changes),
                    )

                # Typed reads validate the final merged snapshot and prepare all
                # independent replacements before the live reference is changed.
                new_rate_limiter = RateLimiter(
                    new_config.getint('Bot', 'rate_limit_seconds', fallback=10)
                )
                new_bot_tx_rate_limiter = BotTxRateLimiter(
                    new_config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
                )
                new_per_user_enabled = new_config.getboolean(
                    'Bot', 'per_user_rate_limit_enabled', fallback=True
                )
                new_per_user_rate_limiter = PerUserRateLimiter(
                    seconds=new_config.getfloat(
                        'Bot', 'per_user_rate_limit_seconds', fallback=5.0
                    ),
                    max_entries=1000,
                )
                new_nominatim_rate_limiter = NominatimRateLimiter(
                    new_config.getfloat(
                        'Bot', 'nominatim_rate_limit_seconds', fallback=1.1
                    )
                )
                new_channel_rate_limiter = self._load_channel_rate_limiter(new_config)
                new_tx_delay_ms = new_config.getint('Bot', 'tx_delay_ms', fallback=250)
                new_max_channels = new_config.getint('Bot', 'max_channels', fallback=40)

                new_language = new_config.get('Localization', 'language', fallback='en')
                new_translation_path = new_config.get(
                    'Localization', 'translation_path', fallback='translations/'
                )
                new_local_translation_path = new_config.get(
                    'Localization',
                    'local_translation_path',
                    fallback=self._default_local_translation_path(new_config),
                )
                new_translator = Translator(
                    new_language, new_translation_path, new_local_translation_path
                )
                new_translator_cache = {new_language: new_translator}

                candidate = {
                    "config": new_config,
                    "_local_root": new_local_root,
                    "rate_limiter": new_rate_limiter,
                    "bot_tx_rate_limiter": new_bot_tx_rate_limiter,
                    "per_user_rate_limit_enabled": new_per_user_enabled,
                    "per_user_rate_limiter": new_per_user_rate_limiter,
                    "nominatim_rate_limiter": new_nominatim_rate_limiter,
                    "channel_rate_limiter": new_channel_rate_limiter,
                    "tx_delay_ms": new_tx_delay_ms,
                    "translation_path": new_translation_path,
                    "local_translation_path": new_local_translation_path,
                    "_translator_cache": new_translator_cache,
                    "translator": new_translator,
                }
                old_state = {name: getattr(self, name) for name in candidate}
                old_command_config_state = self._command_config_state(self.command_manager)
                old_max_channels = self.channel_manager.max_channels

                scheduler_apply_started = False
                try:
                    # Atomic complete-snapshot publication.  Component reference
                    # swaps follow under the single-writer lock and are all
                    # restored if any component rejects the candidate.
                    for name, value in candidate.items():
                        setattr(self, name, value)
                    # Commands and nested delegates require the real bot. They
                    # are therefore constructed after candidate publication,
                    # inside the rollback boundary, rather than against a
                    # facade that could leak into nested objects.
                    new_command_manager = CommandManager(self)
                    old_plugin_failures = (
                        self.command_manager.plugin_loader.get_failed_plugins()
                    )
                    new_plugin_failures = (
                        new_command_manager.plugin_loader.get_failed_plugins()
                    )
                    introduced_failures = {
                        name: reason
                        for name, reason in new_plugin_failures.items()
                        if old_plugin_failures.get(name) != reason
                    }
                    if introduced_failures:
                        names = ", ".join(sorted(introduced_failures))
                        raise ValueError(
                            f"Command plugin reload failed for: {names}"
                        )
                    self._apply_command_config_state(
                        self.command_manager,
                        self._command_config_state(new_command_manager),
                    )
                    self.channel_manager.max_channels = new_max_channels
                    set_config(new_config)

                    if getattr(self, 'region_warning_monitor', None):
                        self.region_warning_monitor.reload_config()

                    if hasattr(self, 'scheduler'):
                        scheduler_apply_started = True
                        self.scheduler.setup_scheduled_messages()
                        self.logger.info("Scheduler config reloaded")
                except (Exception, SystemExit):
                    for name, value in old_state.items():
                        setattr(self, name, value)
                    self._apply_command_config_state(self.command_manager, old_command_config_state)
                    self.channel_manager.max_channels = old_max_channels
                    set_config(old_config)
                    if getattr(self, 'region_warning_monitor', None):
                        self.region_warning_monitor.reload_config()
                    # setup_scheduled_messages may have stopped the previous
                    # APScheduler before failing. Rebuild it against old config.
                    if scheduler_apply_started and hasattr(self, 'scheduler'):
                        try:
                            self.scheduler.setup_scheduled_messages()
                        except Exception as rollback_error:
                            self.logger.error(
                                "Scheduler rollback failed: %s", rollback_error
                            )
                    raise

                self.logger.info("Configuration reloaded successfully")
                return (
                    True,
                    "Configuration reloaded successfully. Active service plugin and "
                    "process settings remain startup-only and require restart.",
                )

        except (Exception, SystemExit) as e:
            error_msg = f"Error reloading configuration: {e}"
            self.logger.error(error_msg)
            import traceback
            self.logger.error(traceback.format_exc())
            return (False, error_msg)

    def create_default_config(self) -> None:
        """Create default configuration file.

        Writes the packaged ``modules/templates/default_config.ini`` (standard
        settings with comments explaining each option) to ``self.config_file``.
        """
        default_config = (Path(__file__).parent / "templates" / "default_config.ini").read_text(encoding="utf-8")
        with open(self.config_file, 'w') as f:
            f.write(default_config)
        # Note: Using print here since logger may not be initialized yet
        print(f"Created default config file: {self.config_file}")

    def setup_logging(self) -> None:
        """Setup logging configuration.

        Configures the logging system based on settings in the config file (see
        :func:`modules.logging_setup.configure_bot_logging`), then installs the
        shutdown signal handlers.
        If [Logging] section is missing, uses defaults (console/journal only, no file).
        """
        self.logger, self._log_formatter = configure_bot_logging(self.config, self.bot_root)

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
        if enable:
            configure_meshcore_loggers(logging.DEBUG, None, shared_handlers=list(self.logger.handlers))
        else:
            configure_meshcore_loggers(meshcore_log_level(self.config), getattr(self, '_log_formatter', None))

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
            # Reflect the disconnected state for cleanup and status reporting
            self.connected = False

        # Register signal handlers
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)


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

        # MeshCore.contacts is a property that always exists (possibly empty).
        self.logger.info(f"Contacts loaded: {len(self.meshcore.contacts)} contacts")

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

        async def on_disconnected(event, metadata=None):
            payload = event.payload if isinstance(event.payload, dict) else {}
            reason = payload.get('reason', 'unknown')
            await self._schedule_transport_reconnect(reason)

        # Subscribe to events
        self.meshcore.subscribe(EventType.DISCONNECTED, on_disconnected)
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

        if self._shutdown_lock is None:
            self._shutdown_lock = asyncio.Lock()

        # Mark bot as initializing so the web viewer can show a status banner
        try:
            self.db_manager.set_metadata('bot.initializing', 'true')
        except Exception as e:
            self.logger.debug("Could not set bot.initializing metadata: %s", e)

        # Start web viewer early (before radio connect) so operators can see the
        # initializing banner and monitor startup progress
        if self.web_viewer_integration and self.web_viewer_integration.enabled:
            self.web_viewer_integration.start_viewer()
            self.logger.info("Web viewer started (early, before radio connect)")

        # Start the inbound webhook service early too (before radio connect). Unlike
        # the other service plugins, this one accepts inbound connections — starting
        # it late leaves a window (proportional to how long radio connect() takes)
        # where external callers get connection-refused instead of a clear response.
        # The handler itself gates on self.connected and returns 503 until the mesh
        # link is actually ready, so it's safe to bind before we're connected.
        webhook_service = self.services.get('webhook')
        if webhook_service is not None and getattr(webhook_service, 'enabled', False):
            await self._start_service_at_boot(
                'webhook', webhook_service,
                started="Service 'webhook' started (early, before radio connect)",
                failed="Failed to start service 'webhook' early",
            )

        # Connect to MeshCore node
        if not await self.connect():
            self.logger.error("Failed to connect to MeshCore node")
            try:
                self.db_manager.set_metadata('bot.initializing', 'false')
            except Exception:
                pass
            return

        # Bot is now connected — clear the initializing flag
        try:
            self.db_manager.set_metadata('bot.initializing', 'false')
        except Exception as e:
            self.logger.debug("Could not clear bot.initializing metadata: %s", e)

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

        # Start admin server if configured
        if self._admin_server is not None:
            self._admin_server.start()
            self.logger.info(
                "Admin server started on http://127.0.0.1:%d",
                self.config.getint('Admin', 'port', fallback=5001),
            )

        # Web viewer already started early above (before radio connect)

        # Send startup advert if enabled
        await self.send_startup_advert()

        # Start all loaded services (webhook already started early, before radio connect)
        for service_name, service_instance in self.services.items():
            if service_name == 'webhook':
                continue
            await self._start_service_at_boot(
                service_name, service_instance,
                started=f"Service '{service_name}' started",
                failed=f"Failed to start service '{service_name}'",
            )

        # Start command queue processor if needed
        self.command_manager._start_queue_processor()

        # Keep running
        self.logger.info("Bot is running. Press Ctrl+C to stop.")
        try:
            while self.keep_running:
                # Before the transport check, so a viewer clear works while disconnected.
                if self._radio_offline_sync_due():
                    await asyncio.to_thread(self._sync_radio_offline_from_metadata, check_due=False)

                # Backup: meshcore transport dropped (DISCONNECTED event is primary)
                if self.meshcore and not self.meshcore.is_connected:
                    await self._schedule_transport_reconnect('poll_detected')
                    await asyncio.sleep(5)
                    continue

                # Monitor web viewer process and health (never restart during shutdown)
                if (
                    self.web_viewer_integration
                    and self.web_viewer_integration.enabled
                    and self.connected
                    and not self._shutdown_event.is_set()
                ):
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
                if not self._radio_zombie_detected:
                    if self._last_radio_probe is None:
                        self._last_radio_probe = time.time()
                    probe_interval = max(
                        300,
                        min(
                            900,
                            self.config.getint(
                                'Connection',
                                'radio_probe_interval_seconds',
                                fallback=self.config.getint('Bot', 'radio_probe_interval_seconds', fallback=300),
                            ),
                        ),
                    )
                    if time.time() - self._last_radio_probe >= probe_interval:
                        self._last_radio_probe = time.time()
                        asyncio.create_task(self._probe_radio_health())

                # Periodically update system health in database (every 30 seconds)
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
                    self._restart_unhealthy_services(time.time(), restart_backoff)

                await asyncio.sleep(5)  # Check every 5 seconds
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        # Shutdown is owned by the entrypoint (e.g. meshcore_bot.run_bot finally)
        # so stop() runs exactly once; see stop() idempotency guard.

    async def stop(self) -> None:
        """Stop the bot.

        Performs graceful shutdown by stopping services, scheduling, and
        disconnecting from the mesh node.
        """
        if self._shutdown_lock is None:
            try:
                self._shutdown_lock = asyncio.Lock()
            except RuntimeError:
                self._shutdown_lock = None

        if self._shutdown_lock is not None:
            async with self._shutdown_lock:
                if self._shutdown_complete:
                    try:
                        self.logger.debug("stop() skipped — shutdown already completed")
                    except (AttributeError, TypeError):
                        pass
                    return
                await self._stop_inner()
        else:
            if self._shutdown_complete:
                return
            await self._stop_inner()

    async def _stop_inner(self) -> None:
        """Run one full shutdown pass (caller holds _shutdown_lock when used)."""
        try:
            try:
                self.logger.info("Stopping MeshCore Bot...")
            except (AttributeError, TypeError):
                print("Stopping MeshCore Bot...")

            self._shutdown_event.set()
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
        finally:
            self._shutdown_complete = True

    async def _start_service_at_boot(self, name: str, service: Any, *, started: str, failed: str) -> None:
        """Start one service during startup; a failure or a declined start waits out the restart backoff."""
        try:
            await service.start()
        except Exception as e:
            self.logger.error(f"{failed}: {e}")
            self._service_restart_failures[name] = time.time()
            return
        if not self._note_if_service_not_running(name, service):
            self.logger.info(started)

    def _restart_unhealthy_services(self, now: float, backoff: float) -> None:
        """Health-loop step: start a restart task for each service that is due one."""
        for name, service in self.services.items():
            if not self._service_restart_due(name, service, now, backoff):
                continue
            self.logger.warning(f"Service '{name}' unhealthy, attempting restart...")
            asyncio.create_task(self._restart_service(name, service))

    def _service_restart_due(self, name: str, service: Any, now: float, backoff: float) -> bool:
        """Whether the health loop should restart *service* now."""
        if not getattr(service, 'enabled', True):
            return False
        try:
            if service.is_healthy():
                return False
        except Exception:
            pass
        if name in self._service_restarting:
            return False
        last_failure = self._service_restart_failures.get(name)
        return last_failure is None or now - last_failure >= backoff

    def _note_if_service_not_running(self, service_name: str, service_instance: Any) -> bool:
        """Back off a service whose start() returned without running; True if so.

        That happens for missing configuration (Discord or Telegram with no
        channels, the map uploader with no key) and for transient conditions
        (the radio not connected yet). Either way, restarting it on every
        health tick cannot help; after the backoff the health loop tries again,
        which recovers the transient case.
        """
        if not getattr(service_instance, 'enabled', True):
            return False  # disabled on purpose; the health loop skips it anyway
        try:
            running = service_instance.is_running()
        except Exception:
            running = True
        if running:
            return False
        backoff = self.config.getint('Bot', 'service_restart_backoff_seconds', fallback=300)
        self.logger.info(
            f"Service '{service_name}' did not start (check its configuration); "
            f"next attempt in {backoff}s"
        )
        self._service_restart_failures[service_name] = time.time()
        return True

    async def _restart_service(self, service_name: str, service_instance: Any) -> bool:
        """Stop and start a service. Used when is_healthy() is False.
        Returns True on success, False on failure. Exceptions are caught and logged.
        """
        self._service_restarting.add(service_name)
        try:
            await service_instance.stop()
            await service_instance.start()
            if self._note_if_service_not_running(service_name, service_instance):
                return False
            if not service_instance.is_healthy():
                # Restarted but still unhealthy: wait out the backoff before the
                # next attempt rather than restarting it on every health tick.
                self.logger.warning(f"Service '{service_name}' is still unhealthy after restart")
                self._service_restart_failures[service_name] = time.time()
                return False
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
                await asyncio.wait_for(
                    self.meshcore.commands.send_advert(flood=False),
                    timeout=30.0,
                )
            elif startup_advert == 'flood':
                self.logger.debug("Sending flood advert")
                await asyncio.wait_for(
                    self.meshcore.commands.send_advert(flood=True),
                    timeout=30.0,
                )
            else:
                self.logger.warning(f"Unknown startup_advert option: {startup_advert}")
                return

            # Update last advert time
            import time
            self.last_advert_time = time.time()

            self.logger.info(f"Startup {startup_advert} advert sent successfully")

        except (OSError, AttributeError, ValueError, RuntimeError, asyncio.TimeoutError) as e:
            if isinstance(e, asyncio.TimeoutError):
                # May trip the outage, which writes bot_metadata; keep that off the loop.
                await asyncio.to_thread(self._record_send_failure)
            self.logger.error(f"Error sending startup advert: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

    def key_prefix(self, public_key: str) -> str:
        return public_key[:self.prefix_hex_chars]

    def is_valid_prefix(self, prefix: str) -> bool:
        return len(prefix) == self.prefix_hex_chars
