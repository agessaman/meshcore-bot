"""Tests for modules.i18n — Translator class."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.i18n import Translator


class TestExtractBaseLanguage:
    """Tests for _extract_base_language."""

    def test_simple_code_unchanged(self):
        t = Translator.__new__(Translator)
        assert t._extract_base_language("en") == "en"

    def test_hyphen_locale(self):
        t = Translator.__new__(Translator)
        assert t._extract_base_language("es-MX") == "es"

    def test_underscore_locale(self):
        t = Translator.__new__(Translator)
        assert t._extract_base_language("es_ES") == "es"

    def test_french(self):
        t = Translator.__new__(Translator)
        assert t._extract_base_language("fr") == "fr"


class TestMergeTranslations:
    """Tests for _merge_translations."""

    def setup_method(self):
        self.t = Translator.__new__(Translator)

    def test_empty_primary_returns_copy_of_fallback(self):
        fallback = {"a": "A", "b": "B"}
        result = self.t._merge_translations({}, fallback)
        assert result == fallback
        assert result is not fallback  # should be a copy

    def test_primary_overrides_fallback(self):
        primary = {"a": "OVERRIDE"}
        fallback = {"a": "original", "b": "keep_me"}
        result = self.t._merge_translations(primary, fallback)
        assert result["a"] == "OVERRIDE"
        assert result["b"] == "keep_me"

    def test_nested_dicts_merged_recursively(self):
        primary = {"grp": {"x": "X_override"}}
        fallback = {"grp": {"x": "X_orig", "y": "Y_orig"}}
        result = self.t._merge_translations(primary, fallback)
        assert result["grp"]["x"] == "X_override"
        assert result["grp"]["y"] == "Y_orig"

    def test_flat_override_wins_over_nested_fallback(self):
        primary = {"grp": "flat_string"}
        fallback = {"grp": {"x": "nested"}}
        result = self.t._merge_translations(primary, fallback)
        assert result["grp"] == "flat_string"


class TestTranslatorWithRealFiles:
    """Tests that use actual translation files (if available)."""

    def test_english_fallback_returns_key_when_missing(self, tmp_path):
        en = {"commands": {"ping": {"response": "Pong!"}}}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        assert t.translate("commands.ping.response") == "Pong!"

    def test_missing_key_returns_key(self, tmp_path):
        en = {"hello": "world"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        assert t.translate("commands.nonexistent.key") == "commands.nonexistent.key"

    def test_translate_with_format_kwargs(self, tmp_path):
        en = {"greeting": "Hello, {name}!"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        assert t.translate("greeting", name="World") == "Hello, World!"

    def test_format_failure_returns_unformatted(self, tmp_path):
        en = {"greeting": "Hello, {name}!"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        # Missing kwarg -> formatting fails -> return unformatted
        result = t.translate("greeting")
        # No kwargs: should just return the value without formatting
        assert "Hello" in result or result == "Hello, {name}!"

    def test_fallback_to_english_when_key_missing_in_locale(self, tmp_path):
        en = {"only_in_english": "English value"}
        es = {"other_key": "Spanish value"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        (tmp_path / "es.json").write_text(json.dumps(es))
        t = Translator(language="es", translation_path=str(tmp_path))
        assert t.translate("only_in_english") == "English value"

    def test_locale_overrides_base(self, tmp_path):
        en = {"greeting": "Hello"}
        es = {"greeting": "Hola"}
        es_mx = {"greeting": "Que tal"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        (tmp_path / "es.json").write_text(json.dumps(es))
        (tmp_path / "es-MX.json").write_text(json.dumps(es_mx))
        t = Translator(language="es-MX", translation_path=str(tmp_path))
        assert t.translate("greeting") == "Que tal"

    def test_get_available_languages(self, tmp_path):
        for lang in ["en", "es", "fr"]:
            (tmp_path / f"{lang}.json").write_text("{}")
        t = Translator(language="en", translation_path=str(tmp_path))
        langs = t.get_available_languages()
        assert sorted(langs) == ["en", "es", "fr"]

    def test_get_available_languages_missing_dir(self, tmp_path):
        t = Translator(language="en", translation_path=str(tmp_path / "nonexistent"))
        assert t.get_available_languages() == []

    def test_get_value_returns_raw_value(self, tmp_path):
        en = {"commands": {"list": ["a", "b", "c"]}}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        val = t.get_value("commands.list")
        assert val == ["a", "b", "c"]

    def test_get_value_missing_key_returns_none(self, tmp_path):
        en = {"key": "value"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        assert t.get_value("no.such.key") is None

    def test_reload_picks_up_new_content(self, tmp_path):
        en_file = tmp_path / "en.json"
        en_file.write_text(json.dumps({"msg": "original"}))
        t = Translator(language="en", translation_path=str(tmp_path))
        assert t.translate("msg") == "original"
        en_file.write_text(json.dumps({"msg": "updated"}))
        t.reload()
        assert t.translate("msg") == "updated"

    def test_invalid_json_returns_empty(self, tmp_path):
        en_file = tmp_path / "en.json"
        en_file.write_text("{invalid json")
        t = Translator(language="en", translation_path=str(tmp_path))
        # Should not crash; translations will be empty
        result = t.translate("any.key")
        assert result == "any.key"

    def test_translate_non_string_value_returns_key(self, tmp_path):
        en = {"items": ["a", "b"]}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        # List value should return key, not the list
        assert t.translate("items") == "items"

    def test_load_file_non_json_exception(self, tmp_path):
        """A non-JSONDecodeError exception in _load_file returns empty dict."""
        en_file = tmp_path / "en.json"
        en_file.write_text('{"key": "value"}')
        t = Translator(language="en", translation_path=str(tmp_path))
        # Now simulate generic exception by patching open
        from unittest.mock import patch, mock_open
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = t._load_file("en")
        assert result == {}

    def test_translate_fallback_inner_loop_executes(self, tmp_path):
        """When outer loop misses, inner fallback loop finds nested key (line 148)."""
        # English has nested key; target language has different top-level key
        en = {"grp": {"deep": "found"}}
        xx = {"other": "x"}  # no 'grp' key
        (tmp_path / "en.json").write_text(json.dumps(en))
        (tmp_path / "xx.json").write_text(json.dumps(xx))
        t = Translator(language="xx", translation_path=str(tmp_path))
        # 'grp.deep' will miss in xx translations (outer loop fails on 'grp'),
        # then inner fallback loop finds 'grp' in English (line 148), then 'deep'
        result = t.translate("grp.deep")
        assert result == "found"

    def test_translate_format_failure_returns_unformatted(self, tmp_path):
        """When .format(**kwargs) raises, return the unformatted value (lines 158-160)."""
        en = {"greeting": "Hello, {name}!"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        t = Translator(language="en", translation_path=str(tmp_path))
        # Pass a wrong kwarg to trigger KeyError in format
        result = t.translate("greeting", wrong_key="x")
        # Should return unformatted string, not crash
        assert result == "Hello, {name}!"

    def test_get_value_fallback_break(self, tmp_path):
        """get_value fallback inner loop completes and hits break (line 210)."""
        en = {"grp": {"val": "found"}}
        xx = {"other": "x"}
        (tmp_path / "en.json").write_text(json.dumps(en))
        (tmp_path / "xx.json").write_text(json.dumps(xx))
        t = Translator(language="xx", translation_path=str(tmp_path))
        result = t.get_value("grp.val")
        assert result == "found"
