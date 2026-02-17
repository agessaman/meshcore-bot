#!/usr/bin/env python3
"""
Message scheduler functionality for the MeshCore Bot
Handles scheduled messages and timing
"""

import time
import threading
import schedule
import datetime
import pytz
import sqlite3
import json
import os
from typing import Dict, Tuple, Any
from pathlib import Path
from .utils import decode_escape_sequences, format_keyword_response_with_placeholders


class MessageScheduler:
    """Manages scheduled messages and timing"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.scheduled_messages = {}
        self.scheduler_thread = None
        self.last_channel_ops_check_time = 0
        self.last_message_queue_check_time = 0
    
    def get_current_time(self):
        """Get current time in configured timezone"""
        timezone_str = self.bot.config.get('Bot', 'timezone', fallback='')
        
        if timezone_str:
            try:
                tz = pytz.timezone(timezone_str)
                return datetime.datetime.now(tz)
            except pytz.exceptions.UnknownTimeZoneError:
                self.logger.warning(f"Invalid timezone '{timezone_str}', using system timezone")
                return datetime.datetime.now()
        else:
            return datetime.datetime.now()
    
    def setup_scheduled_messages(self):
        """Setup scheduled messages from config"""
        # Clear existing scheduled jobs to avoid duplicates on reload
        schedule.clear()
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
                    # Convert HHMM to HH:MM for scheduler
                    hour = int(time_str[:2])
                    minute = int(time_str[2:])
                    schedule_time = f"{hour:02d}:{minute:02d}"
                    
                    schedule.every().day.at(schedule_time).do(
                        self.send_scheduled_message, channel, message
                    )
                    self.scheduled_messages[time_str] = (channel, message)
                    self.logger.info(f"Scheduled message: {schedule_time} -> {channel}: {message}")
                except ValueError:
                    self.logger.warning(f"Invalid scheduled message format: {message_info}")
                except Exception as e:
                    self.logger.warning(f"Error setting up scheduled message '{time_str}': {e}")
        
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
                self.logger.error(f"Error sending scheduled message: {e}")
        else:
            # Fallback: create new event loop if main loop not available
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # Run the async function in the event loop
            loop.run_until_complete(self._send_scheduled_message_async(channel, message))
    
    async def _get_mesh_info(self) -> Dict[str, Any]:
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
                    with sqlite3.connect(self.bot.db_manager.db_path, timeout=30.0) as conn:
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
                with sqlite3.connect(self.bot.db_manager.db_path, timeout=30.0) as conn:
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
                    self.logger.debug(f"Replaced mesh info placeholders in scheduled message")
                except (KeyError, ValueError) as e:
                    self.logger.warning(f"Error replacing placeholders in scheduled message: {e}. Sending message as-is.")
            except Exception as e:
                self.logger.warning(f"Error fetching mesh info for scheduled message: {e}. Sending message as-is.")
        
        await self.bot.command_manager.send_channel_message(channel, message)
    
    def start(self):
        """Start the scheduler in a separate thread"""
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()
    
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
            
            # Check for pending scheduled messages (only log when count changes, max once per 30 seconds)
            pending_jobs = schedule.get_jobs()
            current_job_count = len(pending_jobs) if pending_jobs else 0
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
                        try:
                            future.result(timeout=120)  # 2 minute timeout for feed polling
                            self.logger.debug("Feed polling cycle completed")
                        except Exception as e:
                            self.logger.error(f"Error in feed polling cycle: {e}")
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
                        try:
                            future.result(timeout=30)  # 30 second timeout
                        except Exception as e:
                            self.logger.error(f"Error processing channel operations: {e}")
                    else:
                        # Fallback: create new event loop if main loop not available
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                        
                        loop.run_until_complete(self._process_channel_operations())
                    self.last_channel_ops_check_time = time.time()
            
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
                            future.result(timeout=30)  # 30 second timeout
                        except Exception as e:
                            self.logger.error(f"Error processing message queue: {e}")
                    else:
                        # Fallback: create new event loop if main loop not available
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                        
                        loop.run_until_complete(self.bot.feed_manager.process_message_queue())
                    self.last_message_queue_check_time = time.time()
            
            schedule.run_pending()
            time.sleep(1)
        
        self.logger.info("Scheduler thread stopped")
    
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
                self.logger.error(f"Error sending interval advert: {e}")
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
        try:
            # Use the same advert functionality as the manual advert command
            await self.bot.meshcore.commands.send_advert(flood=True)
            self.logger.info("Interval-based flood advert sent successfully")
        except Exception as e:
            self.logger.error(f"Error sending interval-based advert: {e}")
    
    async def _process_channel_operations(self):
        """Process pending channel operations from the web viewer"""
        try:
            db_path = str(self.bot.db_manager.db_path)  # Ensure string, not Path object
            
            # Get pending operations
            with sqlite3.connect(db_path, timeout=30.0) as conn:
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
                    with sqlite3.connect(db_path, timeout=30.0) as conn:
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
                        with sqlite3.connect(db_path, timeout=30.0) as conn:
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
            self.logger.error(f"Error in _process_channel_operations: {e}")
            if db_path_str != 'unknown':
                path_obj = Path(db_path_str)
                self.logger.error(f"Database path: {db_path_str} (exists: {path_obj.exists()}, readable: {os.access(db_path_str, os.R_OK) if path_obj.exists() else False}, writable: {os.access(db_path_str, os.W_OK) if path_obj.exists() else False})")
                # Check parent directory permissions
                if path_obj.exists():
                    parent = path_obj.parent
                    self.logger.error(f"Parent directory: {parent} (exists: {parent.exists()}, writable: {os.access(str(parent), os.W_OK) if parent.exists() else False})")
            else:
                self.logger.error(f"Database path: {db_path_str}")
