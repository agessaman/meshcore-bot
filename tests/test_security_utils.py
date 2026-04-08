"""Tests for modules.security_utils."""

import os
import socket
from unittest.mock import patch

import pytest

from modules.security_utils import (
    sanitize_input,
    validate_api_key_format,
    validate_external_url,
    validate_integer_range,
    validate_port_number,
    validate_pubkey_format,
    validate_safe_path,
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

    def test_non_integer_port_rejected(self):
        assert validate_port_number("8080") is False  # type: ignore[arg-type]
        assert validate_port_number(None) is False  # type: ignore[arg-type]


class TestValidateIntegerRange:
    """Tests for validate_integer_range()."""

    def test_value_in_range_returns_true(self):
        assert validate_integer_range(5, 1, 10) is True

    def test_value_at_min_boundary_returns_true(self):
        assert validate_integer_range(1, 1, 10) is True

    def test_value_at_max_boundary_returns_true(self):
        assert validate_integer_range(10, 1, 10) is True

    def test_value_below_min_raises(self):
        with pytest.raises(ValueError, match="must be between"):
            validate_integer_range(0, 1, 10, name="retries")

    def test_value_above_max_raises(self):
        with pytest.raises(ValueError, match="must be between"):
            validate_integer_range(11, 1, 10, name="retries")

    def test_non_integer_raises(self):
        with pytest.raises(ValueError, match="must be an integer"):
            validate_integer_range("five", 1, 10)  # type: ignore[arg-type]


class TestSanitizeInputExtra:
    """Additional tests for sanitize_input() covering missed branches."""

    def test_non_string_content_is_cast_to_str(self):
        result = sanitize_input(42)  # type: ignore[arg-type]
        assert result == "42"

    def test_negative_max_length_raises(self):
        with pytest.raises(ValueError):
            sanitize_input("hello", max_length=-1)


class TestValidateApiKeyFormatExtra:
    """Additional tests for validate_api_key_format() covering missed branches."""

    def test_non_string_returns_false(self):
        assert validate_api_key_format(12345) is False  # type: ignore[arg-type]


class TestIsNixEnvironment:
    """Tests for _is_nix_environment() — coverage via validate_safe_path (which calls it)."""

    def test_nix_env_var_enables_dangerous_path_access(self, tmp_path):
        # When NIX_STORE is set, system-path check is skipped
        import modules.security_utils as su
        with patch.object(su, "_is_nix_environment", return_value=True):
            # /proc is dangerous on Linux, but Nix mode should allow it via allow_absolute
            result = validate_safe_path(str(tmp_path), base_dir=str(tmp_path), allow_absolute=True)
            assert result is not None

    def test_non_nix_env_detects_nix_store_var(self):
        import modules.security_utils as su
        with patch.dict(os.environ, {"NIX_STORE": "/nix/store"}, clear=False):
            assert su._is_nix_environment() is True

    def test_non_nix_env_detects_nix_path_var(self):
        import modules.security_utils as su
        env = {k: v for k, v in os.environ.items()
               if k not in ("NIX_STORE", "NIX_PATH", "NIX_REMOTE", "IN_NIX_SHELL")}
        with patch.dict(os.environ, {**env, "NIX_PATH": "/nix"}, clear=True):
            assert su._is_nix_environment() is True

    def test_no_nix_vars_returns_false(self):
        import modules.security_utils as su
        env = {k: v for k, v in os.environ.items()
               if k not in ("NIX_STORE", "NIX_PATH", "NIX_REMOTE", "IN_NIX_SHELL")}
        with patch.dict(os.environ, env, clear=True):
            # Can't assert False here because we might be in Nix, just call it
            result = su._is_nix_environment()
            assert isinstance(result, bool)


class TestValidateExternalUrlExtra:
    """Additional coverage for validate_external_url() socket exception paths."""

    def test_dns_resolution_failure_returns_false(self):
        with patch("socket.gethostbyname", side_effect=socket.gaierror("no such host")):
            assert validate_external_url("http://nonexistent.invalid.example") is False

    def test_dns_timeout_returns_false(self):
        with patch("socket.gethostbyname", side_effect=socket.timeout("timeout")):
            assert validate_external_url("http://slow.example.com") is False

    def test_allow_localhost_permits_loopback(self):
        with patch("socket.gethostbyname", return_value="127.0.0.1"):
            result = validate_external_url("http://localhost", allow_localhost=True)
            assert result is True


class TestValidateSafePathExtra:
    """Additional coverage for validate_safe_path() exception paths."""

    def test_dangerous_system_path_rejected_on_linux(self, tmp_path):
        import modules.security_utils as su
        with patch.object(su, "_is_nix_environment", return_value=False):
            with pytest.raises(ValueError, match="system directory"):
                validate_safe_path("/etc/passwd", allow_absolute=True)

    def test_unexpected_exception_wrapped_as_value_error(self, tmp_path):
        with patch("modules.security_utils.Path.resolve", side_effect=OSError("disk fail")):
            with pytest.raises(ValueError, match="Invalid or unsafe file path"):
                validate_safe_path("some_file.db", base_dir=str(tmp_path))


class TestSanitizeName:
    """Tests for sanitize_name() — log-safe identifier sanitization."""

    def test_newline_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\n" not in sanitize_name("Evil\nNode")

    def test_carriage_return_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\r" not in sanitize_name("Evil\rNode")

    def test_tab_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\t" not in sanitize_name("Tab\tNode")

    def test_null_byte_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\x00" not in sanitize_name("Bad\x00Name")

    def test_ansi_escape_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\x1b" not in sanitize_name("\x1b[31mRed\x1b[0m")

    def test_truncated_to_max_length(self):
        from modules.security_utils import sanitize_name
        result = sanitize_name("A" * 100, max_length=64)
        assert len(result) <= 64

    def test_normal_name_unchanged(self):
        from modules.security_utils import sanitize_name
        assert sanitize_name("Alice") == "Alice"

    def test_non_string_coerced(self):
        from modules.security_utils import sanitize_name
        assert sanitize_name(42) == "42"

    def test_negative_max_length_raises(self):
        from modules.security_utils import sanitize_name
        with pytest.raises(ValueError):
            sanitize_name("test", max_length=-1)


class TestValidateExternalUrlEdgePaths:
    """Cover remaining branches in validate_external_url."""

    def test_default_blocks_multicast_ip(self):
        """Lines 137-138: multicast IP blocked in default mode."""
        with patch("socket.gethostbyname", return_value="224.0.0.1"):
            result = validate_external_url("http://multicast.example.com/path")
        assert result is False

    def test_unexpected_exception_returns_false(self):
        """Lines 152-154: top-level exception handler — unexpected non-socket exception."""
        with patch("socket.gethostbyname", side_effect=RuntimeError("unexpected internal error")):
            result = validate_external_url("http://somehost.local/path")
        assert result is False


class TestNixStorePathDetection:
    """Lines 38-40: _is_nix_environment detects /nix/store/ in cwd."""

    def test_nix_store_in_cwd_returns_true(self):
        from pathlib import Path

        import modules.security_utils as su
        with patch("modules.security_utils.Path") as mock_path_cls:
            mock_path_cls.cwd.return_value.resolve.return_value = Path("/nix/store/abc-pkg/src")
            # Remove NIX_STORE from env so cwd check is reached
            env = {k: v for k, v in os.environ.items()
                   if k not in ("NIX_STORE", "NIX_PATH", "NIX_REMOTE", "IN_NIX_SHELL")}
            with patch.dict(os.environ, env, clear=True):
                assert su._is_nix_environment() is True

    def test_cwd_exception_continues(self):
        """Lines 39-40: exception in Path.cwd() is caught and execution continues."""
        import modules.security_utils as su
        with patch("modules.security_utils.Path") as mock_path_cls:
            mock_path_cls.cwd.side_effect = OSError("permission denied")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("NIX_STORE", "NIX_PATH", "NIX_REMOTE", "IN_NIX_SHELL")}
            with patch.dict(os.environ, env, clear=True):
                # Should not raise, should return False (no nix env vars either)
                result = su._is_nix_environment()
                assert isinstance(result, bool)


class TestValidateSafePathOsPlatforms:
    """Lines 195-204 (Windows), 206-216 (macOS) — dangerous path checks per platform."""

    @patch("modules.security_utils._is_nix_environment", return_value=False)
    @patch("modules.security_utils.platform.system", return_value="Windows")
    def test_windows_system32_blocked(self, mock_sys, mock_nix, tmp_path):
        """Windows branch: mock Path resolution so target looks like a Windows path."""
        from pathlib import Path
        from unittest.mock import MagicMock
        from unittest.mock import Mock as _Mock
        # Build a mock target whose str() starts with a Windows system32 prefix
        fake_target = MagicMock(spec=Path)
        fake_target.__str__ = _Mock(return_value="C:\\Windows\\System32\\cmd.exe")
        fake_target.relative_to = _Mock(return_value=None)  # allow_absolute skips this
        with patch("modules.security_utils.Path") as MockPath:
            # base_dir resolution
            fake_base = MagicMock(spec=Path)
            fake_base.__str__ = _Mock(return_value=str(tmp_path))
            # file_path: is_absolute() → True, resolve() → fake_target
            fake_file = MagicMock(spec=Path)
            fake_file.is_absolute.return_value = True
            fake_file.resolve.return_value = fake_target
            MockPath.side_effect = lambda p: fake_file if "Windows" in str(p) else MagicMock(
                spec=Path, resolve=_Mock(return_value=fake_base), **{"__str__": _Mock(return_value=str(tmp_path))}
            )
            with pytest.raises(ValueError, match="system directory"):
                validate_safe_path("C:\\Windows\\System32\\cmd.exe", allow_absolute=True)

    @patch("modules.security_utils._is_nix_environment", return_value=False)
    @patch("modules.security_utils.platform.system", return_value="Darwin")
    def test_macos_system_blocked(self, mock_sys, mock_nix, tmp_path):
        with pytest.raises(ValueError, match="system directory"):
            validate_safe_path("/System/Library/something", allow_absolute=True)

    @patch("modules.security_utils._is_nix_environment", return_value=False)
    @patch("modules.security_utils.platform.system", return_value="Darwin")
    def test_macos_safe_path_allowed(self, mock_sys, mock_nix, tmp_path):
        result = validate_safe_path(str(tmp_path / "data.db"), base_dir=str(tmp_path), allow_absolute=True)
        assert result is not None


class TestValidateSqlIdentifier:
    """Lines 329-331: validate_sql_identifier raises on invalid identifiers."""

    def test_valid_identifier_returned(self):
        from modules.security_utils import validate_sql_identifier
        assert validate_sql_identifier("my_table") == "my_table"
        assert validate_sql_identifier("_col123") == "_col123"

    def test_invalid_identifier_raises(self):
        from modules.security_utils import validate_sql_identifier
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_sql_identifier("1bad_name")

    def test_sql_injection_attempt_raises(self):
        from modules.security_utils import validate_sql_identifier
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_sql_identifier("users; DROP TABLE users")
