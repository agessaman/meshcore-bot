"""contacts_repo.unique_recent_repeater_key against a real tracking table."""

from unittest.mock import Mock

import pytest

from modules.contacts_repo import unique_recent_repeater_key
from modules.db_manager import DBManager


@pytest.fixture
def db(tmp_path):
    bot = Mock()
    bot.logger = Mock()
    manager = DBManager(bot, str(tmp_path / "contacts.db"))
    rows = [
        ("aa11" + "0" * 60, "repeater", "-1 days", 0),
        ("bb22" + "0" * 60, "repeater", "-1 days", 0),
        ("bb23" + "0" * 60, "roomserver", "-2 days", 1),
        ("cc33" + "0" * 60, "repeater", "-40 days", 0),
        ("dd44" + "0" * 60, "companion", "-1 days", 0),
    ]
    for key, role, age, starred in rows:
        manager.execute_update(
            "INSERT INTO complete_contact_tracking (public_key, name, role, last_heard, is_starred) "
            f"VALUES (?, ?, ?, datetime('now', '{age}'), ?)",
            (key, key[:4], role, starred),
        )
    return manager


@pytest.mark.parametrize(
    ("prefix", "expected_count", "expected_key"),
    [
        ("aa", 1, "aa11" + "0" * 60),
        ("bb", 2, None),  # collision: never guess
        ("bb23", 1, "bb23" + "0" * 60),
        ("cc", 0, None),  # outside the recency window
        ("dd", 0, None),  # not a repeater or room server
        ("ee", 0, None),
    ],
)
def test_unique_recent_repeater_key(db, prefix, expected_count, expected_key):
    assert unique_recent_repeater_key(db, prefix, 7) == (expected_count, expected_key)


def test_database_errors_mean_no_match():
    db = Mock()
    db.execute_query.return_value = []
    assert unique_recent_repeater_key(db, "aa", 7) == (0, None)
    assert db.execute_query.call_count == 1
