"""Tests for PacketCapture observer_name override."""

from __future__ import annotations

import configparser
import logging
import types
from unittest.mock import MagicMock

from modules.service_plugins.packet_capture_service import PacketCaptureService

LOGGER = logging.getLogger("test-packet-capture-observer-name")
DEVICE_KEY = "ab" * 32


def build_service(
    observer_name=None,
    device_name="DeviceNode",
    bot_name="BotNode",
):
    """Build a minimal PacketCaptureService without running __init__."""
    config = configparser.ConfigParser()

    config["Bot"] = {
        "bot_name": bot_name,
    }

    config["PacketCapture"] = {}

    if observer_name is not None:
        config["PacketCapture"]["observer_name"] = observer_name

    bot = MagicMock()
    bot.config = config

    bot.meshcore = types.SimpleNamespace(
        self_info={
            "name": device_name,
            "public_key": DEVICE_KEY,
        }
    )

    service = object.__new__(PacketCaptureService)
    service.bot = bot
    service.logger = LOGGER

    service.debug = False
    service.decode_payloads = False
    service.channel_key_store = None

    return service


def test_observer_name_override():
    service = build_service(
        observer_name="ObserverStation",
        device_name="DeviceNode",
    )

    assert service._get_observer_name() == "ObserverStation"


def test_observer_name_falls_back_to_device_name():
    service = build_service(
        device_name="DeviceNode",
    )

    assert service._get_observer_name() == "DeviceNode"


def test_blank_observer_name_falls_back_to_device_name():
    service = build_service(
        observer_name="   ",
        device_name="DeviceNode",
    )

    assert service._get_observer_name() == "DeviceNode"


def test_packet_payload_uses_observer_name():
    service = build_service(
        observer_name="ObserverStation",
        device_name="DeviceNode",
    )

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

    payload = {
        "snr": 5.0,
        "rssi": -90,
    }

    result = service._format_packet_data(
        "00AA",
        packet_info,
        payload,
    )

    assert result["origin"] == "ObserverStation"
    assert result["origin_id"] == DEVICE_KEY.upper()
