"""The metadata-backed settings routes: defaults, stored values, and saves."""

from __future__ import annotations

from tests.characterization.test_web_endpoint_inventory import _viewer


def _get(viewer, path):
    return viewer.app.test_client().get(path).get_json()


def test_defaults_when_nothing_stored(tmp_path):
    viewer = _viewer(tmp_path, "")
    assert _get(viewer, "/api/config/logging") == {"log_max_bytes": "5242880", "log_backup_count": "3"}
    assert _get(viewer, "/api/config/maintenance") == {
        "db_backup_enabled": "false", "db_backup_schedule": "daily", "db_backup_time": "02:00",
        "db_backup_retention_count": "7", "db_backup_dir": "/data/backups", "email_attach_log": "false",
    }
    notif = _get(viewer, "/api/config/notifications")
    assert notif["smtp_port"] == "587" and notif["smtp_security"] == "starttls"
    assert notif["nightly_enabled"] == "false" and notif["smtp_host"] == ""
    assert set(notif) == {
        "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password",
        "from_name", "from_email", "recipients", "nightly_enabled", "allow_local_smtp",
    }


def test_stored_values_win_and_empty_strings_fall_back(tmp_path):
    viewer = _viewer(tmp_path, "")
    viewer.db_manager.set_metadata("maint.log_backup_count", "9")
    viewer.db_manager.set_metadata("maint.log_max_bytes", "")
    viewer.db_manager.set_metadata("notif.smtp_host", "mail.example")
    assert _get(viewer, "/api/config/logging") == {"log_max_bytes": "5242880", "log_backup_count": "9"}
    assert _get(viewer, "/api/config/notifications")["smtp_host"] == "mail.example"


def test_post_saves_only_allowed_fields(tmp_path):
    viewer = _viewer(tmp_path, "")
    client = viewer.app.test_client()
    resp = client.post("/api/config/logging", json={"log_backup_count": 4, "bogus": "x"}).get_json()
    assert resp == {"success": True, "saved": ["log_backup_count"]}
    assert viewer.db_manager.get_metadata("maint.log_backup_count") == "4"
    assert viewer.db_manager.get_metadata("maint.bogus") is None
    resp = client.post("/api/config/zombie-alert", json={"alert_email": "a@b.c"}).get_json()
    assert resp["saved"] == ["alert_email"]
    assert viewer.db_manager.get_metadata("zombie.alert_email") == "a@b.c"
