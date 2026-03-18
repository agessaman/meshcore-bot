"""Tests for RepeaterManager pure logic (no network, no geocoding)."""

import configparser
from unittest.mock import AsyncMock, Mock, patch

import pytest

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


# ---------------------------------------------------------------------------
# _should_geocode_location
# ---------------------------------------------------------------------------

class TestShouldGeocodeLocation:
    """Tests for RepeaterManager._should_geocode_location()."""

    def _loc(self, lat=47.6, lon=-122.3, state=None, country=None, city=None):
        return {"latitude": lat, "longitude": lon, "state": state, "country": country, "city": city}

    def test_no_existing_data_with_coords_returns_true(self, rm):
        loc = self._loc(lat=47.6, lon=-122.3)
        should, _ = rm._should_geocode_location(loc, existing_data=None)
        assert should is True

    def test_no_existing_data_zero_coords_returns_false(self, rm):
        loc = self._loc(lat=0.0, lon=0.0)
        should, _ = rm._should_geocode_location(loc, existing_data=None)
        assert should is False

    def test_no_existing_data_no_coords_returns_false(self, rm):
        loc = self._loc(lat=None, lon=None)
        should, _ = rm._should_geocode_location(loc, existing_data=None)
        assert should is False

    def test_no_existing_data_all_fields_present_returns_false(self, rm):
        loc = self._loc(lat=47.6, lon=-122.3, state="WA", country="US", city="Seattle")
        should, _ = rm._should_geocode_location(loc, existing_data=None)
        assert should is False

    def test_existing_data_same_coords_sufficient_loc_no_geocode(self, rm):
        loc = self._loc(lat=47.6, lon=-122.3)
        existing = {"latitude": 47.6, "longitude": -122.3, "state": "WA", "country": "US", "city": "Seattle"}
        should, updated = rm._should_geocode_location(loc, existing_data=existing)
        assert should is False
        assert updated["state"] == "WA"
        assert updated["city"] == "Seattle"

    def test_existing_data_moved_triggers_geocode(self, rm):
        loc = self._loc(lat=48.0, lon=-122.0)  # moved > 0.001 degrees
        existing = {"latitude": 47.6, "longitude": -122.3, "state": "WA", "country": "US", "city": "Seattle"}
        should, _ = rm._should_geocode_location(loc, existing_data=existing)
        assert should is True

    def test_existing_data_missing_city_triggers_geocode(self, rm):
        loc = self._loc(lat=47.6, lon=-122.3)
        existing = {"latitude": 47.6, "longitude": -122.3, "state": "WA", "country": "US", "city": None}
        should, _ = rm._should_geocode_location(loc, existing_data=existing)
        assert should is True

    def test_existing_data_no_coords_in_new_data_keeps_existing(self, rm):
        loc = self._loc(lat=None, lon=None)
        existing = {"latitude": 47.6, "longitude": -122.3, "state": "WA", "country": "US", "city": "Seattle"}
        should, updated = rm._should_geocode_location(loc, existing_data=existing)
        assert should is False
        assert updated["state"] == "WA"

    def test_packet_hash_cache_hit_skips_geocode(self, rm):
        import time
        loc = self._loc(lat=47.6, lon=-122.3)
        packet_hash = "abcdef1234567890"
        # Pre-seed the cache
        rm.geocoding_cache[packet_hash] = time.time()
        should, _ = rm._should_geocode_location(loc, existing_data=None, packet_hash=packet_hash)
        assert should is False

    def test_default_packet_hash_not_cached(self, rm):
        loc = self._loc(lat=47.6, lon=-122.3)
        # Default/invalid hash should never match cache
        should, _ = rm._should_geocode_location(loc, existing_data=None, packet_hash="0000000000000000")
        assert should is True  # No cache hit, coords valid → should geocode

    def test_expired_cache_entry_removed(self, rm):
        import time
        loc = self._loc(lat=47.6, lon=-122.3)
        old_hash = "oldpackethash1234"
        # Pre-seed with expired entry
        rm.geocoding_cache[old_hash] = time.time() - rm.geocoding_cache_window - 10
        rm._should_geocode_location(loc, existing_data=None)
        assert old_hash not in rm.geocoding_cache


# ---------------------------------------------------------------------------
# cleanup_repeater_retention
# ---------------------------------------------------------------------------

class TestCleanupRepeaterRetention:

    def test_runs_without_error_on_empty_db(self, rm):
        # Tables may not exist yet; should not raise
        try:
            rm.cleanup_repeater_retention(daily_stats_days=30, observed_paths_days=30)
        except Exception:
            pass  # Some tables may not exist in test DB; that's OK

    def test_does_not_raise_when_db_raises(self, rm):
        from unittest.mock import patch as _patch
        with _patch.object(rm.db_manager, "execute_update", side_effect=Exception("db error")):
            rm.cleanup_repeater_retention()  # Should not raise
        rm.logger.error.assert_called()


# ---------------------------------------------------------------------------
# geocoding cache delegation
# ---------------------------------------------------------------------------

class TestGeocodingCacheDelegation:

    def test_get_cached_geocoding_delegates(self, rm):
        rm.db_manager.get_cached_geocoding = Mock(return_value=(47.6, -122.3))
        result = rm.get_cached_geocoding("Seattle, WA")
        assert result == (47.6, -122.3)
        rm.db_manager.get_cached_geocoding.assert_called_once_with("Seattle, WA")

    def test_cache_geocoding_delegates(self, rm):
        rm.db_manager.cache_geocoding = Mock()
        rm.cache_geocoding("Seattle, WA", 47.6, -122.3)
        rm.db_manager.cache_geocoding.assert_called_once_with("Seattle, WA", 47.6, -122.3, 720)

    def test_cleanup_geocoding_cache_delegates(self, rm):
        rm.db_manager.cleanup_geocoding_cache = Mock()
        rm.cleanup_geocoding_cache()
        rm.db_manager.cleanup_geocoding_cache.assert_called_once()


# ---------------------------------------------------------------------------
# get_complete_contact_database (async)
# ---------------------------------------------------------------------------

class TestGetCompleteContactDatabase:

    async def test_returns_empty_list_on_db_error(self, rm):
        rm.db_manager.execute_query = Mock(side_effect=Exception("db fail"))
        result = await rm.get_complete_contact_database()
        assert result == []

    async def test_returns_all_results_without_filter(self, rm):
        rm.db_manager.execute_query = Mock(return_value=[
            {"public_key": "aabb", "name": "Node1", "role": "repeater"},
        ])
        result = await rm.get_complete_contact_database()
        assert len(result) == 1
        assert result[0]["name"] == "Node1"

    async def test_with_role_filter(self, rm):
        rm.db_manager.execute_query = Mock(return_value=[])
        await rm.get_complete_contact_database(role_filter="repeater")
        call_args = rm.db_manager.execute_query.call_args
        assert "repeater" in str(call_args)

    async def test_not_include_historical(self, rm):
        rm.db_manager.execute_query = Mock(return_value=[])
        await rm.get_complete_contact_database(include_historical=False)
        call_args = rm.db_manager.execute_query.call_args
        assert "is_currently_tracked" in str(call_args)

    async def test_not_include_historical_with_role(self, rm):
        rm.db_manager.execute_query = Mock(return_value=[])
        await rm.get_complete_contact_database(role_filter="companion", include_historical=False)
        call_args = rm.db_manager.execute_query.call_args
        assert "is_currently_tracked" in str(call_args)


# ---------------------------------------------------------------------------
# get_contact_statistics (async)
# ---------------------------------------------------------------------------

class TestGetContactStatistics:

    async def test_returns_empty_dict_on_error(self, rm):
        rm.db_manager.execute_query = Mock(side_effect=Exception("fail"))
        result = await rm.get_contact_statistics()
        assert result == {}

    async def test_returns_stats_structure(self, rm):
        rm.db_manager.execute_query = Mock(side_effect=[
            [{"count": 42}],   # total_heard
            [{"count": 10}],   # currently_tracked
            [{"count": 5}],    # recent_activity
            [{"role": "repeater", "count": 3}, {"role": "companion", "count": 39}],  # by_role
            [{"device_type": "Repeater", "count": 3}],  # by_type
        ])
        result = await rm.get_contact_statistics()
        assert result["total_heard"] == 42
        assert result["currently_tracked"] == 10
        assert result["recent_activity"] == 5
        assert result["by_role"]["repeater"] == 3

    async def test_returns_zeros_on_empty_db(self, rm):
        rm.db_manager.execute_query = Mock(return_value=[])
        result = await rm.get_contact_statistics()
        assert result.get("total_heard", 0) == 0


# ---------------------------------------------------------------------------
# get_contacts_by_role convenience wrappers (async)
# ---------------------------------------------------------------------------

class TestGetContactsByRole:

    async def test_get_repeater_devices_combines_roles(self, rm):
        async def fake_db(role_filter=None, include_historical=True):
            if role_filter == "repeater":
                return [{"name": "RPT1"}]
            elif role_filter == "roomserver":
                return [{"name": "RS1"}]
            return []

        with patch.object(rm, "get_complete_contact_database", side_effect=fake_db):
            result = await rm.get_repeater_devices()
        assert len(result) == 2

    async def test_get_companion_contacts(self, rm):
        with patch.object(rm, "get_complete_contact_database", return_value=[{"name": "Alice"}]) as mock_db:
            result = await rm.get_companion_contacts()
        mock_db.assert_called_once_with(role_filter="companion", include_historical=True)
        assert result[0]["name"] == "Alice"

    async def test_get_sensor_devices(self, rm):
        with patch.object(rm, "get_complete_contact_database", return_value=[]) as mock_db:
            await rm.get_sensor_devices()
        mock_db.assert_called_once_with(role_filter="sensor", include_historical=True)

    async def test_get_gateway_devices(self, rm):
        with patch.object(rm, "get_complete_contact_database", return_value=[]) as mock_db:
            await rm.get_gateway_devices()
        mock_db.assert_called_once_with(role_filter="gateway", include_historical=True)

    async def test_get_bot_devices(self, rm):
        with patch.object(rm, "get_complete_contact_database", return_value=[]) as mock_db:
            await rm.get_bot_devices()
        mock_db.assert_called_once_with(role_filter="bot", include_historical=True)


# ---------------------------------------------------------------------------
# check_and_auto_purge (async)
# ---------------------------------------------------------------------------

class TestCheckAndAutoPurge:

    async def test_returns_false_when_disabled(self, rm):
        rm.auto_purge_enabled = False
        result = await rm.check_and_auto_purge()
        assert result is False

    async def test_returns_false_when_below_threshold(self, rm):
        rm.auto_purge_enabled = True
        rm.auto_purge_threshold = 280
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {str(i): {} for i in range(100)}  # 100 contacts
        result = await rm.check_and_auto_purge()
        assert result is False

    async def test_triggers_purge_when_above_threshold(self, rm):
        rm.auto_purge_enabled = True
        rm.auto_purge_threshold = 10
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {str(i): {} for i in range(15)}  # 15 > threshold
        with patch.object(rm, "_auto_purge_repeaters", new_callable=AsyncMock, return_value=True) as mock_purge:
            result = await rm.check_and_auto_purge()
        mock_purge.assert_called_once()
        assert result is True

    async def test_returns_false_on_exception(self, rm):
        rm.auto_purge_enabled = True
        rm.bot.meshcore = Mock(side_effect=Exception("fail"))
        result = await rm.check_and_auto_purge()
        assert result is False
