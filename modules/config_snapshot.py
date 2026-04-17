"""Helpers for rendering resolved config snapshots with redaction."""

from __future__ import annotations

from configparser import ConfigParser

_REDACT_KEY_PARTS: tuple[str, ...] = (
    "password",
    "smtp_password",
    "api_key",
    "token",
    "secret",
    "smtp_user",
)


def is_sensitive_key(key: str) -> bool:
    """Return True when a config key should be redacted."""
    key_lower = key.lower()
    return any(part in key_lower for part in _REDACT_KEY_PARTS)


def config_to_redacted_sections(config: ConfigParser) -> dict[str, dict[str, str]]:
    """Return config sections as key/value maps with sensitive keys redacted."""
    sections: dict[str, dict[str, str]] = {}
    for section in config.sections():
        sections[section] = {
            key: "●●●●●●" if is_sensitive_key(key) else value
            for key, value in config.items(section, raw=True)
        }
    return sections


def redacted_sections_to_ini_text(sections: dict[str, dict[str, str]]) -> str:
    """Render redacted config sections as human-readable INI text."""
    lines: list[str] = []
    for idx, (section_name, options) in enumerate(sections.items()):
        if idx > 0:
            lines.append("")
        lines.append(f"[{section_name}]")
        for key, value in options.items():
            lines.append(f"{key} = {value}")
    return "\n".join(lines)
