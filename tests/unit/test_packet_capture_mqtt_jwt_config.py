"""PacketCapture MQTT broker JWT renewal interval and TTL parsing."""

from __future__ import annotations

import configparser
from unittest.mock import MagicMock

import pytest

from modules.service_plugins.packet_capture_service import PacketCaptureService


def _bot_from_ini(ini: str) -> MagicMock:
    cp = configparser.ConfigParser()
    cp.read_string(ini.strip())
    bot = MagicMock()
    bot.config = cp
    return bot


def test_parse_mqtt_brokers_defaults_renewal_and_ttl():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        mqtt1_server = broker.example
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert len(brokers) == 1
    assert brokers[0]["jwt_renewal_interval"] == 43200
    assert brokers[0]["jwt_ttl_seconds"] == 86400


def test_parse_mqtt_brokers_global_overrides():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        jwt_renewal_interval = 7200
        jwt_ttl_seconds = 3600
        mqtt1_server = a.example
        mqtt2_server = b.example
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert len(brokers) == 2
    for b in brokers:
        assert b["jwt_renewal_interval"] == 7200
        assert b["jwt_ttl_seconds"] == 3600


def test_parse_mqtt_brokers_per_broker_overrides():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        jwt_renewal_interval = 7200
        jwt_ttl_seconds = 86400
        mqtt1_server = short-ttl.example
        mqtt1_jwt_ttl_seconds = 3600
        mqtt1_jwt_renewal_interval = 1800
        mqtt2_server = inherit.example
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert len(brokers) == 2
    assert brokers[0]["host"] == "short-ttl.example"
    assert brokers[0]["jwt_ttl_seconds"] == 3600
    assert brokers[0]["jwt_renewal_interval"] == 1800
    assert brokers[1]["jwt_ttl_seconds"] == 86400
    assert brokers[1]["jwt_renewal_interval"] == 7200


def test_parse_mqtt_brokers_renewal_zero_override():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        jwt_renewal_interval = 3600
        mqtt1_server = no-renew.example
        mqtt1_jwt_renewal_interval = 0
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert brokers[0]["jwt_renewal_interval"] == 0


def test_auth_token_iat_exp_clamps_non_positive_ttl():
    svc = object.__new__(PacketCaptureService)
    svc.jwt_ttl_seconds = 7200
    iat, exp = PacketCaptureService._auth_token_iat_exp(svc, {"jwt_ttl_seconds": 0})
    assert exp - iat == 86400
    assert exp == iat + 86400

    iat2, exp2 = PacketCaptureService._auth_token_iat_exp(svc, {"jwt_ttl_seconds": -10})
    assert exp2 - iat2 == 86400


def test_auth_token_iat_exp_uses_broker_then_global():
    svc = object.__new__(PacketCaptureService)
    svc.jwt_ttl_seconds = 999
    iat, exp = PacketCaptureService._auth_token_iat_exp(svc, {"jwt_ttl_seconds": 120})
    assert exp - iat == 120


@pytest.mark.parametrize(
    "ttl,phrase",
    [
        (3600, "1 hour"),
        (7200, "2 hours"),
        (60, "1 minute"),
        (180, "3 minutes"),
        (90, "90s"),
    ],
)
def test_jwt_ttl_log_phrase(ttl: int, phrase: str):
    assert PacketCaptureService._jwt_ttl_log_phrase(ttl) == phrase


def _waev_service(ini: str):
    bot = _bot_from_ini(ini)
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    svc.logger = MagicMock()
    return svc, PacketCaptureService._parse_mqtt_brokers(svc, bot.config)


def test_waev_broker_defaults_to_a_ttl_the_broker_accepts():
    """waev.app rejects the 24-hour default, leaving the bot unable to auth (#248)."""
    _, brokers = _waev_service(
        """
        [PacketCapture]
        enabled = false
        mqtt1_server = mqtt-a.waev.app
        mqtt2_server = broker.example
        """
    )
    assert brokers[0]["jwt_ttl_seconds"] == 3600
    assert brokers[0]["jwt_renewal_interval"] == 3500
    # Only waev hosts move; every other broker keeps the project default.
    assert brokers[1]["jwt_ttl_seconds"] == 86400
    assert brokers[1]["jwt_renewal_interval"] == 43200


@pytest.mark.parametrize("host", ["waev.app", "WAEV.APP", "mqtt-b.waev.app"])
def test_waev_default_matches_apex_and_subdomains(host: str):
    _, brokers = _waev_service(
        f"""
        [PacketCapture]
        enabled = false
        mqtt1_server = {host}
        """
    )
    assert brokers[0]["jwt_ttl_seconds"] == 3600


@pytest.mark.parametrize("host", ["notwaev.app", "waev.app.example.com"])
def test_waev_default_does_not_match_lookalike_hosts(host: str):
    _, brokers = _waev_service(
        f"""
        [PacketCapture]
        enabled = false
        mqtt1_server = {host}
        """
    )
    assert brokers[0]["jwt_ttl_seconds"] == 86400


def test_explicit_per_broker_ttl_beats_the_waev_default():
    _, brokers = _waev_service(
        """
        [PacketCapture]
        enabled = false
        mqtt1_server = mqtt-a.waev.app
        mqtt1_jwt_ttl_seconds = 1800
        mqtt1_jwt_renewal_interval = 900
        """
    )
    assert brokers[0]["jwt_ttl_seconds"] == 1800
    assert brokers[0]["jwt_renewal_interval"] == 900


def test_explicit_global_ttl_beats_the_waev_default():
    """An operator who set a global value meant it, even for waev."""
    _, brokers = _waev_service(
        """
        [PacketCapture]
        enabled = false
        jwt_ttl_seconds = 7200
        jwt_renewal_interval = 3600
        mqtt1_server = mqtt-a.waev.app
        """
    )
    assert brokers[0]["jwt_ttl_seconds"] == 7200
    assert brokers[0]["jwt_renewal_interval"] == 3600
