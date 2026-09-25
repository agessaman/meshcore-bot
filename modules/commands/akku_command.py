#!/usr/bin/env python3
"""
Akku command for the MeshCore Bot
Shows the latest battery readings collected by the RepeaterTelemetry service.
"""

import time

from ..models import MeshMessage
from .base_command import BaseCommand


def _age(seconds: float) -> str:
    """Compact age string: 5m, 3h, 2d."""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class AkkuCommand(BaseCommand):
    """Reports repeater battery voltages from the RepeaterTelemetry service.

    ``akku``          -> one line per monitored repeater
    ``akku <name>``   -> details for one repeater
    ``akku jetzt``    -> trigger an immediate poll (add to Admin_ACL if wanted)
    """

    name = "akku"
    keywords = ['akku', 'battery', 'bat']
    description = "Shows repeater battery voltages (RepeaterTelemetry service)"
    category = "meshcore_info"

    short_description = "Akkustand der überwachten Repeater"
    usage = "akku [name|jetzt]"
    examples = ["akku", "akku MeinRepeater", "akku jetzt"]

    def __init__(self, bot):
        super().__init__(bot)
        self.akku_enabled = self.get_config_value('Akku_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.akku_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "akku [name|jetzt] - Akkustand der Repeater"

    def _service(self):
        return getattr(self.bot, 'services', {}).get('repeatertelemetry')

    async def execute(self, message: MeshMessage) -> bool:
        service = self._service()
        if service is None:
            await self.send_response(message, "Repeater-Überwachung ist nicht aktiv ([RepeaterTelemetry_Service] enabled = true).")
            return True

        parts = message.content.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if arg.lower() in ("jetzt", "now", "poll"):
            service.request_poll()
            await self.send_response(message, "Abfrage gestartet, Ergebnisse folgen in ein paar Minuten (akku).")
            return True

        try:
            rows = service.latest_samples()
        except Exception as e:
            self.logger.error(f"akku: reading telemetry failed: {e}")
            await self.send_response(message, "Fehler beim Lesen der Akkudaten.")
            return False

        if not rows:
            await self.send_response(message, "Noch keine Akkudaten vorhanden.")
            return True

        now = time.time()
        if arg:
            match = [r for r in rows if r['name'].lower() == arg.lower()] or \
                    [r for r in rows if arg.lower() in r['name'].lower()]
            if not match:
                await self.send_response(message, f"Kein Repeater '{arg}' gefunden.")
                return True
            r = match[0]
            up_d = (r.get('uptime_s') or 0) // 86400
            text = (f"{r['name']}: {self._volt(r)} vor {_age(now - r['ts'])}, "
                    f"Laufzeit {up_d}d, RSSI {r.get('last_rssi')}")
            if r.get('offline'):
                text += f", aktuell nicht erreichbar ({r.get('fails')}x)"
            await self.send_response(message, text)
            return True

        lines = []
        for r in rows:
            mark = self._mark(r)
            lines.append(f"{r['name']} {self._volt(r)}{mark} ({_age(now - r['ts'])})")
        await self.send_response(message, "\n".join(lines))
        return True

    @staticmethod
    def _volt(r) -> str:
        mv = r.get('bat_mv') or 0
        return f"{mv / 1000:.2f}V" if mv else "--"

    @staticmethod
    def _mark(r) -> str:
        if r.get('offline'):
            return " 📴"
        return {"critical": " 🪫", "warn": " ⚠️"}.get(r.get('level') or "", "")
