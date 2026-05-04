"""Unit tests for flood scope name normalization."""

from modules.command_manager import CommandManager


def test_bare_name_gets_hash_prepended():
    assert CommandManager._normalize_scope_name("west") == "#west"


def test_already_prefixed_unchanged():
    assert CommandManager._normalize_scope_name("#west") == "#west"


def test_empty_string_is_global():
    assert CommandManager._normalize_scope_name("") == ""


def test_star_is_global():
    assert CommandManager._normalize_scope_name("*") == "*"


def test_zero_is_global():
    assert CommandManager._normalize_scope_name("0") == "0"


def test_none_string_is_global():
    assert CommandManager._normalize_scope_name("None") == "None"


def test_multi_word_name_gets_hash():
    assert CommandManager._normalize_scope_name("north east") == "#north east"


def test_already_prefixed_multi_word_unchanged():
    assert CommandManager._normalize_scope_name("#north east") == "#north east"
