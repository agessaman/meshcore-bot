"""Tests for config schema sync, template generation, and TUI schema loading."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from modules.config_schema import (
    canonical_sections_missing_from_example,
    get_example_sections,
    is_known_config_key,
    load_documented_keys_from_example,
)
from modules.config_validation import (
    REQUIRED_SECTIONS,
    SEVERITY_WARNING,
    validate_config,
)
from scripts.config_tui import validate_config as tui_validate_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = PROJECT_ROOT / "config.ini.example"
MINIMAL_PATH = PROJECT_ROOT / "config.ini.minimal-example"
QUICKSTART_PATH = PROJECT_ROOT / "config.ini.quickstart"

# Core commands enabled in minimal-example (section names).
MINIMAL_ENABLED_COMMANDS = frozenset({
    "Ping_Command",
    "Version_Command",
    "Test_Command",
    "Path_Command",
    "Prefix_Command",
    "Multitest_Command",
    "Help_Command",
})


class TestCanonicalSectionsInExample:
    def test_all_canonical_sections_documented_in_example(self):
        missing = canonical_sections_missing_from_example(EXAMPLE_PATH)
        assert missing == [], (
            f"config.ini.example is missing canonical sections: {missing}"
        )


class TestExampleConfigsHaveNoUnknownSections:
    """Regression: shipped templates must not use unknown section names."""

    @pytest.mark.parametrize(
        "filename",
        ["config.ini.example", "config.ini.minimal-example", "config.ini.quickstart"],
    )
    def test_example_configs_have_no_unknown_sections(self, filename):
        path = PROJECT_ROOT / filename
        if not path.exists():
            pytest.skip(f"{filename} not present")
        results = validate_config(str(path))
        unknown = [
            r for r in results
            if r[0] == "info" and "not in canonical list" in r[1]
        ]
        assert unknown == [], f"{filename} has unrecognized sections: {unknown}"


class TestMinimalExamplePurpose:
    """Minimal config stays lean: core commands only, not a copy of the full example."""

    def test_minimal_is_much_smaller_than_example(self):
        example_lines = len(EXAMPLE_PATH.read_text(encoding="utf-8").splitlines())
        minimal_lines = len(MINIMAL_PATH.read_text(encoding="utf-8").splitlines())
        assert minimal_lines < example_lines * 0.4, (
            f"minimal-example ({minimal_lines} lines) should stay lean vs "
            f"example ({example_lines} lines)"
        )

    def test_minimal_sections_are_subset_of_example(self):
        example_sections = set(get_example_sections(EXAMPLE_PATH))
        minimal_sections = set(get_example_sections(MINIMAL_PATH))
        assert minimal_sections <= example_sections, (
            f"minimal has sections not in example: {minimal_sections - example_sections}"
        )

    def test_minimal_enables_only_core_commands(self):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read(MINIMAL_PATH, encoding="utf-8")
        for section in cfg.sections():
            if not section.endswith("_Command"):
                continue
            if not cfg.has_option(section, "enabled"):
                continue
            enabled = cfg.getboolean(section, "enabled")
            if section in MINIMAL_ENABLED_COMMANDS:
                assert enabled is True, f"{section} should be enabled in minimal"
            else:
                assert enabled is False, f"{section} should be disabled in minimal"


class TestDocumentedKeys:
    def test_tcp_connection_keys_documented(self):
        keys = load_documented_keys_from_example(EXAMPLE_PATH).get("Connection", {})
        assert "hostname" in keys
        assert "tcp_port" in keys
        assert "ble_device_name" in keys

    def test_minimal_has_tcp_connection_keys(self):
        keys = load_documented_keys_from_example(MINIMAL_PATH).get("Connection", {})
        assert "hostname" in keys
        assert "tcp_port" in keys

    def test_tui_recognizes_tcp_keys_as_valid(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = tcp
hostname = 192.168.1.60
tcp_port = 5050

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general
""")
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        issues = tui_validate_config(cfg, example_keys)
        warnings = [
            i for i in issues
            if i[0] == "WARNING" and "Unknown key" in i[2]
            and i[1] == "Connection"
        ]
        assert warnings == []

    def test_new_sections_present_in_example(self):
        sections = set(get_example_sections(EXAMPLE_PATH))
        assert "Feed_Manager" in sections
        assert "Custom_Syntax" in sections
        assert "Service_Overrides" in sections

    def test_weather_service_rain_nowcast_cache_documented(self):
        keys = load_documented_keys_from_example(EXAMPLE_PATH).get("Weather_Service", {})
        assert "rain_nowcast_cache_seconds" in keys

    def test_repeater_prefix_collision_external_keys_documented(self):
        keys = load_documented_keys_from_example(EXAMPLE_PATH).get(
            "RepeaterPrefixCollision_Service", {}
        )
        for key in (
            "discord_webhook_urls",
            "telegram_chat_ids",
            "notify_external_on_all_new_repeaters",
            "silence_mesh_output",
        ):
            assert key in keys, f"missing {key} in config.ini.example"

    def test_greeter_command_delay_keys_documented(self):
        keys = load_documented_keys_from_example(EXAMPLE_PATH).get("Greeter_Command", {})
        for key in (
            "dead_air_delay_seconds",
            "defer_to_human_greeting",
            "levenshtein_distance",
        ):
            assert key in keys, f"missing {key} in config.ini.example"


class TestKeyValidation:
    def test_invalid_connection_type_warns(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = invalid
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general
""")
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("connection_type" in r[1] for r in warnings)

    def test_tcp_without_hostname_warns(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = tcp
hostname =
tcp_port = 5050

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general
""")
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("hostname" in r[1] for r in warnings)

    def test_connection_host_key_suggests_hostname(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = tcp
host = 0.0.0.0
port = 5050

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general
""")
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("hostname" in r[1] for r in warnings)
        assert any("tcp_port" in r[1] for r in warnings)


class TestTuiCommandStandardKeys:
    def test_channels_not_unknown_on_worldcup_command(self):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general

[Worldcup_Command]
enabled = true
channels = #bot,#fifa2026
""")
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        assert "channels" not in example_keys.get("Worldcup_Command", {})
        issues = tui_validate_config(cfg, example_keys)
        channel_warnings = [
            i for i in issues
            if i[0] == "WARNING" and i[1] == "Worldcup_Command" and "channels" in i[2]
        ]
        assert channel_warnings == []

    def test_legacy_enabled_key_not_unknown(self):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general

[Sports_Command]
sports_enabled = true
teams = seahawks
""")
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        issues = tui_validate_config(cfg, example_keys)
        legacy_warnings = [
            i for i in issues
            if i[0] == "WARNING" and i[1] == "Sports_Command" and "sports_enabled" in i[2]
        ]
        assert legacy_warnings == []

    def test_is_known_config_key_rejects_prose_comment_false_positives(self):
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        assert not is_known_config_key(
            "Sports_Command",
            "If uncommented with empty value (channels",
            example_keys,
        )

    def test_bridge_channel_mapping_keys_not_unknown(self):
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        assert is_known_config_key("DiscordBridge", "bridge.public", example_keys)
        assert is_known_config_key("TelegramBridge", "bridge.howltest", example_keys)

        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general

[DiscordBridge]
enabled = true
avatar_style = fun-emoji
bridge.public = https://discord.com/api/webhooks/example
""")
        issues = tui_validate_config(cfg, example_keys)
        bridge_warnings = [
            i for i in issues
            if i[0] == "WARNING" and i[1] == "DiscordBridge" and "bridge.public" in i[2]
        ]
        assert bridge_warnings == []

    def test_announcements_trigger_keys_not_unknown(self):
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        assert is_known_config_key(
            "Announcements_Command", "announce.keygen", example_keys
        )
        assert is_known_config_key(
            "Announcements_Command", "announce.analyzer", example_keys
        )

        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general

[Announcements_Command]
enabled = true
announce.keygen = Generate keys at example.com
announce.analyzer = Check the mesh analyzer
""")
        issues = tui_validate_config(cfg, example_keys)
        announce_warnings = [
            i for i in issues
            if i[0] == "WARNING"
            and i[1] == "Announcements_Command"
            and "announce." in i[2]
        ]
        assert announce_warnings == []

    def test_alert_agency_keys_not_unknown(self):
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        assert is_known_config_key(
            "Alert_Command", "agency.city.seattle", example_keys
        )
        assert is_known_config_key(
            "Alert_Command", "agency.county.king", example_keys
        )
        assert is_known_config_key(
            "Alert_Command", "agency.king", example_keys
        )
        assert is_known_config_key(
            "Alert_Command", "agency_king", example_keys
        )

        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general

[Alert_Command]
enabled = true
agency.city.seattle = 17D20,17M15
agency.county.king = 17D02,17M15
""")
        issues = tui_validate_config(cfg, example_keys)
        agency_warnings = [
            i for i in issues
            if i[0] == "WARNING" and i[1] == "Alert_Command" and "agency." in i[2]
        ]
        assert agency_warnings == []


class TestTuiServiceStandardKeys:
    def test_repeater_prefix_collision_external_keys_not_unknown(self):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general

[RepeaterPrefixCollision_Service]
enabled = true
channels = #howltest
cooldown_minutes_per_prefix = 60
discord_webhook_urls = https://discord.com/api/webhooks/example
telegram_chat_ids = -1003715244454
notify_external_on_all_new_repeaters = true
silence_mesh_output = true
""")
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        issues = tui_validate_config(cfg, example_keys)
        ext_warnings = [
            i for i in issues
            if i[0] == "WARNING"
            and i[1] == "RepeaterPrefixCollision_Service"
            and any(k in i[2] for k in (
                "discord_webhook_urls",
                "telegram_chat_ids",
                "notify_external_on_all_new_repeaters",
                "silence_mesh_output",
            ))
        ]
        assert ext_warnings == []


class TestTuiRequiredSections:
    def test_tui_requires_channels_section(self):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read_string("""[Connection]
connection_type = serial

[Bot]
bot_name = TestBot
""")
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        issues = tui_validate_config(cfg, example_keys)
        errors = [i for i in issues if i[0] == "ERROR"]
        assert any("Channels" in e[2] for e in errors)

    def test_tui_and_validator_share_required_sections(self):
        assert "Channels" in REQUIRED_SECTIONS
        assert "Connection" in REQUIRED_SECTIONS
        assert "Bot" in REQUIRED_SECTIONS


class TestQuickstartExample:
    def test_quickstart_stays_compact(self):
        quickstart_lines = len(QUICKSTART_PATH.read_text(encoding="utf-8").splitlines())
        example_lines = len(EXAMPLE_PATH.read_text(encoding="utf-8").splitlines())
        assert quickstart_lines < example_lines * 0.1

    def test_quickstart_sections_are_subset_of_example(self):
        example_sections = set(get_example_sections(EXAMPLE_PATH))
        quickstart_sections = set(get_example_sections(QUICKSTART_PATH))
        assert quickstart_sections <= example_sections


class TestSettingsSchemaValidationSync:
    """The web settings UI and config validation must agree on known keys.

    Every key a plugin declares in settings_schema is written to config.ini by
    the /api/plugins save endpoint; if validation doesn't recognize it, users
    get 'unknown key' warnings for settings the bot's own UI wrote.
    """

    def test_every_settings_schema_key_is_known_to_validation(self):
        from modules.settings_schema import build_plugin_settings_view

        cfg = configparser.ConfigParser()
        view = build_plugin_settings_view(cfg)
        documented = load_documented_keys_from_example(EXAMPLE_PATH)
        unknown = []
        for entry in view:
            for f in entry["fields"]:
                section = f.get("section") or entry["section"]
                if not is_known_config_key(section, f["key"], documented):
                    unknown.append((entry["kind"], entry["name"], section, f["key"]))
        assert unknown == [], (
            "settings_schema keys unknown to config validation — document them "
            f"in config.ini.example: {unknown}"
        )
