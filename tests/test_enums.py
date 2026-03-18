"""Tests for modules/enums.py — enum values and flag combinations."""

import pytest

from modules.enums import AdvertFlags, DeviceRole, PayloadType, PayloadVersion, RouteType


class TestAdvertFlags:
    """Tests for AdvertFlags Flag enum."""

    def test_type_flags_values(self):
        assert AdvertFlags.ADV_TYPE_NONE.value == 0x00
        assert AdvertFlags.ADV_TYPE_CHAT.value == 0x01
        assert AdvertFlags.ADV_TYPE_REPEATER.value == 0x02
        assert AdvertFlags.ADV_TYPE_ROOM.value == 0x03
        assert AdvertFlags.ADV_TYPE_SENSOR.value == 0x04

    def test_feature_flag_values(self):
        assert AdvertFlags.ADV_LATLON_MASK.value == 0x10
        assert AdvertFlags.ADV_FEAT1_MASK.value == 0x20
        assert AdvertFlags.ADV_FEAT2_MASK.value == 0x40
        assert AdvertFlags.ADV_NAME_MASK.value == 0x80

    def test_legacy_aliases_equal_originals(self):
        assert AdvertFlags.IsCompanion == AdvertFlags.ADV_TYPE_CHAT
        assert AdvertFlags.IsRepeater == AdvertFlags.ADV_TYPE_REPEATER
        assert AdvertFlags.IsRoomServer == AdvertFlags.ADV_TYPE_ROOM
        assert AdvertFlags.HasLocation == AdvertFlags.ADV_LATLON_MASK
        assert AdvertFlags.HasName == AdvertFlags.ADV_NAME_MASK
        assert AdvertFlags.HasNameBit7 == AdvertFlags.ADV_NAME_MASK

    def test_flag_combination(self):
        """Flags can be combined with |."""
        combined = AdvertFlags.ADV_TYPE_REPEATER | AdvertFlags.ADV_LATLON_MASK
        assert AdvertFlags.ADV_TYPE_REPEATER in combined
        assert AdvertFlags.ADV_LATLON_MASK in combined

    def test_flag_membership(self):
        combined = AdvertFlags.ADV_TYPE_CHAT | AdvertFlags.ADV_NAME_MASK
        assert AdvertFlags.ADV_NAME_MASK in combined
        assert AdvertFlags.ADV_LATLON_MASK not in combined


class TestPayloadType:
    """Tests for PayloadType Enum."""

    def test_known_values(self):
        assert PayloadType.REQ.value == 0x00
        assert PayloadType.RESPONSE.value == 0x01
        assert PayloadType.TXT_MSG.value == 0x02
        assert PayloadType.ACK.value == 0x03
        assert PayloadType.ADVERT.value == 0x04
        assert PayloadType.GRP_TXT.value == 0x05
        assert PayloadType.GRP_DATA.value == 0x06
        assert PayloadType.ANON_REQ.value == 0x07
        assert PayloadType.PATH.value == 0x08
        assert PayloadType.TRACE.value == 0x09
        assert PayloadType.MULTIPART.value == 0x0A
        assert PayloadType.RAW_CUSTOM.value == 0x0F

    def test_lookup_by_value(self):
        assert PayloadType(0x02) == PayloadType.TXT_MSG
        assert PayloadType(0x04) == PayloadType.ADVERT

    def test_all_members_unique(self):
        values = [m.value for m in PayloadType]
        assert len(values) == len(set(values))


class TestPayloadVersion:
    """Tests for PayloadVersion Enum."""

    def test_versions(self):
        assert PayloadVersion.VER_1.value == 0x00
        assert PayloadVersion.VER_2.value == 0x01
        assert PayloadVersion.VER_3.value == 0x02
        assert PayloadVersion.VER_4.value == 0x03

    def test_lookup(self):
        assert PayloadVersion(0x00) == PayloadVersion.VER_1


class TestRouteType:
    """Tests for RouteType Enum."""

    def test_values(self):
        assert RouteType.TRANSPORT_FLOOD.value == 0x00
        assert RouteType.FLOOD.value == 0x01
        assert RouteType.DIRECT.value == 0x02
        assert RouteType.TRANSPORT_DIRECT.value == 0x03

    def test_lookup_by_value(self):
        assert RouteType(0x01) == RouteType.FLOOD


class TestDeviceRole:
    """Tests for DeviceRole Enum."""

    def test_values(self):
        assert DeviceRole.Companion.value == "Companion"
        assert DeviceRole.Repeater.value == "Repeater"
        assert DeviceRole.RoomServer.value == "RoomServer"

    def test_lookup_by_value(self):
        assert DeviceRole("Repeater") == DeviceRole.Repeater

    def test_all_members(self):
        assert len(list(DeviceRole)) == 3
