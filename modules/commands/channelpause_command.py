#!/usr/bin/env python3
"""
Channel pause command
DM-only admin: pause or resume bot responses on public channels (in-memory only).
"""

from ..models import MeshMessage
from .base_command import BaseCommand


class ChannelPauseCommand(BaseCommand):
    """Pause or resume channel-triggered bot responses (greeter, keywords, commands)."""

    name = "channelpause"
    keywords = ["channelpause", "channelresume"]
    description = "Pause or resume bot responses on channels (DM only, admin only)"
    requires_dm = True
    cooldown_seconds = 2
    category = "admin"

    def __init__(self, bot):
        super().__init__(bot)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.requires_admin_access():
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def requires_admin_access(self) -> bool:
        return True

    def get_help_text(self) -> str:
        return (
            "Controls whether the bot responds to public channel messages.\n"
            "DMs always work (including this command).\n"
            "Not saved across restarts.\n"
            "Usage: channelpause — stop channel responses\n"
            "       channelresume — resume channel responses"
        )

    def _stripped_content_lower(self, message: MeshMessage) -> str:
        content = message.content.strip()
        if self._command_prefix:
            if not content.startswith(self._command_prefix):
                return ""
            content = content[len(self._command_prefix) :].strip()
        elif content.startswith("!"):
            content = content[1:].strip()
        content = self._strip_mentions(content)
        return content.lower()

    async def execute(self, message: MeshMessage) -> bool:
        text = self._stripped_content_lower(message)
        resume_kw = self.keywords[1].lower()
        pause_kw = self.keywords[0].lower()

        if text == resume_kw or text.startswith(resume_kw + " "):
            self.bot.channel_responses_enabled = True
            reply = "Channel responses: ON. The bot will respond on public channels again."
        elif text == pause_kw or text.startswith(pause_kw + " "):
            self.bot.channel_responses_enabled = False
            reply = (
                "Channel responses: OFF. No greeter, keywords, or commands on channels; "
                "DMs still work. Not persisted after restart."
            )
        else:
            reply = "Use channelpause or channelresume."

        await self.send_response(message, reply)
        return True
