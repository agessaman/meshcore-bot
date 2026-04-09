"""Unit tests for decode_meshcore_packet — scope-related fields.

Verifies that decode_meshcore_packet returns the correct fields used by the
scope-matching and allowlist-gate logic in message_handler.py:
  - route_type        (integer: 0=TC_FLOOD, 1=FLOOD)
  - transport_codes   (dict with 'code1' uint16_le, or None for plain FLOOD)
  - payload_type      (integer)
  - payload_hex       (encrypted payload as hex string)

Packet wire format (from decode_meshcore_packet internals):
  header byte  = (payload_version << 6) | (payload_type << 2) | route_type
               PayloadVersion.VER_1 = 0 → bits 7:6 = 0b00
  For TC_FLOOD (route_type=0):
               header + transport[0:4] (code1 LE uint16 + code2 LE uint16)
               + path_len_byte + path_bytes + payload
  For FLOOD    (route_type=1):
               header + path_len_byte + path_bytes + payload

  path_len_byte = 0x00 → hop_count=0, bytes_per_hop=1 → 0 path bytes follow.
"""

import hmac as hmac_mod
from hashlib import sha256
from unittest.mock import MagicMock

from modules.message_handler import MessageHandler

# ── constants ─────────────────────────────────────────────────────────────────

PAYLOAD_TYPE = 5           # GRP_TXT — same value used in scope-matching tests
PKT_PAYLOAD = b"\xde\xad\xbe\xef\x01\x02\x03\x04"  # arbitrary encrypted bytes

_ROUTE_TC_FLOOD = 0        # RouteType.TRANSPORT_FLOOD
_ROUTE_FLOOD    = 1        # RouteType.FLOOD
_PAYLOAD_VER    = 0        # PayloadVersion.VER_1
_PATH_LEN_BYTE  = 0x00    # 0 hops, 1 byte/hop → 0 path bytes follow

# ── helpers ───────────────────────────────────────────────────────────────────

def _scope_key(scope_name: str) -> bytes:
    """16-byte scope key derived from name — same as CommandManager._load_flood_scope_keys."""
    return sha256(scope_name.encode()).digest()[:16]


def make_transport_code(scope_name: str, payload_type: int, pkt_payload: bytes) -> int:
    """Mirror the firmware's calcTransportCode — builds a known-good HMAC transport code."""
    key = _scope_key(scope_name)
    data = bytes([payload_type]) + pkt_payload
    digest = hmac_mod.new(key, data, sha256).digest()
    code = int.from_bytes(digest[:2], "little")
    if code == 0:
        code = 1
    elif code == 0xFFFF:
        code = 0xFFFE
    return code


def _make_header(payload_type: int, route_type: int) -> int:
    """Build the single header byte (payload_version=0, VER_1)."""
    return (_PAYLOAD_VER << 6) | (payload_type << 2) | route_type


def _build_tc_flood_packet(payload_type: int, code1: int, code2: int,
                            payload: bytes) -> str:
    """Build a TC_FLOOD (TRANSPORT_FLOOD) packet as a hex string."""
    header = _make_header(payload_type, _ROUTE_TC_FLOOD)
    transport = code1.to_bytes(2, "little") + code2.to_bytes(2, "little")
    pkt = bytes([header]) + transport + bytes([_PATH_LEN_BYTE]) + payload
    return pkt.hex()


def _build_flood_packet(payload_type: int, payload: bytes) -> str:
    """Build a plain FLOOD packet as a hex string."""
    header = _make_header(payload_type, _ROUTE_FLOOD)
    pkt = bytes([header]) + bytes([_PATH_LEN_BYTE]) + payload
    return pkt.hex()


def _make_mh() -> MessageHandler:
    """Create a minimal MessageHandler instance that can call decode_meshcore_packet."""
    mh = object.__new__(MessageHandler)
    mh.logger = MagicMock()
    return mh


# ── TC_FLOOD tests ────────────────────────────────────────────────────────────

class TestTcFloodPacketDecoding:
    """decode_meshcore_packet extracts scope fields correctly from TC_FLOOD packets."""

    def _decode(self, code1: int = 0x1234, code2: int = 0x0000) -> dict:
        hex_pkt = _build_tc_flood_packet(PAYLOAD_TYPE, code1, code2, PKT_PAYLOAD)
        result = _make_mh().decode_meshcore_packet(hex_pkt)
        assert result is not None, "decode_meshcore_packet returned None — packet may be malformed"
        return result

    def test_tc_flood_route_type_int_is_zero(self):
        """TC_FLOOD packets must have route_type == 0 (TRANSPORT_FLOOD)."""
        result = self._decode()
        assert result['route_type'] == 0

    def test_tc_flood_transport_codes_extracted(self):
        """transport_codes['code1'] must equal the uint16-LE value encoded in the packet."""
        expected_code1 = make_transport_code("#west", PAYLOAD_TYPE, PKT_PAYLOAD)
        hex_pkt = _build_tc_flood_packet(PAYLOAD_TYPE, expected_code1, 0x0000, PKT_PAYLOAD)
        result = _make_mh().decode_meshcore_packet(hex_pkt)
        assert result is not None
        assert result['transport_codes'] is not None
        assert result['transport_codes']['code1'] == expected_code1

    def test_tc_flood_payload_type_int(self):
        """payload_type must equal the integer value encoded in the packet header."""
        result = self._decode()
        assert result['payload_type'] == PAYLOAD_TYPE

    def test_tc_flood_payload_hex_is_encrypted_payload(self):
        """payload_hex must be the hex encoding of the encrypted payload bytes."""
        result = self._decode()
        assert result['payload_hex'] == PKT_PAYLOAD.hex()


# ── plain FLOOD tests ─────────────────────────────────────────────────────────

class TestFloodPacketDecoding:
    """decode_meshcore_packet extracts scope fields correctly from plain FLOOD packets."""

    def _decode(self) -> dict:
        hex_pkt = _build_flood_packet(PAYLOAD_TYPE, PKT_PAYLOAD)
        result = _make_mh().decode_meshcore_packet(hex_pkt)
        assert result is not None, "decode_meshcore_packet returned None — packet may be malformed"
        return result

    def test_flood_route_type_int_is_one(self):
        """Plain FLOOD packets must have route_type == 1 (FLOOD)."""
        result = self._decode()
        assert result['route_type'] == 1

    def test_flood_transport_codes_none(self):
        """Plain FLOOD packets carry no transport codes — transport_codes must be None."""
        result = self._decode()
        assert result['transport_codes'] is None
