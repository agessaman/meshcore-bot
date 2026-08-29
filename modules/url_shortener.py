#!/usr/bin/env python3
"""
Shared URL shortening for MeshCore Bot and web viewer.

Uses the v.gd / is.gd-compatible API (GET .../create.php?format=simple&url=...).
Configure base URL and optional API key under [External_Data] in config.ini.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

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


def _build_create_shlink_url(long_url: str, base: str, api_key: str) -> str:
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
    """Shorten a URL using Shlink API."""
    import json

    shortener_url = _build_create_shlink_url(long_url, base, api_key)
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }
    payload = json.dumps(
        {"longUrl": long_url, "findIfExists": True, "tags": ["meshcore-bot"]}
    )

    get = session.post if session is not None else requests.post
    response = get(shortener_url, headers=headers, data=payload, timeout=timeout)
    if logger:
        logger.debug("Shlink response: %s", response.text)
    data = response.json()
    short_url = data.get("shortUrl") or data.get("shortUrlSlug")

    if short_url:
        return short_url

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
    short = _parse_simple_response(response.text)
    if short:
        return short

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
        )
        api_key = (
            _safe_config_get(config, "External_Data", "short_url_website_api_key", "")
            or ""
        ).strip()
        base = _normalize_base(base)

        if service == "shlink":
            if not api_key:
                if logger:
                    logger.warning(
                        "short_url_website_service=shlink requires short_url_website_api_key; skipping."
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

    except Exception as e:
        if logger:
            logger.error("Unexpected error shortening URL: %s", e)
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
