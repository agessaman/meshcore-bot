"""The local UTC offset for a point, from its time zone when one is known.

MET Norway's forecast carries no timezones. A zone lookup maps a coordinate to
an IANA zone name, and ``pytz`` (which bundles its own tz data, so a slim image
needs no system tzdata) turns that name into a DST-correct offset at a given
instant.

Two lookups are tried in order. ``tzfpy`` is a Rust extension with wheels for
64-bit x86 and ARM on PyPI and for 32-bit ARM on piwheels, which is what the
Raspberry Pi images and the armv7 Docker leg install. ``timezonefinder`` is the
Nix stand-in, because nixpkgs does not carry tzfpy. Without either, the offset
falls back to the longitude estimate, which is exact where civil time follows
solar time and off by an hour or two where it does not (Spain, western China,
the edges of a wide zone).
"""

from __future__ import annotations

import functools
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

# Both lookups are imported on first use rather than at import time, so a
# missing wheel costs one failed import and the tests can stub them.
_state = {
    "checked": False,
    "backend": None,
    "module": None,
    "finder": None,
}
_lock = threading.Lock()


def estimate_offset_hours(lon: float) -> int:
    """The longitude estimate: ``round(lon / 15)`` hours."""
    return int(round(lon / 15.0))


def _detect_backend():
    """Pick the zone lookup: ``tzfpy``, then ``timezonefinder``, else None."""
    try:
        import tzfpy
    except Exception:
        tzfpy = None
    if tzfpy is not None:
        _state["module"] = tzfpy
        return "tzfpy"

    try:
        import timezonefinder
    except Exception:
        return None
    _state["module"] = timezonefinder
    return "timezonefinder"


def _backend():
    """The active backend name, or None when neither lookup is installed."""
    with _lock:
        if not _state["checked"]:
            _state["backend"] = _detect_backend()
            _state["checked"] = True
            if _state["backend"] is None:
                logger.info(
                    "No time zone lookup installed; local times use the longitude "
                    "estimate (install tzfpy or timezonefinder for real time zones)"
                )
            else:
                logger.info(f"Local times use real time zones from {_state['backend']}")
        return _state["backend"]


def _finder():
    """The shared TimezoneFinder, built once: construction loads a large index."""
    with _lock:
        if _state["finder"] is None:
            _state["finder"] = _state["module"].TimezoneFinder()
        return _state["finder"]


@functools.lru_cache(maxsize=512)
def _zone_name(lat: float, lon: float) -> Optional[str]:
    """The IANA zone name for a rounded point, or None when it is unknown."""
    backend = _backend()
    if backend is None:
        return None
    try:
        if backend == "tzfpy":
            # tzfpy takes (longitude, latitude), the opposite of the usual order.
            name = _state["module"].get_tz(lon, lat)
        else:
            name = _finder().timezone_at(lng=lon, lat=lat)
    except Exception:
        return None
    return name or None


def utc_offset_at(lat: float, lon: float, when_utc: datetime) -> timedelta:
    """The UTC offset in force at *when_utc* for the point's time zone.

    Falls back to the longitude estimate when no lookup is installed or the
    lookup does not know the point. The active backend is logged once at INFO;
    an unknown point is logged at DEBUG, because it is a property of the
    coordinate, not a fault.
    """
    name = _zone_name(round(lat, 4), round(lon, 4))
    if name:
        try:
            zone = pytz.timezone(name)
        except Exception:
            zone = None
        if zone is not None:
            moment = when_utc if when_utc.tzinfo is not None else when_utc.replace(tzinfo=timezone.utc)
            offset = moment.astimezone(zone).utcoffset()
            if offset is not None:
                return offset

    if _backend() is not None:
        logger.debug(f"No time zone for {lat},{lon}; using the longitude estimate")
    return timedelta(hours=estimate_offset_hours(lon))


def _reset() -> None:
    """Forget the backend, the finder and the name cache (tests)."""
    with _lock:
        _state["checked"] = False
        _state["backend"] = None
        _state["module"] = None
        _state["finder"] = None
    _zone_name.cache_clear()
