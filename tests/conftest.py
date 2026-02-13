#!/usr/bin/env python3
"""
Pytest fixtures for meshcore-bot tests
"""

import pytest
import sqlite3
import configparser
from unittest.mock import Mock, MagicMock, AsyncMock
from datetime import datetime, timedelta
from typing import Any, Optional

from modules.db_manager import DBManager
from modules.mesh_graph import MeshGraph
from modules.models import MeshMessage
from tests.helpers import create_test_repeater, create_test_edge, populate_test_graph


def mock_message(
    content: str = "ping",
    channel: Optional[str] = "general",
    is_dm: bool = False,
    sender_id: Optional[str] = "TestUser",
    sender_pubkey: Optional[str] = None,
    **kwargs: Any,
) -> MeshMessage:
    """Factory for creating MeshMessage instances in tests."""
    return MeshMessage(
        content=content,
        channel=channel if not is_dm else None,
        is_dm=is_dm,
        sender_id=sender_id,
        sender_pubkey=sender_pubkey,
        **kwargs,
    )


@pytest.fixture
def minimal_config():
    """Minimal ConfigParser for command tests (Connection, Bot, Channels, Keywords)."""
    config = configparser.ConfigParser()
    config.add_section("Connection")
    config.set("Connection", "connection_type", "serial")
    config.set("Connection", "serial_port", "/dev/ttyUSB0")
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.set("Bot", "db_path", "meshcore_bot.db")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general,test,emergency")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.set("Keywords", "ping", "Pong!")
    config.set("Keywords", "test", "ack")
    return config


@pytest.fixture
def command_mock_bot(mock_logger, minimal_config):
    """Lightweight mock bot for command tests. No DB, no mesh_graph."""
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = minimal_config
    bot.translator = MagicMock()

    def _mock_translate(key, **kwargs):
        if kwargs:
            return key + " " + " ".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return key

    bot.translator.translate = Mock(side_effect=_mock_translate)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general", "test", "emergency"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    return bot


@pytest.fixture
def command_mock_bot_with_db(mock_logger, minimal_config, tmp_path):
    """Command mock bot with db_manager for commands that need DB (e.g. StatsCommand)."""
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = minimal_config
    bot.translator = MagicMock()

    def _mock_translate(key, **kwargs):
        if kwargs:
            return key + " " + " ".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return key

    bot.translator.translate = Mock(side_effect=_mock_translate)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general", "test", "emergency"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.db_manager = MagicMock()
    bot.db_manager.db_path = str(tmp_path / "test.db")
    return bot


@pytest.fixture
def mock_logger():
    """Create a mock logger for testing."""
    logger = Mock()
    logger.info = Mock()
    logger.debug = Mock()
    logger.warning = Mock()
    logger.error = Mock()
    return logger


@pytest.fixture
def test_config():
    """Create a test configuration with Path_Command settings."""
    config = configparser.ConfigParser()
    
    # Add Path_Command section with graph-related settings
    config.add_section('Path_Command')
    config.set('Path_Command', 'enabled', 'true')
    config.set('Path_Command', 'graph_based_validation', 'true')
    config.set('Path_Command', 'min_edge_observations', '3')
    config.set('Path_Command', 'graph_write_strategy', 'immediate')  # For faster tests
    config.set('Path_Command', 'graph_batch_interval_seconds', '30')
    config.set('Path_Command', 'graph_batch_max_pending', '100')
    config.set('Path_Command', 'graph_startup_load_days', '0')  # Don't load old data in tests
    config.set('Path_Command', 'graph_edge_expiration_days', '7')
    config.set('Path_Command', 'graph_use_bidirectional', 'true')
    config.set('Path_Command', 'graph_use_hop_position', 'true')
    config.set('Path_Command', 'graph_multi_hop_enabled', 'true')
    config.set('Path_Command', 'graph_multi_hop_max_hops', '2')
    config.set('Path_Command', 'graph_geographic_combined', 'false')
    config.set('Path_Command', 'graph_geographic_weight', '0.7')
    config.set('Path_Command', 'graph_prefer_stored_keys', 'true')
    config.set('Path_Command', 'star_bias_multiplier', '2.5')
    
    # Add Bot section (for location if needed)
    config.add_section('Bot')
    config.set('Bot', 'bot_latitude', '47.6062')
    config.set('Bot', 'bot_longitude', '-122.3321')
    
    return config


@pytest.fixture
def test_db(mock_logger, tmp_path):
    """Create a file-based SQLite database for testing.

    Uses tmp_path (not :memory:) so all connections share the same database.
    SQLite :memory: creates a new empty DB per connection, causing isolation issues.
    """
    db_path = str(tmp_path / "test.db")
    
    # Create a minimal bot mock for DBManager
    mock_bot = Mock()
    mock_bot.logger = mock_logger
    
    # Create DBManager with file-based database
    db_manager = DBManager(mock_bot, db_path)
    
    # Initialize mesh_connections table schema
    db_manager.create_table('mesh_connections', '''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_prefix TEXT NOT NULL,
        to_prefix TEXT NOT NULL,
        from_public_key TEXT,
        to_public_key TEXT,
        observation_count INTEGER DEFAULT 1,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        avg_hop_position REAL,
        geographic_distance REAL,
        UNIQUE(from_prefix, to_prefix)
    ''')
    
    # Initialize complete_contact_tracking table schema (for repeater lookups)
    db_manager.create_table('complete_contact_tracking', '''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_key TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        device_type TEXT,
        first_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        advert_count INTEGER DEFAULT 1,
        latitude REAL,
        longitude REAL,
        city TEXT,
        state TEXT,
        country TEXT,
        raw_advert_data TEXT,
        signal_strength REAL,
        snr REAL,
        hop_count INTEGER,
        is_currently_tracked BOOLEAN DEFAULT 0,
        last_advert_timestamp TIMESTAMP,
        location_accuracy REAL,
        contact_source TEXT DEFAULT 'advertisement',
        out_path TEXT,
        out_path_len INTEGER,
        is_starred INTEGER DEFAULT 0
    ''')
    
    # Create indexes (after tables are created)
    # Create indexes (db_manager created tables in same db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            # Check if table exists before creating indexes
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mesh_connections'")
            if cursor.fetchone():
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_from_prefix ON mesh_connections(from_prefix)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_to_prefix ON mesh_connections(to_prefix)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_last_seen ON mesh_connections(last_seen)')
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='complete_contact_tracking'")
            if cursor.fetchone():
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_public_key ON complete_contact_tracking(public_key)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_role ON complete_contact_tracking(role)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_complete_last_heard ON complete_contact_tracking(last_heard)')
            conn.commit()
    except Exception:
        # Indexes are optional, continue if they fail
        pass
    
    yield db_manager
    
    # Cleanup (tmp_path is automatically cleaned up by pytest)


@pytest.fixture
def mock_bot(mock_logger, test_config, test_db):
    """Create a mock bot instance with all necessary attributes."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = test_config
    bot.db_manager = test_db
    bot.bot_root = '/tmp'  # Dummy path for testing
    
    # Mock repeater_manager if needed
    bot.repeater_manager = Mock()
    bot.repeater_manager.get_repeater_devices = Mock(return_value=[])
    
    # Mock web_viewer_integration (optional, for edge notifications)
    bot.web_viewer_integration = None
    
    return bot


@pytest.fixture
def mesh_graph(mock_bot):
    """Create a MeshGraph instance for testing."""
    # Ensure config is set to immediate strategy for tests
    mock_bot.config.set('Path_Command', 'graph_write_strategy', 'immediate')
    graph = MeshGraph(mock_bot)
    # Ensure batch writer doesn't interfere with tests
    if hasattr(graph, '_batch_thread') and graph._batch_thread:
        graph._shutdown_event.set()
    return graph


@pytest.fixture
def populated_mesh_graph(mesh_graph):
    """Create a MeshGraph instance with sample edges for testing."""
    # Add some test edges
    edges = [
        create_test_edge('01', '7e', observation_count=5, last_seen=datetime.now()),
        create_test_edge('7e', '86', observation_count=3, last_seen=datetime.now()),
        create_test_edge('86', 'e0', observation_count=10, last_seen=datetime.now()),
        create_test_edge('e0', '09', observation_count=2, last_seen=datetime.now()),
        # Bidirectional edge
        create_test_edge('01', '7a', observation_count=4, last_seen=datetime.now()),
        create_test_edge('7a', '01', observation_count=4, last_seen=datetime.now()),
        # Stale edge (old)
        create_test_edge('7a', 'cf', observation_count=1, last_seen=datetime.now() - timedelta(days=30)),
    ]
    populate_test_graph(mesh_graph, edges)
    return mesh_graph
