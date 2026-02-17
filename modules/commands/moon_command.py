#!/usr/bin/env python3
"""
Moon Command - Provides moon phase and position information
"""

from .base_command import BaseCommand
from ..solar_conditions import get_moon
from ..models import MeshMessage


class MoonCommand(BaseCommand):
    """Command to get moon information"""
    
    # Plugin metadata
    name = "moon"
    keywords = ['moon']
    description = "Get moon phase, rise/set times and position"
    category = "solar"
    
    # Documentation
    short_description = "Get moon phase and rise/set times"
    usage = "moon"
    examples = ["moon"]
    
    def __init__(self, bot):
        """Initialize the moon command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.moon_enabled = self.get_config_value('Moon_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.moon_enabled:
            return False
        return super().can_execute(message)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the moon command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Get moon information using default location
            moon_info = get_moon()
            
            # Format the response to be more compact and readable
            response = self._format_moon_response(moon_info)
            
            # Use the unified send_response method
            await self.send_response(message, response)
            return True
            
        except Exception as e:
            error_msg = self.translate('commands.moon.error', error=str(e))
            await self.send_response(message, error_msg)
            return False
    
    def _translate_phase_name(self, phase_name: str) -> str:
        """Translate English phase name to localized version.
        
        Args:
            phase_name: The English phase name (e.g., 'New Moon').
            
        Returns:
            str: The translated phase name, or original if not found.
        """
        # Map English phase names (with or without emoji) to translation keys
        phase_mapping = {
            'New Moon': 'new_moon',
            'Waxing Crescent': 'waxing_crescent',
            'First Quarter': 'first_quarter',
            'Waxing Gibbous': 'waxing_gibbous',
            'Full Moon': 'full_moon',
            'Waning Gibbous': 'waning_gibbous',
            'Last Quarter': 'last_quarter',
            'Waning Crescent': 'waning_crescent'
        }
        
        # Remove emoji if present (they're in the translation)
        phase_clean = phase_name
        for emoji in ['🌑', '🌒', '🌓', '🌔', '🌕', '🌖', '🌗', '🌘']:
            phase_clean = phase_clean.replace(emoji, '').strip()
        
        # Find matching translation key
        for english_name, translation_key in phase_mapping.items():
            if english_name in phase_clean:
                translated = self.translate(f'commands.moon.phases.{translation_key}')
                # If translation found (not just the key), return it
                if translated != f'commands.moon.phases.{translation_key}':
                    return translated
        
        # Fallback: return original if no translation found
        return phase_name
    
    def _format_moon_response(self, moon_info: str) -> str:
        """Format moon information to be more compact and readable.
        
        Args:
            moon_info: The raw moon info string.
            
        Returns:
            str: The formatted response string.
        """
        try:
            # Parse the moon info string to extract key information
            lines = moon_info.split('\n')
            moon_data = {}
            
            for line in lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    moon_data[key.strip()] = value.strip()
            
            # Create a more compact format while keeping essential details
            if 'MoonRise' in moon_data and 'Set' in moon_data and 'Phase' in moon_data:
                # Keep day info but make it more compact
                rise_info = moon_data['MoonRise']  # e.g., "Thu 04 06:47PM"
                set_info = moon_data['Set']        # e.g., "Fri 05 03:43AM"
                
                # Extract phase and illumination
                phase_info = moon_data.get('Phase', self.translate('commands.moon.unknown_phase'))
                if '@:' in phase_info:
                    phase, illum = phase_info.split('@:')
                    phase = phase.strip()
                    illum = illum.strip()
                    # Translate the phase name
                    phase = self._translate_phase_name(phase)
                else:
                    phase = phase_info
                    phase = self._translate_phase_name(phase)
                    illum = self.translate('commands.moon.unknown_illum')
                
                # Add next full and new moon dates (compact format)
                if 'FullMoon' in moon_data and 'NewMoon' in moon_data:
                    full_moon = moon_data['FullMoon']  # e.g., "Sun Sep 07 11:08AM"
                    new_moon = moon_data['NewMoon']    # e.g., "Sun Sep 21 12:54PM"
                    
                    # Extract just the essential date/time parts
                    full_parts = full_moon.split()
                    new_parts = new_moon.split()
                    
                    if len(full_parts) >= 3 and len(new_parts) >= 3:
                        # Format: "Sep 07 11:08AM" and "Sep 21 12:54PM"
                        full_compact = f"{full_parts[1]} {full_parts[2]} {full_parts[3]}"
                        new_compact = f"{new_parts[1]} {new_parts[2]} {new_parts[3]}"
                        # Create compact response with full/new moon dates
                        return self.translate('commands.moon.format_with_dates', 
                                            phase=phase, illum=illum, 
                                            rise=rise_info, set=set_info,
                                            full=full_compact, new=new_compact)
                
                # Create compact response without full/new moon dates
                return self.translate('commands.moon.format', 
                                    phase=phase, illum=illum, 
                                    rise=rise_info, set=set_info)
            else:
                # Fallback to original format if parsing fails
                return self.translate('commands.moon.fallback', info=moon_info)
                
        except Exception as e:
            # Fallback to original format if formatting fails
            return self.translate('commands.moon.fallback', info=moon_info)
    
    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text for this command.
        """
        return self.description
