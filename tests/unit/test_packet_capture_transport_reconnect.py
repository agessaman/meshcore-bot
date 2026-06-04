"""Packet capture re-subscribes to meshcore events after transport reconnect."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from meshcore import EventType

from modules.service_plugins.packet_capture_service import PacketCaptureService


@pytest.mark.asyncio
async def test_on_transport_reconnected_subscribes_on_new_meshcore():
    meshcore_old = MagicMock()
    meshcore_new = MagicMock()

    bot = MagicMock()
    bot.meshcore = meshcore_old

    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    svc.logger = logging.getLogger("test_pc_transport_reconnect")
    svc.logger.addHandler(logging.NullHandler())
    svc._running = True
    svc.event_subscriptions = []

    await PacketCaptureService.setup_event_handlers(svc)
    assert meshcore_old.subscribe.call_count == 2

    bot.meshcore = meshcore_new
    meshcore_new.subscribe.reset_mock()

    await PacketCaptureService.on_transport_reconnected(svc)

    assert meshcore_new.subscribe.call_count == 2
    event_types = [call[0][0] for call in meshcore_new.subscribe.call_args_list]
    assert EventType.RX_LOG_DATA in event_types
    assert EventType.RAW_DATA in event_types


@pytest.mark.asyncio
async def test_on_transport_reconnected_noop_when_not_running():
    meshcore = MagicMock()
    bot = MagicMock()
    bot.meshcore = meshcore

    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    svc.logger = logging.getLogger("test_pc_transport_reconnect2")
    svc.logger.addHandler(logging.NullHandler())
    svc._running = False
    svc.event_subscriptions = []

    await PacketCaptureService.on_transport_reconnected(svc)

    meshcore.subscribe.assert_not_called()
