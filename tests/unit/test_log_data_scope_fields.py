"""Unit tests: verify that meshcore_py parsePacketPayload exposes the fields
needed for scope matching (transport_code, pkt_payload, payload_type) in LOG_DATA events.

These tests exercise the meshcore library's MeshcorePacketParser directly.
"""


import pytest

# Import the meshcore library parser directly
from meshcore.meshcore_parser import MeshcorePacketParser


def make_tc_flood_payload(transport_code_bytes: bytes, payload_type: int,
                          payload: bytes, path: bytes = b"") -> bytes:
    """Build a raw inner MeshCore TC_FLOOD packet (after the SNR/RSSI bytes).

    Header byte: bits 0-1 = 0 (TC_FLOOD), bits 2-5 = payload_type, bits 6-7 = 0 (ver)
    Followed by: 4-byte transport code, path_len_byte, path, payload.
    path_len_byte: bits 0-5 = len(path), bits 6-7 = 0 (→ path_hash_size = 1 byte/hop).
    """
    header = (payload_type & 0x0F) << 2  # route_type=0, payload_type in bits 2-5
    path_len_byte = len(path) & 0x3F     # hop count, hash_size bits = 0 → size = 1
    return bytes([header]) + transport_code_bytes + bytes([path_len_byte]) + path + payload


def make_flood_payload(payload_type: int, payload: bytes, path: bytes = b"") -> bytes:
    """Build a raw inner MeshCore FLOOD packet (route_type=1, no transport code)."""
    header = 0x01 | ((payload_type & 0x0F) << 2)  # route_type=1
    path_len_byte = len(path) & 0x3F
    return bytes([header]) + bytes([path_len_byte]) + path + payload


@pytest.mark.asyncio
async def test_tc_flood_transport_code_hex_exposed():
    parser = MeshcorePacketParser()
    tc_bytes = bytes.fromhex("aabbccdd")
    inner_payload = b"\x01\x02\x03"
    raw = make_tc_flood_payload(tc_bytes, payload_type=5, payload=inner_payload)

    result = await parser.parsePacketPayload(raw)

    assert result.get("transport_code") == "aabbccdd"


@pytest.mark.asyncio
async def test_tc_flood_pkt_payload_is_payload_after_path():
    parser = MeshcorePacketParser()
    tc_bytes = bytes.fromhex("11223344")
    path = bytes([0x7e, 0x86])          # 2-hop path, 1 byte per hop
    inner_payload = b"\xde\xad\xbe\xef"
    raw = make_tc_flood_payload(tc_bytes, payload_type=5, payload=inner_payload, path=path)

    result = await parser.parsePacketPayload(raw)

    assert result.get("pkt_payload") == inner_payload


@pytest.mark.asyncio
async def test_tc_flood_payload_type_field():
    parser = MeshcorePacketParser()
    tc_bytes = b"\x00" * 4
    raw = make_tc_flood_payload(tc_bytes, payload_type=5, payload=b"\xff")

    result = await parser.parsePacketPayload(raw)

    assert result.get("payload_type") == 5


@pytest.mark.asyncio
async def test_flood_no_transport_code():
    """Regular FLOOD packets have route_type=1 and should not match any scope.

    The library always emits transport_code (set to all-zeros for non-TC packets).
    The bot guards against this by checking route_type == 0 before scope matching.
    """
    parser = MeshcorePacketParser()
    raw = make_flood_payload(payload_type=5, payload=b"\xaa\xbb")

    result = await parser.parsePacketPayload(raw)

    assert result.get("route_type") == 1  # FLOOD, not TC_FLOOD
    assert result.get("route_typename") == "FLOOD"
    # Library emits transport_code='00000000' for non-TC packets; bot guards on route_type
    tc = result.get("transport_code", "")
    assert tc == "" or tc == "00000000"


@pytest.mark.asyncio
async def test_tc_flood_route_typename():
    parser = MeshcorePacketParser()
    tc_bytes = b"\x00" * 4
    # Use payload_type=6 (GRP_DATA) — no deep parsing, avoids IndexError on minimal payload
    raw = make_tc_flood_payload(tc_bytes, payload_type=6, payload=b"\x01\x02\x03")

    result = await parser.parsePacketPayload(raw)

    assert result.get("route_typename") == "TC_FLOOD"


@pytest.mark.asyncio
async def test_flood_route_typename():
    parser = MeshcorePacketParser()
    # Use payload_type=6 (GRP_DATA) — no deep parsing, avoids IndexError on minimal payload
    raw = make_flood_payload(payload_type=6, payload=b"\x01\x02\x03")

    result = await parser.parsePacketPayload(raw)

    assert result.get("route_typename") == "FLOOD"
