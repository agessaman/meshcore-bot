#!/usr/bin/env python3
"""
NOAA Aurora Client - Fetches KP index and Ovation aurora probability from NOAA SWPC.
"""

import time
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class AuroraData:
    kp_index: float
    kp_timestamp: str
    aurora_probability: float
    forecast_time: str
    latitude: float
    longitude: float


class NOAAAuroraClient:
    """Client for fetching KP index and aurora probability from NOAA with caching.

    Kp from 1-minute product (planetary_k_index_1m.json); aurora probability
    from Ovation grid (ovation_aurora_latest.json).
    """

    KP_1M_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
    OVATION_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
    KP_CACHE_DURATION = 60  # 1 minute, to match 1m product cadence
    OVATION_CACHE_DURATION = 300  # 5 minutes

    def __init__(self, latitude: float, longitude: float):
        self.latitude = latitude
        self.longitude = longitude

        # Cache storage
        self._kp_cache: Optional[list] = None
        self._kp_cache_time: float = 0
        self._ovation_cache: Optional[dict] = None
        self._ovation_cache_time: float = 0

    def _is_cache_valid(self, cache_time: float, duration: float) -> bool:
        return (time.time() - cache_time) < duration

    def get_kp_index(self) -> tuple[float, str]:
        """Fetch current Kp from 1-minute product. Returns (kp_value, time_tag).

        1-minute planetary_k_index_1m.json: array of {time_tag, kp_index, estimated_kp, kp}.
        time_tag is ISO-like UTC, e.g. "2026-01-21T05:13:00". Uses estimated_kp (float).
        """
        if self._kp_cache and self._is_cache_valid(self._kp_cache_time, self.KP_CACHE_DURATION):
            data = self._kp_cache
        else:
            response = requests.get(self.KP_1M_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            self._kp_cache = data
            self._kp_cache_time = time.time()

        if not data:
            raise ValueError("No Kp data in 1-minute product")

        latest = data[-1]
        timestamp = latest.get("time_tag", "")
        kp_val = latest.get("estimated_kp")
        if kp_val is None:
            kp_val = latest.get("kp_index", 0)
        kp_value = float(kp_val)

        return kp_value, timestamp

    def get_aurora_probability(self) -> tuple[float, str]:
        """Fetch aurora probability for configured location. Returns (probability%, forecast_time)."""
        if self._ovation_cache and self._is_cache_valid(self._ovation_cache_time, self.OVATION_CACHE_DURATION):
            data = self._ovation_cache
        else:
            response = requests.get(self.OVATION_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            self._ovation_cache = data
            self._ovation_cache_time = time.time()

        forecast_time = data.get("Forecast Time", "Unknown")

        # Normalize longitude to 0-360 range (NOAA uses 0-359)
        lon = self.longitude if self.longitude >= 0 else self.longitude + 360

        # Find nearest grid point (1° resolution)
        best_match = None
        min_distance = float("inf")

        for coord in data["coordinates"]:
            coord_lon, coord_lat, probability = coord
            distance = abs(coord_lat - self.latitude) + abs(coord_lon - lon)
            if distance < min_distance:
                min_distance = distance
                best_match = probability

        return float(best_match) if best_match is not None else 0.0, forecast_time

    def get_aurora_data(self) -> AuroraData:
        """Fetch both KP index and aurora probability."""
        kp_value, kp_timestamp = self.get_kp_index()
        probability, forecast_time = self.get_aurora_probability()

        return AuroraData(
            kp_index=kp_value,
            kp_timestamp=kp_timestamp,
            aurora_probability=probability,
            forecast_time=forecast_time,
            latitude=self.latitude,
            longitude=self.longitude,
        )

    def clear_cache(self) -> None:
        """Force refresh on next request."""
        self._kp_cache = None
        self._ovation_cache = None
