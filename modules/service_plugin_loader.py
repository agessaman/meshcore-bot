#!/usr/bin/env python3
"""
Service plugin loader for dynamic service discovery and loading
Handles scanning, loading, and registering service plugins
"""

import os
import sys
import importlib
import inspect
from pathlib import Path
from typing import Dict, List, Any, Optional, Type

from .service_plugins.base_service import BaseServicePlugin


class ServicePluginLoader:
    """Handles dynamic loading and discovery of service plugins"""
    
    def __init__(self, bot, services_dir: str = None):
        self.bot = bot
        self.logger = bot.logger
        self.services_dir = services_dir or os.path.join(
            os.path.dirname(__file__), 'service_plugins'
        )
        self.loaded_services: Dict[str, BaseServicePlugin] = {}
        self.service_metadata: Dict[str, Dict[str, Any]] = {}
        self.service_overrides: Dict[str, str] = {}
        self._load_service_overrides()
    
    def _load_service_overrides(self):
        """Load service override configuration from config file"""
        self.service_overrides = {}
        try:
            if self.bot.config.has_section('Service_Overrides'):
                for service_name, alternative_file in self.bot.config.items('Service_Overrides'):
                    if alternative_file.endswith('.py'):
                        alternative_file = alternative_file[:-3]
                    self.service_overrides[service_name.strip()] = alternative_file.strip()
                    self.logger.info(f"Service override configured: {service_name} -> {alternative_file}")
        except Exception as e:
            self.logger.warning(f"Error loading service overrides: {e}")
    
    def discover_services(self) -> List[str]:
        """Discover all Python files in the service_plugins directory"""
        service_files = []
        services_path = Path(self.services_dir)
        
        if not services_path.exists():
            self.logger.error(f"Services directory does not exist: {self.services_dir}")
            return service_files
        
        # Scan for Python files (excluding __init__.py, base_service.py, and utility files)
        excluded_files = ["__init__.py", "base_service.py", "service_plugin_loader.py"]
        for file_path in services_path.glob("*.py"):
            if file_path.name not in excluded_files and not file_path.name.endswith("_utils.py"):
                service_files.append(file_path.stem)
        
        self.logger.info(f"Discovered {len(service_files)} potential service files: {service_files}")
        return service_files
    
    def load_service(self, service_name: str) -> Optional[BaseServicePlugin]:
        """Load a single service plugin by name"""
        try:
            # Construct the full module path
            module_path = f"modules.service_plugins.{service_name}"
            
            # Check if module is already loaded
            if module_path in sys.modules:
                module = sys.modules[module_path]
            else:
                # Import the module
                module = importlib.import_module(module_path)
            
            # Find the service class (should inherit from BaseServicePlugin)
            service_class = None
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (issubclass(obj, BaseServicePlugin) and 
                    obj != BaseServicePlugin and 
                    obj.__module__ == module_path):
                    service_class = obj
                    break
            
            if not service_class:
                self.logger.warning(f"No valid service class found in {service_name}")
                return None
            
            # Check if service is enabled in config
            config_section = self._get_config_section_for_service(service_class)
            if config_section and self.bot.config.has_section(config_section):
                enabled = self.bot.config.getboolean(config_section, 'enabled', fallback=False)
                if not enabled:
                    self.logger.info(f"Service {service_name} is disabled in config (section: {config_section})")
                    return None
            elif config_section:
                # Config section exists but 'enabled' not set - default to False for safety
                self.logger.info(f"Service {service_name} config section '{config_section}' exists but 'enabled' not set, skipping")
                return None
            
            # Instantiate the service
            service_instance = service_class(self.bot)
            
            # Validate service metadata
            metadata = service_instance.get_metadata()
            if not metadata.get('name'):
                metadata['name'] = service_class.__name__.lower().replace('service', '')
                service_instance.name = metadata['name']
            
            self.logger.info(f"Successfully loaded service: {metadata['name']} from {service_name}")
            return service_instance
            
        except Exception as e:
            self.logger.error(f"Failed to load service {service_name}: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return None
    
    def _get_config_section_for_service(self, service_class) -> Optional[str]:
        """Get config section name for a service class
        
        Tries multiple strategies:
        1. Check for 'config_section' class attribute
        2. Derive from class name (e.g., PacketCaptureService -> PacketCapture)
        3. Return None if not found
        """
        # Check for explicit config_section attribute
        if hasattr(service_class, 'config_section') and service_class.config_section:
            return service_class.config_section
        
        # Derive from class name
        class_name = service_class.__name__
        if class_name.endswith('Service'):
            section_name = class_name[:-7]  # Remove 'Service' suffix
            return section_name
        
        return None
    
    def load_all_services(self) -> Dict[str, BaseServicePlugin]:
        """Load all discovered services"""
        service_files = self.discover_services()
        loaded_services = {}
        
        for service_file in service_files:
            # Check for overrides
            if service_file in self.service_overrides.values():
                # This is an override, skip for now (will be handled separately)
                continue
            
            service_instance = self.load_service(service_file)
            if service_instance:
                metadata = service_instance.get_metadata()
                service_name = metadata['name']
                loaded_services[service_name] = service_instance
                self.service_metadata[service_name] = metadata
        
        # Handle overrides
        for service_name, override_file in self.service_overrides.items():
            if override_file in service_files:
                override_instance = self.load_service(override_file)
                if override_instance:
                    override_metadata = override_instance.get_metadata()
                    override_service_name = override_metadata['name']
                    
                    if override_service_name != service_name:
                        self.logger.warning(
                            f"Override service {override_file} has name '{override_service_name}' "
                            f"but is configured to override '{service_name}'. Using '{override_service_name}'."
                        )
                        service_name = override_service_name
                    
                    if service_name in loaded_services:
                        self.logger.info(f"Replacing service '{service_name}' with override '{override_file}'")
                    loaded_services[service_name] = override_instance
                    self.service_metadata[service_name] = override_metadata
        
        self.loaded_services = loaded_services
        self.logger.info(f"Loaded {len(loaded_services)} service(s): {list(loaded_services.keys())}")
        return loaded_services
    
    def get_service_by_name(self, name: str) -> Optional[BaseServicePlugin]:
        """Get a service instance by name"""
        return self.loaded_services.get(name)
    
    def get_all_services(self) -> Dict[str, BaseServicePlugin]:
        """Get all loaded services"""
        return self.loaded_services.copy()
    
    def get_service_metadata(self, service_name: str = None) -> Dict[str, Any]:
        """Get metadata for a specific service or all services"""
        if service_name:
            return self.service_metadata.get(service_name, {})
        return self.service_metadata.copy()
    
    def validate_service(self, service_instance: BaseServicePlugin) -> List[str]:
        """Validate a service instance and return any issues"""
        issues = []
        metadata = service_instance.get_metadata()
        
        # Check required methods
        if not hasattr(service_instance, 'start'):
            issues.append("Service missing 'start' method")
        elif not inspect.iscoroutinefunction(service_instance.start):
            issues.append("Service 'start' method must be async")
        
        if not hasattr(service_instance, 'stop'):
            issues.append("Service missing 'stop' method")
        elif not inspect.iscoroutinefunction(service_instance.stop):
            issues.append("Service 'stop' method must be async")
        
        return issues

