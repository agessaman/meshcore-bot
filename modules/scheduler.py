#!/usr/bin/env python3
"""
Message scheduler functionality for the MeshCore Bot
Handles scheduled messages and timing
"""

import asyncio
import datetime
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from meshcore.events import EventType

from .maintenance import MaintenanceRunner
from .scheduled_message_cron import is_valid_legacy_hhmm, parse_schedule_key
from .security_utils import validate_external_url
from .utils import decode_escape_sequences, format_keyword_response_with_placeholders, get_config_timezone

# process_message_queue may await long per-feed intervals across many queued items; 30s is too short.
_FEED_MESSAGE_QUEUE_FUTURE_TIMEOUT = 600


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
        # Align with nightly email: first retention run after ~24h uptime (not immediately on boot).
        self.last_data_retention_run = time.time()
        self._data_retention_interval_seconds = 86400  # 24 hours
        self.last_nightly_email_time = time.time()     # don't send immediately on startup
        self.last_db_backup_run = 0
        self.last_log_rotation_check_time = 0
        self.maintenance = MaintenanceRunner(bot, get_current_time=self.get_current_time)

    def get_current_time(self):
        """Get current time in configured timezone"""
        tz, _ = get_config_timezone(self.bot.config, self.logger)
        return datetime.datetime.now(tz)

    def _shutdown_apscheduler_if_running(self) -> None:
        """Stop APScheduler if it exists and is running (idempotent, no spurious errors)."""
        if self._apscheduler is None:
            return
        if not getattr(self._apscheduler, "running", False):
            return
        try:
            self._apscheduler.shutdown(wait=False)
        except Exception as e:
            self.logger.debug("Error shutting down scheduler: %s", e)

    def setup_scheduled_messages(self):
        """Setup scheduled messages from config using APScheduler."""
        # Stop and recreate the APScheduler to avoid duplicate jobs on reload
        self._shutdown_apscheduler_if_running()
        tz, _ = get_config_timezone(self.bot.config, self.logger)
        self._apscheduler = BackgroundScheduler(timezone=tz)
        self.scheduled_messages.clear()

        if self.bot.config.has_section('Scheduled_Messages'):
            self.logger.info("Found Scheduled_Messages section")
            for schedule_key, message_info in self.bot.config.items('Scheduled_Messages'):
                self.logger.info(f"Processing scheduled message: '{schedule_key}' -> '{message_info}'")
                try:
                    parsed = parse_schedule_key(schedule_key, tz)
                    if parsed.trigger is None:
                        self.logger.warning(
                            f"Invalid schedule '{schedule_key}' for scheduled message: {message_info}"
                        )
                        continue

                    if parsed.is_deprecated_hhmm:
                        hh = int(schedule_key[:2])
                        mm = int(schedule_key[2:])
                        cron_suggestion = f"{mm} {hh} * * *"
                        self.logger.warning(
                            "Scheduled_Messages key %r uses deprecated HHMM daily format; "
                            "migrate to 5-field cron (minute hour dom mon dow), e.g. %r. "
                            "HHMM support will be removed in a future release.",
                            schedule_key,
                            cron_suggestion,
                        )

                    channel, message = message_info.split(':', 1)
                    channel = channel.strip()
                    message = decode_escape_sequences(message.strip())

                    job_id = "schedmsg_" + hashlib.sha256(
                        f"{schedule_key}\0{channel}\0{message}".encode()
                    ).hexdigest()[:24]

                    self._apscheduler.add_job(
                        self.send_scheduled_message,
                        parsed.trigger,
                        args=[channel, message],
                        id=job_id,
                        replace_existing=True,
                    )
                    self.scheduled_messages[schedule_key] = (channel, message, parsed.display_label)
                    self.logger.info(
                        f"Scheduled message: {parsed.display_label} -> {channel}: {message}"
                    )
                except ValueError:
                    self.logger.warning(f"Invalid scheduled message format: {message_info}")
                except Exception as e:
                    self.logger.warning(f"Error setting up scheduled message '{schedule_key}': {e}")

        self._apscheduler.start()
        self.logger.info(f"APScheduler started with {len(self.scheduled_messages)} scheduled message(s)")

        self._setup_device_mode_scheduler_jobs()

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

    def _setup_device_mode_scheduler_jobs(self) -> None:
        """One-shot jobs for auto_manage_contacts=device: firmware autoadd + favourite hygiene."""
        if self._apscheduler is None:
            return
        if self.bot.config.get('Bot', 'auto_manage_contacts', fallback='false').lower() != 'device':
            return
        try:
            delay_fw = max(0, self.bot.config.getint('Bot', 'device_mode_firmware_delay_seconds', fallback=30))
            delay_p1 = max(0, self.bot.config.getint('Bot', 'device_mode_favourite_pass1_delay_seconds', fallback=90))
            delay_p2 = max(0, self.bot.config.getint('Bot', 'device_mode_favourite_pass2_delay_seconds', fallback=180))
            base = self.get_current_time()
            self._apscheduler.add_job(
                self._device_mode_firmware_job_sync,
                trigger=DateTrigger(run_date=base + datetime.timedelta(seconds=delay_fw)),
                id='device_mode_firmware_autoadd',
                replace_existing=True,
            )
            self._apscheduler.add_job(
                self._device_mode_favourite_pass1_job_sync,
                trigger=DateTrigger(run_date=base + datetime.timedelta(seconds=delay_p1)),
                id='device_mode_favourite_pass1',
                replace_existing=True,
            )
            self._apscheduler.add_job(
                self._device_mode_favourite_pass2_job_sync,
                trigger=DateTrigger(run_date=base + datetime.timedelta(seconds=delay_p2)),
                id='device_mode_favourite_pass2',
                replace_existing=True,
            )
            self.logger.info(
                'Scheduled device-mode jobs: firmware +%ss, favourite pass1 +%ss, pass2 +%ss',
                delay_fw,
                delay_p1,
                delay_p2,
            )
        except Exception as e:
            self.logger.warning('Could not schedule device-mode contact jobs: %s', e)

    def _run_async_on_main_loop(self, coro: Any, timeout: float = 300.0) -> None:
        """Run async coroutine on bot main loop from APScheduler thread (same pattern as send_scheduled_message)."""
        import asyncio

        if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self.bot.main_event_loop)
            try:
                future.result(timeout=timeout)
            except RuntimeError as e:
                self.logger.warning('Event loop gone during device-mode job: %s', e)
            except Exception as e:
                self.logger.error('Device-mode scheduled job failed: %s', e)
        else:
            self.logger.warning('No running main_event_loop — skipping device-mode scheduled job')

    async def _device_mode_firmware_coro(self) -> None:
        await self.bot.repeater_manager.apply_device_mode_firmware_preferences()

    async def _device_mode_favourite_pass1_coro(self) -> None:
        await self.bot.repeater_manager.sync_device_mode_favourites_pass1()

    async def _device_mode_favourite_pass2_coro(self) -> None:
        await self.bot.repeater_manager.sync_device_mode_favourites_pass2()

    def _device_mode_firmware_job_sync(self) -> None:
        if self.bot.config.get('Bot', 'auto_manage_contacts', fallback='false').lower() != 'device':
            self.logger.debug('Skipping device_mode_firmware job — not device mode')
            return
        self._run_async_on_main_loop(self._device_mode_firmware_coro(), timeout=120.0)

    def _device_mode_favourite_pass1_job_sync(self) -> None:
        if self.bot.config.get('Bot', 'auto_manage_contacts', fallback='false').lower() != 'device':
            return
        self._run_async_on_main_loop(self._device_mode_favourite_pass1_coro(), timeout=600.0)

    def _device_mode_favourite_pass2_job_sync(self) -> None:
        if self.bot.config.get('Bot', 'auto_manage_contacts', fallback='false').lower() != 'device':
            return
        self._run_async_on_main_loop(self._device_mode_favourite_pass2_coro(), timeout=600.0)

    def _is_valid_time_format(self, time_str: str) -> bool:
        """Validate deprecated legacy time format (HHMM). Prefer cron in config keys."""
        return is_valid_legacy_hhmm(time_str)

    def send_scheduled_message(self, channel: str, message: str):
        """Send a scheduled message (synchronous wrapper for schedule library)"""
        if self.bot.is_radio_zombie:
            self.logger.warning("send_scheduled_message suppressed — radio is in zombie state")
            return
        if self.bot.is_radio_offline:
            self.logger.warning("send_scheduled_message suppressed — radio is offline (repeated send timeouts)")
            return

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
                self.bot._record_send_success()
            except RuntimeError as e:
                self.logger.warning("Event loop gone during scheduled message: %s", e)
            except Exception as e:
                self.logger.error(f"Error sending scheduled message: {type(e).__name__}: {e}")
                self.bot._record_send_failure(scheduler=self)
        else:
            # Fallback: create a temporary event loop and close it when done
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._send_scheduled_message_async(channel, message))
            finally:
                loop.close()

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
                except Exception as e:
                    self.logger.debug("Error querying message_stats: %s", e)

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

        import asyncio as _asyncio
        send_timeout = self.bot.config.getint('Bot', 'send_timeout_seconds', fallback=30)
        await _asyncio.wait_for(
            self.bot.command_manager.send_channel_message(channel, message),
            timeout=send_timeout,
        )

    def start(self):
        """Start the scheduler in a separate thread"""
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()

    def join(self, timeout: float = 5.0) -> None:
        """Wait for the scheduler thread to finish and stop APScheduler (e.g. during shutdown)."""
        self._shutdown_apscheduler_if_running()
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
                        # Fallback: create a temporary event loop and close it when done
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(self.bot.feed_manager.poll_all_feeds())
                            self.logger.debug("Feed polling cycle completed")
                        except Exception as e:
                            self.logger.error(f"Error in feed polling cycle: {e}")
                        finally:
                            loop.close()
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
                        try:
                            future.result(timeout=_FEED_MESSAGE_QUEUE_FUTURE_TIMEOUT)
                        except TimeoutError:
                            self.logger.warning(
                                "Timed out waiting for feed message queue after %ss; "
                                "work may still be running on the main loop (per-feed send spacing).",
                                _FEED_MESSAGE_QUEUE_FUTURE_TIMEOUT,
                            )
                        except RuntimeError as e:
                            self.logger.warning("Event loop gone during feed message queue: %s", e)
                        except Exception as e:
                            self.logger.exception(f"Error processing message queue: {e}")
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
                self.maintenance.run_data_retention()
                self.last_data_retention_run = time.time()

            # Nightly maintenance email (24 h interval, after retention so stats are fresh)
            if time.time() - self.last_nightly_email_time >= self._data_retention_interval_seconds:
                self.maintenance.send_nightly_email()
                self.last_nightly_email_time = time.time()

            # Log rotation live-apply: check bot_metadata for config changes every 60 s
            if time.time() - self.last_log_rotation_check_time >= 60:
                self.maintenance.apply_log_rotation_config()
                self.last_log_rotation_check_time = time.time()

            # DB backup: evaluate schedule every 5 minutes
            if time.time() - self.last_db_backup_run >= 300:
                self.maintenance.maybe_run_db_backup()
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
                    except RuntimeError as e:
                        self.logger.warning("Event loop gone during cleanup_database: %s", e)
                    except Exception as e:
                        self.logger.error(f"Error in repeater_manager.cleanup_database: {type(e).__name__}: {e}")
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

            ran_at = datetime.datetime.now(datetime.UTC).isoformat()
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
                ran_at = datetime.datetime.now(datetime.UTC).isoformat()
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
        if self.bot.is_radio_zombie:
            self.logger.warning("send_interval_advert suppressed — radio is in zombie state")
            return
        if self.bot.is_radio_offline:
            self.logger.warning("send_interval_advert suppressed — radio is offline (repeated send timeouts)")
            return

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
                self.bot._record_send_success()
            except Exception as e:
                self.logger.error(f"Error sending interval advert: {type(e).__name__}: {e}")
                self.bot._record_send_failure(scheduler=self)
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
        import asyncio

        from meshcore.events import EventType
        try:
            result = await asyncio.wait_for(
                self.bot.meshcore.commands.send_advert(flood=True),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            # Feed interval advert timeouts into existing zombie-detection heuristics.
            self.bot._radio_fail_count = getattr(self.bot, "_radio_fail_count", 0) + 1
            self.logger.warning(
                "send_interval_advert timed out after 30s; "
                "_radio_fail_count=%d",
                self.bot._radio_fail_count,
            )
            raise
        if hasattr(result, 'type') and result.type == EventType.ERROR:
            reason = (result.payload or {}).get('reason', 'unknown') if hasattr(result, 'payload') else 'unknown'
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
                      AND operation_type IN ('add', 'remove')
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
                          'firmware_read', 'firmware_write',
                          'radio_params_read', 'radio_params_write'
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
                elif op_type == 'radio_params_read':
                    success, result_payload = await self._radio_params_read_op()
                elif op_type == 'radio_params_write':
                    payload = json.loads(op['payload_data'] or '{}')
                    success, result_payload = await self._radio_params_write_op(payload)
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

    async def _radio_params_read_op(self):
        """Read current radio parameters (freq, bw, sf, cr, tx_power) via SELF_INFO."""
        try:
            meshcore = getattr(self.bot, 'meshcore', None)
            if not meshcore or not getattr(meshcore, 'is_connected', False):
                return False, {'error': 'Radio not connected'}

            event = await asyncio.wait_for(
                meshcore.commands.send_appstart(), timeout=10
            )
            if event is None or event.type == EventType.ERROR:
                return False, {'error': 'Failed to read radio parameters'}

            p = event.payload or {}
            return True, {
                'freq': p.get('radio_freq'),
                'bw': p.get('radio_bw'),
                'sf': p.get('radio_sf'),
                'cr': p.get('radio_cr'),
                'tx_power': p.get('tx_power'),
                'max_tx_power': p.get('max_tx_power'),
            }
        except Exception as e:
            self.logger.error(f"Radio params read failed: {e}")
            return False, {'error': str(e)}

    async def _radio_params_write_op(self, payload: dict):
        """Write radio parameters (freq, bw, sf, cr, tx_power) to device."""
        try:
            meshcore = getattr(self.bot, 'meshcore', None)
            if not meshcore or not getattr(meshcore, 'is_connected', False):
                return False, {'error': 'Radio not connected'}

            results = {}
            errors = []

            if any(k in payload for k in ('freq', 'bw', 'sf', 'cr')):
                freq = float(payload['freq'])
                bw = float(payload['bw'])
                sf = int(payload['sf'])
                cr = int(payload['cr'])
                result = await asyncio.wait_for(
                    meshcore.commands.set_radio(freq, bw, sf, cr), timeout=10
                )
                ok = getattr(result, 'type', None) == EventType.OK
                results['radio'] = ok
                if not ok:
                    errors.append(f"set_radio failed: {result}")

            if 'tx_power' in payload:
                result = await asyncio.wait_for(
                    meshcore.commands.set_tx_power(int(payload['tx_power'])), timeout=10
                )
                ok = getattr(result, 'type', None) == EventType.OK
                results['tx_power'] = ok
                if not ok:
                    errors.append(f"set_tx_power failed: {result}")

            success = len(errors) == 0
            response: dict = {'results': results}
            if errors:
                response['errors'] = errors
            return success, response
        except Exception as e:
            self.logger.error(f"Radio params write failed: {e}")
            return False, {'error': str(e)}

    # ── Maintenance (delegates to MaintenanceRunner) ─────────────────────────

    @property
    def _last_retention_stats(self) -> dict[str, Any]:
        return self.maintenance._last_retention_stats

    @_last_retention_stats.setter
    def _last_retention_stats(self, value: dict[str, Any]) -> None:
        self.maintenance._last_retention_stats.clear()
        self.maintenance._last_retention_stats.update(value)

    @property
    def _last_db_backup_stats(self) -> dict[str, Any]:
        return self.maintenance._last_db_backup_stats

    @_last_db_backup_stats.setter
    def _last_db_backup_stats(self, value: dict[str, Any]) -> None:
        self.maintenance._last_db_backup_stats.clear()
        self.maintenance._last_db_backup_stats.update(value)

    @property
    def _last_log_rotation_applied(self) -> dict[str, str]:
        return self.maintenance._last_log_rotation_applied

    @_last_log_rotation_applied.setter
    def _last_log_rotation_applied(self, value: dict[str, str]) -> None:
        self.maintenance._last_log_rotation_applied.clear()
        self.maintenance._last_log_rotation_applied.update(value)

    def run_db_backup(self) -> None:
        """Run a DB backup immediately (manual / HTTP)."""
        self.maintenance.run_db_backup()

    def _run_data_retention(self) -> None:
        self.maintenance.run_data_retention()

    def _get_notif(self, key: str) -> str:
        return self.maintenance.get_notif(key)

    def _collect_email_stats(self) -> dict[str, Any]:
        return self.maintenance.collect_email_stats()

    def _format_email_body(self, stats: dict[str, Any], period_start: str, period_end: str) -> str:
        return self.maintenance.format_email_body(stats, period_start, period_end)

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

        zombie_alert_enabled = self.bot.config.getboolean(
            'Connection',
            'radio_zombie_alert_enabled',
            fallback=self.bot.config.getboolean('Bot', 'radio_zombie_alert_enabled', fallback=False),
        )
        zombie_alert_email_cfg: str | None = None
        db_manager = getattr(self.bot, 'db_manager', None)
        if db_manager is not None and hasattr(db_manager, 'get_metadata'):
            try:
                meta_enabled = db_manager.get_metadata('zombie.alert_enabled')
                if isinstance(meta_enabled, str) and meta_enabled.strip():
                    zombie_alert_enabled = meta_enabled.strip().lower() in {
                        '1', 'true', 'yes', 'on',
                    }
                meta_email = db_manager.get_metadata('zombie.alert_email')
                if isinstance(meta_email, str) and meta_email.strip():
                    zombie_alert_email_cfg = meta_email.strip()
            except Exception:
                pass
        if not zombie_alert_enabled:
            return

        smtp_host     = self._get_notif('smtp_host')
        smtp_security = self._get_notif('smtp_security') or 'starttls'
        smtp_user     = self._get_notif('smtp_user')
        smtp_password = self._get_notif('smtp_password')
        from_name     = self._get_notif('from_name') or 'MeshCore Bot'
        from_email    = self._get_notif('from_email')

        # Alert recipients: dedicated config key, falls back to nightly recipients
        alert_email_cfg = zombie_alert_email_cfg or self.bot.config.get(
            'Connection',
            'radio_zombie_alert_email',
            fallback=self.bot.config.get('Bot', 'radio_zombie_alert_email', fallback=''),
        ).strip()
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

        allow_local = self._get_notif('allow_local_smtp').lower() == 'true'
        if not validate_external_url(f'http://{smtp_host}', allow_private=allow_local):
            self.bot.logger.error(
                "Zombie alert email aborted: SMTP host %r resolves to a private or reserved address",
                smtp_host,
            )
            return

        try:
            smtp_port = int(self._get_notif('smtp_port') or (465 if smtp_security == 'ssl' else 587))
        except ValueError:
            smtp_port = 587

        now_utc         = datetime.datetime.now(datetime.UTC)
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
        return self.maintenance.get_maint(key)

    def _apply_log_rotation_config(self) -> None:
        self.maintenance.apply_log_rotation_config()

    def _maybe_run_db_backup(self) -> None:
        self.maintenance.maybe_run_db_backup()

    def _run_db_backup(self) -> None:
        self.maintenance.run_db_backup()

    # ── Radio offline alert email ────────────────────────────────────────────

    def send_radio_offline_alert_email(self, fail_count: int, threshold: int) -> None:
        """Send an immediate alert email when the radio-offline state is entered.

        Uses the same SMTP settings as the nightly digest.  Recipients are taken
        from the ``radio_offline_alert_email`` config key; if that key is empty the
        nightly maintenance recipients are used as a fallback.

        Intentionally synchronous — intended to be run in a daemon thread.
        """
        import smtplib
        import ssl as _ssl
        from email.message import EmailMessage

        alert_enabled = self.bot.config.getboolean(
            'Connection',
            'radio_offline_alert_enabled',
            fallback=self.bot.config.getboolean('Bot', 'radio_offline_alert_enabled', fallback=False),
        )
        if not alert_enabled:
            return

        smtp_host     = self._get_notif('smtp_host')
        smtp_security = self._get_notif('smtp_security') or 'starttls'
        smtp_user     = self._get_notif('smtp_user')
        smtp_password = self._get_notif('smtp_password')
        from_name     = self._get_notif('from_name') or 'MeshCore Bot'
        from_email    = self._get_notif('from_email')

        alert_email_cfg = self.bot.config.get(
            'Connection',
            'radio_offline_alert_email',
            fallback=self.bot.config.get('Bot', 'radio_offline_alert_email', fallback=''),
        ).strip()
        if alert_email_cfg:
            recipients = [r.strip() for r in alert_email_cfg.split(',') if r.strip()]
        else:
            recipients = [r.strip() for r in self._get_notif('recipients').split(',') if r.strip()]

        if not smtp_host or not from_email or not recipients:
            self.bot.logger.warning(
                "Radio-offline alert email enabled but SMTP settings incomplete "
                f"(host={smtp_host!r}, from={from_email!r}, recipients={recipients}) "
                "— alert email not sent"
            )
            return

        allow_local = self._get_notif('allow_local_smtp').lower() == 'true'
        if not validate_external_url(f'http://{smtp_host}', allow_private=allow_local):
            self.bot.logger.error(
                "Radio-offline alert email aborted: SMTP host %r resolves to a private or reserved address",
                smtp_host,
            )
            return

        try:
            smtp_port = int(self._get_notif('smtp_port') or (465 if smtp_security == 'ssl' else 587))
        except ValueError:
            smtp_port = 587

        now_utc         = datetime.datetime.now(datetime.UTC)
        connection_type = self.bot.config.get('Connection', 'connection_type', fallback='unknown')
        serial_port     = self.bot.config.get('Connection', 'serial_port', fallback='n/a')

        subject = (
            f'ALERT: MeshCore Bot — Radio Offline '
            f'[{now_utc.strftime("%Y-%m-%d %H:%M UTC")}]'
        )
        body = '\n'.join([
            'MeshCore Bot — Radio Offline Alert',
            '=' * 44,
            f'Time          : {now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")}',
            '',
            'RADIO STATUS',
            '─' * 30,
            f'  Connection      : {connection_type}',
            f'  Port / Device   : {serial_port}',
            f'  Failed sends    : {fail_count} of {threshold} (threshold)',
            '',
            'WHAT THIS MEANS',
            '─' * 30,
            '  The bot can no longer send outbound messages to the mesh.',
            '  Inbound packets from the radio may still be arriving normally.',
            '  This is NOT a zombie (firmware lock-up) — the radio is responsive',
            '  but outbound sends are timing out.',
            '',
            'ACTION REQUIRED',
            '─' * 30,
            '  Check the radio power supply and physical connection.',
            '  Use the dashboard "Clear Offline Flag" button once the issue',
            '  is resolved, or restart the bot to auto-probe.',
            '',
            '─' * 44,
            'Outbound sends will be suppressed until the offline flag is cleared.',
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

            self.bot.logger.info(f"Radio-offline alert email sent to {recipients}")
        except Exception as e:
            self.bot.logger.error(f"Failed to send radio-offline alert email: {e}")

    # ── Maintenance helpers ──────────────────────────────────────────────────

