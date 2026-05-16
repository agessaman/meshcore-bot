#!/usr/bin/env python3
"""Unit tests for piped response templates and message_path_bytes_per_hop."""

import configparser
from unittest.mock import MagicMock, Mock

import pytest

from modules.commands.test_command import TestCommand as MeshTestCommand
from modules.models import MeshMessage
from modules.response_template import format_piped_template
from modules.utils import message_path_bytes_per_hop


@pytest.mark.unit
def test_message_path_bytes_per_hop_from_routing():
    msg = MeshMessage(
        content="test",
        channel="c",
        routing_info={"bytes_per_hop": 2, "path_length": 1, "path_nodes": ["0102"]},
    )
    assert message_path_bytes_per_hop(msg) == 2


@pytest.mark.unit
def test_message_path_bytes_per_hop_infers_from_nodes():
    msg = MeshMessage(
        content="test",
        channel="c",
        path="01,02,03 (3 hops)",
        routing_info=None,
    )
    assert message_path_bytes_per_hop(msg) == 1


@pytest.mark.unit
def test_format_piped_template_plain_field():
    out = format_piped_template("a={x}|end", {"x": "hi"}, message=None)
    assert out == "a=hi|end"


@pytest.mark.unit
def test_pathbytes_min_clears_when_below_threshold():
    msg = MeshMessage(
        content="test",
        channel="c",
        routing_info={"bytes_per_hop": 1, "path_length": 2, "path_nodes": ["01", "02"]},
    )
    out = format_piped_template(
        "d={path_distance|pathbytes_min:2}",
        {"path_distance": "10.0km (1 segs)"},
        message=msg,
    )
    assert out == "d="


@pytest.mark.unit
def test_pathbytes_min_keeps_multibyte():
    msg = MeshMessage(
        content="test",
        channel="c",
        routing_info={"bytes_per_hop": 2, "path_length": 1, "path_nodes": ["0102"]},
    )
    out = format_piped_template(
        "d={path_distance|pathbytes:2}",
        {"path_distance": "5.0km (1 segs)"},
        message=msg,
    )
    assert out == "d=5.0km (1 segs)"


@pytest.mark.unit
def test_prefix_if_nonempty_literal_may_contain_pipe():
    """Regression: args like ' | Path Dist: ' must not split into a fake 'Path Dist' filter."""
    msg = MeshMessage(
        content="test",
        channel="c",
        routing_info={"bytes_per_hop": 2, "path_length": 1, "path_nodes": ["0102"]},
    )
    out = format_piped_template(
        "x={path_distance|pathbytes_min:2|prefix_if_nonempty: | Path Dist: }",
        {"path_distance": "1km"},
        message=msg,
        logger=None,
    )
    assert out == "x= | Path Dist: 1km"


@pytest.mark.unit
def test_get_response_format_test_command_over_keywords():
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Keywords")
    bot.config.set("Keywords", "test", "from-keywords")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "response_format", "from-test-cmd")
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "recency_weight", "0.2")
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2

    cmd = MeshTestCommand(bot)
    assert cmd.get_response_format() == "from-test-cmd"
