"""Tests for the /repeater-watch page and its API (#353)."""

from __future__ import annotations

import configparser
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from modules import repeater_watch

KEY_A = "a1b2" + "0" * 60
KEY_B = "c3d4" + "1" * 60
WEBHOOK = "https://discord.com/api/webhooks/123/abc"


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


def _add_repeater(viewer, key, name, hours_ago, role="repeater"):
    heard = datetime.now() - timedelta(hours=hours_ago)
    viewer.db_manager.execute_update(
        "INSERT INTO complete_contact_tracking (public_key, name, role, last_heard) VALUES (?, ?, ?, ?)",
        (key, name, role, heard),
    )


def _written(tmp_path):
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / "config.ini", encoding="utf-8")
    return repeater_watch.load_settings(cfg)


def _save(viewer, **body):
    return viewer.app.test_client().post("/api/repeater-watch/settings", json=body)


class TestPage:
    def test_page_renders_with_a_nonce(self, viewer):
        resp = viewer.app.test_client().get("/repeater-watch")
        assert resp.status_code == 200
        assert b"Repeater Watch" in resp.data
        assert "<script nonce=" in resp.data.decode()
        assert "unsafe-inline" not in resp.headers["Content-Security-Policy"].split("style-src")[0]

    def test_nav_links_to_the_page(self, viewer):
        body = viewer.app.test_client().get("/").data.decode()
        assert 'href="/repeater-watch"' in body


class TestApi:
    def test_defaults_when_section_absent(self, viewer):
        data = viewer.app.test_client().get("/api/repeater-watch").get_json()
        assert data["settings"]["enabled"] == "false"
        assert data["status"]["rows"] == []
        assert data["events"] == []
        assert data["candidates"] == []

    def test_candidates_and_watched_status(self, viewer, tmp_path):
        _add_repeater(viewer, KEY_A, "Hilltop", 2)
        _add_repeater(viewer, KEY_B, "Ridge", 30)
        _add_repeater(viewer, "e" * 64, "Phone", 1, role="companion")
        assert _save(viewer, watch=[KEY_B]).status_code == 200
        data = viewer.app.test_client().get("/api/repeater-watch").get_json()
        assert [c["name"] for c in data["candidates"]] == ["Hilltop", "Ridge"]
        assert data["companions"] == ["Phone"]
        rows = data["status"]["rows"]
        assert [r["name"] for r in rows] == ["Ridge"]
        assert rows[0]["last_advert"] is not None

    def test_secrets_are_not_sent_to_the_browser(self, viewer):
        assert _save(viewer, discord_webhook_urls=WEBHOOK, telegram_bot_token="123:abc").status_code == 200
        settings = viewer.app.test_client().get("/api/repeater-watch").get_json()["settings"]
        assert settings["discord_webhook_urls"] == ""
        assert settings["discord_webhook_urls_set"] == "true"
        assert settings["telegram_bot_token"] == ""
        assert settings["telegram_bot_token_set"] == "true"


class TestSave:
    def test_save_writes_config_and_queues_reload(self, viewer, tmp_path):
        resp = _save(
            viewer, enabled=True, watch=[KEY_A.upper(), KEY_A, KEY_B[:10]],
            offline_after_hours=36, check_interval_minutes=30, notify_recovery=False,
            channels="#ops, #ops", dm_recipients="Alice, Bob", flood_scope="west",
        )
        body = resp.get_json()
        assert resp.status_code == 200 and body["success"] is True
        assert body["reload_queued"] is True
        s = _written(tmp_path)
        assert s.enabled is True
        assert s.watch == (KEY_A, KEY_B[:10])
        assert s.offline_after_hours == 36
        assert s.notify_recovery is False
        assert s.channels == ("#ops",)
        assert s.dm_recipients == ("Alice", "Bob")
        assert s.flood_scope == "#west"

    def test_blank_secret_keeps_the_saved_one(self, viewer, tmp_path):
        _save(viewer, discord_webhook_urls=WEBHOOK, telegram_bot_token="123:abc")
        _save(viewer, discord_webhook_urls="", discord_webhook_urls_keep=True,
              telegram_bot_token="", telegram_bot_token_keep=True)
        s = _written(tmp_path)
        assert s.discord_webhook_urls == (WEBHOOK,)
        assert s.telegram_bot_token == "123:abc"

    def test_secret_can_be_removed(self, viewer, tmp_path):
        _save(viewer, discord_webhook_urls=WEBHOOK, telegram_bot_token="123:abc")
        _save(viewer, discord_webhook_urls="", discord_webhook_urls_keep=False,
              telegram_bot_token="", telegram_bot_token_keep=False)
        s = _written(tmp_path)
        assert s.discord_webhook_urls == ()
        assert s.telegram_bot_token == ""

    @pytest.mark.parametrize(("body", "message"), [
        ({"watch": ["not-a-key"]}, "is not a repeater public key"),
        ({"watch": ["abcd"]}, "is not a repeater public key"),
        ({"offline_after_hours": 0}, "Offline after must be between 1 and 720"),
        ({"check_interval_minutes": "soon"}, "Check every must be a number"),
        ({"offline_message": "{name} is down {oops}"}, "unknown placeholder {oops}"),
        ({"offline_message": "100% down"}, 'cannot contain "%"'),
        ({"recovered_message": "line\nbreak"}, "must be a single line"),
        ({"discord_webhook_urls": "https://example.com/hook"}, "Discord webhook URLs"),
        ({"flood_scope": "#"}, "Flood scope"),
    ])
    def test_rejects_bad_input(self, viewer, tmp_path, body, message):
        resp = _save(viewer, **body)
        assert resp.status_code == 400
        assert message in resp.get_json()["error"]
        assert "[Repeater_Watch]" not in (tmp_path / "config.ini").read_text(encoding="utf-8")
