#!/usr/bin/env python3
"""Unit tests for piped response templates and message_path_bytes_per_hop."""

import configparser
from unittest.mock import MagicMock, Mock

import pytest

from modules.commands.test_command import TestCommand as MeshTestCommand
from modules.models import MeshMessage
from modules.response_template import format_piped_template, format_piped_template_async
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


@pytest.mark.unit
def test_quoted_filter_argument_with_a_nested_placeholder_does_not_close_early():
    """Regression: a quoted filter arg's own '}' (from a nested {field}) must not be
    mistaken for the placeholder's closing brace and truncate the rest of the chain."""
    template = (
        '{packet_hash | if_notempty: '
        '"https://analyzer.example.net/#/packets/{packet_hash}?obs=1620457" '
        '| shorten_url}'
    )
    assert format_piped_template(template, {"packet_hash": ""}) == ""
    assert format_piped_template(template, {"packet_hash": "ABCDEF12"}) == (
        "https://analyzer.example.net/#/packets/ABCDEF12?obs=1620457"
    )


def _shortener_config(**external_data):
    """Config a real shorten_url_sync call will accept, defaulting to v.gd."""
    c = configparser.ConfigParser()
    c["External_Data"] = {}
    for k, v in external_data.items():
        c["External_Data"][k] = v
    return c


@pytest.mark.unit
def test_prefix_if_nonempty_accepts_a_quoted_argument_with_a_nested_placeholder():
    """Regression: the greedy branch used to win over the quoted-argument grammar.

    prefix_if_nonempty is the one filter already in shipped configs, so without this
    the documented quoted syntax emitted raw template text over RF instead.
    """
    out = format_piped_template(
        '{path_distance|prefix_if_nonempty:"Dist {sender}: "}',
        {"path_distance": "5km", "sender": "y"},
    )
    assert out == "Dist y: 5km"


@pytest.mark.unit
def test_prefix_if_nonempty_with_a_quoted_argument_can_be_chained():
    """A quoted arg ends at its closing quote, so a later filter is not swallowed."""
    out = format_piped_template(
        '{d|prefix_if_nonempty:"L "|if_nonempty:Z}',
        {"d": "5km"},
    )
    assert out == "Z"


@pytest.mark.unit
def test_prefix_if_nonempty_keeps_greedy_parsing_for_unquoted_literals():
    """config.ini.example ships `prefix_if_nonempty: | Path Dist: ` -- a literal
    containing a pipe, which only parses if unquoted args stay greedy."""
    out = format_piped_template(
        "ack{path_distance|pathbytes_min:2|prefix_if_nonempty: | Path Dist: }",
        {"path_distance": "12.4km"},
        message=_msg(path="0102 (1 hop)", hops=1,
                     routing_info={"path_length": 1, "bytes_per_hop": 2}),
    )
    assert out == "ack | Path Dist: 12.4km"


@pytest.mark.unit
def test_shorten_url_filter_shortens_through_the_configured_service():
    """End-to-end: the filter reaches shorten_url_sync via ctx['config']."""
    cfg = _shortener_config(short_url_website="https://v.gd")
    resp = MagicMock()
    resp.ok = True
    resp.text = "https://v.gd/abc123"

    with pytest.MonkeyPatch.context() as mp:
        import modules.url_shortener as us
        mp.setattr(us.requests, "get", lambda *a, **k: resp)
        out = format_piped_template(
            "{link|shorten_url}",
            {"link": "https://example.com/a/very/long/path"},
            config=cfg,
        )
    assert out == "https://v.gd/abc123"


@pytest.mark.unit
def test_shorten_is_an_alias_for_shorten_url():
    """Feed formats document `shorten`; the same name must work here."""
    cfg = _shortener_config(short_url_website="https://v.gd")
    resp = MagicMock()
    resp.ok = True
    resp.text = "https://v.gd/abc123"

    with pytest.MonkeyPatch.context() as mp:
        import modules.url_shortener as us
        mp.setattr(us.requests, "get", lambda *a, **k: resp)
        out = format_piped_template(
            "{link|shorten}", {"link": "https://example.com/long"}, config=cfg
        )
    assert out == "https://v.gd/abc123"


@pytest.mark.unit
def test_shorten_url_falls_back_to_the_long_url_when_shortening_fails():
    """A failing shortener costs a longer message, never a broken one."""
    cfg = _shortener_config(short_url_website="https://v.gd")
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 502
    resp.text = "http://short.example/maintenance"

    with pytest.MonkeyPatch.context() as mp:
        import modules.url_shortener as us
        mp.setattr(us.requests, "get", lambda *a, **k: resp)
        out = format_piped_template(
            "{link|shorten_url}", {"link": "https://example.com/long"}, config=cfg
        )
    assert out == "https://example.com/long"


@pytest.mark.unit
def test_shorten_url_without_a_config_passes_the_value_through():
    out = format_piped_template("{link|shorten_url}", {"link": "https://example.com/long"})
    assert out == "https://example.com/long"


@pytest.mark.unit
def test_if_notempty_is_an_alias_for_if_nonempty():
    assert format_piped_template("{d|if_nonempty:Z}", {"d": "x"}) == "Z"
    assert format_piped_template("{d|if_notempty:Z}", {"d": "x"}) == "Z"
    assert format_piped_template("{d|if_nonempty:Z}", {"d": ""}) == ""
    assert format_piped_template("{d|if_notempty:Z}", {"d": ""}) == ""


@pytest.mark.asyncio
async def test_format_piped_template_async_matches_the_sync_render():
    """The async wrapper exists so a shorten filter cannot block the event loop."""
    cfg = _shortener_config(short_url_website="https://v.gd")
    resp = MagicMock()
    resp.ok = True
    resp.text = "https://v.gd/abc123"

    with pytest.MonkeyPatch.context() as mp:
        import modules.url_shortener as us
        mp.setattr(us.requests, "get", lambda *a, **k: resp)
        out = await format_piped_template_async(
            "{link|shorten_url}", {"link": "https://example.com/long"}, config=cfg
        )
    assert out == "https://v.gd/abc123"


@pytest.mark.asyncio
async def test_shorten_url_warns_when_rendered_on_the_event_loop():
    """A blocking HTTP call on the loop stalls radio RX and every other handler."""
    cfg = _shortener_config(short_url_website="https://v.gd")
    resp = MagicMock()
    resp.ok = True
    resp.text = "https://v.gd/abc123"
    logger = MagicMock()

    with pytest.MonkeyPatch.context() as mp:
        import modules.response_template as rt
        import modules.url_shortener as us
        mp.setattr(us.requests, "get", lambda *a, **k: resp)
        mp.setattr(rt, "_warned_blocking_render", False)
        format_piped_template(
            "{link|shorten_url}",
            {"link": "https://example.com/long"},
            config=cfg,
            logger=logger,
        )

    warned = " ".join(str(c) for c in logger.warning.call_args_list)
    assert "event loop" in warned


@pytest.mark.asyncio
async def test_async_render_does_not_warn_about_the_event_loop():
    cfg = _shortener_config(short_url_website="https://v.gd")
    resp = MagicMock()
    resp.ok = True
    resp.text = "https://v.gd/abc123"
    logger = MagicMock()

    with pytest.MonkeyPatch.context() as mp:
        import modules.response_template as rt
        import modules.url_shortener as us
        mp.setattr(us.requests, "get", lambda *a, **k: resp)
        # Without this reset the assertion below passes vacuously whenever an
        # earlier test has already tripped the warn-once flag.
        mp.setattr(rt, "_warned_blocking_render", False)
        out = await format_piped_template_async(
            "{link|shorten_url}",
            {"link": "https://example.com/long"},
            config=cfg,
            logger=logger,
        )

    assert out == "https://v.gd/abc123"
    logger.warning.assert_not_called()


@pytest.mark.unit
def test_a_bare_quote_argument_is_still_a_greedy_literal():
    """Regression: opting into quoted args must not void an unterminated quote.

    `prefix_if_nonempty:"` prepends a literal quote character and always has. Reading
    it as the start of a quoted argument leaves the string unterminated, which would
    reject the whole placeholder and emit raw template text over RF.
    """
    assert format_piped_template('{d|prefix_if_nonempty:"}', {"d": "12.4km"}) == '"12.4km'
    assert format_piped_template(
        '{d|prefix_if_nonempty:"unterminated}', {"d": "12.4km"}
    ) == '"unterminated12.4km'


@pytest.mark.unit
def test_a_quote_after_whitespace_is_a_greedy_literal_not_a_quoted_argument():
    """Only a quote *immediately* after ':' opts in, so spacing is preserved."""
    out = format_piped_template('{d|prefix_if_nonempty: "L" }', {"d": "12.4km"})
    assert out == ' "L" 12.4km'
