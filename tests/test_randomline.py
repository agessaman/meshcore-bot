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
