"""BaseCommand.enabled_attr: the per-command enable switch checked by can_execute."""

from unittest.mock import MagicMock

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


class _Switchable(BaseCommand):
    name = "switchable"
    enabled_attr = "switchable_enabled"
    keywords = ["switchable"]

    def __init__(self, bot, enabled):
        super().__init__(bot)
        self.switchable_enabled = enabled

    async def execute(self, message):  # pragma: no cover - not exercised
        return True


def _bot():
    bot = MagicMock()
    bot.config.has_section.return_value = False
    bot.config.has_option.return_value = False
    return bot


def _dm():
    return MeshMessage(content="switchable", sender_id="Ann", is_dm=True)


def test_disabled_switch_refuses_before_other_checks():
    cmd = _Switchable(_bot(), enabled=False)
    assert cmd.can_execute(_dm()) is False
    assert cmd.can_execute(_dm(), skip_channel_check=True) is False


def test_enabled_switch_falls_through_to_base_checks():
    cmd = _Switchable(_bot(), enabled=True)
    assert cmd.can_execute(_dm()) is True
    cmd.requires_dm = True
    assert cmd.can_execute(MeshMessage(content="switchable", sender_id="Ann", channel="x", is_dm=False)) is False


def test_switch_is_read_at_call_time():
    cmd = _Switchable(_bot(), enabled=True)
    cmd.switchable_enabled = False
    assert cmd.can_execute(_dm()) is False


def test_admin_only_requires_admin_without_acl_config():
    class _Admin(BaseCommand):
        name = "adminonly"
        admin_only = True

        async def execute(self, message):  # pragma: no cover - not exercised
            return True

    cmd = _Admin(_bot())
    assert cmd.requires_admin_access() is True
    assert _Switchable(_bot(), enabled=True).requires_admin_access() is False
