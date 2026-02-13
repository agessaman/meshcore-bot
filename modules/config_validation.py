#!/usr/bin/env python3
"""
Configuration validation for MeshCore Bot config.ini.

Validates section names against canonical (standardized) names and flags
non-standard sections (e.g. WebViewer instead of Web_Viewer). Can be run
standalone via validate_config.py or at bot startup with --validate-config.
"""

import configparser
from pathlib import Path
from typing import List, Tuple

# Severity levels for validation results
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# Canonical non-command section names (as used in config.ini.example and code)
CANONICAL_NON_COMMAND_SECTIONS = frozenset({
    "Connection",
    "Bot",
    "Channels",
    "Banned_Users",
    "Localization",
    "Admin_ACL",
    "Plugin_Overrides",
    "Companion_Purge",
    "Keywords",
    "Scheduled_Messages",
    "Logging",
    "Custom_Syntax",
    "External_Data",
    "Weather",
    "Solar_Config",
    "Channels_List",
    "Web_Viewer",
    "Feed_Manager",
    "PacketCapture",
    "MapUploader",
    "Weather_Service",
    "DiscordBridge",
})

# Non-standard section name -> suggested canonical name (exact match)
SECTION_TYPO_MAP = {
    "WebViewer": "Web_Viewer",
    "FeedManager": "Feed_Manager",
    "PrefixCommand": "Prefix_Command",
    "Jokes": "Joke_Command / DadJoke_Command (deprecated; move options into those sections)",
}


def validate_config(config_path: str) -> List[Tuple[str, str]]:
    """
    Validate config file section names. Returns a list of (severity, message).

    Args:
        config_path: Path to config.ini (or other config file).

    Returns:
        List of (severity, message). severity is one of SEVERITY_*.
    """
    path = Path(config_path)
    if not path.exists():
        return [(SEVERITY_ERROR, f"Config file not found: {config_path}")]

    config = configparser.ConfigParser()
    try:
        config.read(config_path)
    except configparser.Error as e:
        return [(SEVERITY_ERROR, f"Failed to parse config: {e}")]

    results: List[Tuple[str, str]] = []

    for section in config.sections():
        section_stripped = section.strip()
        if not section_stripped:
            continue

        # Valid: canonical non-command section
        if section_stripped in CANONICAL_NON_COMMAND_SECTIONS:
            continue
        # Valid: command section (ends with _Command)
        if section_stripped.endswith("_Command"):
            continue

        # Check typo map for known non-standard names
        if section_stripped in SECTION_TYPO_MAP:
            suggestion = SECTION_TYPO_MAP[section_stripped]
            results.append((
                SEVERITY_WARNING,
                f"Non-standard section [{section_stripped}]; did you mean [{suggestion}]?",
            ))
        else:
            results.append((
                SEVERITY_INFO,
                f"Unknown section [{section_stripped}] (not in canonical list and not a *_Command section).",
            ))

    return results
