"""Tests for local command discovery and disabled-command filtering in generate_website.py"""

import configparser
import logging
import os
from types import SimpleNamespace

import pytest

from generate_website import (
    filter_commands,
    is_command_enabled,
    read_config,
    resolve_local_commands_dir,
)


def _config(text: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read_string(text)
    return config


def _command(name: str, **attrs):
    defaults = {
        'name': name,
        'keywords': [name],
        'category': 'general',
        'requires_admin_access': lambda: False,
    }
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# is_command_enabled
# ---------------------------------------------------------------------------

def test_no_config_section_means_enabled():
    assert is_command_enabled(_command('ping'), _config('')) is True


def test_section_without_enabled_key_means_enabled():
    config = _config('[Ping_Command]\ncooldown_seconds = 5\n')

    assert is_command_enabled(_command('ping'), config) is True


@pytest.mark.parametrize('raw', ['false', 'no', 'off', '0'])
def test_canonical_enabled_key_disables(raw):
    config = _config(f'[Ping_Command]\nenabled = {raw}\n')

    assert is_command_enabled(_command('ping'), config) is False


@pytest.mark.parametrize('raw', ['true', 'yes', 'on', '1'])
def test_canonical_enabled_key_enables(raw):
    config = _config(f'[Ping_Command]\nenabled = {raw}\n')

    assert is_command_enabled(_command('ping'), config) is True


def test_camel_case_name_resolves_its_section():
    config = _config('[DadJoke_Command]\nenabled = false\n')

    assert is_command_enabled(_command('dadjoke'), config) is False
    # The naive "dadjoke".title() spelling must not be what gets read.
    assert not config.has_section('Dadjoke_Command')


def test_legacy_alias_in_another_section_disables():
    """[Jokes] joke_enabled lives in a different section than Joke_Command."""
    config = _config('[Jokes]\njoke_enabled = false\n')

    assert is_command_enabled(_command('joke'), config) is False


@pytest.mark.parametrize('name,section,key', [
    ('sports', 'Sports_Command', 'sports_enabled'),
    ('stats', 'Stats_Command', 'stats_enabled'),
    ('hacker', 'Hacker_Command', 'hacker_enabled'),
    ('alert', 'Alert_Command', 'alert_enabled'),
    # joke/dadjoke accept the same-section spelling too, via their own
    # __init__ fallback — see modules/commands/joke_command.py.
    ('joke', 'Joke_Command', 'joke_enabled'),
    ('dadjoke', 'DadJoke_Command', 'dadjoke_enabled'),
])
def test_legacy_alias_in_same_section_disables(name, section, key):
    config = _config(f'[{section}]\n{key} = false\n')

    assert is_command_enabled(_command(name), config) is False


def test_canonical_key_wins_over_legacy_alias():
    config = _config('[Joke_Command]\nenabled = true\n[Jokes]\njoke_enabled = false\n')

    assert is_command_enabled(_command('joke'), config) is True


def test_unparseable_enabled_value_falls_back_to_enabled():
    config = _config('[Ping_Command]\nenabled = maybe\n')

    assert is_command_enabled(_command('ping'), config) is True


def test_command_without_a_name_is_enabled():
    assert is_command_enabled(SimpleNamespace(), _config('')) is True


# ---------------------------------------------------------------------------
# filter_commands
# ---------------------------------------------------------------------------

def test_filter_commands_drops_disabled_commands():
    commands = {'ping': _command('ping'), 'wx': _command('wx')}
    config = _config('[Wx_Command]\nenabled = false\n')

    assert set(filter_commands(commands, [], config)) == {'ping'}


def test_filter_commands_drops_disabled_local_command():
    """A local command is filtered by the same rules as a built-in one."""
    commands = {'sitrep': _command('sitrep')}
    config = _config('[Sitrep_Command]\nenabled = false\n')

    assert filter_commands(commands, [], config) == {}


def test_filter_commands_keeps_enabled_local_command():
    commands = {'sitrep': _command('sitrep')}

    assert set(filter_commands(commands, [], _config(''))) == {'sitrep'}


def test_filter_commands_still_drops_admin_and_hidden():
    commands = {
        'ping': _command('ping'),
        'reboot': _command('reboot'),
        'secret': _command('secret', category='hidden'),
        'sudo': _command('sudo', requires_admin_access=lambda: True),
        'silent': _command('silent', keywords=[]),
        'quiet': _command('quiet', hidden=True),
    }

    assert set(filter_commands(commands, ['reboot'], _config(''))) == {'ping'}


# ---------------------------------------------------------------------------
# resolve_local_commands_dir
# ---------------------------------------------------------------------------

def test_resolve_local_commands_dir_returns_none_when_missing(tmp_path):
    logger = logging.getLogger('test')

    assert resolve_local_commands_dir(_config(''), str(tmp_path), logger) is None


def test_resolve_local_commands_dir_finds_default_location(tmp_path):
    expected = tmp_path / 'local' / 'commands'
    expected.mkdir(parents=True)
    logger = logging.getLogger('test')

    result = resolve_local_commands_dir(_config(''), str(tmp_path), logger)

    assert result is not None
    assert os.path.realpath(result) == os.path.realpath(str(expected))


def test_resolve_local_commands_dir_honors_configured_local_dir(tmp_path):
    expected = tmp_path / 'custom' / 'commands'
    expected.mkdir(parents=True)
    logger = logging.getLogger('test')

    result = resolve_local_commands_dir(
        _config('[Bot]\nlocal_dir_path = custom\n'), str(tmp_path), logger
    )

    assert result is not None
    assert os.path.realpath(result) == os.path.realpath(str(expected))


def test_resolve_local_commands_dir_rejects_a_file(tmp_path):
    """A file named `commands` is not a plugin directory."""
    (tmp_path / 'local').mkdir()
    (tmp_path / 'local' / 'commands').write_text('not a directory')
    logger = logging.getLogger('test')

    assert resolve_local_commands_dir(_config(''), str(tmp_path), logger) is None


# ---------------------------------------------------------------------------
# read_config local overlay
# ---------------------------------------------------------------------------

def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_read_config_overlays_local_config(tmp_path):
    """The settings UI writes a local plugin's state to local/config.ini."""
    base = _write(tmp_path / 'config.ini', '[Bot]\nbot_name = TestBot\n')
    _write(tmp_path / 'local' / 'config.ini', '[Sitrep_Command]\nenabled = false\n')

    config = read_config(str(base))

    assert config.get('Bot', 'bot_name') == 'TestBot'
    assert is_command_enabled(_command('sitrep'), config) is False


def test_local_overlay_wins_over_base(tmp_path):
    base = _write(tmp_path / 'config.ini', '[Bot]\n[Wx_Command]\nenabled = true\n')
    _write(tmp_path / 'local' / 'config.ini', '[Wx_Command]\nenabled = false\n')

    assert is_command_enabled(_command('wx'), read_config(str(base))) is False


def test_read_config_honors_configured_local_dir(tmp_path):
    base = _write(tmp_path / 'config.ini', '[Bot]\nlocal_dir_path = custom\n')
    _write(tmp_path / 'custom' / 'config.ini', '[Wx_Command]\nenabled = false\n')

    assert is_command_enabled(_command('wx'), read_config(str(base))) is False


def test_read_config_without_local_overlay(tmp_path):
    base = _write(tmp_path / 'config.ini', '[Bot]\nbot_name = TestBot\n')

    config = read_config(str(base))

    assert config.get('Bot', 'bot_name') == 'TestBot'
    assert is_command_enabled(_command('wx'), config) is True


def test_read_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_config(str(tmp_path / 'nope.ini'))
