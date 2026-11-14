"""Tests for NEW_CONTACT / meshcore contact path wire encoding helpers."""

import time
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
        mh._advert_rf = [
            {
                "routing_info": {
                    "path_hex": "0102030405060708",
                    "path_length": 4,
                    "path_len_byte": 0x04,
                    "bytes_per_hop": 1,
                    "path_byte_length": 4,
                },
                "snr": 13.5,
                "timestamp": time.time(),
                "public_key": "a95b4becd36e185eae392d48f11825143d8505d9421a15c7d9f99bc51da70f66",
                "advert_timestamp": 1,
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
        mh._advert_rf = [
            {
                "routing_info": {
                    "path_hex": path_hex,
                    "path_length": 3,
                    "path_len_byte": 0x43,
                    "bytes_per_hop": 2,
                    "path_byte_length": 6,
                },
                "timestamp": time.time(),
                "public_key": "b95b4becd36e185eae392d48f11825143d8505d9421a15c7d9f99bc51da70f66",
                "advert_timestamp": 1,
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


def _rf(public_key, path_hex, *, packet_hash, advert_timestamp=1, age=0.0):
    return {
        "timestamp": time.time() - age,
        "public_key": public_key,
        "advert_timestamp": advert_timestamp,
        "routing_info": {
            "path_hex": path_hex,
            "path_length": len(path_hex) // 2,
            "bytes_per_hop": 1,
            "path_byte_length": len(path_hex) // 2,
            "packet_hash": packet_hash,
        },
        "snr": 11.0,
        "rssi": -80,
    }


def _flood_contact(public_key):
    return {
        "public_key": public_key,
        "type": 1,
        "flags": 0,
        "out_path_hash_mode": -1,
        "out_path_len": -1,
        "out_path": "",
        "adv_name": "Route Test",
        "last_advert": 1,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
        "lastmod": 1,
    }


class TestNewContactRouteSource:
    """NEW_CONTACT takes its route only from the new contact's own ADVERT packet."""

    PK = "c1" * 32
    OTHER = "d2" * 32

    async def _added(self, bot, mh):
        event = MagicMock()
        event.payload = _flood_contact(self.PK)
        await mh.handle_new_contact(event)
        return bot.meshcore.commands.add_contact.await_args[0][0]

    @pytest.mark.asyncio
    async def test_an_unrelated_packet_route_is_not_used(self, companion_new_contact_setup):
        bot, mh = companion_new_contact_setup
        mh._advert_rf = [_rf(self.OTHER, "a1a2a3", packet_hash="00aa00aa00aa00aa")]  # another node's advert
        mh.recent_rf_data = [  # a channel message heard just before
            {"timestamp": time.time(), "payload_type_int": 5, "routing_info": {"path_hex": "b1b2b3", "path_length": 3}},
        ]
        passed = await self._added(bot, mh)
        assert passed["out_path"] == ""
        assert passed["out_path_len"] == -1
        mh._update_mesh_graph_from_advert.assert_not_called()
        mh._store_observed_path.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_contact_advert_route_is_used_and_not_recorded_again(self, companion_new_contact_setup):
        bot, mh = companion_new_contact_setup
        mh._advert_rf = [
            _rf(self.OTHER, "a1a2a3", packet_hash="00aa00aa00aa00aa"),
            _rf(self.PK, "c1c2", packet_hash="00cc00cc00cc00cc"),
            _rf(self.OTHER, "a4a5", packet_hash="00dd00dd00dd00dd"),
        ]
        passed = await self._added(bot, mh)
        assert passed["out_path"] == "c1c2"
        assert passed["out_path_len"] == 2
        mh._update_mesh_graph_from_advert.assert_not_called()
        mh._store_observed_path.assert_not_called()
        hash_passed = bot.repeater_manager.track_contact_advertisement.await_args.kwargs["packet_hash"]
        assert hash_passed == "00cc00cc00cc00cc"

    @pytest.mark.asyncio
    async def test_the_first_copy_of_the_newest_advert_is_used(self, companion_new_contact_setup):
        bot, mh = companion_new_contact_setup
        mh._advert_rf = [
            _rf(self.PK, "0f0f", packet_hash="0001000100010001", advert_timestamp=0, age=8.0),  # an older advert
            _rf(self.PK, "e1e2", packet_hash="0002000200020002", age=2.0),  # first copy heard
            _rf(self.PK, "e3e4e5", packet_hash="0002000200020002", age=1.0),  # a later copy
        ]
        passed = await self._added(bot, mh)
        assert passed["out_path"] == "e1e2"

    @pytest.mark.asyncio
    async def test_an_advert_older_than_the_rf_window_is_not_used(self, companion_new_contact_setup):
        bot, mh = companion_new_contact_setup
        mh._advert_rf = [_rf(self.PK, "c1c2", packet_hash="00cc00cc00cc00cc", age=60.0)]
        passed = await self._added(bot, mh)
        assert passed["out_path"] == ""


    @pytest.mark.asyncio
    async def test_the_advert_named_by_last_advert_is_used(self, companion_new_contact_setup):
        """A delayed copy of an older advert does not stand in for the one NEW_CONTACT is about."""
        bot, mh = companion_new_contact_setup
        mh._advert_rf = [
            _rf(self.PK, "a1a1", packet_hash="000a000a000a000a", advert_timestamp=1, age=5.0),
            _rf(self.PK, "b2b2", packet_hash="000b000b000b000b", advert_timestamp=2, age=3.0),
            _rf(self.PK, "a3a3", packet_hash="000a000a000a000a", advert_timestamp=1, age=1.0),  # delayed copy
        ]
        event = MagicMock()
        event.payload = {**_flood_contact(self.PK), "last_advert": 2}
        await mh.handle_new_contact(event)
        assert bot.meshcore.commands.add_contact.await_args[0][0]["out_path"] == "b2b2"


    @pytest.mark.asyncio
    async def test_last_advert_zero_is_matched_exactly(self, companion_new_contact_setup):
        bot, mh = companion_new_contact_setup
        mh._advert_rf = [
            _rf(self.PK, "a0a0", packet_hash="000a000a000a000a", advert_timestamp=0, age=3.0),
            _rf(self.PK, "a1a1", packet_hash="000b000b000b000b", advert_timestamp=1, age=1.0),
        ]
        event = MagicMock()
        event.payload = {**_flood_contact(self.PK), "last_advert": 0}
        await mh.handle_new_contact(event)
        assert bot.meshcore.commands.add_contact.await_args[0][0]["out_path"] == "a0a0"

class TestAdvertSeenBeforeProcessing:
    @pytest.mark.asyncio
    async def test_new_contact_handled_during_advert_processing_finds_the_advert(self, message_handler):
        """The advert is recorded before _process_advertisement_packet yields."""
        mh = message_handler
        pk = "e7" * 32
        payload_hex = pk + (1234).to_bytes(4, "little").hex() + "00" * 70
        seen = {}

        async def _process(packet_info, signal_info):
            seen["entry"] = mh._find_advert_rf_data(pk, 1234)

        mh._process_advertisement_packet = _process
        routing_info = {"path_hex": "0102", "path_length": 2, "packet_hash": "00ee00ee00ee00ee"}
        mh._remember_advert_rf({"payload_hex": payload_hex}, routing_info, "00ee00ee00ee00ee", {"snr": 5.0}, time.time())
        await mh._process_advertisement_packet({}, {})
        assert seen["entry"]["routing_info"]["path_hex"] == "0102"
        assert mh._find_advert_rf_data(pk, 999) is None
