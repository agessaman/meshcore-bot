"""Regression tests for keyword escape sequences."""

import pytest

from modules.utils import decode_escape_sequences


class TestKeywordEscapes:
    """Regression: decode_escape_sequences handles config escape sequences."""

    def test_newline_in_config(self):
        """Config strings with \\n should produce actual newlines."""
        result = decode_escape_sequences(r"Line 1\nLine 2")
        assert result == "Line 1\nLine 2"

    def test_literal_backslash_n_preserved(self):
        """\\\\n in config should produce literal \\n."""
        result = decode_escape_sequences(r"Literal \\n here")
        assert result == "Literal \\n here"

    def test_tab_escape(self):
        result = decode_escape_sequences(r"Col1\tCol2")
        assert result == "Col1\tCol2"
