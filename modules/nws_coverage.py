"""Bounded, temporary suppression of NWS requests for points without coverage."""

import time
from collections import OrderedDict


class NWSNoCoverageCache:
    MAX_POINTS = 256
    TTL_SECONDS = 24 * 60 * 60

    def __init__(self) -> None:
        self._points: OrderedDict[tuple[float, float], float] = OrderedDict()

    def is_unavailable(self, lat: float, lon: float) -> bool:
        point = (round(lat, 2), round(lon, 2))
        expires = self._points.get(point)
        if expires is None:
            return False
        if time.monotonic() >= expires:
            del self._points[point]
            return False
        self._points.move_to_end(point)
        return True

    def mark_unavailable(self, lat: float, lon: float) -> bool:
        """Remember the point; return whether it needs a new warning."""
        if self.is_unavailable(lat, lon):
            return False
        point = (round(lat, 2), round(lon, 2))
        self._points[point] = time.monotonic() + self.TTL_SECONDS
        if len(self._points) > self.MAX_POINTS:
            self._points.popitem(last=False)
        return True

    def mark_available(self, lat: float, lon: float) -> None:
        self._points.pop((round(lat, 2), round(lon, 2)), None)
