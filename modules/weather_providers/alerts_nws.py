#!/usr/bin/env python3
"""NWS alerts (api.weather.gov/alerts): the US alerts provider behind wx.

Not to be confused with ``modules/nws_alerts.py``, the Atom parsing helpers this
provider uses. The fetch, the prioritizing and the formatting live here; the
command keeps the request and the sending.
"""

from __future__ import annotations

import asyncio
import re
import threading
import xml.dom.minidom
from typing import Optional

import requests

from .. import alert_format
from ..commands.rain_command import nws_http_means_no_coverage
from ..http_retry import make_retry_session
from ..models import MeshMessage
from ..nws_alerts import WX_SPECIAL_RULES, entry_nws_headline, entry_summary, entry_title, parse_alert_fields
from ..nws_coverage import NWSNoCoverageCache
from .base import AlertsProvider, AlertSummary, ProviderHost
from .noaa import ERROR_FETCHING_DATA, NO_ALERTS, NOAA_TIMEOUT


class NwsAlertsProvider(AlertsProvider):
    """Active NWS alerts for US points, prioritized and packed into mesh replies."""

    name = "nws"
    translation_ns = "commands.wx"

    def __init__(
        self,
        host: ProviderHost,
        session: Optional[requests.Session] = None,
        lock: Optional[threading.Lock] = None,
        coverage: Optional[NWSNoCoverageCache] = None,
        fallback_enabled: bool = False,
        no_coverage_key: str = 'commands.wx.source_option_not_available',
    ):
        """Bind the provider to its host and the command's shared NOAA state.

        *session*, *lock* and *coverage* are the same objects the forecast
        provider holds, so one requests.Session, one worker lock and one
        coverage cache serve both. *fallback_enabled* says the command can
        answer points outside NWS coverage from another provider, which is what
        turns a failed fetch into "not available here" rather than an error.
        *no_coverage_key* is the catalog key that reply uses, so a command whose
        forecast provider speaks another catalog says so in its own words.
        """
        super().__init__(host)
        self.session = session if session is not None else make_retry_session()
        self.lock = lock if lock is not None else threading.Lock()
        self.coverage = coverage if coverage is not None else NWSNoCoverageCache()
        self.fallback_enabled = fallback_enabled
        self.no_coverage_key = no_coverage_key
        self.url_timeout = NOAA_TIMEOUT

    def covers(self, lat: float, lon: float) -> bool:
        """Whether NWS is not known to lack coverage for this point."""
        return not self.coverage.is_unavailable(lat, lon)

    def summary(self, lat: float, lon: float, max_length: int) -> Optional[AlertSummary]:
        """The compact alert text and count for the second message, or None.

        The fetch shares the command's requests.Session with the forecast
        provider, so it takes the same worker lock. The lock is re-entrant, so
        a caller that already holds it for the forecast is not deadlocked.
        """
        return self._run_sync(self._summary_locked, lat, lon, max_length)

    def _summary_locked(self, lat: float, lon: float, max_length: int) -> Optional[AlertSummary]:
        """The body of :meth:`summary`, run under the worker lock."""
        alerts_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=False)
        if alerts_result == ERROR_FETCHING_DATA or alerts_result == NO_ALERTS:
            return None
        full_alert_text, _abbreviated_alert_text, alert_count = alerts_result
        if alert_count <= 0:
            return None
        # Get full alert data for prioritized formatting
        alerts_full_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=True)
        if alerts_full_result not in [ERROR_FETCHING_DATA, NO_ALERTS]:
            alerts_list, _ = alerts_full_result
            text = self._format_alerts_compact_summary(alerts_list, alert_count, max_length=max_length)
        else:
            # Fallback to the first alert's own text
            text = full_alert_text
        return AlertSummary(text=text, count=alert_count)

    def _run_sync(self, operation, *args, **kwargs):
        """Run one shared synchronous provider operation under its worker lock."""
        with self.lock:
            return operation(*args, **kwargs)

    async def _get_weather_alerts_noaa_async(
        self, lat: float, lon: float, return_full_data: bool = False
    ) -> tuple:
        return await asyncio.to_thread(
            self._run_sync, self.get_weather_alerts_noaa, lat, lon, return_full_data
        )

    def get_weather_alerts_noaa(self, lat: float, lon: float, return_full_data: bool = False) -> tuple:
        """Get weather alerts from NOAA with full metadata extraction and prioritization

        Args:
            lat: Latitude
            lon: Longitude
            return_full_data: If True, return list of alert dicts instead of formatted strings

        Returns:
            If return_full_data=False: (full_first_alert_text, abbreviated_first_alert_text, alert_count)
            If return_full_data=True: (list of alert dicts, alert_count)
        """
        try:
            if self.coverage.is_unavailable(lat, lon):
                self.host.logger.debug("Skipping NWS weather alerts for cached point %s,%s outside NWS coverage", round(lat, 2), round(lon, 2))
                return ERROR_FETCHING_DATA

            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            alert_url = f"https://api.weather.gov/alerts/active.atom?point={lat_rounded},{lon_rounded}"

            try:
                alert_data = self.session.get(alert_url, timeout=self.url_timeout)
                if not alert_data.ok:
                    if nws_http_means_no_coverage(alert_data.status_code):
                        if self.coverage.mark_unavailable(lat, lon):
                            self.host.logger.warning(
                                "NWS weather alerts unavailable (HTTP %s); NOAA alerts are US-only; "
                                "point %s,%s is outside NWS coverage",
                                alert_data.status_code, round(lat, 2), round(lon, 2),
                            )
                    else:
                        self.host.logger.warning(
                            f"Error fetching weather alerts from NOAA: HTTP {alert_data.status_code}"
                        )
                    return ERROR_FETCHING_DATA
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.host.logger.warning(f"Timeout/connection error fetching weather alerts from NOAA: {e}")
                return ERROR_FETCHING_DATA

            self.coverage.mark_available(lat, lon)

            alerts = []  # Store structured alert data
            alertxml = xml.dom.minidom.parseString(alert_data.text)

            for entry in alertxml.getElementsByTagName("entry"):
                try:
                    title = entry_title(entry)
                    summary = entry_summary(entry)
                    nws_headline = entry_nws_headline(entry)
                    alerts.append(parse_alert_fields(entry, title, summary, nws_headline, WX_SPECIAL_RULES))

                except Exception as e:
                    self.host.logger.warning(f"Error parsing alert entry: {e}")
                    # Fallback: just use title
                    if title:
                        alerts.append({
                            'title': title,
                            'summary': '',
                            'nws_headline': '',
                            'event': title.split()[0] if title else "",
                            'event_type': 'Unknown',
                            'severity': 'Unknown',
                            'urgency': 'Unknown',
                            'certainty': 'Unknown',
                            'effective': '',
                            'expires': '',
                            'area_desc': '',
                            'office': ''
                        })

            if not alerts:
                return NO_ALERTS

            # Post-process alerts to differentiate duplicate Special Statements
            # If multiple statements have the same event, add distinguishing details
            alerts = self._differentiate_duplicate_statements(alerts)

            # Prioritize alerts using hybrid scoring
            alerts = self._prioritize_alerts(alerts)

            if return_full_data:
                return alerts, len(alerts)

            # Format for compact display (backward compatibility)
            # Return first alert formatted, plus count
            first_alert = alerts[0]
            full_first_alert_text = self._format_alert_compact(first_alert, include_details=True)
            abbreviated_first_alert_text = self._format_alert_compact(first_alert, include_details=False)

            return full_first_alert_text, abbreviated_first_alert_text, len(alerts)

        except Exception as e:
            self.host.logger.error(f"Error fetching NOAA weather alerts: {e}")
            return ERROR_FETCHING_DATA

    def _differentiate_duplicate_statements(self, alerts: list) -> list:
        """Differentiate Special Statements that have the same event type by adding unique details

        Args:
            alerts: List of alert dicts

        Returns:
            List of alerts with differentiated event names for duplicate statements
        """
        # Group alerts by event type and event name
        statement_groups = {}
        for alert in alerts:
            if alert.get('event_type') == 'Statement':
                event = alert.get('event', 'Special')
                if event not in statement_groups:
                    statement_groups[event] = []
                statement_groups[event].append(alert)

        # For each group with multiple statements, differentiate them
        for event, group in statement_groups.items():
            if len(group) > 1:
                # Multiple statements with same event - need to differentiate
                for i, alert in enumerate(group):
                    nws_headline = alert.get('nws_headline', '')
                    summary = alert.get('summary', '')
                    effective = alert.get('effective', '')

                    # Try to extract unique distinguishing details
                    distinguishing_detail = ""

                    # Strategy 1: Extract unique keywords from headline that aren't in other headlines
                    if nws_headline:
                        headline_lower = nws_headline.lower()
                        # Look for unique time references
                        if 'today' in headline_lower or 'now' in headline_lower:
                            distinguishing_detail = " (Today)"
                        elif 'week' in headline_lower or 'past week' in headline_lower:
                            distinguishing_detail = " (Week)"
                        elif 'continues' in headline_lower or 'remains' in headline_lower:
                            distinguishing_detail = " (Ongoing)"

                        # Look for unique severity/impact words
                        if not distinguishing_detail:
                            if 'increased' in headline_lower or 'increasing' in headline_lower:
                                distinguishing_detail = " (Increased)"
                            elif 'new' in headline_lower:
                                distinguishing_detail = " (New)"
                            elif 'update' in headline_lower:
                                distinguishing_detail = " (Update)"

                    # Strategy 2: Use timing to differentiate (morning vs afternoon vs evening)
                    if not distinguishing_detail and effective:
                        try:
                            from datetime import datetime
                            # Try to parse effective time
                            if 'T' in effective:
                                dt = datetime.fromisoformat(effective.replace('Z', '+00:00'))
                                hour = dt.hour
                                if 5 <= hour < 12:
                                    distinguishing_detail = " (AM)"
                                elif 12 <= hour < 17:
                                    distinguishing_detail = " (PM)"
                                elif 17 <= hour < 21:
                                    distinguishing_detail = " (Eve)"
                                else:
                                    distinguishing_detail = " (Night)"
                        except:
                            pass

                    # Strategy 3: Extract unique topic from summary if headline didn't help
                    if not distinguishing_detail and summary:
                        summary_lower = summary.lower()
                        # Look for secondary topics that might be unique
                        # Check for specific locations, conditions, or impacts
                        if 'burn' in summary_lower or 'burned area' in summary_lower:
                            distinguishing_detail = " (Burn)"
                        elif 'coastal' in summary_lower:
                            distinguishing_detail = " (Coastal)"
                        elif 'urban' in summary_lower:
                            distinguishing_detail = " (Urban)"
                        elif 'mountain' in summary_lower or 'cascade' in summary_lower:
                            distinguishing_detail = " (Mtn)"

                    # Strategy 4: Use index as last resort (but make it subtle)
                    if not distinguishing_detail:
                        distinguishing_detail = f" ({i+1})"

                    # Update the event name with distinguishing detail
                    alert['event'] = event + distinguishing_detail

        return alerts

    def _prioritize_alerts(self, alerts: list) -> list:
        """Prioritize alerts using hybrid scoring system

        Scoring:
        - Severity: Extreme=100, Severe=75, Moderate=50, Minor=25, Unknown=0
        - Urgency: Immediate=50, Expected=30, Future=10, Past=0
        - Event Type: Warning=40, Watch=30, Advisory=20, Statement=10
        - Time: (hours until expiration) * -5 (sooner = higher score)

        Returns sorted list (highest priority first)
        """
        def calculate_score(alert):
            score = 0

            # Severity score
            severity_scores = {
                'Extreme': 100,
                'Severe': 75,
                'Moderate': 50,
                'Minor': 25,
                'Unknown': 0
            }
            score += severity_scores.get(alert.get('severity', 'Unknown'), 0)

            # Urgency score
            urgency_scores = {
                'Immediate': 50,
                'Expected': 30,
                'Future': 10,
                'Past': 0,
                'Unknown': 0
            }
            score += urgency_scores.get(alert.get('urgency', 'Unknown'), 0)

            # Event type score
            event_type_scores = {
                'Warning': 40,
                'Watch': 30,
                'Advisory': 20,
                'Statement': 10,
                'Unknown': 0
            }
            score += event_type_scores.get(alert.get('event_type', 'Unknown'), 0)

            # Time urgency (estimate hours until expiration)
            expires = alert.get('expires', '')
            expires_hours = 999  # Default to far future
            if expires:
                try:
                    # Try to parse expiration time
                    if 'at' in expires.lower():
                        # Rough estimate: if it says "6:00AM" assume it's today or tomorrow
                        time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', expires, re.IGNORECASE)
                        if time_match:
                            # For simplicity, assume alerts expire within 48 hours
                            expires_hours = 24  # Default estimate
                except:
                    pass

            # Time score: sooner expiration = higher priority
            # Subtract hours (sooner = higher score)
            score += max(0, 50 - expires_hours)

            return score

        # Sort by score (descending), then by event type, then by title
        sorted_alerts = sorted(alerts, key=lambda a: (
            -calculate_score(a),  # Negative for descending
            {'Warning': 0, 'Watch': 1, 'Advisory': 2, 'Statement': 3, 'Unknown': 4}.get(a.get('event_type', 'Unknown'), 4),
            a.get('title', '')
        ))

        return sorted_alerts

    def _format_alert_compact(self, alert: dict, include_details: bool = True) -> str:
        """Format a single alert compactly

        Shares its formatting with the proactive WeatherService broadcasts via
        ``modules.alert_format``, so both localize from one code path.

        Args:
            alert: Alert dict with event, event_type, severity, expires, office, etc.
            include_details: If True, include expiration time and office

        Returns:
            Formatted alert string, e.g. "🟠High Wind Warn til 6AM by NWS SEA"
        """
        return alert_format.format_alert_compact(
            alert,
            self.host.response_translator,
            include_details=include_details,
            # !wx alerts has never shown the area description; the proactive
            # broadcast does, so the shared helper keeps it optional.
            include_location=False,
        )

    def _format_alerts_compact_summary(self, alerts: list, alert_count: int, max_length: int = 130) -> str:
        """Format multiple alerts with prioritized first alert and summary of others

        Args:
            alerts: List of prioritized alert dicts
            alert_count: Total number of alerts
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Compact formatted string: "4 alerts: 🟠High Wind Warn til 6AM | +3: 🌊Flood Watch, ❄️Freeze Adv, 🌫️Dense Fog Adv"
        """
        if not alerts:
            return f"{alert_count} alerts"

        # Format first (highest priority) alert with details
        first_alert = alerts[0]
        first_alert_text = self._format_alert_compact(first_alert, include_details=True)

        # If only one alert, return it
        if alert_count == 1:
            return f"{alert_count} alert: {first_alert_text}"

        # Build summary of remaining alerts
        remaining_alerts = alerts[1:]
        remaining_count = len(remaining_alerts)

        # Format remaining alerts as event types only
        remaining_parts = []
        for alert in remaining_alerts[:5]:  # Limit to 5 to avoid overflow
            event = alert.get('event', '')
            event_type = alert.get('event_type', '')

            # Get emoji for event type
            event_emoji = self._get_event_emoji(event, event_type)

            # Build compact event string. Trimmed harder than the lead alert:
            # first word only, since these are a comma-joined tail.
            event_short = alert_format.shorten_event(event, limit=12, max_words=1)
            event_type_abbrev = alert_format.event_type_abbrev(event_type, self.host.response_translator)
            if event:
                remaining_parts.append(f"{event_emoji}{event_short} {event_type_abbrev}")
            else:
                remaining_parts.append(f"{event_emoji}{event_type_abbrev}")

        # Build summary
        if remaining_count > 5:
            remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:5])}..."
        else:
            remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts)}"

        # Combine: first alert + summary
        result = f"{alert_count} alerts: {first_alert_text} | {remaining_summary}"

        # Check if it fits in max_length chars, truncate if needed
        if self.host._count_display_width(result) > max_length:
            # Try shorter first alert
            first_alert_text_short = self._format_alert_compact(first_alert, include_details=False)
            result = f"{alert_count} alerts: {first_alert_text_short} | {remaining_summary}"

            # If still too long, truncate remaining summary
            if self.host._count_display_width(result) > max_length:
                max_remaining = 3
                while max_remaining > 0 and self.host._count_display_width(result) > max_length:
                    if remaining_count > max_remaining:
                        remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:max_remaining])}..."
                    else:
                        remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:max_remaining])}"
                    result = f"{alert_count} alerts: {first_alert_text_short} | {remaining_summary}"
                    max_remaining -= 1

        return result

    def _get_event_emoji(self, event: str, event_type: str) -> str:
        """Get emoji for event type"""
        event_lower = event.lower() if event else ""

        # Weather event emojis
        if any(word in event_lower for word in ['flood', 'flooding']):
            return '🌊'
        elif any(word in event_lower for word in ['wind', 'gale']):
            return '💨'
        elif any(word in event_lower for word in ['snow', 'winter', 'blizzard']):
            return '❄️'
        elif any(word in event_lower for word in ['fog', 'smoke', 'haze']):
            return '🌫️'
        elif any(word in event_lower for word in ['heat', 'excessive heat']):
            return '🌡️'
        elif any(word in event_lower for word in ['freeze', 'frost']):
            return '🧊'
        elif any(word in event_lower for word in ['thunderstorm', 'tornado']):
            return '⛈️'
        elif any(word in event_lower for word in ['fire', 'red flag']):
            return '🔥'
        elif any(word in event_lower for word in ['hurricane', 'tropical']):
            return '🌀'
        elif any(word in event_lower for word in ['tsunami']):
            return '🌊'
        else:
            # Default by event type
            return {
                'Warning': '⚠️',
                'Watch': '👁️',
                'Advisory': 'ℹ️',
                'Statement': '📢'
            }.get(event_type, '⚠️')

    def _format_alert_full(self, alert: dict, index: int = None) -> str:
        """Format a single alert with full details for multi-message display

        Args:
            alert: Alert dict
            index: Optional alert number (1-based)

        Returns:
            Formatted alert string with start/stop times
        """
        translator = self.host.response_translator
        parts = []

        if index is not None:
            parts.append(f"{index}.")

        # No trimming here — this form is sent across as many messages as it needs.
        parts.append(
            alert_format.severity_emoji(alert.get('severity', 'Unknown'))
            + alert_format.format_event_label(
                alert.get('event', ''), alert.get('event_type', ''), translator, limit=None
            )
        )

        window = alert_format.format_alert_window(alert, translator)
        if window:
            parts.append(window)

        # The full form is not length-capped, so it keeps the longer fallback.
        office = alert_format.format_office(alert.get('office', ''), translator, limit=15)
        if office:
            parts.append(office)

        return " ".join(parts)

    async def send_full_list(self, message: MeshMessage, lat: float, lon: float) -> bool:
        """Send full list of alerts with details, splitting across multiple messages if needed.

        Returns whether every message was sent; a refused first message ends the list.
        """
        # Get full alert data
        alerts_result = await self._get_weather_alerts_noaa_async(
            lat, lon, return_full_data=True
        )
        if alerts_result == ERROR_FETCHING_DATA:
            if self.fallback_enabled and self.coverage.is_unavailable(lat, lon):
                # NWS alerts are not the only source here, so a point outside
                # coverage reads as "not available" from the first request on.
                return bool(await self.host.send_response(message, self.host.translate(self.no_coverage_key)))
            return bool(await self.host.send_response(message, self.host.translate('commands.wx.error_fetching')))
        elif alerts_result == NO_ALERTS:
            return bool(await self.host.send_response(message, "No weather alerts"))

        alerts, alert_count = alerts_result

        if not alerts:
            return bool(await self.host.send_response(message, "No weather alerts"))

        # Format each alert with full details
        alert_lines = []
        for i, alert in enumerate(alerts, 1):
            alert_line = self._format_alert_full(alert, index=i)
            alert_lines.append(alert_line)

        # Send alerts, splitting into multiple messages if needed
        rate_limit = self.host.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
        sleep_time = max(rate_limit + 1.0, 2.0)

        # Get max message length dynamically
        max_length = self.host.get_max_message_length(message)

        # Group alerts into messages that fit within max_length chars
        current_message = f"{alert_count} alerts:"
        messages = []

        for line in alert_lines:
            # Check if adding this line would exceed limit
            test_message = current_message + "\n" + line if current_message else line
            if self.host._count_display_width(test_message) > max_length:
                # Current message is full, start new one
                if current_message:
                    messages.append(current_message)
                current_message = line
            else:
                # Add to current message
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line

        # Add last message
        if current_message:
            messages.append(current_message)

        # Send all messages (per-user rate limit applies only to first; skip for continuations)
        for i, msg in enumerate(messages):
            if not await self.host.send_response(message, msg, skip_user_rate_limit=(i > 0)):
                return False
            if i < len(messages) - 1:
                await self.host._pace_reply(message, sleep_time)
        return True

    def abbreviate_alert_title(self, title: str) -> str:
        """Abbreviate alert title for brevity"""
        # Common alert type abbreviations
        replacements = {
            "warning": "Warn",
            "watch": "Watch",
            "advisory": "Adv",
            "statement": "Stmt",
            "severe thunderstorm": "SvrT-Storm",
            "tornado": "Tornado",
            "flash flood": "FlashFlood",
            "flood": "Flood",
            "winter storm": "WinterStorm",
            "blizzard": "Blizzard",
            "ice storm": "IceStorm",
            "freeze": "Freeze",
            "frost": "Frost",
            "heat": "Heat",
            "excessive heat": "ExHeat",
            "extreme heat": "ExtHeat",
            "wind": "Wind",
            "high wind": "HighWind",
            "wind advisory": "WindAdv",
            "fire weather": "FireWx",
            "red flag": "RedFlag",
            "dense fog": "DenseFog",
            "issued": "iss",
            "until": "til",
            "effective": "eff",
            "expires": "exp",
            "dense smoke": "DenseSmoke",
            "air quality": "AirQuality",
            "coastal flood": "CoastalFlood",
            "lakeshore flood": "LakeshoreFlood",
            "rip current": "RipCurrent",
            "high surf": "HighSurf",
            "hurricane": "Hurricane",
            "tropical storm": "TropStorm",
            "tropical depression": "TropDep",
            "storm surge": "StormSurge",
            "tsunami": "Tsunami",
            "earthquake": "Earthquake",
            "volcano": "Volcano",
            "avalanche": "Avalanche",
            "landslide": "Landslide",
            "debris flow": "DebrisFlow",
            "dust storm": "DustStorm",
            "sandstorm": "Sandstorm",
            "blowing dust": "BlwDust",
            "blowing sand": "BlwSand"
        }

        result = title
        for key, value in replacements.items():
            # Case insensitive replace
            result = result.replace(key, value).replace(key.capitalize(), value).replace(key.upper(), value)

        # Limit to reasonable length
        if len(result) > 30:
            result = result[:27] + "..."

        return result

