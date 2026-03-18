"""Tests for config loading and merging of local/config.ini."""

from pathlib import Path

from modules.core import MeshCoreBot


def _minimal_main_config(bot_root: Path, db_path: Path) -> str:
    return f"""[Connection]
connection_type = ble

[Bot]
db_path = {db_path.as_posix()}

[Channels]
monitor_channels = #general
"""


class TestLoadConfigMerge:
    """Test that load_config() merges local/config.ini when present."""

    def test_load_config_merges_local_config_ini(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path),
            encoding="utf-8",
        )
        local_dir = tmp_path / "local"
        local_dir.mkdir(parents=True)
        local_config = local_dir / "config.ini"
        local_config.write_text(
            "[LocalExtra]\nmy_option = from_local\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        assert bot.config.has_section("LocalExtra")
        assert bot.config.get("LocalExtra", "my_option") == "from_local"

    def test_load_config_no_local_file(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path),
            encoding="utf-8",
        )
        # No local/config.ini
        bot = MeshCoreBot(config_file=str(main_config))
        assert not bot.config.has_section("LocalExtra")


class TestReloadConfigMerge:
    """Test that reload_config() re-reads and merges local/config.ini."""

    def test_reload_config_merges_local_config_ini(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path),
            encoding="utf-8",
        )
        local_dir = tmp_path / "local"
        local_dir.mkdir(parents=True)
        local_config = local_dir / "config.ini"
        local_config.write_text(
            "[LocalExtra]\nmy_option = from_local\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        assert bot.config.get("LocalExtra", "my_option") == "from_local"
        # Update local config and reload
        local_config.write_text(
            "[LocalExtra]\nmy_option = updated_after_reload\n",
            encoding="utf-8",
        )
        success, _ = bot.reload_config()
        assert success
        assert bot.config.get("LocalExtra", "my_option") == "updated_after_reload"
