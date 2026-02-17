#!/usr/bin/env python3
"""
Transmission tracker for monitoring message transmission success
Tracks transmitted message hashes and detects repeats from neighboring repeaters
"""

import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class TransmissionRecord:
    """Record of a transmitted message"""
    timestamp: float
    content: str
    target: str  # Channel name or recipient ID
    message_type: str  # 'channel' or 'dm'
    packet_hash: Optional[str] = None
    repeat_count: int = 0
    repeater_prefixes: Set[str] = field(default_factory=set)
    repeater_counts: Dict[str, int] = field(default_factory=dict)  # Count per repeater prefix
    command_id: Optional[str] = None  # For correlating with command data


class TransmissionTracker:
    """Tracks transmitted messages and detects repeats from neighboring repeaters"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        
        # Store pending transmissions (by timestamp window)
        # Key: approximate timestamp (rounded to nearest second)
        # Value: List of TransmissionRecord
        self.pending_transmissions: Dict[int, List[TransmissionRecord]] = {}
        
        # Store confirmed transmissions with hashes
        # Key: packet_hash
        # Value: TransmissionRecord
        self.confirmed_transmissions: Dict[str, TransmissionRecord] = {}
        
        # Time window for matching transmissions (seconds)
        self.match_window = 30  # Match RF data to transmissions within 30 seconds
        
        # Cleanup old records after this time (seconds)
        self.cleanup_after = 300  # 5 minutes
        
        # Track our bot's public key prefix (first 2 hex chars) for filtering
        self.bot_prefix: Optional[str] = None
        self._update_bot_prefix()
    
    def _update_bot_prefix(self):
        """Update bot prefix from meshcore device info"""
        if self.bot.meshcore and hasattr(self.bot.meshcore, 'device'):
            try:
                device_info = self.bot.meshcore.device
                if hasattr(device_info, 'public_key'):
                    pubkey = device_info.public_key
                    if isinstance(pubkey, str) and len(pubkey) >= 2:
                        self.bot_prefix = pubkey[:2].lower()
                    elif isinstance(pubkey, bytes) and len(pubkey) >= 1:
                        self.bot_prefix = f"{pubkey[0]:02x}".lower()
                    self.logger.debug(f"Bot prefix set to: {self.bot_prefix}")
            except Exception as e:
                self.logger.debug(f"Could not determine bot prefix: {e}")
    
    def record_transmission(self, content: str, target: str, message_type: str, 
                          command_id: Optional[str] = None) -> TransmissionRecord:
        """Record a transmission attempt.
        
        Args:
            content: Message content
            target: Channel name or recipient ID
            message_type: 'channel' or 'dm'
            command_id: Optional command ID for correlation
            
        Returns:
            TransmissionRecord: The created record
        """
        record = TransmissionRecord(
            timestamp=time.time(),
            content=content,
            target=target,
            message_type=message_type,
            command_id=command_id
        )
        
        # Store in pending transmissions (by rounded timestamp)
        timestamp_key = int(record.timestamp)
        if timestamp_key not in self.pending_transmissions:
            self.pending_transmissions[timestamp_key] = []
        self.pending_transmissions[timestamp_key].append(record)
        
        self.logger.debug(f"Recorded transmission: {message_type} to {target} at {record.timestamp}")
        
        return record
    
    def match_packet_hash(self, packet_hash: str, rf_timestamp: float) -> Optional[TransmissionRecord]:
        """Match a received packet hash to a pending transmission.
        
        Args:
            packet_hash: Packet hash from received RF data
            rf_timestamp: Timestamp when RF data was received
            
        Returns:
            TransmissionRecord if matched, None otherwise
        """
        if not packet_hash or packet_hash == "0000000000000000":
            return None
        
        # Check if we already have this hash confirmed
        if packet_hash in self.confirmed_transmissions:
            return self.confirmed_transmissions[packet_hash]
        
        # Search in pending transmissions within the match window
        search_start = int(rf_timestamp - self.match_window)
        search_end = int(rf_timestamp + 1)  # Include current second
        
        for timestamp_key in range(search_start, search_end + 1):
            if timestamp_key not in self.pending_transmissions:
                continue
            
            for record in self.pending_transmissions[timestamp_key]:
                # Check if timestamp is within window
                time_diff = abs(rf_timestamp - record.timestamp)
                if time_diff <= self.match_window:
                    # This is a potential match - store the hash
                    if record.packet_hash is None:
                        record.packet_hash = packet_hash
                        # Move to confirmed transmissions
                        self.confirmed_transmissions[packet_hash] = record
                        self.logger.debug(f"Matched transmission hash {packet_hash} to {record.message_type} to {record.target}")
                        return record
        
        return None
    
    def record_repeat(self, packet_hash: str, repeater_prefix: Optional[str] = None) -> bool:
        """Record that we heard a repeat of one of our transmissions.
        
        Args:
            packet_hash: Packet hash of the repeated message
            repeater_prefix: Repeater prefix (first 2 hex chars) that repeated it
            
        Returns:
            True if this was a match to one of our transmissions, False otherwise
        """
        if not packet_hash or packet_hash == "0000000000000000":
            return False
        
        # Find the transmission record
        record = self.confirmed_transmissions.get(packet_hash)
        if not record:
            # Try to match it
            record = self.match_packet_hash(packet_hash, time.time())
        
        if record:
            record.repeat_count += 1
            if repeater_prefix:
                record.repeater_prefixes.add(repeater_prefix)
                # Track count per repeater
                record.repeater_counts[repeater_prefix] = record.repeater_counts.get(repeater_prefix, 0) + 1
            else:
                # No prefix but still a repeat (heard by radio)
                record.repeater_counts['_unknown'] = record.repeater_counts.get('_unknown', 0) + 1
            
            self.logger.info(f"📡 Recorded repeat for hash {packet_hash}: {record.repeat_count} repeats, {len(record.repeater_prefixes)} unique repeaters, prefixes: {sorted(list(record.repeater_prefixes))}")
            
            # Update the database entry if we have a command_id
            if record.command_id and hasattr(self.bot, 'web_viewer_integration'):
                self._update_command_in_database(record)
            
            return True
        
        return False
    
    def _update_command_in_database(self, record: TransmissionRecord):
        """Update command entry in database with latest repeat information"""
        try:
            import sqlite3
            import json
            import sys
            import os
            # Add parent directory to path for imports
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
            from modules.utils import resolve_path
            
            if not record.command_id:
                return
            
            # Get database path (use [Bot] db_path when [Web_Viewer] db_path is unset)
            base_dir = self.bot.bot_root if hasattr(self.bot, 'bot_root') else '.'
            if (self.bot.config.has_section('Web_Viewer') and self.bot.config.has_option('Web_Viewer', 'db_path')
                    and self.bot.config.get('Web_Viewer', 'db_path', fallback='').strip()):
                db_path = resolve_path(self.bot.config.get('Web_Viewer', 'db_path').strip(), base_dir)
            else:
                from pathlib import Path
                db_path = Path(self.bot.db_manager.db_path).resolve()
            
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                
                # Find the command entry by command_id
                cursor.execute('''
                    SELECT id, data FROM packet_stream
                    WHERE type = 'command'
                    ORDER BY timestamp DESC
                    LIMIT 500
                ''')
                
                rows = cursor.fetchall()
                for row_id, data_json in rows:
                    try:
                        command_data = json.loads(data_json)
                        if command_data.get('command_id') == record.command_id:
                            # Update the command data with latest repeat info
                            command_data['repeat_count'] = record.repeat_count
                            command_data['repeater_prefixes'] = sorted(list(record.repeater_prefixes))
                            command_data['repeater_counts'] = record.repeater_counts.copy()
                            
                            # Update the database entry
                            cursor.execute('''
                                UPDATE packet_stream
                                SET data = ?
                                WHERE id = ?
                            ''', (json.dumps(command_data), row_id))
                            
                            conn.commit()
                            self.logger.info(f"Updated command {record.command_id} in database: {record.repeat_count} repeats, prefixes: {sorted(list(record.repeater_prefixes))}")
                            
                            # Emit update event via web viewer integration
                            # The web viewer polling will pick this up, but we can also try to trigger an immediate update
                            # by inserting a new entry with updated data (the polling will see it)
                            # Actually, updating the existing entry should work - the polling will see the updated timestamp
                            # But we need to update the timestamp so the polling picks it up
                            cursor.execute('''
                                UPDATE packet_stream
                                SET timestamp = ?
                                WHERE id = ?
                            ''', (time.time(), row_id))
                            conn.commit()
                            
                            break
                    except (json.JSONDecodeError, KeyError) as e:
                        continue
                        
        except Exception as e:
            self.logger.debug(f"Error updating command in database: {e}")
    
    def get_repeat_info(self, command_id: Optional[str] = None, 
                       packet_hash: Optional[str] = None) -> Dict[str, any]:
        """Get repeat information for a command or packet hash.
        
        Args:
            command_id: Command ID to look up
            packet_hash: Packet hash to look up (alternative to command_id)
            
        Returns:
            Dict with repeat_count and repeater_prefixes
        """
        record = None
        
        if packet_hash:
            record = self.confirmed_transmissions.get(packet_hash)
        elif command_id:
            # Search for record with matching command_id
            for rec in self.confirmed_transmissions.values():
                if rec.command_id == command_id:
                    record = rec
                    break
        
        if record:
            return {
                'repeat_count': record.repeat_count,
                'repeater_prefixes': sorted(list(record.repeater_prefixes)),
                'repeater_counts': record.repeater_counts.copy()  # Include counts per repeater
            }
        
        return {'repeat_count': 0, 'repeater_prefixes': [], 'repeater_counts': {}}
    
    def extract_repeater_prefixes_from_path(self, path: Optional[str], 
                                           path_nodes: Optional[List[str]] = None) -> List[str]:
        """Extract repeater prefix from the last hop in a message path.
        
        The repeater that sent the packet is always the last hop in the path.
        We only extract the prefix from that last hop, not from intermediate nodes.
        
        Args:
            path: Path string (e.g., "01,7e,55,86")
            path_nodes: List of path nodes (alternative to path string)
            
        Returns:
            List containing the repeater prefix (2-character hex string) from the last hop,
            or empty list if no valid prefix found
        """
        # Try path_nodes first (more reliable)
        if path_nodes and len(path_nodes) > 0:
            # Get the last node in the path (the repeater that sent the packet)
            last_node = path_nodes[-1]
            if isinstance(last_node, str) and len(last_node) >= 2:
                # Take first 2 characters as prefix
                prefix = last_node[:2].lower()
                # Filter out our own prefix
                if prefix != self.bot_prefix:
                    return [prefix]
        
        # Fallback to parsing path string
        elif path:
            # Path format: "01,7e,55,86" or "01,7e,55,86 via ROUTE_TYPE_*"
            path_part = path.split(" via ")[0] if " via " in path else path
            # Remove any hop count info
            if '(' in path_part:
                path_part = path_part.split('(')[0].strip()
            
            # Split by comma and get the last part (the repeater that sent the packet)
            parts = [p.strip() for p in path_part.split(',') if p.strip()]
            if parts:
                last_part = parts[-1]
                if len(last_part) >= 2:
                    prefix = last_part[:2].lower()
                    # Filter out our own prefix
                    if prefix != self.bot_prefix:
                        return [prefix]
        
        return []  # No valid prefix found
    
    def cleanup_old_records(self):
        """Remove old transmission records that are beyond the cleanup window"""
        current_time = time.time()
        cutoff_time = current_time - self.cleanup_after
        
        # Clean up pending transmissions
        keys_to_remove = []
        for timestamp_key, records in self.pending_transmissions.items():
            # Remove records older than cutoff
            filtered_records = [r for r in records if r.timestamp > cutoff_time]
            if filtered_records:
                self.pending_transmissions[timestamp_key] = filtered_records
            else:
                keys_to_remove.append(timestamp_key)
        
        for key in keys_to_remove:
            del self.pending_transmissions[key]
        
        # Clean up confirmed transmissions (keep ones with repeats, remove others)
        hashes_to_remove = []
        for packet_hash, record in self.confirmed_transmissions.items():
            # Keep if it has repeats or is recent
            if record.repeat_count == 0 and record.timestamp < cutoff_time:
                hashes_to_remove.append(packet_hash)
        
        for hash_val in hashes_to_remove:
            del self.confirmed_transmissions[hash_val]
        
        if keys_to_remove or hashes_to_remove:
            self.logger.debug(f"Cleaned up {len(keys_to_remove)} pending transmission windows and {len(hashes_to_remove)} confirmed transmissions")
