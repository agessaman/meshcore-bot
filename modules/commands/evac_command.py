#!/usr/bin/env python3
"""List Watch Duty evacuation orders for a fire."""

from typing import List, Optional, Tuple

from .base_command import BaseCommand
from ..models import MeshMessage
from .. import watchduty_poll


class EvacCommand(BaseCommand):
    name = "evac"
    keywords = ["evac", "evacs"]
    description = "List evacuation info (usage: evac <list #|Watch Duty id|name> [item #])"
    category = "info"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "Show Watch Duty evacuation orders, warnings, notes, and zone status lines"
    usage = "evac <list #|Watch Duty id|name> [item #]"
    examples = ["evac Woods Fire", "evac 1", "evac 93683", "evac 93817 2"]

    def __init__(self, bot):
        super().__init__(bot)
        self._enabled = self.get_config_value(
            "Evac_Command", "enabled", fallback=False, value_type="bool"
        )
        self._include_prescribed = self.get_config_value(
            "Fires_Command", "include_prescribed", fallback=False, value_type="bool"
        )

    def can_execute(self, message: MeshMessage) -> bool:
        if not self._enabled:
            return False
        return super().can_execute(message)

    def _args_tail(self, message: MeshMessage) -> str:
        content = message.content.strip()
        if self._command_prefix and content.startswith(self._command_prefix):
            content = content[len(self._command_prefix) :].strip()
        elif content.startswith("!"):
            content = content[1:].strip()
        content = self._strip_mentions(content)
        parts = content.split()
        if not parts:
            return ""
        kws = {x.lower() for x in self.keywords}
        if parts[0].lower() not in kws:
            return ""
        return " ".join(parts[1:]).strip()

    @staticmethod
    def _parse_event_and_item_query(tail: str) -> Tuple[str, Optional[int]]:
        """
        Parse ``evac`` args into event query and optional evac item number.
        Supports: ``evac <event>`` and ``evac <event> <item #>``.
        """
        text = (tail or "").strip()
        if not text:
            return "", None
        parts = text.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            item_n = int(parts[-1])
            if item_n >= 1:
                return " ".join(parts[:-1]).strip(), item_n
        return text, None

    async def execute(self, message: MeshMessage) -> bool:
        tail = self._args_tail(message)
        event_query, item_n = self._parse_event_and_item_query(tail)
        if not event_query:
            return await self.send_response(
                message,
                "Usage: evac <list #|Watch Duty id|name> [item #] — same list as fires; id from app.watchduty.org/i/<id>",
            )

        try:
            events = watchduty_poll.fetch_active_geo_events_for_user_query(
                self.bot.config,
                include_prescribed=self._include_prescribed,
            )
        except Exception as e:
            self.logger.error("evac command: fetch failed: %s", e)
            return await self.send_response(message, "Could not load fires (Watch Duty).")

        event, err = watchduty_poll.resolve_active_event_by_query(
            events,
            event_query,
            config=self.bot.config,
            include_prescribed=self._include_prescribed,
        )
        if err:
            if err == "usage":
                return await self.send_response(
                    message,
                    "Usage: evac <list #|Watch Duty id|name> [item #] — same list as fires; id from app.watchduty.org/i/<id>",
                )
            return await self.send_response(message, err)

        eid = event.get("id")
        if eid is None:
            return await self.send_response(message, "Invalid event (missing id).")

        detail = watchduty_poll.fetch_event_detail(int(eid))
        if not detail:
            detail = event
        name = (detail.get("name") or f"Event {eid}").strip()

        lines_body = watchduty_poll.evacuation_display_lines(detail)
        max_len = self.get_max_message_length(message)

        if not lines_body:
            msg = f"No evacuation info listed for {name} on Watch Duty."
            return await self.send_response(message, msg[:max_len])

        if item_n is not None:
            if item_n > len(lines_body):
                return await self.send_response(
                    message,
                    f"Only {len(lines_body)} evacuation item(s) for {name}. Try: evac {eid}",
                )
            full_text = lines_body[item_n - 1]
            lines: List[str] = [f"Evac — {name}:", f"{item_n}. {full_text}"]
        else:
            lines = [f"Evac — {name}:"]
            for i, line in enumerate(lines_body, start=1):
                snippet = watchduty_poll.first_sentence(line)
                if not snippet:
                    continue
                if snippet != line and len(snippet) < len(line):
                    snippet = snippet.rstrip() + " [...]"
                lines.append(f"{i}. {snippet}")
            lines.append("Use: evac <fire> <#> for full text.")

        chunks = watchduty_poll.mesh_pack_lines(lines, max_len)
        if len(chunks) == 1:
            return await self.send_response(message, chunks[0])
        return await self.send_response_chunked(message, chunks)
