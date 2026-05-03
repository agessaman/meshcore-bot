#!/usr/bin/env python3
"""
Enums for MeshCore packet parsing
Based on the meshcore protocol specifications
"""

from enum import Enum, Flag


class AdvertFlags(Flag):
    """Advertisement flags for MeshCore packets - matches C++ AdvertDataHelpers.h"""
    # Type flags (bits 0-3)
    ADV_TYPE_NONE = 0x00
    ADV_TYPE_CHAT = 0x01
    ADV_TYPE_REPEATER = 0x02
    ADV_TYPE_ROOM = 0x03
    ADV_TYPE_SENSOR = 0x04

    # Feature flags (bits 4-7)
    ADV_LATLON_MASK = 0x10    # Bit 4 - Has location data
    ADV_FEAT1_MASK = 0x20     # Bit 5 - Future feature 1
    ADV_FEAT2_MASK = 0x40     # Bit 6 - Future feature 2
    ADV_NAME_MASK = 0x80      # Bit 7 - Has name data

    # Legacy aliases for backward compatibility
    IsCompanion = ADV_TYPE_CHAT
    IsRepeater = ADV_TYPE_REPEATER
    IsRoomServer = ADV_TYPE_ROOM
    HasLocation = ADV_LATLON_MASK
    HasName = ADV_NAME_MASK
    HasNameBit7 = ADV_NAME_MASK

class PayloadType(Enum):
    """Payload types for MeshCore packets - matches C++ definitions"""
    REQ = 0x00          # PAYLOAD_TYPE_REQ
    RESPONSE = 0x01     # PAYLOAD_TYPE_RESPONSE
    TXT_MSG = 0x02      # PAYLOAD_TYPE_TXT_MSG
    ACK = 0x03          # PAYLOAD_TYPE_ACK
    ADVERT = 0x04       # PAYLOAD_TYPE_ADVERT
    GRP_TXT = 0x05      # PAYLOAD_TYPE_GRP_TXT
    GRP_DATA = 0x06     # PAYLOAD_TYPE_GRP_DATA
    ANON_REQ = 0x07     # PAYLOAD_TYPE_ANON_REQ
    PATH = 0x08         # PAYLOAD_TYPE_PATH
    TRACE = 0x09        # PAYLOAD_TYPE_TRACE
    MULTIPART = 0x0A    # PAYLOAD_TYPE_MULTIPART
    Type11 = 0x0B       # Reserved
    Type12 = 0x0C       # Reserved
    Type13 = 0x0D        # Reserved
    Type14 = 0x0E       # Reserved
    RAW_CUSTOM = 0x0F   # PAYLOAD_TYPE_RAW_CUSTOM

class PayloadVersion(Enum):
    """Payload versions for MeshCore packets - matches original mctomqtt.py"""
    VER_1 = 0x00  # Version 1
    VER_2 = 0x01  # Reserved for future protocol extensions
    VER_3 = 0x02  # Reserved for future protocol extensions
    VER_4 = 0x03  # Reserved for future protocol extensions

class RouteType(Enum):
    """Route types for MeshCore packets - matches C++ definitions"""
    TRANSPORT_FLOOD = 0x00    # ROUTE_TYPE_TRANSPORT_FLOOD
    FLOOD = 0x01              # ROUTE_TYPE_FLOOD
    DIRECT = 0x02             # ROUTE_TYPE_DIRECT
    TRANSPORT_DIRECT = 0x03   # ROUTE_TYPE_TRANSPORT_DIRECT

class DeviceRole(Enum):
    """Device roles in MeshCore network"""
    Companion = "Companion"
    Repeater = "Repeater"
    RoomServer = "RoomServer"
