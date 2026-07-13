#!/usr/bin/env python3
"""
Path Decode Command for the MeshCore Bot
Decodes hex path data to show which repeaters were involved in message routing
"""

import asyncio
import re
import time
from typing import Any, Callable, Optional

from ..models import MeshMessage
from ..path_inference import (
    PathInferenceConfig,
    select_node_repeater,
    select_repeater_by_graph,
)
from ..utils import (
    bytes_per_hop_from_routing_and_nodes,
    parse_path_string,
    public_key_has_prefix,
)
from .base_command import BaseCommand


class PathCommand(BaseCommand):
    """Command for decoding path data to repeater names"""

    # Plugin metadata
    name = "path"
    keywords = ["path", "decode", "route"]
    description = "Decode hex path data to show which repeaters were involved in message routing"
    requires_dm = False
    cooldown_seconds = 1
    category = "meshcore_info"

    # Documentation
    short_description = "Decode path data to show repeaters involved in message routing"
    usage = "path [hex_data]"
    examples = ["path", "decode"]

    # Web-viewer settings schema (see modules/settings_schema.py).
    # Most deployments only need the preset; advanced graph/geographic knobs
    # override preset values when set. Blank numeric fields defer to the preset.
    settings_schema = [
        {"key": "path_selection_preset", "label": "Selection preset", "type": "enum",
         "options": [
             {"value": "balanced", "label": "Balanced (default)"},
             {"value": "geographic", "label": "Geographic (local networks)"},
             {"value": "graph", "label": "Graph (well-connected networks)"},
         ],
         "default": "balanced",
         "help": "Bundles related settings; individual knobs below can override it."},
        {"key": "proximity_method", "label": "Proximity method", "type": "enum",
         "options": [
             {"value": "path", "label": "Path (prev/next nodes — default)"},
             {"value": "simple", "label": "Simple (bot location)"},
         ],
         "default": "path",
         "help": "How geographic proximity is calculated."},
        {"key": "path_proximity_fallback", "label": "Proximity fallback", "type": "bool",
         "default": True,
         "help": "Fall back to simple proximity when path proximity can't be computed."},
        {"key": "max_proximity_range", "label": "Max proximity range", "type": "float",
         "min": 0, "default": 200.0, "unit": "km",
         "help": "Repeaters beyond this distance lose confidence or are rejected. 0 disables."},
        {"key": "max_repeater_age_days", "label": "Max repeater age", "type": "int",
         "min": 0, "default": 14, "unit": "days",
         "help": "Ignore repeaters not heard within this many days."},
        {"key": "recency_weight", "label": "Recency weight", "type": "float",
         "min": 0, "max": 1, "default": 0.4,
         "help": "Blend of recency vs proximity (0 = all proximity, 1 = all recency)."},
        {"key": "recency_decay_half_life_hours", "label": "Recency half-life", "type": "float",
         "min": 0, "default": 12.0, "unit": "h",
         "help": "Half-life for the recency score decay."},
        {"key": "star_bias_multiplier", "label": "Starred bias", "type": "float",
         "min": 0, "default": 2.5,
         "help": "Confidence multiplier applied to starred repeaters."},
        {"key": "minimum_path_bytes", "label": "Minimum path bytes", "type": "int",
         "min": 0, "default": 0,
         "help": "Ignore path observations shorter than this many bytes. 0 = allow all."},
        {"key": "enable_p_shortcut", "label": "Enable 'p' shortcut", "type": "bool",
         "default": True,
         "help": "Allow 'p' as a shortcut for the path command."},
        {"key": "reply_prefix", "label": "Reply prefix", "type": "str",
         "default": "",
         "help": "Optional text prepended to path replies."},
        # Confidence symbols
        {"key": "high_confidence_symbol", "label": "High-confidence symbol", "type": "str",
         "default": "🎯", "help": "Marker for high-confidence path results."},
        {"key": "medium_confidence_symbol", "label": "Medium-confidence symbol", "type": "str",
         "default": "📍", "help": "Marker for medium-confidence path results."},
        {"key": "low_confidence_symbol", "label": "Low-confidence symbol", "type": "str",
         "default": "❓", "help": "Marker for low-confidence path results."},
        # Graph-based validation
        {"key": "graph_based_validation", "label": "Graph validation", "type": "bool",
         "default": True, "help": "Use the mesh graph to validate candidate paths."},
        {"key": "min_edge_observations", "label": "Min edge observations", "type": "int",
         "min": 1, "default": 3, "help": "Minimum observations before a graph edge is trusted."},
        {"key": "graph_use_bidirectional", "label": "Bidirectional edges", "type": "bool",
         "default": True, "help": "Treat graph edges as bidirectional."},
        {"key": "graph_use_hop_position", "label": "Use hop position", "type": "bool",
         "default": True, "help": "Factor average hop position into graph scoring."},
        {"key": "graph_multi_hop_enabled", "label": "Multi-hop graph", "type": "bool",
         "default": True, "help": "Allow multi-hop graph traversal during validation."},
        {"key": "graph_multi_hop_max_hops", "label": "Multi-hop max hops", "type": "int",
         "min": 1, "default": 2, "help": "Maximum hops considered for multi-hop graph traversal."},
        {"key": "graph_geographic_combined", "label": "Combine graph + geo", "type": "bool",
         "default": False, "help": "Combine graph evidence with geographic proximity."},
        {"key": "graph_geographic_weight", "label": "Geographic weight", "type": "float",
         "min": 0, "max": 1, "default": 0.7, "help": "Weight of geography when combined with graph evidence."},
        {"key": "graph_confidence_override_threshold", "label": "Confidence override threshold", "type": "float",
         "min": 0, "max": 1, "default": "", "help": "Graph confidence above which geography is overridden. Blank = preset."},
        {"key": "graph_prefer_stored_keys", "label": "Prefer stored keys", "type": "bool",
         "default": True, "help": "Prefer stored public keys when identifying nodes."},
        {"key": "graph_zero_hop_bonus", "label": "Zero-hop bonus", "type": "float",
         "min": 0, "default": 0.4, "help": "Confidence bonus for direct (zero-hop) links."},
        # Distance penalty
        {"key": "graph_distance_penalty_enabled", "label": "Distance penalty", "type": "bool",
         "default": True, "help": "Penalise graph links that span implausibly long distances."},
        {"key": "graph_max_reasonable_hop_distance_km", "label": "Max reasonable hop distance", "type": "float",
         "min": 0, "default": "", "unit": "km", "help": "Distance beyond which the penalty applies. Blank = preset."},
        {"key": "graph_distance_penalty_strength", "label": "Distance penalty strength", "type": "float",
         "min": 0, "default": "", "help": "How strongly long hops are penalised. Blank = preset."},
        # Final-hop proximity
        {"key": "graph_final_hop_proximity_enabled", "label": "Final-hop proximity", "type": "bool",
         "default": True, "help": "Weight the final hop by geographic proximity."},
        {"key": "graph_final_hop_proximity_weight", "label": "Final-hop weight", "type": "float",
         "min": 0, "max": 1, "default": "", "help": "Weight of final-hop proximity. Blank = preset."},
        {"key": "graph_final_hop_max_distance", "label": "Final-hop max distance", "type": "float",
         "min": 0, "default": 0.0, "unit": "km", "help": "Max final-hop distance considered. 0 = unlimited."},
        {"key": "graph_final_hop_proximity_normalization_km", "label": "Final-hop normalization", "type": "float",
         "min": 0, "default": 200.0, "unit": "km", "help": "Distance used to normalise final-hop proximity."},
        {"key": "graph_final_hop_very_close_threshold_km", "label": "Final-hop very-close", "type": "float",
         "min": 0, "default": 10.0, "unit": "km", "help": "Distance treated as 'very close' for the final hop."},
        {"key": "graph_final_hop_close_threshold_km", "label": "Final-hop close", "type": "float",
         "min": 0, "default": 30.0, "unit": "km", "help": "Distance treated as 'close' for the final hop."},
        {"key": "graph_final_hop_max_proximity_weight", "label": "Final-hop max weight", "type": "float",
         "min": 0, "max": 1, "default": 0.6, "help": "Upper bound on the final-hop proximity weight."},
        # Path validation bonus
        {"key": "graph_path_validation_max_bonus", "label": "Validation max bonus", "type": "float",
         "min": 0, "default": 0.3, "help": "Maximum confidence bonus from graph path validation."},
        {"key": "graph_path_validation_obs_divisor", "label": "Validation obs divisor", "type": "float",
         "min": 0.1, "default": 50.0, "help": "Observations divisor controlling how the validation bonus scales."},
        # Path-byte gating
        {"key": "require_path_bytes_greater_or_equal_to", "label": "Require path bytes ≥", "type": "int",
         "min": 0, "max": 3, "default": 0,
         "help": "Only respond when the path has at least this many bytes. 0/1 = allow all."},
        {"key": "require_path_bytes_failure_response", "label": "Path-byte reject reply", "type": "str",
         "default": "",
         "help": "Reply when rejected by the path-byte requirement. Empty = silent reject."},
        # Mesh graph capture / persistence
        {"key": "graph_capture_enabled", "label": "Graph capture", "type": "bool",
         "default": True,
         "help": "Observe routing paths from packets and store mesh-graph edges."},
        {"key": "graph_write_strategy", "label": "Graph write strategy", "type": "enum",
         "options": [
             {"value": "hybrid", "label": "Hybrid (balanced — default)"},
             {"value": "immediate", "label": "Immediate (safer, higher I/O)"},
             {"value": "batched", "label": "Batched (faster, crash-risky)"},
         ],
         "default": "hybrid",
         "help": "How edge updates are persisted to the database."},
        {"key": "graph_batch_interval_seconds", "label": "Graph batch interval", "type": "int",
         "min": 1, "default": 30, "unit": "s",
         "help": "How often pending edge updates are flushed (batched/hybrid)."},
        {"key": "graph_batch_max_pending", "label": "Graph batch max pending", "type": "int",
         "min": 1, "default": 100,
         "help": "Force a flush once this many edge updates are pending."},
        {"key": "graph_edge_expiration_days", "label": "Graph edge expiration", "type": "int",
         "min": 0, "default": 7, "unit": "days",
         "help": "Edges older than this are expired from the mesh graph."},
        {"key": "graph_startup_load_days", "label": "Graph startup load window", "type": "int",
         "min": 0, "default": 0, "unit": "days",
         "help": "Days of historical edges to load at startup. 0 = load all."},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.path_enabled = self.get_config_value('Path_Command', 'enabled', fallback=True, value_type='bool')
        # Explicit config toggle; set False to disable even when bot lat/lon are configured
        self.geographic_scoring_config_enabled = bot.config.getboolean(
            'Path_Command', 'geographic_scoring_enabled', fallback=True
        )
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

        # Get recency decay half-life for longer advert intervals (default: 12 hours, suggested: 36-48 for 48-72 hour intervals)
        self.recency_decay_half_life_hours = bot.config.getfloat('Path_Command', 'recency_decay_half_life_hours', fallback=12.0)

        # Check for preset first, then apply individual settings (preset can be overridden)
        preset = bot.config.get('Path_Command', 'path_selection_preset', fallback='balanced').lower()

        # Apply preset defaults, then individual settings override
        if preset == 'geographic':
            # Prioritize geographic proximity
            preset_graph_confidence_threshold = 0.5
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.5
            preset_final_hop_weight = 0.4
        elif preset == 'graph':
            # Prioritize graph evidence
            preset_graph_confidence_threshold = 0.9
            preset_distance_threshold = 50.0
            preset_distance_penalty = 0.2
            preset_final_hop_weight = 0.15
        else:  # 'balanced' (default)
            # Balanced approach
            preset_graph_confidence_threshold = 0.7
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.3
            preset_final_hop_weight = 0.25

        # Graph-based validation settings
        self.graph_based_validation = bot.config.getboolean('Path_Command', 'graph_based_validation', fallback=True)
        self.min_edge_observations = bot.config.getint('Path_Command', 'min_edge_observations', fallback=3)

        # Enhanced graph features
        self.graph_use_bidirectional = bot.config.getboolean('Path_Command', 'graph_use_bidirectional', fallback=True)
        self.graph_use_hop_position = bot.config.getboolean('Path_Command', 'graph_use_hop_position', fallback=True)
        self.graph_multi_hop_enabled = bot.config.getboolean('Path_Command', 'graph_multi_hop_enabled', fallback=True)
        self.graph_multi_hop_max_hops = bot.config.getint('Path_Command', 'graph_multi_hop_max_hops', fallback=2)
        self.graph_geographic_combined = bot.config.getboolean('Path_Command', 'graph_geographic_combined', fallback=False)
        self.graph_geographic_weight = bot.config.getfloat('Path_Command', 'graph_geographic_weight', fallback=0.7)
        self.graph_geographic_weight = max(0.0, min(1.0, self.graph_geographic_weight))  # Clamp to 0.0-1.0
        # Apply preset for confidence threshold, but allow override
        self.graph_confidence_override_threshold = bot.config.getfloat('Path_Command', 'graph_confidence_override_threshold', fallback=preset_graph_confidence_threshold)
        self.graph_confidence_override_threshold = max(0.0, min(1.0, self.graph_confidence_override_threshold))  # Clamp to 0.0-1.0
        self.graph_distance_penalty_enabled = bot.config.getboolean('Path_Command', 'graph_distance_penalty_enabled', fallback=True)

        self.graph_max_reasonable_hop_distance_km = bot.config.getfloat('Path_Command', 'graph_max_reasonable_hop_distance_km', fallback=preset_distance_threshold)
        self.graph_distance_penalty_strength = bot.config.getfloat('Path_Command', 'graph_distance_penalty_strength', fallback=preset_distance_penalty)
        self.graph_distance_penalty_strength = max(0.0, min(1.0, self.graph_distance_penalty_strength))  # Clamp to 0.0-1.0
        self.graph_zero_hop_bonus = bot.config.getfloat('Path_Command', 'graph_zero_hop_bonus', fallback=0.4)
        self.graph_zero_hop_bonus = max(0.0, min(1.0, self.graph_zero_hop_bonus))  # Clamp to 0.0-1.0
        self.graph_prefer_stored_keys = bot.config.getboolean('Path_Command', 'graph_prefer_stored_keys', fallback=True)

        # Final hop proximity settings for graph selection
        # Defaults based on LoRa ranges: typical < 30km, long up to 200km, very close < 10km
        self.graph_final_hop_proximity_enabled = bot.config.getboolean('Path_Command', 'graph_final_hop_proximity_enabled', fallback=True)
        self.graph_final_hop_proximity_weight = bot.config.getfloat('Path_Command', 'graph_final_hop_proximity_weight', fallback=preset_final_hop_weight)
        self.graph_final_hop_proximity_weight = max(0.0, min(1.0, self.graph_final_hop_proximity_weight))  # Clamp to 0.0-1.0
        self.graph_final_hop_max_distance = bot.config.getfloat('Path_Command', 'graph_final_hop_max_distance', fallback=0.0)
        self.graph_final_hop_proximity_normalization_km = bot.config.getfloat('Path_Command', 'graph_final_hop_proximity_normalization_km', fallback=200.0)  # Long LoRa range
        self.graph_final_hop_very_close_threshold_km = bot.config.getfloat('Path_Command', 'graph_final_hop_very_close_threshold_km', fallback=10.0)
        self.graph_final_hop_close_threshold_km = bot.config.getfloat('Path_Command', 'graph_final_hop_close_threshold_km', fallback=30.0)  # Typical LoRa range
        self.graph_final_hop_max_proximity_weight = bot.config.getfloat('Path_Command', 'graph_final_hop_max_proximity_weight', fallback=0.6)
        self.graph_final_hop_max_proximity_weight = max(0.0, min(1.0, self.graph_final_hop_max_proximity_weight))  # Clamp to 0.0-1.0
        self.graph_path_validation_max_bonus = bot.config.getfloat('Path_Command', 'graph_path_validation_max_bonus', fallback=0.3)
        self.graph_path_validation_max_bonus = max(0.0, min(1.0, self.graph_path_validation_max_bonus))  # Clamp to 0.0-1.0
        self.graph_path_validation_obs_divisor = bot.config.getfloat('Path_Command', 'graph_path_validation_obs_divisor', fallback=50.0)

        # Get star bias multiplier (how much to boost starred repeaters' scores)
        # Default 2.5 means starred repeaters get 2.5x their normal score
        self.star_bias_multiplier = bot.config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5)
        self.star_bias_multiplier = max(1.0, self.star_bias_multiplier)  # Ensure at least 1.0

        # Get confidence indicator symbols from config
        self.high_confidence_symbol = bot.config.get('Path_Command', 'high_confidence_symbol', fallback='🎯')
        self.medium_confidence_symbol = bot.config.get('Path_Command', 'medium_confidence_symbol', fallback='📍')
        self.low_confidence_symbol = bot.config.get('Path_Command', 'low_confidence_symbol', fallback='❓')

        # Check if "p" shortcut is enabled (on by default)
        self.enable_p_shortcut = bot.config.getboolean('Path_Command', 'enable_p_shortcut', fallback=True)
        if self.enable_p_shortcut:
            # Add "p" to keywords if enabled
            if "p" not in self.keywords:
                self.keywords.append("p")

        reply_prefix_raw = bot.config.get('Path_Command', 'reply_prefix', fallback='')
        self.path_reply_prefix = self._strip_quotes_from_config(reply_prefix_raw).strip()

        minimum_path_bytes_raw = bot.config.getint('Path_Command', 'minimum_path_bytes', fallback=0)
        if minimum_path_bytes_raw not in (0, 1, 2, 3):
            self.logger.warning(
                f"Invalid Path_Command.minimum_path_bytes={minimum_path_bytes_raw}; defaulting to 0"
            )
            self.minimum_path_bytes = 0
        else:
            self.minimum_path_bytes = minimum_path_bytes_raw

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
                        if self.geographic_scoring_config_enabled:
                            self.geographic_guessing_enabled = True
                            self.logger.info(f"Geographic proximity guessing enabled with bot location: {lat:.4f}, {lon:.4f}")
                        else:
                            self.logger.info("Geographic proximity guessing disabled via config (geographic_scoring_enabled = false)")
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

    def _inference_config(self) -> PathInferenceConfig:
        """Build a shared-engine config snapshot from this command's current attributes.

        Read fresh on each call so tests that mutate ``self.*`` after construction are honored.
        ``bot_command=True`` selects the bot `path` command's selection semantics in the engine.
        """
        return PathInferenceConfig(
            geographic_guessing_enabled=self.geographic_guessing_enabled,
            bot_latitude=self.bot_latitude,
            bot_longitude=self.bot_longitude,
            geographic_scoring_enabled=self.geographic_scoring_config_enabled,
            proximity_method=self.proximity_method,
            path_proximity_fallback=self.path_proximity_fallback,
            max_proximity_range=self.max_proximity_range,
            max_repeater_age_days=self.max_repeater_age_days,
            recency_weight=self.recency_weight,
            proximity_weight=self.proximity_weight,
            recency_decay_half_life_hours=self.recency_decay_half_life_hours,
            graph_based_validation=self.graph_based_validation,
            min_edge_observations=self.min_edge_observations,
            graph_use_bidirectional=self.graph_use_bidirectional,
            graph_use_hop_position=self.graph_use_hop_position,
            graph_multi_hop_enabled=self.graph_multi_hop_enabled,
            graph_multi_hop_max_hops=self.graph_multi_hop_max_hops,
            graph_geographic_combined=self.graph_geographic_combined,
            graph_geographic_weight=self.graph_geographic_weight,
            graph_confidence_override_threshold=self.graph_confidence_override_threshold,
            graph_distance_penalty_enabled=self.graph_distance_penalty_enabled,
            graph_max_reasonable_hop_distance_km=self.graph_max_reasonable_hop_distance_km,
            graph_distance_penalty_strength=self.graph_distance_penalty_strength,
            graph_zero_hop_bonus=self.graph_zero_hop_bonus,
            graph_prefer_stored_keys=self.graph_prefer_stored_keys,
            graph_final_hop_proximity_enabled=self.graph_final_hop_proximity_enabled,
            graph_final_hop_proximity_weight=self.graph_final_hop_proximity_weight,
            graph_final_hop_max_distance=self.graph_final_hop_max_distance,
            graph_final_hop_proximity_normalization_km=self.graph_final_hop_proximity_normalization_km,
            graph_final_hop_very_close_threshold_km=self.graph_final_hop_very_close_threshold_km,
            graph_final_hop_close_threshold_km=self.graph_final_hop_close_threshold_km,
            graph_final_hop_max_proximity_weight=self.graph_final_hop_max_proximity_weight,
            graph_path_validation_max_bonus=self.graph_path_validation_max_bonus,
            graph_path_validation_obs_divisor=self.graph_path_validation_obs_divisor,
            star_bias_multiplier=self.star_bias_multiplier,
            bot_command=True,
        )

    def _bytes_per_hop_from_nodes_and_routing(
        self, node_ids: list[str], routing_info: Optional[dict[str, Any]]
    ) -> int:
        """Bytes per hop from packet metadata or inferred from hex node width."""
        return bytes_per_hop_from_routing_and_nodes(routing_info, node_ids)

    def _should_resolve_repeater_names(
        self, node_ids: list[str], routing_info: Optional[dict[str, Any]]
    ) -> bool:
        if self.minimum_path_bytes in (0, 1):
            return True
        bph = self._bytes_per_hop_from_nodes_and_routing(node_ids, routing_info)
        return bph >= self.minimum_path_bytes

    def _format_path_reply_prefix(self, message: MeshMessage) -> str:
        if not self.path_reply_prefix:
            return ''
        formatted = self.format_response(message, self.path_reply_prefix).rstrip()
        if not formatted:
            return ''
        return formatted + '\n'

    def _format_repeater_resolution_deferred(self, node_ids: list[str]) -> str:
        path_display = ','.join(node_ids)
        return self.translate(
            'commands.path.repeater_resolution_deferred',
            path=path_display,
            minimum_path_bytes=self.minimum_path_bytes,
        )

    async def _decode_node_ids(
        self, node_ids: list[str], routing_info: Optional[dict[str, Any]] = None
    ) -> str:
        self.logger.info(f"Decoding path with {len(node_ids)} nodes: {','.join(node_ids)}")
        if not self._should_resolve_repeater_names(node_ids, routing_info):
            return self._format_repeater_resolution_deferred(node_ids)
        repeater_info = await self._lookup_repeater_names(node_ids)
        return self._format_path_response(node_ids, repeater_info)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
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
        content_lower = self.cleanup_message_for_matching(message)

        # Handle "p" shortcut if enabled
        if self.enable_p_shortcut:
            if content_lower == "p":
                return True  # Just "p" by itself
            elif content_lower.startswith('p ') and len(content_lower) > 2:
                return True  # "p " followed by path data

        # Check if message starts with any of our keywords
        return any(content_lower == keyword or content_lower.startswith(keyword + ' ') for keyword in self.keywords)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute path decode command"""
        self.logger.info(f"Path command executed with content: {message.content}")

        if not await self.enforce_path_byte_requirement(message, 'Path_Command'):
            return True

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

    async def _decode_path(
        self, path_input: str, routing_info: Optional[dict[str, Any]] = None
    ) -> str:
        """Decode hex path data to repeater names.
        Comma-separated tokens infer hop size (2, 4, or 6 hex chars per node).
        Otherwise uses bot.prefix_hex_chars via parse_path_string().
        """
        try:
            # Strip hop-count suffix if present (e.g. "01,5f (2 hops)")
            path_input = re.sub(r'\s*\([^)]*hops?[^)]*\)', '', path_input, flags=re.IGNORECASE)
            path_input = path_input.strip()

            node_ids = None
            # Comma-separated: infer hex chars per node from token length (2, 4, or 6)
            if ',' in path_input:
                tokens = [t.strip() for t in path_input.split(',') if t.strip()]
                if tokens:
                    lengths = {len(t) for t in tokens}
                    valid_hex = all(
                        len(t) in (2, 4, 6) and all(c in '0123456789aAbBcCdDeEfF' for c in t)
                        for t in tokens
                    )
                    if valid_hex and len(lengths) == 1 and next(iter(lengths)) in (2, 4, 6):
                        node_ids = [t.upper() for t in tokens]

            if node_ids is None:
                prefix_hex_chars = getattr(self.bot, 'prefix_hex_chars', 2)
                node_ids = parse_path_string(path_input, prefix_hex_chars=prefix_hex_chars)

            if not node_ids:
                return self.translate('commands.path.no_valid_hex')

            return await self._decode_node_ids(node_ids, routing_info)

        except Exception as e:
            self.logger.error(f"Error decoding path: {e}")
            return self.translate('commands.path.error_decoding', error=str(e))

    async def _lookup_repeater_names(
        self,
        node_ids: list[str],
        lookup_func: Optional[Callable[[str], list[dict[str, Any]]]] = None,
    ) -> dict[str, dict[str, Any]]:
        """Look up repeater names for given node IDs.

        Args:
            node_ids: List of node prefixes to look up.
            lookup_func: Optional test hook. When provided, used instead of
                repeater_manager/db_manager. Callable(node_id) -> list of repeater dicts.
        """
        repeater_info = {}

        try:
            # Skip API cache for path decoding - use database with improved proximity logic
            # API cache doesn't have recency-based proximity selection needed for path decoding

            # Sender location (for path-proximity first-hop selection) is constant across nodes;
            # compute it once, only when geographic guessing is active.
            sender_location = self._get_sender_location() if self.geographic_guessing_enabled else None

            # Query the database for repeaters with matching prefixes
            # Node IDs are the configured prefix of the public key (see Bot.prefix_bytes)
            for node_id in node_ids:
                # Test dependency injection: use provided lookup when available
                if lookup_func is not None:
                    results = lookup_func(node_id)
                    # Normalize to expected format (create_test_repeater already matches)
                    if results:
                        results = [
                            {
                                'name': r['name'],
                                'public_key': r['public_key'],
                                'device_type': r.get('device_type', 'repeater'),
                                'last_seen': r.get('last_seen', r.get('last_heard')),
                                'last_heard': r.get('last_heard', r.get('last_seen')),
                                'last_advert_timestamp': r.get('last_advert_timestamp'),
                                'is_active': r.get('is_active', True),
                                'latitude': r.get('latitude'),
                                'longitude': r.get('longitude'),
                                'city': r.get('city'),
                                'state': r.get('state'),
                                'country': r.get('country'),
                                'advert_count': r.get('advert_count', 1),
                                'signal_strength': r.get('signal_strength'),
                                'snr': r.get('snr'),
                                'hop_count': r.get('hop_count'),
                                'role': r.get('role', 'repeater'),
                                'is_starred': bool(r.get('is_starred', False)),
                            }
                            for r in results
                        ]
                else:
                    # First try complete tracking database (all heard contacts, filtered by role)
                    results = []
                    if hasattr(self.bot, 'repeater_manager'):
                        try:
                            # Get repeater devices from complete database (repeaters and roomservers)
                            complete_db = await self.bot.repeater_manager.get_repeater_devices(include_historical=True)

                            for row in complete_db:
                                if public_key_has_prefix(row['public_key'], node_id):
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
                                        'snr': row.get('snr'),  # Include SNR for zero-hop bonus
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
                                query = f'''
                                    SELECT name, public_key, device_type, last_heard, last_heard as last_seen,
                                           last_advert_timestamp, latitude, longitude, city, state, country,
                                           advert_count, signal_strength, snr, hop_count, role, is_starred
                                    FROM complete_contact_tracking
                                    WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
                                    AND (
                                        (last_advert_timestamp IS NOT NULL AND last_advert_timestamp >= datetime('now', '-{self.max_repeater_age_days} days'))
                                        OR (last_advert_timestamp IS NULL AND last_heard >= datetime('now', '-{self.max_repeater_age_days} days'))
                                    )
                                    ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                                '''
                            else:
                                query = '''
                                    SELECT name, public_key, device_type, last_heard, last_heard as last_seen,
                                           last_advert_timestamp, latitude, longitude, city, state, country,
                                           advert_count, signal_strength, snr, hop_count, role, is_starred
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
                                        'last_heard': row.get('last_heard', row['last_seen']),
                                        'last_advert_timestamp': row.get('last_advert_timestamp'),
                                        'is_active': True,
                                        'latitude': row['latitude'],
                                        'longitude': row['longitude'],
                                        'city': row['city'],
                                        'state': row['state'],
                                        'country': row['country'],
                                        'advert_count': row.get('advert_count', 0),
                                        'signal_strength': row.get('signal_strength'),
                                        'snr': row.get('snr'),
                                        'hop_count': row.get('hop_count'),
                                        'role': row.get('role'),
                                        'is_starred': bool(row.get('is_starred', 0))
                                    } for row in results
                                ]
                        except Exception as e:
                            self.logger.debug(f"Error querying complete_contact_tracking directly: {e}")
                            results = []

                if results:
                    # Build repeaters_data with the fields the selection engine needs. hop_count is
                    # intentionally omitted (this preserves prior behavior: the bot path's graph
                    # selection never applied the zero-hop bonus through this code path).
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
                            'snr': row.get('snr'),  # Include SNR for zero-hop bonus
                            'is_starred': row.get('is_starred', False)  # Include star status for bias
                        } for row in results
                    ]

                    # Delegate recency filtering, graph-based disambiguation, and geographic
                    # proximity to the shared engine (modules.path_inference). Candidate gathering
                    # (above), output shaping, and the device-contacts fallback (below) stay here.
                    selection = select_node_repeater(
                        node_id, repeaters_data, node_ids, self._inference_config(),
                        mesh_graph=getattr(self.bot, 'mesh_graph', None),
                        db_manager=self.bot.db_manager,
                        logger=self.logger,
                        graph_n=getattr(self.bot, 'prefix_hex_chars', 2),
                        sender_location=sender_location,
                    )

                    if selection.status == 'resolved':
                        # High confidence selection (graph or geographic)
                        selected_repeater = selection.repeater
                        repeater_info[node_id] = {
                            'name': selected_repeater['name'],
                            'public_key': selected_repeater['public_key'],
                            'device_type': selected_repeater['device_type'],
                            'last_seen': selected_repeater['last_seen'],
                            'is_active': selected_repeater['is_active'],
                            'found': True,
                            'collision': False,
                            'geographic_guess': (selection.method == 'geographic'),
                            'graph_guess': (selection.method == 'graph'),
                            'confidence': selection.confidence
                        }
                    elif selection.status == 'collision':
                        # Low confidence or no selection method - show collision warning
                        repeater_info[node_id] = {
                            'found': True,
                            'collision': True,
                            'matches': selection.matches,
                            'node_id': node_id,
                            'repeaters': selection.recent_repeaters
                        }
                    elif selection.status == 'single':
                        # Single recent match after filtering - no choice made, so no confidence indicator
                        repeater = selection.repeater
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
                            if public_key_has_prefix(public_key, node_id):
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

    async def _get_api_cache_data(self) -> Optional[dict[str, dict[str, Any]]]:
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


    def _get_sender_location(self) -> Optional[tuple[float, float]]:
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

    def _filter_recent_repeaters(self, repeaters: list[dict[str, Any]], cutoff_hours: int = 24) -> list[dict[str, Any]]:
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

    def _select_repeater_by_graph(self, repeaters: list[dict[str, Any]], node_id: str,
                                  path_context: list[str],
                                  path_prefix_hex_chars: Optional[int] = None) -> tuple[Optional[dict[str, Any]], float, str]:
        """Select a repeater for a colliding prefix using mesh-graph evidence.

        Thin wrapper over the shared engine (modules.path_inference.select_repeater_by_graph);
        kept as a method because the bot wiring and tests call it directly. When the path was
        decoded with multi-byte hops, pass path_prefix_hex_chars (e.g. 4 or 6) for candidate
        matching; graph lookups normalize to bot.prefix_hex_chars.
        """
        return select_repeater_by_graph(
            repeaters, node_id, path_context, self._inference_config(),
            mesh_graph=getattr(self.bot, 'mesh_graph', None),
            db_manager=self.bot.db_manager,
            logger=self.logger,
            graph_n=getattr(self.bot, 'prefix_hex_chars', 2),
            path_prefix_hex_chars=path_prefix_hex_chars,
        )

    def _format_path_response(self, node_ids: list[str], repeater_info: dict[str, dict[str, Any]]) -> str:
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
                elif info.get('geographic_guess', False) or info.get('graph_guess', False):
                    # Geographic or graph-based selection
                    name = info.get('name', self.translate('commands.path.unknown_name'))
                    confidence = info.get('confidence', 0.0)

                    # Truncate name if too long
                    truncation = self.translate('commands.path.truncation')
                    name = self._truncate_to_byte_length(name, 20, truncation)

                    # Add confidence indicator
                    if confidence >= 0.9:
                        confidence_indicator = self.high_confidence_symbol
                    elif confidence >= 0.8:
                        confidence_indicator = self.medium_confidence_symbol
                    else:
                        confidence_indicator = self.low_confidence_symbol

                    # Use geographic translation key for backward compatibility, or add graph-specific if needed
                    line = self.translate('commands.path.node_geographic', node_id=node_id, name=name, confidence=confidence_indicator)
                else:
                    # Single repeater found
                    name = info.get('name', self.translate('commands.path.unknown_name'))

                    truncation = self.translate('commands.path.truncation')
                    name = self._truncate_to_byte_length(name, 27, truncation)

                    line = self.translate('commands.path.node_format', node_id=node_id, name=name)
            else:
                # Unknown repeater
                line = self.translate('commands.path.node_unknown', node_id=node_id)

            line = self._truncate_to_byte_length(line, 150)

            lines.append(line)

        # Return all lines - let _send_path_response handle the splitting
        return "\n".join(lines)

    async def _send_path_response(self, message: MeshMessage, response: str):
        """Send path response, splitting into multiple messages if necessary"""
        prefix = self._format_path_reply_prefix(message)
        self.last_response = prefix + response if prefix else response

        max_length = self.get_max_message_length(message)
        prefix_len = self._count_byte_length(prefix)
        first_segment_max = max_length - prefix_len
        if first_segment_max < 1:
            first_segment_max = 1

        if self._count_byte_length(response) + prefix_len <= max_length:
            await self.send_response(message, prefix + response)
            return

        lines = response.split('\n')
        current_message = ""
        message_count = 0

        for i, line in enumerate(lines):
            body_budget = first_segment_max if message_count == 0 else max_length
            if self._count_byte_length(current_message) + self._count_byte_length(line) + 1 > body_budget:
                if current_message:
                    if i < len(lines):
                        current_message += self.translate('commands.path.continuation_end')
                    out = (prefix + current_message.rstrip()) if message_count == 0 else current_message.rstrip()
                    await self.send_response(
                        message, out,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    await asyncio.sleep(3.0)
                    message_count += 1

                if message_count > 0:
                    current_message = self.translate('commands.path.continuation_start', line=line)
                else:
                    current_message = line
            else:
                if current_message:
                    current_message += f"\n{line}"
                else:
                    current_message = line

        if current_message:
            out = (prefix + current_message.rstrip()) if message_count == 0 else current_message.rstrip()
            await self.send_response(message, out, skip_user_rate_limit=True)

    async def _extract_path_from_recent_messages(self) -> str:
        """Extract path from the current message's path information (same as test command).
        Prefers already-extracted routing_info.path_nodes when present (multi-byte path support).
        """
        try:
            if not hasattr(self, '_current_message') or not self._current_message:
                return self.translate('commands.path.no_path')

            msg = self._current_message

            # Prefer routing_info when present (no re-parsing; preserves bytes_per_hop)
            routing_info = getattr(msg, 'routing_info', None)
            if routing_info is not None:
                path_length = routing_info.get('path_length', 0)
                if path_length == 0:
                    return self.translate('commands.path.direct_connection')
                path_nodes = routing_info.get('path_nodes', [])
                if path_nodes:
                    node_ids = [n.upper() for n in path_nodes]
                    return await self._decode_node_ids(node_ids, routing_info)

            # Fallback: parse message.path string (e.g. no routing_info or legacy path)
            if not msg.path:
                return self.translate('commands.path.no_path')

            path_string = msg.path
            if "Direct" in path_string or "0 hops" in path_string:
                return self.translate('commands.path.direct_connection')

            path_part = path_string.split(" via ROUTE_TYPE_")[0] if " via ROUTE_TYPE_" in path_string else path_string

            if ',' in path_part:
                return await self._decode_path(path_part, routing_info)
            hex_pattern = rf'[0-9a-fA-F]{{{getattr(self.bot, "prefix_hex_chars", 2)}}}'
            if re.search(hex_pattern, path_part):
                return await self._decode_path(path_part, routing_info)
            return self.translate('commands.path.path_prefix', path_string=path_string)

        except Exception as e:
            self.logger.error(f"Error extracting path from current message: {e}")
            return self.translate('commands.path.error_extracting', error=str(e))

    def _count_byte_length(self, text: str) -> int:
        """Count UTF-8 byte length of text. This matches the RF packet byte limit."""
        return len(text.encode('utf-8'))

    def _truncate_to_byte_length(self, text: str, max_bytes: int, ellipsis: str = "...") -> str:
        """Truncate text to fit within max UTF-8 byte length, never splitting multi-byte chars."""
        text_bytes: bytes = text.encode('utf-8')
        if len(text_bytes) <= max_bytes:
            return text

        ellipsis_bytes: bytes = ellipsis.encode('utf-8')
        available: int = max_bytes - len(ellipsis_bytes)
        if available <= 0:
            return ellipsis

        truncated: str = text_bytes[:available].decode('utf-8', errors='ignore')
        return truncated + ellipsis

    def get_help(self) -> str:
        """Get help text for the path command"""
        return self.translate('commands.path.help')

    def get_help_text(self) -> str:
        """Get help text for the path command (used by help system)"""
        return self.get_help()
