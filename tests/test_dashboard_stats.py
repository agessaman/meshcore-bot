#!/usr/bin/env python3
"""Tests for the web viewer dashboard: rollup service, snapshot API, and the
performance floor of the legacy /api/stats payload.

The perf test seeds a synthetic database roughly the shape of a live install
(~100k rows across the tables the dashboard reads) so the request-path cost is
regression-locked rather than eyeballed.
"""

import json
import logging
import sqlite3
import time
from configparser import ConfigParser
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from modules.web_viewer.app import BotDataViewer
from modules.web_viewer.dashboard_stats import (
    LEASE_KEY,
    TOP_KINDS,
    day_bounds,
    local_date_str,
    normalize_role,
)

# ---------------------------------------------------------------------------
# Synthetic database
# ---------------------------------------------------------------------------

CONTACTS = 2000
OBSERVED_PATHS = 20000
PACKETS = 40000
MESSAGES = 20000
COMMANDS = 5000
PATHS = 5000
DAILY_STATS_DAYS = 90
DAILY_STATS_NODES = 200

# Only the first ADVERTISING_CONTACTS contacts appear in observed_paths, so the
# rest fall through the cheap public-key check into the hop-prefix matcher —
# the quadratic step this suite exists to keep out.
ADVERTISING_CONTACTS = 500


def _pk(i: int) -> str:
    """Deterministic 64-hex-char public key for row *i*."""
    return f"{i:064x}"


def _create_stats_tables(conn: sqlite3.Connection) -> None:
    """Create the three tables stats_command owns (migrations do not)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS message_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            sender_id TEXT NOT NULL,
            channel TEXT,
            content TEXT NOT NULL,
            is_dm BOOLEAN NOT NULL,
            hops INTEGER,
            snr REAL,
            rssi INTEGER,
            path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS command_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            sender_id TEXT NOT NULL,
            command_name TEXT NOT NULL,
            channel TEXT,
            is_dm BOOLEAN NOT NULL,
            response_sent BOOLEAN NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS path_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            sender_id TEXT NOT NULL,
            channel TEXT,
            path_length INTEGER NOT NULL,
            path_string TEXT NOT NULL,
            hops INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_message_timestamp ON message_stats(timestamp);
        CREATE INDEX IF NOT EXISTS idx_command_timestamp ON command_stats(timestamp);
        """
    )


def seed_database(db_path: str, now: float | None = None) -> None:
    """Populate *db_path* with a synthetic mesh roughly the shape of a live install."""
    now = time.time() if now is None else now
    roles = ["repeater", "roomserver", "companion", "sensor", "type3"]
    devices = ["Repeater", "Companion", "RoomServer", "Sensor"]

    with sqlite3.connect(db_path, timeout=60) as conn:
        _create_stats_tables(conn)

        conn.executemany(
            """
            INSERT INTO complete_contact_tracking
                (public_key, name, role, device_type, first_heard, last_heard,
                 advert_count, snr, signal_strength, hop_count, is_currently_tracked,
                 out_bytes_per_hop, city, state, country)
            VALUES (?, ?, ?, ?, datetime('now','localtime','-40 days'),
                    datetime('now','localtime', ?), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _pk(i),
                    f"node-{i}",
                    roles[i % len(roles)],
                    devices[i % len(devices)],
                    f"-{i % 10} days",
                    1 + (i % 50),
                    -10.0 + (i % 30),
                    -120.0 + (i % 60),
                    i % 6,
                    1 if i % 3 == 0 else 0,
                    3 if i % 4 == 0 else 1,
                    f"city-{i % 90}",
                    f"state-{i % 14}",
                    "United States" if i % 4 else "Canada",
                )
                for i in range(CONTACTS)
            ],
        )

        conn.executemany(
            """
            INSERT INTO observed_paths
                (public_key, from_prefix, to_prefix, path_hex, path_length,
                 bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
            VALUES (?, ?, ?, ?, ?, ?, 'advert',
                    datetime('now','localtime','-20 days'),
                    datetime('now','localtime', ?), ?)
            """,
            [
                (
                    _pk(i % ADVERTISING_CONTACTS),
                    f"{i % 65536:04x}",
                    f"{(i * 7) % 65536:04x}",
                    f"{i % 65536:04x}{(i * 7) % 65536:04x}",
                    2,
                    2 if i % 5 else 1,
                    f"-{i % 12} days",
                    1 + (i % 9),
                )
                for i in range(OBSERVED_PATHS)
            ],
        )

        conn.executemany(
            "INSERT INTO packet_stream (timestamp, data, type) VALUES (?, ?, 'packet')",
            [
                (
                    now - (i % (3 * 86400)),
                    json.dumps(
                        {
                            "bytes_per_hop": 2 if i % 4 else 1,
                            "route_type_name": "FLOOD" if i % 3 else "DIRECT",
                            "payload_type_name": "ADVERT" if i % 2 else "TXT_MSG",
                            "path_len": i % 5,
                            "payload_hex": "ab" * 40,
                        }
                    ),
                    )
                for i in range(PACKETS)
            ],
        )

        conn.executemany(
            """
            INSERT INTO message_stats
                (timestamp, sender_id, channel, content, is_dm, hops, snr, rssi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(now - (i % (7 * 86400))),
                    f"user-{i % 120}",
                    None if i % 5 == 0 else f"channel-{i % 4}",
                    f"message {i}",
                    1 if i % 5 == 0 else 0,
                    i % 5,
                    -5.0 + (i % 20),
                    -110 + (i % 50),
                )
                for i in range(MESSAGES)
            ],
        )

        conn.executemany(
            """
            INSERT INTO command_stats
                (timestamp, sender_id, command_name, channel, is_dm, response_sent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(now - (i % (7 * 86400))),
                    f"user-{i % 60}",
                    f"cmd{i % 12}",
                    f"channel-{i % 4}",
                    i % 2,
                    0 if i % 20 == 0 else 1,
                )
                for i in range(COMMANDS)
            ],
        )

        conn.executemany(
            """
            INSERT INTO path_stats
                (timestamp, sender_id, channel, path_length, path_string, hops)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(now - (i % (7 * 86400))),
                    f"user-{i % 60}",
                    f"channel-{i % 4}",
                    i % 8,
                    ",".join(f"{(i + h) % 256:02x}" for h in range(i % 8)),
                    i % 8,
                )
                for i in range(PATHS)
            ],
        )

        conn.executemany(
            """
            INSERT INTO daily_stats (date, public_key, advert_count)
            VALUES (date('now','localtime', ?), ?, ?)
            """,
            [
                (f"-{day} days", _pk(node), 1 + ((day * node) % 20))
                for day in range(DAILY_STATS_DAYS)
                for node in range(DAILY_STATS_NODES)
            ],
        )
        conn.commit()


def _fake_setup_logging(self: BotDataViewer) -> None:
    self.logger = logging.getLogger("test_dashboard_stats")
    self.logger.setLevel(logging.DEBUG)
    if not self.logger.handlers:
        self.logger.addHandler(logging.NullHandler())
    self.logger.propagate = False


def build_viewer(tmp_path, extra_web_viewer: dict[str, str] | None = None) -> BotDataViewer:
    """Create a BotDataViewer over a fresh temp DB with all background threads off."""
    db_path = str(tmp_path / "meshcore_bot.db")
    config_path = str(tmp_path / "config.ini")

    config = ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", db_path)
    config.add_section("Web_Viewer")
    config.set("Web_Viewer", "enabled", "false")
    config.set("Web_Viewer", "web_viewer_password", "")
    for key, value in (extra_web_viewer or {}).items():
        config.set("Web_Viewer", key, value)
    with open(config_path, "w") as handle:
        config.write(handle)

    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
        patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
        patch.object(BotDataViewer, "_setup_socketio_handlers", lambda self: None),
        patch("modules.web_viewer.app.RepeaterManager"),
    ):
        viewer = BotDataViewer(db_path=db_path, config_path=config_path)

    viewer.app.config["TESTING"] = True
    return viewer


@pytest.fixture(scope="module")
def seeded_viewer(tmp_path_factory):
    """Module-scoped viewer over a ~100k-row synthetic database."""
    tmp_path = tmp_path_factory.mktemp("dashboard_perf")
    viewer = build_viewer(tmp_path)
    seed_database(viewer.db_path)
    with sqlite3.connect(viewer.db_path, timeout=60) as conn:
        conn.execute("ANALYZE")
    yield viewer


# ---------------------------------------------------------------------------
# Performance floor
# ---------------------------------------------------------------------------


class TestStatsPerformance:
    """Regression lock on the request-path cost of the dashboard payload.

    Thresholds are deliberately loose relative to measured times (~10x headroom)
    so they fail on an algorithmic regression, not on a slow CI runner.
    """

    def test_api_stats_under_threshold(self, seeded_viewer):
        """~35ms after the rework, against ~264ms for the linear hop-prefix scan."""
        with seeded_viewer.app.test_client() as client:
            client.get("/api/stats")  # warm the page cache
            started = time.perf_counter()
            response = client.get("/api/stats")
            elapsed = time.perf_counter() - started

        assert response.status_code == 200
        payload = response.get_json()
        assert "error" not in payload
        assert payload["total_contacts"] == CONTACTS
        assert elapsed < 0.2, f"/api/stats took {elapsed:.2f}s on a synthetic 100k-row DB"

    def test_dashboard_summary_is_a_single_row_read(self, seeded_viewer):
        with closing(seeded_viewer._dashboard_connection()) as conn:
            seeded_viewer.dashboard_stats.refresh(conn)
        with seeded_viewer.app.test_client() as client:
            client.get("/api/dashboard/summary")
            started = time.perf_counter()
            response = client.get("/api/dashboard/summary")
            elapsed = time.perf_counter() - started

        assert response.status_code == 200
        assert elapsed < 0.05, f"/api/dashboard/summary took {elapsed * 1000:.0f}ms"


# ---------------------------------------------------------------------------
# Rollup correctness
# ---------------------------------------------------------------------------


@pytest.fixture
def viewer(tmp_path):
    """Function-scoped viewer over an empty migrated database."""
    return build_viewer(tmp_path)


def _refresh(viewer, **kwargs) -> dict:
    with closing(viewer._dashboard_connection()) as conn:
        return viewer.dashboard_stats.refresh(conn, **kwargs)


def _rollup(viewer, date_str: str) -> dict | None:
    with closing(viewer._dashboard_connection()) as conn:
        row = conn.execute("SELECT * FROM daily_rollup WHERE date = ?", (date_str,)).fetchone()
    return dict(row) if row else None


class TestRollupCorrectness:

    def test_backfilled_days_are_null_not_zero(self, viewer):
        """A pruned source must read as a gap; zero would look like an outage."""
        seed_database(viewer.db_path)
        _refresh(viewer)

        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        row = _rollup(viewer, old_date)
        assert row is not None
        assert row["is_backfilled"] == 1
        # daily_stats survives 90 days, so adverts are real...
        assert row["adverts_total"] > 0
        # ...while the 7-day and 3-day sources are unrecoverable, not empty.
        for column in ("messages_total", "commands_total", "path_obs_total", "packets_total"):
            assert row[column] is None, f"{column} should be NULL for a backfilled day"

    def test_future_timestamps_do_not_create_rollup_rows(self, viewer):
        """Rows dated 2103 exist in the wild; an unclamped bound charts them."""
        seed_database(viewer.db_path)
        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                """
                INSERT INTO message_stats (timestamp, sender_id, channel, content, is_dm)
                VALUES (?, 'ghost', 'general', 'from the future', 0)
                """,
                (int(datetime(2103, 8, 15).timestamp()),),
            )
        _refresh(viewer)

        with closing(viewer._dashboard_connection()) as conn:
            latest = conn.execute("SELECT MAX(date) FROM daily_rollup").fetchone()[0]
        assert latest <= local_date_str()

    def test_today_excludes_future_rows_from_its_count(self, viewer):
        seed_database(viewer.db_path)
        today = local_date_str()
        before = _refresh(viewer) and _rollup(viewer, today)["messages_total"]

        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                """
                INSERT INTO message_stats (timestamp, sender_id, channel, content, is_dm)
                VALUES (?, 'ghost', 'general', 'later today but not yet', 0)
                """,
                (int(time.time()) + 3600,),
            )
        _refresh(viewer)
        assert _rollup(viewer, today)["messages_total"] == before

    def test_refresh_is_idempotent(self, viewer):
        seed_database(viewer.db_path)
        # Clear the packet backfill backlog first: while it drains, successive
        # refreshes legitimately see more dimensioned rows than the last.
        viewer.dashboard_stats.packet_backfill_rows = PACKETS
        _refresh(viewer)
        first = _rollup(viewer, local_date_str())
        _refresh(viewer)
        second = _rollup(viewer, local_date_str())
        for column, value in first.items():
            if column == "computed_at":
                continue
            assert second[column] == value, column

    def test_signal_stored_as_sums_and_counts(self, viewer):
        """Means cannot be re-aggregated across a window; sums and counts can."""
        seed_database(viewer.db_path)
        _refresh(viewer)
        today = local_date_str()
        row = _rollup(viewer, today)
        # Seed timestamps are `now - i`, so near midnight some rows land on
        # yesterday. Compare against the same day window the rollup uses.
        start, end = day_bounds(today)
        with sqlite3.connect(viewer.db_path) as conn:
            expected = conn.execute(
                """
                SELECT SUM(snr),
                       SUM(CASE WHEN snr  IS NOT NULL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN rssi IS NOT NULL THEN 1 ELSE 0 END)
                FROM message_stats
                WHERE timestamp >= ? AND timestamp < ?
                """,
                (start, end),
            ).fetchone()
        assert row["snr_count"] == expected[1]
        assert row["rssi_count"] == expected[2]
        assert row["snr_sum"] == pytest.approx(expected[0])

    def test_missing_stats_tables_degrade_to_null(self, viewer):
        """`collect_stats = false` leaves the tables absent entirely."""
        seed_database(viewer.db_path)
        with sqlite3.connect(viewer.db_path) as conn:
            for table in ("message_stats", "command_stats", "path_stats"):
                conn.execute(f"DROP TABLE {table}")
        _refresh(viewer)

        row = _rollup(viewer, local_date_str())
        assert row["messages_total"] is None
        assert row["commands_total"] is None
        assert row["adverts_total"] > 0  # unaffected sources still populate

        with viewer.app.test_client() as client:
            payload = client.get("/api/dashboard/summary").get_json()
        assert "message_stats" not in payload["coverage"]["sources_present"]
        assert payload["bot"] == {}

    def test_local_date_attribution_under_non_utc_timezone(self, viewer, monkeypatch):
        """The rollup keys on local dates while message_stats stores UTC epochs.

        Get this wrong and the message series is offset from the advert series
        by several hours of traffic — silently, and for months.
        """
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            # 03:00 UTC is 20:00 the previous day in Los Angeles.
            utc_moment = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)
            local_date = utc_moment.astimezone().strftime("%Y-%m-%d")
            assert local_date == "2026-07-28", "fixture assumes a UTC-7/8 offset"

            start, end = day_bounds(local_date)
            assert start <= utc_moment.timestamp() < end

            with sqlite3.connect(viewer.db_path) as conn:
                _create_stats_tables(conn)
                conn.execute(
                    """
                    INSERT INTO message_stats (timestamp, sender_id, channel, content, is_dm)
                    VALUES (?, 'tz-probe', 'general', 'evening message', 0)
                    """,
                    (int(utc_moment.timestamp()),),
                )

            with closing(viewer._dashboard_connection()) as conn:
                computed = viewer.dashboard_stats.compute_day(
                    conn,
                    local_date,
                    sources=viewer.dashboard_stats.detect_sources(conn),
                    today=local_date,
                    first_seen={},
                    multibyte_keys=None,
                )
            assert computed["values"]["messages_total"] == 1
        finally:
            monkeypatch.undo()
            time.tzset()


class TestPacketDimensions:

    def test_column_counts_match_json_extract(self, viewer):
        """The denormalized columns must agree with the JSON they replace."""
        seed_database(viewer.db_path)
        viewer.dashboard_stats.packet_backfill_rows = PACKETS
        _refresh(viewer)

        with sqlite3.connect(viewer.db_path) as conn:
            from_column = conn.execute(
                "SELECT COUNT(*) FROM packet_stream "
                "WHERE type='packet' AND bytes_per_hop IN (2, 3)"
            ).fetchone()[0]
            from_json = conn.execute(
                "SELECT COUNT(*) FROM packet_stream WHERE type='packet' "
                "AND CAST(json_extract(data, '$.bytes_per_hop') AS INTEGER) IN (2, 3)"
            ).fetchone()[0]
            flood_column = conn.execute(
                "SELECT COUNT(*) FROM packet_stream "
                "WHERE type='packet' AND route_type_name LIKE '%FLOOD'"
            ).fetchone()[0]
            flood_json = conn.execute(
                "SELECT COUNT(*) FROM packet_stream WHERE type='packet' "
                "AND json_extract(data, '$.route_type_name') LIKE '%FLOOD'"
            ).fetchone()[0]

        assert from_column == from_json
        assert flood_column == flood_json

    def test_backfill_advances_and_terminates(self, viewer):
        """Rows whose JSON has no route type must still be marked processed."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type) VALUES (?, ?, 'packet')",
                [(time.time(), json.dumps({"hops": 1}), ) for _ in range(10)],
            )
        viewer.dashboard_stats.packet_backfill_rows = 100
        with closing(viewer._dashboard_connection()) as conn:
            first = viewer.dashboard_stats.backfill_packet_dims(conn)
            second = viewer.dashboard_stats.backfill_packet_dims(conn)
        assert first == 10
        assert second == 0, "rows without a route type were reselected forever"

    def test_backlog_probe_uses_the_worklist_index(self, viewer):
        """The 'anything left to dimension?' probe must not scan the table.

        Finding nothing still means reading everything without this index, and
        that cost is paid on every tick *after* the backfill completes — 4.6s
        per minute forever on a 180MB packet_stream.
        """
        with sqlite3.connect(viewer.db_path) as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT id FROM packet_stream "
                "WHERE type = 'packet' AND route_type_name IS NULL "
                "ORDER BY id DESC LIMIT 2000"
            ).fetchall()
        assert any("idx_packet_stream_undimensioned" in str(step) for step in plan), plan

    def test_coverage_reports_the_measured_window(self, viewer):
        """The old page labelled a 3-day table 'last 7 days'."""
        now = time.time()
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name) "
                "VALUES (?, '{}', 'packet', 'FLOOD')",
                [(now - 2.25 * 86400,), (now - 60,)],
            )
        with closing(viewer._dashboard_connection()) as conn:
            coverage = viewer.dashboard_stats.packet_coverage(conn)
        assert coverage["packets_window_label"] == "last 2d 6h"
        assert coverage["packets_with_dims"] == 2


class TestPacketEncodingTrend:
    """The per-payload-type multibyte share charted on the dashboard.

    packet_stream is pruned within days while the chart spans thirty, so each
    day's split is written as that day is rolled up and can never be recovered
    afterwards — every case here is about what gets frozen into the row.
    """

    @staticmethod
    def _insert(conn, payload_type, bytes_per_hop, count, *, age_seconds=60.0):
        conn.executemany(
            "INSERT INTO packet_stream (timestamp, data, type, route_type_name, "
            "payload_type_name, bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', ?, ?)",
            [(time.time() - age_seconds, payload_type, bytes_per_hop)] * count,
        )

    @staticmethod
    def _stored_split(viewer) -> dict:
        row = _rollup(viewer, local_date_str())
        return json.loads(row["packet_type_encoding"])

    def test_split_is_recorded_per_payload_type(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "GRP_TXT", 3, 3)
            self._insert(conn, "GRP_TXT", 1, 1)
            self._insert(conn, "TXT_MSG", 2, 1)
            self._insert(conn, "TXT_MSG", 1, 3)
        _refresh(viewer)

        assert self._stored_split(viewer) == {
            "GRP_TXT": {"mb": 3, "total": 4},
            "TXT_MSG": {"mb": 1, "total": 4},
        }

    def test_series_reports_one_share_per_type(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "REQ", 2, 1)
            self._insert(conn, "REQ", 1, 3)
        _refresh(viewer)

        with viewer.app.test_client() as client:
            payload = client.get(
                "/api/dashboard/series?metric=multibyte_share_req&days=30"
            ).get_json()
        assert payload["is_ratio"] is True
        assert payload["points"][-1] == {
            "date": local_date_str(),
            "value": 25.0,
            "complete": False,
        }

    def test_undimensioned_packets_leave_the_ratio_alone(self, viewer):
        """They are not evidence of single-byte routing, only of pending work.

        Counting them on the denominator invents a dip in whichever type the
        backfill has not reached yet, which reads as a change in the mesh.
        """
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "PATH", 2, 1)
            self._insert(conn, "PATH", 1, 1)
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type, payload_type_name) "
                "VALUES (?, '{}', 'packet', 'PATH')",
                (time.time(),),
            )
        _refresh(viewer)

        assert self._stored_split(viewer)["PATH"] == {"mb": 1, "total": 2}

    def test_untracked_types_roll_into_other(self, viewer):
        """The tail is summed, not dropped: it is part of the denominator.

        The chart has eight colour slots and ACK, TRACE and unmapped ordinals
        like 'Type11' are not among them — but bar heights are a share of the
        day's whole traffic, so discarding those packets would inflate every
        bar rather than simply omitting a category.
        """
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "ACK", 2, 3)
            self._insert(conn, "Type11", 1, 4)
            self._insert(conn, "ANON_REQ", 2, 1)
        _refresh(viewer)

        assert self._stored_split(viewer) == {
            "ANON_REQ": {"mb": 1, "total": 1},
            "OTHER": {"mb": 3, "total": 7},
        }

    def test_bar_height_matches_the_days_multibyte_share(self, viewer):
        """Segments over the day-wide denominator must sum to the real share."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "GRP_TXT", 2, 2)   # multibyte
            self._insert(conn, "GRP_TXT", 1, 6)   # single-byte
            self._insert(conn, "ACK", 3, 1)       # multibyte, rolls into OTHER
            self._insert(conn, "ACK", 1, 1)
        _refresh(viewer)

        split = self._stored_split(viewer)
        packets = sum(entry["total"] for entry in split.values())
        multibyte = sum(entry["mb"] for entry in split.values())
        assert (packets, multibyte) == (10, 3)
        # What the client draws: each segment over the day's whole traffic.
        assert multibyte / packets * 100 == 30.0

    def test_a_type_with_no_traffic_reads_as_a_gap(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "GRP_DATA", 2, 1)
        _refresh(viewer)

        with viewer.app.test_client() as client:
            payload = client.get(
                "/api/dashboard/series?metric=multibyte_share_response&days=30"
            ).get_json()
        assert payload["points"], "the rollup rows exist; only the value is absent"
        assert all(point["value"] is None for point in payload["points"])

    def test_an_early_prune_does_not_erase_a_recorded_day(self, viewer):
        """A day still inside the retention window can lose its rows anyway.

        Recomputing it as an empty split would overwrite the only copy of that
        day's share, so an empty result has to mean "cannot say" instead.
        """
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "GRP_TXT", 2, 2)
        _refresh(viewer)
        recorded = self._stored_split(viewer)

        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute("DELETE FROM packet_stream")
        _refresh(viewer)

        assert self._stored_split(viewer) == recorded

    def test_summary_carries_raw_counts_for_the_stack(self, viewer):
        """The stacked bars need a shared denominator, so counts are sent.

        Eight percentages each taken over their own type's traffic cannot be
        restacked into a composition — the day's multibyte total is not
        recoverable from them.
        """
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "GRP_TXT", 3, 3)
            self._insert(conn, "GRP_TXT", 1, 1)
            self._insert(conn, "TXT_MSG", 2, 1)
        _refresh(viewer)

        with viewer.app.test_client() as client:
            payload = client.get("/api/dashboard/summary").get_json()
        today = [day for day in payload["packet_encoding"] if day["date"] == local_date_str()]
        assert len(today) == 1
        assert today[0]["types"] == {
            "GRP_TXT": {"mb": 3, "total": 4},
            "TXT_MSG": {"mb": 1, "total": 1},
        }

    def test_summary_omits_the_per_type_share_series(self, viewer):
        """Shipping both forms would send the same thirty days twice."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "REQ", 2, 1)
        _refresh(viewer)

        with viewer.app.test_client() as client:
            payload = client.get("/api/dashboard/summary").get_json()
        assert not [name for name in payload["series"] if name.startswith("multibyte_share_")]
        assert "multibyte_share" in payload["series"], "the advert share still sparklines"
        # Still addressable one at a time for anything plotting a single type.
        with viewer.app.test_client() as client:
            single = client.get("/api/dashboard/series?metric=multibyte_share_req").get_json()
        assert single["is_ratio"] is True

    def test_days_with_no_split_survive_as_empty_slots(self, viewer):
        """The x-axis is every rollup day; a blank one must not shift the rest."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._insert(conn, "PATH", 2, 1)
        _refresh(viewer)

        with viewer.app.test_client() as client:
            payload = client.get("/api/dashboard/summary").get_json()
        dates = [day["date"] for day in payload["packet_encoding"]]
        assert dates == sorted(dates), "oldest first, so bars read left to right"
        assert any(day["types"] == {} for day in payload["packet_encoding"])


class TestDerivedWindows:

    @pytest.mark.parametrize(
        ("retention", "expected"),
        [
            (7, ["24h", "7d", "all"]),
            (30, ["24h", "7d", "30d", "all"]),
            (1, ["24h", "all"]),
        ],
    )
    def test_options_never_outrun_retention(self, tmp_path, retention, expected):
        viewer = build_viewer(tmp_path)
        viewer.dashboard_stats.stats_retention_days = retention
        windows = viewer.dashboard_stats.derive_windows()
        assert [o["value"] for o in windows["windows"]["users"]] == expected
        assert windows["windows"]["users"][-1]["label"] == f"All retained ({retention}d)"


class TestOneHopNeighbours:
    """Neighbour membership is MeshCore hop count 0 (empty path), not 1-hop relays.

    hop_count on contacts over-claims zero-hop and is not used for membership.
    """

    def _seed_contact(self, conn, pk, name, role, hop_count, snr, rssi):
        conn.execute(
            """
            INSERT INTO complete_contact_tracking
                (public_key, name, role, hop_count, snr, signal_strength, last_heard)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime','-1 hours'))
            """,
            (pk, name, role, hop_count, snr, rssi),
        )

    def _seed_path(self, conn, pk, path_length, bytes_per_hop, age="-1 hours",
                   snr=None, rssi=None):
        conn.execute(
            """
            INSERT INTO observed_paths
                (public_key, from_prefix, to_prefix, path_hex, path_length,
                 bytes_per_hop, packet_type, last_seen, snr, rssi)
            VALUES (?, 'aa', 'bb', ?, ?, ?, 'advert', datetime('now','localtime', ?), ?, ?)
            """,
            (pk, "ab" * path_length, path_length, bytes_per_hop, age, snr, rssi),
        )

    def _seed_neighbor_link(self, conn, pk, snr, age_hours=1):
        seen = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        conn.execute(
            """
            INSERT INTO neighbor_links
                (self_public_key, neighbor_public_key, last_seen, last_snr, snr_sum, snr_count)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("ff" * 32, pk, seen.isoformat(), snr, snr),
        )

    def _top(self, viewer, window="24h", limit=10):
        with closing(viewer._dashboard_connection()) as conn:
            return viewer.dashboard_stats.read_top(conn, "neighbors", window, limit)

    def test_empty_path_is_a_direct_neighbour(self, viewer):
        """path_length = 0 is MeshCore hop count 0 — heard directly."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "direct", "repeater", 4, 12.0, -45.0)
            self._seed_path(conn, _pk(1), path_length=0, bytes_per_hop=2, snr=5.0, rssi=-80.0)
            self._seed_contact(conn, _pk(2), "one-hop-relay", "repeater", 0, 6.0, -60.0)
            self._seed_path(conn, _pk(2), path_length=1, bytes_per_hop=1)
            self._seed_contact(conn, _pk(3), "multibyte-1hop", "repeater", 0, 7.0, -50.0)
            self._seed_path(conn, _pk(3), path_length=3, bytes_per_hop=3)

        items = self._top(viewer)["items"]
        assert [item["name"] for item in items] == ["direct"]
        assert items[0]["signal_corroborated"] is True
        assert items[0]["snr"] == 5.0
        assert items[0]["rssi"] == -80

    def test_relayed_one_hop_paths_are_not_neighbours(self, viewer):
        """path_length == bytes_per_hop is one encoded hop, not a direct neighbour."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "onebyte-1hop", "repeater", 0, 5.0, -70.0)
            self._seed_path(conn, _pk(1), path_length=1, bytes_per_hop=1)
            self._seed_contact(conn, _pk(2), "multibyte-1hop", "repeater", 0, 6.0, -60.0)
            self._seed_path(conn, _pk(2), path_length=3, bytes_per_hop=3)
        assert self._top(viewer)["items"] == []
        assert self._top(viewer)["total"] == 0

    def test_path_snr_is_shown_without_hop_count_agreement(self, viewer):
        """The path-row measurement belongs to this link; hop_count is ignored."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "direct", "repeater", 4, 12.0, -45.0)
            self._seed_path(conn, _pk(1), 0, 1, snr=4.0, rssi=-80.0)

        item = self._top(viewer)["items"][0]
        assert item["signal_corroborated"] is True
        assert item["snr"] == 4.0
        assert item["rssi"] == -80

    def test_contacts_with_no_zero_hop_path_are_excluded(self, viewer):
        """hop_count = 0 alone does not make something a neighbour."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "claims-direct", "repeater", 0, 12.0, -45.0)
            self._seed_path(conn, _pk(1), path_length=5, bytes_per_hop=1)
        assert self._top(viewer)["items"] == []
        assert self._top(viewer)["total"] == 0

    def test_weakest_measured_links_are_promoted(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            for i, snr in enumerate([9.0, -8.0, 2.0]):
                self._seed_contact(conn, _pk(i), f"m{i}", "repeater", 0, None, None)
                self._seed_path(conn, _pk(i), 0, 1, snr=snr, rssi=-70.0)
            self._seed_contact(conn, _pk(9), "unmeasured", "repeater", 3, None, None)
            self._seed_path(conn, _pk(9), 0, 1)

        items = self._top(viewer)["items"]
        assert [i["name"] for i in items[:3]] == ["m1", "m2", "m0"]
        assert items[-1]["name"] == "unmeasured"

    def test_window_bounds_membership(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "today", "repeater", 0, 5.0, -70.0)
            self._seed_path(conn, _pk(1), 0, 1, age="-2 hours", snr=5.0)
            self._seed_contact(conn, _pk(2), "last-week", "repeater", 0, 5.0, -70.0)
            self._seed_path(conn, _pk(2), 0, 1, age="-4 days", snr=5.0)

        assert {i["name"] for i in self._top(viewer, "24h")["items"]} == {"today"}
        assert {i["name"] for i in self._top(viewer, "7d")["items"]} == {"today", "last-week"}

    def test_discover_links_are_unioned(self, viewer):
        """A repeater that answered node-discover is a neighbour even without an advert path."""
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "heard-advert", "repeater", 0, None, None)
            self._seed_path(conn, _pk(1), 0, 1, snr=3.0, rssi=-90.0)
            self._seed_contact(conn, _pk(2), "discover-only", "repeater", 1, None, None)
            self._seed_neighbor_link(conn, _pk(2), snr=8.5, age_hours=2)

        items = {item["name"]: item for item in self._top(viewer)["items"]}
        assert set(items) == {"heard-advert", "discover-only"}
        assert items["heard-advert"]["snr"] == 3.0
        assert items["discover-only"]["snr"] == 8.5
        assert items["discover-only"]["rssi"] is None
        assert self._top(viewer)["total"] == 2

    def test_windows_are_capped_below_retention(self, viewer):
        """observed_paths keeps 90 days; a month-old link says nothing about today."""
        options = viewer.dashboard_stats.derive_windows()["windows"]["neighbors"]
        assert [o["value"] for o in options] == ["24h", "7d"]

    def test_snapshot_reports_hops_not_path_bytes(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            self._seed_contact(conn, _pk(1), "near", "repeater", 0, 5.0, -70.0)
            self._seed_path(conn, _pk(1), path_length=6, bytes_per_hop=3)  # 2 hops
            self._seed_contact(conn, _pk(2), "far", "repeater", 4, None, None)
            self._seed_path(conn, _pk(2), path_length=4, bytes_per_hop=1)  # 4 hops
        _refresh(viewer)

        with viewer.app.test_client() as client:
            mesh = client.get("/api/dashboard/summary").get_json()["mesh"]
        assert "device_mix" not in mesh          # same field as role
        assert "hop_histogram" not in mesh       # replaced by path-derived hops
        assert "path_len_histogram" not in mesh  # was byte length labelled as hops
        assert dict(mesh["hops"]["nodes"]) == {2: 1, 3: 0, 4: 1}
        assert mesh["neighbors"] == {"24h": 0, "7d": 0}

    def test_no_neighbours_degrades_cleanly(self, viewer):
        payload = self._top(viewer)
        assert payload["items"] == []
        assert payload["total"] == 0

    def test_packet_stream_backfill_inserts_zero_hop_adverts(self, viewer):
        pk = _pk(7)
        payload_hex = pk + "00" * 70
        packet = {
            "payload_type_name": "ADVERT",
            "path_len": 0,
            "bytes_per_hop": 2,
            "payload_hex": payload_hex,
            "snr": 6.5,
            "rssi": -72,
        }
        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                """
                INSERT INTO packet_stream
                    (timestamp, data, type, route_type_name, payload_type_name, path_len, bytes_per_hop)
                VALUES (?, ?, 'packet', 'FLOOD', 'ADVERT', 0, 2)
                """,
                (time.time() - 3600, json.dumps(packet)),
            )
            self._seed_contact(conn, pk, "from-log", "repeater", 2, None, None)

        _refresh(viewer)
        items = {item["name"]: item for item in self._top(viewer)["items"]}
        assert "from-log" in items
        assert items["from-log"]["snr"] == 6.5
        assert items["from-log"]["rssi"] == -72

        # Second refresh must not duplicate or clear the marker.
        _refresh(viewer)
        assert self._top(viewer)["total"] == 1


class TestHopConventions:
    """The two path tables measure paths in different units. Pin both.

    observed_paths.path_length is a BYTE count; packet_stream.path_len is
    already a HOP count (its byte length lives in path_byte_length). Applying
    either table's rule to the other silently rescales a whole axis, and the
    only visible symptom is a chart that looks slightly wrong.
    """

    def _hops(self, viewer) -> dict:
        with closing(viewer._dashboard_connection()) as conn:
            return viewer.dashboard_stats._hops_distribution(
                conn, viewer.dashboard_stats.detect_sources(conn)
            )

    def test_advert_path_length_is_divided_by_bytes_per_hop(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            for i, (length, bph) in enumerate([(9, 3), (4, 2), (3, 1)]):  # 3, 2 and 3 hops
                conn.execute(
                    """
                    INSERT INTO observed_paths (public_key, from_prefix, to_prefix, path_hex,
                        path_length, bytes_per_hop, packet_type, last_seen)
                    VALUES (?, 'aa', 'bb', 'ab', ?, ?, 'advert', datetime('now','localtime'))
                    """,
                    (_pk(i), length, bph),
                )
        assert dict(self._hops(viewer)["nodes"]) == {2: 1, 3: 2}

    def test_packet_path_len_is_used_as_hops_directly(self, viewer):
        """17 hops at 3 bytes each is 17 on the axis, not 5."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', 17, 3)",
                (time.time(),),
            )
        assert dict(self._hops(viewer)["flood_packets"]) == {17: 1}

    def test_direct_packets_are_excluded_from_the_flood_series(self, viewer):
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', ?, ?, 1)",
                [
                    (time.time(), "FLOOD", 2),
                    (time.time(), "TRANSPORT_FLOOD", 2),
                    (time.time(), "DIRECT", 2),
                ],
            )
        assert dict(self._hops(viewer)["flood_packets"]) == {2: 2}

    def test_series_share_one_contiguous_axis(self, viewer):
        """Bars must line up, so both series are padded onto a common range."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                """
                INSERT INTO observed_paths (public_key, from_prefix, to_prefix, path_hex,
                    path_length, bytes_per_hop, packet_type, last_seen)
                VALUES (?, 'aa', 'bb', 'ab', 1, 1, 'advert', datetime('now','localtime'))
                """,
                (_pk(1),),
            )
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', 4, 1)",
                (time.time(),),
            )
        hops = self._hops(viewer)
        assert [h for h, _ in hops["nodes"]] == [1, 2, 3, 4]
        assert [h for h, _ in hops["flood_packets"]] == [1, 2, 3, 4]
        assert dict(hops["nodes"]) == {1: 1, 2: 0, 3: 0, 4: 0}
        assert dict(hops["flood_packets"]) == {1: 0, 2: 0, 3: 0, 4: 1}

    def test_long_paths_are_kept_up_to_the_protocol_ceiling(self, viewer):
        """64 hops fit in a 64-byte path at one byte per hop; all of it is real.

        An earlier limit of 32 quietly dropped 5,654 flood packets on the live
        database, and — because the filter runs after the per-node MIN() — would
        erase any node whose closest path was longer than 32 hops rather than
        placing it at the far end of the axis.
        """
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', ?, 1)",
                [(time.time(), hops) for hops in (33, 47, 63, 64)],
            )
            conn.execute(
                """
                INSERT INTO observed_paths (public_key, from_prefix, to_prefix, path_hex,
                    path_length, bytes_per_hop, packet_type, last_seen)
                VALUES (?, 'aa', 'bb', 'ab', 63, 1, 'advert', datetime('now','localtime'))
                """,
                (_pk(1),),
            )
        hops = self._hops(viewer)
        assert dict(hops["flood_packets"])[63] == 1
        assert dict(hops["flood_packets"])[64] == 1
        assert sum(c for _, c in hops["flood_packets"]) == 4
        assert dict(hops["nodes"])[63] == 1

    def test_a_node_reachable_only_by_a_long_path_still_appears(self, viewer):
        """The cap runs after the per-node MIN(), so it can erase a node outright."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO observed_paths (public_key, from_prefix, to_prefix, path_hex,
                    path_length, bytes_per_hop, packet_type, last_seen)
                VALUES (?, 'aa', 'bb', ?, ?, 1, 'advert', datetime('now','localtime'))
                """,
                # Distinct path_hex: observed_paths dedups on (public_key,
                # path_hex, packet_type), so two routes to one node need two.
                [(_pk(1), "ab" * 48, 48), (_pk(1), "cd" * 55, 55)],
            )
        assert dict(self._hops(viewer)["nodes"]) == {48: 1}

    def test_negligible_flood_buckets_are_withheld_and_counted(self, viewer):
        """The long thin tail is hidden, but never silently."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', ?, 1)",
                # 2000 at hop 3, then single packets far out: each is 0.05% of
                # 2002, comfortably under the 0.1% display threshold.
                [(time.time(), 3) for _ in range(2000)] + [(time.time(), 40), (time.time(), 55)],
            )
        hops = self._hops(viewer)
        drawn = dict(hops["flood_packets"])

        assert drawn[3] == 2000
        assert 40 not in drawn and 55 not in drawn, "withheld buckets fall outside the axis"
        assert hops["flood_hidden"] == {
            "packets": 2,
            "buckets": 2,
            "share_pct": 0.1,
            "threshold_pct": 0.1,
        }
        # Percentages must divide by the whole series, not the drawn subset.
        assert hops["totals"]["flood_packets"] == 2002

    def test_withheld_bucket_inside_the_axis_is_null_not_zero(self, viewer):
        """Null says "not shown"; zero would claim no packets travelled that far."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', ?, 1)",
                [(time.time(), 2) for _ in range(1000)]
                + [(time.time(), 5)]                       # 0.09% — withheld
                + [(time.time(), 9) for _ in range(100)],  # keeps the axis open past it
            )
        drawn = dict(self._hops(viewer)["flood_packets"])
        assert drawn[5] is None, "a withheld bucket inside the range must be null"
        assert drawn[6] == 0, "a genuinely empty bucket stays zero"

    def test_a_single_bucket_is_never_withheld(self, viewer):
        """One bucket holding everything is 100%, not below threshold."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', 7, 1)",
                (time.time(),),
            )
        hops = self._hops(viewer)
        assert dict(hops["flood_packets"]) == {7: 1}
        assert hops["flood_hidden"]["packets"] == 0

    def test_axis_stops_at_the_last_populated_hop(self, viewer):
        """An axis running past the last observation spends its width on nothing."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.execute(
                """
                INSERT INTO observed_paths (public_key, from_prefix, to_prefix, path_hex,
                    path_length, bytes_per_hop, packet_type, last_seen)
                VALUES (?, 'aa', 'bb', 'ab', 2, 1, 'advert', datetime('now','localtime'))
                """,
                (_pk(1),),
            )
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', 5, 1)",
                (time.time(),),
            )
        hops = self._hops(viewer)
        assert [h for h, _ in hops["nodes"]] == [2, 3, 4, 5]
        # Both ends of the axis carry an observation in at least one series.
        assert hops["nodes"][0][1] or hops["flood_packets"][0][1]
        assert hops["nodes"][-1][1] or hops["flood_packets"][-1][1]

    def test_impossible_hop_counts_are_dropped(self, viewer):
        """Beyond 64 the path field cannot hold it, so the value is corrupt."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, path_len, "
                "bytes_per_hop) VALUES (?, '{}', 'packet', 'FLOOD', ?, 1)",
                [(time.time(), 3), (time.time(), 65), (time.time(), 900), (time.time(), -1)],
            )
        assert dict(self._hops(viewer)["flood_packets"]) == {3: 1}


class TestCategoryMixes:

    def test_tail_is_rolled_into_other_not_dropped(self, viewer):
        """Truncating would leave bars that no longer sum to the printed total."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, "
                "payload_type_name) VALUES (?, '{}', 'packet', 'FLOOD', ?)",
                [
                    (time.time(), f"TYPE{i}")
                    for i in range(12)
                    for _ in range(12 - i)  # descending counts, 12 distinct types
                ],
            )
        _refresh(viewer)
        with viewer.app.test_client() as client:
            mix = client.get("/api/dashboard/summary").get_json()["mesh"]["payload_mix"]

        assert len(mix) == 9, mix  # 8 named categories plus Other
        assert mix[-1][0] == "Other"
        assert sum(count for _, count in mix) == sum(range(1, 13))

    def test_payload_mix_matches_the_route_mix_population(self, viewer):
        """Both describe the dimensioned packets, so their totals must agree."""
        with sqlite3.connect(viewer.db_path) as conn:
            conn.executemany(
                "INSERT INTO packet_stream (timestamp, data, type, route_type_name, "
                "payload_type_name) VALUES (?, '{}', 'packet', ?, ?)",
                [
                    (time.time(), "FLOOD", "GRP_TXT"),
                    (time.time(), "TRANSPORT_FLOOD", "ADVERT"),
                    (time.time(), "DIRECT", "ACK"),
                ],
            )
        _refresh(viewer)
        with viewer.app.test_client() as client:
            mesh = client.get("/api/dashboard/summary").get_json()["mesh"]
        assert sum(mesh["route_mix"].values()) == sum(c for _, c in mesh["payload_mix"]) == 3


class TestRoleBucketing:

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("repeater", "repeater"),
            ("RoomServer", "roomserver"),
            ("type0", "Unknown"),
            ("type15", "Unknown"),
            ("", "Unknown"),
            (None, "Unknown"),
            ("typewriter", "typewriter"),
        ],
    )
    def test_unmapped_ordinals_fold_into_unknown(self, raw, expected):
        assert normalize_role(raw) == expected


class TestSnapshotLease:

    def test_second_holder_is_refused_until_expiry(self, viewer):
        with closing(viewer._dashboard_connection()) as conn:
            assert viewer.dashboard_stats.try_claim_lease(conn) is True
            # Same process reclaims its own lease; a different owner does not.
            assert viewer.dashboard_stats.try_claim_lease(conn) is True
            conn.execute(
                "UPDATE bot_metadata SET value = ? WHERE key = ?",
                (f"999999:other-host:{time.time() + 600:.0f}", LEASE_KEY),
            )
            assert viewer.dashboard_stats.try_claim_lease(conn) is False

    def test_expired_lease_is_reclaimed(self, viewer):
        with closing(viewer._dashboard_connection()) as conn:
            viewer.dashboard_stats.try_claim_lease(conn)
            conn.execute(
                "UPDATE bot_metadata SET value = ? WHERE key = ?",
                (f"999999:other-host:{time.time() - 10:.0f}", LEASE_KEY),
            )
            assert viewer.dashboard_stats.try_claim_lease(conn) is True

    def test_concurrent_refreshes_agree(self, viewer):
        """A double refresh wastes CPU but must not corrupt anything."""
        seed_database(viewer.db_path)
        viewer.dashboard_stats.packet_backfill_rows = PACKETS
        _refresh(viewer)
        first = _rollup(viewer, local_date_str())
        _refresh(viewer)  # simulates a second viewer that won the race
        second = _rollup(viewer, local_date_str())
        assert {k: v for k, v in first.items() if k != "computed_at"} == {
            k: v for k, v in second.items() if k != "computed_at"
        }


class TestDashboardApi:

    def test_summary_returns_503_before_first_refresh(self, viewer):
        with viewer.app.test_client() as client:
            response = client.get("/api/dashboard/summary")
        assert response.status_code == 503
        assert response.get_json()["pending"] is True

    def test_summary_etag_yields_304(self, viewer):
        seed_database(viewer.db_path)
        _refresh(viewer)
        with viewer.app.test_client() as client:
            first = client.get("/api/dashboard/summary")
            second = client.get(
                "/api/dashboard/summary",
                headers={"If-None-Match": first.headers["ETag"]},
            )
        assert first.status_code == 200
        assert second.status_code == 304
        assert second.data == b""

    def test_series_gaps_survive_as_null(self, viewer):
        seed_database(viewer.db_path)
        _refresh(viewer)
        with viewer.app.test_client() as client:
            payload = client.get("/api/dashboard/series?metric=messages&days=60").get_json()
        values = [point["value"] for point in payload["points"]]
        assert None in values, "backfilled days must stay null, not become zero"
        assert any(v is not None for v in values)

    def test_ratio_series_averages_rather_than_sums(self, viewer):
        seed_database(viewer.db_path)
        _refresh(viewer)
        with viewer.app.test_client() as client:
            payload = client.get(
                "/api/dashboard/series?metric=multibyte_share&days=30"
            ).get_json()
        assert payload["is_ratio"] is True
        if payload["current_period_total"] is not None:
            assert 0 <= payload["current_period_total"] <= 100

    def test_unknown_metric_and_kind_rejected(self, viewer):
        with viewer.app.test_client() as client:
            assert client.get("/api/dashboard/series?metric=drop%20table").status_code == 400
            assert client.get("/api/dashboard/top?kind=drop%20table").status_code == 400

    @pytest.mark.parametrize("kind", list(TOP_KINDS))
    def test_top_endpoints_return_items(self, viewer, kind):
        seed_database(viewer.db_path)
        with viewer.app.test_client() as client:
            payload = client.get(f"/api/dashboard/top?kind={kind}&window=7d&limit=5").get_json()
        assert payload["kind"] == kind
        assert len(payload["items"]) <= 5
        assert payload["window_label"] == "Last 7 days"

    def test_manual_refresh_advances_generated_at(self, viewer):
        seed_database(viewer.db_path)
        _refresh(viewer)
        with viewer.app.test_client() as client:
            before = client.get("/api/dashboard/summary").get_json()["generated_at"]
            posted = client.post(
                "/api/dashboard/refresh",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            after = client.get("/api/dashboard/summary").get_json()["generated_at"]
        assert posted.status_code == 200
        assert after > before

    def test_stats_shim_is_marked_deprecated(self, viewer):
        with viewer.app.test_client() as client:
            response = client.get("/api/stats")
        assert response.status_code == 200
        assert response.headers["Deprecation"] == "true"
        assert "Sunset" in response.headers
        assert response.get_json()["deprecated"] is True
