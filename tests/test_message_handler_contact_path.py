"""Tests for NEW_CONTACT / meshcore contact path wire encoding helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore import EventType

from modules.message_handler import MessageHandler
from modules.repeater_manager import RepeaterManager, TrackAdvertResult


def _make_config_get():
    defaults = {
        ("Bot", "rf_data_timeout"): "15.0",
        ("Bot", "message_correlation_timeout"): "10.0",
    }

    def _get(section, key, **kw):
        return defaults.get((section, key), kw.get("fallback", ""))

    return _get


@pytest.fixture
def message_handler():
    bot = MagicMock()
    bot.logger = MagicMock()
    bot.config = MagicMock()
    bot.config.get = MagicMock(side_effect=_make_config_get())
    bot.config.getboolean = MagicMock(return_value=True)
    return MessageHandler(bot)


@pytest.fixture
def companion_new_contact_setup():
    """Bot + MessageHandler wired for companion NEW_CONTACT → add_contact (bot auto-manage)."""
    bot = MagicMock()
    bot.logger = MagicMock()

    def _config_get(section, key, **kw):
        defaults = {
            ("Bot", "rf_data_timeout"): "15.0",
            ("Bot", "message_correlation_timeout"): "10.0",
            ("Bot", "auto_manage_contacts"): "bot",
        }
        return defaults.get((section, key), kw.get("fallback", ""))

    bot.config = MagicMock()
    bot.config.get = MagicMock(side_effect=_config_get)
    bot.config.getboolean = MagicMock(return_value=True)
    bot.prefix_hex_chars = 8

    mh = MessageHandler(bot)
    bot.message_handler = mh

    rm = MagicMock()
    rm.bot = bot
    rm.logger = bot.logger
    rm._is_repeater_device = MagicMock(return_value=False)
    rm.track_contact_advertisement = AsyncMock(
        return_value=TrackAdvertResult(ok=True, duplicate_packet=False)
    )
    rm.check_and_auto_purge = AsyncMock()
    rm.get_contact_list_status = AsyncMock(
        return_value={"is_near_limit": False, "usage_percentage": 0.0}
    )
    rm.manage_contact_list = AsyncMock()
    rm.db_manager = MagicMock()
    rm.db_manager.execute_update = MagicMock()

    async def _add_companion(contact_data, contact_name, public_key):
        return await RepeaterManager.add_companion_from_contact_data(rm, contact_data, contact_name, public_key)

    rm.add_companion_from_contact_data = AsyncMock(side_effect=_add_companion)
    bot.repeater_manager = rm

    ok = MagicMock()
    ok.type = EventType.OK
    bot.meshcore = MagicMock()
    bot.meshcore.commands = MagicMock()
    bot.meshcore.commands.add_contact = AsyncMock(return_value=ok)

    mh._update_mesh_graph_from_advert = MagicMock()
    mh._store_observed_path = MagicMock()

    return bot, mh


class TestEnsureContactMeshcorePathEncoding:
    def test_no_op_when_hash_mode_not_negative_one(self, message_handler):
        c = {"out_path_hash_mode": 0, "out_path_len": 4}
        message_handler._ensure_contact_meshcore_path_encoding(c)
        assert c["out_path_hash_mode"] == 0
        assert c["out_path_len"] == 4

    def test_no_op_when_flood_sentinel(self, message_handler):
        c = {"out_path_hash_mode": -1, "out_path_len": -1}
        message_handler._ensure_contact_meshcore_path_encoding(c)
        assert c["out_path_hash_mode"] == -1
        assert c["out_path_len"] == -1

    def test_fixes_inconsistent_flood_hash_with_plain_hop_count(self, message_handler):
        c = {
            "out_path_hash_mode": -1,
            "out_path_len": 4,
            "out_bytes_per_hop": 1,
        }
        message_handler._ensure_contact_meshcore_path_encoding(c)
        assert c["out_path_hash_mode"] == 0
        assert c["out_path_len"] == 4

    def test_fixes_with_multi_byte_path(self, message_handler):
        c = {
            "out_path_hash_mode": -1,
            "out_path_len": 3,
            "out_bytes_per_hop": 2,
        }
        message_handler._ensure_contact_meshcore_path_encoding(c)
        assert c["out_path_hash_mode"] == 1
        assert c["out_path_len"] == 3
        assert (c["out_path_len"] | (c["out_path_hash_mode"] << 6)) == 0x43

    def test_fixes_when_hash_mode_is_string_and_out_path_len_missing(self, message_handler):
        c = {
            "out_path_hash_mode": "-1",
            "out_path": "01020304",
            "out_bytes_per_hop": 1,
        }
        message_handler._ensure_contact_meshcore_path_encoding(c)
        assert c["out_path_hash_mode"] == 0
        assert c["out_path_len"] == 4
        assert (c["out_path_len"] | (c["out_path_hash_mode"] << 6)) == 0x04


class TestHandleNewContactAddContact:
    """handle_new_contact + mocked add_contact: path fields match wire encoding (no OverflowError)."""

    @staticmethod
    def _pack_path_byte(contact: dict) -> int:
        """Same combination as meshcore update_contact after flood check."""
        opl = contact["out_path_len"]
        hm = contact["out_path_hash_mode"]
        if opl == -1 and hm == -1:
            return 255
        return (opl & 0x3F) | ((hm & 0x03) << 6)

    @pytest.mark.asyncio
    async def test_add_contact_receives_merged_path_from_rf_flood_event(self, companion_new_contact_setup):
        """Flood NEW_CONTACT (-1/-1) + RF routing with path_len_byte fixes hash_mode for add_contact."""
        bot, mh = companion_new_contact_setup
        mh.recent_rf_data = [
            {
                "routing_info": {
                    "path_hex": "0102030405060708",
                    "path_length": 4,
                    "path_len_byte": 0x04,
                    "bytes_per_hop": 1,
                    "path_byte_length": 4,
                },
                "snr": 13.5,
            }
        ]

        event = MagicMock()
        event.payload = {
            "public_key": "a95b4becd36e185eae392d48f11825143d8505d9421a15c7d9f99bc51da70f66",
            "type": 1,
            "flags": 0,
            "out_path_hash_mode": -1,
            "out_path_len": -1,
            "out_path": "",
            "adv_name": "Test Companion",
            "last_advert": 1,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
            "lastmod": 1,
        }

        await mh.handle_new_contact(event)

        add = bot.meshcore.commands.add_contact
        add.assert_awaited_once()
        passed = add.await_args[0][0]
        assert passed["out_path_hash_mode"] == 0
        assert passed["out_path_len"] == 4
        assert passed["out_path"] == "0102030405060708"
        pb = self._pack_path_byte(passed)
        assert int(pb).to_bytes(1, "little", signed=False) == b"\x04"

    @pytest.mark.asyncio
    async def test_add_contact_uses_path_len_byte_for_two_byte_hops(self, companion_new_contact_setup):
        bot, mh = companion_new_contact_setup
        path_hex = "414243444546"  # 3 hops × 2 bytes = 6 bytes = 12 hex chars
        mh.recent_rf_data = [
            {
                "routing_info": {
                    "path_hex": path_hex,
                    "path_length": 3,
                    "path_len_byte": 0x43,
                    "bytes_per_hop": 2,
                    "path_byte_length": 6,
                },
            }
        ]

        event = MagicMock()
        event.payload = {
            "public_key": "b95b4becd36e185eae392d48f11825143d8505d9421a15c7d9f99bc51da70f66",
            "type": 1,
            "flags": 0,
            "out_path_hash_mode": -1,
            "out_path_len": -1,
            "out_path": "",
            "adv_name": "MultiByte",
            "last_advert": 1,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
            "lastmod": 1,
        }

        await mh.handle_new_contact(event)

        passed = bot.meshcore.commands.add_contact.await_args[0][0]
        assert passed["out_path_hash_mode"] == 1
        assert passed["out_path_len"] == 3
        assert self._pack_path_byte(passed) == 0x43
        int(self._pack_path_byte(passed)).to_bytes(1, "little", signed=False)
