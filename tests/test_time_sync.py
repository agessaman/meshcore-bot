"""Tests for authenticated time-sync protocol helpers."""

from __future__ import annotations

import hashlib

import pytest

from modules.time_sync import (
    TIME_SYNC_DATA_TYPE,
    TimeSyncError,
    build_time_sync_canonical,
    build_time_sync_group_datagram_data,
    build_time_sync_payload,
    derive_hashtag_channel_secret,
    derive_public_key,
    derive_time_sync_channel_id,
    next_sequence,
    parse_private_key_hex,
    resolve_channel_secret,
    sign_time_sync,
    validate_display_name,
)


VECTOR_PRIVATE_KEY = (
    "7065e18fd9fabb70c1ed90dca19907de"
    "698c88b709ea146eafd93d9b830c7b60"
    "c4681193c79bbc39945ba8064104bb61"
    "8f8fd7a84a0af6f57033d6e8ddcd6471"
)
VECTOR_PUBLIC_KEY = "1ec77175b0918ed206f9ae04ec136d6d5d4315bb26305427f645b492e9350c10"
VECTOR_CHANNEL_SECRET = "5d13043d9a5e61bc61aeb63208f5c64e"
VECTOR_CHANNEL_ID = "e4235ac672871e72d2aeaf8654e0a39893de9fca7c06ccb5c3a788f95a6aeada"
VECTOR_SIGNATURE = (
    "c9e0785bc99e910c813b8984df76c45c"
    "ab7b1b6157594cb710b457eefedd0ede"
    "66f568cbdfc117fb57266283b9d5bbe3"
    "d4ac5d331bbd44b190c14e35bd712108"
)
VECTOR_PAYLOAD = (
    "5476318094536a39300754696d65426f74"
    "c9e0785bc99e910c813b8984df76c45c"
    "ab7b1b6157594cb710b457eefedd0ede"
    "66f568cbdfc117fb57266283b9d5bbe3"
    "d4ac5d331bbd44b190c14e35bd712108"
)
VECTOR_GROUP_DATA = "210151" + VECTOR_PAYLOAD


def test_hashtag_channel_secret_matches_vector():
    assert derive_hashtag_channel_secret("#time").hex() == VECTOR_CHANNEL_SECRET


def test_channel_id_matches_vector():
    channel_secret = bytes.fromhex(VECTOR_CHANNEL_SECRET)
    assert derive_time_sync_channel_id(channel_secret).hex() == VECTOR_CHANNEL_ID


def test_public_channel_uses_documented_fixed_secret():
    assert resolve_channel_secret("Public").hex() == "8b3387e9c5cdea6ac9e5edbaa115cd72"


def test_custom_channel_requires_key():
    with pytest.raises(TimeSyncError, match="custom channel"):
        resolve_channel_secret("private-room")


def test_display_name_validation_rejects_empty_cr_lf_and_long_names():
    for value in ("", "Time\rBot", "Time\nBot", "a" * 21):
        with pytest.raises(TimeSyncError):
            validate_display_name(value)


def test_canonical_signed_bytes_match_vector():
    canonical = build_time_sync_canonical(
        bytes.fromhex(VECTOR_CHANNEL_ID),
        "TimeBot",
        1783862400,
        12345,
    )
    assert canonical == (
        b"MeshCore-Time-v1\n"
        + VECTOR_CHANNEL_ID.encode("ascii")
        + b"\nTimeBot\n1783862400\n12345\n"
    )


def test_binary_payload_serialises_little_endian_and_raw_signature():
    payload = build_time_sync_payload(
        "TimeBot",
        1783862400,
        12345,
        bytes.fromhex(VECTOR_SIGNATURE),
    )
    assert payload.hex() == VECTOR_PAYLOAD
    assert payload[:3] == b"Tv1"
    assert payload[3:7] == bytes.fromhex("8094536a")
    assert payload[7:9] == bytes.fromhex("3930")
    assert len(payload[-64:]) == 64


def test_group_datagram_data_matches_vector():
    wrapped = build_time_sync_group_datagram_data(bytes.fromhex(VECTOR_PAYLOAD))
    assert wrapped.hex() == VECTOR_GROUP_DATA
    assert int.from_bytes(wrapped[:2], "little") == TIME_SYNC_DATA_TYPE
    assert wrapped[2] == 81


def test_sequence_wraps_from_uint16_max_to_zero():
    assert next_sequence(65535) == 0


def test_signature_vector_matches_when_pynacl_available():
    pytest.importorskip("nacl")
    private_key = parse_private_key_hex(VECTOR_PRIVATE_KEY)
    public_key = derive_public_key(private_key)
    assert public_key.hex() == VECTOR_PUBLIC_KEY

    canonical = build_time_sync_canonical(
        bytes.fromhex(VECTOR_CHANNEL_ID),
        "TimeBot",
        1783862400,
        12345,
    )
    assert sign_time_sync(private_key, public_key, canonical).hex() == VECTOR_SIGNATURE


def test_changed_fields_do_not_verify_against_vector_signature_when_pynacl_available():
    pytest.importorskip("nacl")
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError

    public_key = VerifyKey(bytes.fromhex(VECTOR_PUBLIC_KEY))
    signature = bytes.fromhex(VECTOR_SIGNATURE)
    changed_messages = [
        build_time_sync_canonical(
            bytes.fromhex(VECTOR_CHANNEL_ID), "TimeBot", 1783862401, 12345
        ),
        build_time_sync_canonical(
            bytes.fromhex(VECTOR_CHANNEL_ID), "TimeBot", 1783862400, 12346
        ),
        build_time_sync_canonical(
            bytes.fromhex(VECTOR_CHANNEL_ID), "Other", 1783862400, 12345
        ),
        build_time_sync_canonical(
            hashlib.sha256(b"different-secret").digest(), "TimeBot", 1783862400, 12345
        ),
    ]

    for canonical in changed_messages:
        with pytest.raises(BadSignatureError):
            public_key.verify(canonical, signature)
