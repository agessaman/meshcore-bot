"""Characterization: the web viewer's route table and how each route answers.

Records every rule (path, methods, endpoint name) and, for parameterless GET
routes, the status, content type and security-relevant headers of a request
made anonymously with and without a password configured. It also records, for
every mutating route, what an anonymous request gets when a password is set.
Helper extraction or route regrouping in ``app.py`` must leave this unchanged:
endpoint names feed the CSP nonce allowlist and ``url_for``, and auth is
path-based.
"""

from __future__ import annotations

import configparser
import re
from unittest.mock import patch

import pytest

from tests.characterization.golden_util import assert_golden

SKIP_GET = {
    # Streams or long-polls; not a single request/response.
    "/socket.io/",
}


def _viewer(tmp_path, password: str):
    from modules.web_viewer.app import BotDataViewer

    tmp_path.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "bot_name", "InvBot")
    config.add_section("Web_Viewer")
    for key, value in [
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", password),
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
        viewer = BotDataViewer(db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)
    viewer.app.testing = True
    return viewer


def _headers(resp):
    csp = resp.headers.get("Content-Security-Policy", "")
    script_src = next((p.strip() for p in csp.split(";") if p.strip().startswith("script-src")), "")
    return {
        "status": resp.status_code,
        "content_type": (resp.headers.get("Content-Type") or "").split(";")[0],
        "location": re.sub(r"https?://[^/]+", "", resp.headers.get("Location", "")),
        "csp_script_src": re.sub(r"'nonce-[^']+'", "'nonce-X'", script_src),
        "x_frame_options": resp.headers.get("X-Frame-Options"),
        "x_content_type_options": resp.headers.get("X-Content-Type-Options"),
    }


@pytest.mark.timeout(120)
def test_route_table_and_anonymous_responses(tmp_path):
    open_viewer = _viewer(tmp_path / "open", "")
    locked_viewer = _viewer(tmp_path / "locked", "s3cret-pass")
    rules = sorted(open_viewer.app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint))
    inventory = {}
    open_client = open_viewer.app.test_client()
    locked_client = locked_viewer.app.test_client()
    for rule in rules:
        if rule.endpoint == "static":
            continue
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        entry = {"endpoint": rule.endpoint, "methods": methods}
        has_params = bool(rule.arguments)
        if "GET" in methods and not has_params and rule.rule not in SKIP_GET:
            entry["GET open"] = _headers(open_client.get(rule.rule))
            entry["GET locked anon"] = _headers(locked_client.get(rule.rule))
        for method in methods:
            if method == "GET":
                continue
            path = re.sub(r"<(?:[^:>]+:)?[^>]+>", "1", rule.rule)
            resp = locked_client.open(path, method=method, json={})
            entry[f"{method} locked anon"] = _headers(resp)
        inventory[f"{rule.rule}"] = entry
    assert_golden("web_endpoint_inventory", inventory)
