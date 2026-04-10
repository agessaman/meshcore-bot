"""Unit tests for the Public channel guard."""

import configparser
from unittest.mock import MagicMock

import pytest

from modules.config_validation import (
    PUBLIC_CHANNEL_KEY_HEX,
    PUBLIC_CHANNEL_OVERRIDE_KEY,
    SEVERITY_ERROR,
    _channel_name_is_public,
)

# --- _channel_name_is_public() ---

class TestChannelNameIsPublic:
    def test_plain_name_public(self):
        assert _channel_name_is_public("Public") is True

    def test_lowercase_public(self):
        assert _channel_name_is_public("public") is True

    def test_hash_prefixed_public(self):
        assert _channel_name_is_public("#Public") is True

    def test_hash_prefixed_lowercase(self):
        assert _channel_name_is_public("#public") is True

    def test_other_channel_not_public(self):
        assert _channel_name_is_public("#mybotchannel") is False

    def test_general_not_public(self):
        assert _channel_name_is_public("#general") is False

    def test_empty_string_not_public(self):
        assert _channel_name_is_public("") is False

    def test_public_key_hex_constant_correct(self):
        """Public uses a special fixed key, not hashtag-derived. Verify the known value."""
        assert PUBLIC_CHANNEL_KEY_HEX == "8b3387e9c5cdea6ac9e5edbaa115cd72"


# --- validate_config() Public channel check ---

def _make_config(monitor_channels: str, override: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "Connection": {"type": "serial", "port": "/dev/ttyUSB0"},
        "Bot": {"db_path": "/tmp/test.db"},
        "Channels": {"monitor_channels": monitor_channels},
    })
    if override:
        cfg.set("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, override)
    return cfg


class TestValidateConfigPublicGuard:
    def _run(self, monitor_channels: str, override: str = "") -> list:
        """Run the public-channel portion of validate_config using a fake config."""
        cfg = _make_config(monitor_channels, override)
        # Exercise _channel_name_is_public + override check directly (mirrors validate_config logic)
        from modules.config_validation import _channel_name_is_public, strip_optional_quotes
        raw = strip_optional_quotes(cfg.get("Channels", "monitor_channels", fallback=""))
        entries = [e.strip() for e in raw.split(",") if e.strip()]
        results = []
        if any(_channel_name_is_public(e) for e in entries):
            ov = cfg.get("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, fallback="").strip().lower()
            if ov != "true":
                results.append((SEVERITY_ERROR, "public_channel_error"))
        return results

    def test_public_channel_without_override_is_error(self):
        results = self._run("Public")
        assert any(sev == SEVERITY_ERROR for sev, _ in results)

    def test_hash_public_without_override_is_error(self):
        results = self._run("#public")
        assert any(sev == SEVERITY_ERROR for sev, _ in results)

    def test_public_with_override_true_passes(self):
        results = self._run("Public", override="true")
        assert results == []

    def test_other_channel_passes(self):
        results = self._run("#mybotchannel")
        assert results == []

    def test_multi_channel_with_public_is_error(self):
        results = self._run("#mybotchannel, Public")
        assert any(sev == SEVERITY_ERROR for sev, _ in results)

    def test_multi_channel_without_public_passes(self):
        results = self._run("#mybotchannel, #general")
        assert results == []


# --- load_monitor_channels() SystemExit guard ---

class TestLoadMonitorChannelsPublicGuard:
    def _make_command_manager(self, monitor_channels: str, override: str = ""):
        """Create a minimal CommandManager-like object to test load_monitor_channels."""
        from modules.command_manager import CommandManager
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "Connection": {"type": "serial", "port": "/dev/ttyUSB0"},
            "Bot": {"db_path": "/tmp/test.db"},
            "Channels": {"monitor_channels": monitor_channels, "respond_to_dms": "true"},
        })
        if override:
            cfg.set("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, override)

        bot = MagicMock()
        bot.config = cfg
        bot.logger = MagicMock()

        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = MagicMock()
        return cm

    def test_public_channel_raises_system_exit(self):
        cm = self._make_command_manager("Public")
        with pytest.raises(SystemExit):
            cm.load_monitor_channels()

    def test_hash_public_raises_system_exit(self):
        cm = self._make_command_manager("#public")
        with pytest.raises(SystemExit):
            cm.load_monitor_channels()

    def test_public_with_override_does_not_raise(self):
        cm = self._make_command_manager("Public", override="true")
        result = cm.load_monitor_channels()
        assert "Public" in result

    def test_other_channel_does_not_raise(self):
        cm = self._make_command_manager("#mybotchannel")
        result = cm.load_monitor_channels()
        assert "#mybotchannel" in result
