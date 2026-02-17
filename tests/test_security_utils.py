"""Tests for modules.security_utils."""

import pytest
from pathlib import Path
from unittest.mock import patch

from modules.security_utils import (
    validate_pubkey_format,
    validate_safe_path,
    validate_external_url,
    sanitize_input,
    validate_api_key_format,
    validate_port_number,
)


class TestValidatePubkeyFormat:
    """Tests for validate_pubkey_format()."""

    def test_valid_hex_64_chars(self):
        valid_key = "a" * 64
        assert validate_pubkey_format(valid_key) is True
        assert validate_pubkey_format("0123456789abcdef" * 4) is True

    def test_invalid_length(self):
        assert validate_pubkey_format("a" * 63) is False
        assert validate_pubkey_format("a" * 65) is False
        assert validate_pubkey_format("") is False

    def test_invalid_chars(self):
        assert validate_pubkey_format("g" + "a" * 63) is False
        assert validate_pubkey_format("a" * 63 + "Z") is False  # Actually Z might be valid in hex - no, hex is 0-9a-fA-F. Z is invalid.
        assert validate_pubkey_format("a" * 63 + "-") is False

    def test_not_string(self):
        assert validate_pubkey_format(None) is False
        assert validate_pubkey_format(12345) is False


class TestValidateSafePath:
    """Tests for validate_safe_path()."""

    @patch("modules.security_utils._is_nix_environment", return_value=True)
    def test_relative_path_resolution(self, mock_nix, tmp_path):
        # Patch Nix check so tmp_path (under /private on macOS) doesn't trigger dangerous path
        result = validate_safe_path("subdir/file.db", base_dir=str(tmp_path), allow_absolute=False)
        assert result == (tmp_path / "subdir" / "file.db").resolve()

    def test_path_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            validate_safe_path("../../../etc/passwd", base_dir=str(tmp_path), allow_absolute=False)

    def test_absolute_path_when_not_allowed_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            validate_safe_path("/etc/passwd", base_dir=str(tmp_path), allow_absolute=False)

    @patch("modules.security_utils._is_nix_environment", return_value=True)
    def test_absolute_path_when_allowed(self, mock_nix, tmp_path):
        target = tmp_path / "data" / "file.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = validate_safe_path(str(target), base_dir="/other", allow_absolute=True)
        assert result == target.resolve()


class TestValidateExternalUrl:
    """Tests for validate_external_url()."""

    def test_file_scheme_rejected(self):
        assert validate_external_url("file:///etc/passwd") is False

    def test_http_https_scheme_allowed(self):
        with patch("socket.gethostbyname", return_value="93.184.216.34"):
            assert validate_external_url("https://example.com/") is True
            assert validate_external_url("http://example.com/") is True

    def test_localhost_rejected_by_default(self):
        with patch("socket.gethostbyname", return_value="127.0.0.1"):
            assert validate_external_url("http://localhost/") is False
            assert validate_external_url("http://example.com/") is False

    def test_localhost_allowed_when_requested(self):
        with patch("socket.gethostbyname", return_value="127.0.0.1"):
            assert validate_external_url("http://localhost/", allow_localhost=True) is True

    def test_missing_netloc_rejected(self):
        assert validate_external_url("http://") is False


class TestSanitizeInput:
    """Tests for sanitize_input()."""

    def test_truncates_to_max_length(self):
        assert len(sanitize_input("a" * 1000, max_length=100)) == 100

    def test_strips_control_chars(self):
        result = sanitize_input("hello\x01world\x02", strip_controls=True)
        assert "\x01" not in result
        assert "\x02" not in result

    def test_keeps_newline_tab(self):
        result = sanitize_input("hello\nworld\tthere")
        assert "\n" in result
        assert "\t" in result


class TestValidateApiKeyFormat:
    """Tests for validate_api_key_format()."""

    def test_valid_key(self):
        assert validate_api_key_format("a1b2c3d4e5f6g7h8i9j0") is True

    def test_too_short(self):
        assert validate_api_key_format("short") is False

    def test_placeholder_rejected(self):
        assert validate_api_key_format("your_api_key_here" + "x" * 10) is False


class TestValidatePortNumber:
    """Tests for validate_port_number()."""

    def test_valid_port(self):
        assert validate_port_number(8080) is True
        assert validate_port_number(65535) is True

    def test_privileged_port_rejected_by_default(self):
        assert validate_port_number(80) is False
        assert validate_port_number(443) is False

    def test_privileged_port_allowed_when_requested(self):
        assert validate_port_number(80, allow_privileged=True) is True

    def test_invalid_port(self):
        assert validate_port_number(0) is False
        assert validate_port_number(70000) is False
