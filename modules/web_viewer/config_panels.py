"""Config panel registry for the web viewer."""

from __future__ import annotations

from typing import TypedDict


class ConfigPanel(TypedDict):
    id: str
    title: str
    category: str
    order: int
    icon: str
    template: str


PANEL_CATEGORIES: list[tuple[str, str]] = [
    ("core", "Core"),
    ("database", "Database"),
]


CONFIG_PANELS: list[ConfigPanel] = [
    {
        "id": "notifications",
        "title": "Email & Notifications",
        "category": "core",
        "order": 10,
        "icon": "fas fa-envelope",
        "template": "config/panels/notifications.html",
    },
    {
        "id": "log-rotation",
        "title": "Log Rotation",
        "category": "core",
        "order": 20,
        "icon": "fas fa-sync-alt",
        "template": "config/panels/log_rotation.html",
    },
    {
        "id": "maintenance-status",
        "title": "Maintenance Status",
        "category": "core",
        "order": 30,
        "icon": "fas fa-tasks",
        "template": "config/panels/maintenance_status.html",
    },
    {
        "id": "database",
        "title": "Database Information",
        "category": "database",
        "order": 40,
        "icon": "fas fa-table",
        "template": "config/panels/database_info.html",
    },
    {
        "id": "db-backup",
        "title": "Database Backup",
        "category": "database",
        "order": 50,
        "icon": "fas fa-database",
        "template": "config/panels/db_backup.html",
    },
]
