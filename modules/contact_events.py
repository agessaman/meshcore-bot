"""Contact and advert events, mixed into MessageHandler.

NEW_CONTACT handling (route from the contact's own advert, the add-to-radio gate), advert packet tracking and the path encoding a contact is stored with."""

import asyncio
import copy
import time
from typing import Any

from .neighbors_discovery import upsert_zero_hop_observed_path_via_manager
from .security_utils import sanitize_name
from .utils import encode_path_len_byte


class ContactEventsMixin:
    """Mixed into MessageHandler."""

    _advert_rf: Any
    _new_contact_adds: Any
    _store_observed_path: Any
    _update_mesh_graph_from_advert: Any
    _viewer_bridge: Any
    bot: Any
    logger: Any
    parse_advert: Any
    rf_data_timeout: Any

    async def _process_advertisement_packet(
        self, packet_info: dict[str, Any], metadata: dict[str, Any] | None = None
    ) -> None:
        """Process advertisement packets for complete repeater tracking.

        Extracts node information, location data, and routing path from
        advertisement packets and updates the repeater database.

        Args:
            packet_info: Dictionary containing decoded packet information.
            metadata: Optional metadata dictionary with signal metrics.
        """
        try:
            # Check if this is an advertisement packet
            if (
                packet_info.get("payload_type") == "ADVERT"
                or packet_info.get("payload_type_name") == "ADVERT"
                or packet_info.get("type") == "advert"
            ):
                self.logger.debug(f"Processing advertisement packet: {packet_info}")

                # Parse the advert payload if we have it
                advert_data = {}
                if "payload_hex" in packet_info:
                    try:
                        payload_bytes = bytes.fromhex(packet_info["payload_hex"])
                        parsed_advert = self.parse_advert(payload_bytes)
                        if parsed_advert:
                            advert_data = parsed_advert
                            self.logger.info(
                                f"✅ Parsed ADVERT: {sanitize_name(advert_data.get('mode', 'Unknown'))} - {sanitize_name(advert_data.get('name', 'No name'))}"
                            )
                    except Exception as e:
                        self.logger.warning(f"Failed to parse ADVERT payload: {e}")

                # Fallback to basic information if parsing failed
                if not advert_data:
                    advert_data = {
                        "public_key": packet_info.get("sender_id", ""),
                        "name": packet_info.get("name", packet_info.get("adv_name", "Unknown")),
                        "mode": "Unknown",
                    }

                # Add advert data to packet_info for web viewer
                if advert_data:
                    packet_info["advert_name"] = advert_data.get("name")
                    packet_info["advert_mode"] = advert_data.get("mode")
                    packet_info["advert_public_key"] = advert_data.get("public_key")

                # Extract signal information from metadata
                signal_info = {}
                if metadata:
                    signal_info.update(metadata)

                # Add hop count if available
                if "hops" in packet_info:
                    signal_info["hops"] = packet_info["hops"]

                # Extract packet_hash and path information if available (from routing_info or packet_info)
                packet_hash = None
                out_path = ""
                out_path_len = -1

                if "routing_info" in packet_info and packet_info["routing_info"]:
                    routing_info = packet_info["routing_info"]
                    packet_hash = routing_info.get("packet_hash")
                    # Extract path information from routing_info
                    path_hex = routing_info.get("path_hex", "")
                    path_length = routing_info.get("path_length", 0)
                    if path_hex and path_length > 0:
                        out_path = path_hex
                        out_path_len = path_length
                    elif path_length == 0:
                        # Direct connection
                        out_path = ""
                        out_path_len = 0
                elif "packet_hash" in packet_info:
                    packet_hash = packet_info["packet_hash"]

                # Also check packet_info directly for path information (fallback)
                if out_path_len == -1:
                    if "path_hex" in packet_info:
                        out_path = packet_info.get("path_hex", "")
                        out_path_len = packet_info.get("path_len", -1)
                    elif "path_len" in packet_info:
                        out_path_len = packet_info.get("path_len", -1)
                        if out_path_len == 0:
                            out_path = ""

                # Add path information to advert_data so it gets saved to the database
                if out_path_len >= 0:
                    advert_data["out_path"] = out_path
                    advert_data["out_path_len"] = out_path_len
                    advert_data["out_bytes_per_hop"] = packet_info.get("bytes_per_hop", 1)

                # Update mesh graph with edges from the advert path (one edge per hop).
                # This can trigger many send_mesh_edge_update() calls in quick succession;
                # if the web viewer is down, that produces a wave of connection-refused logs.
                path_byte_length = packet_info.get("path_byte_length") or (len(out_path) // 2 if out_path else 0)
                if (
                    out_path
                    and out_path_len > 0
                    and hasattr(self.bot, "mesh_graph")
                    and self.bot.mesh_graph
                    and self.bot.mesh_graph.capture_enabled
                ):
                    self._update_mesh_graph_from_advert(advert_data, out_path, path_byte_length, packet_info)

                # Store complete path in observed_paths table. Empty-path (direct RF)
                # adverts are stored too — those are true one-hop neighbours.
                if out_path_len == 0 and advert_data.get("public_key"):
                    upsert_zero_hop_observed_path_via_manager(
                        getattr(self.bot, "db_manager", None),
                        advert_data["public_key"],
                        self.logger,
                        snr=signal_info.get("snr") if signal_info else None,
                        rssi=(
                            signal_info.get("rssi", signal_info.get("signal_strength"))
                            if signal_info
                            else None
                        ),
                        bytes_per_hop=packet_info.get("bytes_per_hop", 1) or 1,
                        packet_hash=packet_hash,
                        update_rssi=True,
                    )
                elif out_path and out_path_len > 0:
                    self._store_observed_path(
                        advert_data,
                        out_path,
                        path_byte_length,
                        "advert",
                        packet_hash=packet_hash,
                        bytes_per_hop=packet_info.get("bytes_per_hop", 1),
                    )

                # Track this advertisement in the complete database
                if hasattr(self.bot, "repeater_manager"):
                    # Track all advertisements regardless of type
                    track_result = await self.bot.repeater_manager.track_contact_advertisement(
                        advert_data, signal_info, packet_hash=packet_hash
                    )
                    if track_result.ok:
                        # Log rich advert information
                        mode = advert_data.get("mode", "Unknown")
                        name = advert_data.get("name", "No name")
                        location = ""
                        if "lat" in advert_data and "lon" in advert_data:
                            # Try to get resolved location from database if available
                            try:
                                if hasattr(self.bot, "repeater_manager"):
                                    # Look up the contact to get resolved location
                                    public_key = advert_data.get("public_key")
                                    if public_key:
                                        contact_query = self.bot.db_manager.execute_query(
                                            "SELECT city, state, country FROM complete_contact_tracking WHERE public_key = ?",
                                            (public_key,),
                                        )
                                        if contact_query:
                                            contact = contact_query[0]
                                            city = contact.get("city")
                                            state = contact.get("state")
                                            if city and state:
                                                location = f" at {city}, {state}"
                                            elif city:
                                                location = f" at {city}"
                                            else:
                                                # Fallback to coordinates if no resolved location
                                                location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                                        else:
                                            # No contact found yet, use coordinates
                                            location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                                    else:
                                        # No public key, use coordinates
                                        location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                                else:
                                    # No repeater manager, use coordinates
                                    location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                            except Exception as e:
                                # If lookup fails, fallback to coordinates
                                self.logger.debug(f"Could not get resolved location for logging: {e}")
                                location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"

                        # Show hop count in log
                        hop_count = signal_info.get("hops", 0)
                        hop_info = f" ({hop_count} hop{'s' if hop_count != 1 else ''})" if hop_count is not None else ""

                        self.logger.info(f"📡 Tracked {mode}: {name}{location}{hop_info}")
                    else:
                        self.logger.warning(
                            f"Failed to track contact advertisement: {sanitize_name(advert_data.get('name', 'Unknown'))}"
                        )

        except Exception as e:
            self.logger.error(f"Error processing advertisement packet: {e}")

    def _ensure_contact_meshcore_path_encoding(self, contact_data: dict[str, Any]) -> None:
        """If out_path_len is set but out_path_hash_mode is still flood (-1), rebuild wire fields.

        meshcore update_contact uses out_path_len | (out_path_hash_mode << 6); hash_mode -1 with
        non-negative hop count produces a negative int and OverflowError on unsigned to_bytes.
        """
        try:
            hash_mode = int(contact_data.get("out_path_hash_mode", 0))
        except (TypeError, ValueError):
            return
        if hash_mode != -1:
            return

        opl: int | None
        raw_opl = contact_data.get("out_path_len")
        try:
            opl = None if raw_opl is None else int(raw_opl)
        except (TypeError, ValueError):
            opl = None

        bph_raw = contact_data.get("out_bytes_per_hop", 1) or 1
        try:
            bph = int(bph_raw)
        except (TypeError, ValueError):
            bph = 1
        if bph not in (1, 2, 3):
            bph = 1

        # Some NEW_CONTACT payloads omit out_path_len but include out_path + bytes_per_hop.
        # Derive hop count here so meshcore doesn't combine a non-flood path with hash_mode=-1.
        if opl is None:
            out_path_hex = contact_data.get("out_path") or ""
            if not isinstance(out_path_hex, str) or not out_path_hex:
                return
            if (len(out_path_hex) % 2) != 0:
                return
            path_bytes = len(out_path_hex) // 2
            if path_bytes <= 0:
                return
            if (path_bytes % bph) == 0:
                opl = path_bytes // bph
            else:
                opl = path_bytes

        if opl < 0 or opl == -1:
            return

        try:
            pb = encode_path_len_byte(opl, bph)
        except ValueError:
            pb = encode_path_len_byte(min(opl, 0x3F), 1)
        contact_data["out_path_hash_mode"] = (pb >> 6) & 0x03
        contact_data["out_path_len"] = pb & 0x3F

    def _remember_advert_rf(
        self,
        packet_info: dict[str, Any],
        routing_info: dict[str, Any],
        packet_hash: str | None,
        signal_info: dict[str, Any],
        current_time: float,
    ) -> None:
        """Record an advert heard on RF for handle_new_contact.

        Called before the advert is processed, which can yield, so a NEW_CONTACT
        handled meanwhile still finds it. An ADVERT payload starts with the
        sender's 32-byte public key and its 4-byte advert timestamp.
        """
        payload_hex = packet_info.get("payload_hex")
        if not isinstance(payload_hex, str) or len(payload_hex) < 72:
            return
        try:
            advert_timestamp = int.from_bytes(bytes.fromhex(payload_hex[64:72]), "little")
        except ValueError:
            return
        self._advert_rf.append({
            "timestamp": current_time,
            "public_key": payload_hex[:64].lower(),
            "advert_timestamp": advert_timestamp,
            "routing_info": routing_info,
            "packet_hash": packet_hash,
            "snr": signal_info.get("snr"),
            "rssi": signal_info.get("rssi"),
        })
        cutoff = current_time - self.rf_data_timeout
        self._advert_rf = [entry for entry in self._advert_rf if entry["timestamp"] >= cutoff][-256:]

    def _find_advert_rf_data(self, public_key: str, advert_timestamp: Any = None) -> dict[str, Any] | None:
        """The first copy heard of ``public_key``'s advert, or None.

        With ``advert_timestamp`` (NEW_CONTACT's ``last_advert``, 0 included) only
        that advert matches; without it, the most recent one. Copies of one advert heard over
        different paths share its timestamp; the first is the one the device acted on.
        """
        if not public_key:
            return None
        key = public_key.lower()
        now = time.time()
        matches = [
            entry
            for entry in self._advert_rf
            if entry["public_key"] == key and now - entry["timestamp"] < self.rf_data_timeout
        ]
        if isinstance(advert_timestamp, int) and not isinstance(advert_timestamp, bool) and advert_timestamp >= 0:
            matches = [entry for entry in matches if entry["advert_timestamp"] == advert_timestamp]
        elif matches:
            newest = max(matches, key=lambda entry: entry["timestamp"])["advert_timestamp"]
            matches = [entry for entry in matches if entry["advert_timestamp"] == newest]
        if not matches:
            return None
        return min(matches, key=lambda entry: entry["timestamp"])

    def _release_new_contact_add(self, public_key: str, packet_hash: str | None) -> None:
        """Let a later NEW_CONTACT for this advert try again after a failed add."""
        self._new_contact_adds.pop((public_key, packet_hash or ""), None)

    def _claim_new_contact_add(self, public_key: str, packet_hash: str | None) -> bool:
        """False when a NEW_CONTACT for this advert packet already added the contact."""
        if not packet_hash or packet_hash == "0000000000000000":
            return True
        key = (public_key, packet_hash)
        if key in self._new_contact_adds:
            return False
        self._new_contact_adds[key] = None
        while len(self._new_contact_adds) > 256:
            del self._new_contact_adds[next(iter(self._new_contact_adds))]
        return True

    async def handle_new_contact(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
        """Handle NEW_CONTACT events for automatic contact management"""
        try:
            # Copy payload immediately to avoid segfault if event is freed
            # Make a deep copy to ensure we have all the data we need
            if hasattr(event, "payload"):
                contact_data = copy.deepcopy(event.payload)
            else:
                # Fallback: try to copy the event itself if it's a dict-like object
                contact_data = copy.deepcopy(event) if isinstance(event, dict) else None

            if not contact_data:
                self.logger.warning("NEW_CONTACT event has no payload data")
                return

            self.logger.debug(f"🔍 NEW_CONTACT EVENT RECEIVED: {event}")

            # Get contact details
            contact_name = sanitize_name(contact_data.get("name", contact_data.get("adv_name", "Unknown")))
            public_key = contact_data.get("public_key", "")

            self.logger.info(f"Processing new contact: {contact_name} (key: {public_key[:16]}...)")

            # Extract additional signal information from the event
            signal_info = {}
            if metadata:
                signal_info.update(metadata)

            # Take the route, signal data and packet_hash from this contact's own ADVERT
            # packet. Its mesh-graph edges and observed path were already recorded when
            # the packet itself was processed, so they are not recorded again here.
            # Only collect RSSI/SNR for zero-hop (direct) advertisements
            packet_hash = None
            try:
                rf_entry = self._find_advert_rf_data(public_key, contact_data.get("last_advert"))
                if rf_entry:
                    routing_info = rf_entry["routing_info"]

                    # Extract packet_hash if available
                    packet_hash = routing_info.get("packet_hash") or rf_entry.get("packet_hash")

                    # Extract path information from routing_info
                    path_hex = routing_info.get("path_hex", "")
                    path_length = routing_info.get("path_length", 0)

                    # Add path information to contact_data if not already present
                    if "out_path" not in contact_data or not contact_data.get("out_path"):
                        if path_hex and path_length > 0:
                            contact_data["out_path"] = path_hex
                            contact_data["out_bytes_per_hop"] = routing_info.get("bytes_per_hop", 1) or 1
                            bph = contact_data["out_bytes_per_hop"]
                            pb = routing_info.get("path_len_byte")
                            if pb is None or pb == 255:
                                try:
                                    pb = encode_path_len_byte(path_length, bph)
                                except ValueError:
                                    pb = encode_path_len_byte(path_length, 1)
                            contact_data["out_path_hash_mode"] = (pb >> 6) & 0x03
                            contact_data["out_path_len"] = pb & 0x3F
                        elif path_length == 0:
                            contact_data["out_path"] = ""
                            contact_data["out_path_len"] = 0
                            contact_data["out_path_hash_mode"] = 0

                    # Only collect signal data for direct (zero-hop) advertisements
                    if path_length == 0:
                        # Direct advertisement - collect signal data
                        if "snr" in rf_entry:
                            signal_info["snr"] = rf_entry["snr"]
                        if "rssi" in rf_entry:
                            signal_info["rssi"] = rf_entry["rssi"]
                        signal_info["hops"] = 0
                        self.logger.debug(
                            f"📡 Direct advertisement - collecting signal data: SNR={rf_entry.get('snr')}, RSSI={rf_entry.get('rssi')}"
                        )
                    else:
                        # Multi-hop advertisement - only collect hop count, not signal data
                        signal_info["hops"] = path_length
                        self.logger.debug(
                            f"📡 Multi-hop advertisement ({path_length} hops) - skipping signal data collection"
                        )
            except Exception as e:
                self.logger.debug(f"Could not correlate RF data: {e}")

            # Log captured signal information
            if signal_info:
                self.logger.info(f"📡 Signal data: {signal_info}")
            else:
                self.logger.info("📡 No signal data available")

            # Check if this is a repeater or companion
            if hasattr(self.bot, "repeater_manager"):
                is_repeater = self.bot.repeater_manager._is_repeater_device(contact_data)
                existing_tracking = self.bot.repeater_manager.get_tracked_contact_row(public_key)
                already_on_device = self.bot.repeater_manager.is_contact_on_device(public_key)
                known_contact = already_on_device or existing_tracking is not None

                if is_repeater:
                    # REPEATER: Track directly in SQLite database (no device contact list)
                    if known_contact:
                        self.logger.info(f"📡 Known repeater advert: {contact_name} - tracking in database only")
                    else:
                        self.logger.info(f"📡 New repeater discovered: {contact_name} - tracking in database only")

                    # Track repeater in complete database with signal info
                    await self.bot.repeater_manager.track_contact_advertisement(
                        contact_data, signal_info, packet_hash=packet_hash
                    )

                    # Notify web viewer of new node
                    if viewer := self._viewer_bridge():
                        try:
                            node_data = {
                                "public_key": public_key,
                                "prefix": public_key[: self.bot.prefix_hex_chars].lower() if public_key else "",
                                "name": contact_name,
                                "role": "repeater",
                            }
                            viewer.send_mesh_node_update(node_data)
                        except Exception as e:
                            self.logger.debug(f"Failed to notify web viewer of new node: {e}")

                    # Check if auto-purge is needed (run after tracking to ensure data is captured)
                    await self.bot.repeater_manager.check_and_auto_purge()

                    self.logger.info(f"✅ Repeater {contact_name} tracked in database - not added to device contacts")
                    return
                else:
                    # COMPANION: track in DB; device add behaviour depends on auto_manage_contacts
                    auto_manage_setting = self.bot.config.get("Bot", "auto_manage_contacts", fallback="device").lower()
                    if known_contact:
                        self.logger.info(
                            "👤 Known companion advert: %s — auto_manage_contacts=%s",
                            contact_name,
                            auto_manage_setting,
                        )
                    else:
                        self.logger.info(
                            "👤 New companion discovered: %s — auto_manage_contacts=%s",
                            contact_name,
                            auto_manage_setting,
                        )

                    await self.bot.repeater_manager.track_contact_advertisement(
                        contact_data, signal_info, packet_hash=packet_hash
                    )

                    if auto_manage_setting == "false":
                        self.logger.info(
                            "Manual mode — companion %s tracked in database only (not added to device)",
                            contact_name,
                        )
                    elif auto_manage_setting == "device":
                        self.logger.info(
                            "Device mode — companion %s tracked; firmware handles addition; bot may manage capacity",
                            contact_name,
                        )
                        status = await self.bot.repeater_manager.get_contact_list_status()
                        if status and status.get("is_near_limit", False):
                            self.logger.warning(
                                "Contact list near limit (%.1f%%) — managing capacity",
                                status["usage_percentage"],
                            )
                            await self.bot.repeater_manager.manage_contact_list(auto_cleanup=True)
                        else:
                            self.logger.info(
                                "Companion %s — contact list has adequate space",
                                contact_name,
                            )
                    elif auto_manage_setting == "bot":
                        # One add per advert: NEW_CONTACT can repeat for the same packet. The
                        # tracking result can't tell, since the advert packet itself was
                        # usually tracked first. Without a packet_hash every event adds.
                        if not self._claim_new_contact_add(public_key, packet_hash):
                            self.logger.debug(
                                "Skipping add_companion — duplicate packet_hash for %s (already tracked)",
                                contact_name,
                            )
                        else:
                            self.logger.info(
                                "Bot mode — adding companion %s to device with capacity management",
                                contact_name,
                            )
                            try:
                                self._ensure_contact_meshcore_path_encoding(contact_data)
                                ok = await self.bot.repeater_manager.add_companion_from_contact_data(
                                    contact_data, contact_name, public_key
                                )
                                if not ok:
                                    self._release_new_contact_add(public_key, packet_hash)
                                    self.logger.warning(
                                        "Failed to add companion contact %s to device after managed add/retry",
                                        contact_name,
                                    )
                            except asyncio.CancelledError:
                                self._release_new_contact_add(public_key, packet_hash)
                                raise
                            except Exception as e:
                                self._release_new_contact_add(public_key, packet_hash)
                                self.logger.error("Error adding companion %s to device: %s", contact_name, e)

                            status = await self.bot.repeater_manager.get_contact_list_status()
                            if status and status.get("is_near_limit", False):
                                self.logger.warning(
                                    "Contact list near limit (%.1f%%) — managing capacity after add",
                                    status["usage_percentage"],
                                )
                                await self.bot.repeater_manager.manage_contact_list(auto_cleanup=True)
                            else:
                                self.logger.info(
                                    "Companion %s — contact list has adequate space after add attempt",
                                    contact_name,
                                )
                    else:
                        self.logger.warning(
                            "Unknown auto_manage_contacts value %r — treating as manual for %s",
                            auto_manage_setting,
                            contact_name,
                        )

                    await self.bot.repeater_manager.check_and_auto_purge()

                    if not known_contact:
                        self.bot.repeater_manager.log_purging_action(
                            "new_contact_discovered",
                            f"New contact discovered: {contact_name} (key: {public_key[:16]}...)",
                        )
                    return

        except Exception as e:
            self.logger.error(f"Error handling new contact event: {e}")
            import traceback

            self.logger.error(traceback.format_exc())
