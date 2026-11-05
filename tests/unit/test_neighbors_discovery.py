"""Tests for zero-hop neighbor discovery (modules/neighbors_discovery.py).

Ported from the meshcore-packet-capture project's test_neighbors.py, minus the
device-lock tests (the bot serialises radio frames globally in
modules/core.py _serialize_command_frames, so the module takes no lock of its own).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import types

import pytest
from meshcore import EventType

from modules import neighbors_discovery as nb
from modules.db_migrations import MigrationRunner
from modules.enums import AdvertFlags

LOGGER = logging.getLogger("test-neighbors")

SELF_KEY = "ff" * 32
KEY_A = "aa" * 32
KEY_B = "bb" * 32


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeEvent:
    def __init__(self, event_type, payload=None):
        self.type = event_type
        self.payload = payload or {}


class FakeRadio:
    """Delivers DISCOVER_RESPONSE events synchronously from the send call."""

    def __init__(self, responses=None, *, send_result="ok", scopes="DEN,APRS",
                 flood_scope="SEA", reset_result="ok"):
        self.responses = responses or []
        self.send_result = send_result
        self.scopes = scopes
        self.reset_result = reset_result
        self.handler = None
        self.subscribed = 0
        self.unsubscribed = 0
        self.sent_tag = None
        self.regions_calls = []
        self.reset_path_calls = []
        self.self_info = {"public_key": SELF_KEY}
        # A real contact cache; stage 2 must never populate it.
        self.contacts = {}
        self.commands = types.SimpleNamespace(
            send_node_discover_req=self._send,
            req_regions_sync=self._regions,
            get_default_flood_scope=self._flood,
            reset_path=self._reset_path,
        )
        self._flood_scope = flood_scope

    def add_contact(self, pubkey, *, out_path_len=-1):
        """Seed the contact cache the way the bot's contact management would."""
        self.contacts[pubkey] = {"public_key": pubkey, "out_path_len": out_path_len,
                                 "out_path": ""}

    def get_contact_by_key_prefix(self, prefix):
        """Mirrors MeshCore.get_contact_by_key_prefix (prefix match on the key)."""
        for contact in self.contacts.values():
            if contact.get("public_key", "").lower().startswith(prefix.lower()):
                return contact
        return None

    async def _reset_path(self, pubkey):
        self.reset_path_calls.append(pubkey)
        if self.reset_result == "raise":
            raise RuntimeError("serial write failed")
        if self.reset_result == "error":
            # How the library reports a device rejection, and its own response
            # timeout: an ERROR event, not an exception.
            return FakeEvent(EventType.ERROR, {"reason": "timeout"})
        return FakeEvent(EventType.OK, {})

    def subscribe(self, event_type, callback):
        assert event_type == EventType.DISCOVER_RESPONSE
        self.subscribed += 1
        self.handler = callback
        return "subscription"

    def unsubscribe(self, subscription):
        self.unsubscribed += 1

    async def _send(self, filter_bits, prefix_only=True, tag=None):
        self.sent_filter = filter_bits
        self.sent_prefix_only = prefix_only
        self.sent_tag = tag
        if self.send_result == "error":
            return FakeEvent(EventType.ERROR, {"reason": "unsupported"})
        if self.send_result == "none":
            return None
        if self.send_result == "hang":
            await asyncio.sleep(30)
        little_endian = tag.to_bytes(4, "little").hex()
        for response in self.responses:
            payload = dict(response)
            payload.setdefault("tag", little_endian)
            await self.handler(FakeEvent(EventType.DISCOVER_RESPONSE, payload))
        return FakeEvent(EventType.MSG_SENT, {})

    async def _regions(self, pubkey, timeout=0, min_timeout=0):
        self.regions_calls.append(pubkey)
        if self.scopes == "raise":
            raise RuntimeError("device rejected request")
        if self.scopes == "hang":
            await asyncio.sleep(120)
        return self.scopes

    async def _flood(self):
        return FakeEvent(EventType.SELF_INFO, {"scope_name": self._flood_scope})


def response(pubkey, snr=5.0, node_type=None, **extra):
    payload = {
        "pubkey": pubkey,
        "SNR": snr,
        "node_type": AdvertFlags.ADV_TYPE_REPEATER.value if node_type is None else node_type,
    }
    payload.update(extra)
    return payload


def entry(pubkey_char, snr, heard_at, **kwargs):
    return nb.NeighborEntry(pubkey=pubkey_char * 64, snr=snr, heard_at=heard_at, **kwargs)


@pytest.fixture
def fast_cfg():
    """Config with the smallest window the module permits (floored at 5s).

    Tests that must not wait use monkeypatched sleep instead.
    """
    return nb.NeighborsConfig(discover_window=nb.MIN_DISCOVER_WINDOW)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make asyncio.sleep inside the module a no-op so windows are instant."""
    async def instant(_seconds):
        return None
    monkeypatch.setattr(nb.asyncio, "sleep", instant)


# ---------------------------------------------------------------------------
# Interval clamping (firmware band: 12-336h, default 24h)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "given,expected",
    [(11, 12), (12, 12), (24, 24), (48, 48), (336, 336), (400, 336), (0, 24), (-5, 24)],
)
def test_clamp_interval_hours(given, expected):
    assert nb.clamp_interval_hours(given) == expected


def test_interval_seconds_uses_clamped_value():
    assert nb.NeighborsConfig(interval_hours=1).interval_seconds == 12 * 3600
    assert nb.NeighborsConfig(interval_hours=9999).interval_seconds == 336 * 3600


def test_config_floors_values_that_would_never_produce_data():
    cfg = nb.NeighborsConfig(
        discover_window=0, max_neighbors=0, cycle_timeout=1, scope_gap=-3, command_timeout=0
    )
    assert cfg.discover_window == nb.MIN_DISCOVER_WINDOW
    assert cfg.max_neighbors == 1
    assert cfg.cycle_timeout == nb.MIN_CYCLE_TIMEOUT
    assert cfg.scope_gap == 0.0
    # wait_for(timeout=0) raises immediately, which would break every cycle.
    assert cfg.command_timeout == nb.MIN_COMMAND_TIMEOUT


def test_scope_request_budget_exceeds_library_msg_sent_wait():
    cfg = nb.NeighborsConfig(scope_min_timeout=8.0, command_timeout=20.0)
    assert cfg.scope_request_budget > nb.LIBRARY_MSG_SENT_TIMEOUT


def test_cycle_budget_is_smaller_when_scopes_are_disabled():
    off = nb.NeighborsConfig(collect_scopes=False)
    on = nb.NeighborsConfig(collect_scopes=True)
    assert off.cycle_budget < on.cycle_budget
    # Must still cover the listen window plus the discover request.
    assert off.cycle_budget >= off.discover_window + off.command_timeout


def test_discover_filter_targets_repeaters():
    # A bitmask over advert types, so the bit index is the type value.
    assert 1 << AdvertFlags.ADV_TYPE_REPEATER.value == nb.DISCOVER_FILTER_REPEATER
    assert nb.DISCOVER_FILTER_REPEATER == 0x04


# ---------------------------------------------------------------------------
# Ordering: most recently heard, then stronger SNR, then pubkey
# ---------------------------------------------------------------------------

def test_sort_prefers_most_recently_heard():
    older = entry("a", 20.0, 100.0)
    newer = entry("b", -5.0, 200.0)
    assert nb.sort_entries([older, newer], now=200.0) == [newer, older]


def test_sort_breaks_recency_tie_by_snr():
    weak = entry("a", 1.0, 100.0)
    strong = entry("b", 9.0, 100.0)
    assert nb.sort_entries([weak, strong], now=100.0) == [strong, weak]


def test_sort_breaks_full_tie_by_pubkey_ascending():
    first = entry("1", 5.0, 100.0)
    second = entry("2", 5.0, 100.0)
    assert nb.sort_entries([second, first], now=100.0) == [first, second]


def test_sort_compares_whole_seconds_so_snr_can_break_ties():
    """Recency is compared as the *published* heard_secs_ago.

    Sorting on the raw float clock would quantise differently from the field we
    publish, so SNR could never break a tie and the output could be non-monotonic
    in heard_secs_ago. That order decides which entries survive the size budget.
    """
    slightly_newer_weak = entry("a", 1.0, 100.4)
    slightly_older_strong = entry("b", 9.0, 100.0)
    ordered = nb.sort_entries([slightly_newer_weak, slightly_older_strong], now=100.9)
    assert [e.heard_secs_ago(100.9) for e in ordered] == [0, 0]
    assert ordered[0] is slightly_older_strong


# ---------------------------------------------------------------------------
# Payload shape and tail-drop
# ---------------------------------------------------------------------------

def test_message_matches_firmware_contract():
    item = nb.NeighborEntry(pubkey=KEY_A, snr=9.75, heard_at=100.0,
                            scopes="DEN,APRS", status=nb.STATUS_RESPONDED)
    message, dropped = nb.build_neighbors_message(
        "MeshCore-HOWL", "A1B2", "DEN", [item], timestamp="TS", now=142.0
    )
    assert dropped == 0
    # Key order is part of the contract (MQTTPayloadBuilder.cpp).
    assert list(message.keys()) == [
        "timestamp", "origin", "origin_id", "total_neighbors",
        "queried_neighbors", "truncated", "self", "neighbors",
    ]
    assert message["self"] == {"scopes": "DEN"}
    assert message["neighbors"] == [{
        "pubkey": KEY_A.upper(),
        "snr": 9.75,
        "heard_secs_ago": 42,
        "scopes": "DEN,APRS",
        "status": "responded",
    }]


def test_message_reports_truncation_from_the_max_cap():
    items = [entry("a", 1.0, 100.0)]
    message, dropped = nb.build_neighbors_message(
        "o", "i", "", items, timestamp="TS", now=100.0, total_neighbors=9
    )
    assert dropped == 0
    assert message["total_neighbors"] == 9
    assert message["queried_neighbors"] == 1
    assert message["truncated"] is True


def test_message_drops_the_tail_past_the_size_budget():
    items = [nb.NeighborEntry(pubkey=f"{i:064x}", snr=float(i), heard_at=100.0)
             for i in range(300)]
    message, dropped = nb.build_neighbors_message(
        "o", "i", "", items, timestamp="TS", now=100.0
    )
    assert dropped > 0
    assert len(message["neighbors"]) == len(items) - dropped
    assert len(json.dumps(message)) < nb.NEIGHBORS_JSON_BUDGET
    assert message["truncated"] is True


def test_message_publishes_pubkeys_uppercase():
    message, _ = nb.build_neighbors_message(
        "o", "i", "", [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)],
        timestamp="TS", now=0.0,
    )
    assert message["neighbors"][0]["pubkey"] == KEY_A.upper()


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------

async def test_discover_collects_and_sorts_responses(fast_cfg, no_sleep):
    radio = FakeRadio([response(KEY_A, 3.0), response(KEY_B, 9.0)])
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert [e.pubkey for e in entries] == [KEY_B, KEY_A]
    assert radio.sent_filter == nb.DISCOVER_FILTER_REPEATER
    # Full 32-byte pubkeys are required, so prefix_only must be off.
    assert radio.sent_prefix_only is False


async def test_discover_dedupes_keeping_strongest_snr(fast_cfg, no_sleep):
    radio = FakeRadio([response(KEY_A, 3.0), response(KEY_A, 9.0), response(KEY_A, 5.0)])
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert len(entries) == 1
    assert entries[0].snr == 9.0


async def test_discover_accepts_uppercase_pubkeys_as_lowercase(fast_cfg, no_sleep):
    radio = FakeRadio([response(KEY_A.upper(), 1.0)])
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert entries[0].pubkey == KEY_A


@pytest.mark.parametrize("bad", [
    pytest.param({"pubkey": "aa" * 10}, id="short-pubkey"),
    pytest.param({"node_type": AdvertFlags.ADV_TYPE_CHAT.value}, id="not-a-repeater"),
    pytest.param({"tag": "deadbeef"}, id="wrong-tag"),
])
async def test_discover_rejects_unwanted_responses(fast_cfg, no_sleep, bad):
    payload = response(KEY_A, 5.0)
    payload.update(bad)
    radio = FakeRadio([payload])
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert entries == []


async def test_discover_filters_out_self(fast_cfg, no_sleep):
    radio = FakeRadio([response(SELF_KEY, 9.0), response(KEY_A, 1.0)])
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert [e.pubkey for e in entries] == [KEY_A]


async def test_discover_tag_is_known_before_subscribing(fast_cfg, no_sleep):
    """The tag must exist before the handler goes live.

    Otherwise a response arriving between subscribe and send-completion would be
    accepted with no tag check, letting a stale or foreign round leak in.
    """
    radio = FakeRadio([response(KEY_A, 1.0)])
    await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert radio.sent_tag is not None
    assert 1 <= radio.sent_tag <= 0xFFFFFFFF


@pytest.mark.parametrize("result", ["error", "none"])
async def test_discover_returns_none_when_send_fails(fast_cfg, no_sleep, result):
    radio = FakeRadio([], send_result=result)
    assert await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER) is None


async def test_discover_bounds_a_stalled_send():
    """A stalled BLE/serial write must not hang the cycle."""
    cfg = nb.NeighborsConfig(command_timeout=nb.MIN_COMMAND_TIMEOUT)
    radio = FakeRadio([], send_result="hang")
    assert await nb.discover_neighbors(radio, cfg, SELF_KEY, LOGGER) is None


async def test_discover_abandons_cycle_when_session_was_reset(fast_cfg, no_sleep):
    """A reconnect tears down subscriptions, so the collector goes deaf.

    Without this check the cycle would record "0 neighbours" as though the mesh
    were empty.
    """
    radio = FakeRadio([response(KEY_A, 1.0)])
    result = await nb.discover_neighbors(
        radio, fast_cfg, SELF_KEY, LOGGER, still_valid=lambda: False
    )
    assert result is None


async def test_discover_always_unsubscribes(fast_cfg, no_sleep):
    radio = FakeRadio([response(KEY_A, 1.0)])
    await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert radio.subscribed == 1
    assert radio.unsubscribed == 1

    failing = FakeRadio([], send_result="error")
    await nb.discover_neighbors(failing, fast_cfg, SELF_KEY, LOGGER)
    assert failing.unsubscribed == 1


async def test_discover_ignores_stragglers_after_the_window(fast_cfg, no_sleep):
    """Callbacks are spawned as tasks, so one can land after unsubscribe returns."""
    radio = FakeRadio([response(KEY_A, 1.0)])
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    await radio.handler(FakeEvent(EventType.DISCOVER_RESPONSE, response(KEY_B, 9.0)))
    assert [e.pubkey for e in entries] == [KEY_A]


async def test_discover_survives_an_unsubscribe_error(fast_cfg, no_sleep):
    radio = FakeRadio([response(KEY_A, 1.0)])
    radio.unsubscribe = lambda s: (_ for _ in ()).throw(RuntimeError("gone"))
    entries = await nb.discover_neighbors(radio, fast_cfg, SELF_KEY, LOGGER)
    assert [e.pubkey for e in entries] == [KEY_A]


# ---------------------------------------------------------------------------
# Stage 2: scopes
# ---------------------------------------------------------------------------

async def test_collect_scopes_disabled_marks_entries_responded():
    """Stage 2 off must not report a live neighbour as unreachable."""
    cfg = nb.NeighborsConfig(collect_scopes=False)
    radio = FakeRadio()
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].status == nb.STATUS_RESPONDED
    assert entries[0].scopes == ""
    # And it must not spend any airtime.
    assert radio.regions_calls == []


async def test_collect_scopes_populates_scopes_when_enabled():
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes="DEN,APRS")
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].scopes == "DEN,APRS"
    assert entries[0].status == nb.STATUS_RESPONDED
    assert radio.regions_calls == [KEY_A]


async def test_collect_scopes_reports_timeout_when_response_is_none():
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes=None)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].status == nb.STATUS_TIMEOUT


async def test_collect_scopes_reports_send_failed_on_exception():
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes="raise")
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].status == nb.STATUS_SEND_FAILED


async def test_collect_scopes_bounds_a_stalled_request(monkeypatch):
    """A hung request must be cut off, not left holding the radio."""
    # scope_request_budget is floored by the library's 15s MSG_SENT wait, so patch
    # that constant instead of waiting it out.
    monkeypatch.setattr(nb, "LIBRARY_MSG_SENT_TIMEOUT", 0.01)
    cfg = nb.NeighborsConfig(
        collect_scopes=True, scope_gap=0, scope_min_timeout=0.01,
        command_timeout=nb.MIN_COMMAND_TIMEOUT,
    )
    cfg.scope_timeout = 0.01
    radio = FakeRadio(scopes="hang")
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].status == nb.STATUS_SEND_FAILED


async def test_collect_scopes_stops_at_the_cycle_budget(monkeypatch):
    """Unreached neighbours keep the timeout status, matching the firmware."""
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0, cycle_timeout=nb.MIN_CYCLE_TIMEOUT)
    radio = FakeRadio(scopes="DEN")
    entries = [nb.NeighborEntry(pubkey=k, snr=1.0, heard_at=0.0) for k in (KEY_A, KEY_B)]

    clock = {"t": 0.0}
    monkeypatch.setattr(nb.time, "time", lambda: clock["t"])
    real_regions = radio._regions

    async def advance_then_answer(pubkey, timeout=0, min_timeout=0):
        clock["t"] += 1000.0  # blow the budget after the first request
        return await real_regions(pubkey, timeout, min_timeout)

    radio.commands.req_regions_sync = advance_then_answer
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].status == nb.STATUS_RESPONDED
    assert entries[1].status == nb.STATUS_TIMEOUT
    assert len(radio.regions_calls) == 1


async def test_collect_scopes_reraises_cancellation():
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio()

    async def cancelled(pubkey, timeout=0, min_timeout=0):
        raise asyncio.CancelledError

    radio.commands.req_regions_sync = cancelled
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    with pytest.raises(asyncio.CancelledError):
        await nb.collect_scopes(radio, entries, cfg, LOGGER)


@pytest.fixture
def tiny_budget(monkeypatch):
    """A scope_request_budget small enough to cut off a hung request at once.

    scope_request_budget is floored by the library's 15s MSG_SENT wait, so the
    constant has to be patched rather than waited out.
    """
    monkeypatch.setattr(nb, "LIBRARY_MSG_SENT_TIMEOUT", 0.01)
    cfg = nb.NeighborsConfig(
        collect_scopes=True, scope_gap=0, scope_min_timeout=0.01,
        command_timeout=nb.MIN_COMMAND_TIMEOUT,
    )
    cfg.scope_timeout = 0.01
    return cfg


async def test_timed_out_request_restores_a_pinned_contact(tiny_budget):
    """send_anon_req pins a path-less contact to zero-hop and restores it after
    the send, with no try/finally — so our own timeout has to repair it."""
    radio = FakeRadio(scopes="hang")
    radio.add_contact(KEY_A, out_path_len=-1)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, tiny_budget, LOGGER)
    assert entries[0].status == nb.STATUS_SEND_FAILED
    assert radio.reset_path_calls == [KEY_A]


async def test_failed_request_restores_a_pinned_contact():
    """A raising request leaves the same half-applied device state."""
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes="raise")
    radio.add_contact(KEY_A, out_path_len=-1)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert radio.reset_path_calls == [KEY_A]


async def test_contact_with_a_real_path_is_never_reset(tiny_budget):
    """The library only rewrites path-less contacts, so resetting one that has a
    path would throw away routing we did not touch."""
    radio = FakeRadio(scopes="hang")
    radio.add_contact(KEY_A, out_path_len=2)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, tiny_budget, LOGGER)
    assert radio.reset_path_calls == []


async def test_unknown_contact_needs_no_repair(tiny_budget):
    """An unknown pubkey is asked to reply zero-hop; no contact is mutated."""
    radio = FakeRadio(scopes="hang")
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, tiny_budget, LOGGER)
    assert radio.reset_path_calls == []


async def test_a_collapsed_send_error_restores_a_pinned_contact():
    """req_regions_sync returns None for a send_anon_req error too.

    One of those errors is change_contact_path failing *after* the device applied
    the zero-hop path (a lost acknowledgement), which returns before the library's
    own reset_path — so None cannot be assumed to mean "the path was restored".
    """
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes=None)
    radio.add_contact(KEY_A, out_path_len=-1)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert entries[0].status == nb.STATUS_TIMEOUT
    assert radio.reset_path_calls == [KEY_A]


async def test_no_response_needs_no_repair_without_a_known_contact():
    """Nothing was mutated, so nothing may be sent to the device."""
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes=None)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert radio.reset_path_calls == []


@pytest.mark.parametrize("reset_result,expected", [
    ("error", "rejected the flood-path restore"),
    ("raise", "could not restore the flood path"),
])
async def test_a_failed_restore_is_reported_as_a_failure(caplog, reset_result, expected):
    """reset_path reports a rejection as an ERROR event, not an exception.

    Logging success there would hide a contact left pinned to zero-hop.
    """
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes=None, reset_result=reset_result)
    radio.add_contact(KEY_A, out_path_len=-1)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        await nb.collect_scopes(radio, entries, cfg, LOGGER)

    assert radio.reset_path_calls == [KEY_A]
    assert expected in caplog.text
    assert "restored flood path" not in caplog.text


async def test_a_confirmed_restore_is_reported_as_success(caplog):
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes=None)
    radio.add_contact(KEY_A, out_path_len=-1)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]

    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        await nb.collect_scopes(radio, entries, cfg, LOGGER)

    assert "restored flood path" in caplog.text


async def test_successful_request_leaves_the_restore_to_the_library():
    """The library's own reset_path runs on the happy path; ours must not double up."""
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes="DEN")
    radio.add_contact(KEY_A, out_path_len=-1)
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert radio.reset_path_calls == []


async def test_cancellation_schedules_the_repair_and_still_propagates():
    """The cycle budget cancels mid-request; the repair must outlive us."""
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio()
    radio.add_contact(KEY_A, out_path_len=-1)

    async def cancelled(pubkey, timeout=0, min_timeout=0):
        raise asyncio.CancelledError

    radio.commands.req_regions_sync = cancelled
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    with pytest.raises(asyncio.CancelledError):
        await nb.collect_scopes(radio, entries, cfg, LOGGER)
    # Scheduled as an independent task, so it has not run yet.
    assert radio.reset_path_calls == []
    await asyncio.sleep(0)
    assert radio.reset_path_calls == [KEY_A]


async def test_collect_scopes_never_populates_the_contact_cache():
    """Zero-hop probing depends on the neighbour not being a known contact.

    Nothing in this module may add one; the bot's own contact management is a
    separate concern and is why scope collection defaults off.
    """
    cfg = nb.NeighborsConfig(collect_scopes=True, scope_gap=0)
    radio = FakeRadio(scopes="DEN")
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)]
    await nb.collect_scopes(radio, entries, cfg, LOGGER)
    assert radio.contacts == {}


async def test_collect_scopes_handles_no_entries():
    await nb.collect_scopes(FakeRadio(), [], nb.NeighborsConfig(collect_scopes=True), LOGGER)


# ---------------------------------------------------------------------------
# Self scopes
# ---------------------------------------------------------------------------

async def test_self_scopes_override_wins_even_when_stage_two_is_off():
    cfg = nb.NeighborsConfig(collect_scopes=False, self_scopes="OVERRIDE")
    assert await nb.fetch_self_scopes(FakeRadio(), cfg, LOGGER) == "OVERRIDE"


async def test_self_scopes_skipped_when_stage_two_is_off():
    """No scope is published for anyone, so spending a command on ours is wrong."""
    radio = FakeRadio(flood_scope="SEA")
    cfg = nb.NeighborsConfig(collect_scopes=False)
    assert await nb.fetch_self_scopes(radio, cfg, LOGGER) == ""


async def test_self_scopes_read_from_device_when_stage_two_is_on():
    radio = FakeRadio(flood_scope="SEA")
    cfg = nb.NeighborsConfig(collect_scopes=True)
    assert await nb.fetch_self_scopes(radio, cfg, LOGGER) == "SEA"


async def test_self_scopes_tolerates_a_build_without_the_command():
    radio = FakeRadio()
    radio.commands = types.SimpleNamespace()
    cfg = nb.NeighborsConfig(collect_scopes=True)
    assert await nb.fetch_self_scopes(radio, cfg, LOGGER) == ""


async def test_self_scopes_tolerates_a_missing_device_handle():
    cfg = nb.NeighborsConfig(collect_scopes=True)
    assert await nb.fetch_self_scopes(types.SimpleNamespace(commands=None), cfg, LOGGER) == ""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """A real file-based database at the current schema version.

    File-based, not :memory:, because each in-memory connection would be a
    separate database (matching the project's test_db fixture).
    """
    path = tmp_path / "neighbors.db"
    conn = sqlite3.connect(path)
    MigrationRunner(conn, LOGGER).run()
    conn.close()

    class Manager:
        @contextlib.contextmanager
        def connection(self):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    return Manager()


def _link_rows(db):
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM neighbor_links")]


def test_record_neighbors_writes_history_and_aggregate(db):
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=7.5, heard_at=0.0,
                                status=nb.STATUS_RESPONDED)]
    assert nb.record_neighbors(db, SELF_KEY, entries, LOGGER, observed_at="T1") == 1

    rows = _link_rows(db)
    assert len(rows) == 1
    assert rows[0]["self_public_key"] == SELF_KEY
    assert rows[0]["neighbor_public_key"] == KEY_A
    assert rows[0]["observation_count"] == 1
    assert rows[0]["best_snr"] == 7.5

    with db.connection() as conn:
        history = [dict(r) for r in conn.execute("SELECT * FROM neighbor_observations")]
    assert len(history) == 1
    assert history[0]["observed_at"] == "T1"
    assert history[0]["status"] == nb.STATUS_RESPONDED


def test_record_neighbors_accumulates_snr_as_sum_and_count(db):
    """Sums, not means, so any later window re-aggregates exactly."""
    for snr, stamp in ((5.0, "T1"), (9.0, "T2"), (3.0, "T3")):
        nb.record_neighbors(
            db, SELF_KEY,
            [nb.NeighborEntry(pubkey=KEY_A, snr=snr, heard_at=0.0)],
            LOGGER, observed_at=stamp,
        )
    row = _link_rows(db)[0]
    assert row["observation_count"] == 3
    assert row["snr_sum"] == pytest.approx(17.0)
    assert row["snr_count"] == 3
    assert row["snr_sum"] / row["snr_count"] == pytest.approx(17.0 / 3)
    assert row["best_snr"] == 9.0
    assert row["last_snr"] == 3.0
    assert row["first_seen"] == "T1"
    assert row["last_seen"] == "T3"


def test_record_neighbors_keeps_previously_learned_scopes(db):
    """A stage-2-disabled cycle must not erase scopes an earlier cycle learned."""
    nb.record_neighbors(
        db, SELF_KEY,
        [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0, scopes="DEN,APRS")],
        LOGGER, observed_at="T1",
    )
    nb.record_neighbors(
        db, SELF_KEY,
        [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0, scopes="")],
        LOGGER, observed_at="T2",
    )
    assert _link_rows(db)[0]["scopes"] == "DEN,APRS"


def test_record_neighbors_stores_keys_lowercase(db):
    nb.record_neighbors(
        db, SELF_KEY.upper(),
        [nb.NeighborEntry(pubkey=KEY_A.upper(), snr=1.0, heard_at=0.0)],
        LOGGER,
    )
    row = _link_rows(db)[0]
    assert row["self_public_key"] == SELF_KEY
    assert row["neighbor_public_key"] == KEY_A


def test_record_neighbors_noops_without_entries_or_self_key(db):
    assert nb.record_neighbors(db, SELF_KEY, [], LOGGER) == 0
    assert nb.record_neighbors(
        db, "", [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)], LOGGER
    ) == 0
    assert _link_rows(db) == []


def test_record_neighbors_reports_zero_on_database_error(tmp_path):
    class Broken:
        @contextlib.contextmanager
        def connection(self):
            raise sqlite3.OperationalError("database is locked")
            yield  # pragma: no cover

    written = nb.record_neighbors(
        Broken(), SELF_KEY,
        [nb.NeighborEntry(pubkey=KEY_A, snr=1.0, heard_at=0.0)], LOGGER,
    )
    assert written == 0


def _zero_hop_rows(db):
    with db.connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM observed_paths WHERE path_length = 0"
        )]


def test_record_neighbors_upserts_zero_hop_observed_path(db):
    entries = [nb.NeighborEntry(pubkey=KEY_A, snr=7.5, heard_at=0.0,
                                status=nb.STATUS_RESPONDED)]
    assert nb.record_neighbors(db, SELF_KEY, entries, LOGGER) == 1
    rows = _zero_hop_rows(db)
    assert len(rows) == 1
    assert rows[0]["public_key"] == KEY_A
    assert rows[0]["path_hex"] == ""
    assert rows[0]["path_length"] == 0
    assert rows[0]["snr"] == 7.5
    assert rows[0]["rssi"] is None


def test_upsert_zero_hop_discover_does_not_clear_rssi(db):
    with db.connection() as conn:
        nb.upsert_zero_hop_observed_path(
            conn.cursor(), KEY_A, snr=4.0, rssi=-80.0, update_rssi=True
        )
        conn.commit()
        nb.upsert_zero_hop_observed_path(
            conn.cursor(), KEY_A, snr=9.0, update_rssi=False
        )
        conn.commit()
    rows = _zero_hop_rows(db)
    assert len(rows) == 1
    assert rows[0]["snr"] == 9.0
    assert rows[0]["rssi"] == -80.0
    assert rows[0]["observation_count"] == 2


def test_upsert_zero_hop_preserves_signal_when_refresh_has_none(db):
    """A later empty-path advert without RF figures must not NULL the row."""
    with db.connection() as conn:
        nb.upsert_zero_hop_observed_path(
            conn.cursor(), KEY_A, snr=4.0, rssi=-80.0, update_rssi=True
        )
        conn.commit()
        nb.upsert_zero_hop_observed_path(
            conn.cursor(), KEY_A, snr=None, rssi=None, update_rssi=True
        )
        conn.commit()
    rows = _zero_hop_rows(db)
    assert rows[0]["snr"] == 4.0
    assert rows[0]["rssi"] == -80.0
    assert rows[0]["observation_count"] == 2
