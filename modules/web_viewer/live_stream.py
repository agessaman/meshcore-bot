"""Live updates for the web viewer: SocketIO broadcasts to subscribed clients,
the bot log tail, and the database poll that feeds the live command, packet and
message streams."""

from __future__ import annotations

import os
import re
from contextlib import closing
from pathlib import Path
from typing import Any

from modules.utils import resolve_path

# colorlog and other handlers write ANSI SGR sequences; strip for web /logs display
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi_codes(text: str) -> str:
    """Remove ANSI color and reset codes from log lines for SocketIO web clients."""
    return _ANSI_ESCAPE_RE.sub("", text)


class LiveStreamMixin:
    """Mixed into BotDataViewer."""

    _clients_lock: Any
    _config_base: Any
    config: Any
    connected_clients: Any
    db_path: Any
    logger: Any
    socketio: Any

    def _subscribed_clients(self, flag: str) -> list:
        """Sids of connected clients with ``flag`` set, read under the clients lock."""
        with self._clients_lock:
            return [
                client_id for client_id, client_info in self.connected_clients.items()
                if client_info.get(flag, False)
            ]

    def _handle_command_data(self, command_data):
        """Handle incoming command data from bot"""
        try:
            # Broadcast to subscribed clients
            subscribed_clients = self._subscribed_clients('subscribed_commands')

            if subscribed_clients:
                self.socketio.emit('command_data', command_data, room=None)
                self.logger.debug(f"Broadcasted command data to {len(subscribed_clients)} clients")
        except Exception as e:
            self.logger.error(f"Error handling command data: {e}")

    def _handle_packet_data(self, packet_data):
        """Handle incoming packet data from bot"""
        try:
            # Broadcast to subscribed clients
            subscribed_clients = self._subscribed_clients('subscribed_packets')

            if subscribed_clients:
                self.socketio.emit('packet_data', packet_data, room=None)
                self.logger.debug(f"Broadcasted packet data to {len(subscribed_clients)} clients")
        except Exception as e:
            self.logger.error(f"Error handling packet data: {e}")

    def _handle_mesh_edge_data(self, edge_data):
        """Handle incoming mesh edge data from bot"""
        try:
            # Broadcast to subscribed clients
            subscribed_clients = self._subscribed_clients('subscribed_mesh')

            if subscribed_clients:
                event_type = 'mesh_edge_added' if edge_data.get('is_new', False) else 'mesh_edge_updated'
                self.socketio.emit(event_type, edge_data, room=None)
        except Exception as e:
            self.logger.error(f"Error handling mesh edge data: {e}", exc_info=True)

    def _handle_mesh_node_data(self, node_data):
        """Handle incoming mesh node data from bot"""
        try:
            # Broadcast to subscribed clients
            subscribed_clients = self._subscribed_clients('subscribed_mesh')

            if subscribed_clients:
                self.socketio.emit('mesh_node_added', node_data, room=None)
        except Exception as e:
            self.logger.error(f"Error handling mesh node data: {e}", exc_info=True)

    def _handle_message_data(self, msg_data):
        """Broadcast a captured channel message to subscribed clients."""
        try:
            subscribed_clients = self._subscribed_clients('subscribed_messages')
            if subscribed_clients:
                self.socketio.emit('message_data', msg_data, room=None)
        except Exception as e:
            self.logger.error(f"Error handling message data: {e}")

    def _handle_log_line(self, line: str) -> None:
        """Broadcast a log line to clients subscribed to the log stream."""
        try:
            subscribed = self._subscribed_clients('subscribed_logs')
            if subscribed:
                self.socketio.emit(
                    'log_line', {'line': _strip_ansi_codes(line.rstrip())}, room=None
                )
        except Exception as e:
            self.logger.error(f"Error broadcasting log line: {e}")

    def _start_log_tailing(self) -> None:
        """Start a background thread that tails the bot log file and emits SocketIO events."""
        import os
        import threading

        log_file = ''
        try:
            log_file = self.config.get('Logging', 'log_file', fallback='').strip()
            if log_file:
                log_file = str(resolve_path(log_file, self._config_base))
        except Exception:
            pass

        if not log_file:
            self.logger.info("Log tailing disabled: no log_file configured")
            return

        def tail_log():
            import time as _time
            self.logger.info(f"Log tail thread started: {log_file}")
            pos = 0
            # Start at end of file so we only stream new lines
            try:
                pos = os.path.getsize(log_file)
            except OSError:
                pass
            while True:
                try:
                    if not os.path.exists(log_file):
                        _time.sleep(2)
                        continue
                    current_size = os.path.getsize(log_file)
                    if current_size < pos:
                        # File rotated — start from beginning
                        pos = 0
                    if current_size > pos:
                        with open(log_file, encoding='utf-8', errors='replace') as fh:
                            fh.seek(pos)
                            for line in fh:
                                self._handle_log_line(line)
                            pos = fh.tell()
                except Exception as e:
                    self.logger.debug(f"Log tail error: {e}")
                _time.sleep(1)

        tail_thread = threading.Thread(target=tail_log, daemon=True)
        tail_thread.start()
        self.logger.info("Log tailing started")

    def _start_database_polling(self):
        """Start background thread to poll database for new data"""
        import threading

        def poll_database():
            import time as _time
            last_timestamp = _time.time() - 300  # start 5 min back; subscribe handlers replay full history
            consecutive_errors = 0
            max_consecutive_errors = 10

            while True:
                try:
                    import json
                    import sqlite3
                    import time

                    # Subscription handlers replay recent history themselves.
                    # With no live command/packet/message subscribers there is
                    # nothing to broadcast, so avoid opening SQLite and decoding
                    # every packet-stream row merely to discard it.
                    if not self._has_live_stream_subscribers():
                        last_timestamp = time.time()
                        # An idle period is not a failure. Without this, an
                        # error burst before the last subscriber left would
                        # still be counted against the first poll after the
                        # next one arrives, mis-escalating its log level and
                        # backoff.
                        consecutive_errors = 0
                        time.sleep(2.0)
                        continue

                    # Check if database file exists and is accessible
                    db_file = Path(self.db_path)
                    if not db_file.exists():
                        consecutive_errors += 1
                        if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                            self.logger.warning(f"Database file does not exist: {self.db_path}")
                        time.sleep(5)
                        continue

                    if not os.access(self.db_path, os.R_OK):
                        consecutive_errors += 1
                        if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                            self.logger.warning(f"Database file is not readable: {self.db_path}")
                        time.sleep(5)
                        continue

                    # Connect to database with timeout to prevent hanging
                    try:
                        with closing(sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)) as conn:
                            conn.row_factory = sqlite3.Row
                            cursor = conn.cursor()

                            # Get new data since last poll
                            cursor.execute('''
                                SELECT timestamp, data, type FROM packet_stream
                                WHERE timestamp > ?
                                ORDER BY timestamp ASC
                            ''', (last_timestamp,))

                            rows = cursor.fetchall()

                            # Process new data
                            for row in rows:
                                try:
                                    data_json = row[1]
                                    data_type = row[2]
                                    data = json.loads(data_json)

                                    # Broadcast based on type
                                    if data_type == 'command':
                                        self._handle_command_data(data)
                                    elif data_type == 'packet':
                                        self._handle_packet_data(data)
                                    elif data_type == 'routing':
                                        self._handle_packet_data(data)  # Treat routing as packet data
                                    elif data_type == 'message':
                                        self._handle_message_data(data)

                                except Exception as e:
                                    self.logger.warning(f"Error processing database data: {e}")

                            # Update last timestamp
                            if rows:
                                last_timestamp = rows[-1][0]

                            # Reset error counter on success
                            consecutive_errors = 0
                    except sqlite3.OperationalError as conn_error:
                        error_msg = str(conn_error)
                        if "locked" in error_msg.lower() or "database is locked" in error_msg.lower():
                            consecutive_errors += 1
                            if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                                self.logger.warning(f"Database is locked, waiting: {self.db_path}")
                            time.sleep(2)
                            continue
                        raise  # Re-raise non-locked OperationalErrors for outer handler to log/backoff

                    # Sleep before next poll (back off to reduce lock contention with bot writes)
                    time.sleep(2.0)  # Poll every 2s

                except sqlite3.OperationalError as e:
                    consecutive_errors += 1
                    error_msg = str(e)

                    # Provide more diagnostic information on first error or periodic errors
                    if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                        db_file = Path(self.db_path)
                        exists = db_file.exists()
                        readable = os.access(self.db_path, os.R_OK) if exists else False
                        writable = os.access(self.db_path, os.W_OK) if exists else False
                        self.logger.error(
                            f"Database polling error (attempt {consecutive_errors}): {error_msg}\n"
                            f"  Path: {self.db_path}\n"
                            f"  Exists: {exists}\n"
                            f"  Readable: {readable}\n"
                            f"  Writable: {writable}"
                        )

                    # Log at appropriate level based on error frequency
                    if consecutive_errors >= max_consecutive_errors:
                        if consecutive_errors == max_consecutive_errors:
                            self.logger.error(f"Database polling persistent error (attempt {consecutive_errors}): {error_msg}")
                        # Exponential backoff for persistent errors
                        time.sleep(min(60, 2 ** min(consecutive_errors - max_consecutive_errors, 5)))
                    elif consecutive_errors > 3:
                        self.logger.warning(f"Database polling error (attempt {consecutive_errors}): {error_msg}")
                        time.sleep(5)  # Wait longer on repeated errors
                    else:
                        self.logger.debug(f"Database polling error (attempt {consecutive_errors}): {error_msg}")
                        time.sleep(1)  # Wait longer on error

                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        if consecutive_errors == max_consecutive_errors:
                            self.logger.error(f"Database polling unexpected error (attempt {consecutive_errors}): {e}", exc_info=True)
                        time.sleep(min(60, 2 ** min(consecutive_errors - max_consecutive_errors, 5)))
                    else:
                        self.logger.warning(f"Database polling unexpected error (attempt {consecutive_errors}): {e}")
                        time.sleep(2)


        # Start polling thread
        polling_thread = threading.Thread(target=poll_database, daemon=True)
        polling_thread.start()
        self.logger.info("Database polling started")

    def _has_live_stream_subscribers(self) -> bool:
        """Return whether any client consumes a DB-backed live stream."""
        subscription_keys = (
            'subscribed_commands',
            'subscribed_packets',
            'subscribed_messages',
        )
        with self._clients_lock:
            return any(
                any(client.get(key, False) for key in subscription_keys)
                for client in self.connected_clients.values()
            )
