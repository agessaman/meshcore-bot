"""wx and gwx keep logging companion-location lookup failures at WARNING."""

from unittest.mock import MagicMock

import pytest

from modules.models import MeshMessage


@pytest.mark.parametrize("which", ["wx", "gwx"])
def test_companion_lookup_error_is_a_warning(which):
    if which == "wx":
        from modules.commands.wx_command import WxCommand as cls
    else:
        from modules.commands.alternatives.wx_international import GlobalWxCommand as cls
    cmd = object.__new__(cls)
    cmd.bot = MagicMock()
    cmd.bot.db_manager.execute_query.side_effect = RuntimeError("database is locked")
    cmd.logger = MagicMock()
    msg = MeshMessage(content="wx", sender_id="Ann", sender_pubkey="ab" * 32)
    assert cmd._get_companion_location(msg) is None
    cmd.logger.warning.assert_called_once()
    assert "database is locked" in cmd.logger.warning.call_args[0][0]
