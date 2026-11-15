"""Tests for MatrixBridgeService (one-way MeshCore → Matrix bridge)."""

import asyncio
import configparser
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.service_plugins.matrix_bridge_service import (
    MATRIX_MAX_MESSAGE_LENGTH,
    MatrixBridgeService,
    QueuedMessage,
)

TOKEN = "syt_SuperSecretToken_1234567890"


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status = status
        self._data = data or {}
        self.headers = headers or {}

    async def json(self):
        return self._data

    async def text(self):
        return str(self._data)


class FakeRequest:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        # Yield like a real request so concurrent callers can interleave.
        await asyncio.sleep(0)
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Minimal aiohttp.ClientSession stand-in for GET/PUT calls."""

    def __init__(self, get_responder=None, put_responder=None):
        self.get_responder = get_responder or (
            lambda url: FakeResponse(200, {"user_id": "@bot:matrix.example"})
        )
        self.put_responder = put_responder or (
            lambda url, payload: FakeResponse(200, {"event_id": "$event"})
        )
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, None))
        return FakeRequest(self.get_responder(url))

    def put(self, url, **kwargs):
        self.requests.append(("PUT", url, kwargs.get("json")))
        return FakeRequest(self.put_responder(url, kwargs.get("json")))

    async def close(self):
        self.closed = True


@pytest.fixture
def matrix_bot(mock_logger):
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = configparser.ConfigParser()
    bot.config.add_section("MatrixBridge")
    bot.config.set("MatrixBridge", "enabled", "true")
    bot.config.set("MatrixBridge", "homeserver", "https://matrix.example")
    bot.config.set("MatrixBridge", "access_token", TOKEN)
    bot.channel_manager = MagicMock()
    bot.channel_manager.get_channel_name.return_value = "Public"
    bot.channel_sent_listeners = []
    bot.meshcore = MagicMock()
    return bot


def _service(bot):
    return MatrixBridgeService(bot)


def _raise(exc):
    raise exc


def _channel_event(text, channel_idx=0):
    event = MagicMock()
    event.payload = {"channel_idx": channel_idx, "text": text}
    return event


# ---------------------------------------------------------------------------
# Mapping parsing and channel matching
# ---------------------------------------------------------------------------

def test_mapping_parses_room_id_alias_multiple_and_normalizes_names(matrix_bot):
    matrix_bot.config.set(
        "MatrixBridge", "bridge.Public", "!room1:matrix.example, #alias:matrix.example"
    )
    matrix_bot.config.set(
        "MatrixBridge", "bridge.emergency", "!room2:matrix.example, !room3:matrix.example"
    )

    service = _service(matrix_bot)

    # ConfigParser lowercases keys; plain names are title-cased like Discord.
    assert service.channel_rooms == {
        "Public": ["!room1:matrix.example", "#alias:matrix.example"],
        "Emergency": ["!room2:matrix.example", "!room3:matrix.example"],
    }
    # Channel matching ignores case and a leading '#'.
    assert service._targets_for("#public") == service.channel_rooms["Public"]
    assert service._targets_for("PUBLIC") == service.channel_rooms["Public"]
    assert service._targets_for("#emergency") == service.channel_rooms["Emergency"]
    assert service._targets_for("#unmapped") is None


def test_mapping_ignores_values_that_are_neither_room_id_nor_alias(matrix_bot):
    """A token typed into a bridge.* row is ignored, never used as a target."""
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "syt_not_a_room")

    service = _service(matrix_bot)

    assert service.channel_rooms == {}
    assert any(
        "Ignoring invalid Matrix target" in call.args[0]
        for call in matrix_bot.logger.warning.call_args_list
    )


def test_homeserver_validation():
    assert MatrixBridgeService._normalize_homeserver("https://matrix.org/") == "https://matrix.org"
    assert MatrixBridgeService._normalize_homeserver("http://localhost:8008") == "http://localhost:8008"
    assert MatrixBridgeService._normalize_homeserver("http://127.0.0.1:8008") == "http://127.0.0.1:8008"
    assert MatrixBridgeService._normalize_homeserver("http://matrix.org") is None
    assert MatrixBridgeService._normalize_homeserver("matrix.org") is None
    assert MatrixBridgeService._normalize_homeserver("") is None


def test_missing_token_disables_service(matrix_bot, monkeypatch):
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    matrix_bot.config.remove_option("MatrixBridge", "access_token")

    service = _service(matrix_bot)

    assert service.enabled is False
    assert service.is_running() is False
    matrix_bot.logger.error.assert_called_once()


def test_access_token_falls_back_to_env(matrix_bot, monkeypatch):
    matrix_bot.config.remove_option("MatrixBridge", "access_token")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", TOKEN)

    service = _service(matrix_bot)

    assert service.access_token == TOKEN


# ---------------------------------------------------------------------------
# DM exclusion and fan-out
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dms_are_never_bridged(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    matrix_bot.channel_manager.get_channel_name.return_value = "DM"
    service = _service(matrix_bot)

    with patch.object(service, "_deliver", new_callable=AsyncMock) as deliver:
        await service._on_mesh_channel_message(_channel_event("Alice: secret"))

    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_fan_out_to_two_rooms(matrix_bot):
    matrix_bot.config.set(
        "MatrixBridge", "bridge.Public", "!room1:matrix.example, !room2:matrix.example"
    )
    service = _service(matrix_bot)

    await service._on_mesh_channel_message(_channel_event("Alice: hello"))

    assert len(service.message_queues["!room1:matrix.example"]) == 1
    assert len(service.message_queues["!room2:matrix.example"]) == 1
    first = service.message_queues["!room1:matrix.example"][0]
    second = service.message_queues["!room2:matrix.example"][0]
    assert first.payload == second.payload
    assert first.txn_id != second.txn_id


# ---------------------------------------------------------------------------
# Event payload and escaping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_payload_escapes_sender_and_text(matrix_bot):
    service = _service(matrix_bot)

    await service._deliver(
        ["!room:matrix.example"],
        'Evil <&">',
        'hi <b>&" there',
        "Public",
    )

    queued = service.message_queues["!room:matrix.example"][0]
    assert queued.payload == {
        "msgtype": "m.text",
        "body": 'Evil <&">: hi <b>&" there',
        "format": "org.matrix.custom.html",
        "formatted_body": (
            "<b>Evil &lt;&amp;&quot;&gt;</b>: "
            "hi &lt;b&gt;&amp;&quot; there"
        ),
    }


@pytest.mark.asyncio
async def test_mentions_render_as_plain_text(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    service = _service(matrix_bot)

    await service._on_mesh_channel_message(_channel_event("Alice: hi @[Bob] and @[Carol]"))

    payload = service.message_queues["!room:matrix.example"][0].payload
    assert payload["body"] == "Alice: hi @Bob and @Carol"
    assert payload["formatted_body"] == "<b>Alice</b>: hi @Bob and @Carol"
    assert "matrix.to" not in str(payload)


def test_long_messages_are_truncated(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "max_message_length", "20")
    service = _service(matrix_bot)

    payload = service._build_event("Alice", "x" * 100)

    assert len(payload["body"]) <= 20
    assert payload["body"].endswith("…")
    assert len(payload["formatted_body"]) < MATRIX_MAX_MESSAGE_LENGTH


def test_max_message_length_is_capped(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "max_message_length", "32000")

    service = _service(matrix_bot)

    assert MATRIX_MAX_MESSAGE_LENGTH == 4000
    assert service.max_message_length == MATRIX_MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Transaction ids
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_txn_id_is_stable_across_retries_and_distinct_per_message_and_room(matrix_bot):
    session = FakeSession(put_responder=lambda url, payload: FakeResponse(500, {"errcode": "M_UNKNOWN"}))
    service = _service(matrix_bot)
    service.http_session = session

    await service._deliver(["!room1:matrix.example"], "Alice", "hello", "Public")
    await service._deliver(["!room1:matrix.example"], "Alice", "hello again", "Public")
    await service._deliver(["!room2:matrix.example"], "Alice", "hello", "Public")

    first = service.message_queues["!room1:matrix.example"][0]
    second = service.message_queues["!room1:matrix.example"][1]
    other_room = service.message_queues["!room2:matrix.example"][0]
    assert len({first.txn_id, second.txn_id, other_room.txn_id}) == 3

    original_txn = first.txn_id
    assert await service._send_queued(first) is False
    assert await service._send_queued(first) is False
    assert first.txn_id == original_txn

    sent_urls = [url for method, url, _payload in session.requests if method == "PUT"]
    assert len(sent_urls) == 2
    assert all(original_txn in url for url in sent_urls)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_sets_next_retry_at_from_retry_after_ms(matrix_bot):
    session = FakeSession(
        put_responder=lambda url, payload: FakeResponse(
            429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 2000}
        )
    )
    service = _service(matrix_bot)
    service.http_session = session
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )
    before = time.time()

    assert await service._send_queued(queued) is False

    assert queued.next_retry_at >= before + 1.9
    assert queued.retry_count == 0


@pytest.mark.asyncio
async def test_429_falls_back_to_retry_after_header(matrix_bot):
    session = FakeSession(
        put_responder=lambda url, payload: FakeResponse(
            429, {"errcode": "M_LIMIT_EXCEEDED"}, headers={"Retry-After": "3"}
        )
    )
    service = _service(matrix_bot)
    service.http_session = session
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )
    before = time.time()

    assert await service._send_queued(queued) is False
    assert queued.next_retry_at >= before + 2.9


@pytest.mark.asyncio
async def test_rate_limit_wait_survives_the_base_queue_backoff(matrix_bot):
    session = FakeSession(
        put_responder=lambda url, payload: FakeResponse(
            429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 2000}
        )
    )
    service = _service(matrix_bot)
    service.http_session = session
    service._running = True
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )
    service.message_queues["!room:matrix.example"] = [queued]
    before = time.time()

    task = asyncio.create_task(service._process_message_queues())
    await asyncio.sleep(0.15)
    service._running = False
    task.cancel()
    await task

    assert queued.next_retry_at >= before + 1.9
    assert len(service.message_queues["!room:matrix.example"]) == 1


@pytest.mark.asyncio
async def test_429_pauses_every_room(matrix_bot):
    session = FakeSession(
        put_responder=lambda url, payload: FakeResponse(
            429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 5000}
        )
    )
    service = _service(matrix_bot)
    service.http_session = session
    queued = QueuedMessage(
        room_id="!room1:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )

    assert await service._send_queued(queued) is False

    now = time.time()
    assert service._throttled("!room2:matrix.example", [], now) is True
    assert service._throttled("!room2:matrix.example", [], now + 6) is False


@pytest.mark.asyncio
async def test_forbidden_is_logged_once_and_dropped_without_retry(matrix_bot):
    session = FakeSession(
        put_responder=lambda url, payload: FakeResponse(403, {"errcode": "M_FORBIDDEN"})
    )
    service = _service(matrix_bot)
    service.http_session = session
    service._running = True
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )
    service.message_queues["!room:matrix.example"] = [queued]

    task = asyncio.create_task(service._process_message_queues())
    await asyncio.sleep(0.15)
    service._running = False
    task.cancel()
    await task

    assert service.message_queues["!room:matrix.example"] == []
    assert len([r for r in session.requests if r[0] == "PUT"]) == 1
    warnings = [c.args[0] for c in matrix_bot.logger.warning.call_args_list]
    assert sum("dropping without retry" in w for w in warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,errcode",
    [(404, "M_NOT_FOUND")],
)
async def test_permanent_errors_are_dropped(matrix_bot, status, errcode):
    session = FakeSession(put_responder=lambda url, payload: FakeResponse(status, {"errcode": errcode}))
    service = _service(matrix_bot)
    service.http_session = session
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )

    assert await service._send_queued(queued) is True


# ---------------------------------------------------------------------------
# Startup: whoami and alias resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whoami_success_starts_and_logs_user_id(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    session = FakeSession()
    service = _service(matrix_bot)
    service.http_session = session

    await service.start()
    await service._startup_task

    assert service.is_running() is True
    assert service.matrix_user_id == "@bot:matrix.example"
    matrix_bot.meshcore.subscribe.assert_called_once()
    logged = " ".join(str(c) for c in matrix_bot.logger.info.call_args_list)
    assert "@bot:matrix.example" in logged

    await service.stop()
    assert session.closed is True


@pytest.mark.asyncio
async def test_whoami_failure_prevents_delivery(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    session = FakeSession(
        get_responder=lambda url: FakeResponse(401, {"errcode": "M_UNKNOWN_TOKEN"})
    )
    service = _service(matrix_bot)
    service.http_session = session

    await service.start()
    await service._startup_task

    assert service.is_running() is False
    assert service.enabled is False
    assert session.closed is True
    errors = " ".join(str(c) for c in matrix_bot.logger.error.call_args_list)
    assert "access token rejected" in errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responder",
    [
        lambda url: _raise(asyncio.TimeoutError()),
        lambda url: _raise(OSError("Name or service not known")),
        lambda url: FakeResponse(502, {}),
    ],
    ids=["timeout", "network-error", "bad-gateway"],
)
async def test_unreachable_homeserver_at_startup_still_starts(matrix_bot, responder):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    session = FakeSession(get_responder=responder)
    service = _service(matrix_bot)
    service.http_session = session

    await service.start()
    await service._startup_task

    assert service.is_running() is True
    matrix_bot.meshcore.subscribe.assert_called_once()
    warnings = " ".join(str(c) for c in matrix_bot.logger.warning.call_args_list)
    assert "starting anyway" in warnings
    matrix_bot.logger.error.assert_not_called()

    await service.stop()


@pytest.mark.asyncio
async def test_failed_alias_is_retried_after_the_interval(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "#late:matrix.example")
    service = _service(matrix_bot)
    answers = [FakeResponse(503, {}), FakeResponse(200, {"room_id": "!late:matrix.example"})]
    session = FakeSession(get_responder=lambda url: answers.pop(0))
    service.http_session = session

    await service._resolve_configured_aliases()
    assert service._resolved_target("#late:matrix.example") is None

    service._alias_retry_at["#late:matrix.example"] = time.time() - 1
    await service._deliver(["#late:matrix.example"], "Alice", "hi", "Public")

    assert service._resolved_target("#late:matrix.example") == "!late:matrix.example"
    assert len(service.message_queues["!late:matrix.example"]) == 1


@pytest.mark.asyncio
async def test_start_does_not_wait_for_the_homeserver(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    release = asyncio.Event()

    class SlowSession(FakeSession):
        def get(self, url, **kwargs):
            outer = self

            class Slow:
                async def __aenter__(self):
                    await release.wait()
                    return FakeResponse(200, {"user_id": "@bot:matrix.example"})

                async def __aexit__(self, *exc_info):
                    return False

            outer.requests.append(("GET", url, None))
            return Slow()

    service = _service(matrix_bot)
    service.http_session = SlowSession()

    await asyncio.wait_for(service.start(), timeout=1)

    assert service.is_running() is True
    matrix_bot.meshcore.subscribe.assert_called_once()
    release.set()
    await service._startup_task
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,errcode", [(401, "M_UNKNOWN_TOKEN"), (401, None)])
async def test_token_rejected_while_sending_stops_the_bridge(matrix_bot, status, errcode):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")
    body = {"errcode": errcode} if errcode else {}
    session = FakeSession(put_responder=lambda url, payload: FakeResponse(status, body))
    service = _service(matrix_bot)
    service.http_session = session
    await service.start()
    await service._startup_task
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )

    assert await service._send_queued(queued) is True
    await service._shutdown_task

    assert service.is_running() is False
    assert service.is_healthy() is False
    assert service.enabled is False
    errors = " ".join(str(c) for c in matrix_bot.logger.error.call_args_list)
    assert "rejected while sending" in errors


@pytest.mark.asyncio
async def test_concurrent_deliveries_resolve_an_alias_once(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "#slow:matrix.example")
    service = _service(matrix_bot)
    service.http_session = FakeSession(get_responder=lambda url: FakeResponse(503, {}))

    await asyncio.gather(
        service._deliver(["#slow:matrix.example"], "Alice", "one", "Public"),
        service._deliver(["#slow:matrix.example"], "Bob", "two", "Public"),
    )

    lookups = [r for r in service.http_session.requests if "directory/room" in r[1]]
    assert len(lookups) == 1


@pytest.mark.asyncio
async def test_alias_resolution_success_and_failure(matrix_bot):
    matrix_bot.config.set(
        "MatrixBridge", "bridge.Public", "#ok:matrix.example, #bad:matrix.example"
    )
    service = _service(matrix_bot)

    def responder(url):
        if "ok%3Amatrix.example" in url:
            return FakeResponse(200, {"room_id": "!resolved:matrix.example"})
        return FakeResponse(404, {"errcode": "M_NOT_FOUND"})

    service.http_session = FakeSession(get_responder=responder)

    await service._resolve_configured_aliases()

    assert service.resolved_room_ids == {"#ok:matrix.example": "!resolved:matrix.example"}
    assert service._resolved_target("#ok:matrix.example") == "!resolved:matrix.example"
    assert service._resolved_target("#bad:matrix.example") is None
    assert any(
        "Could not resolve Matrix room alias" in call.args[0]
        for call in matrix_bot.logger.warning.call_args_list
    )

    # Within the retry interval, delivery skips the alias without asking again.
    lookups = len(service.http_session.requests)
    await service._deliver(["#bad:matrix.example"], "Alice", "hi", "Public")
    assert service.message_queues == {}
    assert len(service.http_session.requests) == lookups


@pytest.mark.asyncio
async def test_alias_is_resolved_only_once(matrix_bot):
    matrix_bot.config.set(
        "MatrixBridge", "bridge.Public", "#room:matrix.example"
    )
    matrix_bot.config.set(
        "MatrixBridge", "bridge.emergency", "#room:matrix.example"
    )
    service = _service(matrix_bot)
    session = FakeSession(
        get_responder=lambda url: FakeResponse(200, {"room_id": "!resolved:matrix.example"})
    )
    service.http_session = session

    await service._resolve_configured_aliases()

    directory_calls = [r for r in session.requests if "directory/room" in r[1]]
    assert len(directory_calls) == 1


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_access_token_never_appears_in_log_output(matrix_bot):
    matrix_bot.config.set("MatrixBridge", "bridge.Public", "#room:matrix.example")
    session = FakeSession(put_responder=lambda url, payload: FakeResponse(403, {"errcode": "M_FORBIDDEN"}))
    service = _service(matrix_bot)
    service.http_session = session

    await service.start()
    await service._startup_task
    queued = QueuedMessage(
        room_id="!room:matrix.example", payload={"body": "x"}, channel_name="Public", txn_id="t1"
    )
    await service._send_queued(queued)
    await service.stop()

    assert TOKEN not in str(matrix_bot.logger.mock_calls)


def test_config_snapshot_redacts_matrix_token_and_mappings():
    from modules.config_snapshot import config_to_redacted_sections

    config = configparser.ConfigParser()
    config.add_section("MatrixBridge")
    config.set("MatrixBridge", "access_token", TOKEN)
    config.set("MatrixBridge", "homeserver", "https://matrix.example")
    config.set("MatrixBridge", "bridge.Public", "!room:matrix.example")

    redacted = config_to_redacted_sections(config)["MatrixBridge"]

    assert redacted["access_token"] == "●●●●●●"
    assert redacted["bridge.public"] == "●●●●●●"
    assert redacted["homeserver"] == "https://matrix.example"
