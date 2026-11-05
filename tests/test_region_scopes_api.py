"""Tests for the Radio Settings page's region-scope editor and its API."""

from __future__ import annotations

import configparser
import json
from unittest.mock import patch

import pytest

from modules import flood_scope


@pytest.fixture
def viewer(tmp_path):
    """BotDataViewer against a temp config + empty DB (migrations create tables)."""
    from modules.web_viewer.app import BotDataViewer

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.add_section("Web_Viewer")
    for key, value in [
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", ""),
    ]:
        config.set("Web_Viewer", key, value)

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as handle:
        config.write(handle)

    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        instance = BotDataViewer(
            db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)
    instance.app.testing = True
    return instance


def _written(tmp_path, path="config.ini"):
    parser = configparser.ConfigParser()
    parser.read(tmp_path / path, encoding="utf-8")
    return parser


class TestScopeNames:
    def test_a_bare_name_gains_the_hash(self):
        assert flood_scope.validate_scope_name("west") == "#west"

    def test_spaces_inside_a_name_are_kept(self):
        """The bot hashes the name whole, so '#north east' is a real region."""
        assert flood_scope.validate_scope_name("north east") == "#north east"

    def test_global_markers_are_canonicalised(self):
        assert flood_scope.validate_scope_name("*") == "*"
        assert flood_scope.validate_scope_name("none") == "None"
        assert flood_scope.validate_scope_name("") == ""

    def test_a_percent_is_refused(self):
        """config.ini is read with interpolation on, so a bare % breaks every
        later read of [Channels] — including the bot's hot reloads."""
        with pytest.raises(ValueError, match="%"):
            flood_scope.validate_scope_name("100%west")

    def test_a_comma_is_refused(self):
        with pytest.raises(ValueError):
            flood_scope.validate_scope_name("west,east")

    def test_an_inner_hash_is_refused(self):
        with pytest.raises(ValueError):
            flood_scope.validate_scope_name("#west#east")

    def test_a_newline_is_refused(self):
        with pytest.raises(ValueError):
            flood_scope.validate_scope_name("west\neast = oops")

    def test_a_bare_hash_has_no_name(self):
        with pytest.raises(ValueError):
            flood_scope.validate_scope_name("#")

    def test_an_overlong_name_is_refused(self):
        with pytest.raises(ValueError):
            flood_scope.validate_scope_name("w" * flood_scope.MAX_SCOPE_NAME_LENGTH)

    def test_the_allowlist_split_matches_the_bot(self):
        """Duplicates collapse and global markers become the flag, not a key —
        the same shape CommandManager._load_flood_scope_keys produces."""
        assert flood_scope.split_allowlist("west, #west, *, east") == (
            ["#west", "#east"], True)
        assert flood_scope.split_allowlist("#west") == (["#west"], False)
        assert flood_scope.split_allowlist("") == ([], False)


class TestRegionScopesGet:
    def test_defaults_are_reply_to_everything(self, viewer):
        data = viewer.app.test_client().get("/api/region-scopes").get_json()
        assert data["allowlist_active"] is False
        assert data["scopes"] == []
        assert data["allow_global"] is False
        assert data["outgoing_override"] == ""
        assert data["target"] == "config.ini"

    def test_configured_values_are_reported_canonically(self, viewer, tmp_path):
        config_path = tmp_path / "config.ini"
        text = config_path.read_text(encoding="utf-8")
        text = text.replace(
            "[Channels]",
            "[Channels]\nflood_scopes = west, #east, *\n"
            "outgoing_flood_scope_override = west\n",
        )
        config_path.write_text(text, encoding="utf-8")

        data = viewer.app.test_client().get("/api/region-scopes").get_json()
        assert data["allowlist_active"] is True
        assert data["scopes"] == ["#west", "#east"]
        assert data["allow_global"] is True
        assert data["outgoing_override"] == "#west"

    def test_a_global_override_reads_back_as_blank(self, viewer, tmp_path):
        """'*', '0' and 'None' all mean global flood; the page shows one thing."""
        config_path = tmp_path / "config.ini"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "[Channels]", "[Channels]\noutgoing_flood_scope_override = None\n"),
            encoding="utf-8",
        )
        data = viewer.app.test_client().get("/api/region-scopes").get_json()
        assert data["outgoing_override"] == ""

    def test_flood_scopes_left_in_the_bot_section_is_surfaced(self, viewer, tmp_path):
        """CommandManager still honours it there, so hiding it would show
        'replies to every scope' while the bot enforces an allowlist."""
        config_path = tmp_path / "config.ini"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "[Bot]", "[Bot]\nflood_scopes = #west\n"),
            encoding="utf-8",
        )
        data = viewer.app.test_client().get("/api/region-scopes").get_json()
        assert data["legacy_section"] == "Bot"
        assert data["scopes"] == ["#west"]

    def test_per_channel_overrides_are_listed(self, viewer, tmp_path):
        config_path = tmp_path / "config.ini"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "[Channels]",
                "[Channels]\nflood_scope.weather = #sea\nflood_scope.general = *\n"),
            encoding="utf-8",
        )
        data = viewer.app.test_client().get("/api/region-scopes").get_json()
        assert data["channel_overrides"] == [
            {"channel": "general", "scope": ""},
            {"channel": "weather", "scope": "#sea"},
        ]


class TestRegionScopesSave:
    def test_save_writes_config_and_queues_a_reload(self, viewer, tmp_path):
        resp = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True,
            "scopes": "west, east",
            "allow_global": True,
            "outgoing_override": "west",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["reload_queued"] is True
        assert body["reload_operation_id"]

        written = _written(tmp_path)
        assert written.get("Channels", "flood_scopes") == "#west, #east, *"
        assert written.get("Channels", "outgoing_flood_scope_override") == "#west"

    def test_the_queued_reload_is_a_real_operation_row(self, viewer):
        op_id = viewer.app.test_client().post(
            "/api/region-scopes",
            json={"allowlist_enabled": False},
        ).get_json()["reload_operation_id"]
        rows = viewer.db_manager.execute_query(
            "SELECT operation_type, status FROM channel_operations WHERE id = ?", (op_id,))
        assert rows[0]["operation_type"] == "config_reload"
        assert rows[0]["status"] == "pending"

    def test_turning_the_allowlist_off_clears_it(self, viewer, tmp_path):
        viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": "#west", "allow_global": False})
        viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": False, "scopes": "", "allow_global": False})
        assert _written(tmp_path).get("Channels", "flood_scopes") == ""

    def test_a_star_typed_into_the_list_folds_into_the_flag(self, viewer, tmp_path):
        body = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": "#west, *", "allow_global": False,
        }).get_json()
        assert body["settings"]["allow_global"] is True
        assert body["settings"]["scopes"] == ["#west"]
        assert _written(tmp_path).get("Channels", "flood_scopes") == "#west, *"

    def test_scopes_accepts_a_list(self, viewer, tmp_path):
        viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": ["west", "#west", "east"]})
        assert _written(tmp_path).get("Channels", "flood_scopes") == "#west, #east"

    def test_a_global_override_is_stored_as_empty(self, viewer, tmp_path):
        """'*' and '' send identically, but a non-empty override makes
        send_channel_message log 'was not applied' on every global send."""
        viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": False, "outgoing_override": "*"})
        assert _written(tmp_path).get("Channels", "outgoing_flood_scope_override") == ""

    def test_an_empty_allowlist_is_refused(self, viewer):
        resp = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": "", "allow_global": False})
        assert resp.status_code == 400
        assert "at least one" in resp.get_json()["error"]

    def test_a_percent_is_refused_and_never_written(self, viewer, tmp_path):
        resp = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": "100%west"})
        assert resp.status_code == 400
        assert "%" in resp.get_json()["error"]
        assert "100%" not in (tmp_path / "config.ini").read_text(encoding="utf-8")
        # And the file configparser reads back must still be intact.
        list(_written(tmp_path).items("Channels"))

    def test_a_newline_cannot_inject_config(self, viewer, tmp_path):
        """A newline would end the key's line and re-parse the rest as INI."""
        resp = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True,
            "scopes": ["west\nrespond_to_dms = false"],
        })
        assert resp.status_code == 400
        text = (tmp_path / "config.ini").read_text(encoding="utf-8")
        assert "respond_to_dms" not in text

    def test_a_bad_outgoing_override_is_refused(self, viewer):
        resp = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": False, "outgoing_override": "we%st"})
        assert resp.status_code == 400

    def test_turning_the_allowlist_off_does_not_hide_a_bot_section_entry(self, viewer, tmp_path):
        """An empty [Channels] flood_scopes hands the allowlist back to [Bot],
        so the page has to keep reporting it rather than showing the "off" the
        operator asked for."""
        config_path = tmp_path / "config.ini"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "[Bot]", "[Bot]\nflood_scopes = #west\n"),
            encoding="utf-8",
        )
        body = viewer.app.test_client().post(
            "/api/region-scopes", json={"allowlist_enabled": False}).get_json()
        assert _written(tmp_path).get("Channels", "flood_scopes") == ""
        assert body["settings"]["legacy_section"] == "Bot"
        assert body["settings"]["allowlist_active"] is True

    def test_saved_settings_are_read_back_by_the_get(self, viewer):
        viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": "west", "allow_global": True,
            "outgoing_override": "#west"})
        data = viewer.app.test_client().get("/api/region-scopes").get_json()
        assert data["scopes"] == ["#west"]
        assert data["allow_global"] is True
        assert data["allowlist_active"] is True
        assert data["outgoing_override"] == "#west"

    def test_the_local_overlay_wins_when_it_owns_the_section(self, viewer, tmp_path):
        """Writing the base copy would be silently overridden at merge time."""
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "config.ini").write_text(
            "[Channels]\nflood_scopes = #old\n", encoding="utf-8")

        body = viewer.app.test_client().post("/api/region-scopes", json={
            "allowlist_enabled": True, "scopes": "#west"}).get_json()
        assert body["settings"]["target"] == "local/config.ini"

        local = configparser.ConfigParser()
        local.read(local_dir / "config.ini", encoding="utf-8")
        assert local.get("Channels", "flood_scopes") == "#west"
        base = configparser.ConfigParser()
        base.read(tmp_path / "config.ini", encoding="utf-8")
        assert not base.has_option("Channels", "flood_scopes")


class TestDeviceDefaultScopeEndpoint:
    """The radio's own default scope is queued through the firmware config
    write, beside the path hash mode."""

    def _payload(self, viewer, body):
        resp = viewer.app.test_client().post(
            "/api/radio/firmware/config/write", json=body)
        return resp, resp.get_json()

    def test_a_scope_is_queued_in_its_canonical_form(self, viewer):
        resp, body = self._payload(viewer, {"default_flood_scope": "west"})
        assert resp.status_code == 200
        rows = viewer.db_manager.execute_query(
            "SELECT payload_data FROM channel_operations WHERE id = ?",
            (body["operation_id"],))
        assert json.loads(rows[0]["payload_data"]) == {"default_flood_scope": "#west"}

    def test_a_blank_scope_queues_the_clear(self, viewer):
        resp, body = self._payload(viewer, {"default_flood_scope": ""})
        assert resp.status_code == 200
        rows = viewer.db_manager.execute_query(
            "SELECT payload_data FROM channel_operations WHERE id = ?",
            (body["operation_id"],))
        assert json.loads(rows[0]["payload_data"]) == {"default_flood_scope": ""}

    def test_a_global_marker_is_the_clear_too(self, viewer):
        resp, body = self._payload(viewer, {"default_flood_scope": "*"})
        rows = viewer.db_manager.execute_query(
            "SELECT payload_data FROM channel_operations WHERE id = ?",
            (body["operation_id"],))
        assert json.loads(rows[0]["payload_data"])["default_flood_scope"] == ""

    def test_a_name_the_radio_cannot_store_is_refused(self, viewer):
        resp, body = self._payload(viewer, {"default_flood_scope": "#" + "w" * 30})
        assert resp.status_code == 400
        assert "30" in body["error"]

    def test_a_non_ascii_name_is_refused(self, viewer):
        resp, body = self._payload(viewer, {"default_flood_scope": "süd"})
        assert resp.status_code == 400
        assert "ASCII" in body["error"]

    def test_the_path_hash_mode_still_works_alone(self, viewer):
        resp, body = self._payload(viewer, {"path_hash_mode": 2})
        assert resp.status_code == 200
        rows = viewer.db_manager.execute_query(
            "SELECT payload_data FROM channel_operations WHERE id = ?",
            (body["operation_id"],))
        assert json.loads(rows[0]["payload_data"]) == {"path_hash_mode": 2}

    def test_an_empty_body_is_still_rejected(self, viewer):
        resp, body = self._payload(viewer, {"nonsense": 1})
        assert resp.status_code == 400
        assert "default_flood_scope" in body["error"]


class TestRadioPage:
    def test_the_card_renders_on_the_radio_page(self, viewer):
        body = viewer.app.test_client().get("/radio").data.decode()
        assert "Region Scopes" in body
        assert 'id="saveRegionScopesBtn"' in body

    def test_the_device_default_scope_field_sits_in_node_settings(self, viewer):
        body = viewer.app.test_client().get("/radio").data.decode()
        assert 'id="nodeDefaultScope"' in body
        # Next to the path hash size, which is the other firmware-stored
        # setting on that card.
        assert body.index('id="nodePathHashMode"') < body.index('id="nodeDefaultScope"')
        assert body.index('id="nodeDefaultScope"') < body.index('id="nodeName"')
