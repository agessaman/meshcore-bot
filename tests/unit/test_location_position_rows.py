"""The shared position queries behind the commands' sender and repeater-name lookups."""

import sqlite3
from types import SimpleNamespace

import pytest

from modules.location import latest_contact_position_rows, repeater_by_name_rows


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def execute_query(self, query, params=()):
        self.conn.row_factory = sqlite3.Row
        return [dict(row) for row in self.conn.execute(query, params).fetchall()]


@pytest.fixture
def bot():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE complete_contact_tracking (public_key TEXT, name TEXT, role TEXT,"
        " latitude REAL, longitude REAL, last_advert_timestamp TEXT, last_heard TEXT)"
    )
    rows = [
        ("pk1", "Equator", "companion", 0.0, 10.0, "2026-03-01", None),
        ("pk1", "Old", "companion", 47.0, -122.0, "2026-01-01", None),
        ("pk2", "Null Island", "companion", 0.0, 0.0, "2026-03-01", None),
        ("rp1", "Hill Alpha", "repeater", 1.0, 1.0, "2026-03-01", None),
        ("rp2", "Alpha Hill", "repeater", 2.0, 2.0, "2026-01-01", None),
        ("rp3", "alpha", "roomserver", 3.0, 3.0, "2025-01-01", None),
        ("rp4", "Alpha", "companion", 4.0, 4.0, "2026-04-01", None),
        ("rp5", "Prime", "repeater", 5.0, 0.0, "2026-04-01", None),
    ]
    conn.executemany("INSERT INTO complete_contact_tracking VALUES (?,?,?,?,?,?,?)", rows)
    return SimpleNamespace(db_manager=_DB(conn))


def test_latest_position_both_rule_keeps_a_single_zero_coordinate(bot):
    assert latest_contact_position_rows(bot, "pk1") == [{"latitude": 0.0, "longitude": 10.0}]
    assert latest_contact_position_rows(bot, "pk2") == []


def test_latest_position_either_rule_drops_a_single_zero_coordinate(bot):
    assert latest_contact_position_rows(bot, "pk1", zero_rule="either") == [{"latitude": 47.0, "longitude": -122.0}]


def test_repeater_by_name_prefers_exact_then_prefix_then_substring(bot):
    assert repeater_by_name_rows(bot, "  ALPHA ")[0]["name"] == "alpha"
    assert repeater_by_name_rows(bot, "alpha h")[0]["name"] == "Alpha Hill"
    assert repeater_by_name_rows(bot, "lpha")[0]["name"] == "Hill Alpha"


def test_repeater_by_name_zero_rules(bot):
    assert repeater_by_name_rows(bot, "Prime")[0]["name"] == "Prime"
    assert repeater_by_name_rows(bot, "Prime", zero_rule="either") == []
