"""Point-in-polygon geometry and distance, shared by the alert providers.

GeoJSON coordinates are ``[lon, lat]`` pairs. The ray casting here is plain
Python: the alert polygons are small and few, and a geometry library would be a
dependency for one function.
"""

from __future__ import annotations

import math
from typing import Any, Optional

EARTH_RADIUS_KM = 6371.0088


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray casting: whether (lon, lat) is inside one linear ring of [lon, lat] pairs."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    previous = count - 1
    for current in range(count):
        x_current, y_current = ring[current][0], ring[current][1]
        x_previous, y_previous = ring[previous][0], ring[previous][1]
        if (y_current > lat) != (y_previous > lat):
            crossing = (x_previous - x_current) * (lat - y_current) / (y_previous - y_current) + x_current
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """Whether a GeoJSON Polygon's outline contains the point and no hole does."""
    if not polygon:
        return False
    if not point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(point_in_ring(lon, lat, hole) for hole in polygon[1:])


def valid_geometry(geometry: Any) -> bool:
    """Whether a GeoJSON Polygon or MultiPolygon has rings ray casting can read.

    A malformed ring would raise inside :func:`point_in_ring`, so a geometry is
    checked once, before it is cached, rather than on every lookup.
    """
    if not isinstance(geometry, dict):
        return False
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return False
    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon":
        polygons = coordinates
    else:
        return False
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            return False
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 3:
                return False
            for position in ring:
                if not isinstance(position, (list, tuple)) or len(position) < 2:
                    return False
                if not all(isinstance(value, (int, float)) for value in position[:2]):
                    return False
    return True


def geometry_contains(geometry: Optional[dict], lon: float, lat: float) -> bool:
    """Whether a GeoJSON Polygon or MultiPolygon contains the point."""
    if not geometry:
        return False
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "Polygon":
        return point_in_polygon(lon, lat, coordinates)
    if kind == "MultiPolygon":
        return any(point_in_polygon(lon, lat, polygon) for polygon in coordinates)
    return False


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))
