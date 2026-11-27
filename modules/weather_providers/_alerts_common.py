"""The summary and packed full list the alert providers share.

Meteoalarm and Environment Canada answer with the same shapes: a compact
summary of one line per alert joined with ``"; "`` and a ``+N`` when they do not
fit, and a full list of one line per alert packed into as few messages as the
frame allows, paced the way NWS paces its own list.

A provider normalizes its alerts into dicts with ``level`` (1 to 4), ``event``,
``onset``, ``expires`` and ``area``, implements :meth:`_alerts_for` and
:meth:`_warn_once`, and sets ``fetch_error_key``. Everything else is here.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Optional

from .. import alert_format
from .base import AlertSummary

# Level 1 is a statement or an advisory with no colour of its own. Level 5 is
# DWD's violet, the top of its own four-step scale.
LEVEL_EMOJI = {1: "\u2139\ufe0f", 2: "\U0001f7e1", 3: "\U0001f7e0", 4: "\U0001f534",
               5: "\U0001f7e3"}


class PackedAlertsMixin:
    """The compact summary and the packed full list, over normalized alerts."""

    fetch_error_key: ClassVar[str] = "commands.wx.error_fetching"

    def _alerts_for(self, lat: float, lon: float) -> Optional[list]:
        """The active alerts for a point, most severe first; None when unavailable."""
        raise NotImplementedError

    def _warn_once(self, streak: str, message: str) -> None:
        """Log one WARNING per failure streak, not one per request."""
        raise NotImplementedError

    def _alert_area(self, alert: dict) -> str:
        """The area label for the full list, when the alert carries one."""
        return alert.get("area") or ""

    # --- the compact summary ----------------------------------------------

    def _summary_line(self, alert: dict) -> str:
        """One alert for the compact summary, in the same style as the full list."""
        emoji = LEVEL_EMOJI.get(alert["level"], "")
        until = alert_format.format_alert_window(
            {"expires": alert["expires"].isoformat() if alert["expires"] else ""},
            self.host.response_translator,
        )
        return f"{emoji} {alert['event']} {until}".strip()

    def summary(self, lat: float, lon: float, max_length: int) -> Optional[AlertSummary]:
        """The compact alert text and count for the second message, or None."""
        try:
            return self._summary_for(lat, lon, max_length)
        except Exception as error:
            self._warn_once("summary", f"{self.name} summary failed: {type(error).__name__}")
            return None

    def _summary_for(self, lat: float, lon: float, max_length: int) -> Optional[AlertSummary]:
        """The body of :meth:`summary`."""
        alerts = self._alerts_for(lat, lon)
        if not alerts:
            return None

        lines = [self._summary_line(alert) for alert in alerts]
        text = ""
        shown = 0
        for line in lines:
            candidate = f"{text}; {line}" if text else line
            remaining = len(lines) - shown - 1
            suffix = f" +{remaining}" if remaining > 0 else ""
            if self.host._count_display_width(candidate + suffix) > max_length:
                break
            text = candidate
            shown += 1

        if shown == 0:
            # The first line alone is too long: trim it and re-check the suffix.
            return AlertSummary(
                text=self._trim(lines[0], max_length, len(lines) - 1), count=len(alerts)
            )
        remaining = len(lines) - shown
        if remaining > 0:
            text = f"{text} +{remaining}"
        return AlertSummary(text=text, count=len(alerts))

    def _trim(self, line: str, max_length: int, remaining: int) -> str:
        """Cut a line to the budget with an ellipsis, leaving room for " +N"."""
        suffix = f" +{remaining}" if remaining > 0 else ""
        budget = max_length - self.host._count_display_width(suffix)
        trimmed = line
        while trimmed and self.host._count_display_width(trimmed + "\u2026") > budget:
            trimmed = trimmed[:-1]
        return f"{trimmed}\u2026{suffix}" if trimmed else suffix.strip()

    # --- the full list ----------------------------------------------------

    def _full_alert_text(self, alert: dict, index: int, max_length: int) -> str:
        """One alert as a single line, in the shape NWS's full list uses.

        ``{index}. {emoji}{event} {window} {area}``: the window comes from the
        shared alert formatter, and the area is added only when the line fits.
        """
        emoji = LEVEL_EMOJI.get(alert["level"], "")
        window = alert_format.format_alert_window(
            {
                "effective": alert["onset"].isoformat() if alert["onset"] else "",
                "expires": alert["expires"].isoformat() if alert["expires"] else "",
            },
            self.host.response_translator,
        )
        parts = [f"{index}.", f"{emoji} {alert['event']}".strip()]
        if window:
            parts.append(window)
        line = " ".join(parts)
        area = self._alert_area(alert)
        if area:
            candidate = f"{line} {area}"
            if self.host._count_display_width(candidate) <= max_length:
                return candidate
        return line

    def _fit(self, text: str, max_length: int) -> str:
        """Cut a message to the frame budget, by display width."""
        if self.host._count_display_width(text) <= max_length:
            return text
        trimmed = text
        while trimmed and self.host._count_display_width(trimmed) > max_length:
            trimmed = trimmed[:-1]
        return trimmed

    async def send_full_list(self, message: Any, lat: float, lon: float) -> bool:
        """Send the full alert list, packed and paced exactly as the NWS list is."""
        alerts = await asyncio.to_thread(self._alerts_for, lat, lon)
        if alerts is None:
            return bool(await self.host.send_response(
                message, self.host.translate(self.fetch_error_key)
            ))
        if not alerts:
            return bool(await self.host.send_response(message, "No weather alerts"))

        max_length = self.host.get_max_message_length(message)
        rate_limit = self.host.bot.config.getfloat("Bot", "bot_tx_rate_limit_seconds", fallback=1.0)
        sleep_time = max(rate_limit + 1.0, 2.0)

        # A count header, then whole alert lines: a new message starts when the
        # next line would not fit, as NWS's list does.
        current_message = f"{len(alerts)} alerts:"
        messages = []
        for index, alert in enumerate(alerts, 1):
            line = self._fit(self._full_alert_text(alert, index, max_length), max_length)
            test_message = f"{current_message}\n{line}" if current_message else line
            if self.host._count_display_width(test_message) > max_length:
                if current_message:
                    messages.append(current_message)
                current_message = line
            else:
                current_message = test_message
        if current_message:
            messages.append(current_message)

        for index, text in enumerate(messages):
            if not await self.host.send_response(message, text, skip_user_rate_limit=(index > 0)):
                return False
            if index < len(messages) - 1:
                await self.host._pace_reply(message, sleep_time)
        return True
