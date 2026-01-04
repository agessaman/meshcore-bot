#!/usr/bin/env python3
"""
Discord Bridge Service for MeshCore Bot
Posts MeshCore channel messages to Discord via webhooks (one-way, read-only)
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Any
from datetime import datetime

# Import meshcore
from meshcore import EventType

# Try to import aiohttp for async HTTP (preferred)
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False

# Fallback to requests for sync HTTP
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None
    REQUESTS_AVAILABLE = False

# Import base service
from .base_service import BaseServicePlugin


class DiscordBridgeService(BaseServicePlugin):
    """Discord bridge service.

    Posts MeshCore channel messages to Discord channels via webhooks.
    This is a one-way bridge - messages only flow from MeshCore to Discord.
    Direct messages are NEVER bridged for privacy.
    """

    config_section = 'DiscordBridge'
    description = "Posts MeshCore channel messages to Discord (one-way, read-only)"

    def __init__(self, bot: Any):
        """Initialize Discord bridge service.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)

        # Use bot's logger directly (inherited from BaseServicePlugin)
        # self.logger is already set by super().__init__(bot)

        # Check if HTTP library is available
        if not AIOHTTP_AVAILABLE and not REQUESTS_AVAILABLE:
            self.logger.error("Neither aiohttp nor requests library is available. Discord bridge requires one of these.")
            self.enabled = False
            return

        # Load channel mappings from config (bridge.* pattern)
        self.channel_webhooks: Dict[str, str] = {}
        self._load_channel_mappings()

        # NEVER bridge DMs (hardcoded for privacy)
        self.bridge_dms = False

        # Avatar generation style
        self.avatar_style = self.bot.config.get('DiscordBridge', 'avatar_style', fallback='color').lower()

        # Validate avatar style
        valid_styles = ['color', 'fun-emoji', 'avataaars', 'bottts', 'identicon', 'pixel-art', 'adventurer', 'initials']
        if self.avatar_style not in valid_styles:
            self.logger.warning(f"Invalid avatar_style '{self.avatar_style}', using 'color'. Valid options: {', '.join(valid_styles)}")
            self.avatar_style = 'color'

        self.logger.info(f"Avatar style: {self.avatar_style}")

        # Rate limit tracking per webhook
        # Discord webhooks: 30 messages per minute per webhook
        self.rate_limit_info: Dict[str, Dict[str, Any]] = {}
        self.rate_limit_threshold = 0.20  # Warn at 20% of limit exhaustion

        # HTTP session for async requests
        self.http_session: Optional[aiohttp.ClientSession] = None

        # Background task handle
        self._message_handler_task: Optional[asyncio.Task] = None

        if not self.channel_webhooks:
            self.logger.warning("No Discord channel mappings configured. Discord bridge will not post any messages.")
            self.logger.info("Add channel mappings in config: bridge.<channelname> = <webhook_url>")

    def _load_channel_mappings(self) -> None:
        """Load channel webhook mappings from config.

        Parses config entries with pattern: bridge.<channelname> = <webhook_url>
        Channel names are stored case-insensitively for matching.
        """
        if not self.bot.config.has_section('DiscordBridge'):
            self.logger.warning("No [DiscordBridge] section found in config")
            return

        for key, value in self.bot.config.items('DiscordBridge'):
            # Look for bridge.* pattern
            if key.startswith('bridge.'):
                channel_name = key[7:]  # Remove 'bridge.' prefix
                webhook_url = value.strip()

                # Basic validation
                if not webhook_url.startswith('https://discord.com/api/webhooks/'):
                    self.logger.warning(f"Invalid webhook URL for channel '{channel_name}': {webhook_url[:50]}...")
                    continue

                # Store with original case, but we'll match case-insensitively
                self.channel_webhooks[channel_name] = webhook_url
                # Mask webhook token in logs for security
                masked_url = self._mask_webhook_url(webhook_url)
                self.logger.info(f"Configured Discord bridge: {channel_name} → {masked_url}")

        self.logger.info(f"Loaded {len(self.channel_webhooks)} Discord channel mapping(s)")

    def _generate_avatar_url(self, username: str) -> Optional[str]:
        """Generate a unique avatar URL for a username.

        Supports multiple avatar generation methods:
        - 'color': Uses Discord's default colored avatars (no external API, returns None)
        - DiceBear styles: Uses DiceBear API to generate custom avatars

        Args:
            username: The username to generate an avatar for.

        Returns:
            Optional[str]: URL to the generated avatar image, or None for color-hash method.
        """
        # Color mode: Let Discord use its default colored avatars
        # Return None so Discord generates a colored avatar based on username
        if self.avatar_style == 'color':
            return None

        # DiceBear API styles
        from urllib.parse import quote

        # Clean and encode username for URL
        clean_name = username.strip()
        encoded_name = quote(clean_name)

        # Map config style names to DiceBear API style names
        style_map = {
            'fun-emoji': 'fun-emoji',
            'avataaars': 'avataaars',
            'bottts': 'bottts',
            'identicon': 'identicon',
            'pixel-art': 'pixel-art',
            'adventurer': 'adventurer',
            'initials': 'initials'
        }

        dicebear_style = style_map.get(self.avatar_style, 'fun-emoji')
        avatar_url = f"https://api.dicebear.com/7.x/{dicebear_style}/png?seed={encoded_name}"

        return avatar_url

    def _format_mentions(self, text: str) -> str:
        """Format MeshCore mentions for Discord.

        Converts @[username] to **@username** for better visibility.

        Args:
            text: Message text containing MeshCore mentions.

        Returns:
            str: Text with formatted mentions.
        """
        import re

        # Pattern to match @[username] - username can contain spaces, emojis, special chars
        # Match @[ followed by any characters until ]
        pattern = r'@\[([^\]]+)\]'

        # Replace with bolded mention: @[username] → **@username**
        formatted = re.sub(pattern, r'**@\1**', text)

        return formatted

    def _mask_webhook_url(self, url: str) -> str:
        """Mask webhook token for safe logging.

        Args:
            url: Full webhook URL with token.

        Returns:
            str: URL with token partially masked.
        """
        # Discord webhook format: https://discord.com/api/webhooks/{id}/{token}
        parts = url.split('/')
        if len(parts) >= 7:
            # Mask the token (last part)
            token = parts[-1]
            if len(token) > 8:
                masked_token = token[:4] + '...' + token[-4:]
            else:
                masked_token = '***'
            parts[-1] = masked_token
            return '/'.join(parts)
        return url[:50] + '...'

    async def start(self) -> None:
        """Start the Discord bridge service.

        Sets up message event handlers and initializes HTTP session.
        """
        if not self.enabled:
            self.logger.info("Discord bridge service is disabled")
            return

        if not self.channel_webhooks:
            self.logger.warning("Discord bridge enabled but no channels configured")
            return

        self.logger.info("Starting Discord bridge service...")

        # Create aiohttp session if available
        if AIOHTTP_AVAILABLE:
            self.http_session = aiohttp.ClientSession()
            self.logger.debug("Using aiohttp for async HTTP requests")
        else:
            self.logger.debug("Using requests library for HTTP requests (fallback)")

        # Subscribe to channel message events
        # NOTE: We do NOT subscribe to CONTACT_MSG_RECV (DMs are never bridged)
        if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
            self.bot.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_mesh_channel_message)
            self.logger.info("Subscribed to CHANNEL_MSG_RECV events")
        else:
            self.logger.error("Cannot subscribe to events - meshcore not available")
            return

        self._running = True
        self.logger.info(f"Discord bridge service started (bridging {len(self.channel_webhooks)} channels)")

    async def stop(self) -> None:
        """Stop the Discord bridge service.

        Cleans up HTTP session and event handlers.
        """
        self.logger.info("Stopping Discord bridge service...")
        self._running = False

        # Close aiohttp session
        if self.http_session:
            await self.http_session.close()
            self.http_session = None

        self.logger.info("Discord bridge service stopped")

    async def _on_mesh_channel_message(self, event, metadata=None) -> None:
        """Handle incoming mesh channel messages.

        Posts messages to corresponding Discord channels via webhooks.
        DMs are explicitly ignored for privacy.

        Args:
            event: The MeshCore event object containing the message payload.
            metadata: Optional metadata dictionary associated with the event.
        """
        try:
            payload = event.payload

            # Extract channel index and convert to channel name
            channel_idx = payload.get('channel_idx', 0)
            channel_name = self.bot.channel_manager.get_channel_name(channel_idx)

            # Extract sender and text
            # Sender is embedded in the text (format: "sender: message")
            text = payload.get('text', '')
            sender = 'Unknown'

            # Try to extract sender from text
            if ':' in text and not text.startswith('http'):
                parts = text.split(':', 1)
                sender = parts[0].strip()
                # Don't modify text - keep it as is with sender included

            # NEVER bridge DMs (double-check for safety)
            if not channel_name or channel_name.lower() in ['dm', 'direct', 'private']:
                self.logger.debug("Ignoring DM (DMs are never bridged)")
                return

            # Check if this channel is configured for bridging (case-insensitive)
            webhook_url = None
            matched_config_name = None
            for config_channel, url in self.channel_webhooks.items():
                if config_channel.lower() == channel_name.lower():
                    webhook_url = url
                    matched_config_name = config_channel
                    break

            if not webhook_url:
                self.logger.debug(f"Channel '{channel_name}' not configured for Discord bridge")
                return

            # Extract sender and message for better Discord formatting
            # Format the message for better visual separation
            if ':' in text and not text.startswith('http'):
                # Split on first colon to separate sender from message
                parts = text.split(':', 1)
                sender_name = parts[0].strip()
                message_text = parts[1].strip() if len(parts) > 1 else text
            else:
                # No clear sender format, use whole text
                sender_name = sender  # From earlier extraction
                message_text = text

            # Clean up MeshCore @ mentions: @[username] → **@username**
            message_text = self._format_mentions(message_text)

            # Post to Discord
            await self._post_to_webhook(webhook_url, message_text, channel_name, sender_name)

        except Exception as e:
            self.logger.error(f"Error handling mesh channel message: {e}", exc_info=True)

    async def _post_to_webhook(self, webhook_url: str, message: str, channel_name: str, sender_name: str = None) -> None:
        """Post message to Discord webhook.

        Args:
            webhook_url: Discord webhook URL.
            message: Message text to post.
            channel_name: MeshCore channel name (for logging).
            sender_name: Sender's name to use as webhook username (optional).
        """
        try:
            # Prepare webhook payload
            # Use sender's name as webhook username for better visual separation
            username = sender_name if sender_name else f"MeshCore [{channel_name}]"

            # Generate unique avatar for each user based on username
            avatar_url = self._generate_avatar_url(username)

            # Build payload
            payload = {
                "content": message,
                "username": username
            }

            # Only add avatar_url if we have one (None means use Discord's default colored avatars)
            if avatar_url:
                payload["avatar_url"] = avatar_url

            # Send via aiohttp (async) or requests (sync fallback)
            if AIOHTTP_AVAILABLE and self.http_session:
                await self._post_async(webhook_url, payload, channel_name)
            elif REQUESTS_AVAILABLE:
                await self._post_sync(webhook_url, payload, channel_name)
            else:
                self.logger.error("No HTTP library available for posting to Discord")

        except Exception as e:
            self.logger.error(f"Failed to post to Discord webhook [{channel_name}]: {e}", exc_info=True)

    async def _post_async(self, webhook_url: str, payload: Dict[str, str], channel_name: str) -> None:
        """Post to webhook using aiohttp (async).

        Args:
            webhook_url: Discord webhook URL.
            payload: JSON payload to post.
            channel_name: MeshCore channel name (for logging).
        """
        try:
            async with self.http_session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                # Check response status
                if response.status == 204:
                    # Success (Discord webhooks return 204 No Content on success)
                    self.logger.debug(f"Posted to Discord [{channel_name}]: {payload['content'][:50]}...")
                elif response.status == 429:
                    # Rate limited
                    retry_after = response.headers.get('Retry-After', 'unknown')
                    self.logger.warning(f"Discord rate limit hit for [{channel_name}]. Retry after: {retry_after}s")
                else:
                    # Other error
                    response_text = await response.text()
                    self.logger.warning(f"Discord webhook returned {response.status} for [{channel_name}]: {response_text[:200]}")

                # Monitor rate limit headers
                self._check_rate_limit_headers(response.headers, webhook_url, channel_name)

        except asyncio.TimeoutError:
            self.logger.error(f"Timeout posting to Discord webhook [{channel_name}]")
        except Exception as e:
            self.logger.error(f"Error posting to Discord webhook [{channel_name}]: {e}")

    async def _post_sync(self, webhook_url: str, payload: Dict[str, str], channel_name: str) -> None:
        """Post to webhook using requests library (sync fallback).

        Args:
            webhook_url: Discord webhook URL.
            payload: JSON payload to post.
            channel_name: MeshCore channel name (for logging).
        """
        try:
            # Run in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(webhook_url, json=payload, timeout=10)
            )

            # Check response status
            if response.status_code == 204:
                # Success
                self.logger.debug(f"Posted to Discord [{channel_name}]: {payload['content'][:50]}...")
            elif response.status_code == 429:
                # Rate limited
                retry_after = response.headers.get('Retry-After', 'unknown')
                self.logger.warning(f"Discord rate limit hit for [{channel_name}]. Retry after: {retry_after}s")
            else:
                # Other error
                self.logger.warning(f"Discord webhook returned {response.status_code} for [{channel_name}]: {response.text[:200]}")

            # Monitor rate limit headers
            self._check_rate_limit_headers(response.headers, webhook_url, channel_name)

        except Exception as e:
            self.logger.error(f"Error posting to Discord webhook [{channel_name}]: {e}")

    def _check_rate_limit_headers(self, headers: Dict[str, str], webhook_url: str, channel_name: str) -> None:
        """Check Discord rate limit headers and log warnings if approaching limit.

        Discord includes rate limit information in response headers:
        - X-RateLimit-Limit: Total requests allowed per time window
        - X-RateLimit-Remaining: Requests remaining in current window
        - X-RateLimit-Reset: Unix timestamp when limit resets

        Args:
            headers: HTTP response headers from Discord.
            webhook_url: Webhook URL (used as key for tracking).
            channel_name: Channel name for logging.
        """
        try:
            # Extract rate limit headers (case-insensitive)
            # Convert headers to dict if needed (aiohttp uses CIMultiDict)
            headers_dict = dict(headers) if hasattr(headers, 'items') else headers

            limit = headers_dict.get('X-RateLimit-Limit') or headers_dict.get('x-ratelimit-limit')
            remaining = headers_dict.get('X-RateLimit-Remaining') or headers_dict.get('x-ratelimit-remaining')
            reset = headers_dict.get('X-RateLimit-Reset') or headers_dict.get('x-ratelimit-reset')

            if limit and remaining:
                limit = int(limit)
                remaining = int(remaining)

                # Calculate percentage remaining
                if limit > 0:
                    percent_remaining = remaining / limit

                    # Store rate limit info
                    if webhook_url not in self.rate_limit_info:
                        self.rate_limit_info[webhook_url] = {}

                    self.rate_limit_info[webhook_url].update({
                        'limit': limit,
                        'remaining': remaining,
                        'reset': reset,
                        'last_check': time.time()
                    })

                    # Warn if within 20% of exhausting limit
                    if percent_remaining <= self.rate_limit_threshold:
                        reset_time = datetime.fromtimestamp(float(reset)) if reset else 'unknown'
                        self.logger.warning(
                            f"Discord rate limit warning [{channel_name}]: "
                            f"{remaining}/{limit} requests remaining ({percent_remaining*100:.1f}%). "
                            f"Resets at: {reset_time}"
                        )
                    else:
                        # Debug log current state
                        self.logger.debug(
                            f"Discord rate limit [{channel_name}]: "
                            f"{remaining}/{limit} requests remaining ({percent_remaining*100:.1f}%)"
                        )

        except (ValueError, TypeError, KeyError) as e:
            self.logger.debug(f"Error parsing rate limit headers: {e}")
