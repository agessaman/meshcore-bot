"""Tests for meshcore_bot.py CLI flags (--show-config, --validate-config)."""

import subprocess
import sys
import tempfile
import os


def _write_config(content: str) -> str:
    """Write a temp config.ini and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False)
    f.write(content)
    f.close()
    return f.name


SAMPLE_CONFIG = """
[Bot]
bot_name = TestBot
db_path = test.db

[Web_Viewer]
host = 127.0.0.1
port = 8080
"""


class TestShowConfig:
    def test_outputs_sections_and_keys(self):
        cfg_path = _write_config(SAMPLE_CONFIG)
        try:
            result = subprocess.run(
                [sys.executable, "meshcore_bot.py", "--config", cfg_path, "--show-config"],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0
            assert "[Bot]" in result.stdout
            assert "bot_name = TestBot" in result.stdout
            assert "[Web_Viewer]" in result.stdout
            assert "port = 8080" in result.stdout
        finally:
            os.unlink(cfg_path)

    def test_exits_zero(self):
        cfg_path = _write_config(SAMPLE_CONFIG)
        try:
            result = subprocess.run(
                [sys.executable, "meshcore_bot.py", "--config", cfg_path, "--show-config"],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0
        finally:
            os.unlink(cfg_path)

    def test_includes_config_filename_comment(self):
        cfg_path = _write_config(SAMPLE_CONFIG)
        try:
            result = subprocess.run(
                [sys.executable, "meshcore_bot.py", "--config", cfg_path, "--show-config"],
                capture_output=True, text=True, timeout=10,
            )
            assert cfg_path in result.stdout
        finally:
            os.unlink(cfg_path)
