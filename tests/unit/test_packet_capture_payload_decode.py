"""Tests for MeshCore payload decoding (issues #197 & #35).

Covers the standalone decoder (GRP_TXT decryption, ADVERT parsing, key store),
the service's nested ``decoded`` object, the per-broker include_decoded toggle,
and packet-log rotation.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
from unittest.mock import MagicMock

from modules.meshcore_payload_decode import (
    DEFAULT_PUBLIC_CHANNEL_KEY,
    ChannelKeyStore,
    channel_hash_for_key,
    decode_payload,
    decrypt_group_text,
    derive_hashtag_key,
    parse_advert,
)
from modules.service_plugins.packet_capture_service import (
    PacketCaptureService,
    _decode_key_str,
    _parse_size,
    _RotatingPacketLog,
)

# Known GRP_TXT vector from michaelhart/meshcore-decoder tests.
# raw = header(0x15) + path_len(0x40=0 hops) + payload; key is the "#bot" channel key.
BOT_KEY = bytes.fromhex("eb50a1bcb3e4e5d7bf69a57c9dada211")
GRP_RAW = "1540cab3b15626481a5ba64247ab25766e410b026e0678a32da9f0c3946fae5b714cab170f"
GRP_PAYLOAD = bytes.fromhex(GRP_RAW[4:])  # after header + path bytes


# --------------------------------------------------------------------------- #
# Standalone decoder
# --------------------------------------------------------------------------- #
def test_decode_group_text_decrypts_known_vector():
    store = ChannelKeyStore()
    store.add_secret(BOT_KEY, "#bot")
    result = decode_payload(5, GRP_PAYLOAD, store)
    assert result["kind"] == "GRP_TXT"
    assert result["decrypted"] is True
    assert result["channel"] == "#bot"
    assert result["sender"] == "Howl 👾"
    assert result["text"] == "prefix 0101"
    assert result["msg_timestamp"].endswith("Z")


def test_group_text_mac_reject_without_key():
    result = decode_payload(5, GRP_PAYLOAD, ChannelKeyStore())
    assert result["decrypted"] is False
    assert "text" not in result


def test_group_text_wrong_key_fails_mac():
    # A key with the same channel hash bucket is not guaranteed; verify a
    # deliberately wrong key does not decrypt.
    store = ChannelKeyStore()
    wrong = bytes(16)  # all-zero key
    # Force it into the same hash bucket so keys_for() returns it.
    store._by_hash[f"{GRP_PAYLOAD[0]:02x}"] = [(wrong, "wrong")]
    result = decode_payload(5, GRP_PAYLOAD, store)
    assert result["decrypted"] is False


def test_channel_hash_and_keystore_collision():
    store = ChannelKeyStore()
    store.add_secret(BOT_KEY, "#bot")
    h = channel_hash_for_key(BOT_KEY)
    assert store.has(h)
    # Add a second (wrong) key under the same hash bucket -> both tried.
    store._by_hash[h].append((bytes(16), "decoy"))
    assert len(store.keys_for(h)) == 2
    # Decryption still succeeds because the real key is tried.
    assert decode_payload(5, GRP_PAYLOAD, store)["decrypted"] is True


def test_derive_hashtag_key_matches_channel_manager():
    from modules.channel_manager import ChannelManager

    assert derive_hashtag_key("bot") == ChannelManager.generate_hashtag_key("#bot")
    assert derive_hashtag_key("#bot") == derive_hashtag_key("bot")


def test_default_public_key_hash_is_fixed_constant():
    # The default Public key is NOT the hashtag derivation of "#public".
    assert derive_hashtag_key("public") != DEFAULT_PUBLIC_CHANNEL_KEY
    assert channel_hash_for_key(DEFAULT_PUBLIC_CHANNEL_KEY) == "11"


def test_decrypt_group_text_rejects_unaligned_ciphertext():
    assert decrypt_group_text(b"\x00" * 15, b"\x00\x00", BOT_KEY) is None


def test_txt_msg_marked_not_decryptable():
    result = decode_payload(2, b"\x00" * 20, None)
    assert result["kind"] == "TXT_MSG"
    assert result["encrypted"] is True


def test_parse_advert_extracts_fields():
    pub_key = bytes(range(32))
    timestamp = (1_700_000_000).to_bytes(4, "little")
    signature = bytes(64)
    # flags: name (0x80) | latlon (0x10) | chat (0x01)
    flags = bytes([0x91])
    lat = (47_600_000).to_bytes(4, "little", signed=True)
    lon = (-122_300_000 & 0xFFFFFFFF).to_bytes(4, "little")
    name = b"TestNode"
    payload = pub_key + timestamp + signature + flags + lat + lon + name

    advert = parse_advert(payload)
    assert advert["kind"] == "ADVERT"
    assert advert["advert_parse_ok"] is True
    assert advert["mode"] == "Companion"
    assert advert["name"] == "TestNode"
    assert advert["lat"] == 47.6
    assert advert["public_key"] == pub_key.hex()


# --------------------------------------------------------------------------- #
# Service integration
# --------------------------------------------------------------------------- #
def _service_for_format(decode_payloads: bool) -> PacketCaptureService:
    svc = object.__new__(PacketCaptureService)
    svc.decode_payloads = decode_payloads
    svc.debug = False
    svc.logger = logging.getLogger("test-packet-capture")
    svc.bot = None
    # _format_packet_data resolves the reported origin through _get_observer_name,
    # which falls back to _get_bot_name; stub the entry point so these tests stay
    # about payload decoding and not name resolution.
    svc._get_observer_name = lambda: "TestBot"  # type: ignore[method-assign]
    if decode_payloads:
        svc.channel_key_store = ChannelKeyStore()
        svc.channel_key_store.add_secret(BOT_KEY, "#bot")
    else:
        svc.channel_key_store = None
    return svc


def _grp_packet_info() -> dict:
    return {
        "route_type": "FLOOD",
        "payload_type": "GRP_TXT",
        "payload_type_value": 5,
        "path": [],
        "path_byte_length": 0,
        "payload_hex": GRP_PAYLOAD.hex(),
        "payload_bytes": len(GRP_PAYLOAD),
        "packet_hash": "ABCDEF0123456789",
    }


def test_format_packet_data_attaches_decoded():
    svc = _service_for_format(True)
    out = PacketCaptureService._format_packet_data(
        svc, GRP_RAW, _grp_packet_info(), {"snr": 5, "rssi": -100}, None
    )
    assert "decoded" in out
    assert out["decoded"]["text"] == "prefix 0101"
    assert out["decoded"]["sender"] == "Howl 👾"
    # Header fields are not restated inside decoded (no redundancy with top level)
    assert "type_label" not in out["decoded"]
    assert "route_label" not in out["decoded"]


def test_decoded_path_only_when_not_at_top_level():
    svc = _service_for_format(True)

    # Flood route with hops: top level has no "path", so decoded carries it.
    flood = _grp_packet_info()
    flood["path"] = ["F8DA", "7A2A"]
    out = PacketCaptureService._format_packet_data(
        svc, GRP_RAW, flood, {"snr": 5, "rssi": -100}, None
    )
    assert "path" not in out  # top-level path is route=D only
    assert out["decoded"]["path"] == ["F8DA", "7A2A"]

    # Direct route with hops: top level has "path", so decoded does NOT duplicate it.
    direct = _grp_packet_info()
    direct["route_type"] = "DIRECT"
    direct["path"] = ["AB", "CD"]
    out = PacketCaptureService._format_packet_data(
        svc, GRP_RAW, direct, {"snr": 5, "rssi": -100}, None
    )
    assert out["path"] == "AB,CD"
    assert "path" not in out["decoded"]


def test_format_packet_data_no_decoded_when_disabled():
    svc = _service_for_format(False)
    out = PacketCaptureService._format_packet_data(
        svc, GRP_RAW, _grp_packet_info(), {"snr": 5, "rssi": -100}, None
    )
    assert "decoded" not in out


def _bot_from_ini(ini: str) -> MagicMock:
    cp = configparser.ConfigParser()
    cp.read_string(ini.strip())
    bot = MagicMock()
    bot.config = cp
    return bot


def test_per_broker_include_decoded_flag_parsing():
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        include_decoded = true
        mqtt1_server = full.example
        mqtt2_server = raw-only.example
        mqtt2_include_decoded = false
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    svc.include_decoded = True
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert brokers[0]["include_decoded"] is True
    assert brokers[1]["include_decoded"] is False


def test_include_decoded_defaults_off():
    # With nothing configured, the global default is off and brokers inherit it.
    bot = _bot_from_ini(
        """
        [PacketCapture]
        enabled = false
        mqtt1_server = a.example
        mqtt2_server = b.example
        mqtt2_include_decoded = true
        """
    )
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    svc.include_decoded = False
    brokers = PacketCaptureService._parse_mqtt_brokers(svc, bot.config)
    assert brokers[0]["include_decoded"] is False  # inherits global default (off)
    assert brokers[1]["include_decoded"] is True   # explicit opt-in


def test_publish_strips_decoded_per_broker():
    published: dict[str, dict] = {}

    def make_client(name):
        client = MagicMock()

        def publish(topic, payload, qos=0):
            published[name] = json.loads(payload)
            result = MagicMock()
            result.rc = 0
            return result

        client.publish.side_effect = publish
        return client

    svc = object.__new__(PacketCaptureService)
    svc.logger = logging.getLogger("test-publish")
    svc.packet_count = 1
    svc.bot = None
    svc.mqtt_clients = [
        {
            "connected": True,
            "client": make_client("full"),
            "config": {"host": "full", "topic_prefix": "mc/full", "include_decoded": True},
        },
        {
            "connected": True,
            "client": make_client("raw"),
            "config": {"host": "raw", "topic_prefix": "mc/raw", "include_decoded": False},
        },
    ]

    packet = {"type": "PACKET", "packet_type": "5", "raw": "1540AA", "decoded": {"text": "hi"}}
    asyncio.run(PacketCaptureService.publish_packet_mqtt(svc, packet))

    assert "decoded" in published["full"]
    assert "decoded" not in published["raw"]
    assert published["raw"]["raw"] == "1540AA"


# --------------------------------------------------------------------------- #
# Helpers & rotation
# --------------------------------------------------------------------------- #
def test_parse_size_units():
    assert _parse_size("50MB") == 50 * 1024 * 1024
    assert _parse_size("10M") == 10 * 1024 * 1024
    assert _parse_size("1048576") == 1048576
    assert _parse_size("0") == 0
    assert _parse_size("garbage") == 0


def test_decode_key_str_hex_and_base64():
    assert _decode_key_str("eb50a1bcb3e4e5d7bf69a57c9dada211") == BOT_KEY
    assert _decode_key_str("izOH6cXN6mrJ5e26oRXNcg==") == DEFAULT_PUBLIC_CHANNEL_KEY
    assert _decode_key_str("not-a-key") is None


def test_rotating_log_off_single_file(tmp_path):
    path = tmp_path / "packets.jsonl"
    log = _RotatingPacketLog(str(path))
    log.write_line('{"a": 1}')
    log.write_line('{"b": 2}')
    log.close()
    assert path.read_text().splitlines() == ['{"a": 1}', '{"b": 2}']


def test_rotating_log_size_rolls(tmp_path):
    path = tmp_path / "packets.jsonl"
    log = _RotatingPacketLog(str(path), rotation="size", max_bytes=200, backup_count=3)
    for i in range(100):
        log.write_line(json.dumps({"packet": i, "pad": "x" * 20}))
    log.close()
    backups = list(tmp_path.glob("packets.jsonl.*"))
    assert path.exists()
    assert len(backups) >= 1  # rotation produced at least one backup
    assert len(backups) <= 3  # backup_count respected
