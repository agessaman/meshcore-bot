"""RX_LOG vs RAW correlation: prune caches and RAW dedupe vs meshcore-packet-capture."""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.service_plugins.packet_capture_service import PacketCaptureService


def test_prune_correlation_caches_removes_stale_entries():
    svc = object.__new__(PacketCaptureService)
    svc.rf_data_cache_timeout = 15.0
    svc.raw_duplicate_window = 2.0
    now = 1000.0
    svc.rf_data_cache = {
        "alive": {"timestamp": now - 5.0},
        "stale": {"timestamp": now - 20.0},
    }
    svc.recent_rf_packets = {
        "RECENT": now - 0.5,
        "OLD": now - 3.0,
    }
    PacketCaptureService._prune_correlation_caches(svc, now)
    assert list(svc.rf_data_cache.keys()) == ["alive"]
    assert list(svc.recent_rf_packets.keys()) == ["RECENT"]


@pytest.mark.asyncio
async def test_handle_raw_data_skips_duplicate_hex_within_window():
    """RAW_DATA hex equal to RX_LOG-published hex must not call process_packet again."""
    svc = object.__new__(PacketCaptureService)
    svc.logger = logging.getLogger("test_pc_rf")
    svc.logger.addHandler(logging.NullHandler())
    svc.raw_duplicate_window = 2.0

    dup_hex = "AABBCCDD"
    svc.recent_rf_packets = {dup_hex.upper(): time.time()}
    svc.rf_data_cache = {}

    mock_pp = AsyncMock()
    svc.process_packet = mock_pp

    evt = MagicMock()
    evt.payload = {"data": dup_hex.lower()}

    await PacketCaptureService.handle_raw_data(svc, evt, metadata=None)

    mock_pp.assert_not_called()


@pytest.mark.asyncio
async def test_handle_raw_data_merges_rf_cache_by_prefix():
    svc = object.__new__(PacketCaptureService)
    svc.logger = logging.getLogger("test_pc_rf2")
    svc.logger.addHandler(logging.NullHandler())
    svc.raw_duplicate_window = 2.0
    svc.recent_rf_packets = {}
    prefix42 = "42" * 16  # 32 hex chars prefix
    full_hex = prefix42 + "9999"
    svc.rf_data_cache = {
        prefix42: {"snr": 12.5, "rssi": -88, "timestamp": time.time(), "payload_length": None},
    }

    captured: dict = {}

    async def capture_pp(raw_hex, payload, metadata):
        captured["payload"] = payload

    svc.process_packet = capture_pp

    evt = MagicMock()
    evt.payload = {"data": full_hex.upper()}

    await PacketCaptureService.handle_raw_data(svc, evt, metadata=None)

    assert captured["payload"]["snr"] == 12.5
    assert captured["payload"]["rssi"] == -88
