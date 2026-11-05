"""Tests for WebhookService."""

import re
from configparser import ConfigParser
from unittest.mock import AsyncMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.service_plugins.webhook_service import WebhookService


def _strip_part_suffix(text: str) -> str:
    """Drop a trailing " (i/n)" ordering marker so content can be compared."""
    return re.sub(r" \(\d+/\d+\)$", "", text)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_bot(mock_logger, extra_cfg=None):
    """Return a minimal mock bot for WebhookService."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Webhook")
    bot.config.set("Webhook", "enabled", "true")
    bot.config.set("Webhook", "host", "127.0.0.1")
    bot.config.set("Webhook", "port", "8765")
    bot.config.set("Webhook", "secret_token", "")
    bot.config.set("Webhook", "max_message_length", "200")
    bot.config.set("Webhook", "allowed_channels", "")
    if extra_cfg:
        for key, val in extra_cfg.items():
            bot.config.set("Webhook", key, val)
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=True)
    bot.command_manager.send_dm = AsyncMock(return_value=True)
    # The service sizes bodies against the real budget/split helpers, so a bare
    # Mock would hand a Mock to the splitter. Use the production implementations.
    bot.command_manager.split_text_into_utf8_chunks = (
        CommandManager.split_text_into_utf8_chunks
    )
    bot.command_manager.split_text_into_numbered_utf8_chunks = (
        CommandManager.split_text_into_numbered_utf8_chunks
    )
    bot.command_manager.links_split_across = CommandManager.links_split_across
    bot.command_manager.channel_body_budget = Mock(return_value=130)
    bot.connected = True
    return bot


def _make_request(body=None, headers=None, remote="127.0.0.1"):
    """Return a mock aiohttp Request."""
    req = Mock()
    req.remote = remote
    req.headers = headers or {}
    req.json = AsyncMock(return_value=body or {})
    return req


def _make_service(mock_logger, extra_cfg=None):
    bot = _make_bot(mock_logger, extra_cfg)
    return WebhookService(bot), bot


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------


class TestInit:
    def test_enabled_reads_from_config(self, mock_logger):
        svc, _ = _make_service(mock_logger)
        assert svc.enabled is True

    def test_disabled_when_no_section(self, mock_logger):
        bot = Mock()
        bot.logger = mock_logger
        bot.config = ConfigParser()
        svc = WebhookService(bot)
        assert svc.enabled is False

    def test_allowed_channels_parsed(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"allowed_channels": "general, alerts"})
        assert "general" in svc.allowed_channels
        assert "alerts" in svc.allowed_channels

    def test_hash_stripped_from_channel_names(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"allowed_channels": "#general,#alerts"})
        assert "general" in svc.allowed_channels
        assert "alerts" in svc.allowed_channels

    def test_empty_allowed_channels_means_all(self, mock_logger):
        svc, _ = _make_service(mock_logger)
        assert svc.allowed_channels == set()

    def test_secret_token_loaded(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "s3cr3t"})
        assert svc.secret_token == "s3cr3t"


# ---------------------------------------------------------------------------
# TestVerifyToken
# ---------------------------------------------------------------------------


class TestVerifyToken:
    def test_bearer_token_accepted(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "abc123"})
        req = _make_request(headers={"Authorization": "Bearer abc123"})
        assert svc._verify_token(req) is True

    def test_wrong_bearer_rejected(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "abc123"})
        req = _make_request(headers={"Authorization": "Bearer wrong"})
        assert svc._verify_token(req) is False

    def test_x_webhook_token_accepted(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "abc123"})
        req = _make_request(headers={"X-Webhook-Token": "abc123"})
        assert svc._verify_token(req) is True

    def test_no_token_header_rejected(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "abc123"})
        req = _make_request(headers={})
        assert svc._verify_token(req) is False

    def test_case_insensitive_bearer_prefix(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "tok"})
        req = _make_request(headers={"Authorization": "BEARER tok"})
        assert svc._verify_token(req) is True


# ---------------------------------------------------------------------------
# TestHandleWebhook — readiness
# ---------------------------------------------------------------------------


class TestHandleWebhookReadiness:
    @pytest.mark.asyncio
    async def test_not_connected_returns_503(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        bot.connected = False
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_not_connected_does_not_dispatch(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        bot.connected = False
        req = _make_request(body={"channel": "general", "message": "hi"})
        await svc._handle_webhook(req)
        bot.command_manager.send_channel_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connected_true_proceeds_normally(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        bot.connected = True
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_missing_connected_attr_defaults_to_not_ready(self, mock_logger):
        """If `bot.connected` isn't present at all, fail safe (503) rather than assume readiness."""
        svc, bot = _make_service(mock_logger)
        del bot.connected
        # Mock() raises AttributeError for deleted attrs, so getattr(..., False) applies.
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_rate_limit_checked_before_readiness(self, mock_logger):
        """Rate limiting should still apply even while the bot isn't connected yet."""
        svc, bot = _make_service(mock_logger, {"rate_limit_per_minute": "1"})
        bot.connected = False
        req = _make_request(body={"channel": "general", "message": "hi"})
        await svc._handle_webhook(req)
        resp = await svc._handle_webhook(req)
        assert resp.status == 429


# ---------------------------------------------------------------------------
# TestHandleWebhook — auth
# ---------------------------------------------------------------------------


class TestHandleWebhookAuth:
    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "secret"})
        req = _make_request(body={"channel": "general", "message": "hi"}, headers={})
        resp = await svc._handle_webhook(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_correct_token_returns_200(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"secret_token": "secret"})
        req = _make_request(
            body={"channel": "general", "message": "hi"},
            headers={"Authorization": "Bearer secret"},
        )
        resp = await svc._handle_webhook(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_token_required_when_secret_empty(self, mock_logger):
        svc, _ = _make_service(mock_logger)  # no secret_token
        req = _make_request(body={"channel": "general", "message": "hi"}, headers={})
        resp = await svc._handle_webhook(req)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# TestHandleWebhook — validation
# ---------------------------------------------------------------------------


class TestHandleWebhookValidation:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, mock_logger):
        svc, _ = _make_service(mock_logger)
        req = _make_request()
        req.json = AsyncMock(side_effect=Exception("bad json"))
        resp = await svc._handle_webhook(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_message_returns_400(self, mock_logger):
        svc, _ = _make_service(mock_logger)
        req = _make_request(body={"channel": "general"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_channel_and_dm_returns_400(self, mock_logger):
        svc, _ = _make_service(mock_logger)
        req = _make_request(body={"message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_disallowed_channel_returns_400(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"allowed_channels": "alerts"})
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_allowed_channel_passes(self, mock_logger):
        svc, _ = _make_service(mock_logger, {"allowed_channels": "general"})
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# TestHandleWebhook — dispatch
# ---------------------------------------------------------------------------


class TestHandleWebhookDispatch:
    @pytest.mark.asyncio
    async def test_channel_message_dispatched(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "general", "message": "Hello!"})
        await svc._handle_webhook(req)
        bot.command_manager.send_channel_message.assert_awaited_once()
        call_args = bot.command_manager.send_channel_message.call_args
        assert call_args[0][0] == "general"
        assert call_args[0][1] == "Hello!"

    @pytest.mark.asyncio
    async def test_hash_stripped_from_channel_in_body(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "#general", "message": "Hello!"})
        await svc._handle_webhook(req)
        call_args = bot.command_manager.send_channel_message.call_args
        assert call_args[0][0] == "general"

    @pytest.mark.asyncio
    async def test_dm_dispatched(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"dm_to": "Alice", "message": "Hi Alice!"})
        await svc._handle_webhook(req)
        bot.command_manager.send_dm.assert_awaited_once()
        call_args = bot.command_manager.send_dm.call_args
        assert call_args[0][0] == "Alice"
        assert call_args[0][1] == "Hi Alice!"

    @pytest.mark.asyncio
    async def test_flood_scope_in_body_passed_to_send(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "general", "message": "Hello!", "flood_scope": "west"}
        )
        await svc._handle_webhook(req)
        _, kwargs = bot.command_manager.send_channel_message.call_args
        assert kwargs.get("scope") == "#west"

    @pytest.mark.asyncio
    async def test_flood_scope_null_falls_back_to_config(self, mock_logger):
        svc, bot = _make_service(mock_logger, {"flood_scope": "#sea"})
        req = _make_request(
            body={"channel": "general", "message": "Hello!", "flood_scope": None}
        )
        await svc._handle_webhook(req)
        _, kwargs = bot.command_manager.send_channel_message.call_args
        assert kwargs.get("scope") == "#sea"

    @pytest.mark.asyncio
    async def test_config_flood_scope_used_when_body_omits_it(self, mock_logger):
        svc, bot = _make_service(mock_logger, {"flood_scope": "#sea"})
        req = _make_request(body={"channel": "general", "message": "Hello!"})
        await svc._handle_webhook(req)
        _, kwargs = bot.command_manager.send_channel_message.call_args
        assert kwargs.get("scope") == "#sea"

    @pytest.mark.asyncio
    async def test_long_message_truncated(self, mock_logger):
        svc, bot = _make_service(mock_logger, {"max_message_length": "10"})
        long_msg = "A" * 100
        req = _make_request(body={"channel": "general", "message": long_msg})
        await svc._handle_webhook(req)
        sent = bot.command_manager.send_channel_message.call_args[0][1]
        assert len(sent) == 10

    @pytest.mark.asyncio
    async def test_send_failure_returns_500(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        bot.command_manager.send_channel_message = AsyncMock(
            side_effect=RuntimeError("mesh offline")
        )
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_send_returns_false_returns_500(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        bot.command_manager.send_channel_message = AsyncMock(return_value=False)
        req = _make_request(body={"channel": "general", "message": "hi"})
        resp = await svc._handle_webhook(req)
        assert resp.status == 500


# ---------------------------------------------------------------------------
# TestChunking — a message longer than one frame must go out in several parts
# ---------------------------------------------------------------------------


class TestChunking:
    """The 200-char cap is a total, not a frame size.

    A MeshCore channel body only holds ~130 bytes, so relaying a longer payload
    whole gets it dropped by the device with no acknowledgement — the send burns
    its retries and the stalled transport then looks dead.
    """

    # The alert from the reported failure: 173 chars, under the 200-char cap but
    # well over a single channel frame.
    LONG_ALERT = (
        "The Heat Advisory for the I-35 Corridor and Coastal Plains (Hays, "
        "Bexar, Comal, Guadalupe, Caldwell, Atascosa, Wilson, Karnes, "
        "Gonzales, De Witt) has expired as of 7 PM CDT."
    )

    @pytest.mark.asyncio
    async def test_oversized_channel_message_is_chunked(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "ky-wx", "message": self.LONG_ALERT})

        resp = await svc._handle_webhook(req)

        assert resp.status == 200
        bot.command_manager.send_channel_message.assert_not_awaited()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 130

    @pytest.mark.asyncio
    async def test_chunks_preserve_the_whole_message(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "ky-wx", "message": self.LONG_ALERT})

        await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        rejoined = " ".join(_strip_part_suffix(c) for c in chunks)
        assert rejoined.split() == self.LONG_ALERT.split()

    @pytest.mark.asyncio
    async def test_response_reports_part_count(self, mock_logger):
        import json

        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "ky-wx", "message": self.LONG_ALERT})

        resp = await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert json.loads(resp.text) == {"ok": True, "parts": len(chunks)}

    @pytest.mark.asyncio
    async def test_single_part_message_still_reports_one(self, mock_logger):
        import json

        svc, _ = _make_service(mock_logger)
        req = _make_request(body={"channel": "general", "message": "Hello!"})

        resp = await svc._handle_webhook(req)

        assert json.loads(resp.text) == {"ok": True, "parts": 1}

    @pytest.mark.asyncio
    async def test_short_message_uses_the_unchunked_path(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "general", "message": "Hello!"})

        await svc._handle_webhook(req)

        bot.command_manager.send_channel_message.assert_awaited_once()
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_budget_is_resolved_for_the_target_channel_and_scope(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "ky-wx", "message": "hi", "flood_scope": "west"}
        )

        await svc._handle_webhook(req)

        bot.command_manager.channel_body_budget.assert_called_once_with(
            channel="ky-wx", scope="#west"
        )

    @pytest.mark.asyncio
    async def test_scope_is_passed_to_the_chunked_send(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "ky-wx", "message": self.LONG_ALERT, "flood_scope": "west"}
        )

        await svc._handle_webhook(req)

        _, kwargs = bot.command_manager.send_channel_messages_chunked.call_args
        assert kwargs.get("scope") == "#west"

    @pytest.mark.asyncio
    async def test_chunked_send_failure_returns_500(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=False)
        req = _make_request(body={"channel": "ky-wx", "message": self.LONG_ALERT})

        resp = await svc._handle_webhook(req)

        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_narrow_budget_produces_more_parts(self, mock_logger):
        """A long bot name or a regional scope shrinks the budget; parts follow it."""
        svc, bot = _make_service(mock_logger)
        bot.command_manager.channel_body_budget = Mock(return_value=40)
        req = _make_request(body={"channel": "ky-wx", "message": self.LONG_ALERT})

        await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert len(chunks) >= 5
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 40

    @pytest.mark.asyncio
    async def test_truncation_still_applies_before_chunking(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "ky-wx", "message": "A" * 1000}
        )

        await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert sum(len(_strip_part_suffix(c)) for c in chunks) == 200

    @pytest.mark.asyncio
    async def test_long_dm_reports_parts_but_sends_once(self, mock_logger):
        """send_dm applies its own byte guard, so the whole body goes in one call."""
        import json

        svc, bot = _make_service(mock_logger, {"max_message_length": "400"})
        req = _make_request(body={"dm_to": "Alice", "message": "B" * 400})

        resp = await svc._handle_webhook(req)

        bot.command_manager.send_dm.assert_awaited_once()
        assert bot.command_manager.send_dm.call_args[0][1] == "B" * 400
        assert json.loads(resp.text) == {"ok": True, "parts": 3}


# ---------------------------------------------------------------------------
# TestPartNumbering — a mesh does not guarantee delivery order
# ---------------------------------------------------------------------------


class TestPartNumbering:
    """Multi-part posts carry " (i/n)" so a reader can reassemble them.

    Nothing else distinguishes a continuation from a standalone post, and parts
    can arrive out of order.
    """

    @pytest.mark.asyncio
    async def test_each_part_is_tagged_with_its_position(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "ky-wx", "message": TestChunking.LONG_ALERT}
        )

        await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        total = len(chunks)
        assert total > 1
        for i, chunk in enumerate(chunks, 1):
            assert chunk.endswith(f" ({i}/{total})")

    @pytest.mark.asyncio
    async def test_suffix_does_not_push_a_part_over_budget(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "ky-wx", "message": TestChunking.LONG_ALERT}
        )

        await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 130

    @pytest.mark.asyncio
    async def test_single_part_carries_no_suffix(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        req = _make_request(body={"channel": "general", "message": "Hello!"})

        await svc._handle_webhook(req)

        assert bot.command_manager.send_channel_message.call_args[0][1] == "Hello!"

    @pytest.mark.asyncio
    async def test_reported_parts_match_the_numbering(self, mock_logger):
        import json

        svc, bot = _make_service(mock_logger)
        req = _make_request(
            body={"channel": "ky-wx", "message": TestChunking.LONG_ALERT}
        )

        resp = await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert json.loads(resp.text)["parts"] == len(chunks)
        assert chunks[-1].endswith(f" ({len(chunks)}/{len(chunks)})")


class TestWebhookLinkIntegrity:
    """A relayed link must survive the split intact."""

    LINK = "https://is.gd/a1B2c3"

    @pytest.mark.asyncio
    async def test_link_survives_a_split_relay(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        text = (
            "Flood Warning for Caldwell and Hays counties until 9PM CDT, avoid low "
            f"water crossings and do not drive through standing water {self.LINK}"
        )
        req = _make_request(body={"channel": "ky-wx", "message": text})

        resp = await svc._handle_webhook(req)

        assert resp.status == 200
        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert len(chunks) > 1
        assert any(self.LINK in chunk for chunk in chunks)

    @pytest.mark.asyncio
    async def test_unspaced_link_survives_a_split_relay(self, mock_logger):
        svc, bot = _make_service(mock_logger)
        text = f"FloodWarn-Caldwell-Hays-til-9PM-avoid-low-water-crossings|{self.LINK}"
        bot.command_manager.channel_body_budget = Mock(return_value=40)
        req = _make_request(body={"channel": "ky-wx", "message": text})

        await svc._handle_webhook(req)

        chunks = bot.command_manager.send_channel_messages_chunked.call_args[0][1]
        assert any(self.LINK in chunk for chunk in chunks)

    @pytest.mark.asyncio
    async def test_uncuttable_link_still_relays_and_is_warned_about(self, mock_logger):
        """A link longer than one frame cannot survive; the relay must not fail."""
        svc, bot = _make_service(mock_logger)
        long_link = (
            "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abcdef.001.1"
        )
        bot.command_manager.channel_body_budget = Mock(return_value=40)
        req = _make_request(body={"channel": "ky-wx", "message": f"Alert {long_link}"})

        resp = await svc._handle_webhook(req)

        assert resp.status == 200
        assert any(
            "not be clickable" in str(call)
            for call in mock_logger.warning.call_args_list
        )
