"""Meteoalarm alerts: Europe's EUMETNET warnings, behind the weather command.

The OGC EDR API only answers by country, so a point is matched in three steps:
the country polygons from ``/collections/warnings/locations`` say which
countries could cover it, each country's warnings carry a bounding box per
warning area, and a feature whose ``geometryType`` is not ``exact`` is
confirmed against the outline in its ``rel=geometry`` link. Event, level and
expiry live in the CAP 1.2 document the feature links to, not in the feature.

The API wants a ``datetime`` range shorter than a day and still carries
warnings sent days ago, so each country is asked for once per UTC day over the
last four days. The query is by sent date only: whether a warning is active is
decided from its CAP document, because a warning sent yesterday can become
active after the window was cached. It throttles hard, so requests are spaced a
second apart and a 429 pauses every request until its Retry-After passes,
serving cached alerts meanwhile. The feature links are pre-signed URLs on a
storage host: they are fetched without the token and never logged above DEBUG.

The token is sent as a bearer header, never as a query parameter, so it stays
out of URLs and logs, and only to the configured API origin over HTTPS.
Nothing here raises into the command: a failed fetch logs one WARNING per
failure streak and answers with no alerts.

The base URL defaults to the MeteoGate gateway, which serves the same EDR API
and is where a public token comes from. Meteoalarm Direct re-user tokens point
``[Weather] meteoalarm_api_url`` at ``https://api.meteoalarm.org/edr/v1``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import AlertsProvider, AlertSummary, Coverage, ProviderHost

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
# The API rejects a datetime range of 24 hours or more, so each UTC day is a
# window of its own: today's moves and is cached briefly, past days are fixed.
WINDOW_TTL_TODAY = 10 * 60
WINDOW_TTL_PAST = 30 * 60
PAST_DAYS = 3
MAX_PAGES = 5
# Cost: a cold lookup is one locations request plus four window requests per
# country. Steady state is about twelve per country per hour (today's window
# every ten minutes, each of the three past days every thirty), against a
# budget of fifty per window per IP. Requests are spaced a second apart and a
# 429 pauses everything until its Retry-After passes.
MIN_REQUEST_INTERVAL = 1.0
DEFAULT_RETRY_AFTER = 300.0
# The gateway reports the budget in X-RateLimit-Remaining. Near the floor,
# refreshes stop until the reset.
RATE_LIMIT_FLOOR = 5
# Europe's rough extent, wide enough for the Canary Islands and Cyprus. It is
# the ALL feature of the live locations list, widened south to 27 N.
EUROPE_LON = (-25.0, 45.0)
EUROPE_LAT = (27.0, 72.0)
# A bound on the outline cache: one entry per feature id, oldest evicted first.
GEOMETRY_CACHE_MAX = 300
# How long a thread waits for another thread's refresh of the same key.
INFLIGHT_TIMEOUT = 30.0

# A fetch that could not be made, as opposed to one that found nothing.
UNAVAILABLE = object()

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


def _stamp(moment: datetime) -> str:
    """A UTC timestamp in the ISO 8601 form the API's datetime parameter takes."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _origin(url: str) -> tuple:
    """(scheme, host, port) for a URL, with the default port filled in."""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (parsed.scheme, parsed.hostname, port)


def _reset_seconds(headers: Any, fallback: float) -> float:
    """X-RateLimit-Reset as seconds from now, or *fallback*."""
    raw = (headers or {}).get("X-RateLimit-Reset")
    try:
        return max(float(str(raw).strip()), 0.0)
    except (TypeError, ValueError):
        return fallback


def _total_pages(metadata: Any) -> int:
    """metadata.total_pages as a positive int, 1 when it is missing or unusable."""
    try:
        pages = int((metadata or {}).get("total_pages") or 1)
    except (TypeError, ValueError, AttributeError):
        return 1
    return max(pages, 1)


def _in_europe(lat: float, lon: float) -> bool:
    """Whether a point falls inside Europe's rough extent."""
    return EUROPE_LON[0] <= lon <= EUROPE_LON[1] and EUROPE_LAT[0] <= lat <= EUROPE_LAT[1]


def _retry_after_seconds(headers: Any, now: datetime,
                         fallback: float = DEFAULT_RETRY_AFTER) -> float:
    """Retry-After as seconds, from a number or an HTTP date, else *fallback*."""
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return fallback
    text = str(raw).strip()
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return fallback
    if when is None:
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - now).total_seconds(), 0.0)


def _geometry_of(body: Any) -> Optional[dict]:
    """The geometry in a GeoJSON Feature, FeatureCollection or bare geometry."""
    if not isinstance(body, dict):
        return None
    kind = body.get("type")
    if kind == "Feature":
        return body.get("geometry")
    if kind == "FeatureCollection":
        for feature in body.get("features") or []:
            geometry = (feature or {}).get("geometry")
            if geometry:
                return geometry
        return None
    if kind in ("Polygon", "MultiPolygon"):
        return body
    return None


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


def _references(value: Optional[str]) -> set:
    """The identifiers a CAP references: space-separated "sender,id,sent" triples."""
    parts = (value or "").replace(",", " ").split()
    return {parts[index] for index in range(1, len(parts), 3)}


def _index_info(feature: dict) -> Optional[int]:
    """A feature's indexInfo: which CAP info block describes its area."""
    value = (feature.get("properties") or {}).get("indexInfo")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cap_info(root: ET.Element, language: str, indexes: Optional[set] = None) -> Optional[ET.Element]:
    """The info block for *language* (prefix match), else English, else the first.

    *indexes* are the ``indexInfo`` values of the features that contain the
    point. One alert can carry several blocks in the same language for
    different areas, so only those blocks are candidates.
    """
    infos = root.findall("cap:info", CAP_NS)
    if not infos:
        return None
    if indexes:
        matched = [infos[index] for index in sorted(indexes) if 0 <= index < len(infos)]
        if matched:
            infos = matched

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


def parse_cap(text: str, language: str, indexes: Optional[set] = None) -> Optional[dict]:
    """The alert details a CAP 1.2 document carries, or None when it has no info block.

    Parsed with ElementTree rather than defusedxml: the document comes over
    HTTPS from the storage host the API's own links name, and the project does
    not depend on defusedxml.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    info = _cap_info(root, language, indexes)
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

    def text_of(tag: str) -> str:
        return (info.findtext(f"cap:{tag}", default="", namespaces=CAP_NS) or "").strip()

    return {
        "identifier": (root.findtext("cap:identifier", default="", namespaces=CAP_NS) or "").strip(),
        "msg_type": (root.findtext("cap:msgType", default="", namespaces=CAP_NS) or "").strip(),
        "event": text_of("event"),
        "headline": text_of("headline"),
        "onset": _parse_time(text_of("onset")),
        "effective": _parse_time(text_of("effective")),
        "sent": _parse_time((root.findtext("cap:sent", default="", namespaces=CAP_NS) or "").strip()),
        "expires": _parse_time(text_of("expires")),
        "supersedes": _references(
            root.findtext("cap:references", default="", namespaces=CAP_NS) or ""
        ),
        "level": level,
        "awareness_type": awareness_type,
    }


def _is_active(details: dict, now: datetime) -> bool:
    """Whether a CAP document describes a warning in force at *now*."""
    expires = details["expires"]
    if expires is None or expires <= now:
        return False
    start = details["onset"] or details["effective"] or details["sent"]
    return start is None or start <= now


def _make_session() -> requests.Session:
    """A session that retries connection errors and 5xx, never a 429.

    A 429 is the gateway's rate limit and the provider owns the pause, so the
    session must hand the response back instead of retrying behind its back.
    """
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class _RateLimit:
    """The pause and the remaining budget for one API origin.

    Shared by every provider instance that talks to the same origin, so wx, gwx
    and the Open-Meteo fallback cannot each spend the same budget.
    """

    __slots__ = ("lock", "retry_until", "last_request", "remaining")

    def __init__(self):
        self.lock = threading.Lock()
        self.retry_until = 0.0
        self.last_request = 0.0
        self.remaining: Optional[int] = None


_RATE_LIMITS: dict[tuple, _RateLimit] = {}
_RATE_LIMITS_LOCK = threading.Lock()


def _rate_limit_for(base_url: str) -> _RateLimit:
    """The shared rate-limit state for a base URL's origin."""
    origin = _origin(base_url)
    with _RATE_LIMITS_LOCK:
        state = _RATE_LIMITS.get(origin)
        if state is None:
            state = _RATE_LIMITS[origin] = _RateLimit()
        return state


def reset_rate_limits() -> None:
    """Forget every origin's pause and budget. Tests call this between cases."""
    with _RATE_LIMITS_LOCK:
        _RATE_LIMITS.clear()


class _Inflight:
    """One in-flight refresh that other threads wait on instead of refetching."""

    __slots__ = ("done", "value")

    def __init__(self):
        self.done = threading.Event()
        self.value = None


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
        self.session = session if session is not None else _make_session()
        # One lock for every cache. Never held across HTTP.
        self._lock = threading.Lock()
        self._api_origin = _origin(self.base_url)
        self._rate = _rate_limit_for(self.base_url)
        self._locations: Optional[tuple] = None
        self._windows: dict[tuple, tuple] = {}
        self._geometries: dict[str, dict] = {}
        self._caps: dict[tuple, tuple] = {}
        self._streaks: set[str] = set()
        self._inflight: dict[Any, _Inflight] = {}

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

    def _wait(self, seconds: float) -> None:
        """Sleep between gateway requests. Tests replace this with a fake clock."""
        time.sleep(seconds)

    def _paused(self) -> bool:
        """Whether the shared pause for this origin is still running."""
        with self._rate.lock:
            return self._clock() < self._rate.retry_until

    def _pace(self) -> None:
        """Space gateway requests at least MIN_REQUEST_INTERVAL apart."""
        state = self._rate
        with state.lock:
            now = self._clock()
            wait = MIN_REQUEST_INTERVAL - (now - state.last_request) if state.last_request else 0.0
            state.last_request = now + max(wait, 0.0)
        if wait > 0:
            self._wait(wait)

    def _throttled(self, response) -> None:
        """Honor Retry-After, else X-RateLimit-Reset: no gateway requests until it passes."""
        headers = getattr(response, "headers", None) or {}
        delay = _retry_after_seconds(headers, self._utcnow(), _reset_seconds(headers, DEFAULT_RETRY_AFTER))
        with self._rate.lock:
            self._rate.retry_until = self._clock() + delay
        self._warn_once(
            "throttled",
            f"Meteoalarm rate limited the request; pausing for {int(delay)} s",
        )

    def _note_rate_limit(self, response) -> None:
        """Track X-RateLimit-Remaining: near the floor, serve cache until the reset."""
        headers = getattr(response, "headers", None) or {}
        try:
            remaining = int(float(str(headers.get("X-RateLimit-Remaining")).strip()))
        except (TypeError, ValueError):
            return
        with self._rate.lock:
            self._rate.remaining = remaining
        if remaining > RATE_LIMIT_FLOOR:
            return
        delay = _reset_seconds(headers, DEFAULT_RETRY_AFTER)
        with self._rate.lock:
            self._rate.retry_until = max(self._rate.retry_until, self._clock() + delay)
        self.host.logger.debug(
            f"Meteoalarm rate limit budget is down to {remaining}; "
            f"serving cached alerts for {int(delay)} s"
        )

    def _get(self, url: str, params: Optional[dict] = None):
        """One GET, paced and rate-limit aware. Returns the response, or UNAVAILABLE.

        The bearer token goes only to the configured API origin over HTTPS. The
        feature links are pre-signed URLs on a storage host, and those must
        never see it.
        """
        api = _origin(url) == self._api_origin
        # The pause is the gateway's budget, not the storage host's.
        if api and self._paused():
            return UNAVAILABLE
        self._pace()
        # Recheck right before sending: the pacing sleep may have outlasted the
        # pause, or another thread may have spent the budget meanwhile.
        if api and self._paused():
            return UNAVAILABLE
        headers = {}
        if self.token and api and self._api_origin[0] == "https":
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.session.get(
                url, params=params, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            )
        except Exception as error:
            self._warn_once(
                f"http:{urlparse(url).netloc}",
                f"Meteoalarm request failed: {type(error).__name__}",
            )
            return UNAVAILABLE
        if response.status_code == 429:
            self._throttled(response)
            return UNAVAILABLE
        if self._rejected(response):
            return UNAVAILABLE
        if response.status_code >= 400:
            self._warn_once("status", f"Meteoalarm answered HTTP {response.status_code}")
            return UNAVAILABLE
        # A success from this host ends every failure streak it opened.
        self._clear_streak("status")
        self._clear_streak("throttled")
        self._clear_streak("auth")
        self._clear_streak(f"http:{urlparse(url).netloc}")
        if api:
            self._note_rate_limit(response)
        return response

    def _json(self, response):
        """The JSON body, or None when the body is empty or not JSON."""
        try:
            return response.json()
        except Exception:
            return None

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

    # --- shared refreshes -------------------------------------------------

    def _shared(self, key: Any, fetch):
        """Run *fetch* once per key across threads; the others wait for its result."""
        with self._lock:
            entry = self._inflight.get(key)
            owner = entry is None
            if owner:
                entry = self._inflight[key] = _Inflight()
        if not owner:
            entry.done.wait(INFLIGHT_TIMEOUT)
            return entry.value
        try:
            entry.value = fetch()
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            entry.done.set()
        return entry.value

    # --- caches -----------------------------------------------------------

    def _cached_locations(self) -> Optional[list]:
        with self._lock:
            cached = self._locations
        if cached is not None and self._clock() - cached[0] < LOCATIONS_TTL:
            return cached[1]
        return None

    def _country_geometries(self) -> Optional[list]:
        """[(country code, geometry)] for every country, cached for a day."""
        cached = self._cached_locations()
        if cached is not None:
            return cached
        return self._shared(("locations",), self._fetch_locations)

    def _fetch_locations(self) -> Optional[list]:
        cached = self._cached_locations()
        if cached is not None:
            return cached
        with self._lock:
            stale = self._locations
        response = self._get(self.locations_url)
        if response is UNAVAILABLE:
            # A stale list still says which countries could cover the point.
            return stale[1] if stale is not None else None
        if response.status_code == 204:
            features = []
        else:
            body = self._json(response)
            if body is None:
                self._warn_once("locations", "Meteoalarm locations fetch returned no JSON")
                return stale[1] if stale is not None else None
            features = body.get("features") or []

        countries = [
            (feature.get("id"), feature.get("geometry"))
            for feature in features
            if feature.get("id") and feature.get("id") != "ALL" and feature.get("geometry")
        ]
        with self._lock:
            self._locations = (self._clock(), countries)
        self._clear_streak("locations")
        return countries

    def _window_specs(self) -> list:
        """One query window per UTC day: today so far, then the three previous days.

        The API rejects a ``datetime`` range of 24 hours or more, and warnings
        sent days ago are still active, so each day is asked for separately.
        """
        now = self._utcnow()
        today = now.date()
        first = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        end = now if now > first else first + timedelta(seconds=1)
        specs = [(today, f"{_stamp(first)}/{_stamp(end)}", WINDOW_TTL_TODAY)]
        for offset in range(1, PAST_DAYS + 1):
            day = today - timedelta(days=offset)
            start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            last = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc).replace(microsecond=0)
            specs.append((day, f"{_stamp(start)}/{_stamp(last)}", WINDOW_TTL_PAST))
        return specs

    def _cached_window(self, country: str, day, ttl: int) -> Optional[list]:
        with self._lock:
            cached = self._windows.get((country, day))
        if cached is not None and self._clock() - cached[0] < ttl:
            return cached[1]
        return None

    def _window_features(self, country: str, day, interval: str, ttl: int) -> tuple:
        """(features, incomplete) for one country and one day window."""
        cached = self._cached_window(country, day, ttl)
        if cached is not None:
            return cached, False
        return self._shared(
            ("window", country, day), lambda: self._refresh_window(country, day, interval, ttl)
        )

    def _refresh_window(self, country: str, day, interval: str, ttl: int) -> tuple:
        cached = self._cached_window(country, day, ttl)
        if cached is not None:
            return cached, False
        with self._lock:
            stale = self._windows.get((country, day))
        features = self._fetch_window(country, interval)
        if features is None:
            # A failed window serves whatever is cached, stale included.
            return (stale[1] if stale is not None else []), True
        with self._lock:
            self._windows[(country, day)] = (self._clock(), features)
        return features, False

    def _fetch_window(self, country: str, interval: str) -> Optional[list]:
        """One country's warnings for one window, following pagination.

        The query is by sent date only: whether a warning is active is decided
        from its CAP document, so a warning that became active after the window
        was cached is not hidden.
        """
        features: list = []
        total_pages = 1
        for page in range(1, MAX_PAGES + 1):
            response = self._get(
                self.country_url.format(country=country),
                params={"datetime": interval, "page": page},
            )
            if response is UNAVAILABLE:
                return None
            if response.status_code == 204:
                break
            body = self._json(response)
            if body is None:
                self._warn_once(
                    f"country:{country}",
                    f"Meteoalarm warnings fetch for {country} returned no JSON",
                )
                return None
            features.extend(body.get("features") or [])
            total_pages = _total_pages(body.get("metadata"))
            if page >= total_pages:
                break
        if total_pages > MAX_PAGES:
            self.host.logger.debug(
                f"Meteoalarm window for {country} has {total_pages} pages; "
                f"capped at {MAX_PAGES}"
            )
        self._clear_streak(f"country:{country}")
        return features

    def _country_features(self, country: str) -> tuple:
        """(features, incomplete) for one country, merged over the day windows."""
        merged: dict[Any, dict] = {}
        incomplete = False
        for day, interval, ttl in self._window_specs():
            features, failed = self._window_features(country, day, interval, ttl)
            incomplete = incomplete or failed
            for feature in features:
                merged[feature.get("id") or id(feature)] = feature
        return list(merged.values()), incomplete

    def _exact_geometry(self, feature: dict) -> Optional[dict]:
        """The outline behind a bbox feature, cached by feature id.

        The link is pre-signed and changes between responses, so the id is the
        only stable key.
        """
        properties = feature.get("properties") or {}
        key = feature.get("id") or properties.get("OBJECTID")
        if not key:
            return None
        with self._lock:
            cached = self._geometries.get(key)
        if cached is not None:
            return cached
        return self._shared(("geometry", key), lambda: self._fetch_geometry(key, feature))

    def _fetch_geometry(self, key: str, feature: dict) -> Optional[dict]:
        with self._lock:
            cached = self._geometries.get(key)
        if cached is not None:
            return cached
        url = next(
            (link.get("href") for link in feature.get("links") or [] if link.get("rel") == "geometry"),
            None,
        )
        if not url:
            return None
        response = self._get(url)
        if response is UNAVAILABLE or response.status_code == 204:
            return None
        geometry = _geometry_of(self._json(response))
        if geometry is None:
            self._warn_once(f"geometry:{key}", "Meteoalarm geometry fetch returned no geometry")
            return None
        with self._lock:
            self._geometries[key] = geometry
        return geometry

    def _language(self) -> str:
        """``[Localization] language``, lowercased, for the CAP info block."""
        try:
            return (self.host.bot.config.get("Localization", "language", fallback="en") or "en").strip().lower()
        except Exception:
            return "en"

    def _cap_details(self, alert_id: str, features: list, indexes: set) -> tuple:
        """(details, incomplete) for one alert, cached by alertId and area.

        Every feature of an alertId links to the same document, so the first one
        that carries an xml link is enough.
        """
        key = (alert_id, tuple(sorted(indexes)))
        with self._lock:
            cached = self._caps.get(key)
        if cached is not None and cached[0] > self._utcnow():
            return cached[1], False
        return self._shared(("cap", key), lambda: self._fetch_cap(alert_id, features, indexes, key))

    def _fetch_cap(self, alert_id: str, features: list, indexes: set, key: tuple) -> tuple:
        with self._lock:
            cached = self._caps.get(key)
        if cached is not None and cached[0] > self._utcnow():
            return cached[1], False

        url = None
        for feature in features:
            properties = feature.get("properties") or {}
            url = next(
                (link.get("href") for link in feature.get("links") or [] if link.get("rel") == "xml"),
                None,
            ) or properties.get("hubLink")
            if url:
                break
        if not url:
            return None, True

        response = self._get(url)
        if response is UNAVAILABLE or response.status_code == 204:
            return None, True
        details = parse_cap(response.text, self._language(), indexes)
        if details is None or details["expires"] is None:
            return None, True
        if details["msg_type"].lower() == "cancel":
            return None, False
        if not _is_active(details, self._utcnow()) or details["level"] < MIN_LEVEL:
            return None, False

        # Capped at the window TTL: a later Cancel or Update must not hide
        # behind a document cached until its own expiry.
        until = min(details["expires"], self._utcnow() + timedelta(seconds=WINDOW_TTL_TODAY))
        with self._lock:
            self._caps[key] = (until, details)
        return details, False

    def _evict(self) -> None:
        """Drop window entries past the four days and CAP entries that expired."""
        now = self._utcnow()
        oldest = now.date() - timedelta(days=PAST_DAYS)
        with self._lock:
            for key in [key for key in self._windows if key[1] < oldest]:
                del self._windows[key]
            for key in [key for key, value in self._caps.items() if value[0] <= now]:
                del self._caps[key]
            while len(self._geometries) > GEOMETRY_CACHE_MAX:
                self._geometries.pop(next(iter(self._geometries)))

    # --- matching ---------------------------------------------------------

    def covers(self, lat: float, lon: float) -> bool:
        """Whether the point falls inside at least one country's polygon."""
        return self.coverage_for(lat, lon) is Coverage.COVERED

    def coverage_for(self, lat: float, lon: float) -> Coverage:
        """Whether the point falls inside a country polygon, or cannot be told.

        With no locations list at all, a point inside Europe's extent is treated
        as possibly covered so the alerts path goes on and reports the fetch
        failure instead of claiming the source does not cover the place.
        """
        countries = self._country_geometries()
        if countries is None:
            return Coverage.COVERED if _in_europe(lat, lon) else Coverage.NOT_COVERED
        if any(geometry_contains(geometry, lon, lat) for _code, geometry in countries):
            return Coverage.COVERED
        return Coverage.NOT_COVERED

    def _feature_covers(self, feature: dict, lon: float, lat: float) -> tuple:
        """(matched, incomplete) for one warning area, confirmed against its outline."""
        properties = feature.get("properties") or {}
        geometry_type = properties.get("geometryType")
        if geometry_type == "no_geometry":
            self.host.logger.debug(
                f"Meteoalarm feature {feature.get('id')} has no geometry; skipped"
            )
            return False, False
        if not geometry_contains(feature.get("geometry"), lon, lat):
            return False, False
        if geometry_type == "exact":
            return True, False
        exact = self._exact_geometry(feature)
        if exact is None:
            # A bbox is not proof that the point is inside the warning area.
            self.host.logger.debug(
                f"Meteoalarm outline for {feature.get('id')} is unavailable; feature skipped"
            )
            return False, True
        return geometry_contains(exact, lon, lat), False

    def _alerts_for(self, lat: float, lon: float) -> Optional[list]:
        """The active alerts covering a point, most severe first.

        One alert has a feature per CAP info language and area, so features are
        grouped by alertId and the point has to fall inside any one of them.
        None means the data could not be fetched, as opposed to no alerts: any
        window, outline or CAP needed for a candidate that failed makes an empty
        answer unknown rather than an all-clear.
        """
        self._evict()
        countries = self._country_geometries()
        if countries is None:
            return None

        grouped: dict[str, list] = {}
        incomplete = False
        for country, geometry in countries:
            if not geometry_contains(geometry, lon, lat):
                continue
            features, failed = self._country_features(country)
            incomplete = incomplete or failed
            for feature in features:
                properties = feature.get("properties") or {}
                if properties.get("supersededByAlertId"):
                    continue
                alert_id = properties.get("alertId")
                if alert_id:
                    grouped.setdefault(alert_id, []).append(feature)

        alerts = []
        superseded: set = set()
        for alert_id, features in grouped.items():
            matched = []
            for feature in features:
                covers, failed = self._feature_covers(feature, lon, lat)
                incomplete = incomplete or failed
                if covers:
                    matched.append(feature)
            if not matched:
                continue
            indexes = {index for index in (_index_info(feature) for feature in matched)
                       if index is not None}
            details, failed = self._cap_details(alert_id, matched, indexes)
            incomplete = incomplete or failed
            if details is None:
                continue
            superseded |= details["supersedes"]
            alerts.append(details)

        alerts = [alert for alert in alerts if alert["identifier"] not in superseded]
        alerts.sort(key=lambda alert: (-alert["level"], alert["expires"]))
        if incomplete and not alerts:
            return None
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
            # The first line alone is too long: trim it and re-check the suffix.
            return AlertSummary(
                text=self._trim(lines[0], max_length, len(lines) - 1), count=len(alerts)
            )
        remaining = len(lines) - shown
        if remaining > 0:
            text = f"{text} +{remaining}"
        return AlertSummary(text=text, count=len(alerts))

    def _trim(self, line: str, max_length: int, remaining: int) -> str:
        """Cut a line to the budget with an ellipsis, leaving room for " +N"."""
        suffix = f" +{remaining}" if remaining > 0 else ""
        budget = max_length - self.host._count_display_width(suffix)
        trimmed = line
        while trimmed and self.host._count_display_width(trimmed + "\u2026") > budget:
            trimmed = trimmed[:-1]
        return f"{trimmed}\u2026{suffix}" if trimmed else suffix.strip()

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
        if alerts is None:
            return bool(await self.host.send_response(
                message, self.host.translate("commands.wx.meteoalarm.error_fetching")
            ))
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
