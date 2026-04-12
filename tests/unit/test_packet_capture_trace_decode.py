"""Packet capture TRACE decode: path must come from payload, not RF SNR bytes."""

import logging

from modules.service_plugins.packet_capture_service import PacketCaptureService


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
