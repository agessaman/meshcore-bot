#!/usr/bin/env python3
"""
Unit tests for MeshGraph performance optimizations.

Covers the optimizations added for low-memory devices (Raspberry Pi Zero 2 W):
  - Adjacency indexes (_outgoing_index / _incoming_index) for O(1) lookups
  - sys.intern() public-key string deduplication
  - prune_expired_edges() and edge expiration SQL filter
  - Web-viewer notification throttle (_notification_timestamps)
  - capture_enabled flag (graph_capture_enabled config setting)
"""

import time
import sqlite3
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from modules.mesh_graph import MeshGraph


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_key(prefix: str) -> str:
    """Generate a deterministic 64-char hex public key from a 2-char prefix."""
    return (prefix.lower() * 32)[:64]


# ---------------------------------------------------------------------------
# 1. Adjacency Indexes
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAdjacencyIndexes:
    """Verify that _outgoing_index and _incoming_index are maintained correctly."""

    def test_index_populated_on_add_edge(self, mesh_graph):
        """Adding an edge must update both adjacency indexes."""
        mesh_graph.add_edge('ab', 'cd')

        assert 'cd' in mesh_graph._outgoing_index['ab']
        assert 'ab' in mesh_graph._incoming_index['cd']

    def test_index_not_duplicated_on_update(self, mesh_graph):
        """Updating an existing edge must not add duplicate entries to the sets."""
        mesh_graph.add_edge('ab', 'cd')
        mesh_graph.add_edge('ab', 'cd')  # second call is an update

        assert len(mesh_graph._outgoing_index['ab']) == 1
        assert len(mesh_graph._incoming_index['cd']) == 1

    def test_get_outgoing_edges_uses_index(self, mesh_graph):
        """get_outgoing_edges() must return all edges from a node via the index."""
        mesh_graph.add_edge('ab', 'cd')
        mesh_graph.add_edge('ab', 'ef')

        result = mesh_graph.get_outgoing_edges('ab')

        assert len(result) == 2
        to_prefixes = {e['to_prefix'] for e in result}
        assert to_prefixes == {'cd', 'ef'}

    def test_get_incoming_edges_uses_index(self, mesh_graph):
        """get_incoming_edges() must return all edges to a node via the index."""
        mesh_graph.add_edge('ab', 'ef')
        mesh_graph.add_edge('cd', 'ef')

        result = mesh_graph.get_incoming_edges('ef')

        assert len(result) == 2
        from_prefixes = {e['from_prefix'] for e in result}
        assert from_prefixes == {'ab', 'cd'}

    def test_get_outgoing_edges_empty_for_unknown_prefix(self, mesh_graph):
        """get_outgoing_edges() for an unknown prefix must return [] without raising."""
        result = mesh_graph.get_outgoing_edges('zz')
        assert result == []

    def test_get_incoming_edges_empty_for_unknown_prefix(self, mesh_graph):
        """get_incoming_edges() for an unknown prefix must return [] without raising."""
        result = mesh_graph.get_incoming_edges('zz')
        assert result == []

    def test_index_consistent_with_edges_dict(self, mesh_graph):
        """Every (from, to) pair in self.edges must be reflected in both indexes."""
        mesh_graph.add_edge('ab', 'cd')
        mesh_graph.add_edge('ab', 'ef')
        mesh_graph.add_edge('cd', 'ef')

        for (from_p, to_p) in mesh_graph.edges:
            assert to_p in mesh_graph._outgoing_index[from_p], \
                f"Missing {to_p} in _outgoing_index[{from_p}]"
            assert from_p in mesh_graph._incoming_index[to_p], \
                f"Missing {from_p} in _incoming_index[{to_p}]"


# ---------------------------------------------------------------------------
# 2. Public Key Interning
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublicKeyInterning:
    """Verify that sys.intern() causes identical public-key strings to share identity."""

    def test_same_key_shared_across_edges(self, mesh_graph):
        """Identical public keys stored on different edges must be the same object."""
        shared_key = _make_key('ab')

        mesh_graph.add_edge('ab', 'cd', from_public_key=shared_key)
        mesh_graph.add_edge('ab', 'ef', from_public_key=shared_key)

        edge1 = mesh_graph.get_edge('ab', 'cd')
        edge2 = mesh_graph.get_edge('ab', 'ef')

        # 'is' checks object identity — only true if sys.intern() is working
        assert edge1['from_public_key'] is edge2['from_public_key']

    def test_interning_does_not_alter_value(self, mesh_graph):
        """sys.intern() must not change the string's value."""
        key = _make_key('ab')
        mesh_graph.add_edge('ab', 'cd', from_public_key=key)

        edge = mesh_graph.get_edge('ab', 'cd')
        assert edge['from_public_key'] == key


# ---------------------------------------------------------------------------
# 3. Edge Expiration / prune_expired_edges()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEdgeExpiration:
    """Verify that prune_expired_edges() correctly evicts stale edges from RAM."""

    def _add_expired_edge(self, mesh_graph, from_p, to_p, days_old=10):
        """Add an edge and manually back-date its last_seen."""
        mesh_graph.add_edge(from_p, to_p)
        edge_key = (from_p, to_p)
        mesh_graph.edges[edge_key]['last_seen'] = datetime.now() - timedelta(days=days_old)

    def test_prune_removes_expired_edge_from_edges(self, mesh_graph):
        """An edge older than expiration_days must be removed from self.edges."""
        self._add_expired_edge(mesh_graph, 'ab', 'cd')
        assert ('ab', 'cd') in mesh_graph.edges  # sanity

        mesh_graph.prune_expired_edges()

        assert ('ab', 'cd') not in mesh_graph.edges

    def test_prune_removes_expired_edge_from_outgoing_index(self, mesh_graph):
        """Pruned edge must be removed from _outgoing_index."""
        self._add_expired_edge(mesh_graph, 'ab', 'cd')

        mesh_graph.prune_expired_edges()

        assert 'cd' not in mesh_graph._outgoing_index.get('ab', set())

    def test_prune_removes_expired_edge_from_incoming_index(self, mesh_graph):
        """Pruned edge must be removed from _incoming_index."""
        self._add_expired_edge(mesh_graph, 'ab', 'cd')

        mesh_graph.prune_expired_edges()

        assert 'ab' not in mesh_graph._incoming_index.get('cd', set())

    def test_prune_keeps_fresh_edge(self, mesh_graph):
        """An edge with a recent last_seen must NOT be pruned."""
        mesh_graph.add_edge('ab', 'cd')  # last_seen = now

        mesh_graph.prune_expired_edges()

        assert ('ab', 'cd') in mesh_graph.edges

    def test_prune_cleans_notification_timestamp(self, mesh_graph):
        """prune_expired_edges() must also clean up the notification timestamp entry."""
        self._add_expired_edge(mesh_graph, 'ab', 'cd')
        mesh_graph._notification_timestamps[('ab', 'cd')] = time.time()

        mesh_graph.prune_expired_edges()

        assert ('ab', 'cd') not in mesh_graph._notification_timestamps

    def test_prune_removes_empty_index_entries(self, mesh_graph):
        """When the last edge for a prefix is pruned, the index key must be removed."""
        self._add_expired_edge(mesh_graph, 'ab', 'cd')

        mesh_graph.prune_expired_edges()

        assert 'ab' not in mesh_graph._outgoing_index
        assert 'cd' not in mesh_graph._incoming_index

    def test_prune_returns_count_of_removed_edges(self, mesh_graph):
        """prune_expired_edges() must return the number of edges it removed."""
        self._add_expired_edge(mesh_graph, 'ab', 'cd')
        self._add_expired_edge(mesh_graph, 'ab', 'ef')
        mesh_graph.add_edge('ab', 'gh')  # fresh — should NOT be pruned

        count = mesh_graph.prune_expired_edges()

        assert count == 2

    def test_prune_disabled_when_expiration_days_zero(self, mesh_graph):
        """When edge_expiration_days == 0, prune_expired_edges() must do nothing."""
        mesh_graph.edge_expiration_days = 0
        self._add_expired_edge(mesh_graph, 'ab', 'cd')

        count = mesh_graph.prune_expired_edges()

        assert count == 0
        assert ('ab', 'cd') in mesh_graph.edges

    def test_startup_sql_filter_excludes_expired_edges(self, mock_bot):
        """MeshGraph.__init__ must not load edges older than edge_expiration_days."""
        # Insert one expired row and one fresh row directly into the DB
        db_path = mock_bot.db_manager.db_path
        expired_ts = (datetime.now() - timedelta(days=30)).isoformat()
        fresh_ts = datetime.now().isoformat()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                '''INSERT INTO mesh_connections
                   (from_prefix, to_prefix, observation_count, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)''',
                ('aa', 'bb', 5, expired_ts, expired_ts),
            )
            conn.execute(
                '''INSERT INTO mesh_connections
                   (from_prefix, to_prefix, observation_count, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)''',
                ('cc', 'dd', 3, fresh_ts, fresh_ts),
            )
            conn.commit()

        # graph_edge_expiration_days = 7 is set in the test_config fixture
        graph = MeshGraph(mock_bot)

        assert ('aa', 'bb') not in graph.edges, "Expired edge should not have been loaded"
        assert ('cc', 'dd') in graph.edges, "Fresh edge must be loaded"


# ---------------------------------------------------------------------------
# 4. Web Viewer Notification Throttle
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNotificationThrottle:
    """Verify that _notify_web_viewer_edge() throttles repeated update notifications."""

    @pytest.fixture
    def notifying_graph(self, mock_bot):
        """MeshGraph with a mock web_viewer_integration so notifications are trackable."""
        web_vi = MagicMock()
        web_vi.bot_integration = MagicMock()
        web_vi.bot_integration.send_mesh_edge_update = MagicMock()
        mock_bot.web_viewer_integration = web_vi
        mock_bot.config.set('Path_Command', 'graph_write_strategy', 'immediate')
        graph = MeshGraph(mock_bot)
        return graph

    def _notification_count(self, graph):
        return graph.bot.web_viewer_integration.bot_integration.send_mesh_edge_update.call_count

    def test_new_edge_always_notifies(self, notifying_graph):
        """A brand-new edge must trigger an immediate notification."""
        notifying_graph.add_edge('ab', 'cd')
        assert self._notification_count(notifying_graph) == 1

    def test_repeated_update_within_window_skips_notification(self, notifying_graph):
        """A second add_edge() call within the 10-second window must NOT notify again."""
        notifying_graph.add_edge('ab', 'cd')           # new edge → notifies
        notifying_graph.add_edge('ab', 'cd')           # update  → throttled

        assert self._notification_count(notifying_graph) == 1

    def test_update_after_throttle_window_notifies(self, notifying_graph):
        """An update after the 10-second throttle window has passed MUST notify."""
        notifying_graph.add_edge('ab', 'cd')           # new edge → notifies
        # Backdate the stored timestamp to simulate 11 seconds having elapsed
        notifying_graph._notification_timestamps[('ab', 'cd')] = time.time() - 11.0
        notifying_graph.add_edge('ab', 'cd')           # update after window → should notify

        assert self._notification_count(notifying_graph) == 2

    def test_throttle_is_per_edge(self, notifying_graph):
        """Each edge has its own throttle; two new edges must each notify once."""
        notifying_graph.add_edge('ab', 'cd')
        notifying_graph.add_edge('ab', 'ef')

        assert self._notification_count(notifying_graph) == 2


# ---------------------------------------------------------------------------
# 5. capture_enabled Flag
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCaptureEnabled:
    """Verify that graph_capture_enabled controls data collection and thread startup."""

    def test_capture_enabled_by_default(self, mesh_graph):
        """capture_enabled must be True when not explicitly configured."""
        assert mesh_graph.capture_enabled is True

    def test_capture_disabled_prevents_add_edge(self, mesh_graph):
        """When capture_enabled is False, add_edge() must be a no-op."""
        mesh_graph.capture_enabled = False
        mesh_graph.add_edge('ab', 'cd')

        assert mesh_graph.get_edge('ab', 'cd') is None

    def test_capture_disabled_leaves_indexes_empty(self, mesh_graph):
        """When capture is off, no index entries must be created."""
        mesh_graph.capture_enabled = False
        mesh_graph.add_edge('ab', 'cd')

        assert 'cd' not in mesh_graph._outgoing_index.get('ab', set())
        assert 'ab' not in mesh_graph._incoming_index.get('cd', set())

    def test_capture_disabled_from_config(self, mock_bot):
        """MeshGraph must read graph_capture_enabled = false from config."""
        mock_bot.config.set('Path_Command', 'graph_capture_enabled', 'false')

        graph = MeshGraph(mock_bot)

        assert graph.capture_enabled is False

    def test_capture_disabled_no_batch_thread_started(self, mock_bot):
        """When capture is off, the background batch writer thread must NOT start."""
        mock_bot.config.set('Path_Command', 'graph_capture_enabled', 'false')
        mock_bot.config.set('Path_Command', 'graph_write_strategy', 'batched')

        graph = MeshGraph(mock_bot)

        # Either _batch_thread was never set or it is None
        batch_thread = getattr(graph, '_batch_thread', None)
        assert batch_thread is None, "Batch writer thread should not start when capture is disabled"
