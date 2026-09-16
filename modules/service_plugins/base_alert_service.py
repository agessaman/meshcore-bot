#!/usr/bin/env python3
"""
Base alert service plugin for the MeshCore Bot.

An *alert service* is a plugin that knows how to talk to a single external
data source (PulsePoint, USGS, a weather API, a local feed, ...) and turn it
into short, LoRa-friendly incident lines. Every alert service supports two
independent modes:

* **Query mode** — a user runs the ``alert`` command (e.g. ``alert seattle``).
  The command asks every connected service for matching incidents via
  :meth:`BaseAlertService.query_alerts` and stitches the answers together.

* **Polling mode** — the service periodically fetches *new* incidents on its
  own and posts them to one or more channels. This is driven entirely inside
  the service; the ``alert`` command is not involved.

The base class implements the polling loop, channel posting, duplicate
tracking and all of the shared configuration plumbing. A concrete service
only has to implement a small number of methods (see the *Abstract interface*
section below). See ``docs/alert-service.md`` for a full developer guide.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Optional

from .base_service import BaseServicePlugin

# Query type constants. A service advertises which of these it understands via
# get_capabilities(); the alert command routes a parsed query to every service
# that claims support for the detected type.
QUERY_TYPE_COORDINATES = "coordinates"
QUERY_TYPE_ZIPCODE = "zipcode"
QUERY_TYPE_CITY = "city"
QUERY_TYPE_COUNTY = "county"
QUERY_TYPE_STREET_CITY = "street_city"
QUERY_TYPE_NATIVE = "native"  # service-specific identifier (e.g. an agency code)

ALL_QUERY_TYPES = (
    QUERY_TYPE_COORDINATES,
    QUERY_TYPE_ZIPCODE,
    QUERY_TYPE_CITY,
    QUERY_TYPE_COUNTY,
    QUERY_TYPE_STREET_CITY,
    QUERY_TYPE_NATIVE,
)

# Hard cap on the service label length. The label prefixes every message as
# "[LABEL]:\n", so it has to stay short to preserve payload budget on LoRa.
MAX_LABEL_LENGTH = 6

# Bound the in-memory dedup set so a long-running poller cannot grow without
# limit. When exceeded we keep the most-recently-added ids.
SEEN_IDS_MAX = 1000


class BaseAlertService(BaseServicePlugin):
    """Abstract base class for alert service plugins.

    Subclasses MUST set :attr:`alert_service_id` (the short name used to
    reference the service from ``[Alert_Command] services = ...``) and
    :attr:`config_section` (the ``config.ini`` section holding this service's
    settings).

    Abstract interface a subclass implements:

    * :meth:`parse_query` — classify a raw user query string.
    * :meth:`query_alerts` — return incident lines for a query (query mode).
    * :meth:`fetch_new_incidents` — return ``(id, line)`` pairs for polling.
    * :meth:`get_capabilities` — declare which query types are supported.

    Everything else (polling loop, channel posting, dedup, config helpers) is
    provided here.
    """

    # Short identifier used in [Alert_Command] services = ... and for service
    # lookup. Subclasses MUST override. Example: "pulsepoint".
    alert_service_id: str = ""

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        self._poll_task: Optional[asyncio.Task] = None
        # Incident ids already posted to channels this run (polling dedup).
        self._seen_incident_ids: set[str] = set()

        section = self.config_section or self._derive_config_section()

        # --- Common configuration shared by every alert service -------------
        # Service label: prefixes every outgoing message as "[LABEL]:". Falls
        # back to the service id (upper-cased, truncated) when unset.
        raw_label = (self.bot.config.get(section, "label", fallback="") or "").strip()
        if not raw_label:
            raw_label = (self.alert_service_id or self._derive_service_name()).upper()
        self.label = raw_label[:MAX_LABEL_LENGTH]

        # Polling configuration.
        self.polling_enabled = self.bot.config.getboolean(
            section, "polling_enabled", fallback=False
        )
        poll_ms = self.bot.config.getint(section, "polling_interval", fallback=300000)
        # Guard against a pathologically small interval hammering the upstream API.
        self.polling_interval_seconds = max(30.0, poll_ms / 1000.0)
        channels_raw = (
            self.bot.config.get(section, "polling_channels", fallback="") or ""
        )
        self.polling_channels = [
            ch.strip() for ch in channels_raw.split(",") if ch.strip()
        ]

    # ------------------------------------------------------------------ #
    # Abstract interface — subclasses implement these.
    # ------------------------------------------------------------------ #
    def parse_query(self, query: str) -> tuple[str, Optional[str], Optional[float], Optional[float]]:
        """Classify a raw user query string.

        Args:
            query: The raw query text following the command keyword, e.g.
                ``"seattle"`` or ``"178th seattle"`` or ``"98101"``.

        Returns:
            A ``(query_type, location, lat, lon)`` tuple. ``query_type`` should
            be one of the ``QUERY_TYPE_*`` constants. ``location`` is the
            cleaned location string (or None), and ``lat``/``lon`` are only set
            for coordinate queries.
        """
        raise NotImplementedError

    async def query_alerts(self, query: str) -> list[str]:
        """Return incident lines matching ``query`` (query mode).

        This is the entry point used by the ``alert`` command. Implementations
        should return a list of already-formatted incident strings, each no
        longer than ~130 characters and WITHOUT the service label prefix (the
        command adds that). Return an empty list when there are no matches.

        Implementations are expected to keep blocking I/O (HTTP, geocoding) off
        the event loop, e.g. by using :func:`asyncio.to_thread`.

        Args:
            query: The raw query text following the command keyword.

        Returns:
            A list of incident strings, most relevant first.
        """
        raise NotImplementedError

    async def fetch_new_incidents(self) -> list[tuple[str, str]]:
        """Return new incidents for polling mode as ``(incident_id, line)``.

        Called by the polling loop every ``polling_interval``. Return every
        currently-active incident as an ``(id, formatted_line)`` pair; the base
        class filters out ids that have already been posted (see
        :meth:`_post_new_incidents`) so implementations do not need their own
        dedup. ``incident_id`` must be stable across polls for a given incident.

        The default implementation returns nothing, which is appropriate for a
        query-only service. Override to support polling.

        Returns:
            A list of ``(incident_id, line)`` tuples.
        """
        return []

    def get_capabilities(self) -> dict[str, bool]:
        """Declare which query types this service understands.

        Returns:
            A dict mapping ``QUERY_TYPE_*`` values to booleans. Missing keys are
            treated as unsupported. Example::

                {"coordinates": True, "city": True, "zipcode": True}
        """
        return {}

    # ------------------------------------------------------------------ #
    # Capability helpers.
    # ------------------------------------------------------------------ #
    def supports_query_type(self, query_type: str) -> bool:
        """Return True if this service advertises support for ``query_type``."""
        return bool(self.get_capabilities().get(query_type, False))

    # ------------------------------------------------------------------ #
    # Service lifecycle (BaseServicePlugin interface).
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the service, launching the polling loop when configured."""
        if not self.enabled:
            self.logger.info("Alert service '%s' is disabled, not starting", self.alert_service_id)
            return
        self._running = True

        if self.polling_enabled and self.polling_channels:
            self._poll_task = asyncio.create_task(self._poll_loop())
            self.logger.info(
                "Alert service '%s' polling started (interval=%.0fs, channels=%s)",
                self.alert_service_id,
                self.polling_interval_seconds,
                ",".join(self.polling_channels),
            )
        elif self.polling_enabled and not self.polling_channels:
            self.logger.warning(
                "Alert service '%s' has polling_enabled but no polling_channels; polling disabled",
                self.alert_service_id,
            )
        self.logger.info("Alert service '%s' started", self.alert_service_id)

    async def stop(self) -> None:
        """Stop the service and cancel the polling loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        self.logger.info("Alert service '%s' stopped", self.alert_service_id)

    # ------------------------------------------------------------------ #
    # Polling loop.
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        """Background loop: fetch new incidents and post them on an interval."""
        # Seed the dedup set from any persisted ids so a restart does not repost
        # incidents that were live in the previous run.
        self._seen_incident_ids = self._load_seen_ids()

        while self._running:
            try:
                await self._post_new_incidents()
                await asyncio.sleep(self.polling_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:  # keep the loop alive across transient errors
                self.logger.error(
                    "Error in alert service '%s' poll loop: %s", self.alert_service_id, e
                )
                await asyncio.sleep(min(60.0, self.polling_interval_seconds))

    async def _post_new_incidents(self) -> None:
        """Fetch incidents, filter already-seen ids, and post the rest."""
        try:
            incidents = await self.fetch_new_incidents()
        except Exception as e:
            self.logger.error(
                "fetch_new_incidents failed for '%s': %s", self.alert_service_id, e
            )
            return

        new_incidents = [
            (inc_id, line)
            for inc_id, line in incidents
            if inc_id and inc_id not in self._seen_incident_ids
        ]
        if not new_incidents:
            return

        for inc_id, line in new_incidents:
            text = self._prefix_with_label(line)
            for channel in self.polling_channels:
                try:
                    await self.bot.command_manager.send_channel_message(
                        channel, text, scope=self.get_mesh_flood_scope()
                    )
                except Exception as e:
                    self.logger.error(
                        "Failed to post alert from '%s' to %s: %s",
                        self.alert_service_id,
                        channel,
                        e,
                    )
            self._seen_incident_ids.add(inc_id)
            self.logger.info(
                "Alert service '%s' posted incident %s", self.alert_service_id, inc_id
            )

        self._trim_seen_ids()
        self._save_seen_ids()

    # ------------------------------------------------------------------ #
    # Duplicate tracking persistence (via bot_metadata table).
    # ------------------------------------------------------------------ #
    def _metadata_key(self) -> str:
        """Metadata key used to persist seen incident ids for this service."""
        return f"alert_service_seen_ids_{self.alert_service_id}"

    def _load_seen_ids(self) -> set[str]:
        """Load persisted seen incident ids (best effort)."""
        if not getattr(self.bot, "db_manager", None):
            return set()
        try:
            raw = self.bot.db_manager.get_metadata(self._metadata_key())
        except Exception as e:
            self.logger.debug("Could not load seen ids for '%s': %s", self.alert_service_id, e)
            return set()
        if not raw:
            return set()
        return {part for part in raw.split(",") if part}

    def _save_seen_ids(self) -> None:
        """Persist the current seen id set (best effort)."""
        if not getattr(self.bot, "db_manager", None):
            return
        try:
            self.bot.db_manager.set_metadata(
                self._metadata_key(), ",".join(self._seen_incident_ids)
            )
        except Exception as e:
            self.logger.debug("Could not save seen ids for '%s': %s", self.alert_service_id, e)

    def _trim_seen_ids(self) -> None:
        """Bound the dedup set so it cannot grow without limit."""
        if len(self._seen_incident_ids) > SEEN_IDS_MAX:
            self._seen_incident_ids = set(list(self._seen_incident_ids)[-SEEN_IDS_MAX:])

    # ------------------------------------------------------------------ #
    # Formatting helpers.
    # ------------------------------------------------------------------ #
    def _prefix_with_label(self, body: str) -> str:
        """Prefix ``body`` with this service's ``[LABEL]:`` header."""
        return f"[{self.label}]:\n{body}"

    def get_metadata(self) -> dict[str, Any]:
        """Extend base metadata with alert-service specific fields."""
        metadata = super().get_metadata()
        metadata.update(
            {
                "alert_service_id": self.alert_service_id,
                "label": self.label,
                "polling_enabled": self.polling_enabled,
                "polling_channels": list(self.polling_channels),
                "capabilities": self.get_capabilities(),
            }
        )
        return metadata
