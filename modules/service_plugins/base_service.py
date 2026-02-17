#!/usr/bin/env python3
"""
Base service plugin class for background services
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseServicePlugin(ABC):
    """Base class for background service plugins.
    
    This class defines the interface for service plugins, which are long-running
    background tasks that can interact with the bot and mesh network. It manages
    service lifecycle (start/stop) and metadata.
    """
    
    # Optional: Config section name (if different from class name)
    # If not set, will be derived from class name (e.g., PacketCaptureService -> PacketCapture)
    config_section: Optional[str] = None
    
    # Optional: Service description for metadata
    description: str = ""
    
    def __init__(self, bot: Any):
        """Initialize the service plugin.
        
        Args:
            bot: The MeshCoreBot instance containing the service.
        """
        self.bot = bot
        self.logger = bot.logger
        self.enabled = True
        self._running = False
    
    @abstractmethod
    async def start(self) -> None:
        """Start the service.
        
        This method should:
        - Setup event handlers if needed
        - Start background tasks
        - Initialize any required resources
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the service.
        
        This method should:
        - Clean up event handlers
        - Stop background tasks
        - Close any open resources
        """
        pass
    
    def get_metadata(self) -> Dict[str, Any]:
        """Get service metadata.
        
        Returns:
            Dict[str, Any]: Dictionary containing service metadata (name, status, etc.).
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
        """Derive service name from class name.
        
        Returns:
            str: Derived service name (e.g., 'PacketCaptureService' -> 'packetcapture').
        """
        class_name = self.__class__.__name__
        if class_name.endswith('Service'):
            return class_name[:-7].lower()  # Remove 'Service' suffix and lowercase
        return class_name.lower()
    
    def _derive_config_section(self) -> str:
        """Derive config section name from class name.
        
        Returns:
            str: Derived config section name.
        """
        if self.config_section:
            return self.config_section
        
        class_name = self.__class__.__name__
        if class_name.endswith('Service'):
            return class_name[:-7]  # Remove 'Service' suffix
        return class_name
    
    def is_running(self) -> bool:
        """Check if the service is currently running.
        
        Returns:
            bool: True if the service is running, False otherwise.
        """
        return self._running

    def is_healthy(self) -> bool:
        """Report whether the service is healthy. Default: healthy if running.
        Override in subclasses for connection-specific checks (e.g. meshcore, MQTT).
        """
        return self._running

