#!/usr/bin/env python3
"""
Data models for the MeshCore Bot
Contains shared data structures used across modules
"""

from dataclasses import dataclass
from typing import Any, Optional

# Firmware reserves extra bytes for regional (non-global) TC_FLOOD scope on channel text.
CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD = 10

# A DM carries no username prefix, so the whole cipher block is body.
DM_BODY_LIMIT = 158

# Channel text budget in UTF-8 bytes, covering the whole "<username>: <body>".
#
# The firmware accepts up to MAX_TEXT_LEN (160) and silently truncates past it,
# but 155 is the better ceiling. The firmware encrypts a 4-byte timestamp and a
# 1-byte text type ahead of the text, and AES pads to 16-byte blocks: 5 + 155
# fills the 160-byte block exactly (a 163-byte payload), while the last 5 bytes
# up to 160 spill into another block (a 179-byte payload) and cost 16 bytes of
# airtime for 5 bytes of text. Both sizes are legal packets that repeaters
# forward; this is about airtime per byte, not whether the mesh relays it.
CHANNEL_FRAME_TEXT_LIMIT = 155

# Floor for the body once a long username has been charged against the frame
# limit, so a verbose name cannot leave nothing to say. Only names over 121
# bytes reach it.
CHANNEL_BODY_FLOOR = 32


def channel_body_limit(username: Optional[str]) -> int:
    """Global-scope body budget in UTF-8 bytes for a channel message from ``username``.

    Channel messages go out as ``"<username>: <body>"``, so the budget is
    ``CHANNEL_FRAME_TEXT_LIMIT`` minus the name and the ``": "``. Regional scope
    costs a further ``CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD``, which callers
    subtract themselves once they know the outgoing scope.

    Shared by the command layer and the web viewer, which computes the same
    number in a process that has no bot object.
    """
    name = str(username or "Bot")
    return max(CHANNEL_FRAME_TEXT_LIMIT - len(name.encode("utf-8")) - 2, CHANNEL_BODY_FLOOR)


@dataclass
class MeshMessage:
    """Simplified message structure for our bot"""
    content: str
    sender_id: Optional[str] = None
    sender_pubkey: Optional[str] = None
    channel: Optional[str] = None
    hops: Optional[int] = None
    path: Optional[str] = None
    is_dm: bool = False
    timestamp: Optional[int] = None
    snr: Optional[float] = None
    rssi: Optional[int] = None
    elapsed: Optional[str] = None
    # When set from RF routing: path_nodes, path_hex, bytes_per_hop, path_length, route_type, etc.
    routing_info: Optional[dict[str, Any]] = None
    # Matched flood scope for the reply (e.g. "#west"), None means global flood
    reply_scope: Optional[str] = None
    # Lowercased content set by base_command.cleanup_message_for_matching
    content_lower: str = ""
    # Transient flag: True once CommandManager.check_keywords has stripped the
    # configured command prefix (and legacy "!") from content. Prevents per-command
    # cleanup_message_for_matching from re-stripping/re-rejecting an already-normalized
    # message, which previously broke matching for all-but-the-first command.
    prefix_normalized: bool = False
    # Transient: when not None, CommandManager.send_response appends the reply here
    # and transmits nothing. Set by CommandManager.render_command_output so a command
    # can be run for its text alone (e.g. a {cmd:...} placeholder in a scheduled
    # message) without spending airtime. A synthetic message only.
    capture_sink: Optional[list[str]] = None
    # On-air body at construction. Mention/prefix cleanup may rewrite ``content``
    # for command matching; display and web-viewer capture must use this snapshot.
    original_content: str = ""

    def __post_init__(self) -> None:
        if not self.original_content:
            self.original_content = self.content

    def effective_outgoing_flood_scope(self, bot: Any) -> str:
        """Resolve outbound flood scope the same way as ``CommandManager.send_channel_message``.

        For channel replies: ``reply_scope`` when set, else per-channel
        ``[Channels] flood_scope.<channel>``, else ``[Channels] outgoing_flood_scope_override``.
        Empty string means global flood. DMs return ``""`` (not applicable).
        """
        if self.is_dm:
            return ""
        if self.reply_scope is not None:
            return (self.reply_scope or "").strip()
        if self.channel and bot.config.has_section("Channels"):
            channel_key = self.channel.strip().removeprefix("#").lower()
            for key, value in bot.config.items("Channels"):
                if not key.startswith("flood_scope."):
                    continue
                configured_channel = key[len("flood_scope."):].strip().removeprefix("#").lower()
                if configured_channel == channel_key:
                    return (value or "").strip()
        scope_cfg = ""
        if bot.config.has_section("Channels") and bot.config.has_option(
            "Channels", "outgoing_flood_scope_override"
        ):
            scope_cfg = (bot.config.get("Channels", "outgoing_flood_scope_override") or "").strip()
        return scope_cfg

    @staticmethod
    def is_global_flood_scope(scope: str) -> bool:
        """Match ``send_channel_message`` global markers (before ``_normalize_scope_name``)."""
        return scope in ("", "*", "0", "None") or scope.lower() == "none"
