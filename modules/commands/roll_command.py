#!/usr/bin/env python3
"""
Roll command for the MeshCore Bot
Handles random number generation between 1 and X (default 100)
"""

import random
from typing import Optional

from ..models import MeshMessage
from .base_command import BaseCommand


class RollCommand(BaseCommand):
    """Handles random number rolling commands.

    This command generates a random number between 1 and a specified maximum (default 100).
    It supports syntax like 'roll' or 'roll 50'.
    """

    # Plugin metadata
    # Read-only informational output; safe for scheduled {cmd:...} rendering.
    render_safe = True
    name = "roll"
    keywords = ['roll']
    description = "Roll a random number between 1 and X (default 100). Use 'roll' for 1-100, 'roll 50' for 1-50, etc."
    category = "games"

    # Documentation
    short_description = "Roll a random number between 1 and X"
    usage = "roll [max]"
    examples = ["roll", "roll 50"]
    parameters = [
        {"name": "max", "description": "Maximum value (default: 100, max: 10000)"}
    ]

    def __init__(self, bot):
        """Initialize the roll command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.roll_enabled = self.get_config_value('Roll_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.roll_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        """Get help text for the roll command.

        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.roll.help')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Match ``roll`` / aliases; with args, only when the arg is a valid max."""
        def _matches(content_lower: str) -> bool:
            for keyword in self.keywords:
                kw = keyword.lower()
                if content_lower == kw:
                    return True
                if content_lower.startswith(kw + " "):
                    roll_part = content_lower[len(kw):].strip()
                    return self.parse_roll_notation(roll_part) is not None
            return False

        return self._cleaned_content_matches(message, _matches)

    def parse_roll_notation(self, roll_input: str) -> Optional[int]:
        """Parse roll notation and return the maximum number.

        Supports inputs like: 50, 100, 1000.

        Args:
            roll_input: The string part containing the number.

        Returns:
            Optional[int]: The maximum number if valid, None otherwise.
        """
        roll_input = roll_input.strip()

        # Handle direct number (e.g., "50", "100", "1000")
        if roll_input.isdigit():
            max_num = int(roll_input)
            if 1 <= max_num <= 10000:  # Reasonable limit
                return max_num
            else:
                return None

        return None

    def roll_number(self, max_num: int) -> int:
        """Roll a random number between 1 and max_num (inclusive).

        Args:
            max_num: The maximum possible value.

        Returns:
            int: The generated random number.
        """
        return random.randint(1, max_num)

    def format_roll_result(self, max_num: int, result: int) -> str:
        """Format roll result into a readable string.

        Args:
            max_num: The maximum number for the roll.
            result: The actual rolled number.

        Returns:
            str: The formatted result string.
        """
        return self.translate('commands.roll.result', max=max_num, result=result)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the roll command.

        Parses the maximum number (if provided), generates a random number,
        and sends the result to the user.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        _trigger, roll_part = self.split_trigger_and_args(message.content)

        # Default to 1-100 if no specification
        if not roll_part:
            max_num: Optional[int] = 100
        else:
            max_num = self.parse_roll_notation(roll_part)

            if max_num is None:
                # Invalid roll specification
                response = self.translate('commands.roll.invalid_number')
                return await self.send_response(message, response)

        # Roll the number
        result = self.roll_number(max_num)

        # Format and send response
        response = self.format_roll_result(max_num, result)
        return await self.send_response(message, response)
