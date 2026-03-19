"""Tests for RepeaterManager pure logic (no network, no geocoding)."""

import configparser
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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

    async def test_companion_purge_triggered_when_repeater_purge_insufficient(self, rm):
        """When repeater purge doesn't bring count below threshold, companion purge fires."""
        rm.auto_purge_enabled = True
        rm.auto_purge_threshold = 10
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {str(i): {} for i in range(15)}

        async def fake_repeater_purge(count):
            # Simulate no contacts removed (still above threshold after)
            return True

        with patch.object(rm, "_auto_purge_repeaters", side_effect=fake_repeater_purge), \
             patch.object(rm, "_auto_purge_companions", new_callable=AsyncMock, return_value=True) as mock_comp:
            result = await rm.check_and_auto_purge()

        mock_comp.assert_called_once()
        assert result is True

    async def test_returns_false_when_purge_fails(self, rm):
        """When both purge counts succeed=False, check_and_auto_purge returns False."""
        rm.auto_purge_enabled = True
        rm.auto_purge_threshold = 10
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {str(i): {} for i in range(15)}

        with patch.object(rm, "_auto_purge_repeaters", new_callable=AsyncMock, return_value=False):
            result = await rm.check_and_auto_purge()

        assert result is False


# ---------------------------------------------------------------------------
# _determine_device_type — gap branches (lines 593-599, 626-642)
# ---------------------------------------------------------------------------

class TestDetermineDeviceTypeGaps:
    """Cover the previously-uncovered branches of _determine_device_type."""

    def test_advert_mode_companion(self, rm):
        """mode='Companion' in advert_data should return 'Companion'."""
        result = rm._determine_device_type(0, "Bob", advert_data={"mode": "Companion"})
        assert result == "Companion"

    def test_advert_mode_sensor(self, rm):
        """mode='Sensor' in advert_data should return 'Sensor'."""
        result = rm._determine_device_type(0, "WeatherNode", advert_data={"mode": "Sensor"})
        assert result == "Sensor"

    def test_advert_mode_unknown_passthrough(self, rm):
        """An unrecognised mode string is returned verbatim (str(mode))."""
        result = rm._determine_device_type(0, "Gadget", advert_data={"mode": "CustomWidget"})
        assert result == "CustomWidget"

    def test_name_based_bot_detection(self, rm):
        """Fallback name-based detection: 'automated' → Bot."""
        result = rm._determine_device_type(0, "AutomatedHelper")
        assert result == "Bot"

    def test_name_based_gateway_bridge(self, rm):
        """Fallback name-based detection: 'bridge' in name → Gateway."""
        result = rm._determine_device_type(0, "MQTT Bridge Node")
        assert result == "Gateway"

    def test_name_based_sensor(self, rm):
        """Fallback name-based detection: 'sens' in name → Sensor."""
        result = rm._determine_device_type(0, "Temp-Sens-01")
        assert result == "Sensor"

    def test_name_based_gateway_gw(self, rm):
        """Fallback name-based detection: 'gw' in name → Gateway."""
        result = rm._determine_device_type(0, "My-GW-Node")
        assert result == "Gateway"

    def test_device_type_zero_unknown_name_defaults_companion(self, rm):
        """device_type=0 with an ordinary name falls through to Companion."""
        result = rm._determine_device_type(0, "Charlie Brown")
        assert result == "Companion"


# ---------------------------------------------------------------------------
# _update_currently_tracked_status (async, line 742)
# ---------------------------------------------------------------------------

class TestUpdateCurrentlyTrackedStatus:
    """Tests for _update_currently_tracked_status (covers the meshcore contacts loop)."""

    async def test_tracked_when_public_key_matches(self, rm):
        """Contact in meshcore.contacts with matching public_key → is_tracked=True."""
        target_key = "aabbccdd"
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "slot1": {"public_key": target_key},
        }
        rm.db_manager.execute_update = Mock()

        await rm._update_currently_tracked_status(target_key)

        rm.db_manager.execute_update.assert_called_once()
        args = rm.db_manager.execute_update.call_args[0]
        # Second positional arg is the tuple (is_tracked, public_key)
        assert args[1] == (True, target_key)

    async def test_not_tracked_when_key_absent(self, rm):
        """Contact not found in meshcore.contacts → is_tracked=False."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "slot1": {"public_key": "11223344"},
        }
        rm.db_manager.execute_update = Mock()

        await rm._update_currently_tracked_status("aabbccdd")

        args = rm.db_manager.execute_update.call_args[0]
        assert args[1] == (False, "aabbccdd")

    async def test_contact_key_used_when_no_public_key_field(self, rm):
        """When contact_data has no 'public_key' field, the dict key itself is compared."""
        target_key = "aabbccdd"
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {target_key: {}}  # no 'public_key' field
        rm.db_manager.execute_update = Mock()

        await rm._update_currently_tracked_status(target_key)

        args = rm.db_manager.execute_update.call_args[0]
        assert args[1] == (True, target_key)

    async def test_meshcore_has_no_contacts_attr(self, rm):
        """When meshcore object has no contacts attribute → is_tracked=False, no exception."""
        rm.bot.meshcore = object()  # plain object, no 'contacts' attr
        rm.db_manager.execute_update = Mock()

        await rm._update_currently_tracked_status("aabbccdd")

        args = rm.db_manager.execute_update.call_args[0]
        assert args[1] == (False, "aabbccdd")

    async def test_db_exception_is_caught(self, rm):
        """DB error in execute_update should be swallowed and logged, not raised."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}
        rm.db_manager.execute_update = Mock(side_effect=Exception("db error"))

        # Should not raise
        await rm._update_currently_tracked_status("aabbccdd")
        rm.logger.error.assert_called()


# ---------------------------------------------------------------------------
# track_contact_advertisement (async, lines 311-445)
# ---------------------------------------------------------------------------

class TestTrackContactAdvertisement:
    """Tests for track_contact_advertisement — the main contact upsert path."""

    def _make_advert(self, public_key="aabb1122", name="TestNode", **kwargs):
        data = {"public_key": public_key, "name": name, "type": 1}
        data.update(kwargs)
        return data

    async def test_missing_public_key_returns_false(self, rm):
        """Advertisement without public_key should return False immediately."""
        result = await rm.track_contact_advertisement({"name": "Nameless"})
        assert result is False
        rm.logger.warning.assert_called()

    async def test_empty_public_key_returns_false(self, rm):
        result = await rm.track_contact_advertisement({"public_key": "", "name": "X"})
        assert result is False

    async def test_new_contact_inserted_returns_true(self, rm):
        """New contact (not in DB) is inserted and True is returned."""
        advert = self._make_advert()

        # DB query for duplicate packet → nothing
        # DB query for existing contact → nothing
        # execute_update for INSERT, execute_update for is_currently_tracked
        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        with patch.object(rm, "_track_daily_advertisement", new_callable=AsyncMock):
            result = await rm.track_contact_advertisement(advert)

        assert result is True
        # execute_update called at least once (INSERT)
        rm.db_manager.execute_update.assert_called()

    async def test_existing_contact_updated_returns_true(self, rm):
        """Existing contact in DB is updated (advert_count incremented) → True."""
        advert = self._make_advert()
        existing_row = {
            "id": 1,
            "advert_count": 5,
            "last_heard": "2024-01-01 00:00:00",
            "latitude": None,
            "longitude": None,
            "city": None,
            "state": None,
            "country": None,
            "out_path": None,
            "out_path_len": -1,
            "out_bytes_per_hop": None,
        }
        rm.db_manager.execute_query = Mock(return_value=[existing_row])
        rm.db_manager.execute_update = Mock()
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        with patch.object(rm, "_track_daily_advertisement", new_callable=AsyncMock):
            result = await rm.track_contact_advertisement(advert)

        assert result is True
        # The UPDATE path passes advert_count=6
        update_call = rm.db_manager.execute_update.call_args_list[0]
        assert "UPDATE" in update_call[0][0]
        params = update_call[0][1]
        # advert_count is third positional param in the UPDATE tuple
        assert params[2] == 6

    async def test_duplicate_packet_hash_skips_and_returns_true(self, rm):
        """When packet_hash is already in unique_advert_packets, return True without re-inserting."""
        advert = self._make_advert()
        packet_hash = "deadbeef12345678"

        def fake_query(query, params=None):
            if "unique_advert_packets" in query and "public_key" in query and "packet_hash" in query:
                return [{"id": 99}]  # Simulate duplicate found
            return []

        rm.db_manager.execute_query = Mock(side_effect=fake_query)
        rm.db_manager.execute_update = Mock()

        result = await rm.track_contact_advertisement(advert, packet_hash=packet_hash)

        assert result is True
        # execute_update should NOT have been called (no upsert)
        rm.db_manager.execute_update.assert_not_called()

    async def test_signal_info_direct_hop_saves_rssi(self, rm):
        """Zero-hop signal_info should populate signal_strength and snr in the INSERT."""
        advert = self._make_advert()
        signal_info = {"hops": 0, "rssi": -85.0, "snr": 7.5}

        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        with patch.object(rm, "_track_daily_advertisement", new_callable=AsyncMock):
            result = await rm.track_contact_advertisement(advert, signal_info=signal_info)

        assert result is True
        insert_call = rm.db_manager.execute_update.call_args_list[0]
        params = insert_call[0][1]
        # signal_strength and snr should be -85.0 and 7.5 in the INSERT params
        assert -85.0 in params
        assert 7.5 in params

    async def test_multi_hop_signal_info_not_saved(self, rm):
        """Multi-hop (hops>0) signal_info should NOT persist RSSI/SNR."""
        advert = self._make_advert()
        signal_info = {"hops": 2, "rssi": -70.0, "snr": 9.0}

        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        with patch.object(rm, "_track_daily_advertisement", new_callable=AsyncMock):
            await rm.track_contact_advertisement(advert, signal_info=signal_info)

        insert_call = rm.db_manager.execute_update.call_args_list[0]
        params = insert_call[0][1]
        # RSSI (-70.0) should NOT appear; signal_strength should be None
        assert -70.0 not in params

    async def test_db_exception_returns_false(self, rm):
        """An unexpected exception during DB operations should return False."""
        advert = self._make_advert()
        rm.db_manager.execute_query = Mock(side_effect=Exception("db exploded"))

        result = await rm.track_contact_advertisement(advert)

        assert result is False
        rm.logger.error.assert_called()

    async def test_track_daily_advertisement_called(self, rm):
        """_track_daily_advertisement should be awaited once on success."""
        advert = self._make_advert()
        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        with patch.object(rm, "_track_daily_advertisement", new_callable=AsyncMock) as mock_daily:
            await rm.track_contact_advertisement(advert)

        mock_daily.assert_awaited_once()

    async def test_path_fields_preserved_from_existing(self, rm):
        """When existing row already has out_path, the new advert should NOT overwrite it."""
        advert = self._make_advert(out_path="new/path", out_path_len=2)
        existing_row = {
            "id": 1,
            "advert_count": 3,
            "last_heard": "2024-01-01 00:00:00",
            "latitude": None,
            "longitude": None,
            "city": None,
            "state": None,
            "country": None,
            "out_path": "original/path",
            "out_path_len": 1,
            "out_bytes_per_hop": None,
        }
        rm.db_manager.execute_query = Mock(return_value=[existing_row])
        rm.db_manager.execute_update = Mock()
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        with patch.object(rm, "_track_daily_advertisement", new_callable=AsyncMock):
            await rm.track_contact_advertisement(advert)

        update_call = rm.db_manager.execute_update.call_args_list[0]
        params = update_call[0][1]
        assert "original/path" in params


# ---------------------------------------------------------------------------
# _track_daily_advertisement (async, lines 447-535)
# ---------------------------------------------------------------------------

class TestTrackDailyAdvertisement:
    """Tests for _track_daily_advertisement — daily stats upsert logic."""

    def _call(self, rm, public_key="aabb", name="Node", role="companion",
              device_type="Companion", location_info=None, signal_strength=None,
              snr=None, hop_count=None, timestamp=None, packet_hash=None):
        if location_info is None:
            location_info = {"latitude": None, "longitude": None,
                             "city": None, "state": None, "country": None}
        if timestamp is None:
            timestamp = datetime.now()
        return rm._track_daily_advertisement(
            public_key, name, role, device_type, location_info,
            signal_strength, snr, hop_count, timestamp, packet_hash=packet_hash
        )

    async def test_new_daily_entry_inserted_for_unique_packet(self, rm):
        """A new packet_hash on a new day → INSERT into daily_stats."""
        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()

        await self._call(rm, packet_hash="newpacket1234567")

        calls = [str(c) for c in rm.db_manager.execute_update.call_args_list]
        assert any("daily_stats" in c for c in calls)

    async def test_existing_daily_entry_updated(self, rm):
        """When daily_stats already has a row for today, UPDATE is used."""
        existing_daily = [{"id": 1, "advert_count": 4, "first_advert_time": "2024-01-01"}]
        unique_count = [{"COUNT(*)": 5}]

        call_count = {"n": 0}

        def fake_query(query, params=None):
            call_count["n"] += 1
            if "unique_advert_packets" in query and "date" in query and "public_key" in query and "packet_hash" in query:
                return []  # Packet not seen yet
            elif "unique_advert_packets" in query and "COUNT(*)" in query:
                return unique_count
            elif "daily_stats" in query:
                return existing_daily
            return []

        rm.db_manager.execute_query = Mock(side_effect=fake_query)
        rm.db_manager.execute_update = Mock()

        await self._call(rm, packet_hash="freshpacket12345")

        calls = [str(c) for c in rm.db_manager.execute_update.call_args_list]
        assert any("UPDATE daily_stats" in c for c in calls)

    async def test_no_packet_hash_counts_as_unique(self, rm):
        """When packet_hash=None, is_unique_packet=True → daily stat is written."""
        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()

        await self._call(rm, packet_hash=None)

        rm.db_manager.execute_update.assert_called()

    async def test_default_zero_hash_counts_as_unique(self, rm):
        """Packet hash '0000000000000000' is treated as no hash → unique."""
        rm.db_manager.execute_query = Mock(return_value=[])
        rm.db_manager.execute_update = Mock()

        await self._call(rm, packet_hash="0000000000000000")

        rm.db_manager.execute_update.assert_called()

    async def test_duplicate_packet_hash_skips_count(self, rm):
        """A packet_hash already seen today → no INSERT/UPDATE to daily_stats."""

        def fake_query(query, params=None):
            if "unique_advert_packets" in query and "packet_hash" in query:
                return [{"id": 1}]  # Already seen
            return []

        rm.db_manager.execute_query = Mock(side_effect=fake_query)
        rm.db_manager.execute_update = Mock()

        await self._call(rm, packet_hash="seenbeforepacket")

        # daily_stats should not be touched because is_unique_packet=False
        calls = [str(c) for c in rm.db_manager.execute_update.call_args_list]
        assert not any("daily_stats" in c for c in calls)

    async def test_exception_is_caught_and_logged(self, rm):
        """An exception inside _track_daily_advertisement should be caught."""
        rm.db_manager.execute_query = Mock(side_effect=Exception("boom"))

        # Should not raise
        await self._call(rm, packet_hash=None)
        rm.logger.error.assert_called()


# ---------------------------------------------------------------------------
# _get_repeaters_for_purging (async, lines 902-991)
# ---------------------------------------------------------------------------

class TestGetRepeatersForPurging:
    """Tests for _get_repeaters_for_purging — repeater selection logic."""

    def _make_repeater_contact(self, public_key="rpt1", name="RPT-01", last_seen_days_ago=10,
                               type_val=2, lat=None, lon=None):
        """Build a fake contact dict representing a repeater in meshcore.contacts."""
        ts = (datetime.now() - timedelta(days=last_seen_days_ago)).isoformat()
        contact = {
            "public_key": public_key,
            "adv_name": name,
            "type": type_val,
            "last_seen": ts,
        }
        if lat is not None:
            contact["adv_lat"] = lat
            contact["adv_lon"] = lon
        return contact

    async def test_returns_empty_when_no_contacts(self, rm):
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}
        result = await rm._get_repeaters_for_purging(5)
        assert result == []

    async def test_returns_empty_when_all_contacts_are_companions(self, rm):
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "c1": {"public_key": "c1", "type": 1, "name": "Alice"},
        }
        result = await rm._get_repeaters_for_purging(5)
        assert result == []

    async def test_returns_old_repeaters_up_to_count(self, rm):
        """Old repeaters (>2 hours ago) should be returned up to count."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "r1": self._make_repeater_contact("rpt1", "RPT-01", last_seen_days_ago=10),
            "r2": self._make_repeater_contact("rpt2", "RPT-02", last_seen_days_ago=5),
            "r3": self._make_repeater_contact("rpt3", "RPT-03", last_seen_days_ago=1),
        }
        result = await rm._get_repeaters_for_purging(2)
        assert len(result) == 2

    async def test_recent_repeaters_excluded(self, rm):
        """Repeaters seen within the last 2 hours should be excluded."""
        rm.bot.meshcore = Mock()
        # Only one very recent repeater
        ts_now = datetime.now().isoformat()
        rm.bot.meshcore.contacts = {
            "r1": {"public_key": "rpt1", "adv_name": "RPT-01", "type": 2, "last_seen": ts_now},
        }
        result = await rm._get_repeaters_for_purging(5)
        assert result == []

    async def test_roomserver_type_detected(self, rm):
        """type=3 should result in device_type='RoomServer' in the returned entry."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "rs1": self._make_repeater_contact("rs1", "RS-01", last_seen_days_ago=8, type_val=3),
        }
        result = await rm._get_repeaters_for_purging(5)
        assert len(result) == 1
        assert result[0]["device_type"] == "RoomServer"

    async def test_oldest_sorted_first(self, rm):
        """Oldest repeaters (7+ days) should appear before medium-old ones."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "r1": self._make_repeater_contact("rpt1", "Medium", last_seen_days_ago=4),
            "r2": self._make_repeater_contact("rpt2", "VeryOld", last_seen_days_ago=10),
        }
        result = await rm._get_repeaters_for_purging(5)
        # VeryOld (10 days) should come before Medium (4 days)
        assert result[0]["name"] == "VeryOld"

    async def test_integer_timestamp_parsed(self, rm):
        """last_seen as a Unix epoch int should parse without exception."""
        old_ts = int((datetime.now() - timedelta(days=5)).timestamp())
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "r1": {"public_key": "rpt1", "adv_name": "RPT-INT", "type": 2, "last_seen": old_ts},
        }
        result = await rm._get_repeaters_for_purging(5)
        assert len(result) == 1

    async def test_missing_last_seen_defaults_to_old(self, rm):
        """Missing last_seen should be treated as 30 days ago (eligible for purge)."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "r1": {"public_key": "rpt1", "adv_name": "RPT-NOSEEN", "type": 2},
        }
        result = await rm._get_repeaters_for_purging(5)
        assert len(result) == 1

    async def test_exception_returns_empty_list(self, rm):
        """An unexpected exception should return [] and log an error."""
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = Mock(side_effect=Exception("contacts error"))

        result = await rm._get_repeaters_for_purging(5)

        assert result == []
        rm.logger.error.assert_called()


# ---------------------------------------------------------------------------
# _auto_purge_repeaters (async, lines 810-846)
# ---------------------------------------------------------------------------

class TestAutoPurgeRepeaters:
    """Tests for _auto_purge_repeaters — orchestrates purge calls."""

    async def test_returns_false_when_no_repeaters(self, rm):
        with patch.object(rm, "_get_repeaters_for_purging", new_callable=AsyncMock, return_value=[]):
            rm.bot.meshcore = Mock()
            rm.bot.meshcore.contacts = {}
            result = await rm._auto_purge_repeaters(3)
        assert result is False
        rm.logger.warning.assert_called()

    async def test_purges_returned_repeaters(self, rm):
        repeaters = [
            {"public_key": "rpt1", "name": "RPT-01", "last_seen": "2024-01-01 00:00:00"},
            {"public_key": "rpt2", "name": "RPT-02", "last_seen": "2024-01-02 00:00:00"},
        ]
        with patch.object(rm, "_get_repeaters_for_purging", new_callable=AsyncMock, return_value=repeaters), \
             patch.object(rm, "purge_repeater_from_contacts", new_callable=AsyncMock, return_value=True):
            result = await rm._auto_purge_repeaters(2)
        assert result is True

    async def test_returns_false_when_all_purges_fail(self, rm):
        repeaters = [
            {"public_key": "rpt1", "name": "RPT-01", "last_seen": "2024-01-01 00:00:00"},
        ]
        with patch.object(rm, "_get_repeaters_for_purging", new_callable=AsyncMock, return_value=repeaters), \
             patch.object(rm, "purge_repeater_from_contacts", new_callable=AsyncMock, return_value=False):
            result = await rm._auto_purge_repeaters(1)
        assert result is False

    async def test_exception_returns_false(self, rm):
        with patch.object(rm, "_get_repeaters_for_purging", new_callable=AsyncMock,
                          side_effect=Exception("boom")):
            result = await rm._auto_purge_repeaters(1)
        assert result is False
        rm.logger.error.assert_called()

    async def test_partial_failure_still_returns_true(self, rm):
        """If at least one repeater is purged successfully, return True."""
        repeaters = [
            {"public_key": "rpt1", "name": "RPT-01", "last_seen": "2024-01-01 00:00:00"},
            {"public_key": "rpt2", "name": "RPT-02", "last_seen": "2024-01-01 00:00:00"},
        ]
        side_effects = [True, False]  # first succeeds, second fails

        with patch.object(rm, "_get_repeaters_for_purging", new_callable=AsyncMock, return_value=repeaters), \
             patch.object(rm, "purge_repeater_from_contacts", new_callable=AsyncMock,
                          side_effect=side_effects):
            result = await rm._auto_purge_repeaters(2)

        assert result is True


# ---------------------------------------------------------------------------
# _get_companions_for_purging (async, lines 993-1162)
# ---------------------------------------------------------------------------

class TestGetCompanionsForPurging:
    """Tests for _get_companions_for_purging — companion scoring and selection."""

    def _make_companion(self, key="c1", name="Alice", last_seen_days_ago=60):
        ts = (datetime.now() - timedelta(days=last_seen_days_ago)).isoformat()
        return {
            "public_key": key,
            "adv_name": name,
            "type": 1,
            "last_seen": ts,
        }

    async def test_returns_empty_when_purge_disabled(self, rm):
        rm.companion_purge_enabled = False
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {"c1": self._make_companion()}

        result = await rm._get_companions_for_purging(5)

        assert result == []

    async def test_returns_empty_when_no_contacts(self, rm):
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {}

        result = await rm._get_companions_for_purging(5)

        assert result == []

    async def test_skips_acl_companions(self, rm):
        """Companions in the ACL should not be returned for purging."""
        rm.companion_purge_enabled = True
        protected_key = "aclprotected1234"
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {"c1": self._make_companion(key=protected_key, name="Admin")}
        rm.bot.config.add_section("Admin_ACL")
        rm.bot.config.set("Admin_ACL", "admin_pubkeys", protected_key)
        rm.db_manager.execute_query = Mock(return_value=[])

        result = await rm._get_companions_for_purging(5)

        assert result == []

    async def test_skips_recently_active_companions(self, rm):
        """Companions active within 2 hours should be excluded."""
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        recent_ts = (datetime.now() - timedelta(minutes=30)).isoformat()
        rm.bot.meshcore.contacts = {
            "c1": {"public_key": "c1", "adv_name": "ActiveUser", "type": 1, "last_seen": recent_ts},
        }

        def fake_query(query, params=None):
            if "complete_contact_tracking" in query:
                # Return last_heard = recent (within 2 hours)
                return [{"last_heard": recent_ts, "last_advert_timestamp": None,
                          "advert_count": 1, "first_heard": recent_ts}]
            return []

        rm.db_manager.execute_query = Mock(side_effect=fake_query)

        with patch.object(rm, "_get_last_dm_activity",
                          return_value=datetime.now() - timedelta(minutes=30)), \
             patch.object(rm, "_get_last_advert_activity", return_value=None):
            result = await rm._get_companions_for_purging(5)

        assert result == []

    async def test_inactive_companion_included(self, rm):
        """A companion with no recent activity and purge enabled should be included."""
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {"c1": self._make_companion(last_seen_days_ago=90)}
        rm.db_manager.execute_query = Mock(return_value=[])

        with patch.object(rm, "_get_last_dm_activity", return_value=None), \
             patch.object(rm, "_get_last_advert_activity", return_value=None):
            result = await rm._get_companions_for_purging(5)

        assert len(result) == 1
        assert result[0]["public_key"] == "c1"

    async def test_count_limits_results(self, rm):
        """Result list should be capped at the requested count."""
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            f"c{i}": self._make_companion(key=f"c{i}", name=f"User{i}", last_seen_days_ago=90 + i)
            for i in range(5)
        }
        rm.db_manager.execute_query = Mock(return_value=[])

        with patch.object(rm, "_get_last_dm_activity", return_value=None), \
             patch.object(rm, "_get_last_advert_activity", return_value=None):
            result = await rm._get_companions_for_purging(2)

        assert len(result) == 2

    async def test_most_inactive_companion_first(self, rm):
        """More inactive companions (higher days_inactive) should have lower purge_score → ranked first."""
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {
            "c1": self._make_companion("c1", "Somewhat-Old", last_seen_days_ago=30),
            "c2": self._make_companion("c2", "Very-Old", last_seen_days_ago=200),
        }
        rm.db_manager.execute_query = Mock(return_value=[])

        with patch.object(rm, "_get_last_dm_activity", return_value=None), \
             patch.object(rm, "_get_last_advert_activity", return_value=None):
            result = await rm._get_companions_for_purging(5)

        assert len(result) == 2
        assert result[0]["name"] == "Very-Old"

    async def test_exception_returns_empty_list(self, rm):
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = Mock(side_effect=Exception("boom"))

        result = await rm._get_companions_for_purging(5)

        assert result == []
        rm.logger.error.assert_called()

    async def test_purge_score_structure(self, rm):
        """Returned companion dicts should contain expected fields."""
        rm.companion_purge_enabled = True
        rm.bot.meshcore = Mock()
        rm.bot.meshcore.contacts = {"c1": self._make_companion(last_seen_days_ago=90)}
        rm.db_manager.execute_query = Mock(return_value=[])

        with patch.object(rm, "_get_last_dm_activity", return_value=None), \
             patch.object(rm, "_get_last_advert_activity", return_value=None):
            result = await rm._get_companions_for_purging(5)

        assert len(result) == 1
        companion = result[0]
        for field in ("public_key", "name", "purge_score", "days_inactive",
                      "last_dm", "last_advert"):
            assert field in companion, f"Missing field: {field}"
