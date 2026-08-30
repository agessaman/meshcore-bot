#!/usr/bin/env python3
"""
Packet Capture Service for MeshCore Bot
Captures packets from MeshCore radios and outputs to console, file, and MQTT.
Adapted from meshcore-packet-capture project.
"""

import asyncio
import copy
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

# Import meshcore
from meshcore import EventType

# Import bot's enums
from ..enums import PayloadType, PayloadVersion, RouteType
from ..meshcore_payload_decode import (
    DEFAULT_PUBLIC_CHANNEL_KEY,
    ChannelKeyStore,
    decode_payload,
)
from ..neighbors_discovery import (
    MAX_INTERVAL_HOURS,
    MIN_CYCLE_GAP_SECONDS,
    MIN_INTERVAL_HOURS,
    STATUS_RESPONDED,
    NeighborsConfig,
    build_neighbors_message,
    collect_scopes,
    discover_neighbors,
    fetch_self_scopes,
    record_neighbors,
)

# Import bot's utilities for packet hash
from ..utils import (
    calculate_packet_hash,
    decode_path_len_byte,
    parse_trace_payload_route_hashes,
    verify_meshcore_advert_ed25519,
)
from ..version_info import resolve_application_version

# Import MQTT client
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# Import auth token utilities
# Import base service
import contextlib

from .base_service import BaseServicePlugin
from .packet_capture_utils import create_auth_token_async, read_private_key_file

# bot_metadata key holding the last neighbors cycle timestamp. Namespaced because
# bot_metadata is shared across the whole bot.
NEIGHBORS_STATE_KEY = "packet_capture.last_neighbors_publish"

# A cycle can spend airtime without producing a publish timestamp (for example,
# when the discover acknowledgement is lost). Persist that attempt separately so
# a process restart cannot bypass the short airtime guard while still allowing
# the scheduler to retry after MIN_CYCLE_GAP_SECONDS rather than waiting a full
# configured interval.
NEIGHBORS_ATTEMPT_STATE_KEY = "packet_capture.last_neighbors_attempt"

# How long the scheduler waits before re-checking after a cycle that produced no
# result. Short on purpose: the usual causes (radio down, unsupported build) cost
# nothing to re-test. A cycle that did transmit is held by MIN_CYCLE_GAP_SECONDS
# instead, which is longer.
NEIGHBORS_RETRY_BACKOFF_SECONDS = 300.0

# Sentinel meaning "no IATA configured" (documented as invalid in config.ini.example).
DEFAULT_IATA = "XYZ"


def _decode_key_str(key_str: str) -> Optional[bytes]:
    """Decode a 16-byte channel key from a hex (32 chars) or base64 string."""
    key_str = key_str.strip()
    try:
        if len(key_str) == 32 and all(c in "0123456789abcdefABCDEF" for c in key_str):
            return bytes.fromhex(key_str)
        import base64

        raw = base64.b64decode(key_str, validate=True)
        return raw if len(raw) == 16 else None
    except (ValueError, TypeError):
        return None


def _parse_size(value: str) -> int:
    """Parse a size string like '50MB', '10M', or '1048576' into bytes."""
    value = str(value).strip().upper().replace("B", "")
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    try:
        if value and value[-1] in multipliers:
            return int(float(value[:-1]) * multipliers[value[-1]])
        return int(value or 0)
    except ValueError:
        return 0


class _RotatingPacketLog:
    """Writes one JSON line per packet, with optional size/time rotation.

    ``rotation='off'`` appends to a single file (original behavior). ``'size'``
    and ``'time'`` use stdlib rotating handlers. Kept host-agnostic so the same
    logic can be ported to meshcore-packet-capture.
    """

    def __init__(
        self,
        path: str,
        rotation: str = "off",
        max_bytes: int = 0,
        backup_count: int = 5,
        when: str = "midnight",
    ) -> None:
        self._handler = None
        self._fh = None
        if rotation == "size" and max_bytes > 0:
            from logging.handlers import RotatingFileHandler

            self._handler = RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        elif rotation == "time":
            from logging.handlers import TimedRotatingFileHandler

            self._handler = TimedRotatingFileHandler(
                path, when=when, backupCount=backup_count, encoding="utf-8"
            )
        else:
            self._fh = open(path, "a", encoding="utf-8")

    def write_line(self, line: str) -> None:
        if self._handler is not None:
            # Default logging formatter emits just the message plus a terminator.
            record = logging.LogRecord("packetlog", logging.INFO, "(packet)", 0, line, None, None)
            self._handler.emit(record)
        else:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()
        elif self._fh is not None:
            self._fh.close()


class PacketCaptureService(BaseServicePlugin):
    """Packet capture service using bot's meshcore connection.

    Captures packets from MeshCore network and publishes to MQTT.
    Supports multiple MQTT brokers, auth tokens, and output to file.
    """

    config_section = "PacketCapture"  # Explicit config section
    description = "Captures packets from MeshCore network and publishes to MQTT"

    # Web-viewer settings schema (see modules/settings_schema.py). Core settings
    # are typed here; the detailed mqtt1_*/mqtt2_* broker keys remain editable
    # via the raw "Other config values" editor.
    settings_schema = [
        {"key": "output_file", "label": "Output file", "type": "str", "default": "", "width": "lg",
         "help": "Optional file to also write captured packets to."},
        {"key": "owner_public_key", "label": "Owner public key", "type": "str", "default": "", "width": "lg",
         "help": "Owner identity included with uploads."},
        {"key": "owner_email", "label": "Owner email", "type": "str", "default": "",
         "help": "Owner contact email included with uploads."},
        {"key": "private_key_path", "label": "Private key path", "type": "str", "default": "", "width": "lg",
         "help": "Path to the device private key used to sign uploads."},
        {"key": "iata", "label": "IATA code", "type": "str", "default": "",
         "help": "Nearest airport IATA code for location tagging."},
        {"key": "verbose", "label": "Verbose logging", "type": "bool", "default": False, "help": "Detailed logging."},
        {"key": "debug", "label": "Debug logging", "type": "bool", "default": False, "help": "Debug-level logging."},
        {"key": "mqtt_enabled", "label": "MQTT publishing", "type": "bool", "default": True,
         "help": "Master switch for publishing captured packets to the MQTT brokers below."},
        # --- Advanced (collapsed by default in the UI) ---
        {"key": "auth_token_method", "label": "Auth token method", "type": "enum", "group": "Advanced",
         "options": [{"value": "device", "label": "Device (on-device signing, default)"},
                     {"value": "python", "label": "Python (requires private key)"}],
         "default": "device", "help": "How JWT auth tokens are signed."},
        {"key": "mqtt_skip_unparseable_packets", "label": "Skip unparseable packets", "type": "bool",
         "group": "Advanced", "default": True, "help": "Don't publish packets that fail to parse (hash 0000…)."},
        {"key": "advert_require_valid_signature", "label": "Require valid advert signature", "type": "bool",
         "group": "Advanced", "default": False, "help": "Only publish ADVERT packets with a valid signature."},
        {"key": "raw_duplicate_window", "label": "Duplicate window", "type": "float", "group": "Advanced",
         "min": 0, "default": 2.0, "unit": "s", "help": "De-duplicate identical raw packets within this window."},
        {"key": "rf_data_cache_timeout", "label": "RF data cache timeout", "type": "float", "group": "Advanced",
         "min": 0, "default": 15.0, "unit": "s", "help": "How long RF metadata is cached for correlation."},
        {"key": "stats_in_status_enabled", "label": "Stats in status", "type": "bool", "group": "Advanced",
         "default": True, "help": "Include capture statistics in the MQTT status payload."},
        {"key": "stats_refresh_interval", "label": "Stats refresh interval", "type": "int", "group": "Advanced",
         "min": 0, "default": 300, "unit": "s", "help": "How often stats are refreshed. 0 disables."},
        {"key": "health_check_interval", "label": "Health check interval", "type": "int", "group": "Advanced",
         "min": 0, "default": 30, "unit": "s", "help": "Radio health-check cadence. 0 disables."},
        {"key": "health_check_grace_period", "label": "Health check grace period", "type": "int", "group": "Advanced",
         "min": 0, "default": 2, "help": "Consecutive failures tolerated before acting."},
        {"key": "jwt_renewal_interval", "label": "JWT renewal interval", "type": "int", "group": "Advanced",
         "min": 1, "default": 43200, "unit": "s", "help": "Default JWT renewal interval (per-broker overrides apply)."},
        {"key": "jwt_ttl_seconds", "label": "JWT TTL", "type": "int", "group": "Advanced",
         "min": 1, "default": 86400, "unit": "s", "help": "Default JWT lifetime (per-broker overrides apply)."},
        # --- Neighbors (zero-hop discovery; see modules/neighbors_discovery.py) ---
        {"key": "neighbors_enabled", "label": "Neighbors discovery", "type": "bool", "group": "Neighbors",
         "default": False,
         "help": "Periodically ask which repeaters this node hears directly. Records confirmed "
                 "direct links with SNR, and publishes to brokers that opted in."},
        {"key": "neighbors_interval_hours", "label": "Interval", "type": "int", "group": "Neighbors",
         "min": 12, "max": 336, "default": 24, "unit": "h",
         "help": "How often a discovery cycle runs. Clamped to 12-336h, matching the firmware."},
        {"key": "neighbors_discover_window", "label": "Discover window", "type": "float", "group": "Neighbors",
         "min": 5, "default": 60.0, "unit": "s",
         "help": "How long responses are collected. Repeaters reply after a random delay, so a "
                 "short window finds fewer neighbours. The bot stays responsive throughout."},
        {"key": "neighbors_max", "label": "Max neighbours", "type": "int", "group": "Neighbors",
         "min": 1, "default": 32, "help": "Cap per cycle, most useful first (recent, then stronger SNR)."},
        {"key": "neighbors_feed_mesh_graph", "label": "Feed mesh graph", "type": "bool", "group": "Neighbors",
         "default": True, "help": "Also add confirmed direct links as mesh graph edges."},
        {"key": "neighbors_collect_scopes", "label": "Collect region scopes", "type": "bool",
         "group": "Neighbors", "default": False,
         "help": "SLOW — also ask each neighbour for its region scopes. Each request holds the radio "
                 "for up to ~25s, delaying bot replies, and for a repeater with no stored path the "
                 "library temporarily rewrites that contact's path on the device. Leave off unless "
                 "you specifically need scopes."},
        {"key": "neighbors_command_timeout", "label": "Command timeout", "type": "float",
         "group": "Neighbors", "min": 1, "default": 20.0, "unit": "s",
         "help": "Cap on the discover request itself; a stalled link can otherwise block."},
        {"key": "neighbors_scope_timeout", "label": "Scope timeout", "type": "float", "group": "Neighbors",
         "min": 0, "default": 0.0, "unit": "s",
         "help": "Per-scope-request wait. 0 uses the device's own airtime estimate."},
        {"key": "neighbors_scope_min_timeout", "label": "Scope min timeout", "type": "float",
         "group": "Neighbors", "min": 0, "default": 8.0, "unit": "s",
         "help": "Floor under the device-suggested scope timeout."},
        {"key": "neighbors_scope_gap", "label": "Scope gap", "type": "float", "group": "Neighbors",
         "min": 0, "default": 2.0, "unit": "s", "help": "Settle delay between scope requests."},
        {"key": "neighbors_cycle_timeout", "label": "Scope pass budget", "type": "float",
         "group": "Neighbors", "min": 10, "default": 600.0, "unit": "s",
         "help": "Overall budget for the scope pass; unreached neighbours report as timeout."},
        {"key": "neighbors_self_scopes", "label": "Own scopes override", "type": "str",
         "group": "Neighbors", "default": "", "width": "lg",
         "help": "Override for this node's own \"self.scopes\" value. Empty asks the device."},
    ]

    # Repeating structured blocks (see modules/settings_schema.py). Each MQTT
    # broker is mqtt<N>_* and must stay contiguously numbered — the editor
    # renumbers them on save.
    settings_repeating_blocks = [
        {
            "id": "mqtt",
            "label": "MQTT brokers",
            "item_label": "MQTT broker",
            "enabled_field": "enabled",
            "help": "Each broker captured packets are published to. Brokers are tried in order.",
            "fields": [
                {"key": "server", "label": "Server", "type": "str", "default": "",
                 "help": "Broker hostname or IP."},
                {"key": "port", "label": "Port", "type": "int", "min": 1, "max": 65535, "default": 1883,
                 "help": "Broker port (1883 plain, 8883/443 for TLS/websockets)."},
                {"key": "transport", "label": "Transport", "type": "enum",
                 "options": [{"value": "tcp", "label": "TCP"}, {"value": "websockets", "label": "WebSockets"}],
                 "default": "tcp"},
                {"key": "use_tls", "label": "Use TLS", "type": "bool", "default": False},
                {"key": "tls_insecure", "label": "Skip TLS verification", "type": "bool", "default": False,
                 "help": "INSECURE — accept any broker certificate. Only for self-signed brokers on a "
                         "trusted network; leave off so certificate and hostname are verified."},
                {"key": "use_auth_token", "label": "Use JWT auth token", "type": "bool", "default": False,
                 "help": "Authenticate with a signed JWT instead of username/password."},
                {"key": "token_audience", "label": "Token audience", "type": "str", "default": "",
                 "help": "JWT audience claim (when using auth token)."},
                {"key": "jwt_reconnect_on_renew", "label": "Reconnect on token renewal",
                 "type": "bool", "default": True,
                 "help": "Reconnect right after renewing the token, so the fresh one is in "
                         "force. Brokers that enforce the JWT's expiry drop the session "
                         "otherwise. Turn off only if your broker ignores expiry."},
                {"key": "topic_status", "label": "Status topic", "type": "str", "default": ""},
                {"key": "topic_packets", "label": "Packets topic", "type": "str", "default": ""},
                {"key": "neighbors", "label": "Publish neighbours", "type": "bool", "default": True,
                 "help": "Send the zero-hop neighbours snapshot to this broker. On by default, but "
                         "nothing is sent until Neighbours discovery is enabled above. Turn off to "
                         "hold just this broker back."},
                {"key": "topic_neighbors", "label": "Neighbours topic", "type": "str", "default": "",
                 "help": "Defaults to the packets topic with its last segment swapped for "
                         "'neighbors', else <topic prefix>/neighbors."},
                {"key": "websocket_path", "label": "WebSocket path", "type": "str", "default": "/mqtt",
                 "help": "Path when transport is websockets."},
                {"key": "client_id", "label": "Client ID", "type": "str", "default": "",
                 "help": "Leave empty to generate one per broker. Set explicitly only if the "
                         "broker requires a fixed ID; two brokers must never share one."},
                {"key": "keepalive", "label": "Keepalive", "type": "int", "min": 5, "max": 3600,
                 "default": 60,
                 "help": "Seconds between PINGREQs. Lower it (30) for websockets through a proxy "
                         "that drops idle connections."},
                {"key": "upload_packet_types", "label": "Upload packet types", "type": "str", "default": "",
                 "help": "Comma-separated type numbers (e.g. 2,4). Empty = upload all."},
            ],
        }
    ]

    def __init__(self, bot):
        """Initialize packet capture service.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)

        # Don't store meshcore here - it's None until bot connects
        # Use self.meshcore property to get current connection

        # Setup logging (use bot's formatter and configuration)
        self.logger = logging.getLogger("PacketCaptureService")

        # Only setup handlers if none exist to prevent duplicates
        if not self.logger.handlers:
            # Use the same formatter as the bot (colored if enabled)
            # Get formatter from bot's console handler
            bot_formatter = None
            for handler in bot.logger.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                    bot_formatter = handler.formatter
                    break

            # If no formatter found, create one matching bot's style
            if not bot_formatter:
                try:
                    import colorlog

                    colored = (
                        bot.config.getboolean("Logging", "colored_output", fallback=True)
                        if bot.config.has_section("Logging")
                        else True
                    )
                    if colored:
                        bot_formatter = colorlog.ColoredFormatter(
                            "%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S",
                            log_colors={
                                "DEBUG": "cyan",
                                "INFO": "green",
                                "WARNING": "yellow",
                                "ERROR": "red",
                                "CRITICAL": "red,bg_white",
                            },
                        )
                    else:
                        bot_formatter = logging.Formatter(
                            "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                        )
                except ImportError:
                    # Fallback if colorlog not available
                    bot_formatter = logging.Formatter(
                        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                    )

            # Add console handler with bot's formatter
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(bot_formatter)
            self.logger.addHandler(console_handler)

        # Prevent propagation to root logger to avoid duplicate output
        self.logger.propagate = False

        # Load configuration from bot's config
        self._load_config()

        # Connection state (uses bot's connection, but track our state)
        self.connected = False

        # Packet tracking
        self.packet_count = 0
        self.output_handle = None

        # MQTT
        self.mqtt_clients: list[dict[str, Any]] = []
        self.mqtt_connected = False

        # Stats/status publishing
        self.stats_status_enabled = self.get_config_bool("stats_in_status_enabled", True)
        self.stats_refresh_interval = self.get_config_int("stats_refresh_interval", 300)
        self.latest_stats = None
        self.last_stats_fetch = 0
        self.stats_supported = False
        self.stats_capability_state = None
        self.stats_update_task = None
        self.stats_fetch_lock = asyncio.Lock()
        self.cached_firmware_info = None
        self.radio_info = None

        # Neighbors discovery runtime state. Kept out of _load_config so a future
        # config reload cannot orphan a running scheduler or replay a cycle
        # (map_uploader_service re-invokes its own _load_config on reload).
        self.neighbors_task = None
        self.neighbors_capability_state = None
        self.neighbors_discover_failures = 0
        self.neighbors_topic_warned: set[int] = set()
        self.last_neighbors_publish = self._load_neighbors_state()
        # When a cycle last reached the radio, whether or not it produced a
        # result. Callers that ration airtime need this rather than
        # last_neighbors_publish, which a failed cycle never stamps.
        self.last_neighbors_attempt = self._load_neighbors_attempt_state()
        # Single-flight across every trigger (scheduler, command, future callers):
        # two overlapping cycles would collect into each other's discover window
        # and double the airtime. asyncio is single-threaded, so a plain flag set
        # and cleared without an await in between is enough.
        self.neighbors_cycle_active = False

        # Background tasks
        self.background_tasks: list[asyncio.Task] = []
        self.should_exit = False

        # JWT renewal (default: 12 hours) and JWT exp TTL (default: 24 hours; see jwt_ttl_seconds)
        self.jwt_renewal_interval = self.get_config_int("jwt_renewal_interval", 43200)
        self.jwt_ttl_seconds = self.get_config_int("jwt_ttl_seconds", 86400)

        # Health check
        self.health_check_interval = self.get_config_int("health_check_interval", 30)
        self.health_check_grace_period = self.get_config_int("health_check_grace_period", 2)
        self.health_check_failure_count = 0

        # Event subscriptions (track for cleanup)
        self.event_subscriptions = []

        # RF / RAW deduplication (aligns with meshcore-packet-capture: avoid double publish
        # on packet topic when both RX_LOG_DATA and RAW_DATA fire for the same reception).
        # Timeouts/window come from _load_config (raw_duplicate_window, rf_data_cache_timeout).
        self.rf_data_cache: dict[str, dict[str, Any]] = {}
        self.recent_rf_packets: dict[str, float] = {}

        self.logger.info("Packet capture service initialized")

    def _load_config(self) -> None:
        """Load configuration from bot's config.

        Loads settings for output file, MQTT brokers, auth tokens, and
        other service options.
        """
        config = self.bot.config

        # Check if enabled
        self.enabled = config.getboolean("PacketCapture", "enabled", fallback=False)

        # Output file
        self.output_file = config.get("PacketCapture", "output_file", fallback=None)

        # Verbose/debug
        self.verbose = config.getboolean("PacketCapture", "verbose", fallback=False)
        self.debug = config.getboolean("PacketCapture", "debug", fallback=False)
        self._apply_log_level()

        # Packet log rotation (off|size|time). Default off preserves single-file behavior.
        self.log_rotation = config.get("PacketCapture", "log_rotation", fallback="off").strip().lower()
        self.log_max_bytes = _parse_size(config.get("PacketCapture", "log_max_bytes", fallback="0"))
        self.log_backup_count = config.getint("PacketCapture", "log_backup_count", fallback=5)
        self.log_rotation_when = config.get("PacketCapture", "log_rotation_when", fallback="midnight").strip()

        # Payload decoding (decode plain text / structured payload into a nested "decoded" object)
        self.decode_payloads = config.getboolean("PacketCapture", "decode_payloads", fallback=False)
        # Global default for the per-broker mqttN_include_decoded toggle (default off:
        # opt in per broker, or set include_decoded = true to publish decoded everywhere)
        self.include_decoded = config.getboolean("PacketCapture", "include_decoded", fallback=False)
        self.channel_key_store = self._build_channel_key_store(config) if self.decode_payloads else None

        # MQTT configuration
        self.mqtt_enabled = config.getboolean("PacketCapture", "mqtt_enabled", fallback=True)
        self.mqtt_brokers = self._parse_mqtt_brokers(config)

        # Global IATA. Blank stays blank so packet/status {IATA} topics keep
        # their historical empty-segment resolution; a missing key still falls
        # back to the XYZ sentinel. Neighbors treats blank and XYZ as unset.
        self.global_iata = config.get(
            "PacketCapture", "iata", fallback=DEFAULT_IATA
        ).strip().lower()

        # Owner information
        self.owner_public_key = config.get("PacketCapture", "owner_public_key", fallback=None)
        self.owner_email = config.get("PacketCapture", "owner_email", fallback=None)

        # Private key for auth tokens (fallback if device signing not available)
        self.private_key_path = config.get("PacketCapture", "private_key_path", fallback=None)
        self.private_key_hex = None
        if self.private_key_path:
            self.private_key_hex = read_private_key_file(self.private_key_path)
            if not self.private_key_hex:
                self.logger.warning(f"Could not load private key from {self.private_key_path}")

        # Auth token method preference
        self.auth_token_method = config.get("PacketCapture", "auth_token_method", fallback="device").lower()
        # 'device' = try on-device signing first, fallback to Python
        # 'python' = use Python signing only

        # RX_LOG vs RAW correlation (mirror meshcore-packet-capture RAW_DUPLICATE_WINDOW / RF_DATA_TIMEOUT)
        self.raw_duplicate_window = config.getfloat("PacketCapture", "raw_duplicate_window", fallback=2.0)
        self.rf_data_cache_timeout = config.getfloat("PacketCapture", "rf_data_cache_timeout", fallback=15.0)

        # Do not publish to MQTT when content hash is unknown (zeros) — unparseable / strict path reject
        self.mqtt_skip_unparseable_packets = config.getboolean(
            "PacketCapture", "mqtt_skip_unparseable_packets", fallback=True
        )

        # Skip MQTT for ADVERT packets that fail Ed25519 verify (mesh payload corruption)
        self.advert_require_valid_signature = config.getboolean(
            "PacketCapture", "advert_require_valid_signature", fallback=False
        )

        # Note: Python signing can fetch private key from device if not provided via file
        # The create_auth_token_async function will automatically try to export the key
        # from the device if private_key_hex is None and meshcore_instance is available

        self._load_neighbors_config(config)

    def _load_neighbors_config(self, config) -> None:
        """Build the neighbors cycle configuration (see modules/neighbors_discovery.py).

        Off by default: a cycle spends real airtime and, with scope collection
        enabled, holds the shared radio command lock for seconds at a time.
        """
        self.neighbors_enabled = config.getboolean("PacketCapture", "neighbors_enabled", fallback=False)
        self.neighbors_feed_mesh_graph = config.getboolean(
            "PacketCapture", "neighbors_feed_mesh_graph", fallback=True
        )

        requested_interval = config.getint("PacketCapture", "neighbors_interval_hours", fallback=24)
        self.neighbors_config = NeighborsConfig(
            interval_hours=requested_interval,
            discover_window=config.getfloat("PacketCapture", "neighbors_discover_window", fallback=60.0),
            command_timeout=config.getfloat("PacketCapture", "neighbors_command_timeout", fallback=20.0),
            collect_scopes=config.getboolean("PacketCapture", "neighbors_collect_scopes", fallback=False),
            scope_timeout=config.getfloat("PacketCapture", "neighbors_scope_timeout", fallback=0.0),
            scope_min_timeout=config.getfloat("PacketCapture", "neighbors_scope_min_timeout", fallback=8.0),
            scope_gap=config.getfloat("PacketCapture", "neighbors_scope_gap", fallback=2.0),
            cycle_timeout=config.getfloat("PacketCapture", "neighbors_cycle_timeout", fallback=600.0),
            max_neighbors=config.getint("PacketCapture", "neighbors_max", fallback=32),
            self_scopes=config.get("PacketCapture", "neighbors_self_scopes", fallback="").strip(),
        )
        # NeighborsConfig clamps out-of-range values; say so rather than silently
        # honouring something different from what was configured.
        if self.neighbors_enabled and self.neighbors_config.interval_hours != requested_interval:
            self.logger.warning(
                f"neighbors_interval_hours {requested_interval} is outside the supported "
                f"{MIN_INTERVAL_HOURS}-{MAX_INTERVAL_HOURS}h range, "
                f"using {self.neighbors_config.interval_hours}h"
            )

    def _load_neighbors_timestamp(self, key: str, label: str) -> float:
        """Load and validate one neighbors wall-clock timestamp."""
        try:
            raw = self.bot.db_manager.get_metadata(key)
        except Exception as e:
            self.logger.debug(f"Could not read {label}: {e}")
            return 0.0
        if not raw:
            return 0.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            self.logger.warning(f"Ignoring malformed {label} value: {raw!r}")
            return 0.0
        # A clock jump either way would otherwise pin the scheduler: a future
        # timestamp suppresses cycles indefinitely, a nonsensical old one is noise.
        now = time.time()
        if value > now + 300 or value < now - (400 * 86400):
            self.logger.warning(
                f"Ignoring out-of-range {label} timestamp ({value})"
            )
            return 0.0
        return value

    def _load_neighbors_state(self) -> float:
        """Last completed cycle timestamp, from bot_metadata.

        Neighbors intervals are long (12-336h), so surviving a restart is what
        keeps a bot that reboots often from re-running discovery every start.
        """
        return self._load_neighbors_timestamp(NEIGHBORS_STATE_KEY, "neighbors state")

    def _load_neighbors_attempt_state(self) -> float:
        """Last cycle that may have reached the radio, from bot_metadata."""
        return self._load_neighbors_timestamp(
            NEIGHBORS_ATTEMPT_STATE_KEY, "neighbors attempt state"
        )

    def _save_neighbors_timestamp(self, key: str, value: float, label: str) -> None:
        """Persist one neighbors wall-clock timestamp."""
        try:
            self.bot.db_manager.set_metadata(key, str(value))
        except Exception as e:
            self.logger.debug(f"Could not save {label}: {e}")

    def _save_neighbors_state(self) -> None:
        """Persist the last cycle timestamp to bot_metadata."""
        self._save_neighbors_timestamp(
            NEIGHBORS_STATE_KEY, self.last_neighbors_publish, "neighbors state"
        )

    def _save_neighbors_attempt_state(self) -> None:
        """Persist the last cycle that may have reached the radio."""
        self._save_neighbors_timestamp(
            NEIGHBORS_ATTEMPT_STATE_KEY,
            self.last_neighbors_attempt,
            "neighbors attempt state",
        )

    def _build_channel_key_store(self, config) -> ChannelKeyStore:
        """Build a comprehensive channel key store for GRP_TXT decryption.

        Sources (mirrors the web viewer's channel sourcing,
        modules/web_viewer/app.py:_get_additional_decode_channels):
          1. The bot's live radio channels (channel_manager, with real keys).
          2. ``decode_hashtag_channels`` + the ``[Channels_List]`` section (keys derived).
          3. Explicit ``decode_channel_keys`` = name=hexkey list.
          4. The well-known default Public channel key (unless disabled).
        """
        store = ChannelKeyStore()

        # 1. Bot's configured radio channels (have real key material).
        try:
            channel_manager = getattr(self.bot, "channel_manager", None)
            if channel_manager:
                for ch in channel_manager.get_configured_channels():
                    key_hex = ch.get("channel_key_hex")
                    if key_hex:
                        store.add_hex(key_hex, ch.get("channel_name"))
        except Exception as e:
            self.logger.debug(f"Could not load channel_manager keys: {e}")

        # 2a. decode_hashtag_channels: comma list of names (keys derived).
        hashtag_raw = config.get("PacketCapture", "decode_hashtag_channels", fallback="")
        for name in (n.strip() for n in hashtag_raw.split(",")):
            if name:
                store.add_hashtag(name)

        # 2b. [Channels_List] section names (keys derived), matching web viewer behavior.
        if config.has_section("Channels_List"):
            for key in config.options("Channels_List"):
                name = key.split(".")[-1] if "." in key else key
                name = name.strip()
                if name:
                    store.add_hashtag(name)

        # 3. Explicit name=hexkey pairs (hex or base64).
        keys_raw = config.get("PacketCapture", "decode_channel_keys", fallback="")
        for entry in (e.strip() for e in keys_raw.split(",")):
            if not entry or "=" not in entry:
                continue
            name, _, key_str = entry.partition("=")
            key_bytes = _decode_key_str(key_str.strip())
            if key_bytes:
                store.add_secret(key_bytes, name.strip())

        # 4. Built-in default Public channel key.
        if config.getboolean("PacketCapture", "decode_include_public", fallback=True):
            store.add_secret(DEFAULT_PUBLIC_CHANNEL_KEY, "public")

        self.logger.info(f"Payload decoding enabled with {len(store)} channel key(s)")
        return store

    def _prune_correlation_caches(self, current_time: Optional[float] = None) -> None:
        """Drop stale rf_data_cache and recent_rf_packets entries.

        Matches meshcore-packet-capture cleanup in handle_rf_log_data / handle_raw_data.
        """
        if current_time is None:
            current_time = time.time()
        self.rf_data_cache = {
            k: v for k, v in self.rf_data_cache.items() if current_time - v["timestamp"] < self.rf_data_cache_timeout
        }
        self.recent_rf_packets = {
            k: v for k, v in self.recent_rf_packets.items() if current_time - v < self.raw_duplicate_window
        }

    def _parse_mqtt_brokers(self, config) -> list[dict[str, Any]]:
        """Parse MQTT broker configuration (mqttN_* format).

        Args:
            config: ConfigParser object containing the configuration.

        Returns:
            list[dict[str, Any]]: List of configured MQTT broker dictionaries.
        """
        brokers = []

        global_jwt_renewal = config.getint("PacketCapture", "jwt_renewal_interval", fallback=43200)
        global_jwt_ttl = config.getint("PacketCapture", "jwt_ttl_seconds", fallback=86400)

        # Parse multiple brokers (mqtt1_*, mqtt2_*, etc.)
        broker_num = 1
        while True:
            enabled_key = f"mqtt{broker_num}_enabled"
            server_key = f"mqtt{broker_num}_server"

            if not config.has_option("PacketCapture", server_key):
                break

            enabled = config.getboolean("PacketCapture", enabled_key, fallback=True)
            if not enabled:
                broker_num += 1
                continue

            # Parse upload_packet_types: comma-separated list (e.g. "2,4"); empty/unset = upload all
            upload_types_raw = config.get("PacketCapture", f"mqtt{broker_num}_upload_packet_types", fallback="").strip()
            upload_packet_types = None
            if upload_types_raw:
                upload_packet_types = frozenset(t.strip() for t in upload_types_raw.split(",") if t.strip())
                if not upload_packet_types:
                    upload_packet_types = None

            renew_key = f"mqtt{broker_num}_jwt_renewal_interval"
            if config.has_option("PacketCapture", renew_key):
                jwt_renewal_interval = config.getint("PacketCapture", renew_key)
            else:
                jwt_renewal_interval = global_jwt_renewal

            ttl_key = f"mqtt{broker_num}_jwt_ttl_seconds"
            if config.has_option("PacketCapture", ttl_key):
                jwt_ttl_seconds = config.getint("PacketCapture", ttl_key)
            else:
                jwt_ttl_seconds = global_jwt_ttl

            broker = {
                "enabled": True,
                "host": config.get("PacketCapture", server_key, fallback="localhost"),
                "port": config.getint("PacketCapture", f"mqtt{broker_num}_port", fallback=1883),
                "username": config.get("PacketCapture", f"mqtt{broker_num}_username", fallback=None),
                "password": config.get("PacketCapture", f"mqtt{broker_num}_password", fallback=None),
                "topic_prefix": config.get("PacketCapture", f"mqtt{broker_num}_topic_prefix", fallback=None),
                "topic_status": config.get("PacketCapture", f"mqtt{broker_num}_topic_status", fallback=None),
                "topic_packets": config.get("PacketCapture", f"mqtt{broker_num}_topic_packets", fallback=None),
                "topic_neighbors": config.get(
                    "PacketCapture", f"mqtt{broker_num}_topic_neighbors", fallback=None
                ),
                # On by default, so `neighbors_enabled` is the single switch that
                # turns the feature on everywhere. Set false per broker to hold one
                # back. The whole feature is still off until neighbors_enabled.
                "neighbors": config.getboolean(
                    "PacketCapture", f"mqtt{broker_num}_neighbors", fallback=True
                ),
                "use_auth_token": config.getboolean(
                    "PacketCapture", f"mqtt{broker_num}_use_auth_token", fallback=False
                ),
                "token_audience": config.get("PacketCapture", f"mqtt{broker_num}_token_audience", fallback=None),
                "transport": config.get("PacketCapture", f"mqtt{broker_num}_transport", fallback="tcp").lower(),
                "use_tls": config.getboolean("PacketCapture", f"mqtt{broker_num}_use_tls", fallback=False),
                "tls_insecure": config.getboolean(
                    "PacketCapture", f"mqtt{broker_num}_tls_insecure", fallback=False
                ),
                "broker_num": broker_num,
                "websocket_path": config.get("PacketCapture", f"mqtt{broker_num}_websocket_path", fallback="/mqtt"),
                "keepalive": config.getint("PacketCapture", f"mqtt{broker_num}_keepalive", fallback=60),
                "client_id": config.get("PacketCapture", f"mqtt{broker_num}_client_id", fallback=None),
                "upload_packet_types": upload_packet_types,
                "include_decoded": config.getboolean(
                    "PacketCapture",
                    f"mqtt{broker_num}_include_decoded",
                    fallback=getattr(self, "include_decoded", False),
                ),
                "jwt_renewal_interval": jwt_renewal_interval,
                "jwt_ttl_seconds": jwt_ttl_seconds,
                "jwt_reconnect_on_renew": config.getboolean(
                    "PacketCapture", f"mqtt{broker_num}_jwt_reconnect_on_renew", fallback=True
                ),
            }

            # Set default topic_prefix if not set
            if not broker["topic_prefix"]:
                broker["topic_prefix"] = "meshcore/packets"

            brokers.append(broker)
            broker_num += 1

        return brokers

    def get_config_bool(self, key: str, fallback: bool = False) -> bool:
        """Get boolean config value.

        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.

        Returns:
            bool: Config value or fallback.
        """
        return self.bot.config.getboolean("PacketCapture", key, fallback=fallback)

    def get_config_int(self, key: str, fallback: int = 0) -> int:
        """Get integer config value.

        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.

        Returns:
            int: Config value or fallback.
        """
        return self.bot.config.getint("PacketCapture", key, fallback=fallback)

    def get_config_float(self, key: str, fallback: float = 0.0) -> float:
        """Get float config value.

        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.

        Returns:
            float: Config value or fallback.
        """
        return self.bot.config.getfloat("PacketCapture", key, fallback=fallback)

    def get_config_str(self, key: str, fallback: str = "") -> str:
        """Get string config value.

        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.

        Returns:
            str: Config value or fallback.
        """
        return self.bot.config.get("PacketCapture", key, fallback=fallback)

    def _apply_log_level(self) -> None:
        """Set service logger level from PacketCapture verbose/debug, not global bot log_level."""
        if self.debug:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)

    def _log_packet_summary(self, message: str) -> None:
        """Log per-packet summary when verbose or debug is enabled."""
        if not (self.verbose or self.debug):
            return
        if self.debug:
            self.logger.debug(message)
        else:
            self.logger.info(message)

    def _auth_token_iat_exp(self, broker_config: dict[str, Any]) -> tuple[int, int]:
        """Unix iat/exp for JWT payload (exp = iat + ttl). Non-positive TTL uses 86400s."""
        iat = int(time.time())
        ttl = int(broker_config.get("jwt_ttl_seconds", self.jwt_ttl_seconds))
        if ttl <= 0:
            ttl = 86400
        return iat, iat + ttl

    @staticmethod
    def _utc_iso_timestamp() -> str:
        """UTC ISO 8601 timestamp with Z suffix for broad consumer compatibility."""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _disconnect_reason(rc: int) -> str:
        """Describe an on_disconnect return code.

        paho reports MQTT_ERR_* here, not CONNACK codes, so a bare number reads
        as the wrong thing entirely: rc=2 is a protocol error, not "client
        identifier rejected".
        """
        try:
            return f"rc={rc}: {mqtt.error_string(rc)}"
        except Exception:
            return f"rc={rc}"

    @staticmethod
    def _jwt_ttl_log_phrase(ttl_seconds: int) -> str:
        """Short TTL description for log lines."""
        if ttl_seconds >= 3600 and ttl_seconds % 3600 == 0:
            h = ttl_seconds // 3600
            return f"{h} hours" if h != 1 else "1 hour"
        if ttl_seconds >= 60 and ttl_seconds % 60 == 0:
            m = ttl_seconds // 60
            return f"{m} minutes" if m != 1 else "1 minute"
        return f"{ttl_seconds}s"

    @property
    def meshcore(self):
        """Get meshcore connection from bot (always current).

        Returns:
            MeshCore: The meshcore instance from the bot.
        """
        return self.bot.meshcore if self.bot else None

    def is_healthy(self) -> bool:
        # Only check internal running state, not radio connection.
        # Radio disconnects are transient — the service resumes naturally
        # when the radio reconnects. Checking meshcore.is_connected here
        # causes a restart storm during every radio reconnect cycle.
        return self._running

    async def start(self) -> None:
        """Start the packet capture service.

        Initializes output file, MQTT connections, and event handlers.
        Waits for bot connection before starting.
        """
        if not self.enabled:
            self.logger.info("Packet capture service is disabled")
            return

        # Wait for bot to be connected (with timeout)
        max_wait = 30  # seconds
        wait_time: float = 0
        while (not self.bot.connected or not self.meshcore) and wait_time < max_wait:
            await asyncio.sleep(0.5)
            wait_time += 0.5

        if not self.bot.connected or not self.meshcore:
            self.logger.warning("Bot not connected after waiting, cannot start packet capture")
            return

        self.logger.info("Starting packet capture service...")

        # Open output file if specified (with optional size/time rotation)
        if self.output_file:
            try:
                self.output_handle = _RotatingPacketLog(
                    self.output_file,
                    rotation=self.log_rotation,
                    max_bytes=self.log_max_bytes,
                    backup_count=self.log_backup_count,
                    when=self.log_rotation_when,
                )
                rotation_note = "" if self.log_rotation == "off" else f" (rotation: {self.log_rotation})"
                self.logger.info(f"Writing packets to: {self.output_file}{rotation_note}")
            except Exception as e:
                self.logger.error(f"Failed to open output file: {e}")

        # Setup event handlers
        await self.setup_event_handlers()

        # Connect to MQTT brokers
        if self.mqtt_enabled and self._require_mqtt():
            await self.connect_mqtt_brokers()
            # Give MQTT connections a moment to establish
            await asyncio.sleep(2)
            if self.mqtt_connected:
                self.logger.info(f"MQTT connected to {len(self.mqtt_clients)} broker(s)")
            else:
                self.logger.warning("MQTT enabled but no brokers connected")

        # Start background tasks
        await self.start_background_tasks()

        self.connected = True
        self._running = True
        self.logger.info(
            f"Packet capture service started (MQTT: {'connected' if self.mqtt_connected else 'not connected'})"
        )

    async def on_transport_reconnected(self) -> None:
        """Re-register RX_LOG_DATA/RAW_DATA handlers on the new meshcore instance."""
        if not self._running or not self.meshcore:
            return
        self.cleanup_event_subscriptions()
        await self.setup_event_handlers()
        self.logger.info("Packet capture event handlers re-registered after transport reconnect")

    async def stop(self) -> None:
        """Stop the packet capture service.

        Closes output file, disconnects MQTT, and stops background tasks.
        """
        self.logger.info("Stopping packet capture service...")

        self.should_exit = True
        self._running = False
        self.connected = False

        # Cancel background tasks
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Clean up event subscriptions
        self.cleanup_event_subscriptions()

        # Disconnect MQTT
        for mqtt_client_info in self.mqtt_clients:
            try:
                mqtt_client_info["client"].disconnect()
                mqtt_client_info["client"].loop_stop()
            except (AttributeError, RuntimeError, OSError) as e:
                # Silently ignore expected errors during cleanup (client already disconnected, etc.)
                self.logger.debug(f"Error disconnecting MQTT client during cleanup: {e}")
            except Exception as e:
                # Log unexpected errors but don't fail cleanup
                self.logger.warning(f"Unexpected error disconnecting MQTT client: {e}")

        # Close output file
        if self.output_handle:
            self.output_handle.close()
            self.output_handle = None

        self.logger.info(f"Packet capture service stopped. Total packets captured: {self.packet_count}")

    def cleanup_event_subscriptions(self) -> None:
        """Clean up event subscriptions.

        Clears local subscription tracking list.
        """
        # Note: meshcore library handles subscription cleanup automatically
        # This is mainly for tracking/logging
        self.event_subscriptions = []

    async def setup_event_handlers(self) -> None:
        """Setup event handlers for packet capture.

        Subscribes to RX_LOG_DATA and RAW_DATA events.
        """
        if not self.meshcore:
            return

        # Handle RX log data
        async def on_rx_log_data(event, metadata=None):
            await self.handle_rx_log_data(event, metadata)

        # Handle raw data
        async def on_raw_data(event, metadata=None):
            await self.handle_raw_data(event, metadata)

        # Subscribe to events (meshcore supports multiple subscribers)
        self.meshcore.subscribe(EventType.RX_LOG_DATA, on_rx_log_data)
        self.meshcore.subscribe(EventType.RAW_DATA, on_raw_data)

        self.event_subscriptions = [(EventType.RX_LOG_DATA, on_rx_log_data), (EventType.RAW_DATA, on_raw_data)]

        self.logger.info("Packet capture event handlers registered")

    async def handle_rx_log_data(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
        """Handle RX log data events (matches original script).

        Args:
            event: The RX log data event.
            metadata: Optional metadata dictionary.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if payload is None:
                self.logger.warning("RX log data event has no payload")
                return

            if "snr" in payload:
                # Try to get packet data - prefer 'payload' field, fallback to 'raw_hex'
                # This matches the original script's logic exactly
                raw_hex = None

                # First, try the 'payload' field (already stripped of framing bytes)
                if "payload" in payload and payload["payload"]:
                    raw_hex = payload["payload"]
                # Fallback to raw_hex with first 2 bytes stripped
                elif "raw_hex" in payload and payload["raw_hex"]:
                    raw_hex = payload["raw_hex"][4:]  # Skip first 2 bytes (4 hex chars)

                if raw_hex:
                    if self.debug:
                        self.logger.debug(f"Received RX_LOG_DATA: {raw_hex[:50]}...")

                    # Correlate with RAW_DATA: cache SNR/RSSI for prefix; record hex for dedupe
                    # (meshcore-packet-capture: recent_rf_packets + rf_data_cache)
                    current_time = time.time()
                    # Both correlation caches are keyed on UPPERCASE hex: this
                    # payload arrives lowercase but handle_raw_data uppercases
                    # before looking up, so the cases must be normalized here.
                    raw_hex_key = raw_hex.upper()
                    packet_prefix = raw_hex_key[:32] if len(raw_hex_key) >= 32 else raw_hex_key
                    self.rf_data_cache[packet_prefix] = {
                        "snr": payload.get("snr"),
                        "rssi": payload.get("rssi"),
                        "timestamp": current_time,
                        "payload_length": payload.get("payload_length"),
                    }
                    self.recent_rf_packets[raw_hex_key] = current_time
                    self._prune_correlation_caches(current_time)

                    # Process packet
                    await self.process_packet(raw_hex, payload, metadata)
                else:
                    self.logger.warning(
                        f"RF log data missing both 'payload' and 'raw_hex' fields: {list(payload.keys())}"
                    )

        except Exception as e:
            self.logger.error(f"Error handling RX log data: {e}")

    async def handle_raw_data(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
        """Handle raw data events.

        Aligns with meshcore-packet-capture: resolve data like reference, dedupe against
        RX_LOG_DATA when the same hex was just processed, merge cached RF metadata by prefix.

        Args:
            event: The raw data event.
            metadata: Optional metadata dictionary.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if payload is None:
                self.logger.warning("Raw data event has no payload")
                return

            raw_hex_src = None
            if isinstance(payload, dict):
                # meshcore's reader dispatches RAW_DATA as
                # {"SNR", "RSSI", "payload": "<hex>"} — "payload" is the real
                # field; "data"/"raw_hex" are only kept for other producers.
                for field in ("payload", "data", "raw_hex"):
                    if payload.get(field):
                        raw_hex_src = payload[field]
                        break
            elif hasattr(payload, "data"):
                raw_hex_src = payload.data

            if raw_hex_src is None:
                return

            if isinstance(raw_hex_src, bytes):
                raw_hex = raw_hex_src.hex()
            elif isinstance(raw_hex_src, str):
                raw_hex = raw_hex_src
                if raw_hex.startswith("0x"):
                    raw_hex = raw_hex[2:]
            else:
                return

            raw_hex = raw_hex.upper()
            current_time = time.time()

            recent_rf_time = self.recent_rf_packets.get(raw_hex)
            if recent_rf_time is not None and (current_time - recent_rf_time) < self.raw_duplicate_window:
                if self.debug:
                    self.logger.debug("Skipping RAW_DATA packet already processed from RX_LOG_DATA (duplicate raw hex)")
                return

            self.recent_rf_packets = {
                k: v for k, v in self.recent_rf_packets.items() if current_time - v < self.raw_duplicate_window
            }

            packet_prefix = raw_hex[:32] if len(raw_hex) >= 32 else raw_hex
            rf_cached = self.rf_data_cache.get(packet_prefix)

            merged_payload: dict[str, Any]
            if isinstance(payload, dict):
                merged_payload = dict(payload)
            else:
                merged_payload = {}

            # RAW_DATA carries "SNR"/"RSSI"; RX_LOG_DATA and _format_packet_data
            # use the lowercase spelling, so fold the event's own values in
            # first — they are more specific than the prefix-matched cache.
            for upper, lower in (("SNR", "snr"), ("RSSI", "rssi")):
                if merged_payload.get(lower) is None and merged_payload.get(upper) is not None:
                    merged_payload[lower] = merged_payload[upper]

            if rf_cached:
                if merged_payload.get("snr") is None:
                    merged_payload["snr"] = rf_cached.get("snr")
                if merged_payload.get("rssi") is None:
                    merged_payload["rssi"] = rf_cached.get("rssi")
                pl = merged_payload.get("payload_length")
                if pl is None:
                    merged_payload["payload_length"] = rf_cached.get("payload_length")

            await self.process_packet(raw_hex, merged_payload, metadata)

        except Exception as e:
            self.logger.error(f"Error handling raw data: {e}")

    def _format_packet_data(
        self, raw_hex: str, packet_info: dict[str, Any], payload: dict[str, Any], metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Format packet data to match original script's format_packet_data exactly.

        Args:
            raw_hex: Raw hex string of the packet.
            packet_info: Decoded packet information.
            payload: Payload dictionary from the event.
            metadata: Optional metadata dictionary.

        Returns:
            dict[str, Any]: Formatted packet dictionary.
        """
        current_time = datetime.now()
        timestamp = self._utc_iso_timestamp()

        # Remove 0x prefix if present
        clean_raw_hex = raw_hex.replace("0x", "").upper()
        packet_len = len(clean_raw_hex) // 2  # Convert hex string to byte count

        # Map route type to single letter (matches original script)
        route_map = {"TRANSPORT_FLOOD": "F", "FLOOD": "F", "DIRECT": "D", "TRANSPORT_DIRECT": "T", "UNKNOWN": "U"}
        route = route_map.get(packet_info.get("route_type", "UNKNOWN"), "U")

        # Map payload type to string number (matches original script)
        payload_type_map = {
            "REQ": "0",
            "RESPONSE": "1",
            "TXT_MSG": "2",
            "ACK": "3",
            "ADVERT": "4",
            "GRP_TXT": "5",
            "GRP_DATA": "6",
            "ANON_REQ": "7",
            "PATH": "8",
            "TRACE": "9",
            "MULTIPART": "10",
            "Type11": "11",
            "Type12": "12",
            "Type13": "13",
            "Type14": "14",
            "RAW_CUSTOM": "15",
            "UNKNOWN": "0",
        }
        packet_type = payload_type_map.get(packet_info.get("payload_type", "UNKNOWN"), "0")

        # MQTT payload_len: byte length of application payload after header/transport/path.
        # Subtract path *bytes*, not hop count (multi-byte path IDs); prefer decoded size.
        firmware_payload_len = payload.get("payload_length")
        decoded_ok = packet_info.get("payload_type", "UNKNOWN") != "UNKNOWN"
        if decoded_ok and "payload_bytes" in packet_info:
            payload_len = str(packet_info["payload_bytes"])
        elif firmware_payload_len is not None:
            payload_len = str(firmware_payload_len)
        else:
            path_bytes = packet_info.get("path_byte_length")
            if path_bytes is None:
                path_bytes = packet_info.get("path_len", 0)
            has_transport = packet_info.get("has_transport_codes", False)
            transport_bytes = 4 if has_transport else 0
            payload_len = str(max(0, packet_len - 1 - transport_bytes - 1 - path_bytes))

        # Get device name and public key
        device_name = self._get_observer_name()
        if not device_name:
            device_name = "MeshCore Device"

        # Get device public key for origin_id
        origin_id = None
        if self.meshcore and hasattr(self.meshcore, "self_info"):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    origin_id = self_info.get("public_key", "")
                elif hasattr(self_info, "public_key"):
                    origin_id = self_info.public_key

                # Convert to hex string if bytes
                if isinstance(origin_id, bytes):
                    origin_id = origin_id.hex()
                elif isinstance(origin_id, bytearray):
                    origin_id = bytes(origin_id).hex()
            except Exception:
                pass

        # Normalize origin_id to uppercase
        origin_id = origin_id.replace("0x", "").replace(" ", "").upper() if origin_id else "UNKNOWN"

        # Extract RF data
        snr = str(payload.get("snr", "Unknown"))
        rssi = str(payload.get("rssi", "Unknown"))

        # Get packet hash from decoded packet_info — same clean bytes as the upload's "raw" field,
        # so this is always the correct hash (matches what other observers compute).
        packet_hash = packet_info.get("packet_hash", "0000000000000000")

        # Only fall back to direct calculation if decode_packet didn't produce a hash
        if packet_hash == "0000000000000000":
            try:
                payload_type_value = packet_info.get("payload_type_value")
                if payload_type_value is not None:
                    if hasattr(payload_type_value, "value"):
                        payload_type_value = payload_type_value.value
                    payload_type_value = int(payload_type_value) & 0x0F
                packet_hash = calculate_packet_hash(clean_raw_hex, payload_type_value)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Error calculating packet hash: {e}")
                packet_hash = "0000000000000000"

        # Build packet data structure (matches original script exactly)
        packet_data = {
            "origin": device_name,
            "origin_id": origin_id,
            "timestamp": timestamp,
            "type": "PACKET",
            "direction": "rx",
            "time": current_time.strftime("%H:%M:%S"),
            "date": current_time.strftime("%d/%m/%Y"),
            "len": str(packet_len),
            "packet_type": packet_type,
            "route": route,
            "payload_len": payload_len,
            "raw": clean_raw_hex,
            "SNR": snr,
            "RSSI": rssi,
            "hash": packet_hash,
        }

        # Add optional fields from payload if present (score, duration, etc.)
        if "score" in payload:
            packet_data["score"] = str(payload["score"])
        if "duration" in payload:
            packet_data["duration"] = str(payload["duration"])

        # Add path for route=D (matches original script)
        if route == "D" and packet_info.get("path"):
            packet_data["path"] = ",".join(packet_info["path"])

        # Attach decoded payload (issues #197 & #35): plain text / structured fields.
        # Only payload-specific content goes here — header fields (packet_type, route)
        # already exist at the top level, so we don't restate them.
        if self.decode_payloads and self.channel_key_store is not None:
            try:
                payload_type_value = packet_info.get("payload_type_value", 0)
                if hasattr(payload_type_value, "value"):
                    payload_type_value = payload_type_value.value
                payload_bytes = bytes.fromhex(packet_info.get("payload_hex", "") or "")
                decoded = decode_payload(int(payload_type_value), payload_bytes, self.channel_key_store)
                # Include the decoded hop path only when it isn't already at the top level
                # (top-level "path" is added for route=D) — captures flood paths without duplicating.
                if packet_info.get("path") and "path" not in packet_data:
                    decoded["path"] = list(packet_info["path"])
                packet_data["decoded"] = decoded
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Payload decode failed: {e}")

        return packet_data

    async def process_packet(
        self, raw_hex: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None
    ) -> None:
        """Process a captured packet.

        Decodes the packet, formats it, writes to file, and publishes to MQTT.

        Args:
            raw_hex: Raw hex string of the packet.
            payload: Payload dictionary from the event.
            metadata: Optional metadata dictionary.
        """
        try:
            self.packet_count += 1

            # Extract packet information (decode may fail, but we still publish)
            packet_info = self.decode_packet(raw_hex, payload)

            # If decode failed, create minimal packet_info with defaults (matches original script)
            if not packet_info:
                if self.debug:
                    self.logger.debug(
                        f"Packet {self.packet_count} decode failed, using defaults (raw_hex: {raw_hex[:50]}...)"
                    )
                # Try to calculate packet hash even if decode failed (extract payload_type from header if possible)
                packet_hash = "0000000000000000"
                payload_type_value = None
                try:
                    # Try to extract payload type from header for hash calculation
                    byte_data = bytes.fromhex(raw_hex.replace("0x", ""))
                    if len(byte_data) >= 1:
                        header = byte_data[0]
                        payload_type_value = (header >> 2) & 0x0F
                        packet_hash = calculate_packet_hash(raw_hex.replace("0x", ""), payload_type_value)
                except Exception:
                    pass  # Use default hash if calculation fails

                # Create minimal packet info with defaults (matches original script's format_packet_data)
                packet_info = {
                    "route_type": "UNKNOWN",
                    "payload_type": "UNKNOWN",
                    "payload_type_value": payload_type_value or 0,
                    "payload_version": 0,
                    "path_len": 0,
                    "path_byte_length": 0,
                    "path_hex": "",
                    "path": [],
                    "payload_hex": raw_hex.replace("0x", ""),
                    "payload_bytes": len(raw_hex.replace("0x", "")) // 2,
                    "raw_hex": raw_hex.replace("0x", ""),
                    "packet_hash": packet_hash,
                    "has_transport_codes": False,
                    "transport_codes": None,
                }

            # Format packet data to match original script's format
            formatted_packet = self._format_packet_data(raw_hex, packet_info, payload, metadata)

            skip_mqtt_unparseable = (
                self.mqtt_skip_unparseable_packets and formatted_packet.get("hash") == "0000000000000000"
            )

            skip_mqtt_invalid_advert_signature = False
            if self.advert_require_valid_signature and packet_info.get("payload_type") == PayloadType.ADVERT.name:
                try:
                    mesh_payload = bytes.fromhex(packet_info.get("payload_hex", ""))
                    if not verify_meshcore_advert_ed25519(mesh_payload):
                        skip_mqtt_invalid_advert_signature = True
                except Exception:
                    skip_mqtt_invalid_advert_signature = True

            skip_mqtt = skip_mqtt_unparseable or skip_mqtt_invalid_advert_signature

            # Write to file
            if self.output_handle:
                self.output_handle.write_line(json.dumps(formatted_packet, default=str))

            # Publish to MQTT if enabled
            # The publish function will check per-broker connection status
            publish_metrics: dict[str, Any] = {
                "attempted": 0,
                "succeeded": 0,
                "skipped_by_filter": False,
                "skipped_unparseable": False,
                "skipped_invalid_advert_signature": False,
            }
            if self.mqtt_enabled and not skip_mqtt:
                if self.debug:
                    self.logger.debug(f"Calling publish_packet_mqtt for packet {self.packet_count}")
                publish_metrics = await self.publish_packet_mqtt(formatted_packet)
                publish_metrics.setdefault("skipped_unparseable", False)
                publish_metrics.setdefault("skipped_invalid_advert_signature", False)
            elif self.mqtt_enabled and skip_mqtt:
                publish_metrics["skipped_unparseable"] = skip_mqtt_unparseable
                publish_metrics["skipped_invalid_advert_signature"] = skip_mqtt_invalid_advert_signature

            # Per-packet summary: INFO when verbose, DEBUG when debug (lifecycle stays INFO)
            if publish_metrics.get("skipped_unparseable"):
                action = "Captured (MQTT skipped: zero hash / unparseable)"
            elif publish_metrics.get("skipped_invalid_advert_signature"):
                action = "Captured (MQTT skipped: invalid advert signature)"
            elif publish_metrics.get("skipped_by_filter"):
                action = "Skipping"
            else:
                action = "Captured"
            self._log_packet_summary(
                f"📦 {action} packet #{self.packet_count}: {formatted_packet['route']} type {formatted_packet['packet_type']}, {formatted_packet['len']} bytes, SNR: {formatted_packet['SNR']}, RSSI: {formatted_packet['RSSI']}, hash: {formatted_packet['hash']} (MQTT: {publish_metrics['succeeded']}/{publish_metrics['attempted']})"
            )

            # Output full packet data structure in debug mode only (matches original script)
            if self.debug:
                self.logger.debug("📋 Full packet data structure:")
                self.logger.debug(json.dumps(formatted_packet, indent=2))

        except Exception as e:
            self.logger.error(f"Error processing packet: {e}")

    def decode_packet(self, raw_hex: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Decode a MeshCore packet - matches original packet_capture.py functionality.

        Args:
            raw_hex: Raw hex string of the packet.
            payload: Payload dictionary from the event (unused in this method but kept for compatibility).

        Returns:
            dict[str, Any] | None: Decoded packet info, or None if decoding fails.
        """
        try:
            # Remove 0x prefix if present
            if raw_hex.startswith("0x"):
                raw_hex = raw_hex[2:]

            byte_data = bytes.fromhex(raw_hex)

            if len(byte_data) < 2:
                if self.debug:
                    self.logger.debug(f"Packet too short ({len(byte_data)} bytes), cannot decode")
                return None

            header = byte_data[0]

            # Extract route type
            route_type = RouteType(header & 0x03)
            has_transport = route_type in [RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT]

            # Extract transport codes if present
            transport_codes = None
            offset = 1
            if has_transport and len(byte_data) >= 5:
                transport_bytes = byte_data[1:5]
                transport_codes = {
                    "code1": int.from_bytes(transport_bytes[0:2], byteorder="little"),
                    "code2": int.from_bytes(transport_bytes[2:4], byteorder="little"),
                    "hex": transport_bytes.hex(),
                }
                offset = 5

            if len(byte_data) <= offset:
                if self.debug:
                    self.logger.debug(
                        f"Packet too short after transport codes ({len(byte_data)} bytes, offset {offset}), cannot decode"
                    )
                return None

            path_len_byte = byte_data[offset]
            offset += 1
            path_parts = decode_path_len_byte(path_len_byte)
            if path_parts is None:
                if self.debug:
                    self.logger.debug("Packet invalid path_len encoding (not firmware-valid), cannot decode")
                return None
            path_byte_length, bytes_per_hop = path_parts

            if len(byte_data) < offset + path_byte_length:
                if self.debug:
                    self.logger.debug(
                        f"Packet too short for path ({len(byte_data)} bytes, need {offset + path_byte_length}), cannot decode"
                    )
                return None

            # Extract path
            path_bytes = byte_data[offset : offset + path_byte_length]
            offset += path_byte_length

            # Chunk path by bytes_per_hop from packet (1, 2, or 3); odd remainder → 1-byte chunks
            hex_chars = bytes_per_hop * 2
            path_hex = path_bytes.hex()
            path_nodes = [path_hex[i : i + hex_chars].upper() for i in range(0, len(path_hex), hex_chars)]
            if (len(path_hex) % hex_chars) != 0 or not path_nodes:
                path_nodes = [path_hex[i : i + 2].upper() for i in range(0, len(path_hex), 2)]

            # Remaining data is payload
            packet_payload = byte_data[offset:]

            # Extract payload version
            payload_version = PayloadVersion((header >> 6) & 0x03)

            if payload_version != PayloadVersion.VER_1:
                if self.debug:
                    self.logger.debug(f"Unsupported payload version: {payload_version} (expected VER_1), skipping")
                return None

            # Extract payload type
            payload_type = PayloadType((header >> 2) & 0x0F)

            # Calculate packet hash (for tracking same message via different paths)
            packet_hash = calculate_packet_hash(raw_hex, payload_type.value)

            # Build packet info (matching original format)
            packet_info = {
                "header": f"0x{header:02x}",
                "route_type": route_type.name,
                "route_type_value": route_type.value,
                "payload_type": payload_type.name,
                "payload_type_value": payload_type.value,
                "payload_version": payload_version.value,
                "path_len": len(path_nodes),
                "path_byte_length": path_byte_length,
                "bytes_per_hop": bytes_per_hop,
                "path_hex": path_hex,
                "path": path_nodes,  # For TRACE, RF path is SNR×4 per hop — replaced below
                "payload_hex": packet_payload.hex(),
                "payload_bytes": len(packet_payload),
                "raw_hex": raw_hex,
                "packet_hash": packet_hash,
                "has_transport_codes": has_transport,
                "transport_codes": transport_codes,
            }

            # TRACE: RF path bytes are SNR samples; commanded route is in payload[9:]
            if payload_type == PayloadType.TRACE:
                packet_info["trace_snr_path_hex"] = path_hex.upper()
                trace_route = parse_trace_payload_route_hashes(packet_payload)
                if trace_route:
                    packet_info["path"] = trace_route
                    packet_info["path_len"] = len(trace_route)

            return packet_info

        except Exception as e:
            self.logger.debug(f"Error decoding packet: {e} (raw_hex: {raw_hex[:50]}...)")
            import traceback

            if self.debug:
                self.logger.debug(f"Decode error traceback: {traceback.format_exc()}")
            return None

    def _get_observer_name(self) -> str:
        """Get observer name for PacketCapture MQTT reporting.

        Allows the observer/analyzer identity to differ from the
        MeshCore RF node and bot name.
        """
        observer_name = self.bot.config.get(
            "PacketCapture",
            "observer_name",
            fallback=""
        ).strip()

        if observer_name:
            return observer_name

        return self._get_bot_name()

    def _get_bot_name(self) -> str:
        """Get bot name from device or config.

        Returns:
            str: The name of the bot/device.
        """
        # Try to get name from device first
        if self.meshcore and hasattr(self.meshcore, "self_info"):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    device_name = self_info.get("name") or self_info.get("adv_name")
                    if device_name:
                        return device_name
                elif hasattr(self_info, "name"):
                    if self_info.name:
                        return self_info.name
                elif hasattr(self_info, "adv_name"):
                    if self_info.adv_name:
                        return self_info.adv_name
            except Exception as e:
                self.logger.debug(f"Could not get name from device: {e}")

        # Fallback to config
        bot_name = self.bot.config.get("Bot", "bot_name", fallback="MeshCoreBot")
        return bot_name

    def _require_mqtt(self) -> bool:
        """Check if MQTT is available and required.

        Returns:
            bool: True if MQTT requirements are met, False otherwise.
        """
        if mqtt is None:
            self.logger.warning("MQTT support not available. Install paho-mqtt: pip install paho-mqtt")
            return False
        return True

    async def connect_mqtt_brokers(self) -> None:
        """Connect to MQTT brokers.

        Establish connections to all configured MQTT brokers.
        """
        if not self._require_mqtt():
            return

        # Get bot name for client ID
        bot_name = self._get_bot_name()

        for broker_config in self.mqtt_brokers:
            if not broker_config.get("enabled", True):
                continue

            try:
                # Use configured client_id, or generate from bot name
                client_id = broker_config.get("client_id")
                if not client_id:
                    # Sanitize bot name for MQTT client ID (alphanumeric and hyphens only)
                    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in bot_name)
                    # The broker number keeps the ID distinct per broker. Two hosts that
                    # share a session store — mqtt-a and mqtt-b of one cluster — evict
                    # each other's session when both connect under the same ID, which
                    # reads in the log as the two brokers flapping in lockstep.
                    client_id = f"{safe_name}-packet-capture-{broker_config.get('broker_num', 1)}-{os.getpid()}"

                # Create client based on transport type
                transport = broker_config.get("transport", "tcp").lower()
                if transport == "websockets":
                    try:
                        client = mqtt.Client(client_id=client_id, transport="websockets")
                        # Set WebSocket path (must be done before connect)
                        ws_path = broker_config.get("websocket_path", "/mqtt")
                        client.ws_set_options(path=ws_path, headers=None)
                    except Exception as e:
                        self.logger.error(f"WebSockets transport not available: {e}")
                        continue
                else:
                    client = mqtt.Client(client_id=client_id)

                # Enable paho-mqtt's built-in automatic reconnection (matches original script)
                client.reconnect_delay_set(min_delay=1, max_delay=120)

                # Set TLS if enabled
                if broker_config.get("use_tls", False):
                    try:
                        import ssl

                        # For WebSockets with TLS (WSS), we need to set TLS on the client
                        # The TLS handshake happens during the WebSocket upgrade
                        if broker_config.get("tls_insecure", False):
                            # Explicitly opted out — accepts self-signed certs.
                            client.tls_set(cert_reqs=ssl.CERT_NONE)
                            client.tls_insecure_set(True)
                            self.logger.warning(
                                "TLS certificate verification is DISABLED for %s "
                                "(mqtt%s_tls_insecure = true) — credentials are exposed to a MITM",
                                broker_config["host"], broker_config.get("broker_num", "N"),
                            )
                        else:
                            # Verify certificate and hostname against the system
                            # trust store; the credentials set below would
                            # otherwise be readable by any interceptor.
                            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                        if self.debug:
                            self.logger.debug(f"TLS enabled for {broker_config['host']} ({transport})")
                    except Exception as e:
                        self.logger.warning(f"TLS setup failed for {broker_config['host']}: {e}")

                # Set username/password if provided
                username = broker_config.get("username")
                password = broker_config.get("password")
                token_exp = None

                if broker_config.get("use_auth_token"):
                    # Use auth token with audience if specified
                    token_audience = broker_config.get("token_audience") or broker_config["host"]

                    # Get device's public key (from self_info) - this is what we use for username and JWT publicKey
                    # The owner_public_key is ONLY for the 'owner' field in the JWT payload
                    device_public_key_hex = None
                    if self.meshcore and hasattr(self.meshcore, "self_info"):
                        try:
                            self_info = self.meshcore.self_info
                            if isinstance(self_info, dict):
                                device_public_key_hex = self_info.get("public_key", "")
                            elif hasattr(self_info, "public_key"):
                                device_public_key_hex = self_info.public_key

                            # Convert to hex string if bytes
                            if isinstance(device_public_key_hex, bytes):
                                device_public_key_hex = device_public_key_hex.hex()
                            elif isinstance(device_public_key_hex, bytearray):
                                device_public_key_hex = bytes(device_public_key_hex).hex()
                        except Exception as e:
                            self.logger.debug(f"Could not get public key from device: {e}")

                    if not device_public_key_hex:
                        self.logger.warning(
                            f"No device public key available for auth token (broker: {broker_config['host']})"
                        )
                        continue

                    # Create auth token (tries on-device signing first if available)
                    use_device = self.auth_token_method == "device" and self.meshcore and self.meshcore.is_connected

                    # For Python signing, we still need meshcore_instance to fetch the private key
                    # The use_device flag only controls whether we try on-device signing first
                    meshcore_for_key_fetch = self.meshcore if self.meshcore and self.meshcore.is_connected else None

                    try:
                        # Use v1_{device_public_key} format for username (device's actual key, not owner key)
                        if not username:
                            username = f"v1_{device_public_key_hex.upper()}"

                        iat, exp = self._auth_token_iat_exp(broker_config)
                        ttl_used = exp - iat
                        token = await create_auth_token_async(
                            meshcore_instance=meshcore_for_key_fetch,
                            public_key_hex=device_public_key_hex,  # Device's actual public key (for JWT publicKey field)
                            private_key_hex=self.private_key_hex,
                            iata=self.global_iata,
                            timestamp=iat,
                            audience=token_audience,
                            exp=exp,
                            owner_public_key=self.owner_public_key,  # Owner's key (only for 'owner' field in JWT)
                            owner_email=self.owner_email,
                            use_device=use_device,
                        )
                        if token:
                            password = token
                            token_exp = exp
                            ttl_phrase = self._jwt_ttl_log_phrase(ttl_used)
                            self.logger.debug(
                                f"Created auth token for {broker_config['host']} "
                                f"(username: {username}, TTL {ttl_phrase}) "
                                f"using {'device' if use_device else 'Python'} signing"
                            )
                        else:
                            self.logger.warning(f"Failed to create auth token for {broker_config['host']}")
                    except Exception as e:
                        self.logger.error(f"Error creating auth token for {broker_config['host']}: {e}")

                if username:
                    client.username_pw_set(username, password)

                # Setup callbacks (resolve broker from mqtt_clients — avoid late-bound loop vars)
                def on_connect(client, userdata, flags, rc, properties=None):
                    cfg = None
                    for mqtt_info in self.mqtt_clients:
                        if mqtt_info["client"] == client:
                            cfg = mqtt_info["config"]
                            break
                    if cfg is None:
                        return
                    tr = cfg.get("transport", "tcp").lower()
                    host, port = cfg["host"], cfg["port"]
                    if rc == 0:
                        self.logger.info(f"✓ Connected to MQTT broker: {host}:{port} ({tr})")
                        # Track connection per broker
                        for mqtt_info in self.mqtt_clients:
                            if mqtt_info["client"] == client:
                                mqtt_info["connected"] = True
                                mqtt_info["down_since"] = None
                                mqtt_info["stall_warned"] = False
                                break
                        # Set global connected flag if any broker is connected
                        self.mqtt_connected = any(m.get("connected", False) for m in self.mqtt_clients)
                    else:
                        # CONNACK return codes (MQTT 3.1.1 §3.2.2.3). These are NOT the
                        # MQTT_ERR_* codes on_disconnect reports — see _disconnect_reason.
                        error_messages = {
                            1: "protocol version rejected",
                            2: "client identifier rejected",
                            3: "server unavailable",
                            4: "bad username or password",
                            5: "not authorized",
                        }
                        error_msg = error_messages.get(rc, f"unknown error ({rc})")
                        self.logger.error(f"✗ Failed to connect to MQTT broker {host}: {rc} ({error_msg})")
                        # Mark this broker as disconnected
                        for mqtt_info in self.mqtt_clients:
                            if mqtt_info["client"] == client:
                                mqtt_info["connected"] = False
                                break
                        # Update global flag
                        self.mqtt_connected = any(m.get("connected", False) for m in self.mqtt_clients)

                def on_disconnect(client, userdata, rc, properties=None):
                    # Mark this broker as disconnected
                    for mqtt_info in self.mqtt_clients:
                        if mqtt_info["client"] == client:
                            mqtt_info["connected"] = False
                            cfg = mqtt_info["config"]
                            host = cfg["host"]
                            if rc != 0:
                                self.logger.warning(
                                    f"Disconnected from MQTT broker {host} ({self._disconnect_reason(rc)})"
                                )
                            else:
                                self.logger.debug(f"Disconnected from MQTT broker {host}")
                            break
                    # Update global flag
                    self.mqtt_connected = any(m.get("connected", False) for m in self.mqtt_clients)

                client.on_connect = on_connect
                client.on_disconnect = on_disconnect

                # Connect
                try:
                    host = broker_config["host"]
                    port = broker_config["port"]
                    keepalive = int(broker_config.get("keepalive", 60))

                    # Validate hostname (basic check)
                    if not host or not host.strip():
                        self.logger.error(f"Invalid MQTT broker hostname: '{host}'")
                        continue

                    # Try to resolve hostname first (for better error messages)
                    try:
                        import socket

                        socket.gethostbyname(host)
                    except socket.gaierror as dns_error:
                        # Only log DNS errors at debug level, not as errors
                        if self.debug:
                            self.logger.debug(f"DNS resolution check for '{host}': {dns_error}")
                        # Continue anyway - connection attempt will show actual error
                    except Exception as resolve_error:
                        if self.debug:
                            self.logger.debug(f"Hostname resolution check for '{host}': {resolve_error}")

                    # Add client to list BEFORE starting the loop, so callbacks can find it
                    self.mqtt_clients.append(
                        {
                            "client": client,
                            "config": broker_config,
                            "connected": False,  # Track connection status per broker
                            # Expiry of the credential currently set on the client, so
                            # the monitor can refresh it before paho's next retry.
                            "token_exp": token_exp,
                            # Set when the client first goes down; cleared on connect.
                            "down_since": None,
                            "stall_warned": False,
                            # Serializes disconnect/loop_stop/reconnect/loop_start so
                            # the renewal task and the monitor cannot overlap.
                            "cycle_lock": asyncio.Lock(),
                        }
                    )

                    if transport == "websockets":
                        # WebSocket path already set via ws_set_options above
                        ws_path = broker_config.get("websocket_path", "/mqtt")
                        self.logger.debug(
                            f"Connecting to MQTT broker {host}:{port} via WebSockets (path: {ws_path}, TLS: {broker_config.get('use_tls', False)})"
                        )
                        # For WebSockets, connect without path parameter (path set via ws_set_options)
                        # Run connect in executor to avoid blocking the event loop
                        loop = asyncio.get_event_loop()
                        try:
                            await loop.run_in_executor(None, client.connect, host, port, keepalive)
                        except Exception as connect_error:
                            # Connection failed, but don't block - let loop_start handle retries
                            self.logger.debug(f"Initial connect() call failed (non-blocking): {connect_error}")
                    else:
                        self.logger.debug(
                            f"Connecting to MQTT broker {host}:{port} via TCP (TLS: {broker_config.get('use_tls', False)})"
                        )
                        # Run connect in executor to avoid blocking the event loop
                        loop = asyncio.get_event_loop()
                        try:
                            await loop.run_in_executor(None, client.connect, host, port, keepalive)
                        except Exception as connect_error:
                            # Connection failed, but don't block - let loop_start handle retries
                            self.logger.debug(f"Initial connect() call failed (non-blocking): {connect_error}")

                    # Start network loop (non-blocking)
                    client.loop_start()

                    self.logger.info(f"MQTT connection initiated to {host}:{port} ({transport})")

                    # Give connection a moment to establish (especially for WebSockets)
                    await asyncio.sleep(1)
                except Exception as e:
                    error_msg = str(e)
                    if "nodename nor servname provided" in error_msg or "Name or service not known" in error_msg:
                        # Log DNS errors at debug level, actual connection errors at warning
                        if self.debug:
                            self.logger.debug(f"DNS/Connection error for '{broker_config['host']}': {error_msg}")
                        else:
                            self.logger.warning(
                                f"Could not connect to MQTT broker '{broker_config['host']}' (check network/DNS)"
                            )
                    elif "Connection refused" in error_msg:
                        if self.debug:
                            self.logger.debug(
                                f"Connection refused by '{broker_config['host']}:{broker_config['port']}': {error_msg}"
                            )
                        else:
                            self.logger.warning(
                                f"Connection refused by MQTT broker '{broker_config['host']}:{broker_config['port']}'"
                            )
                    else:
                        if self.debug:
                            self.logger.debug(f"MQTT connection error for '{broker_config['host']}': {error_msg}")
                        else:
                            self.logger.warning(f"Error connecting to MQTT broker '{broker_config['host']}'")

            except Exception as e:
                self.logger.error(f"Error setting up MQTT broker: {e}")

        # Wait a bit for connections to establish
        await asyncio.sleep(2)

        # Log summary and publish initial status (matches original script)
        connected_count = sum(1 for m in self.mqtt_clients if m.get("connected", False))
        if connected_count > 0:
            self.logger.info(f"Connected to {connected_count} MQTT broker(s)")
            # Publish initial status with firmware version now that MQTT is connected (matches original script)
            await asyncio.sleep(1)  # Give MQTT connections a moment to stabilize
            await self.publish_status("online")
        else:
            self.logger.warning("MQTT enabled but no brokers connected")

    def _resolve_topic_template(self, template: str, packet_type: str = "packet") -> str | None:
        """Resolve topic template with placeholders.

        Args:
            template: Topic template string.
            packet_type: Type of packet ('packet' or 'status').

        Returns:
            str | None: Resolved topic string, or None if template is empty.
        """
        if not template:
            return None

        # Get device's public key (NOT owner's key - owner key is only for JWT 'owner' field)
        # This matches the original script which uses self.device_public_key from self_info
        device_public_key = None
        if self.meshcore and hasattr(self.meshcore, "self_info"):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    device_public_key = self_info.get("public_key", "")
                elif hasattr(self_info, "public_key"):
                    device_public_key = self_info.public_key

                # Convert to hex string if bytes
                if isinstance(device_public_key, bytes):
                    device_public_key = device_public_key.hex()
                elif isinstance(device_public_key, bytearray):
                    device_public_key = bytes(device_public_key).hex()
            except Exception as e:
                self.logger.debug(f"Could not get public key from device: {e}")

        # Normalize to uppercase (remove 0x prefix if present)
        if device_public_key:
            device_public_key = device_public_key.replace("0x", "").replace(" ", "").upper()

        # Replace placeholders (matches original script's resolve_topic_template)
        topic = template.replace("{IATA}", self.global_iata.upper())
        topic = topic.replace("{iata}", self.global_iata.lower())
        topic = topic.replace(
            "{PUBLIC_KEY}", device_public_key if device_public_key and device_public_key != "Unknown" else "DEVICE"
        )
        topic = topic.replace(
            "{public_key}",
            (device_public_key if device_public_key and device_public_key != "Unknown" else "DEVICE").lower(),
        )

        return topic

    async def publish_packet_mqtt(self, packet_info: dict[str, Any]) -> dict[str, Any]:
        """Publish packet to MQTT - returns metrics dict with attempted/succeeded/skipped_by_filter.

        Args:
            packet_info: Formatted packet dictionary.

        Returns:
            Dict with 'attempted', 'succeeded' counts and 'skipped_by_filter' (True when
            packet type was excluded by mqttN_upload_packet_types for all connected brokers).
        """
        # Always log when function is called (helps diagnose if it's not being invoked)
        self.logger.debug(f"publish_packet_mqtt called (packet {self.packet_count}, {len(self.mqtt_clients)} clients)")

        # Initialize metrics (skipped_by_filter: True when packet type excluded by upload_packet_types)
        metrics = {"attempted": 0, "succeeded": 0, "skipped_by_filter": False}

        # Check per-broker connection status (more accurate than global flag)
        # Don't use early return - let the loop check each broker individually
        if not self.mqtt_clients:
            self.logger.debug("No MQTT clients configured, skipping publish")
            return metrics

        connected_count = sum(1 for m in self.mqtt_clients if m.get("connected", False))
        self.logger.debug(f"Publishing packet to MQTT ({connected_count}/{len(self.mqtt_clients)} brokers connected)")

        for mqtt_client_info in self.mqtt_clients:
            # Only publish to connected brokers
            if not mqtt_client_info.get("connected", False):
                self.logger.debug(
                    f"Skipping MQTT broker {mqtt_client_info['config'].get('host', 'unknown')} (not connected)"
                )
                continue
            try:
                client = mqtt_client_info["client"]
                config = mqtt_client_info["config"]

                # Per-broker packet type filter: if set, only upload listed types (e.g. 2,4 = TXT_MSG, ADVERT)
                upload_types = config.get("upload_packet_types")
                if upload_types is not None and packet_info.get("packet_type", "") not in upload_types:
                    metrics["skipped_by_filter"] = True
                    self.logger.debug(
                        f"Skipping MQTT broker {config.get('host', 'unknown')} (packet type {packet_info.get('packet_type')} not in {sorted(upload_types)})"
                    )
                    continue

                # Determine topic
                topic = None
                if config.get("topic_packets"):
                    topic = self._resolve_topic_template(config["topic_packets"], "packet")
                elif config.get("topic_prefix"):
                    topic = f"{config['topic_prefix']}/packet"
                else:
                    topic = "meshcore/packets/packet"

                if not topic:
                    continue

                # Per-broker: strip the decoded object for brokers that opt out.
                broker_packet = packet_info
                if "decoded" in packet_info and not config.get("include_decoded", False):
                    broker_packet = {k: v for k, v in packet_info.items() if k != "decoded"}

                payload = json.dumps(broker_packet, default=str)

                # Log topic and payload size for debugging
                self.logger.debug(f"Publishing to topic '{topic}' on {config['host']} (payload: {len(payload)} bytes)")

                # Count as attempted
                metrics["attempted"] += 1

                # Use QoS 0 (matches original script - prevents retry storms)
                result = client.publish(topic, payload, qos=0)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    metrics["succeeded"] += 1
                    self.logger.debug(f"Published packet to MQTT topic '{topic}' on {config['host']} (qos=0)")
                else:
                    self.logger.warning(
                        f"Failed to publish packet to MQTT topic '{topic}' on {config['host']}: {result.rc} ({mqtt.error_string(result.rc)})"
                    )

            except Exception as e:
                self.logger.error(f"Error publishing packet to MQTT on {config.get('host', 'unknown')}: {e}")

        # Log summary
        if metrics["succeeded"] > 0:
            self.logger.debug(f"Published packet to {metrics['succeeded']} MQTT broker(s)")
        elif connected_count == 0:
            self.logger.debug("No MQTT brokers connected, packet not published")

        return metrics

    async def start_background_tasks(self) -> None:
        """Start background tasks.

        Initializes scheduler for stats refresh, per-broker JWT renewal, health checks,
        and MQTT reconnection monitor.
        """
        # Stats refresh scheduler (matches original script)
        if self.stats_status_enabled and self.stats_refresh_interval > 0:
            self.stats_update_task = asyncio.create_task(self.stats_refresh_scheduler())
            self.background_tasks.append(self.stats_update_task)

        # Per-broker JWT renewal (only brokers with use_auth_token and jwt_renewal_interval > 0)
        for mqtt_client_info in self.mqtt_clients:
            cfg = mqtt_client_info["config"]
            if cfg.get("use_auth_token") and cfg.get("jwt_renewal_interval", 0) > 0:
                task = asyncio.create_task(self.jwt_renewal_scheduler_for_client(mqtt_client_info))
                self.background_tasks.append(task)

        # Health check
        if self.health_check_interval > 0:
            task = asyncio.create_task(self.health_check_loop())
            self.background_tasks.append(task)

        # MQTT reconnection monitor (proactive reconnection for failed/disconnected brokers)
        if self.mqtt_enabled:
            task = asyncio.create_task(self.mqtt_reconnection_monitor())
            self.background_tasks.append(task)

        # Neighbors discovery. Unlike the upstream capture tool this does not
        # require a broker: the database is a legitimate consumer on its own, so
        # the cycle runs whenever the feature is enabled and publishes to
        # whichever brokers opted in (possibly none).
        if self.neighbors_enabled:
            self.neighbors_task = asyncio.create_task(self.neighbors_scheduler())
            self.background_tasks.append(self.neighbors_task)

    def neighbors_brokers(self) -> list[dict[str, Any]]:
        """Connected MQTT clients whose broker opted into the neighbors topic."""
        return [
            info for info in self.mqtt_clients
            if info.get("connected", False) and info["config"].get("neighbors", False)
        ]

    def neighbors_commands_available(self) -> bool:
        """Detect whether the connected build exposes the neighbors commands.

        ``send_node_discover_req`` needs companion CMD_SEND_CONTROL_DATA (v8+);
        ``req_regions_sync`` needs CMD_SEND_ANON_REQ and is only required when
        scope collection is enabled. Logged once per state change, like stats.
        """
        if not self.meshcore or not hasattr(self.meshcore, "commands"):
            return False

        commands = self.meshcore.commands
        required = ["send_node_discover_req"]
        if self.neighbors_config.collect_scopes:
            required.append("req_regions_sync")
        available = all(callable(getattr(commands, attr, None)) for attr in required)
        state = "available" if available else "missing"
        if state != self.neighbors_capability_state:
            if available:
                self.logger.info("MeshCore neighbors commands detected - neighbors discovery enabled")
            else:
                self.logger.warning(
                    "MeshCore neighbors commands not available - neighbors discovery disabled "
                    "(needs a newer firmware/meshcore build)"
                )
            self.neighbors_capability_state = state
        return available

    def _neighbors_topic_template(self, broker_config: dict[str, Any]) -> Optional[str]:
        """Unresolved neighbors topic template for one broker.

        Order matters because brokers publish neighbors by default, so the derived
        value has to be *correct*, not merely non-empty:

        1. Explicit ``topic_neighbors`` always wins.
        2. Otherwise swap the last segment of ``topic_packets``. A broker
           configured with ``meshcore/{IATA}/{PUBLIC_KEY}/packets`` then gets
           ``meshcore/{IATA}/{PUBLIC_KEY}/neighbors`` — the topic the firmware
           actually publishes — instead of an unrelated flat topic.
        3. Otherwise ``<topic_prefix>/neighbors``, mirroring how the packet path
           falls back to ``<prefix>/packet``.
        """
        explicit = broker_config.get("topic_neighbors")
        if explicit:
            return explicit

        packets = broker_config.get("topic_packets")
        if packets:
            if "/" in packets:
                return packets.rsplit("/", 1)[0] + "/neighbors"
            return "neighbors"

        prefix = broker_config.get("topic_prefix")
        if prefix:
            return f"{prefix}/neighbors"
        return None

    def _iata_is_unset(self) -> bool:
        """True when no real IATA is configured (blank or the XYZ sentinel)."""
        return (not self.global_iata) or self.global_iata == DEFAULT_IATA.lower()

    def _resolve_neighbors_topic(self, broker_config: dict[str, Any]) -> Optional[str]:
        """Resolved topic for one broker's neighbors snapshot, or None if unroutable."""
        template = self._neighbors_topic_template(broker_config)
        if not template:
            return None

        # An unset IATA is blank or the documented sentinel "XYZ", and this topic
        # is location-routed on the community brokers. Publishing a snapshot to
        # meshcore/XYZ/... (or meshcore//...) would pollute a shared namespace, so
        # refuse instead — matching the upstream rule that neighbors needs a real
        # IATA to route. Only applies to a template we derived; an explicit topic
        # is the operator's call.
        # .upper() catches both the {IATA} and {iata} placeholder spellings.
        if (
            not broker_config.get("topic_neighbors")
            and "{IATA}" in template.upper()
            and self._iata_is_unset()
        ):
            return None

        return self._resolve_topic_template(template, "neighbors")

    def publish_neighbors_mqtt(self, message: dict[str, Any]) -> dict[str, int]:
        """Publish a neighbors snapshot to every opted-in, connected broker.

        Non-retained: a snapshot taken every 12-336h is not a useful last-will
        value, and a stale retained copy would read as current.
        """
        metrics = {"attempted": 0, "succeeded": 0}
        if not self.mqtt_enabled or mqtt is None:
            return metrics

        payload = json.dumps(message, default=str)
        for mqtt_client_info in self.neighbors_brokers():
            config = mqtt_client_info["config"]
            broker_num = config.get("broker_num", 0)
            host = config.get("host", "unknown")
            topic = self._resolve_neighbors_topic(config)
            if not topic:
                # Warn once per broker: the cycle spent real airtime, so silently
                # dropping the result would be worse than noisy.
                if broker_num not in self.neighbors_topic_warned:
                    self.neighbors_topic_warned.add(broker_num)
                    template = self._neighbors_topic_template(config)
                    if template and "{IATA}" in template.upper():
                        reason = (
                            f"its topic is location-routed ({template}) but no IATA is set; "
                            f"set [PacketCapture] iata, or mqtt{broker_num}_topic_neighbors "
                            f"to a topic that does not need one"
                        )
                    else:
                        reason = (
                            f"no neighbors topic could be resolved (set "
                            f"mqtt{broker_num}_topic_neighbors or a topic prefix)"
                        )
                    self.logger.warning(
                        f"Not publishing neighbors to {host}: {reason}"
                    )
                continue

            try:
                metrics["attempted"] += 1
                result = mqtt_client_info["client"].publish(topic, payload, qos=0, retain=False)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    metrics["succeeded"] += 1
                    self.logger.debug(f"Published neighbors to '{topic}' on {host}")
                else:
                    self.logger.warning(
                        f"Failed to publish neighbors to '{topic}' on {host}: "
                        f"{result.rc} ({mqtt.error_string(result.rc)})"
                    )
            except Exception as e:
                self.logger.error(f"Error publishing neighbors to MQTT on {host}: {e}")

        return metrics

    def _feed_mesh_graph(self, self_pubkey: str, entries: list[Any]) -> None:
        """Add each confirmed direct link to the mesh graph, both directions.

        Both directions are correct: a discover response proves we transmitted,
        they received, they transmitted, and we received.

        Note that ``mesh_connections`` cannot represent *why* an edge exists —
        its multi-byte confirmation flag is memory-only and never persisted — so
        ``neighbor_links`` remains the source of truth for this evidence, and the
        viewer derives its neighbors evidence mode from that table instead.
        """
        mesh_graph = getattr(self.bot, "mesh_graph", None)
        if not mesh_graph or not self_pubkey:
            return

        self_prefix = self_pubkey.lower()[:6]
        added = 0
        for entry in entries:
            neighbor_prefix = entry.pubkey.lower()[:6]
            if not neighbor_prefix or neighbor_prefix == self_prefix:
                continue
            try:
                # hop_position 1: a direct link is the first hop of any path
                # through it. add_edge honours the graph capture kill-switch.
                mesh_graph.add_edge(
                    self_prefix, neighbor_prefix,
                    from_public_key=self_pubkey.lower(),
                    to_public_key=entry.pubkey.lower(),
                    hop_position=1, prefix_bytes=2,
                )
                mesh_graph.add_edge(
                    neighbor_prefix, self_prefix,
                    from_public_key=entry.pubkey.lower(),
                    to_public_key=self_pubkey.lower(),
                    hop_position=1, prefix_bytes=2,
                )
                added += 2
            except Exception as e:
                self.logger.debug(f"Neighbors: could not add graph edge for {neighbor_prefix}: {e}")

        if added and self.debug:
            self.logger.debug(f"Neighbors: fed {added} mesh graph edge(s)")

    def _device_public_key(self) -> str:
        """This node's public key, lowercase hex, or '' when unavailable."""
        if not self.meshcore or not hasattr(self.meshcore, "self_info"):
            return ""
        try:
            self_info = self.meshcore.self_info
            if isinstance(self_info, dict):
                key = self_info.get("public_key", "")
            else:
                key = getattr(self_info, "public_key", "")
            if isinstance(key, (bytes, bytearray)):
                key = bytes(key).hex()
            key = str(key or "").replace("0x", "").replace(" ", "").strip().lower()
            return "" if key in ("", "unknown") else key
        except Exception as e:
            self.logger.debug(f"Could not read device public key: {e}")
            return ""

    @staticmethod
    def _empty_neighbors_summary(reason: str = "") -> dict[str, Any]:
        """A cycle summary that reports no work done, optionally with a reason."""
        return {
            "ok": False, "reason": reason, "discovered": 0, "queried": 0,
            "best_snr": None, "attempted": 0, "succeeded": 0, "recorded": 0,
        }

    def neighbors_cooldown_remaining(self) -> float:
        """Seconds until another cycle may reach the radio.

        Measured from the last cycle that got as far as transmitting, whichever
        trigger started it — so a cycle that failed on a lost acknowledgement
        still counts, because the discover broadcast went out regardless. A cycle
        that bailed before touching the radio stamps nothing and so costs nothing.
        """
        last = max(self.last_neighbors_attempt, self.last_neighbors_publish)
        if last <= 0:
            return 0.0
        return max(0.0, MIN_CYCLE_GAP_SECONDS - (time.time() - last))

    def claim_neighbors_cycle(self) -> Optional[str]:
        """Claim the single-flight lock before a cycle reaches the radio.

        Returns ``None`` when claimed. Returns a refusal reason when another
        cycle is already running or the airtime cooldown has not expired.

        The ``neighbors`` command claims *before* acknowledging so an await
        cannot let the scheduler sneak in and turn "started" into an immediate
        failure. Pair every successful claim with ``release_neighbors_cycle``
        (``run_neighbors_cycle`` does this in ``finally``).
        """
        if self.neighbors_cycle_active:
            self.logger.info(
                "Neighbors: a discovery cycle is already running, skipping this trigger"
            )
            return "a discovery cycle is already running"

        cooldown = self.neighbors_cooldown_remaining()
        if cooldown > 0:
            self.logger.info(
                f"Neighbors: last cycle was too recent, {cooldown:.0f}s left before "
                f"another may run"
            )
            return f"another cycle may run in {cooldown:.0f}s"

        self.neighbors_cycle_active = True
        return None

    def release_neighbors_cycle(self) -> None:
        """Drop the single-flight lock claimed by ``claim_neighbors_cycle``."""
        self.neighbors_cycle_active = False

    async def run_neighbors_cycle(self, *, already_claimed: bool = False) -> dict[str, Any]:
        """Run one discovery cycle, subject to the airtime guards.

        Both guards live here rather than in a caller, because the scheduler, the
        ``neighbors`` command and any future trigger all reach the same radio:

        * no overlap — two concurrent cycles would each collect the other's
          discover responses and spend twice the airtime for no extra information;
        * no cycle within ``MIN_CYCLE_GAP_SECONDS`` of the last one that
          transmitted.

        Pass ``already_claimed=True`` when the caller has successfully called
        ``claim_neighbors_cycle`` (the manual command does this so it can
        acknowledge only after the lock is held).

        Returns a summary dict (also used by the ``neighbors`` command):
        ``{'ok', 'reason', 'discovered', 'queried', 'best_snr', 'attempted',
        'succeeded', 'recorded'}``.
        """
        if not already_claimed:
            reason = self.claim_neighbors_cycle()
            if reason is not None:
                return self._empty_neighbors_summary(reason)

        try:
            return await self._run_neighbors_cycle()
        finally:
            self.release_neighbors_cycle()

    async def _run_neighbors_cycle(self) -> dict[str, Any]:
        """One discovery cycle: record it, feed the graph, publish it.

        Always call through run_neighbors_cycle, which holds the single-flight
        guard for the whole cycle.
        """
        summary = self._empty_neighbors_summary()

        if not self.neighbors_enabled:
            summary["reason"] = "neighbors discovery is disabled"
            return summary
        if not self.meshcore or not self.bot.connected:
            summary["reason"] = "radio not connected"
            return summary
        if not self.neighbors_commands_available():
            summary["reason"] = "radio build does not support neighbor discovery"
            return summary

        cfg = self.neighbors_config
        self_pubkey = self._device_public_key()
        self.logger.info("Neighbors: starting discovery cycle")

        # A reconnect swaps in a fresh MeshCore object and clears every event
        # subscription, so a cycle spanning one is collecting into a dead handler.
        session = self.meshcore

        def session_intact() -> bool:
            return self.meshcore is session and self.bot.connected

        # Stamp the attempt before the request, not after the cycle: from here on
        # the discover broadcast may go out and spend airtime even if we never
        # learn that it did (a lost acknowledgement fails the cycle without
        # stamping last_neighbors_publish). Rate limiting has to charge for the
        # transmission, not for the result. Persist this to ration requests
        # across restarts as well. It is separate from
        # last_neighbors_publish so a failed cycle retries after the short airtime
        # gap rather than waiting the full configured schedule interval.
        self.last_neighbors_attempt = time.time()
        self._save_neighbors_attempt_state()

        entries = await discover_neighbors(
            self.meshcore, cfg, self_pubkey, self.logger,
            debug=self.debug, still_valid=session_intact,
        )
        if entries is None:
            # A build that rejects the discover command fails every cycle; warn
            # once rather than on every wakeup forever.
            self.neighbors_discover_failures += 1
            if self.neighbors_discover_failures == 1:
                self.logger.warning(
                    "Neighbors: discovery request failed; will keep retrying quietly "
                    "on the configured interval"
                )
            else:
                self.logger.debug(
                    f"Neighbors: discovery request failed "
                    f"({self.neighbors_discover_failures} consecutive)"
                )
            summary["reason"] = "discovery request failed"
            return summary
        self.neighbors_discover_failures = 0

        total_discovered = len(entries)
        summary["discovered"] = total_discovered
        if total_discovered > cfg.max_neighbors:
            self.logger.info(
                f"Neighbors: {total_discovered} discovered, keeping the "
                f"{cfg.max_neighbors} most useful"
            )
            entries = entries[:cfg.max_neighbors]

        self.logger.info(f"Neighbors: {len(entries)} neighbor(s) discovered")
        await collect_scopes(self.meshcore, entries, cfg, self.logger, debug=self.debug)

        if not session_intact():
            self.logger.warning(
                "Neighbors: device session was reset mid-cycle, discarding this cycle "
                "rather than recording partial data"
            )
            summary["reason"] = "device session reset mid-cycle"
            return summary

        summary["queried"] = len(entries)
        if entries:
            summary["best_snr"] = max(e.snr for e in entries)

        # Record before publishing: the data is valuable on its own, and a broker
        # problem must not cost us the observation.
        if entries and self_pubkey:
            summary["recorded"] = record_neighbors(
                self.bot.db_manager, self_pubkey, entries, self.logger
            )
        elif entries and not self_pubkey:
            self.logger.warning(
                "Neighbors: device public key unavailable, cannot record links "
                "(an unattributed link is not usable evidence)"
            )

        if entries and self.neighbors_feed_mesh_graph and self_pubkey:
            self._feed_mesh_graph(self_pubkey, entries)

        self_scopes = await fetch_self_scopes(self.meshcore, cfg, self.logger)
        origin_id = self_pubkey.upper() if self_pubkey else "DEVICE"
        message, dropped = build_neighbors_message(
            self._get_bot_name() or "MeshCore Device",
            origin_id, self_scopes, entries,
            total_neighbors=total_discovered,
        )
        if dropped:
            self.logger.warning(
                f"Neighbors: payload budget reached, dropped {dropped} least-useful entry(ies)"
            )

        metrics = self.publish_neighbors_mqtt(message)
        summary["attempted"] = metrics["attempted"]
        summary["succeeded"] = metrics["succeeded"]

        responded = sum(1 for e in entries if e.status == STATUS_RESPONDED)
        if metrics["attempted"]:
            self.logger.info(
                f"Neighbors: published {len(message['neighbors'])} entry(ies) "
                f"({responded} responded) to {metrics['succeeded']}/{metrics['attempted']} broker(s)"
            )
        else:
            self.logger.info(
                f"Neighbors: recorded {summary['recorded']} link(s); "
                f"no broker has neighbors enabled, nothing published"
            )

        # Stamp the attempt even when nothing was published, so a persistently
        # failing broker cannot turn every wakeup into a fresh discovery burst.
        self.last_neighbors_publish = time.time()
        self._save_neighbors_state()
        summary["ok"] = True
        return summary

    async def neighbors_scheduler(self) -> None:
        """Run a discovery cycle on the configured interval."""
        interval_seconds = self.neighbors_config.interval_seconds
        if self.debug:
            self.logger.debug(
                f"Starting neighbors scheduler "
                f"({self.neighbors_config.interval_hours}h interval)"
            )

        while not self.should_exit:
            try:
                time_since_last = time.time() - self.last_neighbors_publish
                if time_since_last < interval_seconds:
                    sleep_time = interval_seconds - time_since_last
                    if self.debug:
                        self.logger.debug(f"Next neighbors cycle in {sleep_time / 3600:.1f} hours")
                    if await self._wait_with_shutdown(sleep_time):
                        break
                    continue

                await self.run_neighbors_cycle()

                # run_neighbors_cycle stamps last_neighbors_publish when it got as
                # far as a result. If it bailed early (not connected, unsupported
                # build) back off before retrying so we re-check periodically
                # rather than spinning — but never retry inside the airtime
                # cooldown: a cycle that failed on a lost acknowledgement already
                # put a discover broadcast on the air, and retrying every
                # NEIGHBORS_RETRY_BACKOFF_SECONDS would spend triple the intended
                # airtime.
                if (time.time() - self.last_neighbors_publish) >= interval_seconds:
                    backoff = max(NEIGHBORS_RETRY_BACKOFF_SECONDS,
                                  self.neighbors_cooldown_remaining())
                    if await self._wait_with_shutdown(backoff):
                        break

            except asyncio.CancelledError:
                if self.debug:
                    self.logger.debug("Neighbors scheduler cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error in neighbors scheduler: {e}")
                if await self._wait_with_shutdown(NEIGHBORS_RETRY_BACKOFF_SECONDS):
                    break

    async def stats_refresh_scheduler(self) -> None:
        """Periodically refresh stats and publish them via MQTT (matches original script).

        Fetches updated radio stats and triggers status publication.
        """
        if self.stats_refresh_interval <= 0 or not self.stats_status_enabled:
            return

        while not self.should_exit:
            try:
                # Only fetch stats when we're about to publish status
                if self.mqtt_enabled:
                    connected_count = sum(1 for m in self.mqtt_clients if m.get("connected", False))
                    if connected_count > 0:
                        await self.publish_status("online", refresh_stats=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.debug(f"Stats refresh error: {exc}")

            if await self._wait_with_shutdown(self.stats_refresh_interval):
                break

    async def _wait_with_shutdown(self, timeout: float) -> bool:
        """Wait for specified time but return immediately if shutdown is requested.

        Args:
            timeout: Time to wait in seconds.

        Returns:
            bool: True if shutdown requested, False if timeout completed.
        """
        if self.should_exit:
            return True
        await asyncio.sleep(timeout)
        return False

    def _load_client_version(self) -> str:
        """Load client version from shared runtime resolver."""
        try:
            info = resolve_application_version()
            display = info.get("display") or "unknown"
            return f"meshcore-bot/{display}"
        except Exception as e:
            self.logger.debug(f"Could not load version info: {e}")
            return "meshcore-bot/unknown"

    async def get_firmware_info(self) -> dict[str, str]:
        """Get firmware information from meshcore device (matches original script).

        Returns:
            dict[str, str]: Dictionary containing 'model' and 'version'.
        """
        try:
            # During shutdown, always use cached info - don't query the device
            if self.should_exit:
                if self.cached_firmware_info:
                    self.logger.debug("Using cached firmware info (shutdown in progress)")
                    return self.cached_firmware_info
                else:
                    return {"model": "unknown", "version": "unknown"}

            # Always use cached info if available - firmware info doesn't change during runtime
            if self.cached_firmware_info:
                if self.debug:
                    self.logger.debug("Using cached firmware info")
                return self.cached_firmware_info

            # Only query if we don't have cached info
            if not self.meshcore or not self.meshcore.is_connected:
                return {"model": "unknown", "version": "unknown"}

            self.logger.debug("Querying device for firmware info...")
            # Use send_device_query() to get firmware version
            result = await self.meshcore.commands.send_device_query()

            if result is None:
                self.logger.debug("Device query failed")
                return {"model": "unknown", "version": "unknown"}

            if result.type == EventType.ERROR:
                self.logger.debug(f"Device query failed: {result}")
                return {"model": "unknown", "version": "unknown"}

            if result.payload:
                payload = result.payload
                self.logger.debug(f"Device query payload: {payload}")

                # Check firmware version format
                fw_ver = payload.get("fw ver", 0)
                self.logger.debug(f"Firmware version number: {fw_ver}")

                if fw_ver >= 3:
                    # For newer firmware versions (v3+)
                    model = payload.get("model", "Unknown")
                    version = payload.get("ver", "Unknown")
                    build_date = payload.get("fw_build", "Unknown")
                    # Remove 'v' prefix from version if it already has one
                    if version.startswith("v"):
                        version = version[1:]
                    version_str = f"v{version} (Build: {build_date})"
                    self.logger.debug(f"New firmware format - Model: {model}, Version: {version_str}")
                    firmware_info = {"model": model, "version": version_str}
                    self.cached_firmware_info = firmware_info  # Cache the result
                    return firmware_info
                else:
                    # For older firmware versions
                    version_str = f"v{fw_ver}"
                    self.logger.debug(f"Old firmware format - Model: unknown, Version: {version_str}")
                    firmware_info = {"model": "unknown", "version": version_str}
                    self.cached_firmware_info = firmware_info  # Cache the result
                    return firmware_info

            self.logger.debug("No payload in device query result")
            return {"model": "unknown", "version": "unknown"}

        except Exception as e:
            self.logger.debug(f"Error getting firmware info: {e}")
            return {"model": "unknown", "version": "unknown"}

    def stats_commands_available(self) -> bool:
        """Detect whether the connected meshcore build exposes stats commands (matches original script).

        Returns:
            bool: True if stats commands are available.
        """
        if not self.meshcore or not hasattr(self.meshcore, "commands"):
            return False

        commands = self.meshcore.commands
        required = ["get_stats_core", "get_stats_radio"]
        available = all(callable(getattr(commands, attr, None)) for attr in required)
        state = "available" if available else "missing"
        if state != self.stats_capability_state:
            if available:
                self.logger.info("MeshCore stats commands detected - status messages will include device stats")
            else:
                self.logger.info("MeshCore stats commands not available - skipping stats in status messages")
            self.stats_capability_state = state
        self.stats_supported = available
        return available

    async def refresh_stats(self, force: bool = False) -> dict[str, Any] | None:
        """Fetch stats from the radio and cache them for status publishing (matches original script).

        Args:
            force: Force refresh even if cache is fresh.

        Returns:
            dict[str, Any] | None: Dictionary of stats or None if unavailable.
        """
        if not self.stats_status_enabled:
            if self.debug:
                self.logger.debug("Stats refresh skipped: stats_status_enabled is False")
            return None

        if not self.meshcore or not self.meshcore.is_connected:
            return None

        if self.stats_refresh_interval <= 0:
            if self.debug:
                self.logger.debug("Stats refresh skipped: stats_refresh_interval is 0 or negative")
            return None

        if not self.stats_commands_available():
            if self.debug:
                self.logger.debug("Stats refresh skipped: stats commands not available")
            return None

        now = time.time()
        if (
            not force
            and self.latest_stats
            and (now - self.last_stats_fetch) < max(60, self.stats_refresh_interval // 2)
        ):
            return dict(self.latest_stats)

        async with self.stats_fetch_lock:
            # Another coroutine may have completed the refresh while we waited
            if (
                not force
                and self.latest_stats
                and (time.time() - self.last_stats_fetch) < max(60, self.stats_refresh_interval // 2)
            ):
                return dict(self.latest_stats)

            stats_payload = {}
            try:
                core_result = await self.meshcore.commands.get_stats_core()
                if core_result and core_result.type == EventType.STATS_CORE and core_result.payload:
                    stats_payload.update(core_result.payload)
                elif core_result and core_result.type == EventType.ERROR:
                    self.logger.debug(f"Core stats unavailable: {core_result.payload}")
            except Exception as exc:
                self.logger.debug(f"Error fetching core stats: {exc}")

            try:
                radio_result = await self.meshcore.commands.get_stats_radio()
                if radio_result and radio_result.type == EventType.STATS_RADIO and radio_result.payload:
                    stats_payload.update(radio_result.payload)
                elif radio_result and radio_result.type == EventType.ERROR:
                    self.logger.debug(f"Radio stats unavailable: {radio_result.payload}")
            except Exception as exc:
                self.logger.debug(f"Error fetching radio stats: {exc}")

            if stats_payload:
                self.latest_stats = stats_payload
                self.last_stats_fetch = time.time()
                if self.debug:
                    self.logger.debug(f"Updated stats cache: {self.latest_stats}")
            elif self.debug:
                self.logger.debug("Stats refresh completed but returned no data")

        return dict(self.latest_stats) if self.latest_stats else None

    async def publish_status(self, status: str, refresh_stats: bool = True) -> None:
        """Publish status with additional information (matches original script exactly).

        Args:
            status: Status string (e.g., 'online', 'offline').
            refresh_stats: Whether to refresh stats before publishing.
        """
        firmware_info = await self.get_firmware_info()

        # Get device name and public key
        device_name = self._get_observer_name()
        if not device_name:
            device_name = "MeshCore Device"

        # Get device public key for origin_id
        device_public_key = None
        if self.meshcore and hasattr(self.meshcore, "self_info"):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    device_public_key = self_info.get("public_key", "")
                elif hasattr(self_info, "public_key"):
                    device_public_key = self_info.public_key

                # Convert to hex string if bytes
                if isinstance(device_public_key, bytes):
                    device_public_key = device_public_key.hex()
                elif isinstance(device_public_key, bytearray):
                    device_public_key = bytes(device_public_key).hex()
            except Exception:
                pass

        # Normalize origin_id to uppercase
        if device_public_key:
            device_public_key = device_public_key.replace("0x", "").replace(" ", "").upper()
        else:
            device_public_key = "DEVICE"

        # Get radio info if available
        if not self.radio_info and self.meshcore and hasattr(self.meshcore, "self_info"):
            try:
                self_info = self.meshcore.self_info
                radio_freq = (
                    self_info.get("radio_freq", 0)
                    if isinstance(self_info, dict)
                    else getattr(self_info, "radio_freq", 0)
                )
                radio_bw = (
                    self_info.get("radio_bw", 0) if isinstance(self_info, dict) else getattr(self_info, "radio_bw", 0)
                )
                radio_sf = (
                    self_info.get("radio_sf", 0) if isinstance(self_info, dict) else getattr(self_info, "radio_sf", 0)
                )
                radio_cr = (
                    self_info.get("radio_cr", 0) if isinstance(self_info, dict) else getattr(self_info, "radio_cr", 0)
                )
                self.radio_info = f"{radio_freq},{radio_bw},{radio_sf},{radio_cr}"
            except Exception:
                pass

        status_msg = {
            "status": status,
            "timestamp": self._utc_iso_timestamp(),
            "origin": device_name,
            "origin_id": device_public_key,
            "model": firmware_info.get("model", "unknown"),
            "firmware_version": firmware_info.get("version", "unknown"),
            "radio": self.radio_info or "unknown",
            "client_version": self._load_client_version(),
        }

        # Attach stats (online status only) if supported and enabled
        if status.lower() == "online" and self.stats_status_enabled:
            stats_payload = None
            if refresh_stats:
                # Always force refresh stats right before publishing to ensure fresh data
                stats_payload = await self.refresh_stats(force=True)
                if not stats_payload:
                    self.logger.debug("Stats refresh returned no data - stats will not be included in status message")
            elif self.latest_stats:
                stats_payload = dict(self.latest_stats)

            if stats_payload:
                status_msg["stats"] = stats_payload
            elif self.debug:
                self.logger.debug("No stats payload available - status message will not include stats")

        # Publish status to all connected brokers
        for mqtt_client_info in self.mqtt_clients:
            # Only publish to connected brokers
            if not mqtt_client_info.get("connected", False):
                continue
            try:
                client = mqtt_client_info["client"]
                config = mqtt_client_info["config"]

                # Determine topic
                topic = None
                if config.get("topic_status"):
                    topic = self._resolve_topic_template(config["topic_status"], "status")
                elif config.get("topic_prefix"):
                    topic = f"{config['topic_prefix']}/status"
                else:
                    topic = "meshcore/status"

                if not topic:
                    continue

                payload = json.dumps(status_msg, default=str)

                # Use QoS 0 with retain=True for status (matches original script)
                result = client.publish(topic, payload, qos=0, retain=True)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    if self.debug:
                        self.logger.debug(f"Published status: {status}")
                else:
                    self.logger.warning(f"Failed to publish status to MQTT topic '{topic}': {result.rc}")

            except Exception as e:
                self.logger.error(f"Error publishing status to MQTT: {e}")

    async def _renew_mqtt_auth_token(self, mqtt_client_info: dict[str, Any]) -> bool:
        """Mint a new auth token and apply it to one MQTT client (per-broker TTL).

        Only updates the credentials used by the next CONNECT; it does not touch
        the socket, so it is safe to call while paho's network thread is running.

        Returns:
            bool: True if a new token was minted and applied.
        """
        config = mqtt_client_info["config"]
        client = mqtt_client_info["client"]
        broker_host = config.get("host", "unknown")

        if not config.get("use_auth_token"):
            return False

        self.logger.debug(f"Renewing auth token for MQTT broker {broker_host}...")

        device_public_key_hex = None
        if self.meshcore and hasattr(self.meshcore, "self_info"):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    device_public_key_hex = self_info.get("public_key", "")
                elif hasattr(self_info, "public_key"):
                    device_public_key_hex = self_info.public_key

                if isinstance(device_public_key_hex, bytes):
                    device_public_key_hex = device_public_key_hex.hex()
                elif isinstance(device_public_key_hex, bytearray):
                    device_public_key_hex = bytes(device_public_key_hex).hex()
            except Exception as e:
                self.logger.debug(f"Could not get public key from device: {e}")

        if not device_public_key_hex:
            self.logger.warning(f"No device public key available for token renewal (broker: {broker_host})")
            return False

        token_audience = config.get("token_audience") or broker_host
        username = f"v1_{device_public_key_hex.upper()}"

        use_device = self.auth_token_method == "device" and self.meshcore and self.meshcore.is_connected
        meshcore_for_key_fetch = self.meshcore if self.meshcore and self.meshcore.is_connected else None

        try:
            iat, exp = self._auth_token_iat_exp(config)
            ttl_used = exp - iat
            token = await create_auth_token_async(
                meshcore_instance=meshcore_for_key_fetch,
                public_key_hex=device_public_key_hex,
                private_key_hex=self.private_key_hex,
                iata=self.global_iata,
                timestamp=iat,
                audience=token_audience,
                exp=exp,
                owner_public_key=self.owner_public_key,
                owner_email=self.owner_email,
                use_device=use_device,
            )

            if token:
                client.username_pw_set(username, token)
                mqtt_client_info["token_exp"] = exp
                ttl_phrase = self._jwt_ttl_log_phrase(ttl_used)
                self.logger.info(f"✓ Renewed auth token for MQTT broker {broker_host} (TTL {ttl_phrase})")
                return True
            self.logger.warning(f"Failed to renew auth token for MQTT broker {broker_host}")
        except Exception as e:
            self.logger.error(f"Error renewing token for MQTT broker {broker_host}: {e}")
        return False

    async def jwt_renewal_scheduler_for_client(self, mqtt_client_info: dict[str, Any]) -> None:
        """Background task: renew JWT on one broker every config jwt_renewal_interval seconds."""
        config = mqtt_client_info["config"]
        interval = int(config.get("jwt_renewal_interval", 0))
        if interval <= 0:
            return

        while not self.should_exit:
            try:
                if await self._wait_with_shutdown(float(interval)):
                    break
                if self.should_exit:
                    break
                if not config.get("use_auth_token"):
                    continue
                renewed = await self._renew_mqtt_auth_token(mqtt_client_info)
                # username_pw_set only affects the next CONNECT, so without this the
                # session keeps running on the token it was opened with. Brokers that
                # enforce the JWT's exp then evict us mid-stream (issue #248); cycling
                # here turns that into one clean, scheduled reconnect instead.
                if (
                    renewed
                    and config.get("jwt_reconnect_on_renew", True)
                    and mqtt_client_info["client"].is_connected()
                ):
                    await self._cycle_mqtt_client(mqtt_client_info, "auth token renewed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in JWT renewal scheduler ({config.get('host', 'unknown')}): {e}")
                if await self._wait_with_shutdown(60):
                    break

    async def health_check_loop(self) -> None:
        """Background task for health checks.

        Monitors connection status and warns on failures.
        """
        if self.health_check_interval <= 0:
            return

        while not self.should_exit:
            try:
                await asyncio.sleep(self.health_check_interval)

                if not self.meshcore or not self.meshcore.is_connected:
                    self.health_check_failure_count += 1
                    if self.health_check_failure_count >= self.health_check_grace_period:
                        self.logger.warning("Health check failed - connection lost")
                else:
                    self.health_check_failure_count = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(60)

    async def _cycle_mqtt_client(self, mqtt_client_info: dict[str, Any], reason: str) -> bool:
        """Take one MQTT client down and bring it back up, serialized.

        The steps must not overlap. disconnect() lets paho's network thread wind
        down cleanly, loop_stop() joins it, and only then is it safe for this
        thread to drive reconnect(). Calling reconnect() while that thread is
        still running is what produces two sockets on one client.

        Returns:
            bool: True if the client is connected when this returns.
        """
        client = mqtt_client_info["client"]
        broker_host = mqtt_client_info["config"].get("host", "unknown")
        loop = asyncio.get_event_loop()

        async with mqtt_client_info["cycle_lock"]:
            self.logger.info(f"Cycling MQTT connection to {broker_host} ({reason})")
            try:
                await loop.run_in_executor(None, client.disconnect)
            except Exception as e:
                self.logger.debug(f"disconnect() during cycle of {broker_host}: {e}")
            try:
                await loop.run_in_executor(None, client.loop_stop)
            except Exception as e:
                self.logger.debug(f"loop_stop() during cycle of {broker_host}: {e}")

            try:
                await loop.run_in_executor(None, client.reconnect)
            except Exception as e:
                # Not fatal: loop_start() below hands the retries back to paho.
                self.logger.debug(f"reconnect() during cycle of {broker_host} failed: {e}")

            try:
                client.loop_start()
            except Exception as e:
                self.logger.warning(f"Could not restart network loop for {broker_host}: {e}")
                return False

            # CONNACK arrives on the network thread; give it a moment to land.
            await asyncio.sleep(2)
            connected = client.is_connected()
            mqtt_client_info["connected"] = connected
            self.mqtt_connected = any(m.get("connected", False) for m in self.mqtt_clients)
            if connected:
                self.logger.info(f"✓ Reconnected to MQTT broker {broker_host}")
            else:
                self.logger.debug(f"Cycle of {broker_host} did not connect yet; paho will keep retrying")
            return connected

    @staticmethod
    def _loop_thread_alive(client) -> bool:
        """Whether paho's network thread is still running for this client.

        While it lives, that thread owns reconnection and nothing else may drive
        the socket. Reading the private attribute is deliberate: paho 1.x exposes
        no public equivalent, and guessing wrong here is what caused the storm.
        """
        thread = getattr(client, "_thread", None)
        if thread is None:
            return False
        try:
            return bool(thread.is_alive())
        except Exception:
            return True

    # How long a broker may stay down before the log says so at warning level.
    MQTT_STALL_WARN_AFTER = 300
    # Refresh the token this far ahead of its expiry, so paho's next retry carries
    # a credential the broker will still accept.
    MQTT_TOKEN_REFRESH_MARGIN = 120

    async def _check_mqtt_client(self, mqtt_client_info: dict[str, Any], now: float) -> None:
        """One watchdog pass over a single client.

        Observes, keeps the auth token fresh for paho's next attempt, and
        intervenes only when paho's network thread is gone. It must never call
        connect()/reconnect() while that thread is alive.
        """
        client = mqtt_client_info["client"]
        config = mqtt_client_info["config"]
        broker_host = config.get("host", "unknown")

        if client.is_connected():
            mqtt_client_info["connected"] = True
            mqtt_client_info["down_since"] = None
            mqtt_client_info["stall_warned"] = False
            return

        mqtt_client_info["connected"] = False
        down_since = mqtt_client_info.get("down_since")
        if down_since is None:
            down_since = now
            mqtt_client_info["down_since"] = down_since
        downtime = now - down_since

        # Keep the credential valid so paho's retries can succeed. This only sets
        # the fields used by the next CONNECT; it does not touch the socket, so it
        # is safe alongside the network thread.
        if config.get("use_auth_token"):
            token_exp = mqtt_client_info.get("token_exp")
            if token_exp is None or token_exp - now < self.MQTT_TOKEN_REFRESH_MARGIN:
                await self._renew_mqtt_auth_token(mqtt_client_info)

        if self._loop_thread_alive(client):
            # paho is retrying with backoff. Say so once when the outage stops
            # looking transient, then stay quiet.
            if downtime >= self.MQTT_STALL_WARN_AFTER and not mqtt_client_info.get("stall_warned"):
                self.logger.warning(
                    f"MQTT broker {broker_host} has been disconnected for "
                    f"{int(downtime // 60)}m; paho is still retrying"
                )
                mqtt_client_info["stall_warned"] = True
            elif self.debug:
                self.logger.debug(
                    f"MQTT broker {broker_host} disconnected for {int(downtime)}s (paho retrying)"
                )
            return

        # No network thread: nothing is retrying, so this one is ours to fix.
        self.logger.info(f"MQTT network loop for {broker_host} is not running, restarting it")
        await self._cycle_mqtt_client(mqtt_client_info, "network loop stopped")

    async def mqtt_reconnection_monitor(self) -> None:
        """Watch the MQTT clients and recover only what paho cannot recover itself.

        paho owns reconnection here: reconnect_delay_set() plus loop_start() give
        each client a network thread that retries with backoff. This loop must not
        call connect()/reconnect() alongside that thread — two threads driving one
        client's socket produce duplicate CONNACKs, spurious protocol errors, and a
        fixed-interval retry that flattens paho's backoff into a storm (issue #248).
        """
        if not self.mqtt_enabled:
            return

        check_interval = 30

        while not self.should_exit:
            try:
                await asyncio.sleep(check_interval)

                if not self.mqtt_clients:
                    continue

                now = time.time()
                for mqtt_client_info in self.mqtt_clients:
                    try:
                        await self._check_mqtt_client(mqtt_client_info, now)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        host = mqtt_client_info["config"].get("host", "unknown")
                        self.logger.debug(f"Error checking MQTT broker {host}: {e}")

                self.mqtt_connected = any(m.get("connected", False) for m in self.mqtt_clients)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in MQTT reconnection monitor: {e}")
                await asyncio.sleep(60)
