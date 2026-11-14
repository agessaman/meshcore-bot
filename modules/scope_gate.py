"""Regional flood-scope decisions for incoming channel messages.

Pure functions over correlated RF rows and decoded packet info: matching a
TC_FLOOD transport code against configured scope keys, and judging whether a
message was scoped, global, or unknown. MessageHandler keeps thin delegating
methods under the old private names.
"""

from __future__ import annotations

import hmac as hmac_mod
from hashlib import sha256
from typing import Any

from .enums import PayloadType, RouteType
from .region_warning import VERDICT_GLOBAL, VERDICT_SCOPED, VERDICT_UNKNOWN
from .rf_match import rf_data_is_correlated


def match_scope(
    transport_code: int, payload_type: int, pkt_payload: bytes, scope_keys: dict[str, bytes]
) -> str | None:
    """Return the scope name whose HMAC matches transport_code, or None.

    Mirrors the firmware's TransportKey::calcTransportCode: computes
    HMAC-SHA256(scope_key, [payload_type_byte] + pkt_payload)[0:2] as uint16_le
    and compares it against transport_code (transport_codes[0] from TC_FLOOD header).
    """
    if not scope_keys:
        return None
    check_data = bytes([payload_type]) + pkt_payload
    for name, key in scope_keys.items():
        digest = hmac_mod.new(key, check_data, sha256).digest()
        computed = int.from_bytes(digest[:2], "little")
        if computed == 0:
            computed = 1
        elif computed == 0xFFFF:
            computed = 0xFFFE
        if computed == transport_code:
            return name
    return None


def scope_fields_from_packet_info(
    packet_info: dict[str, Any] | None,
) -> tuple[int | None, int | None, int | None, str]:
    """Extract TC_FLOOD scope-match inputs from decode_meshcore_packet output."""
    if not packet_info:
        return None, None, None, ""
    rt = packet_info.get("route_type")
    if rt is not None and hasattr(rt, "value"):
        rt = rt.value
    pt = packet_info.get("payload_type")
    if pt is not None and hasattr(pt, "value"):
        pt = int(pt.value)
    elif pt is not None:
        pt = int(pt)
    tc_code1 = None
    transport_codes = packet_info.get("transport_codes")
    if isinstance(transport_codes, dict):
        tc_code1 = transport_codes.get("code1")
    payload_hex = (packet_info.get("payload_hex") or "") or ""
    return rt, tc_code1, pt, payload_hex


def resolve_reply_scope_from_rf_data(
    recent_rf_data: dict[str, Any],
    packet_info: dict[str, Any] | None,
    scope_keys: dict[str, bytes],
    logger: Any,
) -> str | None:
    """Match incoming TC_FLOOD to flood_scopes, preferring decoded packet over stale cache."""
    rt = recent_rf_data.get("route_type_int")
    tc_code1 = recent_rf_data.get("transport_code1")
    scope_payload_type = recent_rf_data.get("payload_type_int")
    scope_payload_hex = recent_rf_data.get("scope_payload_hex") or ""

    dec_rt, dec_tc, dec_pt, dec_hex = scope_fields_from_packet_info(packet_info)
    used_decode_fallback = False
    if dec_rt == 0:
        if rt != 0:
            used_decode_fallback = True
        rt = 0
        if dec_tc is not None:
            if tc_code1 != dec_tc:
                used_decode_fallback = True
            tc_code1 = dec_tc
        if dec_pt is not None:
            if scope_payload_type != dec_pt:
                used_decode_fallback = True
            scope_payload_type = dec_pt
        if dec_hex:
            if scope_payload_hex != dec_hex:
                used_decode_fallback = True
            scope_payload_hex = dec_hex

    if used_decode_fallback:
        logger.debug(
            "TC_FLOOD scope fields from packet decode (cache had route_type=%s tc=%s)",
            recent_rf_data.get("route_type_int"),
            recent_rf_data.get("transport_code1"),
        )

    if not (
        rt == 0
        and tc_code1 is not None
        and scope_payload_type is not None
        and scope_payload_hex
    ):
        if scope_keys:
            logger.debug(
                "Scope check: route_type=%s (need 0=TC_FLOOD), "
                "tc_code1=%s, payload_type=%s, payload_hex=%s",
                rt,
                "set" if tc_code1 is not None else "None",
                scope_payload_type,
                "set" if scope_payload_hex else "empty",
            )
        return None

    try:
        pkt_payload_bytes = bytes.fromhex(scope_payload_hex)
    except ValueError:
        logger.debug("Scope check: invalid scope_payload_hex on correlated RF data")
        return None

    reply_scope = match_scope(tc_code1, scope_payload_type, pkt_payload_bytes, scope_keys)
    if reply_scope:
        logger.info(
            "Incoming TC_FLOOD matched scope '%s' (tc_code1=%s); reply will use same scope",
            reply_scope,
            tc_code1,
        )
    elif scope_keys:
        logger.debug(
            "TC_FLOOD scope not matched: tc_code1=%s payload_type=%s "
            "(configured scopes: %s)",
            tc_code1,
            scope_payload_type,
            ", ".join(sorted(scope_keys.keys())),
        )
    return reply_scope


def effective_route_type_int(
    recent_rf_data: dict[str, Any] | None,
    packet_info: dict[str, Any] | None,
) -> int | None:
    """Route type for allowlist gate, preferring decode when it indicates TC_FLOOD."""
    if not recent_rf_data:
        return None
    rt = recent_rf_data.get("route_type_int")
    dec_rt, _, _, _ = scope_fields_from_packet_info(packet_info)
    if dec_rt == 0:
        return 0
    return rt


def grp_txt_payload_type_int() -> int:
    """Payload type used for channel text on TC_FLOOD (GRP_TXT)."""
    return int(PayloadType.GRP_TXT.value)


def is_confirmed_global_flood(
    rf_data: dict[str, Any] | None,
    packet_info: dict[str, Any] | None = None,
    *,
    scoped_traffic_in_window: bool = True,
) -> bool:
    """True only when this message is proven *not* to be a scoped regional flood.

    Used to decide whether a '*' entry in flood_scopes authorizes a reply. '*'
    permits unscoped global traffic, so it needs positive evidence, and there
    are two independent ways to get it:

    * RF data correlated to *this* message showing RouteType.FLOOD.
    * No scope-eligible packet anywhere in the RF window
      (``scoped_traffic_in_window=False``). A scoped message travels as
      TRANSPORT_FLOOD GRP_TXT, so if the radio heard no such packet while this
      message arrived, the message cannot have been scoped. That conclusion is
      window-wide, so unlike a route type read off a fallback row it does not
      depend on having picked the right cached packet.

    The second route is what makes '*' usable on a channel: MeshCore's CHAN
    payload carries neither raw_hex nor a pubkey prefix, so a channel message
    has no correlation key at all and always lands on the most-recent-packet
    fallback. Requiring correlation alone rejected *every* channel message.

    An empty RF window still fails closed: with no observed traffic there is
    no evidence either way.
    """
    if not rf_data:
        return False

    if rf_data_is_correlated(rf_data):
        route_type = rf_data.get("route_type_int")
        dec_rt, _tc, _pt, _hex = scope_fields_from_packet_info(packet_info)
        if dec_rt is not None:
            route_type = dec_rt
        return route_type == RouteType.FLOOD.value

    # Uncorrelated: this row describes some other packet, so its route type
    # proves nothing about the message. Only the absence of scoped traffic does.
    return not scoped_traffic_in_window


def is_rf_data_scope_eligible(
    rf_data: dict[str, Any] | None,
    packet_info: dict[str, Any] | None = None,
) -> bool:
    """True when RF row has fields needed for TC_FLOOD regional scope HMAC matching."""
    if not rf_data:
        return False
    rt = rf_data.get("route_type_int")
    tc_code1 = rf_data.get("transport_code1")
    payload_type = rf_data.get("payload_type_int")
    scope_payload_hex = rf_data.get("scope_payload_hex") or ""

    dec_rt, dec_tc, dec_pt, dec_hex = scope_fields_from_packet_info(packet_info)
    if dec_rt == 0:
        rt = 0
        if dec_tc is not None:
            tc_code1 = dec_tc
        if dec_pt is not None:
            payload_type = dec_pt
        if dec_hex:
            scope_payload_hex = dec_hex

    if rt != int(RouteType.TRANSPORT_FLOOD.value):
        return False
    if tc_code1 is None or payload_type is None or not scope_payload_hex:
        return False
    return int(payload_type) == grp_txt_payload_type_int()


def classify_channel_flood_scope(
    *,
    reply_scope: str | None,
    recent_rf_data: dict[str, Any] | None,
    packet_info: dict[str, Any] | None,
    scope_rf_data: dict[str, Any] | None,
    scope_packet_info: dict[str, Any] | None,
) -> str:
    """Classify a channel message's flood scope as scoped, global, or unknown.

    "Scoped" means the message carried a region code (a TC_FLOOD transport
    code), whether or not that code matches one of ours. "Global" means it
    was proven to be an ordinary unscoped FLOOD. Anything else is unknown.

    Every test here needs RF data correlated to *this* message, and the
    scoped tests run before the global one, so both kinds of ambiguity
    resolve away from ``global``. That direction matters: ``global`` is the
    verdict that can spend airtime telling someone to fix their config, and
    a message whose scope the radio did not witness is not evidence that the
    sender omitted a region.

    In particular this does **not** use ``is_confirmed_global_flood``'s
    second route, which infers "unscoped" from the absence of any
    scope-eligible packet in the window. That inference is sound enough to
    decide whether a ``*`` entry in ``flood_scopes`` authorizes a reply — the
    cost of being wrong is one reply the operator broadly wanted — but it is
    an argument from absence, and the cost of being wrong here is an
    unsolicited message accusing someone of a misconfiguration they may not
    have. Channel messages still correlate through
    ``_find_rf_row_matching_chan_payload`` (payload type, path length and
    SNR all agreeing), so the ordinary case is unaffected.
    """
    if reply_scope:
        return VERDICT_SCOPED

    # A correlated scope-eligible row *is* a TC_FLOOD GRP_TXT for this
    # message: it carried a transport code, so a region was set even though
    # it is not one this bot has keys for.
    if (
        scope_rf_data
        and rf_data_is_correlated(scope_rf_data)
        and is_rf_data_scope_eligible(scope_rf_data, scope_packet_info)
    ):
        return VERDICT_SCOPED

    if recent_rf_data and rf_data_is_correlated(recent_rf_data):
        route_type = effective_route_type_int(recent_rf_data, packet_info)
        if route_type == int(RouteType.TRANSPORT_FLOOD.value):
            return VERDICT_SCOPED

    if rf_data_is_correlated(recent_rf_data) and is_confirmed_global_flood(
        recent_rf_data,
        packet_info,
        scoped_traffic_in_window=scope_rf_data is not None,
    ):
        return VERDICT_GLOBAL

    return VERDICT_UNKNOWN
