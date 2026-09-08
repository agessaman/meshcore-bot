#!/usr/bin/env python3
"""
Web Viewer Command
Provides commands to manage the web viewer integration
"""

from ..models import MeshMessage
from .base_command import BaseCommand


class WebViewerCommand(BaseCommand):
    """Command for managing web viewer integration"""

    # Plugin metadata
    name = "webviewer"
    keywords = ["webviewer", "web", "viewer", "wv"]
    description = "Manage web viewer integration (DM only)"
    requires_dm = True
    cooldown_seconds = 0
    category = "management"

    def __init__(self, bot):
        """Initialize the webviewer command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.webviewer_enabled = self.get_config_value('WebViewer_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.webviewer_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        """Get help text for the webviewer command.

        Returns:
            str: The help text for this command.
        """
        return "Usage: webviewer <subcommand>\nSubcommands: status, reset, restart"

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with 'webviewer' keyword.

        Args:
            message: The received message.

        Returns:
            bool: True if matches, False otherwise.
        """
        return self._cleaned_content_matches(
            message,
            lambda content_lower: any(
                content_lower.startswith(keyword + ' ') or content_lower == keyword
                for keyword in self.keywords
            ),
        )

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the webviewer command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()

        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()

        # Parse subcommand
        parts = content.split()
        if len(parts) < 2:
            await self.send_response(message, "Usage: webviewer <subcommand>\nSubcommands: status, reset, restart")
            return True

        subcommand = parts[1].lower()

        if subcommand == "status":
            await self._handle_status(message)
        elif subcommand == "reset":
            await self._handle_reset(message)
        elif subcommand == "restart":
            await self._handle_restart(message)
        else:
            await self.send_response(message, "Unknown subcommand. Use: status, reset, restart")

        return True

    async def _handle_status(self, message: MeshMessage) -> None:
        """Handle status subcommand.

        Args:
            message: The message that triggered the detailed status request.
        """
        if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
            await self.send_response(message, "Web viewer integration not available")
            return

        integration = self.bot.web_viewer_integration
        en = 1 if integration.enabled else 0
        run = 1 if integration.running else 0
        url = f"{integration.host}:{integration.port}" if integration.running else "-"

        cb = "ok"
        fail = 0
        shut = 0
        if hasattr(integration, 'bot_integration') and integration.bot_integration:
            bot_integration = integration.bot_integration
            cb = "open" if bot_integration.circuit_breaker_open else "ok"
            fail = bot_integration.circuit_breaker_failures
            shut = 1 if getattr(bot_integration, 'is_shutting_down', False) else 0

        # Compact one-liner stays under DM 158-byte budget
        status_text = f"WV en={en} run={run} url={url} cb={cb} fail={fail} shut={shut}"
        await self.send_response(message, status_text)

    async def _handle_reset(self, message: MeshMessage) -> None:
        """Handle reset subcommand.

        Args:
            message: The message that triggered the reset request.
        """
        if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
            await self.send_response(message, "Web viewer integration not available")
            return

        if hasattr(self.bot.web_viewer_integration, 'bot_integration') and self.bot.web_viewer_integration.bot_integration:
            self.bot.web_viewer_integration.bot_integration.reset_circuit_breaker()
            await self.send_response(message, "Circuit breaker reset")
        else:
            await self.send_response(message, "Bot integration not available")

    async def _handle_restart(self, message: MeshMessage) -> None:
        """Handle restart subcommand.

        Args:
            message: The message that triggered the restart request.
        """
        if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
            await self.send_response(message, "Web viewer integration not available")
            return

        try:
            self.bot.web_viewer_integration.restart_viewer()
            await self.send_response(message, "Web viewer restart initiated")
        except Exception as e:
            await self.send_response(message, f"Failed to restart web viewer: {e}")
