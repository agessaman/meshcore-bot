"""Environment Canada weather alerts: the ``eccc`` alerts provider.

The same public OGC API that serves the city pages serves ``weather-alerts``: one
feature per alert area, with a polygon, a risk colour, a validity window and the
alert's own text in English and French. A point is answered by the features
whose polygon contains it, so a border town can take its forecast from NOAA and
its alerts from here.

The shapes are the ones Meteoalarm and NWS use: a compact summary for the second
message and a packed full list, both from :mod:`._alerts_common`. A failed fetch
is reported as a fetch error, never as "No weather alerts", and nothing raises
into the command.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..http_retry import make_retry_session
from ._alerts_common import PackedAlertsMixin
from ._geo import geometry_contains
from .base import AlertsProvider, Coverage, ProviderHost
from .eccc import CANADA_LAT, CANADA_LON, ECCC_BASE_URL, ECCC_TIMEOUT

ALERTS_COLLECTION = "weather-alerts"
ALERT_CACHE_SECONDS = 300
ALERT_BBOX_DEGREES = 0.05

# The risk colour is the severity: a statement carries none and ranks lowest.
RISK_LEVEL = {"yellow": 2, "orange": 3, "red": 4}
STATEMENT_LEVEL = 1


def _parse_time(value: Any) -> Optional[datetime]:
    """An ISO 8601 timestamp from the API, as an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class EcccAlertsProvider(PackedAlertsMixin, AlertsProvider):
    """Environment Canada's weather alerts, matched to a point by polygon."""

    name = "eccc"
    translation_ns = "commands.wx"
    fetch_error_key = "commands.wx.eccc.error_fetching"

    def __init__(self, host: ProviderHost, session: Any = None, clock: Any = None,
                 now: Any = None):
        """Bind the provider to its host.

        *session*, *clock* and *now* are for tests: a fake session, a fake
        monotonic clock for the cache and a fixed wall clock, so neither the
        cache nor the expiry filter depends on when the suite runs.
        """
        super().__init__(host)
        self._session = session
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._streaks: set = set()

    # --- coverage ---------------------------------------------------------

    def covers(self, lat: float, lon: float) -> bool:
        """Whether the point is inside the alert service's extent."""
        return (
            CANADA_LAT[0] <= lat <= CANADA_LAT[1]
            and CANADA_LON[0] <= lon <= CANADA_LON[1]
        )

    def coverage_for(self, lat: float, lon: float) -> Coverage:
        """Coverage is a fixed extent here, so it never has to guess."""
        return Coverage.COVERED if self.covers(lat, lon) else Coverage.NOT_COVERED

    def _warn_once(self, streak: str, message: str) -> None:
        """Log one WARNING per failure streak, not one per request."""
        with self._lock:
            if streak in self._streaks:
                return
            self._streaks.add(streak)
        self.host.logger.warning(message)

    def _clear_streak(self, streak: str) -> None:
        with self._lock:
            self._streaks.discard(streak)

    # --- the lookup -------------------------------------------------------

    def _alerts_for(self, lat: float, lon: float) -> Optional[list]:
        """The active alerts covering a point, most severe first.

        None means the data could not be fetched, as opposed to no alerts, and
        nothing raises into the command.
        """
        try:
            return self._alerts_for_point(lat, lon)
        except Exception as error:
            self._warn_once("alerts", f"Environment Canada alert lookup failed: {type(error).__name__}")
            return None

    def _alerts_for_point(self, lat: float, lon: float) -> Optional[list]:
        """The body of :meth:`_alerts_for`."""
        features, failed = self._features_for(lat, lon)
        if failed:
            return None

        now = self._now()
        alerts = []
        for feature in features:
            if not geometry_contains(feature.get("geometry"), lon, lat):
                continue
            alert = self._normalize(feature, now)
            if alert is not None:
                alerts.append(alert)
        alerts.sort(key=lambda alert: (-alert["level"], alert["onset"] or now))
        return alerts

    def _features_for(self, lat: float, lon: float) -> tuple[list, bool]:
        """The alert features around a point, and whether the fetch failed."""
        key = (round(lat, 2), round(lon, 2))
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1], cached[2]

        features, failed = self._fetch(lat, lon)
        with self._lock:
            self._cache[key] = (now + ALERT_CACHE_SECONDS, features, failed)
        return features, failed

    def _fetch(self, lat: float, lon: float) -> tuple[list, bool]:
        """One request for the alerts around a point."""
        radius = ALERT_BBOX_DEGREES
        params = {
            "f": "json",
            "lang": self._language(),
            "bbox": (
                f"{lon - radius:.4f},{lat - radius:.4f},"
                f"{lon + radius:.4f},{lat + radius:.4f}"
            ),
        }
        url = f"{ECCC_BASE_URL}/collections/{ALERTS_COLLECTION}/items"
        try:
            if self._session is not None:
                response = self._session.get(url, params=params, timeout=ECCC_TIMEOUT)
            else:
                with make_retry_session() as session:
                    response = session.get(url, params=params, timeout=ECCC_TIMEOUT)
        except Exception as error:
            self._warn_once("http", f"Error fetching Environment Canada alerts: {error}")
            return [], True

        if not getattr(response, "ok", False):
            self._warn_once(
                "http",
                f"Error fetching Environment Canada alerts: {getattr(response, 'status_code', '?')}",
            )
            return [], True
        self._clear_streak("http")

        try:
            body = response.json()
        except Exception:
            self._warn_once("json", "Environment Canada alerts returned no JSON")
            return [], True
        features = body.get("features") if isinstance(body, dict) else None
        return [feature for feature in (features or []) if isinstance(feature, dict)], False

    # --- reading the data -------------------------------------------------

    def _language(self) -> str:
        """The reply language: French when the bot's language is French, else English."""
        try:
            value = (self.host.bot.config.get('Localization', 'language', fallback='') or '').strip()
        except Exception:
            value = ''
        return 'fr' if value.lower().startswith('fr') else 'en'

    def _normalize(self, feature: dict, now: datetime) -> Optional[dict]:
        """One alert in the shape the shared summary and full list expect."""
        properties = feature.get("properties") or {}
        if str(properties.get("status_en") or "").strip().lower() == "ended":
            return None

        expires = _parse_time(properties.get("event_end_datetime")) or _parse_time(
            properties.get("expiration_datetime")
        )
        if expires is not None and expires <= now:
            return None

        language = self._language()
        event = (
            properties.get(f"alert_short_name_{language}")
            or properties.get(f"alert_name_{language}")
            or properties.get("alert_short_name_en")
            or properties.get("alert_name_en")
            or ""
        )
        colour = str(properties.get("risk_colour_en") or "").strip().lower()
        return {
            "level": RISK_LEVEL.get(colour, STATEMENT_LEVEL),
            "event": str(event).strip(),
            "onset": _parse_time(properties.get("validity_datetime")),
            "expires": expires,
            "area": str(properties.get(f"feature_name_{language}") or "").strip(),
        }
