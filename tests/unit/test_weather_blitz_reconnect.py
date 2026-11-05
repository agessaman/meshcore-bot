#!/usr/bin/env python3
"""Unit tests for the Blitzortung MQTT reconnect loop and strike handoff (#362)."""

import asyncio
import configparser
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.service_plugins import weather_service as ws
from modules.service_plugins.weather_service import WeatherService

pytestmark = pytest.mark.unit

BOX = {
    "blitz_area_min_lat": "47.0",
    "blitz_area_min_lon": "-123.0",
    "blitz_area_max_lat": "48.5",
    "blitz_area_max_lon": "-121.5",
}


class FakeClient:
    """A paho client whose session lasts ``session_checks`` is_connected() calls."""

    instances: list["FakeClient"] = []
    session_checks = 2

    def __init__(self, client_id):
        self.client_id = client_id
        self.on_message = None
        self.on_disconnect = None
        self._checks_left = 0
        FakeClient.instances.append(self)

    def connect(self, host, port, keepalive):
        pass

    def subscribe(self, topic):
        pass

    def loop_start(self):
        self._checks_left = FakeClient.session_checks

    def is_connected(self):
        if self._checks_left > 0:
            self._checks_left -= 1
            return True
        return False

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def service(monkeypatch, mock_logger):
    FakeClient.instances = []
    FakeClient.session_checks = 2
    monkeypatch.setattr(ws, "mqtt", SimpleNamespace(Client=FakeClient), raising=False)
    monkeypatch.setattr(ws, "MQTT_AVAILABLE", True)

    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6062")
    config.set("Weather_Service", "my_position_lon", "-122.3321")
    for key, val in BOX.items():
        config.set("Weather_Service", key, val)

    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock()
    svc = WeatherService(bot)
    svc._running = True
    return svc


def record_backoff(monkeypatch, service, attempts):
    """Make sleeps instant; record the backoff ones and stop after ``attempts``."""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(seconds):
        if seconds >= ws.BLITZ_RECONNECT_MIN_SECONDS:
            delays.append(seconds)
            if len(delays) >= attempts:
                service._running = False
        await real_sleep(0)

    monkeypatch.setattr(ws.asyncio, "sleep", fake_sleep)
    return delays


class TestReconnectBackoff:
    async def test_flapping_broker_backs_off_to_the_cap(self, monkeypatch, service):
        delays = record_backoff(monkeypatch, service, attempts=8)
        await asyncio.wait_for(service._connect_blitzortung_mqtt(), 5)
        assert delays == [5, 10, 20, 40, 80, 160, 300, 300]

    async def test_stable_session_resets_the_delay(self, monkeypatch, service):
        monkeypatch.setattr(ws, "BLITZ_STABLE_SESSION_SECONDS", 0.0)
        delays = record_backoff(monkeypatch, service, attempts=4)
        await asyncio.wait_for(service._connect_blitzortung_mqtt(), 5)
        assert delays == [5, 5, 5, 5]

    async def test_unaccepted_connection_is_not_logged_as_connected(self, monkeypatch, service, mock_logger):
        FakeClient.session_checks = 0
        monkeypatch.setattr(ws, "BLITZ_CONNACK_TIMEOUT_SECONDS", 0.0)
        delays = record_backoff(monkeypatch, service, attempts=3)
        await asyncio.wait_for(service._connect_blitzortung_mqtt(), 5)
        assert delays == [5, 10, 20]
        infos = [c.args[0] for c in mock_logger.info.call_args_list]
        assert not any("Connected to Blitzortung" in m for m in infos)
        warnings = [c.args[0] for c in mock_logger.warning.call_args_list]
        assert "Blitzortung MQTT broker did not accept the connection, reconnecting in 5s" in warnings

    async def test_connect_error_backs_off(self, monkeypatch, service):
        monkeypatch.setattr(FakeClient, "connect", Mock(side_effect=OSError("refused")))
        delays = record_backoff(monkeypatch, service, attempts=3)
        await asyncio.wait_for(service._connect_blitzortung_mqtt(), 5)
        assert delays == [5, 10, 20]

    async def test_client_ids_are_unique_within_a_second(self, monkeypatch, service):
        record_backoff(monkeypatch, service, attempts=5)
        await asyncio.wait_for(service._connect_blitzortung_mqtt(), 5)
        ids = [c.client_id for c in FakeClient.instances]
        assert len(ids) == 5
        assert len(set(ids)) == 5


class TestStrikeHandoff:
    async def test_strike_from_network_thread_reaches_the_handler(self, monkeypatch, service):
        handled = asyncio.Event()
        received = []

        async def handle(data):
            received.append(data)
            handled.set()

        monkeypatch.setattr(service, "_handle_lightning_strike", handle)
        FakeClient.session_checks = 10_000
        task = asyncio.create_task(service._connect_blitzortung_mqtt())
        while not FakeClient.instances or FakeClient.instances[0].on_message is None:
            await asyncio.sleep(0)

        on_message = FakeClient.instances[0].on_message
        strike = {"lat": 47.6, "lon": -122.3, "time": 1}
        msg = SimpleNamespace(payload=json.dumps(strike).encode())
        thread = threading.Thread(target=on_message, args=(None, None, msg))
        thread.start()
        thread.join()

        await asyncio.wait_for(handled.wait(), 2)
        assert received == [strike]
        service._running = False
        await asyncio.wait_for(task, 5)
