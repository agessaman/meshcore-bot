"""Published packet payloads carry UTC time fields, not host-local ones (issue #278)."""

from __future__ import annotations

import configparser
import logging
import os
import time
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from modules.service_plugins.packet_capture_service import PacketCaptureService

LOGGER = logging.getLogger("test-packet-capture-utc-payload-time")
DEVICE_KEY = "cd" * 32

# A fixed UTC+5 zone: no DST, so the offset is nonzero no matter when CI runs.
NON_UTC_TZ = "Etc/GMT-5"


def build_service():
    """Build a minimal PacketCaptureService without running __init__."""
    config = configparser.ConfigParser()
    config["Bot"] = {"bot_name": "BotNode"}
    config["PacketCapture"] = {}

    bot = MagicMock()
    bot.config = config
    bot.meshcore = types.SimpleNamespace(
        self_info={"name": "DeviceNode", "public_key": DEVICE_KEY}
    )

    service = object.__new__(PacketCaptureService)
    service.bot = bot
    service.logger = LOGGER
    service.debug = False
    service.decode_payloads = False
    service.channel_key_store = None
    return service


@pytest.fixture
def non_utc_timezone():
    """Run the test body under a non-UTC local zone, then restore the original."""
    original = os.environ.get("TZ")
    os.environ["TZ"] = NON_UTC_TZ
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def format_packet():
    packet_info = {
        "route_type": "FLOOD",
        "payload_type": "ADVERT",
        "payload_type_value": 4,
        "payload_bytes": 1,
        "path_len": 0,
        "path_byte_length": 0,
        "path": [],
        "packet_hash": "0123456789ABCDEF",
        "has_transport_codes": False,
    }
    return build_service()._format_packet_data("00AA", packet_info, {"snr": 5.0, "rssi": -90})


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset is POSIX-only")
def test_packet_time_and_date_are_utc(non_utc_timezone):
    result = format_packet()

    moment = datetime.fromisoformat(result["timestamp"].replace("Z", "+00:00"))
    assert moment.utcoffset().total_seconds() == 0

    # Pre-fix these were datetime.now() renderings, so under UTC+5 they ran five
    # hours ahead of the "timestamp" they sit beside in the same payload.
    assert result["time"] == moment.strftime("%H:%M:%S")
    assert result["date"] == moment.strftime("%d/%m/%Y")


def test_utc_iso_timestamp_renders_a_supplied_instant():
    moment = datetime(2026, 3, 1, 4, 5, 6, tzinfo=timezone.utc)
    assert PacketCaptureService._utc_iso_timestamp(moment) == "2026-03-01T04:05:06Z"
