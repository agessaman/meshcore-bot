#!/usr/bin/env python3
"""Authenticated MeshCore time-sync source protocol helpers.

The repeater firmware verifies binary ``Tv1`` group datagrams.  This module
keeps the signing scope and wire serialisation in one small place so service
code cannot accidentally send JSON, text, or host-endian structures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .config_validation import PUBLIC_CHANNEL_KEY_HEX

TIME_SYNC_MARKER = b"Tv1"
TIME_SYNC_CANONICAL_PREFIX = "MeshCore-Time-v1"
TIME_SYNC_DATA_TYPE = 0x0121
TIME_SYNC_MIN_TIMESTAMP = 1715770351
TIME_SYNC_MAX_TIMESTAMP = 2147483647
TIME_SYNC_MAX_DISPLAY_NAME_BYTES = 20
TIME_SYNC_SIGNATURE_BYTES = 64
TIME_SYNC_MAX_PAYLOAD_BYTES = (
    len(TIME_SYNC_MARKER)
    + 4
    + 2
    + 1
    + TIME_SYNC_MAX_DISPLAY_NAME_BYTES
    + TIME_SYNC_SIGNATURE_BYTES
)


class TimeSyncError(ValueError):
    """Raised when time-sync configuration or payload material is invalid."""


@dataclass(frozen=True)
class TimeSyncPrivateKey:
    """Parsed Ed25519 private key material.

    ``expanded`` means the 64-byte orlp-style key used by MeshCore tooling:
    32-byte scalar followed by 32-byte prefix. ``seed`` means an RFC 8032
    32-byte Ed25519 seed accepted by PyNaCl's ``SigningKey``.
    """

    raw: bytes
    kind: str


def normalise_hex(value: str, *, expected_bytes: int | None = None, field_name: str = "hex") -> str:
    """Return lowercase hex after removing common separators and validating length."""
    cleaned = "".join(value.replace("0x", "").replace("0X", "").split()).lower()
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise TimeSyncError(f"{field_name} must be valid hexadecimal") from exc
    if expected_bytes is not None and len(raw) != expected_bytes:
        raise TimeSyncError(f"{field_name} must be {expected_bytes} bytes")
    return cleaned


def parse_private_key_hex(value: str) -> TimeSyncPrivateKey:
    """Parse a configured Ed25519 private key from hex."""
    cleaned = normalise_hex(value, field_name="private_key")
    raw = bytes.fromhex(cleaned)
    if len(raw) == 64:
        return TimeSyncPrivateKey(raw=raw, kind="expanded")
    if len(raw) == 32:
        return TimeSyncPrivateKey(raw=raw, kind="seed")
    raise TimeSyncError("private_key must be either 32-byte seed or 64-byte expanded Ed25519 hex")


def read_private_key_file(path: str) -> TimeSyncPrivateKey:
    """Read a private key from a text file containing hex material."""
    try:
        raw_text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TimeSyncError(f"could not read private key file: {exc}") from exc
    return parse_private_key_hex(raw_text)


def validate_display_name(display_name: str) -> bytes:
    """Validate and return exact UTF-8 bytes for the configured display name."""
    display_bytes = display_name.encode("utf-8")
    if not display_bytes:
        raise TimeSyncError("display_name must not be empty")
    if b"\r" in display_bytes or b"\n" in display_bytes:
        raise TimeSyncError("display_name must not contain carriage return or line feed")
    if len(display_bytes) > TIME_SYNC_MAX_DISPLAY_NAME_BYTES:
        raise TimeSyncError("display_name must be 20 bytes or fewer")
    return display_bytes


def validate_timestamp(timestamp: int) -> int:
    """Validate firmware-supported unsigned Unix timestamp bounds."""
    if not isinstance(timestamp, int):
        raise TimeSyncError("timestamp must be an integer")
    if timestamp < TIME_SYNC_MIN_TIMESTAMP or timestamp > TIME_SYNC_MAX_TIMESTAMP:
        raise TimeSyncError(
            f"timestamp must be between {TIME_SYNC_MIN_TIMESTAMP} and {TIME_SYNC_MAX_TIMESTAMP}"
        )
    return timestamp


def validate_sequence(sequence: int) -> int:
    """Validate uint16 sequence number."""
    if not isinstance(sequence, int):
        raise TimeSyncError("sequence must be an integer")
    if sequence < 0 or sequence > 0xFFFF:
        raise TimeSyncError("sequence must be a uint16 value")
    return sequence


def derive_hashtag_channel_secret(channel_name: str) -> bytes:
    """Derive the MeshCore raw 16-byte secret for a hashtag channel."""
    if not channel_name.startswith("#"):
        channel_name = "#" + channel_name
    return hashlib.sha256(channel_name.lower().encode("utf-8")).digest()[:16]


def resolve_channel_secret(channel_name: str, channel_key_hex: str | None = None) -> bytes:
    """Resolve the raw 16-byte MeshCore channel secret for signing scope.

    Public has a fixed MeshCore key. Hashtag channels are derived exactly as the
    firmware derives them. Custom/private channels require the fetched
    ``channel_key_hex`` from the radio/cache so the signature binds to the real
    configured channel secret.
    """
    if channel_name.strip().lstrip("#").lower() == "public":
        return bytes.fromhex(PUBLIC_CHANNEL_KEY_HEX)
    if channel_name.startswith("#"):
        return derive_hashtag_channel_secret(channel_name)
    if not channel_key_hex:
        raise TimeSyncError("custom channel requires a configured 16-byte channel key")
    return bytes.fromhex(normalise_hex(channel_key_hex, expected_bytes=16, field_name="channel_key_hex"))


def derive_time_sync_channel_id(channel_secret_16: bytes) -> bytes:
    """Return SHA-256(raw 16-byte channel secret), used in signed bytes."""
    if len(channel_secret_16) != 16:
        raise TimeSyncError("channel secret must be exactly 16 bytes")
    return hashlib.sha256(channel_secret_16).digest()


def build_time_sync_canonical(
    channel_id: bytes,
    display_name: str,
    timestamp: int,
    sequence: int,
) -> bytes:
    """Build the exact canonical ASCII/UTF-8 byte string covered by Ed25519."""
    if len(channel_id) != 32:
        raise TimeSyncError("channel_id must be exactly 32 bytes")
    validate_display_name(display_name)
    timestamp = validate_timestamp(timestamp)
    sequence = validate_sequence(sequence)
    return (
        f"{TIME_SYNC_CANONICAL_PREFIX}\n"
        f"{channel_id.hex()}\n"
        f"{display_name}\n"
        f"{timestamp}\n"
        f"{sequence}\n"
    ).encode("utf-8")


def _require_nacl():
    """Import PyNaCl lazily so config validation can run without crypto loaded."""
    try:
        import nacl.bindings
        import nacl.signing
    except ImportError as exc:
        raise TimeSyncError("PyNaCl is required for time-sync Ed25519 signing") from exc
    return nacl


def derive_public_key(private_key: TimeSyncPrivateKey) -> bytes:
    """Derive the 32-byte Ed25519 public key for repeater pinning."""
    nacl = _require_nacl()
    if private_key.kind == "expanded":
        return nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(private_key.raw[:32])
    if private_key.kind == "seed":
        return bytes(nacl.signing.SigningKey(private_key.raw).verify_key)
    raise TimeSyncError(f"unsupported private key kind: {private_key.kind}")


def sign_time_sync(private_key: TimeSyncPrivateKey, public_key: bytes, canonical: bytes) -> bytes:
    """Sign canonical bytes and return a raw 64-byte Ed25519 signature."""
    nacl = _require_nacl()
    if len(public_key) != 32:
        raise TimeSyncError("public_key must be exactly 32 bytes")

    if private_key.kind == "seed":
        signature = nacl.signing.SigningKey(private_key.raw).sign(canonical).signature
    elif private_key.kind == "expanded":
        from .service_plugins.packet_capture_utils import ed25519_sign_with_expanded_key

        signature = ed25519_sign_with_expanded_key(
            canonical,
            private_key.raw[:32],
            private_key.raw[32:],
            public_key,
        )
    else:
        raise TimeSyncError(f"unsupported private key kind: {private_key.kind}")

    if len(signature) != TIME_SYNC_SIGNATURE_BYTES:
        raise TimeSyncError("Ed25519 signature must be exactly 64 bytes")
    return signature


def build_time_sync_payload(
    display_name: str,
    timestamp: int,
    sequence: int,
    signature: bytes,
) -> bytes:
    """Build binary ``Tv1`` payload bytes for the group datagram data field."""
    display_bytes = validate_display_name(display_name)
    timestamp = validate_timestamp(timestamp)
    sequence = validate_sequence(sequence)
    if len(signature) != TIME_SYNC_SIGNATURE_BYTES:
        raise TimeSyncError("signature must be exactly 64 raw bytes")

    payload = (
        TIME_SYNC_MARKER
        + timestamp.to_bytes(4, byteorder="little", signed=False)
        + sequence.to_bytes(2, byteorder="little", signed=False)
        + bytes([len(display_bytes)])
        + display_bytes
        + signature
    )
    if len(payload) > TIME_SYNC_MAX_PAYLOAD_BYTES:
        raise TimeSyncError("time-sync payload exceeds supported maximum length")
    return payload


def build_time_sync_group_datagram_data(tv1_payload: bytes) -> bytes:
    """Wrap ``Tv1`` bytes in MeshCore's group-datagram plaintext structure."""
    if len(tv1_payload) > 0xFF:
        raise TimeSyncError("group datagram data field must fit in one byte")
    return TIME_SYNC_DATA_TYPE.to_bytes(2, byteorder="little") + bytes([len(tv1_payload)]) + tv1_payload


def next_sequence(sequence: int) -> int:
    """Return uint16 sequence increment with firmware-supported rollover."""
    return (validate_sequence(sequence) + 1) & 0xFFFF
