"""Socket.IO clients for the web viewer: connect/disconnect and stream
subscription handlers, history replay to a new subscriber, logout disconnects
and stale-client cleanup."""

from __future__ import annotations

import configparser
import json
import os
import sqlite3
import time
from contextlib import closing, suppress
from typing import Any

from flask import request, session
from flask_socketio import disconnect, emit

from modules.utils import resolve_path
from modules.web_viewer.live_stream import _strip_ansi_codes


class SocketClientsMixin:
    """Mixed into BotDataViewer."""

    _clients_lock: Any
    _config_base: Any
    config: Any
    connected_clients: Any
    db_path: Any
    logger: Any
    max_clients: Any
    socketio: Any
    web_viewer_password: Any

    def _setup_socketio_handlers(self):
        """Setup SocketIO event handlers using modern patterns"""

        @self.socketio.on('connect')
        def handle_connect():
            """Handle client connection"""
            try:
                client_id = request.sid
                if not client_id:
                    self.logger.warning("Connect event received but client_id is None")
                    return False

                # Reject unauthenticated SocketIO connections when auth is enabled.
                # Live packet/message/log streams stay admin-only — they can carry
                # decrypted channel traffic and operator logs (deviation from #240).
                if self.web_viewer_password and not session.get('authenticated_admin'):
                    self.logger.warning(f"Rejected unauthenticated SocketIO connection from {client_id}")
                    with suppress(Exception):
                        disconnect()
                    return False

                self.logger.info(f"Client connected: {client_id}")

                with self._clients_lock:
                    # Check client limit
                    if len(self.connected_clients) >= self.max_clients:
                        self.logger.warning(f"Client limit reached ({self.max_clients}), rejecting connection")
                        try:
                            disconnect()
                        except Exception as e:
                            self.logger.error(f"Error disconnecting client: {e}")
                        return False

                    # Track client
                    self.connected_clients[client_id] = {
                        'admin_login_id': session.get('admin_login_id'),
                        'connected_at': time.time(),
                        'last_activity': time.time(),
                        'subscribed_commands': False,
                        'subscribed_packets': False,
                        'subscribed_messages': False,
                        'subscribed_mesh': False,
                        'subscribed_logs': False,
                    }

                    # Connection status is shown via the green indicator in the navbar, no toast needed
                    self.logger.info(f"Client {client_id} connected. Total clients: {len(self.connected_clients)}")
            except Exception as e:
                self.logger.error(f"Error in handle_connect: {e}", exc_info=True)
                return False

        @self.socketio.on('disconnect')
        def handle_disconnect(data=None):
            """Handle client disconnection"""
            try:
                # Safely get client_id - it may be None if disconnect happens during error state
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        del self.connected_clients[client_id]
                        self.logger.info(f"Client {client_id} disconnected. Total clients: {len(self.connected_clients)}")
                    elif client_id:
                        # Client disconnected but wasn't in our tracking dict (might have been cleaned up)
                        self.logger.debug(f"Client {client_id} disconnected (not in tracking dict)")
                    else:
                        # No client_id available - this can happen during error states
                        self.logger.debug("Disconnect event received but client_id is None")
            except Exception as e:
                # Don't emit errors during disconnect as the connection may be broken
                self.logger.error(f"Error in handle_disconnect: {e}", exc_info=True)

        @self.socketio.on('subscribe_commands')
        def handle_subscribe_commands():
            """Handle command stream subscription — also replays recent history to the new subscriber."""
            try:
                client_id = self._mark_subscribed('subscribed_commands')
                # Keep connection/subscription success silent; navbar indicator already shows socket state.
                self.logger.debug(f"Client {client_id} subscribed to commands")
                # Replay recent command history so the page isn't blank on load (BUG-023 fix)
                try:
                    self._replay_packet_stream("type = 'command'", lambda _type: 'command_data')
                except Exception as e:
                    self.logger.warning(f"Error replaying command history: {e}", exc_info=True)
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_commands: {e}", exc_info=True)

        @self.socketio.on('subscribe_packets')
        def handle_subscribe_packets():
            """Handle packet stream subscription — also replays recent history to the new subscriber."""
            try:
                client_id = self._mark_subscribed('subscribed_packets')
                self.logger.debug(f"Client {client_id} subscribed to packets")
                # Replay recent packet/command/routing history so the page isn't blank on load
                try:
                    self._replay_packet_stream(
                        "type IN ('packet','command','routing')",
                        lambda _type: 'command_data' if _type == 'command' else 'packet_data',
                    )
                except Exception as e:
                    self.logger.warning(f"Error replaying packet history: {e}", exc_info=True)
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_packets: {e}", exc_info=True)

        @self.socketio.on('subscribe_mesh')
        def handle_subscribe_mesh():
            """Handle mesh graph stream subscription"""
            try:
                client_id = self._mark_subscribed('subscribed_mesh')
                self.logger.debug(f"Client {client_id} subscribed to mesh graph")
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_mesh: {e}", exc_info=True)

        @self.socketio.on('subscribe_messages')
        def handle_subscribe_messages():
            """Handle live channel message stream subscription — also replays recent messages."""
            try:
                client_id = self._mark_subscribed('subscribed_messages')
                self.logger.debug(f"Client {client_id} subscribed to messages")
                # Replay recent channel messages so the page isn't blank on load
                try:
                    self._replay_packet_stream("type = 'message'", lambda _type: 'message_data')
                except Exception as e:
                    self.logger.warning(f"Error replaying message history: {e}", exc_info=True)
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_messages: {e}", exc_info=True)

        @self.socketio.on('subscribe_logs')
        def handle_subscribe_logs():
            """Handle live log stream subscription — also sends last 200 log lines to the new subscriber."""
            try:
                client_id = self._mark_subscribed('subscribed_logs')
                self.logger.debug(f"Client {client_id} subscribed to logs")
                # Send recent log history so the page isn't blank on load
                log_file = ''
                try:
                    log_file = self.config.get('Logging', 'log_file', fallback='').strip()
                    if log_file:
                        log_file = str(resolve_path(log_file, self._config_base))
                except (configparser.Error, OSError, ValueError):  # bad config or inaccessible path
                    pass
                if log_file and os.path.exists(log_file):
                    try:
                        with open(log_file, encoding='utf-8', errors='replace') as _fh:
                            recent_lines = _fh.readlines()[-200:]
                        for line in recent_lines:
                            emit('log_line', {'line': _strip_ansi_codes(line.rstrip())})
                    except Exception as e:
                        self.logger.debug(f"Error reading log history: {e}")
            except Exception as e:
                self.logger.error(f"Error in handle_subscribe_logs: {e}", exc_info=True)

        @self.socketio.on('ping')
        def handle_ping():
            """Handle client ping (modern ping/pong pattern)"""
            try:
                client_id = getattr(request, 'sid', None)
                with self._clients_lock:
                    if client_id and client_id in self.connected_clients:
                        self.connected_clients[client_id]['last_activity'] = time.time()
                emit('pong')  # Server responds with pong (Flask-SocketIO 5.x pattern)
            except Exception as e:
                self.logger.error(f"Error in handle_ping: {e}", exc_info=True)

        @self.socketio.on_error_default
        def default_error_handler(e):
            """Handle SocketIO errors gracefully"""
            try:
                self.logger.error(f"SocketIO error: {e}", exc_info=True)
                # Only emit if we have a valid request context
                if hasattr(request, 'sid') and request.sid:
                    emit('error', {'message': str(e)})
            except Exception as emit_error:
                # If we can't emit, just log it
                self.logger.error(f"Error emitting error message: {emit_error}")

    def _mark_subscribed(self, flag: str):
        """Set a stream flag on the requesting Socket.IO client; returns its sid (may be None)."""
        client_id = getattr(request, 'sid', None)
        with self._clients_lock:
            if client_id and client_id in self.connected_clients:
                self.connected_clients[client_id][flag] = True
        return client_id

    def _replay_packet_stream(self, where: str, event_for_type) -> None:
        """Emit the newest 50 packet_stream rows matching *where* to the requesting client, oldest first.

        ``event_for_type`` maps a row's type to its event name; rows whose data
        does not parse are skipped.
        """
        with closing(sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)) as _conn:
            _conn.row_factory = sqlite3.Row
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT data, type FROM packet_stream"
                f" WHERE {where}"
                " ORDER BY timestamp DESC LIMIT 50"
            )
            rows = list(reversed(_cur.fetchall()))
        for row in rows:
            try:
                data = json.loads(row['data'])
                emit(event_for_type(row['type']), data)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    def _disconnect_login_sockets(self, login_id):
        """Disconnect every Socket.IO client opened under the given admin login."""
        with self._clients_lock:
            sids = [
                sid for sid, info in self.connected_clients.items()
                if info.get('admin_login_id') == login_id
            ]
        for sid in sids:
            try:
                self.socketio.server.disconnect(sid, namespace='/')
            except Exception as e:
                self.logger.warning(f"Could not disconnect socket {sid} on logout: {e}")
            with self._clients_lock:
                self.connected_clients.pop(sid, None)
        if sids:
            self.logger.info(f"Logout disconnected {len(sids)} live socket(s)")

    def _cleanup_stale_clients(self, max_idle_seconds: int = 300):
        """Remove clients that haven't had activity in max_idle_seconds"""
        try:
            current_time = time.time()
            stale_clients = []

            with self._clients_lock:
                for client_id, client_info in self.connected_clients.items():
                    last_activity = client_info.get('last_activity', 0)
                    if current_time - last_activity > max_idle_seconds:
                        stale_clients.append(client_id)

                for client_id in stale_clients:
                    del self.connected_clients[client_id]

            if stale_clients:
                self.logger.info(f"Cleaned up {len(stale_clients)} stale client(s)")

        except Exception as e:
            self.logger.error(f"Error cleaning up stale clients: {e}")
