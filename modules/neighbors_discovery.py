#!/usr/bin/env python3
"""Zero-hop neighbor discovery and scope collection.

Ported from the ``meshcore-packet-capture`` project's ``neighbors.py``, which is
itself a port of the observer firmware's ``WITH_MQTT_NEIGHBORS`` feature (see
``examples/simple_repeater/MyMesh.cpp``). Two stages per cycle:

1. A zero-hop node-discover request; repeaters answer with their pubkey and the
   SNR we heard them at. Responses are collected for a fixed window.
2. One anon-regions request per discovered neighbor, yielding that neighbor's
   scope names.

Both requests are issued through meshcore_py (``send_node_discover_req`` and
``req_regions_sync``); nothing here re-encodes packets.

Stage 1 is the valuable part for this bot: it produces a *confirmed direct RF
link* between two full 32-byte public keys, with a measured SNR. That is
stronger evidence than anything path inference can offer, and it costs one radio
command plus a passive listen window.

Stage 2 (scopes) is deliberately optional here and defaults off, for a reason
that does not apply to the upstream capture tool: upstream relies on a freshly
discovered neighbor *not* being a known contact, which is what makes
``send_anon_req`` ask for a zero-hop reply path. This bot populates the
library's contact cache. For a contact with no path (``out_path_len == -1``,
the common case for a flood repeater) the library reaches zero-hop by calling
``change_contact_path()`` and then ``reset_path()`` -- i.e. it *mutates the
device's contact table* per neighbor. Those two calls are not paired by
``try``/``finally`` upstream, so a request cut short between them leaves the
contact pinned to zero-hop; collect_scopes restores it itself (see
``_restore_flood_path``).

No device-command lock is passed around: unlike upstream, every frame
``meshcore.commands`` writes is already serialized and paced by
``modules.core._serialize_command_frames``. The lock covers each frame and its
immediate reply only, so waiting for a neighbor's scope reply doesn't stall the
bot's other sends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from meshcore import EventType

from .enums import AdvertFlags

# The firmware discovers repeaters only. The request filter is a bitmask over
# advert *types*, so the bit index is the type value (2 -> 0x04).
DISCOVER_FILTER_REPEATER = 1 << AdvertFlags.ADV_TYPE_REPEATER.value

# Matches MQTTBridge::NEIGHBORS_JSON_BUFFER_SIZE. Entries past this budget are
# dropped from the tail so the payload stays within the firmware's contract.
NEIGHBORS_JSON_BUDGET = 10240

STATUS_RESPONDED = "responded"
STATUS_TIMEOUT = "timeout"
STATUS_SEND_FAILED = "send_failed"

# Firmware interval band (MQTTPrefsStorage.h).
MIN_INTERVAL_HOURS = 12
MAX_INTERVAL_HOURS = 336
DEFAULT_INTERVAL_HOURS = 24

# Minimum wall time between cycles that reach the radio, whichever trigger asks.
# A cycle broadcasts a discover request and draws a reply from every direct
# neighbour, so the cost being rationed belongs to the whole mesh rather than to
# one caller. The neighbors command advertises this as its cooldown.
MIN_CYCLE_GAP_SECONDS = 900.0

# Floors that keep a misconfiguration from producing an empty snapshot forever.
# Repeaters answer a discover request after a randomised delay, so a very short
# window collects nothing at all.
MIN_DISCOVER_WINDOW = 5.0
MIN_CYCLE_TIMEOUT = 10.0
MIN_COMMAND_TIMEOUT = 1.0

# meshcore_py's CommandHandlerBase.DEFAULT_TIMEOUT: how long req_regions_sync
# waits for MSG_SENT before it even begins waiting for the response.
LIBRARY_MSG_SENT_TIMEOUT = 15.0

# A contact with no stored path. This is the value the library both tests for
# before pinning a contact to zero-hop and restores afterwards.
CONTACT_NO_PATH = -1

# Empty-path advert rows in observed_paths: both endpoints are the originator.
# 3-byte prefixes match the mesh graph's neighbor-evidence width.
ZERO_HOP_PATH_HEX = ""
ZERO_HOP_PREFIX_HEX_CHARS = 6


def clamp_interval_hours(hours: int) -> int:
    """Clamp to the firmware's 12-336h band, falling back to the 24h default."""
    if hours <= 0:
        return DEFAULT_INTERVAL_HOURS
    return max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, hours))


@dataclass
class NeighborsConfig:
    """Tuning for one neighbors cycle. Defaults mirror the firmware."""

    interval_hours: int = DEFAULT_INTERVAL_HOURS
    discover_window: float = 60.0     # stage 1 collection window
    command_timeout: float = 20.0     # cap on the discover request itself
    collect_scopes: bool = False      # stage 2 opt-in (bot-only; see module docstring)
    scope_timeout: float = 0.0        # 0 = let the device suggest it
    scope_min_timeout: float = 8.0    # floor under the suggested timeout
    scope_gap: float = 2.0            # settle delay between scope requests
    cycle_timeout: float = 600.0      # overall budget for the scope pass
    max_neighbors: int = 32
    self_scopes: str = ""             # explicit override; "" = ask the device

    def __post_init__(self) -> None:
        # Values that would silently produce a permanently empty snapshot get a
        # floor rather than being honoured: a 0s discover window collects nothing,
        # and max_neighbors = 0 queries nobody.
        self.interval_hours = clamp_interval_hours(self.interval_hours)
        if self.discover_window < MIN_DISCOVER_WINDOW:
            self.discover_window = MIN_DISCOVER_WINDOW
        if self.max_neighbors < 1:
            self.max_neighbors = 1
        if self.cycle_timeout < MIN_CYCLE_TIMEOUT:
            self.cycle_timeout = MIN_CYCLE_TIMEOUT
        if self.scope_gap < 0:
            self.scope_gap = 0.0
        # wait_for(timeout=0) raises immediately, so 0 here would break every
        # cycle forever -- and it reads as "no cap" by analogy with scope_timeout.
        if self.command_timeout < MIN_COMMAND_TIMEOUT:
            self.command_timeout = MIN_COMMAND_TIMEOUT

    @property
    def scope_request_budget(self) -> float:
        """Hard ceiling on one scope request.

        req_regions_sync waits for MSG_SENT (the library default, 15s) and then
        for the response, so the budget has to exceed both or healthy requests
        would be cut off.
        """
        wait = self.scope_timeout if self.scope_timeout > 0 else self.scope_min_timeout
        return LIBRARY_MSG_SENT_TIMEOUT + max(wait, self.scope_min_timeout) + self.command_timeout

    @property
    def interval_seconds(self) -> float:
        return self.interval_hours * 3600.0

    @property
    def cycle_budget(self) -> float:
        """Worst-case wall time for one full cycle, for callers that bound it.

        Covers stage 1, the scope pass, and the self-scope query. Used by the
        manual trigger so a stalled link cannot hang the caller indefinitely.
        """
        budget = self.discover_window + self.command_timeout + self.command_timeout
        if self.collect_scopes:
            budget += self.cycle_timeout + self.scope_request_budget + self.scope_gap
        return budget


@dataclass
class NeighborEntry:
    """One discovered neighbor. Snapshotted so later table changes can't alter it."""

    pubkey: str
    snr: float
    heard_at: float                   # wall clock of the response
    scopes: str = ""
    status: str = STATUS_TIMEOUT

    def heard_secs_ago(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        return max(0, int(now - self.heard_at))


def sort_key(entry: NeighborEntry, now: Optional[float] = None) -> tuple[int, float, str]:
    """Firmware ordering: most recently heard, then stronger SNR, then pubkey.

    Mirrors neighborPublishEntryComesBefore() and the pre-query sort added in
    firmware commit aba571ed, so query order and publish order agree -- a
    truncated cycle still covers the most useful neighbors.

    Recency is compared as the published heard_secs_ago, ascending -- the exact
    value the firmware's comparator uses. Sorting on the raw float clock instead
    would quantise differently from the field we publish, so the output could be
    non-monotonic in heard_secs_ago, and SNR would never break a tie: the most
    recent response would always win outright. That matters because this order
    decides which entries survive the payload budget.
    """
    return (entry.heard_secs_ago(now), -entry.snr, entry.pubkey)


def sort_entries(entries: list[NeighborEntry],
                 now: Optional[float] = None) -> list[NeighborEntry]:
    # One `now` for the whole sort, and the same one the payload uses, so the
    # published heard_secs_ago values are ordered exactly as sorted.
    now = time.time() if now is None else now
    return sorted(entries, key=lambda e: sort_key(e, now))


async def discover_neighbors(
    meshcore: Any,
    cfg: NeighborsConfig,
    self_pubkey: Optional[str],
    logger: logging.Logger,
    *,
    debug: bool = False,
    still_valid: Optional[Callable[[], bool]] = None,
) -> Optional[list[NeighborEntry]]:
    """Stage 1: zero-hop node-discover, collecting responses for the window.

    Returns the discovered entries (possibly empty), or None if the request
    could not be sent or the session was invalidated mid-window.

    ``still_valid`` is an optional predicate that must keep returning True for
    the collected data to mean anything. A reconnect part-way through the window
    tears down every event subscription (the service clears them wholesale), so
    our response handler silently stops firing -- without this check the cycle
    would happily record "0 neighbours" as though the mesh were empty.
    """
    collected: dict[str, NeighborEntry] = {}
    self_key = (self_pubkey or "").lower()
    # EventDispatcher spawns async callbacks as background tasks, so an event
    # already dequeued can still reach the handler after unsubscribe() returns.
    # This latch keeps a straggler from mutating entries we have already returned.
    closed = False

    # Generate the tag ourselves rather than letting the library pick one, so it is
    # known *before* the subscription goes live. Otherwise a response arriving
    # between subscribe and send-completion would be accepted with no tag check --
    # which is how a stale round, or another client's round, could leak in.
    # DISCOVER_RESPONSE reports the tag as little-endian hex.
    tag = random.randint(1, 0xFFFFFFFF)
    expected_tag = tag.to_bytes(4, "little").hex()

    async def on_discover_response(event: Any) -> None:
        if closed:
            return
        payload = getattr(event, "payload", None) or {}
        pubkey = str(payload.get("pubkey", "")).lower()
        # Full 32-byte pubkeys only; the firmware rejects short prefixes too.
        if len(pubkey) != 64:
            return
        if self_key and pubkey == self_key:
            return
        if payload.get("node_type") != AdvertFlags.ADV_TYPE_REPEATER.value:
            return
        if str(payload.get("tag", "")).lower() != expected_tag:
            return

        snr = float(payload.get("SNR", 0) or 0)
        existing = collected.get(pubkey)
        if existing is None:
            collected[pubkey] = NeighborEntry(pubkey=pubkey, snr=snr, heard_at=time.time())
        else:
            # Same neighbor heard again (repeaters delay responses randomly).
            # putNeighbour() refreshes the timestamp on every response, so do
            # that unconditionally; keep the strongest SNR seen.
            existing.heard_at = time.time()
            existing.snr = max(existing.snr, snr)

    # Subscribe per-run rather than at startup: the service drops every
    # subscription on reconnect, and this handler is only wanted for the
    # duration of the window.
    subscription = meshcore.subscribe(EventType.DISCOVER_RESPONSE, on_discover_response)
    try:
        # Bounded: on a stalled link the underlying BLE/serial write can block far
        # longer than the library's own response timeout, and an unbounded wait
        # here stalls the whole cycle.
        try:
            result = await asyncio.wait_for(
                meshcore.commands.send_node_discover_req(
                    DISCOVER_FILTER_REPEATER,
                    prefix_only=False,   # we need the full 32-byte pubkey
                    tag=tag,
                ),
                timeout=cfg.command_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Neighbors: node-discover request did not complete within "
                f"{cfg.command_timeout:.0f}s, abandoning this cycle"
            )
            return None

        if result is None or result.type == EventType.ERROR:
            reason = ""
            if result is not None:
                reason = (getattr(result, "payload", None) or {}).get("reason", "")
            # Logged at debug: the caller owns the user-facing message, because on a
            # build that lacks the command this fails every cycle forever.
            logger.debug(
                f"Neighbors: node-discover request failed{f' ({reason})' if reason else ''}"
            )
            return None

        if debug:
            logger.debug(
                f"Neighbors: node-discover sent (tag={expected_tag}), "
                f"collecting for {cfg.discover_window:.0f}s"
            )
        await asyncio.sleep(cfg.discover_window)

        if still_valid is not None and not still_valid():
            logger.warning(
                "Neighbors: device session was reset during the discovery window "
                "(event subscriptions are torn down on reconnect), abandoning this cycle"
            )
            return None
    finally:
        closed = True
        try:
            meshcore.unsubscribe(subscription)
        except Exception as exc:
            logger.debug(f"Neighbors: error unsubscribing discover handler: {exc}")

    return sort_entries(list(collected.values()))


def _contact_has_no_path(meshcore: Any, pubkey: str) -> bool:
    """True when *pubkey* is a known contact the library will pin to zero-hop.

    That is the one case where ``send_anon_req`` mutates the device's contact
    table (``out_path_len == -1`` -> zero-hop, restored after the send). An
    unknown contact, or one with a real stored path, is left alone.
    """
    try:
        contact = meshcore.get_contact_by_key_prefix(pubkey.lower())
    except Exception:
        # A stubbed or older client without the lookup: assume no mutation
        # rather than "restoring" a path we know nothing about.
        return False
    if not isinstance(contact, dict):
        return False
    return contact.get("out_path_len", CONTACT_NO_PATH) == CONTACT_NO_PATH


def _event_error_reason(event: Any) -> Optional[str]:
    """The failure reason when *event* is an ERROR event, else None.

    Device commands report a rejection -- and their own response timeout -- as an
    ERROR event rather than an exception, so a returned event has to be inspected
    before the command can be called successful.
    """
    if event is None or getattr(event, "type", None) != EventType.ERROR:
        return None
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        reason = payload.get("reason") or payload.get("error")
        if reason:
            return str(reason)
    return "error"


async def _restore_flood_path(meshcore: Any, pubkey: str,
                              logger: logging.Logger) -> bool:
    """Put a contact back to "no path" after an interrupted scope request.

    ``send_anon_req`` sets the zero-hop path and restores it *after* the send,
    with no ``try``/``finally``, and one of its error paths returns between the
    two. Either way the contact stays pinned to zero-hop on the device and every
    later message to it is sent direct-only, so we repair it here instead.

    Returns True when the device confirmed the reset. A failure is worth a
    warning rather than a retry: the operator needs to know that contact's
    routing is wrong, and hammering an unresponsive device does not help.
    """
    try:
        event = await meshcore.commands.reset_path(pubkey)
    except Exception as exc:
        logger.warning(
            f"Neighbors: could not restore the flood path for {pubkey[:12]} "
            f"after an interrupted scope request ({exc}); it may be left "
            f"pinned to zero-hop on the device"
        )
        return False

    reason = _event_error_reason(event)
    if reason is not None:
        logger.warning(
            f"Neighbors: the device rejected the flood-path restore for "
            f"{pubkey[:12]} ({reason}); it may be left pinned to zero-hop, "
            f"so messages to it will be sent direct-only"
        )
        return False

    logger.info(
        f"Neighbors: restored flood path for {pubkey[:12]} after an "
        f"interrupted scope request"
    )
    return True


def _restore_flood_path_detached(meshcore: Any, pubkey: str,
                                 logger: logging.Logger) -> None:
    """Schedule the repair as its own task, for use while being cancelled.

    Awaiting inside a ``except CancelledError`` block cannot work — the next
    suspension point re-raises — so the repair has to outlive this coroutine.
    """
    try:
        task = asyncio.create_task(_restore_flood_path(meshcore, pubkey, logger))
    except RuntimeError as exc:
        # No running loop (shutdown): nothing left to repair with.
        logger.warning(
            f"Neighbors: {pubkey[:12]} may be left pinned to zero-hop; "
            f"could not schedule a repair ({exc})"
        )
        return
    # Nothing awaits this task, so make sure a failure is not swallowed silently.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def collect_scopes(
    meshcore: Any,
    entries: list[NeighborEntry],
    cfg: NeighborsConfig,
    logger: logging.Logger,
    *,
    debug: bool = False,
) -> None:
    """Stage 2: one anon-regions request per neighbor, paced, updating in place.

    Entries must already be sorted (see sort_entries) so that if the cycle
    budget runs out the most useful neighbors have been covered. Anything not
    reached keeps its initial ``timeout`` status, matching the firmware's
    fallback.

    Opt-in only -- see the module docstring for why. In particular, for a
    neighbor that *is* a known contact with no stored path, the library reaches
    zero-hop by temporarily rewriting that contact's path on the device.

    This is the single entry point for stage 2, including when it is disabled,
    so that the meaning of ``status`` is decided in exactly one place.
    """
    if not entries:
        return

    if not cfg.collect_scopes:
        # Stage 2 disabled. Every entry here answered the discover request, and
        # that reception is the entire claim the snapshot makes, so `responded`
        # is accurate -- whereas leaving the `timeout` default would report a
        # live neighbor as unreachable. Scopes stay empty.
        for entry in entries:
            entry.status = STATUS_RESPONDED
        if debug:
            logger.debug(
                f"Neighbors: scope collection disabled, reporting "
                f"{len(entries)} neighbor(s) without scopes"
            )
        return

    deadline = time.time() + cfg.cycle_timeout
    # 0 means "let the device decide": req_regions_sync derives the wait from the
    # suggested_timeout the radio returns, which is its own airtime estimate.
    timeout = cfg.scope_timeout if cfg.scope_timeout > 0 else 0

    for index, entry in enumerate(entries):
        if time.time() >= deadline:
            dropped = len(entries) - index
            logger.warning(
                f"Neighbors: cycle budget ({cfg.cycle_timeout:.0f}s) reached, "
                f"{dropped} of {len(entries)} neighbor(s) left unqueried (reported as timeout)"
            )
            break

        # Settle gap between requests, standing in for the firmware's
        # wait-for-TX-completion gating.
        if index > 0 and cfg.scope_gap > 0:
            await asyncio.sleep(cfg.scope_gap)

        # Only a known contact with no stored path gets rewritten by the library,
        # and only then does an interrupted request need repairing (see
        # _restore_flood_path). Read it before the request, because the library
        # updates the same dict in place.
        pinned_to_zero_hop = _contact_has_no_path(meshcore, entry.pubkey)

        try:
            # Bounded: a hung write holds the shared radio command lock, and an
            # unbounded stall here would block every other bot command for as
            # long as the write hangs.
            scopes = await asyncio.wait_for(
                meshcore.commands.req_regions_sync(
                    entry.pubkey,
                    timeout=timeout,
                    min_timeout=cfg.scope_min_timeout,
                ),
                timeout=cfg.scope_request_budget,
            )
        except asyncio.CancelledError:
            if pinned_to_zero_hop:
                _restore_flood_path_detached(meshcore, entry.pubkey, logger)
            raise
        except asyncio.TimeoutError:
            entry.status = STATUS_SEND_FAILED
            logger.warning(
                f"Neighbors: scope request to {entry.pubkey[:12]} exceeded "
                f"{cfg.scope_request_budget:.0f}s; the device link may be stalled"
            )
            if pinned_to_zero_hop:
                await _restore_flood_path(meshcore, entry.pubkey, logger)
            continue
        except Exception as exc:
            entry.status = STATUS_SEND_FAILED
            logger.debug(f"Neighbors: scope request to {entry.pubkey[:12]} failed: {exc}")
            if pinned_to_zero_hop:
                await _restore_flood_path(meshcore, entry.pubkey, logger)
            continue

        if scopes is None:
            # req_regions_sync collapses every failure to None: a real timeout,
            # but also a device-level send rejection. We report timeout, which is
            # the firmware's own fallback for anything that isn't a clean send
            # failure or response.
            entry.status = STATUS_TIMEOUT
            if debug:
                logger.debug(f"Neighbors: no scope response from {entry.pubkey[:12]}")
            # One of those collapsed failures is send_anon_req giving up because
            # change_contact_path returned an error -- which it also does when the
            # device *applied* the zero-hop path but its acknowledgement was lost.
            # That returns before the library's own reset_path, so repair it here
            # too. On the far more common "neighbour just did not answer" path the
            # library has already restored the path and this is a redundant device
            # command: no airtime, idempotent, and it re-syncs the contact cache.
            if pinned_to_zero_hop:
                await _restore_flood_path(meshcore, entry.pubkey, logger)
            continue

        entry.scopes = str(scopes).strip()
        entry.status = STATUS_RESPONDED
        if debug:
            logger.debug(
                f"Neighbors: {entry.pubkey[:12]} scopes="
                f"{entry.scopes if entry.scopes else '(none)'}"
            )


async def fetch_self_scopes(meshcore: Any, cfg: NeighborsConfig,
                            logger: logging.Logger) -> str:
    """This node's own scope names for the message's ``self`` object.

    A companion radio has no region_map, so the closest analogue to the
    firmware's exportNamesTo(REGION_DENY_FLOOD) is the default flood scope name.
    ``neighbors_self_scopes`` overrides it outright and is honoured even when
    stage 2 is disabled.
    """
    if cfg.self_scopes:
        return cfg.self_scopes

    # With stage 2 off the snapshot reports no scopes for anyone, so spending a
    # radio command to learn our own would be inconsistent as well as wasteful:
    # the default cycle is meant to cost one command plus a listen window.
    if not cfg.collect_scopes:
        return ""

    # A reconnect can null out the device handle mid-cycle (a cycle spans minutes).
    commands = getattr(meshcore, "commands", None)
    getter = getattr(commands, "get_default_flood_scope", None)
    if not callable(getter):
        return ""

    try:
        result = await asyncio.wait_for(getter(), timeout=cfg.command_timeout)
    except Exception as exc:
        logger.debug(f"Neighbors: could not read default flood scope: {exc}")
        return ""

    if result is None or result.type == EventType.ERROR:
        return ""
    return str((getattr(result, "payload", None) or {}).get("scope_name", "") or "").strip()


def _observed_paths_has_signal_columns(cursor: Any) -> bool:
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(observed_paths)")}
    return "snr" in cols and "rssi" in cols


def upsert_zero_hop_observed_path(
    cursor: Any,
    public_key: str,
    *,
    snr: Optional[float] = None,
    rssi: Optional[float] = None,
    bytes_per_hop: int = 1,
    packet_hash: Optional[str] = None,
    last_seen: Optional[str] = None,
    update_rssi: bool = True,
) -> None:
    """Insert or refresh a direct-RF (empty path) advert row in observed_paths.

    Discover responses carry SNR only, so ``update_rssi=False`` leaves a
    previously stored RSSI from a zero-path advert in place. A later reception
    with no measurement must not NULL out a stored figure either: COALESCE
    keeps the existing column when the new value is None.
    """
    key = (public_key or "").strip().lower()
    if len(key) < 2:
        return
    tables = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='observed_paths'"
        )
    }
    if "observed_paths" not in tables:
        return

    prefix = key[:ZERO_HOP_PREFIX_HEX_CHARS]
    stamp = last_seen or datetime.now().isoformat()
    stored_hash = packet_hash if (packet_hash and packet_hash != "0000000000000000") else None
    has_signal = _observed_paths_has_signal_columns(cursor)

    existing = cursor.execute(
        """
        SELECT id, observation_count FROM observed_paths
        WHERE public_key = ? AND path_hex = ? AND packet_type = 'advert'
        """,
        (key, ZERO_HOP_PATH_HEX),
    ).fetchone()

    if existing:
        path_id = existing["id"] if not isinstance(existing, tuple) else existing[0]
        count = (existing["observation_count"] if not isinstance(existing, tuple) else existing[1]) or 1
        if has_signal:
            cursor.execute(
                """
                UPDATE observed_paths
                SET observation_count = ?,
                    last_seen = ?,
                    snr = COALESCE(?, snr),
                    rssi = CASE WHEN ? THEN COALESCE(?, rssi) ELSE rssi END
                WHERE id = ?
                """,
                (count + 1, stamp, snr, 1 if update_rssi else 0, rssi, path_id),
            )
        else:
            cursor.execute(
                """
                UPDATE observed_paths
                SET observation_count = ?, last_seen = ?
                WHERE id = ?
                """,
                (count + 1, stamp, path_id),
            )
        return

    if has_signal:
        cursor.execute(
            """
            INSERT INTO observed_paths
                (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length,
                 bytes_per_hop, packet_type, first_seen, last_seen, observation_count,
                 snr, rssi)
            VALUES (?, ?, ?, ?, ?, 0, ?, 'advert', ?, ?, 1, ?, ?)
            """,
            (key, stored_hash, prefix, prefix, ZERO_HOP_PATH_HEX, bytes_per_hop,
             stamp, stamp, snr, rssi if update_rssi else None),
        )
    else:
        cursor.execute(
            """
            INSERT INTO observed_paths
                (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length,
                 bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
            VALUES (?, ?, ?, ?, ?, 0, ?, 'advert', ?, ?, 1)
            """,
            (key, stored_hash, prefix, prefix, ZERO_HOP_PATH_HEX, bytes_per_hop,
             stamp, stamp),
        )


def upsert_zero_hop_observed_path_via_manager(
    db_manager: Any,
    public_key: str,
    logger: logging.Logger,
    **kwargs: Any,
) -> None:
    """Open a connection, upsert one zero-hop row, and commit."""
    if db_manager is None or not hasattr(db_manager, "connection"):
        return
    try:
        with db_manager.connection() as conn:
            upsert_zero_hop_observed_path(conn.cursor(), public_key, **kwargs)
            conn.commit()
    except Exception as exc:
        logger.debug(f"Could not store zero-hop observed path: {exc}")


def record_neighbors(
    db_manager: Any,
    self_pubkey: str,
    entries: list[NeighborEntry],
    logger: logging.Logger,
    *,
    observed_at: Optional[str] = None,
) -> int:
    """Persist one cycle to ``neighbor_observations`` and ``neighbor_links``.

    Returns the number of links written. Only entries we actually heard are
    recorded: an entry left at ``timeout`` because the scope pass ran out of
    budget was still a real zero-hop reception, but one that never answered
    discovery never reaches this function at all.

    SNR is accumulated as sum+count rather than a running mean so that any later
    window can re-aggregate exactly (the ``daily_rollup`` convention).
    """
    if not entries or not self_pubkey:
        return 0

    self_key = self_pubkey.lower()
    stamp = observed_at or datetime.now(timezone.utc).isoformat()
    written = 0

    try:
        with db_manager.connection() as conn:
            cursor = conn.cursor()
            for entry in entries:
                cursor.execute(
                    """
                    INSERT INTO neighbor_observations
                        (observed_at, self_public_key, neighbor_public_key,
                         snr, heard_secs_ago, scopes, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (stamp, self_key, entry.pubkey.lower(), entry.snr,
                     entry.heard_secs_ago(), entry.scopes or "", entry.status),
                )
                # ON CONFLICT rather than a read-modify-write: the aggregate must
                # stay correct even if two cycles were ever to overlap.
                cursor.execute(
                    """
                    INSERT INTO neighbor_links
                        (self_public_key, neighbor_public_key, first_seen, last_seen,
                         observation_count, snr_sum, snr_count, best_snr, last_snr,
                         last_status, scopes)
                    VALUES (?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(self_public_key, neighbor_public_key) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        observation_count = observation_count + 1,
                        snr_sum = snr_sum + excluded.snr_sum,
                        snr_count = snr_count + 1,
                        best_snr = MAX(COALESCE(best_snr, excluded.best_snr), excluded.best_snr),
                        last_snr = excluded.last_snr,
                        last_status = excluded.last_status,
                        -- Keep the last non-empty scopes: a cycle with stage 2
                        -- disabled must not erase scopes an earlier cycle learned.
                        scopes = CASE
                            WHEN excluded.scopes != '' THEN excluded.scopes ELSE scopes
                        END
                    """,
                    (self_key, entry.pubkey.lower(), stamp, stamp,
                     entry.snr, entry.snr, entry.snr, entry.status, entry.scopes or ""),
                )
                # Discover is a confirmed direct RF reception: keep the dashboard
                # neighbour list in sync. SNR only — leave RSSI to zero-path adverts.
                upsert_zero_hop_observed_path(
                    cursor,
                    entry.pubkey,
                    snr=entry.snr,
                    update_rssi=False,
                )
                written += 1
            conn.commit()
    except Exception as exc:
        logger.error(f"Neighbors: could not persist snapshot: {exc}")
        return 0

    return written


def build_neighbors_message(
    origin: str,
    origin_id: str,
    self_scopes: str,
    entries: list[NeighborEntry],
    *,
    timestamp: Optional[str] = None,
    now: Optional[float] = None,
    budget: int = NEIGHBORS_JSON_BUDGET,
    total_neighbors: Optional[int] = None,
) -> tuple[dict[str, Any], int]:
    """Build the neighbors payload, dropping the tail past ``budget`` bytes.

    Matches MQTTPayloadBuilder::buildNeighborsMessage. Returns the message and
    the number of entries dropped. Key order is part of the contract.
    """
    now = time.time() if now is None else now
    # total_neighbors is how many were discovered before any max_neighbors cap;
    # queried_neighbors is how many we actually asked. Firmware key order
    # (MQTTPayloadBuilder.cpp): these sit between origin_id and self.
    queried = len(entries)
    total = queried if total_neighbors is None else total_neighbors
    message: dict[str, Any] = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "origin_id": origin_id,
        "total_neighbors": total,
        "queried_neighbors": queried,
        # Set below once we know whether the payload budget dropped a tail.
        "truncated": total > queried,
        "self": {"scopes": self_scopes or ""},
        "neighbors": [],
    }

    neighbors = message["neighbors"]
    dropped = 0
    for position, entry in enumerate(sort_entries(entries, now)):
        neighbors.append(
            {
                "pubkey": entry.pubkey.upper(),
                "snr": entry.snr,
                "heard_secs_ago": entry.heard_secs_ago(now),
                "scopes": entry.scopes or "",
                "status": entry.status,
            }
        )
        # Entries are ordered most- to least-useful, so once one overflows, drop
        # it and everything after it.
        if len(json.dumps(message)) >= budget:
            neighbors.pop()
            dropped = len(entries) - position
            break

    # Truncated covers both causes: the max_neighbors cap and the payload budget.
    message["truncated"] = bool(dropped) or total > queried
    return message, dropped
