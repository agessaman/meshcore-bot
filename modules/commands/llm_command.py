#!/usr/bin/env python3
"""
LLM command for the MeshCore Bot.
Sends a short prompt to a local llama.cpp OpenAI-compatible endpoint.
"""

import re
import asyncio
from typing import Dict, Any
import requests
from .base_command import BaseCommand
from ..models import MeshMessage


class LlmCommand(BaseCommand):
    """Handles llm command for local llama.cpp chat responses."""

    name = "llm"
    keywords = ["llm", "ia", "ai", "chat"]
    description = "Chat with local llama.cpp AI (ask a short question)"
    category = "basic"
    cooldown_seconds = 5

    short_description = "Ask the local llama.cpp model a short question"
    usage = "llm <question>"
    examples = ["llm What is APRS?", "llm summarize LoRa in one sentence"]
    parameters = [
        {"name": "question", "description": "Prompt to send to local llama.cpp"}
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.llm_enabled = self.get_config_value("Llm_Command", "enabled", fallback=False, value_type="bool")
        self.endpoint = self.get_config_value(
            "Llm_Command",
            "endpoint",
            fallback="http://127.0.0.1:8080/v1/chat/completions",
            value_type="str",
        )
        self.model = self.get_config_value("Llm_Command", "model", fallback="", value_type="str")
        self.system_prompt = self.get_config_value(
            "Llm_Command",
            "system_prompt",
            fallback="You are a helpful assistant on a low-bandwidth mesh network. Reply briefly in one short sentence.",
            value_type="str",
        )
        self.timeout_seconds = max(
            1.0,
            min(
                120.0,
                self.get_config_value("Llm_Command", "timeout_seconds", fallback=20.0, value_type="float"),
            ),
        )
        self.max_tokens = max(
            8,
            min(
                512,
                self.get_config_value("Llm_Command", "max_tokens", fallback=80, value_type="int"),
            ),
        )
        self.temperature = max(
            0.0,
            min(
                2.0,
                self.get_config_value("Llm_Command", "temperature", fallback=0.4, value_type="float"),
            ),
        )
        self.top_p = max(
            0.0,
            min(
                1.0,
                self.get_config_value("Llm_Command", "top_p", fallback=0.9, value_type="float"),
            ),
        )
        self.strip_thinking_tags = self.get_config_value(
            "Llm_Command",
            "strip_thinking_tags",
            fallback=True,
            value_type="bool",
        )

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.llm_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        pfx = self._command_prefix
        return f"Usage: {pfx}llm <question> - Ask local llama.cpp for a short reply"

    def _extract_prompt(self, message: MeshMessage) -> str:
        content = message.content.strip()

        if self._command_prefix:
            if content.startswith(self._command_prefix):
                content = content[len(self._command_prefix):].strip()
        elif content.startswith("!"):
            # Backward-compatibility: base_command.matches_keyword also strips a leading
            # "!" when no command_prefix is configured, so we do the same here.
            content = content[1:].strip()

        content = self._strip_mentions(content)
        lowered = content.lower()

        for keyword in sorted(self.keywords, key=len, reverse=True):
            kw = keyword.lower()
            if lowered == kw:
                return ""
            if lowered.startswith(kw) and len(lowered) > len(kw) and lowered[len(kw)] == " ":
                return content[len(keyword):].strip()

        return ""

    def _build_payload(self, prompt: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if self.model:
            payload["model"] = self.model
        return payload

    def _clean_ai_response(self, content: str, max_length: int) -> str:
        cleaned = content or ""
        if self.strip_thinking_tags:
            cleaned = re.sub(
                r"<(?:think|thinking)>.*?</(?:think|thinking)>",
                "",
                cleaned,
                flags=re.IGNORECASE | re.DOTALL,
            )
        cleaned = " ".join(cleaned.split()).strip()

        if not cleaned:
            cleaned = "No response from AI."

        if len(cleaned) > max_length:
            cleaned = cleaned[: max(0, max_length - 3)].rstrip()
            cleaned = (cleaned + "...") if cleaned else "..."

        return cleaned

    async def execute(self, message: MeshMessage) -> bool:
        prompt = self._extract_prompt(message)
        if not prompt:
            pfx = self._command_prefix
            return await self.send_response(message, f"Usage: {pfx}llm <question>")

        try:
            response = await asyncio.to_thread(
                requests.post,
                self.endpoint,
                json=self._build_payload(prompt),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as e:
            self.logger.warning(f"LLM command connection error: {e}")
            return await self.send_response(message, "LLM unavailable: local llama.cpp is unreachable.")

        if response.status_code != 200:
            self.logger.warning(f"LLM command error status: {response.status_code}")
            return await self.send_response(message, "LLM error: llama.cpp returned an invalid response.")

        try:
            data = response.json()
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                content = choices[0].get("message", {}).get("content", "")
            else:
                content = ""
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            self.logger.warning(f"LLM command parse error: {e}")
            return await self.send_response(message, "LLM error: could not parse response.")

        max_length = self.get_max_message_length(message)
        cleaned = self._clean_ai_response(content, max_length)
        return await self.send_response(message, cleaned)
