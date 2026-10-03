"""RF log correlation, mixed into MessageHandler.

Matches messages to the RF log rows they arrived in: the recent-row cache and its indexes, eviction, the authenticated channel cache, payload verification of channel rows, and the provenance tag (RF_MATCH_KEY) on every match."""

import asyncio
import time
from collections.abc import Iterable
from hashlib import sha256
from typing import Any

from .meshcore_payload_decode import channel_hash_for_key, decrypt_group_text
from .rf_match import (
    RF_MATCH_CHANNEL_AUTHENTICATED,
    RF_MATCH_EXACT,
    RF_MATCH_FALLBACK,
    RF_MATCH_KEY,
    RF_MATCH_PARTIAL,
    RF_MATCH_PAYLOAD,
    RF_MATCH_PUBKEY,
    rf_data_is_correlated,
)


class RfCorrelationMixin:
    """Mixed into MessageHandler."""

    _cache_cleanup_interval: Any
    _channel_rf_cache_timeout: Any
    _grp_txt_payload_type_int: Any
    _is_rf_data_scope_eligible: Any
    _last_cache_cleanup: Any
    _max_rf_cache_size: Any
    bot: Any
    channel_rf_data: Any
    enhanced_correlation: Any
    logger: Any
    message_timeout: Any
    recent_rf_data: Any
    rf_data_by_pubkey: Any
    rf_data_by_timestamp: Any
    rf_data_timeout: Any

    @staticmethod
    def _channel_message_identity(
        channel_idx: Any, sender_timestamp: Any, txt_type: Any, text: Any
    ) -> str | None:
        """Return the identity shared by an authenticated RF row and CHAN event."""
        try:
            idx = int(channel_idx)
            timestamp = int(sender_timestamp)
            message_type = int(txt_type)
        except (TypeError, ValueError):
            return None
        if not 0 <= idx <= 0xFFFF or not 0 <= timestamp <= 0xFFFFFFFF:
            return None
        if not 0 <= message_type <= 0xFF or not isinstance(text, str):
            return None

        digest = sha256()
        digest.update(b"meshcore-channel-message-v1\0")
        digest.update(idx.to_bytes(2, "little"))
        digest.update(timestamp.to_bytes(4, "little"))
        digest.update(bytes([message_type]))
        digest.update(text.rstrip("\x00").encode("utf-8"))
        return digest.hexdigest()

    def _channel_secrets(self) -> list[tuple[int, bytes]]:
        """Return configured channel indexes and keys without logging secrets."""
        meshcore = getattr(self.bot, "meshcore", None)
        channels = getattr(meshcore, "channels", None)
        items: Iterable[tuple[int, Any]]
        if isinstance(channels, dict):
            items = channels.items()
        elif isinstance(channels, list):
            items = enumerate(channels)
        else:
            return []

        result: list[tuple[int, bytes]] = []
        for fallback_idx, channel in items:
            if not isinstance(channel, dict):
                continue
            # Annotated Any: with a mixed Any | int default, mypy matches .get()'s
            # None-default overload and infers Any | None. int() rejects None anyway.
            raw_idx: Any = channel.get("channel_idx", fallback_idx)
            try:
                channel_idx = int(raw_idx)
            except (TypeError, ValueError):
                continue

            secret = channel.get("channel_secret")
            if isinstance(secret, str):
                try:
                    secret = bytes.fromhex(secret)
                except ValueError:
                    secret = None
            if not isinstance(secret, (bytes, bytearray)):
                key_hex = channel.get("channel_key_hex")
                if isinstance(key_hex, str):
                    try:
                        secret = bytes.fromhex(key_hex)
                    except ValueError:
                        secret = None
            if isinstance(secret, bytearray):
                secret = bytes(secret)
            if isinstance(secret, bytes) and len(secret) == 16 and any(secret):
                result.append((channel_idx, secret))
        return result

    def _decode_authenticated_channel_identity(
        self, packet_info: dict[str, Any] | None, *, include_text: bool = False
    ) -> dict[str, Any] | None:
        """Authenticate a decoded GRP_TXT packet and derive its CHAN identity.

        ``include_text`` adds the full decrypted text as ``channel_message`` and
        the text without its sender prefix as ``channel_text``.
        """
        if not packet_info or packet_info.get("payload_type") != self._grp_txt_payload_type_int():
            return None
        payload_hex = packet_info.get("payload_hex")
        if not isinstance(payload_hex, str):
            return None
        try:
            group_payload = bytes.fromhex(payload_hex)
        except ValueError:
            return None
        if len(group_payload) < 3:
            return None

        channel_hash = f"{group_payload[0]:02x}"
        cipher_mac = group_payload[1:3]
        ciphertext = group_payload[3:]
        for channel_idx, secret in self._channel_secrets():
            if channel_hash_for_key(secret) != channel_hash:
                continue
            decrypted = decrypt_group_text(ciphertext, cipher_mac, secret)
            if not decrypted:
                continue
            txt_type = int(decrypted["flags"]) >> 2
            identity = self._channel_message_identity(
                channel_idx,
                decrypted["timestamp"],
                txt_type,
                decrypted["message"],
            )
            if identity is None:
                return None
            result = {
                "channel_message_id": identity,
                "channel_idx": channel_idx,
                "channel_attempt": int(decrypted["flags"]) & 0x03,
            }
            if include_text:
                result["channel_text"] = decrypted["text"]
                result["channel_message"] = decrypted["message"]
            return result
        return None

    def _cache_authenticated_channel_rf_data(
        self,
        rf_data: dict[str, Any],
        packet_info: dict[str, Any] | None,
        current_time: float,
    ) -> None:
        identity = self._decode_authenticated_channel_identity(packet_info)
        if identity is None:
            return
        rf_data.update(identity)
        self.channel_rf_data.append(rf_data)
        cutoff = current_time - self._channel_rf_cache_timeout
        self.channel_rf_data = [
            row for row in self.channel_rf_data if row.get("timestamp", 0) >= cutoff
        ]
        if len(self.channel_rf_data) > self._max_rf_cache_size:
            self.channel_rf_data = self.channel_rf_data[-self._max_rf_cache_size :]

    def _find_authenticated_channel_rf_data(
        self, payload: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, bool]:
        """Find the first RF reception for an authenticated channel message.

        The companion logs raw RF before duplicate suppression and queues only
        the first unseen channel packet. Repeater echoes share the packet hash,
        so the earliest row is the reception represented by CHANNEL_MSG_RECV.
        The boolean prevents weaker matching after an authenticated ambiguity.
        """
        if not payload:
            return None, False
        identity = self._channel_message_identity(
            payload.get("channel_idx"),
            payload.get("sender_timestamp"),
            payload.get("txt_type"),
            payload.get("text"),
        )
        if identity is None:
            return None, False
        now = time.time()
        max_age = getattr(
            self,
            "_channel_rf_cache_timeout",
            max(300.0, getattr(self, "rf_data_timeout", 15.0) * 4),
        )
        matches = [
            row
            for row in getattr(self, "channel_rf_data", [])
            if row.get("channel_message_id") == identity
            and isinstance(row.get("timestamp"), (int, float))
            and now - row["timestamp"] <= max_age
        ]
        if not matches:
            return None, False

        packet_hashes = {row.get("packet_hash") for row in matches}
        if len(packet_hashes) != 1 or not all(packet_hashes):
            self.logger.warning(
                "Authenticated channel message matched %d distinct or unidentified packets; "
                "leaving RF attribution unresolved",
                len(packet_hashes),
            )
            return None, True

        selected = min(matches, key=lambda row: row.get("timestamp", float("inf")))
        return {**selected, RF_MATCH_KEY: RF_MATCH_CHANNEL_AUTHENTICATED}, True

    def _cleanup_stale_cache_entries(self, current_time: float | None = None) -> None:
        """Remove stale entries from RF data caches and enforce maximum size limits.

        Args:
            current_time: Optional timestamp to use as "now". Defaults to time.time().
        """
        if current_time is None:
            current_time = time.time()

        # Only run periodic cleanup if enough time has passed
        if current_time - self._last_cache_cleanup < self._cache_cleanup_interval:
            # Still do basic timeout cleanup, but skip size enforcement
            cutoff_time = current_time - self.rf_data_timeout

            # Clean timestamp-indexed cache (timeout only)
            stale_timestamps = [ts for ts in self.rf_data_by_timestamp if ts < cutoff_time]
            for ts in stale_timestamps:
                del self.rf_data_by_timestamp[ts]

            # Clean pubkey-indexed cache (timeout only)
            for pubkey in list(self.rf_data_by_pubkey.keys()):
                self.rf_data_by_pubkey[pubkey] = [
                    data
                    for data in self.rf_data_by_pubkey[pubkey]
                    if current_time - data["timestamp"] < self.rf_data_timeout
                ]
                if not self.rf_data_by_pubkey[pubkey]:
                    del self.rf_data_by_pubkey[pubkey]

            # Clean recent_rf_data list (timeout only)
            self.recent_rf_data = [
                data for data in self.recent_rf_data if current_time - data["timestamp"] < self.rf_data_timeout
            ]
            return

        # Full cleanup with size enforcement
        self._last_cache_cleanup = current_time
        cutoff_time = current_time - self.rf_data_timeout

        # Clean timestamp-indexed cache
        stale_timestamps = [ts for ts in self.rf_data_by_timestamp if ts < cutoff_time]
        for ts in stale_timestamps:
            del self.rf_data_by_timestamp[ts]

        # Enforce maximum size on timestamp cache (keep most recent)
        if len(self.rf_data_by_timestamp) > self._max_rf_cache_size:
            sorted_items = sorted(
                self.rf_data_by_timestamp.items(), key=lambda x: x[1].get("timestamp", 0), reverse=True
            )
            self.rf_data_by_timestamp = dict(sorted_items[: self._max_rf_cache_size])

        # Clean pubkey-indexed cache
        for pubkey in list(self.rf_data_by_pubkey.keys()):
            self.rf_data_by_pubkey[pubkey] = [
                data
                for data in self.rf_data_by_pubkey[pubkey]
                if current_time - data["timestamp"] < self.rf_data_timeout
            ]
            if not self.rf_data_by_pubkey[pubkey]:
                del self.rf_data_by_pubkey[pubkey]

        # Enforce maximum size on pubkey cache (keep most recent per pubkey)
        total_pubkey_entries = sum(len(entries) for entries in self.rf_data_by_pubkey.values())
        if total_pubkey_entries > self._max_rf_cache_size:
            # Sort all entries by timestamp and keep most recent
            all_pubkey_entries = []
            for pubkey, entries in self.rf_data_by_pubkey.items():
                for entry in entries:
                    all_pubkey_entries.append((pubkey, entry))
            all_pubkey_entries.sort(key=lambda x: x[1].get("timestamp", 0), reverse=True)

            # Rebuild pubkey cache with only the most recent entries
            self.rf_data_by_pubkey = {}
            for pubkey, entry in all_pubkey_entries[: self._max_rf_cache_size]:
                if pubkey not in self.rf_data_by_pubkey:
                    self.rf_data_by_pubkey[pubkey] = []
                self.rf_data_by_pubkey[pubkey].append(entry)

        # Clean recent_rf_data list
        self.recent_rf_data = [
            data for data in self.recent_rf_data if current_time - data["timestamp"] < self.rf_data_timeout
        ]

        # Enforce maximum size on recent_rf_data (keep most recent)
        if len(self.recent_rf_data) > self._max_rf_cache_size:
            self.recent_rf_data.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            self.recent_rf_data = self.recent_rf_data[: self._max_rf_cache_size]

    async def _correlate_channel_message_rf_data(
        self,
        message_packet_prefix: str | None,
        message_pubkey: str,
        payload: dict[str, Any],
        *,
        scope_eligible_only: bool,
        extended_timeout: float,
    ) -> dict[str, Any] | None:
        """Correlate a channel message with cached RF log rows (strategies 1–4)."""
        authenticated, authenticated_identity_seen = self._find_authenticated_channel_rf_data(payload)
        if authenticated_identity_seen:
            if authenticated is None:
                return None
            if scope_eligible_only and not self._is_rf_data_scope_eligible(authenticated):
                return None
            self.logger.debug(
                "Authenticated channel message matched first RF reception %s (packet %s)",
                (authenticated.get("packet_prefix") or "?")[:16],
                authenticated.get("packet_hash") or "?",
            )
            return authenticated

        recent_rf_data: dict[str, Any] | None = None

        if message_packet_prefix:
            recent_rf_data = self.find_recent_rf_data(
                message_packet_prefix, scope_eligible_only=scope_eligible_only
            )
        elif message_pubkey:
            recent_rf_data = self.find_recent_rf_data(
                message_pubkey, scope_eligible_only=scope_eligible_only
            )

        if not recent_rf_data and self.enhanced_correlation and not scope_eligible_only:
            # The RF log row can trail the message event; give it a moment, then look again
            await asyncio.sleep(0.1)
            authenticated, authenticated_identity_seen = self._find_authenticated_channel_rf_data(payload)
            if authenticated_identity_seen:
                if authenticated is None:
                    return None
                if scope_eligible_only and not self._is_rf_data_scope_eligible(authenticated):
                    return None
                return authenticated
            recent_rf_data = self.find_recent_rf_data(payload.get("pubkey_prefix", ""))

        if not recent_rf_data:
            if message_packet_prefix:
                recent_rf_data = self.find_recent_rf_data(
                    message_packet_prefix,
                    max_age_seconds=extended_timeout,
                    scope_eligible_only=scope_eligible_only,
                )
            elif message_pubkey:
                recent_rf_data = self.find_recent_rf_data(
                    message_pubkey,
                    max_age_seconds=extended_timeout,
                    scope_eligible_only=scope_eligible_only,
                )

        if not recent_rf_data:
            recent_rf_data = self.find_recent_rf_data(
                max_age_seconds=extended_timeout,
                scope_eligible_only=scope_eligible_only,
            )

        # A channel message has no packet prefix or pubkey to match on, so everything
        # above lands on the most-recent-packet fallback. The decoded payload carries
        # its own copies of fields the RF row also has, though, so the cache can be
        # searched for this message's own packet rather than assuming the newest row.
        if recent_rf_data is not None and not rf_data_is_correlated(recent_rf_data):
            verified = self._find_rf_row_matching_chan_payload(
                payload, scope_eligible_only=scope_eligible_only
            )
            if verified is not None:
                if verified.get("packet_prefix") != recent_rf_data.get("packet_prefix"):
                    self.logger.debug(
                        "Most recent RF row %s is not this message's packet (a later "
                        "reception of another packet); the payload matches %s instead",
                        (recent_rf_data.get("packet_prefix") or "?")[:16],
                        (verified.get("packet_prefix") or "?")[:16],
                    )
                recent_rf_data = {**verified, RF_MATCH_KEY: RF_MATCH_PAYLOAD}
                self.logger.debug(
                    "Verified RF row %s against the channel payload (GRP_TXT, path_len=%s, "
                    "SNR=%s); treating it as this message's packet",
                    (recent_rf_data.get("packet_prefix") or "?")[:16],
                    payload.get("path_len"),
                    payload.get("SNR"),
                )

        return recent_rf_data

    def _find_rf_row_matching_chan_payload(
        self, payload: dict[str, Any] | None, *, scope_eligible_only: bool = False
    ) -> dict[str, Any] | None:
        """Return the cached RF row this channel message was received on, or None.

        Checking only the newest row assumes the RF log row and the decoded CHAN
        event for one reception arrive back to back with nothing in between. On a
        dense mesh they do not: a repeater's echo of the very same packet is
        routinely logged in the gap, so the newest row is that echo — a different
        path length and a different measured SNR — and the message loses its route
        even though its own row is sitting in the cache (#255). Search the cache
        instead, and let _rf_data_matches_chan_payload decide which row is ours.

        Two rows can only both match when they are receptions of the same packet
        (same hash) that also agree on path length and SNR; anything else is
        ambiguous and keeps the fallback tag, because a guess is what #80 cost.
        """
        if not payload:
            return None

        matches = [
            row
            for row in self.recent_rf_data
            if self._rf_data_matches_chan_payload(row, payload)
            and (not scope_eligible_only or self._is_rf_data_scope_eligible(row))
        ]
        if not matches:
            return None

        if len(matches) > 1:
            hashes = {row.get("packet_hash") for row in matches}
            if len(hashes) != 1 or not all(hashes):
                self.logger.debug(
                    "%d cached RF rows agree with the channel payload across %d packet(s); "
                    "leaving the route unresolved rather than guessing",
                    len(matches),
                    len(hashes),
                )
                return None

        return max(matches, key=lambda row: row["timestamp"])

    def _rf_data_matches_chan_payload(
        self, rf_data: dict[str, Any] | None, payload: dict[str, Any] | None
    ) -> bool:
        """True when a cached RF row is confirmed to be this channel message's packet.

        MeshCore's CHAN event carries neither raw_hex nor a pubkey prefix, so the
        prefix strategies in find_recent_rf_data cannot fire and every channel
        message falls through to the most recent packet. That fallback is normally
        right — the firmware emits the RF log row and the decoded CHAN event for the
        same reception back to back — but "normally right" is not evidence, and #80
        is what happens when a guess is treated as one.

        The CHAN payload does, however, restate three things the RF row records
        independently: payload type, path length and SNR. Requiring all three to
        agree on a row heard within the correlation window turns the fallback into
        a checked hypothesis. SNR is the discriminating one: it is the measured
        value of a single reception, quantised to 0.25 dB, so an unrelated packet
        matching all three is improbable rather than merely unlikely.

        _find_rf_row_matching_chan_payload applies this to every cached row rather
        than only the newest, so a repeater echo logged in between cannot displace
        the message's own row (#255).
        """
        if not rf_data or not payload:
            return False

        if rf_data.get("payload_type_int") != self._grp_txt_payload_type_int():
            return False

        # Bound the age: the RF row and its CHAN event arrive together, so an old row
        # that happens to agree is a coincidence, not this message.
        timestamp = rf_data.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            return False
        if time.time() - timestamp > self.message_timeout:
            return False

        payload_path_len = payload.get("path_len")
        rf_path_len = (rf_data.get("routing_info") or {}).get("path_length")
        if payload_path_len is None or rf_path_len is None:
            return False
        if int(payload_path_len) != int(rf_path_len):
            return False

        payload_snr = payload.get("SNR")
        rf_snr = rf_data.get("snr")
        if payload_snr is None or rf_snr is None:
            return False
        try:
            # Tolerance covers float representation only; SNR is quantised to 0.25 dB.
            return abs(float(payload_snr) - float(rf_snr)) < 1e-6
        except (TypeError, ValueError):
            return False

    def find_recent_rf_data(
        self,
        correlation_key: str | None = None,
        max_age_seconds: float | None = None,
        *,
        scope_eligible_only: bool = False,
    ) -> dict[str, Any] | None:
        """Find recent RF data for SNR/RSSI and packet decoding with improved correlation

        Args:
            correlation_key: Can be either:
                - packet_prefix (from raw_hex[:32]) for RF data correlation
                - pubkey_prefix (from message payload) for message correlation
            max_age_seconds: Maximum age of RF cache entries to consider.
            scope_eligible_only: When True, only return TC_FLOOD / GRP_TXT rows suitable
                for flood_scopes HMAC matching. Strategy 4 (most-recent fallback) skips
                unrelated packets such as ADVERT.

        Returns:
            A shallow copy of the cached RF entry with ``RF_MATCH_KEY`` describing how it
            was found: "exact", "pubkey", "partial", or "fallback". A "fallback" result is
            the most recent packet in the cache and is **not** known to be this message's
            packet, so its route must not be attributed to the message (see issue #80).
        """
        import time

        current_time = time.time()

        # Use default timeout if not specified
        if max_age_seconds is None:
            max_age_seconds = self.rf_data_timeout

        # Filter recent RF data by age
        recent_data = [data for data in self.recent_rf_data if current_time - data["timestamp"] < max_age_seconds]

        if not recent_data:
            self.logger.debug(f"No recent RF data found within {max_age_seconds}s window")
            return None

        def _accept(data: dict[str, Any], how: str) -> dict[str, Any] | None:
            if scope_eligible_only and not self._is_rf_data_scope_eligible(data):
                return None
            # Shallow copy so the provenance tag never persists into the cache.
            return {**data, RF_MATCH_KEY: how}

        # Strategy 1: Try exact packet prefix match first (for RF data correlation)
        if correlation_key:
            for data in recent_data:
                rf_packet_prefix = data.get("packet_prefix", "") or ""
                if rf_packet_prefix == correlation_key:
                    accepted = _accept(data, RF_MATCH_EXACT)
                    if accepted:
                        self.logger.debug(f"Found exact packet prefix match: {rf_packet_prefix}")
                        return accepted

        # Strategy 2: Try pubkey prefix match (for message correlation).
        # A pubkey prefix identifies a sender, not one transmission. When the cache
        # holds several packets from that sender the match is ambiguous, so take the
        # newest and mark it non-authoritative rather than attributing its route.
        if correlation_key:
            pubkey_matches = [
                data for data in recent_data
                if (data.get("pubkey_prefix", "") or "") == correlation_key
            ]
            if pubkey_matches:
                newest = max(pubkey_matches, key=lambda x: x["timestamp"])
                unique = len(pubkey_matches) == 1
                accepted = _accept(newest, RF_MATCH_PUBKEY if unique else RF_MATCH_FALLBACK)
                if accepted:
                    if unique:
                        self.logger.debug(f"Found exact pubkey prefix match: {correlation_key}")
                    else:
                        self.logger.debug(
                            "%d cached packets share pubkey prefix %s; using the newest "
                            "for signal only, not for routing",
                            len(pubkey_matches), correlation_key,
                        )
                    return accepted

        # Strategy 3: Try partial packet prefix matches. Same ambiguity caveat as
        # above: a shared 16-character prefix is not proof of the same transmission.
        if correlation_key:
            partial_matches = []
            for data in recent_data:
                rf_packet_prefix = data.get("packet_prefix", "") or ""
                min_length = min(len(rf_packet_prefix), len(correlation_key), 16)
                if rf_packet_prefix[:min_length] == correlation_key[:min_length] and min_length >= 16:
                    partial_matches.append(data)
            if partial_matches:
                newest = max(partial_matches, key=lambda x: x["timestamp"])
                unique = len(partial_matches) == 1
                accepted = _accept(newest, RF_MATCH_PARTIAL if unique else RF_MATCH_FALLBACK)
                if accepted:
                    if unique:
                        self.logger.debug(
                            f"Found partial packet prefix match for {correlation_key[:16]}..."
                        )
                    else:
                        self.logger.debug(
                            "%d cached packets share the partial prefix %s...; using the "
                            "newest for signal only, not for routing",
                            len(partial_matches), correlation_key[:16],
                        )
                    return accepted

        # Strategy 4: Use most recent data (fallback for timing issues)
        if recent_data:
            candidates = recent_data
            if scope_eligible_only:
                candidates = [d for d in recent_data if self._is_rf_data_scope_eligible(d)]
                if not candidates:
                    self.logger.debug(
                        "No scope-eligible RF data in cache for fallback "
                        "(need TC_FLOOD GRP_TXT with transport code)"
                    )
                    return None
            most_recent = max(candidates, key=lambda x: x["timestamp"])
            packet_prefix = most_recent.get("packet_prefix", "unknown")
            if scope_eligible_only:
                self.logger.debug(
                    "Using most recent scope-eligible RF data (fallback): %s at %s",
                    packet_prefix,
                    most_recent["timestamp"],
                )
            else:
                self.logger.debug(
                    f"Using most recent RF data (fallback): {packet_prefix} at {most_recent['timestamp']}"
                )
            return {**most_recent, RF_MATCH_KEY: RF_MATCH_FALLBACK}

        return None
