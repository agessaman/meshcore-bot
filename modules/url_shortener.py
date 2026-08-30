#!/usr/bin/env python3
"""Shared URL shortening for MeshCore Bot and web viewer.

Supports v.gd / is.gd-compatible ``create.php`` services and Shlink's JSON REST
API. Select the backend, base URL and credentials under ``[External_Data]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any
from urllib.parse import quote, urlparse

import requests


def _coerce_url_string(url: Any) -> str:
    """Normalize feed/API link values to a string (feedparser may use dicts with href)."""
    if url is None:
        return ""
    if isinstance(url, str):
        return url.strip()
    if isinstance(url, (bytes, bytearray)):
        try:
            return url.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
    if isinstance(url, dict):
        href = url.get("href") or url.get("url")
        if href is not None:
            return str(href).strip()
        return ""
    return str(url).strip()


def _safe_config_get(config: Any, section: str, option: str, fallback: str = "") -> str:
    """Read config without raising (missing section, interpolation, etc.)."""
    if config is None:
        return fallback
    try:
        get = getattr(config, "get", None)
        if not callable(get):
            return fallback
        return get(section, option, fallback=fallback)
    except Exception:
        return fallback


DEFAULT_SHORT_URL_BASE = "https://v.gd"

# Accepted values for [External_Data] short_url_website_service. Keep in sync with
# the enum in modules/config_schema.py (which lints the same key at startup / in
# --strict CI). Anything else is treated as a misconfiguration and fails closed.
_KNOWN_SERVICES = frozenset({"gd", "shlink"})
_CONFIG_WARNINGS_EMITTED: set[tuple[str, str]] = set()
_CONFIG_WARNING_LOCK = threading.Lock()

# Hostnames that use the public create.php API without an API key query param.
_VGD_COMPAT_HOSTS = frozenset(
    {
        "v.gd",
        "www.v.gd",
        "is.gd",
        "www.is.gd",
    }
)


def _normalize_base(base: str) -> str:
    b = (base or "").strip().rstrip("/")
    return b if b else DEFAULT_SHORT_URL_BASE


def _host_allows_key_in_query(host: str) -> bool:
    """True if we may append api_key for this host. v.gd/is.gd public API: False."""
    h = (host or "").lower().split(":")[0]
    return h not in _VGD_COMPAT_HOSTS


def _parse_simple_response(body: str) -> str | None:
    text = (body or "").strip()
    if not text:
        return None
    if text.startswith("Error:"):
        return None
    if text.startswith("http"):
        return text
    return None


def _warn_config_once(
    logger: logging.Logger | None,
    kind: str,
    value: str,
    message: str,
    *args: object,
) -> None:
    """Emit one warning per persistent shortener misconfiguration."""
    if logger is None:
        return
    key = (kind, value)
    with _CONFIG_WARNING_LOCK:
        if key in _CONFIG_WARNINGS_EMITTED:
            return
        _CONFIG_WARNINGS_EMITTED.add(key)
    logger.warning(message, *args)


def _parse_http_url(value: Any) -> str | None:
    """Return a normalized HTTP(S) URL string, or ``None`` for an invalid value."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _build_create_gd_url(long_url: str, base: str, api_key: str) -> str:
    from urllib.parse import urlparse, urlunparse

    encoded = quote(long_url, safe="")
    root = _normalize_base(base)
    if "://" not in root:
        root = f"https://{root}"
    parsed = urlparse(root)
    netloc = parsed.netloc
    if not netloc and parsed.path:
        netloc = parsed.path.split("/")[0]
    path = (parsed.path or "").rstrip("/") + "/create.php"
    if not path.startswith("/"):
        path = "/" + path
    query = f"format=simple&url={encoded}"
    if api_key and _host_allows_key_in_query(parsed.hostname or ""):
        query = f"{query}&key={quote(api_key, safe='')}"
    rebuilt = urlunparse((parsed.scheme or "https", netloc, path, "", query, ""))
    return rebuilt


def _build_create_shlink_url(base: str) -> str:
    """Build the Shlink create endpoint from *base*.

    Shlink authenticates with an ``X-Api-Key`` header, so unlike the v.gd builder
    this takes neither the long URL nor the key — nothing about them belongs in
    the URL, and passing them in invited the assumption that they did.
    """
    from urllib.parse import urlparse, urlunparse

    root = _normalize_base(base)
    if "://" not in root:
        root = f"https://{root}"
    parsed = urlparse(root)
    netloc = parsed.netloc
    if not netloc and parsed.path:
        netloc = parsed.path.split("/")[0]
    path = (parsed.path or "").rstrip("/") + "/rest/v3/short-urls"
    if not path.startswith("/"):
        path = "/" + path
    query = ""
    rebuilt = urlunparse((parsed.scheme or "https", netloc, path, "", query, ""))
    return rebuilt


def _shorten_url_with_shlink(
    long_url: str,
    base: str,
    api_key: str,
    session: requests.Session | None = None,
    timeout: float = 5.0,
    logger: logging.Logger | None = None,
) -> str:
    """Shorten a URL using the Shlink API."""
    shortener_url = _build_create_shlink_url(base)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }
    payload = json.dumps(
        {"longUrl": long_url, "findIfExists": True, "tags": ["meshcore-bot"]}
    )

    post = session.post if session is not None else requests.post
    response = post(shortener_url, headers=headers, data=payload, timeout=timeout)
    if not response.ok:
        # A bad API key is a 401 with a JSON problem-details body; without this the
        # misconfiguration is indistinguishable from "the shortener had nothing".
        if logger:
            logger.debug("Error shortening URL: HTTP %s", response.status_code)
        return ""

    data = response.json()
    # A proxy or health-check shim can return HTTP 200 with a JSON array or bare
    # string; calling .get() on that raises AttributeError, which surfaces to the
    # operator as an opaque "'list' object has no attribute 'get'" instead of
    # "the shortener returned an unexpected body".
    if not isinstance(data, dict):
        if logger:
            logger.debug("Shlink response was not a JSON object: %s", str(data)[:200])
        return ""
    # Shlink's create response carries `shortUrl` (and `shortCode`, which is a bare
    # slug, not a URL). Anything else means we did not get a usable link.
    short_url = _parse_http_url(data.get("shortUrl"))
    if short_url:
        return short_url

    if logger:
        logger.debug("Shlink response had no shortUrl: %s", str(data)[:200])
    return ""


def _shorten_url_with_gd(
    long_url: str,
    base: str,
    api_key: str,
    session: requests.Session | None = None,
    timeout: float = 5.0,
    logger: logging.Logger | None = None,
) -> str:
    """Shorten a URL using v.gd / is.gd API."""
    shortener_url = _build_create_gd_url(long_url, base, api_key)

    get = session.get if session is not None else requests.get

    response = get(shortener_url, timeout=timeout)
    if not response.ok:
        if logger:
            logger.debug("Error shortening URL: HTTP %s", response.status_code)
        return ""

    short = _parse_simple_response(response.text)
    if short:
        return short

    if logger:
        logger.debug("URL shortener returned error: %s", response.text.strip()[:200])
    return ""


def shorten_url_sync(
    url: Any,
    *,
    config: Any,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
    timeout: float = 5.0,
) -> str:
    """Shorten a URL using [External_Data] short_url_website (default v.gd).

    Returns the shortened URL or empty string on failure.
    """
    try:
        url_str = _coerce_url_string(url)
        if not url_str:
            return ""

        base = _safe_config_get(config, "External_Data", "short_url_website", "")
        service = (
            _safe_config_get(config, "External_Data", "short_url_website_service", "gd")
            .strip()
            .lower()
        ) or "gd"
        api_key = (
            _safe_config_get(config, "External_Data", "short_url_website_api_key", "")
            or ""
        ).strip()
        base = _normalize_base(base)

        # Fail closed on an unrecognized service. Without this, a typo like
        # "shlnik" fell through to the v.gd branch, which — depending on the
        # configured base — either posted the (possibly internal) URL to the
        # public v.gd host or appended the Shlink API key as a &key= query param
        # to the operator's own host, where it lands in access logs. Neither the
        # URL nor the key should leave the box on a misconfiguration.
        if service not in _KNOWN_SERVICES:
            _warn_config_once(
                logger,
                "unknown-service",
                service,
                "Unknown short_url_website_service=%r; expected one of %s. "
                "Skipping URL shortening.",
                service,
                ", ".join(sorted(_KNOWN_SERVICES)),
            )
            return ""

        if service == "shlink":
            if not api_key:
                _warn_config_once(
                    logger,
                    "missing-shlink-key",
                    base,
                    "short_url_website_service=shlink requires "
                    "short_url_website_api_key; skipping.",
                )
                return ""
            return _shorten_url_with_shlink(
                url_str,
                base,
                api_key,
                session=session,
                timeout=timeout,
                logger=logger,
            )

        # v.gd / is.gd-compatible: api_key is optional (unused for the public hosts,
        # only appended for self-hosted alternates via _host_allows_key_in_query).
        return _shorten_url_with_gd(
            url_str,
            base,
            api_key,
            session=session,
            timeout=timeout,
            logger=logger,
        )

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # A mesh node's uplink drops out routinely; that is not an error worth
        # raising the log level for, and it used to be logged at debug.
        if logger:
            logger.debug("Error shortening URL: %s", e)
        return ""
    except Exception as e:
        if logger:
            logger.debug("shorten_url_sync failed: %s", e)
        return ""


async def shorten_url(
    url: str,
    *,
    config: Any,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
    timeout: float = 5.0,
) -> str:
    """Async wrapper: runs shorten_url_sync in the default executor."""
    if not url:
        return ""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: shorten_url_sync(
                url,
                config=config,
                session=session,
                logger=logger,
                timeout=timeout,
            ),
        )
    except Exception as e:
        if logger:
            logger.debug("Unexpected error shortening URL: %s", e)
        return ""
