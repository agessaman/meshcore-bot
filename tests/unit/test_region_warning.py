"""Unit tests for regional flood-scope classification and the warning monitor."""

import configparser
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules import region_warning
from modules.db_migrations import MigrationRunner
from modules.enums import PayloadType, RouteType
from modules.message_handler import (
    CHANNEL_SENDER_FALLBACK,
    RF_MATCH_KEY,
    MessageHandler,
)
from modules.region_warning import (
    ACTION_DRY_RUN,
    ACTION_FAILED,
    ACTION_SENT,
    VERDICT_GLOBAL,
    VERDICT_SCOPED,
    VERDICT_UNKNOWN,
    RegionWarningMonitor,
    RegionWarningSettings,
)

GRP_TXT = int(PayloadType.GRP_TXT.value)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _handler() -> MessageHandler:
    """A MessageHandler with only the attributes the classifier touches."""
    handler = object.__new__(MessageHandler)
    handler.bot = MagicMock()
    handler.logger = Mock()
    return handler


def _rf(correlated: bool = True, **fields):
    row = {RF_MATCH_KEY: "exact" if correlated else "fallback"}
    row.update(fields)
    return row


def _scope_eligible_rf(correlated: bool = True):
    return _rf(
        correlated,
        route_type_int=int(RouteType.TRANSPORT_FLOOD.value),
        transport_code1=4242,
        payload_type_int=GRP_TXT,
        scope_payload_hex="aabbcc",
    )


class _FakeDBManager:
    """A real SQLite database on a shared in-memory connection."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        MigrationRunner(self._conn, Mock()).run()

    @contextmanager
    def connection(self):
        yield self._conn

    def execute_query(self, query, params=()):
        cursor = self._conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def set_metadata(self, key, value):
        self._conn.execute(
            "INSERT INTO bot_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_metadata(self, key):
        row = self._conn.execute(
            "SELECT value FROM bot_metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def _config(**values) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section(region_warning.CONFIG_SECTION)
    for key, value in values.items():
        config.set(region_warning.CONFIG_SECTION, key, str(value))
    return config


def _monitor(db=None, **settings_values) -> RegionWarningMonitor:
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = _config(**settings_values)
    bot.db_manager = db if db is not None else _FakeDBManager()
    bot.channel_responses_enabled = True
    bot.meshcore.self_info = {"public_key": "bbbbcccc" * 8}
    # CHANNEL_MSG_RECV carries no pubkey, so every sender below is a bare name.
    bot.meshcore.contacts = {}
    bot.meshcore.get_contact_by_name = lambda name: {"adv_name": name}
    bot.command_manager = MagicMock()
    bot.command_manager.is_user_banned.return_value = False
    bot.command_manager.send_dm = AsyncMock(return_value=True)
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    bot.command_manager.get_max_message_length.return_value = 130
    return RegionWarningMonitor(bot)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassifyFloodScope:
    def _classify(self, **kwargs):
        args = {
            "reply_scope": None,
            "recent_rf_data": None,
            "packet_info": None,
            "scope_rf_data": None,
            "scope_packet_info": None,
        }
        args.update(kwargs)
        return _handler()._classify_channel_flood_scope(**args)

    def test_matched_reply_scope_is_scoped(self):
        assert self._classify(reply_scope="#west") == VERDICT_SCOPED

    def test_correlated_scope_eligible_packet_is_scoped(self):
        """A transport code means a region was set, even one we have no key for."""
        assert self._classify(scope_rf_data=_scope_eligible_rf()) == VERDICT_SCOPED

    def test_correlated_transport_flood_is_scoped(self):
        assert self._classify(
            recent_rf_data=_rf(route_type_int=int(RouteType.TRANSPORT_FLOOD.value)),
            scope_rf_data=_scope_eligible_rf(),
        ) == VERDICT_SCOPED

    def test_correlated_plain_flood_is_global(self):
        assert self._classify(
            recent_rf_data=_rf(route_type_int=int(RouteType.FLOOD.value)),
        ) == VERDICT_GLOBAL

    def test_uncorrelated_row_is_never_global(self):
        """An argument from absence must not spend airtime accusing anyone.

        flood_scopes accepts "no scope-eligible packet in the window" as proof
        of an unscoped FLOOD, because the cost of being wrong there is one extra
        reply. Here it would be an unsolicited warning, so the verdict needs RF
        correlated to this message.
        """
        assert self._classify(
            recent_rf_data=_rf(False, route_type_int=int(RouteType.FLOOD.value)),
            scope_rf_data=None,
        ) == VERDICT_UNKNOWN

    def test_uncorrelated_transport_flood_row_is_never_global(self):
        """A row that says TC_FLOOD must not come back as 'no region code'."""
        assert self._classify(
            recent_rf_data=_rf(False, route_type_int=int(RouteType.TRANSPORT_FLOOD.value)),
            scope_rf_data=None,
        ) == VERDICT_UNKNOWN

    def test_payload_verified_correlation_counts_as_correlated(self):
        """The CHAN payload match (#255) is how a channel message correlates at all."""
        row = {RF_MATCH_KEY: "payload", "route_type_int": int(RouteType.FLOOD.value)}
        assert self._classify(recent_rf_data=row, scope_rf_data=None) == VERDICT_GLOBAL

    def test_uncorrelated_with_scoped_traffic_in_window_is_unknown(self):
        assert self._classify(
            recent_rf_data=_rf(False, route_type_int=int(RouteType.FLOOD.value)),
            scope_rf_data=_scope_eligible_rf(correlated=False),
        ) == VERDICT_UNKNOWN

    def test_no_rf_data_is_unknown(self):
        assert self._classify() == VERDICT_UNKNOWN

    def test_uncorrelated_scope_row_never_proves_scoped(self):
        """An uncorrelated scope row describes some other packet, so it decides nothing."""
        assert self._classify(
            scope_rf_data=_scope_eligible_rf(correlated=False),
        ) == VERDICT_UNKNOWN

    def test_decoded_packet_overrides_stale_cached_route_type(self):
        assert self._classify(
            recent_rf_data=_rf(route_type_int=int(RouteType.FLOOD.value)),
            packet_info={"route_type": RouteType.TRANSPORT_FLOOD},
            scope_rf_data=None,
        ) == VERDICT_SCOPED


@pytest.mark.asyncio
class TestObservationHook:
    """The MessageHandler side of the hook, independent of the monitor."""

    def _hooked_handler(self):
        handler = _handler()
        handler.bot.region_warning_monitor = MagicMock()
        handler.bot.region_warning_monitor.observe = AsyncMock()
        handler.bot.connection_time = None
        return handler

    async def _observe(self, handler, **overrides):
        args = {
            "sender_id": "Ann",
            "sender_pubkey": "ab",
            "channel": "#gen",
            "sender_timestamp": 0,
            "reply_scope": None,
            "recent_rf_data": None,
            "packet_info": None,
            "scope_rf_data": None,
            "scope_packet_info": None,
        }
        args.update(overrides)
        await handler._observe_flood_scope(**args)

    async def test_verdict_is_forwarded_to_the_monitor(self):
        handler = self._hooked_handler()
        await self._observe(
            handler, recent_rf_data=_rf(route_type_int=int(RouteType.FLOOD.value)))
        kwargs = handler.bot.region_warning_monitor.observe.await_args.kwargs
        assert kwargs["verdict"] == VERDICT_GLOBAL
        assert kwargs["sender_id"] == "Ann"

    async def test_no_monitor_is_a_noop(self):
        handler = self._hooked_handler()
        handler.bot.region_warning_monitor = None
        await self._observe(handler)

    async def test_old_cached_message_is_skipped(self):
        """A reconnect replays cached traffic; counting it would distort everything."""
        handler = self._hooked_handler()
        handler.bot.connection_time = 2_000
        await self._observe(handler, sender_timestamp=1_000)
        handler.bot.region_warning_monitor.observe.assert_not_awaited()

    async def test_monitor_failure_does_not_escape_to_the_message_path(self):
        handler = self._hooked_handler()
        handler.bot.region_warning_monitor.observe = AsyncMock(
            side_effect=RuntimeError("boom"))
        await self._observe(handler)
        handler.logger.exception.assert_called()

    async def test_unattributable_sender_is_passed_through_as_none(self):
        handler = self._hooked_handler()
        await self._observe(handler, sender_id=None)
        kwargs = handler.bot.region_warning_monitor.observe.await_args.kwargs
        assert kwargs["sender_id"] is None


def test_channel_sender_fallback_is_the_handler_literal():
    """The hook drops this name; if the handler renames it, the guard silently dies."""
    assert CHANNEL_SENDER_FALLBACK == "Channel User"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestLoadSettings:
    def test_defaults_when_section_absent(self):
        settings = region_warning.load_settings(configparser.ConfigParser())
        assert settings.enabled is False
        assert settings.dry_run is True
        assert settings.delivery == region_warning.DELIVERY_DM
        assert settings.track_traffic is True

    def test_channels_normalized(self):
        settings = region_warning.load_settings(_config(channels="#General, public ,"))
        assert settings.channels == ("general", "public")

    def test_unknown_delivery_falls_back_to_dm(self):
        settings = region_warning.load_settings(_config(delivery="carrier pigeon"))
        assert settings.delivery == region_warning.DELIVERY_DM

    def test_garbage_number_falls_back_to_default(self):
        settings = region_warning.load_settings(_config(max_warnings_per_day="soon"))
        assert settings.max_warnings_per_day == 6

    def test_negative_value_falls_back_rather_than_clamping(self):
        """Clamping a typo of -1 to 0 would silently mean 'unlimited' on the cap."""
        settings = region_warning.load_settings(_config(mesh_cooldown_minutes="-5"))
        assert settings.mesh_cooldown_minutes == 30.0

    def test_negative_daily_cap_does_not_become_unlimited(self):
        settings = region_warning.load_settings(_config(max_warnings_per_day="-1"))
        assert settings.max_warnings_per_day == 6

    def test_bare_percent_in_the_message_is_read_not_swallowed(self):
        """A hand-edited bare % must yield the operator's wording, not the default.

        Built with read_string rather than config.set: set() rejects a bare %
        up front, so it cannot reproduce the value that broke the reload. Read
        without raw=True, interpolation raises, _get swallows it, and the bot
        transmits DEFAULT_MESSAGE instead of what the file says.
        """
        config = configparser.ConfigParser()
        config.read_string(
            f"[{region_warning.CONFIG_SECTION}]\nmessage = 100% of the mesh, really\n"
        )
        settings = region_warning.load_settings(config)
        assert settings.message == "100% of the mesh, really"
        assert settings.message != region_warning.DEFAULT_MESSAGE

    def test_empty_allowlist_monitors_every_channel(self):
        assert RegionWarningSettings().monitors_channel("#anything") is True

    def test_allowlist_matches_regardless_of_hash_and_case(self):
        settings = RegionWarningSettings(channels=("general",))
        assert settings.monitors_channel("#General") is True
        assert settings.monitors_channel("weather") is False

    def test_round_trip_through_config_values(self):
        original = RegionWarningSettings(
            enabled=True, dry_run=False, delivery="channel",
            channels=("general",), message="hi {sender}",
            min_unscoped_messages=2, per_sender_cooldown_hours=12.5,
            mesh_cooldown_minutes=0, max_warnings_per_day=0, track_traffic=False,
        )
        values = region_warning.settings_to_config_values(original)
        assert region_warning.load_settings(_config(**values)) == original


class TestMessageRendering:
    def test_placeholders_substituted(self):
        text = region_warning.render_message("hey {sender} on {channel}", "Ann", "#gen")
        assert text == "hey Ann on #gen"

    def test_unknown_placeholder_left_alone(self):
        assert region_warning.render_message("{nope}", "Ann", "#gen") == "{nope}"

    def test_truncation_does_not_split_a_character(self):
        text = "aaaééé"  # each é is two UTF-8 bytes
        trimmed = region_warning.truncate_to_bytes(text, 6)
        assert trimmed == "aaaé"
        assert len(trimmed.encode("utf-8")) <= 6

    def test_short_text_unchanged(self):
        assert region_warning.truncate_to_bytes("hello", 158) == "hello"


# ---------------------------------------------------------------------------
# Monitor behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestObservation:
    async def test_tallies_each_verdict(self):
        monitor = _monitor()
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        await monitor.observe(
            verdict=VERDICT_SCOPED, sender_id="Bob", sender_pubkey="cd", channel="#gen")
        rows = monitor.bot.db_manager.execute_query(
            "SELECT scoped_count, global_count FROM region_scope_daily")
        assert rows == [{"scoped_count": 1, "global_count": 1}]

    async def test_track_traffic_off_writes_no_tally(self):
        monitor = _monitor(track_traffic="false")
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        assert monitor.bot.db_manager.execute_query(
            "SELECT * FROM region_scope_daily") == []

    async def test_disabled_never_sends(self):
        monitor = _monitor(min_unscoped_messages=1)
        for _ in range(5):
            await monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        monitor.bot.command_manager.send_dm.assert_not_called()
        assert monitor.bot.db_manager.execute_query(
            "SELECT * FROM region_warning_events") == []

    async def test_unknown_verdict_never_warns(self):
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        for _ in range(5):
            await monitor.observe(
                verdict=VERDICT_UNKNOWN, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        monitor.bot.command_manager.send_dm.assert_not_called()

    async def test_unattributable_sender_is_counted_but_never_warned(self):
        """A channel message with no "Name: " prefix has no real sender."""
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        for _ in range(5):
            await monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id=None, sender_pubkey="", channel="#gen")
        monitor.bot.command_manager.send_dm.assert_not_called()
        rows = monitor.bot.db_manager.execute_query(
            "SELECT global_count FROM region_scope_daily")
        assert rows[0]["global_count"] == 5

    async def test_bookkeeping_failure_does_not_raise(self):
        monitor = _monitor()
        monitor.bot.db_manager = None
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")


@pytest.mark.asyncio
class TestWarningGates:
    async def _flood(self, monitor, count, sender="Ann", channel="#gen"):
        # sender_pubkey="" matches handle_channel_message: CHANNEL_MSG_RECV has none.
        for _ in range(count):
            await monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id=sender, sender_pubkey="", channel=channel)

    async def test_warns_after_min_unscoped_messages(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=3, mesh_cooldown_minutes=0)
        await self._flood(monitor, 2)
        monitor.bot.command_manager.send_dm.assert_not_called()
        await self._flood(monitor, 1)
        monitor.bot.command_manager.send_dm.assert_called_once()
        recipient, body = monitor.bot.command_manager.send_dm.call_args[0]
        assert recipient == "Ann"
        assert "region code" in body

    async def test_scoped_message_resets_the_run(self):
        """Once a sender sets a region, their earlier unscoped run is stale."""
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=3, mesh_cooldown_minutes=0)
        await self._flood(monitor, 2)
        await monitor.observe(
            verdict=VERDICT_SCOPED, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        await self._flood(monitor, 2)
        monitor.bot.command_manager.send_dm.assert_not_called()

    async def test_channel_allowlist_excludes_other_channels(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, channels="general")
        await self._flood(monitor, 3, channel="#weather")
        monitor.bot.command_manager.send_dm.assert_not_called()
        await self._flood(monitor, 1, channel="#general")
        monitor.bot.command_manager.send_dm.assert_called_once()

    async def test_banned_sender_is_never_warned(self):
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        monitor.bot.command_manager.is_user_banned.return_value = True
        await self._flood(monitor, 3)
        monitor.bot.command_manager.send_dm.assert_not_called()

    async def test_bot_never_warns_itself(self):
        """The channel path has no pubkey, so the name is what has to catch it."""
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        await self._flood(monitor, 3, sender="TestBot")
        monitor.bot.command_manager.send_dm.assert_not_called()

    async def test_bot_is_recognized_by_pubkey_when_one_is_available(self):
        """Not reachable from CHANNEL_MSG_RECV today; kept for paths that do carry one."""
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        for _ in range(3):
            await monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id="Renamed Bot",
                sender_pubkey="bbbbcccc", channel="#gen")
        monitor.bot.command_manager.send_dm.assert_not_called()

    async def test_withheld_warnings_are_counted_so_the_page_can_say_so(self):
        """An empty log otherwise looks the same as a mesh with nothing to report."""
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        monitor.bot.meshcore.get_contact_by_name = lambda name: None
        await self._flood(monitor, 4, sender="Stranger")
        budget = region_warning.warning_budget(
            monitor.bot.db_manager, monitor.settings, monitor.bot.config)
        assert budget["withheld_today"] == 4
        assert budget["used_today"] == 0

    async def test_withheld_counter_does_not_run_for_channel_delivery(self):
        monitor = _monitor(
            enabled="true", dry_run="false", delivery="channel",
            min_unscoped_messages=1, mesh_cooldown_minutes=0)
        monitor.bot.meshcore.get_contact_by_name = lambda name: None
        await self._flood(monitor, 1, sender="Passer By")
        budget = region_warning.warning_budget(
            monitor.bot.db_manager, monitor.settings, monitor.bot.config)
        assert budget["withheld_today"] == 0

    async def test_a_name_with_no_contact_behind_it_is_not_dmed(self):
        """The sender is a display name off the wire, forgeable by anyone."""
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        monitor.bot.meshcore.get_contact_by_name = lambda name: None
        await self._flood(monitor, 3, sender="Victim Node")
        monitor.bot.command_manager.send_dm.assert_not_called()
        assert monitor.bot.db_manager.execute_query(
            "SELECT * FROM region_warning_events") == []

    async def test_channel_delivery_does_not_require_a_contact(self):
        """A channel reply addresses the channel, so there is no contact to resolve."""
        monitor = _monitor(
            enabled="true", dry_run="false", delivery="channel",
            min_unscoped_messages=1, mesh_cooldown_minutes=0)
        monitor.bot.meshcore.get_contact_by_name = lambda name: None
        await self._flood(monitor, 1, sender="Passer By")
        monitor.bot.command_manager.send_channel_message.assert_called_once()

    async def test_channelpause_silences_warnings(self):
        monitor = _monitor(enabled="true", dry_run="false", min_unscoped_messages=1)
        monitor.bot.channel_responses_enabled = False
        await self._flood(monitor, 3)
        monitor.bot.command_manager.send_dm.assert_not_called()

    async def test_daily_cap_counts_failed_attempts(self):
        """A failed send may still have spent airtime, so it spends the cap."""
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0, max_warnings_per_day=2)
        monitor.bot.command_manager.send_dm = AsyncMock(side_effect=[False, False, True])
        for name in ("Ann", "Bob", "Cat"):
            await self._flood(monitor, 1, sender=name)
        assert monitor.bot.command_manager.send_dm.call_count == 2

    async def test_concurrent_messages_cannot_both_pass_a_cap_of_one(self):
        """The slot is reserved before the send awaits, so a second message sees it."""
        import asyncio

        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0, max_warnings_per_day=1)

        async def slow_send(*_args, **_kwargs):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return True

        monitor.bot.command_manager.send_dm = AsyncMock(side_effect=slow_send)
        await asyncio.gather(
            monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen"),
            monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id="Bob", sender_pubkey="cd", channel="#gen"),
        )
        assert monitor.bot.command_manager.send_dm.call_count == 1

    async def test_failed_send_row_is_corrected_not_duplicated(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0)
        monitor.bot.command_manager.send_dm = AsyncMock(return_value=False)
        await self._flood(monitor, 1)
        rows = monitor.bot.db_manager.execute_query(
            "SELECT action, detail FROM region_warning_events")
        assert len(rows) == 1
        assert rows[0]["action"] == ACTION_FAILED
        assert "failed" in rows[0]["detail"]

    async def test_sender_table_stays_bounded(self):
        monitor = _monitor(min_unscoped_messages=99)
        monitor.MAX_TRACKED_SENDERS = 10
        for i in range(60):
            await monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id=f"node-{i}",
                sender_pubkey="ab", channel="#gen")
        assert len(monitor._senders) <= monitor.MAX_TRACKED_SENDERS + 1

    async def test_mesh_cooldown_blocks_a_second_sender(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=30, per_sender_cooldown_hours=0)
        await self._flood(monitor, 1, sender="Ann")
        await self._flood(monitor, 1, sender="Bob")
        assert monitor.bot.command_manager.send_dm.call_count == 1

    async def test_per_sender_cooldown_blocks_a_repeat(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=168)
        await self._flood(monitor, 1, sender="Ann")
        await self._flood(monitor, 1, sender="Bob")
        await self._flood(monitor, 1, sender="Ann")
        senders = [c[0][0] for c in monitor.bot.command_manager.send_dm.call_args_list]
        assert senders == ["Ann", "Bob"]

    async def test_daily_cap_stops_further_warnings(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0, max_warnings_per_day=2)
        for name in ("Ann", "Bob", "Cat", "Dan"):
            await self._flood(monitor, 1, sender=name)
        assert monitor.bot.command_manager.send_dm.call_count == 2

    async def test_cooldown_survives_a_restart(self):
        db = _FakeDBManager()
        first = _monitor(
            db, enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=60, per_sender_cooldown_hours=0)
        await self._flood(first, 1, sender="Ann")
        assert first.bot.command_manager.send_dm.call_count == 1

        second = _monitor(
            db, enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=60, per_sender_cooldown_hours=0)
        await self._flood(second, 1, sender="Bob")
        second.bot.command_manager.send_dm.assert_not_called()

    async def test_failed_send_does_not_start_the_mesh_cooldown(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=60, per_sender_cooldown_hours=0)
        monitor.bot.command_manager.send_dm = AsyncMock(side_effect=[False, True])
        await self._flood(monitor, 1, sender="Ann")
        await self._flood(monitor, 1, sender="Bob")
        assert monitor.bot.command_manager.send_dm.call_count == 2
        actions = [
            row["action"] for row in
            monitor.bot.db_manager.execute_query(
                "SELECT action FROM region_warning_events ORDER BY id")
        ]
        assert actions == [ACTION_FAILED, ACTION_SENT]

    async def test_one_failing_sender_cannot_burn_the_cap_every_day(self):
        """The cap counts attempts, so the per-sender cooldown must too.

        When only the cap counted failures, a sender the radio could not reach
        was retried every min_unscoped_messages messages forever: the day's
        whole budget went on one node and nobody was ever warned.
        """
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=168,
            max_warnings_per_day=3)
        monitor.bot.command_manager.send_dm = AsyncMock(return_value=False)
        await self._flood(monitor, 10, sender="Unreachable")
        assert monitor.bot.command_manager.send_dm.call_count == 1

        # The budget is still there for someone the bot can actually reach.
        monitor.bot.command_manager.send_dm = AsyncMock(return_value=True)
        await self._flood(monitor, 1, sender="Real Offender")
        monitor.bot.command_manager.send_dm.assert_called_once()

    async def test_failed_send_still_spends_the_run(self):
        """Otherwise a failing contact is retried on the sender's very next message."""
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=2,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0)
        monitor.bot.command_manager.send_dm = AsyncMock(return_value=False)
        await self._flood(monitor, 3)
        assert monitor.bot.command_manager.send_dm.call_count == 1


@pytest.mark.asyncio
class TestDelivery:
    async def test_budget_keeps_previews_apart_from_real_sends(self):
        """A morning of dry run must not read as afternoon transmissions."""
        monitor = _monitor(
            enabled="true", dry_run="true", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0)
        for name in ("Ann", "Bob"):
            await monitor.observe(
                verdict=VERDICT_GLOBAL, sender_id=name, sender_pubkey="", channel="#gen")
        budget = region_warning.warning_budget(
            monitor.bot.db_manager, monitor.settings, monitor.bot.config)
        assert budget["previewed_today"] == 2
        assert budget["delivered_today"] == 0
        assert budget["used_today"] == 2

    async def test_dry_run_records_but_transmits_nothing(self):
        monitor = _monitor(
            enabled="true", dry_run="true", min_unscoped_messages=1, mesh_cooldown_minutes=0)
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        monitor.bot.command_manager.send_dm.assert_not_called()
        monitor.bot.command_manager.send_channel_message.assert_not_called()
        rows = monitor.bot.db_manager.execute_query(
            "SELECT action FROM region_warning_events")
        assert [r["action"] for r in rows] == [ACTION_DRY_RUN]

    async def test_dry_run_consumes_the_same_budget_it_previews(self):
        monitor = _monitor(
            enabled="true", dry_run="true", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, per_sender_cooldown_hours=0, max_warnings_per_day=1)
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Bob", sender_pubkey="cd", channel="#gen")
        rows = monitor.bot.db_manager.execute_query(
            "SELECT sender_id FROM region_warning_events")
        assert [r["sender_id"] for r in rows] == ["Ann"]

    async def test_channel_delivery_forces_global_scope(self):
        """A scoped reply could never reach someone outside the region."""
        monitor = _monitor(
            enabled="true", dry_run="false", delivery="channel",
            min_unscoped_messages=1, mesh_cooldown_minutes=0)
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        call = monitor.bot.command_manager.send_channel_message.call_args
        assert call[0][0] == "#gen"
        assert call[1]["scope"] == "*"

    async def test_long_message_is_truncated_to_the_dm_budget(self):
        monitor = _monitor(
            enabled="true", dry_run="false", min_unscoped_messages=1,
            mesh_cooldown_minutes=0, message="x" * 400)
        await monitor.observe(
            verdict=VERDICT_GLOBAL, sender_id="Ann", sender_pubkey="ab", channel="#gen")
        body = monitor.bot.command_manager.send_dm.call_args[0][1]
        assert len(body.encode("utf-8")) == region_warning.DM_BODY_LIMIT


class TestReadHelpers:
    def _seed(self, db, rows):
        with db.connection() as conn:
            for date, channel, scoped, globally, unknown in rows:
                conn.execute(
                    "INSERT INTO region_scope_daily "
                    "(date, channel, scoped_count, global_count, unknown_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (date, channel, scoped, globally, unknown),
                )
            conn.commit()

    def test_traffic_summary_excludes_unknown_from_the_share(self):
        """A mesh the bot can't classify must read as no data, not as clean."""
        db = _FakeDBManager()
        today = datetime.now().date().isoformat()
        self._seed(db, [(today, "#gen", 30, 10, 500)])
        summary = region_warning.traffic_summary(db, _config(), days=7)
        assert summary["channels"][0]["unscoped_pct"] == 25.0
        assert summary["totals"]["unknown"] == 500

    def test_traffic_summary_share_is_none_without_classified_traffic(self):
        db = _FakeDBManager()
        today = datetime.now().date().isoformat()
        self._seed(db, [(today, "#gen", 0, 0, 12)])
        summary = region_warning.traffic_summary(db, _config(), days=7)
        assert summary["channels"][0]["unscoped_pct"] is None
        assert summary["totals"]["unscoped_pct"] is None

    def test_traffic_summary_ignores_rows_outside_the_window(self):
        db = _FakeDBManager()
        old = (datetime.now().date() - timedelta(days=30)).isoformat()
        self._seed(db, [(old, "#gen", 5, 5, 0)])
        assert region_warning.traffic_summary(db, _config(), days=7)["channels"] == []

    def test_daily_series_zero_fills_quiet_days(self):
        db = _FakeDBManager()
        today = datetime.now().date().isoformat()
        self._seed(db, [(today, "#gen", 1, 2, 3)])
        series = region_warning.daily_series(db, _config(), days=5)
        assert len(series) == 5
        assert series[-1] == {"date": today, "scoped": 1, "global": 2, "unknown": 3}
        assert series[0]["global"] == 0

    def test_budget_counts_only_todays_decisions(self):
        db = _FakeDBManager()
        old = (datetime.now() - timedelta(days=2)).isoformat(sep=" ", timespec="seconds")
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        with db.connection() as conn:
            for created_at in (old, now, now):
                conn.execute(
                    "INSERT INTO region_warning_events "
                    "(created_at, sender_id, channel, delivery, action) "
                    "VALUES (?, 'Ann', '#gen', 'dm', 'sent')",
                    (created_at,),
                )
            conn.commit()
        budget = region_warning.warning_budget(
            db, RegionWarningSettings(max_warnings_per_day=6), _config())
        assert budget["used_today"] == 2
        assert budget["remaining"] == 4

    def test_budget_reports_unlimited_when_cap_is_zero(self):
        budget = region_warning.warning_budget(
            _FakeDBManager(), RegionWarningSettings(max_warnings_per_day=0), _config())
        assert budget["unlimited"] is True
        assert budget["remaining"] is None

    def test_read_helpers_tolerate_a_broken_database(self):
        broken = Mock()
        broken.execute_query.side_effect = sqlite3.OperationalError("no such table")
        assert region_warning.recent_events(broken) == []
        assert region_warning.traffic_summary(broken, _config())["channels"] == []
        assert region_warning.warning_budget(
            broken, RegionWarningSettings(), _config())["used_today"] == 0
