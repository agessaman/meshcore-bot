"""Tests for the World Cup command and its data helper."""

import configparser
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.clients.espn_client import ESPNClient
from modules.clients.worldcup_data import WorldCupData
from modules.commands.worldcup_command import WorldCupCommand
from tests.conftest import mock_message


def _make_bot():
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Worldcup_Command")
    config.set("Worldcup_Command", "enabled", "true")
    bot.config = config
    bot.translator = MagicMock()
    # Echo the key back (with the kwargs ignored) so assertions can match on key
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    return bot


def _make_command():
    cmd = WorldCupCommand(_make_bot())
    # Deterministic, network-free message budget
    cmd.get_max_message_length = lambda message: 150
    return cmd


def _window_calendar(active: bool):
    """Build an ESPN-style calendar that does/doesn't contain now."""
    if active:
        start = datetime.now(timezone.utc) - timedelta(days=2)
        end = datetime.now(timezone.utc) + timedelta(days=20)
    else:
        start = datetime(2023, 7, 20, tzinfo=timezone.utc)
        end = datetime(2023, 8, 20, tzinfo=timezone.utc)
    fmt = "%Y-%m-%dT%H:%MZ"
    return [{"label": "Group", "startDate": start.strftime(fmt), "endDate": end.strftime(fmt)}]


# --------------------------------------------------------------------- helpers


class TestStatValue:
    def test_lookup_by_name_and_type(self):
        stats = [
            {"name": "wins", "value": 2.0, "type": "wins"},
            {"name": "ties", "value": 1.0, "type": "ties"},
            {"name": "pointDifferential", "value": 3.0, "type": "pointdifferential"},
        ]
        assert ESPNClient._stat_value(stats, "wins") == 2.0
        assert ESPNClient._stat_value(stats, "ties", "draws") == 1.0
        assert ESPNClient._stat_value(stats, "missing") is None


class TestPackLines:
    def test_respects_byte_budget_and_cap(self):
        cmd = _make_command()
        lines = [f"line-{i}-xxxxxxxxxxxxxxxxxxxx" for i in range(20)]
        chunks = cmd._pack_lines(lines, max_len=40, max_chunks=3)
        assert len(chunks) <= 3
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 40

    def test_oversized_single_line_truncated(self):
        cmd = _make_command()
        chunks = cmd._pack_lines(["x" * 500], max_len=50, max_chunks=3)
        assert len(chunks) == 1
        assert len(chunks[0].encode("utf-8")) <= 50


# ------------------------------------------------------------ season detection


class TestActiveTournament:
    async def test_mens_in_window_is_active(self):
        helper = WorldCupData(MagicMock())
        helper.espn.fetch_scoreboard_with_calendar = AsyncMock(
            return_value={"calendar": _window_calendar(True), "league_name": "FIFA World Cup", "competitors": []}
        )
        active = await helper.get_active_tournament()
        assert active is not None
        assert active["league"] == "fifa.world"

    async def test_off_season_returns_none(self):
        helper = WorldCupData(MagicMock())
        helper.espn.fetch_scoreboard_with_calendar = AsyncMock(
            return_value={"calendar": _window_calendar(False), "league_name": "FIFA Women's World Cup", "competitors": []}
        )
        active = await helper.get_active_tournament()
        assert active is None

    async def test_result_is_cached(self):
        helper = WorldCupData(MagicMock())
        helper.espn.fetch_scoreboard_with_calendar = AsyncMock(
            return_value={"calendar": _window_calendar(True), "league_name": "FIFA World Cup", "competitors": []}
        )
        await helper.get_active_tournament()
        await helper.get_active_tournament()
        # Only the men's probe runs once (cached), never re-hitting ESPN
        assert helper.espn.fetch_scoreboard_with_calendar.await_count == 1


# ------------------------------------------------------------ nation resolving


class TestResolveNation:
    async def _helper_with_teams(self):
        helper = WorldCupData(MagicMock())
        helper.espn.fetch_standings = AsyncMock(
            return_value=[
                {"group_name": "Group A", "entries": [
                    {"id": "203", "name": "Mexico", "abbr": "MEX", "location": "Mexico"},
                ]},
                {"group_name": "Group C", "entries": [
                    {"id": "111", "name": "United States", "abbr": "USA", "location": "United States"},
                ]},
            ]
        )
        helper.espn.fetch_scoreboard_with_calendar = AsyncMock(return_value={"competitors": []})
        return helper

    async def test_resolve_by_name(self):
        helper = await self._helper_with_teams()
        info = await helper.resolve_nation("mexico", "fifa.world")
        assert info and info["team_id"] == "203"
        assert info["league"] == "fifa.world"

    async def test_resolve_by_synonym(self):
        helper = await self._helper_with_teams()
        info = await helper.resolve_nation("usa", "fifa.world")
        assert info and info["team_id"] == "111"

    async def test_unknown_nation(self):
        helper = await self._helper_with_teams()
        assert await helper.resolve_nation("atlantis", "fifa.world") is None


# ----------------------------------------------------------------- dispatch


def _patch_active(cmd, league="fifa.world", dates=False, in_group_stage=False):
    value = {"league": league, "label": "FIFA World Cup", "in_group_stage": in_group_stage}
    if dates:
        value["start_date"] = "20260611"
        value["end_date"] = "20260719"
    cmd.wc_data.get_active_tournament = AsyncMock(return_value=value)


class TestDispatch:
    async def test_not_in_season(self):
        cmd = _make_command()
        cmd.wc_data.get_active_tournament = AsyncMock(return_value=None)
        await cmd.execute(mock_message("wc"))
        cmd.bot.command_manager.send_response.assert_awaited_once()
        assert cmd.bot.command_manager.send_response.await_args.args[1] == "commands.worldcup.not_in_season"

    async def test_today_no_args(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.espn_client.fetch_scoreboard_with_calendar = AsyncMock(
            return_value={"events": [
                {"formatted": "@GER 7-1 CUW (FT)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 100},
                {"formatted": "@NED 2-2 JPN (45')", "timestamp": -1, "status": "STATUS_IN_PROGRESS", "event_timestamp": 200},
            ]}
        )
        await cmd.execute(mock_message("wc"))
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        # Live game should be ordered before the completed result
        assert chunks[0].splitlines()[0].endswith("(45')")

    async def test_group_standings(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.espn_client.fetch_standings = AsyncMock(
            return_value=[
                {"group_name": "Group A", "entries": [
                    {"rank": 1, "abbr": "MEX", "pts": 3, "gd": 2, "w": 1, "d": 0, "l": 0},
                    {"rank": 2, "abbr": "KSA", "pts": 0, "gd": -2, "w": 0, "d": 0, "l": 1},
                ]},
            ]
        )
        await cmd.execute(mock_message("wc group a"))
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        joined = "\n".join(chunks)
        assert "Group A" in joined
        assert "MEX 3p +2 (1-0-0)" in joined

    async def test_group_not_found(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.espn_client.fetch_standings = AsyncMock(
            return_value=[{"group_name": "Group A", "entries": []}]
        )
        await cmd.execute(mock_message("wc group z"))
        assert cmd.bot.command_manager.send_response.await_args.args[1] == "commands.worldcup.group_not_found"

    async def test_groups_list(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.espn_client.fetch_standings = AsyncMock(
            return_value=[{"group_name": "Group A", "entries": []}, {"group_name": "Group B", "entries": []}]
        )
        await cmd.execute(mock_message("wc groups"))
        assert cmd.bot.command_manager.send_response.await_args.args[1] == "commands.worldcup.groups_list"

    async def test_nation_matches(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.wc_data.resolve_nation = AsyncMock(return_value={"sport": "soccer", "league": "fifa.world", "team_id": "481"})
        cmd.espn_client.fetch_team_schedule = AsyncMock(
            return_value=[{"formatted": "@GER 7-1 CUW (FT)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 100, "id": "1"}]
        )
        await cmd.execute(mock_message("wc germany"))
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        assert "GER 7-1 CUW" in "\n".join(chunks)

    async def test_unknown_nation(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.wc_data.resolve_nation = AsyncMock(return_value=None)
        await cmd.execute(mock_message("wc atlantis"))
        assert cmd.bot.command_manager.send_response.await_args.args[1] == "commands.worldcup.nation_not_found"

    async def test_nation_group_stage_shows_all_in_one_message(self):
        cmd = _make_command()
        _patch_active(cmd, dates=True, in_group_stage=True)
        cmd.wc_data.resolve_nation = AsyncMock(return_value={"sport": "soccer", "league": "fifa.world", "team_id": "660"})
        cmd.espn_client.fetch_team_fixtures = AsyncMock(return_value=[
            {"formatted": "@USA 4-1 PAR (FT, 6/12)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 100, "id": "1"},
            {"formatted": "@USA vs. AUS (6/19 12:00 PM)", "timestamp": 200, "status": "STATUS_SCHEDULED", "event_timestamp": 200, "id": "2"},
            {"formatted": "@TUR vs. USA (6/25 7:00 PM)", "timestamp": 300, "status": "STATUS_SCHEDULED", "event_timestamp": 300, "id": "3"},
        ])
        await cmd.execute(mock_message("wc usa"))
        cmd.espn_client.fetch_team_fixtures.assert_awaited_once_with("soccer", "fifa.world", "660", "20260611", "20260719")
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        assert len(chunks) == 1  # single message
        joined = chunks[0]
        # All three group matches shown
        assert "USA 4-1 PAR" in joined and "USA vs. AUS" in joined and "TUR vs. USA" in joined

    async def test_nation_knockout_shows_last_and_next(self):
        cmd = _make_command()
        _patch_active(cmd, dates=True, in_group_stage=False)
        cmd.wc_data.resolve_nation = AsyncMock(return_value={"sport": "soccer", "league": "fifa.world", "team_id": "660"})
        cmd.espn_client.fetch_team_fixtures = AsyncMock(return_value=[
            {"formatted": "@USA 4-1 PAR (FT, 6/12)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 100, "id": "1"},
            {"formatted": "@USA 2-0 AUS (FT, 6/19)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 200, "id": "2"},
            {"formatted": "@TUR 1-3 USA (FT, 6/25)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 300, "id": "3"},
            {"formatted": "@USA vs. WIN (7/3 11:00 AM)", "timestamp": 400, "status": "STATUS_SCHEDULED", "event_timestamp": 400, "id": "4"},
        ])
        await cmd.execute(mock_message("wc usa"))
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        assert len(chunks) == 1
        joined = chunks[0]
        # Only the most recent result + the next fixture (earlier results omitted)
        assert "TUR 1-3 USA" in joined and "USA vs. WIN" in joined
        assert "USA 4-1 PAR" not in joined and "USA 2-0 AUS" not in joined


class TestPhaseDetection:
    async def test_group_stage_flag_from_calendar(self):
        from datetime import datetime, timedelta, timezone
        helper = WorldCupData(MagicMock())
        now = datetime.now(timezone.utc)
        cal = [
            {"label": "Group Stage", "startDate": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%MZ"),
             "endDate": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%MZ")},
            {"label": "Round of 16", "startDate": (now + timedelta(days=6)).strftime("%Y-%m-%dT%H:%MZ"),
             "endDate": (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%MZ")},
        ]
        helper.espn.fetch_scoreboard_with_calendar = AsyncMock(
            return_value={"calendar": cal, "league_name": "FIFA World Cup", "competitors": []}
        )
        active = await helper.get_active_tournament()
        assert active["in_group_stage"] is True

    async def test_nation_matches_deduped(self):
        cmd = _make_command()
        _patch_active(cmd)
        cmd.wc_data.resolve_nation = AsyncMock(return_value={"sport": "soccer", "league": "fifa.world", "team_id": "481"})
        # The schedule endpoint can return the same event twice (schedule + scoreboard fallback)
        dup = {"formatted": "@GER 7-1 CUW (FT)", "timestamp": 9999999998, "status": "STATUS_FULL_TIME", "event_timestamp": 100, "id": "1"}
        cmd.espn_client.fetch_team_schedule = AsyncMock(return_value=[dict(dup), dict(dup)])
        await cmd.execute(mock_message("wc germany"))
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        assert "\n".join(chunks).count("GER 7-1 CUW") == 1
