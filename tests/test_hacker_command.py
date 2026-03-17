"""Tests for modules.commands.hacker_command — get_hacker_error and matches_keyword."""

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.hacker_command import HackerCommand
from tests.conftest import mock_message


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def _make_bot(enabled=True):
    bot = MagicMock()
    bot.logger = Mock()

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Hacker_Command")
    config.set("Hacker_Command", "enabled", "true" if enabled else "false")

    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)  # No translations → use fallback
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)

    return bot


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# get_hacker_error — one test per command category
# ---------------------------------------------------------------------------

class TestGetHackerError:
    def setup_method(self):
        self.cmd = HackerCommand(_make_bot())

    def test_sudo_error(self):
        result = self.cmd.get_hacker_error("sudo rm -rf /")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ps_aux_error(self):
        result = self.cmd.get_hacker_error("ps aux")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_grep_error(self):
        result = self.cmd.get_hacker_error("grep -r password /etc")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ls_l_error(self):
        result = self.cmd.get_hacker_error("ls -l /home")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ls_la_error(self):
        result = self.cmd.get_hacker_error("ls -la")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_echo_path_error(self):
        result = self.cmd.get_hacker_error("echo $PATH")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_rm_rf_error(self):
        result = self.cmd.get_hacker_error("rm -rf /")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_rm_r_error(self):
        result = self.cmd.get_hacker_error("rm -r mydir")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_rm_error(self):
        result = self.cmd.get_hacker_error("rm file.txt")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cat_error(self):
        result = self.cmd.get_hacker_error("cat /etc/passwd")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_whoami_error(self):
        result = self.cmd.get_hacker_error("whoami")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_top_error(self):
        result = self.cmd.get_hacker_error("top")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_htop_error(self):
        result = self.cmd.get_hacker_error("htop")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_netstat_error(self):
        result = self.cmd.get_hacker_error("netstat -an")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ss_error(self):
        result = self.cmd.get_hacker_error("ss -tlnp")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_kill_error(self):
        result = self.cmd.get_hacker_error("kill 1234")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_killall_error(self):
        result = self.cmd.get_hacker_error("killall nginx")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_chmod_error(self):
        result = self.cmd.get_hacker_error("chmod 777 /etc")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_find_error(self):
        result = self.cmd.get_hacker_error("find / -name '*.conf'")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_history_error(self):
        result = self.cmd.get_hacker_error("history")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_passwd_error(self):
        result = self.cmd.get_hacker_error("passwd root")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_su_error(self):
        result = self.cmd.get_hacker_error("su root")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ssh_error(self):
        result = self.cmd.get_hacker_error("ssh user@host")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_wget_error(self):
        result = self.cmd.get_hacker_error("wget http://example.com/file")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_curl_error(self):
        result = self.cmd.get_hacker_error("curl http://example.com")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_df_h_error(self):
        result = self.cmd.get_hacker_error("df -h")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_df_error(self):
        result = self.cmd.get_hacker_error("df")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_free_error(self):
        result = self.cmd.get_hacker_error("free -h")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ifconfig_error(self):
        result = self.cmd.get_hacker_error("ifconfig eth0")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ip_addr_error(self):
        result = self.cmd.get_hacker_error("ip addr show")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_uname_a_error(self):
        result = self.cmd.get_hacker_error("uname -a")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generic_error_for_unknown_command(self):
        result = self.cmd.get_hacker_error("make coffee")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_case_insensitive(self):
        result = self.cmd.get_hacker_error("SUDO apt-get install vim")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_with_translations(self):
        """When translator returns a list, random.choice is used."""
        bot = _make_bot()
        bot.translator.get_value = Mock(return_value=["error1", "error2", "error3"])
        cmd = HackerCommand(bot)
        result = cmd.get_hacker_error("sudo foo")
        assert result in ["error1", "error2", "error3"]

    def test_with_empty_translation_falls_back(self):
        """When translator returns empty list, fallback is used."""
        bot = _make_bot()
        bot.translator.get_value = Mock(return_value=[])
        cmd = HackerCommand(bot)
        result = cmd.get_hacker_error("sudo foo")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_with_non_list_translation_falls_back(self):
        """When translator returns non-list, fallback is used."""
        bot = _make_bot()
        bot.translator.get_value = Mock(return_value="not a list")
        cmd = HackerCommand(bot)
        result = cmd.get_hacker_error("sudo foo")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# matches_keyword
# ---------------------------------------------------------------------------

class TestMatchesKeyword:
    def test_disabled_never_matches(self):
        cmd = HackerCommand(_make_bot(enabled=False))
        assert cmd.matches_keyword(mock_message(content="sudo rm -rf /")) is False

    def test_sudo_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="sudo ls")) is True

    def test_sudo_alone_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="sudo")) is True

    def test_ps_aux_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ps aux")) is True

    def test_rm_rf_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="rm -rf /")) is True

    def test_rm_alone_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="rm file.txt")) is True

    def test_ls_l_exact_match(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ls -l")) is True

    def test_ls_la_exact_match(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ls -la")) is True

    def test_whoami_exact(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="whoami")) is True

    def test_history_exact(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="history")) is True

    def test_ssh_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ssh user@host")) is True

    def test_non_hacker_command_no_match(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ping")) is False

    def test_with_exclamation_prefix(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="!sudo rm foo")) is True

    def test_partial_match_no_space_no_match(self):
        """'sudofoo' should not match 'sudo' prefix (no trailing space)."""
        cmd = HackerCommand(_make_bot(enabled=True))
        # "sudofoo" is not "sudo " nor exactly "sudo"
        assert cmd.matches_keyword(mock_message(content="sudofoo")) is False

    def test_ip_addr_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ip addr show")) is True

    def test_ifconfig_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="ifconfig eth0")) is True

    def test_uname_a_exact_matches(self):
        cmd = HackerCommand(_make_bot(enabled=True))
        assert cmd.matches_keyword(mock_message(content="uname -a")) is True


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

class TestExecute:
    def test_execute_disabled_returns_false(self):
        bot = _make_bot(enabled=False)
        cmd = HackerCommand(bot)
        cmd.enabled = False
        msg = mock_message(content="sudo foo")
        result = _run(cmd.execute(msg))
        assert result is False

    def test_execute_enabled_sends_response(self):
        bot = _make_bot(enabled=True)
        cmd = HackerCommand(bot)
        cmd.enabled = True
        msg = mock_message(content="sudo foo")
        result = _run(cmd.execute(msg))
        assert result is True
        bot.command_manager.send_response.assert_called_once()

    def test_execute_with_exclamation_prefix(self):
        bot = _make_bot(enabled=True)
        cmd = HackerCommand(bot)
        cmd.enabled = True
        msg = mock_message(content="!sudo rm -rf /")
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()


# ---------------------------------------------------------------------------
# get_help_text
# ---------------------------------------------------------------------------

class TestGetHelpText:
    def test_returns_description(self):
        cmd = HackerCommand(_make_bot())
        result = cmd.get_help_text()
        assert result == cmd.description
