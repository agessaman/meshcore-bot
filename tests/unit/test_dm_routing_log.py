"""The INFO route lines for a pubkey-correlated DM are still logged."""

from unittest.mock import MagicMock

from modules.message_handler import RF_MATCH_EXACT, RF_MATCH_FALLBACK, RF_MATCH_KEY, MessageHandler


def _handler(rf):
    mh = object.__new__(MessageHandler)
    mh.logger = MagicMock()
    mh.find_recent_rf_data = MagicMock(return_value=rf)
    mh.decode_meshcore_packet = MagicMock(return_value={"route_type": 1})
    return mh


def _infos(mh):
    return [c.args[0] for c in mh.logger.info.call_args_list]


def test_routed_dm_logs_message_routing():
    mh = _handler({"raw_hex": "aa", RF_MATCH_KEY: RF_MATCH_EXACT,
                   "routing_info": {"path_length": 2, "path_nodes": ["01", "02"], "route_type": "FLOOD"}})
    mh._log_dm_routing_from_rf("ab12")
    assert _infos(mh) == ["🛣️  MESSAGE ROUTING: 01,02 (2 hops via FLOOD)"]


def test_direct_dm_logs_direct_message():
    mh = _handler({"raw_hex": "aa", RF_MATCH_KEY: RF_MATCH_EXACT,
                   "routing_info": {"path_length": 0, "route_type": "DIRECT"}})
    mh._log_dm_routing_from_rf("ab12")
    assert _infos(mh) == ["📡 DIRECT MESSAGE: Direct via DIRECT"]


def test_uncorrelated_or_missing_rf_logs_nothing_at_info():
    mh = _handler({"raw_hex": "aa", RF_MATCH_KEY: RF_MATCH_FALLBACK,
                   "routing_info": {"path_length": 0, "route_type": "DIRECT"}})
    mh._log_dm_routing_from_rf("ab12")
    assert _infos(mh) == []
    mh = _handler(None)
    mh._log_dm_routing_from_rf("ab12")
    assert _infos(mh) == []
