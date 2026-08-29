#!/usr/bin/env python3
"""PathCommand wires the shorten_url pre-pass into its reply prefix.

_format_path_reply_prefix is the only place resolve_template_async() is called, so
without these a dropped `await` or a missing `shortened=` would disable shortening
silently: the clause just stops appearing and nothing fails.
"""

from unittest.mock import MagicMock, patch

import pytest

from modules.commands.path_command import PathCommand
from modules.models import MeshMessage

LONG = "https://scope.example.net/#/packets/ABCDEF12"
TEMPLATE = '{packet_hash|if_nonempty:"https://scope.example.net/#/packets/{packet_hash}"|shorten_url}'


def _msg():
    return MeshMessage(content="path", sender_id="!aabbccdd", is_dm=True)


@pytest.fixture
def cmd(mock_bot):
    c = PathCommand(mock_bot)
    c.path_reply_prefix = TEMPLATE
    c.get_standard_placeholder_fields = MagicMock(return_value={"packet_hash": "ABCDEF12"})
    c._format_path_distance = MagicMock(return_value="")
    return c


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_prefix_uses_the_shortened_link(cmd):
    with patch(
        "modules.response_template.shorten_url", return_value="https://v.gd/abc"
    ) as shorten:
        out = await cmd._format_path_reply_prefix(_msg())

    assert out == "https://v.gd/abc\n"
    shorten.assert_called_once()
    assert shorten.call_args[0][0] == LONG


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_prefix_drops_the_clause_when_shortening_fails(cmd):
    """An unreachable shortener costs the link, not a second transmission."""
    with patch("modules.response_template.shorten_url", return_value=""):
        assert await cmd._format_path_reply_prefix(_msg()) == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_prefix_never_shortens_from_the_sync_render(cmd):
    """The HTTP call must happen in the pre-pass, not inside format_piped_template."""
    with patch("modules.url_shortener.requests.get") as get, \
         patch("modules.url_shortener.requests.post") as post, \
         patch("modules.response_template.shorten_url", return_value="https://v.gd/abc"):
        await cmd._format_path_reply_prefix(_msg())
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prefix_without_shorten_url_makes_no_request(cmd):
    cmd.path_reply_prefix = "{packet_hash|prefix_if_nonempty:# }"
    with patch("modules.response_template.shorten_url") as shorten:
        out = await cmd._format_path_reply_prefix(_msg())
    assert out == "# ABCDEF12\n"
    shorten.assert_not_called()
