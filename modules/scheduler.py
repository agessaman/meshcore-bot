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
from typing import Dict, Tuple
from pathlib import Path


class MessageScheduler:
    """Manages scheduled messages and timing"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.scheduled_messages = {}
        self.scheduler_thread = None
    
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
                    # Convert HHMM to HH:MM for scheduler
                    hour = int(time_str[:2])
                    minute = int(time_str[2:])
                    schedule_time = f"{hour:02d}:{minute:02d}"
                    
                    schedule.every().day.at(schedule_time).do(
                        self.send_scheduled_message, channel.strip(), message.strip()
                    )
                    self.scheduled_messages[time_str] = (channel.strip(), message.strip())
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
        
        # Create a new event loop for this thread if one doesn't exist
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # Run the async function in the event loop
        loop.run_until_complete(self._send_scheduled_message_async(channel, message))
    
    async def _send_scheduled_message_async(self, channel: str, message: str):
        """Send a scheduled message (async implementation)"""
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
        
        while self.bot.connected:
            current_time = self.get_current_time()
            
            # Log current time every 5 minutes for debugging
            if time.time() - last_log_time > 300:  # 5 minutes
                self.logger.info(f"Scheduler running - Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                last_log_time = time.time()
            
            # Check for pending scheduled messages
            pending_jobs = schedule.get_jobs()
            if pending_jobs:
                self.logger.debug(f"Found {len(pending_jobs)} scheduled jobs")
            
            # Check for interval-based advertising
            self.check_interval_advertising()
            
            # Poll feeds every minute (but feeds themselves control their check intervals)
            if time.time() - last_feed_poll_time >= 60:  # Every 60 seconds
                if (hasattr(self.bot, 'feed_manager') and self.bot.feed_manager and 
                    hasattr(self.bot.feed_manager, 'enabled') and self.bot.feed_manager.enabled and
                    hasattr(self.bot, 'connected') and self.bot.connected):
                    # Run feed polling in async context
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    
                    # Schedule feed polling
                    try:
                        loop.run_until_complete(self.bot.feed_manager.poll_all_feeds())
                        self.logger.debug("Feed polling cycle completed")
                    except Exception as e:
                        self.logger.error(f"Error in feed polling cycle: {e}")
                    last_feed_poll_time = time.time()
            
            # Channels are fetched once on launch only - no periodic refresh
            # This prevents losing channels during incomplete updates
            
            # Process pending channel operations from web viewer (every 5 seconds)
            if not hasattr(self, 'last_channel_ops_check_time'):
                self.last_channel_ops_check_time = 0
            
            if time.time() - self.last_channel_ops_check_time >= 5:  # Every 5 seconds
                if (hasattr(self.bot, 'channel_manager') and self.bot.channel_manager and 
                    hasattr(self.bot, 'connected') and self.bot.connected):
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    
                    loop.run_until_complete(self._process_channel_operations())
                    self.last_channel_ops_check_time = time.time()
            
            # Process feed message queue (every 2 seconds)
            if not hasattr(self, 'last_message_queue_check_time'):
                self.last_message_queue_check_time = 0
            
            if time.time() - self.last_message_queue_check_time >= 2:  # Every 2 seconds
                if (hasattr(self.bot, 'feed_manager') and self.bot.feed_manager and 
                    hasattr(self.bot, 'connected') and self.bot.connected):
                    import asyncio
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
        
        # Create a new event loop for this thread if one doesn't exist
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
            db_path = self.bot.db_manager.db_path
            
            # Get pending operations
            with sqlite3.connect(db_path) as conn:
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
                    with sqlite3.connect(db_path) as conn:
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
                        with sqlite3.connect(db_path) as conn:
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
            self.logger.error(f"Error in _process_channel_operations: {e}")
            self.logger.error(f"Database path: {db_path} (exists: {Path(db_path).exists() if isinstance(db_path, str) else False})")
