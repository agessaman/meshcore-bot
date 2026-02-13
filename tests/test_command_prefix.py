#!/usr/bin/env python3
"""
Unit tests for command prefix functionality
Tests that all commands properly handle command prefixes when enabled
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from configparser import ConfigParser

from modules.models import MeshMessage
from modules.commands.base_command import BaseCommand
from modules.commands.hello_command import HelloCommand
from modules.commands.ping_command import PingCommand
from modules.commands.help_command import HelpCommand
from modules.command_manager import CommandManager


class MockTestCommand(BaseCommand):
    """Mock command for testing prefix functionality"""
    name = "test"
    keywords = ['test', 't']
    description = "Test command"
    category = "test"
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the test command (required by abstract base class)"""
        return True


@pytest.fixture
def mock_bot():
    """Create a mock bot instance"""
    bot = Mock()
    bot.logger = Mock()
    bot.logger.debug = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.config = ConfigParser()
    bot.config.add_section('Bot')
    bot.config.add_section('Channels')
    bot.config.set('Channels', 'monitor_channels', 'general')
    bot.config.set('Channels', 'respond_to_dms', 'true')
    bot.meshcore = None
    bot.translator = None
    bot.rate_limiter = Mock()
    bot.rate_limiter.can_send = Mock(return_value=True)
    bot.bot_tx_rate_limiter = Mock()
    bot.bot_tx_rate_limiter.wait_for_tx = Mock()
    bot.tx_delay_ms = 0
    return bot


@pytest.fixture
def mock_message():
    """Create a mock message"""
    return MeshMessage(
        content="test",
        sender_id="TestUser",
        channel="general",
        is_dm=False
    )


class TestCommandPrefix:
    """Test command prefix functionality"""
    
    def test_no_prefix_allows_commands(self, mock_bot, mock_message):
        """Test that without prefix configured, commands work normally"""
        mock_bot.config.set('Bot', 'command_prefix', '')
        command = MockTestCommand(mock_bot)
        
        # Should match without prefix
        assert command.matches_keyword(mock_message) is True
        
        # Should also match with legacy ! prefix
        mock_message.content = "!test"
        assert command.matches_keyword(mock_message) is True
    
    def test_prefix_required_when_configured(self, mock_bot, mock_message):
        """Test that when prefix is configured, it's required"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        command = MockTestCommand(mock_bot)
        
        # Should match with prefix
        mock_message.content = "!test"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match without prefix
        mock_message.content = "test"
        assert command.matches_keyword(mock_message) is False
    
    def test_dot_prefix(self, mock_bot, mock_message):
        """Test dot prefix (e.g., .ping)"""
        mock_bot.config.set('Bot', 'command_prefix', '.')
        command = MockTestCommand(mock_bot)
        
        # Should match with dot prefix
        mock_message.content = ".test"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match without prefix
        mock_message.content = "test"
        assert command.matches_keyword(mock_message) is False
    
    def test_single_char_prefix(self, mock_bot, mock_message):
        """Test single character prefix (e.g., bping)"""
        mock_bot.config.set('Bot', 'command_prefix', 'b')
        command = MockTestCommand(mock_bot)
        
        # Should match with 'b' prefix
        mock_message.content = "btest"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match without prefix
        mock_message.content = "test"
        assert command.matches_keyword(mock_message) is False
    
    def test_multi_char_prefix(self, mock_bot, mock_message):
        """Test multi-character prefix (e.g., abcping)"""
        mock_bot.config.set('Bot', 'command_prefix', 'abc')
        command = MockTestCommand(mock_bot)
        
        # Should match with 'abc' prefix
        mock_message.content = "abctest"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match without prefix
        mock_message.content = "test"
        assert command.matches_keyword(mock_message) is False
        
        # Should NOT match with partial prefix
        mock_message.content = "abtest"
        assert command.matches_keyword(mock_message) is False
    
    def test_prefix_with_whitespace(self, mock_bot, mock_message):
        """Test that prefix works with whitespace after it"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        command = MockTestCommand(mock_bot)
        
        # Should match with prefix and space
        mock_message.content = "! test"
        assert command.matches_keyword(mock_message) is True
        
        # Should match with prefix and no space
        mock_message.content = "!test"
        assert command.matches_keyword(mock_message) is True
    
    def test_prefix_with_keyword_variations(self, mock_bot, mock_message):
        """Test prefix with different keyword variations"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        command = MockTestCommand(mock_bot)
        
        # Test first keyword
        mock_message.content = "!test"
        assert command.matches_keyword(mock_message) is True
        
        # Test second keyword
        mock_message.content = "!t"
        assert command.matches_keyword(mock_message) is True
        
        # Test keyword with arguments
        mock_message.content = "!test arg1 arg2"
        assert command.matches_keyword(mock_message) is True
    
    def test_hello_command_with_prefix(self, mock_bot, mock_message):
        """Test hello command specifically with prefix"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        mock_bot.config.set('Bot', 'bot_name', 'TestBot')
        mock_bot.config.add_section('Hello_Command')
        mock_bot.config.set('Hello_Command', 'enabled', 'true')
        command = HelloCommand(mock_bot)
        
        # Should match with prefix
        mock_message.content = "!hello"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match without prefix
        mock_message.content = "hello"
        assert command.matches_keyword(mock_message) is False
    
    def test_ping_command_with_prefix(self, mock_bot, mock_message):
        """Test ping command with prefix"""
        mock_bot.config.set('Bot', 'command_prefix', '.')
        mock_bot.config.add_section('Ping_Command')
        mock_bot.config.set('Ping_Command', 'enabled', 'true')
        command = PingCommand(mock_bot)
        
        # Should match with dot prefix
        mock_message.content = ".ping"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match without prefix
        mock_message.content = "ping"
        assert command.matches_keyword(mock_message) is False
    
    def test_command_manager_with_prefix(self, mock_bot, mock_message):
        """Test CommandManager handles prefix correctly"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        mock_bot.config.add_section('Keywords')
        mock_bot.config.set('Keywords', 'keywords', '')
        mock_bot.config.add_section('Custom_Syntax')
        mock_bot.config.set('Custom_Syntax', 'custom_syntax', '')
        
        # Mock plugin loader to return empty commands for simplicity
        with patch('modules.command_manager.PluginLoader') as mock_loader_class:
            mock_loader = Mock()
            mock_loader.load_all_plugins = Mock(return_value={})
            mock_loader_class.return_value = mock_loader
            
            manager = CommandManager(mock_bot)
            
            # Should return empty matches for message without prefix
            mock_message.content = "test"
            matches = manager.check_keywords(mock_message)
            assert matches == []
            
            # Should process message with prefix
            mock_message.content = "!test"
            matches = manager.check_keywords(mock_message)
            # Will be empty because no commands loaded, but should process without error
            assert isinstance(matches, list)
    
    def test_prefix_with_mentions(self, mock_bot, mock_message):
        """Test that prefix works correctly with @[username] mentions"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        mock_bot.config.set('Bot', 'bot_name', 'TestBot')
        command = MockTestCommand(mock_bot)
        
        # Mock self_info to return bot name
        mock_bot.meshcore = Mock()
        mock_bot.meshcore.self_info = {'name': 'TestBot'}
        
        # Should match with prefix and bot mention
        mock_message.content = "!@[TestBot] test"
        assert command.matches_keyword(mock_message) is True
        
        # Should NOT match with prefix but other user mention
        mock_message.content = "!@[OtherUser] test"
        assert command.matches_keyword(mock_message) is False
    
    def test_different_prefixes_dont_match(self, mock_bot, mock_message):
        """Test that wrong prefix doesn't match"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        command = MockTestCommand(mock_bot)
        
        # Should NOT match with wrong prefix
        mock_message.content = ".test"
        assert command.matches_keyword(mock_message) is False
        
        mock_message.content = "btest"
        assert command.matches_keyword(mock_message) is False
        
        mock_message.content = "abctest"
        assert command.matches_keyword(mock_message) is False
    
    def test_prefix_case_sensitive(self, mock_bot, mock_message):
        """Test that prefix matching is case-sensitive"""
        mock_bot.config.set('Bot', 'command_prefix', '!')
        command = MockTestCommand(mock_bot)
        
        # Should match with exact prefix
        mock_message.content = "!test"
        assert command.matches_keyword(mock_message) is True
        
        # Prefix matching is case-sensitive, so different case shouldn't match
        # (This tests the actual behavior - prefixes are case-sensitive)
        mock_message.content = "!TEST"  # Prefix is still '!', so this should match
        assert command.matches_keyword(mock_message) is True  # '!' is same case
        
        # But if prefix is lowercase, uppercase shouldn't match
        mock_bot.config.set('Bot', 'command_prefix', 'b')
        command = MockTestCommand(mock_bot)
        mock_message.content = "Btest"  # Uppercase B
        assert command.matches_keyword(mock_message) is False  # Should not match lowercase 'b'
    
    def test_empty_prefix_string(self, mock_bot, mock_message):
        """Test that empty string prefix means no prefix required"""
        mock_bot.config.set('Bot', 'command_prefix', '')
        command = MockTestCommand(mock_bot)
        
        # Should match without prefix
        mock_message.content = "test"
        assert command.matches_keyword(mock_message) is True
        
        # Should also match with legacy ! prefix
        mock_message.content = "!test"
        assert command.matches_keyword(mock_message) is True
