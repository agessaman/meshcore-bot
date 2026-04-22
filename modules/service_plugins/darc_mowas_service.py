#!/usr/bin/env python3
"""
Implementation of the DARC MoWaS receiver that is called via a
webhook (http post request) and distributes the passed MoWaS alert
to meshcore channels.

Alerts need to be sent to /api/alert, the status of the service
is exposed as /api/health. To mitigate the risk of spam, the
/api/alert endpoint should be operated behind a reverse proxy and
basic auth (supported by DARC MoWaS gateway).

For details regarding the interface, see
https://www.darc.de/index.php?id=58435
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any
from flask import (
    Flask,
    jsonify,
    redirect,
    request,
)
import aiohttp
import xml.dom.minidom
from modules import i18n
from werkzeug.serving import make_server
from modules.service_plugins.base_service import BaseServicePlugin
import random


class DARC_MoWaS_Service(BaseServicePlugin):
    config_section = "DARC_MoWaS_Service"
    description = "Receives the MoWaS alerts from a DARC operated backend"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        self.host = self.bot.config.get(self.config_section, "host", fallback="0.0.0.0")
        self.port = self.bot.config.getint(self.config_section, "port", fallback=8080)
        self.channels = {
            "de": self.bot.config.get(
                self.config_section, "channel_de", fallback="mowas"
            ),
            "en": self.bot.config.get(
                self.config_section, "channel_en", fallback="mowas-en"
            ),
        }
        self.use_hamnet = self.bot.config.getboolean(
            self.config_section, "hamnet", fallback=False
        )

        self.translators = {
            "de": i18n.Translator("de"),
            "en": i18n.Translator("en"),
        }

        self.app = Flask(__name__)
        self._server = None
        self._server_future = None
        self._tasks: set[asyncio.Task] = set()
        self._setup_routes()

    async def start(self) -> None:
        self._running = True
        await self._ensure_channels()
        self.logger.info(
            "MoWaSAlert service starting on %s:%s",
            self.host,
            self.port,
        )
        self._server = make_server(self.host, self.port, self.app)
        self._loop = asyncio.get_event_loop()
        self._server_future = self._loop.run_in_executor(
            None, self._server.serve_forever
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._server:
            self._server.shutdown()
        if self._server_future:
            await self._server_future
        self.logger.info("MoWaSAlert service stopped")

    async def _ensure_channels(self) -> None:
        """Create configured channels on the node if they don't already exist."""
        cm = self.bot.channel_manager
        used = set(cm._channels_cache.keys())
        free = (i for i in range(1, cm.max_channels) if i not in used)
        for name in self.channels.values():
            if cm.get_channel_number(name) is None:
                idx = next(free, None)
                if idx is None:
                    self.logger.error("No free channel slot for '%s'", name)
                    return
                self.logger.info("Creating channel '%s' at index %d", name, idx)
                await cm.add_hashtag_channel(idx, name)

    async def _process_mowas_notification(self, data: dict) -> None:
        self.logger.info("Processing alert '%s'", data["title"])
        urls = (
            data.get("url", {})
            .get("xml", {})
            .get("hamnet" if self.use_hamnet else "internet", [])
        )
        if not urls:
            self.logger.warning("No download URLs in alert '%s'", data["title"])
            return
        async with aiohttp.ClientSession() as session:
            # for load-balancing, probe URLs in random order
            for url in sorted(urls, key=lambda _: random.random()):
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 200:
                            body = await resp.read()
                            self.logger.info(
                                "Downloaded %d bytes from %s", len(body), url
                            )
                            cap_xml = xml.dom.minidom.parseString(await resp.text())
                            self._process_emergency_cap(cap_xml)
                            return
                        self.logger.warning(
                            "HTTP %d from %s, trying next", resp.status, url
                        )
                except Exception as exc:
                    self.logger.warning("Failed to download %s: %s", url, exc)
        self.logger.error("All download URLs failed for alert '%s'", data["title"])

    def _process_emergency_cap(self, cap: xml.dom.Node) -> None:
        """
        Implementation based on TR DE-Alert
        """
        for alert in cap.getElementsByTagName("alert"):
            self._process_alert(alert)

    def _process_alert(self, cap_alert: xml.dom.Node) -> None:
        alert = TRDECapAlert.from_xml(cap_alert)
        self.logger.info("process alert id '%s'", alert.identifier)
        if not alert.info:
            self.logger.warning("Alert '%s' has no info element", alert.identifier)
            return
        for info in alert.info:
            # on NINA test messages the lang is missing
            lang = info.language.lower()[:2] or "de"
            channel = self.channels.get(lang)
            message = self.make_cb_message(alert, info)
            task = asyncio.create_task(
                self.bot.command_manager.send_channel_messages_chunked(
                    channel, self.chunk_message(message), scope=self.scope
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def make_cb_message(self, alert: "TRDECapAlert", info: "TRDECapAlertInfo") -> str:
        """
        Represent an alert info as cell broadcast message.
        If properties are matching the EU-Alert definition, represent as such.
        Otherwise present as generic alert with type info.event.
        """
        lang = info.language.lower()[:2]
        translator = self.translators.get(lang, self.translators["de"])

        status = (alert.status or "").lower()
        scope = (alert.scope or "").lower()
        severity = (info.severity or "").lower()
        urgency = (info.urgency or "").lower()
        certainty = (info.certainty or "").lower()
        # note, that the TR-DE does not have a mapping for level 3
        # other types like eu-test and eu-reserved, ... are not yet mapped in Germany
        match (status, scope, severity, urgency, certainty):
            case ("actual", "public", "extreme", "immediate", "observed"):
                eu_level = "eu-alert-level-1"
            case ("actual", "public", "extreme", "immediate", "likely"):
                eu_level = "eu-alert-level-2"
            case ("actual", "public", "minor", "expected", "likely"):
                eu_level = "eu-alert-level-4"
            # no official level, but used by MoWaS for test messages
            case ("actual", "public", "minor", "immediate", "observed"):
                eu_level = "eu-alert-level-4"
            case _:
                eu_level = None

        if eu_level is not None:
            severity = translator.translate(
                f"services.darcmowas.messagetype.{eu_level}"
            )
        else:
            severity = info.event

        footer = []
        # ignore pure polygon areas
        area_texts = [x.areaDesc for x in info.area if "polygonal" not in x.areaDesc]
        if len(area_texts) == 1:
            headline = f"[{severity} {info.area[0].areaDesc}] {info.headline}"
        else:
            headline = f"[{severity}] {info.headline}"
            footer.append(
                translator.translate(
                    "services.darcmowas.fields.area", areas=", ".join(area_texts)
                )
            )
        footer.append(
            translator.translate(
                "services.darcmowas.fields.sender", sender=alert.sender
            )
        )
        return "\n".join(p for p in [headline, info.description] + footer if p)

    @staticmethod
    def chunk_message(text: str, max_length: int = 130) -> list[str]:
        """
        Chunk the message in max_length pieces, add (x/n) at the end
        of each chunk if multiple chunks are created.
        """
        if len(text) <= max_length:
            return [text]

        def _chunk_words(words: list[str], limit: int) -> list[str]:
            chunks: list[str] = []
            current = ""
            for word in words:
                candidate = f"{current} {word}" if current else word
                if len(candidate) <= limit:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = word
            if current:
                chunks.append(current)
            return chunks

        words = text.split(" ")
        # Estimate chunk count, then re-chunk with suffix space reserved
        n = len(_chunk_words(words, max_length))
        suffix_len = len(f" ({n}/{n})")
        chunks = _chunk_words(words, max_length - suffix_len)
        return [f"{c.strip()} ({i+1}/{len(chunks)})" for i, c in enumerate(chunks)]

    def _setup_routes(self):
        """Setup webhook route"""

        # Log full traceback for 500 errors so service logs show the real cause
        @self.app.errorhandler(500)
        def internal_error(e):
            self.logger.exception("Unhandled exception (500): %s", e)
            return jsonify({"error": "Internal Server Error"}), 500

        @self.app.route("/")
        def main():
            return redirect("/api/health", code=302)

        @self.app.route("/api/alert", methods=["POST"])
        def api_alert():
            """
            MoWaS alert webhook. Caller uses basic auth, which should be handled
            by reverse proxy.
            """
            data = request.get_json(silent=True)
            if not data:
                return jsonify({"error": "Invalid or missing JSON"}), 400
            self.logger.info("MoWaSAlert received")
            asyncio.run_coroutine_threadsafe(
                self._process_mowas_notification(data), self._loop
            )
            return jsonify({"status": "ok"})

        @self.app.route("/api/health")
        def api_health():
            """Health check endpoint"""
            return jsonify(
                {
                    "status": "healthy",
                    "channels": self.channels,
                    "timestamp": time.time(),
                }
            )


def _child_text(node: xml.dom.Node, tag: str) -> str | None:
    """Return the text content of the first descendant element with the given tag name."""
    elements = node.getElementsByTagName(tag)
    if not elements or not elements[0].firstChild:
        return None
    text = elements[0].firstChild.nodeValue
    return text.strip() or None if text else None


@dataclass
class TRDECapAlert:
    """Incomplete representation of a CAP 1.2 alert with TR-DE 1.1 semantics"""

    identifier: str | None
    sender: str | None
    sent: datetime | None
    status: str | None
    msgType: str | None
    scope: str | None
    references: str | None
    info: list["TRDECapAlertInfo"]

    @staticmethod
    def from_xml(alert: xml.dom.Node) -> "TRDECapAlert":
        sent_str = _child_text(alert, "sent")
        sent = datetime.fromisoformat(sent_str) if sent_str else None

        infos = []
        for info_el in alert.getElementsByTagName("info"):
            infos.append(TRDECapAlertInfo.from_xml(info_el))

        return TRDECapAlert(
            identifier=_child_text(alert, "identifier"),
            sender=_child_text(alert, "sender"),
            sent=sent,
            status=_child_text(alert, "status"),
            msgType=_child_text(alert, "msgType"),
            scope=_child_text(alert, "scope"),
            references=_child_text(alert, "references"),
            info=infos,
        )


@dataclass
class TRDECapAlertInfo:
    """Incomplete representation of the CAP 1.2 info field"""

    language: str
    category: str | None
    event: str | None
    urgency: str | None
    severity: str | None
    certainty: str | None
    description: str
    parameter: list[tuple[str, str]]
    headline: str | None
    area: list["TRDECapAlertArea"]

    @staticmethod
    def from_xml(info: xml.dom.Node) -> "TRDECapAlertInfo":
        parameters = []
        for param_el in info.getElementsByTagName("parameter"):
            name = _child_text(param_el, "valueName")
            value = _child_text(param_el, "value")
            if name is not None:
                parameters.append((name, value or ""))

        area = []
        for area_el in info.getElementsByTagName("area"):
            area.append(TRDECapAlertArea.from_xml(area_el) if area_el else None)

        return TRDECapAlertInfo(
            language=_child_text(info, "language") or "",
            category=_child_text(info, "category"),
            event=_child_text(info, "event"),
            urgency=_child_text(info, "urgency"),
            severity=_child_text(info, "severity"),
            certainty=_child_text(info, "certainty"),
            description=_child_text(info, "description") or "",
            parameter=parameters,
            headline=_child_text(info, "headline"),
            area=area,
        )


@dataclass
class TRDECapAlertArea:
    """Incomplete representation of the CAP 1.2 area field"""

    areaDesc: str | None
    geocode: list[tuple[str, str]]

    @staticmethod
    def from_xml(area: xml.dom.Node) -> "TRDECapAlertArea":
        geocodes = []
        for geocode_el in area.getElementsByTagName("geocode"):
            name = _child_text(geocode_el, "valueName")
            value = _child_text(geocode_el, "value")
            if name is not None:
                geocodes.append((name, value or ""))

        return TRDECapAlertArea(
            areaDesc=_child_text(area, "areaDesc"),
            geocode=geocodes,
        )
