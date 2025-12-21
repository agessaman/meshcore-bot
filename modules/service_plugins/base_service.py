#!/usr/bin/env python3
"""
Base service plugin class for background services
"""

from abc import ABC, abstractmethod
from typing import Dict, Any


class BaseServicePlugin(ABC):
    """Base class for background service plugins"""
    
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
            'name': self.__class__.__name__,
            'enabled': self.enabled,
            'running': self._running
        }
    
    def is_running(self) -> bool:
        """Check if the service is currently running"""
        return self._running

