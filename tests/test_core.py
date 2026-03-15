"""Tests for MeshCoreBot logic (config loading, radio settings, helpers)."""

import pytest
from pathlib import Path

from modules.core import MeshCoreBot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, db_path: Path, extra: str = "") -> None:
    path.write_text(
        f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
timeout = 30

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 1

[Channels]
monitor_channels = #general
{extra}
""",
        encoding="utf-8",
    )


@pytest.fixture
def bot(tmp_path):
    """Minimal MeshCoreBot from a temporary config file."""
    config_file = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    _write_config(config_file, db_path)
    return MeshCoreBot(config_file=str(config_file))


# ---------------------------------------------------------------------------
# bot_root property
# ---------------------------------------------------------------------------

class TestBotRoot:
    def test_returns_config_directory(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        bot = MeshCoreBot(config_file=str(config_file))
        assert bot.bot_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _get_radio_settings
# ---------------------------------------------------------------------------

class TestGetRadioSettings:
    def test_returns_dict_with_expected_keys(self, bot):
        settings = bot._get_radio_settings()
        assert "connection_type" in settings
        assert "serial_port" in settings
        assert "ble_device_name" in settings
        assert "hostname" in settings
        assert "tcp_port" in settings
        assert "timeout" in settings

    def test_reads_connection_type(self, bot):
        assert bot._get_radio_settings()["connection_type"] == "serial"

    def test_reads_serial_port(self, bot):
        assert bot._get_radio_settings()["serial_port"] == "/dev/ttyUSB0"

    def test_reads_timeout(self, bot):
        assert bot._get_radio_settings()["timeout"] == 30

    def test_defaults_when_missing(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        # Write config without optional fields
        config_file.write_text(
            f"""[Connection]
connection_type = ble

[Bot]
db_path = {db_path.as_posix()}

[Channels]
monitor_channels = #general
""",
            encoding="utf-8",
        )
        b = MeshCoreBot(config_file=str(config_file))
        settings = b._get_radio_settings()
        assert settings["ble_device_name"] == ""
        assert settings["hostname"] == ""
        assert settings["tcp_port"] == 5000


# ---------------------------------------------------------------------------
# reload_config
# ---------------------------------------------------------------------------

class TestReloadConfig:
    def test_reload_succeeds_with_same_radio_settings(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        b = MeshCoreBot(config_file=str(config_file))
        success, msg = b.reload_config()
        assert success is True

    def test_reload_fails_when_radio_settings_changed(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        b = MeshCoreBot(config_file=str(config_file))
        # Change serial port in config file
        _write_config(config_file, db_path, extra="")
        config_file.write_text(
            f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB1
timeout = 30

[Bot]
db_path = {db_path.as_posix()}

[Channels]
monitor_channels = #general
""",
            encoding="utf-8",
        )
        success, msg = b.reload_config()
        assert success is False
        assert "serial_port" in msg.lower() or "restart" in msg.lower()

    def test_reload_config_not_found_returns_false(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        b = MeshCoreBot(config_file=str(config_file))
        # Delete the config file
        config_file.unlink()
        success, msg = b.reload_config()
        assert success is False


# ---------------------------------------------------------------------------
# key_prefix / is_valid_prefix
# ---------------------------------------------------------------------------

class TestKeyPrefixHelpers:
    def test_key_prefix_returns_first_n_chars(self, bot):
        assert bot.key_prefix("deadbeef1234") == "de"

    def test_key_prefix_uses_prefix_hex_chars(self, bot):
        bot.prefix_hex_chars = 4
        assert bot.key_prefix("deadbeef1234") == "dead"

    def test_is_valid_prefix_correct_length(self, bot):
        assert bot.is_valid_prefix("de") is True

    def test_is_valid_prefix_wrong_length(self, bot):
        assert bot.is_valid_prefix("d") is False
        assert bot.is_valid_prefix("dead") is False

    def test_prefix_hex_chars_from_config(self, tmp_path):
        config_file = tmp_path / "config2.ini"
        db_path = tmp_path / "bot2.db"
        config_file.write_text(
            f"""[Connection]
connection_type = ble

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 2

[Channels]
monitor_channels = #general
""",
            encoding="utf-8",
        )
        b = MeshCoreBot(config_file=str(config_file))
        assert b.prefix_hex_chars == 4
        assert b.is_valid_prefix("dead") is True
        assert b.is_valid_prefix("de") is False
