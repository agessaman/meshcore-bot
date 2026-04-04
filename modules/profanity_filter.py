#!/usr/bin/env python3
"""
Shared profanity filter for bridge services (Discord, Telegram).

Uses better-profanity when available; gracefully falls back to no-op if not installed.
Uses unidecode when available to normalize Unicode (e.g. homoglyphs) to ASCII so
better-profanity can detect them.
Also checks for hate symbols (e.g. swastika Unicode) that word lists do not catch.
"""

from typing import Any, Optional

# Unicode code points for symbols we treat as profanity (e.g. swastika forms).
# These are checked in addition to better-profanity's word list.
_HATE_SYMBOL_CODEPOINTS = frozenset({
    0x5350,  # 卐 CJK swastika
    0x534D,  # 卍 CJK swastika (reversed)
})

_profanity_available = False
_profanity_initialized = False
_warned_unavailable = False
_unidecode_available = False

profanity: Any = None
try:
    from better_profanity import profanity as _better_profanity

    profanity = _better_profanity
    _profanity_available = True
except ImportError:
    pass

unidecode: Any = None
try:
    from unidecode import unidecode as _unidecode_impl

    unidecode = _unidecode_impl
    _unidecode_available = True
except ImportError:
    pass
    unidecode = None


def _has_hate_symbols(text: str) -> bool:
    """Return True if text contains any blocked hate-symbol code point."""
    return any(chr(cp) in text for cp in _HATE_SYMBOL_CODEPOINTS)


def _replace_hate_symbols(text: str, replacement: str = "***") -> str:
    """Replace any hate-symbol code point in text with replacement."""
    result = text
    for cp in _HATE_SYMBOL_CODEPOINTS:
        result = result.replace(chr(cp), replacement)
    return result


def _normalize_for_profanity(text: str) -> str:
    """Convert Unicode to ASCII when unidecode is available (catches homoglyph slurs)."""
    if _unidecode_available and unidecode is not None:
        return unidecode(text)
    return text


def _ensure_initialized(logger: Optional[object] = None) -> bool:
    """Load censor wordlist on first use. Returns True if filtering is available."""
    global _profanity_initialized, _warned_unavailable
    if not _profanity_available:
        if not _warned_unavailable:
            _warned_unavailable = True
            if logger is not None and hasattr(logger, "warning"):
                logger.warning(
                    "better-profanity not installed; profanity filter disabled. "
                    "Install with: pip install better-profanity"
                )
        return False
    if not _profanity_initialized:
        _profanity_initialized = True
        profanity.load_censor_words()
    return True


def censor(text: Optional[str], logger: Optional[object] = None) -> str:
    """
    Replace profanity in text with ****. Returns original text if library unavailable.
    Hate symbols (e.g. swastika Unicode) are replaced with ***.

    Args:
        text: Input string (message or username).
        logger: Optional logger for one-time warning when better_profanity is not installed.

    Returns:
        Censored string, or original if filtering unavailable / text is None or not str.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        return str(text)
    if not text.strip():
        return text
    # Replace hate symbols first (no dependency on better-profanity)
    text = _replace_hate_symbols(text)
    if not _ensure_initialized(logger):
        return text
    normalized = _normalize_for_profanity(text)
    return profanity.censor(normalized)


def contains_profanity(text: Optional[str], logger: Optional[object] = None) -> bool:
    """
    Return True if text contains any word from the profanity wordlist or a blocked hate symbol.

    Args:
        text: Input string to check.
        logger: Optional logger for one-time warning when better_profanity is not installed.

    Returns:
        True if profanity or hate symbol detected, False otherwise or if library unavailable.
    """
    if text is None or not isinstance(text, str) or not text.strip():
        return False
    if _has_hate_symbols(text):
        return True
    if not _ensure_initialized(logger):
        return False
    normalized = _normalize_for_profanity(text)
    return profanity.contains_profanity(normalized)
