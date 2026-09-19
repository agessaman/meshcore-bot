#!/usr/bin/env python3
"""
Contact command for the MeshCore Bot
Adds the bot contact info to the current channel
"""

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


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

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        """Get help text for the contact command.

        Returns:
            str: Help text string.
        """
        return self.translate('commands.contact.help')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Override to handle contact-specific matching.

        Args:
            message: The received message.

        Returns:
            bool: True if message is a dice command, False otherwise.
        """
        content_lower = self.cleanup_message_for_matching(message)

        # Check for exact "contact" match
        if content_lower == "contact":
            return True

        return False


    async def execute(self, message: MeshMessage) -> bool:
        """Execute the contact command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()

        # Handle command-style messages
        if content.startswith('!'):
            content = content[1:].strip()

        # Default to d6 if no specification
        if content.lower() == "contact":
            my_public_key = self.bot.meshcore.self_info.get("public_key")
            my_name = self.bot.meshcore.self_info.get("name")
            response = f"<{my_public_key}:1:{my_name}>"
            return await self.send_response(message, response)

        return False
