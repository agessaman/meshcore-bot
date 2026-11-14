"""Shared queries over complete_contact_tracking.

Rows only: selection and tie-breaking stay with each caller. Database errors
follow ``DBManager.execute_query``, which logs them and returns no rows.
"""

from __future__ import annotations

from typing import Any, Optional


def unique_recent_repeater_key(db_manager: Any, prefix: str, recency_days: int) -> tuple[int, Optional[str]]:
    """``(count, public_key)`` for repeaters/room servers whose key starts with *prefix*.

    ``count`` is how many distinct such nodes were heard within *recency_days*.
    ``public_key`` is set only when exactly one was, since only then is the
    prefix known to name that node (starred first, then most recently heard).
    """
    prefix_pattern = f"{prefix}%"
    count_query = f"""
        SELECT COUNT(DISTINCT public_key) as count
        FROM complete_contact_tracking
        WHERE public_key LIKE ?
        AND role IN ('repeater', 'roomserver')
        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
    """
    count_results = db_manager.execute_query(count_query, (prefix_pattern,))
    count = count_results[0].get("count", 0) if count_results else 0
    if count != 1:
        return count, None
    query = f"""
        SELECT public_key
        FROM complete_contact_tracking
        WHERE public_key LIKE ?
        AND role IN ('repeater', 'roomserver')
        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
        LIMIT 1
    """
    results = db_manager.execute_query(query, (prefix_pattern,))
    if results and results[0].get("public_key"):
        return count, results[0]["public_key"]
    return count, None
