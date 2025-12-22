#!/usr/bin/env python3
"""
Base service plugin class for background services
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseServicePlugin(ABC):
    """Base class for background service plugins"""
    
    # Optional: Config section name (if different from class name)
    # If not set, will be derived from class name (e.g., PacketCaptureService -> PacketCapture)
    config_section: Optional[str] = None
    
    # Optional: Service description for metadata
    description: str = ""
    
    def __init__(self, bot):
        """
        Initialize the service plugin
        
        Args:
            bot: The MeshCoreBot instance
        """
        self.bot = bot
        self.logger = bot.logger
        self.enabled = True
        self._running = False
    
    @abstractmethod
    async def start(self):
        """
        Start the service
        
        This method should:
        - Setup event handlers if needed
        - Start background tasks
        - Initialize any required resources
        """
        pass
    
    @abstractmethod
    async def stop(self):
        """
        Stop the service
        
        This method should:
        - Clean up event handlers
        - Stop background tasks
        - Close any open resources
        """
        pass
    
    def get_metadata(self) -> Dict[str, Any]:
        """
        Get service metadata
        
        Returns:
            Dictionary containing service metadata
        """
        return {
            'name': self._derive_service_name(),
            'class_name': self.__class__.__name__,
            'description': getattr(self, 'description', ''),
            'enabled': self.enabled,
            'running': self._running,
            'config_section': self.config_section or self._derive_config_section()
        }
    
    def _derive_service_name(self) -> str:
        """Derive service name from class name"""
        class_name = self.__class__.__name__
        if class_name.endswith('Service'):
            return class_name[:-7].lower()  # Remove 'Service' suffix and lowercase
        return class_name.lower()
    
    def _derive_config_section(self) -> str:
        """Derive config section name from class name"""
        if self.config_section:
            return self.config_section
        
        class_name = self.__class__.__name__
        if class_name.endswith('Service'):
            return class_name[:-7]  # Remove 'Service' suffix
        return class_name
    
    def is_running(self) -> bool:
        """Check if the service is currently running"""
        return self._running

