"""Unit tests for TC_FLOOD scope matching via HMAC-SHA256.

_match_scope mirrors the firmware's TransportKey::calcTransportCode:
  HMAC-SHA256(scope_key, payload_type_byte || pkt_payload)[0:2] as uint16_le

make_transport_code is the same computation used to generate known-good test inputs.
"""

import hmac as hmac_mod
from hashlib import sha256

from modules.message_handler import MessageHandler


def _scope_key(scope_name: str) -> bytes:
    """16-byte scope key for a named region (auto-hashtag convention)."""
    return sha256(scope_name.encode()).digest()[:16]


def make_transport_code(scope_name: str, payload_type: int, pkt_payload: bytes) -> int:
    """Mirror the firmware's calcTransportCode — used to build test inputs."""
    key = _scope_key(scope_name)
    data = bytes([payload_type]) + pkt_payload
    digest = hmac_mod.new(key, data, sha256).digest()
    code = int.from_bytes(digest[:2], "little")
    if code == 0:
        code = 1
    elif code == 0xFFFF:
        code = 0xFFFE
    return code


PAYLOAD_TYPE = 0x05  # GRP_TXT (channel message)
PAYLOAD = b"\xde\xad\xbe\xef\x01\x02\x03\x04"  # arbitrary encrypted bytes


def test_matching_scope_returned():
    key = _scope_key("#west")
    scope_keys = {"#west": key}
    tc = make_transport_code("#west", PAYLOAD_TYPE, PAYLOAD)
    result = MessageHandler._match_scope(tc, PAYLOAD_TYPE, PAYLOAD, scope_keys)
    assert result == "#west"


def test_non_matching_scope_returns_none():
    key_east = _scope_key("#east")
    scope_keys = {"#east": key_east}
    tc = make_transport_code("#west", PAYLOAD_TYPE, PAYLOAD)
    result = MessageHandler._match_scope(tc, PAYLOAD_TYPE, PAYLOAD, scope_keys)
    assert result is None


def test_empty_scope_map_returns_none():
    tc = make_transport_code("#west", PAYLOAD_TYPE, PAYLOAD)
    assert MessageHandler._match_scope(tc, PAYLOAD_TYPE, PAYLOAD, {}) is None


def test_none_transport_code_returns_none():
    key = _scope_key("#west")
    scope_keys = {"#west": key}
    assert MessageHandler._match_scope(None, PAYLOAD_TYPE, PAYLOAD, scope_keys) is None


def test_correct_scope_selected_from_multiple():
    key_west = _scope_key("#west")
    key_east = _scope_key("#east")
    scope_keys = {"#west": key_west, "#east": key_east}
    tc = make_transport_code("#east", PAYLOAD_TYPE, PAYLOAD)
    result = MessageHandler._match_scope(tc, PAYLOAD_TYPE, PAYLOAD, scope_keys)
    assert result == "#east"


def test_wrong_payload_does_not_match():
    key = _scope_key("#west")
    scope_keys = {"#west": key}
    tc = make_transport_code("#west", PAYLOAD_TYPE, PAYLOAD)
    different_payload = b"\x00\x00\x00\x00"
    result = MessageHandler._match_scope(tc, PAYLOAD_TYPE, different_payload, scope_keys)
    assert result is None


def test_different_payload_type_does_not_match():
    key = _scope_key("#west")
    scope_keys = {"#west": key}
    tc = make_transport_code("#west", PAYLOAD_TYPE, PAYLOAD)
    result = MessageHandler._match_scope(tc, PAYLOAD_TYPE + 1, PAYLOAD, scope_keys)
    assert result is None


def _make_handler_for_scope_resolve():
    from unittest.mock import MagicMock

    mh = object.__new__(MessageHandler)
    mh.logger = MagicMock()
    return mh


def test_resolve_reply_scope_uses_decode_when_cache_route_type_wrong():
    """Stale RF cache (FLOOD) + decoded TC_FLOOD still matches configured scope."""
    scope_name = "#w-wa"
    scope_keys = {scope_name: _scope_key(scope_name)}
    tc = make_transport_code(scope_name, PAYLOAD_TYPE, PAYLOAD)
    packet_info = {
        "route_type": 0,
        "transport_codes": {"code1": tc, "code2": 0},
        "payload_type": PAYLOAD_TYPE,
        "payload_hex": PAYLOAD.hex(),
    }
    recent_rf_data = {
        "route_type_int": 1,
        "transport_code1": None,
        "payload_type_int": PAYLOAD_TYPE,
        "scope_payload_hex": "",
    }
    mh = _make_handler_for_scope_resolve()
    assert mh._resolve_reply_scope_from_rf_data(recent_rf_data, packet_info, scope_keys) == scope_name
    mh.logger.debug.assert_any_call(
        "TC_FLOOD scope fields from packet decode (cache had route_type=%s tc=%s)",
        1,
        None,
    )


def test_effective_route_type_prefers_decode_tc_flood():
    mh = _make_handler_for_scope_resolve()
    recent_rf_data = {"route_type_int": 1}
    packet_info = {"route_type": 0, "transport_codes": {"code1": 1}}
    assert mh._effective_route_type_int(recent_rf_data, packet_info) == 0


def test_scope_fields_from_packet_info_enum_route_type():
    class _EnumVal:
        def __init__(self, value: int):
            self.value = value

    rt, tc, pt, hx = MessageHandler._scope_fields_from_packet_info(
        {
            "route_type": _EnumVal(0),
            "payload_type": _EnumVal(PAYLOAD_TYPE),
            "transport_codes": {"code1": 42},
            "payload_hex": PAYLOAD.hex(),
        }
    )
    assert rt == 0
    assert tc == 42
    assert pt == PAYLOAD_TYPE
    assert hx == PAYLOAD.hex()
