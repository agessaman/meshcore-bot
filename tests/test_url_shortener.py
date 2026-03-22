"""Unit tests for modules.url_shortener."""

import configparser
from unittest.mock import MagicMock, patch

import pytest
import requests

from modules.url_shortener import (
    DEFAULT_SHORT_URL_BASE,
    _build_create_url,
    _coerce_url_string,
    shorten_url_sync,
)


def _minimal_config(**external_data):
    c = configparser.ConfigParser()
    c["External_Data"] = {}
    for k, v in external_data.items():
        c["External_Data"][k] = v
    return c


class TestBuildCreateUrl:
    def test_vgd_no_key_in_query(self):
        u = _build_create_url("http://example.com/path?q=1", "https://v.gd", "secret")
        assert "key=" not in u
        assert "format=simple" in u
        assert "url=http" in u

    def test_custom_host_appends_key_when_set(self):
        u = _build_create_url("http://a.com", "https://short.example/api", "k1")
        assert "key=k1" in u

    def test_is_gd_no_key_in_query(self):
        u = _build_create_url("http://a.com", "https://is.gd", "secret")
        assert "key=" not in u
        assert "create.php" in u


class TestCoerceUrlString:
    def test_dict_href(self):
        assert _coerce_url_string({"href": "https://a.com/x"}) == "https://a.com/x"

    def test_dict_empty_returns_empty(self):
        assert _coerce_url_string({}) == ""


class TestShortenUrlSync:
    def test_empty_url(self):
        cfg = _minimal_config()
        assert shorten_url_sync("", config=cfg) == ""

    def test_dict_url_coerced_like_feedparser_link(self):
        cfg = _minimal_config(short_url_website="https://v.gd")
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/xyz\n"
        session = MagicMock()
        session.get.return_value = mock_resp
        out = shorten_url_sync(
            {"href": "https://example.com/path"},
            config=cfg,
            session=session,
        )
        assert out == "https://v.gd/xyz"

    def test_success_simple_format(self):
        cfg = _minimal_config(short_url_website="https://v.gd")
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/AbCdEf\n"
        session = MagicMock()
        session.get.return_value = mock_resp

        out = shorten_url_sync(
            "https://maps.example.com/?x=1&y=2",
            config=cfg,
            session=session,
        )
        assert out == "https://v.gd/AbCdEf"
        session.get.assert_called_once()
        call_url = session.get.call_args[0][0]
        assert call_url.startswith("https://v.gd/create.php")
        assert "format=simple" in call_url

    def test_error_line_returns_empty(self):
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "Error: Rate limit exceeded\n"
        session = MagicMock()
        session.get.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_unexpected_response_body_returns_empty(self):
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "unexpected garbage"
        session = MagicMock()
        session.get.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_whitespace_only_response_returns_empty(self):
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "   \n\t  "
        session = MagicMock()
        session.get.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_timeout_returns_empty(self):
        cfg = _minimal_config()
        session = MagicMock()
        session.get.side_effect = requests.exceptions.Timeout("timed out")

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_connection_error_returns_empty(self):
        cfg = _minimal_config()
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError("unreachable")

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_unexpected_exception_from_get_returns_empty(self):
        cfg = _minimal_config()
        session = MagicMock()
        session.get.side_effect = ValueError("boom")

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_bare_hostname_short_url_website(self):
        cfg = _minimal_config(short_url_website="v.gd")
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/short"
        session = MagicMock()
        session.get.return_value = mock_resp

        shorten_url_sync("http://z.com", config=cfg, session=session)
        call_url = session.get.call_args[0][0]
        assert call_url.startswith("https://v.gd/create.php")

    def test_http_error_returns_empty(self):
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 503
        session = MagicMock()
        session.get.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_default_base_when_keys_missing(self):
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/xyz"
        session = MagicMock()
        session.get.return_value = mock_resp

        shorten_url_sync("http://b.com", config=cfg, session=session)
        call_url = session.get.call_args[0][0]
        assert call_url.startswith(DEFAULT_SHORT_URL_BASE)

    @patch("modules.url_shortener.requests.get")
    def test_no_session_uses_requests_get(self, mock_get):
        cfg = _minimal_config(short_url_website="https://v.gd")
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/ok"
        mock_get.return_value = mock_resp

        out = shorten_url_sync("http://c.com", config=cfg, session=None)
        assert out == "https://v.gd/ok"
        mock_get.assert_called_once()


@pytest.mark.asyncio
async def test_shorten_url_async():
    from modules.url_shortener import shorten_url

    cfg = _minimal_config(short_url_website="https://v.gd")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.text = "https://v.gd/async1"
    session = MagicMock()
    session.get.return_value = mock_resp

    out = await shorten_url("http://d.com", config=cfg, session=session)
    assert out == "https://v.gd/async1"
