"""How an RF log row was tied to a message, and whether that tie is evidence."""

from __future__ import annotations

# How a cached RF entry was matched to a message, recorded on the dict returned by
# MessageHandler.find_recent_rf_data. Anything other than a fallback is known to be
# this message's own packet; a fallback is merely the most recent packet heard, so its
# route belongs to some other transmission and must not be attributed (issue #80).
RF_MATCH_KEY = "_rf_match"
RF_MATCH_EXACT = "exact"
RF_MATCH_PUBKEY = "pubkey"
RF_MATCH_PARTIAL = "partial"
# Verified against the decoded message payload's own fields rather than a packet
# prefix. Channel messages have no prefix to match on, so this is the only positive
# correlation available to them (see _rf_data_matches_chan_payload).
RF_MATCH_PAYLOAD = "payload"
RF_MATCH_CHANNEL_AUTHENTICATED = "channel_authenticated"
RF_MATCH_FALLBACK = "fallback"


def rf_data_is_correlated(rf_data: dict | None) -> bool:
    """True when rf_data is known to be this message's packet, not a fallback guess."""
    if not rf_data:
        return False
    return rf_data.get(RF_MATCH_KEY, RF_MATCH_FALLBACK) != RF_MATCH_FALLBACK
