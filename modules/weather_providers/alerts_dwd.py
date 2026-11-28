"""German weather service alerts: the ``dwd`` alerts provider, via Bright Sky.

Bright Sky republishes Deutscher Wetterdienst's CAP warnings as JSON. One
request for a point answers with the warnings for the DWD warning cell that
contains it, plus the cell's own name, district and state, and a point outside
Germany answers 404 rather than an empty list.

The shapes are the ones Meteoalarm, Environment Canada and NWS use: a compact
summary for the second message and a packed full list, both from
:mod:`._alerts_common`. A failed fetch is reported as a fetch error, never as
"No weather alerts", and nothing raises into the command.

DWD's warnings are open data that requires credit, so ``wx`` help names the
source when this provider is selected.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..http_retry import make_retry_session
from ._alerts_common import PackedAlertsMixin
from ._tz import utc_offset_at
from .base import AlertsProvider, Coverage, ProviderHost
from .metno import _default_user_agent

BRIGHTSKY_BASE_URL = "https://api.brightsky.dev"
BRIGHTSKY_TIMEOUT = (5, 6)  # (connect, read) seconds, per attempt
ALERT_CACHE_SECONDS = 300
# A failed fetch is retried soon: caching it as long as a success would hide a
# recovered service for the whole TTL.
FAILURE_CACHE_SECONDS = 60

# Germany, wide enough for the coast and the Alpine edge.
GERMANY_LON = (5.8, 15.1)
GERMANY_LAT = (47.2, 55.1)

# DWD's own four-step scale: yellow, orange, red, violet. The shared level
# numbers rank them, so a violet warning sorts above a red one.
SEVERITY_LEVEL = {"minor": 2, "moderate": 3, "severe": 4, "extreme": 5}

# A warning that has not started yet is shown only when it starts soon.
FUTURE_ONSET = timedelta(hours=24)


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


# German warning titles are written in capitals ("FROST", "SCHWERES GEWITTER")
# and read as title case, except for the joining words.
_GERMAN_SMALL_WORDS = {"und", "oder", "mit", "bei", "vor", "im", "am"}


def _title_case(text: str, language: str = "en") -> str:
    """DWD's events are written in capitals; the list is not.

    English events arrive lower case ("frost", "icy surfaces") and take a
    capital on the first word only. German ones arrive in full capitals and
    read as title case, word by word, with the joining words left alone.
    """
    if not text:
        return text
    if language != "de":
        return text[:1].upper() + text[1:]
    out = []
    first = True
    for word in text.lower().split(" "):
        pieces: list[str] = []
        for piece in word.split("-"):
            if not piece:
                pieces.append(piece)
            elif not first and piece in _GERMAN_SMALL_WORDS:
                pieces.append(piece)
            else:
                pieces.append(piece[:1].upper() + piece[1:])
            if piece:
                first = False
        out.append("-".join(pieces))
    return " ".join(out)


class DwdAlertsProvider(PackedAlertsMixin, AlertsProvider):
    """Deutscher Wetterdienst's warnings, from Bright Sky."""

    name = "dwd"
    translation_ns = "commands.wx"
    fetch_error_key = "commands.wx.dwd.error_fetching"

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
        """Whether the point is inside Germany, where DWD warns."""
        return (
            GERMANY_LAT[0] <= lat <= GERMANY_LAT[1]
            and GERMANY_LON[0] <= lon <= GERMANY_LON[1]
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
        """The active alerts for a point, most severe first.

        None means the data could not be fetched, as opposed to no alerts, and
        nothing raises into the command.
        """
        try:
            return self._alerts_for_point(lat, lon)
        except Exception as error:
            self._warn_once("alerts", f"DWD alert lookup failed: {type(error).__name__}")
            return None

    def _alerts_for_point(self, lat: float, lon: float) -> Optional[list]:
        """The body of :meth:`_alerts_for`."""
        body, failed = self._fetch(lat, lon)
        if failed:
            return None
        if body is None:
            # A 404: the point is outside DWD's warning cells.
            return []

        now = self._now()
        raw_location = body.get("location")
        location = raw_location if isinstance(raw_location, dict) else {}
        area = str(location.get("name") or "").strip()
        alerts = []
        for raw in body.get("alerts") or []:
            if not isinstance(raw, dict):
                continue
            alert = self._normalize(raw, area, lat, lon, now)
            if alert is not None:
                alerts.append(alert)
        alerts.sort(key=lambda alert: (-alert["level"], alert["onset"] or now))
        return alerts

    def _fetch(self, lat: float, lon: float) -> tuple[Optional[dict], bool]:
        """The alerts around a point, and whether the fetch failed.

        ``(None, False)`` is a 404, which means the point is not covered; a 5xx,
        a timeout or a malformed body is ``(None, True)``.
        """
        key = (round(lat, 2), round(lon, 2))
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1], cached[2]

        body, failed = self._request(lat, lon)
        ttl = FAILURE_CACHE_SECONDS if failed else ALERT_CACHE_SECONDS
        with self._lock:
            self._cache[key] = (now + ttl, body, failed)
        return body, failed

    def _request(self, lat: float, lon: float) -> tuple[Optional[dict], bool]:
        """One request for the alerts around a point."""
        url = f"{BRIGHTSKY_BASE_URL}/alerts"
        params = {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"}
        headers = {"User-Agent": self._user_agent()}
        try:
            if self._session is not None:
                response = self._session.get(
                    url, params=params, headers=headers, timeout=BRIGHTSKY_TIMEOUT
                )
            else:
                with make_retry_session() as session:
                    response = session.get(
                        url, params=params, headers=headers, timeout=BRIGHTSKY_TIMEOUT
                    )
        except Exception as error:
            self._warn_once("http", f"Error fetching DWD alerts: {error}")
            return None, True

        status = getattr(response, "status_code", None)
        if status == 404:
            # Bright Sky answers 404 for a point outside its warning cells, and
            # those cells do not follow the border: Prague, Zurich and
            # Strasbourg are all inside the bbox and outside the service. A 404
            # is therefore "not covered" wherever the point is.
            self._clear_streak("http")
            return None, False
        if not getattr(response, "ok", False):
            self._warn_once("http", f"Error fetching DWD alerts: {status or '?'}")
            return None, True
        self._clear_streak("http")

        try:
            body = response.json()
        except Exception:
            self._warn_once("json", "DWD alerts returned no JSON")
            return None, True
        if not isinstance(body, dict):
            self._warn_once("json", "DWD alerts returned an unexpected body")
            return None, True
        return body, False

    def _user_agent(self) -> str:
        """The identifying User-Agent, the same one MET Norway's terms ask for."""
        return _default_user_agent()

    # --- reading the data -------------------------------------------------

    def _language(self) -> str:
        """The reply language: German when the bot's language is German, else English."""
        try:
            value = (self.host.bot.config.get('Localization', 'language', fallback='') or '').strip()
        except Exception:
            value = ''
        return 'de' if value.lower().startswith('de') else 'en'

    def _normalize(self, raw: dict, area: str, lat: float, lon: float,
                   now: datetime) -> Optional[dict]:
        """One alert in the shape the shared summary and full list expect."""
        if str(raw.get("status") or "").strip().lower() != "actual":
            return None

        expires = _parse_time(raw.get("expires"))
        if expires is not None and expires <= now:
            return None

        onset = _parse_time(raw.get("onset")) or _parse_time(raw.get("effective"))
        if onset is not None and onset > now + FUTURE_ONSET:
            return None

        language = self._language()
        event = raw.get(f"event_{language}") or raw.get("event_en") or ""
        severity = str(raw.get("severity") or "").strip().lower()
        return {
            "level": SEVERITY_LEVEL.get(severity, 2),
            "event": _title_case(str(event).strip(), language),
            "onset": self._local(onset, lat, lon),
            "expires": self._local(expires, lat, lon),
            "area": area,
        }

    @staticmethod
    def _local(moment: Optional[datetime], lat: float, lon: float) -> Optional[datetime]:
        """A UTC instant in the point's own offset, so the window reads locally."""
        if moment is None:
            return None
        return moment + utc_offset_at(lat, lon, moment)
