"""Tests for RepeaterManager pure logic (no network, no geocoding)."""

import pytest
import configparser
from unittest.mock import Mock, MagicMock, patch

from modules.repeater_manager import RepeaterManager


@pytest.fixture
def bot(mock_logger, test_db):
    """Minimal bot mock for RepeaterManager — uses a real test DB."""
    bot = Mock()
    bot.logger = mock_logger
    bot.db_manager = test_db
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "auto_manage_contacts", "false")
    bot.config.add_section("Companion_Purge")
    bot.config.set("Companion_Purge", "companion_purge_enabled", "false")
    bot.config.set("Companion_Purge", "companion_dm_threshold_days", "30")
    bot.config.set("Companion_Purge", "companion_advert_threshold_days", "30")
    bot.config.set("Companion_Purge", "companion_min_inactive_days", "30")
    bot.meshcore = None
    return bot


@pytest.fixture
def rm(bot):
    """RepeaterManager instance for pure logic tests."""
    return RepeaterManager(bot)


# ---------------------------------------------------------------------------
# _determine_contact_role
# ---------------------------------------------------------------------------

class TestDetermineContactRole:
    """Tests for RepeaterManager._determine_contact_role()."""

    def test_mode_repeater(self, rm):
        assert rm._determine_contact_role({"mode": "Repeater"}) == "repeater"

    def test_mode_roomserver(self, rm):
        assert rm._determine_contact_role({"mode": "RoomServer"}) == "roomserver"

    def test_mode_companion(self, rm):
        assert rm._determine_contact_role({"mode": "Companion"}) == "companion"

    def test_mode_sensor(self, rm):
        assert rm._determine_contact_role({"mode": "Sensor"}) == "sensor"

    def test_mode_unknown_lowercased(self, rm):
        result = rm._determine_contact_role({"mode": "CustomMode"})
        assert result == "custommode"

    def test_device_type_2_returns_repeater(self, rm):
        assert rm._determine_contact_role({"type": 2}) == "repeater"

    def test_device_type_3_returns_roomserver(self, rm):
        assert rm._determine_contact_role({"type": 3}) == "roomserver"

    def test_name_rpt_returns_repeater(self, rm):
        assert rm._determine_contact_role({"name": "My-RPT-01"}) == "repeater"

    def test_name_roomserver_returns_roomserver(self, rm):
        assert rm._determine_contact_role({"name": "Room Server"}) == "roomserver"

    def test_name_sensor_returns_sensor(self, rm):
        assert rm._determine_contact_role({"name": "Weather Sensor"}) == "sensor"

    def test_name_bot_returns_bot(self, rm):
        assert rm._determine_contact_role({"name": "AutomatedBot"}) == "bot"

    def test_name_gateway_returns_gateway(self, rm):
        assert rm._determine_contact_role({"name": "GW-01"}) == "gateway"

    def test_unknown_defaults_to_companion(self, rm):
        assert rm._determine_contact_role({"name": "Alice"}) == "companion"

    def test_empty_contact_defaults_to_companion(self, rm):
        assert rm._determine_contact_role({}) == "companion"


# ---------------------------------------------------------------------------
# _determine_device_type
# ---------------------------------------------------------------------------

class TestDetermineDeviceType:
    """Tests for RepeaterManager._determine_device_type()."""

    def test_advert_data_mode_repeater(self, rm):
        result = rm._determine_device_type(0, "Test", advert_data={"mode": "Repeater"})
        assert result == "Repeater"

    def test_advert_data_mode_roomserver(self, rm):
        result = rm._determine_device_type(0, "Test", advert_data={"mode": "RoomServer"})
        assert result == "RoomServer"

    def test_device_type_1(self, rm):
        assert rm._determine_device_type(1, "Alice") == "Companion"

    def test_device_type_2(self, rm):
        assert rm._determine_device_type(2, "Node") == "Repeater"

    def test_device_type_3(self, rm):
        assert rm._determine_device_type(3, "Node") == "RoomServer"

    def test_name_roomserver(self, rm):
        assert rm._determine_device_type(0, "RoomServer Node") == "RoomServer"

    def test_name_repeater(self, rm):
        assert rm._determine_device_type(0, "RPT-01 repeater") == "Repeater"

    def test_name_sensor(self, rm):
        assert rm._determine_device_type(0, "Weather sens") == "Sensor"

    def test_name_gateway(self, rm):
        assert rm._determine_device_type(0, "MQTT-GW bridge") == "Gateway"

    def test_name_bot(self, rm):
        assert rm._determine_device_type(0, "Automated assistant") == "Bot"

    def test_unknown_defaults_to_companion(self, rm):
        assert rm._determine_device_type(0, "Alice Johnson") == "Companion"


# ---------------------------------------------------------------------------
# _is_repeater_device
# ---------------------------------------------------------------------------

class TestIsRepeaterDevice:
    """Tests for RepeaterManager._is_repeater_device()."""

    def test_type_2_is_repeater(self, rm):
        assert rm._is_repeater_device({"type": 2}) is True

    def test_type_3_is_repeater(self, rm):
        assert rm._is_repeater_device({"type": 3}) is True

    def test_type_1_not_repeater(self, rm):
        assert rm._is_repeater_device({"type": 1}) is False

    def test_role_repeater_field(self, rm):
        assert rm._is_repeater_device({"role": "repeater"}) is True

    def test_role_roomserver_field(self, rm):
        assert rm._is_repeater_device({"device_role": "RoomServer"}) is True

    def test_name_repeater(self, rm):
        assert rm._is_repeater_device({"adv_name": "My Repeater Node"}) is True

    def test_name_gateway(self, rm):
        assert rm._is_repeater_device({"name": "MQTT Gateway"}) is True

    def test_companion_not_repeater(self, rm):
        assert rm._is_repeater_device({"type": 1, "name": "Alice"}) is False

    def test_empty_data_not_repeater(self, rm):
        assert rm._is_repeater_device({}) is False


# ---------------------------------------------------------------------------
# _is_companion_device
# ---------------------------------------------------------------------------

class TestIsCompanionDevice:
    """Tests for RepeaterManager._is_companion_device()."""

    def test_companion_type_1(self, rm):
        assert rm._is_companion_device({"type": 1}) is True

    def test_repeater_type_2_not_companion(self, rm):
        assert rm._is_companion_device({"type": 2}) is False

    def test_empty_is_companion(self, rm):
        assert rm._is_companion_device({}) is True


# ---------------------------------------------------------------------------
# _is_in_acl
# ---------------------------------------------------------------------------

class TestIsInAcl:
    """Tests for RepeaterManager._is_in_acl()."""

    def test_no_acl_section_returns_false(self, rm):
        assert rm._is_in_acl("deadbeef") is False

    def test_key_in_acl(self, rm):
        rm.bot.config.add_section("Admin_ACL")
        rm.bot.config.set("Admin_ACL", "admin_pubkeys", "deadbeef,cafebabe")
        assert rm._is_in_acl("deadbeef") is True

    def test_key_not_in_acl(self, rm):
        rm.bot.config.add_section("Admin_ACL")
        rm.bot.config.set("Admin_ACL", "admin_pubkeys", "deadbeef")
        assert rm._is_in_acl("cafebabe") is False

    def test_empty_acl_list_returns_false(self, rm):
        rm.bot.config.add_section("Admin_ACL")
        rm.bot.config.set("Admin_ACL", "admin_pubkeys", "")
        assert rm._is_in_acl("deadbeef") is False

    def test_exact_match_required(self, rm):
        """Partial key match should not succeed."""
        rm.bot.config.add_section("Admin_ACL")
        rm.bot.config.set("Admin_ACL", "admin_pubkeys", "deadbeef00112233")
        assert rm._is_in_acl("deadbeef") is False

    def test_auto_purge_disabled_by_default(self, rm):
        assert rm.auto_purge_enabled is False

    def test_auto_purge_enabled_when_set(self, bot):
        bot.config.set("Bot", "auto_manage_contacts", "device")
        rm2 = RepeaterManager(bot)
        assert rm2.auto_purge_enabled is True
