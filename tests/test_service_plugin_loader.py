"""Tests for modules.service_plugin_loader."""

import configparser
from unittest.mock import MagicMock, Mock

import pytest

from modules.service_plugin_loader import ServicePluginLoader
from modules.service_plugins.base_service import BaseServicePlugin

# Minimal local service source (valid BaseServicePlugin subclass)
_LOCAL_SERVICE_SOURCE = '''
from modules.service_plugins.base_service import BaseServicePlugin


class MyLocalService(BaseServicePlugin):
    config_section = "MyLocalService"
    description = "Local test service"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
'''


@pytest.fixture
def service_loader_bot(tmp_path):
    """Mock bot for ServicePluginLoader tests."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.logger.debug = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Connection")
    bot.config.add_section("Bot")
    bot.config.add_section("Channels")
    bot.bot_root = tmp_path
    return bot


class TestDiscoverLocalServices:
    """Tests for local/service_plugins discovery."""

    def test_discover_local_services_empty_when_no_dir(self, service_loader_bot):
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=None)
        assert loader.discover_local_services() == []

    def test_discover_local_services_empty_when_dir_missing(self, service_loader_bot, tmp_path):
        missing = tmp_path / "local" / "service_plugins"
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=str(missing))
        assert loader.discover_local_services() == []

    def test_discover_local_services_finds_py_files(self, service_loader_bot, tmp_path):
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "my_svc.py").write_text("# test")
        (local_dir / "other.py").write_text("# test")
        (local_dir / "__init__.py").write_text("# init")
        (local_dir / "base_service.py").write_text("# base")
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=str(local_dir))
        stems = loader.discover_local_services()
        assert set(stems) == {"my_svc", "other"}
        assert "__init__" not in stems
        assert "base_service" not in stems


class TestLoadServiceFromPath:
    """Tests for load_service_from_path."""

    def test_load_service_from_path_loads_when_enabled(self, service_loader_bot, tmp_path):
        service_loader_bot.config.add_section("MyLocalService")
        service_loader_bot.config.set("MyLocalService", "enabled", "true")
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "my_local_service.py").write_text(_LOCAL_SERVICE_SOURCE)
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=str(local_dir))
        instance = loader.load_service_from_path(local_dir / "my_local_service.py")
        assert instance is not None
        assert isinstance(instance, BaseServicePlugin)
        assert instance.get_metadata()["name"] == "mylocal"

    def test_load_service_from_path_returns_none_when_disabled(self, service_loader_bot, tmp_path):
        service_loader_bot.config.add_section("MyLocalService")
        service_loader_bot.config.set("MyLocalService", "enabled", "false")
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "my_local_service.py").write_text(_LOCAL_SERVICE_SOURCE)
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=str(local_dir))
        instance = loader.load_service_from_path(local_dir / "my_local_service.py")
        assert instance is None

    def test_load_service_from_path_returns_none_for_invalid_file(self, service_loader_bot, tmp_path):
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "not_a_service.py").write_text("print('no service class')\n")
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=str(local_dir))
        instance = loader.load_service_from_path(local_dir / "not_a_service.py")
        assert instance is None

    def test_load_service_from_path_returns_none_when_section_exists_but_enabled_not_set(
        self, service_loader_bot, tmp_path
    ):
        service_loader_bot.config.add_section("MyLocalService")
        # do not set enabled
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "my_local_service.py").write_text(_LOCAL_SERVICE_SOURCE)
        loader = ServicePluginLoader(service_loader_bot, local_services_dir=str(local_dir))
        instance = loader.load_service_from_path(local_dir / "my_local_service.py")
        assert instance is None


class TestLoadAllServicesWithLocal:
    """Tests for load_all_services with local/service_plugins."""

    def test_load_all_services_includes_local_service(self, service_loader_bot, tmp_path):
        service_loader_bot.config.add_section("MyLocalService")
        service_loader_bot.config.set("MyLocalService", "enabled", "true")
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "my_local_service.py").write_text(_LOCAL_SERVICE_SOURCE)
        # Use nonexistent services_dir so no built-in services load
        loader = ServicePluginLoader(
            service_loader_bot,
            services_dir=str(tmp_path / "nonexistent_services"),
            local_services_dir=str(local_dir),
        )
        loaded = loader.load_all_services()
        assert "mylocal" in loaded
        assert loader.get_service_by_name("mylocal") is not None

    def test_load_all_services_skips_local_when_name_collision(self, service_loader_bot, tmp_path):
        # Local service with same derived name as another local: second one skipped
        # We need two local services; one gets loaded first, second has same name -> skip
        service_loader_bot.config.add_section("MyLocalService")
        service_loader_bot.config.set("MyLocalService", "enabled", "true")
        local_dir = tmp_path / "local" / "service_plugins"
        local_dir.mkdir(parents=True)
        (local_dir / "my_local_service.py").write_text(_LOCAL_SERVICE_SOURCE)
        # Second file with same config_section/name
        other_src = _LOCAL_SERVICE_SOURCE.replace("MyLocalService", "MyLocalService").replace(
            "my_local_service", "my_local_service_dup"
        )
        (local_dir / "my_local_service_dup.py").write_text(other_src)
        loader = ServicePluginLoader(
            service_loader_bot,
            services_dir=str(tmp_path / "nonexistent_services"),
            local_services_dir=str(local_dir),
        )
        loaded = loader.load_all_services()
        # Only one "mylocal" (first file wins; second is skipped with warning)
        assert loaded.get("mylocal") is not None
        service_loader_bot.logger.warning.assert_called()
        warning_calls = [str(c) for c in service_loader_bot.logger.warning.call_args_list]
        assert any("already loaded" in str(c) for c in warning_calls)
