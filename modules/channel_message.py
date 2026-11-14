"""Channel message steps, mixed into MessageHandler.

handle_channel_message stays on MessageHandler; these are its parts: the sender in the text, signal values, the route from the correlated RF row, the scope packet, the flood_scopes allowlist and the sender's full key.
"""

from collections.abc import Callable
from typing import Any

from .rf_match import rf_data_is_correlated

# Stand-in used when a channel message carries no "Name: " prefix to extract a
# sender from. It is not a node: every such message would share this identity.
CHANNEL_SENDER_FALLBACK = "Channel User"

def _signal_value(
    payload: dict[str, Any],
    metadata: dict[str, Any] | None,
    payload_keys: tuple[str, ...],
    metadata_keys: tuple[str, ...],
    convert: Callable[[Any], Any],
) -> Any:
    """Read a signal metric (SNR or RSSI) from an event payload, else its metadata.

    The first of ``payload_keys`` present in the payload decides, even when its
    value is None; metadata is consulted only when no payload key is present.
    A present value is passed through ``convert``.
    """
    for key in payload_keys:
        if key in payload:
            raw = payload.get(key)
            return convert(raw) if raw is not None else None
    if metadata:
        for key in metadata_keys:
            if key in metadata:
                raw = metadata.get(key)
                return convert(raw) if raw is not None else None
    return None



class ChannelMessageMixin:
    """Mixed into MessageHandler."""

    _find_contact: Any
    _get_path_from_rf_data: Any
    _is_confirmed_global_flood: Any
    _is_rf_data_scope_eligible: Any
    _mesh_graph_capturing: Any
    _resolve_reply_scope_from_rf_data: Any
    _update_mesh_graph: Any
    _update_mesh_graph_from_trace: Any
    decode_meshcore_packet: Any
    extract_path_from_raw_hex: Any
    logger: Any
    rssi_cache: Any
    snr_cache: Any

    def _split_channel_sender(self, text: str) -> tuple[str, str]:
        """(sender, content) of a channel message's text; "Name: message" names its sender."""
        # Get sender information from text field if it's in "SENDER: message" format
        sender_id = CHANNEL_SENDER_FALLBACK  # Default fallback

        # Try to extract sender from text field (e.g., "HOWL: Test" -> "HOWL")
        message_content = text  # Default to full text
        if ":" in text and not text.startswith(":"):
            parts = text.split(":", 1)
            if len(parts) == 2 and parts[0].strip():
                sender_id = parts[0].strip()
                message_content = parts[1].strip()  # Use the part after the colon for keyword processing
                self.logger.debug(f"Extracted sender from text: {sender_id}")
                self.logger.debug(f"Message content for processing: {message_content}")

        # Always strip trailing whitespace/newlines from message content to handle cases like "Wx 98104\n"
        message_content = message_content.strip()
        return sender_id, message_content

    def _message_signal(
        self, payload: dict[str, Any], metadata: dict[str, Any] | None, snr_payload_keys: tuple[str, ...]
    ) -> tuple[float | None, int | None]:
        """A message's SNR and RSSI from its payload or metadata, else the signal cache by pubkey prefix."""
        snr: float | None = None
        rssi: int | None = None

        # Try to get SNR from payload first
        snr = _signal_value(payload, metadata, snr_payload_keys, ("snr", "SNR"), float)

        # If still no SNR, try to get it from the cache using pubkey prefix from payload
        if snr is None:
            pubkey_prefix = payload.get("pubkey_prefix", "")
            if pubkey_prefix and pubkey_prefix in self.snr_cache:
                snr = self.snr_cache[pubkey_prefix]
                self.logger.debug(f"Retrieved cached SNR {snr} for pubkey {pubkey_prefix}")

        # Try to get RSSI from payload first
        rssi = _signal_value(payload, metadata, ("RSSI", "rssi", "signal_strength"), ("rssi", "RSSI"), int)

        # If still no RSSI, try to get it from the cache using pubkey prefix from payload
        if rssi is None:
            pubkey_prefix = payload.get("pubkey_prefix", "")
            if pubkey_prefix and pubkey_prefix in self.rssi_cache:
                rssi = int(self.rssi_cache[pubkey_prefix])
                self.logger.debug(f"Retrieved cached RSSI {rssi} for pubkey {pubkey_prefix}")
        return snr, rssi

    def _channel_route_from_rf(
        self,
        recent_rf_data: dict[str, Any] | None,
        payload: dict[str, Any],
        snr: Any,
        rssi: Any,
    ) -> tuple[Any, Any, dict[str, Any] | None, Any, str | None]:
        """Signal values, decoded packet, hops and path for a channel message from its correlated RF row.

        An uncorrelated fallback row supplies signal values but never a route (#80).
        """
        packet_info: dict[str, Any] | None = None
        if recent_rf_data and recent_rf_data.get("raw_hex"):
            raw_hex = recent_rf_data["raw_hex"]
            self.logger.info(f"🔍 FOUND RF DATA: {len(raw_hex)} chars, starts with: {raw_hex[:32]}...")
            self.logger.debug(f"Full RF data: {raw_hex}")

            # Extract SNR/RSSI from the RF data
            if recent_rf_data.get("snr"):
                snr = recent_rf_data["snr"]
                self.logger.debug(f"Using SNR from RF data: {snr}")

            if recent_rf_data.get("rssi"):
                rssi = recent_rf_data["rssi"]
                self.logger.debug(f"Using RSSI from RF data: {rssi}")

            # Single path source: prefer routing_info, else decode/fallback via helper
            path_string = None
            hops = payload.get("path_len", 255)
            payload_hex = recent_rf_data.get("payload")
            packet_info = self.decode_meshcore_packet(raw_hex, payload_hex)
            packet_hash = recent_rf_data.get("packet_hash")
            if packet_hash and packet_info:
                packet_info["packet_hash"] = packet_hash

            # A fallback correlation is just the most recent packet heard, not this
            # message's packet. Attributing its route here is how a multi-hop message
            # ended up recorded as a single direct hop (#80) — and it would write a
            # fabricated edge into the mesh graph. Leave the route unknown instead.
            route_is_attributable = rf_data_is_correlated(recent_rf_data)

            if not route_is_attributable:
                # Terminal on purpose. Falling through would reach the raw-hex and
                # routing_info fallbacks below, which would take the route from the
                # unrelated packet — the exact bug this guards against (#80).
                self.logger.debug(
                    "RF data for this channel message is an uncorrelated fallback; "
                    "not attributing its route (hops/path left unresolved)"
                )
                hops = payload.get("path_len", 255)
                path_string = None
            elif packet_info and packet_info.get("path_len") is not None:
                hops = packet_info.get("path_len", 0)
                if packet_info.get("payload_type") == 9:  # TRACE packet
                    path_info = packet_info.get("path_info", {})
                    path_hashes = path_info.get("path_hashes") or path_info.get("path", [])
                    if path_hashes:
                        path_string = ",".join(path_hashes)
                        self.logger.debug(f"Path from TRACE packet: {path_string} ({len(path_hashes)} hops)")
                        if self._mesh_graph_capturing():
                            self._update_mesh_graph_from_trace(path_hashes, packet_info)
                    else:
                        path_string = "Direct" if hops == 0 else f"Unknown routing ({hops} hops)"
                        self.logger.debug(f"Path from TRACE packet: {path_string}")
                else:
                    had_routing_nodes = bool((recent_rf_data.get("routing_info") or {}).get("path_nodes"))
                    path_string, path_nodes, hops = self._get_path_from_rf_data(
                        recent_rf_data, payload_hex=payload_hex, packet_info=packet_info
                    )
                    if path_string and path_nodes and self._mesh_graph_capturing():
                        self._update_mesh_graph(path_nodes, packet_info)
                    if path_string and not had_routing_nodes:
                        self.logger.debug(f"Path from fallback decode: {path_string} ({hops} hops)")
            else:
                self.logger.debug("Packet decoding failed, trying direct hex or routing_info fallback")
                path_string = self.extract_path_from_raw_hex(raw_hex, hops)
                if (
                    not path_string
                    and recent_rf_data.get("routing_info")
                    and recent_rf_data["routing_info"].get("path_nodes")
                ):
                    routing_info = recent_rf_data["routing_info"]
                    path_nodes = routing_info["path_nodes"]
                    hops = len(path_nodes)
                    path_string = ",".join(str(n).lower() for n in path_nodes)
                    self.logger.debug(f"Path from RF routing_info fallback: {path_string} ({hops} hops)")
        else:
            self.logger.warning("❌ NO RF DATA found for channel message after all correlation attempts")
            hops = payload.get("path_len", 255)
            path_string = None
        return snr, rssi, packet_info, hops, path_string

    def _decode_scope_packet(self, scope_rf_data: dict[str, Any] | None) -> dict[str, Any] | None:
        """The decoded inner packet of the scope-eligible RF row, carrying its packet hash."""
        scope_packet_info: dict[str, Any] | None = None
        if scope_rf_data and scope_rf_data.get("raw_hex"):
            # Decode the full inner MeshCore packet (header + path + ciphertext).
            # scope_payload_hex is ciphertext-only for HMAC; do not pass it to decode_meshcore_packet.
            inner_packet_hex = scope_rf_data.get("payload")
            if inner_packet_hex:
                scope_packet_info = self.decode_meshcore_packet(inner_packet_hex)
            if scope_rf_data.get("packet_hash") and scope_packet_info:
                scope_packet_info["packet_hash"] = scope_rf_data["packet_hash"]
        return scope_packet_info

    def _channel_reply_scope(
        self,
        scope_rf_data: dict[str, Any] | None,
        scope_rf_is_correlated: bool,
        scope_packet_info: dict[str, Any] | None,
        scope_keys: dict[str, bytes],
    ) -> str | None:
        """The flood scope to reply in, from this message's own scope-eligible packet."""
        reply_scope: str | None = None
        if scope_rf_data and scope_keys:
            if scope_rf_is_correlated:
                reply_scope = self._resolve_reply_scope_from_rf_data(
                    scope_rf_data, scope_packet_info, scope_keys
                )
            else:
                self.logger.info(
                    "Scope for this channel message is unknown: the only scope-eligible "
                    "RF data is an uncorrelated fallback from another packet, so it "
                    "cannot authorise a reply under flood_scopes"
                )
        return reply_scope

    def _channel_scope_allowed(
        self,
        scope_keys: dict[str, bytes],
        allow_global: Any,
        reply_scope: str | None,
        scope_rf_data: dict[str, Any] | None,
        scope_rf_is_correlated: bool,
        scope_packet_info: dict[str, Any] | None,
        recent_rf_data: dict[str, Any] | None,
        packet_info: dict[str, Any] | None,
    ) -> bool:
        """The flood_scopes allowlist: False (after logging why) when the bot must not reply."""
        # Allowlist enforcement: when flood_scopes is configured, only reply to
        # messages whose scope matched an entry.  Unscoped FLOOD is allowed only
        # when '*' (or equivalent) is explicitly listed.
        # A '*'-only flood_scopes leaves scope_keys empty but still means an
        # allowlist is configured (global only). Gating on scope_keys alone let
        # that configuration skip authorisation entirely.
        if (scope_keys or allow_global) and reply_scope is None:
            if (
                scope_rf_data
                and scope_rf_is_correlated
                and self._is_rf_data_scope_eligible(scope_rf_data, scope_packet_info)
            ):
                self.logger.info("Ignoring TC_FLOOD: scope not in flood_scopes allowlist")
                return False
            # '*' permits *unscoped global* traffic, not traffic of unknown scope,
            # so it needs positive evidence that this message's own packet was
            # ordinary FLOOD. The general RF correlation carries that evidence for
            # the normal case; without it the scope is unknown and an allowlist
            # should fail closed rather than assume global.
            if allow_global and not self._is_confirmed_global_flood(
                recent_rf_data,
                packet_info,
                scoped_traffic_in_window=scope_rf_data is not None,
            ):
                self.logger.info(
                    "Ignoring channel message: flood_scopes lists '*', but a scoped "
                    "TC_FLOOD packet was heard alongside this message and it could "
                    "not be confirmed as unscoped FLOOD (scope unknown, not global)"
                )
                return False

            if not allow_global:
                if scope_rf_data is None:
                    self.logger.info(
                        "Ignoring channel message: no TC_FLOOD RF correlation for "
                        "flood_scopes allowlist (avoid replying on wrong scope)"
                    )
                else:
                    self.logger.debug(
                        "Ignoring FLOOD: unscoped messages not permitted (add '*' to flood_scopes)"
                    )
                return False
        return True

    def _full_sender_pubkey(self, payload: dict[str, Any], sender_id: str) -> str:
        """The sender's full public key from the radio's contacts, else the payload's pubkey prefix."""
        # Get the full public key from contacts if available
        sender_pubkey = payload.get("pubkey_prefix", "")
        if sender_pubkey:
            prefix = sender_pubkey
            contact = self._find_contact(lambda c: c.get("public_key", "").startswith(prefix))
            if contact:
                sender_pubkey = contact.get("public_key", sender_pubkey)
                self.logger.debug(f"Found full public key for {sender_id}: {sender_pubkey[:16]}...")
        return sender_pubkey
