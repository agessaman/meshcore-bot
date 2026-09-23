"""The radio's own default flood scope: reading it, writing it, clearing it.

The firmware keeps a default region in NodePrefs (``default_scope_name`` plus
``default_scope_key``) and uses it for any send the app has not scoped itself.
These cover the scheduler operations behind the Radio page's Default Region
Scope field.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from meshcore.events import EventType
from meshcore.packets import CommandType

from modules import flood_scope
from modules.scheduler import MessageScheduler


def _scheduler(meshcore):
    bot = MagicMock()
    bot.meshcore = meshcore
    scheduler = object.__new__(MessageScheduler)
    scheduler.bot = bot
    scheduler.logger = Mock()
    return scheduler


def _meshcore(**command_attrs):
    meshcore = MagicMock()
    meshcore.is_connected = True
    meshcore.commands = MagicMock()
    for name, value in command_attrs.items():
        setattr(meshcore.commands, name, value)
    return meshcore


def _event(event_type, payload=None):
    event = MagicMock()
    event.type = event_type
    event.payload = payload
    return event


class TestReadDefaultScope:
    @pytest.mark.asyncio
    async def test_a_configured_scope_is_reported_with_its_key(self):
        key = flood_scope.scope_key_hex("#west")
        meshcore = _meshcore(get_default_flood_scope=AsyncMock(
            return_value=_event(
                EventType.DEFAULT_FLOOD_SCOPE,
                {"scope_name": "#west", "scope_key": key},
            )))
        result = await _scheduler(meshcore)._read_default_flood_scope(meshcore)
        assert result == {
            "default_scope_name": "#west",
            "default_scope_key": key,
            "default_scope_supported": True,
            "default_scope_key_matches": True,
        }

    @pytest.mark.asyncio
    async def test_a_build_flag_default_stored_without_its_hash_still_matches(self):
        """The firmware's DEFAULT_FLOOD_SCOPE_NAME build flag stores the bare
        name but hashes the '#' form, so comparing raw would report a mismatch
        on a radio that is perfectly consistent."""
        meshcore = _meshcore(get_default_flood_scope=AsyncMock(
            return_value=_event(
                EventType.DEFAULT_FLOOD_SCOPE,
                {"scope_name": "west", "scope_key": flood_scope.scope_key_hex("#west")},
            )))
        result = await _scheduler(meshcore)._read_default_flood_scope(meshcore)
        assert result["default_scope_key_matches"] is True

    @pytest.mark.asyncio
    async def test_a_key_that_is_not_the_name_s_hash_is_flagged(self):
        meshcore = _meshcore(get_default_flood_scope=AsyncMock(
            return_value=_event(
                EventType.DEFAULT_FLOOD_SCOPE,
                {"scope_name": "#west", "scope_key": "00" * 16},
            )))
        result = await _scheduler(meshcore)._read_default_flood_scope(meshcore)
        assert result["default_scope_key_matches"] is False

    @pytest.mark.asyncio
    async def test_the_one_byte_sentinel_reads_as_cleared_not_unsupported(self):
        """A radio with no default scope answers with the response code alone,
        which the library turns into an empty payload."""
        meshcore = _meshcore(get_default_flood_scope=AsyncMock(
            return_value=_event(EventType.DEFAULT_FLOOD_SCOPE, {})))
        result = await _scheduler(meshcore)._read_default_flood_scope(meshcore)
        assert result["default_scope_supported"] is True
        assert result["default_scope_name"] == ""
        assert "default_scope_key_matches" not in result

    @pytest.mark.asyncio
    async def test_an_error_reply_reports_nothing_rather_than_cleared(self):
        """Firmware without the command must not be shown as 'no scope set'."""
        meshcore = _meshcore(get_default_flood_scope=AsyncMock(
            return_value=_event(EventType.ERROR)))
        assert await _scheduler(meshcore)._read_default_flood_scope(meshcore) == {}

    @pytest.mark.asyncio
    async def test_a_raised_error_never_costs_the_caller_the_path_hash_read(self):
        meshcore = _meshcore(get_default_flood_scope=AsyncMock(
            side_effect=TimeoutError("no answer")))
        assert await _scheduler(meshcore)._read_default_flood_scope(meshcore) == {}

    @pytest.mark.asyncio
    async def test_a_library_without_the_command_reports_nothing(self):
        meshcore = _meshcore()
        del meshcore.commands.get_default_flood_scope
        assert await _scheduler(meshcore)._read_default_flood_scope(meshcore) == {}

    @pytest.mark.asyncio
    async def test_the_firmware_read_op_carries_the_scope_alongside_the_mode(self):
        meshcore = _meshcore(
            get_path_hash_mode=AsyncMock(return_value=1),
            get_default_flood_scope=AsyncMock(return_value=_event(
                EventType.DEFAULT_FLOOD_SCOPE,
                {"scope_name": "#west", "scope_key": flood_scope.scope_key_hex("#west")},
            )),
        )
        ok, data = await _scheduler(meshcore)._firmware_read_op()
        assert ok is True
        assert data["path_hash_mode"] == 1
        assert data["default_scope_name"] == "#west"


class TestWriteDefaultScope:
    @pytest.mark.asyncio
    async def test_setting_a_scope_goes_through_the_library(self):
        setter = AsyncMock(return_value=_event(EventType.OK))
        meshcore = _meshcore(set_default_flood_scope=setter)
        ok, error = await _scheduler(meshcore)._write_default_flood_scope(meshcore, "#west")
        assert (ok, error) == (True, "")
        setter.assert_awaited_once_with("#west")

    @pytest.mark.asyncio
    async def test_clearing_sends_the_bare_frame_the_firmware_documents(self):
        """set_default_flood_scope cannot clear: None raises on len(None), ''
        earns ILLEGAL_ARG, and '*' only works via a padding off-by-one."""
        setter = AsyncMock(return_value=_event(EventType.OK))
        send = AsyncMock(return_value=_event(EventType.OK))
        meshcore = _meshcore(set_default_flood_scope=setter, send=send)
        ok, error = await _scheduler(meshcore)._write_default_flood_scope(meshcore, "")
        assert (ok, error) == (True, "")
        setter.assert_not_awaited()
        frame = send.await_args.args[0]
        assert bytes(frame) == bytes([CommandType.SET_DEFAULT_FLOOD_SCOPE.value])

    @pytest.mark.asyncio
    async def test_a_rejected_write_is_reported_with_the_radio_s_answer(self):
        meshcore = _meshcore(set_default_flood_scope=AsyncMock(
            return_value=_event(EventType.ERROR)))
        ok, error = await _scheduler(meshcore)._write_default_flood_scope(meshcore, "#west")
        assert ok is False
        assert "set_default_flood_scope(#west) failed" in error

    @pytest.mark.asyncio
    async def test_a_library_without_the_command_fails_loudly(self):
        meshcore = _meshcore()
        del meshcore.commands.set_default_flood_scope
        ok, error = await _scheduler(meshcore)._write_default_flood_scope(meshcore, "#west")
        assert ok is False
        assert "meshcore library" in error

    @pytest.mark.asyncio
    async def test_the_firmware_write_op_carries_both_settings(self):
        setter = AsyncMock(return_value=_event(EventType.OK))
        meshcore = _meshcore(
            set_path_hash_mode=AsyncMock(return_value=_event(EventType.OK)),
            set_default_flood_scope=setter,
        )
        ok, data = await _scheduler(meshcore)._firmware_write_op(
            {"path_hash_mode": 1, "default_flood_scope": "#west"})
        assert ok is True
        assert data["results"] == {"path_hash_mode": True, "default_flood_scope": True}


class TestDeviceScopeNameRules:
    def test_a_name_is_normalized_like_every_other_scope(self):
        assert flood_scope.validate_device_scope_name("west") == "#west"

    def test_a_name_too_long_for_the_radio_s_field_is_refused(self):
        """NodePrefs.default_scope_name is 31 bytes and the firmware demands
        room for the terminator, so 30 characters is the ceiling."""
        assert flood_scope.validate_device_scope_name("#" + "w" * 29) == "#" + "w" * 29
        with pytest.raises(ValueError, match="30"):
            flood_scope.validate_device_scope_name("#" + "w" * 30)

    def test_a_non_ascii_name_is_refused(self):
        """The library pads the name frame by character count while encoding
        UTF-8, so a multi-byte character displaces the transport key."""
        with pytest.raises(ValueError, match="ASCII"):
            flood_scope.validate_device_scope_name("süd")

    def test_global_markers_pass_through_as_the_cleared_state(self):
        assert flood_scope.validate_device_scope_name("*") == "*"
