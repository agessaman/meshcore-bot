"""The local UTC offset for a point, from its time zone when one is known.

MET Norway's forecast carries no timezones. ``tzfpy`` maps a coordinate to an
IANA zone name from a dataset it bundles, and ``pytz`` (which bundles its own tz
data, so a slim image needs no system tzdata) turns that name into a
DST-correct offset at a given instant.

``tzfpy`` publishes wheels for 64-bit x86 and ARM only, so on armv7 the import
fails and the offset falls back to the longitude estimate. That estimate is
exact where civil time follows solar time and off by an hour or two where it
does not (Spain, western China, the edges of a wide zone).
"""

from __future__ import annotations

import functools
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

# tzfpy is imported on first use rather than at import time, so a missing wheel
# costs one failed import and the tests can stub it.
_state = {"checked": False, "module": None, "warned": False}
_lock = threading.Lock()


def estimate_offset_hours(lon: float) -> int:
    """The longitude estimate: ``round(lon / 15)`` hours."""
    return int(round(lon / 15.0))


def _tzfpy():
    """The tzfpy module, or None when it is not installed."""
    with _lock:
        if not _state["checked"]:
            try:
                import tzfpy
            except Exception:
                tzfpy = None
            _state["module"] = tzfpy
            _state["checked"] = True
        return _state["module"]


@functools.lru_cache(maxsize=512)
def _zone_name(lat: float, lon: float) -> Optional[str]:
    """The IANA zone name for a rounded point, or None when it is unknown."""
    module = _tzfpy()
    if module is None:
        return None
    try:
        # tzfpy takes (longitude, latitude), the opposite of the usual order.
        name = module.get_tz(lon, lat)
    except Exception:
        return None
    return name or None


def utc_offset_at(lat: float, lon: float, when_utc: datetime) -> timedelta:
    """The UTC offset in force at *when_utc* for the point's time zone.

    Falls back to the longitude estimate when tzfpy is missing or does not know
    the point. A missing tzfpy is logged once at INFO; an unknown point is
    logged at DEBUG, because it is a property of the coordinate, not a fault.
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

    if _tzfpy() is None:
        with _lock:
            if not _state["warned"]:
                _state["warned"] = True
                logger.info(
                    "tzfpy is not installed; local times use the longitude estimate "
                    "(install tzfpy on a 64-bit platform for real time zones)"
                )
    else:
        logger.debug(f"No time zone for {lat},{lon}; using the longitude estimate")
    return timedelta(hours=estimate_offset_hours(lon))


def _reset() -> None:
    """Forget the import attempt, the log-once flag and the name cache (tests)."""
    with _lock:
        _state["checked"] = False
        _state["module"] = None
        _state["warned"] = False
    _zone_name.cache_clear()
