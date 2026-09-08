"""Mesh-graph edges derived from confirmed zero-hop neighbor discovery.

Covers BotDataViewer._compute_neighbor_evidence_edges and friends, which back
GET /api/mesh/edges?evidence=neighbors.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from modules.db_migrations import MigrationRunner
from modules.web_viewer.app import BotDataViewer

LOGGER = logging.getLogger("test-neighbor-edges")

SELF_KEY = "ff" * 32
KEY_A = "aa" * 32
KEY_B = "bb" * 32


def ago(days: float) -> str:
    """A fixture timestamp `days` before now, in the format the bot stores.

    These have to be relative: the days= filters compare last_seen against
    wall-clock now, so an absolute "recent" literal silently ages out of the
    window and turns the filter tests red on a date nobody picked.
    """
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return stamp.replace(microsecond=0).isoformat()


RECENT = ago(1)          # inside every window these tests exercise
EARLIER = ago(250)       # a lifetime first_seen, well outside a 30-day window
ANCIENT = ago(365 * 6)   # outside any window the viewer offers


class ViewerStub:
    """Just the db plumbing the derivation methods need.

    Binding the real functions keeps this a test of production code rather than a
    reimplementation; BotDataViewer.__init__ would want a Flask app and a bot.
    """

    logger = LOGGER
    NEIGHBOR_PREFIX_HEX_CHARS = BotDataViewer.NEIGHBOR_PREFIX_HEX_CHARS
    _compute_neighbor_evidence_edges = BotDataViewer._compute_neighbor_evidence_edges
    _derive_neighbor_evidence_graph = BotDataViewer._derive_neighbor_evidence_graph
    _neighbor_evidence_edge_keys = BotDataViewer._neighbor_evidence_edge_keys
    _filter_multibyte_evidence_edges = staticmethod(
        BotDataViewer._filter_multibyte_evidence_edges
    )

    def __init__(self, path):
        self._path = path

    @contextlib.contextmanager
    def _with_db_connection(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def insert_link(conn, self_key, neighbor_key, *, observations=1, snr_sum=5.0,
                snr_count=1, best_snr=5.0, last_snr=5.0, first_seen=RECENT,
                last_seen=RECENT):
    conn.execute(
        """
        INSERT INTO neighbor_links
            (self_public_key, neighbor_public_key, first_seen, last_seen,
             observation_count, snr_sum, snr_count, best_snr, last_snr,
             last_status, scopes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'responded', '')
        """,
        (self_key, neighbor_key, first_seen, last_seen, observations,
         snr_sum, snr_count, best_snr, last_snr),
    )


@pytest.fixture
def viewer(tmp_path):
    path = tmp_path / "viewer.db"
    conn = sqlite3.connect(path)
    MigrationRunner(conn, LOGGER).run()
    conn.close()
    return ViewerStub(path)


@pytest.fixture
def seeded(viewer):
    with viewer._with_db_connection() as conn:
        insert_link(conn, SELF_KEY, KEY_A, observations=4, snr_sum=30.0,
                    snr_count=4, best_snr=9.5, last_snr=7.0,
                    first_seen=EARLIER, last_seen=RECENT)
        insert_link(conn, SELF_KEY, KEY_B, observations=1, snr_sum=-3.0,
                    snr_count=1, best_snr=-3.0, last_snr=-3.0,
                    last_seen=ANCIENT)
        conn.commit()
    return viewer


def find_edge(edges, from_key, to_key):
    chars = ViewerStub.NEIGHBOR_PREFIX_HEX_CHARS
    matches = [
        e for e in edges
        if e["from_prefix"] == from_key[:chars] and e["to_prefix"] == to_key[:chars]
    ]
    assert len(matches) == 1
    return matches[0]


def test_each_link_yields_both_directions(seeded):
    """A discover response proves both directions of the link."""
    edges = seeded._compute_neighbor_evidence_edges()
    assert len(edges) == 4
    find_edge(edges, SELF_KEY, KEY_A)
    find_edge(edges, KEY_A, SELF_KEY)


def test_edges_carry_full_public_keys(seeded):
    """The multi-byte path derivation cannot supply these — paths have no keys."""
    edge = find_edge(seeded._compute_neighbor_evidence_edges(), SELF_KEY, KEY_A)
    assert edge["from_public_key"] == SELF_KEY
    assert edge["to_public_key"] == KEY_A


def test_edges_report_measured_snr(seeded):
    edge = find_edge(seeded._compute_neighbor_evidence_edges(), SELF_KEY, KEY_A)
    assert edge["snr"] == pytest.approx(30.0 / 4)
    assert edge["best_snr"] == 9.5
    assert edge["last_snr"] == 7.0


def test_edges_are_tagged_and_treated_as_first_hop(seeded):
    edge = find_edge(seeded._compute_neighbor_evidence_edges(), SELF_KEY, KEY_A)
    assert edge["evidence"] == "neighbors"
    # A direct link is the first hop of any path crossing it.
    assert edge["avg_hop_position"] == 1.0


def test_edges_preserve_lifetime_counts_and_timestamps(seeded):
    edge = find_edge(seeded._compute_neighbor_evidence_edges(), SELF_KEY, KEY_A)
    assert edge["observation_count"] == 4
    assert edge["first_seen"] == EARLIER
    assert edge["last_seen"] == RECENT


def test_missing_snr_samples_yield_null_mean(viewer):
    with viewer._with_db_connection() as conn:
        insert_link(conn, SELF_KEY, KEY_A, snr_count=0, snr_sum=0.0, best_snr=None)
        conn.commit()
    edge = find_edge(viewer._compute_neighbor_evidence_edges(), SELF_KEY, KEY_A)
    assert edge["snr"] is None


def test_days_filter_drops_stale_links(seeded):
    edges, prefix_hex_chars = seeded._derive_neighbor_evidence_graph(days=30)
    assert prefix_hex_chars == ViewerStub.NEIGHBOR_PREFIX_HEX_CHARS
    # Only the recent link survives, in both directions.
    assert len(edges) == 2
    assert {e["to_prefix"] for e in edges} | {e["from_prefix"] for e in edges} == {
        SELF_KEY[:6], KEY_A[:6]
    }


def test_min_observations_filter(seeded):
    edges, _ = seeded._derive_neighbor_evidence_graph(min_observations=3)
    assert len(edges) == 2
    assert all(e["observation_count"] >= 3 for e in edges)


def test_unfiltered_returns_everything(seeded):
    edges, _ = seeded._derive_neighbor_evidence_graph()
    assert len(edges) == 4


def test_edge_keys_cover_both_directions(seeded):
    """Used to relabel mesh_connections edges, which lost their provenance."""
    keys = seeded._neighbor_evidence_edge_keys()
    assert (SELF_KEY[:6], KEY_A[:6]) in keys.prefixes
    assert (KEY_A[:6], SELF_KEY[:6]) in keys.prefixes
    assert (SELF_KEY[:6], KEY_B[:6]) in keys.prefixes
    assert ("11", "22") not in keys.prefixes


def test_edge_keys_include_full_public_key_pairs(seeded):
    """The graph keeps some edges at a 1-byte prefix but fills in the keys.

    MeshGraph.add_edge deliberately does not promote a 1-byte edge that has no
    public key, so matching on the 3-byte prefix alone would leave a confirmed
    neighbor labelled 'singlebyte'.
    """
    keys = seeded._neighbor_evidence_edge_keys()
    assert (SELF_KEY, KEY_A) in keys.public_keys
    assert (KEY_A, SELF_KEY) in keys.public_keys


def test_edge_keys_honour_the_days_window(seeded):
    """neighbor_links is never pruned, so stale evidence must not label edges."""
    keys = seeded._neighbor_evidence_edge_keys(days=30)
    assert (SELF_KEY[:6], KEY_A[:6]) in keys.prefixes
    # KEY_B was last heard years ago.
    assert (SELF_KEY[:6], KEY_B[:6]) not in keys.prefixes
    assert (SELF_KEY, KEY_B) not in keys.public_keys


def test_blank_keys_are_skipped(viewer):
    with viewer._with_db_connection() as conn:
        insert_link(conn, "", KEY_A)
        conn.commit()
    assert viewer._compute_neighbor_evidence_edges() == []
    assert viewer._neighbor_evidence_edge_keys() == ((set(), set()))


def test_empty_database_yields_nothing(viewer):
    assert viewer._compute_neighbor_evidence_edges() == []
    assert viewer._neighbor_evidence_edge_keys() == ((set(), set()))


def test_pre_migration_database_degrades_quietly(tmp_path):
    """A database without migration 22 simply has no neighbor evidence."""
    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    stub = ViewerStub(path)
    assert stub._compute_neighbor_evidence_edges() == []
    assert stub._neighbor_evidence_edge_keys() == ((set(), set()))
    edges, chars = stub._derive_neighbor_evidence_graph(days=7)
    assert edges == []
    assert chars == ViewerStub.NEIGHBOR_PREFIX_HEX_CHARS
