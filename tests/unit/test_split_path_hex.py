"""split_path_hex: the shared multi-byte path chunker with its legacy fallback."""

import pytest

from modules.packet_decode import split_path_hex


@pytest.mark.parametrize(
    ("path_hex", "hex_chars", "expected"),
    [
        ("A1B2C3", 2, ["a1", "b2", "c3"]),
        ("A1B2C3D4", 4, ["a1b2", "c3d4"]),
        ("A1B2C3", 4, ["a1", "b2", "c3"]),  # does not divide: one-byte fallback
        ("A1B2C3D4E5F6", 6, ["a1b2c3", "d4e5f6"]),
        ("", 4, []),
    ],
)
def test_split_path_hex(path_hex, hex_chars, expected):
    assert split_path_hex(path_hex, hex_chars) == expected
