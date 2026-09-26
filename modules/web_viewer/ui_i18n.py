"""Web viewer UI language selection.

The web viewer's templates are written in English.  For supported languages a
client-side dictionary (``static/js/ui-i18n-<lang>.js``) swaps the visible text
after rendering, so the templates themselves stay untouched and upstream
updates merge cleanly.

Language resolution: ``[Web_Viewer] ui_language`` if set, otherwise the bot's
``[Localization] language``; anything without a UI dictionary falls back to
English.
"""

from __future__ import annotations

import configparser

SUPPORTED_UI_LANGUAGES = ("en", "de")


def resolve_ui_language(config: configparser.ConfigParser | None) -> str:
    raw = ""
    if config is not None:
        raw = (config.get("Web_Viewer", "ui_language", fallback="") or "").strip()
        if not raw:
            raw = (config.get("Localization", "language", fallback="") or "").strip()
    base = raw.replace("_", "-").split("-")[0].lower()
    return base if base in SUPPORTED_UI_LANGUAGES else "en"
