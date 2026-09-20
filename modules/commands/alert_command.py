#!/usr/bin/env python3
"""
Alert command for the MeshCore Bot.

This command is a thin *orchestrator* over one or more alert services (see
``modules/service_plugins/base_alert_service.py``). It no longer talks to any
API directly; instead it:

1. Discovers the alert services connected via ``[Alert_Command] services``.
2. Decides which services to query for a given message (all connected
   services, or — when the message arrives in a channel a service polls — just
   that one service).
3. Asks each service for incident lines matching the user's query.
4. Distributes a total incident budget across the responding services,
   prefixes each service's output with its ``[LABEL]:`` header, and sends the
   messages.

See ``docs/alert-service.md`` for the full architecture and a guide to writing
new alert services.
"""

import asyncio
from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand

# Default total number of incidents shown across all services for one query.
DEFAULT_MAX_INCIDENTS_TOTAL = 10

# Default LoRa-friendly message length budget (bytes/chars).
DEFAULT_MAX_MESSAGE_LENGTH = 130

# Delay between successive response messages, seconds.
INTER_MESSAGE_DELAY = 2.0


class AlertCommand(BaseCommand):
    """Query connected alert services for active incidents.

    The command aggregates results from every configured alert service and
    presents them as label-prefixed messages. Individual services encapsulate
    all API and query-parsing logic.
    """

    # Plugin metadata
    name = "alert"
    keywords = ['alert', 'alerts', 'incident', 'incidents']
    description = "Get active emergency incidents (usage: alert seattle, alert 98258, alert 178th seattle)"
    category = "emergency"
    cooldown_seconds = 10  # 10 second cooldown to prevent API abuse

    # Documentation
    short_description = "Get active emergency incidents from connected alert services"
    usage = "alert <location>"
    examples = ["alert seattle", "alert 98101"]
    parameters = [
        {"name": "location", "description": "City, zip code, county, or street address"},
    ]
    requires_internet = True

    # Web-viewer settings schema (see modules/settings_schema.py).
    settings_schema = [
        {"key": "enabled", "label": "Enabled", "type": "bool", "default": True,
         "help": "Enable the alert command."},
        {"key": "services", "label": "Connected services", "type": "str",
         "default": "pulsepoint",
         "help": "Comma-separated alert service ids to query (e.g. pulsepoint)."},
        {"key": "max_incidents_total", "label": "Max incidents", "type": "int",
         "min": 1, "default": DEFAULT_MAX_INCIDENTS_TOTAL,
         "help": "Total incidents shown across all services for one query."},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        # Load enabled (standard 'enabled'; 'alert_enabled' legacy).
        self.alert_enabled = self.get_config_value('Alert_Command', 'enabled', fallback=None, value_type='bool')
        if self.alert_enabled is None:
            self.alert_enabled = self.get_config_value('Alert_Command', 'alert_enabled', fallback=True, value_type='bool')

        # Which alert services this command is connected to.
        self.service_names = self._load_service_names()

        # Total incident budget across all responding services.
        self.max_incidents_total = self.get_config_value(
            'Alert_Command', 'max_incidents_total',
            fallback=DEFAULT_MAX_INCIDENTS_TOTAL, value_type='int'
        )

    def _load_service_names(self) -> list[str]:
        """Load connected alert service ids from ``[Alert_Command] services``.

        Defaults to ``pulsepoint`` for backward compatibility with the original
        single-service alert command.

        Returns:
            List of alert service ids (lowercased, stripped).
        """
        raw = self.get_config_value('Alert_Command', 'services', fallback='pulsepoint', value_type='str')
        if not raw:
            return []
        return [s.strip().lower() for s in raw.split(',') if s.strip()]

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message."""
        if not self.alert_enabled:
            return False
        return super().can_execute(message)

    # ------------------------------------------------------------------ #
    # Service discovery / selection.
    # ------------------------------------------------------------------ #
    def _all_alert_services(self) -> list[Any]:
        """Return every loaded alert service instance.

        Alert services are loaded at bot startup (after commands are
        constructed), so this must be resolved lazily at execute time. Services
        are identified by the presence of an ``alert_service_id`` attribute
        rather than by dict key, because the service loader keys the registry
        by the class-derived name.
        """
        loaded = getattr(self.bot, 'services', None) or {}
        return [
            svc for svc in loaded.values()
            if getattr(svc, 'alert_service_id', None)
        ]

    def _get_connected_services(self, all_alert: list[Any]) -> list[Any]:
        """Filter loaded alert services down to the connected, ordered list."""
        by_id = {getattr(svc, 'alert_service_id'): svc for svc in all_alert}
        connected = []
        for name in self.service_names:
            svc = by_id.get(name)
            if svc is not None:
                connected.append(svc)
            else:
                self.logger.debug("Alert service '%s' configured but not loaded", name)
        return connected

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        """Normalize a channel name for comparison (case-insensitive, no '#')."""
        return channel.lower().strip().lstrip('#')

    def _get_services_for_message(self, message: MeshMessage) -> list[Any]:
        """Decide which alert services should answer this message.

        If the message arrives in a channel that a loaded alert service polls,
        only that service (or services) answers. Otherwise the connected
        services from ``[Alert_Command] services`` are queried.

        Args:
            message: The triggering message.

        Returns:
            Ordered list of alert service instances to query.
        """
        all_alert = self._all_alert_services()

        # Channel-specific: a service polling this channel answers alone.
        if not message.is_dm and message.channel:
            ch = self._normalize_channel(message.channel)
            channel_services = [
                svc for svc in all_alert
                if ch in [self._normalize_channel(c) for c in getattr(svc, 'polling_channels', []) or []]
            ]
            if channel_services:
                self.logger.debug(
                    "Alert: channel '%s' maps to service(s) %s",
                    message.channel,
                    [getattr(s, 'alert_service_id', '?') for s in channel_services],
                )
                return channel_services

        # Default: connected services from config.
        return self._get_connected_services(all_alert)

    # ------------------------------------------------------------------ #
    # Incident distribution.
    # ------------------------------------------------------------------ #
    def _distribute_incidents(self, counts: list[int], max_total: int) -> list[int]:
        """Compute an incident allocation per service given a total budget.

        Incidents are distributed as equally as possible. Any budget a service
        cannot use (because it returned fewer incidents than its share) is
        redistributed to services that have more incidents to show, so the
        total shown is maximized without exceeding ``max_total``.

        Args:
            counts: Number of incidents each service actually returned.
            max_total: Total incident budget across all services.

        Returns:
            A list of per-service allocations, parallel to ``counts``.
        """
        n = len(counts)
        if n == 0 or max_total <= 0:
            return [0] * n

        # Equal base share, with the remainder handed to the first services.
        base = max_total // n
        remainder = max_total % n
        alloc = [base + (1 if i < remainder else 0) for i in range(n)]

        # Cap each allocation at what the service can actually supply, tracking
        # freed budget for redistribution.
        alloc = [min(a, counts[i]) for i, a in enumerate(alloc)]

        # Redistribute leftover budget to services that still have more to show.
        while True:
            used = sum(alloc)
            leftover = max_total - used
            if leftover <= 0:
                break
            # Services that can take at least one more incident.
            hungry = [i for i in range(n) if alloc[i] < counts[i]]
            if not hungry:
                break
            progressed = False
            for i in hungry:
                if leftover <= 0:
                    break
                alloc[i] += 1
                leftover -= 1
                progressed = True
            if not progressed:
                break
        return alloc

    def _build_service_messages(self, label: str, incidents: list[str],
                                allocation: int, max_length: int) -> list[str]:
        """Build label-prefixed messages for one service's incidents.

        Shows up to ``allocation`` incidents, packed into as few messages as
        possible within ``max_length``. Every message carries the ``[LABEL]:``
        header. When the service returned more incidents than ``allocation``, a
        trailing ``(N more)`` note is appended (the service label already tells
        the user which source has more, so the source is not repeated).

        Args:
            label: The service label (already <=6 chars).
            incidents: All incident lines the service returned.
            allocation: How many of them this service may show.
            max_length: Max characters per message.

        Returns:
            A list of ready-to-send message strings.
        """
        shown = incidents[:allocation]
        if not shown:
            return []

        remaining = len(incidents) - len(shown)
        header = f"[{label}]:"

        more_note = f"({remaining} more)" if remaining > 0 else None

        # Pack incident lines into messages, each starting with the header.
        messages: list[str] = []
        current_lines = [header]
        current_length = len(header)

        for line in shown:
            addition = len(line) + 1  # +1 for the joining newline
            if current_length + addition > max_length and len(current_lines) > 1:
                # Current message is full; flush and start a new one with header.
                messages.append("\n".join(current_lines))
                current_lines = [header, line]
                current_length = len(header) + addition
            else:
                current_lines.append(line)
                current_length += addition

        # Append the "(N more)" note to the last message if it fits, else its
        # own message.
        if more_note:
            addition = len(more_note) + 1
            if current_length + addition <= max_length:
                current_lines.append(more_note)
            else:
                messages.append("\n".join(current_lines))
                current_lines = [header, more_note]
        messages.append("\n".join(current_lines))

        return messages

    # ------------------------------------------------------------------ #
    # Command execution.
    # ------------------------------------------------------------------ #
    async def _query_service(self, service: Any, query: str) -> list[str]:
        """Query a single service, converting any failure into an empty list."""
        try:
            result = await service.query_alerts(query)
            return list(result) if result else []
        except Exception as e:
            self.logger.error(
                "Alert service '%s' query failed: %s",
                getattr(service, 'alert_service_id', '?'), e
            )
            return []

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the alert command.

        Parses the user query, selects the services to ask, aggregates their
        incident lines, and sends label-prefixed messages.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True (the command always produces a response).
        """
        content = message.content.strip()
        parts = content.split(None, 1)
        if len(parts) < 2:
            await self.send_response(message, "Usage: alert <city|zipcode|street city|county>")
            return True

        query = parts[1].strip()

        # Backward compatibility: the legacy "all" suffix is now implicit. Strip
        # it so it is neither treated as part of the location nor rejected.
        if query.lower().endswith(' all'):
            query = query[:-4].strip()

        try:
            services = self._get_services_for_message(message)
            if not services:
                await self.send_response(message, "🚨 No alert services configured")
                return True

            # Query every selected service concurrently.
            results = await asyncio.gather(
                *[self._query_service(svc, query) for svc in services]
            )

            counts = [len(r) for r in results]
            if sum(counts) == 0:
                await self.send_response(message, "🚨 No active incidents")
                return True

            allocation = self._distribute_incidents(counts, self.max_incidents_total)
            max_length = self.get_max_message_length(message)

            # Build all messages, grouped per service (preserving service order).
            all_messages: list[str] = []
            for svc, incidents, alloc in zip(services, results, allocation):
                if alloc <= 0 or not incidents:
                    continue
                label = getattr(svc, 'label', getattr(svc, 'alert_service_id', '?'))
                all_messages.extend(
                    self._build_service_messages(label, incidents, alloc, max_length)
                )

            if not all_messages:
                await self.send_response(message, "🚨 No active incidents")
                return True

            for i, msg in enumerate(all_messages):
                await self.send_response(message, msg)
                if i < len(all_messages) - 1:
                    await asyncio.sleep(INTER_MESSAGE_DELAY)
            return True

        except Exception as e:
            self.logger.error(f"Error in alert command: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            await self.send_response(message, f"Error fetching alerts: {str(e)}")
            return True
