#!/usr/bin/env python3
"""
Earthquake Alert Service for MeshCore Bot
Polls USGS Earthquake API and notifies a channel when earthquakes occur in a configured region.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Set

import requests

from .base_service import BaseServicePlugin

# California bounding box defaults (decimal degrees)
DEFAULT_MIN_LAT = 32.5
DEFAULT_MAX_LAT = 42.0
DEFAULT_MIN_LON = -124.5
DEFAULT_MAX_LON = -114.0
USGS_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
SEEN_IDS_MAX = 500
METADATA_KEY_LAST_POSTED_TIME = "earthquake_last_posted_time"


class EarthquakeService(BaseServicePlugin):
    """Service that polls USGS for earthquakes in a region and posts alerts to a channel."""

    config_section = "Earthquake_Service"
    description = "Earthquake alerts for a configured region (USGS API)"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        section = "Earthquake_Service"
        self.channel = self.bot.config.get(section, "channel", fallback="general")
        poll_ms = self.bot.config.getint(section, "poll_interval", fallback=60000)
        self.poll_interval_seconds = poll_ms / 1000.0
        self.time_window_minutes = self.bot.config.getint(
            section, "time_window_minutes", fallback=10
        )
        self.min_magnitude = self.bot.config.getfloat(
            section, "min_magnitude", fallback=3.0
        )
        self.minlatitude = self.bot.config.getfloat(
            section, "minlatitude", fallback=DEFAULT_MIN_LAT
        )
        self.maxlatitude = self.bot.config.getfloat(
            section, "maxlatitude", fallback=DEFAULT_MAX_LAT
        )
        self.minlongitude = self.bot.config.getfloat(
            section, "minlongitude", fallback=DEFAULT_MIN_LON
        )
        self.maxlongitude = self.bot.config.getfloat(
            section, "maxlongitude", fallback=DEFAULT_MAX_LON
        )
        self.send_link = self.bot.config.getboolean(
            section, "send_link", fallback=True
        )

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self.seen_event_ids: Set[str] = set()
        self._last_posted_time_ms: int = self._load_last_posted_time_ms()
        self._session = requests.Session()

        self.logger.info(
            "Earthquake service initialized: channel=%s, region lat %.1f–%.1f lon %.1f–%.1f, M>=%.1f",
            self.channel,
            self.minlatitude,
            self.maxlatitude,
            self.minlongitude,
            self.maxlongitude,
            self.min_magnitude,
        )

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Earthquake service is disabled, not starting")
            return
        self._running = True
        self.logger.info("Starting earthquake service")
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info("Earthquake service started")

    async def stop(self) -> None:
        self._running = False
        self.logger.info("Stopping earthquake service")
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self._session.close()
        self.logger.info("Earthquake service stopped")

    def _load_last_posted_time_ms(self) -> int:
        """Load last posted event time (ms) from bot_metadata to avoid reposts after restart."""
        if not getattr(self.bot, "db_manager", None):
            return 0
        raw = self.bot.db_manager.get_metadata(METADATA_KEY_LAST_POSTED_TIME)
        if not raw:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    async def _poll_loop(self) -> None:
        self.logger.info(
            "Earthquake poll loop started (interval=%.1fs, window=%d min)",
            self.poll_interval_seconds,
            self.time_window_minutes,
        )
        while self._running:
            try:
                await self._check_earthquakes()
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in earthquake poll loop: %s", e)
                await asyncio.sleep(60)

    async def _check_earthquakes(self) -> None:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=self.time_window_minutes)
        params = {
            "format": "geojson",
            "starttime": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "endtime": end_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmagnitude": self.min_magnitude,
            "minlatitude": self.minlatitude,
            "maxlatitude": self.maxlatitude,
            "minlongitude": self.minlongitude,
            "maxlongitude": self.maxlongitude,
            "orderby": "magnitude",
        }

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._session.get(USGS_QUERY_URL, params=params, timeout=10),
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            self.logger.warning("USGS request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("USGS response parse error: %s", e)
            return

        features = data.get("features", [])
        max_posted_time_ms = self._last_posted_time_ms
        for quake in features:
            event_id = quake.get("id")
            props = quake.get("properties", {})
            event_time_ms = props.get("time") or 0
            if not event_id or event_id in self.seen_event_ids:
                continue
            if event_time_ms <= self._last_posted_time_ms:
                continue

            try:
                text = self._format_quake(quake)
                if text:
                    await self.bot.command_manager.send_channel_message(
                        self.channel, text
                    )
                    url_detail = props.get("url", "")
                    if self.send_link and url_detail:
                        await self.bot.command_manager.send_channel_message(
                            self.channel, url_detail
                        )
                    self.logger.info("Earthquake alert sent: %s", event_id)
                self.seen_event_ids.add(event_id)
                if event_time_ms > max_posted_time_ms:
                    max_posted_time_ms = event_time_ms
            except Exception as e:
                self.logger.error("Error sending earthquake alert: %s", e)

        if max_posted_time_ms > self._last_posted_time_ms and getattr(
            self.bot, "db_manager", None
        ):
            self._last_posted_time_ms = max_posted_time_ms
            self.bot.db_manager.set_metadata(
                METADATA_KEY_LAST_POSTED_TIME, str(max_posted_time_ms)
            )

        if len(self.seen_event_ids) > SEEN_IDS_MAX:
            self.seen_event_ids = set(list(self.seen_event_ids)[-SEEN_IDS_MAX:])

    def _format_quake(self, quake: dict) -> str:
        props = quake.get("properties", {})
        geometry = quake.get("geometry", {})
        coords = geometry.get("coordinates", [])

        mag = props.get("mag")
        mag_type = props.get("magType", "")
        place = props.get("place", "Unknown location")
        depth = coords[2] if len(coords) > 2 else None
        lon = coords[0] if len(coords) > 0 else None
        lat = coords[1] if len(coords) > 1 else None

        quake_time_ms = props.get("time")
        if quake_time_ms:
            quake_time = datetime.fromtimestamp(
                quake_time_ms / 1000, tz=timezone.utc
            )
            time_str = quake_time.strftime("%H:%M:%S UTC")
        else:
            time_str = "Unknown"

        parts = ["Earthquake M%.1f" % (mag if mag is not None else 0)]
        if mag_type:
            parts[0] += " %s" % mag_type
        parts.append(place)
        parts.append(time_str)
        if depth is not None:
            parts.append("depth %s km" % (int(depth) if isinstance(depth, (int, float)) else depth))
        if lat is not None and lon is not None:
            parts.append("%.2fN %.2fW" % (lat, abs(lon)))

        # When send_link is true the link is sent in a separate follow-up message
        return " | ".join(str(p) for p in parts)
