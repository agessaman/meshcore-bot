#!/usr/bin/env python3
"""Dashboard statistics: daily rollups, the current-state snapshot, and readers.

The web viewer's landing page used to recompute ~50 aggregate queries on every
request, five times per page load.  This module moves that work off the request
path into a background refresher that writes two tables:

``daily_rollup``
    One row per local date.  Holds the metrics whose raw sources are pruned
    long before the dashboard's 30-day window — message/command/path stats at 7
    days, packet_stream at 3 — so trends outlive retention.  Signal metrics are
    stored as sums and counts, never means, so any window re-aggregates
    correctly.

``dashboard_snapshot``
    A single row of JSON describing current state, regenerated whole each tick.

Every method takes a ``sqlite3.Connection`` rather than owning one, so the
service unit-tests without Flask and the caller controls transaction scope.

Two conventions matter throughout:

* **NULL is not zero.**  A NULL column means "no source data for this day" and
  must render as a gap.  Writing 0 instead produces a fake cliff at the
  retention boundary that reads as an outage.
* **Local dates, clamped timestamps.**  Rollup dates are local to match
  ``daily_stats`` (the bot writes ``date('now','localtime')``), while the stats
  tables store UTC epochs.  Those tables also contain future-dated rows
  (observed up to year 2103), so every query clamps to now — otherwise the
  rollup grows a row for 2103 and the x-axis spans 77 years.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

SCHEMA_REV = 1

LEASE_KEY = "dashboard.snapshot_lease"
ZERO_HOP_BACKFILL_KEY = "dashboard.zero_hop_path_backfill"

# Source-presence bitmask stored on each daily_rollup row.  A missing table is
# genuinely possible: message_stats/command_stats/path_stats are created by
# stats_command, not by migrations, so `collect_stats = false` installs lack them.
SOURCE_MESSAGE_STATS = 1 << 0
SOURCE_COMMAND_STATS = 1 << 1
SOURCE_PATH_STATS = 1 << 2
SOURCE_PACKET_STREAM = 1 << 3
SOURCE_DAILY_STATS = 1 << 4
SOURCE_CONTACT_TRACKING = 1 << 5
SOURCE_OBSERVED_PATHS = 1 << 6

SOURCE_NAMES = {
    SOURCE_MESSAGE_STATS: "message_stats",
    SOURCE_COMMAND_STATS: "command_stats",
    SOURCE_PATH_STATS: "path_stats",
    SOURCE_PACKET_STREAM: "packet_stream",
    SOURCE_DAILY_STATS: "daily_stats",
    SOURCE_CONTACT_TRACKING: "complete_contact_tracking",
    SOURCE_OBSERVED_PATHS: "observed_paths",
}

# Payload types charted on the packet encoding trend, in the fixed order the
# chart assigns its categorical colours in.  A colour belongs to a type and not
# to its current rank, so a quiet day for one type must never repaint the other
# seven lines — which means this order is part of the contract with the client,
# not a display detail, and new types are appended rather than inserted.
PACKET_ENCODING_TYPES = (
    "GRP_TXT", "RESPONSE", "REQ", "PATH", "TXT_MSG", "ANON_REQ", "GRP_DATA", "ADVERT",
)

# Everything else the firmware emits — ACK, TRACE, MULTIPART, unmapped ordinals
# like 'Type11' — folded into one bucket rather than dropped.  On the live mesh
# that is 0.8% of traffic, and dropping it would leave the chart's bar heights a
# share of the charted types instead of a share of the day, disagreeing with the
# multibyte doughnut sitting on the same card.  Charting each separately instead
# would spend the palette's remaining separation on traffic nobody watches.
OTHER_PAYLOAD_TYPE = "OTHER"

# Storage/chart order.  The named types keep their colour slots and the residual
# bucket sits last, drawn in a neutral so it does not read as a ninth category.
PACKET_ENCODING_BUCKETS = (*PACKET_ENCODING_TYPES, OTHER_PAYLOAD_TYPE)


def packet_share_metric(payload_type: str) -> str:
    """Series-metric name carrying one payload type's multibyte share."""
    return f"multibyte_share_{payload_type.lower()}"


def _packet_share_sql(payload_type: str) -> str:
    """Percent of that type's classified packets that used 2- or 3-byte hops.

    Reads the JSON written by ``_packet_encoding_by_type``.  A day with no
    stored split, or none for this type, yields NULL — a gap, not a zero.
    """
    multibyte = f"""json_extract(packet_type_encoding, '$."{payload_type}".mb')"""
    total = f"""json_extract(packet_type_encoding, '$."{payload_type}".total')"""
    return (
        f"CASE WHEN COALESCE({total}, 0) > 0 "
        f"THEN ROUND(COALESCE({multibyte}, 0) * 100.0 / {total}, 1) END"
    )


# Series exposed by /api/dashboard/series and folded into the summary payload.
# The multibyte shares are ratios the UI plots on a 0-100 axis; the rest are
# counts.
SERIES_METRICS: dict[str, str] = {
    "messages": "messages_total",
    "commands": "commands_total",
    "adverts": "adverts_total",
    "nodes": "nodes_active",
    "new_nodes": "nodes_new",
    "packets": "packets_total",
    "multibyte_share": (
        "CASE WHEN COALESCE(adverts_from_multibyte, 0) + COALESCE(adverts_from_singlebyte, 0) > 0 "
        "THEN ROUND(adverts_from_multibyte * 100.0 / "
        "(adverts_from_multibyte + adverts_from_singlebyte), 1) END"
    ),
    **{
        packet_share_metric(payload_type): _packet_share_sql(payload_type)
        for payload_type in PACKET_ENCODING_TYPES
    },
}

PACKET_SHARE_METRICS = frozenset(
    packet_share_metric(payload_type) for payload_type in PACKET_ENCODING_TYPES
)

# Metrics folded into the summary payload's sparkline block.  The per-payload-
# type shares stay out of it: the dashboard chart stacks raw counts from
# ``packet_encoding`` instead, and eight independent percentages cannot be
# restacked into a composition — the shared denominator is gone.  They remain on
# /api/dashboard/series for anyone plotting one type on its own.
SUMMARY_METRICS = tuple(
    name for name in SERIES_METRICS if name not in PACKET_SHARE_METRICS
)

SUMMARY_SERIES_POINTS = 30

# Categories to show in a role/payload mix before the tail is rolled into "Other".
MIX_ROWS = 8

# MeshCore carries up to a 64-byte path, so a route can be 64 hops long at one
# byte per hop (and proportionally fewer with 2- or 3-byte hashes: 32 and 21).
# Beyond that the path field could not have held it, so the value is corrupt.
#
# Not a display convenience.  The earlier limit of 32 was inherited from the old
# dashboard's `BETWEEN 0 AND 32` filters and silently discarded 5,654 flood
# packets on the live database — genuine traffic arriving from as far as 63
# hops.  It also applies after the per-node MIN(), so any node whose *closest*
# path exceeded 32 hops vanished from the chart entirely rather than appearing
# at the far end; one such node exists in the live history.
MAX_PLOTTED_HOPS = 64

# Flood hop buckets carrying less than this share of the series are not drawn.
# The tail decays for ~20 hops in bars under a pixel tall, which reads as noise;
# the amount withheld is reported alongside the chart rather than dropped
# quietly.
FLOOD_MIN_SHARE_PCT = 0.1

# Metrics that are already a ratio: a period "total" has to be the mean of the
# daily values, not their sum — adding percentages together means nothing.
RATIO_METRICS = frozenset(
    {
        "multibyte_share",
        *(packet_share_metric(payload_type) for payload_type in PACKET_ENCODING_TYPES),
    }
)

TOP_KINDS = ("users", "commands", "channels", "paths", "repeaters", "neighbors")

# Neighbour windows are capped well below observed_paths' 90-day retention: a
# link last exercised a month ago says nothing about whether it works today.
NEIGHBOR_WINDOWS = ("24h", "7d")

# Direct RF neighbour: MeshCore hop count 0 (empty path). path_length is a
# BYTE count, so a 1-hop relayed path is path_length = bytes_per_hop — that is
# not a neighbour. Empty-path rows are the ones this radio heard itself.
ZERO_HOP_PATH = "op.path_length = 0"

# Roles the firmware reports as an unmapped enum ordinal.  They are real
# contacts, so they belong in the mix — just not as sixteen singleton slices.
_UNKNOWN_ROLE_PREFIX = "type"


def local_date_str(when: float | None = None) -> str:
    """Local YYYY-MM-DD for *when* (default now), matching date('now','localtime')."""
    return datetime.fromtimestamp(time.time() if when is None else when).strftime("%Y-%m-%d")


def day_bounds(date_str: str) -> tuple[float, float]:
    """Local-midnight epoch bounds [start, end) for *date_str*.

    Computed in Python rather than with date(timestamp,'unixepoch','localtime')
    so the comparison stays sargable against the timestamp indexes, and so DST
    transitions produce a genuine 23- or 25-hour day.
    """
    start = datetime.strptime(date_str, "%Y-%m-%d")
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not table.isidentifier():
        return False
    return any(row[1] == column for row in conn.execute(f'PRAGMA table_info("{table}")'))


def _parse_aware_utc(value: Any) -> datetime | None:
    """Parse neighbor_links.last_seen (UTC ISO, with or without offset)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _neighbor_window_cutoff_utc(window: str) -> datetime:
    days = 1 if window == "24h" else 7
    return datetime.now(timezone.utc) - timedelta(days=days)


def _top_n_with_other(entries: list[list[Any]], limit: int = MIX_ROWS) -> list[list[Any]]:
    """Keep the largest *limit* categories and roll the tail into "Other".

    Truncating the list instead would silently drop counts, leaving a chart whose
    bars no longer add up to the total printed beside it.
    """
    ranked = sorted(entries, key=lambda entry: -(entry[1] or 0))
    if len(ranked) <= limit:
        return ranked
    tail = sum(entry[1] or 0 for entry in ranked[limit:])
    return [*ranked[:limit], ["Other", tail]]


def _change_pct(current: float | None, previous: float | None) -> float | None:
    """Percent change, or None when the baseline cannot support one.

    Computed server-side precisely so the client never has to decide what
    dividing by zero means.
    """
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 1)


def normalize_role(role: str | None) -> str:
    """Map firmware role values to display buckets, folding unmapped ordinals.

    A handful of contacts report role as the raw enum ordinal ('type0'…'type15')
    rather than a name.  Charting each as its own slice buries the real roles.
    """
    text = (role or "").strip()
    if not text:
        return "Unknown"
    lowered = text.lower()
    if lowered.startswith(_UNKNOWN_ROLE_PREFIX) and lowered[len(_UNKNOWN_ROLE_PREFIX):].isdigit():
        return "Unknown"
    return lowered


def humanize_span(seconds: float | None) -> str:
    """Render a covered-window length as 'last 2d 6h' / 'last 45m'."""
    if seconds is None or seconds <= 0:
        return "no data"
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"last {days}d {hours}h" if hours else f"last {days}d"
    if hours:
        return f"last {hours}h {minutes}m" if minutes else f"last {hours}h"
    return f"last {max(1, minutes)}m"


class DashboardStatsService:
    """Computes and reads the dashboard's rollup and snapshot tables.

    ``multibyte_contacts_fn`` is injected by the app so the service can reuse
    the viewer's cached hop-prefix evidence without importing Flask.  It takes a
    cursor and returns ``(multibyte_count, total_count)`` for contacts heard in
    the last 7 days, or None if it cannot be computed.
    """

    def __init__(
        self,
        logger,
        *,
        history_days: int = 400,
        packet_backfill_rows: int = 2000,
        interval_seconds: int = 60,
        stats_retention_days: int = 7,
        packet_retention_days: int = 3,
        adverts_retention_days: int = 90,
        multibyte_contacts_fn: Callable[[sqlite3.Cursor], tuple[int, int] | None] | None = None,
    ) -> None:
        self.logger = logger
        self.history_days = max(7, int(history_days))
        self.packet_backfill_rows = max(0, int(packet_backfill_rows))
        self.interval_seconds = max(5, int(interval_seconds))
        self.stats_retention_days = max(1, int(stats_retention_days))
        self.packet_retention_days = max(1, int(packet_retention_days))
        self.adverts_retention_days = max(1, int(adverts_retention_days))
        self.multibyte_contacts_fn = multibyte_contacts_fn
        # Recompute today plus a trailing window, bounded by each source's own
        # retention: recomputing a day whose raw rows were just pruned would
        # overwrite a real value with a zero.
        self.trailing_days = 3
        self._zero_hop_backfilled = False

    # -- source availability -------------------------------------------------

    def detect_sources(self, conn: sqlite3.Connection) -> int:
        """Bitmask of the source tables actually present in this database."""
        present = 0
        for bit, name in SOURCE_NAMES.items():
            if _table_exists(conn, name):
                present |= bit
        return present

    def _retention_for(self, bit: int) -> int:
        if bit == SOURCE_PACKET_STREAM:
            return self.packet_retention_days
        if bit in (SOURCE_MESSAGE_STATS, SOURCE_COMMAND_STATS, SOURCE_PATH_STATS):
            return self.stats_retention_days
        return self.adverts_retention_days

    def _recompute_dates(self, today: str) -> list[str]:
        """Dates to recompute this tick: today plus the trailing window."""
        base = datetime.strptime(today, "%Y-%m-%d")
        return [
            (base - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(self.trailing_days + 1)
        ]

    # -- packet dimension backfill ------------------------------------------

    def backfill_packet_dims(self, conn: sqlite3.Connection) -> int:
        """Populate denormalized packet columns for a bounded batch of old rows.

        Rows written before migration 0019 carry their dimensions only inside
        the JSON blob.  Widening all of them at once rewrites the whole table
        into the WAL, so this walks backwards a batch per tick instead; with
        3-day retention the backlog clears long before it matters.

        route_type_name doubles as the "already dimensioned" marker, so a row
        whose JSON has no route type is stamped UNKNOWN_ROUTE rather than left
        NULL — otherwise the same batch would be reselected on every tick and
        the backlog would never advance.

        Returns the number of rows updated.
        """
        if not self.packet_backfill_rows or not _table_exists(conn, "packet_stream"):
            return 0
        try:
            cursor = conn.execute(
                """
                UPDATE packet_stream SET
                    route_type_name   = COALESCE(json_extract(data, '$.route_type_name'),
                                                 'UNKNOWN_ROUTE'),
                    payload_type_name = json_extract(data, '$.payload_type_name'),
                    path_len          = CAST(json_extract(data, '$.path_len') AS INTEGER),
                    bytes_per_hop     = CAST(json_extract(data, '$.bytes_per_hop') AS INTEGER)
                WHERE id IN (
                    SELECT id FROM packet_stream
                    WHERE type = 'packet' AND route_type_name IS NULL
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (self.packet_backfill_rows,),
            )
            return cursor.rowcount or 0
        except sqlite3.OperationalError as exc:
            # Older SQLite builds without the JSON1 extension, or a viewer DB
            # that predates the migration.  Packet tiles degrade to "unknown".
            self.logger.debug(f"packet_stream dimension backfill unavailable: {exc}")
            return 0

    @staticmethod
    def _advert_identity_from_packet_json(
        data: dict[str, Any],
    ) -> tuple[str | None, float | None, float | None]:
        """Recover originator key and RF signal from a packet_stream JSON blob."""
        pk = data.get("advert_public_key")
        if not (isinstance(pk, str) and len(pk) >= 64):
            hex_str = str(data.get("payload_hex") or "").replace("0x", "").replace(" ", "")
            pk = hex_str[:64] if len(hex_str) >= 64 else None
        if isinstance(pk, str):
            pk = pk.lower()
            if len(pk) != 64 or any(char not in "0123456789abcdef" for char in pk):
                pk = None
        else:
            pk = None

        def _num(value: Any) -> float | None:
            if value is None or value == "" or value == "Unknown":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return pk, _num(data.get("snr", data.get("SNR"))), _num(data.get("rssi", data.get("RSSI")))

    def backfill_zero_hop_from_packet_stream(self, conn: sqlite3.Connection) -> int:
        """One-time copy of zero-hop ADVERT packets into observed_paths.

        Empty-path adverts were never stored; packet_stream still has ~3 days of
        them with hop count and payload_hex. Idempotent on the advert unique
        index. SNR is taken from the JSON when present, else neighbor_links.
        """
        if self._zero_hop_backfilled:
            return 0
        if _table_exists(conn, "bot_metadata"):
            row = conn.execute(
                "SELECT value FROM bot_metadata WHERE key = ?",
                (ZERO_HOP_BACKFILL_KEY,),
            ).fetchone()
            if row:
                self._zero_hop_backfilled = True
                return 0
        if not _table_exists(conn, "packet_stream") or not _table_exists(conn, "observed_paths"):
            self._zero_hop_backfilled = True
            return 0

        from modules.neighbors_discovery import upsert_zero_hop_observed_path

        try:
            rows = conn.execute(
                """
                SELECT timestamp, data, path_len, bytes_per_hop, payload_type_name
                FROM packet_stream
                WHERE type = 'packet'
                  AND (
                    payload_type_name = 'ADVERT'
                    OR json_extract(data, '$.payload_type_name') = 'ADVERT'
                  )
                  AND COALESCE(
                    path_len,
                    CAST(json_extract(data, '$.path_len') AS INTEGER)
                  ) = 0
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            self.logger.debug(f"zero-hop packet_stream backfill unavailable: {exc}")
            return 0

        discover_snr: dict[str, float] = {}
        if _table_exists(conn, "neighbor_links"):
            for row in conn.execute(
                "SELECT neighbor_public_key, last_snr FROM neighbor_links WHERE last_snr IS NOT NULL"
            ):
                key = (row["neighbor_public_key"] or "").lower()
                if key:
                    discover_snr[key] = float(row["last_snr"])

        latest: dict[str, dict[str, Any]] = {}
        existing_keys = {
            (row[0] or "").lower()
            for row in conn.execute(
                """
                SELECT public_key FROM observed_paths
                WHERE packet_type = 'advert' AND path_length = 0 AND public_key IS NOT NULL
                """
            )
        }
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            pk, snr, rssi = self._advert_identity_from_packet_json(data)
            if not pk or pk in existing_keys:
                continue
            ts = row["timestamp"]
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            prev = latest.get(pk)
            if prev is not None and ts_f <= prev["ts"]:
                continue
            if snr is None:
                snr = discover_snr.get(pk)
            bph = row["bytes_per_hop"] or data.get("bytes_per_hop") or 1
            try:
                bph = int(bph)
            except (TypeError, ValueError):
                bph = 1
            latest[pk] = {"ts": ts_f, "snr": snr, "rssi": rssi, "bytes_per_hop": bph}

        cursor = conn.cursor()
        for pk, info in latest.items():
            upsert_zero_hop_observed_path(
                cursor,
                pk,
                snr=info["snr"],
                rssi=info["rssi"],
                bytes_per_hop=info["bytes_per_hop"] or 1,
                last_seen=datetime.fromtimestamp(info["ts"]).isoformat(),
                update_rssi=info["rssi"] is not None,
            )

        if _table_exists(conn, "bot_metadata"):
            conn.execute(
                """
                INSERT INTO bot_metadata (key, value, updated_at)
                VALUES (?, '1', CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (ZERO_HOP_BACKFILL_KEY,),
            )
        self._zero_hop_backfilled = True
        if latest:
            self.logger.info(
                "Backfilled %d zero-hop neighbour path(s) from packet_stream",
                len(latest),
            )
        return len(latest)

    def packet_coverage(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Actual time span covered by packet rows carrying denormalized dims.

        The old dashboard labelled this window "last 7 days" while packet_stream
        retention is 3 — reporting the measured span instead of a config value
        is the whole point.
        """
        coverage: dict[str, Any] = {
            "packets_from": None,
            "packets_to": None,
            "packets_window_label": "no data",
            "packets_with_dims": 0,
        }
        if not _table_exists(conn, "packet_stream"):
            return coverage
        row = conn.execute(
            """
            SELECT MIN(timestamp), MAX(timestamp), COUNT(*)
            FROM packet_stream
            WHERE type = 'packet' AND route_type_name IS NOT NULL
            """
        ).fetchone()
        if not row or not row[2]:
            return coverage
        coverage["packets_from"] = row[0]
        coverage["packets_to"] = row[1]
        coverage["packets_with_dims"] = row[2]
        coverage["packets_window_label"] = humanize_span(time.time() - row[0])
        return coverage

    # -- daily rollup --------------------------------------------------------

    def _first_advert_dates(self, conn: sqlite3.Connection) -> dict[str, str]:
        """public_key -> earliest daily_stats date, for new-node attribution.

        Computed once per refresh: the alternative is a HAVING MIN(date) scan
        per day, which the backfill would run ninety times.
        """
        if not _table_exists(conn, "daily_stats"):
            return {}
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT public_key, MIN(date) FROM daily_stats GROUP BY public_key"
            )
        }

    def _message_metrics(
        self, conn: sqlite3.Connection, start: float, end: float
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN is_dm THEN 1 ELSE 0 END),
                   SUM(CASE WHEN channel IS NOT NULL AND channel != '' THEN 1 ELSE 0 END),
                   COUNT(DISTINCT sender_id),
                   COUNT(DISTINCT channel),
                   SUM(snr),  SUM(CASE WHEN snr  IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(rssi), SUM(CASE WHEN rssi IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(hops), SUM(CASE WHEN hops IS NOT NULL THEN 1 ELSE 0 END)
            FROM message_stats
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start, end),
        ).fetchone()
        return {
            "messages_total": row[0] or 0,
            "messages_dm": row[1] or 0,
            "messages_channel": row[2] or 0,
            "unique_senders": row[3] or 0,
            "unique_channels": row[4] or 0,
            "snr_sum": row[5],
            "snr_count": row[6] or 0,
            "rssi_sum": row[7],
            "rssi_count": row[8] or 0,
            "hops_sum": row[9],
            "hops_count": row[10] or 0,
        }

    def _command_metrics(
        self, conn: sqlite3.Connection, start: float, end: float
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN response_sent THEN 1 ELSE 0 END),
                   COUNT(DISTINCT sender_id)
            FROM command_stats
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start, end),
        ).fetchone()
        return {
            "commands_total": row[0] or 0,
            "commands_replied": row[1] or 0,
            "unique_command_users": row[2] or 0,
        }

    def _path_metrics(
        self, conn: sqlite3.Connection, start: float, end: float
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT COUNT(*), MAX(path_length), SUM(path_length)
            FROM path_stats
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start, end),
        ).fetchone()
        return {
            "path_obs_total": row[0] or 0,
            "path_len_max": row[1],
            "path_len_sum": row[2],
        }

    def _packet_metrics(
        self, conn: sqlite3.Connection, start: float, end: float
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN route_type_name LIKE '%FLOOD'  THEN 1 ELSE 0 END),
                   SUM(CASE WHEN route_type_name LIKE '%DIRECT' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN bytes_per_hop IN (2, 3) THEN 1 ELSE 0 END)
            FROM packet_stream
            WHERE type = 'packet' AND timestamp >= ? AND timestamp < ?
            """,
            (start, end),
        ).fetchone()
        return {
            "packets_total": row[0] or 0,
            "packets_flood": row[1] or 0,
            "packets_direct": row[2] or 0,
            "packets_multibyte": row[3] or 0,
            "packet_type_encoding": self._packet_encoding_by_type(conn, start, end),
        }

    def _packet_encoding_by_type(
        self, conn: sqlite3.Connection, start: float, end: float
    ) -> str | None:
        """One day's multibyte split per payload type, as JSON.

        Written now because it cannot be recovered later: packet_stream is
        pruned at three days while the chart shows thirty.

        Covers *every* payload type, with the uncharted tail summed into
        ``OTHER``, because the totals here are the chart's denominator: bar
        heights are each type's multibyte packets over the day's whole traffic,
        so leaving a type out of the file would quietly inflate every bar.

        Packets whose dimensions the refresher has not backfilled yet are
        excluded from both sides rather than counted as single-byte.  On a
        count that would merely undershoot; on a ratio it invents a dip in
        whichever type the backfill has not reached, which reads as a real
        change in the mesh.
        """
        counts: dict[str, dict[str, int]] = {}
        for name, total, multibyte in conn.execute(
            """
            SELECT payload_type_name, COUNT(*),
                   SUM(CASE WHEN bytes_per_hop IN (2, 3) THEN 1 ELSE 0 END)
            FROM packet_stream
            WHERE type = 'packet' AND timestamp >= ? AND timestamp < ?
              AND bytes_per_hop IS NOT NULL
            GROUP BY payload_type_name
            """,
            (start, end),
        ):
            # A NULL type is dimensioned but unnamed, which is still a packet
            # the day's traffic contains — it belongs in the residual bucket,
            # not thrown away.
            key = name if name in PACKET_ENCODING_TYPES else OTHER_PAYLOAD_TYPE
            bucket = counts.setdefault(key, {"mb": 0, "total": 0})
            bucket["mb"] += multibyte or 0
            bucket["total"] += total or 0
        # Nothing found means "cannot say", not "the mesh was silent": a day
        # whose rows were pruned ahead of the retention window would otherwise
        # overwrite the split recorded for it while the rows still existed.
        return json.dumps(counts, separators=(",", ":")) if counts else None

    def _advert_metrics(
        self,
        conn: sqlite3.Connection,
        date_str: str,
        first_seen: dict[str, str],
        multibyte_keys: set[str] | None,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT SUM(advert_count), COUNT(DISTINCT public_key) FROM daily_stats WHERE date = ?",
            (date_str,),
        ).fetchone()
        metrics: dict[str, Any] = {
            "adverts_total": row[0] or 0,
            "nodes_active": row[1] or 0,
            "nodes_new": sum(1 for first in first_seen.values() if first == date_str),
            "adverts_from_multibyte": None,
            "adverts_from_singlebyte": None,
        }
        if multibyte_keys is None:
            return metrics
        # Classification is only knowable as of now, so this split is written
        # for the recompute window and then frozen — see the tooltip copy.
        multibyte = single = 0
        for key, count in conn.execute(
            "SELECT public_key, advert_count FROM daily_stats WHERE date = ?", (date_str,)
        ):
            if key in multibyte_keys:
                multibyte += count or 0
            else:
                single += count or 0
        metrics["adverts_from_multibyte"] = multibyte
        metrics["adverts_from_singlebyte"] = single
        return metrics

    def _multibyte_advert_keys(self, conn: sqlite3.Connection) -> set[str] | None:
        """Public keys currently classified as advertising over multibyte paths."""
        if not _table_exists(conn, "observed_paths"):
            return None
        try:
            return {
                row[0]
                for row in conn.execute(
                    """
                    SELECT DISTINCT public_key FROM observed_paths
                    WHERE packet_type = 'advert' AND public_key IS NOT NULL
                      AND bytes_per_hop IN (2, 3)
                    """
                )
                if row[0]
            }
        except sqlite3.Error as exc:
            self.logger.debug(f"Could not load multibyte advert keys: {exc}")
            return None

    def compute_day(
        self,
        conn: sqlite3.Connection,
        date_str: str,
        *,
        sources: int,
        today: str,
        first_seen: dict[str, str],
        multibyte_keys: set[str] | None,
        backfill: bool = False,
    ) -> dict[str, Any]:
        """Recompute every rollup column for one local date.

        Families whose raw rows have already aged out contribute NULL rather
        than 0, so an upsert preserves whatever history is already stored.
        """
        start, end = day_bounds(date_str)
        now = time.time()
        # Future-dated rows exist in the wild (max observed: 2103-08-15); an
        # unclamped upper bound would mint a rollup row for that year.
        end = min(end, now)
        values: dict[str, Any] = {"date": date_str}
        present = 0

        if end <= start:
            # Entirely in the future — nothing observable belongs to this date.
            return {"values": values, "sources": 0}

        age_days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(date_str, "%Y-%m-%d")).days

        def _in_retention(bit: int) -> bool:
            return age_days < self._retention_for(bit)

        for bit, compute in (
            (SOURCE_MESSAGE_STATS, self._message_metrics),
            (SOURCE_COMMAND_STATS, self._command_metrics),
            (SOURCE_PATH_STATS, self._path_metrics),
            (SOURCE_PACKET_STREAM, self._packet_metrics),
        ):
            if not (sources & bit) or not _in_retention(bit):
                continue
            try:
                values.update(compute(conn, start, end))
                present |= bit
            except sqlite3.Error as exc:
                self.logger.debug(f"Rollup {SOURCE_NAMES[bit]} for {date_str}: {exc}")

        if sources & SOURCE_DAILY_STATS:
            try:
                values.update(
                    self._advert_metrics(conn, date_str, first_seen, multibyte_keys)
                )
                present |= SOURCE_DAILY_STATS
            except sqlite3.Error as exc:
                self.logger.debug(f"Rollup daily_stats for {date_str}: {exc}")

        # Contact totals are a point-in-time reading with no historical form, so
        # only today's row gets them; older rows keep whatever was stored then.
        if (sources & SOURCE_CONTACT_TRACKING) and date_str == today and not backfill:
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*), SUM(CASE WHEN is_currently_tracked = 1 THEN 1 ELSE 0 END)
                    FROM complete_contact_tracking
                    """
                ).fetchone()
                values["contacts_known"] = row[0] or 0
                values["contacts_tracked"] = row[1] or 0
                present |= SOURCE_CONTACT_TRACKING
            except sqlite3.Error as exc:
                self.logger.debug(f"Rollup contact totals for {date_str}: {exc}")

        return {"values": values, "sources": present}

    ROLLUP_COLUMNS = (
        "messages_total", "messages_dm", "messages_channel",
        "unique_senders", "unique_channels",
        "commands_total", "commands_replied", "unique_command_users",
        "path_obs_total", "path_len_max", "path_len_sum",
        "snr_sum", "snr_count", "rssi_sum", "rssi_count",
        "hops_sum", "hops_count",
        "packets_total", "packets_flood", "packets_direct", "packets_multibyte",
        "packet_type_encoding",
        "adverts_total", "nodes_active", "nodes_new",
        "adverts_from_multibyte", "adverts_from_singlebyte",
        "contacts_known", "contacts_tracked",
    )

    def upsert_day(
        self,
        conn: sqlite3.Connection,
        computed: dict[str, Any],
        *,
        is_backfilled: bool = False,
        is_final: bool = False,
    ) -> None:
        """Write one recomputed date.

        Idempotent: every value is a full recomputation from raw rows, not an
        increment.  Columns arrive as NULL when their source is absent or aged
        out, and COALESCE keeps the stored value rather than erasing history.
        """
        values = computed["values"]
        columns = ("date", *self.ROLLUP_COLUMNS, "sources_present", "is_backfilled", "is_final", "computed_at")
        params = [values["date"]]
        params.extend(values.get(column) for column in self.ROLLUP_COLUMNS)
        params.extend([
            computed["sources"],
            1 if is_backfilled else 0,
            1 if is_final else 0,
            datetime.now().isoformat(timespec="seconds"),
        ])
        assignments = ", ".join(
            f"{column} = COALESCE(excluded.{column}, daily_rollup.{column})"
            for column in self.ROLLUP_COLUMNS
        )
        conn.execute(
            f"""
            INSERT INTO daily_rollup ({', '.join(columns)})
            VALUES ({', '.join('?' * len(columns))})
            ON CONFLICT(date) DO UPDATE SET
                {assignments},
                sources_present = daily_rollup.sources_present | excluded.sources_present,
                is_backfilled   = CASE WHEN excluded.is_backfilled = 0 THEN 0
                                       ELSE daily_rollup.is_backfilled END,
                is_final        = MAX(daily_rollup.is_final, excluded.is_final),
                computed_at     = excluded.computed_at
            """,
            params,
        )

    def plan_backfill(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Compute (read-only) one rollup row per date present in daily_stats.

        Message, command, path and packet columns stay NULL: those raw rows were
        pruned days ago and cannot be recovered.  The UI renders them as gaps.
        Separated from the write so the ~90-day pass never holds a write lock
        while it reads.
        """
        if not _table_exists(conn, "daily_stats"):
            return []
        today = local_date_str()
        horizon = (
            datetime.strptime(today, "%Y-%m-%d") - timedelta(days=self.history_days)
        ).strftime("%Y-%m-%d")
        dates = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT date FROM daily_stats WHERE date >= ? AND date <= ? ORDER BY date",
                (horizon, today),
            )
            if row[0]
        ]
        if not dates:
            return []
        first_seen = self._first_advert_dates(conn)
        sources = self.detect_sources(conn) & SOURCE_DAILY_STATS
        return [
            self.compute_day(
                conn,
                date_str,
                sources=sources,
                today=today,
                first_seen=first_seen,
                multibyte_keys=None,
                backfill=True,
            )
            for date_str in dates
        ]

    def write_backfill(self, conn: sqlite3.Connection, planned: list[dict[str, Any]]) -> int:
        """Persist a planned backfill. Caller owns the transaction."""
        today = local_date_str()
        for computed in planned:
            self.upsert_day(
                conn,
                computed,
                is_backfilled=True,
                is_final=computed["values"]["date"] < today,
            )
        return len(planned)

    def has_history(self, conn: sqlite3.Connection) -> bool:
        """True once daily_rollup holds anything — backfill runs only when empty."""
        row = conn.execute("SELECT 1 FROM daily_rollup LIMIT 1").fetchone()
        return row is not None

    def prune_history(self, conn: sqlite3.Connection) -> int:
        cutoff = (datetime.now() - timedelta(days=self.history_days)).strftime("%Y-%m-%d")
        cursor = conn.execute("DELETE FROM daily_rollup WHERE date < ?", (cutoff,))
        return cursor.rowcount or 0

    # -- snapshot ------------------------------------------------------------

    def _mesh_snapshot(self, conn: sqlite3.Connection, sources: int) -> dict[str, Any]:
        mesh: dict[str, Any] = {}
        today = local_date_str()

        if sources & SOURCE_DAILY_STATS:
            row = conn.execute(
                "SELECT SUM(advert_count), COUNT(DISTINCT public_key) FROM daily_stats WHERE date = ?",
                (today,),
            ).fetchone()
            mesh["adverts_24h"] = row[0] or 0
            mesh["nodes_24h"] = row[1] or 0
            week_ago = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
            row = conn.execute(
                """
                SELECT SUM(CASE WHEN first_date  = ? THEN 1 ELSE 0 END),
                       SUM(CASE WHEN first_date >= ? THEN 1 ELSE 0 END)
                FROM (SELECT MIN(date) AS first_date FROM daily_stats GROUP BY public_key)
                """,
                (today, week_ago),
            ).fetchone()
            mesh["new_nodes_24h"] = row[0] or 0
            mesh["new_nodes_7d"] = row[1] or 0

        if sources & SOURCE_CONTACT_TRACKING:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN is_currently_tracked = 1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN last_heard <  datetime('now','localtime','-7 days')
                                 AND last_heard >= datetime('now','localtime','-14 days')
                                THEN 1 ELSE 0 END)
                FROM complete_contact_tracking
                """
            ).fetchone()
            mesh["contacts_known"] = row[0] or 0
            mesh["contacts_tracked"] = row[1] or 0
            # "Heard last week, silent this week" — a churn signal, not a total.
            mesh["gone_quiet_7d"] = row[2] or 0

            # Each column carries its own emptiness test rather than sharing one
            # in the WHERE clause: filtering the row set on country would drop
            # contacts that have a state or city but no country, undercounting
            # both.  COUNT(DISTINCT ...) already ignores the NULLs.
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT CASE
                           WHEN country IS NULL OR country = '' THEN NULL
                           WHEN country IN ('United States','United States of America','US','USA')
                           THEN 'United States' ELSE country END),
                       COUNT(DISTINCT CASE WHEN state IS NOT NULL AND state != '' THEN state END),
                       COUNT(DISTINCT CASE WHEN city  IS NOT NULL AND city  != '' THEN city  END)
                FROM complete_contact_tracking
                WHERE last_heard > datetime('now','localtime','-30 days')
                  AND is_currently_tracked = 1
                """
            ).fetchone()
            mesh["countries"] = row[0] or 0
            mesh["states"] = row[1] or 0
            mesh["cities"] = row[2] or 0

            mesh["role_mix"] = self._mix(conn, "role")

        mesh["hops"] = self._hops_distribution(conn, sources)

        if sources & SOURCE_OBSERVED_PATHS or _table_exists(conn, "neighbor_links"):
            mesh["neighbors"] = {
                window: self._count_one_hop_nodes(conn, window)
                for window in NEIGHBOR_WINDOWS
            }

        if sources & SOURCE_PACKET_STREAM:
            row = conn.execute(
                """
                SELECT SUM(CASE WHEN route_type_name LIKE '%FLOOD'  THEN 1 ELSE 0 END),
                       SUM(CASE WHEN route_type_name LIKE '%DIRECT' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN bytes_per_hop IN (2, 3) THEN 1 ELSE 0 END),
                       COUNT(*)
                FROM packet_stream
                WHERE type = 'packet' AND route_type_name IS NOT NULL
                """
            ).fetchone()
            mesh["route_mix"] = {"flood": row[0] or 0, "direct": row[1] or 0}
            mesh.setdefault("encoding", {})["packets"] = {
                "multibyte": row[2] or 0,
                "total": row[3] or 0,
            }
            # Same population as route_mix — what the traffic is, beside how it
            # is routed.
            mesh["payload_mix"] = _top_n_with_other(
                [
                    [name or "Unknown", count]
                    for name, count in conn.execute(
                        """
                        SELECT payload_type_name, COUNT(*) FROM packet_stream
                        WHERE type = 'packet' AND route_type_name IS NOT NULL
                        GROUP BY payload_type_name ORDER BY COUNT(*) DESC
                        """
                    )
                ]
            )

        if self.multibyte_contacts_fn is not None:
            try:
                result = self.multibyte_contacts_fn(conn.cursor())
            except Exception as exc:  # noqa: BLE001 - injected callback
                self.logger.debug(f"Multibyte contact evidence unavailable: {exc}")
                result = None
            if result is not None:
                multibyte, total = result
                mesh.setdefault("encoding", {})["contacts_7d"] = {
                    "multibyte": multibyte,
                    "total": total,
                }
        return mesh

    def _mix(self, conn: sqlite3.Connection, column: str) -> list[list[Any]]:
        """Contact counts per role, unmapped ordinals folded into Unknown."""
        buckets: dict[str, int] = {}
        for value, count in conn.execute(
            f"SELECT {column}, COUNT(*) FROM complete_contact_tracking GROUP BY {column}"  # noqa: S608 - fixed identifiers
        ):
            key = normalize_role(value) if column == "role" else (value or "Unknown")
            buckets[key] = buckets.get(key, 0) + (count or 0)
        return _top_n_with_other([[name, count] for name, count in buckets.items()])

    def _hops_distribution(self, conn: sqlite3.Connection, sources: int) -> dict[str, Any]:
        """Two views of distance: where nodes are, and where flood traffic comes from.

        Beware that the two tables count paths in different units, and the
        conversion is not symmetric:

        * ``observed_paths.path_length`` is a BYTE count, so hops are
          ``path_length / bytes_per_hop`` — a 3-hop multibyte path is 6 or 9.
        * ``packet_stream.path_len`` is already a HOP count, with the byte
          length carried separately as ``path_byte_length``.

        Dividing the second by bytes_per_hop, or failing to divide the first,
        silently rescales a whole axis.  Verified against live rows in
        tests/test_dashboard_stats.py::TestHopConventions.

        The two series also cover different spans — adverts over 7 days,
        packets over whatever packet_stream retains (typically 3) — so each is
        labelled with its own window rather than being presented as one period.
        """
        distribution: dict[str, Any] = {"nodes": [], "flood_packets": []}

        if sources & SOURCE_OBSERVED_PATHS:
            distribution["nodes"] = self._bucket_hops(
                conn.execute(
                    """
                    SELECT MIN(path_length / bytes_per_hop) AS hops, COUNT(*) AS n
                    FROM observed_paths
                    WHERE packet_type = 'advert' AND bytes_per_hop > 0
                      AND last_seen >= datetime('now','localtime','-7 days')
                    GROUP BY public_key
                    """
                ),
                per_row=True,
            )

        if sources & SOURCE_PACKET_STREAM:
            # path_len is already hops here — do NOT divide by bytes_per_hop.
            distribution["flood_packets"] = self._bucket_hops(
                conn.execute(
                    """
                    SELECT path_len AS hops, COUNT(*) AS n FROM packet_stream
                    WHERE type = 'packet' AND route_type_name LIKE '%FLOOD'
                      AND path_len IS NOT NULL
                    GROUP BY path_len
                    """
                )
            )

        totals = {key: sum(count for _, count in series) for key, series in distribution.items()}

        # The flood tail is a long thin decay — on the live mesh it runs to 63
        # hops in bars under a pixel tall. Hide the buckets carrying less than
        # FLOOD_MIN_SHARE_PCT of the series, but count what was hidden: an
        # unannounced cut is the same lie as a truncated category list.
        flood_total = totals["flood_packets"]
        hidden_packets = 0
        hidden_buckets = 0
        if flood_total:
            kept = []
            for hop, count in distribution["flood_packets"]:
                if count and (count / flood_total) * 100 < FLOOD_MIN_SHARE_PCT:
                    hidden_packets += count
                    hidden_buckets += 1
                    kept.append([hop, None])
                else:
                    kept.append([hop, count])
            distribution["flood_packets"] = kept

        # Pad both onto one contiguous axis so the bars line up, bounded by the
        # hops that actually show something: an axis that runs on past the last
        # drawn bar spends its width on nothing.
        populated = [
            hop
            for series in distribution.values()
            for hop, count in series
            if count
        ]
        if populated:
            low, high = min(populated), max(populated)
            for key, series in distribution.items():
                counts = dict(series)
                # Gaps pad with 0 ("no packets at this hop"), which is a
                # different claim from the None used above for "withheld".
                distribution[key] = [
                    [hop, counts.get(hop, 0)] for hop in range(low, high + 1)
                ]

        distribution["totals"] = totals
        distribution["flood_hidden"] = {
            "packets": hidden_packets,
            "buckets": hidden_buckets,
            "share_pct": round(hidden_packets / flood_total * 100, 2) if flood_total else 0,
            "threshold_pct": FLOOD_MIN_SHARE_PCT,
        }
        return distribution

    @staticmethod
    def _bucket_hops(rows, per_row: bool = False) -> list[list[int]]:
        """Fold (hops, n) rows into a hop -> count mapping, dropping absurd hops."""
        counts: dict[int, int] = {}
        for row in rows:
            hops = row["hops"]
            if hops is None or not 0 <= hops <= MAX_PLOTTED_HOPS:
                continue
            counts[int(hops)] = counts.get(int(hops), 0) + (1 if per_row else (row["n"] or 0))
        return [[hop, count] for hop, count in sorted(counts.items())]

    @staticmethod
    def _window_offset(window: str) -> str:
        return {"24h": "-1 days", "7d": "-7 days", "30d": "-30 days"}.get(window, "-7 days")

    def _count_one_hop_nodes(self, conn: sqlite3.Connection, window: str) -> int:
        return len(self._direct_neighbor_keys(conn, window))

    def _direct_neighbor_keys(self, conn: sqlite3.Connection, window: str) -> set[str]:
        """Public keys heard directly in *window*: empty-path adverts plus discover."""
        keys: set[str] = set()
        if _table_exists(conn, "observed_paths"):
            for row in conn.execute(
                f"""
                SELECT DISTINCT op.public_key FROM observed_paths op
                WHERE op.packet_type = 'advert' AND {ZERO_HOP_PATH}
                  AND op.last_seen > datetime('now','localtime', ?)
                """,  # noqa: S608 - ZERO_HOP_PATH is a module constant, not input
                (self._window_offset(window),),
            ):
                if row[0]:
                    keys.add(str(row[0]).lower())
        keys.update(self._neighbor_link_keys_in_window(conn, window))
        return keys

    def _neighbor_link_keys_in_window(
        self, conn: sqlite3.Connection, window: str
    ) -> dict[str, dict[str, Any]]:
        """Discover-confirmed neighbours whose last_seen falls in *window*.

        neighbor_links.last_seen is UTC ISO; do not compare it with
        datetime('now','localtime', …).
        """
        found: dict[str, dict[str, Any]] = {}
        if not _table_exists(conn, "neighbor_links"):
            return found
        cutoff = _neighbor_window_cutoff_utc(window)
        for row in conn.execute(
            """
            SELECT neighbor_public_key, last_snr, last_seen
            FROM neighbor_links
            """
        ):
            key = (row["neighbor_public_key"] or "").lower()
            if not key:
                continue
            seen = _parse_aware_utc(row["last_seen"])
            if seen is None or seen < cutoff:
                continue
            found[key] = {
                "snr": row["last_snr"],
                "last_seen": row["last_seen"],
            }
        return found

    def _one_hop_rows(self, conn: sqlite3.Connection, window: str, limit: int) -> list[dict]:
        """Nodes this radio heard directly, weakest measured link first.

        Membership is empty-path advert evidence (MeshCore hop count 0) union
        in-window ``neighbor_links``. ``complete_contact_tracking.hop_count``
        is not used for membership — it over-claims zero-hop.

        Signal prefers the path-row measurement, then discover SNR, then the
        contact row only when hop_count is 0.
        """
        has_path_snr = _table_exists(conn, "observed_paths") and _column_exists(
            conn, "observed_paths", "snr"
        )
        snr_select = "op.snr AS path_snr, op.rssi AS path_rssi," if has_path_snr else (
            "NULL AS path_snr, NULL AS path_rssi,"
        )
        path_rows = []
        if _table_exists(conn, "observed_paths"):
            path_rows = conn.execute(
                f"""
                SELECT op.public_key,
                       MAX(op.last_seen) AS last_seen,
                       {snr_select}
                       c.name, c.role, c.snr, c.signal_strength, c.hop_count
                FROM observed_paths op
                LEFT JOIN complete_contact_tracking c ON c.public_key = op.public_key
                WHERE op.packet_type = 'advert' AND {ZERO_HOP_PATH}
                  AND op.last_seen > datetime('now','localtime', ?)
                GROUP BY op.public_key
                """,  # noqa: S608 - ZERO_HOP_PATH / snr_select are module-controlled
                (self._window_offset(window),),
            ).fetchall()

        discover = self._neighbor_link_keys_in_window(conn, window)
        by_key: dict[str, dict[str, Any]] = {}

        def _contact_name(row, key: str) -> str:
            return row["name"] or key[:12]

        for row in path_rows:
            key = (row["public_key"] or "").lower()
            if not key:
                continue
            snr = row["path_snr"]
            rssi = row["path_rssi"]
            link = discover.pop(key, None)
            if snr is None and link is not None:
                snr = link.get("snr")
            if snr is None and row["hop_count"] == 0:
                snr = row["snr"]
                rssi = row["signal_strength"] if rssi is None else rssi
            corroborated = snr is not None
            by_key[key] = {
                "name": _contact_name(row, key),
                "public_key": row["public_key"],
                "role": normalize_role(row["role"]),
                "snr": round(float(snr), 1) if corroborated else None,
                "rssi": (
                    round(float(rssi)) if corroborated and rssi is not None else None
                ),
                "signal_corroborated": corroborated,
                "last_seen": row["last_seen"],
            }

        # Discover-only neighbours (answered node-discover, no stored empty-path advert).
        if discover:
            contacts = {}
            if _table_exists(conn, "complete_contact_tracking"):
                placeholders = ",".join("?" * len(discover))
                for row in conn.execute(
                    f"""
                    SELECT public_key, name, role FROM complete_contact_tracking
                    WHERE lower(public_key) IN ({placeholders})
                    """,  # noqa: S608 - placeholders match bound keys
                    tuple(discover),
                ):
                    contacts[(row["public_key"] or "").lower()] = row
            for key, link in discover.items():
                contact = contacts.get(key)
                snr = link.get("snr")
                corroborated = snr is not None
                by_key[key] = {
                    "name": (contact["name"] if contact else None) or key[:12],
                    "public_key": (contact["public_key"] if contact else key),
                    "role": normalize_role(contact["role"] if contact else None),
                    "snr": round(float(snr), 1) if snr is not None else None,
                    "rssi": None,
                    "signal_corroborated": corroborated,
                    "last_seen": link.get("last_seen"),
                }

        items = list(by_key.values())
        measured = [item for item in items if item["signal_corroborated"]]
        unmeasured = [item for item in items if not item["signal_corroborated"]]
        measured.sort(key=lambda item: item["snr"])
        unmeasured.sort(key=lambda item: item["last_seen"] or "", reverse=True)
        return (measured + unmeasured)[:limit]

    def _bot_snapshot(self, conn: sqlite3.Connection, sources: int) -> dict[str, Any]:
        """Rolling 24-hour bot activity (distinct from the calendar-day rollup)."""
        bot: dict[str, Any] = {}
        now = time.time()
        cutoff = now - 86400
        if sources & SOURCE_MESSAGE_STATS:
            row = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT sender_id), COUNT(DISTINCT channel)
                FROM message_stats WHERE timestamp >= ? AND timestamp <= ?
                """,
                (cutoff, now),
            ).fetchone()
            bot["messages_24h"] = row[0] or 0
            bot["unique_users_24h"] = row[1] or 0
            bot["unique_channels_24h"] = row[2] or 0
        if sources & SOURCE_COMMAND_STATS:
            row = conn.execute(
                """
                SELECT COUNT(*), SUM(CASE WHEN response_sent THEN 1 ELSE 0 END)
                FROM command_stats WHERE timestamp >= ? AND timestamp <= ?
                """,
                (cutoff, now),
            ).fetchone()
            total = row[0] or 0
            bot["commands_24h"] = total
            bot["reply_rate_24h"] = round(((row[1] or 0) / total) * 100, 1) if total else None
        return bot

    def _series_from_rollup(
        self, conn: sqlite3.Connection, points: int = SUMMARY_SERIES_POINTS
    ) -> dict[str, list[dict[str, Any]]]:
        expressions = ", ".join(f"{SERIES_METRICS[name]} AS {name}" for name in SUMMARY_METRICS)
        rows = conn.execute(
            f"""
            SELECT date, is_final, {expressions}
            FROM daily_rollup ORDER BY date DESC LIMIT ?
            """,
            (points,),
        ).fetchall()
        series: dict[str, list[dict[str, Any]]] = {name: [] for name in SUMMARY_METRICS}
        for row in reversed(rows):
            for name in SUMMARY_METRICS:
                series[name].append(
                    {
                        "date": row["date"],
                        "value": row[name],
                        "complete": bool(row["is_final"]),
                    }
                )
        return series

    def _deltas_from_rollup(self, conn: sqlite3.Connection) -> dict[str, float | None]:
        """Percent change of the last complete day against the day before it.

        Explicitly calendar days, not a rolling 24 hours: quoting one against
        the other is the classic dashboard lie, so the UI says which it is.
        """
        expressions = ", ".join(f"{SERIES_METRICS[name]} AS {name}" for name in SUMMARY_METRICS)
        rows = conn.execute(
            f"""
            SELECT date, {expressions} FROM daily_rollup
            WHERE is_final = 1 ORDER BY date DESC LIMIT 2
            """
        ).fetchall()
        if len(rows) < 2:
            return {}
        current, previous = rows[0], rows[1]
        return {name: _change_pct(current[name], previous[name]) for name in SUMMARY_METRICS}

    def _packet_encoding_from_rollup(
        self, conn: sqlite3.Connection, points: int = SUMMARY_SERIES_POINTS
    ) -> list[dict[str, Any]]:
        """Per-day multibyte/total counts per payload type, oldest first.

        The chart stacks raw counts rather than the ratio series because a
        composition needs one shared denominator — each day's multibyte total —
        and that cannot be rebuilt from eight percentages taken over eight
        different denominators.  Sending the counts also lets the tooltip quote
        both readings: a type's slice of the day's multibyte traffic, and how
        much of that type's own traffic was multibyte.
        """
        rows = conn.execute(
            "SELECT date, is_final, packet_type_encoding FROM daily_rollup "
            "ORDER BY date DESC LIMIT ?",
            (points,),
        ).fetchall()
        series: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                stored = json.loads(row["packet_type_encoding"] or "{}")
            except (TypeError, ValueError):
                self.logger.debug(f"Unreadable packet encoding split for {row['date']}")
                stored = {}
            series.append(
                {
                    "date": row["date"],
                    "complete": bool(row["is_final"]),
                    # Keyed by name: Flask sorts JSON object keys, so the client
                    # cannot read a colour slot off position and looks each
                    # bucket up instead.  OTHER is included — it is part of the
                    # denominator the client divides by.
                    "types": {
                        name: stored[name]
                        for name in PACKET_ENCODING_BUCKETS
                        if isinstance(stored.get(name), dict)
                    },
                }
            )
        return series

    def build_snapshot(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Assemble the whole current-state payload. Read-only."""
        sources = self.detect_sources(conn)
        payload: dict[str, Any] = {
            "coverage": {
                **self.packet_coverage(conn),
                "stats_retention_days": self.stats_retention_days,
                "packet_retention_days": self.packet_retention_days,
                "adverts_retention_days": self.adverts_retention_days,
                "sources_present": [
                    name for bit, name in SOURCE_NAMES.items() if sources & bit
                ],
            },
            "mesh": self._mesh_snapshot(conn, sources),
            "bot": self._bot_snapshot(conn, sources),
        }
        payload["series"] = self._series_from_rollup(conn)
        payload["deltas"] = self._deltas_from_rollup(conn)
        payload["packet_encoding"] = self._packet_encoding_from_rollup(conn)
        return payload

    # -- refresh orchestration ----------------------------------------------

    def refresh(self, conn: sqlite3.Connection, *, backfill: bool | None = None) -> dict[str, Any]:
        """Recompute rollups and the snapshot.

        The whole read phase runs outside any explicit transaction and every
        write goes in one short BEGIN IMMEDIATE at the end, so a refresh never
        holds a write lock while it reads and cannot stall the bot's inserts.

        ``backfill`` defaults to "only if daily_rollup is empty".
        """
        started = time.perf_counter()
        today = local_date_str()

        # --- write phase 0: dimension a batch of older packet rows -----------
        # First, not last: every packet figure below reads the denormalized
        # columns, so backfilling afterwards would make each tick describe the
        # previous tick's coverage and leave the counts drifting upward until
        # the backlog cleared.
        conn.execute("BEGIN IMMEDIATE")
        try:
            backfilled_packets = self.backfill_packet_dims(conn)
            self.backfill_zero_hop_from_packet_stream(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # --- read phase 1: everything the rollup rows need -------------------
        sources = self.detect_sources(conn)
        first_seen = self._first_advert_dates(conn)
        multibyte_keys = self._multibyte_advert_keys(conn)

        if backfill is None:
            backfill = not self.has_history(conn)
        planned_backfill = self.plan_backfill(conn) if backfill else []

        computed_days = [
            self.compute_day(
                conn,
                date_str,
                sources=sources,
                today=today,
                first_seen=first_seen,
                multibyte_keys=multibyte_keys,
            )
            for date_str in self._recompute_dates(today)
        ]
        oldest_recomputed = min(day["values"]["date"] for day in computed_days)

        # --- write phase 1: rollups and the packet dimension backfill --------
        conn.execute("BEGIN IMMEDIATE")
        try:
            if planned_backfill:
                self.write_backfill(conn, planned_backfill)
            for day in computed_days:
                self.upsert_day(conn, day, is_final=day["values"]["date"] < today)
            # A day that has left the recompute window will never change again.
            conn.execute(
                "UPDATE daily_rollup SET is_final = 1 WHERE date < ? AND is_final = 0",
                (oldest_recomputed,),
            )
            self.prune_history(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # --- read phase 2: the snapshot, which reads what we just wrote ------
        # Series and deltas come from daily_rollup and the packet tiles from the
        # freshly dimensioned rows, so this has to follow the write, not precede
        # it — otherwise every snapshot describes the previous tick.
        payload = self.build_snapshot(conn)
        generated_at = time.time()
        duration_ms = int((time.perf_counter() - started) * 1000)

        # --- write phase 2: the snapshot row ---------------------------------
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO dashboard_snapshot (id, generated_at, duration_ms, schema_rev, payload)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    duration_ms  = excluded.duration_ms,
                    schema_rev   = excluded.schema_rev,
                    payload      = excluded.payload
                """,
                (generated_at, duration_ms, SCHEMA_REV, json.dumps(payload)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "generated_at": generated_at,
            "duration_ms": duration_ms,
            "days": len(computed_days),
            "backfilled_days": len(planned_backfill),
            "backfilled_packet_rows": backfilled_packets,
        }

    # -- lease ---------------------------------------------------------------

    def try_claim_lease(self, conn: sqlite3.Connection) -> bool:
        """Best-effort exclusion between viewers sharing one database.

        Refreshes are idempotent full recomputations, so a double refresh wastes
        CPU and corrupts nothing.  This exists to skip that waste, not to
        guarantee correctness — treat a lost race as "someone else has it".
        """
        if not _table_exists(conn, "bot_metadata"):
            return True
        now = time.time()
        owner = f"{os.getpid()}:{socket.gethostname()}:{now + self.interval_seconds * 2:.0f}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM bot_metadata WHERE key = ?", (LEASE_KEY,)
            ).fetchone()
            if row and row[0]:
                parts = str(row[0]).rsplit(":", 1)
                try:
                    expires = float(parts[-1])
                except (TypeError, ValueError):
                    expires = 0.0
                held_by_other = parts[0] != f"{os.getpid()}:{socket.gethostname()}"
                if expires > now and held_by_other:
                    conn.rollback()
                    return False
            conn.execute(
                """
                INSERT INTO bot_metadata (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (LEASE_KEY, owner),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            # Losing the race to another viewer surfaces here as a lock error;
            # treat any failure as "someone else has it" and skip the tick.
            with suppress(sqlite3.Error):
                conn.rollback()
            self.logger.debug(f"Snapshot lease not claimed: {exc}")
            return False

    # -- readers -------------------------------------------------------------

    def read_summary(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """Return the stored snapshot with freshness metadata, or None if unset."""
        if not _table_exists(conn, "dashboard_snapshot"):
            return None
        row = conn.execute(
            "SELECT generated_at, duration_ms, schema_rev, payload FROM dashboard_snapshot WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError) as exc:
            self.logger.warning(f"Discarding unreadable dashboard snapshot: {exc}")
            return None
        age = max(0.0, time.time() - row["generated_at"])
        payload.update(
            {
                "generated_at": row["generated_at"],
                "duration_ms": row["duration_ms"],
                "schema_rev": row["schema_rev"],
                "snapshot_age_seconds": round(age, 1),
                "stale": age > self.interval_seconds * 3,
            }
        )
        return payload

    def read_series(self, conn: sqlite3.Connection, metric: str, days: int) -> dict[str, Any]:
        """Points for one metric plus period-over-period totals."""
        if metric not in SERIES_METRICS:
            raise ValueError(f"Unknown metric: {metric}")
        days = max(1, min(int(days), self.history_days))
        expression = SERIES_METRICS[metric]
        start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        previous_start = (datetime.now() - timedelta(days=days * 2 - 1)).strftime("%Y-%m-%d")

        rows = conn.execute(
            f"SELECT date, is_final, {expression} AS value, computed_at "
            "FROM daily_rollup WHERE date >= ? ORDER BY date",
            (start,),
        ).fetchall()
        points = [
            {"date": row["date"], "value": row["value"], "complete": bool(row["is_final"])}
            for row in rows
        ]
        previous_rows = conn.execute(
            f"SELECT {expression} AS value FROM daily_rollup WHERE date >= ? AND date < ?",
            (previous_start, start),
        ).fetchall()

        def aggregate(source) -> float | None:
            values = [row["value"] for row in source if row["value"] is not None]
            if not values:
                return None
            if metric in RATIO_METRICS:
                return round(sum(values) / len(values), 1)
            return sum(values)

        current_total = aggregate(rows)
        previous_total = aggregate(previous_rows)
        etag_row = conn.execute(
            "SELECT MAX(computed_at) FROM daily_rollup WHERE date >= ?", (previous_start,)
        ).fetchone()

        return {
            "metric": metric,
            "days": days,
            "is_ratio": metric in RATIO_METRICS,
            "points": points,
            "current_period_total": current_total,
            "previous_period_total": previous_total,
            "change_pct": _change_pct(current_total, previous_total),
            "etag_source": etag_row[0] if etag_row else None,
        }

    def _window_cutoff(self, window: str) -> float | None:
        """Epoch lower bound for a window token, or None for 'all'."""
        return {
            "24h": time.time() - 86400,
            "7d": time.time() - 7 * 86400,
            "30d": time.time() - 30 * 86400,
            "90d": time.time() - 90 * 86400,
        }.get(window)

    def read_top(
        self, conn: sqlite3.Connection, kind: str, window: str, limit: int
    ) -> dict[str, Any]:
        """One narrow leaderboard query, instead of the whole stats payload."""
        if kind not in TOP_KINDS:
            raise ValueError(f"Unknown top kind: {kind}")
        limit = max(1, min(int(limit), 50))
        cutoff = self._window_cutoff(window)
        now = time.time()
        retention = self.stats_retention_days
        items: list[dict[str, Any]] = []

        total: int | None = None

        if kind == "neighbors":
            if window not in NEIGHBOR_WINDOWS:
                window = NEIGHBOR_WINDOWS[0]
            retention = self.adverts_retention_days
            if _table_exists(conn, "observed_paths") or _table_exists(conn, "neighbor_links"):
                items = self._one_hop_rows(conn, window, limit)
                total = self._count_one_hop_nodes(conn, window)
        elif kind == "repeaters":
            retention = self.adverts_retention_days
            days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(window, retention)
            since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            if _table_exists(conn, "daily_stats") and _table_exists(conn, "complete_contact_tracking"):
                items = [
                    {
                        "name": row["name"] or row["public_key"][:12],
                        "public_key": row["public_key"],
                        "role": normalize_role(row["role"]),
                        "count": row["adverts"] or 0,
                    }
                    for row in conn.execute(
                        """
                        SELECT ds.public_key, c.name, c.role, SUM(ds.advert_count) AS adverts
                        FROM daily_stats ds
                        JOIN complete_contact_tracking c ON c.public_key = ds.public_key
                        WHERE ds.date >= ? AND c.role IN ('repeater', 'roomserver')
                        GROUP BY ds.public_key ORDER BY adverts DESC LIMIT ?
                        """,
                        (since, limit),
                    )
                ]
        elif kind == "users" and _table_exists(conn, "message_stats"):
            items = [
                {"name": row[0], "count": row[1]}
                for row in conn.execute(
                    """
                    SELECT sender_id, COUNT(*) AS n FROM message_stats
                    WHERE timestamp <= ? AND timestamp >= ?
                    GROUP BY sender_id ORDER BY n DESC LIMIT ?
                    """,
                    (now, cutoff if cutoff is not None else 0, limit),
                )
            ]
        elif kind == "commands" and _table_exists(conn, "command_stats"):
            items = [
                {"name": row[0], "count": row[1]}
                for row in conn.execute(
                    """
                    SELECT command_name, COUNT(*) AS n FROM command_stats
                    WHERE timestamp <= ? AND timestamp >= ?
                    GROUP BY command_name ORDER BY n DESC LIMIT ?
                    """,
                    (now, cutoff if cutoff is not None else 0, limit),
                )
            ]
        elif kind == "channels" and _table_exists(conn, "message_stats"):
            items = [
                {"name": row[0], "count": row[1], "users": row[2]}
                for row in conn.execute(
                    """
                    SELECT channel, COUNT(*) AS n, COUNT(DISTINCT sender_id) AS users
                    FROM message_stats
                    WHERE channel IS NOT NULL AND channel != ''
                      AND timestamp <= ? AND timestamp >= ?
                    GROUP BY channel ORDER BY n DESC LIMIT ?
                    """,
                    (now, cutoff if cutoff is not None else 0, limit),
                )
            ]
        elif kind == "paths" and _table_exists(conn, "path_stats"):
            items = [
                {
                    "name": row[0],
                    "path_length": row[1],
                    "path_string": row[2],
                    "timestamp": row[3],
                }
                for row in conn.execute(
                    """
                    SELECT sender_id, path_length, path_string, timestamp FROM path_stats
                    WHERE timestamp <= ? AND timestamp >= ?
                    ORDER BY path_length DESC, timestamp DESC LIMIT ?
                    """,
                    (now, cutoff if cutoff is not None else 0, limit),
                )
            ]

        requested_days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(window)
        return {
            "kind": kind,
            "window": window,
            "window_label": self._window_label(window, retention),
            "retention_days": retention,
            "truncated_by_retention": bool(
                requested_days is None or requested_days > retention
            ),
            "total": total if total is not None else len(items),
            "items": items,
        }

    @staticmethod
    def _window_label(window: str, retention_days: int) -> str:
        return {
            "24h": "Last 24 hours",
            "7d": "Last 7 days",
            "30d": "Last 30 days",
            "90d": "Last 90 days",
        }.get(window, f"All retained ({retention_days}d)")

    def derive_windows(self, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Build the selector options from retention, so labels cannot overclaim.

        The old UI offered "30d" and "All" against tables pruned at 7 days: every
        one of those options returned the same 7-day number under a wrong label.
        """
        retention = {
            "stats_days": self.stats_retention_days,
            "packet_stream_days": self.packet_retention_days,
            "daily_stats_days": self.adverts_retention_days,
            "observed_paths_days": self.adverts_retention_days,
        }

        def options(retention_days: int) -> list[dict[str, str]]:
            candidates = [("24h", 1), ("7d", 7), ("30d", 30), ("90d", 90)]
            out = [
                {"value": value, "label": self._window_label(value, retention_days)}
                for value, days in candidates
                if days <= retention_days or value == "24h"
            ]
            out.append({"value": "all", "label": f"All retained ({retention_days}d)"})
            return out

        stats_options = options(self.stats_retention_days)
        return {
            "retention": retention,
            "windows": {
                "users": stats_options,
                "commands": stats_options,
                "channels": stats_options,
                "paths": stats_options,
                "repeaters": options(self.adverts_retention_days),
                # Deliberately not retention-derived: observed_paths keeps 90
                # days, but a link last used a month ago tells you nothing about
                # whether it works now.
                "neighbors": [
                    {"value": value, "label": self._window_label(value, self.adverts_retention_days)}
                    for value in NEIGHBOR_WINDOWS
                ],
            },
        }
