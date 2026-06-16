"""Tests for the World Cup live-score service (detection + tick flow)."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.service_plugins.worldcup_service import WorldCupLiveService


def _make_bot(**overrides):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Worldcup_Service")
    config.set("Worldcup_Service", "enabled", "true")
    config.set("Worldcup_Service", "channel", "#fifa")
    for k, v in overrides.items():
        config.set("Worldcup_Service", k, v)
    bot.config = config
    # In-memory metadata store
    store = {}
    bot.db_manager = MagicMock()
    bot.db_manager.get_metadata = Mock(side_effect=lambda k: store.get(k))
    bot.db_manager.set_metadata = Mock(side_effect=lambda k, v: store.update({k: v}))
    bot.command_manager = MagicMock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    return bot


def _svc(**overrides):
    svc = WorldCupLiveService(_make_bot(**overrides))
    # Avoid external-notification config lookups in _post
    svc.has_external_notification_targets = lambda: False
    svc.get_mesh_flood_scope = lambda: None
    return svc


def _match(eid="1", h=0, a=0, status="STATUS_SCHEDULED", clock="", hp=None, ap=None,
           hid="100", aid="200", hn="Côte d'Ivoire", an="Ecuador"):
    return {
        "id": eid, "home_id": hid, "away_id": aid, "home_name": hn, "away_name": an,
        "home_score": h, "away_score": a, "status": status, "clock": clock,
        "home_pen": hp, "away_pen": ap,
    }


GROUPS = {"100": "Group E", "200": "Group E"}


class TestDetect:
    def _detect(self, svc, prev, m, in_group=True):
        cur = {"h": m["home_score"], "a": m["away_score"], "s": m["status"]}
        return svc._detect(prev, cur, m, in_group, GROUPS, "Round of 32")

    def test_kickoff(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SCHEDULED"}
        m = _match(status="STATUS_FIRST_HALF")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire vs Ecuador (kick-off)"

    def test_goal_with_minute(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="23'")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 0 (23')"

    def test_halftime(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=0, a=0, status="STATUS_HALFTIME")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 0, Ecuador 0 (half-time)"

    def test_fulltime(self):
        svc = _svc()
        prev = {"h": 1, "a": 1, "s": "STATUS_SECOND_HALF"}
        m = _match(h=1, a=2, status="STATUS_FULL_TIME")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 2 (full-time)"

    def test_fulltime_penalties(self):
        svc = _svc()
        prev = {"h": 2, "a": 2, "s": "STATUS_SECOND_HALF"}
        m = _match(h=2, a=2, status="STATUS_FINAL_PEN", hp=3, ap=4)
        assert self._detect(svc, prev, m, in_group=False) == \
            "Round of 32: Côte d'Ivoire 2, Ecuador 2 (full-time, pens 3-4)"

    def test_no_change_returns_none(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF")
        assert self._detect(svc, prev, m) is None

    def test_triggers_can_be_disabled(self):
        svc = _svc(announce_goals="false")
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="23'")
        assert self._detect(svc, prev, m) is None

    def test_knockout_uses_stage_label(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SCHEDULED"}
        m = _match(status="STATUS_FIRST_HALF")
        # in_group False -> stage label instead of group
        cur = {"h": 0, "a": 0, "s": m["status"]}
        msg = svc._detect(prev, cur, m, False, GROUPS, "Round of 32")
        assert msg.startswith("Round of 32: ")


class TestTick:
    async def test_seeds_silently_then_announces(self):
        svc = _svc()
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)

        # First poll: match already in progress -> seeded, no message
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=0, a=0, status="STATUS_FIRST_HALF")])
        await svc._tick()
        svc.bot.command_manager.send_channel_message.assert_not_awaited()

        # Second poll: a goal -> announced
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=1, a=0, status="STATUS_FIRST_HALF", clock="12'")])
        await svc._tick()
        svc.bot.command_manager.send_channel_message.assert_awaited_once()
        args = svc.bot.command_manager.send_channel_message.await_args.args
        assert args[0] == "#fifa"
        assert args[1] == "Group E: Côte d'Ivoire 1, Ecuador 0 (12')"

    async def test_polls_faster_while_live(self):
        svc = _svc(live_poll_interval="20000", poll_interval="60000")
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)

        # A live match -> fast cadence
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(status="STATUS_FIRST_HALF")])
        assert await svc._tick() == 20.0

        # Only scheduled/finished matches -> normal cadence
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(status="STATUS_SCHEDULED")])
        assert await svc._tick() == 60.0

    async def test_idle_when_no_tournament(self):
        svc = _svc()
        svc.wc_data.get_active_tournament = AsyncMock(return_value=None)
        svc.espn_client.fetch_match_states = AsyncMock()
        delay = await svc._tick()
        assert delay == svc.idle_interval_seconds
        svc.espn_client.fetch_match_states.assert_not_awaited()

    async def test_state_persisted_across_restart(self):
        bot = _make_bot()
        svc1 = WorldCupLiveService(bot)
        svc1.has_external_notification_targets = lambda: False
        svc1.get_mesh_flood_scope = lambda: None
        svc1.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc1.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)
        svc1.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=1, a=0, status="STATUS_FIRST_HALF")])
        await svc1._tick()  # seeds 1-0

        # New instance reuses the same bot/db -> loads persisted state, no re-announce of seed
        svc2 = WorldCupLiveService(bot)
        svc2.has_external_notification_targets = lambda: False
        svc2.get_mesh_flood_scope = lambda: None
        svc2.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc2.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)
        svc2.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=1, a=0, status="STATUS_FIRST_HALF")])
        await svc2._tick()
        svc2.bot.command_manager.send_channel_message.assert_not_awaited()
        assert svc2._state["1"]["h"] == 1
