#!/usr/bin/env python3
"""
World Cup live-score service for the MeshCore Bot.

Polls the active FIFA World Cup scoreboard and posts proactive updates to a channel as
matches progress: kick-off, each goal (score change), half-time, and full-time. Example:

    Group E: Côte d'Ivoire 0, Ecuador 0 (half-time)

Only runs while a tournament is in progress (auto-detected from the ESPN calendar via
WorldCupData); otherwise it idles. Per-match state is persisted in bot metadata so a
restart does not replay events. On first sight of a match, its state is seeded silently
so matches already underway at startup are not back-announced.
"""

import asyncio
import contextlib
import json
from typing import Any, Optional

from ..clients.espn_client import ESPNClient
from ..clients.worldcup_data import WorldCupData
from ..clients.worldcup_fastcast import WorldCupFastcastClient
from ..utils import espn_dates_for_local_day, filter_events_local_day, get_config_timezone
from .base_service import BaseServicePlugin

LIVE_STATUSES = {"STATUS_IN_PROGRESS", "STATUS_FIRST_HALF", "STATUS_SECOND_HALF", "STATUS_END_PERIOD"}
FINAL_STATUSES = {"STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_FINAL_PEN"}
HT_STATUS = "STATUS_HALFTIME"
SCHEDULED_STATUS = "STATUS_SCHEDULED"
PLAYING_STATUSES = LIVE_STATUSES | {HT_STATUS}
# Match-stoppage statuses and how to word them.
STOPPAGE_WORDS = {
    "STATUS_POSTPONED": "postponed",
    "STATUS_SUSPENDED": "suspended",
    "STATUS_ABANDONED": "abandoned",
    "STATUS_CANCELED": "cancelled",
    "STATUS_CANCELLED": "cancelled",
}
METADATA_KEY = "worldcup_live_state"


class WorldCupLiveService(BaseServicePlugin):
    """Polls the World Cup scoreboard and posts live score updates to a channel."""

    config_section = "Worldcup_Service"
    description = "Live FIFA World Cup score updates posted to a channel"
    name = "worldcup"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        section = self.config_section

        self.channel = bot.config.get(section, "channel", fallback="general")
        # Cadence while a tournament is active but no match is in progress (waiting for
        # the next kick-off). Milliseconds.
        poll_ms = bot.config.getint(section, "poll_interval", fallback=60000)
        self.poll_interval_seconds = poll_ms / 1000.0
        # Faster cadence used while at least one match is live, for timelier goal/HT/FT
        # updates. Milliseconds. ESPN's scoreboard is edge-cached ~15-20s, so polling
        # faster than that yields no fresher data.
        live_ms = bot.config.getint(section, "live_poll_interval", fallback=20000)
        self.live_poll_interval_seconds = live_ms / 1000.0
        # Cadence when no tournament is in progress (off-season). Seconds.
        self.idle_interval_seconds = bot.config.getint(section, "idle_interval", fallback=1800)
        timeout = bot.config.getint(section, "api_timeout", fallback=10)

        def _flag(key: str, default: bool) -> bool:
            return bot.config.getboolean(section, key, fallback=default)

        self.announce_kickoff = _flag("announce_kickoff", True)
        self.announce_goals = _flag("announce_goals", True)
        self.announce_disallowed = _flag("announce_disallowed", True)
        self.announce_red_cards = _flag("announce_red_cards", True)
        self.announce_yellow_cards = _flag("announce_yellow_cards", False)
        self.announce_stoppage = _flag("announce_stoppage", True)
        self.announce_halftime = _flag("announce_halftime", True)
        self.announce_fulltime = _flag("announce_fulltime", True)
        self.silence_mesh_output = _flag("silence_mesh_output", False)

        # Experimental ESPN fastcast WebSocket push (off by default). When on, pushes wake
        # the loop for near-real-time ticks; REST polling continues as the heartbeat/source
        # of truth. fastcast_topic overrides the (reverse-engineered) default topic.
        self.use_fastcast = _flag("use_fastcast", False)
        self.fastcast_topic = bot.config.get(section, "fastcast_topic", fallback="").strip()

        self.espn_client = ESPNClient(logger=self.logger, timeout=timeout)
        self.wc_data = WorldCupData(self.espn_client, logger=self.logger)

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._fastcast: Optional[WorldCupFastcastClient] = None
        self._fastcast_task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._state: dict[str, dict] = self._load_state()

        self.logger.info(
            "World Cup live service initialized: channel=%s, poll=%.0fs, triggers=%s",
            self.channel,
            self.poll_interval_seconds,
            ",".join(
                t for t, on in (
                    ("kickoff", self.announce_kickoff), ("goals", self.announce_goals),
                    ("ht", self.announce_halftime), ("ft", self.announce_fulltime),
                ) if on
            ),
        )

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("World Cup live service is disabled, not starting")
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        if self.use_fastcast:
            self._start_fastcast()
        self.logger.info("World Cup live service started (fastcast=%s)", self.use_fastcast)

    async def stop(self) -> None:
        self._running = False
        if self._fastcast:
            self._fastcast.stop()
        await self._cancel_tasks(self._fastcast_task, self._poll_task)
        self._fastcast_task = None
        self._poll_task = None
        self.logger.info("World Cup live service stopped")

    def _start_fastcast(self) -> None:
        topic = self.fastcast_topic or "gp-soccer-fifa.world"
        self._fastcast = WorldCupFastcastClient(topic, self._on_push, logger=self.logger)
        self._fastcast_task = asyncio.create_task(self._fastcast.run())
        self.logger.info("Fastcast enabled (topic=%s)", topic)

    def _on_push(self) -> None:
        """Fastcast change signal: wake the poll loop to tick immediately."""
        self._wake.set()

    # --------------------------------------------------------------- state I/O

    def _load_state(self) -> dict[str, dict]:
        if not getattr(self.bot, "db_manager", None):
            return {}
        raw = self.bot.db_manager.get_metadata(METADATA_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _save_state(self) -> None:
        if getattr(self.bot, "db_manager", None):
            try:
                self.bot.db_manager.set_metadata(METADATA_KEY, json.dumps(self._state))
            except Exception as e:
                self.logger.error("Failed to persist World Cup live state: %s", e)

    # ------------------------------------------------------------- poll + tick

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                delay = await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in World Cup live poll loop: %s", e)
                delay = 60.0
            try:
                if self.use_fastcast:
                    # Wake early on a fastcast push; otherwise tick on the heartbeat delay.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    self._wake.clear()
                else:
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> float:
        """Run one poll cycle; return how many seconds to sleep before the next."""
        active = await self.wc_data.get_active_tournament()
        if not active:
            return self.idle_interval_seconds

        league = active["league"]
        in_group = active.get("in_group_stage", False)
        stage_label = active.get("stage_label", "")

        local_tz, _ = get_config_timezone(self.bot.config, self.logger)
        start_date, end_date, local_start_ts, local_end_ts = espn_dates_for_local_day(local_tz)
        matches = await self.espn_client.fetch_match_states(
            "soccer", league,
            cache_bust=self.use_fastcast,
            start_date=start_date,
            end_date=end_date,
        )
        matches = filter_events_local_day(matches, local_start_ts, local_end_ts)
        if not matches:
            return self.poll_interval_seconds

        groups = await self.wc_data.get_team_groups(league) if in_group else {}

        messages: list[str] = []
        changed = False
        for m in matches:
            eid = m["id"]
            # "g" is the count of goals that are both scored AND have a named scoring play —
            # min(scoring plays, score total). This ignores transient states where a play is
            # listed before the score updates (or a goal pending VAR), which otherwise cause a
            # phantom "0-0 (17' Scorer)" announcement.
            committed = min(len(m.get("goals") or []), m["home_score"] + m["away_score"])
            prev = self._state.get(eid)
            cur = {"h": m["home_score"], "a": m["away_score"], "s": m["status"], "g": committed,
                   # red/yellow card counts so far in the match
                   "rc": len(m.get("cards") or []),
                   "yc": len(m.get("yellows") or []),
                   # carry the last announced goal forward so a later VAR reversal can name it
                   "lg": prev.get("lg") if prev else None}
            if prev is None:
                # First sight: seed silently so in-progress matches aren't back-announced.
                self._state[eid] = cur
                changed = True
                continue
            msg = self._detect(prev, cur, m, in_group, groups, stage_label)
            if msg:
                messages.append(msg)
            if self.announce_red_cards:
                rc = self._detect_card(prev, cur, m, in_group, groups, stage_label,
                                       count_key="rc", list_key="cards", word="red card")
                if rc:
                    messages.append(rc)
            if self.announce_yellow_cards:
                yc = self._detect_card(prev, cur, m, in_group, groups, stage_label,
                                       count_key="yc", list_key="yellows", word="yellow card")
                if yc:
                    messages.append(yc)
            if cur != prev:
                self._state[eid] = cur
                changed = True

        for text in messages:
            await self._post(text)
        if changed:
            self._save_state()

        # Poll faster while any match is live so goals/HT/FT land promptly; otherwise use
        # the normal cadence (which still catches the next kick-off within poll_interval).
        any_live = any(m["status"] in PLAYING_STATUSES for m in matches)
        return self.live_poll_interval_seconds if any_live else self.poll_interval_seconds

    # ----------------------------------------------------------- detection/fmt

    def _detect(self, prev: dict, cur: dict, m: dict, in_group: bool, groups: dict, stage_label: str) -> Optional[str]:
        """Return a message for the most significant transition this poll, or None."""
        ps, cs = prev["s"], cur["s"]
        label = self._label(m, in_group, groups, stage_label)

        if self.announce_kickoff and ps == SCHEDULED_STATUS and cs in PLAYING_STATUSES:
            return f"{label}: {m['home_name']} vs {m['away_name']} (kick-off)"
        if self.announce_stoppage and cs in STOPPAGE_WORDS and ps not in STOPPAGE_WORDS:
            return self._score_line(label, m, cur, STOPPAGE_WORDS[cs])
        if self.announce_fulltime and cs in FINAL_STATUSES and ps not in FINAL_STATUSES:
            return self._score_line(label, m, cur, "full-time")
        if self.announce_halftime and cs == HT_STATUS and ps != HT_STATUS:
            return self._score_line(label, m, cur, "half-time")
        # A goal is announced only when it is backed by BOTH the score and a named scoring
        # play: cur["g"] = min(scoring plays, score total). Announcing the slice
        # goals[prev_g:cur_g] guarantees the scorer is known and the score reflects it,
        # which avoids phantom announcements when a play is listed before the score updates
        # or while a goal is under VAR review. A *decrease* in that committed count means a
        # previously-announced goal was rescinded (VAR) — a separate notification path.
        if self.announce_goals and cs in PLAYING_STATUSES:
            goals = m.get("goals") or []
            # prev_g is absent for state saved before this feature; fall back to the prior
            # score total as the baseline so old goals are not replayed.
            prev_g = prev.get("g")
            if prev_g is None:
                prev_g = prev["h"] + prev["a"]

            if cur["g"] > prev_g:
                new_goals = goals[prev_g:cur["g"]]
                cur["lg"] = self._fmt_goal(new_goals[-1])  # remember in case it's later overturned
                return self._score_line(label, m, cur, "; ".join(self._fmt_goal(g) for g in new_goals))

            # Disallowed goal: the score itself went down. A falling score only happens via a
            # VAR reversal/correction, and keying off the score (not the play count) avoids a
            # false positive when a match simply has no scoring-play details.
            if self.announce_disallowed and (cur["h"] + cur["a"]) < (prev["h"] + prev["a"]):
                ruled_out = prev.get("lg")
                detail = f"{ruled_out} ruled out (VAR)" if ruled_out else "goal ruled out (VAR)"
                cur["lg"] = None
                return f"{label}: {m['home_name']} {cur['h']}, {m['away_name']} {cur['a']} — {detail}"
        return None

    @staticmethod
    def _fmt_goal(goal: dict) -> str:
        """Format one scoring play, e.g. \"64' Mohammad Mohebbi\", \"7' Elijah Just, pen\",
        or \"30' Cody Gakpo, header\"."""
        clock = (goal.get("clock") or "").strip()
        name = (goal.get("scorer") or "").strip()
        base = f"{clock} {name}".strip() if name else (clock or "goal")
        if goal.get("own_goal"):
            base += ", OG"
        elif goal.get("penalty"):
            base += ", pen"
        elif goal.get("kind"):
            base += f", {goal['kind']}"
        return base

    def _score_line(self, label: str, m: dict, cur: dict, tag: Optional[str]) -> str:
        line = f"{label}: {m['home_name']} {cur['h']}, {m['away_name']} {cur['a']}"
        if tag == "full-time" and m.get("status") == "STATUS_FINAL_PEN" and m.get("home_pen") is not None:
            return f"{line} (full-time, pens {m['home_pen']}-{m['away_pen']})"
        if tag:
            line += f" ({tag})"
        return line

    def _detect_card(self, prev: dict, cur: dict, m: dict, in_group: bool, groups: dict, stage_label: str,
                     *, count_key: str, list_key: str, word: str) -> Optional[str]:
        """Announce newly-shown card(s) of one kind (red or yellow). Runs alongside _detect
        (a card can occur in the same poll as a goal), keyed off the card count growing.
        The caller gates on the relevant announce_* flag."""
        if cur["s"] not in PLAYING_STATUSES:
            return None
        prev_c = prev.get(count_key)
        if prev_c is None:
            # State saved before this feature: re-baseline so existing cards aren't replayed.
            prev_c = cur[count_key]
        if cur[count_key] <= prev_c:
            return None
        new_cards = (m.get(list_key) or [])[prev_c:cur[count_key]]
        label = self._label(m, in_group, groups, stage_label)
        details = "; ".join(self._fmt_card(c, m, word) for c in new_cards)
        return f"{label}: {m['home_name']} {cur['h']}, {m['away_name']} {cur['a']} — {details}"

    @staticmethod
    def _fmt_card(card: dict, m: dict, word: str) -> str:
        """Format a card, e.g. \"red card: Tarik Muharemovic (Switzerland, 80')\"."""
        player = (card.get("player") or "").strip() or "a player"
        clock = (card.get("clock") or "").strip()
        team = m["home_name"] if card.get("team_id") == m.get("home_id") else m["away_name"]
        inside = ", ".join(p for p in (team, clock) if p)
        return f"{word}: {player} ({inside})" if inside else f"{word}: {player}"

    @staticmethod
    def _label(m: dict, in_group: bool, groups: dict, stage_label: str) -> str:
        if in_group:
            group = groups.get(m["home_id"]) or groups.get(m["away_id"])
            if group:
                return group
        return stage_label or "World Cup"

    async def _post(self, text: str) -> None:
        try:
            if not self.silence_mesh_output:
                await self.bot.command_manager.send_channel_message(
                    self.channel, text, scope=self.get_mesh_flood_scope()
                )
            if self.has_external_notification_targets():
                await self.send_external_notifications(text)
            self.logger.info("World Cup live update: %s", text)
        except Exception as e:
            self.logger.error("Error posting World Cup live update: %s", e)
