#!/usr/bin/env python3
"""
Plugin loader for dynamic command discovery and loading
Handles scanning, loading, and registering command plugins
"""

import os
import sys
import inspect
import importlib
import importlib.util
from pathlib import Path
from typing import Dict, List, Any, Optional, Type
import logging

from .commands.base_command import BaseCommand


class PluginLoader:
    """Handles dynamic loading and discovery of command plugins"""
    
    def __init__(self, bot, commands_dir: str = None):
        self.bot = bot
        self.logger = bot.logger
        self.commands_dir = commands_dir or os.path.join(os.path.dirname(__file__), 'commands')
        self.alternatives_dir = os.path.join(self.commands_dir, 'alternatives')
        self.loaded_plugins: Dict[str, BaseCommand] = {}
        self.plugin_metadata: Dict[str, Dict[str, Any]] = {}
        self.keyword_mappings: Dict[str, str] = {}  # keyword -> plugin_name
        self.plugin_overrides: Dict[str, str] = {}  # plugin_name -> alternative_file_name
        self._failed_plugins: Dict[str, str] = {}  # plugin_name -> error_message
        self._load_plugin_overrides()
        
    def _load_plugin_overrides(self):
        """Load plugin override configuration from config file"""
        self.plugin_overrides = {}
        try:
            if self.bot.config.has_section('Plugin_Overrides'):
                for command_name, alternative_file in self.bot.config.items('Plugin_Overrides'):
                    # Remove .py extension if present
                    if alternative_file.endswith('.py'):
                        alternative_file = alternative_file[:-3]
                    self.plugin_overrides[command_name.strip()] = alternative_file.strip()
                    self.logger.info(f"Plugin override configured: {command_name} -> {alternative_file}")
        except Exception as e:
            self.logger.error(f"Error loading plugin overrides: {e}")
            # Track this as a configuration error
            self._failed_plugins['plugin_overrides_config'] = str(e)
    
    def discover_plugins(self) -> List[str]:
        """Discover all Python files in the commands directory that could be plugins"""
        plugin_files = []
        commands_path = Path(self.commands_dir)
        
        if not commands_path.exists():
            self.logger.error(f"Commands directory does not exist: {self.commands_dir}")
            return plugin_files
        
        # Scan for Python files (excluding __init__.py and base_command.py)
        for file_path in commands_path.glob("*.py"):
            if file_path.name not in ["__init__.py", "base_command.py", "plugin_loader.py"]:
                plugin_files.append(file_path.stem)
        
        self.logger.info(f"Discovered {len(plugin_files)} potential plugin files: {plugin_files}")
        return plugin_files
    
    def discover_alternative_plugins(self) -> List[str]:
        """Discover all Python files in the alternatives directory that could be plugins
        Note: Plugins in the 'inactive' subdirectory are ignored
        """
        plugin_files = []
        alternatives_path = Path(self.alternatives_dir)
        
        if not alternatives_path.exists():
            # Alternatives directory doesn't exist yet, that's okay
            return plugin_files
        
        # Scan for Python files (excluding __init__.py)
        # Note: glob("*.py") only matches files in the current directory, not subdirectories
        # So the 'inactive' subdirectory is automatically excluded
        for file_path in alternatives_path.glob("*.py"):
            if file_path.name not in ["__init__.py"]:
                plugin_files.append(file_path.stem)
        
        if plugin_files:
            self.logger.info(f"Discovered {len(plugin_files)} alternative plugin files: {plugin_files}")
        return plugin_files
    
    def _validate_plugin(self, plugin_class: Type[BaseCommand]) -> List[str]:
        """
        Validate a plugin class has required attributes before instantiation.
        All plugins should define name and keywords as class attributes (the standard convention).
        We validate leniently here and re-check after instantiation to catch any edge cases.
        
        Args:
            plugin_class: The plugin class to validate
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        
        # Check for execute method (must exist as class method)
        if not hasattr(plugin_class, 'execute'):
            errors.append(f"Missing required attribute: execute")
        elif hasattr(plugin_class, 'execute'):
            if not inspect.iscoroutinefunction(plugin_class.execute):
                errors.append("Plugin 'execute' method must be async")
        
        # Check for keywords type if it exists (allow empty list for system commands)
        if hasattr(plugin_class, 'keywords'):
            keywords = getattr(plugin_class, 'keywords', None)
            if keywords is not None and not isinstance(keywords, list):
                errors.append("Plugin 'keywords' must be a list")
            # Don't error on empty keywords - some commands (like greeter) intentionally have none
        
        # Note: We don't check name/keywords presence here since they may be set in __init__
        # or derived from class name. Post-instantiation validation will catch missing attributes.
        
        return errors
    
    def _validate_plugin_instance(self, plugin_instance: BaseCommand, plugin_name: str) -> List[str]:
        """
        Validate a plugin instance after instantiation.
        Ensures required attributes are present and correctly typed.
        Note: Name may be derived from class name if not explicitly set.
        
        Args:
            plugin_instance: The instantiated plugin
            plugin_name: The file name of the plugin (for error messages)
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        
        # Check name - must be set (either as class attribute, in __init__, or derived from class name)
        if not hasattr(plugin_instance, 'name') or not plugin_instance.name:
            errors.append("Plugin 'name' attribute is empty or not set")
        
        # Check keywords - must be a list (can be empty for system commands like greeter)
        if not hasattr(plugin_instance, 'keywords'):
            errors.append("Plugin 'keywords' attribute is missing")
        elif not isinstance(plugin_instance.keywords, list):
            errors.append("Plugin 'keywords' must be a list")
        # Allow empty keywords - some system commands intentionally have none
        
        return errors
    
    def load_plugin(self, plugin_name: str, from_alternatives: bool = False) -> Optional[BaseCommand]:
        """Load a single plugin by name
        
        Args:
            plugin_name: Name of the plugin file (without .py extension)
            from_alternatives: If True, load from alternatives directory; if False, load from commands directory
        """
        try:
            # Construct the full module path
            if from_alternatives:
                module_path = f"modules.commands.alternatives.{plugin_name}"
            else:
                module_path = f"modules.commands.{plugin_name}"
            
            # Check if module is already loaded
            if module_path in sys.modules:
                module = sys.modules[module_path]
            else:
                # Import the module
                module = importlib.import_module(module_path)
            
            # Find the command class (should be the only class that inherits from BaseCommand)
            command_class = None
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (issubclass(obj, BaseCommand) and 
                    obj != BaseCommand and 
                    obj.__module__ == module_path):
                    command_class = obj
                    break
            
            if not command_class:
                error_msg = f"No valid command class found in {plugin_name}"
                self.logger.warning(error_msg)
                self._failed_plugins[plugin_name] = error_msg
                return None
            
            # Validate plugin class before instantiation (basic checks)
            validation_errors = self._validate_plugin(command_class)
            if validation_errors:
                error_msg = f"Plugin validation failed: {', '.join(validation_errors)}"
                self.logger.error(f"Failed to load plugin '{plugin_name}': {error_msg}")
                self._failed_plugins[plugin_name] = error_msg
                return None
            
            # Instantiate the command
            plugin_instance = command_class(self.bot)
            
            # Set name from class name if not set (before validation)
            if not hasattr(plugin_instance, 'name') or not plugin_instance.name:
                # Use the class name as the plugin name if not specified
                derived_name = command_class.__name__.lower().replace('command', '')
                plugin_instance.name = derived_name
            
            # Validate plugin instance after instantiation (catches attributes set in __init__)
            instance_validation_errors = self._validate_plugin_instance(plugin_instance, plugin_name)
            if instance_validation_errors:
                error_msg = f"Plugin instance validation failed: {', '.join(instance_validation_errors)}"
                self.logger.error(f"Failed to load plugin '{plugin_name}': {error_msg}")
                self._failed_plugins[plugin_name] = error_msg
                return None
            
            # Validate plugin metadata
            metadata = plugin_instance.get_metadata()
            
            source = "alternatives" if from_alternatives else "default"
            self.logger.info(f"Successfully loaded plugin: {metadata['name']} from {plugin_name} ({source})")
            return plugin_instance
            
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Failed to load plugin '{plugin_name}': {error_msg}")
            self._failed_plugins[plugin_name] = error_msg
            return None
    
    def load_all_plugins(self) -> Dict[str, BaseCommand]:
        """Load all discovered plugins, with alternative plugins taking priority when configured"""
        # First, discover all default and alternative plugins
        default_plugin_files = self.discover_plugins()
        alternative_plugin_files = self.discover_alternative_plugins()
        
        # Build a map of plugin names to their file names for default plugins
        default_plugin_map = {}  # plugin_name -> file_name
        loaded_plugins = {}
        
        # First pass: Load all default plugins and build the map
        for plugin_file in default_plugin_files:
            plugin_instance = self.load_plugin(plugin_file, from_alternatives=False)
            if plugin_instance:
                metadata = plugin_instance.get_metadata()
                plugin_name = metadata['name']
                default_plugin_map[plugin_name] = plugin_file
                loaded_plugins[plugin_name] = plugin_instance
                self.plugin_metadata[plugin_name] = metadata
        
        # Second pass: Check for overrides and load alternative plugins
        # Check config-based overrides first
        for plugin_name, alternative_file in self.plugin_overrides.items():
            if alternative_file in alternative_plugin_files:
                # Load the alternative plugin
                alt_instance = self.load_plugin(alternative_file, from_alternatives=True)
                if alt_instance:
                    alt_metadata = alt_instance.get_metadata()
                    alt_plugin_name = alt_metadata['name']
                    
                    # If the alternative plugin has a different name, use the override name
                    # This allows wx_international (name="gwx") to be used as "wx" when configured
                    if alt_plugin_name != plugin_name:
                        self.logger.info(
                            f"Alternative plugin {alternative_file} (name='{alt_plugin_name}') "
                            f"is being used to override '{plugin_name}'. Registering as '{plugin_name}'."
                        )
                        # Update the instance's name and metadata to match the override name
                        alt_instance.name = plugin_name
                        alt_metadata['name'] = plugin_name
                    
                    # Replace the default plugin with the alternative
                    if plugin_name in loaded_plugins:
                        self.logger.info(f"Replacing default plugin '{plugin_name}' with alternative '{alternative_file}'")
                    loaded_plugins[plugin_name] = alt_instance
                    self.plugin_metadata[plugin_name] = alt_metadata
                else:
                    self.logger.warning(f"Failed to load alternative plugin '{alternative_file}' for '{plugin_name}'")
            else:
                self.logger.warning(
                    f"Alternative plugin '{alternative_file}' not found in alternatives directory "
                    f"for override of '{plugin_name}'"
                )
        
        # Third pass: Load alternative plugins that aren't overriding anything
        # (standalone alternative plugins)
        for alt_file in alternative_plugin_files:
            # Skip if this alternative is already loaded as an override
            if alt_file in self.plugin_overrides.values():
                continue
            
            alt_instance = self.load_plugin(alt_file, from_alternatives=True)
            if alt_instance:
                alt_metadata = alt_instance.get_metadata()
                alt_plugin_name = alt_metadata['name']
                
                # Special case: If wx_international is available and wx command is missing,
                # automatically use it as wx (fallback behavior)
                if alt_file == 'wx_international' and 'wx' not in loaded_plugins:
                    self.logger.info(
                        f"Default 'wx' command not found, using 'wx_international' as fallback for 'wx' command"
                    )
                    # Update the instance to use 'wx' name and keywords
                    alt_instance.name = 'wx'
                    alt_metadata['name'] = 'wx'
                    # Update keywords to match wx command
                    alt_instance.keywords = ['wx', 'weather', 'wxa', 'wxalert']
                    alt_metadata['keywords'] = ['wx', 'weather', 'wxa', 'wxalert']
                    alt_metadata['description'] = "Get weather information for any location (usage: wx Tokyo)"
                    loaded_plugins['wx'] = alt_instance
                    self.plugin_metadata['wx'] = alt_metadata
                    continue
                
                # If an alternative plugin has the same name as a default plugin,
                # it will replace it (unless already overridden by config)
                if alt_plugin_name in loaded_plugins:
                    if alt_plugin_name not in self.plugin_overrides:
                        self.logger.info(
                            f"Alternative plugin '{alt_file}' replaces default plugin '{alt_plugin_name}' "
                            f"(same plugin name detected)"
                        )
                else:
                    self.logger.info(f"Loading standalone alternative plugin: {alt_plugin_name} from {alt_file}")
                
                loaded_plugins[alt_plugin_name] = alt_instance
                self.plugin_metadata[alt_plugin_name] = alt_metadata
        
        # Build keyword mappings for all loaded plugins
        for plugin_name, plugin_instance in loaded_plugins.items():
            metadata = self.plugin_metadata[plugin_name]
            self._build_keyword_mappings(plugin_name, metadata)
        
        self.loaded_plugins = loaded_plugins
        
        # Report loading summary
        self.logger.info(f"Loaded {len(loaded_plugins)} plugins: {list(loaded_plugins.keys())}")
        if self._failed_plugins:
            self.logger.warning(f"Failed to load {len(self._failed_plugins)} plugin(s): {list(self._failed_plugins.keys())}")
            for plugin_name, error_msg in self._failed_plugins.items():
                self.logger.warning(f"  - {plugin_name}: {error_msg}")
        
        return loaded_plugins
    
    def _build_keyword_mappings(self, plugin_name: str, metadata: Dict[str, Any]):
        """Build keyword to plugin name mappings"""
        # Map keywords to plugin name
        for keyword in metadata.get('keywords', []):
            self.keyword_mappings[keyword.lower()] = plugin_name
        
        # Map aliases to plugin name
        for alias in metadata.get('aliases', []):
            self.keyword_mappings[alias.lower()] = plugin_name
    
    def get_plugin_by_keyword(self, keyword: str) -> Optional[BaseCommand]:
        """Get a plugin instance by keyword"""
        plugin_name = self.keyword_mappings.get(keyword.lower())
        if plugin_name:
            return self.loaded_plugins.get(plugin_name)
        return None
    
    def get_plugin_by_name(self, name: str) -> Optional[BaseCommand]:
        """Get a plugin instance by name"""
        return self.loaded_plugins.get(name)
    
    def get_all_plugins(self) -> Dict[str, BaseCommand]:
        """Get all loaded plugins"""
        return self.loaded_plugins.copy()
    
    def get_plugin_metadata(self, plugin_name: str = None) -> Dict[str, Any]:
        """Get metadata for a specific plugin or all plugins"""
        if plugin_name:
            return self.plugin_metadata.get(plugin_name, {})
        return self.plugin_metadata.copy()
    
    def get_failed_plugins(self) -> Dict[str, str]:
        """Return dict of plugins that failed to load with their error messages"""
        return self._failed_plugins.copy()
    
    def get_plugins_by_category(self, category: str) -> Dict[str, BaseCommand]:
        """Get all plugins in a specific category"""
        return {
            name: plugin for name, plugin in self.loaded_plugins.items()
            if plugin.category == category
        }
    
    def reload_plugin(self, plugin_name: str) -> bool:
        """Reload a specific plugin"""
        try:
            # Remove from loaded plugins
            if plugin_name in self.loaded_plugins:
                del self.loaded_plugins[plugin_name]
            
            # Remove from metadata
            if plugin_name in self.plugin_metadata:
                del self.plugin_metadata[plugin_name]
            
            # Remove keyword mappings
            keywords_to_remove = []
            for keyword, mapped_name in self.keyword_mappings.items():
                if mapped_name == plugin_name:
                    keywords_to_remove.append(keyword)
            
            for keyword in keywords_to_remove:
                del self.keyword_mappings[keyword]
            
            # Check if this plugin should be loaded from alternatives
            from_alternatives = False
            if plugin_name in self.plugin_overrides:
                # This plugin is overridden, reload from alternatives
                alternative_file = self.plugin_overrides[plugin_name]
                plugin_instance = self.load_plugin(alternative_file, from_alternatives=True)
            else:
                # Try to find the plugin file name
                # First check if it's in default plugins
                default_plugins = self.discover_plugins()
                plugin_file = None
                for df in default_plugins:
                    test_instance = self.load_plugin(df, from_alternatives=False)
                    if test_instance and test_instance.get_metadata().get('name') == plugin_name:
                        plugin_file = df
                        break
                
                if plugin_file:
                    plugin_instance = self.load_plugin(plugin_file, from_alternatives=False)
                else:
                    # Try alternatives
                    alt_plugins = self.discover_alternative_plugins()
                    for alt in alt_plugins:
                        test_instance = self.load_plugin(alt, from_alternatives=True)
                        if test_instance and test_instance.get_metadata().get('name') == plugin_name:
                            plugin_instance = self.load_plugin(alt, from_alternatives=True)
                            break
                    else:
                        plugin_instance = None
            
            if plugin_instance:
                metadata = plugin_instance.get_metadata()
                self.loaded_plugins[plugin_name] = plugin_instance
                self.plugin_metadata[plugin_name] = metadata
                self._build_keyword_mappings(plugin_name, metadata)
                self.logger.info(f"Successfully reloaded plugin: {plugin_name}")
                return True
            else:
                self.logger.error(f"Failed to reload plugin: {plugin_name}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error reloading plugin {plugin_name}: {e}")
            return False
    
    def validate_plugin(self, plugin_instance: BaseCommand) -> List[str]:
        """Validate a plugin instance and return any issues"""
        issues = []
        metadata = plugin_instance.get_metadata()
        
        # Check required metadata
        if not metadata.get('name'):
            issues.append("Plugin missing 'name' metadata")
        
        if not metadata.get('description'):
            issues.append("Plugin missing 'description' metadata")
        
        # Check if execute method is implemented
        if not hasattr(plugin_instance, 'execute'):
            issues.append("Plugin missing 'execute' method")
        
        # Check for keyword conflicts
        for keyword in metadata.get('keywords', []):
            if keyword.lower() in self.keyword_mappings:
                existing_plugin = self.keyword_mappings[keyword.lower()]
                if existing_plugin != metadata['name']:
                    issues.append(f"Keyword '{keyword}' conflicts with plugin '{existing_plugin}'")
        
        return issues
