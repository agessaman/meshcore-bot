"""Status stats carry the radio's packet counters, as meshcore-packet-capture's do."""

from __future__ import annotations

import asyncio
import logging
import types

from meshcore import EventType
from meshcore.events import Event

from modules.service_plugins.packet_capture_service import PacketCaptureService


def build_service(commands):
    """Build a minimal PacketCaptureService without running __init__."""
    service = object.__new__(PacketCaptureService)
    service.bot = types.SimpleNamespace(meshcore=types.SimpleNamespace(is_connected=True, commands=commands))
    service.logger = logging.getLogger("test-packet-capture-stats")
    service.debug = False
    service.stats_status_enabled = True
    service.stats_refresh_interval = 300
    service.stats_capability_state = None
    service.stats_fetch_lock = asyncio.Lock()
    service.latest_stats = None
    service.last_stats_fetch = 0
    return service


async def test_status_stats_include_packet_counters():
    core = {"battery_mv": 4100, "uptime_secs": 600, "errors": 0, "queue_len": 0}
    radio = {"noise_floor": -110, "tx_air_secs": 12, "rx_air_secs": 340}
    # The keys meshcore_py parses from a STATS_TYPE_PACKETS frame
    packets = {"recv": 15, "sent": 9, "flood_tx": 6, "direct_tx": 3, "flood_rx": 14, "direct_rx": 1, "recv_errors": 7}

    async def get_stats_core():
        return Event(EventType.STATS_CORE, core)

    async def get_stats_radio():
        return Event(EventType.STATS_RADIO, radio)

    async def get_stats_packets():
        return Event(EventType.STATS_PACKETS, packets)

    commands = types.SimpleNamespace(
        get_stats_core=get_stats_core, get_stats_radio=get_stats_radio, get_stats_packets=get_stats_packets
    )
    stats = await build_service(commands).refresh_stats(force=True)

    assert stats == {**core, **radio, **packets, "packets_sent": 9, "packets_received": 15}
