"""Radio link and health, mixed into MeshCoreBot.

Connecting and reconnecting the transport, the host->radio command serializer and
``radio_session``, zombie detection and the periodic health probe."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import time
from typing import Any

import meshcore
from meshcore import EventType

# True while the current task holds the radio through MeshCoreBot.radio_session(),
# so the frames it sends don't try to take the (non-reentrant) lock again.
_radio_session_held: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "radio_session_held", default=False
)


def _serialize_command_frames(bot: RadioLinkMixin, commands: Any) -> bool:
    """Route every host->radio frame through the bot's radio command lock.

    The companion firmware processes one host serial frame per main-loop
    iteration and has no mid-frame resync: a burst of concurrent commands can
    overrun the radio's USB-CDC RX buffer, drop a byte, and permanently desync
    its frame parser (commands stop being acted on while RX push frames keep
    flowing). The bot issues commands from many independent asyncio tasks
    (sends, channel/contact ops, scheduler ops, health probes, auto message
    fetch) with no shared serialization.

    Every meshcore command writes its frame through ``CommandHandler.send()``,
    which waits for the radio's immediate reply (OK, ERROR, MSG_SENT, ...).
    Wrapping ``send`` on the handler instance serializes exactly that exchange
    and paces frames by a minimum interval, so there is at most one in-flight
    companion frame at a time. Library methods call ``self.send``, so composite
    commands (``send_msg_with_retry``, ``req_*_sync``, ``send_login_sync``) and
    ``meshcore_cli.next_cmd`` are covered too, while their waits for ACKs and
    remote responses happen outside the lock and don't block other senders.

    Returns False when ``commands`` is already serialized.
    """
    send = commands.send
    if getattr(send, "_radio_serialized", False):
        return False

    @functools.wraps(send)
    async def _serialized_send(*args: Any, **kwargs: Any) -> Any:
        if _radio_session_held.get():
            await bot._pace_radio_command()
            return await send(*args, **kwargs)
        async with bot._get_radio_cmd_lock():
            await bot._pace_radio_command()
            return await send(*args, **kwargs)

    _serialized_send._radio_serialized = True  # type: ignore[attr-defined]
    commands.send = _serialized_send
    return True




class RadioLinkMixin:
    """Mixed into MeshCoreBot."""

    _allow_offline_trial: Any
    _configure_meshcore_debug_logging: Any
    _radio_cmd_last_ts: Any
    _radio_cmd_lock: Any
    _radio_cmd_min_interval: Any
    _radio_fail_count: Any
    _radio_relinks_in_progress: Any
    _radio_zombie_detected: Any
    _shutdown_event: Any
    _tcp_probe_fail_count: Any
    _transport_reconnect_in_progress: Any
    _transport_reconnect_lock: Any
    channel_manager: Any
    config: Any
    connected: Any
    connection_time: Any
    db_manager: Any
    logger: Any
    meshcore: Any
    set_device_name: Any
    set_radio_clock: Any
    setup_message_handlers: Any
    transmission_tracker: Any
    wait_for_contacts: Any

    @property
    def is_radio_zombie(self) -> bool:
        """True when the radio firmware has been confirmed unresponsive.

        All outbound radio sends should check this flag and abort immediately.
        Only a physical power cycle can recover the radio; the flag is cleared
        automatically when connect() succeeds after a power cycle.
        """
        return bool(self._radio_zombie_detected)

    async def _attempt_reconnect(self) -> bool:
        """Attempt to reconnect to the MeshCore node with exponential backoff.

        Reads reconnect settings from [Connection]:
          reconnect_max_retries  – max attempts before giving up (0 = unlimited, default 0)
          reconnect_delay_seconds – initial wait between attempts (default 10)
          reconnect_max_delay_seconds – cap on wait time (default 60)

        Returns:
            bool: True if reconnection succeeded, False if max retries exhausted or shutdown.
        """
        max_retries = self.config.getint('Connection', 'reconnect_max_retries', fallback=0)
        delay = self.config.getfloat('Connection', 'reconnect_delay_seconds', fallback=10.0)
        max_delay = self.config.getfloat('Connection', 'reconnect_max_delay_seconds', fallback=60.0)

        attempt = 0
        while not self._shutdown_event.is_set():
            if max_retries > 0 and attempt >= max_retries:
                self.logger.error(f"Reconnect failed after {max_retries} attempt(s), giving up")
                return False

            attempt += 1
            retry_label = f"{attempt}/{max_retries}" if max_retries > 0 else str(attempt)
            self.logger.info(f"Reconnect attempt {retry_label}...")

            # Clean up the stale connection object
            old_meshcore = self.meshcore
            self.meshcore = None
            self.connected = False
            if old_meshcore is not None:
                try:
                    await asyncio.wait_for(old_meshcore.disconnect(), timeout=5.0)
                except Exception:
                    pass

            if await self.connect():
                self.logger.info("Reconnected successfully")
                if hasattr(self, 'transmission_tracker') and self.transmission_tracker:
                    self.transmission_tracker._update_bot_prefix()
                return True

            self.logger.warning(
                f"Reconnect attempt {retry_label} failed, retrying in {delay:.0f}s..."
            )
            # Interruptible sleep so shutdown isn't delayed
            elapsed = 0.0
            while elapsed < delay and not self._shutdown_event.is_set():
                await asyncio.sleep(1.0)
                elapsed += 1.0

            delay = min(delay * 2, max_delay)

        return False

    def _connection_type(self) -> str:
        """Configured transport: serial, ble, or tcp."""
        return self.config.get('Connection', 'connection_type', fallback='ble').lower()

    def _radio_probe_fail_threshold(self) -> int:
        return self.config.getint(
            'Connection',
            'radio_probe_fail_threshold',
            fallback=self.config.getint('Bot', 'radio_probe_fail_threshold', fallback=3),
        )

    def _radio_probe_interval_seconds(self) -> int:
        return max(
            300,
            min(
                900,
                self.config.getint(
                    'Connection',
                    'radio_probe_interval_seconds',
                    fallback=self.config.getint(
                        'Bot', 'radio_probe_interval_seconds', fallback=300
                    ),
                ),
            ),
        )

    async def _schedule_transport_reconnect(self, reason: str) -> None:
        """Queue a transport-level reconnect (non-blocking)."""
        if self._shutdown_event.is_set():
            return
        if reason == 'manual_disconnect':
            return
        if self._transport_reconnect_in_progress:
            return
        if not getattr(self, 'connected', False):
            return

        self._transport_reconnect_in_progress = True
        self.logger.warning(
            "Transport disconnect detected (%s), scheduling reconnect...",
            reason,
        )
        self._update_radio_connected_metadata(False)
        asyncio.create_task(self._run_transport_reconnect())

    async def _run_transport_reconnect(self) -> None:
        """Run reconnect with lock; clear in-progress flag when done."""
        try:
            if self._transport_reconnect_lock is None:
                self._transport_reconnect_lock = asyncio.Lock()
            async with self._transport_reconnect_lock:
                if self._shutdown_event.is_set():
                    return
                if not await self._attempt_reconnect():
                    self.logger.error("Could not reconnect, shutting down")
                    self.connected = False
        finally:
            self._transport_reconnect_in_progress = False

    def _get_radio_cmd_lock(self) -> asyncio.Lock:
        """Return the lock that serializes host->radio commands.

        Created lazily so it binds to the running event loop.
        """
        if self._radio_cmd_lock is None:
            self._radio_cmd_lock = asyncio.Lock()
        return self._radio_cmd_lock

    async def _pace_radio_command(self) -> None:
        """Enforce a minimum gap between consecutive companion frames.

        Must be called while holding the radio command lock so the timestamp
        bookkeeping stays serialized.
        """
        interval = self._radio_cmd_min_interval
        if interval <= 0:
            return
        now = time.monotonic()
        wait = interval - (now - self._radio_cmd_last_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._radio_cmd_last_ts = time.monotonic()

    @contextlib.asynccontextmanager
    async def radio_session(self):
        """Hold the radio for a short sequence of frames that must not interleave.

        Frames are serialized one at a time, so another task's frame can land
        between two of ours. Use this when that matters, e.g. setting the flood
        scope, sending, and restoring it, so no other send goes out under the
        temporary scope. Keep it short: every other sender waits, so don't wait
        for ACKs or remote responses inside it. Re-entering from the same task
        is a no-op. Tasks created inside the session inherit it, so don't spawn
        work that sends after the session ends.
        """
        if _radio_session_held.get():
            yield
            return
        async with self._get_radio_cmd_lock():
            token = _radio_session_held.set(True)
            try:
                yield
            finally:
                _radio_session_held.reset(token)

    def _install_command_serializer(self) -> None:
        """Serialize and pace every frame ``meshcore.commands`` writes.

        Idempotent and safe to call after each (re)connect. The handler is
        wrapped in place, so existing call sites (``self.meshcore.commands.*``
        and ``meshcore_cli.next_cmd``) need no per-call changes.
        """
        if not self.meshcore:
            return
        cmds = getattr(self.meshcore, "commands", None)
        if cmds is None:
            return
        try:
            if _serialize_command_frames(self, cmds):
                self.logger.debug(
                    "Installed serialized command gateway (min interval %.0fms)",
                    self._radio_cmd_min_interval * 1000,
                )
        except (AttributeError, TypeError) as e:
            self.logger.warning(f"Could not install command serializer: {e}")

    async def connect(self) -> bool:
        """Connect to MeshCore node using official package.

        Establishes a connection to the mesh node via Serial, TCP, or BLE
        based on the configuration.

        Returns:
            bool: True if connection was successful, False otherwise.
        """
        new_meshcore = None
        connection_ready = False
        try:
            self.logger.info("Connecting to MeshCore node...")

            # Get connection type from config
            connection_type = self.config.get('Connection', 'connection_type', fallback='ble').lower()
            # radio_debug: config.ini baseline, overridden by bot_metadata (set via web UI)
            radio_debug = self.config.getboolean('Connection', 'radio_debug', fallback=False)
            try:
                meta_val = self.db_manager.get_metadata('radio.debug')
                if meta_val == 'true':
                    radio_debug = True
                elif meta_val == 'false':
                    radio_debug = False
            except Exception:
                pass
            self.logger.info(f"Using connection type: {connection_type}")
            if radio_debug:
                self.logger.info("Radio debug logging enabled — meshcore library output will be at DEBUG level")

            if connection_type == 'serial':
                # Create serial connection
                serial_port = self.config.get('Connection', 'serial_port', fallback='/dev/ttyUSB0')
                self.logger.info(f"Connecting via serial port: {serial_port}")
                new_meshcore = await meshcore.MeshCore.create_serial(serial_port, debug=radio_debug)
            elif connection_type == 'tcp':
                # Create TCP connection
                hostname = self.config.get('Connection', 'hostname', fallback=None)
                tcp_port = self.config.getint('Connection', 'tcp_port', fallback=5000)
                if not hostname:
                    self.logger.error("TCP connection requires 'hostname' to be set in config")
                    return False
                self.logger.info(f"Connecting via TCP: {hostname}:{tcp_port}")
                new_meshcore = await meshcore.MeshCore.create_tcp(hostname, tcp_port, debug=radio_debug)
            else:
                # Create BLE connection (default)
                ble_device_name = self.config.get('Connection', 'ble_device_name', fallback=None)
                self.logger.info("Connecting via BLE" + (f" to device: {ble_device_name}" if ble_device_name else ""))
                new_meshcore = await meshcore.MeshCore.create_ble(ble_device_name, debug=radio_debug)

            self.meshcore = new_meshcore

            # Route meshcore library output through the bot's handlers (including log file)
            self._configure_meshcore_debug_logging(radio_debug)

            # Serialize all host->radio commands before issuing any (the connect
            # init burst — contacts, channel fetch, clock, name — runs through it).
            self._install_command_serializer()

            if self.meshcore and self.meshcore.is_connected:
                self.connected = True
                self._update_radio_connected_metadata(True)
                # Track connection time to skip processing old cached messages
                self.connection_time = time.time()
                # Clear zombie state — a successful connect means the radio is alive again
                self._radio_zombie_detected = False
                self._radio_fail_count = 0
                self._tcp_probe_fail_count = 0
                try:
                    self.db_manager.set_metadata('bot.radio_zombie', 'false')
                    self.db_manager.set_metadata('bot.radio_zombie_since', '')
                except Exception:
                    pass
                self.logger.info(f"Connected to: {self.meshcore.self_info} at {self.connection_time}")

                # Wait for contacts to load
                await self.wait_for_contacts()

                # A connected transport without channel data cannot route replies.
                if not await self.channel_manager.fetch_channels():
                    raise ConnectionError(
                        "MeshCore node returned no channels after retries"
                    )

                # Setup message event handlers
                await self.setup_message_handlers()

                # Set radio clock if needed
                await self.set_radio_clock()

                # Set device name to match config if needed
                await self.set_device_name()

                await self._notify_services_transport_reconnected()

                connection_ready = True
                return True
            else:
                self.logger.error("Failed to connect to MeshCore node")
                return False

        except (OSError, ConnectionError, TimeoutError, ValueError, AttributeError) as e:
            self.logger.error(f"Connection failed: {e}")
            return False
        finally:
            if not connection_ready:
                self.connected = False
                self._update_radio_connected_metadata(False)
                if new_meshcore is not None:
                    try:
                        await asyncio.wait_for(new_meshcore.disconnect(), timeout=5.0)
                    except Exception as e:
                        self.logger.warning(
                            "Could not clean up incomplete MeshCore connection: %s",
                            e,
                        )
                    finally:
                        if self.meshcore is new_meshcore:
                            self.meshcore = None

    async def _notify_services_transport_reconnected(self) -> None:
        """Re-bind mesh event subscriptions on running services after transport reconnect."""
        services = getattr(self, 'services', None) or {}
        for name, service in services.items():
            if not service.is_running():
                continue
            try:
                await service.on_transport_reconnected()
            except Exception as e:
                self.logger.error(
                    "Service '%s' on_transport_reconnected failed: %s",
                    name,
                    e,
                    exc_info=True,
                )

    def _update_radio_connected_metadata(self, connected: bool) -> None:
        """Write radio connection state to bot_metadata for the web viewer."""
        try:
            self.db_manager.set_metadata('radio_connected', '1' if connected else '0')
        except Exception as e:
            self.logger.warning(f"Could not update radio_connected metadata: {e}")

    async def disconnect_radio(self) -> bool:
        """Disconnect from the radio, which also stops the bot.

        Despite the name, this is not a radio-only operation: ``run()``ing loops
        while ``keep_running`` is true. This operation clears ``connected`` without
        setting a reconnect/relink flag, so it ends the main loop and the process
        exits. The web viewer therefore labels the control "Stop Bot" and confirms
        first (issue #240). Keep that in mind before calling this from anywhere that
        only means to drop the radio link.

        Called by the scheduler via the operation queue.
        """
        import asyncio
        try:
            if self.meshcore:
                try:
                    await asyncio.wait_for(self.meshcore.disconnect(), timeout=10)
                except asyncio.TimeoutError:
                    self.logger.warning("Radio disconnect timed out after 10s — forcing disconnected state")
            self.connected = False
            self._update_radio_connected_metadata(False)
            self.logger.info("Radio disconnected via web viewer request")
            return True
        except Exception as e:
            self.logger.error(f"Error disconnecting radio: {e}")
            return False

    async def reboot_radio(self) -> bool:
        """Send firmware reboot command, disconnect, wait for reboot, then reconnect."""
        import asyncio
        # Hold the loops open (see keep_running) while connected is False
        self._radio_relinks_in_progress += 1
        try:
            if self.meshcore and self.meshcore.is_connected:
                self.logger.info("Sending firmware reboot command")
                try:
                    await asyncio.wait_for(self.meshcore.commands.reboot(), timeout=5)
                except (asyncio.TimeoutError, Exception) as e:
                    # Reboot command may drop the connection before a reply arrives
                    self.logger.debug(f"Reboot command response: {e} (expected on firmware reboot)")
            # Disconnect cleanly (firmware may have already dropped the link)
            try:
                if self.meshcore:
                    await asyncio.wait_for(self.meshcore.disconnect(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
            self.connected = False
            self._update_radio_connected_metadata(False)
            self.logger.info("Waiting for radio to reboot (8s)…")
            await asyncio.sleep(8)
            return await self.connect()
        except Exception as e:
            self.logger.error(f"Error rebooting radio: {e}")
            return False
        finally:
            self._radio_relinks_in_progress -= 1

    async def reconnect_radio(self) -> bool:
        """Disconnect then reconnect. Called by scheduler for connect ops."""
        import asyncio
        # Hold the loops open (see keep_running) while connected is False
        self._radio_relinks_in_progress += 1
        try:
            if self.meshcore:
                try:
                    await asyncio.wait_for(self.meshcore.disconnect(), timeout=10)
                except asyncio.TimeoutError:
                    self.logger.warning("Disconnect timed out during reconnect — proceeding")
            self.connected = False
            self._update_radio_connected_metadata(False)
            return await self.connect()
        except Exception as e:
            self.logger.error(f"Error reconnecting radio: {e}")
            return False
        finally:
            self._radio_relinks_in_progress -= 1

    def _handle_serial_probe_error(self, threshold: int, interval: int) -> bool:
        """Serial/BLE: failed get_time may indicate zombie firmware (no transport reconnect)."""
        import datetime as _dt

        self._radio_fail_count = self._radio_fail_count + 1
        self.logger.warning(
            "Radio health probe failed "
            "(%d/%d): no response to get_time",
            self._radio_fail_count,
            threshold,
        )
        if self._radio_fail_count >= threshold:
            fail_count = self._radio_fail_count
            self._radio_fail_count = 0
            self._radio_zombie_detected = True
            self.logger.critical(
                "ZOMBIE RADIO DETECTED after %d consecutive failed probes "
                "(probe interval %ds). The radio firmware is unresponsive. "
                "A physical POWER CYCLE is required — disconnect/reconnect "
                "will NOT fix this. Probing suspended until next reconnect.",
                fail_count,
                interval,
            )
            try:
                self.db_manager.set_metadata('bot.radio_zombie', 'true')
                self.db_manager.set_metadata(
                    'bot.radio_zombie_since',
                    _dt.datetime.now(_dt.timezone.utc).isoformat(),
                )
            except Exception:
                pass
            scheduler = getattr(self, 'scheduler', None)
            if scheduler is not None:
                loop = asyncio.get_event_loop()
                loop.run_in_executor(
                    None,
                    scheduler.send_zombie_alert_email,
                    fail_count,
                    threshold,
                    interval,
                )
        return False

    async def _handle_tcp_probe_failure(
        self, threshold: int, interval: int, detail: str
    ) -> bool:
        """TCP: repeated probe failures trigger transport reconnect, not zombie."""
        self._tcp_probe_fail_count = self._tcp_probe_fail_count + 1
        self.logger.warning(
            "TCP radio health probe failed (%d/%d): %s",
            self._tcp_probe_fail_count,
            threshold,
            detail,
        )
        if self._tcp_probe_fail_count >= threshold:
            self._tcp_probe_fail_count = 0
            self.logger.warning(
                "TCP transport unresponsive after %d probes (interval %ds) — reconnecting",
                threshold,
                interval,
            )
            await self._schedule_transport_reconnect('tcp_probe_failed')
        return False

    async def _probe_radio_health(self) -> bool:
        """Send a lightweight get_time() probe to verify the radio is responding.

        Serial/BLE: repeated ERROR responses declare zombie firmware (no reconnect).
        TCP: repeated ERROR or timeout responses schedule transport reconnect.
        """
        if self._radio_zombie_detected:
            return False

        if not self.meshcore or not self.meshcore.is_connected:
            await self._schedule_transport_reconnect('probe_not_connected')
            return False

        is_tcp = self._connection_type() == 'tcp'
        threshold = self._radio_probe_fail_threshold()
        interval = self._radio_probe_interval_seconds()

        try:
            result = await asyncio.wait_for(
                self.meshcore.commands.get_time(), timeout=10.0
            )
            if result.type == EventType.ERROR:
                if is_tcp:
                    return await self._handle_tcp_probe_failure(
                        threshold, interval, 'no response to get_time'
                    )
                return self._handle_serial_probe_error(threshold, interval)

            if self._radio_fail_count > 0:
                self.logger.info("Radio health probe recovered — resetting fail counter")
            self._radio_fail_count = 0
            self._tcp_probe_fail_count = 0
            self._allow_offline_trial("the radio answered a health probe")
            return True
        except asyncio.TimeoutError:
            if is_tcp:
                return await self._handle_tcp_probe_failure(
                    threshold, interval, 'probe timed out'
                )
            self.logger.warning("Radio health probe timed out")
            return False
        except Exception as e:
            self.logger.warning(f"Radio health probe error: {e}")
            return False
