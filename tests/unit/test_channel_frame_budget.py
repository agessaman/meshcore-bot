#!/usr/bin/env python3
"""The channel budget must produce frames the mesh will relay, not merely frames
the local radio accepts.

Measured on a live mesh (analyzer packets b6e4f88b180d2d8a and 0bf2843bce623095,
same channel six seconds apart): 152 bytes of channel text encrypts to a 160-byte
AES block, a 163-byte payload and a 165-byte frame, which one repeater forwarded
and nobody else did. 47 bytes of text reached 15 observers at up to 11 hops. Every
frame seen relaying was 147 bytes or smaller, and the largest payload directly
observed relaying was 131.
"""

import pytest

from modules.models import (
    CHANNEL_BODY_FLOOR,
    CHANNEL_FRAME_TEXT_LIMIT,
    CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD,
    channel_body_limit,
)

# Payload the mesh was observed to relay (7+ hops). A body sized by
# channel_body_limit must never encrypt to more than this.
RELAYING_PAYLOAD_CEILING = 131

BOT_NAMES = [
    "Bot",
    "TestBot",
    "ComchanBot \U0001f916",     # 15 bytes
    "KY-ComchanBot \U0001f916",  # 18 bytes, the live bot
    "\U0001f916" * 4,            # 16 bytes, all multi-byte
]


def _payload_bytes(framed_text: str) -> int:
    """Channel-message payload size for *framed_text*, per the wire format.

    1 channel-hash byte + 2 MAC bytes + AES-128 blocks over a 4-byte timestamp
    and the text.
    """
    cipher = -(-(4 + len(framed_text.encode("utf-8"))) // 16) * 16
    return 3 + cipher


class TestFrameAccounting:
    def test_payload_model_matches_the_observed_packets(self):
        """Anchors the arithmetic to the two real packets before relying on it."""
        # part 1: 152 bytes of text -> 163-byte payload, 165-byte frame at 0 hops
        assert _payload_bytes("x" * 152) == 163
        # part 2: 47 bytes of text -> 67-byte payload, 69-byte frame
        assert _payload_bytes("x" * 47) == 67

    @pytest.mark.parametrize("name", BOT_NAMES)
    def test_body_plus_prefix_never_exceeds_the_frame_limit(self, name):
        body = channel_body_limit(name)
        framed = f"{name}: " + "x" * body
        assert len(framed.encode("utf-8")) <= CHANNEL_FRAME_TEXT_LIMIT

    @pytest.mark.parametrize("name", BOT_NAMES)
    def test_a_full_body_stays_within_a_relaying_payload(self, name):
        """The regression: a full-length body used to encrypt to 163 bytes."""
        body = channel_body_limit(name)
        framed = f"{name}: " + "x" * body
        assert _payload_bytes(framed) <= RELAYING_PAYLOAD_CEILING

    @pytest.mark.parametrize("name", BOT_NAMES)
    def test_a_regional_scope_body_also_relays(self, name):
        body = channel_body_limit(name) - CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD
        framed = f"{name}: " + "x" * body
        assert _payload_bytes(framed) <= RELAYING_PAYLOAD_CEILING

    def test_the_old_160_byte_sizing_would_fail_this(self):
        """Guards the guard: the assertions above must be capable of failing.

        The observed 163-byte payload came from a 132-byte body that was already
        under the old budget. A body actually filled to the old limit framed to
        160 bytes and encrypted to 179 -- worse still than the packet that stalled.
        """
        name = "KY-ComchanBot \U0001f916"
        old_body = 160 - len(name.encode("utf-8")) - 2
        framed = f"{name}: " + "x" * old_body
        assert len(framed.encode("utf-8")) == 160
        assert _payload_bytes(framed) == 179
        assert _payload_bytes(framed) > RELAYING_PAYLOAD_CEILING
        # and the body that was actually sent, for the record
        assert _payload_bytes(f"{name}: " + "x" * 132) == 163

    def test_multibyte_body_content_is_charged_in_bytes(self):
        """A body of emoji must not slip past the frame limit."""
        name = "KY-ComchanBot \U0001f916"
        body_budget = channel_body_limit(name)
        # Fill the budget with 4-byte characters rather than ASCII.
        body = "\U0001f916" * (body_budget // 4)
        framed = f"{name}: {body}"
        assert len(framed.encode("utf-8")) <= CHANNEL_FRAME_TEXT_LIMIT
        assert _payload_bytes(framed) <= RELAYING_PAYLOAD_CEILING


class TestFloor:
    def test_a_long_name_still_leaves_a_usable_body(self):
        assert channel_body_limit("A" * 200) == CHANNEL_BODY_FLOOR
        assert CHANNEL_BODY_FLOOR > 0

    def test_the_floor_is_below_the_frame_limit(self):
        """A floor above the cap would silently defeat the cap for every name."""
        assert CHANNEL_BODY_FLOOR < CHANNEL_FRAME_TEXT_LIMIT

    def test_names_up_to_the_floor_boundary_are_exact(self):
        for name_len in (1, 20, 50, 90):
            name = "A" * name_len
            expected = max(CHANNEL_FRAME_TEXT_LIMIT - name_len - 2, CHANNEL_BODY_FLOOR)
            assert channel_body_limit(name) == expected

    def test_missing_username_falls_back_rather_than_raising(self):
        assert channel_body_limit(None) == channel_body_limit("Bot")
        assert channel_body_limit("") == channel_body_limit("Bot")


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
