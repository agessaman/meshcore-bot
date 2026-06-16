#!/usr/bin/env python3
"""
World Cup command for the MeshCore Bot.

Provides FIFA World Cup coverage (men's fifa.world / women's fifa.wwc) via the ESPN API,
but only while a tournament is actually in progress (determined from the ESPN tournament
calendar). Supports:

  wc / worldcup          -> today's scores, including live results
  wc group <X>           -> standings for group X
  wc <nation>            -> that nation's matches in the current tournament

World Cup data is also reachable year-round through the `sports` command
(e.g. `sports fifa`, `sports brazil`); this command is the season-gated, purpose-built
front end with live scores, standings, and nation lookups.
"""

from typing import TYPE_CHECKING

from ..clients.espn_client import ESPNClient
from ..clients.sports_mappings import SPORT_EMOJIS
from ..clients.worldcup_data import WorldCupData
from ..models import MeshMessage
from .base_command import BaseCommand

if TYPE_CHECKING:
    from ..core import MeshCoreBot

SOCCER_EMOJI = SPORT_EMOJIS.get("soccer", "⚽")
FINAL_STATUSES = {"STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_FINAL_PEN", "STATUS_POSTPONED"}


class WorldCupCommand(BaseCommand):
    """Season-gated FIFA World Cup scores, standings, and nation lookups."""

    # Plugin metadata
    name = "worldcup"
    keywords = ["wc", "worldcup"]
    description = "FIFA World Cup scores, standings, and nation results (in-season only)"
    category = "sports"
    cooldown_seconds = 3
    requires_internet = True

    # Documentation
    short_description = "FIFA World Cup scores (in-season)"
    usage = "wc [group <X> | <nation>]"
    examples = ["wc", "wc group a", "wc brazil"]
    parameters = [
        {"name": "group <X>", "description": "Standings for a group (e.g. group a)"},
        {"name": "nation", "description": "A nation's matches (e.g. brazil, usa)"},
    ]

    def __init__(self, bot: "MeshCoreBot"):
        """Initialize the World Cup command with an ESPN client and data helper."""
        super().__init__(bot)
        self.url_timeout = self.get_config_value("Worldcup_Command", "api_timeout", fallback=10, value_type="int")

        # Standard enabled flag
        self.worldcup_enabled = self.get_config_value("Worldcup_Command", "enabled", fallback=True, value_type="bool")

        cache_ttl_minutes = self.get_config_value("Worldcup_Command", "cache_ttl_minutes", fallback=360, value_type="int")

        self.espn_client: ESPNClient = ESPNClient(logger=self.logger, timeout=self.url_timeout)
        self.wc_data = WorldCupData(self.espn_client, logger=self.logger, cache_ttl=cache_ttl_minutes * 60)

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Match only when a keyword is the first word (mirrors SportsCommand)."""
        if not self.keywords:
            return False
        content_lower = self.cleanup_message_for_matching(message)
        words = content_lower.split()
        if not words:
            return False
        return any(words[0] == keyword.lower() for keyword in self.keywords)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Gate on the enabled flag; season gating happens in execute() (needs network)."""
        if not self.worldcup_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return self.translate("commands.worldcup.help")

    # ------------------------------------------------------------------ helpers

    def _pack_lines(self, lines: list[str], max_len: int, max_chunks: int = 3) -> list[str]:
        """Greedily pack lines into up to max_chunks messages within max_len UTF-8 bytes."""
        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate.encode("utf-8")) <= max_len:
                current = candidate
                continue
            # Line doesn't fit in current chunk; flush and start a new one
            if current:
                chunks.append(current)
                if len(chunks) >= max_chunks:
                    return chunks
                current = ""
            if len(line.encode("utf-8")) <= max_len:
                current = line
            else:
                # Single oversized line: hard-truncate on a byte boundary
                truncated = line.encode("utf-8")[:max_len].decode("utf-8", "ignore")
                chunks.append(truncated)
                if len(chunks) >= max_chunks:
                    return chunks
        if current and len(chunks) < max_chunks:
            chunks.append(current)
        return chunks[:max_chunks]

    @staticmethod
    def _today_sort_key(game: dict) -> tuple[int, float]:
        """Order today's games: live first, then completed results, then upcoming."""
        status = game.get("status", "")
        ts = game.get("event_timestamp") or 0
        if game.get("timestamp", 0) < 0:
            return (0, ts)  # live / halftime
        if status in ("STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_FINAL_PEN", "STATUS_POSTPONED"):
            return (1, ts)  # completed results
        return (2, ts)  # scheduled later

    @staticmethod
    def _dedupe_games(games: list[dict]) -> list[dict]:
        """Drop duplicate events (the team-schedule endpoint can repeat today's game)."""
        seen = set()
        unique = []
        for game in games:
            key = game.get("id") or game.get("formatted")
            if key in seen:
                continue
            seen.add(key)
            unique.append(game)
        return unique

    @staticmethod
    def _select_nation_games(games: list[dict], in_group_stage: bool) -> list[dict]:
        """Pick which of a nation's matches to show so the reply fits one message.

        During the group stage, show all of the nation's (known) group matches. Afterward,
        show just the last result (or a live game) plus the next scheduled fixture.
        """
        if in_group_stage:
            return games

        live = [g for g in games if g.get("timestamp", 0) < 0]
        completed = sorted(
            [g for g in games if g.get("status") in FINAL_STATUSES],
            key=lambda g: g.get("event_timestamp") or 0,
            reverse=True,
        )
        upcoming = sorted(
            [g for g in games if g.get("status") == "STATUS_SCHEDULED"],
            key=lambda g: g.get("event_timestamp") or 0,
        )
        selected: list[dict] = []
        if live:
            selected.extend(live)
        elif completed:
            selected.append(completed[0])
        if upcoming:
            selected.append(upcoming[0])
        return selected

    def _format_games(self, games: list[dict]) -> list[str]:
        """Format parsed game dicts into '⚽ ...' lines (emoji de-duplicated)."""
        lines = []
        for game in games:
            formatted = game.get("formatted", "").strip()
            if formatted and formatted[0] in SPORT_EMOJIS.values():
                formatted = formatted[1:].strip()
            lines.append(f"{SOCCER_EMOJI} {formatted}")
        return lines

    async def _refresh_live(self, games: list[dict], league: str) -> list[dict]:
        """Refresh any live games with up-to-date scoreboard data (for stale schedules)."""
        for i, game in enumerate(games):
            if game.get("timestamp", 0) < 0 and game.get("id"):
                try:
                    live = await self.espn_client.fetch_live_event_data(game["id"], "soccer", league)
                    if live:
                        updated = self.espn_client.parse_game_event_with_timestamp(live, "", "soccer", league)
                        if updated:
                            games[i] = updated
                except Exception as e:
                    self.logger.warning(f"World Cup live refresh failed for {game.get('id')}: {e}")
        return games

    # ----------------------------------------------------------------- handlers

    async def _handle_today(self, message: MeshMessage, league: str) -> bool:
        """Send today's scores (live first), chunked across up to 3 messages."""
        # The scoreboard endpoint already carries live scores, so no refresh is needed.
        data = await self.espn_client.fetch_scoreboard_with_calendar("soccer", league)
        games = data.get("events", []) if data else []
        if not games:
            return await self.send_response(message, self.translate("commands.worldcup.no_games_today"))

        games.sort(key=self._today_sort_key)
        lines = self._format_games(games)
        chunks = self._pack_lines(lines, self.get_max_message_length(message), max_chunks=3)
        return await self.send_response_chunked(message, chunks)

    async def _handle_group(self, message: MeshMessage, league: str, letter: str) -> bool:
        """Send standings for a single group, or list groups if no letter given."""
        groups = await self.espn_client.fetch_standings("soccer", league)
        if not groups:
            return await self.send_response(message, self.translate("commands.worldcup.no_games_today"))

        if not letter:
            available = ", ".join(
                g["group_name"].replace("Group ", "").strip() for g in groups if g.get("group_name")
            )
            return await self.send_response(message, self.translate("commands.worldcup.groups_list", groups=available))

        letter = letter.strip().lower()
        match = next(
            (g for g in groups if g.get("group_name", "").strip().lower().endswith(f" {letter}")),
            None,
        )
        if not match:
            return await self.send_response(message, self.translate("commands.worldcup.group_not_found", group=letter.upper()))

        lines = [f"{SOCCER_EMOJI} {match['group_name']}"]
        for e in match["entries"]:
            lines.append(f"{e['rank']}.{e['abbr']} {e['pts']}p {e['gd']:+d} ({e['w']}-{e['d']}-{e['l']})")
        chunks = self._pack_lines(lines, self.get_max_message_length(message), max_chunks=2)
        return await self.send_response_chunked(message, chunks)

    async def _handle_nation(self, message: MeshMessage, active: dict, name: str) -> bool:
        """Send a nation's matches in the current tournament (results + upcoming fixtures)."""
        league = active["league"]
        team_info = await self.wc_data.resolve_nation(name, league)
        if not team_info:
            return await self.send_response(message, self.translate("commands.worldcup.nation_not_found", nation=name))

        # Prefer the dated scoreboard range: it includes known future fixtures, which the
        # team /schedule endpoint omits for tournament national teams. Fall back to the
        # schedule endpoint if the tournament window is unavailable.
        start, end = active.get("start_date"), active.get("end_date")
        if start and end:
            games = await self.espn_client.fetch_team_fixtures("soccer", league, team_info["team_id"], start, end)
        else:
            games = await self.espn_client.fetch_team_schedule("soccer", league, team_info["team_id"])

        if not games:
            return await self.send_response(message, self.translate("commands.worldcup.no_games_nation", nation=name))

        games = self._dedupe_games(games)
        games = await self._refresh_live(games, league)
        games = self._select_nation_games(games, active.get("in_group_stage", False))
        games.sort(key=self._today_sort_key)
        lines = self._format_games(games)
        chunks = self._pack_lines(lines, self.get_max_message_length(message), max_chunks=1)
        return await self.send_response_chunked(message, chunks)

    async def execute(self, message: MeshMessage) -> bool:
        """Main entry point: gate on season, then dispatch on subcommand."""
        try:
            self.record_execution(message.sender_id)

            content = message.content.strip()
            if content.startswith("!"):
                content = content[1:].strip()

            # Drop the leading keyword, keep the remainder as the argument string
            parts = content.split(" ", 1)
            args = parts[1].strip() if len(parts) > 1 else ""

            active = await self.wc_data.get_active_tournament()
            if not active:
                return await self.send_response(message, self.translate("commands.worldcup.not_in_season"))

            league = active["league"]

            if not args:
                return await self._handle_today(message, league)

            lowered = args.lower()
            if lowered == "group" or lowered == "groups":
                return await self._handle_group(message, league, "")
            if lowered.startswith("group "):
                return await self._handle_group(message, league, args[6:].strip())

            return await self._handle_nation(message, active, args)

        except Exception as e:
            self.logger.error(f"Error in worldcup execute: {e}")
            return await self.send_response(message, self.translate("commands.worldcup.error_fetching"))
