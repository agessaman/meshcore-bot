"""Tests for the plugin settings UI stack.

Covers modules/ini_writer.py (comment-preserving INI writes),
modules/settings_schema.py (validation + view assembly),
modules/settings_store.py (INI backend), and the /api/plugins endpoints.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.ini_writer import backup_config, update_ini_values
from modules.settings_schema import (
    build_plugin_settings_view,
    read_enabled,
    to_config_string,
    validate_field,
)
from modules.settings_store import ConfigIniStore, get_settings_store

# ---------------------------------------------------------------------------
# ini_writer
# ---------------------------------------------------------------------------

SAMPLE_INI = """\
# Top comment stays
[Bot]
; bot name comment
bot_name = MeshCoreBot
rate_limit_seconds = 10

[Weather]
default_state = WA
"""


class TestUpdateIniValues:
    def _write(self, tmp_path: Path, text: str = SAMPLE_INI) -> Path:
        p = tmp_path / "config.ini"
        p.write_text(text, encoding="utf-8")
        return p

    def test_change_existing_value_preserves_comments(self, tmp_path):
        p = self._write(tmp_path)
        result = update_ini_values(str(p), {"Bot": {"rate_limit_seconds": "5"}})
        text = p.read_text(encoding="utf-8")
        assert "rate_limit_seconds = 5" in text
        assert "# Top comment stays" in text
        assert "; bot name comment" in text
        assert ("Bot", "rate_limit_seconds") in result["changed"]

    def test_unchanged_value_not_reported_changed(self, tmp_path):
        p = self._write(tmp_path)
        result = update_ini_values(str(p), {"Bot": {"bot_name": "MeshCoreBot"}})
        assert result["changed"] == []

    def test_new_key_appended_to_existing_section(self, tmp_path):
        p = self._write(tmp_path)
        result = update_ini_values(str(p), {"Bot": {"new_key": "42"}})
        cfg = configparser.ConfigParser()
        cfg.read(p, encoding="utf-8")
        assert cfg.get("Bot", "new_key") == "42"
        # Must land in [Bot], not [Weather].
        assert not cfg.has_option("Weather", "new_key")
        assert ("Bot", "new_key") in result["added"]

    def test_new_section_appended_at_eof(self, tmp_path):
        p = self._write(tmp_path)
        result = update_ini_values(str(p), {"Brand_New": {"alpha": "1"}})
        cfg = configparser.ConfigParser()
        cfg.read(p, encoding="utf-8")
        assert cfg.get("Brand_New", "alpha") == "1"
        assert "Brand_New" in result["created_sections"]

    def test_delete_key(self, tmp_path):
        p = self._write(tmp_path)
        result = update_ini_values(str(p), {}, deletes={"Weather": ["default_state"]})
        cfg = configparser.ConfigParser()
        cfg.read(p, encoding="utf-8")
        assert not cfg.has_option("Weather", "default_state")
        assert ("weather", "default_state") in result["removed"]

    def test_case_insensitive_section_and_key_match(self, tmp_path):
        p = self._write(tmp_path)
        update_ini_values(str(p), {"bot": {"BOT_NAME": "Zed"}})
        cfg = configparser.ConfigParser()
        cfg.read(p, encoding="utf-8")
        assert cfg.get("Bot", "bot_name") == "Zed"
        # No duplicate key line was appended.
        assert p.read_text(encoding="utf-8").lower().count("bot_name") == 1

    def test_backup_created(self, tmp_path):
        p = self._write(tmp_path)
        result = update_ini_values(str(p), {"Bot": {"bot_name": "X"}})
        assert Path(result["backup_path"]).exists()
        assert "config.ini.bak." in result["backup_path"]

    def test_value_with_percent_survives(self, tmp_path):
        """'%' must round-trip: cron/strftime values would break interpolation."""
        p = self._write(tmp_path)
        update_ini_values(str(p), {"Bot": {"fmt": "%H:%M"}})
        raw = configparser.RawConfigParser()
        raw.read(p, encoding="utf-8")
        assert raw.get("Bot", "fmt") == "%H:%M"

    def test_noop_returns_empty_summary(self, tmp_path):
        p = self._write(tmp_path)
        before = p.read_text(encoding="utf-8")
        result = update_ini_values(str(p), {})
        assert result["backup_path"] == ""
        assert p.read_text(encoding="utf-8") == before

    def test_backup_prune_keeps_bounded_count(self, tmp_path):
        p = self._write(tmp_path)
        for i in range(25):
            # Distinct timestamps not guaranteed; emulate by naming manually.
            (tmp_path / f"config.ini.bak.20260101-{i:06d}").write_text("x")
        backup_config(str(p))
        backups = list(tmp_path.glob("config.ini.bak.*"))
        assert len(backups) <= 20


# ---------------------------------------------------------------------------
# settings_schema.validate_field
# ---------------------------------------------------------------------------

class TestValidateField:
    def test_bool_accepts_configparser_forms(self):
        f = {"key": "x", "type": "bool"}
        for raw, expected in [("true", True), ("no", False), ("1", True), ("off", False)]:
            ok, val, err = validate_field(f, raw)
            assert ok and val is expected and err is None

    def test_bool_rejects_garbage(self):
        ok, _, err = validate_field({"key": "x", "type": "bool", "label": "Flag"}, "maybe")
        assert not ok and "Flag" in err

    def test_int_bounds(self):
        f = {"key": "x", "type": "int", "min": 1, "max": 10}
        assert validate_field(f, "5")[0]
        assert not validate_field(f, "0")[0]
        assert not validate_field(f, "11")[0]
        assert not validate_field(f, "abc")[0]

    def test_empty_optional_returns_default(self):
        f = {"key": "x", "type": "int", "default": 7}
        ok, val, _ = validate_field(f, "")
        assert ok and val == 7

    def test_empty_required_fails(self):
        ok, _, err = validate_field({"key": "x", "type": "str", "required": True, "label": "Name"}, "")
        assert not ok and "required" in err

    def test_enum_membership(self):
        f = {"key": "x", "type": "enum", "options": [{"value": "a"}, {"value": "b"}]}
        assert validate_field(f, "a") == (True, "a", None)
        assert not validate_field(f, "c")[0]

    def test_list_split_and_pattern(self):
        f = {"key": "x", "type": "list", "pattern": r"[a-z]+"}
        ok, val, _ = validate_field(f, "abc, def")
        assert ok and val == ["abc", "def"]
        assert not validate_field(f, "abc, 123")[0]

    def test_str_pattern(self):
        f = {"key": "x", "type": "str", "pattern": r"[0-9a-f]{4}"}
        assert validate_field(f, "beef")[0]
        assert not validate_field(f, "zzzz")[0]

    def test_to_config_string_bool_and_list(self):
        assert to_config_string({"type": "bool"}, True) == "true"
        assert to_config_string({"type": "list"}, ["a", "b"]) == "a, b"


# ---------------------------------------------------------------------------
# settings_schema.read_enabled — legacy aliases
# ---------------------------------------------------------------------------

class TestReadEnabled:
    def test_canonical_key_wins(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Joke_Command")
        cfg.set("Joke_Command", "enabled", "false")
        assert read_enabled(cfg, "Joke_Command", True) is False

    def test_legacy_jokes_alias(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Jokes")
        cfg.set("Jokes", "joke_enabled", "false")
        assert read_enabled(cfg, "Joke_Command", True) is False

    def test_default_when_absent(self):
        cfg = configparser.ConfigParser()
        assert read_enabled(cfg, "Anything_Command", True) is True
        assert read_enabled(cfg, "SomeService", False) is False


# ---------------------------------------------------------------------------
# settings_schema.build_plugin_settings_view
# ---------------------------------------------------------------------------

class TestBuildView:
    @pytest.fixture(scope="class")
    def view(self):
        cfg = configparser.ConfigParser()
        return build_plugin_settings_view(cfg)

    def _entry(self, view, kind, name):
        return next((e for e in view if e["kind"] == kind and e["name"] == name), None)

    def test_discovers_commands_and_services(self, view):
        kinds = {e["kind"] for e in view}
        assert kinds == {"command", "service"}
        assert len(view) > 20

    def test_optin_commands_show_disabled_by_default(self, view):
        """Announcements/Greeter read enabled with fallback=False in __init__;
        the UI must mirror that instead of showing them 'on'."""
        for name in ("announcements", "greeter"):
            entry = self._entry(view, "command", name)
            assert entry is not None
            assert entry["enabled"] is False, f"{name} should default to disabled"

    def test_regular_command_defaults_enabled(self, view):
        entry = self._entry(view, "command", "ping")
        assert entry is not None and entry["enabled"] is True

    def test_services_default_disabled(self, view):
        assert all(e["enabled"] is False for e in view if e["kind"] == "service")

    def test_every_command_gets_channels_field(self, view):
        for e in view:
            if e["kind"] == "command":
                assert any(f["key"].lower() == "channels" for f in e["fields"]), (
                    f"{e['name']} is missing the injected channels field"
                )


# ---------------------------------------------------------------------------
# settings_store
# ---------------------------------------------------------------------------

class TestConfigIniStore:
    def test_write_values_updates_memory_and_disk(self, tmp_path):
        p = tmp_path / "config.ini"
        p.write_text("[Bot]\nbot_name = A\n", encoding="utf-8")
        cfg = configparser.ConfigParser()
        cfg.read(p, encoding="utf-8")
        store = ConfigIniStore(cfg, str(p))
        store.write_values("Bot", {"bot_name": "B"})
        assert cfg.get("Bot", "bot_name") == "B"
        assert "bot_name = B" in p.read_text(encoding="utf-8")

    def test_percent_value_skips_memory_but_hits_disk(self, tmp_path):
        p = tmp_path / "config.ini"
        p.write_text("[Bot]\n", encoding="utf-8")
        cfg = configparser.ConfigParser()  # BasicInterpolation rejects lone %
        cfg.read(p, encoding="utf-8")
        store = ConfigIniStore(cfg, str(p))
        store.write_values("Bot", {"fmt": "%H"})
        raw = configparser.RawConfigParser()
        raw.read(p, encoding="utf-8")
        assert raw.get("Bot", "fmt") == "%H"

    def test_database_mode_falls_back_to_ini(self, tmp_path):
        p = tmp_path / "config.ini"
        p.write_text("[Bot]\npersist_settings = database\n", encoding="utf-8")
        cfg = configparser.ConfigParser()
        cfg.read(p, encoding="utf-8")
        store = get_settings_store(cfg, str(p))
        assert isinstance(store, ConfigIniStore)


# ---------------------------------------------------------------------------
# /api/plugins endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def viewer(tmp_path):
    """BotDataViewer against a temp config + empty DB (migrations create tables)."""
    from modules.web_viewer.app import BotDataViewer

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.add_section("Web_Viewer")
    for k, v in [
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", ""),
    ]:
        config.set("Web_Viewer", k, v)
    config.add_section("Channels_List")
    config.set("Channels_List", "seattle", "Seattle chat")
    config.set("Channels_List", "tacoma", "Tacoma chat")

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as f:
        config.write(f)

    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        v = BotDataViewer(db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)
    v.app.testing = True
    return v


class TestPluginsApi:
    def test_get_plugins_lists_view(self, viewer):
        client = viewer.app.test_client()
        resp = client.get("/api/plugins")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "plugins" in data and len(data["plugins"]) > 20

    def test_save_unknown_plugin_404(self, viewer):
        client = viewer.app.test_client()
        resp = client.post("/api/plugins/command/nope", json={"values": {}})
        assert resp.status_code == 404

    def test_save_validation_error_400(self, viewer):
        client = viewer.app.test_client()
        resp = client.post(
            "/api/plugins/command/greeter",
            json={"values": {"rollout_days": "not-a-number"}},
        )
        assert resp.status_code == 400
        assert "rollout_days" in resp.get_json().get("errors", {})

    def test_save_writes_config_and_queues_reload(self, viewer, tmp_path):
        client = viewer.app.test_client()
        resp = client.post(
            "/api/plugins/command/ping",
            json={"enabled": True, "values": {}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["reload_queued"] is True
        text = (tmp_path / "config.ini").read_text(encoding="utf-8")
        assert "[Ping_Command]" in text
        rows = viewer.db_manager.execute_query(
            "SELECT operation_type, status FROM channel_operations "
            "WHERE operation_type = 'config_reload'"
        )
        assert rows and rows[0]["status"] == "pending"

    def test_omitted_dynamic_sections_do_not_wipe_managed_keys(self, viewer, tmp_path):
        """A payload without dynamic_sections must leave [Channels_List] alone."""
        client = viewer.app.test_client()
        resp = client.post(
            "/api/plugins/command/channels",
            json={"enabled": True, "values": {}},
        )
        assert resp.status_code == 200
        cfg = configparser.ConfigParser()
        cfg.read(tmp_path / "config.ini", encoding="utf-8")
        assert cfg.get("Channels_List", "seattle") == "Seattle chat"
        assert cfg.get("Channels_List", "tacoma") == "Tacoma chat"

    def test_dynamic_sections_row_delete_and_add(self, viewer, tmp_path):
        client = viewer.app.test_client()
        resp = client.post(
            "/api/plugins/command/channels",
            json={
                "enabled": True,
                "values": {},
                "dynamic_sections": {
                    "Channels_List": [
                        {"key": "seattle", "value": "Seattle chat"},
                        {"key": "olympia", "value": "Olympia chat"},
                    ]
                },
            },
        )
        assert resp.status_code == 200, resp.get_json()
        cfg = configparser.ConfigParser()
        cfg.read(tmp_path / "config.ini", encoding="utf-8")
        assert cfg.get("Channels_List", "olympia") == "Olympia chat"
        assert not cfg.has_option("Channels_List", "tacoma")

    def test_dynamic_key_with_separator_rejected(self, viewer):
        client = viewer.app.test_client()
        resp = client.post(
            "/api/plugins/command/channels",
            json={
                "enabled": True,
                "values": {},
                "dynamic_sections": {
                    "Channels_List": [{"key": "bad=key", "value": "x"}]
                },
            },
        )
        assert resp.status_code == 400

    def test_reload_status_endpoint(self, viewer):
        client = viewer.app.test_client()
        resp = client.get("/api/plugins/reload-status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] is None
