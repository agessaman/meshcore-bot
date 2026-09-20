"""End-to-end tests for the scheduled-message CRUD API (#174).

These drive the real Flask routes against a real config.ini on disk, so they cover
the whole path an operator takes: the INI is edited surgically, a config reload is
queued for the bot, and the change is visible on the next read.
"""

import json
import sqlite3
from configparser import ConfigParser
from unittest.mock import patch

import pytest


@pytest.fixture
def viewer(tmp_path):
    from modules.web_viewer.app import BotDataViewer

    config = ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "timezone", "UTC")
    config.add_section("Web_Viewer")
    for key, value in (
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", ""),
    ):
        config.set("Web_Viewer", key, value)
    config.add_section("Scheduled_Messages")
    config.set("Scheduled_Messages", "0 8 * * *", "Public:Good morning")

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as fh:
        config.write(fh)

    db_path = str(tmp_path / "meshcore_bot.db")
    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        v = BotDataViewer(db_path=db_path, config_path=config_path)
    v.config_path = config_path
    v.app.testing = True
    return v


def _client(viewer):
    return viewer.app.test_client()


def _post(client, path, payload, method="post"):
    return getattr(client, method)(
        path, data=json.dumps(payload), content_type="application/json",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def _queued_reloads(viewer):
    with sqlite3.connect(viewer.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM channel_operations WHERE operation_type='config_reload'"
        ).fetchone()[0]


def _config_text(viewer):
    with open(viewer.config_path, encoding="utf-8") as fh:
        return fh.read()


class TestListing:
    def test_lists_the_existing_entry(self, viewer):
        resp = _client(viewer).get("/api/scheduled-messages")
        assert resp.status_code == 200
        entries = resp.get_json()["entries"]
        assert len(entries) == 1
        assert entries[0]["channel"] == "Public"
        assert entries[0]["message"] == "Good morning"
        assert entries[0]["next_runs"]


class TestPreview:
    def test_previews_a_valid_schedule(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages/preview",
                     {"schedule": "0 6,12,18 * * *", "count": 3})
        data = resp.get_json()
        assert data["valid"] is True
        assert len(data["next_runs"]) == 3

    def test_reports_an_invalid_schedule(self, viewer):
        data = _post(_client(viewer), "/api/scheduled-messages/preview",
                     {"schedule": "not a cron"}).get_json()
        assert data["valid"] is False
        assert data["error"]

    def test_applies_the_command_placeholder_floor(self, viewer):
        data = _post(_client(viewer), "/api/scheduled-messages/preview",
                     {"schedule": "*/5 * * * *", "message": "{cmd:wx}"}).get_json()
        assert data["valid"] is False
        assert "15 minutes" in data["error"]


class TestCreate:
    def test_creates_and_persists_to_config(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"schedule": "30 7 * * *", "channel": "Public", "message": "Hi"})
        assert resp.status_code == 200
        assert "30 7 * * * = Public:Hi" in _config_text(viewer)

    def test_queues_a_config_reload_so_no_restart_is_needed(self, viewer):
        before = _queued_reloads(viewer)
        _post(_client(viewer), "/api/scheduled-messages",
              {"schedule": "30 7 * * *", "channel": "Public", "message": "Hi"})
        assert _queued_reloads(viewer) == before + 1

    def test_creates_a_scoped_entry(self, viewer):
        _post(_client(viewer), "/api/scheduled-messages",
              {"schedule": "30 7 * * *", "channel": "Public", "message": "Hi", "scope": "sea"})
        assert "Public:#sea:Hi" in _config_text(viewer)

    def test_rejects_an_invalid_schedule(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"schedule": "nope", "channel": "Public", "message": "Hi"})
        assert resp.status_code == 400

    def test_rejects_a_missing_message(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"schedule": "30 7 * * *", "channel": "Public", "message": ""})
        assert resp.status_code == 400

    def test_refuses_to_overwrite_an_existing_schedule(self, viewer):
        """Schedules are INI keys, so a duplicate would silently replace the other."""
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"schedule": "0 8 * * *", "channel": "Public", "message": "Clash"})
        assert resp.status_code == 409
        assert "Good morning" in _config_text(viewer)

    def test_nothing_is_written_when_validation_fails(self, viewer):
        before = _config_text(viewer)
        _post(_client(viewer), "/api/scheduled-messages",
              {"schedule": "nope", "channel": "Public", "message": "Hi"})
        assert _config_text(viewer) == before


class TestUpdate:
    def test_updates_message_in_place(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"original_schedule": "0 8 * * *", "schedule": "0 8 * * *",
                      "channel": "Public", "message": "Changed"}, method="put")
        assert resp.status_code == 200
        text = _config_text(viewer)
        assert "Changed" in text
        assert "Good morning" not in text

    def test_changing_the_schedule_removes_the_old_key(self, viewer):
        _post(_client(viewer), "/api/scheduled-messages",
              {"original_schedule": "0 8 * * *", "schedule": "0 9 * * *",
               "channel": "Public", "message": "Good morning"}, method="put")
        text = _config_text(viewer)
        assert "0 9 * * *" in text
        assert "0 8 * * *" not in text

    def test_unknown_entry_is_a_404(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"original_schedule": "0 3 * * *", "schedule": "0 3 * * *",
                      "channel": "Public", "message": "x"}, method="put")
        assert resp.status_code == 404


class TestDelete:
    def test_deletes_the_entry(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"schedule": "0 8 * * *"}, method="delete")
        assert resp.status_code == 200
        assert "Good morning" not in _config_text(viewer)

    def test_delete_queues_a_reload(self, viewer):
        before = _queued_reloads(viewer)
        _post(_client(viewer), "/api/scheduled-messages", {"schedule": "0 8 * * *"}, method="delete")
        assert _queued_reloads(viewer) == before + 1

    def test_unknown_entry_is_a_404(self, viewer):
        resp = _post(_client(viewer), "/api/scheduled-messages",
                     {"schedule": "0 3 * * *"}, method="delete")
        assert resp.status_code == 404


class TestRoundTrip:
    def test_created_entry_is_readable_by_the_bots_own_parser(self, viewer):
        """What the UI writes must be what the scheduler reads back."""
        from configparser import ConfigParser as CP

        from modules.scheduled_message_cron import parse_scheduled_message_value

        _post(_client(viewer), "/api/scheduled-messages",
              {"schedule": "15 9 * * mon,fri", "channel": "Public",
               "message": "Standup at 9:15", "scope": "#sea"})

        parser = CP()
        parser.read(viewer.config_path, encoding="utf-8")
        raw = parser.get("Scheduled_Messages", "15 9 * * mon,fri")
        channel, message, scope = parse_scheduled_message_value(raw)
        assert (channel, message, scope) == ("Public", "Standup at 9:15", "#sea")


class TestWriteSerialisation:
    """Duplicate/existence checks must share the write's critical section, or a
    concurrent request can slip between the check and the write."""

    def test_concurrent_creates_of_one_schedule_yield_exactly_one_success(self, viewer):
        import threading

        results = []
        barrier = threading.Barrier(2)

        def create(body):
            client = viewer.app.test_client()
            barrier.wait()
            resp = _post(client, "/api/scheduled-messages", body)
            results.append(resp.status_code)

        threads = [
            threading.Thread(target=create, args=({
                "schedule": "45 7 * * *", "channel": "Public", "message": f"msg {i}",
            },)) for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == [200, 409], results
        # And exactly one entry landed, not one silently replacing the other.
        assert _config_text(viewer).count("45 7 * * * =") == 1

    def test_update_of_a_removed_entry_is_a_404_not_a_resurrection(self, viewer):
        client = _client(viewer)
        _post(client, "/api/scheduled-messages", {"schedule": "0 8 * * *"}, method="delete")
        resp = _post(client, "/api/scheduled-messages", {
            "original_schedule": "0 8 * * *", "schedule": "0 9 * * *",
            "channel": "Public", "message": "back from the dead",
        }, method="put")
        assert resp.status_code == 404
        assert "back from the dead" not in _config_text(viewer)
