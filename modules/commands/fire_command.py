#!/usr/bin/env python3
"""List active fires or show one fire's details."""

from typing import List

from .base_command import BaseCommand
from ..models import MeshMessage
from .. import watchduty_poll


class FireCommand(BaseCommand):
    name = "fire"
    keywords = ["fire"]
    description = "List active fires or show details (usage: fire [list #|Watch Duty id|name])"
    category = "info"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "List active fires when no args; show one fire detail with an argument"
    usage = "fire [list #|Watch Duty id|name]"
    examples = ["fire", "fire 1", "fire 93683", "fire Woods Fire"]

    def __init__(self, bot):
        super().__init__(bot)
        self._enabled = self.get_config_value(
            "Fire_Command", "enabled", fallback=False, value_type="bool"
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

    async def execute(self, message: MeshMessage) -> bool:
        tail = self._args_tail(message)
        try:
            events = watchduty_poll.fetch_active_geo_events_for_user_query(
                self.bot.config,
                include_prescribed=self._include_prescribed,
            )
        except Exception as e:
            self.logger.error("fire command: fetch failed: %s", e)
            return await self.send_response(message, "Could not load fires (Watch Duty).")
        if not tail:
            if not events:
                return await self.send_response(message, "No active fires match the current filter.")
            max_len = self.get_max_message_length(message)
            lines: List[str] = [f"Active fires ({len(events)}):"]
            for i, event in enumerate(events, start=1):
                name = (event.get("name") or f"Event {event.get('id')}").strip()
                loc = watchduty_poll.format_location_short(event)
                eid = event.get("id")
                id_part = f" · {eid}" if eid is not None else ""
                lines.append(f"{i}. {name} ({loc}){id_part}")
            chunks = watchduty_poll.mesh_pack_lines(lines, max_len)
            if len(chunks) == 1:
                return await self.send_response(message, chunks[0])
            return await self.send_response_chunked(message, chunks)

        event, err = watchduty_poll.resolve_active_event_by_query(
            events,
            tail,
            config=self.bot.config,
            include_prescribed=self._include_prescribed,
        )
        if err:
            if err == "usage":
                return await self.send_response(
                    message,
                    "Usage: fire [list #|Watch Duty id|name] — ids match app.watchduty.org/i/<id>.",
                )
            return await self.send_response(message, err)

        eid = event.get("id")
        if eid is None:
            return await self.send_response(message, "Invalid event (missing id).")

        detail = watchduty_poll.fetch_event_detail(int(eid))
        if not detail:
            detail = event
        name = (detail.get("name") or f"Event {eid}").strip()
        acres = watchduty_poll.get_event_acres(detail)
        acres_s = f"{acres:g} ac" if acres is not None else "acres: unknown"
        containment = watchduty_poll.format_containment_display(detail)
        location = watchduty_poll.format_location(detail)
        evac_count = watchduty_poll.evacuation_display_count(detail)

        lines = [
            name,
            f"{acres_s} | {containment} contained",
            f"{location}",
            f"evacs: {evac_count}",
        ]
        max_len = self.get_max_message_length(message)
        chunks = watchduty_poll.mesh_pack_lines(lines, max_len)
        if len(chunks) == 1:
            return await self.send_response(message, chunks[0])
        return await self.send_response_chunked(message, chunks)
