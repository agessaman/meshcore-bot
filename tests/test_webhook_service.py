"""Tests for WebhookService."""

from configparser import ConfigParser
from unittest.mock import AsyncMock, Mock

import pytest

from modules.service_plugins.webhook_service import WebhookService

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
    bot.command_manager.send_dm = AsyncMock(return_value=True)
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
