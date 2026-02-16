"""Tests for modules.config_validation."""

import pytest
from pathlib import Path

from modules.config_validation import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    strip_optional_quotes,
    validate_config,
    _resolve_path,
    _check_path_writable,
    _suggest_similar_command,
    _get_command_prefix_to_section,
)


class TestStripOptionalQuotes:
    """Tests for strip_optional_quotes (monitor_channels and similar config values)."""

    def test_unquoted_unchanged(self):
        assert strip_optional_quotes("#bot,#bot-everett,#bots") == "#bot,#bot-everett,#bots"
        assert strip_optional_quotes("general,test") == "general,test"

    def test_double_quoted_stripped(self):
        assert strip_optional_quotes('"#bot,#bot-everett,#bots"') == "#bot,#bot-everett,#bots"

    def test_single_quoted_stripped(self):
        assert strip_optional_quotes("'#bot,#bot-everett,#bots'") == "#bot,#bot-everett,#bots"

    def test_empty_and_whitespace(self):
        assert strip_optional_quotes("") == ""
        assert strip_optional_quotes("  ") == ""

    def test_mismatched_quotes_not_stripped(self):
        assert strip_optional_quotes('"#bot,#bots\'') == '"#bot,#bots\''
        assert strip_optional_quotes('\'#bot,#bots"') == '\'#bot,#bots"'

    def test_single_char_quoted_stripped(self):
        assert strip_optional_quotes('"a"') == "a"


class TestValidateConfig:
    """Tests for validate_config()."""

    def test_config_file_not_found(self):
        results = validate_config("/nonexistent/path/config.ini")
        assert len(results) == 1
        assert results[0][0] == SEVERITY_ERROR
        assert "not found" in results[0][1]

    def test_missing_required_sections(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("[Bot]\nbot_name = Test\n")
        results = validate_config(str(config))
        errors = [r for r in results if r[0] == SEVERITY_ERROR]
        assert any("Connection" in r[1] for r in errors)
        assert any("Channels" in r[1] for r in errors)

    def test_valid_minimal_config(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Keywords]
test = ack
ping = Pong
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        errors = [r for r in results if r[0] == SEVERITY_ERROR]
        assert len(errors) == 0

    def test_optional_sections_absent_info(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Keywords]
test = ack
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        infos = [r for r in results if r[0] == SEVERITY_INFO]
        assert any("Admin_ACL" in r[1] for r in infos)
        assert any("Banned_Users" in r[1] for r in infos)
        assert any("Localization" in r[1] for r in infos)

    def test_non_standard_section_typo(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[WebViewer]
debug = false
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("WebViewer" in r[1] and "Web_Viewer" in r[1] for r in warnings)

    def test_unknown_section_similar_command(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Stats]
enabled = true
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        infos = [r for r in results if r[0] == SEVERITY_INFO]
        assert any("Stats" in r[1] and "Stats_Command" in r[1] for r in infos)

    def test_jokes_overlap_suggests_removal(self, tmp_path):
        """When both [Jokes] and [Joke_Command]/[DadJoke_Command] exist, suggest removing [Jokes]."""
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Jokes]
joke_enabled = true

[Joke_Command]
enabled = true

[Keywords]
test = ack
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any(
            "Both [Jokes]" in r[1] and "Consider removing [Jokes]" in r[1]
            for r in warnings
        )


class TestPathValidation:
    """Tests for path writability validation."""

    def test_db_path_nonexistent_parent_warns(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = /nonexistent/path/12345/meshcore_bot.db

[Channels]
monitor_channels = general
respond_to_dms = true

[Keywords]
test = ack
""")
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("Database path" in r[1] for r in warnings)
        assert any("parent directory does not exist" in r[1] for r in warnings)

    def test_log_path_nonexistent_parent_warns(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Logging]
log_file = /nonexistent/logs/12345/meshcore_bot.log

[Keywords]
test = ack
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("Log file path" in r[1] for r in warnings)

    def test_writable_paths_pass(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Logging]
log_file = {log_file}

[Keywords]
test = ack
""".format(
            db_path=str(tmp_path / "meshcore_bot.db"),
            log_file=str(log_dir / "meshcore_bot.log"),
        ))
        results = validate_config(str(config))
        path_warnings = [r for r in results if r[0] == SEVERITY_WARNING
                        and ("Database path" in r[1] or "Log file path" in r[1])]
        assert len(path_warnings) == 0

    def test_relative_db_path_resolved_from_config_dir(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = meshcore_bot.db

[Channels]
monitor_channels = general
respond_to_dms = true

[Keywords]
test = ack
""")
        results = validate_config(str(config))
        path_warnings = [r for r in results if r[0] == SEVERITY_WARNING
                        and "Database path" in r[1]]
        assert len(path_warnings) == 0

    def test_web_viewer_db_path_validation(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Web_Viewer]
db_path = /nonexistent/webviewer/12345/bot_data.db

[Keywords]
test = ack
""".format(db_path=str(tmp_path / "meshcore_bot.db")))
        results = validate_config(str(config))
        warnings = [r for r in results if r[0] == SEVERITY_WARNING]
        assert any("Web viewer db_path" in r[1] for r in warnings)

    def test_directory_not_writable_warns(self, tmp_path):
        read_only_dir = tmp_path / "readonly"
        read_only_dir.mkdir()
        read_only_dir.chmod(0o444)
        try:
            config = tmp_path / "config.ini"
            config.write_text("""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Keywords]
test = ack
""".format(db_path=str(read_only_dir / "meshcore_bot.db")))
            results = validate_config(str(config))
            warnings = [r for r in results if r[0] == SEVERITY_WARNING]
            assert any("Database path" in r[1] for r in warnings)
            assert any("not writable" in r[1] for r in warnings)
        finally:
            read_only_dir.chmod(0o755)


class TestResolvePath:
    """Tests for _resolve_path()."""

    def test_absolute_path_returns_resolved(self):
        result = _resolve_path("/foo/bar/baz", Path("/other"))
        assert result == Path("/foo/bar/baz").resolve()

    def test_relative_path_resolved_from_base(self):
        base = Path("/base/dir")
        result = _resolve_path("subdir/file.db", base)
        assert result == (base / "subdir" / "file.db").resolve()


class TestCheckPathWritable:
    """Tests for _check_path_writable()."""

    def test_empty_path_returns_none(self):
        assert _check_path_writable("", Path("/tmp"), "Test") is None
        assert _check_path_writable("   ", Path("/tmp"), "Test") is None

    def test_nonexistent_parent_returns_warning(self):
        msg = _check_path_writable(
            "/nonexistent/path/xyz/file.log",
            Path("/tmp"),
            "Test path",
        )
        assert msg is not None
        assert "parent directory does not exist" in msg

    def test_writable_dir_returns_none(self, tmp_path):
        target = tmp_path / "subdir" / "file.log"
        assert _check_path_writable(
            str(target),
            tmp_path,
            "Test path",
        ) is None


class TestSuggestSimilarCommand:
    """Tests for _suggest_similar_command()."""

    def test_exact_match(self):
        prefix_map = {"stats": "Stats_Command", "hacker": "Hacker_Command"}
        assert _suggest_similar_command("stats", prefix_map) == "Stats_Command"
        assert _suggest_similar_command("Stats", prefix_map) == "Stats_Command"

    def test_no_match(self):
        prefix_map = {"stats": "Stats_Command"}
        assert _suggest_similar_command("unknown", prefix_map) is None


class TestGetCommandPrefixToSection:
    """Tests for _get_command_prefix_to_section()."""

    def test_returns_dict(self):
        result = _get_command_prefix_to_section()
        assert isinstance(result, dict)

    def test_contains_known_commands(self):
        result = _get_command_prefix_to_section()
        assert "stats" in result or "ping" in result
        for k, v in result.items():
            assert v.endswith("_Command")
