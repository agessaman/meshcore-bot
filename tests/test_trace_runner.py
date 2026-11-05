"""Tests for modules.trace_runner: retries, late replies and timeouts."""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from meshcore import EventType

from modules.trace_runner import _get_timeout_seconds, run_trace


class _FakeMeshCore:
    """Dispatches TRACE_DATA to subscribers; replies are scheduled per sent tag."""

    def __init__(self, reply_delays=None, error_reason=None):
        # reply_delays: {attempt index: seconds until that attempt's reply arrives}
        self.reply_delays = reply_delays or {}
        self.error_reason = error_reason
        self.subscribers = []
        self.sent_tags = []
        self.commands = SimpleNamespace(send_trace=self._send_trace)

    def subscribe(self, event_type, callback, attribute_filters=None):
        assert event_type == EventType.TRACE_DATA
        entry = [callback]
        self.subscribers.append(entry)
        return SimpleNamespace(unsubscribe=lambda: self.subscribers.remove(entry))

    def dispatch(self, tag):
        payload = {"tag": tag, "flags": 1, "path_len": 1, "path": [{"hash": "0101", "snr": 11.5}, {"snr": 12.5}]}
        event = SimpleNamespace(type=EventType.TRACE_DATA, payload=payload, attributes={"tag": tag, "auth_code": 0})
        for (callback,) in list(self.subscribers):
            callback(event)

    async def _send_trace(self, auth_code, tag, flags, path):
        attempt = len(self.sent_tags)
        self.sent_tags.append(tag)
        if self.error_reason:
            return SimpleNamespace(type=EventType.ERROR, payload={"reason": self.error_reason})
        delay = self.reply_delays.get(attempt)
        if delay == 0:
            self.dispatch(tag)
        elif delay is not None:
            asyncio.get_running_loop().call_later(delay, self.dispatch, tag)
        return SimpleNamespace(type=EventType.MSG_SENT, payload={})


def _make_bot(meshcore, retry_delay="0.05", attempts="2"):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Trace_Command")
    config.set("Trace_Command", "trace_retry_count", attempts)
    config.set("Trace_Command", "trace_retry_delay_seconds", retry_delay)
    bot.config = config
    bot.meshcore = meshcore
    bot.transmission_tracker = None
    return bot


@pytest.fixture
def tags():
    with patch("modules.trace_runner.random.randint", side_effect=[111, 222, 333]):
        yield


@pytest.mark.usefixtures("tags")
class TestRunTrace:
    def test_reply_to_first_attempt(self):
        mc = _FakeMeshCore(reply_delays={0: 0.02})
        result = asyncio.run(run_trace(_make_bot(mc), path=["0101"], timeout_seconds=0.2))
        assert result.success and result.tag == 111
        assert result.path_nodes[0] == {"hash": "0101", "snr": 11.5}
        assert mc.sent_tags == [111]
        assert mc.subscribers == []

    def test_late_reply_to_first_attempt_counts_during_retry(self):
        # The first trace's reply arrives after its own timeout, while the retry waits
        mc = _FakeMeshCore(reply_delays={0: 0.3})
        result = asyncio.run(run_trace(_make_bot(mc), path=["0101"], timeout_seconds=0.2))
        assert result.success and result.tag == 111
        assert mc.sent_tags == [111, 222]

    def test_late_reply_during_retry_delay_skips_the_retry(self):
        mc = _FakeMeshCore(reply_delays={0: 0.22})
        bot = _make_bot(mc, retry_delay="0.3")
        result = asyncio.run(run_trace(bot, path=["0101"], timeout_seconds=0.2))
        assert result.success and result.tag == 111
        assert mc.sent_tags == [111]

    def test_reply_dispatched_before_waiting_is_not_missed(self):
        mc = _FakeMeshCore(reply_delays={0: 0})
        result = asyncio.run(run_trace(_make_bot(mc), path=["0101"], timeout_seconds=0.2))
        assert result.success and result.tag == 111
        assert mc.sent_tags == [111]

    def test_no_reply_fails_after_every_attempt(self):
        mc = _FakeMeshCore()
        result = asyncio.run(run_trace(_make_bot(mc), path=["0101", "7a2a"], timeout_seconds=0.05))
        assert not result.success
        assert result.error_message == "No trace response within timeout (path: 0101,7a2a)"
        assert mc.sent_tags == [111, 222]
        assert mc.subscribers == []

    def test_reply_after_last_timeout_is_too_late(self):
        mc = _FakeMeshCore(reply_delays={1: 0.5})
        result = asyncio.run(run_trace(_make_bot(mc), path=["0101"], timeout_seconds=0.05))
        assert not result.success

    def test_other_tags_are_ignored(self):
        mc = _FakeMeshCore()

        async def go():
            asyncio.get_running_loop().call_later(0.02, mc.dispatch, 999)
            return await run_trace(_make_bot(mc, attempts="1"), path=["0101"], timeout_seconds=0.1)

        result = asyncio.run(go())
        assert not result.success

    def test_send_error_is_reported(self):
        mc = _FakeMeshCore(error_reason="no route")
        result = asyncio.run(run_trace(_make_bot(mc), path=["0101"], timeout_seconds=0.05))
        assert not result.success
        assert result.error_message == "no route"
        assert mc.sent_tags == [111, 222]
        assert mc.subscribers == []


class TestTimeout:
    def test_defaults_allow_for_repeater_tx_delay(self):
        bot = MagicMock()
        bot.config = configparser.ConfigParser()
        assert _get_timeout_seconds(bot, ["0101", "7a2a", "0101"]) == pytest.approx(6.5)

    def test_configured_values(self):
        bot = MagicMock()
        bot.config = configparser.ConfigParser()
        bot.config.read_dict({"Trace_Command": {"timeout_base_seconds": "6", "timeout_per_hop_seconds": "1.5"}})
        assert _get_timeout_seconds(bot, ["01", "02", "03"]) == pytest.approx(10.5)
        assert _get_timeout_seconds(bot, None) == pytest.approx(7.5)
