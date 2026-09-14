#!/usr/bin/env python3
"""
Internationalization (i18n) module for MeshCore Bot
Provides translation functionality for bot commands and responses
"""

import json
import logging
from importlib import resources
from pathlib import Path
from typing import Any

logger = logging.getLogger("MeshCoreBot")

class Translator:
    """Handles translation loading and lookup for the bot"""

    def __init__(self, language: str = 'en', translation_path: str = 'translations/', local_translation_path: str = 'local/translations/'):
        """
        Initialize translator

        Args:
            language: Language code (e.g., 'en', 'es', 'es-MX', 'es-ES', 'fr', 'de')
                      Supports locale codes like 'es-MX' for Mexican Spanish or 'es-ES' for Spain Spanish
            translation_path: Path to the distributed translation files directory
            local_translation_path: Path to the operator's own translation files, merged
                      over the distributed ones key by key. Defaults to
                      ``<local_dir_path>/translations`` when set from config.
        """
        self.language = language
        self.translation_path = translation_path
        self.local_translation_path = local_translation_path
        self.base_language = self._extract_base_language(language)
        self.translations: dict[str, Any] = {}
        self.fallback_translations: dict[str, Any] = {}
        self._load_translations()

    def _extract_base_language(self, language: str) -> str:
        """
        Extract base language code from locale code

        Args:
            language: Language code (e.g., 'en', 'es', 'es-MX', 'es-ES')

        Returns:
            Base language code (e.g., 'es' from 'es-MX')
        """
        # Handle locale codes like 'es-MX' or 'es_ES'
        if '-' in language:
            return language.split('-')[0]
        elif '_' in language:
            return language.split('_')[0]
        return language

    def _load_translations(self):
        """Load translation files with locale support"""
        # Load default (English) first for final fallback
        self.fallback_translations = self._load_file('en')

        # Load requested language with locale support
        if self.language == 'en':
            self.translations = self.fallback_translations
        else:
            # Load base language first (e.g., es.json)
            base_translations = {}
            if self.base_language != 'en':
                base_translations = self._load_file(self.base_language)

            # Try to load locale-specific file (e.g., es-MX.json)
            locale_translations = {}
            if self.base_language != self.language:
                locale_translations = self._load_file(self.language)

            # Merge: locale-specific overrides base language, base language overrides English
            # First merge base into English
            merged = self._merge_translations(base_translations, self.fallback_translations)
            # Then merge locale-specific into the merged result
            self.translations = self._merge_translations(locale_translations, merged)

    def _merge_translations(self, primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        """
        Merge primary translations with fallback, with primary taking precedence

        Args:
            primary: Primary translation dictionary (may be empty)
            fallback: Fallback translation dictionary

        Returns:
            Merged dictionary with primary values overriding fallback
        """
        if not primary:
            return fallback.copy()

        result = fallback.copy()
        for key, value in primary.items():
            existing = result.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                # Rebuild the sub-dict rather than mutating it in place: it is still
                # shared with `fallback`, which the caller keeps using.
                result[key] = self._merge_translations(value, existing)
            else:
                result[key] = value
        return result

    def _load_file(self, lang: str) -> dict[str, Any]:
        """
        Load a language's catalog: the distributed file, overlaid with the local one

        The local catalog is merged key by key over the distributed one, so an operator
        can translate their own local commands, or override individual strings, without
        editing a shipped file.

        Args:
            lang: Language code

        Returns:
            Dictionary of translations, empty dict if no catalog was found
        """
        file_path = Path(self.translation_path) / f"{lang}.json"
        local_file_path = Path(self.local_translation_path) / f"{lang}.json"
        try:
            # An explicitly configured filesystem catalog always wins.  The
            # package fallback makes the defaults work from an installed wheel
            # (where ``translations/`` is not relative to the current cwd).
            catalog: dict[str, Any] = {}
            found = False
            for path in (file_path, local_file_path):
                if not path.is_file():
                    continue
                logger.info("Loading translation catalog: %s", path)
                with open(path, encoding='utf-8') as f:
                    # Later file wins on overlapping keys.
                    catalog = self._merge_translations(json.load(f), catalog)
                found = True
            if found:
                # A configured catalog wins even when it is empty, so an operator can
                # deliberately blank one out without the bundled defaults reappearing.
                return catalog

            if not self._uses_bundled_defaults():
                return {}
            bundled = resources.files("translations").joinpath(f"{lang}.json")
            if not bundled.is_file():
                return {}
            return json.loads(bundled.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error("Error parsing translation file for %r: %s", lang, e)
            return {}
        except Exception as e:
            logger.error("Error loading translation file for %r: %s", lang, e)
            return {}

    def translate(self, key: str, **kwargs) -> str:
        """
        Translate a key with optional formatting

        Args:
            key: Dot-separated key path (e.g., 'commands.wx.usage')
            **kwargs: Formatting parameters for string.format()

        Returns:
            Translated string, or key if translation not found
        """
        # Navigate through nested dict structure
        keys = key.split('.')
        value = self.translations

        # Try requested language first
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                # Fallback to English
                value = self.fallback_translations
                for k in keys:
                    if isinstance(value, dict) and k in value:
                        value = value[k]
                    else:
                        # Final fallback: return key (makes missing translations visible)
                        return key

        # If we got a string, format it if kwargs provided
        if isinstance(value, str):
            if kwargs:
                try:
                    return value.format(**kwargs)
                except (KeyError, ValueError):
                    # If formatting fails, return unformatted string
                    return value
            return value

        # If value is not a string, return the key
        return key

    def reload(self):
        """Reload translation files (useful for development)"""
        self._load_translations()

    def get_available_languages(self) -> list:
        """
        Get list of available language files

        Returns:
            List of language codes (e.g., ['en', 'es', 'fr'])
        """
        languages = []
        trans_path = Path(self.translation_path)
        if trans_path.exists():
            for file in trans_path.glob('*.json'):
                languages.append(file.stem)
        if self._uses_bundled_defaults():
            try:
                for resource in resources.files("translations").iterdir():
                    if resource.is_file() and resource.name.endswith(".json"):
                        languages.append(resource.name[:-5])
            except (ModuleNotFoundError, OSError):
                pass
        return sorted(set(languages))

    def _uses_bundled_defaults(self) -> bool:
        """Return whether ``translation_path`` names the shipped catalog."""
        normalized = str(self.translation_path).replace("\\", "/").rstrip("/")
        return normalized == "translations"

    def get_value(self, key: str) -> Any:
        """
        Get a raw value from translations (can be string, list, dict, etc.)

        Args:
            key: Dot-separated key path (e.g., 'commands.hacker.sudo_errors')

        Returns:
            The value at the key path, or None if not found
        """
        keys = key.split('.')
        value = self.translations

        # Try requested language first
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                # Fallback to English
                value = self.fallback_translations
                for k in keys:
                    if isinstance(value, dict) and k in value:
                        value = value[k]
                    else:
                        # Not found
                        return None
                break

        return value
