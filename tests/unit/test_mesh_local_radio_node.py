"""The bot's own radio as a mesh-graph node (GitHub #386).

Every confirmed-neighbor edge has the local radio on one end, but the radio is
a companion, so it is never among the repeater contacts the node list is built
from. Covers BotDataViewer._local_radio_nodes, which fills that gap.
"""

from __future__ import annotations

import configparser
import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from modules.db_migrations import MigrationRunner
from modules.web_viewer.app import BotDataViewer

LOGGER = logging.getLogger("test-local-radio-node")

SELF_KEY = "ff" * 32
OLD_SELF_KEY = "ee" * 32
KEY_A = "aa" * 32


def ago(days: float) -> str:
    """A timestamp `days` before now, in the format record_neighbors stores."""
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return stamp.replace(microsecond=0).isoformat()


class ViewerStub:
    """The db and config plumbing _local_radio_nodes needs, bound to production code."""

    logger = LOGGER
    _local_radio_nodes = BotDataViewer._local_radio_nodes
    _local_radio_contact = staticmethod(BotDataViewer._local_radio_contact)
    _configured_bot_position = BotDataViewer._configured_bot_position

    def __init__(self, path, bot_section: dict[str, str]):
        self._path = path
        self.config = configparser.ConfigParser()
        self.config["Bot"] = bot_section

    @contextlib.contextmanager
    def connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def nodes(self, prefix_hex_chars=6, days=None):
        with self.connect() as conn:
            return self._local_radio_nodes(conn, prefix_hex_chars, days)


def insert_link(conn, self_key, neighbor_key, last_seen):
    conn.execute(
        """
        INSERT INTO neighbor_links
            (self_public_key, neighbor_public_key, first_seen, last_seen,
             observation_count, snr_sum, snr_count, best_snr, last_snr,
             last_status, scopes)
        VALUES (?, ?, ?, ?, 1, 5.0, 1, 5.0, 5.0, 'responded', '')
        """,
        (self_key, neighbor_key, last_seen, last_seen),
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "viewer.db"
    conn = sqlite3.connect(path)
    MigrationRunner(conn, LOGGER).run()
    conn.close()
    return path


def make_viewer(db_path, **bot):
    return ViewerStub(db_path, {"bot_name": "TestBot", **bot})


def seed(viewer, *links):
    with viewer.connect() as conn:
        for self_key, neighbor_key, last_seen in links:
            insert_link(conn, self_key, neighbor_key, last_seen)
        conn.commit()


def test_no_discovery_means_no_local_node(db_path):
    assert make_viewer(db_path).nodes() == []


def test_local_node_uses_configured_name_and_position(db_path):
    viewer = make_viewer(db_path, bot_latitude="47.5", bot_longitude="-122.3")
    seed(viewer, (SELF_KEY, KEY_A, ago(1)))

    [node] = viewer.nodes(prefix_hex_chars=4)

    assert node["public_key"] == SELF_KEY
    assert node["prefix"] == SELF_KEY[:4]
    assert node["name"] == "TestBot"
    assert (node["latitude"], node["longitude"]) == (47.5, -122.3)
    assert node["role"] == "local"
    assert node["is_local"] is True
    assert node["last_heard"] is not None


def test_local_node_without_position_has_null_coordinates(db_path):
    """The graph view can still place it; the map skips it."""
    viewer = make_viewer(db_path)
    seed(viewer, (SELF_KEY, KEY_A, ago(1)))

    [node] = viewer.nodes()

    assert node["latitude"] is None
    assert node["longitude"] is None


def test_zero_configured_position_counts_as_unset(db_path):
    viewer = make_viewer(db_path, bot_latitude="0", bot_longitude="0")
    seed(viewer, (SELF_KEY, KEY_A, ago(1)))

    [node] = viewer.nodes()

    assert node["latitude"] is None


def test_own_contact_row_wins_over_config(db_path):
    viewer = make_viewer(db_path, bot_latitude="47.5", bot_longitude="-122.3")
    seed(viewer, (SELF_KEY, KEY_A, ago(1)))
    with viewer.connect() as conn:
        conn.execute(
            """
            INSERT INTO complete_contact_tracking
                (public_key, name, role, latitude, longitude)
            VALUES (?, 'Advertised Name', 'companion', 45.1, -120.2)
            """,
            (SELF_KEY.upper(),),
        )
        conn.commit()

    [node] = viewer.nodes()

    assert node["name"] == "Advertised Name"
    assert (node["latitude"], node["longitude"]) == (45.1, -120.2)


def test_replaced_radio_keeps_its_links_but_not_the_current_identity(db_path):
    """Only the most recent key is this station as configured now."""
    viewer = make_viewer(db_path, bot_latitude="47.5", bot_longitude="-122.3")
    seed(viewer, (OLD_SELF_KEY, KEY_A, ago(20)), (SELF_KEY, KEY_A, ago(1)))

    current, old = viewer.nodes()

    assert current["public_key"] == SELF_KEY
    assert current["name"] == "TestBot"
    assert old["public_key"] == OLD_SELF_KEY
    assert old["name"] == f"Local radio {OLD_SELF_KEY[:6].upper()}"
    assert old["latitude"] is None


def test_days_window_drops_a_radio_with_only_stale_links(db_path):
    viewer = make_viewer(db_path)
    seed(viewer, (OLD_SELF_KEY, KEY_A, ago(40)), (SELF_KEY, KEY_A, ago(1)))

    assert [n["public_key"] for n in viewer.nodes(days=30)] == [SELF_KEY]
    assert len(viewer.nodes(days=None)) == 2
