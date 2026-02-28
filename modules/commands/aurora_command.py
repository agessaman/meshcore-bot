#!/usr/bin/env python3
"""
Aurora command - NOAA KP index and Ovation aurora probability for a location.
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

import pytz

from ..clients.noaa_aurora_client import NOAAAuroraClient
from ..models import MeshMessage
from ..utils import geocode_city_sync, geocode_zipcode_sync, get_config_timezone
from .base_command import BaseCommand


class AuroraCommand(BaseCommand):
    """Command to get aurora (KP index and probability) for a location."""

    name = "aurora"
    keywords = ["aurora", "kp"]
    description = "Get aurora forecast (KP index and probability) for a location"
    category = "solar"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "Get aurora forecast (KP index and probability) for a location"
    usage = "aurora [city|zipcode|lat,lon]"
    examples = ["aurora", "aurora seattle", "aurora 98101", "aurora 48.08,-121.97"]
    parameters = [
        {"name": "location", "description": "Optional: city, US ZIP, or lat,lon. Default: config or companion location."}
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.aurora_enabled = self.get_config_value("Aurora_Command", "enabled", fallback=True, value_type="bool")
        self.default_state = self.bot.config.get("Weather", "default_state", fallback="")
        self.default_country = self.bot.config.get("Weather", "default_country", fallback="US")
        self.url_timeout = 10

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.aurora_enabled:
            return False
        return super().can_execute(message)

    def _get_companion_location(self, message: MeshMessage) -> Optional[Tuple[float, float]]:
        """Get companion/sender location from database."""
        try:
            sender_pubkey = getattr(message, "sender_pubkey", None)
            if not sender_pubkey:
                return None
            query = """
                SELECT latitude, longitude
                FROM complete_contact_tracking
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            """
            results = self.bot.db_manager.execute_query(query, (sender_pubkey,))
            if results:
                row = results[0]
                return (float(row["latitude"]), float(row["longitude"]))
            return None
        except Exception as e:
            self.logger.debug(f"Error getting companion location: {e}")
            return None

    def _get_bot_location(self) -> Optional[Tuple[float, float]]:
        """Get bot location from config ([Bot] bot_latitude, bot_longitude)."""
        try:
            lat = self.bot.config.getfloat("Bot", "bot_latitude", fallback=None)
            lon = self.bot.config.getfloat("Bot", "bot_longitude", fallback=None)
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
            return None
        except Exception as e:
            self.logger.debug(f"Error getting bot location: {e}")
            return None

    # 8-level block meter for visibility chance (▁ low → █ high), not stoplight
    _PROB_INDICATORS = "▁▂▃▄▅▆▇█"

    def _prob_indicator(self, prob_pct: int) -> str:
        """Return a one-char bar (▁→█) for probability 0–100%."""
        idx = min(7, max(0, int(prob_pct / 12.5))) if prob_pct else 0
        return self._PROB_INDICATORS[idx]

    def _format_kp_time(self, ts: str) -> str:
        """Format NOAA Kp time_tag (UTC) to compact form in local time or Zulu.

        Supports 1m product ISO format (e.g. "2026-01-21T05:13:00") and legacy
        space-separated formats. Uses [Bot] timezone when set; if [Solar_Config]
        use_zulu_time is true or no Bot timezone, shows UTC with Z. Otherwise local.
        """
        if not ts or not ts.strip():
            return "—"
        s = ts.strip()
        dt_utc = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                dt_utc = dt.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if dt_utc is None and ("T" in s or "Z" in s):
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if dt_utc is None:
            return "—"
        use_zulu = self.get_config_value("Solar_Config", "use_zulu_time", fallback=False, value_type="bool")
        tz, iana_str = get_config_timezone(self.bot.config, self.logger)
        if use_zulu or iana_str == "UTC":
            return dt_utc.strftime("%b %d ") + f"{dt_utc.hour:02d}Z"
        local = dt_utc.astimezone(tz)
        return local.strftime("%b %d %I:%M%p")

    def _resolve_location(
        self, message: MeshMessage, location: Optional[str]
    ) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
        """
        Resolve to (lat, lon, location_label, error_key).
        location_label is for display; error_key is a translation key if resolution failed.
        """
        # 1. No user input: companion, then [Aurora_Command] default, then bot location
        if not location or not location.strip():
            co = self._get_companion_location(message)
            if co:
                return (co[0], co[1], f"{co[0]:.1f},{co[1]:.1f}", None)
            default_lat = default_lon = None
            if self.bot.config.has_section("Aurora_Command"):
                default_lat = self.bot.config.getfloat("Aurora_Command", "default_lat", fallback=None)
                default_lon = self.bot.config.getfloat("Aurora_Command", "default_lon", fallback=None)
            if default_lat is not None and default_lon is not None:
                if -90 <= default_lat <= 90 and -180 <= default_lon <= 180:
                    label = f"{default_lat:.1f},{default_lon:.1f}"
                    return (default_lat, default_lon, label, None)
            bot_loc = self._get_bot_location()
            if bot_loc:
                return (bot_loc[0], bot_loc[1], f"{bot_loc[0]:.1f},{bot_loc[1]:.1f}", None)
            return (None, None, None, "commands.aurora.no_location")

        loc = location.strip()

        # 2. Coordinates
        if re.match(r"^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$", loc):
            try:
                a, b = loc.split(",", 1)
                lat, lon = float(a.strip()), float(b.strip())
                if not (-90 <= lat <= 90):
                    return (None, None, None, "commands.aurora.error")  # pass error via translate with error=...
                if not (-180 <= lon <= 180):
                    return (None, None, None, "commands.aurora.error")
                return (lat, lon, loc, None)
            except ValueError:
                return (None, None, None, "commands.aurora.error")

        # 3. ZIP (5 digits)
        if re.match(r"^\s*\d{5}\s*$", loc):
            lat, lon = geocode_zipcode_sync(
                self.bot, loc, default_country=self.default_country, timeout=self.url_timeout
            )
            if lat is None or lon is None:
                return (None, None, None, "commands.aurora.no_location_zipcode")
            return (lat, lon, loc, None)

        # 4. City
        lat, lon, _ = geocode_city_sync(
            self.bot,
            loc,
            default_state=self.default_state,
            default_country=self.default_country,
            include_address_info=False,
            timeout=self.url_timeout,
        )
        if lat is None or lon is None:
            region = self.default_state or self.default_country
            return (None, None, None, "commands.aurora.no_location_city")  # needs location, state
        return (lat, lon, loc, None)

    async def execute(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if content.startswith("!"):
            content = content[1:].strip()
        parts = content.split()
        location: Optional[str] = None
        if len(parts) >= 2:
            location = " ".join(parts[1:]).strip()

        lat, lon, location_label, err_key = self._resolve_location(message, location)
        if lat is None or lon is None:
            region = self.default_state or self.default_country
            if err_key == "commands.aurora.no_location":
                await self.send_response(message, self.translate("commands.aurora.no_location"))
            elif err_key == "commands.aurora.no_location_zipcode":
                await self.send_response(
                    message, self.translate("commands.aurora.no_location_zipcode", location=location or "")
                )
            elif err_key == "commands.aurora.no_location_city":
                await self.send_response(
                    message,
                    self.translate("commands.aurora.no_location_city", location=location or "", state=region),
                )
            else:
                await self.send_response(
                    message, self.translate("commands.aurora.error", error="Invalid location or coordinates")
                )
            return True

        try:
            self.record_execution(message.sender_id)
            loop = asyncio.get_event_loop()
            client = NOAAAuroraClient(latitude=lat, longitude=lon)
            data = await loop.run_in_executor(None, lambda: client.get_aurora_data())
        except Exception as e:
            self.logger.error(f"Error fetching aurora data: {e}")
            await self.send_response(
                message, self.translate("commands.aurora.error_fetching")
            )
            return True

        # KP -> status (short labels for one-line response)
        kp = data.kp_index
        if kp >= 7:
            status = self.translate("commands.aurora.status.g3_severe")
        elif kp >= 5:
            status = self.translate("commands.aurora.status.g1_g2")
        elif kp >= 4:
            status = self.translate("commands.aurora.status.unsettled")
        else:
            status = self.translate("commands.aurora.status.quiet")

        kp_time_str = self._format_kp_time(data.kp_timestamp)
        prob_pct = int(round(data.aurora_probability))
        prob_indicator = self._prob_indicator(prob_pct)
        response = self.translate(
            "commands.aurora.response",
            kp=f"{data.kp_index:.1f}",
            kp_time=kp_time_str,
            prob=prob_pct,
            prob_indicator=prob_indicator,
            location=location_label or f"{lat:.1f},{lon:.1f}",
            status=status,
        )
        max_len = self.get_max_message_length(message)
        if len(response) > max_len:
            response = response[: max_len - 3] + "..."

        await self.send_response(message, response)
        return True
