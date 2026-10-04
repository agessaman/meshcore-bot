"""Characterization: packet decoding over the committed MQTT packet fixture.

decode_meshcore_packet and calculate_packet_hash moved out of message_handler
during the refactor (checked then by a replay of a private 4,009-packet
capture). This pins their output for the public fixture packets, each also cut
short and at every path prefix width, so later changes show up in review.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from modules.packet_decode import calculate_packet_hash, decode_meshcore_packet
from tests.characterization.golden_util import assert_golden

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mqtt_packets.json"
LOGGER = logging.getLogger("packet_decode_golden")


def _variants(raw: str) -> dict[str, str]:
    return {"full": raw, "half": raw[: len(raw) // 2], "header_only": raw[:4], "odd_length": raw[:-1]}


def test_decode_and_hash_over_fixture_packets():
    packets = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = {}
    for index, packet in enumerate(packets):
        for name, raw in _variants(packet["raw"]).items():
            key = f"{index:02d}-{name}"
            result[key] = {
                str(width): decode_meshcore_packet(raw, prefix_hex_chars=width, logger=LOGGER)
                for width in (2, 4, 6)
            }
            try:
                result[key]["hash"] = calculate_packet_hash(raw)
            except Exception as e:  # recorded, not raised: the contract includes the failure
                result[key]["hash"] = f"{type(e).__name__}: {e}"
    assert_golden("packet_decode_fixture", result)
