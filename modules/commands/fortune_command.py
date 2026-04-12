#!/usr/bin/env python3
"""
Fortune command for the MeshCore Bot
Returns a random fortune from a BSD-format fortune file (entries separated by %)
"""

import random
from pathlib import Path
from typing import Any

from ..models import MeshMessage
from ..security_utils import validate_safe_path
from .base_command import BaseCommand


class FortuneCommand(BaseCommand):
    """Returns a random fortune from a configured BSD-format fortune file."""

    # Plugin metadata
    name = "fortune"
    keywords = ["fortune"]
    description = "Get a random fortune from the fortune file"
    category = "entertainment"
    cooldown_seconds = 5
    requires_dm = False
    requires_internet = False

    # Documentation
    short_description = "Get a random fortune"
    usage = "fortune"
    examples = ["fortune"]

    def __init__(self, bot: Any) -> None:
        """Initialize the fortune command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.fortune_enabled = self.get_config_value(
            "Fortune_Command", "enabled", fallback=True, value_type="bool"
        )
        self.fortune_file = self.get_config_value(
            "Fortune_Command", "file", fallback="data/fortune/fortunes.txt"
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if the command can be executed.

        Args:
            message: The message triggering the command.
            skip_channel_check: Unused; passed to super().

        Returns:
            bool: True if enabled and base checks pass.
        """
        if not self.fortune_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        """Return help text for the fortune command."""
        return "Get a random fortune. Usage: fortune"

    def _load_fortunes(self) -> list[str]:
        """Load and parse fortunes from the configured file.

        Fortunes are separated by a line containing only '%'.

        Returns:
            List of fortune strings, or empty list on error.
        """
        try:
            safe_path = validate_safe_path(self.fortune_file, allow_absolute=True)
        except (ValueError, Exception) as e:
            self.logger.error(f"Fortune file path rejected by security check: {e}")
            return []

        if safe_path is None:
            self.logger.error(
                f"Fortune file path rejected by security check: {self.fortune_file}"
            )
            return []

        try:
            text = Path(safe_path).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            self.logger.error(f"Fortune file not found: {safe_path}")
            return []
        except OSError as e:
            self.logger.error(f"Error reading fortune file {safe_path}: {e}")
            return []

        # BSD fortune format: lines containing only '%' separate entries.
        # Works whether '%' is used as a separator (no trailing %) or
        # a terminator (trailing % at end of file).
        fortunes: list[str] = []
        current: list[str] = []
        for line in text.splitlines():
            if line.strip() == "%":
                entry = "\n".join(current).strip()
                if entry:
                    fortunes.append(entry)
                current = []
            else:
                current.append(line)
        # Capture final entry if file has no trailing %
        tail = "\n".join(current).strip()
        if tail:
            fortunes.append(tail)

        return fortunes

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the fortune command.

        Picks a random fortune from the file and sends it.

        Args:
            message: The message that triggered the command.

        Returns:
            bool: True always (errors are handled gracefully).
        """
        try:
            fortunes = self._load_fortunes()

            if not fortunes:
                await self.send_response(message, "No fortunes available right now. Try again later!")
                return True

            fortune = random.choice(fortunes)
            await self.send_response(message, fortune)
            return True

        except Exception as e:
            self.logger.error(f"Error in fortune command: {e}", exc_info=True)
            await self.send_response(message, "Sorry, something went wrong getting a fortune!")
            return True
