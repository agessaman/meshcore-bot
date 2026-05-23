"""Hardware-free bot/backend integration tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from modules.core import MeshCoreBot
from shared.radio_backend import BackendCapabilities, BackendEvent, BackendEventType, BackendResult


def _write_integration_config(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""[Connection]
connection_type = pymc
timeout = 30

[Bot]
bot_name = TestBot
db_path = {db_path.as_posix()}
enabled = true
passive_mode = false
rate_limit_seconds = 0
bot_tx_rate_limit_seconds = 0
tx_delay_ms = 0
max_channels = 1
prefix_bytes = 1

[Channels]
monitor_channels = #general
respond_to_dms = true
max_response_hops = 64

[Keywords]
ping = "Pong!"
""",
        encoding="utf-8",
    )


class FakeCommands:
    def __init__(self, backend: "FakeBackend") -> None:
        self.backend = backend
        self.get_contacts_calls = 0
        self.sent_dms: list[tuple[dict[str, Any], str]] = []
        self.sent_channels: list[tuple[int, str, int | None]] = []
        self.adverts: list[bool] = []

    async def get_contacts(self) -> BackendResult:
        self.get_contacts_calls += 1
        return BackendResult.ok({"contacts": list(self.backend.contacts.values())})

    async def get_channel(self, channel_idx: int) -> BackendResult:
        if channel_idx == 0:
            return BackendResult.ok(
                {
                    "channel_idx": 0,
                    "channel_name": "#general",
                    "channel_secret": b"1" * 16,
                }
            )
        return BackendResult.ok({"channel_idx": channel_idx, "channel_name": "", "channel_secret": b"\x00" * 16})

    async def get_time(self) -> BackendResult:
        return BackendResult.ok({"time": int(time.time()) + 60})

    async def set_time(self, timestamp: int) -> BackendResult:
        return BackendResult.ok({"time": timestamp})

    async def set_name(self, name: str) -> BackendResult:
        self.backend.self_info["name"] = name
        self.backend.self_info["adv_name"] = name
        return BackendResult.ok({"name": name})

    async def send_msg_with_retry(self, contact: dict[str, Any], content: str, **kwargs: Any) -> BackendResult:
        self.sent_dms.append((contact, content))
        return BackendResult.sent({"contact": contact.get("public_key", "")})

    async def send_msg(self, contact: dict[str, Any], content: str) -> BackendResult:
        self.sent_dms.append((contact, content))
        return BackendResult.sent({"contact": contact.get("public_key", "")})

    async def send_chan_msg(self, channel_idx: int, content: str, timestamp: int | None = None) -> BackendResult:
        self.sent_channels.append((channel_idx, content, timestamp))
        return BackendResult.sent({"channel_idx": channel_idx})

    async def set_flood_scope(self, scope: str) -> BackendResult:
        return BackendResult.ok({"scope": scope})

    async def send_advert(self, flood: bool = False) -> BackendResult:
        self.adverts.append(flood)
        return BackendResult.ok({"flood": flood})


class FakeBackend:
    capabilities = BackendCapabilities(backend_name="pymc_core", unlimited_contacts=True)

    def __init__(self) -> None:
        self.commands = FakeCommands(self)
        self.connected = False
        self.contacts = {
            "ab" * 32: {
                "public_key": "ab" * 32,
                "name": "Alice",
                "adv_name": "Alice",
                "out_path": "",
                "out_path_len": 0,
            }
        }
        self.channels: dict[int, dict[str, Any]] = {}
        self.self_info = {"name": "TestBot", "adv_name": "TestBot", "public_key": "cd" * 32}
        self.subscribers: dict[BackendEventType, list[Any]] = {}
        self.auto_fetch_started = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.connected = False

    async def start_auto_message_fetching(self) -> None:
        self.auto_fetch_started = True

    def subscribe(self, event_type: BackendEventType, callback: Any) -> tuple[BackendEventType, Any]:
        self.subscribers.setdefault(event_type, []).append(callback)
        return (event_type, callback)

    def unsubscribe(self, subscription: tuple[BackendEventType, Any]) -> None:
        callbacks = self.subscribers.get(subscription[0], [])
        if subscription[1] in callbacks:
            callbacks.remove(subscription[1])

    def get_contact_by_name(self, name: str) -> dict[str, Any] | None:
        for contact in self.contacts.values():
            if contact.get("name") == name or contact.get("adv_name") == name:
                return contact
        return None

    def get_contact_by_key_prefix(self, prefix: str) -> dict[str, Any] | None:
        for public_key, contact in self.contacts.items():
            if public_key.startswith(prefix):
                return contact
        return None

    async def emit(self, event_type: BackendEventType, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
        event = BackendEvent(event_type, payload, metadata or {})
        for callback in list(self.subscribers.get(event_type, [])):
            result = callback(event, event.metadata)
            if hasattr(result, "__await__"):
                await result


@pytest.fixture
def integration_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MeshCoreBot, FakeBackend]:
    config_file = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    _write_integration_config(config_file, db_path)
    fake_backend = FakeBackend()

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("modules.core.create_radio_backend", lambda *args, **kwargs: fake_backend)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    return MeshCoreBot(config_file=str(config_file)), fake_backend


@pytest.mark.asyncio
async def test_bot_connects_with_fake_backend_and_subscribes_handlers(integration_bot: tuple[MeshCoreBot, FakeBackend]) -> None:
    bot, backend = integration_bot

    assert await bot.connect() is True

    assert bot.radio_backend is backend
    assert bot.meshcore is backend
    assert bot.connected is True
    assert backend.auto_fetch_started is True
    assert backend.commands.get_contacts_calls == 1
    assert BackendEventType.CONTACT_MSG_RECV in backend.subscribers
    assert BackendEventType.CHANNEL_MSG_RECV in backend.subscribers
    assert BackendEventType.RX_LOG_DATA in backend.subscribers
    assert BackendEventType.RAW_DATA in backend.subscribers
    assert BackendEventType.NEW_CONTACT in backend.subscribers
    assert bot.channel_manager.get_channel_number("#general") == 0


@pytest.mark.asyncio
async def test_dm_ping_flows_from_backend_event_to_backend_send(integration_bot: tuple[MeshCoreBot, FakeBackend]) -> None:
    bot, backend = integration_bot
    assert await bot.connect() is True

    await backend.emit(
        BackendEventType.CONTACT_MSG_RECV,
        {
            "text": "ping",
            "pubkey_prefix": "ab",
            "sender_timestamp": int(time.time()) + 5,
            "path_len": 0,
            "snr": 7.5,
            "rssi": -101,
        },
        {"pubkey_prefix": "ab", "snr": 7.5, "rssi": -101},
    )

    assert len(backend.commands.sent_dms) == 1
    contact, content = backend.commands.sent_dms[0]
    assert contact["public_key"] == "ab" * 32
    assert content == "Pong!"


@pytest.mark.asyncio
async def test_channel_ping_flows_from_backend_event_to_channel_send(integration_bot: tuple[MeshCoreBot, FakeBackend]) -> None:
    bot, backend = integration_bot
    assert await bot.connect() is True

    await backend.emit(
        BackendEventType.CHANNEL_MSG_RECV,
        {
            "channel_idx": 0,
            "text": "Alice: ping",
            "pubkey_prefix": "ab",
            "sender_timestamp": int(time.time()) + 5,
            "path_len": 0,
            "snr": 6.0,
            "rssi": -99,
        },
        {"pubkey_prefix": "ab", "snr": 6.0, "rssi": -99},
    )

    assert backend.commands.sent_channels == [(0, "Pong!", None)]
