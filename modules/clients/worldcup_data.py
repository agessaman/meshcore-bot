#!/usr/bin/env python3
"""
Shared FIFA World Cup data helper.

Wraps an ESPNClient to provide:
  - in-season detection (men's fifa.world / women's fifa.wwc) via the tournament calendar
  - nation name -> ESPN team_id resolution built from live standings/scoreboard data

Both the dedicated `wc`/`worldcup` command and the `sports` command's nation fallback
use this helper. Results are cached in-memory with a TTL so repeated mesh queries do not
re-hit ESPN. Today's live scores are intentionally NOT cached here (callers fetch those
fresh), so the cache TTL only covers slow-changing data (tournament window, team roster).
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .sports_mappings import WORLD_CUP_NATIONS

MENS_LEAGUE = "fifa.world"
WOMENS_LEAGUE = "fifa.wwc"


def _parse_iso(date_str: str) -> Optional[datetime]:
    """Parse an ESPN ISO date like '2026-06-11T04:00Z' into an aware datetime."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class WorldCupData:
    """Caches World Cup season state and nation lookups around an ESPNClient."""

    def __init__(self, espn_client, logger: Optional[logging.Logger] = None, cache_ttl: int = 21600):
        """Initialize the helper.

        Args:
            espn_client: An ESPNClient instance used for all network calls.
            logger: Logger for diagnostics. Defaults to the module logger.
            cache_ttl: Seconds to cache season/roster data (default 6 hours).
        """
        self.espn = espn_client
        self.logger = logger or logging.getLogger(__name__)
        self.cache_ttl = cache_ttl
        # (fetched_at, result-or-None) where result = {'league', 'label'}
        self._active_cache: Optional[tuple[float, Optional[dict]]] = None
        # league -> (fetched_at, {variant_lower: {'id', 'abbr', 'name'}})
        self._nation_index: dict[str, tuple[float, dict]] = {}
        # league -> (fetched_at, {team_id: group_name})
        self._team_group_cache: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def _calendar_window(calendar: list[dict]) -> Optional[tuple[datetime, datetime]]:
        """Return (start, end) spanning all calendar stages, or None."""
        starts, ends = [], []
        for entry in calendar:
            s = _parse_iso(entry.get("startDate", ""))
            e = _parse_iso(entry.get("endDate", ""))
            if s:
                starts.append(s)
            if e:
                ends.append(e)
        if not starts or not ends:
            return None
        return min(starts), max(ends)

    @staticmethod
    def _group_stage_end(calendar: list[dict]) -> Optional[datetime]:
        """Return the end of the group stage from the calendar (label contains 'group')."""
        ends = []
        for entry in calendar:
            if "group" in str(entry.get("label", "")).lower():
                e = _parse_iso(entry.get("endDate", ""))
                if e:
                    ends.append(e)
        return max(ends) if ends else None

    @staticmethod
    def _current_stage_label(calendar: list[dict], now_dt: datetime) -> str:
        """Return the calendar stage label whose window contains now (e.g. 'Round of 32')."""
        for entry in calendar:
            s = _parse_iso(entry.get("startDate", ""))
            e = _parse_iso(entry.get("endDate", ""))
            if s and e and s <= now_dt <= e:
                return str(entry.get("label", ""))
        return ""

    async def get_active_tournament(self) -> Optional[dict]:
        """Return tournament state for the in-progress World Cup, or None. The dict has
        'league', 'label', 'start_date'/'end_date' ('YYYYMMDD' strings spanning the whole
        tournament, used to fetch a nation's fixture list) and 'in_group_stage' (bool).

        Probes men's then women's; the two never overlap so the first whose calendar
        window contains *now* wins. Cached for cache_ttl seconds.
        """
        now = time.time()
        if self._active_cache and (now - self._active_cache[0]) < self.cache_ttl:
            return self._active_cache[1]

        result: Optional[dict] = None
        now_dt = datetime.now(timezone.utc)
        for league in (MENS_LEAGUE, WOMENS_LEAGUE):
            data = await self.espn.fetch_scoreboard_with_calendar("soccer", league)
            if not data:
                continue
            calendar = data.get("calendar", [])
            window = self._calendar_window(calendar)
            if window and window[0] <= now_dt <= window[1]:
                group_end = self._group_stage_end(calendar)
                result = {
                    "league": league,
                    "label": data.get("league_name") or "World Cup",
                    "start_date": window[0].strftime("%Y%m%d"),
                    "end_date": window[1].strftime("%Y%m%d"),
                    "in_group_stage": bool(group_end and now_dt <= group_end),
                    "stage_label": self._current_stage_label(calendar, now_dt),
                }
                break

        self._active_cache = (now, result)
        return result

    async def _get_nation_index(self, league: str) -> dict:
        """Build/return a cached {name_variant_lower: team_info} index for a league."""
        cached = self._nation_index.get(league)
        if cached and (time.time() - cached[0]) < self.cache_ttl:
            return cached[1]

        index: dict[str, dict] = {}

        def _add(team_id: str, abbr: str, *names: str) -> None:
            info = {"id": str(team_id), "abbr": abbr or "", "name": names[0] if names else ""}
            for n in names:
                if n:
                    index.setdefault(n.strip().lower(), info)
            if abbr:
                index.setdefault(abbr.strip().lower(), info)

        # Preferred source: standings (covers every team in the tournament)
        groups = await self.espn.fetch_standings("soccer", league)
        for group in groups:
            for entry in group.get("entries", []):
                _add(
                    entry.get("id", ""),
                    entry.get("abbr", ""),
                    entry.get("name", ""),
                    entry.get("location", ""),
                )

        # Standings entries lack team id in our parsed shape; enrich from scoreboard
        # competitors which carry id + name + abbreviation + location.
        data = await self.espn.fetch_scoreboard_with_calendar("soccer", league)
        if data:
            for team in data.get("competitors", []):
                _add(
                    team.get("id", ""),
                    team.get("abbreviation", ""),
                    team.get("displayName", ""),
                    team.get("name", ""),
                    team.get("location", ""),
                    team.get("shortDisplayName", ""),
                )

        self._nation_index[league] = (time.time(), index)
        return index

    async def get_team_groups(self, league: str) -> dict[str, str]:
        """Return a cached {team_id: group_name} map from standings (e.g. '481' -> 'Group C')."""
        cached = self._team_group_cache.get(league)
        if cached and (time.time() - cached[0]) < self.cache_ttl:
            return cached[1]

        mapping: dict[str, str] = {}
        for group in await self.espn.fetch_standings("soccer", league):
            group_name = group.get("group_name", "")
            for entry in group.get("entries", []):
                team_id = str(entry.get("id", ""))
                if team_id and group_name:
                    mapping[team_id] = group_name

        self._team_group_cache[league] = (time.time(), mapping)
        return mapping

    async def resolve_nation(self, name: str, league: str) -> Optional[dict]:
        """Resolve a nation name to {'sport','league','team_id','abbr','name'}, or None."""
        if not name:
            return None
        key = name.strip().lower()
        # Apply synonym map first (e.g. 'usa' -> 'United States')
        canonical = WORLD_CUP_NATIONS.get(key)
        index = await self._get_nation_index(league)

        info = None
        if canonical:
            info = index.get(canonical.strip().lower())
        if info is None:
            info = index.get(key)
        if info is None or not info.get("id"):
            return None

        return {
            "sport": "soccer",
            "league": league,
            "team_id": info["id"],
            "abbr": info.get("abbr", ""),
            "name": info.get("name", ""),
        }
