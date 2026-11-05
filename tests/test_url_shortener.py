"""Unit tests for modules.url_shortener."""

import configparser
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from modules.url_shortener import (
    DEFAULT_SHORT_URL_BASE,
    _build_create_gd_url,
    _build_create_shlink_url,
    _coerce_url_string,
    shorten_url_sync,
)


def _minimal_config(**external_data):
    c = configparser.ConfigParser()
    c["External_Data"] = {}
    for k, v in external_data.items():
        c["External_Data"][k] = v
    return c


class TestBuildCreateGdUrl:
    def test_vgd_no_key_in_query(self):
        u = _build_create_gd_url("http://example.com/path?q=1", "https://v.gd", "secret")
        assert "key=" not in u
        assert "format=simple" in u
        assert "url=http" in u

    def test_custom_host_appends_key_when_set(self):
        u = _build_create_gd_url("http://a.com", "https://short.example/api", "k1")
        assert "key=k1" in u

    def test_is_gd_no_key_in_query(self):
        u = _build_create_gd_url("http://a.com", "https://is.gd", "secret")
        assert "key=" not in u
        assert "create.php" in u


class TestBuildCreateShlinkUrl:
    def test_appends_rest_v3_short_urls_path(self):
        u = _build_create_shlink_url("https://short.example")
        assert u == "https://short.example/rest/v3/short-urls"

    def test_strips_trailing_slash_on_base(self):
        u = _build_create_shlink_url("https://short.example/")
        assert u == "https://short.example/rest/v3/short-urls"

    def test_bare_hostname_gets_https_scheme(self):
        u = _build_create_shlink_url("short.example")
        assert u == "https://short.example/rest/v3/short-urls"

    def test_preserves_a_path_prefix(self):
        u = _build_create_shlink_url("https://short.example/shlink")
        assert u == "https://short.example/shlink/rest/v3/short-urls"


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

    def test_default_gd_service_needs_no_api_key(self):
        """Regression: v.gd/is.gd are documented as keyless (config.ini.example);
        the default `gd` service must not require short_url_website_api_key."""
        cfg = _minimal_config(short_url_website="https://v.gd")
        assert cfg.get("External_Data", "short_url_website_api_key", fallback="") == ""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/nokey\n"
        session = MagicMock()
        session.get.return_value = mock_resp

        out = shorten_url_sync("http://a.com", config=cfg, session=session)
        assert out == "https://v.gd/nokey"
        session.get.assert_called_once()

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
        """A failing response body must never be returned as the short URL.

        The body here is what a reverse proxy in front of a self-hosted is.gd-compatible
        shortener actually serves on 502 -- it parses as a URL, so without the
        response.ok guard it would be transmitted over RF as the shortened link.
        """
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 502
        mock_resp.text = "http://short.example/maintenance"
        session = MagicMock()
        session.get.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_http_error_is_logged_at_warning(self):
        cfg = _minimal_config()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 502
        mock_resp.text = "http://short.example/maintenance"
        session = MagicMock()
        session.get.return_value = mock_resp
        logger = MagicMock()

        shorten_url_sync("http://a.com", config=cfg, session=session, logger=logger)

        assert logger.warning.called

    def test_timeout_is_not_logged_as_an_unexpected_error(self):
        """A shortener timeout is expected on an intermittent uplink, not an error."""
        cfg = _minimal_config()
        session = MagicMock()
        session.get.side_effect = requests.exceptions.Timeout("timed out")
        logger = MagicMock()

        assert shorten_url_sync("http://a.com", config=cfg, session=session, logger=logger) == ""
        logger.error.assert_not_called()
        assert logger.debug.called

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

    def test_service_option_is_case_insensitive(self):
        cfg = _minimal_config(short_url_website="https://v.gd", short_url_website_service="GD")
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "https://v.gd/caseok"
        session = MagicMock()
        session.get.return_value = mock_resp

        out = shorten_url_sync("http://a.com", config=cfg, session=session)
        assert out == "https://v.gd/caseok"

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


class TestShortenUrlSyncShlink:
    def _shlink_config(self, **overrides):
        defaults = {
            "short_url_website_service": "shlink",
            "short_url_website": "https://short.example",
            "short_url_website_api_key": "test-api-key",
        }
        defaults.update(overrides)
        return _minimal_config(**defaults)

    def test_success_returns_short_url(self):
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"shortUrl": "https://short.example/abc123"}
        session = MagicMock()
        session.post.return_value = mock_resp

        out = shorten_url_sync("https://example.com/long/path", config=cfg, session=session)

        assert out == "https://short.example/abc123"
        session.post.assert_called_once()

    def test_posts_to_rest_v3_short_urls_with_api_key_header(self):
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"shortUrl": "https://short.example/abc123"}
        session = MagicMock()
        session.post.return_value = mock_resp

        shorten_url_sync("https://example.com/long/path", config=cfg, session=session)

        call = session.post.call_args
        assert call[0][0] == "https://short.example/rest/v3/short-urls"
        assert call.kwargs["headers"]["X-Api-Key"] == "test-api-key"
        assert call.kwargs["headers"]["Content-Type"] == "application/json"
        payload = json.loads(call.kwargs["data"])
        assert payload["longUrl"] == "https://example.com/long/path"
        assert payload["findIfExists"] is True

    def test_bare_slug_in_response_is_not_treated_as_a_url(self):
        """Shlink's create response carries shortUrl; a slug is not a link.

        Returning one would put an unclickable `abc123` where the reply expects a URL.
        """
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"shortUrlSlug": "abc123", "shortCode": "abc123"}
        session = MagicMock()
        session.post.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_missing_base_does_not_send_the_api_key_anywhere(self):
        """Regression: an unset base must not fall back to the public v.gd default.

        _normalize_base defaults to https://v.gd, so without an explicit guard a
        shlink deployment with no short_url_website POSTs the operator's API key to
        an unrelated third party.
        """
        cfg = self._shlink_config(short_url_website="")
        session = MagicMock()
        logger = MagicMock()

        out = shorten_url_sync("http://a.com", config=cfg, session=session, logger=logger)

        assert out == ""
        session.post.assert_not_called()
        assert logger.warning.called

    def test_base_pointing_at_the_public_vgd_api_is_refused(self):
        """v.gd is not Shlink; sending it an X-Api-Key only discloses the key."""
        cfg = self._shlink_config(short_url_website="https://v.gd")
        session = MagicMock()

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""
        session.post.assert_not_called()

    def test_http_error_returns_empty_and_warns(self):
        """A bad API key must be diagnosable above DEBUG.

        Shlink reports failures as RFC 7807 problem details, which parse as JSON and
        simply lack shortUrl -- indistinguishable from an unshortenable URL without
        checking the status.
        """
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        mock_resp.text = '{"title": "Invalid API key", "status": 401}'
        session = MagicMock()
        session.post.return_value = mock_resp
        logger = MagicMock()

        out = shorten_url_sync("http://a.com", config=cfg, session=session, logger=logger)

        assert out == ""
        assert logger.warning.called

    def test_http_error_does_not_leak_the_api_key_into_the_log(self):
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        mock_resp.text = "denied"
        session = MagicMock()
        session.post.return_value = mock_resp
        logger = MagicMock()

        shorten_url_sync("http://a.com", config=cfg, session=session, logger=logger)

        logged = " ".join(str(c) for c in logger.warning.call_args_list)
        assert "test-api-key" not in logged

    def test_missing_short_url_in_response_returns_empty(self):
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"unexpected": "shape"}
        session = MagicMock()
        session.post.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_missing_api_key_skips_the_request(self):
        """Regression: shlink genuinely needs an API key, unlike v.gd/is.gd, so it
        must not attempt the call (and must not crash) when one isn't configured."""
        cfg = self._shlink_config(short_url_website_api_key="")
        session = MagicMock()

        out = shorten_url_sync("http://a.com", config=cfg, session=session)

        assert out == ""
        session.post.assert_not_called()

    def test_request_exception_returns_empty(self):
        cfg = self._shlink_config()
        session = MagicMock()
        session.post.side_effect = requests.exceptions.ConnectionError("unreachable")

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    def test_malformed_json_response_returns_empty(self):
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.side_effect = ValueError("not json")
        session = MagicMock()
        session.post.return_value = mock_resp

        assert shorten_url_sync("http://a.com", config=cfg, session=session) == ""

    @patch("modules.url_shortener.requests.post")
    def test_no_session_uses_requests_post(self, mock_post):
        cfg = self._shlink_config()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"shortUrl": "https://short.example/xyz"}
        mock_post.return_value = mock_resp

        out = shorten_url_sync("http://a.com", config=cfg, session=None)
        assert out == "https://short.example/xyz"
        mock_post.assert_called_once()


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


@pytest.mark.asyncio
async def test_shorten_url_async_shlink():
    from modules.url_shortener import shorten_url

    cfg = _minimal_config(
        short_url_website_service="shlink",
        short_url_website="https://short.example",
        short_url_website_api_key="test-api-key",
    )
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"shortUrl": "https://short.example/async1"}
    session = MagicMock()
    session.post.return_value = mock_resp

    out = await shorten_url("http://d.com", config=cfg, session=session)
    assert out == "https://short.example/async1"
