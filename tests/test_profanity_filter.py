"""Tests for modules.profanity_filter (Discord/Telegram bridge profanity filtering)."""

from unittest.mock import Mock, patch

import pytest

from modules.profanity_filter import censor, contains_profanity


class TestProfanityFilterEdgeCases:
    """Edge cases that do not depend on better_profanity being installed."""

    def test_censor_none_returns_empty_string(self):
        assert censor(None) == ""

    def test_censor_empty_string_returns_empty(self):
        assert censor("") == ""

    def test_censor_whitespace_only_returns_unchanged(self):
        text = "   \n\t  "
        assert censor(text) == text

    def test_censor_non_string_returns_string_version(self):
        assert censor(123) == "123"
        assert censor(True) == "True"

    def test_contains_profanity_none_returns_false(self):
        assert contains_profanity(None) is False

    def test_contains_profanity_empty_returns_false(self):
        assert contains_profanity("") is False

    def test_contains_profanity_whitespace_only_returns_false(self):
        assert contains_profanity("   \n\t  ") is False

    def test_contains_profanity_non_string_returns_false(self):
        assert contains_profanity(123) is False

    def test_hate_symbol_swastika_detected(self):
        """CJK swastika Unicode in text is detected as profanity (no better_profanity needed)."""
        assert contains_profanity("\u5350") is True   # 卐
        assert contains_profanity("\u534d") is True   # 卍
        assert contains_profanity("User\u5350name") is True
        assert contains_profanity("Hello \u534d world") is True

    def test_hate_symbol_swastika_censored(self):
        """CJK swastika Unicode is replaced with *** (no better_profanity needed)."""
        assert censor("\u5350") == "***"
        assert censor("\u534d") == "***"
        assert censor("User\u5350name") == "User***name"
        assert "***" in censor("Hello \u534d world")
        assert "\u5350" not in censor("User\u5350name")
        assert "\u534d" not in censor("Hello \u534d world")


class TestProfanityFilterWithLibrary:
    """Tests that require better_profanity to be installed (skip if not)."""

    @pytest.fixture(autouse=True)
    def _reset_module_state(self):
        """Ensure the profanity module is initialized for these tests."""
        import modules.profanity_filter as pf
        if pf._profanity_available and not pf._profanity_initialized:
            pf.profanity.load_censor_words()
            pf._profanity_initialized = True
        yield

    def test_censor_replaces_profanity_when_library_available(self):
        import modules.profanity_filter as pf
        if not pf._profanity_available:
            pytest.skip("better_profanity not installed")
        result = censor("You piece of shit.")
        assert "****" in result
        assert "shit" not in result

    def test_contains_profanity_true_when_present(self):
        import modules.profanity_filter as pf
        if not pf._profanity_available:
            pytest.skip("better_profanity not installed")
        assert contains_profanity("You piece of shit.") is True

    def test_contains_profanity_false_when_clean(self):
        import modules.profanity_filter as pf
        if not pf._profanity_available:
            pytest.skip("better_profanity not installed")
        assert contains_profanity("Hello world, this is fine.") is False

    def test_censor_clean_text_unchanged(self):
        import modules.profanity_filter as pf
        if not pf._profanity_available:
            pytest.skip("better_profanity not installed")
        text = "Hello world, nothing bad here."
        assert censor(text) == text

    def test_unicode_homoglyph_slur_does_not_crash(self):
        """Filter runs on Unicode homoglyph variant (math double-struck); may or may not be detected."""
        # Unicode string: mathematical double-struck letters (homoglyph of ASCII slur)
        unicode_slur = "ℕ𝕚𝕘𝕘𝕖𝕣"
        result_censor = censor(unicode_slur)
        result_contains = contains_profanity(unicode_slur)
        assert isinstance(result_censor, str)
        assert isinstance(result_contains, bool)
        # When library is available, it may or may not match this variant; either way we exercised the path
        if result_contains:
            assert "****" in result_censor

    def test_unicode_homoglyph_slur_detected_with_unidecode(self):
        """With unidecode, homoglyph (math double-struck) is normalized to ASCII and caught."""
        import modules.profanity_filter as pf
        if not pf._profanity_available:
            pytest.skip("better_profanity not installed")
        if not pf._unidecode_available:
            pytest.skip("unidecode not installed")
        unicode_slur = "ℕ𝕚𝕘𝕘𝕖𝕣"
        assert contains_profanity(unicode_slur) is True
        censored = censor(unicode_slur)
        assert "****" in censored
        assert unicode_slur not in censored


class TestProfanityFilterFallbackWhenLibraryUnavailable:
    """When better_profanity is not available, censor passes through and contains_profanity returns False."""

    def test_censor_returns_unchanged_when_library_unavailable(self):
        import modules.profanity_filter as pf
        with patch.object(pf, "_profanity_available", False):
            text = "some bad word here"
            assert censor(text) == text

    def test_contains_profanity_returns_false_when_library_unavailable(self):
        import modules.profanity_filter as pf
        with patch.object(pf, "_profanity_available", False):
            assert contains_profanity("some bad word here") is False

    def test_censor_logs_warning_once_when_library_unavailable(self):
        import modules.profanity_filter as pf
        logger = Mock()
        with patch.object(pf, "_profanity_available", False), patch.object(pf, "_warned_unavailable", False):
            censor("hello", logger=logger)
            logger.warning.assert_called_once()
            assert "better-profanity" in logger.warning.call_args[0][0]

    def test_hate_symbol_still_detected_and_censored_when_library_unavailable(self):
        """Hate symbols (e.g. swastika) are detected and replaced even when better_profanity is not installed."""
        import modules.profanity_filter as pf
        with patch.object(pf, "_profanity_available", False):
            assert contains_profanity("\u5350") is True
            assert censor("\u5350") == "***"
