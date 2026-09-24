"""The channel text budget must fit the firmware and fill whole cipher blocks."""

import pytest

from modules.models import CHANNEL_BODY_FLOOR, CHANNEL_FRAME_TEXT_LIMIT, channel_body_limit

# Firmware MAX_TEXT_LEN (BaseChatMesh.h): "<name>: <text>" past this is truncated.
FIRMWARE_MAX_TEXT_LEN = 160
# Firmware encrypts a 4-byte timestamp and a 1-byte text type ahead of the text.
PLAINTEXT_HEADER = 5
CIPHER_BLOCK = 16


def _payload_bytes(framed_len: int) -> int:
    """GRP_TXT payload size: channel hash + 2-byte MAC + padded ciphertext."""
    cipher = -(-(PLAINTEXT_HEADER + framed_len) // CIPHER_BLOCK) * CIPHER_BLOCK
    return 3 + cipher


def test_limit_is_within_the_firmware_text_limit():
    assert CHANNEL_FRAME_TEXT_LIMIT <= FIRMWARE_MAX_TEXT_LEN


def test_limit_fills_its_cipher_block_exactly():
    """One more byte would cost a whole extra block of airtime."""
    assert (PLAINTEXT_HEADER + CHANNEL_FRAME_TEXT_LIMIT) % CIPHER_BLOCK == 0
    assert _payload_bytes(CHANNEL_FRAME_TEXT_LIMIT) == 163
    assert _payload_bytes(CHANNEL_FRAME_TEXT_LIMIT + 1) == 179


@pytest.mark.parametrize("name", ["Bot", "TestBot", "KY-ComchanBot \U0001f916", "\U0001f916" * 4, "A" * 121])
def test_full_body_plus_prefix_fits_the_limit(name):
    framed = len(name.encode("utf-8")) + 2 + channel_body_limit(name)
    assert framed == CHANNEL_FRAME_TEXT_LIMIT


def test_long_name_gets_the_floor():
    assert channel_body_limit("A" * 200) == CHANNEL_BODY_FLOOR


class TestSharedByEveryPath:
    """Three call sites used to carry their own copy of this arithmetic."""

    def test_scheduler_defers_to_the_shared_limit(self):
        import inspect

        from modules import scheduler

        src = inspect.getsource(scheduler.MessageScheduler._channel_body_budget)
        assert "channel_body_limit(" in src
        assert "160 -" not in src

    def test_command_manager_defers_to_the_shared_limit(self):
        import inspect

        from modules import command_manager

        for fn in (
            command_manager.CommandManager.get_max_message_length,
            command_manager.CommandManager.channel_body_budget,
        ):
            src = inspect.getsource(fn)
            assert "channel_body_limit(" in src, fn.__name__
            assert "160 -" not in src, fn.__name__
