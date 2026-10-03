#!/usr/bin/env python3
"""
Transmission tracker for monitoring message transmission success
Tracks transmitted message hashes and detects repeats from neighboring repeaters
"""

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .utils import resolve_path

# MeshCore payload types a transmission can be recognized by
_PAYLOAD_TXT_MSG = 0x02
_PAYLOAD_GRP_TXT = 0x05
_PAYLOAD_TRACE = 0x09



def own_public_key(bot: Any) -> Optional[str]:
    """The radio's public key as lowercase hex, or None before it is known.

    meshcore_py keeps it in ``meshcore.self_info``; ``meshcore.device`` is read
    as a fallback for objects that provide one.
    """
    meshcore = getattr(bot, "meshcore", None)
    if not meshcore:
        return None
    try:
        self_info = getattr(meshcore, "self_info", None)
        key = self_info.get("public_key") if isinstance(self_info, dict) else None
        if not key:
            device = getattr(meshcore, "device", None)
            key = getattr(device, "public_key", None) if device is not None else None
    except Exception:
        return None
    if isinstance(key, (bytes, bytearray)):
        key = bytes(key).hex()
    return key.lower() if isinstance(key, str) and key else None

@dataclass
class TransmissionRecord:
    """Record of a transmitted message"""
    timestamp: float
    content: str
    target: str  # Channel name or recipient ID
    message_type: str  # 'channel' or 'dm'
    packet_hash: Optional[str] = None
    repeat_count: int = 0
    repeater_prefixes: set[str] = field(default_factory=set)
    repeater_counts: dict[str, int] = field(default_factory=dict)  # Count per repeater prefix
    command_id: Optional[str] = None  # For correlating with command data
    # What identifies this transmission's packet when it is heard again: the
    # channel index for a channel message, the recipient's key for a DM, the
    # tag for a trace. A record without one is never matched to a packet.
    channel_idx: Optional[int] = None
    recipient_key: Optional[str] = None
    trace_tag: Optional[int] = None


class TransmissionTracker:
    """Tracks transmitted messages and detects repeats from neighboring repeaters"""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

        # Store pending transmissions (by timestamp window)
        # Key: approximate timestamp (rounded to nearest second)
        # Value: List of TransmissionRecord
        self.pending_transmissions: dict[int, list[TransmissionRecord]] = {}

        # Store confirmed transmissions with hashes
        # Key: packet_hash
        # Value: TransmissionRecord
        self.confirmed_transmissions: dict[str, TransmissionRecord] = {}

        # Time window for matching transmissions (seconds)
        self.match_window = 30  # Match RF data to transmissions within 30 seconds

        # Cleanup old records after this time (seconds)
        self.cleanup_after = 300  # 5 minutes
        # Records that collected repeats are held longer, so a late repeat still
        # lands on the same record — but they must expire too. Repeat counts are
        # already persisted to packet_stream, so nothing is lost, and keeping
        # them forever grew memory for the whole process lifetime.
        self.repeat_cleanup_after = 1800  # 30 minutes
        self._cleanup_interval = 60  # Run cleanup check every 60 seconds
        self._last_cleanup_time = 0.0

        # Lock protects record mutations (repeat_count, repeater_prefixes, etc.)
        self._lock = threading.Lock()

        # One worker keeps repeat-count writes in order and off the event loop
        self._db_executor: Optional[ThreadPoolExecutor] = None
        self._db_closed = False

        # Our radio's public key prefix, for filtering. Read lazily: the tracker is
        # created before the radio connects and reports its key.
        self._bot_prefix: Optional[str] = None
        self._update_bot_prefix()

    @property
    def bot_prefix(self) -> Optional[str]:
        """Our radio's public key prefix (``prefix_hex_chars`` long), or None before it is known."""
        if self._bot_prefix is None:
            self._update_bot_prefix()
        return self._bot_prefix

    @bot_prefix.setter
    def bot_prefix(self, value: Optional[str]) -> None:
        self._bot_prefix = value

    def _update_bot_prefix(self):
        """Update bot prefix from the radio's public key"""
        key = own_public_key(self.bot)
        if key and len(key) >= 2:
            self._bot_prefix = key[:self.bot.prefix_hex_chars].lower()
            self.logger.debug(f"Bot prefix set to: {self._bot_prefix}")

    def record_transmission(self, content: str, target: str, message_type: str,
                          command_id: Optional[str] = None, *,
                          channel_idx: Optional[int] = None,
                          recipient_key: Optional[str] = None,
                          trace_tag: Optional[int] = None) -> TransmissionRecord:
        """Record a transmission attempt.

        Args:
            content: Message content
            target: Channel name or recipient ID
            message_type: 'channel', 'dm' or 'trace'
            command_id: Optional command ID for correlation
            channel_idx: Channel index a channel message was sent on
            recipient_key: Recipient public key (hex) of a DM
            trace_tag: Tag of a trace

        Returns:
            TransmissionRecord: The created record
        """
        record = TransmissionRecord(
            timestamp=time.time(),
            content=content,
            target=target,
            message_type=message_type,
            command_id=command_id,
            channel_idx=channel_idx,
            recipient_key=recipient_key,
            trace_tag=trace_tag,
        )

        # Store in pending transmissions (by rounded timestamp)
        timestamp_key = int(record.timestamp)
        if timestamp_key not in self.pending_transmissions:
            self.pending_transmissions[timestamp_key] = []
        self.pending_transmissions[timestamp_key].append(record)

        self.logger.debug(f"Recorded transmission: {message_type} to {target} at {record.timestamp}")

        # Periodically clean up old records to prevent unbounded memory growth
        self._maybe_cleanup()

        return record

    def match_packet_hash(self, packet_hash: str, rf_timestamp: float,
                          packet: Optional[dict[str, Any]] = None) -> Optional[TransmissionRecord]:
        """Match a received packet hash to a pending transmission.

        A pending transmission is claimed only by a packet that carries its
        identity (see ``_packet_is_ours``); without ``packet`` only hashes that
        were already matched are found.

        Args:
            packet_hash: Packet hash from received RF data
            rf_timestamp: Timestamp when RF data was received
            packet: What the packet says about itself: ``payload_type`` and
                ``payload_hex``, plus ``channel_idx``, ``channel_message`` (the full
                decrypted text) and ``channel_text`` (without the sender name) for
                a channel message that decrypted with one of our channel keys

        Returns:
            TransmissionRecord if matched, None otherwise
        """
        if not packet_hash or packet_hash == "0000000000000000":
            return None

        # Check if we already have this hash confirmed
        if packet_hash in self.confirmed_transmissions:
            return self.confirmed_transmissions[packet_hash]

        if packet is None:
            return None

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
                    if record.packet_hash is None and self._packet_is_ours(record, packet):
                        record.packet_hash = packet_hash
                        # Move to confirmed transmissions
                        self.confirmed_transmissions[packet_hash] = record
                        self.logger.debug(f"Matched transmission hash {packet_hash} to {record.message_type} to {record.target}")
                        return record

        return None

    def _own_name(self) -> Optional[str]:
        """The radio's advertised name, or None before it is known."""
        self_info = getattr(self.bot.meshcore, "self_info", None) if self.bot.meshcore else None
        name = self_info.get("name") if isinstance(self_info, dict) else None
        return name if isinstance(name, str) and name else None

    def _own_key_byte(self) -> Optional[str]:
        """First byte (hex) of the radio's public key, or None before it is known."""
        key = own_public_key(self.bot)
        return key[:2] if key and len(key) >= 2 else None

    def _packet_is_ours(self, record: TransmissionRecord, packet: Optional[dict[str, Any]]) -> bool:
        """Whether ``packet`` is this transmission, judged by what both carry.

        A channel message must decrypt on the same channel to our name and the
        same text; a DM
        must be a TXT_MSG addressed to the recipient's hash, from ours when known;
        a trace must carry the same tag.
        """
        if not packet:
            return False
        payload_type = packet.get("payload_type")
        payload_hex = packet.get("payload_hex") or ""
        if record.message_type == "channel":
            if (
                payload_type != _PAYLOAD_GRP_TXT
                or record.channel_idx is None
                or packet.get("channel_idx") != record.channel_idx
            ):
                return False
            # The radio sends "<its name>: <text>"; without the name, only the text can be compared.
            name = self._own_name()
            if name:
                return packet.get("channel_message") == f"{name}: {record.content}"
            return packet.get("channel_text") == record.content
        if record.message_type == "dm":
            if payload_type != _PAYLOAD_TXT_MSG or not record.recipient_key or len(payload_hex) < 4:
                return False
            if payload_hex[:2].lower() != record.recipient_key[:2].lower():
                return False
            own = self._own_key_byte()
            return own is None or payload_hex[2:4].lower() == own
        if record.message_type == "trace":
            if payload_type != _PAYLOAD_TRACE or record.trace_tag is None or len(payload_hex) < 8:
                return False
            try:
                return int.from_bytes(bytes.fromhex(payload_hex[:8]), "little") == record.trace_tag
            except ValueError:
                return False
        return False

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
            with self._lock:
                record.repeat_count += 1
                if repeater_prefix:
                    record.repeater_prefixes.add(repeater_prefix)
                    # Track count per repeater
                    record.repeater_counts[repeater_prefix] = record.repeater_counts.get(repeater_prefix, 0) + 1
                else:
                    # No prefix but still a repeat (heard by radio)
                    record.repeater_counts['_unknown'] = record.repeater_counts.get('_unknown', 0) + 1

                repeat_count = record.repeat_count
                unique_repeaters = len(record.repeater_prefixes)
                prefixes = sorted(record.repeater_prefixes)

            self.logger.info(f"📡 Recorded repeat for hash {packet_hash}: {repeat_count} repeats, {unique_repeaters} unique repeaters, prefixes: {prefixes}")

            # Update the database entry if we have a command_id
            if record.command_id and hasattr(self.bot, 'web_viewer_integration'):
                self._schedule_command_update(record)

            return True

        return False

    def _schedule_command_update(self, record: TransmissionRecord) -> None:
        """Write the record's repeat counts to the database, off the event loop when one is running."""
        with self._lock:
            snapshot = TransmissionRecord(
                timestamp=record.timestamp,
                content=record.content,
                target=record.target,
                message_type=record.message_type,
                packet_hash=record.packet_hash,
                repeat_count=record.repeat_count,
                repeater_prefixes=set(record.repeater_prefixes),
                repeater_counts=dict(record.repeater_counts),
                command_id=record.command_id,
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._update_command_in_database(snapshot)
            return
        if self._db_closed:
            self._update_command_in_database(snapshot)
            return
        if self._db_executor is None:
            self._db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tx-tracker-db")
        self._db_executor.submit(self._update_command_in_database, snapshot)

    def close(self) -> None:
        """Wait for queued repeat-count writes; later writes run inline."""
        self._db_closed = True
        if self._db_executor is not None:
            self._db_executor.shutdown(wait=True)
            self._db_executor = None

    def _update_command_in_database(self, record: TransmissionRecord):
        """Update command entry in database with latest repeat information"""
        try:
            if not record.command_id:
                return

            # Get database path (use [Bot] db_path when [Web_Viewer] db_path is unset)
            base_dir = self.bot.bot_root if hasattr(self.bot, 'bot_root') else '.'
            if (self.bot.config.has_section('Web_Viewer') and self.bot.config.has_option('Web_Viewer', 'db_path')
                    and self.bot.config.get('Web_Viewer', 'db_path', fallback='').strip()):
                db_path = resolve_path(self.bot.config.get('Web_Viewer', 'db_path').strip(), base_dir)
            else:
                db_path = str(Path(self.bot.db_manager.db_path).resolve())

            with closing(sqlite3.connect(str(db_path), timeout=30.0)) as conn:
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
                            command_data['repeater_prefixes'] = sorted(record.repeater_prefixes)
                            command_data['repeater_counts'] = record.repeater_counts.copy()

                            # Update the database entry
                            cursor.execute('''
                                UPDATE packet_stream
                                SET data = ?
                                WHERE id = ?
                            ''', (json.dumps(command_data), row_id))

                            conn.commit()
                            self.logger.info(f"Updated command {record.command_id} in database: {record.repeat_count} repeats, prefixes: {sorted(record.repeater_prefixes)}")

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
                    except (json.JSONDecodeError, KeyError):
                        continue

        except Exception as e:
            self.logger.debug(f"Error updating command in database: {e}")

    def get_repeat_info(self, command_id: Optional[str] = None,
                       packet_hash: Optional[str] = None) -> dict[str, Any]:
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
                'repeater_prefixes': sorted(record.repeater_prefixes),
                'repeater_counts': record.repeater_counts.copy()  # Include counts per repeater
            }

        return {'repeat_count': 0, 'repeater_prefixes': [], 'repeater_counts': {}}

    def extract_repeater_prefixes_from_path(self, path: Optional[str],
                                           path_nodes: Optional[list[str]] = None) -> list[str]:
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
                prefix = last_node[:self.bot.prefix_hex_chars].lower()
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
                    prefix = last_part[:self.bot.prefix_hex_chars].lower()
                    # Filter out our own prefix
                    if prefix != self.bot_prefix:
                        return [prefix]

        return []  # No valid prefix found

    def _maybe_cleanup(self) -> None:
        """Run cleanup if enough time has passed since the last run."""
        now = time.time()
        if now - self._last_cleanup_time >= self._cleanup_interval:
            self._last_cleanup_time = now
            self.cleanup_old_records()

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

        # Clean up confirmed transmissions. Repeated records get a longer grace
        # period than plain ones, but both eventually age out.
        repeat_cutoff_time = current_time - self.repeat_cleanup_after
        hashes_to_remove = []
        for packet_hash, record in self.confirmed_transmissions.items():
            expiry = repeat_cutoff_time if record.repeat_count > 0 else cutoff_time
            if record.timestamp < expiry:
                hashes_to_remove.append(packet_hash)

        for hash_val in hashes_to_remove:
            del self.confirmed_transmissions[hash_val]

        if keys_to_remove or hashes_to_remove:
            self.logger.debug(f"Cleaned up {len(keys_to_remove)} pending transmission windows and {len(hashes_to_remove)} confirmed transmissions")
