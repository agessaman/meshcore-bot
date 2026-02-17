"""Tests for modules.plugin_loader."""

import pytest
from unittest.mock import Mock, MagicMock, AsyncMock

from modules.plugin_loader import PluginLoader
from modules.commands.base_command import BaseCommand


@pytest.fixture
def loader_bot(mock_logger, minimal_config):
    """Mock bot for PluginLoader tests."""
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = minimal_config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda k, **kw: k)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.meshcore = None
    return bot


def _load_and_register(loader, plugin_file):
    """Load a plugin and register it in the loader's internal state (like load_all_plugins does)."""
    instance = loader.load_plugin(plugin_file)
    if instance:
        metadata = instance.get_metadata()
        name = metadata["name"]
        loader.loaded_plugins[name] = instance
        loader.plugin_metadata[name] = metadata
        loader._build_keyword_mappings(name, metadata)
    return instance


class TestDiscover:
    """Tests for plugin discovery."""

    def test_discover_plugins_finds_command_files(self, loader_bot):
        loader = PluginLoader(loader_bot)
        plugins = loader.discover_plugins()
        assert isinstance(plugins, list)
        assert len(plugins) > 0
        # Should find well-known commands
        assert "ping_command" in plugins
        assert "help_command" in plugins

    def test_discover_plugins_excludes_base_and_init(self, loader_bot):
        loader = PluginLoader(loader_bot)
        plugins = loader.discover_plugins()
        assert "__init__" not in plugins
        assert "base_command" not in plugins

    def test_discover_alternative_plugins_empty_when_no_dir(self, loader_bot, tmp_path):
        loader = PluginLoader(loader_bot, commands_dir=str(tmp_path / "nonexistent"))
        result = loader.discover_alternative_plugins()
        assert result == []


class TestValidatePlugin:
    """Tests for plugin class validation."""

    def test_validate_missing_execute(self, loader_bot):
        loader = PluginLoader(loader_bot)

        class NoExecute:
            name = "test"
            keywords = ["test"]

        errors = loader._validate_plugin(NoExecute)
        assert any("execute" in e.lower() for e in errors)

    def test_validate_sync_execute(self, loader_bot):
        loader = PluginLoader(loader_bot)

        class SyncExecute:
            name = "test"
            keywords = ["test"]

            def execute(self, message):
                return True

        errors = loader._validate_plugin(SyncExecute)
        assert any("async" in e.lower() for e in errors)

    def test_validate_valid_class(self, loader_bot):
        loader = PluginLoader(loader_bot)

        class ValidCommand:
            name = "test"
            keywords = ["test"]

            async def execute(self, message):
                return True

        errors = loader._validate_plugin(ValidCommand)
        assert len(errors) == 0


class TestLoadPlugin:
    """Tests for loading individual plugins."""

    def test_load_ping_command(self, loader_bot):
        loader = PluginLoader(loader_bot)
        plugin = loader.load_plugin("ping_command")
        assert plugin is not None
        assert isinstance(plugin, BaseCommand)
        assert plugin.name == "ping"

    def test_load_nonexistent_returns_none(self, loader_bot):
        loader = PluginLoader(loader_bot)
        plugin = loader.load_plugin("totally_nonexistent_command")
        assert plugin is None
        assert "totally_nonexistent_command" in loader._failed_plugins


class TestKeywordLookup:
    """Tests for keyword-based plugin lookup after registration."""

    def test_get_plugin_by_keyword(self, loader_bot):
        loader = PluginLoader(loader_bot)
        _load_and_register(loader, "ping_command")
        result = loader.get_plugin_by_keyword("ping")
        assert result is not None
        assert result.name == "ping"

    def test_get_plugin_by_keyword_miss(self, loader_bot):
        loader = PluginLoader(loader_bot)
        assert loader.get_plugin_by_keyword("nonexistent") is None

    def test_get_plugin_by_name(self, loader_bot):
        loader = PluginLoader(loader_bot)
        _load_and_register(loader, "ping_command")
        result = loader.get_plugin_by_name("ping")
        assert result is not None
        assert result.name == "ping"


class TestCategoryAndFailed:
    """Tests for category filtering and failed plugin tracking."""

    def test_get_plugins_by_category(self, loader_bot):
        loader = PluginLoader(loader_bot)
        _load_and_register(loader, "ping_command")
        _load_and_register(loader, "help_command")
        # Ping and help are in the "basic" category
        result = loader.get_plugins_by_category("basic")
        assert isinstance(result, dict)
        assert "ping" in result or "help" in result

    def test_get_failed_plugins_returns_copy(self, loader_bot):
        loader = PluginLoader(loader_bot)
        loader.load_plugin("nonexistent_command")
        failed = loader.get_failed_plugins()
        assert isinstance(failed, dict)
        assert "nonexistent_command" in failed
        # Mutating the return should not affect internal state
        failed.clear()
        assert len(loader.get_failed_plugins()) > 0
