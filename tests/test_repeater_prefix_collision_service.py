import asyncio
import configparser
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.service_plugins.repeater_prefix_collision_service import (
    RepeaterPrefixCollisionService,
)


class _FakeDB:
    """
    Very small fake DBManager that responds to the queries used by the service.
    It is intentionally strict-ish: we match on substrings.
    """

    def __init__(self):
        self._row_checks = 0

        # Test-controlled behavior
        self.row_after_checks = 1
        self.contact_row = None
        self.duplicate_count = 1
        self.used_prefixes = 10
        self.unique_advert_count_today = 1

    def execute_query(self, query: str, params=()):
        q = " ".join(query.split())

        if "FROM complete_contact_tracking WHERE public_key = ?" in q and "SELECT public_key" in q:
            self._row_checks += 1
            if self._row_checks >= self.row_after_checks:
                return [self.contact_row] if self.contact_row else []
            return []

        if "DATE(?) = DATE('now', 'localtime')" in q:
            fh = params[0] if params else None
            if fh is None:
                return []
            prefix = str(fh)[:10]
            if prefix == date.today().isoformat():
                return [{"ok": 1}]
            return []

        if "FROM unique_advert_packets" in q and "COUNT(*)" in q:
            return [{"n": self.unique_advert_count_today}]

        if "SELECT COUNT(*) AS cnt" in q and "SUBSTR(public_key" in q:
            return [{"cnt": self.duplicate_count}]

        if "COUNT(DISTINCT SUBSTR(public_key" in q and "AS used" in q:
            return [{"used": self.used_prefixes}]

        raise AssertionError(f"Unexpected query: {q} params={params}")


def _base_contact_row(**overrides):
    today = date.today().isoformat()
    row = {
        "public_key": "01020304",
        "name": "NewRepeater",
        "role": "repeater",
        "advert_count": 1,
        "first_heard": f"{today} 12:00:00",
        "last_heard": "2026-01-01",
        "latitude": None,
        "longitude": None,
        "city": "Seattle",
        "state": "WA",
        "country": "US",
    }
    row.update(overrides)
    return row


def _make_bot(section_overrides=None):
    bot = MagicMock()
    bot.logger = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.logger.debug = Mock()

    cfg = configparser.ConfigParser()
    cfg.add_section("Connection")
    cfg.add_section("Bot")
    cfg.add_section("Channels")
    cfg.add_section("RepeaterPrefixCollision_Service")
    cfg.set("RepeaterPrefixCollision_Service", "enabled", "true")
    cfg.set("RepeaterPrefixCollision_Service", "channels", "#general,#repeaters")
    cfg.set("RepeaterPrefixCollision_Service", "notify_on_prefix_bytes", "1")
    cfg.set("RepeaterPrefixCollision_Service", "heard_window_days", "30")
    cfg.set("RepeaterPrefixCollision_Service", "prefix_free_days", "30")
    cfg.set("RepeaterPrefixCollision_Service", "post_process_delay_seconds", "0.0")
    cfg.set("RepeaterPrefixCollision_Service", "post_process_timeout_seconds", "2.0")
    cfg.set("RepeaterPrefixCollision_Service", "post_process_poll_interval_seconds", "0.01")
    cfg.set("RepeaterPrefixCollision_Service", "include_prefix_free_hint", "true")
    cfg.set("RepeaterPrefixCollision_Service", "cooldown_minutes_per_prefix", "60")

    if section_overrides:
        for k, v in section_overrides.items():
            cfg.set("RepeaterPrefixCollision_Service", k, str(v))

    bot.config = cfg
    bot.command_manager = MagicMock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    bot.meshcore = MagicMock()
    bot.meshcore.subscribe = Mock()
    bot.db_manager = _FakeDB()
    return bot


def _make_event(public_key: str, name: str = "NewRepeater"):
    e = MagicMock()
    e.payload = {"public_key": public_key, "name": name}
    return e


@pytest.mark.asyncio
async def test_posts_message_on_duplicate_prefix_after_row_available():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 2
    db.used_prefixes = 10

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._handle_new_contact_payload({"public_key": "01020304", "name": "NewRepeater"})

    assert bot.command_manager.send_channel_message.await_count == 2
    calls = [c.args for c in bot.command_manager.send_channel_message.await_args_list]
    assert calls[0][0] == "#general"
    assert calls[1][0] == "#repeaters"
    msg = calls[0][1]
    assert "Heard new repeater NewRepeater" in msg
    assert "prefix 01" in msg  # 1-byte prefix
    assert "near Seattle, WA, US" in msg
    assert "free prefixes remain" in msg
    assert "Type 'prefix free' to find one." in msg


@pytest.mark.asyncio
async def test_skips_when_two_unique_adverts_today():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row(advert_count=2)
    db.unique_advert_count_today = 2
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._handle_new_contact_payload({"public_key": "01020304", "name": "OldRepeater"})

    assert bot.command_manager.send_channel_message.await_count == 0


@pytest.mark.asyncio
async def test_skips_when_first_heard_not_today():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    db.contact_row = _base_contact_row(first_heard=f"{yesterday} 12:00:00", advert_count=1)
    db.unique_advert_count_today = 1
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._handle_new_contact_payload({"public_key": "01020304", "name": "OldFriend"})

    assert bot.command_manager.send_channel_message.await_count == 0


@pytest.mark.asyncio
async def test_skips_when_zero_unique_adverts_today():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.unique_advert_count_today = 0
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._handle_new_contact_payload({"public_key": "01020304", "name": "Weird"})

    assert bot.command_manager.send_channel_message.await_count == 0


@pytest.mark.asyncio
async def test_skips_first_day_when_no_duplicate_prefix():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.unique_advert_count_today = 1
    db.duplicate_count = 0

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._handle_new_contact_payload({"public_key": "01020304", "name": "Lonely"})

    assert bot.command_manager.send_channel_message.await_count == 0


@pytest.mark.asyncio
async def test_dedup_suppresses_repeat_for_same_public_key_and_prefix_bytes():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row(
        public_key="A1B2C3D4",
        name="R1",
        city="Austin",
        state="TX",
        country="US",
    )
    db.duplicate_count = 1

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    payload = {"public_key": "A1B2C3D4", "name": "R1"}
    await svc._handle_new_contact_payload(payload)
    await svc._handle_new_contact_payload(payload)

    assert bot.command_manager.send_channel_message.await_count == 2  # first time only (2 channels)


@pytest.mark.asyncio
async def test_multi_byte_notifications_work_and_hint_is_omitted():
    bot = _make_bot(
        {
            "notify_on_prefix_bytes": "2",
            "include_prefix_free_hint": "true",
        }
    )
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row(
        public_key="010101AA",
        name="R2",
        city="",
        state="",
        country="",
        latitude=47.6,
        longitude=-122.3,
    )
    db.duplicate_count = 1

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._handle_new_contact_payload({"public_key": "010101AA", "name": "R2"})

    assert bot.command_manager.send_channel_message.await_count == 2
    msg = bot.command_manager.send_channel_message.await_args_list[0].args[1]
    assert "prefix 0101" in msg
    assert "Type 'prefix free' to find one." not in msg


@pytest.mark.asyncio
async def test_on_new_contact_schedules_task_that_sends():
    """Thin _on_new_contact should not block; work completes in background task."""
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    await svc._on_new_contact(_make_event("01020304", "NewRepeater"), None)
    assert bot.command_manager.send_channel_message.await_count == 0

    await asyncio.sleep(0.05)

    assert bot.command_manager.send_channel_message.await_count == 2


@pytest.mark.asyncio
async def test_discovery_external_when_notify_all_true_and_webhook():
    bot = _make_bot(
        {
            "notify_external_on_all_new_repeaters": "true",
            "discord_webhook_urls": "https://discord.com/api/webhooks/123456789/abcdefghij",
        }
    )
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 0

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    with patch.object(svc, "send_external_notifications", new_callable=AsyncMock) as ext:
        await svc._handle_new_contact_payload({"public_key": "01020304", "name": "Lonely"})

    assert ext.await_count == 1
    args = ext.await_args_list[0][0]
    assert "New repeater heard" in args[0]
    assert "NewRepeater" in args[0]
    assert bot.command_manager.send_channel_message.await_count == 0


@pytest.mark.asyncio
async def test_discovery_skips_external_when_no_targets_configured():
    bot = _make_bot({"notify_external_on_all_new_repeaters": "true"})
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 0

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    with patch.object(svc, "send_external_notifications", new_callable=AsyncMock) as ext:
        await svc._handle_new_contact_payload({"public_key": "01020304", "name": "Lonely"})

    assert ext.await_count == 0


@pytest.mark.asyncio
async def test_silence_mesh_skips_channel_but_sends_collision_external():
    bot = _make_bot(
        {
            "silence_mesh_output": "true",
            "notify_external_on_all_new_repeaters": "false",
        }
    )
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    with patch.object(svc, "send_external_notifications", new_callable=AsyncMock) as ext:
        await svc._handle_new_contact_payload({"public_key": "01020304", "name": "NewRepeater"})

    assert bot.command_manager.send_channel_message.await_count == 0
    assert ext.await_count == 1
    assert "Heard new repeater" in ext.await_args_list[0][0][0]


@pytest.mark.asyncio
async def test_collision_external_default_username_when_notify_all_false():
    bot = _make_bot()
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    with patch.object(svc, "send_external_notifications", new_callable=AsyncMock) as ext:
        await svc._handle_new_contact_payload({"public_key": "01020304", "name": "NewRepeater"})

    kw = ext.await_args_list[0][1]
    assert kw.get("discord_username") == "Repeater prefix collision"


@pytest.mark.asyncio
async def test_notify_all_true_collision_only_discovery_external_not_collision_copy():
    bot = _make_bot(
        {
            "notify_external_on_all_new_repeaters": "true",
            "discord_webhook_urls": "https://discord.com/api/webhooks/123456789/abcdefghij",
        }
    )
    db: _FakeDB = bot.db_manager
    db.contact_row = _base_contact_row()
    db.duplicate_count = 2

    svc = RepeaterPrefixCollisionService(bot)
    await svc.start()

    with patch.object(svc, "send_external_notifications", new_callable=AsyncMock) as ext:
        await svc._handle_new_contact_payload({"public_key": "01020304", "name": "Dup"})

    assert ext.await_count == 1
    assert "New repeater heard" in ext.await_args_list[0][0][0]
    assert "free prefixes remain" not in ext.await_args_list[0][0][0]
