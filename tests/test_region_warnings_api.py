"""Tests for the /region-warnings page and its API."""

from __future__ import annotations

import configparser
from datetime import datetime
from unittest.mock import patch

import pytest

from modules import region_warning


@pytest.fixture
def viewer(tmp_path):
    """BotDataViewer against a temp config + empty DB (migrations create tables)."""
    from modules.web_viewer.app import BotDataViewer

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "bot_name", "TestBot")
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


def _seed_tally(viewer, channel="#general", scoped=30, globally=10, unknown=5):
    today = datetime.now().date().isoformat()
    with viewer.db_manager.connection() as conn:
        conn.execute(
            "INSERT INTO region_scope_daily "
            "(date, channel, scoped_count, global_count, unknown_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (today, channel, scoped, globally, unknown),
        )
        conn.commit()


class TestRegionWarningsPage:
    def test_page_renders(self, viewer):
        resp = viewer.app.test_client().get("/region-warnings")
        assert resp.status_code == 200
        assert b"Region Warnings" in resp.data

    def test_page_script_carries_a_csp_nonce(self, viewer):
        """The endpoint is in the nonce-hardened set, so its script must be nonced."""
        resp = viewer.app.test_client().get("/region-warnings")
        body = resp.data.decode()
        assert "<script nonce=" in body
        assert "unsafe-inline" not in resp.headers["Content-Security-Policy"].split("style-src")[0]


class TestRegionWarningsApi:
    def test_defaults_when_section_absent(self, viewer):
        data = viewer.app.test_client().get("/api/region-warnings").get_json()
        assert data["settings"]["enabled"] == "false"
        assert data["settings"]["dry_run"] == "true"
        assert data["traffic"]["channels"] == []
        assert data["events"] == []

    def test_limits_account_for_the_bot_name(self, viewer):
        data = viewer.app.test_client().get("/api/region-warnings").get_json()
        assert data["limits"]["dm"] == region_warning.DM_BODY_LIMIT
        assert data["limits"]["channel"] == max(130, 160 - len("TestBot") - 2)

    def test_traffic_reflects_seeded_tallies(self, viewer):
        _seed_tally(viewer)
        data = viewer.app.test_client().get("/api/region-warnings").get_json()
        channel = data["traffic"]["channels"][0]
        assert channel["channel"] == "#general"
        assert channel["unscoped_pct"] == 25.0
        assert len(data["series"]) == 14

    def test_window_is_clamped(self, viewer):
        data = viewer.app.test_client().get("/api/region-warnings?days=9999").get_json()
        assert data["traffic"]["days"] == 90

    def test_bad_window_falls_back(self, viewer):
        data = viewer.app.test_client().get("/api/region-warnings?days=abc").get_json()
        assert data["traffic"]["days"] == 14


class TestRegionWarningsSave:
    def test_save_writes_config_and_queues_reload(self, viewer, tmp_path):
        resp = viewer.app.test_client().post("/api/region-warnings/settings", json={
            "enabled": True,
            "dry_run": True,
            "delivery": "channel",
            "channels": "#General, general, weather",
            "message": "Set a region, {sender}",
            "min_unscoped_messages": 5,
            "per_sender_cooldown_hours": 24,
            "mesh_cooldown_minutes": 15,
            "max_warnings_per_day": 3,
            "track_traffic": False,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["reload_queued"] is True
        # Duplicates collapse and the leading # is dropped.
        assert body["settings"]["channels"] == "general, weather"

        text = (tmp_path / "config.ini").read_text(encoding="utf-8")
        assert "[Region_Warnings]" in text
        written = configparser.ConfigParser()
        written.read(tmp_path / "config.ini", encoding="utf-8")
        settings = region_warning.load_settings(written)
        assert settings.enabled is True
        assert settings.delivery == "channel"
        assert settings.channels == ("general", "weather")
        assert settings.min_unscoped_messages == 5
        assert settings.track_traffic is False

    def test_rejects_unknown_delivery(self, viewer):
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings", json={"delivery": "smoke signal"})
        assert resp.status_code == 400
        assert "delivery" in resp.get_json()["error"]

    def test_rejects_out_of_range_number(self, viewer):
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "min_unscoped_messages": 0})
        assert resp.status_code == 400

    def test_rejects_non_numeric(self, viewer):
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "mesh_cooldown_minutes": "soon"})
        assert resp.status_code == 400

    def test_rejects_multiline_message(self, viewer):
        """A newline in an INI value would break the file open at the next read."""
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "message": "line one\nline two"})
        assert resp.status_code == 400

    def test_rejects_percent_in_the_message(self, viewer):
        """A bare % makes configparser raise on every later read of the section,
        which would reject the bot's hot reloads until the file was hand-edited."""
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "message": "100% of the mesh, set a region"})
        assert resp.status_code == 400
        assert "%" in resp.get_json()["error"]

    def test_percent_is_never_written_to_config(self, viewer, tmp_path):
        viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "message": "50% done"})
        text = (tmp_path / "config.ini").read_text(encoding="utf-8")
        assert "50%" not in text
        # And the file configparser reads back must still be intact.
        written = configparser.ConfigParser()
        written.read(tmp_path / "config.ini", encoding="utf-8")
        if written.has_section("Region_Warnings"):
            list(written.items("Region_Warnings"))

    def test_rejects_an_overlong_message(self, viewer):
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "message": "x" * 501})
        assert resp.status_code == 400

    def test_budget_separates_delivered_from_attempted(self, viewer):
        with viewer.db_manager.connection() as conn:
            for action in ("sent", "failed", "failed"):
                conn.execute(
                    "INSERT INTO region_warning_events "
                    "(created_at, sender_id, channel, delivery, action) VALUES (?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(sep=" ", timespec="seconds"),
                     "Ann", "#gen", "dm", action),
                )
            conn.commit()
        budget = viewer.app.test_client().get("/api/region-warnings").get_json()["budget"]
        assert budget["used_today"] == 3
        assert budget["delivered_today"] == 1
        assert budget["failed_today"] == 2
        assert budget["previewed_today"] == 0
        assert budget["withheld_today"] == 0

    def test_budget_reports_warnings_withheld_for_want_of_a_contact(self, viewer):
        """The viewer reads the bot's counter, so an empty log can explain itself."""
        import json

        viewer.db_manager.set_metadata(
            region_warning.WITHHELD_METADATA_KEY,
            json.dumps({"date": datetime.now().date().isoformat(), "count": 7}),
        )
        budget = viewer.app.test_client().get("/api/region-warnings").get_json()["budget"]
        assert budget["withheld_today"] == 7

    def test_a_stale_withheld_counter_is_not_reported_as_today(self, viewer):
        import json

        viewer.db_manager.set_metadata(
            region_warning.WITHHELD_METADATA_KEY,
            json.dumps({"date": "2020-01-01", "count": 99}),
        )
        budget = viewer.app.test_client().get("/api/region-warnings").get_json()["budget"]
        assert budget["withheld_today"] == 0

    def test_empty_message_falls_back_to_the_default(self, viewer):
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings", json={"delivery": "dm", "message": "   "})
        assert resp.status_code == 200
        assert resp.get_json()["settings"]["message"] == region_warning.DEFAULT_MESSAGE

    def test_channels_accepts_a_list(self, viewer):
        resp = viewer.app.test_client().post(
            "/api/region-warnings/settings",
            json={"delivery": "dm", "channels": ["#a", "b"]})
        assert resp.get_json()["settings"]["channels"] == "a, b"

    def test_saved_settings_are_read_back_by_the_get(self, viewer):
        viewer.app.test_client().post("/api/region-warnings/settings", json={
            "enabled": True, "dry_run": False, "delivery": "dm",
            "max_warnings_per_day": 2,
        })
        data = viewer.app.test_client().get("/api/region-warnings").get_json()
        assert data["settings"]["enabled"] == "true"
        assert data["settings"]["dry_run"] == "false"
        assert data["budget"]["cap"] == 2
