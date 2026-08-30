"""Tests for config schema sync, template generation, and TUI schema loading."""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

import pytest

from modules.config_schema import (
    canonical_sections_missing_from_example,
    get_example_sections,
    is_known_config_key,
    load_documented_keys_from_example,
    validate_config_keys,
)
from modules.config_validation import (
    CANONICAL_NON_COMMAND_SECTIONS,
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

    def test_localization_auto_detection_is_documented(self):
        keys = load_documented_keys_from_example(EXAMPLE_PATH).get("Localization", {})
        assert "auto_detect_language" in keys


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

    def test_invalid_short_url_service_warns(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("""[External_Data]
short_url_website_service = shlnik
""")

        warnings = validate_config_keys(cfg)

        assert warnings == [(
            SEVERITY_WARNING,
            "[External_Data] 'short_url_website_service' must be one of: "
            "gd, shlink (got 'shlnik').",
        )]


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

    def test_graph_write_strategy_defaults_stay_in_sync(self):
        from modules.commands.path_command import PathCommand

        cfg = configparser.ConfigParser()
        cfg.read(EXAMPLE_PATH)
        field = next(
            item
            for item in PathCommand.settings_schema
            if item["key"] == "graph_write_strategy"
        )
        assert field["default"] == "batched"
        assert cfg.get("Path_Command", "graph_write_strategy") == field["default"]


class TestUnknownKeyWarnings:
    """Misspelled keys must be reported, not silently ignored.

    The v1.0.0 release notes promise unknown/misspelled keys are surfaced at
    startup; before this, only section names and a hardcoded [Connection]
    host/port pair were checked.
    """

    _BASE = """[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot

[Channels]
monitor_channels = general
"""

    def _warnings(self, extra: str) -> list[str]:
        cfg = configparser.ConfigParser()
        cfg.read_string(self._BASE + extra)
        return [m for sev, m in validate_config_keys(cfg) if sev == SEVERITY_WARNING]

    @pytest.mark.parametrize(
        "filename",
        ["config.ini.example", "config.ini.minimal-example", "config.ini.quickstart"],
    )
    def test_shipped_examples_have_no_unknown_keys(self, filename):
        """No false positives on our own templates — the noise-floor gate."""
        path = PROJECT_ROOT / filename
        if not path.exists():
            pytest.skip(f"{filename} not present")
        cfg = configparser.ConfigParser()
        cfg.read(str(path), encoding="utf-8")
        unknown = [
            m for _sev, m in validate_config_keys(cfg)
            if "unknown key" in m or "is not valid" in m
        ]
        assert unknown == [], f"{filename} tripped unknown-key validation: {unknown}"

    def test_misspelled_non_command_key_is_reported(self):
        warnings = self._warnings(
            "\n[Feed_Manager]\nfeed_manager_enabled = true\nmax_posts_per_chek = 5\n"
        )
        assert any("max_posts_per_chek" in w for w in warnings)
        assert any("max_posts_per_check" in w for w in warnings), (
            "expected a 'did you mean' suggestion"
        )

    def test_misspelled_command_key_is_reported(self):
        """Command sections were skipped wholesale before this."""
        warnings = self._warnings("\n[Aqi_Command]\nenabled = true\nchanels = general\n")
        assert any("chanels" in w and "channels" in w for w in warnings)

    def test_misspelled_service_key_is_reported(self):
        warnings = self._warnings(
            "\n[Weather_Service]\nenabled = true\nsilence_mesh_ouput = true\n"
        )
        assert any("silence_mesh_ouput" in w for w in warnings)

    def test_standard_command_keys_are_not_reported(self):
        warnings = self._warnings(
            "\n[Aqi_Command]\nenabled = true\nchannels = general\naliases = air\n"
            "cooldown_queue_threshold_seconds = 30\n"
        )
        assert warnings == []

    def test_numbered_mqtt_broker_keys_are_not_reported(self):
        """config.ini.example documents broker 1 + an mqttN_* placeholder block."""
        warnings = self._warnings(
            "\n[PacketCapture]\nmqtt3_server = example.org\nmqtt3_port = 1883\n"
            "mqtt3_jwt_ttl_seconds = 60\n"
        )
        assert warnings == []

    def test_bogus_mqtt_broker_key_is_reported(self):
        warnings = self._warnings("\n[PacketCapture]\nmqtt3_bogus_key = x\n")
        assert any("mqtt3_bogus_key" in w for w in warnings)

    def test_dynamic_sections_accept_any_key(self):
        warnings = self._warnings(
            "\n[Keywords]\nanything_at_all = hi\n"
            "\n[Rate_Limits]\nchannel.BotCmds_seconds = 5\n"
        )
        assert warnings == []

    def test_undocumented_third_party_section_keys_are_not_reported(self):
        """Local/third-party plugins have no documented key list; the unknown
        *section* diagnostic already covers them, so don't double-report."""
        warnings = self._warnings(
            "\n[MyThirdParty_Command]\nenabled = true\nwhatever_custom = 1\n"
        )
        assert warnings == []

    def test_connection_host_port_keep_specific_guidance(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(
            "[Connection]\nconnection_type = tcp\nhost = 1.2.3.4\nport = 5050\n"
            "\n[Bot]\nbot_name = TestBot\n\n[Channels]\nmonitor_channels = general\n"
        )
        warnings = [m for sev, m in validate_config_keys(cfg) if sev == SEVERITY_WARNING]
        assert any("host" in w and "hostname" in w for w in warnings)
        assert any("port" in w and "tcp_port" in w for w in warnings)


class TestEveryConfigReadKeyIsKnown:
    """Every key the code reads must be known to unknown-key validation.

    Without this gate, adding a `config.get("Bot", "new_knob")` read silently
    turns into an "unknown key" warning on the startup of every user who sets
    it — noise that buries the real typos the validator exists to surface.
    Register new keys in config.ini.example or in SECTIONS.
    """

    GETTERS = {"get", "getint", "getboolean", "getfloat", "has_option"}

    @staticmethod
    def _receiver_is_config(call: ast.Call) -> bool:
        """True when the call's receiver chain ends in .config / .cfg."""
        cur = call.func.value
        while isinstance(cur, ast.Attribute):
            if cur.attr in ("config", "cfg", "_config"):
                return True
            cur = cur.value
        return isinstance(cur, ast.Name) and cur.id in ("config", "cfg", "_config")

    @staticmethod
    def _is_config_section(name: str) -> bool:
        return (
            name in CANONICAL_NON_COMMAND_SECTIONS
            or name.endswith("_Command")
            or name.endswith("_Service")
        )

    def _literal_config_reads(self):
        """Yield (section, key, location) for literal config reads in the tree."""
        roots = [PROJECT_ROOT / "modules", PROJECT_ROOT / "scripts"]
        for root in roots:
            for path in root.rglob("*.py"):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                        continue
                    if node.func.attr not in self.GETTERS or len(node.args) < 2:
                        continue
                    if not self._receiver_is_config(node):
                        continue
                    section, key = node.args[0], node.args[1]
                    if not (isinstance(section, ast.Constant) and isinstance(section.value, str)):
                        continue
                    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                        continue
                    if not self._is_config_section(section.value):
                        continue
                    rel = path.relative_to(PROJECT_ROOT)
                    yield section.value, key.value, f"{rel}:{node.lineno}"

    def test_every_config_read_key_is_known(self):
        example_keys = load_documented_keys_from_example(EXAMPLE_PATH)
        unknown = [
            f"[{section}] {key}  ({loc})"
            for section, key, loc in self._literal_config_reads()
            if not is_known_config_key(section, key.lower(), example_keys)
        ]
        assert unknown == [], (
            "these keys are read by the code but unknown to validation, so setting "
            "them warns on every startup — document them in config.ini.example or "
            "add them to SECTIONS:\n  " + "\n  ".join(sorted(unknown))
        )
