#!/usr/bin/env python3
"""
Minimal Flask HTTP admin server for the MeshCore Bot.

Runs in a daemon thread alongside the bot's asyncio loop.
Configured via the ``[Admin]`` section in config.ini:

    [Admin]
    enabled = true
    port    = 5001
    token   = <secret>   ; required; requests without matching Bearer token are rejected
"""

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import MeshCoreBot


class BotAdminServer(threading.Thread):
    """Minimal Flask HTTP server exposing bot admin endpoints."""

    def __init__(self, bot: "MeshCoreBot", port: int, token: str) -> None:
        super().__init__(daemon=True, name="BotAdminServer")
        self._bot = bot
        self._port = port
        self._token = token

    def run(self) -> None:
        try:
            from flask import Flask, Response, jsonify
            from flask import request as flask_request

            app = Flask("bot_admin")
            # Suppress Flask startup banner and request logs
            import logging as _logging
            _logging.getLogger("werkzeug").setLevel(_logging.ERROR)

            def _check_auth() -> "Response | None":
                auth = flask_request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] != self._token:
                    return jsonify({"error": "unauthorized"}), 401
                return None

            @app.post("/api/admin/reload")
            def reload_config():  # type: ignore[no-untyped-def]
                denied = _check_auth()
                if denied is not None:
                    return denied
                success, msg = self._bot.reload_config()
                status = 200 if success else 409
                return jsonify({"success": success, "message": msg}), status

            @app.get("/api/admin/health")
            def health():  # type: ignore[no-untyped-def]
                denied = _check_auth()
                if denied is not None:
                    return denied
                return jsonify({"status": "ok"})

            app.run(host="127.0.0.1", port=self._port, threaded=True)
        except Exception as exc:  # noqa: BLE001
            self._bot.logger.error("BotAdminServer failed to start: %s", exc)
