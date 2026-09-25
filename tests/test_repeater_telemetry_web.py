"""Tests for the Repeater-Akku web page, its API and the shared settings store."""

import configparser
import re
import sqlite3
import time
from pathlib import Path

import pytest

from modules import repeater_telemetry_store as store
from tests.test_web_viewer_app import viewer_with_db  # noqa: F401  (fixture)

TEMPLATE = Path(__file__).resolve().parent.parent / "modules" / "web_viewer" / "templates" / "repeater_akku.html"
HDR = {"X-Requested-With": "XMLHttpRequest"}


# ----------------------------------------------------------------- store

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    store.ensure_tables(c)
    yield c
    c.close()


def _config(**values):
    cfg = configparser.ConfigParser()
    cfg[store.CONFIG_SECTION] = {k: str(v) for k, v in values.items()}
    return cfg


class TestStore:
    def test_layers_defaults_config_web(self, conn):
        cfg = _config(repeaters="A:pw, B", warn_mv=3600, alert_dm="X, Y")
        s = store.load_settings(conn, cfg)
        assert s["warn_mv"] == 3600 and s["critical_mv"] == 3300
        assert s["repeaters"] == [{"name": "A", "password": "pw"}, {"name": "B", "password": ""}]
        assert s["admins"] == ["X", "Y"] and s["web_managed"] is False
        store.save_settings(conn, {"warn_mv": 3700, "admins": ["Z"]})
        s = store.load_settings(conn, cfg)
        assert s["warn_mv"] == 3700 and s["admins"] == ["Z"] and s["web_managed"] is True
        store.reset_web_settings(conn)
        assert store.load_settings(conn, cfg)["warn_mv"] == 3600

    def test_config_admins_capped_at_five(self, conn):
        s = store.load_settings(conn, _config(admins="a,b,c,d,e,f,g"))
        assert s["admins"] == ["a", "b", "c", "d", "e"]

    def test_blank_password_keeps_stored_one(self):
        current = {"repeaters": [{"name": "Rep", "password": "secret"}], "warn_mv": 3500, "critical_mv": 3300}
        out = store.validate_submission({"repeaters": [{"name": "rep", "password": ""}]}, current)
        assert out["repeaters"] == [{"name": "rep", "password": "secret"}]
        out = store.validate_submission({"repeaters": [{"name": "Rep", "password": "", "clear_password": True}]},
                                        current)
        assert out["repeaters"][0]["password"] == ""

    @pytest.mark.parametrize("data,msg", [
        ({"admins": ["a", "b", "c", "d", "e", "f"]}, "Höchstens 5"),
        ({"repeaters": [{"name": "A"}, {"name": "a"}]}, "doppelt"),
        ({"repeaters": [{"name": "A,B"}]}, "Komma"),
        ({"warn_mv": 3300, "critical_mv": 3500}, "kritische"),
        ({"interval_minutes": 1}, "mindestens"),
        ({"interval_minutes": "abc"}, "Zahl"),
        ({"template_warn": "{oops}"}, "ungültig"),
    ])
    def test_validation_errors(self, data, msg):
        with pytest.raises(store.SettingsError, match=msg):
            store.validate_submission(data, {"repeaters": [], "warn_mv": 3500, "critical_mv": 3300})

    def test_public_settings_hide_passwords(self):
        pub = store.public_settings({"repeaters": [{"name": "A", "password": "pw"}], "admins": []})
        assert pub["repeaters"] == [{"name": "A", "has_password": True}]
        assert "pw" not in str(pub)

    def test_status_and_history(self, conn):
        now = int(time.time())
        conn.executemany(
            f"INSERT INTO {store.TABLE_SAMPLES} (ts, name, ok, bat_mv, error) VALUES (?,?,?,?,?)",
            [(now - 7200, "A", 1, 3900, None), (now - 60, "A", 0, None, "login failed/timeout")])
        conn.execute(f"INSERT INTO {store.TABLE_STATE} VALUES ('A', 'ok', 1, 0, 0)")
        st = store.status_overview(conn, ["A", "Neu"])
        assert st[0]["bat_mv"] == 3900 and st[0]["last_ok"] is False and st[0]["fails"] == 1
        assert st[1]["bat_mv"] is None and st[1]["last_try_ts"] is None
        assert len(store.history(conn, "A", 1)) == 2


# ------------------------------------------------------------------- web

@pytest.fixture
def client(viewer_with_db):  # noqa: F811
    return viewer_with_db.app.test_client()


class TestWebApi:
    def test_page_renders_german(self, client):
        resp = client.get("/repeater-akku")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Repeater-Akku" in html and "Jetzt abfragen" in html

    def test_page_inline_scripts_carry_csp_nonce(self, client):
        resp = client.get("/repeater-akku")
        csp = resp.headers["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        html = resp.get_data(as_text=True)
        for attrs in re.findall(r"<script\b([^>]*)>", html):
            if "src=" not in attrs:
                assert f'nonce="{nonce}"' in attrs

    def test_template_has_no_html_sinks_or_inline_handlers(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        assert ".innerHTML" not in src and "insertAdjacentHTML" not in src
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", src, re.IGNORECASE)

    def test_nav_link_present(self, client):
        assert 'href="/repeater-akku"' in client.get("/repeater-akku").get_data(as_text=True)

    def test_get_save_roundtrip(self, client):
        data = client.get("/api/repeater-telemetry").get_json()
        assert data["service_enabled"] is False
        assert data["limits"]["max_admins"] == 5
        assert data["settings"]["repeaters"] == []

        resp = client.post("/api/repeater-telemetry/settings", headers=HDR, json={
            "repeaters": [{"name": "Rep1", "password": "geheim"}],
            "admins": ["Manuel", "Henne"],
            "warn_mv": 3600, "critical_mv": 3400, "paused": True,
        })
        assert resp.status_code == 200, resp.get_json()
        data = client.get("/api/repeater-telemetry").get_json()
        s = data["settings"]
        assert s["repeaters"] == [{"name": "Rep1", "has_password": True}]
        assert s["admins"] == ["Manuel", "Henne"] and s["warn_mv"] == 3600 and s["paused"] is True
        assert "geheim" not in str(data)
        assert [r["name"] for r in data["status"]] == ["Rep1"]

    def test_save_rejects_six_admins(self, client):
        resp = client.post("/api/repeater-telemetry/settings", headers=HDR,
                           json={"admins": ["a", "b", "c", "d", "e", "f"]})
        assert resp.status_code == 400
        assert "Höchstens 5" in resp.get_json()["error"]

    def test_poll_and_reset(self, client, viewer_with_db):  # noqa: F811
        assert client.post("/api/repeater-telemetry/poll", headers=HDR).status_code == 200
        client.post("/api/repeater-telemetry/settings", headers=HDR, json={"warn_mv": 3700})
        with viewer_with_db.db_manager.connection() as c:
            s = store.load_settings(c, None)
        assert s[store.POLL_REQUEST_KEY] > 0 and s["warn_mv"] == 3700
        assert client.post("/api/repeater-telemetry/reset", headers=HDR).status_code == 200
        assert client.get("/api/repeater-telemetry").get_json()["settings"]["warn_mv"] == 3500

    def test_history_requires_name(self, client):
        assert client.get("/api/repeater-telemetry/history").status_code == 400
        data = client.get("/api/repeater-telemetry/history?name=X&days=abc").get_json()
        assert data["days"] == 7 and data["samples"] == []
