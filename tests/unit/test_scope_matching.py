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
