"""Regression tests for TC_FLOOD scope RF correlation (snoco bench captures)."""

import time
from hashlib import sha256
from unittest.mock import Mock

import pytest

from modules.command_manager import CommandManager
from modules.message_handler import MessageHandler
from tests.integration.test_flood_scope_reply import make_transport_code


def _fresh_rf(entry: dict) -> dict:
    """Copy RF fixture with a current timestamp so find_recent_rf_data age filter passes."""
    out = dict(entry)
    out["timestamp"] = time.time()
    return out


SNOCO_PING_RF = {
    "timestamp": 0,
    "packet_prefix": "33ec149748000000ca37f40824e44f7c",
    "pubkey_prefix": None,
    "snr": 12.75,
    "rssi": -20,
    "raw_hex": (
        "33ec149748000000ca37f40824e44f7c4a819746c46d811faa5745753b17ade613e3a648adf8cbf7c1b03b"
    ),
    "payload": "149748000000ca37f40824e44f7c4a819746c46d811faa5745753b17ade613e3a648adf8cbf7c1b03b",
    "route_type_int": 0,
    "transport_code1": 18583,
    "payload_type_int": 5,
    "scope_payload_hex": (
        "ca37f40824e44f7c4a819746c46d811faa5745753b17ade613e3a648adf8cbf7c1b03b"
    ),
}

SNOCO_PING_REPLY_HEX = (
    "14337a000000cac8cd8b8e7c8b96d9fc79040f518a4a1c24b536e7f27a6ad4fcd500d6376d3e182e01"
)

SNOCO_WX_TRIGGER_HEX = (
    "149fad0000020901ca4941e45582364d4849dbb657e3ca1b2b0627b8dc2dc271869b8a503608d92d64ceb5"
)

SNOCO_WX_REPLY_HEX = (
    "15040901217aca42f8b41fecd26a6c994258382397075a008badd92461435d93823e58e02e8ff23665bf169365a21"
    "accb3b3afe36b66176357d77c699f0d5f3be52d1e84dd333d23e381eeebc110ca6d2caa5eac1d11a20cf92093ee0fda693df9639a911e4"
    "c0ef0486c828e24a351a29f3bc27fd8778968ce62dfe8d275e70fd8f9893dc18773ea519bda9e7c51da9b95451d8812756bbc07"
)

STALE_ADVERT_RF = {
    "timestamp": 0,
    "packet_prefix": "1ce11104c77ee01e72dcb234ee207ce1",
    "pubkey_prefix": None,
    "snr": 7.0,
    "rssi": -31,
    "raw_hex": (
        "1ce11104c77ee01e72dcb234ee207ce10f6bc1ce6b21773ffc077c44b15a7d9f48c1793b12a527198a910b6a65806c520a5583733"
        "f9c4091b912c35c21f01d8a41d7a2f4f341401077248ed0354f65de7ac8594ab243238c5e10ecc641c3e18dee632b4371eef9a3f3e8"
        "ed09923d2cd50204fbb9f8224953512d494d532d5270747222"
    ),
    "route_type_int": 1,
    "transport_code1": None,
    "payload_type_int": 4,
    "scope_payload_hex": "",
}


def _scope_key(name: str) -> bytes:
    return sha256(name.encode()).digest()[:16]


@pytest.fixture
def mh() -> MessageHandler:
    handler = object.__new__(MessageHandler)
    handler.logger = Mock()
    handler.rf_data_timeout = 15.0
    handler.enhanced_correlation = False
    handler.recent_rf_data = []
    handler.pending_messages = {}
    return handler


class TestScopeEligibleRfData:
    def test_snoco_ping_rf_is_scope_eligible(self, mh: MessageHandler):
        assert mh._is_rf_data_scope_eligible(SNOCO_PING_RF) is True

    def test_stale_advert_rf_is_not_scope_eligible(self, mh: MessageHandler):
        assert mh._is_rf_data_scope_eligible(STALE_ADVERT_RF) is False

    def test_find_recent_scope_fallback_skips_advert(self, mh: MessageHandler):
        advert = _fresh_rf(STALE_ADVERT_RF)
        ping = _fresh_rf(SNOCO_PING_RF)
        mh.recent_rf_data = [advert, ping]
        result = mh.find_recent_rf_data(scope_eligible_only=True)
        assert result is ping

    def test_find_recent_scope_fallback_returns_none_without_tc_flood(self, mh: MessageHandler):
        mh.recent_rf_data = [_fresh_rf(STALE_ADVERT_RF)]
        assert mh.find_recent_rf_data(scope_eligible_only=True) is None


class TestSnocoPingScopeMatch:
    def test_ping_rf_resolves_reply_scope_snoco(self, mh: MessageHandler):
        scope_keys = {"#snoco": _scope_key("#snoco")}
        packet_info = mh.decode_meshcore_packet(SNOCO_PING_RF["payload"])
        reply_scope = mh._resolve_reply_scope_from_rf_data(
            SNOCO_PING_RF, packet_info, scope_keys
        )
        assert reply_scope == "#snoco"

    def test_raw_wrapper_decode_does_not_invalidate_scope_eligibility(self, mh: MessageHandler):
        """Decoding RF raw_hex without inner packet must not poison allowlist checks."""
        bad_packet_info = mh.decode_meshcore_packet(SNOCO_PING_RF["raw_hex"])
        assert bad_packet_info is not None
        assert bad_packet_info.get("route_type_name") != "TRANSPORT_FLOOD"
        assert mh._is_rf_data_scope_eligible(SNOCO_PING_RF, bad_packet_info) is True

    def test_scope_payload_hex_is_not_a_decodable_packet(self, mh: MessageHandler):
        assert mh.decode_meshcore_packet(SNOCO_PING_RF["scope_payload_hex"]) is None

    def test_ping_reply_on_air_is_transport_flood_snoco(self, mh: MessageHandler):
        info = mh.decode_meshcore_packet(SNOCO_PING_REPLY_HEX)
        assert info is not None
        assert info.get("route_type_name") == "TRANSPORT_FLOOD"
        tc1 = (info.get("transport_codes") or {}).get("code1")
        payload = bytes.fromhex(info.get("payload_hex") or "")
        assert MessageHandler._match_scope(tc1, 5, payload, {"#snoco": _scope_key("#snoco")}) == "#snoco"

    def test_wx_trigger_hex_is_scoped_snoco(self, mh: MessageHandler):
        info = mh.decode_meshcore_packet(SNOCO_WX_TRIGGER_HEX)
        assert info.get("route_type_name") == "TRANSPORT_FLOOD"
        tc1 = (info.get("transport_codes") or {}).get("code1")
        payload = bytes.fromhex(info.get("payload_hex") or "")
        assert tc1 == make_transport_code("#snoco", 5, payload)

    def test_wx_reply_hex_is_global_flood(self, mh: MessageHandler):
        info = mh.decode_meshcore_packet(SNOCO_WX_REPLY_HEX)
        assert info.get("route_type_name") == "FLOOD"
        assert not info.get("has_transport_codes")


class TestStaleAdvertScopeCorrelation:
    """Wx failure mode: ADVERT is most recent but scope lookup must use TC_FLOOD row."""

    def test_scope_correlation_finds_ping_not_advert(self, mh: MessageHandler):
        ping = _fresh_rf(SNOCO_PING_RF)
        advert = _fresh_rf(STALE_ADVERT_RF)
        advert["timestamp"] = ping["timestamp"] + 1.0
        mh.recent_rf_data = [ping, advert]
        path_rf = mh.find_recent_rf_data()
        scope_rf = mh.find_recent_rf_data(scope_eligible_only=True)
        assert path_rf is advert
        assert scope_rf is ping

    def test_resolve_scope_from_advert_returns_none(self, mh: MessageHandler):
        scope_keys = {"#snoco": _scope_key("#snoco")}
        packet_info = mh.decode_meshcore_packet(STALE_ADVERT_RF["raw_hex"])
        assert (
            mh._resolve_reply_scope_from_rf_data(STALE_ADVERT_RF, packet_info, scope_keys)
            is None
        )


class TestCommandManagerOutboundScopeLogging:
    def test_warning_when_global_despite_override(self):
        cm = object.__new__(CommandManager)
        cm.bot = Mock()
        cm.bot.config = Mock()
        cm.bot.config.has_section = Mock(return_value=True)
        cm.bot.config.has_option = Mock(return_value=True)
        cm.bot.config.get = Mock(
            side_effect=lambda section, key, fallback=None: (
                "#snoco" if key == "outgoing_flood_scope_override" else fallback
            )
        )
        cm.logger = Mock()
        assert cm._outgoing_flood_scope_override() == "#snoco"
