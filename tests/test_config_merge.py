"""Tests for config loading and merging of local/config.ini."""

import threading
from pathlib import Path
from unittest.mock import patch

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

    def test_reload_rejects_radio_change_from_local_overlay(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path),
            encoding="utf-8",
        )
        local_dir = tmp_path / "local"
        local_dir.mkdir(parents=True)
        local_config = local_dir / "config.ini"
        local_config.write_text("[LocalExtra]\nvalue = original\n", encoding="utf-8")
        bot = MeshCoreBot(config_file=str(main_config))
        old_config = bot.config

        local_config.write_text(
            "[Connection]\nconnection_type = tcp\nhostname = radio.example\n",
            encoding="utf-8",
        )
        success, message = bot.reload_config()

        assert success is False
        assert "restart required" in message.lower()
        assert bot.config is old_config
        assert bot.config.get("Connection", "connection_type") == "ble"

    def test_malformed_local_overlay_leaves_snapshot_untouched(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path),
            encoding="utf-8",
        )
        local_dir = tmp_path / "local"
        local_dir.mkdir(parents=True)
        local_config = local_dir / "config.ini"
        local_config.write_text("[LocalExtra]\nvalue = original\n", encoding="utf-8")
        bot = MeshCoreBot(config_file=str(main_config))
        old_config = bot.config

        local_config.write_text("this is not an ini section\n", encoding="utf-8")
        success, message = bot.reload_config()

        assert success is False
        assert "error reloading" in message.lower()
        assert bot.config is old_config
        assert bot.config.get("LocalExtra", "value") == "original"

    def test_invalid_typed_value_in_overlay_leaves_snapshot_untouched(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path),
            encoding="utf-8",
        )
        local_dir = tmp_path / "local"
        local_dir.mkdir(parents=True)
        local_config = local_dir / "config.ini"
        local_config.write_text("[LocalExtra]\nvalue = original\n", encoding="utf-8")
        bot = MeshCoreBot(config_file=str(main_config))
        old_config = bot.config

        local_config.write_text("[Web_Viewer]\nport = not-a-port\n", encoding="utf-8")
        success, message = bot.reload_config()

        assert success is False
        assert "invalid literal" in message.lower()
        assert bot.config is old_config

    def test_cached_service_change_requires_restart(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + "\n[Cached_Local_Service]\ninterval = 10\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        cached_service = type(
            "CachedService", (), {"config_section": "Cached_Local_Service"}
        )()
        bot.services["cached"] = cached_service
        old_config = bot.config
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + "\n[Cached_Local_Service]\ninterval = 20\n",
            encoding="utf-8",
        )

        success, message = bot.reload_config()

        assert success is False
        assert "cached service" in message.lower()
        assert "Cached_Local_Service" in message
        assert bot.config is old_config

    def test_component_failure_rolls_back_config_and_components(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path).replace(
                "db_path =", "rate_limit_seconds = 10\ndb_path ="
            ),
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        old_config = bot.config
        old_manager = bot.command_manager
        old_limiter = bot.rate_limiter
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path).replace(
                "db_path =", "rate_limit_seconds = 25\ndb_path ="
            ),
            encoding="utf-8",
        )

        with patch.object(
            bot.scheduler,
            "setup_scheduled_messages",
            side_effect=RuntimeError("scheduler rejected candidate"),
        ):
            success, message = bot.reload_config()

        assert success is False
        assert "scheduler rejected candidate" in message
        assert bot.config is old_config
        assert bot.command_manager is old_manager
        assert bot.rate_limiter is old_limiter
        assert bot.config.getint("Bot", "rate_limit_seconds") == 10

    def test_system_exit_during_command_reload_rolls_back_state(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path).replace(
                "db_path =", "rate_limit_seconds = 10\ndb_path ="
            ),
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        old_config = bot.config
        old_manager = bot.command_manager
        old_limiter = bot.rate_limiter
        old_translator = bot.translator
        old_scheduler = bot.scheduler._apscheduler
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path).replace(
                "db_path =", "rate_limit_seconds = 25\ndb_path ="
            ),
            encoding="utf-8",
        )

        with patch("modules.core.CommandManager", side_effect=SystemExit("bad plugin")):
            success, message = bot.reload_config()

        assert success is False
        assert "bad plugin" in message
        assert bot.config is old_config
        assert bot.command_manager is old_manager
        assert bot.rate_limiter is old_limiter
        assert bot.translator is old_translator
        assert bot.scheduler._apscheduler is old_scheduler
        assert bot.config.getint("Bot", "rate_limit_seconds") == 10

    def test_nested_command_objects_keep_real_bot_after_reload(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + "\n[Weather]\nweather_provider = openmeteo\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))

        success, _ = bot.reload_config()

        assert success is True
        wx_command = bot.command_manager.commands["wx"]
        assert wx_command.bot is bot
        assert wx_command.delegate_command is not None
        assert wx_command.delegate_command.bot is bot

    def test_concurrent_reader_observes_only_complete_snapshots(self, tmp_path):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + "\n[Atomic]\ngeneration = old\nleft = old\nright = old\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + "\n[Atomic]\ngeneration = new\nleft = new\nright = new\n",
            encoding="utf-8",
        )

        stop = threading.Event()
        observed: set[tuple[str, str, str]] = set()

        def read_snapshots() -> None:
            while not stop.is_set():
                snapshot = bot.config
                observed.add(
                    (
                        snapshot.get("Atomic", "generation"),
                        snapshot.get("Atomic", "left"),
                        snapshot.get("Atomic", "right"),
                    )
                )

        reader = threading.Thread(target=read_snapshots)
        reader.start()
        try:
            success, _ = bot.reload_config()
            assert success is True
        finally:
            stop.set()
            reader.join(timeout=2)

        assert observed
        assert observed <= {("old", "old", "old"), ("new", "new", "new")}


class TestLocalTranslationPath:
    """The local translation catalog follows [Bot] local_dir_path."""

    def _bot_with_local_dir(self, tmp_path, local_dir_path: str | None):
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        text = _minimal_main_config(tmp_path, db_path)
        if local_dir_path is not None:
            text = text.replace("[Bot]\n", f"[Bot]\nlocal_dir_path = {local_dir_path}\n")
        main_config.write_text(text, encoding="utf-8")
        return MeshCoreBot(config_file=str(main_config))

    def test_defaults_into_local_dir_path(self, tmp_path):
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        bot = self._bot_with_local_dir(tmp_path, str(elsewhere))
        assert bot.local_translation_path == str(elsewhere / "translations")

    def test_defaults_to_local_when_unset(self, tmp_path):
        bot = self._bot_with_local_dir(tmp_path, None)
        # Absolute and anchored on the config's directory, not the process cwd.
        assert bot.local_translation_path == str(tmp_path / "local" / "translations")

    def test_explicit_setting_overrides_the_default(self, tmp_path):
        custom = tmp_path / "my-catalogs"
        custom.mkdir()
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + f"\n[Localization]\nlanguage = en\nlocal_translation_path = {custom.as_posix()}\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        assert bot.local_translation_path == str(custom)

    def test_local_catalog_reaches_the_translator(self, tmp_path):
        """End to end: a catalog under local_dir_path overrides a shipped string."""
        import json

        dist = tmp_path / "translations"
        dist.mkdir()
        (dist / "en.json").write_text(
            json.dumps({"commands": {"ping": "shipped", "other": "keep"}}), encoding="utf-8"
        )
        local_translations = tmp_path / "local" / "translations"
        local_translations.mkdir(parents=True)
        (local_translations / "en.json").write_text(
            json.dumps({"commands": {"ping": "from local"}}), encoding="utf-8"
        )
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + f"\n[Localization]\nlanguage = en\ntranslation_path = {dist.as_posix()}\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        assert bot.translator.translate("commands.ping") == "from local"
        assert bot.translator.translate("commands.other") == "keep"

    def test_reload_picks_up_a_changed_local_translation_path(self, tmp_path):
        """A reload must republish the local path, not keep the pre-reload one."""
        db_path = tmp_path / "bot.db"
        main_config = tmp_path / "config.ini"
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + "\n[Localization]\nlanguage = en\n",
            encoding="utf-8",
        )
        bot = MeshCoreBot(config_file=str(main_config))
        assert bot.local_translation_path == str(tmp_path / "local" / "translations")

        moved = tmp_path / "moved-catalogs"
        moved.mkdir()
        main_config.write_text(
            _minimal_main_config(tmp_path, db_path)
            + f"\n[Localization]\nlanguage = en\nlocal_translation_path = {moved.as_posix()}\n",
            encoding="utf-8",
        )
        success, _ = bot.reload_config()
        assert success is True
        assert bot.local_translation_path == str(moved)
