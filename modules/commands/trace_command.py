#!/usr/bin/env python3
"""
Trace and Tracer commands for the MeshCore Bot.
Link diagnostics: trace (manual path when given, reciprocal when no path); tracer (reciprocal so return is heard by bot).
"""

import asyncio
import re
from typing import Optional

from ..graph_trace_helper import update_mesh_graph_from_trace_data
from ..models import MeshMessage
from ..trace_runner import RunTraceResult, run_trace
from .base_command import BaseCommand


class TraceCommand(BaseCommand):
    """Trace (manual path) and Tracer (reciprocal path) for link diagnostics."""

    name = "trace"
    keywords = ["trace", "tracer"]
    description = "Run a trace along a path (trace=manual if path given, else round-trip; tracer=always round-trip)"
    requires_dm = False
    cooldown_seconds = 2
    category = "meshcore_info"

    short_description = "Run link trace (manual or reciprocal path)"
    usage = "trace [path]  or  tracer [path]"
    examples = ["trace 01,7a,55", "trace feed,6ddf,feed", "tracer", "tracer 01,7a,55"]

    def __init__(self, bot):
        super().__init__(bot)
        self.trace_enabled = self.get_config_value("Trace_Command", "enabled", fallback=True, value_type="bool")
        self.maximum_hops = self.bot.config.getint("Trace_Command", "maximum_hops", fallback=5)
        self.trace_mode = (self.bot.config.get("Trace_Command", "trace_mode", fallback="one_byte") or "one_byte").strip().lower()
        self.timeout_per_hop = self.bot.config.getfloat("Trace_Command", "timeout_per_hop_seconds", fallback=1.5)
        self.update_graph_one_byte = self.bot.config.getboolean(
            "Trace_Command", "update_graph_one_byte", fallback=True
        )
        self.update_graph_two_byte = self.bot.config.getboolean(
            "Trace_Command", "update_graph_two_byte", fallback=True
        )
        # Optional: single emoji or string for bot in trace output; unset/empty = "[Bot]"
        self.bot_label = (self.bot.config.get("Trace_Command", "bot_label", fallback="") or "").strip()
        if not self.bot_label:
            self.bot_label = "[Bot]"
        output_fmt = (self.bot.config.get("Trace_Command", "output_format", fallback="inline") or "inline").strip().lower()
        self.output_format = output_fmt if output_fmt in ("inline", "vertical") else "inline"

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.trace_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return (
            "trace [path] — run trace along path (return may not be heard). No path = round-trip like tracer. "
            "tracer [path] — round-trip so bot hears return. Path: comma-separated hex nodes "
            "(2-char 1-byte e.g. 01,7a,55; 4-char 2-byte e.g. feed,6ddf,feed). "
            "No path = use your message path (round-trip)."
        )

    def matches_keyword(self, message: MeshMessage) -> bool:
        content_lower = self.cleanup_message_for_matching(message)
        if content_lower == "trace" or content_lower == "tracer":
            return True
        return bool(content_lower.startswith("trace ") or content_lower.startswith("tracer "))

    def _extract_path_from_message(self, message: MeshMessage) -> list[str]:
        """Extract path node IDs from message.path (supports 1-byte, 2-byte, and 3-byte hashes)."""
        if not message.path:
            return []
        if "Direct" in message.path or "0 hops" in message.path:
            return []
        path_string = message.path
        if " via ROUTE_TYPE_" in path_string:
            path_string = path_string.split(" via ROUTE_TYPE_")[0]
        if "(" in path_string:
            path_string = path_string.split("(")[0].strip()
        path_string = path_string.strip()
        # Single node (e.g. "01 (1 hop)") has no comma
        if "," not in path_string:
            if len(path_string) in (2, 4, 6) and all(c in "0123456789abcdefABCDEF" for c in path_string):
                return [path_string.lower()]
            return []
        parts = path_string.split(",")
        valid = []
        expected_len = None
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) not in (2, 4, 6) or not all(c in "0123456789abcdefABCDEF" for c in part):
                continue
            if expected_len is None:
                expected_len = len(part)
            if len(part) != expected_len:
                continue
            valid.append(part.lower())
        return valid

    def _parse_path_arg(self, content: str) -> Optional[list[str]]:
        """Parse path from command content after 'trace ' or 'tracer '.
        Accepts comma-separated hex nodes where each segment is the same length:
          2-char = 1-byte (e.g. 01,7a,55), 4-char = 2-byte (e.g. feed,6ddf),
          6-char = 3-byte (e.g. feedca,6ddf01).
        Without commas, treats contiguous hex as 2-char (1-byte) nodes.
        Returns list of hex node IDs, or None if no path args / invalid.
        """
        content = content.strip()
        if content.startswith("!"):
            content = content[1:].strip()
        rest = ""
        for kw in ["tracer ", "trace "]:
            if content.lower().startswith(kw):
                rest = content[len(kw) :].strip()
                break
        if not rest:
            return None
        # Comma-separated: each segment is one node; preserves multibyte groupings
        if "," in rest:
            parts = [p.strip().lower() for p in rest.split(",") if p.strip()]
            if not parts:
                return None
            first_len = len(parts[0])
            if first_len not in (2, 4, 6) or not all(
                len(p) == first_len and all(c in "0123456789abcdef" for c in p)
                for p in parts
            ):
                return None
            return parts
        # No commas: treat as contiguous hex, split into 2-char (1-byte) nodes
        hex_chars = re.sub(r"\s+", "", rest).lower()
        if not hex_chars or len(hex_chars) % 2 != 0:
            return None
        if not all(c in "0123456789abcdef" for c in hex_chars):
            return None
        return [hex_chars[i : i + 2] for i in range(0, len(hex_chars), 2)]

    def _build_reciprocal_path(self, nodes: list[str]) -> list[str]:
        """Build round-trip path: [01,7a,55] -> [01,7a,55,7a,01] (out then back without duplicating destination)."""
        if not nodes or len(nodes) < 2:
            return list(nodes)
        # Return path: reverse of path excluding last node (so we don't duplicate the far end)
        return nodes + list(reversed(nodes[:-1]))

    def _format_trace_result(self, message: MeshMessage, result: RunTraceResult) -> str:
        """Format trace result as inline (default) or vertical per output_format config."""
        sender_str = f"@[{message.sender_id}] "
        if not result.success:
            return f"{sender_str}Trace failed: {result.error_message or 'unknown'}"
        if self.output_format == "vertical":
            return self._format_trace_vertical(sender_str, result)
        return self._format_trace_inline(sender_str, result)

    def _format_trace_inline(self, sender_str: str, result: RunTraceResult) -> str:
        """Single chain: bot_label SNR [node] ... SNR bot_label."""
        parts = [self.bot_label]
        for node in result.path_nodes:
            s = node.get("snr")
            h = node.get("hash")
            node_str = f"[{h}]" if h else self.bot_label
            if s is not None:
                parts.append(f"{s:.1f}")
            parts.append(node_str)
        return sender_str + " ".join(parts)

    def _format_trace_vertical(self, sender_str: str, result: RunTraceResult) -> str:
        """Vertical format: one line per hop, 'from → snr →' (next line's from); 'db' only on first SNR."""
        lines = [f"{sender_str}Trace:"]
        nodes = result.path_nodes
        for i, node in enumerate(nodes):
            s = node.get("snr")
            # Left label = source of this hop: bot for first hop, else previous node's hash
            if i == 0:
                from_label = self.bot_label
            else:
                prev_h = nodes[i - 1].get("hash")
                from_label = prev_h if prev_h else "—"
            if i == 0:
                # First hop: bot → SNR db →
                snr_str = f"{s:.1f} db" if s is not None else "—"
                lines.append(f"{from_label} → {snr_str} →")
            elif i == len(nodes) - 1:
                # Last hop: from_node → SNR → bot
                snr_str = f"{s:.1f}" if s is not None else "—"
                lines.append(f"{from_label} → {snr_str} → {self.bot_label}")
            else:
                snr_str = f"{s:.1f}" if s is not None else "—"
                lines.append(f"{from_label} → {snr_str} →")
        return "\n".join(lines)

    async def execute(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if content.startswith("!"):
            content = content[1:].strip()

        is_tracer = content.lower().startswith("tracer")
        path_arg = self._parse_path_arg(message.content)
        if path_arg is not None:
            path_nodes = path_arg[: self.maximum_hops]
        else:
            path_nodes = self._extract_path_from_message(message)
            if not path_nodes:
                await self.send_response(
                    message,
                    "Trace/tracer need a path (e.g. trace 01,7a,55) or a message that has a path.",
                )
                return True
            path_nodes = path_nodes[: self.maximum_hops]
            # Incoming path is sender → ... → bot; reverse so we have path from bot toward sender
            path_nodes = list(reversed(path_nodes))

        # Use reciprocal path for tracer (always) or when no path given (so round-trip completes)
        if is_tracer or path_arg is None:
            path_nodes = self._build_reciprocal_path(path_nodes)
            # Do not cap here: outbound was already capped above; truncating would drop the return path

        if not self.bot.connected or not self.bot.meshcore:
            await self.send_response(message, "Not connected to radio.")
            return True

        if not getattr(self.bot.meshcore, "commands", None) or not hasattr(
            self.bot.meshcore.commands, "send_trace"
        ):
            await self.send_response(message, "Trace not available (firmware/connection).")
            return True

        # Auto-detect flags from path element length: 2-char=1-byte→0, 4-char=2-byte→1
        node_len = len(path_nodes[0]) if path_nodes else 2
        flags = {2: 0, 4: 1}.get(node_len, 0)

        result = await run_trace(
            self.bot,
            path=path_nodes,
            flags=flags,
            timeout_seconds=None,
        )

        response = self._format_trace_result(message, result)
        sent = await self.send_response(message, response)
        # If rate limited (e.g. after retry), wait and retry once so the user gets the result
        if not sent and hasattr(self.bot, "command_manager") and self.bot.command_manager:
            wait_s = self.bot.command_manager.get_rate_limit_wait_seconds(
                self.bot.command_manager.get_rate_limit_key(message)
            )
            if wait_s > 0.1:
                await asyncio.sleep(wait_s)
                await self.send_response(message, response)

        if result.success and self.update_graph_one_byte and result.path_nodes:
            path_hashes = [n.get("hash") for n in result.path_nodes if n.get("hash")]
            if path_hashes and hasattr(self.bot, "mesh_graph") and self.bot.mesh_graph and self.bot.mesh_graph.capture_enabled:
                update_mesh_graph_from_trace_data(
                    self.bot,
                    path_hashes,
                    {},
                    is_our_trace=True,
                )

        return True
