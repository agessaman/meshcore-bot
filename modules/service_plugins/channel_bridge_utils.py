#!/usr/bin/env python3
"""
Shared base for one-way bridges that post MeshCore channel messages elsewhere
(Discord, Telegram). The file name ends in ``_utils`` so the service loader
does not take ChannelBridgeBase for a service of its own.
"""

import asyncio
import contextlib
import copy
import time
from collections import deque
from typing import Any

from meshcore import EventType

from ..profanity_filter import censor, contains_profanity
from .base_service import BaseServicePlugin


class ChannelBridgeBase(BaseServicePlugin):
    """Lifecycle, message intake and the retrying send queue shared by channel bridges.

    Subclasses provide the destination: which targets a channel maps to, how a
    message is formatted and queued, the rate limit, and the HTTP post. Log
    wording that differs between bridges is kept per subclass through the
    ``*_log`` templates.
    """

    # "Discord" or "Telegram": names the bridge in shared log lines
    bridge_label = ""
    # Payload key holding the message text (for the dropped-after-retries line)
    payload_text_key = "text"
    old_message_log = "Dropping old message from queue [{channel}]: age {age:.1f}s > {max_age}s"
    retry_log = "Message failed, retry in {delay:.1f}s ({retry}/{max_retries}) [{channel}]"
    processor_error_log = "Error in queue processor: {error}"

    # Set by subclasses in __init__ (annotations for type checkers)
    filter_profanity: str
    bridge_bot_responses: bool
    http_session: Any
    message_queues: dict[str, Any]
    send_times: dict[str, Any]
    max_retries: int
    retry_delay_base: float
    max_queue_age: float
    _queue_processor_task: Any

    @property
    def _bridge_mappings(self) -> dict[str, Any]:
        """Configured channel name -> destination(s)."""
        raise NotImplementedError

    def _open_http_session(self) -> None:
        raise NotImplementedError

    def _init_queues(self) -> None:
        raise NotImplementedError

    def _targets_for(self, channel_name: str) -> Any:
        """The configured destination(s) for a channel, or None."""
        raise NotImplementedError

    def _prepare_message_text(self, message_text: str) -> str:
        """Hook applied to the message text before the profanity filter."""
        return message_text

    async def _deliver(self, targets: Any, sender_name: str, message_text: str, channel_name: str) -> None:
        raise NotImplementedError

    def _throttled(self, key: str, queue: Any, current_time: float) -> bool:
        """True when the destination's rate limit says to wait."""
        raise NotImplementedError

    async def _send_queued(self, queued_msg: Any) -> bool:
        raise NotImplementedError

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info(f"{self.bridge_label} bridge service is disabled")
            return
        if not self._bridge_mappings:
            self.logger.warning(f"{self.bridge_label} bridge enabled but no channels configured")
            return

        self.logger.info(f"Starting {self.bridge_label} bridge service...")
        self._open_http_session()

        # Subscribe to channel message events
        # NOTE: We do NOT subscribe to CONTACT_MSG_RECV (DMs are never bridged)
        if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
            self._subscribe(self.bot.meshcore, EventType.CHANNEL_MSG_RECV, self._on_mesh_channel_message)
            self.logger.info("Subscribed to CHANNEL_MSG_RECV events")
        else:
            self.logger.error("Cannot subscribe to events - meshcore not available")
            if self.http_session is not None:
                await self.http_session.close()
                self.http_session = None
            return

        # Register for bot-sent channel messages so bot responses are bridged too
        if self.bridge_bot_responses and getattr(self.bot, 'channel_sent_listeners', None) is not None:
            self.bot.channel_sent_listeners.append(self._on_mesh_channel_message)
            self.logger.info("Registered for bot channel-sent events (bridge_bot_responses=true)")

        self._init_queues()

        # Start background queue processor task
        self._queue_processor_task = asyncio.create_task(self._process_message_queues())

        self._running = True
        self.logger.info(
            f"{self.bridge_label} bridge service started (bridging {len(self._bridge_mappings)} channels)"
        )

    async def on_transport_reconnected(self) -> None:
        """Re-subscribe to channel messages on the new meshcore instance."""
        if not self._running or not getattr(self.bot, 'meshcore', None):
            return
        self._unsubscribe_all()
        self._subscribe(self.bot.meshcore, EventType.CHANNEL_MSG_RECV, self._on_mesh_channel_message)
        self.logger.info(f"{self.bridge_label} bridge re-subscribed to CHANNEL_MSG_RECV after transport reconnect")

    async def stop(self) -> None:
        self.logger.info(f"Stopping {self.bridge_label} bridge service...")
        self._running = False
        self._unsubscribe_all()

        # Unregister bot channel-sent listener
        if getattr(self.bot, 'channel_sent_listeners', None) is not None:
            with contextlib.suppress(ValueError):
                self.bot.channel_sent_listeners.remove(self._on_mesh_channel_message)

        await self._cancel_tasks(self._queue_processor_task)
        if self.http_session:
            await self.http_session.close()
            self.http_session = None
        self.logger.info(f"{self.bridge_label} bridge service stopped")

    async def _on_mesh_channel_message(self, event: Any, metadata: Any = None) -> None:
        """Bridge one mesh channel message. DMs are never bridged."""
        try:
            # Copy payload immediately to avoid segfault if event is freed
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("Channel message event has no payload")
                return

            channel_idx = payload.get('channel_idx', 0)
            channel_name = self.bot.channel_manager.get_channel_name(channel_idx)
            text = payload.get('text', '')

            # NEVER bridge DMs (double-check for safety)
            if not channel_name or channel_name.lower() in ('dm', 'direct', 'private'):
                self.logger.debug("Ignoring DM (DMs are never bridged)")
                return

            targets = self._targets_for(channel_name)
            if not targets:
                self.logger.debug(f"Channel '{channel_name}' not configured for {self.bridge_label} bridge")
                return

            # Sender is embedded in the text ("sender: message")
            if ':' in text and not text.startswith('http'):
                parts = text.split(':', 1)
                sender_name = parts[0].strip()
                message_text = parts[1].strip() if len(parts) > 1 else text
            else:
                sender_name = 'Unknown'
                message_text = text

            message_text = self._prepare_message_text(message_text)

            # Profanity filter: drop (don't bridge), censor (replace with ****), or off
            if self.filter_profanity == 'drop':
                if contains_profanity(sender_name, self.logger) or contains_profanity(message_text, self.logger):
                    self.logger.debug(
                        f"{self.bridge_label} bridge: dropping message with profanity from [{channel_name}]"
                    )
                    return
            elif self.filter_profanity == 'censor':
                sender_name = censor(sender_name, self.logger)
                message_text = censor(message_text, self.logger)

            await self._deliver(targets, sender_name, message_text, channel_name)
        except Exception as e:
            self.logger.error(f"Error handling mesh channel message: {e}", exc_info=True)

    async def _process_message_queues(self) -> None:
        """Send queued messages, respecting the rate limit, with exponential backoff on failure."""
        while self._running:
            try:
                current_time = time.time()
                for key, queue in list(self.message_queues.items()):
                    if not queue:
                        continue
                    if self._throttled(key, queue, current_time):
                        continue

                    # Next message whose retry delay has passed
                    queued_msg = None
                    for msg in queue:
                        if current_time >= msg.next_retry_at:
                            queued_msg = msg
                            break
                    if queued_msg is None:
                        continue

                    age = current_time - queued_msg.first_queued
                    if age > self.max_queue_age:
                        queue.remove(queued_msg)
                        self.logger.warning(self.old_message_log.format(
                            channel=queued_msg.channel_name, age=age, max_age=self.max_queue_age
                        ))
                        continue

                    success = await self._send_queued(queued_msg)
                    if success:
                        queue.remove(queued_msg)
                        if key not in self.send_times:
                            self.send_times[key] = deque()
                        self.send_times[key].append(current_time)
                    else:
                        queued_msg.retry_count += 1
                        if queued_msg.retry_count > self.max_retries:
                            queue.remove(queued_msg)
                            self.logger.error(
                                f"Dropping message after {self.max_retries} retries "
                                f"[{queued_msg.channel_name}]: {queued_msg.payload[self.payload_text_key][:50]}..."
                            )
                        else:
                            delay = self.retry_delay_base * (2 ** (queued_msg.retry_count - 1))
                            queued_msg.next_retry_at = current_time + delay
                            self.logger.debug(self.retry_log.format(
                                delay=delay,
                                retry=queued_msg.retry_count,
                                max_retries=self.max_retries,
                                channel=queued_msg.channel_name,
                            ))

                # Small delay to prevent tight loop
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(self.processor_error_log.format(error=e), exc_info=True)
                await asyncio.sleep(1.0)
