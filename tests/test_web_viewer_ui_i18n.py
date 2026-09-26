"""Tests for the German web UI layer and the guide page."""

import configparser
import re
from pathlib import Path

import pytest

from modules.web_viewer.ui_i18n import resolve_ui_language
from tests.test_web_viewer_app import viewer_with_db  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parent.parent / "modules" / "web_viewer"


def _cfg(**sections):
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    return cfg


@pytest.mark.parametrize("sections,expected", [
    ({}, "en"),
    ({"Localization": {"language": "de"}}, "de"),
    ({"Localization": {"language": "de-DE"}}, "de"),
    ({"Localization": {"language": "fr"}}, "en"),
    ({"Localization": {"language": "de"}, "Web_Viewer": {"ui_language": "en"}}, "en"),
    ({"Localization": {"language": "en"}, "Web_Viewer": {"ui_language": "de"}}, "de"),
])
def test_resolve_ui_language(sections, expected):
    assert resolve_ui_language(_cfg(**sections)) == expected


def test_resolve_ui_language_none():
    assert resolve_ui_language(None) == "en"


def test_dictionary_file_is_safe():
    src = (WEB / "static" / "js" / "ui-i18n-de.js").read_text(encoding="utf-8")
    # Text is swapped via nodeValue/setAttribute only, never parsed as HTML
    assert ".innerHTML" not in src and "insertAdjacentHTML" not in src and "eval(" not in src


def _set_language(viewer, lang):
    viewer.config.read_dict({"Localization": {"language": lang}})


@pytest.fixture
def client(viewer_with_db):  # noqa: F811
    return viewer_with_db.app.test_client()


def test_german_ui_loads_dictionary(client, viewer_with_db):  # noqa: F811
    _set_language(viewer_with_db, "de")
    html = client.get("/").get_data(as_text=True)
    assert '<html lang="de">' in html
    assert "js/ui-i18n-de.js" in html
    assert "moment.js/2.29.4/locale/de.min.js" in html


def test_english_ui_has_no_dictionary(client, viewer_with_db):  # noqa: F811
    _set_language(viewer_with_db, "en")
    html = client.get("/").get_data(as_text=True)
    assert '<html lang="en">' in html
    assert "ui-i18n-de.js" not in html


def test_dictionary_served(client):
    resp = client.get("/static/js/ui-i18n-de.js")
    assert resp.status_code == 200
    assert b"Einstellungen" in resp.data


def test_guide_page_and_nav(client):
    resp = client.get("/anleitung")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Anleitung" in html and "Repeater-Akku" in html and "Befehlsübersicht" in html
    assert 'href="/anleitung"' in html
    # CSP-hardened page: every inline script carries the nonce
    nonce = re.search(r"'nonce-([^']+)'", resp.headers["Content-Security-Policy"]).group(1)
    for attrs in re.findall(r"<script\b([^>]*)>", html):
        if "src=" not in attrs:
            assert f'nonce="{nonce}"' in attrs


def test_guide_anchor_links_resolve():
    html = (WEB / "templates" / "anleitung.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for anchor in re.findall(r'href="#([^"]+)"', html):
        assert anchor in ids, anchor


def test_mod_credit_in_footer_and_login(client):
    html = client.get("/anleitung").get_data(as_text=True)
    footer = html[html.index("<footer"):html.index("</footer>")]
    assert "Mod by" in footer and "https://mesh.weserbergland.cc" in footer
    assert "https://github.com/agessaman/meshcore-bot" in footer  # upstream credit stays
    login = (WEB / "templates" / "login.html").read_text(encoding="utf-8")
    assert "Mesh.Weserbergland.cc" in login
