"""Home Assistant ingress: mount prefix, admin auth, and URL regression.

The Supervisor forwards the panel with the prefix removed and sends it in
X-Ingress-Path. Only 172.30.32.2 is that proxy. With ha_ingress off, the same
header changes nothing.
"""

from __future__ import annotations

import configparser
import logging
import re
from pathlib import Path

import pytest

from modules.web_viewer.app import BotDataViewer
from modules.web_viewer.ha_ingress import (
    HA_INGRESS_PROXY_IP,
    ingress_prefix,
    is_browser_origin,
    normalize_ingress_path,
    socketio_cors_allowed,
)

PROXY = HA_INGRESS_PROXY_IP
PREFIX = "/api/hassio_ingress/abc"
PASSWORD = "secret123"

# Root-relative literals that are not navigations. Each entry must match at
# least one line, so a stale reason cannot hide a new URL.
_ALLOWLIST = (
    (
        "templates/base.html",
        "settings_paths = [",
        "Compared with request.path for the Settings menu active state, not fetched as a URL.",
    ),
    (
        "templates/config.html",
        'placeholder="/path/to/backup.db"',
        "Example filesystem path in an input placeholder.",
    ),
    (
        "templates/config/panels/db_backup.html",
        'placeholder="/data/backups"',
        "Example filesystem path in an input placeholder.",
    ),
)

_LITERAL = re.compile(r"""(?P<q>['"`])(?P<path>/(?!/)[^\s'"`\\]*)(?P=q)""")
_URL_PATH = re.compile(r"^/(?:[A-Za-z0-9._~:@%!$&*+,;=/?{}()-]*)$")
_SAFE_CALL = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:url_for|appUrl|fetchMeshJson|queueAndPoll|fetch|api)\(\s*$"
)
_REQUEST_PATH_CMP = re.compile(r"request\.path\s*[=!]=\s*$")
_ATTR = re.compile(r"(?:href|src|action)\s*=\s*$", re.IGNORECASE)
_SINK = re.compile(
    r"(?:\.href|\.src|location(?:\.href)?\s*=|window\.open\s*\(|EventSource\s*\(|"
    r"\bimport\s*\(|new\s+URL\s*\(|history\.(?:pushState|replaceState)\s*\(|\bpath\s*:)\s*$"
)


def _fake_setup_logging(self: BotDataViewer) -> None:
    self.logger = logging.getLogger("test_web_viewer_ha_ingress")
    self.logger.setLevel(logging.DEBUG)
    if not self.logger.handlers:
        self.logger.addHandler(logging.NullHandler())
    self.logger.propagate = False


def _write_config(path: Path, db_path: str, *, ha_ingress: bool) -> None:
    cfg = configparser.ConfigParser()
    cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_path, "prefix_bytes": "1"}
    cfg["Web_Viewer"] = {
        "web_viewer_password": PASSWORD,
        "ha_ingress": "true" if ha_ingress else "false",
    }
    cfg["Path_Command"] = {
        "graph_capture_enabled": "false",
        "graph_write_strategy": "immediate",
    }
    with path.open("w", encoding="utf-8") as fh:
        cfg.write(fh)


def _make_viewer(tmp_path_factory, name: str, *, ha_ingress: bool) -> BotDataViewer:
    from unittest.mock import patch

    tmp = tmp_path_factory.mktemp(name)
    db_path = str(tmp / "test.db")
    config_path = tmp / "config.ini"
    _write_config(config_path, db_path, ha_ingress=ha_ingress)
    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
        patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
    ):
        viewer = BotDataViewer(db_path=db_path, config_path=str(config_path))
    viewer.app.config["TESTING"] = True
    return viewer


@pytest.fixture(scope="module")
def ingress_viewer(tmp_path_factory):
    return _make_viewer(tmp_path_factory, "ha_ingress_on", ha_ingress=True)


@pytest.fixture(scope="module")
def plain_viewer(tmp_path_factory):
    return _make_viewer(tmp_path_factory, "ha_ingress_off", ha_ingress=False)


@pytest.fixture
def ingress_client(ingress_viewer):
    ingress_viewer.connected_clients.clear()
    with ingress_viewer.app.test_client() as client:
        yield client
    ingress_viewer.connected_clients.clear()


@pytest.fixture
def plain_client(plain_viewer):
    with plain_viewer.app.test_client() as client:
        yield client


def _ingress(client, method: str, path: str, *, header: str | None = PREFIX, remote: str = PROXY, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if header is not None:
        headers["X-Ingress-Path"] = header
    environ = {"REMOTE_ADDR": remote, **(kwargs.pop("environ_base", {}) or {})}
    return getattr(client, method)(path, headers=headers, environ_base=environ, **kwargs)


def _polling(client, *, origin: str, remote: str, header: str | None):
    headers = {"Origin": origin}
    if header is not None:
        headers["X-Ingress-Path"] = header
    return client.get(
        "/socket.io/",
        query_string={"EIO": "4", "transport": "polling"},
        headers=headers,
        environ_base={"REMOTE_ADDR": remote},
    )


class TestIngressPath:
    def test_strips_trailing_slash_and_whitespace(self):
        assert normalize_ingress_path(f"  {PREFIX}/  ") == PREFIX

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "/",
            "//evil.example",
            "/api/../secret",
            "/api/./secret",
            "/api//secret",
            "/api/hassio ingress",
            "/api/hassio%2e%2e",
            "https://ha.example/ingress",
            "/api\\secret",
            "x" * 300,
        ],
    )
    def test_rejects_unsafe_header(self, value):
        assert normalize_ingress_path(value) is None

    def test_prefix_requires_the_supervisor_address(self):
        good = {"REMOTE_ADDR": PROXY, "HTTP_X_INGRESS_PATH": PREFIX}
        assert ingress_prefix(good) == PREFIX
        assert ingress_prefix({**good, "REMOTE_ADDR": "10.0.0.8"}) is None
        assert ingress_prefix({"REMOTE_ADDR": PROXY}) is None


class TestSocketIoCorsDecision:
    def test_ingress_accepts_public_ha_origin_only(self):
        ingress = {
            "REMOTE_ADDR": PROXY,
            "HTTP_X_INGRESS_PATH": PREFIX,
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "172.30.32.1:8080",
        }
        direct = {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_INGRESS_PATH": PREFIX,
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "127.0.0.1:8080",
        }
        ha = "https://homeassistant.local:8123"
        assert socketio_cors_allowed(None, ha, ingress)
        assert not socketio_cors_allowed(None, ha, direct)
        assert socketio_cors_allowed(None, "http://127.0.0.1:8080", direct)
        assert socketio_cors_allowed(["https://ok.example"], "https://ok.example", direct)
        assert not socketio_cors_allowed(["https://ok.example"], "https://no.example", direct)
        assert socketio_cors_allowed(["https://ok.example"], ha, ingress)
        assert not is_browser_origin("https://homeassistant.local:8123/extra")
        assert not is_browser_origin("javascript:alert(1)")
        assert not is_browser_origin(None)


class TestIngressMiddlewareAndAuth:
    def test_prefix_links_and_app_root_only_from_the_supervisor(self, ingress_client, plain_client):
        html = _ingress(ingress_client, "get", "/").get_data(as_text=True)
        assert f'data-app-root="{PREFIX}"' in html
        assert f'href="{PREFIX}/static/vendor/bootstrap/bootstrap.min.css"' in html
        assert f'href="{PREFIX}/"' in html
        assert "window.appUrl" in html

        ignored = _ingress(ingress_client, "get", "/", remote="10.1.1.1").get_data(as_text=True)
        assert 'data-app-root=""' in ignored
        assert f'href="{PREFIX}/' not in ignored

        off = _ingress(plain_client, "get", "/").get_data(as_text=True)
        assert 'data-app-root=""' in off
        assert PREFIX not in off

    def test_unsafe_header_does_not_set_the_prefix(self, ingress_client):
        html = _ingress(ingress_client, "get", "/", header="/api/../secret").get_data(as_text=True)
        assert 'data-app-root=""' in html

    def test_trailing_slash_on_the_header_is_stripped(self, ingress_client):
        html = _ingress(ingress_client, "get", "/", header=PREFIX + "/").get_data(as_text=True)
        assert f'data-app-root="{PREFIX}"' in html
        assert f'data-app-root="{PREFIX}/"' not in html

    def test_ingress_skips_login_and_other_clients_do_not(self, ingress_client, plain_client):
        page = _ingress(ingress_client, "get", "/logs")
        assert page.status_code == 200
        assert f'href="{PREFIX}/realtime"' in page.get_data(as_text=True)

        api = _ingress(ingress_client, "get", "/api/connected_clients")
        assert api.status_code != 401

        direct = ingress_client.get("/logs")
        assert direct.status_code == 302
        assert "/login" in direct.headers["Location"]
        assert "hassio_ingress" not in direct.headers["Location"]
        assert ingress_client.get("/api/connected_clients").status_code == 401

        spoofed = _ingress(ingress_client, "get", "/logs", remote="8.8.8.8")
        assert spoofed.status_code == 302
        assert "hassio_ingress" not in spoofed.headers["Location"]

        bad = _ingress(ingress_client, "get", "/logs", header="//evil.example")
        assert bad.status_code == 302

        still_locked = _ingress(plain_client, "get", "/logs")
        assert still_locked.status_code == 302
        assert "hassio_ingress" not in still_locked.headers["Location"]

    def test_redirects_keep_the_prefix(self, ingress_client):
        logout = _ingress(ingress_client, "get", "/logout")
        assert logout.status_code == 302
        assert logout.headers["Location"].startswith(PREFIX + "/")

        cache = _ingress(ingress_client, "get", "/cache")
        assert cache.status_code == 302
        assert cache.headers["Location"] == f"{PREFIX}/config#database"

        logged_in = _ingress(
            ingress_client,
            "post",
            "/login",
            query_string={"next": "/logs?lines=50"},
            data={"password": PASSWORD},
        )
        assert logged_in.status_code == 302
        assert logged_in.headers["Location"] == f"{PREFIX}/logs?lines=50"

        offsite = _ingress(
            ingress_client,
            "post",
            "/login",
            query_string={"next": "https://attacker.example/"},
            data={"password": PASSWORD},
        )
        assert offsite.headers["Location"] == f"{PREFIX}/"

    def test_non_ingress_login_redirect_is_unchanged(self, ingress_client):
        resp = ingress_client.post(
            "/login",
            query_string={"next": "/logs?lines=50"},
            data={"password": PASSWORD},
        )
        assert resp.headers["Location"] == "/logs?lines=50"

    def test_socketio_connect_uses_the_ingress_predicate(self, ingress_viewer, plain_viewer):
        allowed = ingress_viewer.app.test_client()
        allowed.environ_base["REMOTE_ADDR"] = PROXY
        sock = ingress_viewer.socketio.test_client(
            ingress_viewer.app,
            flask_test_client=allowed,
            headers={"X-Ingress-Path": PREFIX},
        )
        assert sock.is_connected()
        sock.disconnect()

        denied = ingress_viewer.app.test_client()
        denied.environ_base["REMOTE_ADDR"] = "10.0.0.9"
        outsider = ingress_viewer.socketio.test_client(
            ingress_viewer.app,
            flask_test_client=denied,
            headers={"X-Ingress-Path": PREFIX},
        )
        assert not outsider.is_connected()

        no_header = ingress_viewer.app.test_client()
        no_header.environ_base["REMOTE_ADDR"] = PROXY
        missing = ingress_viewer.socketio.test_client(
            ingress_viewer.app,
            flask_test_client=no_header,
        )
        assert not missing.is_connected()

        plain = plain_viewer.app.test_client()
        plain.environ_base["REMOTE_ADDR"] = PROXY
        ignored = plain_viewer.socketio.test_client(
            plain_viewer.app,
            flask_test_client=plain,
            headers={"X-Ingress-Path": PREFIX},
        )
        assert not ignored.is_connected()

    def test_socketio_origin_check_is_scoped_to_ingress(self, ingress_viewer, ingress_client, plain_viewer, plain_client):
        ha = "https://homeassistant.local:8123"
        assert _polling(ingress_client, origin=ha, remote=PROXY, header=PREFIX).status_code == 200
        assert _polling(ingress_client, origin=ha, remote="127.0.0.1", header=PREFIX).status_code == 400
        assert _polling(ingress_client, origin="http://localhost", remote="127.0.0.1", header=None).status_code == 200
        assert _polling(plain_client, origin=ha, remote=PROXY, header=PREFIX).status_code == 400
        assert not callable(plain_viewer.socketio.server.eio.cors_allowed_origins)
        assert callable(ingress_viewer.socketio.server.eio.cors_allowed_origins)


def _viewer_sources():
    root = Path(__file__).resolve().parent.parent / "modules" / "web_viewer"
    files = list((root / "templates").rglob("*.html"))
    files.extend(path for path in (root / "static" / "js").rglob("*") if path.suffix in {".js", ".mjs"})
    return [path for path in files if "vendor" not in path.parts]


def _line_at(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return text[start:end]


def test_root_relative_urls_go_through_url_for_app_url_or_fetch():
    """New root-relative URLs have to use url_for, appUrl, or the fetch wrapper.

    fetch('/api/...') is covered because base.html rewrites string URLs inside
    the one window.fetch wrapper. Helpers that pass that string straight to
    fetch (api, queueAndPoll, fetchMeshJson) count the same way. Anything the
    browser resolves itself, and any non-API path, has to be prefixed explicitly.
    """
    violations = []
    used = dict.fromkeys(_ALLOWLIST, False)
    for path in _viewer_sources():
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(Path(__file__).resolve().parent.parent / "modules" / "web_viewer"))
        for match in _LITERAL.finditer(text):
            literal = match.group("path")
            if not (literal == "/" or _URL_PATH.fullmatch(literal)):
                continue
            before = text[: match.start()].rstrip()
            line = _line_at(text, match.start())
            if _SAFE_CALL.search(before) or _REQUEST_PATH_CMP.search(before):
                continue
            navigates = bool(_ATTR.search(before) or _SINK.search(before))
            # A quoted "/" is a character test (appUrl, path slicing), not a URL.
            if literal == "/" and not navigates:
                continue
            allow = next(
                (entry for entry in _ALLOWLIST if entry[0] == rel and entry[1] in line),
                None,
            )
            if allow is not None:
                used[allow] = True
                continue
            # /api/... strings ride the fetch wrapper unless a sink resolves them
            # itself. /api-explorer is a page, so the slash after "api" matters.
            if literal.startswith("/api/") and not navigates:
                continue
            violations.append(f"{rel}:{text[:match.start()].count(chr(10)) + 1}: {line.strip()}")

    stale = [entry[1] for entry, hit in used.items() if not hit]
    assert stale == [], f"allowlist entries matched nothing: {stale}"
    assert violations == [], "root-relative URL bypasses url_for/appUrl/fetch:\n" + "\n".join(violations)
