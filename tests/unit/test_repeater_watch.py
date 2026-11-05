#!/usr/bin/env python3
"""Unit tests for watched-repeater offline reporting (#353)."""

import configparser
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from modules import repeater_watch as rw

pytestmark = pytest.mark.unit

KEY_A = "a1b2" + "0" * 60
KEY_B = "c3d4" + "1" * 60
KEY_C = "a1ff" + "2" * 60  # shares 1 byte (a1) with KEY_A, not 2

NOW = datetime(2026, 11, 4, 12, 0, 0)
NOW_TS = NOW.timestamp()
HOUR = 3600.0


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def add_repeater(db, key, name, heard, role="repeater", starred=0):
    db.execute_update(
        "INSERT INTO complete_contact_tracking (public_key, name, role, last_heard, is_starred) "
        "VALUES (?, ?, ?, ?, ?)",
        (key, name, role, heard, starred),
    )


def add_edge(db, from_prefix, to_prefix, seen, from_key=None, to_key=None):
    db.execute_update(
        "INSERT INTO mesh_connections (from_prefix, to_prefix, from_public_key, to_public_key, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (from_prefix, to_prefix, from_key, to_key, seen.isoformat()),
    )


def make_config(**values):
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.add_section(rw.CONFIG_SECTION)
    for key, value in values.items():
        cfg.set(rw.CONFIG_SECTION, key, str(value))
    return cfg


class TestSettings:
    def test_defaults(self):
        s = rw.load_settings(make_config())
        assert not s.enabled
        assert s.offline_after_hours == 24
        assert s.check_interval_minutes == 15
        assert s.notify_recovery
        assert s.offline_message == rw.DEFAULT_OFFLINE_MESSAGE

    def test_watch_entries_are_normalized_and_deduplicated(self):
        s = rw.load_settings(make_config(watch=f"{KEY_A.upper()}, {KEY_A}, nothex!, ab12, {KEY_B[:8]}"))
        assert s.watch == (KEY_A, KEY_B[:8])

    def test_out_of_range_numbers_fall_back(self):
        s = rw.load_settings(make_config(offline_after_hours="0", check_interval_minutes="abc"))
        assert s.offline_after_hours == 24
        assert s.check_interval_minutes == 15

    def test_broken_template_falls_back(self):
        s = rw.load_settings(make_config(offline_message="{nmae} is down"))
        assert s.offline_message == rw.DEFAULT_OFFLINE_MESSAGE

    def test_round_trip(self):
        s = rw.load_settings(make_config(
            enabled="true", watch=KEY_A, channels="#ops, Public", dm_recipients="Alice",
            offline_after_hours="36", flood_scope="#west",
        ))
        again = rw.load_settings(make_config(**rw.settings_to_config_values(s)))
        assert again == s

    def test_template_errors(self):
        assert rw.template_errors(rw.DEFAULT_OFFLINE_MESSAGE) == []
        assert rw.template_errors("{nope}") == ["unknown placeholder {nope}"]
        assert rw.template_errors("{name:>10}") == ["{name} takes no format spec"]
        assert rw.template_errors("{name") != []


class TestFormatting:
    @pytest.mark.parametrize(("seconds", "text"), [
        (30, "1m"), (45 * 60, "45m"), (26 * HOUR, "26h"), (50 * HOUR, "2d 2h"), (72 * HOUR, "3d"),
    ])
    def test_duration(self, seconds, text):
        assert rw.format_duration(seconds) == text

    def test_prefix_follows_bot_width(self):
        assert rw.display_prefix(KEY_A, 1) == "A1"
        assert rw.display_prefix(KEY_A, 2) == "A1B2"

    def test_pack_lines_fits_the_budget(self):
        lines = ["x" * 40, "y" * 40, "z" * 40]
        packed = rw.pack_lines(lines, 90)
        assert packed == ["x" * 40 + " | " + "y" * 40, "z" * 40]
        assert all(len(m.encode()) <= 90 for m in packed)


class TestEvidence:
    def test_advert_and_relay_by_public_key(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_edge(test_db, "a1b2", "c3d4", ago(2), from_key=KEY_A)
        act = rw.gather_activity(test_db, [KEY_A])[KEY_A]
        assert act.name == "Hilltop"
        assert act.last_advert == pytest.approx(ago(30).timestamp())
        assert act.last_relay == pytest.approx(ago(2).timestamp())
        assert act.last_activity == pytest.approx(ago(2).timestamp())

    def test_two_byte_prefix_counts_when_unique(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_repeater(test_db, KEY_C, "Valley", ago(1))
        add_edge(test_db, "a1b2", "c3d4", ago(3))
        act = rw.gather_activity(test_db, [KEY_A])[KEY_A]
        assert act.last_relay == pytest.approx(ago(3).timestamp())

    def test_one_byte_prefix_never_counts(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_edge(test_db, "a1", "c3", ago(1))
        assert rw.gather_activity(test_db, [KEY_A])[KEY_A].last_relay is None

    def test_shared_two_byte_prefix_does_not_count(self, test_db):
        twin = "a1b2" + "9" * 60
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_repeater(test_db, twin, "Twin", ago(1))
        add_edge(test_db, "a1b2", "c3d4", ago(1))
        assert rw.gather_activity(test_db, [KEY_A])[KEY_A].last_relay is None

    def test_prefix_edge_owned_by_another_key_does_not_count(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_edge(test_db, "a1b2", "c3d4", ago(1), from_key="a1b2" + "f" * 60)
        assert rw.gather_activity(test_db, [KEY_A])[KEY_A].last_relay is None

    def test_watch_list_resolves_prefixes_and_starred(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(1))
        add_repeater(test_db, KEY_B, "Ridge", ago(1), starred=1)
        add_repeater(test_db, "e" * 64, "Phone", ago(1), role="companion", starred=1)
        s = rw.load_settings(make_config(watch=KEY_A[:10], watch_starred="true"))
        assert rw.resolve_watch_list(test_db, s) == [KEY_A, KEY_B]


def activity(key, last_hours_ago, name="Hilltop"):
    a = rw.RepeaterActivity(public_key=key, name=name)
    a.last_advert = None if last_hours_ago is None else NOW_TS - last_hours_ago * HOUR
    return {key: a}


class TestEvaluate:
    def test_quiet_past_the_threshold_goes_offline_once(self):
        listening = NOW_TS - 100 * HOUR
        t, state = rw.evaluate(NOW_TS, activity(KEY_A, 25), {}, listening, 24 * HOUR)
        assert [x.event for x in t] == [rw.EVENT_OFFLINE]
        assert t[0].silent_for == pytest.approx(25 * HOUR)
        t2, state2 = rw.evaluate(NOW_TS + HOUR, activity(KEY_A, 26), state, listening, 24 * HOUR)
        assert t2 == []
        assert state2[KEY_A].status == rw.STATUS_OFFLINE

    def test_not_judged_on_silence_before_listening_started(self):
        listening = NOW_TS - 2 * HOUR  # bot restarted two hours ago
        t, state = rw.evaluate(NOW_TS, activity(KEY_A, 200), {}, listening, 24 * HOUR)
        assert t == []
        assert state[KEY_A].status == rw.STATUS_UP

    def test_never_heard_goes_offline_after_a_full_window(self):
        listening = NOW_TS - 25 * HOUR
        t, _ = rw.evaluate(NOW_TS, activity(KEY_A, None), {}, listening, 24 * HOUR)
        assert [x.event for x in t] == [rw.EVENT_OFFLINE]
        assert t[0].silent_for is None

    def test_heard_again_recovers_with_outage_length(self):
        listening = NOW_TS - 100 * HOUR
        _, state = rw.evaluate(NOW_TS, activity(KEY_A, 25), {}, listening, 24 * HOUR)
        later = NOW_TS + 10 * HOUR
        back = activity(KEY_A, -9)  # heard 9h after NOW, i.e. 1h before `later`
        t, state = rw.evaluate(later, back, state, listening, 24 * HOUR)
        assert [x.event for x in t] == [rw.EVENT_RECOVERED]
        assert t[0].down_for == pytest.approx(34 * HOUR)
        assert state[KEY_A].status == rw.STATUS_UP

    def test_unwatched_repeaters_drop_out_of_state(self):
        state = {KEY_B: rw.WatchState(rw.STATUS_OFFLINE, NOW_TS, None)}
        _, new_state = rw.evaluate(NOW_TS, activity(KEY_A, 1), state, NOW_TS - 50 * HOUR, 24 * HOUR)
        assert set(new_state) == {KEY_A}

    def test_notice_lines_skip_recovery_when_off(self):
        s = rw.load_settings(make_config(notify_recovery="false"))
        transitions = [
            rw.Transition(KEY_A, rw.EVENT_RECOVERED, "Hilltop", down_for=5 * HOUR),
            rw.Transition(KEY_B, rw.EVENT_OFFLINE, "Ridge", silent_for=30 * HOUR),
        ]
        assert rw.notice_lines(transitions, s, NOW_TS, NOW_TS - 50 * HOUR) == [
            "Repeater offline: Ridge (C3), not heard for 30h",
        ]

    def test_never_heard_notice_says_since_listening(self):
        s = rw.load_settings(make_config())
        t = [rw.Transition(KEY_A, rw.EVENT_OFFLINE, "Hilltop")]
        assert rw.notice_lines(t, s, NOW_TS, NOW_TS - 30 * HOUR) == [
            "Repeater offline: Hilltop (A1), not heard for 30h+",
        ]


def make_bot(db, **cfg):
    config = make_config(enabled="true", **cfg)
    bot = Mock()
    bot.logger = Mock()
    bot.config = config
    bot.db_manager = db
    bot.connected = True
    bot.is_radio_offline = False
    bot.is_radio_zombie = False
    bot.command_manager = Mock()
    bot.command_manager.channel_body_budget = Mock(return_value=120)
    bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=True)
    bot.command_manager.send_dm = AsyncMock(return_value=True)
    return bot


class TestMonitor:
    async def test_offline_then_recovered_notifies_each_once(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_repeater(test_db, KEY_B, "Ridge", ago(0.1))  # keeps the bot from looking deaf
        test_db.set_metadata(rw.LISTENING_METADATA_KEY, json.dumps(NOW_TS - 100 * HOUR))
        test_db.set_metadata(rw.LAST_CHECK_METADATA_KEY, json.dumps(NOW_TS - 600))
        bot = make_bot(test_db, watch=KEY_A, channels="#ops", dm_recipients="Alice")
        monitor = rw.RepeaterWatchMonitor(bot)

        out = await monitor.check(monitor.settings, now=NOW_TS)
        assert [t.event for t in out.transitions] == [rw.EVENT_OFFLINE]
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once_with(
            "#ops", ["Repeater offline: Hilltop (A1), not heard for 30h"], scope=None
        )
        bot.command_manager.send_dm.assert_awaited_once()

        out = await monitor.check(monitor.settings, now=NOW_TS + 900)
        assert out.transitions == []
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

        test_db.execute_update(
            "UPDATE complete_contact_tracking SET last_heard = ? WHERE public_key = ?",
            (NOW + timedelta(seconds=1000), KEY_A),
        )
        out = await monitor.check(monitor.settings, now=NOW_TS + 1800)
        assert [t.event for t in out.transitions] == [rw.EVENT_RECOVERED]
        events = rw.recent_events(test_db)
        assert [e["event"] for e in events] == [rw.EVENT_RECOVERED, rw.EVENT_OFFLINE]

    async def test_restart_keeps_offline_state(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_repeater(test_db, KEY_B, "Ridge", ago(0.1))
        test_db.set_metadata(rw.LISTENING_METADATA_KEY, json.dumps(NOW_TS - 100 * HOUR))
        test_db.set_metadata(rw.LAST_CHECK_METADATA_KEY, json.dumps(NOW_TS - 600))
        bot = make_bot(test_db, watch=KEY_A, channels="#ops")
        await rw.RepeaterWatchMonitor(bot).check(rw.load_settings(bot.config), now=NOW_TS)
        fresh = rw.RepeaterWatchMonitor(bot)  # as after a restart
        out = await fresh.check(fresh.settings, now=NOW_TS + 900)
        assert out.transitions == []
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

    async def test_long_downtime_restarts_the_listening_clock(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_repeater(test_db, KEY_B, "Ridge", ago(0.1))
        test_db.set_metadata(rw.LISTENING_METADATA_KEY, json.dumps(NOW_TS - 100 * HOUR))
        test_db.set_metadata(rw.LAST_CHECK_METADATA_KEY, json.dumps(NOW_TS - 48 * HOUR))
        bot = make_bot(test_db, watch=KEY_A, channels="#ops")
        monitor = rw.RepeaterWatchMonitor(bot)
        out = await monitor.check(monitor.settings, now=NOW_TS)
        assert out.transitions == []
        assert json.loads(test_db.get_metadata(rw.LISTENING_METADATA_KEY)) == NOW_TS

    async def test_deaf_bot_skips_and_resets_listening(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        test_db.set_metadata(rw.LISTENING_METADATA_KEY, json.dumps(NOW_TS - 100 * HOUR))
        test_db.set_metadata(rw.LAST_CHECK_METADATA_KEY, json.dumps(NOW_TS - 600))
        bot = make_bot(test_db, watch=KEY_A, channels="#ops")
        monitor = rw.RepeaterWatchMonitor(bot)
        out = await monitor.check(monitor.settings, now=NOW_TS)
        assert out.skipped == "nothing heard recently"
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        assert json.loads(test_db.get_metadata(rw.LISTENING_METADATA_KEY)) is None

    async def test_disconnected_radio_skips(self, test_db):
        bot = make_bot(test_db, watch=KEY_A)
        bot.connected = False
        monitor = rw.RepeaterWatchMonitor(bot)
        out = await monitor.check(monitor.settings, now=NOW_TS)
        assert out.skipped == "radio not listening"

    async def test_tick_respects_enabled_and_interval(self, test_db):
        bot = make_bot(test_db)
        bot.config.set(rw.CONFIG_SECTION, "enabled", "false")
        monitor = rw.RepeaterWatchMonitor(bot)
        monitor.tick(NOW_TS)
        assert monitor._task is None
        bot.config.set(rw.CONFIG_SECTION, "enabled", "true")
        monitor.tick(NOW_TS)
        first = monitor._task
        assert first is not None
        await first
        monitor.tick(NOW_TS + 60)  # inside the 15-minute interval
        assert monitor._task is first
        monitor.tick(NOW_TS + 901)
        assert monitor._task is not first
        await monitor.stop()


class TestStatusView:
    def test_rows_report_status_and_evidence(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(30))
        add_repeater(test_db, KEY_B, "Ridge", ago(1))
        rw.save_state(test_db, {KEY_A: rw.WatchState(rw.STATUS_OFFLINE, NOW_TS - HOUR, ago(30).timestamp())})
        view = rw.status_view(test_db, make_config(watch=f"{KEY_A},{KEY_B}"), now=NOW_TS)
        assert [r["public_key"] for r in view["rows"]] == [KEY_A, KEY_B]
        assert view["rows"][0]["status"] == rw.STATUS_OFFLINE
        assert view["rows"][1]["status"] == rw.STATUS_UP

    def test_candidates_list_only_repeaters(self, test_db):
        add_repeater(test_db, KEY_A, "Hilltop", ago(3))
        add_repeater(test_db, KEY_B, "Room", ago(1), role="roomserver")
        add_repeater(test_db, "e" * 64, "Phone", ago(1), role="companion")
        names = [c["name"] for c in rw.candidate_repeaters(test_db)]
        assert names == ["Room", "Hilltop"]
