#!/usr/bin/env python3
"""
Message scheduler functionality for the MeshCore Bot
Handles scheduled messages and timing
"""

import asyncio
import datetime
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .utils import decode_escape_sequences, format_keyword_response_with_placeholders, get_config_timezone


class MessageScheduler:
    """Manages scheduled messages and timing"""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.scheduled_messages = {}
        self.scheduler_thread = None
        self._apscheduler: Optional[BackgroundScheduler] = None
        self.last_channel_ops_check_time = 0
        self.last_message_queue_check_time = 0
        self.last_radio_ops_check_time = 0
        self.last_data_retention_run = 0
        self._data_retention_interval_seconds = 86400  # 24 hours
        self.last_nightly_email_time = time.time()     # don't send immediately on startup
        self._last_retention_stats: dict[str, Any] = {}
        self.last_db_backup_run = 0
        self._last_db_backup_stats: dict[str, Any] = {}
        self.last_log_rotation_check_time = 0
        self._last_log_rotation_applied: dict[str, str] = {}

    def get_current_time(self):
        """Get current time in configured timezone"""
        tz, _ = get_config_timezone(self.bot.config, self.logger)
        return datetime.datetime.now(tz)

    def setup_scheduled_messages(self):
        """Setup scheduled messages from config using APScheduler."""
        # Stop and recreate the APScheduler to avoid duplicate jobs on reload
        if self._apscheduler is not None:
            try:
                self._apscheduler.shutdown(wait=False)
            except Exception:
                pass
        tz, _ = get_config_timezone(self.bot.config, self.logger)
        self._apscheduler = BackgroundScheduler(timezone=tz)
        self.scheduled_messages.clear()

        if self.bot.config.has_section('Scheduled_Messages'):
            self.logger.info("Found Scheduled_Messages section")
            for time_str, message_info in self.bot.config.items('Scheduled_Messages'):
                self.logger.info(f"Processing scheduled message: '{time_str}' -> '{message_info}'")
                try:
                    # Validate time format first
                    if not self._is_valid_time_format(time_str):
                        self.logger.warning(f"Invalid time format '{time_str}' for scheduled message: {message_info}")
                        continue

                    channel, message = message_info.split(':', 1)
                    channel = channel.strip()
                    message = decode_escape_sequences(message.strip())
                    hour = int(time_str[:2])
                    minute = int(time_str[2:])

                    self._apscheduler.add_job(
                        self.send_scheduled_message,
                        CronTrigger(hour=hour, minute=minute),
                        args=[channel, message],
                        id=f"msg_{time_str}_{channel}",
                        replace_existing=True,
                    )
                    self.scheduled_messages[time_str] = (channel, message)
                    self.logger.info(f"Scheduled message: {hour:02d}:{minute:02d} -> {channel}: {message}")
                except ValueError:
                    self.logger.warning(f"Invalid scheduled message format: {message_info}")
                except Exception as e:
                    self.logger.warning(f"Error setting up scheduled message '{time_str}': {e}")

        self._apscheduler.start()
        self.logger.info(f"APScheduler started with {len(self.scheduled_messages)} scheduled message(s)")

        # Setup interval-based advertising
        self.setup_interval_advertising()

    def setup_interval_advertising(self):
        """Setup interval-based advertising from config"""
        try:
            advert_interval_hours = self.bot.config.getint('Bot', 'advert_interval_hours', fallback=0)
            if advert_interval_hours > 0:
                self.logger.info(f"Setting up interval-based advertising every {advert_interval_hours} hours")
                # Initialize bot's last advert time to now to prevent immediate advert if not already set
                if not hasattr(self.bot, 'last_advert_time') or self.bot.last_advert_time is None:
                    self.bot.last_advert_time = time.time()
            else:
                self.logger.info("Interval-based advertising disabled (advert_interval_hours = 0)")
        except Exception as e:
            self.logger.warning(f"Error setting up interval advertising: {e}")

    def _is_valid_time_format(self, time_str: str) -> bool:
        """Validate time format (HHMM)"""
        try:
            if len(time_str) != 4:
                return False
            hour = int(time_str[:2])
            minute = int(time_str[2:])
            return 0 <= hour <= 23 and 0 <= minute <= 59
        except ValueError:
            return False

    def send_scheduled_message(self, channel: str, message: str):
        """Send a scheduled message (synchronous wrapper for schedule library)"""
        current_time = self.get_current_time()
        self.logger.info(f"📅 Sending scheduled message at {current_time.strftime('%H:%M:%S')} to {channel}: {message}")

        import asyncio

        # Use the main event loop if available, otherwise create a new one
        # This prevents deadlock when the main loop is already running
        if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
            # Schedule coroutine in the running main event loop
            future = asyncio.run_coroutine_threadsafe(
                self._send_scheduled_message_async(channel, message),
                self.bot.main_event_loop
            )
            # Wait for completion (with timeout to prevent indefinite blocking)
            try:
                future.result(timeout=60)  # 60 second timeout
            except Exception as e:
                self.logger.error(f"Error sending scheduled message: {type(e).__name__}: {e}", exc_info=True)
        else:
            # Fallback: create new event loop if main loop not available
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Run the async function in the event loop
            loop.run_until_complete(self._send_scheduled_message_async(channel, message))

    async def _get_mesh_info(self) -> dict[str, Any]:
        """Get mesh network information for scheduled messages"""
        info = {
            'total_contacts': 0,
            'total_repeaters': 0,
            'total_companions': 0,
            'total_roomservers': 0,
            'total_sensors': 0,
            'recent_activity_24h': 0,
            'new_companions_7d': 0,
            'new_repeaters_7d': 0,
            'new_roomservers_7d': 0,
            'new_sensors_7d': 0,
            'total_contacts_30d': 0,
            'total_repeaters_30d': 0,
            'total_companions_30d': 0,
            'total_roomservers_30d': 0,
            'total_sensors_30d': 0
        }

        try:
            # Get contact statistics from repeater manager if available
            if hasattr(self.bot, 'repeater_manager'):
                try:
                    stats = await self.bot.repeater_manager.get_contact_statistics()
                    if stats:
                        info['total_contacts'] = stats.get('total_heard', 0)
                        by_role = stats.get('by_role', {})
                        info['total_repeaters'] = by_role.get('repeater', 0)
                        info['total_companions'] = by_role.get('companion', 0)
                        info['total_roomservers'] = by_role.get('roomserver', 0)
                        info['total_sensors'] = by_role.get('sensor', 0)
                        info['recent_activity_24h'] = stats.get('recent_activity', 0)
                except Exception as e:
                    self.logger.debug(f"Error getting stats from repeater_manager: {e}")

            # Fallback to device contacts if repeater manager stats not available
            if info['total_contacts'] == 0 and hasattr(self.bot, 'meshcore') and hasattr(self.bot.meshcore, 'contacts'):
                info['total_contacts'] = len(self.bot.meshcore.contacts)

                # Count repeaters and companions
                if hasattr(self.bot, 'repeater_manager'):
                    for contact_data in self.bot.meshcore.contacts.values():
                        if self.bot.repeater_manager._is_repeater_device(contact_data):
                            info['total_repeaters'] += 1
                        else:
                            info['total_companions'] += 1

            # Get recent activity from message_stats if available
            if info['recent_activity_24h'] == 0:
                try:
                    with self.bot.db_manager.connection() as conn:
                        cursor = conn.cursor()
                        # Check if message_stats table exists
                        cursor.execute('''
                            SELECT name FROM sqlite_master
                            WHERE type='table' AND name='message_stats'
                        ''')
                        if cursor.fetchone():
                            cutoff_time = int(time.time()) - (24 * 60 * 60)
                            cursor.execute('''
                                SELECT COUNT(DISTINCT sender_id)
                                FROM message_stats
                                WHERE timestamp >= ? AND is_dm = 0
                            ''', (cutoff_time,))
                            result = cursor.fetchone()
                            if result:
                                info['recent_activity_24h'] = result[0]
                except Exception:
                    pass

            # Calculate new devices in last 7 days (matching web viewer logic)
            # Query devices first heard in the last 7 days, grouped by role
            # Also calculate devices active in last 30 days (last_heard)
            try:
                with self.bot.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    # Check if complete_contact_tracking table exists
                    cursor.execute('''
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name='complete_contact_tracking'
                    ''')
                    if cursor.fetchone():
                        # Get new devices by role (first_heard in last 7 days)
                        # Use role field for matching (more reliable than device_type)
                        cursor.execute('''
                            SELECT role, COUNT(DISTINCT public_key) as count
                            FROM complete_contact_tracking
                            WHERE first_heard >= datetime('now', '-7 days')
                            AND role IS NOT NULL AND role != ''
                            GROUP BY role
                        ''')
                        for row in cursor.fetchall():
                            role = (row[0] or '').lower()
                            count = row[1] or 0

                            if role == 'companion':
                                info['new_companions_7d'] = count
                            elif role == 'repeater':
                                info['new_repeaters_7d'] = count
                            elif role == 'roomserver':
                                info['new_roomservers_7d'] = count
                            elif role == 'sensor':
                                info['new_sensors_7d'] = count

                        # Get total contacts active in last 30 days (last_heard)
                        cursor.execute('''
                            SELECT COUNT(DISTINCT public_key) as count
                            FROM complete_contact_tracking
                            WHERE last_heard >= datetime('now', '-30 days')
                        ''')
                        result = cursor.fetchone()
                        if result:
                            info['total_contacts_30d'] = result[0] or 0

                        # Get devices active in last 30 days by role (last_heard)
                        cursor.execute('''
                            SELECT role, COUNT(DISTINCT public_key) as count
                            FROM complete_contact_tracking
                            WHERE last_heard >= datetime('now', '-30 days')
                            AND role IS NOT NULL AND role != ''
                            GROUP BY role
                        ''')
                        for row in cursor.fetchall():
                            role = (row[0] or '').lower()
                            count = row[1] or 0

                            if role == 'companion':
                                info['total_companions_30d'] = count
                            elif role == 'repeater':
                                info['total_repeaters_30d'] = count
                            elif role == 'roomserver':
                                info['total_roomservers_30d'] = count
                            elif role == 'sensor':
                                info['total_sensors_30d'] = count
            except Exception as e:
                self.logger.debug(f"Error getting new device counts or 30-day activity: {e}")

        except Exception as e:
            self.logger.debug(f"Error getting mesh info: {e}")

        return info

    def _has_mesh_info_placeholders(self, message: str) -> bool:
        """Check if message contains mesh info placeholders"""
        placeholders = [
            '{total_contacts}', '{total_repeaters}', '{total_companions}',
            '{total_roomservers}', '{total_sensors}', '{recent_activity_24h}',
            '{new_companions_7d}', '{new_repeaters_7d}', '{new_roomservers_7d}', '{new_sensors_7d}',
            '{total_contacts_30d}', '{total_repeaters_30d}', '{total_companions_30d}',
            '{total_roomservers_30d}', '{total_sensors_30d}',
            # Legacy placeholders for backward compatibility
            '{repeaters}', '{companions}'
        ]
        return any(placeholder in message for placeholder in placeholders)

    async def _send_scheduled_message_async(self, channel: str, message: str):
        """Send a scheduled message (async implementation)"""
        # Check if message contains mesh info placeholders
        if self._has_mesh_info_placeholders(message):
            try:
                mesh_info = await self._get_mesh_info()
                # Use shared formatting function (message=None for scheduled messages)
                try:
                    message = format_keyword_response_with_placeholders(
                        message,
                        message=None,  # No message object for scheduled messages
                        bot=self.bot,
                        mesh_info=mesh_info
                    )
                    self.logger.debug("Replaced mesh info placeholders in scheduled message")
                except (KeyError, ValueError) as e:
                    self.logger.warning(f"Error replacing placeholders in scheduled message: {e}. Sending message as-is.")
            except Exception as e:
                self.logger.warning(f"Error fetching mesh info for scheduled message: {e}. Sending message as-is.")

        send_timeout = self.bot.config.getint('Bot', 'send_timeout_seconds', fallback=30)
        await asyncio.wait_for(
            self.bot.command_manager.send_channel_message(channel, message),
            timeout=send_timeout,
        )

    def start(self):
        """Start the scheduler in a separate thread"""
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()

    def join(self, timeout: float = 5.0) -> None:
        """Wait for the scheduler thread to finish and stop APScheduler (e.g. during shutdown)."""
        if self._apscheduler is not None:
            try:
                self._apscheduler.shutdown(wait=False)
            except Exception:
                pass
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=timeout)
            if self.scheduler_thread.is_alive():
                self.logger.debug("Scheduler thread did not finish within %s s", timeout)

    def run_scheduler(self):
        """Run the scheduler in a separate thread"""
        self.logger.info("Scheduler thread started")
        last_log_time = 0
        last_feed_poll_time = 0
        last_job_count = 0
        last_job_log_time = 0

        while self.bot.connected:
            current_time = self.get_current_time()

            # Log current time every 5 minutes for debugging
            if time.time() - last_log_time > 300:  # 5 minutes
                self.logger.info(f"Scheduler running - Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                last_log_time = time.time()

            # Log APScheduler job count when it changes (max once per 30 seconds)
            if self._apscheduler is not None:
                current_job_count = len(self._apscheduler.get_jobs())
                current_time_sec = time.time()
                if current_job_count != last_job_count and (current_time_sec - last_job_log_time) >= 30:
                    if current_job_count > 0:
                        self.logger.debug(f"Found {current_job_count} scheduled jobs")
                    last_job_count = current_job_count
                    last_job_log_time = current_time_sec

            # Check for interval-based advertising
            self.check_interval_advertising()

            # Poll feeds every minute (but feeds themselves control their check intervals)
            if time.time() - last_feed_poll_time >= 60:  # Every 60 seconds
                if (hasattr(self.bot, 'feed_manager') and self.bot.feed_manager and
                    hasattr(self.bot.feed_manager, 'enabled') and self.bot.feed_manager.enabled and
                    hasattr(self.bot, 'connected') and self.bot.connected):
                    # Run feed polling in async context
                    import asyncio
                    if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
                        # Schedule coroutine in the running main event loop
                        future = asyncio.run_coroutine_threadsafe(
                            self.bot.feed_manager.poll_all_feeds(),
                            self.bot.main_event_loop
                        )
                        future.add_done_callback(
                            lambda f: self.logger.error("Error in feed polling cycle: %s", f.exception())
                            if not f.cancelled() and f.exception() else None
                        )
                    else:
                        # Fallback: create new event loop if main loop not available
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                        try:
                            loop.run_until_complete(self.bot.feed_manager.poll_all_feeds())
                            self.logger.debug("Feed polling cycle completed")
                        except Exception as e:
                            self.logger.error(f"Error in feed polling cycle: {e}")
                    last_feed_poll_time = time.time()

            # Channels are fetched once on launch only - no periodic refresh
            # This prevents losing channels during incomplete updates

            # Process pending channel operations from web viewer (every 5 seconds)
            if time.time() - self.last_channel_ops_check_time >= 5:  # Every 5 seconds
                if (hasattr(self.bot, 'channel_manager') and self.bot.channel_manager and
                    hasattr(self.bot, 'connected') and self.bot.connected):
                    import asyncio
                    if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
                        # Schedule coroutine in the running main event loop
                        future = asyncio.run_coroutine_threadsafe(
                            self._process_channel_operations(),
                            self.bot.main_event_loop
                        )
                        future.add_done_callback(
                            lambda f: self.logger.exception("Error processing channel operations: %s", f.exception())
                            if not f.cancelled() and f.exception() else None
                        )
                    else:
                        # Fallback: create new event loop if main loop not available
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                        loop.run_until_complete(self._process_channel_operations())
                    self.last_channel_ops_check_time = time.time()

            # Process pending radio operations from web viewer (every 5 seconds)
            if time.time() - self.last_radio_ops_check_time >= 5:
                if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
                    import asyncio
                    future = asyncio.run_coroutine_threadsafe(
                        self._process_radio_operations(),
                        self.bot.main_event_loop
                    )
                    future.add_done_callback(
                        lambda f: self.logger.exception("Error processing radio operations: %s", f.exception())
                        if not f.cancelled() and f.exception() else None
                    )
                self.last_radio_ops_check_time = time.time()

            # Process feed message queue (every 2 seconds)
            if time.time() - self.last_message_queue_check_time >= 2:  # Every 2 seconds
                if (hasattr(self.bot, 'feed_manager') and self.bot.feed_manager and
                    hasattr(self.bot, 'connected') and self.bot.connected):
                    import asyncio
                    if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
                        # Schedule coroutine in the running main event loop
                        future = asyncio.run_coroutine_threadsafe(
                            self.bot.feed_manager.process_message_queue(),
                            self.bot.main_event_loop
                        )
                        future.add_done_callback(
                            lambda f: self.logger.exception("Error processing message queue: %s", f.exception())
                            if not f.cancelled() and f.exception() else None
                        )
                    else:
                        # Fallback: create new event loop if main loop not available
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                        loop.run_until_complete(self.bot.feed_manager.process_message_queue())
                    self.last_message_queue_check_time = time.time()

            # Data retention: run daily (packet_stream, repeater tables, stats, caches, mesh_connections)
            if time.time() - self.last_data_retention_run >= self._data_retention_interval_seconds:
                self._run_data_retention()
                self.last_data_retention_run = time.time()

            # Nightly maintenance email (24 h interval, after retention so stats are fresh)
            if time.time() - self.last_nightly_email_time >= self._data_retention_interval_seconds:
                self._send_nightly_email()
                self.last_nightly_email_time = time.time()

            # Log rotation live-apply: check bot_metadata for config changes every 60 s
            if time.time() - self.last_log_rotation_check_time >= 60:
                self._apply_log_rotation_config()
                self.last_log_rotation_check_time = time.time()

            # DB backup: evaluate schedule every 5 minutes
            if time.time() - self.last_db_backup_run >= 300:
                self._maybe_run_db_backup()
                self.last_db_backup_run = time.time()

            time.sleep(1)

        self.logger.info("Scheduler thread stopped")

    def _run_data_retention(self):
        """Run data retention cleanup: packet_stream, repeater tables, stats, caches, mesh_connections."""
        import asyncio

        def get_retention_days(section: str, key: str, default: int) -> int:
            try:
                if self.bot.config.has_section(section) and self.bot.config.has_option(section, key):
                    return self.bot.config.getint(section, key)
            except Exception:
                pass
            return default

        packet_stream_days = get_retention_days('Data_Retention', 'packet_stream_retention_days', 3)
        purging_log_days = get_retention_days('Data_Retention', 'purging_log_retention_days', 90)
        daily_stats_days = get_retention_days('Data_Retention', 'daily_stats_retention_days', 90)
        observed_paths_days = get_retention_days('Data_Retention', 'observed_paths_retention_days', 90)
        mesh_connections_days = get_retention_days('Data_Retention', 'mesh_connections_retention_days', 7)
        stats_days = get_retention_days('Stats_Command', 'data_retention_days', 7)

        try:
            # Packet stream (web viewer integration)
            if hasattr(self.bot, 'web_viewer_integration') and self.bot.web_viewer_integration:
                bi = getattr(self.bot.web_viewer_integration, 'bot_integration', None)
                if bi and hasattr(bi, 'cleanup_old_data'):
                    bi.cleanup_old_data(packet_stream_days)

            # Repeater manager: purging_log and optional daily_stats / unique_advert / observed_paths
            if hasattr(self.bot, 'repeater_manager') and self.bot.repeater_manager:
                if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self.bot.repeater_manager.cleanup_database(purging_log_days),
                        self.bot.main_event_loop
                    )
                    try:
                        future.result(timeout=60)
                    except Exception as e:
                        self.logger.error(f"Error in repeater_manager.cleanup_database: {e}")
                else:
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    loop.run_until_complete(self.bot.repeater_manager.cleanup_database(purging_log_days))
                if hasattr(self.bot.repeater_manager, 'cleanup_repeater_retention'):
                    self.bot.repeater_manager.cleanup_repeater_retention(
                        daily_stats_days=daily_stats_days,
                        observed_paths_days=observed_paths_days
                    )

            # Stats tables (message_stats, command_stats, path_stats)
            if hasattr(self.bot, 'command_manager') and self.bot.command_manager:
                stats_cmd = self.bot.command_manager.commands.get('stats') if getattr(self.bot.command_manager, 'commands', None) else None
                if stats_cmd and hasattr(stats_cmd, 'cleanup_old_stats'):
                    stats_cmd.cleanup_old_stats(stats_days)

            # Expired caches (geocoding_cache, generic_cache)
            if hasattr(self.bot, 'db_manager') and self.bot.db_manager and hasattr(self.bot.db_manager, 'cleanup_expired_cache'):
                self.bot.db_manager.cleanup_expired_cache()

            # Mesh connections (DB prune to match in-memory expiration)
            if hasattr(self.bot, 'mesh_graph') and self.bot.mesh_graph and hasattr(self.bot.mesh_graph, 'delete_expired_edges_from_db'):
                self.bot.mesh_graph.delete_expired_edges_from_db(mesh_connections_days)

            ran_at = datetime.datetime.utcnow().isoformat()
            self._last_retention_stats['ran_at'] = ran_at
            try:
                self.bot.db_manager.set_metadata('maint.status.data_retention_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.data_retention_outcome', 'ok')
            except Exception:
                pass

        except Exception as e:
            self.logger.exception(f"Error during data retention cleanup: {e}")
            self._last_retention_stats['error'] = str(e)
            try:
                ran_at = datetime.datetime.utcnow().isoformat()
                self.bot.db_manager.set_metadata('maint.status.data_retention_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.data_retention_outcome', f'error: {e}')
            except Exception:
                pass

    def check_interval_advertising(self):
        """Check if it's time to send an interval-based advert"""
        try:
            advert_interval_hours = self.bot.config.getint('Bot', 'advert_interval_hours', fallback=0)
            if advert_interval_hours <= 0:
                return  # Interval advertising disabled

            current_time = time.time()

            # Check if enough time has passed since last advert
            if not hasattr(self.bot, 'last_advert_time') or self.bot.last_advert_time is None:
                # First time, set the timer
                self.bot.last_advert_time = current_time
                return

            time_since_last_advert = current_time - self.bot.last_advert_time
            interval_seconds = advert_interval_hours * 3600  # Convert hours to seconds

            if time_since_last_advert >= interval_seconds:
                self.logger.info(f"Time for interval-based advert (every {advert_interval_hours} hours)")
                self.send_interval_advert()
                self.bot.last_advert_time = current_time

        except Exception as e:
            self.logger.error(f"Error checking interval advertising: {e}")

    def send_interval_advert(self):
        """Send an interval-based advert (synchronous wrapper)"""
        current_time = self.get_current_time()
        self.logger.info(f"📢 Sending interval-based flood advert at {current_time.strftime('%H:%M:%S')}")

        import asyncio

        # Use the main event loop if available, otherwise create a new one
        # This prevents deadlock when the main loop is already running
        if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
            # Schedule coroutine in the running main event loop
            future = asyncio.run_coroutine_threadsafe(
                self._send_interval_advert_async(),
                self.bot.main_event_loop
            )
            # Wait for completion (with timeout to prevent indefinite blocking)
            try:
                future.result(timeout=60)  # 60 second timeout
            except Exception as e:
                self.logger.error(f"Error sending interval advert: {type(e).__name__}: {e}", exc_info=True)
        else:
            # Fallback: create new event loop if main loop not available
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Run the async function in the event loop
            loop.run_until_complete(self._send_interval_advert_async())

    async def _send_interval_advert_async(self):
        """Send an interval-based advert (async implementation)"""
        if self.bot.is_radio_zombie:
            self.bot.logger.warning("send_advert suppressed — radio is in zombie state; power cycle required")
            return
        from meshcore.events import EventType
        try:
            result = await asyncio.wait_for(
                self.bot.meshcore.commands.send_advert(flood=True),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            # Radio did not respond — increment the zombie fail counter so that
            # repeated timeouts eventually trigger zombie detection via the
            # normal _probe_radio_health threshold.
            self.bot._radio_fail_count = getattr(self.bot, '_radio_fail_count', 0) + 1
            raise RuntimeError(
                f"send_advert timed out after 30 s "
                f"(radio_fail_count={self.bot._radio_fail_count})"
            )
        if result.type == EventType.ERROR:
            reason = result.payload.get('reason', 'unknown')
            raise RuntimeError(f"send_advert failed: {reason}")
        self.logger.info("Interval-based flood advert sent successfully")

    async def _process_channel_operations(self):
        """Process pending channel operations from the web viewer"""
        try:
            # Get pending operations
            with self.bot.db_manager.connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT id, operation_type, channel_idx, channel_name, channel_key_hex
                    FROM channel_operations
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 10
                ''')

                operations = cursor.fetchall()

            if not operations:
                return

            self.logger.info(f"Processing {len(operations)} pending channel operation(s)")

            for op in operations:
                op_id = op['id']
                op_type = op['operation_type']
                channel_idx = op['channel_idx']
                channel_name = op['channel_name']
                channel_key_hex = op['channel_key_hex']

                try:
                    success = False
                    error_msg = None

                    if op_type == 'add':
                        # Add channel
                        if channel_key_hex:
                            # Custom channel with key
                            channel_secret = bytes.fromhex(channel_key_hex)
                            success = await self.bot.channel_manager.add_channel(
                                channel_idx, channel_name, channel_secret=channel_secret
                            )
                        else:
                            # Hashtag channel (firmware generates key)
                            success = await self.bot.channel_manager.add_channel(
                                channel_idx, channel_name
                            )

                        if success:
                            self.logger.info(f"Successfully processed channel add operation: {channel_name} at index {channel_idx}")
                        else:
                            error_msg = "Failed to add channel"

                    elif op_type == 'remove':
                        # Remove channel
                        success = await self.bot.channel_manager.remove_channel(channel_idx)

                        if success:
                            self.logger.info(f"Successfully processed channel remove operation: index {channel_idx}")
                        else:
                            error_msg = "Failed to remove channel"

                    # Update operation status
                    with self.bot.db_manager.connection() as conn:
                        cursor = conn.cursor()
                        if success:
                            cursor.execute('''
                                UPDATE channel_operations
                                SET status = 'completed',
                                    processed_at = CURRENT_TIMESTAMP,
                                    result_data = ?
                                WHERE id = ?
                            ''', (json.dumps({'success': True}), op_id))
                        else:
                            cursor.execute('''
                                UPDATE channel_operations
                                SET status = 'failed',
                                    processed_at = CURRENT_TIMESTAMP,
                                    error_message = ?
                                WHERE id = ?
                            ''', (error_msg or 'Unknown error', op_id))
                        conn.commit()

                except Exception as e:
                    self.logger.error(f"Error processing channel operation {op_id}: {e}")
                    # Mark as failed
                    try:
                        with self.bot.db_manager.connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute('''
                                UPDATE channel_operations
                                SET status = 'failed',
                                    processed_at = CURRENT_TIMESTAMP,
                                    error_message = ?
                                WHERE id = ?
                            ''', (str(e), op_id))
                            conn.commit()
                    except Exception as update_error:
                        self.logger.error(f"Error updating operation status: {update_error}")

        except Exception as e:
            db_path = getattr(self.bot.db_manager, 'db_path', 'unknown')
            db_path_str = str(db_path) if db_path != 'unknown' else 'unknown'
            self.logger.exception(f"Error in _process_channel_operations: {e}")
            if db_path_str != 'unknown':
                path_obj = Path(db_path_str)
                self.logger.error(f"Database path: {db_path_str} (exists: {path_obj.exists()}, readable: {os.access(db_path_str, os.R_OK) if path_obj.exists() else False}, writable: {os.access(db_path_str, os.W_OK) if path_obj.exists() else False})")
                # Check parent directory permissions
                if path_obj.exists():
                    parent = path_obj.parent
                    self.logger.error(f"Parent directory: {parent} (exists: {parent.exists()}, writable: {os.access(str(parent), os.W_OK) if parent.exists() else False})")
            else:
                self.logger.error(f"Database path: {db_path_str}")

    async def _process_radio_operations(self):
        """Process pending radio connect/disconnect/reboot/firmware operations from the web viewer."""
        try:
            with self.bot.db_manager.connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, operation_type, payload_data
                    FROM channel_operations
                    WHERE status = 'pending'
                      AND operation_type IN (
                          'radio_reboot', 'radio_connect', 'radio_disconnect',
                          'firmware_read', 'firmware_write'
                      )
                    ORDER BY created_at ASC
                    LIMIT 1
                ''')
                op = cursor.fetchone()

            if not op:
                return

            op_id = op['id']
            op_type = op['operation_type']
            self.logger.info(f"Processing radio operation {op_id}: {op_type}")

            try:
                result_payload = {'success': True}
                if op_type == 'radio_reboot':
                    success = await self.bot.reboot_radio()
                elif op_type == 'radio_connect':
                    success = await self.bot.reconnect_radio()
                elif op_type == 'radio_disconnect':
                    success = await self.bot.disconnect_radio()
                elif op_type == 'firmware_read':
                    success, result_payload = await self._firmware_read_op()
                elif op_type == 'firmware_write':
                    payload = json.loads(op['payload_data'] or '{}')
                    success, result_payload = await self._firmware_write_op(payload)
                else:
                    success = False

                with self.bot.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    if success:
                        cursor.execute('''
                            UPDATE channel_operations
                            SET status = 'completed',
                                processed_at = CURRENT_TIMESTAMP,
                                result_data = ?
                            WHERE id = ?
                        ''', (json.dumps(result_payload), op_id))
                    else:
                        error_msg = result_payload.get('error', 'Radio operation returned False') \
                            if isinstance(result_payload, dict) else 'Radio operation returned False'
                        cursor.execute('''
                            UPDATE channel_operations
                            SET status = 'failed',
                                processed_at = CURRENT_TIMESTAMP,
                                error_message = ?
                            WHERE id = ?
                        ''', (error_msg, op_id))
                    conn.commit()

            except Exception as e:
                self.logger.error(f"Error executing radio operation {op_id}: {e}")
                try:
                    with self.bot.db_manager.connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            UPDATE channel_operations
                            SET status = 'failed',
                                processed_at = CURRENT_TIMESTAMP,
                                error_message = ?
                            WHERE id = ?
                        ''', (str(e), op_id))
                        conn.commit()
                except Exception as update_error:
                    self.logger.error(f"Error updating radio operation status: {update_error}")

        except Exception as e:
            self.logger.exception(f"Error in _process_radio_operations: {e}")

    async def _firmware_read_op(self):
        """Read path.hash.mode and custom vars (including loop.detect) from radio firmware."""
        import asyncio
        try:
            meshcore = getattr(self.bot, 'meshcore', None)
            if not meshcore or not getattr(meshcore, 'is_connected', False):
                return False, {'error': 'Radio not connected'}

            path_hash_mode = await asyncio.wait_for(
                meshcore.commands.get_path_hash_mode(), timeout=10
            )

            custom_vars_event = await asyncio.wait_for(
                meshcore.commands.get_custom_vars(), timeout=10
            )

            custom_vars = {}
            if custom_vars_event and custom_vars_event.payload:
                custom_vars = dict(custom_vars_event.payload)

            return True, {
                'path_hash_mode': path_hash_mode,
                'loop_detect': custom_vars.get('loop.detect'),
                'custom_vars': custom_vars,
            }
        except Exception as e:
            self.logger.error(f"Firmware read failed: {e}")
            return False, {'error': str(e)}

    async def _firmware_write_op(self, payload: dict):
        """Write path.hash.mode and/or loop.detect to radio firmware."""
        import asyncio

        from meshcore.events import EventType
        try:
            meshcore = getattr(self.bot, 'meshcore', None)
            if not meshcore or not getattr(meshcore, 'is_connected', False):
                return False, {'error': 'Radio not connected'}

            results = {}
            errors = []

            if 'path_hash_mode' in payload:
                mode = int(payload['path_hash_mode'])
                result = await asyncio.wait_for(
                    meshcore.commands.set_path_hash_mode(mode), timeout=10
                )
                ok = getattr(result, 'type', None) == EventType.OK
                results['path_hash_mode'] = ok
                if not ok:
                    errors.append(f"set_path_hash_mode({mode}) failed: {result}")

            if 'loop_detect' in payload:
                value = str(payload['loop_detect']).lower()
                result = await asyncio.wait_for(
                    meshcore.commands.set_custom_var('loop.detect', value), timeout=10
                )
                ok = getattr(result, 'type', None) == EventType.OK
                results['loop_detect'] = ok
                if not ok:
                    errors.append(f"set_custom_var(loop.detect, {value}) failed: {result}")

            success = len(errors) == 0
            response: dict[str, Any] = {'results': results}
            if errors:
                response['errors'] = errors
            return success, response
        except Exception as e:
            self.logger.error(f"Firmware write failed: {e}")
            return False, {'error': str(e)}

    # ── Nightly maintenance email ────────────────────────────────────────────

    def _get_notif(self, key: str) -> str:
        """Read a notification setting from bot_metadata."""
        try:
            val = self.bot.db_manager.get_metadata(f'notif.{key}')
            return val if val is not None else ''
        except Exception:
            return ''

    def _collect_email_stats(self) -> dict[str, Any]:
        """Gather 24h summary stats for the nightly digest."""
        stats: dict[str, Any] = {}

        # Bot uptime
        try:
            start = getattr(self.bot, 'connection_time', None)
            if start:
                delta = datetime.timedelta(seconds=int(time.time() - start))
                hours, rem = divmod(delta.seconds, 3600)
                minutes = rem // 60
                parts = []
                if delta.days:
                    parts.append(f"{delta.days}d")
                parts.append(f"{hours}h {minutes}m")
                stats['uptime'] = ' '.join(parts)
            else:
                stats['uptime'] = 'unknown'
        except Exception:
            stats['uptime'] = 'unknown'

        # Contact counts from DB
        try:
            with self.bot.db_manager.connection() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM complete_contact_tracking")
                stats['contacts_total'] = (cur.fetchone() or {}).get('n', 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM complete_contact_tracking "
                    "WHERE last_heard >= datetime('now', '-1 day')"
                )
                stats['contacts_24h'] = (cur.fetchone() or {}).get('n', 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM complete_contact_tracking "
                    "WHERE first_heard >= datetime('now', '-1 day')"
                )
                stats['contacts_new_24h'] = (cur.fetchone() or {}).get('n', 0)
        except Exception as e:
            stats['contacts_error'] = str(e)

        # DB file size
        try:
            db_path = str(self.bot.db_manager.db_path)
            size_bytes = os.path.getsize(db_path)
            stats['db_size_mb'] = f'{size_bytes / 1_048_576:.1f}'
            stats['db_path'] = db_path
        except Exception:
            stats['db_size_mb'] = 'unknown'

        # Log file stats + rotation
        try:
            log_file = self.bot.config.get('Logging', 'log_file', fallback='').strip()
            if log_file:
                log_path = Path(log_file)
                stats['log_file'] = str(log_path)
                if log_path.exists():
                    stats['log_size_mb'] = f'{log_path.stat().st_size / 1_048_576:.1f}'
                    # Count ERROR/CRITICAL lines written in the last 24h by scanning the file
                    time.time() - 86400
                    error_count = critical_count = 0
                    try:
                        with open(log_path, encoding='utf-8', errors='replace') as fh:
                            for line in fh:
                                if ' ERROR ' in line or ' CRITICAL ' in line:
                                    if ' ERROR ' in line:
                                        error_count += 1
                                    else:
                                        critical_count += 1
                        stats['errors_24h'] = error_count
                        stats['criticals_24h'] = critical_count
                    except Exception:
                        stats['errors_24h'] = 'n/a'
                        stats['criticals_24h'] = 'n/a'
                    # Detect recent rotation: check for .1 backup file newer than 24h
                    backup = Path(str(log_path) + '.1')
                    if backup.exists() and (time.time() - backup.stat().st_mtime) < 86400:
                        stats['log_rotated_24h'] = True
                        stats['log_backup_size_mb'] = f'{backup.stat().st_size / 1_048_576:.1f}'
                    else:
                        stats['log_rotated_24h'] = False
        except Exception:
            pass

        # Data retention last run
        stats['retention'] = self._last_retention_stats.copy()

        return stats

    def _format_email_body(self, stats: dict[str, Any], period_start: str, period_end: str) -> str:
        lines = [
            'MeshCore Bot — Nightly Maintenance Report',
            '=' * 44,
            f'Period : {period_start} → {period_end}',
            '',
            'BOT STATUS',
            '─' * 30,
            f"  Uptime    : {stats.get('uptime', 'unknown')}",
            f"  Connected : {'yes' if getattr(self.bot, 'connected', False) else 'no'}",
            '',
            'NETWORK ACTIVITY (past 24 h)',
            '─' * 30,
            f"  Active contacts  : {stats.get('contacts_24h', 'n/a')}",
            f"  New contacts     : {stats.get('contacts_new_24h', 'n/a')}",
            f"  Total tracked    : {stats.get('contacts_total', 'n/a')}",
            '',
            'DATABASE',
            '─' * 30,
            f"  Size : {stats.get('db_size_mb', 'n/a')} MB",
        ]
        if self._last_retention_stats.get('ran_at'):
            lines.append(f"  Last retention run : {self._last_retention_stats['ran_at']} UTC")
        if self._last_retention_stats.get('error'):
            lines.append(f"  Retention error    : {self._last_retention_stats['error']}")

        lines += [
            '',
            'ERRORS (past 24 h)',
            '─' * 30,
            f"  ERROR    : {stats.get('errors_24h', 'n/a')}",
            f"  CRITICAL : {stats.get('criticals_24h', 'n/a')}",
        ]
        if stats.get('log_file'):
            lines += [
                '',
                'LOG FILES',
                '─' * 30,
                f"  Current : {stats.get('log_file')} ({stats.get('log_size_mb', '?')} MB)",
            ]
            if stats.get('log_rotated_24h'):
                lines.append(
                    f"  Rotated : yes — backup is {stats.get('log_backup_size_mb', '?')} MB"
                )
            else:
                lines.append('  Rotated : no')

        lines += [
            '',
            '─' * 44,
            'Manage notification settings: /config',
        ]
        return '\n'.join(lines)

    def _send_nightly_email(self) -> None:
        """Build and dispatch the nightly maintenance digest if enabled."""
        import smtplib
        import ssl as _ssl
        from email.message import EmailMessage

        if self._get_notif('nightly_enabled') != 'true':
            return

        smtp_host     = self._get_notif('smtp_host')
        smtp_security = self._get_notif('smtp_security') or 'starttls'
        smtp_user     = self._get_notif('smtp_user')
        smtp_password = self._get_notif('smtp_password')
        from_name     = self._get_notif('from_name') or 'MeshCore Bot'
        from_email    = self._get_notif('from_email')
        recipients    = [r.strip() for r in self._get_notif('recipients').split(',') if r.strip()]

        if not smtp_host or not from_email or not recipients:
            self.logger.warning(
                "Nightly email enabled but SMTP settings incomplete "
                f"(host={smtp_host!r}, from={from_email!r}, recipients={recipients})"
            )
            return

        try:
            smtp_port = int(self._get_notif('smtp_port') or (465 if smtp_security == 'ssl' else 587))
        except ValueError:
            smtp_port = 587

        now_utc   = datetime.datetime.utcnow()
        yesterday = now_utc - datetime.timedelta(days=1)
        period_start = yesterday.strftime('%Y-%m-%d %H:%M UTC')
        period_end   = now_utc.strftime('%Y-%m-%d %H:%M UTC')

        try:
            stats = self._collect_email_stats()
            body  = self._format_email_body(stats, period_start, period_end)

            msg = EmailMessage()
            msg['Subject'] = f'MeshCore Bot — Nightly Report {now_utc.strftime("%Y-%m-%d")}'
            msg['From']    = f'{from_name} <{from_email}>'
            msg['To']      = ', '.join(recipients)
            msg.set_content(body)

            # Optionally attach current log file before rotation
            if self._get_maint('email_attach_log') == 'true':
                log_file = self.bot.config.get('Logging', 'log_file', fallback='').strip()
                if log_file:
                    log_path = Path(log_file)
                    max_attach = 5 * 1024 * 1024  # 5 MB cap on attachment
                    if log_path.exists() and log_path.stat().st_size <= max_attach:
                        try:
                            with open(log_path, 'rb') as fh:
                                msg.add_attachment(fh.read(), maintype='text', subtype='plain',
                                                   filename=log_path.name)
                        except Exception as attach_err:
                            self.logger.warning(f"Could not attach log file to nightly email: {attach_err}")

            context = _ssl.create_default_context()

            _smtp_timeout = 30  # seconds — prevents indefinite hang on unreachable host
            if smtp_security == 'ssl':
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=_smtp_timeout) as s:
                    if smtp_user and smtp_password:
                        s.login(smtp_user, smtp_password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=_smtp_timeout) as s:
                    if smtp_security == 'starttls':
                        s.ehlo()
                        s.starttls(context=context)
                        s.ehlo()
                    if smtp_user and smtp_password:
                        s.login(smtp_user, smtp_password)
                    s.send_message(msg)

            self.logger.info(
                f"Nightly maintenance email sent to {recipients} "
                f"(contacts_24h={stats.get('contacts_24h')}, "
                f"errors={stats.get('errors_24h')})"
            )
            try:
                ran_at = datetime.datetime.utcnow().isoformat()
                self.bot.db_manager.set_metadata('maint.status.nightly_email_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.nightly_email_outcome', 'ok')
            except Exception:
                pass

        except Exception as e:
            self.logger.error(f"Failed to send nightly maintenance email: {e}")
            try:
                ran_at = datetime.datetime.utcnow().isoformat()
                self.bot.db_manager.set_metadata('maint.status.nightly_email_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.nightly_email_outcome', f'error: {e}')
            except Exception:
                pass

    # ── Zombie radio alert email ─────────────────────────────────────────────

    def send_zombie_alert_email(self, fail_count: int, threshold: int, interval: int) -> None:
        """Send an immediate alert email when a zombie radio is detected.

        Uses the same SMTP settings as the nightly digest.  Recipients are taken
        from the ``radio_zombie_alert_email`` config key; if that key is empty the
        nightly maintenance recipients are used as a fallback.

        This method is intentionally synchronous so it can be run in a thread
        executor from the async event loop without blocking it.
        """
        import smtplib
        import ssl as _ssl
        from email.message import EmailMessage

        if not self.bot.config.getboolean('Bot', 'radio_zombie_alert_enabled', fallback=True):
            return

        smtp_host     = self._get_notif('smtp_host')
        smtp_security = self._get_notif('smtp_security') or 'starttls'
        smtp_user     = self._get_notif('smtp_user')
        smtp_password = self._get_notif('smtp_password')
        from_name     = self._get_notif('from_name') or 'MeshCore Bot'
        from_email    = self._get_notif('from_email')

        # Alert recipients: dedicated config key, falls back to nightly recipients
        alert_email_cfg = self.bot.config.get('Bot', 'radio_zombie_alert_email', fallback='').strip()
        if alert_email_cfg:
            recipients = [r.strip() for r in alert_email_cfg.split(',') if r.strip()]
        else:
            recipients = [r.strip() for r in self._get_notif('recipients').split(',') if r.strip()]

        if not smtp_host or not from_email or not recipients:
            self.bot.logger.warning(
                "Zombie alert email enabled but SMTP settings incomplete "
                f"(host={smtp_host!r}, from={from_email!r}, recipients={recipients}) "
                "— alert email not sent"
            )
            return

        try:
            smtp_port = int(self._get_notif('smtp_port') or (465 if smtp_security == 'ssl' else 587))
        except ValueError:
            smtp_port = 587

        now_utc         = datetime.datetime.utcnow()
        connection_type = self.bot.config.get('Connection', 'connection_type', fallback='unknown')
        serial_port     = self.bot.config.get('Connection', 'serial_port', fallback='n/a')
        interval_min    = interval // 60

        subject = (
            f'ALERT: MeshCore Bot — Zombie Radio Detected '
            f'[{now_utc.strftime("%Y-%m-%d %H:%M UTC")}]'
        )
        body = '\n'.join([
            'MeshCore Bot — Zombie Radio Alert',
            '=' * 44,
            f'Time          : {now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")}',
            '',
            'RADIO STATUS',
            '─' * 30,
            f'  Connection    : {connection_type}',
            f'  Port / Device : {serial_port}',
            f'  Failed probes : {fail_count} of {threshold} (threshold)',
            f'  Probe interval: {interval}s ({interval_min} min)',
            '',
            'ACTION REQUIRED',
            '─' * 30,
            '  The radio firmware is unresponsive (zombie state).',
            '  A physical POWER CYCLE of the radio is required.',
            '  Disconnect/reconnect of the serial/BLE transport will NOT fix this.',
            '',
            '  Steps to recover:',
            '    1. Power off the radio hardware',
            '    2. Wait 10 seconds',
            '    3. Power on the radio hardware',
            '    4. The bot will reconnect and resume normal operation automatically',
            '',
            '─' * 44,
            'Probe monitoring has been suspended to avoid log spam.',
            'It will resume automatically after the next successful reconnect.',
        ])

        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From']    = f'{from_name} <{from_email}>'
            msg['To']      = ', '.join(recipients)
            msg.set_content(body)

            context = _ssl.create_default_context()
            _smtp_timeout = 30

            if smtp_security == 'ssl':
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=_smtp_timeout) as s:
                    if smtp_user and smtp_password:
                        s.login(smtp_user, smtp_password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=_smtp_timeout) as s:
                    if smtp_security == 'starttls':
                        s.ehlo()
                        s.starttls(context=context)
                        s.ehlo()
                    if smtp_user and smtp_password:
                        s.login(smtp_user, smtp_password)
                    s.send_message(msg)

            self.bot.logger.info(
                f"Zombie radio alert email sent to {recipients}"
            )
        except Exception as e:
            self.bot.logger.error(f"Failed to send zombie radio alert email: {e}")

    # ── Maintenance helpers ──────────────────────────────────────────────────

    def _get_maint(self, key: str) -> str:
        """Read a maintenance setting from bot_metadata."""
        try:
            val = self.bot.db_manager.get_metadata(f'maint.{key}')
            return val if val is not None else ''
        except Exception:
            return ''

    def _apply_log_rotation_config(self) -> None:
        """Check bot_metadata for log rotation settings and replace the RotatingFileHandler if changed."""
        from logging.handlers import RotatingFileHandler as _RFH

        max_bytes_str = self._get_maint('log_max_bytes')
        backup_count_str = self._get_maint('log_backup_count')

        if not max_bytes_str and not backup_count_str:
            return  # Nothing stored yet — nothing to apply

        new_cfg = {'max_bytes': max_bytes_str, 'backup_count': backup_count_str}
        if new_cfg == self._last_log_rotation_applied:
            return  # No change

        try:
            max_bytes = int(max_bytes_str) if max_bytes_str else 5 * 1024 * 1024
            backup_count = int(backup_count_str) if backup_count_str else 3
        except ValueError:
            self.logger.warning(f"Invalid log rotation config in bot_metadata: {new_cfg}")
            return

        logger = self.bot.logger
        for i, handler in enumerate(logger.handlers):
            if isinstance(handler, _RFH):
                log_path = handler.baseFilename
                formatter = handler.formatter
                level = handler.level
                handler.close()
                new_handler = _RFH(log_path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
                new_handler.setFormatter(formatter)
                new_handler.setLevel(level)
                logger.handlers[i] = new_handler
                self._last_log_rotation_applied = new_cfg
                self.logger.info(f"Log rotation config applied: maxBytes={max_bytes}, backupCount={backup_count}")
                try:
                    ran_at = datetime.datetime.utcnow().isoformat()
                    self.bot.db_manager.set_metadata('maint.status.log_rotation_applied_at', ran_at)
                except Exception:
                    pass
                break

    def _maybe_run_db_backup(self) -> None:
        """Check if a scheduled DB backup is due and run it."""
        if self._get_maint('db_backup_enabled') != 'true':
            return

        sched = self._get_maint('db_backup_schedule') or 'daily'
        if sched == 'manual':
            return

        backup_time_str = self._get_maint('db_backup_time') or '02:00'
        now = self.get_current_time()
        try:
            bh, bm = [int(x) for x in backup_time_str.split(':')]
        except Exception:
            bh, bm = 2, 0

        scheduled_today = now.replace(hour=bh, minute=bm, second=0, microsecond=0)

        # Only fire within a 2-minute window after the scheduled time.
        # This allows for scheduler lag while preventing a late bot startup
        # from triggering an immediate backup for a time that passed hours ago.
        fire_window_end = scheduled_today + datetime.timedelta(minutes=2)
        if now < scheduled_today or now > fire_window_end:
            return

        if sched == 'weekly' and now.weekday() != 0:  # Monday only
            return

        # Deduplicate: don't re-run if already ran today (daily) / this week (weekly).
        # Seed from DB on first check so restarts don't re-trigger a backup that
        # already ran earlier today.
        if not self._last_db_backup_stats:
            try:
                db_ran_at = self.bot.db_manager.get_metadata('maint.status.db_backup_ran_at') or ''
                if db_ran_at:
                    self._last_db_backup_stats['ran_at'] = db_ran_at
            except Exception:
                pass

        date_key = now.strftime('%Y-%m-%d')
        week_key = f"{now.year}-W{now.isocalendar()[1]}"
        last_ran = self._last_db_backup_stats.get('ran_at', '')
        if sched == 'daily' and last_ran.startswith(date_key):
            return
        if sched == 'weekly' and self._last_db_backup_stats.get('week_key') == week_key:
            return

        self._run_db_backup()
        if sched == 'weekly':
            self._last_db_backup_stats['week_key'] = week_key

    def _run_db_backup(self) -> None:
        """Backup the SQLite database using sqlite3.Connection.backup(), then prune old backups."""
        import sqlite3 as _sqlite3

        backup_dir_str = self._get_maint('db_backup_dir') or '/data/backups'
        try:
            retention_count = int(self._get_maint('db_backup_retention_count') or '7')
        except ValueError:
            retention_count = 7

        backup_dir = Path(backup_dir_str)
        ran_at = datetime.datetime.utcnow().isoformat()

        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.logger.error(f"DB backup: cannot create backup directory {backup_dir}: {e}")
            self._last_db_backup_stats = {'ran_at': ran_at, 'error': str(e)}
            try:
                self.bot.db_manager.set_metadata('maint.status.db_backup_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.db_backup_outcome', f'error: {e}')
            except Exception:
                pass
            return

        db_path = Path(str(self.bot.db_manager.db_path))
        ts = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')
        backup_path = backup_dir / f"{db_path.stem}_{ts}.db"

        try:
            src = _sqlite3.connect(str(db_path), check_same_thread=False)
            dst = _sqlite3.connect(str(backup_path))
            try:
                src.backup(dst, pages=200)
            finally:
                dst.close()
                src.close()

            size_mb = backup_path.stat().st_size / 1_048_576
            self.logger.info(f"DB backup created: {backup_path} ({size_mb:.1f} MB)")

            # Prune oldest backups beyond retention count
            stem = db_path.stem
            backups = sorted(backup_dir.glob(f"{stem}_*.db"), key=lambda p: p.stat().st_mtime)
            while len(backups) > retention_count:
                oldest = backups.pop(0)
                try:
                    oldest.unlink()
                    self.logger.info(f"DB backup pruned: {oldest}")
                except OSError:
                    pass

            ran_at = datetime.datetime.utcnow().isoformat()
            self._last_db_backup_stats = {'ran_at': ran_at, 'path': str(backup_path), 'size_mb': f'{size_mb:.1f}'}
            try:
                self.bot.db_manager.set_metadata('maint.status.db_backup_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.db_backup_outcome', 'ok')
                self.bot.db_manager.set_metadata('maint.status.db_backup_path', str(backup_path))
            except Exception:
                pass

        except Exception as e:
            self.logger.error(f"DB backup failed: {e}")
            self._last_db_backup_stats = {'ran_at': ran_at, 'error': str(e)}
            try:
                self.bot.db_manager.set_metadata('maint.status.db_backup_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.db_backup_outcome', f'error: {e}')
            except Exception:
                pass
