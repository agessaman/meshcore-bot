#!/usr/bin/env python3
"""
Web Viewer Command
Provides commands to manage the web viewer integration
"""

from .base_command import BaseCommand
from ..models import MeshMessage


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
        super().__init__(bot)
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with 'webviewer' keyword"""
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Check if message starts with any of our keywords
        content_lower = content.lower()
        for keyword in self.keywords:
            if content_lower.startswith(keyword + ' '):
                return True
        return False
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the webviewer command"""
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Parse subcommand
        parts = content.split()
        if len(parts) < 2:
            await self.bot.send_response("Usage: webviewer <subcommand>\nSubcommands: status, reset, restart")
            return True
        
        subcommand = parts[1].lower()
        
        if subcommand == "status":
            await self._handle_status(message)
        elif subcommand == "reset":
            await self._handle_reset(message)
        elif subcommand == "restart":
            await self._handle_restart(message)
        else:
            await self.bot.send_response("Unknown subcommand. Use: status, reset, restart")
        
        return True
    
    async def _handle_status(self, message: MeshMessage):
        """Handle status subcommand"""
        if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
            await self.bot.send_response("Web viewer integration not available")
            return
        
        integration = self.bot.web_viewer_integration
        status = {
            'enabled': integration.enabled,
            'running': integration.running,
            'url': f"http://{integration.host}:{integration.port}" if integration.running else None
        }
        
        if hasattr(integration, 'bot_integration') and integration.bot_integration:
            bot_integration = integration.bot_integration
            status.update({
                'circuit_breaker_open': bot_integration.circuit_breaker_open,
                'circuit_breaker_failures': bot_integration.circuit_breaker_failures,
                'shutdown': getattr(bot_integration, 'is_shutting_down', False)
            })
        
        status_text = "Web Viewer Status:\n"
        for key, value in status.items():
            status_text += f"• {key}: {value}\n"
        
        await self.bot.send_response(status_text)
    
    async def _handle_reset(self, message: MeshMessage):
        """Handle reset subcommand"""
        if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
            await self.bot.send_response("Web viewer integration not available")
            return
        
        if hasattr(self.bot.web_viewer_integration, 'bot_integration') and self.bot.web_viewer_integration.bot_integration:
            self.bot.web_viewer_integration.bot_integration.reset_circuit_breaker()
            await self.bot.send_response("Circuit breaker reset")
        else:
            await self.bot.send_response("Bot integration not available")
    
    async def _handle_restart(self, message: MeshMessage):
        """Handle restart subcommand"""
        if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
            await self.bot.send_response("Web viewer integration not available")
            return
        
        try:
            self.bot.web_viewer_integration.restart_viewer()
            await self.bot.send_response("Web viewer restart initiated")
        except Exception as e:
            await self.bot.send_response(f"Failed to restart web viewer: {e}")
