#!/usr/bin/env python3
"""
Base service plugin class for background services
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ExternalNotifySettings:
    """Parsed outbound notification targets from the plugin config section."""

    discord_urls: list[str]
    telegram_chat_ids: list[str]
    telegram_token: Optional[str]


class BaseServicePlugin(ABC):
    """Base class for background service plugins.

    This class defines the interface for service plugins, which are long-running
    background tasks that can interact with the bot and mesh network. It manages
    service lifecycle (start/stop) and metadata.

    **Optional outbound notifications (Discord / Telegram)** — read from this
    plugin's config section (``config_section``):

    - ``discord_webhook_urls`` — comma-separated Discord webhook URLs
      (``https://discord.com/api/webhooks/...``).
    - ``telegram_chat_ids`` — comma-separated chat IDs or ``@channel`` usernames.
    - ``telegram_bot_token`` — optional; else ``TELEGRAM_BOT_TOKEN`` env, then
      ``[TelegramBridge] api_token``.

    **Mesh silence convention:** ``silence_mesh_output`` (default false). Services
    that both transmit mesh channel messages and call ``send_external_notifications``
    should skip ``send_channel_message`` when this is true so alerts go only to
    webhook/Telegram. Subclasses implement the guard at their mesh send sites.
    """

    # Optional: Config section name (if different from class name)
    # If not set, will be derived from class name (e.g., PacketCaptureService -> PacketCapture)
    config_section: Optional[str] = None

    # Optional: Service description for metadata
    description: str = ""

    # Optional: Service name for metadata
    name: str = ""

    def __init__(self, bot: Any):
        """Initialize the service plugin.

        Args:
            bot: The MeshCoreBot instance containing the service.
        """
        self.bot = bot
        self.logger = bot.logger
        self.enabled = True
        self._running = False
        self._external_notify_cache: Optional[ExternalNotifySettings] = None

    def _resolve_telegram_token_for_section(self, section: str) -> Optional[str]:
        import os

        if self.bot.config.has_section(section):
            t = (self.bot.config.get(section, "telegram_bot_token", fallback="") or "").strip()
            if t:
                return t
        t = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if t:
            return t
        if self.bot.config.has_section("TelegramBridge"):
            t = (self.bot.config.get("TelegramBridge", "api_token", fallback="") or "").strip()
            if t:
                return t
        return None

    def _parse_external_notify_settings(self) -> ExternalNotifySettings:
        from modules.bridge_outbound import is_valid_discord_webhook_url

        section = self.config_section or self._derive_config_section()
        discord_raw = ""
        telegram_raw = ""
        if self.bot.config.has_section(section):
            discord_raw = (self.bot.config.get(section, "discord_webhook_urls", fallback="") or "").strip()
            telegram_raw = (self.bot.config.get(section, "telegram_chat_ids", fallback="") or "").strip()

        discord_urls = [
            u.strip()
            for u in discord_raw.split(",")
            if u.strip() and is_valid_discord_webhook_url(u.strip())
        ]
        telegram_chat_ids = [t.strip() for t in telegram_raw.split(",") if t.strip()]
        telegram_token = self._resolve_telegram_token_for_section(section)

        return ExternalNotifySettings(
            discord_urls=discord_urls,
            telegram_chat_ids=telegram_chat_ids,
            telegram_token=telegram_token,
        )

    def _get_external_notify_settings(self) -> ExternalNotifySettings:
        if self._external_notify_cache is None:
            self._external_notify_cache = self._parse_external_notify_settings()
        return self._external_notify_cache

    def has_external_notification_targets(self) -> bool:
        """True if Discord URLs are set, or Telegram chats plus a resolved bot token."""
        s = self._get_external_notify_settings()
        if s.discord_urls:
            return True
        return bool(s.telegram_chat_ids and s.telegram_token)

    def _external_notify_discord_username(self) -> str:
        label = self.config_section or self._derive_config_section()
        return (label or "MeshCore")[:80]

    async def send_external_notifications(self, text: str, *, discord_username: Optional[str] = None) -> None:
        """Send text to configured Discord webhooks and Telegram chats.

        No-op when no URLs/chat IDs are configured. Logs per-target failures;
        does not raise. Uses aiohttp with one shared session when available.
        """
        from modules import bridge_outbound

        settings = self._get_external_notify_settings()
        if not settings.discord_urls and not settings.telegram_chat_ids:
            return

        user = discord_username if discord_username is not None else self._external_notify_discord_username()

        if bridge_outbound.AIOHTTP_AVAILABLE:
            import aiohttp

            aws: list[Any] = []
            async with aiohttp.ClientSession() as session:
                for url in settings.discord_urls:
                    aws.append(
                        bridge_outbound.post_discord_webhook(
                            url, text, username=user, session=session, logger=self.logger
                        )
                    )
                tok = settings.telegram_token
                if tok:
                    for cid in settings.telegram_chat_ids:
                        aws.append(
                            bridge_outbound.post_telegram_message(
                                tok, cid, text, session=session, logger=self.logger
                            )
                        )
                elif settings.telegram_chat_ids:
                    self.logger.warning(
                        "telegram_chat_ids set but no Telegram bot token "
                        "(set telegram_bot_token, TELEGRAM_BOT_TOKEN, or [TelegramBridge] api_token)"
                    )

                if aws:
                    results = await asyncio.gather(*aws, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            self.logger.warning("External notification error: %s", r, exc_info=True)
            return

        for url in settings.discord_urls:
            try:
                await bridge_outbound.post_discord_webhook(
                    url, text, username=user, session=None, logger=self.logger
                )
            except Exception as e:
                self.logger.warning("Discord webhook failed: %s", e, exc_info=True)

        tok = settings.telegram_token
        if tok:
            for cid in settings.telegram_chat_ids:
                try:
                    await bridge_outbound.post_telegram_message(
                        tok, cid, text, session=None, logger=self.logger
                    )
                except Exception as e:
                    self.logger.warning("Telegram failed: %s", e, exc_info=True)
        elif settings.telegram_chat_ids:
            self.logger.warning(
                "telegram_chat_ids set but no Telegram bot token "
                "(set telegram_bot_token, TELEGRAM_BOT_TOKEN, or [TelegramBridge] api_token)"
            )

    @abstractmethod
    async def start(self) -> None:
        """Start the service.

        This method should:
        - Setup event handlers if needed
        - Start background tasks
        - Initialize any required resources
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the service.

        This method should:
        - Clean up event handlers
        - Stop background tasks
        - Close any open resources
        """
        pass

    def get_metadata(self) -> dict[str, Any]:
        """Get service metadata.

        Returns:
            Dict[str, Any]: Dictionary containing service metadata (name, status, etc.).
        """
        return {
            'name': self._derive_service_name(),
            'class_name': self.__class__.__name__,
            'description': getattr(self, 'description', ''),
            'enabled': self.enabled,
            'running': self._running,
            'config_section': self.config_section or self._derive_config_section()
        }

    def _derive_service_name(self) -> str:
        """Derive service name from class name.

        Returns:
            str: Derived service name (e.g., 'PacketCaptureService' -> 'packetcapture').
        """
        class_name = self.__class__.__name__
        if class_name.endswith('Service'):
            # Remove 'Service' suffix and lowercase
            return class_name[:-7].lower().strip('_')
        return class_name.lower()

    def _derive_config_section(self) -> str:
        """Derive config section name from class name.

        Returns:
            str: Derived config section name.
        """
        if self.config_section:
            return self.config_section

        class_name = self.__class__.__name__
        if class_name.endswith('Service'):
            return class_name[:-7]  # Remove 'Service' suffix
        return class_name

    def is_running(self) -> bool:
        """Check if the service is currently running.

        Returns:
            bool: True if the service is running, False otherwise.
        """
        return self._running

    def is_healthy(self) -> bool:
        """Report whether the service is healthy. Default: healthy if running.
        Override in subclasses for connection-specific checks (e.g. meshcore, MQTT).
        """
        return self._running

