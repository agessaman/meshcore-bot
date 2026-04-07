#!/usr/bin/env python3
"""
Data models for the MeshCore Bot
Contains shared data structures used across modules
"""

from dataclasses import dataclass
from typing import Any

# Firmware reserves extra bytes for regional (non-global) TC_FLOOD scope on channel text.
CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD = 10


@dataclass
class MeshMessage:
    """Simplified message structure for our bot"""
    content: str
    sender_id: str | None = None
    sender_pubkey: str | None = None
    channel: str | None = None
    hops: int | None = None
    path: str | None = None
    is_dm: bool = False
    timestamp: int | None = None
    snr: float | None = None
    rssi: int | None = None
    elapsed: str | None = None
    # When set from RF routing: path_nodes, path_hex, bytes_per_hop, path_length, route_type, etc.
    routing_info: dict[str, Any] | None = None
    # Flood scope tag matched from TC_FLOOD transport code (set by message_handler)
    reply_scope: str | None = None

    def effective_outgoing_flood_scope(self, bot: Any) -> str:
        """Resolve outbound flood scope the same way as ``CommandManager.send_channel_message``.

        For channel replies: ``reply_scope`` when set, else ``[Channels] outgoing_flood_scope_override``.
        Empty string means global flood. DMs return ``""`` (not applicable).
        """
        if self.is_dm:
            return ""
        if self.reply_scope is not None:
            return (self.reply_scope or "").strip()
        scope_cfg = ""
        if bot.config.has_section("Channels") and bot.config.has_option(
            "Channels", "outgoing_flood_scope_override"
        ):
            scope_cfg = (bot.config.get("Channels", "outgoing_flood_scope_override") or "").strip()
        return scope_cfg

    @staticmethod
    def is_global_flood_scope(scope: str) -> bool:
        """Match ``send_channel_message`` global markers (before ``_normalize_scope_name``)."""
        return scope in ("", "*", "0", "None")
