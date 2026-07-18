#!/usr/bin/env python3
"""Standalone MeshCore packet-payload decoder.

Decodes the *application payload* of a MeshCore packet into plain-text /
structured fields: GRP_TXT (channel) message decryption, ADVERT parsing, and
light structured fields for other payload types.

This module is intentionally **self-contained** — it depends only on the Python
standard library plus ``cryptography`` (already a project dependency). It does
NOT import any bot-specific modules so that it can be copied verbatim into the
parent project ``meshcore-packet-capture`` (canonical home), mirroring the
existing ``auth_token.py`` <-> ``packet_capture_utils.py`` lineage.

Key sourcing (which channel keys to try) is the host's responsibility: build a
:class:`ChannelKeyStore` from your own config / database and hand it to
:func:`decode_payload`.

GRP_TXT wire format and crypto follow the reference implementation
https://github.com/michaelhart/meshcore-decoder :

    payload = channel_hash(1) + cipher_mac(2) + ciphertext(...)

    channel_hash = first byte of SHA256(channel_key_16)
    MAC          = HMAC_SHA256(key32, ciphertext)[:2], key32 = key16 + 16 zero bytes
    cipher       = AES-128-ECB, NoPadding, key = key16
    plaintext    = timestamp(4, LE u32) + flags(1) + text(UTF-8, NUL-terminated),
                   text usually "sender: message"
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("meshcore_payload_decode")

# --- Protocol constants (mirror modules/enums.py; kept local for portability) ---

# PayloadType values (header bits 2-5). See modules/enums.py:PayloadType.
PT_REQ = 0x00
PT_RESPONSE = 0x01
PT_TXT_MSG = 0x02
PT_ACK = 0x03
PT_ADVERT = 0x04
PT_GRP_TXT = 0x05
PT_GRP_DATA = 0x06
PT_ANON_REQ = 0x07
PT_PATH = 0x08
PT_TRACE = 0x09
PT_MULTIPART = 0x0A
PT_RAW_CUSTOM = 0x0F
TIME_SYNC_DATA_TYPE = 0x0121
TIME_SYNC_MARKER = b"Tv1"
TIME_SYNC_SIGNATURE_BYTES = 64

PAYLOAD_TYPE_NAMES = {
    PT_REQ: "REQ",
    PT_RESPONSE: "RESPONSE",
    PT_TXT_MSG: "TXT_MSG",
    PT_ACK: "ACK",
    PT_ADVERT: "ADVERT",
    PT_GRP_TXT: "GRP_TXT",
    PT_GRP_DATA: "GRP_DATA",
    PT_ANON_REQ: "ANON_REQ",
    PT_PATH: "PATH",
    PT_TRACE: "TRACE",
    PT_MULTIPART: "MULTIPART",
    0x0B: "Type11",
    0x0C: "Type12",
    0x0D: "Type13",
    0x0E: "Type14",
    PT_RAW_CUSTOM: "RAW_CUSTOM",
}

# Advert flag bits (see modules/enums.py:AdvertFlags / C++ AdvertDataHelpers.h)
ADV_TYPE_MASK = 0x0F
ADV_TYPE_CHAT = 0x01
ADV_TYPE_REPEATER = 0x02
ADV_TYPE_ROOM = 0x03
ADV_TYPE_SENSOR = 0x04
ADV_LATLON_MASK = 0x10
ADV_FEAT1_MASK = 0x20
ADV_FEAT2_MASK = 0x40
ADV_NAME_MASK = 0x80

_ADV_TYPE_NAMES = {
    ADV_TYPE_CHAT: "Companion",
    ADV_TYPE_REPEATER: "Repeater",
    ADV_TYPE_ROOM: "RoomServer",
    ADV_TYPE_SENSOR: "Sensor",
}

# The well-known MeshCore default "Public" channel key (base64 izOH6cXN6mrJ5e26oRXNcg==).
# NOTE: this is a fixed constant, NOT the hashtag derivation of "#public"
# (SHA256("#public")[:16] = 8b4b705b... which is different).
DEFAULT_PUBLIC_CHANNEL_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def derive_hashtag_key(name: str) -> bytes:
    """Derive a public/hashtag channel key from its name.

    The key is the first 16 bytes of SHA256 of the lowercased ``#name``.

    NOTE: This duplicates ``modules/channel_manager.py:generate_hashtag_key`` on
    purpose — this module stays dependency-free for portability. If the MeshCore
    derivation ever changes, update both (and the shared test vector).
    """
    if not name.startswith("#"):
        name = "#" + name
    return hashlib.sha256(name.lower().encode("utf-8")).digest()[:16]


def channel_hash_for_key(key16: bytes) -> str:
    """Return the 2-hex channel hash (first byte of SHA256(key)) for a channel key."""
    return f"{hashlib.sha256(key16).digest()[0]:02x}"


class ChannelKeyStore:
    """Maps a channel hash -> candidate 16-byte keys (handles hash collisions)."""

    def __init__(self) -> None:
        # channel_hash (2-hex, lower) -> list of (key16, name)
        self._by_hash: dict[str, list[tuple[bytes, Optional[str]]]] = {}

    def add_secret(self, key16: bytes, name: Optional[str] = None) -> None:
        """Add a raw 16-byte channel key (optionally with a display name)."""
        if not key16 or len(key16) != 16:
            logger.debug("Ignoring channel key with invalid length: %r", key16)
            return
        h = channel_hash_for_key(key16)
        bucket = self._by_hash.setdefault(h, [])
        if any(existing == key16 for existing, _ in bucket):
            return  # de-dup identical keys
        bucket.append((key16, name))

    def add_hex(self, key_hex: str, name: Optional[str] = None) -> None:
        """Add a channel key from a 32-char hex string."""
        try:
            self.add_secret(bytes.fromhex(key_hex.strip()), name)
        except ValueError:
            logger.debug("Ignoring non-hex channel key: %r", key_hex)

    def add_hashtag(self, name: str) -> None:
        """Add a public/hashtag channel by name (key derived from the name)."""
        normalized = name if name.startswith("#") else "#" + name
        self.add_secret(derive_hashtag_key(name), normalized.lower())

    def has(self, channel_hash: str) -> bool:
        return channel_hash.lower() in self._by_hash

    def keys_for(self, channel_hash: str) -> list[tuple[bytes, Optional[str]]]:
        return self._by_hash.get(channel_hash.lower(), [])

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_hash.values())


def decrypt_group_ciphertext(ciphertext: bytes, cipher_mac: bytes, key16: bytes) -> Optional[bytes]:
    """Verify MeshCore channel MAC and decrypt GRP_TXT/GRP_DATA ciphertext."""
    if len(ciphertext) < 16 or len(ciphertext) % 16 != 0:
        return None

    # MAC: HMAC-SHA256 over ciphertext with 32-byte secret (key16 + 16 zero bytes)
    key32 = key16 + b"\x00" * 16
    calc_mac = hmac.new(key32, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(calc_mac[:2], cipher_mac[:2]):
        return None

    # Decrypt: AES-128-ECB, no padding
    try:
        decryptor = Cipher(algorithms.AES(key16), modes.ECB(), backend=default_backend()).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("AES decrypt failed: %s", e)
        return None

    return plaintext


def decrypt_group_text(ciphertext: bytes, cipher_mac: bytes, key16: bytes) -> Optional[dict[str, Any]]:
    """Verify+decrypt a GRP_TXT ciphertext with a single channel key.

    Returns ``{timestamp, flags, sender, text}`` on success, or ``None`` if the
    MAC fails or the plaintext is malformed.
    """
    plaintext = decrypt_group_ciphertext(ciphertext, cipher_mac, key16)
    if plaintext is None:
        return None

    if len(plaintext) < 5:
        return None

    timestamp = int.from_bytes(plaintext[0:4], "little")
    flags = plaintext[4]

    text = plaintext[5:].decode("utf-8", errors="ignore")
    nul = text.find("\x00")
    if nul >= 0:
        text = text[:nul]

    # Split "sender: message" when the prefix looks like a name
    sender: Optional[str] = None
    content = text
    colon = text.find(": ")
    if 0 < colon < 50:
        candidate = text[:colon]
        if not any(c in candidate for c in ":[]"):
            sender = candidate
            content = text[colon + 2:]

    return {"timestamp": timestamp, "flags": flags, "sender": sender, "text": content}


def _iso_utc(unix_ts: int) -> Optional[str]:
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def decode_group_text(payload: bytes, key_store: Optional[ChannelKeyStore]) -> dict[str, Any]:
    """Decode (and, if a key matches, decrypt) a GRP_TXT payload."""
    if len(payload) < 3:
        return {"kind": "GRP_TXT", "decrypted": False, "error": "payload_too_short"}

    channel_hash = f"{payload[0]:02x}"
    cipher_mac = payload[1:3]
    ciphertext = payload[3:]

    result: dict[str, Any] = {
        "kind": "GRP_TXT",
        "channel_hash": channel_hash,
        "cipher_mac": cipher_mac.hex(),
        "ciphertext_len": len(ciphertext),
        "decrypted": False,
    }

    if key_store and key_store.has(channel_hash):
        for key16, name in key_store.keys_for(channel_hash):
            decrypted = decrypt_group_text(ciphertext, cipher_mac, key16)
            if decrypted:
                result["decrypted"] = True
                result["channel"] = name
                result["sender"] = decrypted["sender"]
                result["text"] = decrypted["text"]
                result["flags"] = decrypted["flags"]
                result["msg_timestamp"] = _iso_utc(decrypted["timestamp"])
                break

    return result


def parse_time_sync_payload(data: bytes) -> dict[str, Any] | None:
    """Parse a MeshCore-Time-v1 ``Tv1`` application payload."""
    if not data.startswith(TIME_SYNC_MARKER) or len(data) < 10 + TIME_SYNC_SIGNATURE_BYTES:
        return None

    timestamp = int.from_bytes(data[3:7], "little", signed=False)
    sequence = int.from_bytes(data[7:9], "little", signed=False)
    name_len = data[9]
    name_start = 10
    name_end = name_start + name_len
    sig_end = name_end + TIME_SYNC_SIGNATURE_BYTES
    if name_end > len(data) or sig_end > len(data):
        return None

    identity_name = data[name_start:name_end].decode("utf-8", errors="replace")
    signature = data[name_end:sig_end]
    return {
        "kind": "TIME_SYNC",
        "format": "MeshCore-Time-v1",
        "marker": TIME_SYNC_MARKER.decode("ascii"),
        "timestamp": timestamp,
        "timestamp_iso": _iso_utc(timestamp),
        "sequence": sequence,
        "identity_name": identity_name,
        "signature_len": len(signature),
        "signature_fingerprint": hashlib.sha256(signature).hexdigest()[:16],
    }


def decode_group_data(payload: bytes, key_store: Optional[ChannelKeyStore]) -> dict[str, Any]:
    """Decode and, if a key matches, decrypt a GRP_DATA payload."""
    if len(payload) < 3:
        return {"kind": "GRP_DATA", "decrypted": False, "error": "payload_too_short"}

    channel_hash = f"{payload[0]:02x}"
    cipher_mac = payload[1:3]
    ciphertext = payload[3:]

    result: dict[str, Any] = {
        "kind": "GRP_DATA",
        "channel_hash": channel_hash,
        "cipher_mac": cipher_mac.hex(),
        "ciphertext_len": len(ciphertext),
        "decrypted": False,
    }

    if key_store and key_store.has(channel_hash):
        for key16, name in key_store.keys_for(channel_hash):
            decrypted = decrypt_group_ciphertext(ciphertext, cipher_mac, key16)
            if decrypted is None or len(decrypted) < 3:
                continue

            data_type = int.from_bytes(decrypted[0:2], "little", signed=False)
            data_len = decrypted[2]
            data_end = 3 + data_len
            if data_end > len(decrypted):
                continue

            app_data = decrypted[3:data_end]
            result.update(
                {
                    "decrypted": True,
                    "channel": name,
                    "data_type": data_type,
                    "data_type_hex": f"0x{data_type:04x}",
                    "data_len": data_len,
                    "data_hex": app_data.hex(),
                }
            )

            if data_type == TIME_SYNC_DATA_TYPE:
                time_sync = parse_time_sync_payload(app_data)
                if time_sync:
                    result["application"] = time_sync
            break

    return result


def parse_advert(payload: bytes) -> dict[str, Any]:
    """Parse an ADVERT payload (port of meshcore-packet-capture parse_advert).

    Layout: pub_key(32) + timestamp(4) + signature(64) + app_data(flags + optional
    latlon/feat1/feat2/name).
    """
    result: dict[str, Any] = {"kind": "ADVERT"}
    try:
        if len(payload) < 100:
            result.update({"advert_parse_ok": False, "advert_error": "payload_too_short_header"})
            return result

        result.update(
            {
                "advert_parse_ok": True,
                "public_key": payload[0:32].hex(),
                "advert_time": int.from_bytes(payload[32:36], "little"),
                "signature": payload[36:100].hex(),
            }
        )

        app_data = payload[100:]
        if not app_data:
            return result

        flags_byte = app_data[0]
        adv_type = flags_byte & ADV_TYPE_MASK
        result["mode"] = _ADV_TYPE_NAMES.get(adv_type, f"Type{adv_type}")

        i = 1
        if flags_byte & ADV_LATLON_MASK:
            if len(app_data) < i + 8:
                return result
            lat = int.from_bytes(app_data[i:i + 4], "little", signed=True)
            lon = int.from_bytes(app_data[i + 4:i + 8], "little", signed=True)
            result["lat"] = round(lat / 1000000.0, 6)
            result["lon"] = round(lon / 1000000.0, 6)
            i += 8

        if flags_byte & ADV_FEAT1_MASK:
            if len(app_data) < i + 2:
                return result
            result["feat1"] = int.from_bytes(app_data[i:i + 2], "little")
            i += 2

        if flags_byte & ADV_FEAT2_MASK:
            if len(app_data) < i + 2:
                return result
            result["feat2"] = int.from_bytes(app_data[i:i + 2], "little")
            i += 2

        if flags_byte & ADV_NAME_MASK and len(app_data) > i:
            result["name"] = app_data[i:].decode("utf-8", errors="ignore").rstrip("\x00")

        return result
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Error parsing ADVERT: %s", e)
        result.update({"advert_parse_ok": False, "advert_error": "exception", "advert_error_detail": str(e)})
        return result


def decode_payload(
    payload_type_value: int,
    payload: bytes,
    key_store: Optional[ChannelKeyStore] = None,
) -> dict[str, Any]:
    """Decode a packet's application payload into structured / plain-text fields.

    Args:
        payload_type_value: PayloadType (header bits 2-5), e.g. 5 for GRP_TXT.
        payload: The application payload bytes (after header/transport/path).
        key_store: Optional channel keys used to decrypt GRP_TXT messages.

    Returns:
        A dict describing the decoded payload. Always contains ``kind``.
    """
    if payload_type_value == PT_GRP_TXT:
        return decode_group_text(payload, key_store)
    if payload_type_value == PT_GRP_DATA:
        return decode_group_data(payload, key_store)
    if payload_type_value == PT_ADVERT:
        return parse_advert(payload)
    if payload_type_value == PT_TXT_MSG:
        # Direct messages are ECDH-encrypted between two nodes; a passive
        # observer cannot decrypt them.
        return {
            "kind": "TXT_MSG",
            "encrypted": True,
            "note": "direct message; not decryptable by observer",
        }
    if payload_type_value == PT_ACK:
        return {"kind": "ACK", "ack": payload.hex()}

    return {"kind": PAYLOAD_TYPE_NAMES.get(payload_type_value, f"Type{payload_type_value}")}
