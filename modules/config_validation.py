#!/usr/bin/env python3
"""
Configuration validation for MeshCore Bot config.ini.

Validates section names against canonical (standardized) names and flags
non-standard sections (e.g. WebViewer instead of Web_Viewer). Can be run
standalone via validate_config.py or at bot startup with --validate-config.
"""

import configparser
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

# Sections required for the bot to start (accessed without has_section guards)
# Missing any of these causes ConfigParser.NoSectionError during startup
REQUIRED_SECTIONS = frozenset({
    "Connection",   # Serial/BLE/TCP connection params
    "Bot",          # db_path, rate limits, bot_name, etc.
    "Channels",     # monitor_channels, respond_to_dms
})

# Optional sections: when absent, use defaults or treat as empty/disabled
ADMIN_ACL_SECTION = "Admin_ACL"        # admin commands disabled
BANNED_USERS_SECTION = "Banned_Users"   # empty banned list
LOCALIZATION_SECTION = "Localization"   # language=en, translation_path=translations/

def strip_optional_quotes(s: str) -> str:
    """Strip one layer of surrounding double or single quotes if present.

    Allows config values like monitor_channels to be written as
    "#bot,#bot-everett,#bots" so the list does not look like comments.
    Backward compatible: unquoted values are returned unchanged.
    """
    if not isinstance(s, str):
        return s
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        return s[1:-1]
    return s


# Non-standard section name -> suggested canonical name (exact match)
SECTION_TYPO_MAP = {
    "WebViewer": "Web_Viewer",
    "FeedManager": "Feed_Manager",
    "PrefixCommand": "Prefix_Command",
    "Jokes": "Joke_Command / DadJoke_Command (deprecated; move options into those sections)",
}


def _get_command_prefix_to_section() -> Dict[str, str]:
    """Build map from command prefix (lowercase) to canonical section for similarity suggestions.

    Discovers command sections from config.ini.example in the project. Returns a dict
    mapping prefix.lower() -> "Prefix_Command" (e.g. "stats" -> "Stats_Command").
    """
    result: Dict[str, str] = {}
    base = Path(__file__).resolve().parent.parent  # project root
    example_paths = [base / "config.ini.example", base / "config.ini.minimal-example"]
    for path in example_paths:
        if not path.exists():
            continue
        try:
            parser = configparser.ConfigParser()
            parser.read(path)
            for section in parser.sections():
                if section.endswith("_Command"):
                    prefix = section[:-8]  # remove "_Command"
                    result[prefix.lower()] = section
        except configparser.Error:
            pass
    return result


def _suggest_similar_command(section: str, prefix_to_section: Dict[str, str]) -> Optional[str]:
    """If section looks like a command name (e.g. Stats, Hacker), suggest the canonical section."""
    return prefix_to_section.get(section.strip().lower())


def _resolve_path(file_path: str, base_dir: Path) -> Path:
    """Resolve a path relative to base_dir (or as absolute)."""
    p = Path(file_path)
    if p.is_absolute():
        return p.resolve()
    return (base_dir.resolve() / p).resolve()


def _check_path_writable(
    file_path: str, base_dir: Path, description: str
) -> Optional[str]:
    """Check if a file path can be written. Returns warning message if not."""
    if not file_path or not file_path.strip():
        return None
    try:
        resolved = _resolve_path(file_path.strip(), base_dir)
    except (OSError, RuntimeError):
        return f"{description}: cannot resolve path '{file_path}'"
    parent = resolved.parent
    # Find first existing ancestor to check writability
    check_dir = parent
    while not check_dir.exists():
        check_dir = check_dir.parent
        if check_dir == check_dir.parent:  # reached root
            return f"{description} '{resolved}': parent directory does not exist"
    if not os.access(str(check_dir), os.W_OK):
        return f"{description} '{resolved}': directory {check_dir} is not writable"
    # If file exists, verify it's writable
    if resolved.exists() and not os.access(str(resolved), os.W_OK):
        return f"{description} '{resolved}': file exists but is not writable"
    return None


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

    # Check required sections (bot fails to start without these)
    sections_present = frozenset(s.strip() for s in config.sections() if s.strip())
    missing_required = REQUIRED_SECTIONS - sections_present
    for section in sorted(missing_required):
        results.append((
            SEVERITY_ERROR,
            f"Missing required section [{section}]; bot will not start without it.",
        ))

    # Note when optional sections are absent
    if ADMIN_ACL_SECTION not in sections_present:
        results.append((
            SEVERITY_INFO,
            f"Section [{ADMIN_ACL_SECTION}] absent; admin commands (repeater, webviewer, reload) disabled.",
        ))
    if BANNED_USERS_SECTION not in sections_present:
        results.append((
            SEVERITY_INFO,
            f"Section [{BANNED_USERS_SECTION}] absent; no users banned.",
        ))
    if LOCALIZATION_SECTION not in sections_present:
        results.append((
            SEVERITY_INFO,
            f"Section [{LOCALIZATION_SECTION}] absent; using defaults (language=en, translation_path=translations/).",
        ))

    # Check writable paths (database, log file)
    bot_root = Path(config_path).resolve().parent
    if config.has_section("Bot"):
        db_path = config.get("Bot", "db_path", fallback="").strip()
        if db_path:
            msg = _check_path_writable(db_path, bot_root, "Database path")
            if msg:
                results.append((SEVERITY_WARNING, msg))
    if config.has_section("Logging"):
        log_file = config.get("Logging", "log_file", fallback="").strip()
        if log_file:
            msg = _check_path_writable(log_file, bot_root, "Log file path")
            if msg:
                results.append((SEVERITY_WARNING, msg))
    if config.has_section("Web_Viewer"):
        web_db = config.get("Web_Viewer", "db_path", fallback="").strip()
        if web_db:
            msg = _check_path_writable(web_db, bot_root, "Web viewer db_path")
            if msg:
                results.append((SEVERITY_WARNING, msg))

    prefix_to_section: Optional[Dict[str, str]] = None

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
            # Special case: [Jokes] + [Joke_Command]/[DadJoke_Command] overlap
            if section_stripped == "Jokes" and (
                "Joke_Command" in sections_present or "DadJoke_Command" in sections_present
            ):
                results.append((
                    SEVERITY_WARNING,
                    "Both [Jokes] and [Joke_Command]/[DadJoke_Command] are present; "
                    "the *_Command sections take precedence. Consider removing [Jokes] to avoid confusion.",
                ))
            else:
                results.append((
                    SEVERITY_WARNING,
                    f"Non-standard section [{section_stripped}]; did you mean [{suggestion}]?",
                ))
        else:
            # Check if section looks like a command name (e.g. [Stats] -> [Stats_Command])
            if prefix_to_section is None:
                prefix_to_section = _get_command_prefix_to_section()
            similar = _suggest_similar_command(section_stripped, prefix_to_section)
            if similar:
                msg = f"Unknown section [{section_stripped}]; did you mean [{similar}]?"
            else:
                msg = f"Unknown section [{section_stripped}] (not in canonical list and not a *_Command section)."
            results.append((SEVERITY_INFO, msg))

    return results
