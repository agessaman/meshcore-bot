"""Home Assistant ingress: prefix, authentication, and Socket.IO origins.

The Supervisor proxies an add-on panel at ``/api/hassio_ingress/<token>/...``,
strips that prefix before forwarding, and sends it back in ``X-Ingress-Path``.
Those requests always come from 172.30.32.2. Only Home Assistant users can open
an ingress panel, so a request that passes these checks is already authenticated.
"""

from __future__ import annotations

import configparser
import re
from collections.abc import Mapping

# The Supervisor's proxy address on the add-on network. Anything else is a
# normal viewer request, even if it copies the ingress header.
HA_INGRESS_PROXY_IP = "172.30.32.2"
_INGRESS_HEADER = "HTTP_X_INGRESS_PATH"
_MAX_INGRESS_PATH_LEN = 256
# token_urlsafe() is base64url (letters, digits, "-", "_"). Dots and tildes
# are ordinary path characters and stay allowed.
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
# An origin we are willing to echo in Access-Control-Allow-Origin. No userinfo,
# path, or whitespace: the browser's public Home Assistant URL, not a free-form
# header.
_BROWSER_ORIGIN = re.compile(
    r"^https?://(?:[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,64}\])(?::\d{1,5})?$"
)


def ha_ingress_enabled(config: configparser.ConfigParser) -> bool:
    """True when ``[Web_Viewer] ha_ingress`` is set; malformed values are off."""
    try:
        return config.getboolean("Web_Viewer", "ha_ingress", fallback=False)
    except (configparser.Error, ValueError, TypeError):
        return False


def normalize_ingress_path(value: object) -> str | None:
    """Return a safe SCRIPT_NAME, or None when the header is not a path.

    Trailing slashes are removed. ``.`` and ``..`` segments, backslashes,
    encoding, and anything that is not a path character are rejected so the
    value cannot retarget a redirect.
    """
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path.startswith("/") or path.startswith("//") or len(path) > _MAX_INGRESS_PATH_LEN:
        return None
    path = path.rstrip("/")
    if not path:
        return None
    for segment in path.split("/")[1:]:
        if segment in {".", ".."} or _PATH_SEGMENT.fullmatch(segment) is None:
            return None
    return path


def ingress_prefix(environ: Mapping[str, object]) -> str | None:
    """Ingress path for this WSGI environ, or None when it is not ingress."""
    if environ.get("REMOTE_ADDR") != HA_INGRESS_PROXY_IP:
        return None
    return normalize_ingress_path(environ.get(_INGRESS_HEADER))


class HaIngressMiddleware:
    """Set SCRIPT_NAME from X-Ingress-Path for Supervisor requests only."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        prefix = ingress_prefix(environ)
        if prefix:
            environ["SCRIPT_NAME"] = prefix
        return self.app(environ, start_response)


def is_browser_origin(origin: object) -> bool:
    """True for an http(s) origin with a host and an optional port."""
    return isinstance(origin, str) and _BROWSER_ORIGIN.fullmatch(origin) is not None


def _origin_matches_host(origin: object, environ: Mapping[str, object]) -> bool:
    """Same-origin check engineio uses when cors_allowed_origins is left unset.

    A callable replaces that default, so non-ingress requests reproduce it here.
    """
    if not isinstance(origin, str):
        return False
    scheme = environ.get("wsgi.url_scheme")
    host = environ.get("HTTP_HOST")
    if not isinstance(scheme, str) or not isinstance(host, str):
        return False
    allowed = [f"{scheme}://{host}"]
    forwarded_proto = environ.get("HTTP_X_FORWARDED_PROTO")
    forwarded_host = environ.get("HTTP_X_FORWARDED_HOST")
    if forwarded_proto is not None or forwarded_host is not None:
        raw_scheme = forwarded_proto if isinstance(forwarded_proto, str) else scheme
        raw_host = forwarded_host if isinstance(forwarded_host, str) else host
        allowed.append(f"{raw_scheme.split(',')[0].strip()}://{raw_host.split(',')[0].strip()}")
    return origin in allowed


def socketio_cors_allowed(
    configured: str | list[str] | None,
    origin: object,
    environ: Mapping[str, object],
) -> bool:
    """CORS decision for one Socket.IO handshake.

    ``configured`` is the ``[Web_Viewer] cors_allowed_origins`` value: ``"*"``,
    a list of origins, ``[]`` (engineio disables the check), or None (same
    origin only). Ingress requests also accept the browser's public Home
    Assistant origin, which does not match the add-on's internal Host.
    """
    # A set cannot hold ``[]``. engineio also treats TypeError from this
    # callable as "try again with origin only", so this must not raise.
    if configured == "*" or configured == []:
        return True
    if isinstance(configured, list) and isinstance(origin, str) and origin in configured:
        return True
    if configured is None and _origin_matches_host(origin, environ):
        return True
    return ingress_prefix(environ) is not None and is_browser_origin(origin)
