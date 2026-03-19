"""Tests for modules/models.py — MeshMessage dataclass."""

import pytest

from modules.models import MeshMessage


class TestMeshMessageDefaults:
    """MeshMessage default values."""

    def test_content_is_required(self):
        msg = MeshMessage(content="hello")
        assert msg.content == "hello"

    def test_optional_fields_default_to_none(self):
        msg = MeshMessage(content="test")
        assert msg.sender_id is None
        assert msg.sender_pubkey is None
        assert msg.channel is None
        assert msg.hops is None
        assert msg.path is None
        assert msg.timestamp is None
        assert msg.snr is None
        assert msg.rssi is None
        assert msg.elapsed is None
        assert msg.routing_info is None

    def test_is_dm_defaults_false(self):
        msg = MeshMessage(content="test")
        assert msg.is_dm is False


class TestMeshMessageConstruction:
    """MeshMessage construction with various field combinations."""

    def test_channel_message(self):
        msg = MeshMessage(content="ping", channel="general", sender_id="Alice")
        assert msg.content == "ping"
        assert msg.channel == "general"
        assert msg.sender_id == "Alice"
        assert msg.is_dm is False

    def test_dm_message(self):
        msg = MeshMessage(content="hello", is_dm=True, sender_pubkey="deadbeef")
        assert msg.is_dm is True
        assert msg.channel is None
        assert msg.sender_pubkey == "deadbeef"

    def test_routing_info_dict(self):
        routing = {"path_nodes": ["01", "7e"], "route_type": "FLOOD"}
        msg = MeshMessage(content="data", routing_info=routing)
        assert msg.routing_info == routing
        assert msg.routing_info["route_type"] == "FLOOD"

    def test_numeric_fields(self):
        msg = MeshMessage(
            content="test",
            hops=3,
            timestamp=1700000000,
            snr=5.5,
            rssi=-80,
        )
        assert msg.hops == 3
        assert msg.timestamp == 1700000000
        assert msg.snr == 5.5
        assert msg.rssi == -80

    def test_path_field(self):
        msg = MeshMessage(content="test", path="01,7e,86")
        assert msg.path == "01,7e,86"

    def test_elapsed_field(self):
        msg = MeshMessage(content="test", elapsed="1.23s")
        assert msg.elapsed == "1.23s"


class TestMeshMessageEquality:
    """MeshMessage dataclass equality."""

    def test_equal_messages(self):
        msg1 = MeshMessage(content="ping", channel="general", sender_id="Alice")
        msg2 = MeshMessage(content="ping", channel="general", sender_id="Alice")
        assert msg1 == msg2

    def test_different_content_not_equal(self):
        msg1 = MeshMessage(content="ping")
        msg2 = MeshMessage(content="pong")
        assert msg1 != msg2

    def test_different_channel_not_equal(self):
        msg1 = MeshMessage(content="ping", channel="general")
        msg2 = MeshMessage(content="ping", channel="test")
        assert msg1 != msg2
