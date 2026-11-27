"""Meteoalarm alerts: Europe's EUMETNET warnings, behind the weather command.

The OGC EDR API only answers by country, so a point is matched in three steps:
the country polygons from ``/collections/warnings/locations`` say which
countries could cover it, each country's active warnings carry a bounding box
per warning area, and a feature whose ``geometryType`` is not ``exact`` is
confirmed against the outline in its ``rel=geometry`` link. Event, level and
expiry live in the CAP 1.2 document the feature links to, not in the feature.

The token is sent as a bearer header, never as a query parameter, so it stays
out of URLs and logs. Nothing here raises into the command: a failed fetch
logs one WARNING per failure streak and answers with no alerts.

The base URL defaults to the MeteoGate gateway, which serves the same EDR API
and is where a public token comes from. Meteoalarm Direct re-user tokens point
``[Weather] meteoalarm_api_url`` at ``https://api.meteoalarm.org/edr/v1``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional

from ..http_retry import make_retry_session
from .base import AlertsProvider, AlertSummary, ProviderHost

# MeteoGate serves the same EDR API as Meteoalarm Direct and is where a public
# token comes from. Direct re-user tokens point [Weather] meteoalarm_api_url at
# https://api.meteoalarm.org/edr/v1 instead.
DEFAULT_BASE_URL = "https://api.meteogate.eu/warnings"
LOCATIONS_PATH = "/collections/warnings/locations"
COUNTRY_PATH = "/collections/warnings/locations/{country}"
LOCATIONS_URL = f"{DEFAULT_BASE_URL}{LOCATIONS_PATH}"
COUNTRY_URL = f"{DEFAULT_BASE_URL}{COUNTRY_PATH}"

# A short connect timeout and a longer read: the API answers from a CDN, and a
# stalled read is worth waiting out once rather than dropping the alert.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10

LOCATIONS_TTL = 24 * 60 * 60
COUNTRY_TTL = 10 * 60
MAX_PAGES = 5

CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
LEVEL_EMOJI = {2: "\U0001f7e1", 3: "\U0001f7e0", 4: "\U0001f534"}
MIN_LEVEL = 2


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
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


def _point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """Whether a GeoJSON Polygon's outline contains the point and no hole does."""
    if not polygon:
        return False
    if not _point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in polygon[1:])


def geometry_contains(geometry: Optional[dict], lon: float, lat: float) -> bool:
    """Whether a GeoJSON Polygon or MultiPolygon contains the point."""
    if not geometry:
        return False
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "Polygon":
        return _point_in_polygon(lon, lat, coordinates)
    if kind == "MultiPolygon":
        return any(_point_in_polygon(lon, lat, polygon) for polygon in coordinates)
    return False


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    """A CAP timestamp as an aware UTC datetime, or None."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _awareness_level(value: str) -> int:
    """The level number from an awareness_level parameter ("3; orange; Severe")."""
    head = (value or "").split(";")[0].strip()
    try:
        return int(head)
    except ValueError:
        return 0


def _awareness_type(value: str) -> str:
    """The type name from an awareness_type parameter ("1; Wind")."""
    parts = (value or "").split(";")
    return parts[1].strip() if len(parts) > 1 else ""


def _cap_info(root: ET.Element, language: str) -> Optional[ET.Element]:
    """The info block for *language* (prefix match), else English, else the first."""
    infos = root.findall("cap:info", CAP_NS)
    if not infos:
        return None

    def code(info: ET.Element) -> str:
        return (info.findtext("cap:language", default="", namespaces=CAP_NS) or "").strip().lower()

    if language:
        for info in infos:
            if code(info).startswith(language):
                return info
    for info in infos:
        if code(info).startswith("en"):
            return info
    return infos[0]


def parse_cap(text: str, language: str) -> Optional[dict]:
    """The alert details a CAP 1.2 document carries, or None when it has no info block.

    Parsed with ElementTree rather than defusedxml: the document comes over
    HTTPS from one fixed host (storage.meteoalarm.org), which the API's own
    links name, and the project does not depend on defusedxml.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    info = _cap_info(root, language)
    if info is None:
        return None

    level = 0
    awareness_type = ""
    for parameter in info.findall("cap:parameter", CAP_NS):
        name = (parameter.findtext("cap:valueName", default="", namespaces=CAP_NS) or "").strip()
        value = (parameter.findtext("cap:value", default="", namespaces=CAP_NS) or "").strip()
        if name == "awareness_level":
            level = _awareness_level(value)
        elif name == "awareness_type":
            awareness_type = _awareness_type(value)

    return {
        "event": (info.findtext("cap:event", default="", namespaces=CAP_NS) or "").strip(),
        "headline": (info.findtext("cap:headline", default="", namespaces=CAP_NS) or "").strip(),
        "onset": _parse_time(info.findtext("cap:onset", default="", namespaces=CAP_NS)),
        "expires": _parse_time(info.findtext("cap:expires", default="", namespaces=CAP_NS)),
        "level": level,
        "awareness_type": awareness_type,
    }


class MeteoalarmAlertsProvider(AlertsProvider):
    """Europe's Meteoalarm warnings, matched to a point by country and outline."""

    name = "meteoalarm"
    translation_ns = "commands.wx"

    def __init__(self, host: ProviderHost, token: Optional[str] = None,
                 session: Any = None, base_url: Optional[str] = None):
        """Bind the provider to its host, its bearer token and a retry session.

        *token* comes from ``[Weather] meteoalarm_token``. It is never logged and
        never put in a URL: the API takes it as ``Authorization: Bearer``.
        *base_url* comes from ``[Weather] meteoalarm_api_url`` and defaults to the
        MeteoGate gateway.
        """
        super().__init__(host)
        self.token = (token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.locations_url = f"{self.base_url}{LOCATIONS_PATH}"
        self.country_url = f"{self.base_url}{COUNTRY_PATH}"
        self.session = session if session is not None else make_retry_session()
        # One lock for every cache. Never held across HTTP.
        self._lock = threading.Lock()
        self._locations: Optional[tuple] = None
        self._countries: dict[str, tuple] = {}
        self._geometries: dict[str, dict] = {}
        self._caps: dict[str, tuple] = {}
        self._streaks: set[str] = set()

    # --- clock ------------------------------------------------------------

    def _clock(self) -> float:
        """Monotonic seconds, for cache TTLs."""
        return time.monotonic()

    def _utcnow(self) -> datetime:
        """Wall clock in UTC, for CAP timestamps."""
        return datetime.now(timezone.utc)

    # --- logging ----------------------------------------------------------

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

    # --- HTTP -------------------------------------------------------------

    def _get(self, url: str, params: Optional[dict] = None):
        """One GET with the bearer token. The token never reaches the URL or a log."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return self.session.get(
            url, params=params, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )

    def _rejected(self, response) -> bool:
        """Whether the API refused the token; logs once per streak, without the token."""
        if response.status_code not in (401, 403):
            return False
        self._warn_once(
            "auth",
            "Meteoalarm rejected the token (HTTP "
            f"{response.status_code}); check [Weather] meteoalarm_token",
        )
        return True

    # --- caches -----------------------------------------------------------

    def _country_geometries(self) -> Optional[list]:
        """[(country code, geometry)] for every country, cached for a day."""
        with self._lock:
            cached = self._locations
        if cached is not None and self._clock() - cached[0] < LOCATIONS_TTL:
            return cached[1]

        try:
            response = self._get(self.locations_url)
            if self._rejected(response):
                return None
            response.raise_for_status()
            features = response.json().get("features") or []
        except Exception as error:
            self._warn_once("locations", f"Meteoalarm locations fetch failed: {type(error).__name__}")
            return None

        countries = [
            (feature.get("id"), feature.get("geometry"))
            for feature in features
            if feature.get("id") and feature.get("id") != "ALL" and feature.get("geometry")
        ]
        with self._lock:
            self._locations = (self._clock(), countries)
        self._clear_streak("locations")
        return countries

    def _country_features(self, country: str) -> Optional[list]:
        """One country's active warnings, following pagination, cached for ten minutes."""
        with self._lock:
            cached = self._countries.get(country)
        if cached is not None and self._clock() - cached[0] < COUNTRY_TTL:
            return cached[1]

        # The spec requires both filters; an interval from now to now asks for
        # warnings active at this moment.
        moment = self._utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        interval = f"{moment}/{moment}"
        features: list = []
        try:
            for page in range(1, MAX_PAGES + 1):
                response = self._get(
                    self.country_url.format(country=country),
                    params={"active": interval, "datetime": interval, "page": page},
                )
                if self._rejected(response):
                    return None
                response.raise_for_status()
                body = response.json()
                features.extend(body.get("features") or [])
                total_pages = (body.get("metadata") or {}).get("total_pages") or 1
                if page >= total_pages:
                    break
        except Exception as error:
            self._warn_once(
                f"country:{country}",
                f"Meteoalarm warnings fetch failed for {country}: {type(error).__name__}",
            )
            return None

        with self._lock:
            self._countries[country] = (self._clock(), features)
        self._clear_streak(f"country:{country}")
        return features

    def _exact_geometry(self, feature: dict) -> Optional[dict]:
        """The outline behind a bbox feature, cached by URL."""
        url = next(
            (link.get("href") for link in feature.get("links") or [] if link.get("rel") == "geometry"),
            None,
        )
        if not url:
            return None
        with self._lock:
            cached = self._geometries.get(url)
        if cached is not None:
            return cached

        try:
            response = self._get(url)
            if self._rejected(response):
                return None
            response.raise_for_status()
            body = response.json()
        except Exception as error:
            self._warn_once(f"geometry:{url}", f"Meteoalarm geometry fetch failed: {type(error).__name__}")
            return None

        geometry = body.get("geometry") if body.get("type") == "Feature" else body
        with self._lock:
            self._geometries[url] = geometry
        return geometry

    def _language(self) -> str:
        """``[Localization] language``, lowercased, for the CAP info block."""
        try:
            return (self.host.bot.config.get("Localization", "language", fallback="en") or "en").strip().lower()
        except Exception:
            return "en"

    def _cap_details(self, alert_id: str, feature: dict) -> Optional[dict]:
        """One alert's CAP details, cached by alertId until it expires."""
        with self._lock:
            cached = self._caps.get(alert_id)
        if cached is not None and cached[0] > self._utcnow():
            return cached[1]

        properties = feature.get("properties") or {}
        url = next(
            (link.get("href") for link in feature.get("links") or [] if link.get("rel") == "xml"),
            None,
        ) or properties.get("hubLink")
        if not url:
            return None

        try:
            response = self._get(url)
            if self._rejected(response):
                return None
            response.raise_for_status()
            details = parse_cap(response.text, self._language())
        except Exception as error:
            self._warn_once(f"cap:{alert_id}", f"Meteoalarm CAP fetch failed: {type(error).__name__}")
            return None

        if details is None or details["expires"] is None:
            return None
        if details["expires"] <= self._utcnow() or details["level"] < MIN_LEVEL:
            return None

        with self._lock:
            self._caps[alert_id] = (details["expires"], details)
        return details

    # --- matching ---------------------------------------------------------

    def covers(self, lat: float, lon: float) -> bool:
        """Whether the point falls inside at least one country's polygon."""
        countries = self._country_geometries()
        if not countries:
            return False
        return any(geometry_contains(geometry, lon, lat) for _code, geometry in countries)

    def _feature_covers(self, feature: dict, lon: float, lat: float) -> bool:
        """Whether one warning area contains the point, confirmed against its outline."""
        properties = feature.get("properties") or {}
        geometry_type = properties.get("geometryType")
        if geometry_type == "no_geometry":
            self.host.logger.debug(
                f"Meteoalarm feature {feature.get('id')} has no geometry; skipped"
            )
            return False
        if not geometry_contains(feature.get("geometry"), lon, lat):
            return False
        if geometry_type == "exact":
            return True
        exact = self._exact_geometry(feature)
        if exact is None:
            # The bbox hit stands when the outline cannot be fetched.
            return True
        return geometry_contains(exact, lon, lat)

    def _alerts_for(self, lat: float, lon: float) -> list:
        """The active alerts covering a point, most severe first."""
        countries = self._country_geometries()
        if not countries:
            return []

        grouped: dict[str, dict] = {}
        for country, geometry in countries:
            if not geometry_contains(geometry, lon, lat):
                continue
            for feature in self._country_features(country) or []:
                properties = feature.get("properties") or {}
                if properties.get("supersededByAlertId"):
                    continue
                if not self._feature_covers(feature, lon, lat):
                    continue
                alert_id = properties.get("alertId")
                if alert_id:
                    grouped.setdefault(alert_id, feature)

        alerts = []
        for alert_id, feature in grouped.items():
            details = self._cap_details(alert_id, feature)
            if details is not None:
                alerts.append(details)
        alerts.sort(key=lambda alert: (-alert["level"], alert["expires"]))
        return alerts

    # --- formatting -------------------------------------------------------

    def _compact_time(self, moment: datetime) -> str:
        """A short UTC stamp for a reply: the hour when it is today, else the day and hour."""
        if moment.date() == self._utcnow().date():
            return moment.strftime("%H:%M")
        return moment.strftime("%d %b %H:%M")

    def _summary_line(self, alert: dict) -> str:
        emoji = LEVEL_EMOJI.get(alert["level"], "")
        until = self.host.translate(
            "commands.wx.meteoalarm.until", time=self._compact_time(alert["expires"])
        )
        return f"{emoji} {alert['event']} {until}".strip()

    def summary(self, lat: float, lon: float, max_length: int) -> Optional[AlertSummary]:
        """The compact alert text and count for the second message, or None."""
        alerts = self._alerts_for(lat, lon)
        if not alerts:
            return None

        lines = [self._summary_line(alert) for alert in alerts]
        text = ""
        shown = 0
        for line in lines:
            candidate = f"{text}; {line}" if text else line
            remaining = len(lines) - shown - 1
            suffix = f" +{remaining}" if remaining > 0 else ""
            if self.host._count_display_width(candidate + suffix) > max_length:
                break
            text = candidate
            shown += 1

        if shown == 0:
            return AlertSummary(text=lines[0], count=len(alerts))
        remaining = len(lines) - shown
        if remaining > 0:
            text = f"{text} +{remaining}"
        return AlertSummary(text=text, count=len(alerts))

    def _full_alert_text(self, alert: dict, index: int, total: int) -> str:
        emoji = LEVEL_EMOJI.get(alert["level"], "")
        lines = [f"{index}/{total} {emoji} {alert['event']}".strip()]
        if alert["headline"]:
            lines.append(alert["headline"])
        if alert["onset"]:
            lines.append(
                self.host.translate(
                    "commands.wx.meteoalarm.from", time=self._compact_time(alert["onset"])
                )
            )
        lines.append(
            self.host.translate(
                "commands.wx.meteoalarm.until", time=self._compact_time(alert["expires"])
            )
        )
        if alert["awareness_type"]:
            lines.append(alert["awareness_type"])
        return "\n".join(lines)

    def _fit(self, text: str, max_length: int) -> str:
        """Cut a message to the frame budget, by display width."""
        if self.host._count_display_width(text) <= max_length:
            return text
        trimmed = text
        while trimmed and self.host._count_display_width(trimmed) > max_length:
            trimmed = trimmed[:-1]
        return trimmed

    async def send_full_list(self, message: Any, lat: float, lon: float) -> bool:
        """Send one message per alert, most severe first, paced like the NWS list."""
        alerts = await asyncio.to_thread(self._alerts_for, lat, lon)
        if not alerts:
            return bool(await self.host.send_response(message, "No weather alerts"))

        max_length = self.host.get_max_message_length(message)
        rate_limit = self.host.bot.config.getfloat("Bot", "bot_tx_rate_limit_seconds", fallback=1.0)
        sleep_time = max(rate_limit + 1.0, 2.0)

        for index, alert in enumerate(alerts, 1):
            text = self._fit(self._full_alert_text(alert, index, len(alerts)), max_length)
            if not await self.host.send_response(message, text, skip_user_rate_limit=(index > 1)):
                return False
            if index < len(alerts):
                await self.host._pace_reply(message, sleep_time)
        return True
