"""PacketCapture MQTT reconnection behaviour (issue #248).

paho owns reconnection once loop_start() is running. The watchdog must observe
rather than drive the socket, brokers must not share a client ID, and a renewed
token has to be put in force before the old one expires.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
from unittest.mock import MagicMock

import pytest

from modules.service_plugins.packet_capture_service import PacketCaptureService


def _bot_from_ini(ini: str) -> MagicMock:
    cp = configparser.ConfigParser()
    cp.read_string(ini.strip())
    bot = MagicMock()
    bot.config = cp
    return bot


def _service() -> PacketCaptureService:
    svc = object.__new__(PacketCaptureService)
    svc.logger = logging.getLogger("test-packet-capture")
    svc.debug = False
    svc.mqtt_enabled = True
    svc.mqtt_connected = False
    svc.mqtt_clients = []
    svc.should_exit = False
    return svc


def _client_info(svc, *, connected: bool, thread_alive: bool | None, **config) -> dict:
    client = MagicMock()
    client.is_connected.return_value = connected
    if thread_alive is None:
        client._thread = None
    else:
        thread = MagicMock()
        thread.is_alive.return_value = thread_alive
        client._thread = thread
    info = {
        "client": client,
        "config": {"host": "broker.example", **config},
        "connected": connected,
        "token_exp": None,
        "down_since": None,
        "stall_warned": False,
        "cycle_lock": asyncio.Lock(),
    }
    svc.mqtt_clients.append(info)
    return info


# --- client identity -------------------------------------------------------


def test_generated_client_ids_are_distinct_per_broker(monkeypatch):
    """Brokers behind one cluster evict each other when they share an ID."""
    bot = _bot_from_ini(
        """
        [Bot]
        bot_name = observer
        [PacketCapture]
        enabled = true
        mqtt1_server = mqtt-a.waev.app
        mqtt2_server = mqtt-b.waev.app
        """
    )
    svc = _service()
    svc.bot = bot
    svc.mqtt_brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)

    created_ids = []

    def fake_client(client_id=None, transport=None):
        created_ids.append(client_id)
        client = MagicMock()
        client.is_connected.return_value = True
        return client

    fake_mqtt = MagicMock()
    fake_mqtt.Client.side_effect = fake_client
    monkeypatch.setattr("modules.service_plugins.packet_capture_service.mqtt", fake_mqtt)

    asyncio.run(PacketCaptureService.connect_mqtt_brokers(svc))

    assert len(created_ids) == 2
    assert created_ids[0] != created_ids[1]


def test_explicit_client_id_is_respected(monkeypatch):
    bot = _bot_from_ini(
        """
        [Bot]
        bot_name = observer
        [PacketCapture]
        enabled = true
        mqtt1_server = mqtt-a.waev.app
        mqtt1_client_id = my-fixed-id
        """
    )
    svc = _service()
    svc.bot = bot
    svc.mqtt_brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)

    created_ids = []
    fake_mqtt = MagicMock()
    fake_mqtt.Client.side_effect = lambda client_id=None, transport=None: (
        created_ids.append(client_id) or MagicMock()
    )
    monkeypatch.setattr("modules.service_plugins.packet_capture_service.mqtt", fake_mqtt)

    asyncio.run(PacketCaptureService.connect_mqtt_brokers(svc))
    assert created_ids == ["my-fixed-id"]


# --- watchdog --------------------------------------------------------------


def test_watchdog_does_not_reconnect_while_paho_retries():
    """The storm in #248: reconnect() called under a live network thread."""
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=True)

    asyncio.run(svc._check_mqtt_client(info, now=1000.0))

    info["client"].reconnect.assert_not_called()
    info["client"].connect.assert_not_called()
    info["client"].disconnect.assert_not_called()
    assert info["connected"] is False


def test_watchdog_restarts_a_dead_network_loop():
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=False)

    asyncio.run(svc._check_mqtt_client(info, now=1000.0))

    info["client"].reconnect.assert_called_once()
    info["client"].loop_start.assert_called_once()


def test_watchdog_restarts_when_no_thread_was_ever_started():
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=None)

    asyncio.run(svc._check_mqtt_client(info, now=1000.0))

    info["client"].reconnect.assert_called_once()


def test_watchdog_clears_downtime_once_connected():
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=True)
    asyncio.run(svc._check_mqtt_client(info, now=1000.0))
    assert info["down_since"] == 1000.0

    info["client"].is_connected.return_value = True
    asyncio.run(svc._check_mqtt_client(info, now=1030.0))

    assert info["connected"] is True
    assert info["down_since"] is None


def test_watchdog_warns_once_when_an_outage_persists(caplog):
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=True)

    asyncio.run(svc._check_mqtt_client(info, now=1000.0))
    with caplog.at_level(logging.WARNING, logger="test-packet-capture"):
        asyncio.run(svc._check_mqtt_client(info, now=1000.0 + svc.MQTT_STALL_WARN_AFTER))
        asyncio.run(svc._check_mqtt_client(info, now=1000.0 + svc.MQTT_STALL_WARN_AFTER + 30))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_watchdog_refreshes_an_expiring_token_without_touching_the_socket():
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=True, use_auth_token=True)
    info["token_exp"] = 1000.0 + 10  # inside the refresh margin

    renewed = []

    async def fake_renew(client_info):
        renewed.append(client_info)
        return True

    svc._renew_mqtt_auth_token = fake_renew
    asyncio.run(svc._check_mqtt_client(info, now=1000.0))

    assert renewed == [info]
    info["client"].reconnect.assert_not_called()


def test_watchdog_leaves_a_valid_token_alone():
    svc = _service()
    info = _client_info(svc, connected=False, thread_alive=True, use_auth_token=True)
    info["token_exp"] = 1000.0 + 3600

    renewed = []

    async def fake_renew(client_info):
        renewed.append(client_info)
        return True

    svc._renew_mqtt_auth_token = fake_renew
    asyncio.run(svc._check_mqtt_client(info, now=1000.0))

    assert renewed == []


# --- connection cycling ----------------------------------------------------


def test_cycle_serializes_teardown_before_reconnect():
    """loop_stop() must join the network thread before this one drives the socket."""
    svc = _service()
    info = _client_info(svc, connected=True, thread_alive=True)
    calls = []
    for name in ("disconnect", "loop_stop", "reconnect", "loop_start"):
        getattr(info["client"], name).side_effect = (
            lambda *_a, _n=name, **_kw: calls.append(_n)
        )

    asyncio.run(svc._cycle_mqtt_client(info, "test"))

    assert calls == ["disconnect", "loop_stop", "reconnect", "loop_start"]


# --- config ----------------------------------------------------------------


def test_keepalive_defaults_and_overrides():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        mqtt1_server = a.example
        mqtt2_server = b.example
        mqtt2_keepalive = 30
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert brokers[0]["keepalive"] == 60
    assert brokers[1]["keepalive"] == 30


def test_jwt_reconnect_on_renew_defaults_on():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        mqtt1_server = a.example
        mqtt2_server = b.example
        mqtt2_jwt_reconnect_on_renew = false
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert brokers[0]["jwt_reconnect_on_renew"] is True
    assert brokers[1]["jwt_reconnect_on_renew"] is False


# --- logging ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rc", "fragment"),
    [(7, "rc=7"), (2, "rc=2")],
)
def test_disconnect_reason_names_the_paho_error(rc, fragment):
    reason = PacketCaptureService._disconnect_reason(rc)
    assert fragment in reason
    # A bare number reads as a CONNACK code; the description is the point.
    assert reason != fragment
