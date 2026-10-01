"""Characterization: how wx and gwx pack a multi-day forecast into messages."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.models import MeshMessage


def _cmd(cls):
    cmd = object.__new__(cls)
    cmd.bot = MagicMock()
    cmd.logger = MagicMock()
    cmd.get_max_message_length = lambda message: 40
    cmd.send_response = AsyncMock(return_value=True)
    return cmd


def _classes():
    from modules.commands.alternatives.wx_international import GlobalWxCommand
    from modules.commands.wx_command import WxCommand

    return [WxCommand, GlobalWxCommand]


@pytest.mark.parametrize("cls_index", [0, 1])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mon 50/40 rain\nTue 52/41", [("Mon 50/40 rain\nTue 52/41", {})]),
        (
            "Mon 50/40 rain all day\n\nTue 52/41 sunny\nWed 49/38 showers late\nThu 51/40",
            [
                ("Mon 50/40 rain all day\nTue 52/41 sunny", {"skip_user_rate_limit": False}),
                ("Wed 49/38 showers late\nThu 51/40", {"skip_user_rate_limit": True}),
            ],
        ),
        (
            "x" * 50 + "\nshort line",
            [("x" * 50, {"skip_user_rate_limit": False}), ("short line", {"skip_user_rate_limit": True})],
        ),
        ("   \n  ", []),
    ],
)
async def test_multiday_packing(cls_index, text, expected):
    cmd = _cmd(_classes()[cls_index])
    msg = MeshMessage(content="wx", sender_id="Ann", channel="general")
    with patch("asyncio.sleep", AsyncMock()) as sleep:
        await cmd._send_multiday_forecast(msg, text)
    sent = [(c.args[1], c.kwargs) for c in cmd.send_response.await_args_list]
    assert sent == expected
    assert sleep.await_count == max(0, len(expected) - 1)
