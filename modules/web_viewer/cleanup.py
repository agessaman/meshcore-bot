"""Background cleanup for the web viewer: the hourly scheduler that drops
stale Socket.IO clients and prunes old packet_stream rows."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from modules.db_retention import (
    delete_timestamp_rows_in_chunks,
    retention_delete_settings,
)


class CleanupSchedulerMixin:
    """Mixed into BotDataViewer."""

    _cleanup_stale_clients: Any
    _with_db_connection: Any
    config: Any
    logger: Any

    def _start_cleanup_scheduler(self):
        """Start background thread for periodic database cleanup"""
        import threading

        def cleanup_scheduler():
            import time
            while True:
                try:
                    # Clean up stale clients every 5 minutes
                    for _ in range(12):  # 12 x 5 minutes = 1 hour
                        time.sleep(300)  # 5 minutes
                        self._cleanup_stale_clients()

                    # Clean up old data every hour (after 12 stale client cleanups)
                    self._cleanup_old_data()

                except Exception as e:
                    self.logger.error(f"Error in cleanup scheduler: {e}", exc_info=True)
                    time.sleep(60)  # Sleep on error

        # Start the cleanup thread
        cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
        cleanup_thread.start()
        self.logger.info("Cleanup scheduler started")

    def _cleanup_old_data(self, days_to_keep: int | None = None):
        """Clean up old packet stream data to prevent database bloat.
        Uses [Data_Retention] packet_stream_retention_days when days_to_keep is not provided."""
        try:
            import sqlite3

            if days_to_keep is None:
                days_to_keep = 3
                if self.config.has_section('Data_Retention') and self.config.has_option('Data_Retention', 'packet_stream_retention_days'):
                    with suppress(ValueError, TypeError):
                        days_to_keep = self.config.getint('Data_Retention', 'packet_stream_retention_days')

            cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
            batch_size, pause_seconds = retention_delete_settings(self.config)
            total_deleted = delete_timestamp_rows_in_chunks(
                self._with_db_connection,
                'packet_stream',
                'timestamp',
                cutoff_time,
                batch_size=batch_size,
                pause_seconds=pause_seconds,
                logger=self.logger,
                progress_label='packet stream',
            )
            if total_deleted > 0:
                self.logger.info(
                    f"Cleaned up {total_deleted} old packet stream entries "
                    f"(older than {days_to_keep} days)"
                )

        except sqlite3.OperationalError as e:
            self.logger.warning(f"Database busy during cleanup (will retry next cycle): {e}")
        except Exception as e:
            self.logger.error(f"Error cleaning up old packet stream data: {e}", exc_info=True)
