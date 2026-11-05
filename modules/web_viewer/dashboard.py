"""Dashboard snapshots for the web viewer: building the dashboard service from
config, its read-only database connection, and the background refresher."""

from __future__ import annotations

import configparser
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any

from modules.web_viewer.dashboard_stats import DashboardStatsService


class DashboardSnapshotMixin:
    """Mixed into BotDataViewer."""

    _bucket_hop_chunks: Any
    _configure_db_connection: Any
    _contact_has_multibyte_path_evidence: Any
    _get_cached_contact_multibyte_hop_chunks: Any
    config: Any
    dashboard_snapshot_enabled: Any
    dashboard_snapshot_interval: Any
    dashboard_stats: Any
    db_path: Any
    logger: Any

    def _config_int(self, section: str, option: str, fallback: int) -> int:
        """Read an int config value, falling back on a missing or malformed entry."""
        try:
            return self.config.getint(section, option, fallback=fallback)
        except (configparser.Error, ValueError, TypeError):
            return fallback

    def _init_dashboard_service(self):
        """Build the dashboard rollup/snapshot service from config."""
        try:
            self.dashboard_snapshot_enabled = self.config.getboolean(
                'Web_Viewer', 'dashboard_snapshot_enabled', fallback=True
            )
        except (configparser.Error, ValueError, TypeError):
            self.dashboard_snapshot_enabled = True

        self.dashboard_snapshot_interval = max(
            15, self._config_int('Web_Viewer', 'dashboard_snapshot_interval_seconds', 60)
        )
        self.dashboard_stats = DashboardStatsService(
            self.logger,
            history_days=self._config_int('Web_Viewer', 'dashboard_snapshot_history_days', 400),
            packet_backfill_rows=self._config_int('Web_Viewer', 'dashboard_packet_backfill_rows', 2000),
            interval_seconds=self.dashboard_snapshot_interval,
            # Retention drives which window labels the UI is allowed to offer,
            # so read the same keys the cleanup jobs enforce.
            stats_retention_days=self._config_int('Stats_Command', 'data_retention_days', 7),
            packet_retention_days=self._config_int('Data_Retention', 'packet_stream_retention_days', 3),
            adverts_retention_days=self._config_int('Data_Retention', 'daily_stats_retention_days', 90),
            multibyte_contacts_fn=self._count_contacts_7d_multibyte,
        )

    def _count_contacts_7d_multibyte(self, cursor) -> tuple[int, int] | None:
        """(multibyte, total) contacts heard in the last 7 days, or None if unavailable.

        Injected into DashboardStatsService so the snapshot reuses the viewer's
        memoized hop-prefix evidence instead of rebuilding it.
        """
        try:
            cursor.execute(
                """
                SELECT COUNT(*) FROM complete_contact_tracking
                WHERE last_heard > datetime('now', 'localtime', '-7 days')
                """
            )
            total = cursor.fetchone()[0] or 0
        except sqlite3.Error as e:
            self.logger.debug(f"Could not count 7d contacts: {e}")
            return None

        chunk_buckets = self._bucket_hop_chunks(
            self._get_cached_contact_multibyte_hop_chunks(cursor, recent_days=7)
        )
        mb_advert_pks: set[str] = set()
        try:
            cursor.execute(
                """
                SELECT DISTINCT public_key FROM observed_paths
                WHERE packet_type = 'advert' AND public_key IS NOT NULL
                AND bytes_per_hop IN (2, 3)
                AND date(last_seen) >= date('now', 'localtime', '-7 days')
                """
            )
            mb_advert_pks = {row[0] for row in cursor.fetchall() if row[0]}
        except sqlite3.Error as e:
            self.logger.debug(f"Could not load 7d multibyte advert keys: {e}")

        try:
            cursor.execute(
                """
                SELECT public_key, role, out_bytes_per_hop
                FROM complete_contact_tracking
                WHERE last_heard > datetime('now', 'localtime', '-7 days')
                """
            )
            multibyte = sum(
                1
                for row in cursor.fetchall()
                if self._contact_has_multibyte_path_evidence(
                    row[0], row[1], row[2], mb_advert_pks, chunk_buckets
                )
            )
        except sqlite3.Error as e:
            self.logger.debug(f"Could not compute contacts_7d_multibyte_path: {e}")
            return None
        return multibyte, total

    def _dashboard_connection(self):
        """Connection for the refresher: autocommit, so BEGIN IMMEDIATE is ours.

        The reader connections leave transaction control to pysqlite, but the
        refresher deliberately brackets its own write phase and must not have an
        implicit transaction opened underneath it.
        """
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._configure_db_connection(conn)
        return conn

    def _start_dashboard_refresher(self):
        """Recompute the dashboard snapshot on an interval, in this process.

        The refresher lives in the viewer rather than the bot for two reasons:
        the viewer may point at a different database ([Web_Viewer] db_path), and
        it already runs migrations itself, so the rollup tables exist in
        whichever file this process opens.  A bot-side scheduler job would write
        to the wrong file in a split-DB install and would not run at all for a
        standalone viewer.
        """
        if not self.dashboard_snapshot_enabled:
            self.logger.info("Dashboard snapshot refresher disabled by config")
            return

        def refresher():
            consecutive_errors = 0
            # Refresh once at startup so the first page load has a snapshot.
            delay = 2.0
            while True:
                time.sleep(delay)
                delay = self.dashboard_snapshot_interval
                try:
                    with closing(self._dashboard_connection()) as conn:
                        if not self.dashboard_stats.try_claim_lease(conn):
                            self.logger.debug(
                                "Another viewer holds the dashboard snapshot lease; skipping tick"
                            )
                            continue
                        result = self.dashboard_stats.refresh(conn)
                    consecutive_errors = 0
                    self.logger.debug(
                        "Dashboard snapshot refreshed in %sms (%s days, %s packet rows backfilled)",
                        result['duration_ms'],
                        result['days'],
                        result['backfilled_packet_rows'],
                    )
                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors == 1:
                        self.logger.error(f"Dashboard snapshot refresh failed: {e}", exc_info=True)
                    else:
                        self.logger.warning(
                            f"Dashboard snapshot refresh failed ({consecutive_errors}): {e}"
                        )
                    delay = min(600, self.dashboard_snapshot_interval * (2 ** min(consecutive_errors, 5)))

        thread = threading.Thread(target=refresher, name="dashboard-snapshot", daemon=True)
        thread.start()
        self.logger.info(
            f"Dashboard snapshot refresher started (every {self.dashboard_snapshot_interval}s)"
        )
