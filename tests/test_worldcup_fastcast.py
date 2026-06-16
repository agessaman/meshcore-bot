"""Tests for the experimental fastcast client and its service wake integration."""

import json
from unittest.mock import AsyncMock

from modules.clients.worldcup_fastcast import WorldCupFastcastClient


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class TestFastcastHandle:
    async def test_connect_ack_subscribes_to_topic(self):
        ws = _FakeWS()
        client = WorldCupFastcastClient("gp-soccer-fifa.world", on_push=lambda: None)
        await client._handle(ws, json.dumps({"op": "C", "sid": "abc123"}))
        assert client._sid == "abc123"
        assert ws.sent == [{"op": "S", "sid": "abc123", "tc": "gp-soccer-fifa.world"}]

    async def test_publish_for_topic_fires_callback(self):
        fired = []
        client = WorldCupFastcastClient("gp-soccer-fifa.world", on_push=lambda: fired.append(True))
        await client._handle(_FakeWS(), json.dumps({"op": "P", "tc": "gp-soccer-fifa.world", "pl": "x"}))
        assert fired == [True]

    async def test_publish_for_other_topic_ignored(self):
        fired = []
        client = WorldCupFastcastClient("gp-soccer-fifa.world", on_push=lambda: fired.append(True))
        await client._handle(_FakeWS(), json.dumps({"op": "P", "tc": "gp-football-nfl", "pl": "x"}))
        assert fired == []

    async def test_async_callback_awaited(self):
        cb = AsyncMock()
        client = WorldCupFastcastClient("topicX", on_push=cb)
        await client._handle(_FakeWS(), json.dumps({"op": "P", "tc": "topicX"}))
        cb.assert_awaited_once()

    async def test_heartbeat_echoed(self):
        ws = _FakeWS()
        client = WorldCupFastcastClient("topicX", on_push=lambda: None)
        client._sid = "s1"
        await client._handle(ws, json.dumps({"op": "H"}))
        assert ws.sent == [{"op": "H", "sid": "s1"}]

    async def test_malformed_frame_ignored(self):
        client = WorldCupFastcastClient("topicX", on_push=lambda: None)
        # Should not raise
        await client._handle(_FakeWS(), "not-json{")


class TestServiceWake:
    def test_on_push_sets_wake_event(self):
        import configparser
        from unittest.mock import MagicMock, Mock

        from modules.service_plugins.worldcup_service import WorldCupLiveService

        bot = MagicMock()
        bot.logger = Mock()
        cfg = configparser.ConfigParser()
        cfg.add_section("Worldcup_Service")
        cfg.set("Worldcup_Service", "enabled", "true")
        cfg.set("Worldcup_Service", "use_fastcast", "true")
        bot.config = cfg
        bot.db_manager = None
        svc = WorldCupLiveService(bot)
        assert svc.use_fastcast is True
        assert not svc._wake.is_set()
        svc._on_push()
        assert svc._wake.is_set()
