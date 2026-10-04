#!/usr/bin/env python3
"""Tests for the template placeholder reference and its preview."""

import configparser
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.clients.mqtt_weather import DEFAULT_JSON_TEMPLATE, JSON_TEMPLATE_PLACEHOLDERS
from modules.commands.greeter_command import GreeterCommand, parse_channel_greetings
from modules.commands.multitest_command import MultitestCommand
from modules.commands.path_command import PathCommand
from modules.commands.test_command import TestCommand as MeshTestCommand
from modules.models import MeshMessage
from modules.response_template import RESPONSE_TEMPLATE_FILTERS
from modules.service_plugins.mqtt_weather_service import MqttWeatherService
from modules.template_reference import (
    MESSAGE_PLACEHOLDERS,
    PIPED_FILTERS,
    template_spec,
)
from modules.template_reference import render_preview as _render_preview
from modules.utils import decode_escape_sequences


def render_preview(spec, template, config=None):
    """The preview's rows; see TestPreviewNotes for the note beside them."""
    return _render_preview(spec, template, config)["scenarios"]


def _field(cls, key):
    return next(f for f in cls.settings_schema if f["key"] == key)


def _test_command():
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.add_section("Test_Command")
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2
    return MeshTestCommand(bot)


def _by_id(results):
    return {r["id"]: r["output"] for r in results}


@pytest.mark.unit
class TestCatalogMatchesTheEngine:
    def test_every_filter_and_alias_is_listed(self):
        listed = set()
        for f in PIPED_FILTERS:
            listed.add(f["name"])
            listed.update(f["aliases"])
        assert listed == set(RESPONSE_TEMPLATE_FILTERS)

    def test_message_placeholders_are_the_standard_fields(self):
        cmd = _test_command()
        msg = MeshMessage(content="test", sender_id="Alice", hops=0, routing_info={"path_length": 0})
        assert set(MESSAGE_PLACEHOLDERS) == set(cmd.get_standard_placeholder_fields(msg))

    def test_test_reply_lists_only_fields_the_command_renders(self):
        cmd = _test_command()
        msg = MeshMessage(content="test hi", sender_id="Alice", hops=0, routing_info={"path_length": 0})
        with patch("modules.commands.test_command.format_piped_template", return_value="") as fmt:
            cmd.format_response(msg, "{sender}")
        rendered = set(fmt.call_args.args[1])
        listed = {p["name"] for p in _field(MeshTestCommand, "response_format")["template"]["placeholders"]}
        assert listed <= rendered

    def test_test_reply_blank_default_is_the_command_default(self):
        spec = _field(MeshTestCommand, "response_format")["template"]
        assert spec["blank_default"] == MeshTestCommand.DEFAULT_FORMAT

    def test_path_prefix_lists_standard_fields_and_distance(self):
        spec = _field(PathCommand, "reply_prefix")["template"]
        assert [p["name"] for p in spec["placeholders"]] == [*MESSAGE_PLACEHOLDERS, "path_distance"]
        assert spec["escapes"] is False

    def test_mqtt_weather_lists_every_allowed_placeholder(self):
        spec = _field(MqttWeatherService, "json_template")["template"]
        assert {p["name"] for p in spec["placeholders"]} == set(JSON_TEMPLATE_PLACEHOLDERS)

    def test_format_fields_offer_no_filters(self):
        spec = _field(MultitestCommand, "response_format")["template"]
        assert spec["syntax"] == "format"
        assert "filters" not in spec

    def test_every_template_field_has_a_preview(self):
        from modules.settings_schema import build_plugin_settings_view

        view = build_plugin_settings_view(configparser.ConfigParser())
        fields = [f for e in view for f in e["fields"] if f.get("template")]
        assert len(fields) >= 7
        assert all(f["template"].get("previewable") for f in fields)

    def test_greeting_blank_default_is_the_schema_default(self):
        """A blank greeting saves the schema default, so that is what to preview."""
        field = _field(GreeterCommand, "greeting_message")
        assert field["template"]["blank_default"] == field["default"]

    def test_mqtt_blank_default_is_the_client_default(self):
        field = _field(MqttWeatherService, "json_template")
        assert field["default"] == field["template"]["blank_default"] == DEFAULT_JSON_TEMPLATE

    def test_unknown_format_preview_is_rejected(self):
        with pytest.raises(ValueError):
            template_spec("format", {"x": ""}, preview="nope")

    def test_unknown_placeholder_name_is_rejected(self):
        with pytest.raises(KeyError):
            template_spec("piped", ("sender", "no_such_field"))


@pytest.mark.unit
class TestRenderPreview:
    def setup_method(self):
        self.spec = _field(MeshTestCommand, "response_format")["template"]

    def test_pathbytes_min_shows_only_on_the_multibyte_sample(self):
        out = _by_id(render_preview(self.spec, "{path_distance|pathbytes_min:2|prefix_if_nonempty:D }"))
        assert out == {"multibyte": "D 12.4km", "onebyte": "", "direct": ""}

    def test_direct_n_a_shows_the_trap_and_hops_min_fixes_it(self):
        bare = _by_id(render_preview(self.spec, "{path_distance|prefix_if_nonempty:D }"))
        gated = _by_id(render_preview(self.spec, "{path_distance|hops_min:1|prefix_if_nonempty:D }"))
        assert bare["direct"] == "D N/A"
        assert gated["direct"] == ""
        assert gated["onebyte"] == "D 12.4km"

    def test_blank_previews_the_default(self):
        out = _by_id(render_preview(self.spec, "  "))
        assert out["direct"].startswith("ack @[Alice]: radio check | Direct | SNR:")

    def test_quotes_stripped_and_escapes_decoded_like_the_command(self):
        out = _by_id(render_preview(self.spec, '"a\\nb {hops}"'))
        assert out["multibyte"] == "a\nb 3"

    def test_no_escape_decoding_where_the_plugin_does_none(self):
        spec = _field(PathCommand, "reply_prefix")["template"]
        assert _by_id(render_preview(spec, "a\\nb"))["direct"] == "a\\nb"

    def test_byte_count_is_utf8(self):
        result = render_preview(self.spec, "📏{hops}")[0]
        assert result["bytes"] == len("📏3".encode())

    def test_a_field_without_a_renderer_cannot_be_previewed(self):
        with pytest.raises(ValueError):
            render_preview(template_spec("format", {"x": ""}), "{x}")


def _preview(cls, key, template):
    return render_preview(_field(cls, key)["template"], template)


@pytest.mark.unit
class TestFormatPreviews:
    def test_greeting_parts_become_separate_messages(self):
        rows = _preview(GreeterCommand, "greeting_message", "Hi @[{sender}]!|Line\\none")
        assert [r["output"] for r in rows] == ["Hi @[Alice]!", "Line\none"]
        assert rows[1]["label"] == "Greeting, message 2 of 2"

    def test_greeting_unknown_placeholder_sends_nothing(self):
        (row,) = _preview(GreeterCommand, "greeting_message", "Hi {nmae}")
        assert row["output"] == ""
        assert "Unknown placeholder {nmae}" in row["error"]
        assert "no greeting" in row["error"]

    def test_blank_greeting_previews_the_default(self):
        (row,) = _preview(GreeterCommand, "greeting_message", "")
        assert row["output"] == "Welcome to the mesh, Alice!"

    def test_channel_greetings_use_the_greeters_parser(self):
        rows = _preview(GreeterCommand, "channel_greetings", "stray, Public:Hi, {sender}!,#local:Hey")
        assert [(r["label"], r["output"]) for r in rows] == [
            ("Ignored", "stray"),
            ("Public", "Hi, Alice!"),
            ("#local", "Hey"),
        ]
        assert "error" in rows[0] and "error" not in rows[1]

    def test_channel_greetings_parser_matches_the_command(self, command_mock_bot):
        raw = "Public:Welcome, {sender}!|Part 2,#local:Hey, see https://x.io at 3:30pm"
        command_mock_bot.config.add_section("Greeter_Command")
        command_mock_bot.config.set("Greeter_Command", "channel_greetings", raw)
        with patch.object(GreeterCommand, "_init_greeter_tables"):
            cmd = GreeterCommand(command_mock_bot)
        assert cmd.channel_greetings == parse_channel_greetings(raw)[0]

    def test_mesh_info_error_drops_only_the_mesh_info(self):
        (row,) = _preview(GreeterCommand, "mesh_info_format", "{total} contacts")
        assert row["output"] == ""
        assert "without mesh info" in row["error"]

    def test_mesh_info_renders_with_escapes(self):
        (row,) = _preview(GreeterCommand, "mesh_info_format", "\\n\\n{repeaters} repeaters")
        assert row["output"] == "\n\n57 repeaters"

    def test_multitest_blank_is_the_default_format(self):
        (row,) = _preview(MultitestCommand, "response_format", "")
        assert row["output"].startswith("Paths(3):\n")

    def test_multitest_bad_template_falls_back_to_the_default(self):
        (row,) = _preview(MultitestCommand, "response_format", "{nope}")
        assert row["output"].startswith("Paths(3):\n")
        assert "default format" in row["error"]

    def test_multitest_decodes_like_the_command(self):
        (row,) = _preview(MultitestCommand, "response_format", '"{path_count}\\n{listening_duration}s"')
        assert row["output"] == "3\n6s"

    def test_mqtt_format_spec_fails_on_a_missing_reading(self):
        full, partial = _preview(MqttWeatherService, "json_template", "{humidity:.0f}%")
        assert full["output"] == "72%"
        assert "error" in partial
        assert partial["output"] == "MQTT weather payload error: template format error"

    def test_mqtt_unknown_placeholder(self):
        full, _ = _preview(MqttWeatherService, "json_template", "{bogus}")
        assert "Invalid template placeholder: {bogus}" in full["output"]


def _config(text):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(text)
    return config


@pytest.mark.unit
class TestCodexReviewFixes:
    def test_blank_test_format_previews_the_keywords_fallback(self):
        spec = _field(MeshTestCommand, "response_format")["template"]
        config = _config('[Keywords]\ntest = "ack {hops}"\n')
        result = _render_preview(spec, "", config)
        assert _by_id(result["scenarios"])["direct"] == "ack 0"
        assert "[Keywords] test" in result["note"]

    def test_quoted_empty_test_format_is_blank_too(self):
        spec = _field(MeshTestCommand, "response_format")["template"]
        result = _render_preview(spec, '""')
        assert _by_id(result["scenarios"])["direct"].startswith("ack @[Alice]: radio check")
        assert "built-in default" in result["note"]

    def test_quoted_empty_multitest_format_is_the_default(self):
        (row,) = _preview(MultitestCommand, "response_format", '""')
        assert row["output"].startswith("Paths(3):\n")

    def test_multitest_follows_the_saved_path_layout(self):
        spec = _field(MultitestCommand, "response_format")["template"]
        flat = _render_preview(spec, "{paths}", _config("[Multitest_Command]\ncondense_paths = false\n"))
        condensed = _render_preview(spec, "{paths}", _config("[Multitest_Command]\ncondense_paths = true\n"))
        assert flat["scenarios"][0]["output"] == "a1,b7,e5\na1,c3,e5\na1,c3,f2"
        assert condensed["scenarios"][0]["output"] != flat["scenarios"][0]["output"]

    def test_mqtt_uses_the_configured_length_limit(self):
        spec = _field(MqttWeatherService, "json_template")["template"]
        config = _config("[MqttWeather]\npassthrough_max_length = 64\n")
        rows = _render_preview(spec, "{time}" + "x" * 100, config)["scenarios"]
        assert len(rows[0]["output"]) == 64

    def test_huge_format_width_is_not_rendered(self):
        (row,) = _preview(GreeterCommand, "greeting_message", "{sender:100000000}")
        assert row["output"] == ""
        assert "over 500" in row["error"]

    def test_greeter_form_defaults_are_its_missing_key_fallbacks(self, command_mock_bot):
        command_mock_bot.config.add_section("Greeter_Command")
        with patch.object(GreeterCommand, "_init_greeter_tables"):
            cmd = GreeterCommand(command_mock_bot)
        assert cmd.greeting_message == _field(GreeterCommand, "greeting_message")["default"]
        assert cmd.mesh_info_format == decode_escape_sequences(_field(GreeterCommand, "mesh_info_format")["default"])

    def test_temperature_format_preview_and_fallback(self):
        from modules.commands.wx_command import WxCommand

        spec = _field(WxCommand, "temperature_high_low_format")["template"]
        assert render_preview(spec, "↓{low}°↑{high}{units}")[0]["output"] == "↓51°↑68°F"
        (row,) = render_preview(spec, "{hi}")
        assert row["output"] == "H:68°F L:51°F"
        assert "default format" in row["error"]


@pytest.mark.unit
class TestSchemaFixes:
    def test_feed_shows_the_inherited_feed_manager_value(self):
        from modules.commands.feed_command import FeedCommand
        from modules.settings_schema import _read_typed

        field = _field(FeedCommand, "allow_private_urls")
        assert _read_typed(_config("[Feed_Manager]\nallow_private_urls = true\n"), "Feed_Command", field) is True
        own = _config("[Feed_Manager]\nallow_private_urls = true\n[Feed_Command]\nallow_private_urls = false\n")
        assert _read_typed(own, "Feed_Command", field) is False

    def test_sports_reads_its_api_timeout(self, command_mock_bot):
        from modules.commands.sports_command import SportsCommand

        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "api_timeout", "42")
        assert SportsCommand(command_mock_bot).url_timeout == 42

    def test_prefix_collision_accepts_a_list_of_lengths(self):
        from modules.service_plugins.repeater_prefix_collision_service import RepeaterPrefixCollisionService
        from modules.settings_schema import validate_field

        field = _field(RepeaterPrefixCollisionService, "notify_on_prefix_bytes")
        assert validate_field(field, "2, 3") == (True, ["2", "3"], None)
        assert validate_field(field, "4")[0] is False
