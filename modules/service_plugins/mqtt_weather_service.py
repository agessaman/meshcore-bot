#!/usr/bin/env python3
"""
MQTT subscriber for custom weather topics (custom.mqtt_weather.* in [Weather]).
Caches the latest payload per topic for wx / gwx commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from ..clients.mqtt_weather import MqttWeatherCache, iter_mqtt_weather_topics
from .base_service import BaseServicePlugin


class MqttWeatherService(BaseServicePlugin):
    """Subscribe to configured MQTT weather topics and update bot.mqtt_weather_cache."""

    config_section = "MqttWeather"
    description = "MQTT subscriber for custom.mqtt_weather.* wx/gwx sources"

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.logger = logging.getLogger("MqttWeatherService")
        self.logger.setLevel(bot.logger.level)
        self._client: Any = None
        self._cache: Optional[MqttWeatherCache] = None
        self._topics: list[str] = []

    def _parse_broker_config(self) -> Optional[dict[str, Any]]:
        cfg = self.bot.config
        sec = "MqttWeather"
        if not cfg.has_section(sec):
            return None
        host = cfg.get(sec, "broker", fallback="").strip()
        if not host:
            self.logger.error("MqttWeather: broker hostname is required")
            return None
        port = cfg.getint(sec, "port", fallback=1883)
        transport = cfg.get(sec, "transport", fallback="tcp").strip().lower()
        ws_path = cfg.get(sec, "websocket_path", fallback="/mqtt").strip() or "/mqtt"
        use_tls = cfg.getboolean(sec, "use_tls", fallback=False)
        username = cfg.get(sec, "username", fallback="").strip() or None
        password = cfg.get(sec, "password", fallback="").strip() or None
        client_id = cfg.get(sec, "client_id", fallback="").strip() or None
        qos = cfg.getint(sec, "qos", fallback=0)
        if qos not in (0, 1, 2):
            qos = 0
        return {
            "host": host,
            "port": port,
            "transport": transport,
            "websocket_path": ws_path,
            "use_tls": use_tls,
            "username": username,
            "password": password,
            "client_id": client_id,
            "qos": qos,
        }

    async def start(self) -> None:
        if not self.enabled:
            return

        self._topics = iter_mqtt_weather_topics(self.bot.config)
        if not self._topics:
            self.logger.warning(
                "MqttWeather enabled but no valid custom.mqtt_weather.* topics in [Weather]; "
                "subscriber not started"
            )
            self._running = True
            return

        if mqtt is None:
            self.logger.error(
                "MqttWeather: paho-mqtt not installed; pip install paho-mqtt"
            )
            self._running = True
            return

        broker = self._parse_broker_config()
        if not broker:
            self._running = True
            return

        self._cache = MqttWeatherCache()
        self.bot.mqtt_weather_cache = self._cache

        bot_name = self.bot.config.get("Bot", "bot_name", fallback="MeshCoreBot")
        client_id = broker["client_id"]
        if not client_id:
            safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in bot_name)
            client_id = f"{safe_name}-mqtt-wx-{os.getpid()}"

        topics = self._topics
        qos = broker["qos"]
        cache = self._cache
        logger = self.logger

        def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
            try:
                payload = msg.payload
                if payload is None:
                    return
                if isinstance(payload, (bytes, bytearray)):
                    b = bytes(payload)
                else:
                    b = str(payload).encode("utf-8", errors="replace")
                topic_str = getattr(msg, "topic", None)
                if isinstance(topic_str, bytes):
                    topic_str = topic_str.decode("utf-8", errors="replace")
                if not topic_str:
                    return
                cache.update(topic_str, b)
            except Exception as e:
                logger.debug(f"MqttWeather on_message error: {e}")

        def on_connect(client: Any, userdata: Any, flags: Any, rc: int, properties: Any = None) -> None:
            if rc != 0:
                logger.warning(f"MqttWeather connect failed rc={rc}")
                return
            for t in userdata:
                try:
                    client.subscribe(t, qos=qos)
                    logger.info(f"MqttWeather subscribed to {t!r} (qos={qos})")
                except Exception as e:
                    logger.error(f"MqttWeather subscribe failed for {t!r}: {e}")

        transport = broker["transport"]
        try:
            if transport == "websockets":
                self._client = mqtt.Client(
                    client_id=client_id,
                    userdata=topics,
                    transport="websockets",
                )
                self._client.ws_set_options(path=broker["websocket_path"], headers=None)
            else:
                self._client = mqtt.Client(client_id=client_id, userdata=topics)

            self._client.reconnect_delay_set(min_delay=1, max_delay=120)
            self._client.on_connect = on_connect
            self._client.on_message = on_message

            if broker["use_tls"]:
                import ssl

                self._client.tls_set(cert_reqs=ssl.CERT_NONE)

            if broker["username"]:
                self._client.username_pw_set(broker["username"], broker["password"])

            loop = asyncio.get_event_loop()

            def do_connect() -> None:
                self._client.connect(broker["host"], broker["port"], keepalive=60)

            await loop.run_in_executor(None, do_connect)
            self._client.loop_start()
            self.logger.info(
                f"MqttWeather MQTT started ({broker['host']}:{broker['port']}, {transport})"
            )
        except Exception as e:
            self.logger.error(f"MqttWeather failed to start MQTT client: {e}")
            self._client = None

        self._running = True

    async def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as e:
                self.logger.debug(f"MqttWeather disconnect: {e}")
            self._client = None

        if self._cache is not None:
            self._cache.clear()
        self._cache = None
        if getattr(self.bot, "mqtt_weather_cache", None) is not None:
            delattr(self.bot, "mqtt_weather_cache")

        self._running = False
