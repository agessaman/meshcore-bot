#!/usr/bin/env python3
"""
Tests for additive topology shadow table initialization/migration.
"""

import configparser
from unittest.mock import Mock

import pytest

from modules.db_manager import DBManager
from modules.repeater_manager import RepeaterManager


@pytest.mark.unit
def test_repeater_manager_creates_topology_shadow_tables(tmp_path):
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "auto_manage_contacts", "false")
    config.add_section("Companion_Purge")
    config.set("Companion_Purge", "companion_purge_enabled", "false")

    bot = Mock()
    bot.logger = Mock()
    bot.config = config
    bot.db_manager = DBManager(bot, str(tmp_path / "rm_topology.db"))

    RepeaterManager(bot)

    tables = bot.db_manager.execute_query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('topology_inference_shadow', 'topology_ghost_nodes', 'topology_model_metrics')"
    )
    names = {row["name"] for row in tables}
    assert "topology_inference_shadow" in names
    assert "topology_ghost_nodes" in names
    assert "topology_model_metrics" in names
