from types import SimpleNamespace
from unittest.mock import patch

from modules.command_manager import CommandManager


class TestRandomLine:
    def test_match_randomline_exact_match_normalizes_spaces_and_case(self, mock_bot, tmp_path):
        f = tmp_path / "momjoke.txt"
        f.write_text("line one\n\nline two\n", encoding="utf-8")

        if not mock_bot.config.has_section("RandomLine"):
            mock_bot.config.add_section("RandomLine")
        mock_bot.config.set("RandomLine", "prefix.default", "")
        mock_bot.config.set("RandomLine", "triggers.momjoke", "momjoke,mom joke")
        mock_bot.config.set("RandomLine", "file.momjoke", str(f))
        mock_bot.config.set("RandomLine", "prefix.momjoke", "🥸")

        manager = CommandManager(mock_bot)
        manager.command_prefix = ""

        msg = SimpleNamespace(
            content="  MOM   JOKE  ",
            is_dm=True,
            sender_id="abc",
            channel="general",
        )

        with patch("modules.command_manager.random.choice", return_value="line two"):
            result = manager.match_randomline(msg)

        assert result is not None
        key, response = result
        assert key == "momjoke"
        assert response == "🥸 line two"

    def test_match_randomline_does_not_match_extra_words(self, mock_bot, tmp_path):
        f = tmp_path / "funfacts.txt"
        f.write_text("fact one\n", encoding="utf-8")

        if not mock_bot.config.has_section("RandomLine"):
            mock_bot.config.add_section("RandomLine")
        mock_bot.config.set("RandomLine", "prefix.default", "")
        mock_bot.config.set("RandomLine", "triggers.funfact", "funfact,fun fact")
        mock_bot.config.set("RandomLine", "file.funfact", str(f))
        mock_bot.config.set("RandomLine", "prefix.funfact", "💡")

        manager = CommandManager(mock_bot)
        manager.command_prefix = ""

        msg = SimpleNamespace(
            content="fun fact please",
            is_dm=True,
            sender_id="abc",
            channel="general",
        )

        assert manager.match_randomline(msg) is None

    def test_match_randomline_channel_filter_allowed(self, mock_bot, tmp_path):
        """When channel.<key> is set, trigger only matches in that channel."""
        f = tmp_path / "momjoke.txt"
        f.write_text("line one\n", encoding="utf-8")

        if not mock_bot.config.has_section("RandomLine"):
            mock_bot.config.add_section("RandomLine")
        mock_bot.config.set("RandomLine", "prefix.default", "")
        mock_bot.config.set("RandomLine", "triggers.momjoke", "momjoke")
        mock_bot.config.set("RandomLine", "file.momjoke", str(f))
        mock_bot.config.set("RandomLine", "prefix.momjoke", "🥸")
        mock_bot.config.set("RandomLine", "channel.momjoke", "#jokes")

        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        manager.monitor_channels = ["general", "jokes"]

        msg = SimpleNamespace(
            content="momjoke",
            is_dm=False,
            sender_id="abc",
            channel="jokes",
        )

        with patch("modules.command_manager.random.choice", return_value="line one"):
            result = manager.match_randomline(msg)

        assert result is not None
        key, response = result
        assert key == "momjoke"

    def test_match_randomline_channel_filter_denied(self, mock_bot, tmp_path):
        """When channel.<key> is set, trigger does not match in other channels."""
        f = tmp_path / "momjoke.txt"
        f.write_text("line one\n", encoding="utf-8")

        if not mock_bot.config.has_section("RandomLine"):
            mock_bot.config.add_section("RandomLine")
        mock_bot.config.set("RandomLine", "prefix.default", "")
        mock_bot.config.set("RandomLine", "triggers.momjoke", "momjoke")
        mock_bot.config.set("RandomLine", "file.momjoke", str(f))
        mock_bot.config.set("RandomLine", "prefix.momjoke", "🥸")
        mock_bot.config.set("RandomLine", "channel.momjoke", "#jokes")

        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        manager.monitor_channels = ["general", "jokes"]

        msg = SimpleNamespace(
            content="momjoke",
            is_dm=False,
            sender_id="abc",
            channel="general",
        )

        assert manager.match_randomline(msg) is None

    def test_match_randomline_channel_override_allows_channel_not_in_monitor(self, mock_bot, tmp_path):
        """When channel.<key> is set, trigger works in that channel even if not in monitor_channels."""
        f = tmp_path / "momjoke.txt"
        f.write_text("line one\n", encoding="utf-8")

        if not mock_bot.config.has_section("RandomLine"):
            mock_bot.config.add_section("RandomLine")
        mock_bot.config.set("RandomLine", "prefix.default", "")
        mock_bot.config.set("RandomLine", "triggers.momjoke", "momjoke,mom joke")
        mock_bot.config.set("RandomLine", "file.momjoke", str(f))
        mock_bot.config.set("RandomLine", "prefix.momjoke", "🥸")
        mock_bot.config.set("RandomLine", "channel.momjoke", "#jokes")

        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        # #jokes is NOT in monitor list (e.g. only #bot, BotTest are monitored)
        manager.monitor_channels = ["BotTest", "#bot"]

        msg = SimpleNamespace(
            content="mom joke",
            is_dm=False,
            sender_id="abc",
            channel="#jokes",
        )

        with patch("modules.command_manager.random.choice", return_value="line one"):
            result = manager.match_randomline(msg)

        assert result is not None
        key, response = result
        assert key == "momjoke"
        assert response == "🥸 line one"


# ---------------------------------------------------------------------------
# Security: RandomLine path validation (path traversal prevention)
# ---------------------------------------------------------------------------


class TestRandomLinePathSecurity:
    """validate_safe_path must block dangerous file paths in [RandomLine] config.

    Covers GAP C1: open() called without path validation in match_randomline.
    Uses validate_safe_path() from security_utils — verified via None return value.
    """

    def _setup_randomline(self, mock_bot, key, trigger, file_path):
        if not mock_bot.config.has_section("RandomLine"):
            mock_bot.config.add_section("RandomLine")
        mock_bot.config.set("RandomLine", "prefix.default", "")
        mock_bot.config.set("RandomLine", f"triggers.{key}", trigger)
        mock_bot.config.set("RandomLine", f"file.{key}", file_path)

    def _make_msg(self, content):
        return SimpleNamespace(
            content=content, is_dm=True, sender_id="x", channel=""
        )

    def test_etc_passwd_path_rejected(self, mock_bot):
        """/etc/passwd must be rejected as a RandomLine source file."""
        self._setup_randomline(mock_bot, "secret", "secret", "/etc/passwd")
        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        result = manager.match_randomline(self._make_msg("secret"))
        assert result is None

    def test_etc_shadow_path_rejected(self, mock_bot):
        """/etc/shadow must be rejected (system credential file)."""
        self._setup_randomline(mock_bot, "shadow", "shadow", "/etc/shadow")
        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        result = manager.match_randomline(self._make_msg("shadow"))
        assert result is None

    def test_proc_self_environ_rejected(self, mock_bot):
        """/proc/self/environ must be rejected (process environment leak)."""
        self._setup_randomline(mock_bot, "env", "env", "/proc/self/environ")
        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        result = manager.match_randomline(self._make_msg("env"))
        assert result is None

    def test_valid_file_outside_restricted_dirs_is_allowed(self, mock_bot, tmp_path):
        """A normal file in a safe directory is still readable."""
        f = tmp_path / "jokes.txt"
        f.write_text("ha ha ha\n", encoding="utf-8")
        self._setup_randomline(mock_bot, "joke", "joke", str(f))
        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        with patch("modules.command_manager.random.choice", return_value="ha ha ha"):
            result = manager.match_randomline(self._make_msg("joke"))
        assert result is not None
        assert result[1] == "ha ha ha"

    def test_validate_safe_path_called_before_open(self, mock_bot, tmp_path):
        """validate_safe_path is invoked — not bypassed — before open()."""
        f = tmp_path / "data.txt"
        f.write_text("line\n", encoding="utf-8")
        self._setup_randomline(mock_bot, "data", "data", str(f))
        manager = CommandManager(mock_bot)
        manager.command_prefix = ""
        with patch("modules.command_manager.validate_safe_path", wraps=lambda p, **kw: None) as mock_vsp:
            result = manager.match_randomline(self._make_msg("data"))
        mock_vsp.assert_called_once()
        assert result is None  # wraps returns None → path rejected
