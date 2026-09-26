"""Web viewer page and API for the repeater telemetry feature ("Repeater-Akku").

Settings saved here go to the shared SQLite database (see
modules/repeater_telemetry_store.py); the running RepeaterTelemetryService
picks them up within a few seconds, so no config reload or restart is needed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from flask import jsonify, render_template, request

from modules import repeater_telemetry_store as store


def register_repeater_telemetry_routes(viewer: Any) -> None:
    """Attach the page and its JSON API to the BotDataViewer's Flask app."""
    app = viewer.app

    def _connection():
        return viewer.db_manager.connection()

    def _service_enabled(config) -> bool:
        return config.has_section(store.CONFIG_SECTION) and config.getboolean(
            store.CONFIG_SECTION, "enabled", fallback=False)

    def _contact_names(conn: sqlite3.Connection, roles: tuple[str, ...]) -> list[str]:
        try:
            marks = ",".join("?" * len(roles))
            rows = conn.execute(
                f"SELECT DISTINCT name FROM complete_contact_tracking WHERE role IN ({marks}) "
                f"AND name IS NOT NULL AND name != '' ORDER BY name COLLATE NOCASE LIMIT 500", roles).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error:
            return []

    @app.route('/anleitung')
    def guide_page():
        """German user guide for the bot and the web interface."""
        return render_template('anleitung.html')

    @app.route('/repeater-akku')
    def repeater_akku_page():
        """Repeater battery monitoring (German UI)."""
        return render_template('repeater_akku.html')

    @app.route('/api/repeater-telemetry')
    def api_repeater_telemetry():
        try:
            config = viewer._load_merged_config()
            with _connection() as conn:
                store.ensure_tables(conn)
                settings = store.load_settings(conn, config)
                names = [r["name"] for r in settings["repeaters"]]
                return jsonify({
                    "service_enabled": _service_enabled(config),
                    "settings": store.public_settings(settings),
                    "status": store.status_overview(conn, names),
                    "known_repeaters": _contact_names(conn, ("repeater", "roomserver")),
                    "known_contacts": _contact_names(conn, ("companion",)),
                    "limits": {"max_admins": store.MAX_ADMINS, "max_repeaters": store.MAX_REPEATERS},
                    "default_templates": store.DEFAULT_TEMPLATES,
                })
        except Exception:
            viewer.logger.exception("Error loading repeater telemetry")
            return jsonify({"error": "Interner Fehler – siehe Server-Log"}), 500

    @app.route('/api/repeater-telemetry/settings', methods=['POST'])
    def api_repeater_telemetry_save():
        data = request.get_json(silent=True) or {}
        try:
            config = viewer._load_merged_config()
            with _connection() as conn:
                store.ensure_tables(conn)
                current = store.load_settings(conn, config)
                values = store.validate_submission(data, current)
                store.save_settings(conn, values)
            viewer.logger.info("Repeater telemetry settings saved from web viewer (%s)", ", ".join(sorted(values)))
            return jsonify({"success": True})
        except store.SettingsError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception:
            viewer.logger.exception("Error saving repeater telemetry settings")
            return jsonify({"success": False, "error": "Interner Fehler – siehe Server-Log"}), 500

    @app.route('/api/repeater-telemetry/poll', methods=['POST'])
    def api_repeater_telemetry_poll():
        try:
            with _connection() as conn:
                store.request_poll(conn)
            return jsonify({"success": True})
        except Exception:
            viewer.logger.exception("Error requesting repeater poll")
            return jsonify({"success": False, "error": "Interner Fehler – siehe Server-Log"}), 500

    @app.route('/api/repeater-telemetry/reset', methods=['POST'])
    def api_repeater_telemetry_reset():
        try:
            with _connection() as conn:
                store.reset_web_settings(conn)
            return jsonify({"success": True})
        except Exception:
            viewer.logger.exception("Error resetting repeater telemetry settings")
            return jsonify({"success": False, "error": "Interner Fehler – siehe Server-Log"}), 500

    @app.route('/api/repeater-telemetry/history')
    def api_repeater_telemetry_history():
        name = (request.args.get('name') or '').strip()
        try:
            days = max(1, min(365, int(request.args.get('days', 7))))
        except ValueError:
            days = 7
        if not name:
            return jsonify({"error": "name fehlt"}), 400
        try:
            with _connection() as conn:
                store.ensure_tables(conn)
                return jsonify({"name": name, "days": days, "samples": store.history(conn, name, days)})
        except Exception:
            viewer.logger.exception("Error loading repeater telemetry history")
            return jsonify({"error": "Interner Fehler – siehe Server-Log"}), 500
