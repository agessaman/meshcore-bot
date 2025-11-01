#!/usr/bin/env python3
"""
Repeater Contact Management System
Manages a database of repeater contacts and provides purging functionality
"""

import sqlite3
import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from meshcore import EventType



class RepeaterManager:
    """Manages repeater contacts database and purging operations"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.db_path = bot.db_manager.db_path
        
        # Use the shared database manager
        self.db_manager = bot.db_manager
        
        # Initialize repeater-specific tables
        self._init_repeater_tables()
        
        # Check for and handle database schema migration
        self._migrate_database_schema()
        
        # Initialize auto-purge monitoring
        self.contact_limit = 300  # MeshCore device limit (will be updated from device info)
        self.auto_purge_threshold = 280  # Start purging when 280+ contacts
        self.auto_purge_enabled = True
    
    def _init_repeater_tables(self):
        """Initialize repeater-specific database tables"""
        try:
            # Create repeater_contacts table
            self.db_manager.create_table('repeater_contacts', '''
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                device_type TEXT NOT NULL,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                contact_data TEXT,
                latitude REAL,
                longitude REAL,
                city TEXT,
                state TEXT,
                country TEXT,
                is_active BOOLEAN DEFAULT 1,
                purge_count INTEGER DEFAULT 0
            ''')
            
            # Create complete_contact_tracking table for all heard contacts
            self.db_manager.create_table('complete_contact_tracking', '''
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_key TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                device_type TEXT,
                first_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                advert_count INTEGER DEFAULT 1,
                latitude REAL,
                longitude REAL,
                city TEXT,
                state TEXT,
                country TEXT,
                raw_advert_data TEXT,
                signal_strength REAL,
                snr REAL,
                hop_count INTEGER,
                is_currently_tracked BOOLEAN DEFAULT 0,
                last_advert_timestamp TIMESTAMP,
                location_accuracy REAL,
                contact_source TEXT DEFAULT 'advertisement',
                out_path TEXT,
                out_path_len INTEGER
            ''')
            
            # Create daily_stats table for daily statistics tracking
            self.db_manager.create_table('daily_stats', '''
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                public_key TEXT NOT NULL,
                advert_count INTEGER DEFAULT 1,
                first_advert_time TIMESTAMP,
                last_advert_time TIMESTAMP,
                UNIQUE(date, public_key)
            ''')
            
            # Create purging_log table for audit trail
            self.db_manager.create_table('purging_log', '''
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,
                public_key TEXT NOT NULL,
                name TEXT NOT NULL,
                reason TEXT
            ''')
            
            # Create indexes for better performance
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_public_key ON repeater_contacts(public_key)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_device_type ON repeater_contacts(device_type)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_last_seen ON repeater_contacts(last_seen)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_is_active ON repeater_contacts(is_active)')
                
                # Indexes for contact tracking table
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_public_key ON complete_contact_tracking(public_key)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_role ON complete_contact_tracking(role)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_last_heard ON complete_contact_tracking(last_heard)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_currently_tracked ON complete_contact_tracking(is_currently_tracked)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_location ON complete_contact_tracking(latitude, longitude)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_role_tracked ON complete_contact_tracking(role, is_currently_tracked)')
                conn.commit()
            
            self.logger.info("Repeater contacts database initialized successfully")
                
        except Exception as e:
            self.logger.error(f"Failed to initialize repeater database: {e}")
            raise
    
    def _migrate_database_schema(self):
        """Handle database schema migration for existing installations"""
        try:
            # Check if the new location columns exist in repeater_contacts
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(repeater_contacts)")
                columns = [row[1] for row in cursor.fetchall()]
                
                # Add missing location columns if they don't exist
                new_columns = [
                    ('latitude', 'REAL'),
                    ('longitude', 'REAL'),
                    ('city', 'TEXT'),
                    ('state', 'TEXT'),
                    ('country', 'TEXT')
                ]
                
                for column_name, column_type in new_columns:
                    if column_name not in columns:
                        self.logger.info(f"Adding missing column to repeater_contacts: {column_name}")
                        cursor.execute(f"ALTER TABLE repeater_contacts ADD COLUMN {column_name} {column_type}")
                        conn.commit()
                
                # Check if the new path columns exist in complete_contact_tracking
                cursor.execute("PRAGMA table_info(complete_contact_tracking)")
                tracking_columns = [row[1] for row in cursor.fetchall()]
                
                # Add missing path columns if they don't exist
                path_columns = [
                    ('out_path', 'TEXT'),
                    ('out_path_len', 'INTEGER'),
                    ('snr', 'REAL')
                ]
                
                for column_name, column_type in path_columns:
                    if column_name not in tracking_columns:
                        self.logger.info(f"Adding missing column to complete_contact_tracking: {column_name}")
                        cursor.execute(f"ALTER TABLE complete_contact_tracking ADD COLUMN {column_name} {column_type}")
                        conn.commit()
                
                self.logger.info("Database schema migration completed")
                
        except Exception as e:
            self.logger.error(f"Error during database schema migration: {e}")
    
    async def track_contact_advertisement(self, advert_data: Dict, signal_info: Dict = None) -> bool:
        """Track any contact advertisement in the complete tracking database"""
        try:
            # Extract basic information
            public_key = advert_data.get('public_key', '')
            name = advert_data.get('name', advert_data.get('adv_name', 'Unknown'))
            device_type = advert_data.get('type', 'Unknown')
            
            if not public_key:
                self.logger.warning("No public key in advertisement data")
                return False
            
            # Determine role and device type
            role = self._determine_contact_role(advert_data)
            device_type_str = self._determine_device_type(device_type, name, advert_data)
            
            # Extract signal information
            signal_strength = None
            snr = None
            hop_count = None
            if signal_info:
                hop_count = signal_info.get('hops', signal_info.get('hop_count'))
                
                # Only save RSSI/SNR for zero-hop (direct) connections
                # For multi-hop packets, signal strength represents the last hop, not the source
                if hop_count == 0:
                    signal_strength = signal_info.get('rssi', signal_info.get('signal_strength'))
                    snr = signal_info.get('snr')
                    self.logger.debug(f"📡 Saving signal data for direct connection: RSSI={signal_strength}, SNR={snr}")
                else:
                    self.logger.debug(f"📡 Skipping signal data for {hop_count}-hop connection (not direct)")
            
            # Extract path information from advert_data
            out_path = advert_data.get('out_path', '')
            out_path_len = advert_data.get('out_path_len', -1)
            
            # Check if this contact is already in our complete tracking
            existing = self.db_manager.execute_query(
                'SELECT id, advert_count, last_heard, latitude, longitude, city, state, country FROM complete_contact_tracking WHERE public_key = ?',
                (public_key,)
            )
            
            current_time = datetime.now()
            
            # Extract location data first (without geocoding)
            self.logger.debug(f"🔍 Extracting location data for {name}...")
            location_info = self._extract_location_data(advert_data, should_geocode=False)
            self.logger.debug(f"📍 Location data extracted: {location_info}")
            
            # Check if we need to perform geocoding based on location changes
            existing_data = existing[0] if existing else None
            should_geocode, location_info = self._should_geocode_location(location_info, existing_data, name)
            
            # Re-extract location data with geocoding if needed
            if should_geocode:
                self.logger.debug(f"📍 Re-extracting location data with geocoding for {name}")
                location_info = self._extract_location_data(advert_data, should_geocode=True)
                self.logger.debug(f"📍 Location data with geocoding: {location_info}")
            
            if existing:
                # Update existing entry
                advert_count = existing[0]['advert_count'] + 1
                self.db_manager.execute_update('''
                    UPDATE complete_contact_tracking 
                    SET name = ?, last_heard = ?, advert_count = ?, role = ?, device_type = ?,
                        latitude = ?, longitude = ?, city = ?, state = ?, country = ?, 
                        raw_advert_data = ?, signal_strength = ?, snr = ?, hop_count = ?, 
                        last_advert_timestamp = ?, out_path = ?, out_path_len = ?
                    WHERE public_key = ?
                ''', (
                    name, current_time, advert_count, role, device_type_str,
                    location_info['latitude'], location_info['longitude'], 
                    location_info['city'], location_info['state'], location_info['country'],
                    json.dumps(advert_data), signal_strength, snr, hop_count,
                    current_time, out_path, out_path_len, public_key
                ))
                
                self.logger.debug(f"Updated contact tracking: {name} ({role}) - count: {advert_count}")
            else:
                # Insert new entry
                self.db_manager.execute_update('''
                    INSERT INTO complete_contact_tracking 
                    (public_key, name, role, device_type, first_heard, last_heard, advert_count,
                     latitude, longitude, city, state, country, raw_advert_data,
                     signal_strength, snr, hop_count, last_advert_timestamp, out_path, out_path_len)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    public_key, name, role, device_type_str, current_time, current_time, 1,
                    location_info['latitude'], location_info['longitude'], 
                    location_info['city'], location_info['state'], location_info['country'],
                    json.dumps(advert_data), signal_strength, snr, hop_count, current_time,
                    out_path, out_path_len
                ))
                
                self.logger.info(f"Added new contact to complete tracking: {name} ({role})")
            
            # Update the currently_tracked flag based on device contact list
            await self._update_currently_tracked_status(public_key)
            
            # Track daily advertisement statistics
            await self._track_daily_advertisement(public_key, name, role, device_type_str, 
                                                location_info, signal_strength, snr, hop_count, current_time)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error tracking contact advertisement: {e}")
            return False
    
    async def _track_daily_advertisement(self, public_key: str, name: str, role: str, device_type: str,
                                       location_info: Dict, signal_strength: float, snr: float, 
                                       hop_count: int, timestamp: datetime):
        """Track daily advertisement statistics for accurate time-based reporting"""
        try:
            from datetime import date
            
            # Get today's date
            today = date.today()
            
            # Check if we already have an entry for this contact today
            existing_daily = self.db_manager.execute_query(
                'SELECT id, advert_count, first_advert_time FROM daily_stats WHERE date = ? AND public_key = ?',
                (today, public_key)
            )
            
            if existing_daily:
                # Update existing daily entry
                daily_advert_count = existing_daily[0]['advert_count'] + 1
                self.db_manager.execute_update('''
                    UPDATE daily_stats 
                    SET advert_count = ?, last_advert_time = ?
                    WHERE date = ? AND public_key = ?
                ''', (daily_advert_count, timestamp, today, public_key))
                
                self.logger.debug(f"Updated daily stats for {name}: {daily_advert_count} adverts today")
            else:
                # Insert new daily entry
                self.db_manager.execute_update('''
                    INSERT INTO daily_stats 
                    (date, public_key, advert_count, first_advert_time, last_advert_time)
                    VALUES (?, ?, ?, ?, ?)
                ''', (today, public_key, 1, timestamp, timestamp))
                
                self.logger.debug(f"Added daily stats for {name}: first advert today")
                
        except Exception as e:
            self.logger.error(f"Error tracking daily advertisement: {e}")
    
    def _determine_contact_role(self, contact_data: Dict) -> str:
        """Determine the role of a contact based on MeshCore specifications"""
        from .enums import DeviceRole
        
        # First priority: Use the mode field from parsed advertisement data
        mode = contact_data.get('mode', '')
        if mode:
            # Convert DeviceRole enum values to lowercase role strings
            if mode == DeviceRole.Repeater.value:
                return 'repeater'
            elif mode == DeviceRole.RoomServer.value:
                return 'roomserver'
            elif mode == DeviceRole.Companion.value:
                return 'companion'
            elif mode == 'Sensor':
                return 'sensor'
            else:
                # Handle any other mode values
                return mode.lower()
        
        # Fallback to legacy detection methods
        name = contact_data.get('name', contact_data.get('adv_name', '')).lower()
        device_type = contact_data.get('type', 0)
        
        # Check device type (legacy indicator)
        if device_type == 2:
            return 'repeater'
        elif device_type == 3:
            return 'roomserver'
        
        # Check name-based indicators for role detection (legacy fallback)
        if any(keyword in name for keyword in ['repeater', 'rpt', 'rp']):
            return 'repeater'
        elif any(keyword in name for keyword in ['room', 'server', 'rs', 'roomserver']):
            return 'roomserver'
        elif any(keyword in name for keyword in ['sensor', 'sens']):
            return 'sensor'
        elif any(keyword in name for keyword in ['bot', 'automated', 'automation']):
            return 'bot'
        elif any(keyword in name for keyword in ['gateway', 'gw', 'bridge']):
            return 'gateway'
        else:
            # Default to companion for unknown contacts (human users)
            return 'companion'
    
    def _determine_device_type(self, device_type: int, name: str, advert_data: Dict = None) -> str:
        """Determine device type string from numeric type and name following MeshCore specs"""
        from .enums import DeviceRole
        
        # First priority: Use the mode field from parsed advertisement data
        if advert_data and advert_data.get('mode'):
            mode = advert_data.get('mode')
            if mode == DeviceRole.Repeater.value:
                return 'Repeater'
            elif mode == DeviceRole.RoomServer.value:
                return 'RoomServer'
            elif mode == DeviceRole.Companion.value:
                return 'Companion'
            elif mode == 'Sensor':
                return 'Sensor'
            else:
                # Handle any other mode values
                return mode
        
        # Fallback to legacy detection methods
        if device_type == 3:
            return 'RoomServer'
        elif device_type == 2:
            return 'Repeater'
        elif device_type == 1:
            return 'Companion'
        else:
            # Fallback to name-based detection
            name_lower = name.lower()
            if 'room' in name_lower or 'server' in name_lower or 'roomserver' in name_lower:
                return 'RoomServer'
            elif 'repeater' in name_lower or 'rpt' in name_lower:
                return 'Repeater'
            elif 'sensor' in name_lower or 'sens' in name_lower:
                return 'Sensor'
            elif 'gateway' in name_lower or 'gw' in name_lower or 'bridge' in name_lower:
                return 'Gateway'
            elif 'bot' in name_lower or 'automated' in name_lower:
                return 'Bot'
            else:
                return 'Companion'  # Default to companion for human users
    
    async def _update_currently_tracked_status(self, public_key: str):
        """Update the is_currently_tracked flag based on device contact list"""
        try:
            # Check if this repeater is currently in the device's contact list
            is_tracked = False
            if hasattr(self.bot.meshcore, 'contacts'):
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', contact_key) == public_key:
                        is_tracked = True
                        break
            
            # Update the flag
            self.db_manager.execute_update(
                'UPDATE complete_contact_tracking SET is_currently_tracked = ? WHERE public_key = ?',
                (is_tracked, public_key)
            )
            
        except Exception as e:
            self.logger.error(f"Error updating currently tracked status: {e}")
    
    async def get_complete_contact_database(self, role_filter: str = None, include_historical: bool = True) -> List[Dict]:
        """Get complete contact database for path estimation and analysis"""
        try:
            if include_historical:
                if role_filter:
                    # Get all contacts of specific role ever heard
                    query = '''
                        SELECT public_key, name, role, device_type, first_heard, last_heard, 
                               advert_count, latitude, longitude, city, state, country,
                               signal_strength, hop_count, is_currently_tracked, last_advert_timestamp
                        FROM complete_contact_tracking
                        WHERE role = ?
                        ORDER BY last_heard DESC
                    '''
                    results = self.db_manager.execute_query(query, (role_filter,))
                else:
                    # Get all contacts ever heard
                    query = '''
                        SELECT public_key, name, role, device_type, first_heard, last_heard, 
                               advert_count, latitude, longitude, city, state, country,
                               signal_strength, hop_count, is_currently_tracked, last_advert_timestamp
                        FROM complete_contact_tracking
                        ORDER BY last_heard DESC
                    '''
                    results = self.db_manager.execute_query(query)
            else:
                if role_filter:
                    # Get only currently tracked contacts of specific role
                    query = '''
                        SELECT public_key, name, role, device_type, first_heard, last_heard, 
                               advert_count, latitude, longitude, city, state, country,
                               signal_strength, hop_count, is_currently_tracked, last_advert_timestamp
                        FROM complete_contact_tracking
                        WHERE role = ? AND is_currently_tracked = 1
                        ORDER BY last_heard DESC
                    '''
                    results = self.db_manager.execute_query(query, (role_filter,))
                else:
                    # Get only currently tracked contacts
                    query = '''
                        SELECT public_key, name, role, device_type, first_heard, last_heard, 
                               advert_count, latitude, longitude, city, state, country,
                               signal_strength, hop_count, is_currently_tracked, last_advert_timestamp
                        FROM complete_contact_tracking
                        WHERE is_currently_tracked = 1
                        ORDER BY last_heard DESC
                    '''
                    results = self.db_manager.execute_query(query)
            
            return results
            
        except Exception as e:
            self.logger.error(f"Error getting complete repeater database: {e}")
            return []
    
    async def get_contact_statistics(self) -> Dict:
        """Get statistics about the contact tracking database"""
        try:
            stats = {}
            
            # Total contacts ever heard
            total_result = self.db_manager.execute_query(
                'SELECT COUNT(*) as count FROM complete_contact_tracking'
            )
            stats['total_heard'] = total_result[0]['count'] if total_result else 0
            
            # Currently tracked contacts
            current_result = self.db_manager.execute_query(
                'SELECT COUNT(*) as count FROM complete_contact_tracking WHERE is_currently_tracked = 1'
            )
            stats['currently_tracked'] = current_result[0]['count'] if current_result else 0
            
            # Recent activity (last 24 hours)
            recent_result = self.db_manager.execute_query(
                'SELECT COUNT(*) as count FROM complete_contact_tracking WHERE last_heard > datetime("now", "-1 day")'
            )
            stats['recent_activity'] = recent_result[0]['count'] if recent_result else 0
            
            # Role breakdown
            role_result = self.db_manager.execute_query(
                'SELECT role, COUNT(*) as count FROM complete_contact_tracking GROUP BY role'
            )
            stats['by_role'] = {row['role']: row['count'] for row in role_result}
            
            # Device type breakdown
            type_result = self.db_manager.execute_query(
                'SELECT device_type, COUNT(*) as count FROM complete_contact_tracking GROUP BY device_type'
            )
            stats['by_type'] = {row['device_type']: row['count'] for row in type_result}
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error getting contact statistics: {e}")
            return {}
    
    async def get_contacts_by_role(self, role: str, include_historical: bool = True) -> List[Dict]:
        """Get contacts filtered by specific MeshCore role (repeater, roomserver, companion, sensor, gateway, bot)"""
        return await self.get_complete_contact_database(role_filter=role, include_historical=include_historical)
    
    async def get_repeater_devices(self, include_historical: bool = True) -> List[Dict]:
        """Get all repeater devices (repeaters and roomservers) following MeshCore terminology"""
        repeater_db = await self.get_complete_contact_database(role_filter='repeater', include_historical=include_historical)
        roomserver_db = await self.get_complete_contact_database(role_filter='roomserver', include_historical=include_historical)
        return repeater_db + roomserver_db
    
    async def get_companion_contacts(self, include_historical: bool = True) -> List[Dict]:
        """Get all companion contacts (human users) following MeshCore terminology"""
        return await self.get_complete_contact_database(role_filter='companion', include_historical=include_historical)
    
    async def get_sensor_devices(self, include_historical: bool = True) -> List[Dict]:
        """Get all sensor devices following MeshCore terminology"""
        return await self.get_complete_contact_database(role_filter='sensor', include_historical=include_historical)
    
    async def get_gateway_devices(self, include_historical: bool = True) -> List[Dict]:
        """Get all gateway devices following MeshCore terminology"""
        return await self.get_complete_contact_database(role_filter='gateway', include_historical=include_historical)
    
    async def get_bot_devices(self, include_historical: bool = True) -> List[Dict]:
        """Get all bot/automated devices following MeshCore terminology"""
        return await self.get_complete_contact_database(role_filter='bot', include_historical=include_historical)
    
    async def check_and_auto_purge(self) -> bool:
        """Check contact limit and auto-purge repeaters if needed"""
        try:
            if not self.auto_purge_enabled:
                return False
                
            # Get current contact count
            current_count = len(self.bot.meshcore.contacts)
            
            if current_count >= self.auto_purge_threshold:
                self.logger.info(f"🔄 Auto-purge triggered: {current_count}/{self.contact_limit} contacts (threshold: {self.auto_purge_threshold})")
                
                # Calculate how many to purge
                target_count = self.auto_purge_threshold - 20  # Leave some buffer
                purge_count = current_count - target_count
                
                if purge_count > 0:
                    success = await self._auto_purge_repeaters(purge_count)
                    if success:
                        self.logger.info(f"✅ Auto-purged {purge_count} repeaters, now at {len(self.bot.meshcore.contacts)}/{self.contact_limit} contacts")
                        return True
                    else:
                        self.logger.warning(f"❌ Auto-purge failed to remove {purge_count} repeaters")
                        return False
                        
            return False
            
        except Exception as e:
            self.logger.error(f"Error in auto-purge check: {e}")
            return False
    
    async def _auto_purge_repeaters(self, count: int) -> bool:
        """Automatically purge repeaters using intelligent selection"""
        try:
            # Get all repeaters sorted by priority (least important first)
            repeaters_to_purge = await self._get_repeaters_for_purging(count)
            
            if not repeaters_to_purge:
                self.logger.warning("No repeaters available for auto-purge")
                # Log some debugging info
                total_contacts = len(self.bot.meshcore.contacts)
                repeater_count = sum(1 for contact_data in self.bot.meshcore.contacts.values() if self._is_repeater_device(contact_data))
                self.logger.debug(f"Debug: {total_contacts} total contacts, {repeater_count} repeaters found")
                return False
            
            purged_count = 0
            for repeater in repeaters_to_purge:
                try:
                    # Use the improved purge method
                    public_key = repeater['public_key']
                    success = await self.purge_repeater_from_contacts(public_key, "Auto-purge - contact limit management")
                    
                    if success:
                        purged_count += 1
                        self.logger.info(f"🗑️ Auto-purged repeater: {repeater['name']} (last seen: {repeater['last_seen']})")
                    else:
                        self.logger.warning(f"Failed to auto-purge repeater: {repeater['name']}")
                        
                except Exception as e:
                    self.logger.error(f"Error auto-purging repeater {repeater['name']}: {e}")
                    continue
            
            self.logger.info(f"✅ Auto-purge completed: {purged_count}/{count} repeaters removed")
            return purged_count > 0
            
        except Exception as e:
            self.logger.error(f"Error in auto-purge execution: {e}")
            return False
    
    async def _get_repeaters_for_purging(self, count: int) -> List[Dict]:
        """Get list of repeaters to purge based on intelligent criteria from device contacts"""
        try:
            # Get repeaters directly from device contacts, not database
            device_repeaters = []
            
            for contact_key, contact_data in self.bot.meshcore.contacts.items():
                # Check if this is a repeater device
                if self._is_repeater_device(contact_data):
                    public_key = contact_data.get('public_key', contact_key)
                    name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
                    device_type = 'Repeater'
                    if contact_data.get('type') == 3:
                        device_type = 'RoomServer'
                    
                    # Get last seen timestamp
                    last_seen = contact_data.get('last_seen', contact_data.get('last_advert', contact_data.get('timestamp')))
                    if last_seen:
                        try:
                            if isinstance(last_seen, str):
                                last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                            elif isinstance(last_seen, (int, float)):
                                last_seen_dt = datetime.fromtimestamp(last_seen)
                            else:
                                last_seen_dt = last_seen
                        except:
                            last_seen_dt = datetime.now() - timedelta(days=30)  # Default to old
                    else:
                        last_seen_dt = datetime.now() - timedelta(days=30)  # Default to old
                    
                    device_repeaters.append({
                        'public_key': public_key,
                        'name': name,
                        'device_type': device_type,
                        'last_seen': last_seen_dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'latitude': contact_data.get('adv_lat'),
                        'longitude': contact_data.get('adv_lon'),
                        'city': contact_data.get('city'),
                        'state': contact_data.get('state'),
                        'country': contact_data.get('country')
                    })
            
            # Sort by priority (oldest first, with location data as secondary factor)
            device_repeaters.sort(key=lambda x: (
                # Priority 1: Very old (7+ days)
                1 if (datetime.now() - datetime.strptime(x['last_seen'], '%Y-%m-%d %H:%M:%S')).days >= 7 else
                # Priority 2: Medium old (3-7 days)
                2 if (datetime.now() - datetime.strptime(x['last_seen'], '%Y-%m-%d %H:%M:%S')).days >= 3 else
                # Priority 3: Recent (0-3 days)
                3,
                # Within same priority, prefer repeaters without location data, then oldest first
                0 if not (x.get('latitude') and x.get('longitude')) else 1,
                x['last_seen']
            ))
            
            # Apply additional filtering criteria
            filtered_repeaters = []
            for repeater in device_repeaters:
                # Skip repeaters with very recent activity (last 2 hours) - more lenient
                last_seen_dt = datetime.strptime(repeater['last_seen'], '%Y-%m-%d %H:%M:%S')
                if last_seen_dt > datetime.now() - timedelta(hours=2):
                    continue
                    
                # Don't skip repeaters with location data - location data is common and not a reason to preserve
                # The sorting logic above already prioritizes repeaters without location data
                filtered_repeaters.append(repeater)
                
                if len(filtered_repeaters) >= count:
                    break
            
            self.logger.debug(f"Found {len(device_repeaters)} device repeaters, {len(filtered_repeaters)} available for purging")
            
            # Additional debugging info
            if len(filtered_repeaters) == 0 and len(device_repeaters) > 0:
                self.logger.debug("No repeaters available for purging - checking filtering criteria:")
                recent_count = 0
                location_count = 0
                for repeater in device_repeaters:
                    last_seen_dt = datetime.strptime(repeater['last_seen'], '%Y-%m-%d %H:%M:%S')
                    if last_seen_dt > datetime.now() - timedelta(hours=2):
                        recent_count += 1
                    if repeater['latitude'] and repeater['longitude']:
                        location_count += 1
                self.logger.debug(f"Filtering stats: {recent_count} too recent, {location_count} with location data")
            
            return filtered_repeaters[:count]
            
        except Exception as e:
            self.logger.error(f"Error getting repeaters for purging: {e}")
            return []
    
    def _extract_location_data(self, contact_data: Dict, should_geocode: bool = True) -> Dict[str, Optional[str]]:
        """Extract location data from contact_data JSON"""
        location_info = {
            'latitude': None,
            'longitude': None,
            'city': None,
            'state': None,
            'country': None
        }
        
        try:
            # First check for direct lat/lon fields (from parsed advert data)
            if 'lat' in contact_data and 'lon' in contact_data:
                try:
                    location_info['latitude'] = float(contact_data['lat'])
                    location_info['longitude'] = float(contact_data['lon'])
                    self.logger.debug(f"📍 Direct lat/lon found: {location_info['latitude']}, {location_info['longitude']}")
                    # Don't return here - continue to geocoding logic below
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"Failed to parse direct lat/lon: {e}")
            
            # Check for various possible location field names in contact data
            location_fields = [
                'location', 'gps', 'coordinates', 'lat_lon', 'lat_lng',
                'position', 'geo', 'geolocation', 'loc'
            ]
            
            for field in location_fields:
                if field in contact_data:
                    loc_data = contact_data[field]
                    if isinstance(loc_data, dict):
                        # Handle structured location data
                        if 'lat' in loc_data and 'lon' in loc_data:
                            try:
                                location_info['latitude'] = float(loc_data['lat'])
                                location_info['longitude'] = float(loc_data['lon'])
                            except (ValueError, TypeError):
                                pass
                        elif 'latitude' in loc_data and 'longitude' in loc_data:
                            try:
                                location_info['latitude'] = float(loc_data['latitude'])
                                location_info['longitude'] = float(loc_data['longitude'])
                            except (ValueError, TypeError):
                                pass
                        
                        # Extract city/state/country if available
                        for addr_field in ['city', 'state', 'country', 'region', 'province']:
                            if addr_field in loc_data and loc_data[addr_field]:
                                if addr_field == 'region' or addr_field == 'province':
                                    location_info['state'] = str(loc_data[addr_field])
                                else:
                                    location_info[addr_field] = str(loc_data[addr_field])
                    
                    elif isinstance(loc_data, str):
                        # Handle string location data (e.g., "lat,lon" or "city, state")
                        if ',' in loc_data:
                            parts = [p.strip() for p in loc_data.split(',')]
                            if len(parts) >= 2:
                                try:
                                    # Try to parse as coordinates
                                    lat = float(parts[0])
                                    lon = float(parts[1])
                                    location_info['latitude'] = lat
                                    location_info['longitude'] = lon
                                except ValueError:
                                    # Treat as city, state format
                                    location_info['city'] = parts[0]
                                    if len(parts) > 1:
                                        location_info['state'] = parts[1]
                                    if len(parts) > 2:
                                        location_info['country'] = parts[2]
            
            # Check for individual lat/lon fields (including MeshCore-specific fields)
            for lat_field in ['adv_lat', 'lat', 'latitude', 'gps_lat']:
                if lat_field in contact_data:
                    try:
                        location_info['latitude'] = float(contact_data[lat_field])
                        break
                    except (ValueError, TypeError):
                        pass
            
            for lon_field in ['adv_lon', 'lon', 'lng', 'longitude', 'gps_lon', 'gps_lng']:
                if lon_field in contact_data:
                    try:
                        location_info['longitude'] = float(contact_data[lon_field])
                        break
                    except (ValueError, TypeError):
                        pass
            
            # Check for address fields
            for city_field in ['city', 'town', 'municipality']:
                if city_field in contact_data and contact_data[city_field]:
                    location_info['city'] = str(contact_data[city_field])
                    break
            
            for state_field in ['state', 'province', 'region']:
                if state_field in contact_data and contact_data[state_field]:
                    location_info['state'] = str(contact_data[state_field])
                    break
            
            for country_field in ['country', 'nation']:
                if country_field in contact_data and contact_data[country_field]:
                    location_info['country'] = str(contact_data[country_field])
                    break
            
            # Validate coordinates if we have them
            if location_info['latitude'] is not None and location_info['longitude'] is not None:
                lat, lon = location_info['latitude'], location_info['longitude']
                
                # Treat 0,0 coordinates as "hidden" location (common in MeshCore)
                if lat == 0.0 and lon == 0.0:
                    location_info['latitude'] = None
                    location_info['longitude'] = None
                # Check for valid coordinate ranges
                elif not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    # Invalid coordinates
                    location_info['latitude'] = None
                    location_info['longitude'] = None
                else:
                    # Valid coordinates - try reverse geocoding if we don't have city/state/country and geocoding is enabled
                    if should_geocode and (not location_info['city'] or not location_info['state'] or not location_info['country']):
                        try:
                            # Use reverse geocoding to get city/state/country
                            city = self._get_city_from_coordinates(lat, lon)
                            if city:
                                location_info['city'] = city
                            
                            # Get state and country from coordinates
                            state, country = self._get_state_country_from_coordinates(lat, lon)
                            if state:
                                location_info['state'] = state
                            if country:
                                location_info['country'] = country
                                
                        except Exception as e:
                            self.logger.debug(f"Reverse geocoding failed: {e}")
                    elif not should_geocode:
                        self.logger.debug(f"📍 Skipping geocoding for coordinates {lat}, {lon} (location unchanged)")
            
        except Exception as e:
            self.logger.debug(f"Error extracting location data: {e}")
        
        return location_info

    def _should_geocode_location(self, location_info: Dict, existing_data: Dict = None, name: str = "Unknown") -> tuple[bool, Dict]:
        """
        Determine if geocoding should be performed based on location changes.
        
        Args:
            location_info: New location data extracted from advert
            existing_data: Existing location data from database (optional)
            name: Contact name for logging
            
        Returns:
            tuple: (should_geocode: bool, updated_location_info: Dict)
        """
        should_geocode = False
        updated_location_info = location_info.copy()
        
        # If no existing data, only geocode if we have valid coordinates but no city
        if not existing_data:
            should_geocode = (
                location_info['latitude'] is not None and 
                location_info['longitude'] is not None and 
                not (location_info['latitude'] == 0.0 and location_info['longitude'] == 0.0) and
                not location_info['city']
            )
            if should_geocode:
                self.logger.debug(f"📍 New contact {name}, will geocode coordinates")
            return should_geocode, updated_location_info
        
        # Extract existing location data
        existing_lat = existing_data.get('latitude', 0.0) if existing_data.get('latitude') is not None else 0.0
        existing_lon = existing_data.get('longitude', 0.0) if existing_data.get('longitude') is not None else 0.0
        existing_city = existing_data.get('city')
        existing_state = existing_data.get('state')
        existing_country = existing_data.get('country')
        
        # Only geocode if coordinates changed or we don't have city data
        if (location_info['latitude'] is not None and 
            location_info['longitude'] is not None and 
            not (location_info['latitude'] == 0.0 and location_info['longitude'] == 0.0)):
            
            coordinates_changed = (
                abs(location_info['latitude'] - existing_lat) > 0.0001 or 
                abs(location_info['longitude'] - existing_lon) > 0.0001
            )
            
            # Only geocode if coordinates changed or we don't have a city
            should_geocode = coordinates_changed or not existing_city
            
            if not should_geocode and existing_city:
                # Use existing city data, no need to geocode
                updated_location_info['city'] = existing_city
                updated_location_info['state'] = existing_state
                updated_location_info['country'] = existing_country
                self.logger.debug(f"📍 Using existing location data for {name}: {existing_city}")
            elif should_geocode:
                self.logger.debug(f"📍 Location changed for {name}, will geocode new coordinates")
        else:
            # No valid coordinates in new data, keep existing location
            updated_location_info['latitude'] = existing_lat if existing_lat != 0.0 else None
            updated_location_info['longitude'] = existing_lon if existing_lon != 0.0 else None
            updated_location_info['city'] = existing_city
            updated_location_info['state'] = existing_state
            updated_location_info['country'] = existing_country
        
        return should_geocode, updated_location_info

    def _get_state_country_from_coordinates(self, latitude: float, longitude: float) -> tuple[Optional[str], Optional[str]]:
        """Get state and country from coordinates using reverse geocoding"""
        try:
            from geopy.geocoders import Nominatim
            
            # Initialize geocoder with proper timeout
            geolocator = Nominatim(user_agent="meshcore-bot", timeout=10)
            
            # Perform reverse geocoding
            location = geolocator.reverse(f"{latitude}, {longitude}")
            if location:
                address = location.raw.get('address', {})
                
                # Get state/province
                state = (address.get('state') or 
                        address.get('province') or 
                        address.get('region'))
                
                # Get country
                country = address.get('country')
                
                return state, country
                
        except Exception as e:
            self.logger.debug(f"Reverse geocoding for state/country failed: {e}")
        
        return None, None

    def _get_city_from_coordinates(self, latitude: float, longitude: float) -> Optional[str]:
        """Get city name from coordinates using reverse geocoding, with neighborhood for large cities"""
        try:
            from geopy.geocoders import Nominatim
            
            # Initialize geocoder with proper timeout
            geolocator = Nominatim(user_agent="meshcore-bot", timeout=10)
            
            # Perform reverse geocoding
            location = geolocator.reverse(f"{latitude}, {longitude}")
            if location:
                address = location.raw.get('address', {})
                
                # Get city name from various fields
                city = (address.get('city') or 
                       address.get('town') or 
                       address.get('village') or 
                       address.get('hamlet') or 
                       address.get('municipality') or 
                       address.get('suburb'))
                
                if city:
                    # For large cities, try to get neighborhood information
                    neighborhood = self._get_neighborhood_for_large_city(address, city)
                    if neighborhood:
                        return f"{neighborhood}, {city}"
                    else:
                        return city
            
            return None
            
        except Exception as e:
            self.logger.debug(f"Error getting city from coordinates {latitude}, {longitude}: {e}")
            return None
    
    def _get_full_location_from_coordinates(self, latitude: float, longitude: float) -> Dict[str, Optional[str]]:
        """Get complete location information (city, state, country) from coordinates using reverse geocoding"""
        location_info = {
            'city': None,
            'state': None,
            'country': None
        }
        
        try:
            # Validate coordinates first
            if latitude == 0.0 and longitude == 0.0:
                self.logger.debug(f"Skipping geocoding for hidden location: {latitude}, {longitude}")
                return location_info
            
            # Check for valid coordinate ranges
            if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
                self.logger.debug(f"Skipping geocoding for invalid coordinates: {latitude}, {longitude}")
                return location_info
            
            # Check cache first to avoid duplicate API calls
            cache_key = f"location_{latitude:.6f}_{longitude:.6f}"
            cached_result = self.db_manager.get_cached_json(cache_key, "geolocation")
            
            if cached_result:
                self.logger.debug(f"Using cached location data for {latitude}, {longitude}")
                return cached_result
            
            from geopy.geocoders import Nominatim
            
            # Initialize geocoder with proper user agent and timeout
            geolocator = Nominatim(
                user_agent="meshcore-bot-geolocation-update",
                timeout=10  # 10 second timeout
            )
            
            # Perform reverse geocoding
            location = geolocator.reverse(f"{latitude}, {longitude}")
            if location:
                address = location.raw.get('address', {})
                
                # Get city name from various fields
                city = (address.get('city') or 
                       address.get('town') or 
                       address.get('village') or 
                       address.get('hamlet') or 
                       address.get('municipality') or 
                       address.get('suburb'))
                
                if city:
                    # For large cities, try to get neighborhood information
                    neighborhood = self._get_neighborhood_for_large_city(address, city)
                    if neighborhood:
                        location_info['city'] = f"{neighborhood}, {city}"
                    else:
                        location_info['city'] = city
                
                # Get state/province information
                state = (address.get('state') or 
                        address.get('province') or 
                        address.get('region') or 
                        address.get('county'))
                if state:
                    location_info['state'] = state
                
                # Get country information
                country = (address.get('country') or 
                          address.get('country_code'))
                if country:
                    location_info['country'] = country
            
            # Cache the result for 24 hours to avoid duplicate API calls
            self.db_manager.cache_json(cache_key, location_info, "geolocation", cache_hours=24)
            
            return location_info
            
        except Exception as e:
            error_msg = str(e)
            if "No route to host" in error_msg or "Connection" in error_msg:
                self.logger.warning(f"Network error geocoding {latitude}, {longitude}: {error_msg}")
            else:
                self.logger.debug(f"Error getting full location from coordinates {latitude}, {longitude}: {e}")
            return location_info
    
    def _get_neighborhood_for_large_city(self, address: dict, city: str) -> Optional[str]:
        """Get neighborhood information for large cities"""
        try:
            # List of large cities where neighborhood info is useful
            large_cities = [
                'seattle', 'portland', 'san francisco', 'los angeles', 'san diego',
                'chicago', 'new york', 'boston', 'philadelphia', 'washington',
                'atlanta', 'miami', 'houston', 'dallas', 'austin', 'denver',
                'phoenix', 'las vegas', 'minneapolis', 'detroit', 'cleveland',
                'pittsburgh', 'baltimore', 'richmond', 'norfolk', 'tampa',
                'orlando', 'jacksonville', 'nashville', 'memphis', 'kansas city',
                'st louis', 'milwaukee', 'cincinnati', 'columbus', 'indianapolis',
                'louisville', 'lexington', 'charlotte', 'raleigh', 'greensboro',
                'winston-salem', 'durham', 'charleston', 'columbia', 'greenville',
                'savannah', 'augusta', 'macon', 'columbus', 'atlanta'
            ]
            
            # Check if this is a large city
            if city.lower() not in large_cities:
                return None
            
            # Try to get neighborhood information from various address fields
            neighborhood_fields = [
                'neighbourhood', 'neighborhood', 'suburb', 'quarter', 'district',
                'area', 'locality', 'hamlet', 'village', 'town'
            ]
            
            for field in neighborhood_fields:
                if field in address and address[field]:
                    neighborhood = address[field]
                    # Skip if it's the same as the city name
                    if neighborhood.lower() != city.lower():
                        return neighborhood
            
            # For Seattle specifically, try to get more specific area info
            if city.lower() == 'seattle':
                # Check for specific Seattle neighborhoods/areas
                seattle_areas = [
                    'capitol hill', 'ballard', 'fremont', 'queen anne', 'belltown',
                    'pioneer square', 'international district', 'chinatown',
                    'first hill', 'central district', 'central', 'beacon hill',
                    'columbia city', 'rainier valley', 'west seattle', 'alki',
                    'magnolia', 'greenwood', 'phinney ridge', 'wallingford',
                    'university district', 'udistrict', 'ravenna', 'laurelhurst',
                    'sand point', 'wedgwood', 'view ridge', 'matthews beach',
                    'lake city', 'bitter lake', 'broadview', 'crown hill',
                    'loyal heights', 'sunset hill', 'interbay', 'downtown',
                    'south lake union', 'denny triangle', 'denny regrade',
                    'eastlake', 'montlake', 'madison park', 'madrona',
                    'leschi', 'mount baker', 'columbia city', 'rainier beach',
                    'south park', 'georgetown', 'soho', 'industrial district'
                ]
                
                # Check if any of the address fields contain Seattle neighborhood names
                for field, value in address.items():
                    if isinstance(value, str):
                        value_lower = value.lower()
                        for area in seattle_areas:
                            if area in value_lower:
                                return area.title()
            
            return None
            
        except Exception as e:
            self.logger.debug(f"Error getting neighborhood for {city}: {e}")
            return None

    def _is_repeater_device(self, contact_data: Dict) -> bool:
        """Check if a contact is a repeater or room server using available contact data"""
        try:
            # Primary detection: Check device type field
            # Based on the actual contact data structure:
            # type: 2 = repeater, type: 3 = room server
            device_type = contact_data.get('type')
            if device_type in [2, 3]:
                return True
            
            # Secondary detection: Check for role fields in contact data
            role_fields = ['role', 'device_role', 'mode', 'device_type']
            for field in role_fields:
                value = contact_data.get(field, '')
                if value and isinstance(value, str):
                    value_lower = value.lower()
                    if any(role in value_lower for role in ['repeater', 'roomserver', 'room_server']):
                        return True
            
            # Tertiary detection: Check advertisement flags
            # Some repeaters have specific flags that indicate their function
            flags = contact_data.get('flags', contact_data.get('advert_flags', ''))
            if flags:
                if isinstance(flags, (int, str)):
                    flags_str = str(flags).lower()
                    if any(role in flags_str for role in ['repeater', 'roomserver', 'room_server']):
                        return True
            
            # Quaternary detection: Check name patterns with validation
            name = contact_data.get('adv_name', contact_data.get('name', '')).lower()
            if name:
                # Strong repeater indicators
                strong_indicators = ['repeater', 'roompeater', 'room server', 'roomserver', 'relay', 'gateway']
                if any(indicator in name for indicator in strong_indicators):
                    return True
                
                # Room server indicators
                room_indicators = ['room', 'rs ', 'rs-', 'rs_']
                if any(indicator in name for indicator in room_indicators):
                    # Additional validation to avoid false positives
                    user_indicators = ['user', 'person', 'mobile', 'phone', 'device', 'pager']
                    if not any(user_indicator in name for user_indicator in user_indicators):
                        return True
            
            # Quinary detection: Check path characteristics
            # Some repeaters have specific path patterns
            out_path_len = contact_data.get('out_path_len', -1)
            if out_path_len == 0:  # Direct connection might indicate repeater
                # Additional validation with name check
                if name and any(indicator in name for indicator in ['repeater', 'room', 'relay']):
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking if device is repeater: {e}")
            return False
    
    async def scan_and_catalog_repeaters(self) -> int:
        """Scan current contacts and catalog any repeaters found"""
        # Wait for contacts to be loaded if they're not ready yet
        if not hasattr(self.bot.meshcore, 'contacts') or not self.bot.meshcore.contacts:
            self.logger.info("Contacts not loaded yet, waiting...")
            # Wait up to 10 seconds for contacts to load
            for i in range(20):  # 20 * 0.5 = 10 seconds
                await asyncio.sleep(0.5)
                if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                    break
            else:
                self.logger.warning("No contacts available to scan for repeaters after waiting")
                return 0
        
        contacts = self.bot.meshcore.contacts
        self.logger.info(f"Scanning {len(contacts)} contacts for repeaters...")
        
        cataloged_count = 0
        updated_count = 0
        processed_count = 0
        
        try:
            for contact_key, contact_data in self.bot.meshcore.contacts.items():
                processed_count += 1
                
                # Log progress every 20 contacts
                if processed_count % 20 == 0:
                    self.logger.info(f"Scan progress: {processed_count}/{len(contacts)} contacts processed, {cataloged_count} repeaters found")
                
                # Debug logging for first few contacts to understand structure
                if processed_count <= 5:
                    self.logger.debug(f"Contact {processed_count}: {contact_data.get('name', 'Unknown')} (type: {contact_data.get('type')}, keys: {list(contact_data.keys())})")
                
                if self._is_repeater_device(contact_data):
                    public_key = contact_data.get('public_key', contact_key)
                    name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
                    self.logger.info(f"Found repeater: {name} (type: {contact_data.get('type')}, key: {public_key[:16]}...)")
                    
                    # Determine device type based on contact data
                    contact_type = contact_data.get('type')
                    if contact_type == 3:
                        device_type = 'RoomServer'
                    elif contact_type == 2:
                        device_type = 'Repeater'
                    else:
                        # Fallback to name-based detection
                        device_type = 'Repeater'
                        if 'room' in name.lower() or 'server' in name.lower():
                            device_type = 'RoomServer'
                    
                    # Extract location data from contact_data
                    location_info = self._extract_location_data(contact_data, should_geocode=False)
                    
                    # Check if already exists and get existing location data
                    existing = self.db_manager.execute_query(
                        'SELECT id, last_seen, latitude, longitude, city FROM repeater_contacts WHERE public_key = ?',
                        (public_key,)
                    )
                    
                    # Check if we need to perform geocoding based on location changes
                    existing_data = None
                    if existing:
                        existing_data = {
                            'latitude': existing[0][2],
                            'longitude': existing[0][3], 
                            'city': existing[0][4]
                        }
                    
                    should_geocode, location_info = self._should_geocode_location(location_info, existing_data, name)
                    
                    if should_geocode:
                        city_from_coords = self._get_city_from_coordinates(
                            location_info['latitude'], 
                            location_info['longitude']
                        )
                        if city_from_coords:
                            location_info['city'] = city_from_coords
                    
                    if existing:
                        # Update last_seen timestamp and location data if available
                        update_query = 'UPDATE repeater_contacts SET last_seen = CURRENT_TIMESTAMP, is_active = 1'
                        update_params = []
                        
                        # Add location fields if we have new data
                        if location_info['latitude'] is not None:
                            update_query += ', latitude = ?'
                            update_params.append(location_info['latitude'])
                        if location_info['longitude'] is not None:
                            update_query += ', longitude = ?'
                            update_params.append(location_info['longitude'])
                        if location_info['city']:
                            update_query += ', city = ?'
                            update_params.append(location_info['city'])
                        if location_info['state']:
                            update_query += ', state = ?'
                            update_params.append(location_info['state'])
                        if location_info['country']:
                            update_query += ', country = ?'
                            update_params.append(location_info['country'])
                        
                        update_query += ' WHERE public_key = ?'
                        update_params.append(public_key)
                        
                        self.db_manager.execute_update(update_query, tuple(update_params))
                        updated_count += 1
                    else:
                        # Insert new repeater with location data
                        self.db_manager.execute_update('''
                            INSERT INTO repeater_contacts 
                            (public_key, name, device_type, contact_data, latitude, longitude, city, state, country)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            public_key,
                            name,
                            device_type,
                            json.dumps(contact_data),
                            location_info['latitude'],
                            location_info['longitude'],
                            location_info['city'],
                            location_info['state'],
                            location_info['country']
                        ))
                        
                        # Log the addition
                        self.db_manager.execute_update('''
                            INSERT INTO purging_log (action, public_key, name, reason)
                            VALUES ('added', ?, ?, 'Auto-detected during contact scan')
                        ''', (public_key, name))
                        
                        cataloged_count += 1
                        location_str = ""
                        if location_info['city'] or location_info['latitude']:
                            if location_info['city']:
                                location_str = f" in {location_info['city']}"
                                if location_info['state']:
                                    location_str += f", {location_info['state']}"
                            elif location_info['latitude'] and location_info['longitude']:
                                location_str = f" at {location_info['latitude']:.4f}, {location_info['longitude']:.4f}"
                        self.logger.info(f"Cataloged new repeater: {name} ({device_type}){location_str}")
                
        except Exception as e:
            self.logger.error(f"Error scanning contacts for repeaters: {e}")
        
        if cataloged_count > 0:
            self.logger.info(f"Cataloged {cataloged_count} new repeaters")
        
        if updated_count > 0:
            self.logger.info(f"Updated {updated_count} existing repeaters with location data")
        
        self.logger.info(f"Scan completed: {cataloged_count} new repeaters cataloged, {updated_count} existing repeaters updated from {len(contacts)} contacts")
        self.logger.info(f"Scan summary: {processed_count} contacts processed, {cataloged_count + updated_count} repeaters processed")
        return cataloged_count
    
    async def get_repeater_contacts(self, active_only: bool = True) -> List[Dict]:
        """Get list of repeater contacts from database"""
        try:
            query = 'SELECT * FROM repeater_contacts'
            if active_only:
                query += ' WHERE is_active = 1'
            query += ' ORDER BY last_seen DESC'
            
            return self.db_manager.execute_query(query)
                
        except Exception as e:
            self.logger.error(f"Error retrieving repeater contacts: {e}")
            return []
    
    async def test_meshcore_cli_commands(self) -> Dict[str, bool]:
        """Test if meshcore-cli commands are working properly"""
        results = {}
        
        try:
            from meshcore_cli.meshcore_cli import next_cmd
            
            # Test a simple command that should always work
            try:
                result = await asyncio.wait_for(
                    next_cmd(self.bot.meshcore, ["help"]),
                    timeout=10.0
                )
                results['help'] = result is not None
                self.logger.info(f"meshcore-cli help command test: {'PASS' if results['help'] else 'FAIL'}")
            except Exception as e:
                results['help'] = False
                self.logger.warning(f"meshcore-cli help command test FAILED: {e}")
            
            # Test remove_contact command (we'll use a dummy key)
            try:
                result = await asyncio.wait_for(
                    next_cmd(self.bot.meshcore, ["remove_contact", "dummy_key"]),
                    timeout=10.0
                )
                # Even if it fails, if we get here without "Unknown command" error, the command exists
                results['remove_contact'] = True
                self.logger.info(f"meshcore-cli remove_contact command test: PASS")
            except Exception as e:
                if "Unknown command" in str(e):
                    results['remove_contact'] = False
                    self.logger.error(f"meshcore-cli remove_contact command test FAILED: {e}")
                else:
                    # Command exists but failed for other reasons (expected with dummy key)
                    results['remove_contact'] = True
                    self.logger.info(f"meshcore-cli remove_contact command test: PASS (command exists)")
            
        except Exception as e:
            self.logger.error(f"Error testing meshcore-cli commands: {e}")
            results['error'] = str(e)
        
        return results

    async def purge_repeater_from_contacts(self, public_key: str, reason: str = "Manual purge") -> bool:
        """Remove a specific repeater from the device's contact list using proper MeshCore API"""
        self.logger.info(f"Starting purge process for public_key: {public_key}")
        self.logger.debug(f"Purge reason: {reason}")
        
        try:
            # Find the contact in meshcore using proper MeshCore methods
            contact_to_remove = None
            contact_name = None
            contact_key = None
            
            self.logger.debug(f"Searching through {len(self.bot.meshcore.contacts)} contacts...")
            
            # Try to find contact using MeshCore helper methods first
            try:
                # Method 1: Try to find by public key prefix
                contact_to_remove = self.bot.meshcore.get_contact_by_key_prefix(public_key[:8])
                if contact_to_remove:
                    contact_name = contact_to_remove.get('adv_name', contact_to_remove.get('name', 'Unknown'))
                    contact_key = public_key
                    self.logger.debug(f"Found contact using key prefix: {contact_name}")
            except Exception as e:
                self.logger.debug(f"Key prefix lookup failed: {e}")
            
            # Method 2: Fallback to manual search
            if not contact_to_remove:
                for key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', key) == public_key:
                        contact_to_remove = contact_data
                        contact_name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
                        contact_key = key
                        self.logger.debug(f"Found contact manually: {contact_name} (key: {contact_key})")
                        break
            
            if not contact_to_remove:
                self.logger.warning(f"Repeater with public key {public_key} not found in current contacts")
                return False
            
            # Check if repeater exists in database, if not add it first
            existing_repeater = self.db_manager.execute_query(
                'SELECT id FROM repeater_contacts WHERE public_key = ?',
                (public_key,)
            )
            
            if not existing_repeater:
                # Add repeater to database first
                contact_name = contact_to_remove.get('adv_name', contact_to_remove.get('name', 'Unknown'))
                device_type = 'Repeater'
                if contact_to_remove.get('type') == 3:
                    device_type = 'RoomServer'
                elif 'room' in contact_name.lower() or 'server' in contact_name.lower():
                    device_type = 'RoomServer'
                
                self.db_manager.execute_update('''
                    INSERT INTO repeater_contacts 
                    (public_key, name, device_type, contact_data)
                    VALUES (?, ?, ?, ?)
                ''', (
                    public_key,
                    contact_name,
                    device_type,
                    json.dumps(contact_to_remove)
                ))
                
                self.logger.info(f"Added repeater {contact_name} to database before purging")
            
            # Track whether device removal was successful
            device_removal_successful = False
            
            # Verify contact still exists before attempting removal
            contact_still_exists = any(
                contact_data.get('public_key', key) == public_key 
                for key, contact_data in self.bot.meshcore.contacts.items()
            )
            
            if not contact_still_exists:
                self.logger.info(f"✅ Contact '{contact_name}' not found in device contacts (already removed) - treating as success")
                device_removal_successful = True
            else:
                # Remove the contact using the proper MeshCore API
                try:
                    self.logger.info(f"Removing contact '{contact_name}' from device using MeshCore API...")
                    self.logger.debug(f"Contact details: public_key={public_key}, contact_key={contact_key}, name='{contact_name}'")
                    
                    # Try different key formats for removal
                    removal_keys_to_try = []
                    
                    # Add the public key if it's different from contact key
                    if public_key != contact_key:
                        removal_keys_to_try.append(public_key)
                    
                    # Add the contact key
                    if contact_key:
                        removal_keys_to_try.append(contact_key)
                    
                    # Add the public key as bytes if it's a hex string
                    try:
                        if len(public_key) == 64:  # 32 bytes in hex
                            public_key_bytes = bytes.fromhex(public_key)
                            removal_keys_to_try.append(public_key_bytes)
                    except:
                        pass
                    
                    self.logger.debug(f"Will try removal with keys: {removal_keys_to_try}")
                    
                    # Try each key format
                    for key_to_try in removal_keys_to_try:
                        try:
                            self.logger.debug(f"Trying removal with key: {key_to_try} (type: {type(key_to_try)})")
                            result = await asyncio.wait_for(
                                self.bot.meshcore.commands.remove_contact(key_to_try),
                                timeout=30.0
                            )
                            
                            # Check if removal was successful
                            if result.type == EventType.OK:
                                device_removal_successful = True
                                self.logger.info(f"✅ Successfully removed contact '{contact_name}' from device using key: {key_to_try}")
                                break
                            elif result.type == EventType.ERROR:
                                # Log detailed error information
                                error_code = result.payload.get('error_code', 'unknown') if hasattr(result, 'payload') else 'unknown'
                                self.logger.debug(f"❌ Removal failed with key {key_to_try}: {result}")
                                self.logger.debug(f"❌ Error type: {result.type}, Error code: {error_code}")
                                
                                # Error code 2 typically means "contact not found" - treat as success
                                if error_code == 2:
                                    self.logger.info(f"✅ Contact '{contact_name}' not found (already removed) - treating as success")
                                    device_removal_successful = True
                                    break
                            else:
                                # Log other error types
                                error_code = result.payload.get('error_code', 'unknown') if hasattr(result, 'payload') else 'unknown'
                                self.logger.debug(f"❌ Removal failed with key {key_to_try}: {result}")
                                self.logger.debug(f"❌ Error type: {result.type}, Error code: {error_code}")
                        except Exception as e:
                            self.logger.debug(f"Exception with key {key_to_try}: {e}")
                            continue
                    
                    # If all key formats failed, try fallback methods
                    if not device_removal_successful:
                        self.logger.warning(f"All key formats failed for '{contact_name}' - trying fallback methods...")
                        device_removal_successful = await self._try_fallback_removal_methods(public_key, contact_name, reason)
                    
                except Exception as e:
                    self.logger.error(f"Failed to remove contact '{contact_name}' from device: {e}")
                    # Try fallback methods on exception
                    self.logger.info(f"Attempting fallback methods for contact '{contact_name}' due to exception...")
                    device_removal_successful = await self._try_fallback_removal_methods(public_key, contact_name, reason)
            
            # Only mark as inactive in database if device removal was successful
            if device_removal_successful:
                self.db_manager.execute_update(
                    'UPDATE repeater_contacts SET is_active = 0, purge_count = purge_count + 1 WHERE public_key = ?',
                    (public_key,)
                )
                
                # Log the purge action
                self.db_manager.execute_update('''
                    INSERT INTO purging_log (action, public_key, name, reason)
                    VALUES ('purged', ?, ?, ?)
                ''', (public_key, contact_name, reason))
                
                self.logger.info(f"Successfully purged repeater {contact_name}: {reason}")
                self.logger.debug(f"Purge process completed successfully for {contact_name}")
                return True
            else:
                self.logger.error(f"Failed to remove repeater {contact_name} from device - not marking as purged in database")
                # Log the failed attempt
                self.db_manager.execute_update('''
                    INSERT INTO purging_log (action, public_key, name, reason)
                    VALUES ('purge_failed', ?, ?, ?)
                ''', (public_key, contact_name, f"{reason} - Device removal failed"))
                return False
            
        except Exception as e:
            self.logger.error(f"Error purging repeater {public_key}: {e}")
            self.logger.debug(f"Error type: {type(e).__name__}")
            return False
    
    async def _try_fallback_removal_methods(self, public_key: str, contact_name: str, reason: str) -> bool:
        """Try alternative methods to remove a contact when the primary MeshCore API fails"""
        try:
            self.logger.info(f"Trying fallback removal methods for '{contact_name}'...")
            
            # Method 1: Try direct removal from contacts dictionary
            try:
                self.logger.info(f"Fallback Method 1: Direct removal from contacts dictionary...")
                contact_removed = False
                for contact_key, contact_data in list(self.bot.meshcore.contacts.items()):
                    if contact_data.get('public_key', contact_key) == public_key:
                        del self.bot.meshcore.contacts[contact_key]
                        contact_removed = True
                        self.logger.info(f"✅ Successfully removed contact '{contact_name}' from contacts dictionary")
                        break
                
                if contact_removed:
                    # Verify removal worked
                    await asyncio.sleep(1)
                    contact_still_exists = any(
                        contact_data.get('public_key', key) == public_key 
                        for key, contact_data in self.bot.meshcore.contacts.items()
                    )
                    if not contact_still_exists:
                        return True
                    else:
                        self.logger.warning(f"Contact '{contact_name}' still exists after dictionary removal")
            except Exception as e:
                self.logger.debug(f"Fallback Method 1 failed: {e}")
            
            # Method 2: Try alternative meshcore-cli commands
            try:
                self.logger.info(f"Fallback Method 2: Alternative meshcore-cli commands...")
                from meshcore_cli.meshcore_cli import next_cmd
                
                # Try different removal commands (using valid meshcore-cli commands)
                alternative_commands = [
                    ["remove_contact", public_key],
                    ["del_contact", public_key]
                ]
                
                for cmd in alternative_commands:
                    try:
                        self.logger.info(f"Trying fallback command: {' '.join(cmd)}")
                        
                        result = await asyncio.wait_for(
                            next_cmd(self.bot.meshcore, cmd),
                            timeout=15.0
                        )
                        
                        if result is not None:
                            self.logger.debug(f"Fallback command {' '.join(cmd)} result: {result}")
                            
                            # Verify removal
                            await asyncio.sleep(1)
                            contact_still_exists = any(
                                contact_data.get('public_key', key) == public_key 
                                for key, contact_data in self.bot.meshcore.contacts.items()
                            )
                            
                            if not contact_still_exists:
                                self.logger.info(f"✅ Fallback command {' '.join(cmd)} succeeded")
                                return True
                            else:
                                self.logger.debug(f"Contact '{contact_name}' still exists after {' '.join(cmd)}")
                    except Exception as e:
                        self.logger.debug(f"Fallback command {' '.join(cmd)} failed: {e}")
                        continue
                        
            except Exception as e:
                self.logger.debug(f"Fallback Method 2 failed: {e}")
            
            # Method 3: Try using contact key instead of public key
            try:
                self.logger.info(f"Fallback Method 3: Using contact key...")
                contact_key = None
                for key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', key) == public_key:
                        contact_key = key
                        break
                
                if contact_key:
                    # Try both meshcore API and CLI commands with contact key
                    try:
                        # Try meshcore API with contact key
                        result = await asyncio.wait_for(
                            self.bot.meshcore.commands.remove_contact(contact_key),
                            timeout=15.0
                        )
                        
                        if result.type == EventType.OK:
                            self.logger.info(f"✅ Fallback Method 3 succeeded using contact key via API")
                            return True
                        elif result.type == EventType.ERROR:
                            error_code = result.payload.get('error_code', 'unknown') if hasattr(result, 'payload') else 'unknown'
                            if error_code == 2:
                                self.logger.info(f"✅ Contact not found (already removed) - treating as success")
                                return True
                    except Exception as e:
                        self.logger.debug(f"API removal with contact key failed: {e}")
                    
                    # Try CLI command with contact key
                    try:
                        from meshcore_cli.meshcore_cli import next_cmd
                        
                        result = await asyncio.wait_for(
                            next_cmd(self.bot.meshcore, ["remove_contact", contact_key]),
                            timeout=15.0
                        )
                        
                        if result is not None:
                            # Verify removal
                            await asyncio.sleep(1)
                            contact_still_exists = any(
                                contact_data.get('public_key', key) == public_key 
                                for key, contact_data in self.bot.meshcore.contacts.items()
                            )
                            
                            if not contact_still_exists:
                                self.logger.info(f"✅ Fallback Method 3 succeeded using contact key via CLI")
                                return True
                    except Exception as e:
                        self.logger.debug(f"CLI removal with contact key failed: {e}")
            except Exception as e:
                self.logger.debug(f"Fallback Method 3 failed: {e}")
            
            self.logger.warning(f"All fallback methods failed for '{contact_name}'")
            return False
            
        except Exception as e:
            self.logger.error(f"Error in fallback removal methods: {e}")
            return False
    
    async def purge_repeater_by_contact_key(self, contact_key: str, reason: str = "Manual purge") -> bool:
        """Remove a repeater using the contact key from the device's contact list"""
        self.logger.info(f"Starting purge process for contact_key: {contact_key}")
        self.logger.debug(f"Purge reason: {reason}")
        
        try:
            # Find the contact in meshcore using the contact key
            if contact_key not in self.bot.meshcore.contacts:
                self.logger.warning(f"Contact with key {contact_key} not found in current contacts")
                return False
            
            contact_data = self.bot.meshcore.contacts[contact_key]
            contact_name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
            public_key = contact_data.get('public_key', contact_key)
            
            self.logger.info(f"Found contact: {contact_name} (key: {contact_key}, public_key: {public_key[:16]}...)")
            
            # Check if repeater exists in database, if not add it first
            existing_repeater = self.db_manager.execute_query(
                'SELECT id FROM repeater_contacts WHERE public_key = ?',
                (public_key,)
            )
            
            if not existing_repeater:
                # Add repeater to database first
                device_type = 'Repeater'
                if contact_data.get('type') == 3:
                    device_type = 'RoomServer'
                elif 'room' in contact_name.lower() or 'server' in contact_name.lower():
                    device_type = 'RoomServer'
                
                self.db_manager.execute_update('''
                    INSERT INTO repeater_contacts 
                    (public_key, name, device_type, contact_data)
                    VALUES (?, ?, ?, ?)
                ''', (
                    public_key,
                    contact_name,
                    device_type,
                    json.dumps(contact_data)
                ))
                
                self.logger.info(f"Added repeater {contact_name} to database before purging")
            
            # Track whether device removal was successful
            device_removal_successful = False
            
            # Try multiple approaches to remove the contact
            try:
                self.logger.info(f"Starting removal of contact '{contact_name}' from device...")
                
                # Method 1: Try direct removal from contacts dictionary
                try:
                    self.logger.info(f"Method 1: Attempting direct removal from contacts dictionary...")
                    if contact_key in self.bot.meshcore.contacts:
                        del self.bot.meshcore.contacts[contact_key]
                        self.logger.info(f"Successfully removed contact '{contact_name}' from contacts dictionary")
                        device_removal_successful = True
                    else:
                        self.logger.warning(f"Contact '{contact_name}' not found in contacts dictionary")
                except Exception as e:
                    self.logger.warning(f"Direct removal failed: {e}")
                
                # Method 2: Try using meshcore commands if available
                if not device_removal_successful and hasattr(self.bot.meshcore, 'commands'):
                    try:
                        self.logger.info(f"Method 2: Attempting removal via meshcore commands...")
                        # Check if there's a remove_contact method
                        if hasattr(self.bot.meshcore.commands, 'remove_contact'):
                            # Try different parameter combinations
                            try:
                                # Try with contact_data
                                result = await self.bot.meshcore.commands.remove_contact(contact_data)
                                if result:
                                    self.logger.info(f"Successfully removed contact '{contact_name}' via meshcore commands (contact_data)")
                                    device_removal_successful = True
                            except Exception as e1:
                                self.logger.debug(f"remove_contact(contact_data) failed: {e1}")
                                try:
                                    # Try with public_key
                                    result = await self.bot.meshcore.commands.remove_contact(public_key)
                                    if result:
                                        self.logger.info(f"Successfully removed contact '{contact_name}' via meshcore commands (public_key)")
                                        device_removal_successful = True
                                except Exception as e2:
                                    self.logger.debug(f"remove_contact(public_key) failed: {e2}")
                                    try:
                                        # Try with contact_key
                                        result = await self.bot.meshcore.commands.remove_contact(contact_key)
                                        if result:
                                            self.logger.info(f"Successfully removed contact '{contact_name}' via meshcore commands (contact_key)")
                                            device_removal_successful = True
                                    except Exception as e3:
                                        self.logger.debug(f"remove_contact(contact_key) failed: {e3}")
                                        self.logger.warning(f"All meshcore commands remove_contact attempts failed")
                        else:
                            self.logger.info("No remove_contact method found in meshcore commands")
                    except Exception as e:
                        self.logger.warning(f"Meshcore commands removal failed: {e}")
                
                # Method 3: Try CLI as fallback
                if not device_removal_successful:
                    try:
                        self.logger.info(f"Method 3: Attempting removal via CLI...")
                        import asyncio
                        import sys
                        import io
                        
                        # Use asyncio.wait_for to add timeout for LoRa communication
                        start_time = asyncio.get_event_loop().time()
                        
                        # Use the meshcore-cli API for device commands
                        from meshcore_cli.meshcore_cli import next_cmd
                        
                        # Capture stdout/stderr to catch "Unknown contact" messages
                        old_stdout = sys.stdout
                        old_stderr = sys.stderr
                        captured_output = io.StringIO()
                        captured_errors = io.StringIO()
                        
                        try:
                            sys.stdout = captured_output
                            sys.stderr = captured_errors
                            
                            # Try using the contact key instead of public key
                            result = await asyncio.wait_for(
                                next_cmd(self.bot.meshcore, ["remove_contact", contact_key]),
                                timeout=30.0  # 30 second timeout for LoRa communication
                            )
                        finally:
                            sys.stdout = old_stdout
                            sys.stderr = old_stderr
                        
                        # Get captured output
                        stdout_content = captured_output.getvalue()
                        stderr_content = captured_errors.getvalue()
                        all_output = stdout_content + stderr_content
                        
                        end_time = asyncio.get_event_loop().time()
                        duration = end_time - start_time
                        self.logger.info(f"CLI remove command completed in {duration:.2f} seconds")
                        
                        # Check if removal was successful
                        self.logger.debug(f"CLI command result: {result}")
                        self.logger.debug(f"CLI captured output: {all_output}")
                        
                        # Check if the captured output indicates the contact was unknown (doesn't exist)
                        if "unknown contact" in all_output.lower():
                            self.logger.warning(f"CLI: Contact '{contact_name}' was not found on device")
                        elif result is not None:
                            self.logger.info(f"CLI: Successfully removed contact '{contact_name}' from device")
                            device_removal_successful = True
                        else:
                            self.logger.warning(f"CLI: Contact removal command returned no result for '{contact_name}'")
                            
                    except Exception as e:
                        self.logger.warning(f"CLI removal failed: {e}")
                
                # Verify removal and ensure persistence
                if device_removal_successful:
                    import asyncio
                    await asyncio.sleep(3)  # Give device more time to process and save
                    
                    # Try to force device to save changes
                    try:
                        self.logger.info(f"Attempting to force device to save contact changes...")
                        from meshcore_cli.meshcore_cli import next_cmd
                        
                        # Try to refresh contacts from device
                        try:
                            self.logger.info("Refreshing contacts from device...")
                            await asyncio.wait_for(
                                next_cmd(self.bot.meshcore, ["contacts"]),
                                timeout=15.0
                            )
                            self.logger.info("Contacts refreshed from device")
                        except Exception as e:
                            self.logger.warning(f"Failed to refresh contacts: {e}")
                        
                    except Exception as e:
                        self.logger.warning(f"Failed to force device persistence: {e}")
                    
                    # Wait a bit more after refresh
                    await asyncio.sleep(1)
                    
                    # Check if contact still exists in the bot's memory after refresh
                    contact_still_exists = contact_key in self.bot.meshcore.contacts
                    
                    if contact_still_exists:
                        self.logger.warning(f"Contact '{contact_name}' still exists after removal and refresh - removal may have failed")
                        device_removal_successful = False
                    else:
                        self.logger.info(f"Verified: Contact '{contact_name}' successfully removed from device")
                
            except Exception as e:
                self.logger.error(f"Failed to remove contact '{contact_name}' from device: {e}")
                self.logger.debug(f"Error type: {type(e).__name__}")
                device_removal_successful = False
            
            # Only mark as inactive in database if device removal was successful
            if device_removal_successful:
                self.db_manager.execute_update(
                    'UPDATE repeater_contacts SET is_active = 0, purge_count = purge_count + 1 WHERE public_key = ?',
                    (public_key,)
                )
                
                # Log the purge action
                self.db_manager.execute_update('''
                    INSERT INTO purging_log (action, public_key, name, reason)
                    VALUES ('purged', ?, ?, ?)
                ''', (public_key, contact_name, reason))
                
                self.logger.info(f"Successfully purged repeater {contact_name}: {reason}")
                self.logger.debug(f"Purge process completed successfully for {contact_name}")
                return True
            else:
                self.logger.error(f"Failed to remove repeater {contact_name} from device - not marking as purged in database")
                # Log the failed attempt
                self.db_manager.execute_update('''
                    INSERT INTO purging_log (action, public_key, name, reason)
                    VALUES ('purge_failed', ?, ?, ?)
                ''', (public_key, contact_name, f"{reason} - Device removal failed"))
                return False
            
        except Exception as e:
            self.logger.error(f"Error purging repeater {contact_key}: {e}")
            self.logger.debug(f"Error type: {type(e).__name__}")
            return False
    
    async def force_purge_repeater_from_contacts(self, public_key: str, reason: str = "Force purge") -> bool:
        """Force remove a repeater from device contacts using multiple methods"""
        self.logger.info(f"Starting FORCE purge process for public_key: {public_key}")
        self.logger.debug(f"Force purge reason: {reason}")
        
        try:
            # Find the contact in meshcore
            contact_to_remove = None
            contact_name = None
            
            for contact_key, contact_data in self.bot.meshcore.contacts.items():
                if contact_data.get('public_key', contact_key) == public_key:
                    contact_to_remove = contact_data
                    contact_name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
                    break
            
            if not contact_to_remove:
                self.logger.warning(f"Repeater with public key {public_key} not found in current contacts")
                return False
            
            # Method 1: Try standard removal
            self.logger.info(f"Method 1: Attempting standard removal for '{contact_name}'")
            success = await self.purge_repeater_from_contacts(public_key, reason)
            if success:
                self.logger.info(f"Standard removal successful for '{contact_name}'")
                return True
            
            # Method 2: Try alternative removal commands
            self.logger.info(f"Method 2: Attempting alternative removal for '{contact_name}'")
            try:
                from meshcore_cli.meshcore_cli import next_cmd
                
                # Try different removal commands
                alternative_commands = [
                    ["delete_contact", public_key],
                    ["remove", public_key],
                    ["del", public_key],
                    ["clear_contact", public_key]
                ]
                
                for cmd in alternative_commands:
                    try:
                        self.logger.info(f"Trying command: {' '.join(cmd)}")
                        
                        # Capture stdout/stderr to catch "Unknown contact" messages
                        import sys
                        import io
                        old_stdout = sys.stdout
                        old_stderr = sys.stderr
                        captured_output = io.StringIO()
                        captured_errors = io.StringIO()
                        
                        try:
                            sys.stdout = captured_output
                            sys.stderr = captured_errors
                            
                            result = await asyncio.wait_for(
                                next_cmd(self.bot.meshcore, cmd),
                                timeout=15.0
                            )
                        finally:
                            sys.stdout = old_stdout
                            sys.stderr = old_stderr
                        
                        # Get captured output
                        stdout_content = captured_output.getvalue()
                        stderr_content = captured_errors.getvalue()
                        all_output = stdout_content + stderr_content
                        
                        if result is not None:
                            self.logger.debug(f"Alternative command {' '.join(cmd)} result: {result}")
                            self.logger.debug(f"Captured output: {all_output}")
                            
                            # Check if the captured output indicates the contact was unknown (doesn't exist)
                            if "unknown contact" in all_output.lower():
                                self.logger.warning(f"Contact '{contact_name}' was not found on device - this suggests the contact list is out of sync")
                                # Don't mark as successful - we need to actually remove contacts that exist
                                continue  # Try next command
                            else:
                                self.logger.info(f"Alternative command {' '.join(cmd)} succeeded")
                                # Verify removal
                                await asyncio.sleep(1)
                                contact_still_exists = False
                                for check_key, check_data in self.bot.meshcore.contacts.items():
                                    if check_data.get('public_key', check_key) == public_key:
                                        contact_still_exists = True
                                        break
                                
                                if not contact_still_exists:
                                    # Mark as purged in database
                                    self.db_manager.execute_update(
                                        'UPDATE repeater_contacts SET is_active = 0, purge_count = purge_count + 1 WHERE public_key = ?',
                                        (public_key,)
                                    )
                                    
                                    self.db_manager.execute_update('''
                                        INSERT INTO purging_log (action, public_key, name, reason)
                                        VALUES ('force_purged', ?, ?, ?)
                                    ''', (public_key, contact_name, f"{reason} - Alternative command: {' '.join(cmd)}"))
                                    
                                    self.logger.info(f"Force purge successful for '{contact_name}' using {' '.join(cmd)}")
                                    return True
                    except Exception as e:
                        self.logger.debug(f"Alternative command {' '.join(cmd)} failed: {e}")
                        continue
                        
            except Exception as e:
                self.logger.error(f"Error with alternative removal methods: {e}")
            
            # Method 3: Mark as purged anyway and log the issue
            self.logger.warning(f"All removal methods failed for '{contact_name}' - marking as purged anyway")
            self.db_manager.execute_update(
                'UPDATE repeater_contacts SET is_active = 0, purge_count = purge_count + 1 WHERE public_key = ?',
                (public_key,)
            )
            
            self.db_manager.execute_update('''
                INSERT INTO purging_log (action, public_key, name, reason)
                VALUES ('force_purged_failed', ?, ?, ?)
            ''', (public_key, contact_name, f"{reason} - All removal methods failed, marked as purged anyway"))
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error in force purge for repeater {public_key}: {e}")
            return False
    
    async def purge_old_repeaters(self, days_old: int = 30, reason: str = "Automatic purge - old contacts") -> int:
        """Purge repeaters that haven't been seen in specified days"""
        try:
            cutoff_date = datetime.now() - timedelta(days=days_old)
            
            # Find old repeaters by checking their actual last_advert time from contact data
            # We need to cross-reference the database with the current contact data
            old_repeaters = []
            
            # Get all active repeaters from database
            all_repeaters = self.db_manager.execute_query('''
                SELECT public_key, name FROM repeater_contacts 
                WHERE is_active = 1
            ''')
            
            # Check each repeater's actual last_advert time
            for repeater in all_repeaters:
                public_key = repeater['public_key']
                name = repeater['name']
                
                # Find the contact in meshcore.contacts
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', contact_key) == public_key:
                        # Check the actual last_advert time
                        last_advert = contact_data.get('last_advert')
                        if last_advert:
                            try:
                                # Parse the last_advert timestamp
                                if isinstance(last_advert, str):
                                    last_advert_dt = datetime.fromisoformat(last_advert.replace('Z', '+00:00'))
                                elif isinstance(last_advert, (int, float)):
                                    # Unix timestamp (seconds since epoch)
                                    last_advert_dt = datetime.fromtimestamp(last_advert)
                                else:
                                    # Assume it's already a datetime object
                                    last_advert_dt = last_advert
                                
                                # Check if it's older than cutoff
                                if last_advert_dt < cutoff_date:
                                    old_repeaters.append({
                                        'public_key': public_key,
                                        'name': name,
                                        'last_seen': last_advert
                                    })
                                    self.logger.debug(f"Found old repeater: {name} (last_advert: {last_advert} -> {last_advert_dt})")
                                else:
                                    self.logger.debug(f"Recent repeater: {name} (last_advert: {last_advert} -> {last_advert_dt})")
                            except Exception as e:
                                self.logger.debug(f"Error parsing last_advert for {name}: {e} (type: {type(last_advert)}, value: {last_advert})")
                        break
            
            # Debug logging
            self.logger.info(f"Purge criteria: cutoff_date = {cutoff_date.isoformat()}, days_old = {days_old}")
            self.logger.info(f"Found {len(old_repeaters)} repeaters older than {days_old} days")
            
            # Show some examples of what we found
            if old_repeaters:
                for i, repeater in enumerate(old_repeaters[:3]):  # Show first 3
                    self.logger.info(f"Old repeater {i+1}: {repeater['name']} (last_advert: {repeater['last_seen']})")
            else:
                # Show some recent repeaters to understand the timestamp format
                self.logger.info("No old repeaters found. Showing recent repeater activity:")
                recent_count = 0
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if self._is_repeater_device(contact_data):
                        last_advert = contact_data.get('last_advert', 'No last_advert')
                        name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
                        if last_advert != 'No last_advert':
                            try:
                                if isinstance(last_advert, (int, float)):
                                    last_advert_dt = datetime.fromtimestamp(last_advert)
                                    self.logger.info(f"  {name}: {last_advert} (Unix timestamp) -> {last_advert_dt}")
                                else:
                                    self.logger.info(f"  {name}: {last_advert} (type: {type(last_advert)})")
                            except Exception as e:
                                self.logger.info(f"  {name}: {last_advert} (parse error: {e})")
                        else:
                            self.logger.info(f"  {name}: No last_advert")
                        recent_count += 1
                        if recent_count >= 3:
                            break
            
            purged_count = 0
            
            # Process repeaters with delays to avoid overwhelming LoRa network
            self.logger.info(f"Starting batch purge of {len(old_repeaters)} old repeaters...")
            start_time = asyncio.get_event_loop().time()
            
            for i, repeater in enumerate(old_repeaters):
                public_key = repeater['public_key']
                name = repeater['name']
                
                self.logger.info(f"Purging repeater {i+1}/{len(old_repeaters)}: {name}")
                self.logger.debug(f"Processing public_key: {public_key}")
                
                try:
                    if await self.purge_repeater_from_contacts(public_key, f"{reason} (last seen: {cutoff_date.date()})"):
                        purged_count += 1
                        self.logger.info(f"Successfully purged {i+1}/{len(old_repeaters)}: {name}")
                    else:
                        self.logger.warning(f"Failed to purge {i+1}/{len(old_repeaters)}: {name}")
                except Exception as e:
                    self.logger.error(f"Exception purging {i+1}/{len(old_repeaters)}: {name} - {e}")
                
                # Add delay between removals to avoid overwhelming LoRa network
                if i < len(old_repeaters) - 1:  # Don't delay after the last one
                    self.logger.debug(f"Waiting 2 seconds before next removal...")
                    await asyncio.sleep(2)  # 2 second delay between removals
            
            end_time = asyncio.get_event_loop().time()
            total_duration = end_time - start_time
            self.logger.info(f"Batch purge completed in {total_duration:.2f} seconds")
            
            # After purging, toggle auto-add off and discover new contacts manually
            if purged_count > 0:
                await self._post_purge_contact_management()
            
            self.logger.info(f"Purged {purged_count} old repeaters (older than {days_old} days)")
            return purged_count
                
        except Exception as e:
            self.logger.error(f"Error purging old repeaters: {e}")
            return 0
    
    async def _post_purge_contact_management(self):
        """Post-purge contact management: enable manual contact addition and discover new contacts manually"""
        try:
            self.logger.info("Starting post-purge contact management...")
            
            # Step 1: Enable manual contact addition
            self.logger.info("Enabling manual contact addition on device...")
            try:
                from meshcore_cli.meshcore_cli import next_cmd
                result = await asyncio.wait_for(
                    next_cmd(self.bot.meshcore, ["set_manual_add_contacts", "true"]),
                    timeout=15.0
                )
                self.logger.info("Successfully enabled manual contact addition")
                self.logger.debug(f"Manual add contacts enable result: {result}")
            except asyncio.TimeoutError:
                self.logger.warning("Timeout enabling manual contact addition (LoRa communication)")
            except Exception as e:
                self.logger.error(f"Failed to enable manual contact addition: {e}")
            
            # Step 2: Discover new companion contacts manually
            self.logger.info("Starting manual companion contact discovery...")
            try:
                from meshcore_cli.meshcore_cli import next_cmd
                result = await asyncio.wait_for(
                    next_cmd(self.bot.meshcore, ["discover_companion_contacts"]),
                    timeout=30.0
                )
                self.logger.info("Successfully initiated companion contact discovery")
                self.logger.debug(f"Discovery result: {result}")
            except asyncio.TimeoutError:
                self.logger.warning("Timeout during companion contact discovery (LoRa communication)")
            except Exception as e:
                self.logger.error(f"Failed to discover companion contacts: {e}")
            
            # Step 3: Log the post-purge management action
            self.db_manager.execute_update(
                'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                ('post_purge_management', 'Enabled manual contact addition and initiated companion contact discovery')
            )
            
            self.logger.info("Post-purge contact management completed")
            
        except Exception as e:
            self.logger.error(f"Error in post-purge contact management: {e}")
    
    async def get_contact_list_status(self) -> Dict:
        """Get current contact list status and limits"""
        try:
            # Get current contact count
            current_contacts = len(self.bot.meshcore.contacts) if hasattr(self.bot.meshcore, 'contacts') else 0
            
            # Update contact limit from device info
            await self._update_contact_limit_from_device()
            
            # Use the updated contact limit
            estimated_limit = self.contact_limit
            
            # Calculate usage percentage
            usage_percentage = (current_contacts / estimated_limit) * 100 if estimated_limit > 0 else 0
            
            # Count repeaters from actual device contacts (more accurate than database)
            device_repeater_count = 0
            if hasattr(self.bot.meshcore, 'contacts'):
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if self._is_repeater_device(contact_data):
                        device_repeater_count += 1
            
            # Also get database repeater count for reference
            db_repeater_count = len(await self.get_repeater_contacts(active_only=True))
            
            # Use device count as primary, fall back to database count
            repeater_count = device_repeater_count if device_repeater_count > 0 else db_repeater_count
            
            # Calculate companion count (total contacts minus repeaters)
            companion_count = current_contacts - repeater_count
            
            # Get contacts without recent adverts (potential candidates for removal)
            stale_contacts = await self._get_stale_contacts()
            
            return {
                'current_contacts': current_contacts,
                'estimated_limit': estimated_limit,
                'usage_percentage': usage_percentage,
                'repeater_count': repeater_count,
                'companion_count': companion_count,
                'stale_contacts_count': len(stale_contacts),
                'available_slots': max(0, estimated_limit - current_contacts),
                'is_near_limit': usage_percentage > 80,  # Warning at 80%
                'is_at_limit': usage_percentage >= 95,   # Critical at 95%
                'stale_contacts': stale_contacts[:10]  # Top 10 stale contacts
            }
            
        except Exception as e:
            self.logger.error(f"Error getting contact list status: {e}")
            return {}
    
    async def _get_stale_contacts(self, days_without_advert: int = 7) -> List[Dict]:
        """Get contacts that haven't sent adverts in specified days"""
        try:
            cutoff_date = datetime.now() - timedelta(days=days_without_advert)
            
            # Get contacts from device
            if not hasattr(self.bot.meshcore, 'contacts'):
                return []
            
            stale_contacts = []
            for contact_key, contact_data in self.bot.meshcore.contacts.items():
                # Skip repeaters (they're managed separately)
                if self._is_repeater_device(contact_data):
                    continue
                
                # Check last_seen or similar timestamp fields
                last_seen = contact_data.get('last_seen', contact_data.get('last_advert', contact_data.get('timestamp')))
                if last_seen:
                    try:
                        # Parse timestamp
                        if isinstance(last_seen, str):
                            last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                        elif isinstance(last_seen, (int, float)):
                            # Unix timestamp (seconds since epoch)
                            last_seen_dt = datetime.fromtimestamp(last_seen)
                        else:
                            # Assume it's already a datetime object
                            last_seen_dt = last_seen
                        
                        if last_seen_dt < cutoff_date:
                            stale_contacts.append({
                                'name': contact_data.get('name', contact_data.get('adv_name', 'Unknown')),
                                'public_key': contact_data.get('public_key', ''),
                                'last_seen': last_seen,
                                'days_stale': (datetime.now() - last_seen_dt).days
                            })
                    except Exception as e:
                        self.logger.debug(f"Error parsing timestamp for contact {contact_data.get('name', 'Unknown')}: {e}")
                        continue
            
            # Sort by days stale (oldest first)
            stale_contacts.sort(key=lambda x: x['days_stale'], reverse=True)
            return stale_contacts
            
        except Exception as e:
            self.logger.error(f"Error getting stale contacts: {e}")
            return []
    
    async def manage_contact_list(self, auto_cleanup: bool = True) -> Dict:
        """Manage contact list to prevent hitting limits"""
        try:
            status = await self.get_contact_list_status()
            
            if not status:
                return {'error': 'Failed to get contact list status'}
            
            actions_taken = []
            
            # If near limit, start cleanup
            if status['is_near_limit']:
                self.logger.warning(f"Contact list at {status['usage_percentage']:.1f}% capacity ({status['current_contacts']}/{status['estimated_limit']})")
                
                if auto_cleanup:
                    # Step 1: Remove stale contacts
                    stale_removed = await self._remove_stale_contacts(status['stale_contacts'])
                    if stale_removed > 0:
                        actions_taken.append(f"Removed {stale_removed} stale contacts")
                    
                    # Step 2: If still near limit, remove old repeaters
                    if status['is_near_limit'] and status['repeater_count'] > 0:
                        old_repeaters_removed = await self.purge_old_repeaters(days_old=14, reason="Contact list management - near limit")
                        if old_repeaters_removed > 0:
                            actions_taken.append(f"Removed {old_repeaters_removed} old repeaters")
                    
                    # Step 3: If still at critical limit, more aggressive cleanup
                    if status['is_at_limit']:
                        self.logger.warning("Contact list at critical capacity, performing aggressive cleanup")
                        aggressive_removed = await self._aggressive_contact_cleanup()
                        if aggressive_removed > 0:
                            actions_taken.append(f"Aggressive cleanup removed {aggressive_removed} contacts")
            
            # Log the management action
            if actions_taken:
                self.db_manager.execute_update(
                    'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                    ('contact_management', f'Contact list management: {"; ".join(actions_taken)}')
                )
            
            return {
                'status': status,
                'actions_taken': actions_taken,
                'success': True
            }
            
        except Exception as e:
            self.logger.error(f"Error managing contact list: {e}")
            return {'error': str(e), 'success': False}
    
    async def _remove_stale_contacts(self, stale_contacts: List[Dict], max_remove: int = 10) -> int:
        """Remove stale contacts to free up space"""
        try:
            removed_count = 0
            
            for contact in stale_contacts[:max_remove]:
                try:
                    contact_name = contact['name']
                    public_key = contact['public_key']
                    
                    self.logger.info(f"Removing stale contact: {contact_name} (last seen {contact['days_stale']} days ago)")
                    
                    # Check if we have a valid public key
                    if not public_key or public_key.strip() == '':
                        self.logger.warning(f"Skipping stale contact '{contact_name}': no public key available")
                        continue
                    
                    # Remove from device using MeshCore API
                    result = await asyncio.wait_for(
                        self.bot.meshcore.commands.remove_contact(public_key),
                        timeout=15.0
                    )
                    
                    if result.type == EventType.OK:
                        removed_count += 1
                        self.logger.info(f"✅ Successfully removed stale contact: {contact_name}")
                        
                        # Log the removal
                        self.db_manager.execute_update(
                            'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                            ('stale_contact_removal', f'Removed stale contact: {contact_name} (last seen {contact["days_stale"]} days ago)')
                        )
                    else:
                        error_code = result.payload.get('error_code', 'unknown') if hasattr(result, 'payload') else 'unknown'
                        self.logger.warning(f"❌ Failed to remove stale contact: {contact_name} - Error: {result.type}, Code: {error_code}")
                    
                    # Small delay between removals
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    self.logger.error(f"Error removing stale contact {contact.get('name', 'Unknown')}: {e}")
                    continue
            
            return removed_count
            
        except Exception as e:
            self.logger.error(f"Error removing stale contacts: {e}")
            return 0
    
    async def _aggressive_contact_cleanup(self) -> int:
        """Perform aggressive cleanup when at critical limit"""
        try:
            removed_count = 0
            
            # Remove very old repeaters (7+ days)
            old_repeaters = await self.purge_old_repeaters(days_old=7, reason="Aggressive cleanup - critical limit")
            removed_count += old_repeaters
            
            # Remove very stale contacts (14+ days)
            very_stale = await self._get_stale_contacts(days_without_advert=14)
            stale_removed = await self._remove_stale_contacts(very_stale, max_remove=20)
            removed_count += stale_removed
            
            return removed_count
            
        except Exception as e:
            self.logger.error(f"Error in aggressive contact cleanup: {e}")
            return 0
    
    async def add_discovered_contact(self, contact_name: str, public_key: str = None, reason: str = "Manual addition") -> bool:
        """Add a discovered contact to the contact list using multiple methods"""
        try:
            self.logger.info(f"Adding discovered contact: {contact_name}")
            
            # Track whether contact addition was successful
            contact_addition_successful = False
            
            # Method 1: Try using meshcore commands if available
            if hasattr(self.bot.meshcore, 'commands'):
                try:
                    self.logger.info(f"Method 1: Attempting addition via meshcore commands...")
                    # Check if there's an add_contact method
                    if hasattr(self.bot.meshcore.commands, 'add_contact'):
                        # Try different parameter combinations
                        try:
                            # Try with contact_name and public_key
                            result = await self.bot.meshcore.commands.add_contact(contact_name, public_key)
                            if result:
                                self.logger.info(f"Successfully added contact '{contact_name}' via meshcore commands (name+key)")
                                contact_addition_successful = True
                        except Exception as e1:
                            self.logger.debug(f"add_contact(name, key) failed: {e1}")
                            try:
                                # Try with just contact_name
                                result = await self.bot.meshcore.commands.add_contact(contact_name)
                                if result:
                                    self.logger.info(f"Successfully added contact '{contact_name}' via meshcore commands (name only)")
                                    contact_addition_successful = True
                            except Exception as e2:
                                self.logger.debug(f"add_contact(name) failed: {e2}")
                                self.logger.warning(f"All meshcore commands add_contact attempts failed")
                    else:
                        self.logger.info("No add_contact method found in meshcore commands")
                except Exception as e:
                    self.logger.warning(f"Meshcore commands addition failed: {e}")
            
            # Method 2: Try CLI as fallback
            if not contact_addition_successful:
                try:
                    self.logger.info(f"Method 2: Attempting addition via CLI...")
                    from meshcore_cli.meshcore_cli import next_cmd
                    import sys
                    import io
                    
                    # Capture stdout/stderr to catch any error messages
                    old_stdout = sys.stdout
                    old_stderr = sys.stderr
                    captured_output = io.StringIO()
                    captured_errors = io.StringIO()
                    
                    try:
                        sys.stdout = captured_output
                        sys.stderr = captured_errors
                        
                        result = await asyncio.wait_for(
                            next_cmd(self.bot.meshcore, ["add_contact", contact_name, public_key] if public_key else ["add_contact", contact_name]),
                            timeout=15.0
                        )
                    finally:
                        sys.stdout = old_stdout
                        sys.stderr = old_stderr
                    
                    # Get captured output
                    stdout_content = captured_output.getvalue()
                    stderr_content = captured_errors.getvalue()
                    all_output = stdout_content + stderr_content
                    
                    self.logger.debug(f"CLI command result: {result}")
                    self.logger.debug(f"CLI captured output: {all_output}")
                    
                    if result is not None:
                        self.logger.info(f"CLI: Successfully added contact '{contact_name}' from device")
                        contact_addition_successful = True
                    else:
                        self.logger.warning(f"CLI: Contact addition command returned no result for '{contact_name}'")
                        
                except Exception as e:
                    self.logger.warning(f"CLI addition failed: {e}")
            
            # Method 3: Try discovery approach as last resort
            if not contact_addition_successful:
                try:
                    self.logger.info(f"Method 3: Attempting addition via discovery...")
                    from meshcore_cli.meshcore_cli import next_cmd
                    
                    result = await asyncio.wait_for(
                        next_cmd(self.bot.meshcore, ["discover_companion_contacts"]),
                        timeout=30.0
                    )
                    
                    if result is not None:
                        self.logger.info("Contact discovery initiated")
                        contact_addition_successful = True
                    else:
                        self.logger.warning("Contact discovery failed")
                        
                except Exception as e:
                    self.logger.warning(f"Discovery addition failed: {e}")
            
            # Log the addition if successful
            if contact_addition_successful:
                self.db_manager.execute_update(
                    'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                    ('contact_addition', f'Added discovered contact: {contact_name} - {reason}')
                )
                self.logger.info(f"Successfully added contact '{contact_name}': {reason}")
                return True
            else:
                self.logger.error(f"Failed to add contact '{contact_name}' - all methods failed")
                return False
            
        except Exception as e:
            self.logger.error(f"Error adding discovered contact: {e}")
            return False
    
    async def toggle_auto_add(self, enabled: bool, reason: str = "Manual toggle") -> bool:
        """Toggle the manual contact addition setting on the device"""
        try:
            from meshcore_cli.meshcore_cli import next_cmd
            
            self.logger.info(f"{'Enabling' if enabled else 'Disabling'} manual contact addition on device...")
            
            result = await asyncio.wait_for(
                next_cmd(self.bot.meshcore, ["set_manual_add_contacts", "true" if enabled else "false"]),
                timeout=15.0
            )
            
            self.logger.info(f"Successfully {'enabled' if enabled else 'disabled'} manual contact addition")
            self.logger.debug(f"Manual contact addition toggle result: {result}")
            
            # Log the action
            self.db_manager.execute_update(
                'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                ('manual_add_toggle', f'{"Enabled" if enabled else "Disabled"} manual contact addition - {reason}')
            )
            
            return True
            
        except asyncio.TimeoutError:
            self.logger.warning("Timeout toggling manual contact addition (LoRa communication)")
            return False
        except Exception as e:
            self.logger.error(f"Failed to toggle manual contact addition: {e}")
            return False
    
    async def discover_companion_contacts(self, reason: str = "Manual discovery") -> bool:
        """Manually discover companion contacts"""
        try:
            from meshcore_cli.meshcore_cli import next_cmd
            
            self.logger.info("Starting manual companion contact discovery...")
            
            result = await asyncio.wait_for(
                next_cmd(self.bot.meshcore, ["discover_companion_contacts"]),
                timeout=30.0
            )
            
            self.logger.info("Successfully initiated companion contact discovery")
            self.logger.debug(f"Discovery result: {result}")
            
            # Log the action
            self.db_manager.execute_update(
                'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                ('companion_discovery', f'Manual companion contact discovery - {reason}')
            )
            
            return True
            
        except asyncio.TimeoutError:
            self.logger.warning("Timeout during companion contact discovery (LoRa communication)")
            return False
        except Exception as e:
            self.logger.error(f"Failed to discover companion contacts: {e}")
            return False
    
    async def restore_repeater(self, public_key: str, reason: str = "Manual restore") -> bool:
        """Restore a previously purged repeater"""
        try:
            # Get repeater info before updating
            result = self.db_manager.execute_query('''
                SELECT name, contact_data FROM repeater_contacts WHERE public_key = ?
            ''', (public_key,))
            
            if not result:
                self.logger.warning(f"No repeater found with public key {public_key}")
                return False
            
            name = result[0]['name']
            
            # Mark as active again
            self.db_manager.execute_update(
                'UPDATE repeater_contacts SET is_active = 1 WHERE public_key = ?',
                (public_key,)
            )
            
            # Log the restore action
            self.db_manager.execute_update('''
                INSERT INTO purging_log (action, public_key, name, reason)
                VALUES ('restored', ?, ?, ?)
            ''', (public_key, name, reason))
            
            # Note: Restoring a contact to the device would require re-adding it
            # This is complex as it requires the contact's URI or public key
            # For now, we just mark it as active in our database
            # The contact would need to be re-discovered through normal mesh operations
            
            self.logger.info(f"Restored repeater {name} ({public_key}) - contact will need to be re-discovered")
            return True
                    
        except Exception as e:
            self.logger.error(f"Error restoring repeater {public_key}: {e}")
            return False
    
    async def get_purging_stats(self) -> Dict:
        """Get statistics about repeater purging operations"""
        try:
            # Get total counts
            total_repeaters = self.db_manager.execute_query('SELECT COUNT(*) as count FROM repeater_contacts')[0]['count']
            active_repeaters = self.db_manager.execute_query('SELECT COUNT(*) as count FROM repeater_contacts WHERE is_active = 1')[0]['count']
            purged_repeaters = self.db_manager.execute_query('SELECT COUNT(*) as count FROM repeater_contacts WHERE is_active = 0')[0]['count']
            
            # Get recent purging activity
            recent_activity = self.db_manager.execute_query('''
                SELECT action, COUNT(*) as count FROM purging_log 
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY action
            ''')
            
            return {
                'total_repeaters': total_repeaters,
                'active_repeaters': active_repeaters,
                'purged_repeaters': purged_repeaters,
                'recent_activity_7_days': {row['action']: row['count'] for row in recent_activity}
            }
                
        except Exception as e:
            self.logger.error(f"Error getting purging stats: {e}")
            return {}
    
    async def cleanup_database(self, days_to_keep_logs: int = 90):
        """Clean up old purging log entries"""
        try:
            cutoff_date = datetime.now() - timedelta(days=days_to_keep_logs)
            
            deleted_count = self.db_manager.execute_update(
                'DELETE FROM purging_log WHERE timestamp < ?',
                (cutoff_date.isoformat(),)
            )
            
            if deleted_count > 0:
                self.logger.info(f"Cleaned up {deleted_count} old purging log entries")
                
        except Exception as e:
            self.logger.error(f"Error cleaning up database: {e}")
    
    # Delegate geocoding cache methods to db_manager
    def get_cached_geocoding(self, query: str) -> Tuple[Optional[float], Optional[float]]:
        """Get cached geocoding result for a query"""
        return self.db_manager.get_cached_geocoding(query)
    
    def cache_geocoding(self, query: str, latitude: float, longitude: float, cache_hours: int = 24):
        """Cache geocoding result for future use"""
        self.db_manager.cache_geocoding(query, latitude, longitude, cache_hours)
    
    def cleanup_geocoding_cache(self):
        """Remove expired geocoding cache entries"""
        self.db_manager.cleanup_geocoding_cache()
    
    async def populate_missing_geolocation_data(self, dry_run: bool = False, batch_size: int = 10) -> Dict[str, int]:
        """Populate missing geolocation data (state, country) for repeaters that have coordinates but missing location info"""
        try:
            # Check network connectivity first
            if not dry_run:
                try:
                    import socket
                    socket.create_connection(("nominatim.openstreetmap.org", 443), timeout=5)
                except OSError:
                    return {
                        'total_found': 0,
                        'updated': 0,
                        'errors': 1,
                        'skipped': 0,
                        'error': 'No network connectivity to geocoding service'
                    }
            # Find contacts with valid coordinates but missing state or country
            # Use complete_contact_tracking table to match the geocoding status command
            repeaters_to_update = self.db_manager.execute_query('''
                SELECT id, name, latitude, longitude, city, state, country 
                FROM complete_contact_tracking 
                WHERE latitude IS NOT NULL 
                AND longitude IS NOT NULL 
                AND NOT (latitude = 0.0 AND longitude = 0.0)
                AND latitude BETWEEN -90 AND 90
                AND longitude BETWEEN -180 AND 180
                AND (city IS NULL OR city = '' OR state IS NULL OR country IS NULL)
                AND last_geocoding_attempt IS NULL
                ORDER BY last_heard DESC
                LIMIT ?
            ''', (batch_size,))
            
            if not repeaters_to_update:
                return {
                    'total_found': 0,
                    'updated': 0,
                    'errors': 0,
                    'skipped': 0
                }
            
            self.logger.info(f"Found {len(repeaters_to_update)} repeaters with missing geolocation data")
            
            updated_count = 0
            error_count = 0
            skipped_count = 0
            
            for repeater in repeaters_to_update:
                repeater_id = repeater['id']
                name = repeater['name']
                latitude = repeater['latitude']
                longitude = repeater['longitude']
                current_city = repeater['city']
                current_state = repeater['state']
                current_country = repeater['country']
                
                try:
                    # Get full location information from coordinates
                    location_info = self._get_full_location_from_coordinates(latitude, longitude)
                    
                    # Debug logging to see what we got
                    self.logger.debug(f"Geocoding result for {name}: city='{location_info['city']}', state='{location_info['state']}', country='{location_info['country']}'")
                    
                    # Check if we got any useful data
                    if not any(location_info.values()):
                        self.logger.debug(f"No location data found for {name} at {latitude}, {longitude}")
                        skipped_count += 1
                        # Still add delay to be respectful to the API
                        await asyncio.sleep(2.0)
                        continue
                    
                    # Determine what needs to be updated
                    updates = []
                    params = []
                    
                    # Update city if we don't have one or if the new one is more detailed
                    if not current_city and location_info['city']:
                        updates.append('city = ?')
                        params.append(location_info['city'])
                    elif current_city and location_info['city'] and len(location_info['city']) > len(current_city):
                        # Update if new city info is more detailed (e.g., includes neighborhood)
                        updates.append('city = ?')
                        params.append(location_info['city'])
                    
                    # Update state if missing
                    if not current_state and location_info['state']:
                        updates.append('state = ?')
                        params.append(location_info['state'])
                    
                    # Update country if missing
                    if not current_country and location_info['country']:
                        updates.append('country = ?')
                        params.append(location_info['country'])
                    
                    if updates:
                        if not dry_run:
                            # Update the database - use complete_contact_tracking table
                            update_query = f"UPDATE complete_contact_tracking SET {', '.join(updates)} WHERE id = ?"
                            params.append(repeater_id)
                            
                            self.db_manager.execute_update(update_query, tuple(params))
                            
                            # Log the actual values being updated
                            update_details = []
                            for i, update in enumerate(updates):
                                field = update.split(' = ')[0]
                                value = params[i] if i < len(params) else 'Unknown'
                                update_details.append(f"{field} = {value}")
                            
                            self.logger.info(f"Updated geolocation for {name}: {', '.join(update_details)}")
                        else:
                            self.logger.info(f"[DRY RUN] Would update {name}: {', '.join(updates)}")
                        
                        updated_count += 1
                    else:
                        self.logger.debug(f"No updates needed for {name}")
                        skipped_count += 1
                    
                    # Add longer delay to avoid overwhelming the geocoding service
                    # Nominatim has a rate limit of 1 request per second, we'll be more conservative
                    await asyncio.sleep(2.0)
                    
                except Exception as e:
                    error_msg = str(e)
                    if "429" in error_msg or "Bandwidth limit exceeded" in error_msg:
                        self.logger.warning(f"Rate limited by geocoding service for {name}. Waiting longer...")
                        # Wait longer if we're rate limited
                        await asyncio.sleep(10.0)
                        error_count += 1
                    elif "No route to host" in error_msg or "Connection" in error_msg:
                        self.logger.warning(f"Network connectivity issue for {name}. Skipping...")
                        # Skip this repeater due to network issues
                        skipped_count += 1
                    else:
                        self.logger.error(f"Error updating geolocation for {name}: {e}")
                        error_count += 1
                    continue
            
            result = {
                'total_found': len(repeaters_to_update),
                'updated': updated_count,
                'errors': error_count,
                'skipped': skipped_count
            }
            
            if not dry_run:
                self.logger.info(f"Geolocation update completed: {updated_count} updated, {error_count} errors, {skipped_count} skipped")
            else:
                self.logger.info(f"Geolocation update dry run completed: {updated_count} would be updated, {error_count} errors, {skipped_count} skipped")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error populating missing geolocation data: {e}")
            return {
                'total_found': 0,
                'updated': 0,
                'errors': 1,
                'skipped': 0,
                'error': str(e)
            }
    
    async def periodic_contact_monitoring(self):
        """Periodic monitoring of contact limit and auto-purge if needed"""
        try:
            if not self.auto_purge_enabled:
                return
                
            current_count = len(self.bot.meshcore.contacts)
            
            # Log current status
            if current_count >= self.auto_purge_threshold:
                self.logger.warning(f"⚠️ Contact limit monitoring: {current_count}/{self.contact_limit} contacts (threshold: {self.auto_purge_threshold})")
                
                # Trigger auto-purge
                await self.check_and_auto_purge()
            elif current_count >= self.auto_purge_threshold - 20:
                self.logger.info(f"📊 Contact limit monitoring: {current_count}/{self.contact_limit} contacts (approaching threshold)")
            else:
                self.logger.debug(f"📊 Contact limit monitoring: {current_count}/{self.contact_limit} contacts (healthy)")
            
            # Background geocoding for contacts missing location data
            await self._background_geocoding()
                
        except Exception as e:
            self.logger.error(f"Error in periodic contact monitoring: {e}")
    
    async def _background_geocoding(self):
        """Background geocoding for contacts missing location data"""
        try:
            # Find contacts with coordinates but missing city data
            contacts_needing_geocoding = self.db_manager.execute_query('''
                SELECT id, name, latitude, longitude, city, state, country 
                FROM complete_contact_tracking 
                WHERE latitude IS NOT NULL 
                AND longitude IS NOT NULL 
                AND (city IS NULL OR city = '')
                AND last_geocoding_attempt IS NULL
                ORDER BY last_heard DESC 
                LIMIT 1
            ''')
            
            if not contacts_needing_geocoding:
                return
            
            contact = contacts_needing_geocoding[0]
            contact_id = contact['id']
            name = contact['name']
            lat = contact['latitude']
            lon = contact['longitude']
            
            self.logger.debug(f"🌍 Background geocoding: {name} ({lat}, {lon})")
            
            # Attempt geocoding
            try:
                # Get city from coordinates
                city = self._get_city_from_coordinates(lat, lon)
                
                # Get state and country from coordinates
                state, country = self._get_state_country_from_coordinates(lat, lon)
                
                # Update the contact with geocoded data
                updates = []
                params = []
                
                if city:
                    updates.append("city = ?")
                    params.append(city)
                
                if state:
                    updates.append("state = ?")
                    params.append(state)
                
                if country:
                    updates.append("country = ?")
                    params.append(country)
                
                # Always update the geocoding attempt timestamp
                updates.append("last_geocoding_attempt = ?")
                params.append(datetime.now())
                
                if updates:
                    params.append(contact_id)
                    query = f"UPDATE complete_contact_tracking SET {', '.join(updates)} WHERE id = ?"
                    self.db_manager.execute_update(query, params)
                    
                    self.logger.info(f"✅ Background geocoding successful: {name} → {city or 'Unknown'}, {state or 'Unknown'}, {country or 'Unknown'}")
                else:
                    # Mark as attempted even if no data was found
                    self.db_manager.execute_update(
                        'UPDATE complete_contact_tracking SET last_geocoding_attempt = ? WHERE id = ?',
                        (datetime.now(), contact_id)
                    )
                    self.logger.debug(f"🌍 Background geocoding: {name} - no additional location data found")
                
            except Exception as e:
                # Mark as attempted even if geocoding failed
                self.db_manager.execute_update(
                    'UPDATE complete_contact_tracking SET last_geocoding_attempt = ? WHERE id = ?',
                    (datetime.now(), contact_id)
                )
                self.logger.debug(f"🌍 Background geocoding failed for {name}: {e}")
                
        except Exception as e:
            self.logger.debug(f"Background geocoding error: {e}")
    
    async def _update_contact_limit_from_device(self):
        """Update contact limit from device using proper MeshCore API"""
        try:
            # Use the correct MeshCore API to get device info
            device_info = await self.bot.meshcore.commands.send_device_query()
            
            # Check if the query was successful
            if hasattr(device_info, 'type') and device_info.type.name == 'DEVICE_INFO':
                max_contacts = device_info.payload.get("max_contacts")
                
                if max_contacts and max_contacts > 100:
                    self.contact_limit = max_contacts
                    # Update threshold to be 20 contacts below the limit
                    self.auto_purge_threshold = max(200, max_contacts - 20)
                    self.logger.debug(f"Updated contact limit from device query: {self.contact_limit} (threshold: {self.auto_purge_threshold})")
                    return True
                else:
                    self.logger.debug(f"Device returned invalid max_contacts: {max_contacts}")
            else:
                self.logger.debug(f"Device query failed: {device_info}")
                
        except Exception as e:
            self.logger.debug(f"Could not update contact limit from device: {e}")
        
        # Keep default values if device query failed
        self.logger.debug(f"Using default contact limit: {self.contact_limit}")
        return False
    
    async def get_auto_purge_status(self) -> Dict:
        """Get current auto-purge configuration and status"""
        try:
            # Update contact limit from device info
            await self._update_contact_limit_from_device()
            
            current_count = len(self.bot.meshcore.contacts)
            return {
                'enabled': self.auto_purge_enabled,
                'contact_limit': self.contact_limit,
                'threshold': self.auto_purge_threshold,
                'current_count': current_count,
                'usage_percentage': (current_count / self.contact_limit) * 100,
                'is_near_limit': current_count >= self.auto_purge_threshold,
                'is_at_limit': current_count >= self.contact_limit
            }
        except Exception as e:
            self.logger.error(f"Error getting auto-purge status: {e}")
            return {
                'enabled': False,
                'error': str(e)
            }
    
    async def test_purge_system(self) -> Dict:
        """Test the improved purge system with a single contact"""
        try:
            # Find a test contact to purge
            test_contact = None
            test_public_key = None
            
            # Look for a repeater contact to test with
            for key, contact_data in self.bot.meshcore.contacts.items():
                if self._is_repeater_device(contact_data):
                    test_contact = contact_data
                    test_public_key = contact_data.get('public_key', key)
                    break
            
            if not test_contact:
                return {
                    'success': False,
                    'error': 'No repeater contacts found to test with',
                    'contact_count': len(self.bot.meshcore.contacts)
                }
            
            contact_name = test_contact.get('adv_name', test_contact.get('name', 'Unknown'))
            initial_count = len(self.bot.meshcore.contacts)
            
            self.logger.info(f"Testing purge system with contact: {contact_name}")
            
            # Test the purge
            success = await self.purge_repeater_from_contacts(test_public_key, "Test purge - system validation")
            
            final_count = len(self.bot.meshcore.contacts)
            
            return {
                'success': success,
                'test_contact': contact_name,
                'initial_count': initial_count,
                'final_count': final_count,
                'contacts_removed': initial_count - final_count,
                'purge_method': 'Improved MeshCore API'
            }
            
        except Exception as e:
            self.logger.error(f"Error testing purge system: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def get_daily_advertisement_stats(self, days: int = 30) -> Dict:
        """Get daily advertisement statistics for the specified number of days"""
        try:
            from datetime import date, timedelta
            
            # Calculate date range
            end_date = date.today()
            start_date = end_date - timedelta(days=days-1)
            
            # Get daily advertisement counts with contact details
            daily_stats = self.db_manager.execute_query('''
                SELECT ds.date, 
                       COUNT(DISTINCT ds.public_key) as unique_nodes,
                       SUM(ds.advert_count) as total_adverts,
                       AVG(ds.advert_count) as avg_adverts_per_node,
                       COUNT(DISTINCT c.role) as unique_roles,
                       COUNT(DISTINCT c.device_type) as unique_device_types
                FROM daily_stats ds
                LEFT JOIN complete_contact_tracking c ON ds.public_key = c.public_key
                WHERE ds.date >= ? AND ds.date <= ?
                GROUP BY ds.date
                ORDER BY ds.date DESC
            ''', (start_date, end_date))
            
            # Get summary statistics
            summary = self.db_manager.execute_query('''
                SELECT 
                    COUNT(DISTINCT ds.public_key) as total_unique_nodes,
                    SUM(ds.advert_count) as total_advertisements,
                    COUNT(DISTINCT ds.date) as active_days,
                    AVG(ds.advert_count) as avg_adverts_per_day,
                    COUNT(DISTINCT c.role) as unique_roles,
                    COUNT(DISTINCT c.device_type) as unique_device_types
                FROM daily_stats ds
                LEFT JOIN complete_contact_tracking c ON ds.public_key = c.public_key
                WHERE ds.date >= ? AND ds.date <= ?
            ''', (start_date, end_date))
            
            return {
                'daily_stats': daily_stats,
                'summary': summary[0] if summary else {},
                'date_range': {
                    'start': start_date.isoformat(),
                    'end': end_date.isoformat(),
                    'days': days
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error getting daily advertisement stats: {e}")
            return {'error': str(e)}
    
    def get_nodes_per_day_stats(self, days: int = 30) -> Dict:
        """Get nodes-per-day statistics for accurate daily tracking"""
        try:
            from datetime import date, timedelta
            
            # Calculate date range
            end_date = date.today()
            start_date = end_date - timedelta(days=days-1)
            
            # Get nodes per day with role breakdowns
            nodes_per_day = self.db_manager.execute_query('''
                SELECT ds.date, 
                       COUNT(DISTINCT ds.public_key) as unique_nodes,
                       COUNT(DISTINCT CASE WHEN c.role = 'repeater' THEN ds.public_key END) as repeaters,
                       COUNT(DISTINCT CASE WHEN c.role = 'companion' THEN ds.public_key END) as companions,
                       COUNT(DISTINCT CASE WHEN c.role = 'roomserver' THEN ds.public_key END) as room_servers,
                       COUNT(DISTINCT CASE WHEN c.role = 'sensor' THEN ds.public_key END) as sensors
                FROM daily_stats ds
                LEFT JOIN complete_contact_tracking c ON ds.public_key = c.public_key
                WHERE ds.date >= ? AND ds.date <= ?
                GROUP BY ds.date
                ORDER BY ds.date DESC
            ''', (start_date, end_date))
            
            return {
                'nodes_per_day': nodes_per_day,
                'date_range': {
                    'start': start_date.isoformat(),
                    'end': end_date.isoformat(),
                    'days': days
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error getting nodes per day stats: {e}")
            return {'error': str(e)}