#!/usr/bin/env python3
"""Unit tests for piped response templates and message_path_bytes_per_hop."""

import configparser
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.commands.test_command import TestCommand as MeshTestCommand
from modules.models import MeshMessage
from modules.response_template import (
    format_piped_template,
    format_piped_template_async,
    resolve_template_async,
    template_needs_resolution,
)
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
def test_hopless_packet_is_never_multibyte():
    """bytes_per_hop describes how a path is encoded; a direct packet has no path for
    it to describe, so pathbytes_min must not read the format field as a wide path."""
    msg = MeshMessage(
        content="test",
        channel="c",
        path="Direct",
        routing_info={"bytes_per_hop": 2, "path_length": 0, "path_nodes": []},
    )
    assert message_path_bytes_per_hop(msg) == 1


@pytest.mark.unit
def test_pathbytes_min_hides_label_on_a_direct_message():
    """A direct message has no path distance, so the whole clause must disappear
    rather than render "Path Dist: N/A"."""
    msg = MeshMessage(
        content="test",
        channel="c",
        path="Direct",
        routing_info={"bytes_per_hop": 2, "path_length": 0, "path_nodes": []},
    )
    out = format_piped_template(
        "ack{path_distance|pathbytes_min:2|prefix_if_nonempty: | Path Dist: }",
        {"path_distance": "N/A"},
        message=msg,
    )
    assert out == "ack"


@pytest.mark.unit
def test_pathbytes_min_still_passes_a_real_multibyte_path():
    msg = MeshMessage(
        content="test",
        channel="c",
        path="7a2a,0102 (2 hops)",
        routing_info={"bytes_per_hop": 2, "path_length": 2, "path_nodes": ["7A2A", "0102"]},
    )
    out = format_piped_template(
        "ack{path_distance|pathbytes_min:2|prefix_if_nonempty: | Path Dist: }",
        {"path_distance": "12.4km"},
        message=msg,
    )
    assert out == "ack | Path Dist: 12.4km"


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
def test_format_piped_template_drops_label_for_empty_field():
    out = format_piped_template(
        "hash={packet_hash|prefix_if_nonempty:id:}.",
        {"packet_hash": ""},
        message=None,
    )
    assert out == "hash=."


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


@pytest.mark.unit
def test_test_command_response_expands_rssi_placeholder():
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "recency_weight", "0.2")
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2

    cmd = MeshTestCommand(bot)
    msg = MeshMessage(
        content="test",
        sender_id="Alice",
        path="Direct (0 hops)",
        hops=0,
        snr=12.25,
        rssi=-91,
        routing_info={"path_length": 0},
    )

    out = cmd.format_response(msg, "RSSI: {rssi} | SNR: {snr} | Dist: {firstlast_distance}")

    assert out == "RSSI: -91 | SNR: 12.25 | Dist: N/A"


@pytest.mark.unit
def test_test_command_response_expands_packet_hash_placeholder():
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "recency_weight", "0.2")
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2

    cmd = MeshTestCommand(bot)
    msg = MeshMessage(
        content="test",
        sender_id="Alice",
        path="Direct (0 hops)",
        hops=0,
        snr=12.25,
        rssi=-91,
        routing_info={"path_length": 0, "packet_hash": "ABCDEF0123456789"},
    )

    out = cmd.format_response(msg, "hash={packet_hash}")

    assert out == "hash=ABCDEF0123456789"


@pytest.mark.unit
def test_test_command_response_omits_missing_packet_hash():
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "recency_weight", "0.2")
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2

    cmd = MeshTestCommand(bot)
    msg = MeshMessage(
        content="test",
        sender_id="Alice",
        path="Direct (0 hops)",
        hops=0,
        routing_info={"path_length": 0},
    )

    out = cmd.format_response(msg, "hash={packet_hash|prefix_if_nonempty:id:}.")

    assert out == "hash=."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_test_command_async_response_shortens_url():
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.read_dict({
        "Bot": {"bot_name": "TestBot"},
        "Channels": {"monitor_channels": "general", "respond_to_dms": "true"},
        "Test_Command": {"enabled": "true"},
        "Path_Command": {"recency_weight": "0.2"},
        "External_Data": {"short_url_website": "https://v.gd"},
    })
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2
    cmd = MeshTestCommand(bot)
    msg = MeshMessage(
        content="test",
        sender_id="Alice",
        path="Direct (0 hops)",
        hops=0,
        routing_info={"path_length": 0, "packet_hash": "ABCDEF0123456789"},
    )
    template = (
        '{packet_hash|if_nonempty:"https://scope.example/p/{packet_hash}"|shorten_url}'
    )

    with patch("modules.response_template.shorten_url", return_value="https://v.gd/one"):
        out = await cmd.format_response_async(msg, template)

    assert out == "https://v.gd/one"


def _msg(**kw):
    base = dict(content="test", channel="c")
    base.update(kw)
    return MeshMessage(**base)


@pytest.mark.unit
def test_hops_min_clears_on_a_direct_message():
    out = format_piped_template(
        "ack{d|hops_min:1|prefix_if_nonempty: | Dist: }",
        {"d": "N/A"},
        message=_msg(path="Direct", hops=0, routing_info={"path_length": 0, "bytes_per_hop": 2}),
    )
    assert out == "ack"


@pytest.mark.unit
def test_hops_min_keeps_a_single_byte_multihop_path():
    """The point of hops_min over pathbytes_min: a one-byte path still travelled,
    so its distance is real and must not be discarded with the direct messages."""
    msg = _msg(path="01,02 (2 hops)", hops=2,
               routing_info={"path_length": 2, "path_nodes": ["01", "02"], "bytes_per_hop": 1})
    assert format_piped_template("{d|hops_min:1}", {"d": "12.4km"}, message=msg) == "12.4km"
    assert format_piped_template("{d|pathbytes_min:2}", {"d": "12.4km"}, message=msg) == ""


@pytest.mark.unit
def test_hops_min_threshold_is_inclusive():
    msg = _msg(path="01,02 (2 hops)", hops=2)
    assert format_piped_template("{d|hops_min:2}", {"d": "x"}, message=msg) == "x"
    assert format_piped_template("{d|hops_min:3}", {"d": "x"}, message=msg) == ""


@pytest.mark.unit
def test_hops_min_zero_admits_a_direct_message():
    msg = _msg(path="Direct", hops=0)
    assert format_piped_template("{d|hops_min:0}", {"d": "x"}, message=msg) == "x"


@pytest.mark.unit
def test_hops_min_clears_when_the_hop_count_is_unknown():
    """A gate that cannot confirm the route suppresses rather than guesses."""
    msg = _msg(path=None, hops=None, routing_info=None)
    assert format_piped_template("{d|hops_min:1}", {"d": "x"}, message=msg) == ""


@pytest.mark.unit
def test_hops_min_without_a_message_clears():
    assert format_piped_template("{d|hops_min:1}", {"d": "x"}, message=None) == ""


@pytest.mark.unit
def test_hops_min_with_an_unusable_argument_passes_the_value_through():
    msg = _msg(path="Direct", hops=0)
    assert format_piped_template("{d|hops_min:abc}", {"d": "x"}, message=msg) == "x"
    assert format_piped_template("{d|hops_min:-1}", {"d": "x"}, message=msg) == "x"


@pytest.mark.unit
def test_unknown_filter_passes_value_through_and_warns():
    logger = Mock()
    out = format_piped_template("{x|nope:1}", {"x": "hi"}, logger=logger)
    assert out == "hi"
    logger.warning.assert_called_once()


@pytest.mark.unit
def test_empty_braces_are_left_literal():
    assert format_piped_template("a{}b", {}) == "a{}b"


@pytest.mark.unit
def test_unterminated_placeholder_is_left_literal():
    assert format_piped_template("a {oops no close", {"x": "hi"}) == "a {oops no close"


@pytest.mark.unit
def test_a_malformed_placeholder_does_not_block_a_later_valid_one():
    assert format_piped_template("{} then {x}", {"x": "hi"}) == "{} then hi"


@pytest.mark.unit
def test_quoted_string_literal_is_used_verbatim():
    assert format_piped_template('{"hello world"}', {}) == "hello world"


@pytest.mark.unit
def test_quoted_string_literal_substitutes_a_nested_field():
    assert format_piped_template('{"Hello {name}!"}', {"name": "Alice"}) == "Hello Alice!"


@pytest.mark.unit
def test_quoted_string_literal_substitutes_multiple_nested_fields():
    assert format_piped_template('{"{a}-{b}"}', {"a": "x", "b": "y"}) == "x-y"


@pytest.mark.unit
def test_quoted_string_literal_nested_field_missing_renders_empty():
    assert format_piped_template('{"Hi {ghost}"}', {}) == "Hi "


@pytest.mark.unit
def test_quoted_string_literal_supports_escaped_quotes_and_backslashes():
    assert format_piped_template('{"She said \\"hi\\""}', {}) == 'She said "hi"'
    assert format_piped_template('{"a\\\\b"}', {}) == "a\\b"


@pytest.mark.unit
def test_quoted_string_literal_can_be_filtered():
    msg = _msg(path="01,02 (2 hops)", hops=2)
    assert format_piped_template('{"{d}"|hops_min:1}', {"d": "12.4km"}, message=msg) == "12.4km"
    assert format_piped_template('{"{d}"|hops_min:5}', {"d": "12.4km"}, message=msg) == ""


@pytest.mark.unit
def test_nested_placeholder_inside_a_quoted_literal_can_carry_its_own_filter():
    msg = _msg(path="01,02 (2 hops)", hops=2)
    assert format_piped_template('{"Dist: {d|hops_min:1}"}', {"d": "12.4km"}, message=msg) == "Dist: 12.4km"
    assert format_piped_template('{"Dist: {d|hops_min:5}"}', {"d": "12.4km"}, message=msg) == "Dist: "


_LINK_TEMPLATE = (
    '{packet_hash | if_nonempty: '
    '"https://analyzer.example.net/#/packets/{packet_hash}?obs=1620457" '
    '| shorten_url}'
)
_LONG_LINK = "https://analyzer.example.net/#/packets/ABCDEF12?obs=1620457"


@pytest.mark.unit
def test_quoted_filter_argument_with_a_nested_placeholder_does_not_close_early():
    """Regression: a quoted filter arg's own '}' (from a nested {field}) must not be
    mistaken for the placeholder's closing brace and truncate the rest of the chain."""
    template = (
        '{packet_hash | if_nonempty: '
        '"https://analyzer.example.net/#/packets/{packet_hash}?obs=1620457"}'
    )
    assert format_piped_template(template, {"packet_hash": ""}) == ""
    assert format_piped_template(template, {"packet_hash": "ABCDEF12"}) == _LONG_LINK


@pytest.mark.unit
def test_shorten_url_uses_the_preresolved_mapping():
    out = format_piped_template(
        _LINK_TEMPLATE,
        {"packet_hash": "ABCDEF12"},
        shortened={_LONG_LINK: "https://v.gd/abc"},
    )
    assert out == "https://v.gd/abc"


@pytest.mark.unit
def test_shorten_url_never_calls_the_network_from_the_sync_render():
    """The render path runs on the event loop; a blocking shortener call here would
    stall the radio transport for the length of its timeout."""
    with patch("modules.url_shortener.requests.get") as get, \
         patch("modules.url_shortener.requests.post") as post:
        format_piped_template(_LINK_TEMPLATE, {"packet_hash": "ABCDEF12"}, logger=Mock())
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
def test_unresolved_shorten_url_drops_the_clause_and_warns():
    """A 59-byte URL against a ~158-byte budget would push a path reply into a second
    transmission, so an unresolved link is dropped rather than sent long."""
    logger = Mock()
    out = format_piped_template(_LINK_TEMPLATE, {"packet_hash": "ABCDEF12"}, logger=logger)
    assert out == ""
    logger.warning.assert_called_once()


@pytest.mark.unit
def test_template_needs_resolution_only_for_network_filters():
    assert template_needs_resolution(_LINK_TEMPLATE)
    assert not template_needs_resolution("{path_distance|prefix_if_nonempty: | Dist: }")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_shortens_the_built_link():
    cfg = configparser.ConfigParser()
    cfg.add_section("External_Data")
    cfg.set("External_Data", "short_url_website", "https://v.gd")
    resp = MagicMock()
    resp.ok = True
    resp.text = "https://v.gd/abc"
    session = MagicMock()
    session.get.return_value = resp

    with patch("modules.url_shortener.requests.get", session.get):
        resolved = await resolve_template_async(
            _LINK_TEMPLATE, {"packet_hash": "ABCDEF12"}, config=cfg
        )

    assert resolved == {_LONG_LINK: "https://v.gd/abc"}
    assert format_piped_template(
        _LINK_TEMPLATE, {"packet_hash": "ABCDEF12"}, shortened=resolved
    ) == "https://v.gd/abc"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_skips_a_gated_clause():
    """hops_min has already suppressed the clause during the collection pass, so no
    request is made for a link that would never have been sent."""
    cfg = configparser.ConfigParser()
    cfg.add_section("External_Data")
    template = '{d|hops_min:5|if_nonempty:"https://x.example/{d}"|shorten_url}'
    with patch("modules.url_shortener.requests.get") as get:
        resolved = await resolve_template_async(
            template, {"d": "12.4km"}, message=_msg(path="Direct", hops=0), config=cfg
        )
    assert resolved == {}
    get.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_is_a_noop_without_config():
    assert await resolve_template_async(_LINK_TEMPLATE, {"packet_hash": "A"}) == {}


@pytest.fixture(autouse=True)
def _fresh_warn_state():
    """Reset the module-level warn-once cache around every test in this file.

    `_UNRESOLVED_WARNED` deduplicates the unresolved-template warning across the
    process, so without this a test that renders such a template silently suppresses
    the warning in whichever test runs next — an order-dependent failure. Autouse
    because any future test here could trip on it.
    """
    from modules import response_template

    response_template._UNRESOLVED_WARNED.clear()
    response_template._IGNORED_FILTER_ARGS_WARNED.clear()
    yield
    response_template._UNRESOLVED_WARNED.clear()
    response_template._IGNORED_FILTER_ARGS_WARNED.clear()


@pytest.mark.unit
def test_urlencode_escapes_a_field_interpolated_into_a_url():
    """`sender` is whatever a remote node advertises; unencoded it rewrites the URL."""
    template = '{sender|if_nonempty:"https://x.example/u/{sender|urlencode}"}'
    out = format_piped_template(template, {"sender": "bob&admin=1 #frag"})
    assert out == "https://x.example/u/bob%26admin%3D1%20%23frag"


@pytest.mark.unit
def test_urlencode_escapes_slashes_too():
    """An interpolated field is one path segment, not a path."""
    assert format_piped_template('{"p/{a|urlencode}"}', {"a": "x/../y"}) == "p/x%2F..%2Fy"


@pytest.mark.unit
def test_urlencode_leaves_an_empty_value_empty():
    assert format_piped_template("{missing|urlencode}", {}) == ""


@pytest.mark.unit
def test_unresolved_shorten_url_warns_once_per_template():
    """This runs on the inbound message path; an unconditional warning would be one
    log line per message forever."""
    logger = Mock()
    for _ in range(5):
        assert format_piped_template(_LINK_TEMPLATE, {"packet_hash": "AB"}, logger=logger) == ""
    assert logger.warning.call_count == 1


@pytest.mark.unit
def test_a_second_distinct_template_still_warns():
    logger = Mock()
    format_piped_template(_LINK_TEMPLATE, {"packet_hash": "AB"}, logger=logger)
    format_piped_template('{a|shorten_url}', {"a": "https://other.example"}, logger=logger)
    assert logger.warning.call_count == 2


@pytest.mark.unit
def test_a_resolved_mapping_that_misses_does_not_warn():
    """A pre-pass that ran but could not shorten is a transient network condition,
    not a misconfiguration — it must not escalate to WARNING on the message path."""
    logger = Mock()
    out = format_piped_template(
        _LINK_TEMPLATE, {"packet_hash": "AB"}, logger=logger, shortened={}
    )
    assert out == ""
    logger.warning.assert_not_called()
    logger.debug.assert_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_warns_when_config_is_missing():
    """Regression: this used to return {} indistinguishably from 'nothing to do', so
    the render dropped the clause with no diagnostic anywhere."""
    logger = Mock()
    assert await resolve_template_async(_LINK_TEMPLATE, {"packet_hash": "AB"}, logger=logger) == {}
    logger.warning.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_does_not_warn_without_shorten_url():
    logger = Mock()
    assert await resolve_template_async("{d|hops_min:1}", {"d": "1km"}, logger=logger) == {}
    logger.warning.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_propagates_cancellation():
    """`return_exceptions=True` captures CancelledError like any other exception;
    swallowing it would let a cancelled render carry on and transmit at shutdown."""
    import asyncio

    cfg = configparser.ConfigParser()
    cfg.add_section("External_Data")

    async def _cancelled(*a, **k):
        raise asyncio.CancelledError()

    with patch("modules.response_template.shorten_url", _cancelled):
        with pytest.raises(asyncio.CancelledError):
            await resolve_template_async(
                _LINK_TEMPLATE, {"packet_hash": "AB"}, config=cfg
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_template_async_still_swallows_ordinary_failures():
    cfg = configparser.ConfigParser()
    cfg.add_section("External_Data")

    async def _boom(*a, **k):
        raise RuntimeError("shortener exploded")

    with patch("modules.response_template.shorten_url", _boom):
        assert await resolve_template_async(
            _LINK_TEMPLATE, {"packet_hash": "AB"}, config=cfg
        ) == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_format_piped_template_async_resolves_and_renders_in_one_call():
    cfg = configparser.ConfigParser()
    cfg.add_section("External_Data")
    with patch("modules.response_template.shorten_url", return_value="https://v.gd/one"):
        out = await format_piped_template_async(
            _LINK_TEMPLATE, {"packet_hash": "ABCDEF12"}, config=cfg
        )
    assert out == "https://v.gd/one"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collection_pass_does_not_duplicate_unknown_filter_warning():
    cfg = configparser.ConfigParser()
    cfg.add_section("External_Data")
    logger = Mock()
    template = '{a|typo|if_nonempty:"https://x.example/{a}"|shorten_url}'

    with patch("modules.response_template.shorten_url", return_value="https://v.gd/one"):
        assert await format_piped_template_async(
            template, {"a": "value"}, config=cfg, logger=logger
        ) == "https://v.gd/one"

    unknown_warnings = [
        call for call in logger.warning.call_args_list
        if "Unknown response template filter" in str(call)
    ]
    assert len(unknown_warnings) == 1


@pytest.mark.unit
def test_shorten_url_argument_warns_once_and_is_ignored():
    logger = Mock()
    template = "{a|shorten_url:custom-slug}"
    for _ in range(3):
        assert format_piped_template(
            template,
            {"a": "https://x.example"},
            logger=logger,
            shortened={"https://x.example": "https://v.gd/one"},
        ) == "https://v.gd/one"

    ignored_arg_warnings = [
        call for call in logger.warning.call_args_list
        if "does not accept an argument" in str(call)
    ]
    assert len(ignored_arg_warnings) == 1
