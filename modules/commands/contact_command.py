#!/usr/bin/env python3
"""
Contact command for the MeshCore Bot
Adds the bot contact info to the current channel
"""

import re
from typing import Any, Optional

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

PUBLIC_KEY_RE = re.compile(r'^[0-9a-fA-F]{64}$')


class ContactCommand(BaseCommand):
    """Handles contact command"""

    # Plugin metadata
    name = "contact"
    keywords = ['contact']
    description = "Display the bot's contact information"
    category = "basic"

    # Documentation
    short_description = "Display the bot's contact information"
    usage = "contact"
    examples = [
        "contact"
    ]

    def __init__(self, bot):
        """Initialize the contact command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.enabled = self.get_config_value('Contact_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.
            skip_channel_check: If True, skip the channel check.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self) -> str:
        """Get help text for the contact command.

        Returns:
            str: Help text string.
        """
        return self.translate('commands.contact.help')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Match ``contact`` (or a configured alias) on its own, with no arguments.

        Args:
            message: The received message.

        Returns:
            bool: True if the message is a contact command, False otherwise.
        """
        def _matches(content_lower: str) -> bool:
            return any(content_lower == keyword.lower() for keyword in self.keywords)

        return self._cleaned_content_matches(message, _matches)

    def _self_info_value(self, key: str) -> Optional[str]:
        """Read a field from the radio's self_info, which may be a dict or an object.

        Args:
            key: The self_info field name.

        Returns:
            str: The field value, or None if unavailable.
        """
        meshcore: Any = getattr(self.bot, 'meshcore', None)
        self_info = getattr(meshcore, 'self_info', None) if meshcore else None
        if not self_info:
            return None
        if isinstance(self_info, dict):
            value = self_info.get(key)
        else:
            value = getattr(self_info, key, None)
        return str(value).strip() if value else None

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the contact command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        public_key = self._self_info_value('public_key')
        name = self._self_info_value('name') or self._self_info_value('adv_name')

        if not public_key or not PUBLIC_KEY_RE.match(public_key):
            self.logger.warning("Contact command: no usable public key in self_info")
            return await self.send_response(message, self.translate('commands.contact.unavailable'))

        if not name:
            self.logger.warning("Contact command: no device name in self_info")
            return await self.send_response(message, self.translate('commands.contact.unavailable'))

        return await self.send_response(message, f"<{public_key.lower()}:1:{name}>")
