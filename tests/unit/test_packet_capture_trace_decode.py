"""Packet capture TRACE decode: path must come from payload, not RF SNR bytes."""

import hashlib
import logging

from modules.service_plugins.packet_capture_service import PacketCaptureService
from modules.utils import calculate_packet_hash


def test_calculate_packet_hash_trace_uses_wire_byte():
    """TRACE hash must use the raw path_len wire byte, not the decoded byte count.

    When size_code > 0 (multi-byte-per-hop paths), path_len_byte != path_byte_length.
    The firmware hashes path_len_byte (the raw wire value), so we must too.
    """
    # header 0x26: route_type=DIRECT(2), payload_type=TRACE(9), version=VER_1(0)
    # path_len_byte 0x42: size_code=1 → 2 bytes/hop, hop_count=2 → path_byte_length=4
    # path: AABBCCDD (4 bytes), payload: 01020304
    raw_hex = "2642AABBCCDD01020304"

    # Correct: use path_len_byte=0x42 (66), matching firmware Packet::calculatePacketHash()
    expected = hashlib.sha256(
        bytes([0x09])
        + (0x42).to_bytes(2, "little")
        + bytes.fromhex("01020304")
    ).hexdigest()[:16].upper()

    result = calculate_packet_hash(raw_hex, 9)
    assert result == expected, f"Expected {expected}, got {result}"

    # Confirm this differs from the old buggy value (path_byte_length=4 instead of wire byte 0x42)
    buggy = hashlib.sha256(
        bytes([0x09])
        + (4).to_bytes(2, "little")
        + bytes.fromhex("01020304")
    ).hexdigest()[:16].upper()
    assert result != buggy, "Hash must not match old path_byte_length computation"


def test_decode_packet_trace_uses_payload_route_hashes():
    # Avoid full PacketCaptureService.__init__ (config/MQTT); only decode_packet needs logger + debug.
    svc = object.__new__(PacketCaptureService)
    svc.logger = logging.getLogger("test_packet_capture_trace_decode")
    svc.debug = False
    raw = "26033128235F0AED1A000000000037D637"
    info = PacketCaptureService.decode_packet(svc, raw, {})
    assert info is not None
    assert info["payload_type"] == "TRACE"
    assert info["path"] == ["37", "D6", "37"]
    assert info["path_len"] == 3
    assert info["trace_snr_path_hex"] == "312823"
