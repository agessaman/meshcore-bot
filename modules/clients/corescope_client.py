#!/usr/bin/env python3
"""CoreScope client - unique paths an observer network heard for one packet.

CoreScope (e.g. https://analyzer.comchan.net) collects MeshCore packets from many
observers. One packet reaches each observer by its own route, so CoreScope holds a
far wider view of a packet's spread than any single radio: on the packet this
client was developed against, 46 observations from 25 observers carried 40
distinct routes, where the bot's own radio heard a handful.

The API is a single call, ``GET /api/packets/<hash>``, keyed on the MeshCore packet
hash the bot already calculates (:func:`modules.packet_decode.calculate_packet_hash`).
It needs no authentication and is case-insensitive on the hash.

CoreScope does **not** return a path count. ``observation_count`` counts receptions,
and the map's own path picker lists one entry per observation without deduplicating,
so the count here is this client's definition: distinct hop sequences, lowercased,
empty paths dropped. See :meth:`CoreScopeClient.fetch`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# MeshCore packet hash: 8 bytes, 16 hex chars. calculate_packet_hash() returns it
# uppercase and CoreScope serves it lowercase; the API accepts either.
_HASH_RE = re.compile(r"^[0-9a-fA-F]{16}$")

# A packet relayed widely enough carries a large observation list -- 46 observations
# were 80KB, and a heavily flooded packet has no fixed ceiling. Abandon rather than
# parse an unbounded body into the bot's memory.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

DEFAULT_TIMEOUT = 4.0
DEFAULT_CACHE_SECONDS = 300


@dataclass(frozen=True)
class ScopePacket:
    """What CoreScope holds for one packet.

    ``paths`` are comma-joined lowercase hop prefixes -- the same shape
    ``MultitestCommand`` keeps in ``collected_paths`` (``extract_path_from_rf_data``
    lowercases every hop) -- so they can be handed straight to
    ``_condense_path_lines`` and look the same in a reply whichever mode produced
    them. CoreScope serves the hops uppercase; deduplication is case-insensitive.
    """

    unique_paths: int
    observations: int
    observers: int
    max_hops: int
    paths: tuple[str, ...] = ()


def _make_session() -> requests.Session:
    """A session that retries once, not twice.

    ``modules.http_retry.make_retry_session`` allows two retries with backoff, so a
    4s timeout there becomes a ~13s worst case. This lookup already sits behind a
    settle delay before a reply the user is waiting on, so it trades a retry for a
    predictable ceiling.
    """
    session = requests.Session()
    retry = Retry(
        total=1,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def normalize_base_url(base: str) -> str:
    """Trim a configured base URL, adding ``https://`` when no scheme is given.

    Returns ``""`` for anything unusable, which callers treat as "not configured"
    rather than guessing a host: this client talks to whichever instance the
    operator named, and there is no sensible default to fall back to.
    """
    raw = (base or "").strip()
    if not raw:
        return ""
    # Check the scheme before trimming: stripping "/" from a bare "https://" first
    # leaves "https:", which then reads as a scheme-less host and normalizes to the
    # nonsense "https://https:".
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    return raw.rstrip("/")


class CoreScopeClient:
    """Fetches and counts the paths CoreScope holds for a packet hash.

    Every failure -- unusable hash, 404, timeout, transport error, oversized or
    malformed body -- returns ``None`` from :meth:`fetch`. The caller renders the
    corresponding placeholders empty; a lookup that did not work must never take
    down the reply it was meant to enrich.

    Blocking. :meth:`fetch` does network I/O with ``requests``, so callers on the
    event loop must run it in an executor (see
    ``modules.response_template.format_piped_template_async`` for the pattern) --
    inline it would stall radio RX for the whole timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        logger: Any = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.logger = logger
        self._session = session
        self._cache: dict[str, tuple[float, ScopePacket]] = {}

    @property
    def configured(self) -> bool:
        """True when a usable base URL was given."""
        return bool(self.base_url)

    def map_url(self, packet_hash: str) -> str:
        """Deep link to the packet's map page with every path drawn.

        ``&paths=all`` opens the path picker's union view. The hash is lowercased
        to match the links CoreScope itself produces; the API and the page both
        accept either case. Returns ``""`` when unconfigured or given a bad hash,
        so the placeholder renders empty instead of a broken link.
        """
        if not self.configured or not _HASH_RE.match(packet_hash or ""):
            return ""
        return f"{self.base_url}/#/map?packet={packet_hash.lower()}&paths=all"

    def _debug(self, msg: str) -> None:
        if self.logger is not None:
            self.logger.debug(msg)

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = _make_session()
        return self._session

    def _cached(self, key: str) -> Optional[ScopePacket]:
        hit = self._cache.get(key)
        if hit is None:
            return None
        stored_at, packet = hit
        if (time.time() - stored_at) >= self.cache_seconds:
            del self._cache[key]
            return None
        return packet

    def fetch(self, packet_hash: str) -> Optional[ScopePacket]:
        """Look up *packet_hash*, or ``None`` if it cannot be counted.

        Paths are deduplicated on the hop sequence: each observation's
        ``path_json`` (itself a JSON string, not an array) is parsed, hops are
        lowercased and joined with commas, and empty paths -- an observer that
        heard the packet direct -- are left out, matching how the bot counts its
        own paths. A route that is a prefix of another counts separately: two
        observers hearing the same packet two and three hops out saw two
        different routes.
        """
        if not self.configured:
            return None
        if not _HASH_RE.match(packet_hash or ""):
            # A malformed hash 404s, so spending a round trip on it only turns a
            # local mistake into an indistinguishable remote failure.
            self._debug(f"CoreScope: refusing to look up malformed hash {packet_hash!r}")
            return None

        key = packet_hash.lower()
        cached = self._cached(key)
        if cached is not None:
            return cached

        body = self._get(key)
        if body is None:
            return None
        packet = self._parse(body)
        if packet is None:
            return None
        self._cache[key] = (time.time(), packet)
        return packet

    def _get(self, packet_hash: str) -> Optional[str]:
        """GET the packet, enforcing the size ceiling. ``None`` on any failure."""
        url = f"{self.base_url}/api/packets/{packet_hash}"
        response: Optional[requests.Response] = None
        try:
            response = self._get_session().get(url, timeout=self.timeout, stream=True)
            if response.status_code == 404:
                # The normal "no observer reported this packet" answer.
                self._debug(f"CoreScope: no data for {packet_hash}")
                return None
            if response.status_code != 200:
                self._debug(f"CoreScope: HTTP {response.status_code} for {packet_hash}")
                return None

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                self._debug(
                    f"CoreScope: refusing {declared} byte response for {packet_hash}"
                )
                return None

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    self._debug(
                        f"CoreScope: response for {packet_hash} exceeded "
                        f"{MAX_RESPONSE_BYTES} bytes, abandoned"
                    )
                    return None
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self._debug(f"CoreScope: {type(e).__name__} fetching {packet_hash}")
            return None
        except requests.exceptions.RequestException as e:
            self._debug(f"CoreScope: request failed for {packet_hash}: {e}")
            return None
        finally:
            if response is not None:
                response.close()

    def _parse(self, body: str) -> Optional[ScopePacket]:
        """Count unique paths in a response body. ``None`` if it is not usable."""
        try:
            data = json.loads(body)
        except ValueError:
            self._debug("CoreScope: response was not JSON")
            return None
        if not isinstance(data, dict):
            return None

        # The payload nests under "packet"; tolerate a flat one too, as the map's
        # own deep-link loader does.
        packet = data.get("packet")
        if not isinstance(packet, dict):
            packet = data
        observations = packet.get("observations")
        if not isinstance(observations, list) or not observations:
            self._debug("CoreScope: response carried no observations")
            return None

        paths: set[str] = set()
        observers: set[str] = set()
        counted = 0
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            counted += 1
            observer = observation.get("observer_id") or observation.get("observer_name")
            if observer:
                observers.add(str(observer))
            path = self._path_of(observation)
            if path:
                paths.add(path)

        if not counted:
            return None

        return ScopePacket(
            unique_paths=len(paths),
            # Counted here rather than read from ``observation_count``: the paths
            # and the reception count should describe the same list.
            observations=counted,
            observers=len(observers),
            max_hops=max((p.count(",") + 1 for p in paths), default=0),
            paths=tuple(sorted(paths)),
        )

    def _path_of(self, observation: dict[str, Any]) -> str:
        """``path_json`` as comma-joined lowercase hops, or ``""``.

        A malformed entry is skipped rather than failing the whole lookup -- one
        bad observation should not cost the other forty.
        """
        raw = observation.get("path_json")
        if isinstance(raw, list):
            hops_any: list[Any] = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw or "[]")
            except ValueError:
                self._debug(f"CoreScope: unparseable path_json {raw!r}, skipped")
                return ""
            if not isinstance(parsed, list):
                return ""
            hops_any = parsed
        else:
            return ""

        hops = [str(h).strip().lower() for h in hops_any if str(h).strip()]
        return ",".join(hops)
