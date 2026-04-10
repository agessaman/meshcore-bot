#!/usr/bin/env python3
"""
Check-in API receiver — stdlib-only HTTP server for the meshcore-bot Check-in API.

Implements the contract in docs/checkin-api.md: POST JSON with Bearer auth,
upsert into SQLite by packet_hash. Run behind nginx with TLS.

Environment:
  CHECKIN_API_SECRET  Required. Bearer token; must match bot [CheckIn] api_key.
  CHECKIN_PORT       Port to bind (default 9999).
  CHECKIN_DB_PATH    SQLite file path (default ./checkins.db). Parent dir created if missing.
"""

import json
import logging
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

REQUIRED_FIELDS = ("packet_hash", "username", "message", "channel", "timestamp")
DEFAULT_PORT = 9999
DEFAULT_DB = "checkins.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkins (
    packet_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    message TEXT NOT NULL,
    channel TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    source_bot TEXT,
    updated_at TEXT NOT NULL
);
"""


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def init_db(db_path: str) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def upsert_checkin(db_path: str, data: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO checkins (
                packet_hash, username, message, channel, timestamp, source_bot, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(packet_hash) DO UPDATE SET
                username = excluded.username,
                message = excluded.message,
                channel = excluded.channel,
                timestamp = excluded.timestamp,
                source_bot = excluded.source_bot,
                updated_at = excluded.updated_at
            """,
            (
                data["packet_hash"],
                data["username"],
                data["message"],
                data["channel"],
                data["timestamp"],
                data.get("source_bot") or "",
                now,
            ),
        )
        conn.commit()


class CheckinHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _secret(self) -> str:
        return get_env("CHECKIN_API_SECRET")

    def _db_path(self) -> str:
        return get_env("CHECKIN_DB_PATH") or DEFAULT_DB

    def _send(self, code: int, body: str = "", content_type: str = "application/json") -> None:
        self.send_response(code)
        if body:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def _bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            n = int(length, 10)
        except ValueError:
            return b""
        if n <= 0 or n > 4096:
            return b""
        return self.rfile.read(n)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/checkins"):
            self._send(200, '{"status":"ok"}')
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/" and parsed.path != "/checkins":
            self._send(404, '{"error":"not found"}')
            return

        secret = self._secret()
        if not secret:
            self._send(500, '{"error":"server misconfiguration: CHECKIN_API_SECRET not set"}')
            return

        token = self._bearer_token()
        if token is None or not secrets.compare_digest(secret, token):
            self._send(401, '{"error":"unauthorized"}')
            return

        raw = self._read_body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logging.warning("CheckinReceiver: invalid JSON: %s", e)
            self._send(400, '{"error":"invalid json"}')
            return

        if not isinstance(data, dict):
            self._send(400, '{"error":"body must be a json object"}')
            return

        missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
        if missing:
            self._send(400, json.dumps({"error": "missing fields", "fields": missing}))
            return

        db_path = self._db_path()
        try:
            init_db(db_path)
            upsert_checkin(db_path, data)
        except Exception as e:
            logging.exception("CheckinReceiver: db error: %s", e)
            self._send(500, '{"error":"internal error"}')
            return

        self._send(201, "{}")

    def log_message(self, format: str, *args: object) -> None:
        logging.info("%s - %s", self.address_string(), format % args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    port = DEFAULT_PORT
    try:
        p = get_env("CHECKIN_PORT")
        if p:
            port = int(p, 10)
    except ValueError:
        pass

    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, CheckinHandler)
    logging.info("CheckinReceiver listening on http://%s:%s", server_address[0], server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
