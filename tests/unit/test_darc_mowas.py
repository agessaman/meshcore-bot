#!/usr/bin/env python3
"""
Unit tests for DARC MoWaS CAP alert parsing
"""

import asyncio
import configparser
import xml.dom.minidom
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from modules.service_plugins.darc_mowas_service import (
    DARC_MoWaS_Service,
    TRDECapAlert,
    TRDECapAlertArea,
    TRDECapAlertInfo,
)

DARC_MOWAS_EXAMPLE_CAP = """<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
    <identifier>test</identifier>
    <sender>test</sender>
    <sent>2024-12-31T23:59:59+02:00</sent>
    <status>Actual</status>
    <msgType>Alert</msgType>
    <scope>Public</scope>
    <info>
        <language>DE</language>
        <category>Fire</category>
        <event>Gefahreninformation</event>
        <urgency>Immediate</urgency>
        <severity>Minor</severity>
        <certainty>Observed</certainty>
        <eventCode>
            <valueName>profile:DE-BBK-EVENTCODE:01.00R</valueName>
            <value>BBK-EVC-010</value>
        </eventCode>
        <headline>Test der MoWaS Zulieferung für den DARC</headline>
        <description>Test der MoWaS Zulieferung für den DARC</description>
        <instruction></instruction>
        <contact />
        <parameter>
            <valueName>warnVerwaltungsbereiche</valueName>
            <value>100000000000</value>
        </parameter>
        <parameter>
            <valueName>instructionCode</valueName>
            <value>Test</value>
        </parameter>
        <parameter>
            <valueName>sender_langname</valueName>
            <value>DARC e.V.</value>
        </parameter>
        <parameter>
            <valueName>sender_signature</valueName>
            <value>DARC e.V.
                Lindenallee 4
                34225 Baunatal</value>
        </parameter>
        <area>
            <areaDesc>Deutschland</areaDesc>
            <geocode>
                <valueName>SHN</valueName>
                <value>100000000000</value>
            </geocode>
        </area>
    </info>
</alert>
"""


@pytest.fixture(scope="module")
def cap_alert() -> TRDECapAlert:
    doc = xml.dom.minidom.parseString(DARC_MOWAS_EXAMPLE_CAP)
    alert_el = doc.getElementsByTagName("alert")[0]
    return TRDECapAlert.from_xml(alert_el)


@pytest.mark.unit
class TestMoWaSAlertParsing:
    def test_alert_top_level_fields(self, cap_alert):
        alert = cap_alert
        assert alert.identifier == "test"
        assert alert.sender == "test"
        assert alert.sent == datetime(
            2024, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=2))
        )
        assert alert.status == "Actual"
        assert alert.msgType == "Alert"
        assert alert.scope == "Public"
        assert alert.references is None

    def test_alert_info(self, cap_alert):
        assert len(cap_alert.info) == 1
        info = cap_alert.info[0]
        assert isinstance(info, TRDECapAlertInfo)
        assert info.language == "DE"
        assert info.category == "Fire"
        assert info.event == "Gefahreninformation"
        assert info.urgency == "Immediate"
        assert info.severity == "Minor"
        assert info.certainty == "Observed"
        assert info.headline == "Test der MoWaS Zulieferung für den DARC"
        assert info.description == "Test der MoWaS Zulieferung für den DARC"

    def test_alert_info_parameters(self, cap_alert):
        params = cap_alert.info[0].parameter
        assert ("warnVerwaltungsbereiche", "100000000000") in params
        assert ("instructionCode", "Test") in params
        assert ("sender_langname", "DARC e.V.") in params
        assert any(name == "sender_signature" for name, _ in params)

    def test_alert_area(self, cap_alert):
        area = cap_alert.info[0].area[0]
        assert isinstance(area, TRDECapAlertArea)
        assert area.areaDesc == "Deutschland"
        assert ("SHN", "100000000000") in area.geocode


def _mowas_service_bot():
    bot = MagicMock()
    bot.logger = MagicMock()
    cfg = configparser.ConfigParser()
    cfg.add_section("DARC_MoWaS_Service")
    bot.config = cfg
    return bot


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_chunks_assigns_ascending_timestamps_per_index():
    """Each chunk gets ts_now + i seconds so clients can order and dedupe."""
    svc = DARC_MoWaS_Service(_mowas_service_bot())
    captured: list[tuple[int, datetime]] = []

    async def capture(channel, chunk, index, total, timestamp):
        captured.append((index, timestamp))

    svc._send_chunk_with_retry = capture  # type: ignore[method-assign]

    pending: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def schedule(coro):
        task = real_create_task(coro)
        pending.append(task)
        return task

    with patch(
        "modules.service_plugins.darc_mowas_service.asyncio.create_task",
        side_effect=schedule,
    ):
        await svc._send_chunks("mowas", ["a", "b", "c"])

    await asyncio.gather(*pending)

    assert len(captured) == 3
    captured.sort(key=lambda x: x[0])
    timestamps = [ts for _, ts in captured]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 3
    for i in range(1, len(timestamps)):
        assert timestamps[i] - timestamps[i - 1] == timedelta(seconds=1)
